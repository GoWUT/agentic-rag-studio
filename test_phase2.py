"""Offline acceptance and regression tests for the research workspace."""
from collections import defaultdict
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pypdf import PdfWriter

from server.agent.context_harness import ContextHarness
from server.agent.evidence import Citation, Evidence, evidence_from_hit, render_citations, validate_citations
from server.agent.graph import build_agent
from server.agent.schemas import TaskPlan
from server.rag.ingestion import DocumentIngestionPipeline
from server.rag.workspace_retrieval import WorkspaceRetriever
from server.sessions import AgentSessionManager, SessionStore
from server.workspaces import DocumentRecord, WorkspaceNotFoundError, WorkspaceStore


def pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def document(workspace_id: str, identity: str) -> DocumentRecord:
    return DocumentRecord(id=identity, workspace_id=workspace_id, filename=identity + ".pdf",
                          display_name=identity + ".pdf", fingerprint=identity, index_id=identity,
                          status="ready", page_count=2, created_at=identity, metadata={})


class IndexAdapter:
    index_suffix = ""
    def __init__(self):
        self.builds = 0
    def exists(self, directory):
        return (directory / "ready").exists()
    def build(self, pdf_path, directory):
        self.builds += 1
        directory.mkdir()
        (directory / "ready").write_text("ready")
    def load(self, directory):
        return Mock()


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = WorkspaceStore(self.path / "sessions.sqlite3", max_documents=2)
        self.workspace = self.store.create("Research")

    def test_crud_and_safe_shared_index_retention(self):
        record = document(self.workspace.id, "a")
        self.store.register(record)
        self.assertEqual(self.store.get(self.workspace.id).name, "Research")
        self.assertEqual(len(self.store.list()), 1)
        self.store.delete_document(self.workspace.id, "a")
        self.assertEqual(self.store.documents(self.workspace.id), [])
        self.store.delete(self.workspace.id)
        self.assertEqual(self.store.list(), [])

    def test_registration_is_idempotent_and_limited(self):
        record = document(self.workspace.id, "a")
        self.assertEqual(self.store.register(record), self.store.register(record))
        self.store.register(document(self.workspace.id, "b"))
        with self.assertRaises(ValueError):
            self.store.register(document(self.workspace.id, "c"))

    def test_workspace_documents_survive_restart(self):
        self.store.register(document(self.workspace.id, "a"))
        restored = WorkspaceStore(self.store.path)
        self.assertEqual(restored.documents(self.workspace.id)[0].id, "a")

    def test_missing_workspace_and_document_are_rejected(self):
        with self.assertRaises(WorkspaceNotFoundError):
            self.store.documents("missing")
        with self.assertRaises(WorkspaceNotFoundError):
            self.store.delete_document(self.workspace.id, "missing")

    def test_same_pdf_reuses_ingestion_index_across_workspaces(self):
        adapter = IndexAdapter()
        pipeline = DocumentIngestionPipeline(workspace=self.path, max_upload_bytes=100000, index_adapter=adapter)
        manager = AgentSessionManager({"WORKSPACE_DIR": self.path, "EMBEDDING_MODEL": "test"}, ingestion_pipeline=pipeline)
        first = manager.workspaces.create("First")
        second = manager.workspaces.create("Second")
        a = manager.add_workspace_document(first.id, "a.pdf", BytesIO(pdf_bytes()))
        b = manager.add_workspace_document(second.id, "b.pdf", BytesIO(pdf_bytes()))
        self.assertFalse(a["index_reused"])
        self.assertTrue(b["index_reused"])
        self.assertEqual(a["index_id"], b["index_id"])
        self.assertEqual(adapter.builds, 1)
        manager.workspaces.delete(first.id)
        self.assertTrue(Path(b["index_id"]).exists())

    def test_workspace_session_restores_without_single_pdf(self):
        pipeline = Mock()
        manager = AgentSessionManager({"WORKSPACE_DIR": self.path, "EMBEDDING_MODEL": "test"}, ingestion_pipeline=pipeline)
        workspace = manager.workspaces.create("Research")
        session = manager.create_workspace_session(workspace.id)
        manager.store.save_messages(session["session_id"], [HumanMessage(content="question"), AIMessage(content="answer")])
        restored = AgentSessionManager(manager.config, ingestion_pipeline=pipeline)
        restored._make_runtime = Mock(return_value={"messages": []})
        restored._get_or_restore_runtime(session["session_id"])
        self.assertEqual(restored._make_runtime.call_args.kwargs["workspace_id"], workspace.id)
        self.assertEqual(restored.get_history(session["session_id"])[-1]["content"], "answer")

    def test_safe_session_migration_preserves_old_rows(self):
        import sqlite3
        db = self.path / "legacy.sqlite3"
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE sessions(session_id TEXT PRIMARY KEY, file_id TEXT, file_name TEXT, pdf_path TEXT, chroma_dir TEXT, messages_json TEXT, created_at TEXT, updated_at TEXT)")
            connection.execute("INSERT INTO sessions VALUES ('old','file','a.pdf','a.pdf','index','[]','now','now')")
        connection.close()
        restored = SessionStore(db)
        self.assertIsNone(restored.get("old").workspace_id)
        self.assertEqual(restored.get("old").file_name, "a.pdf")

    def retriever(self, reranker=None, loader=None):
        config = {"RERANKER_ENABLED": False, "WORKSPACE_RETRIEVAL_GLOBAL_K": 3,
                  "WORKSPACE_RETRIEVAL_PER_DOC_K": 2, "WORKSPACE_RETRIEVAL_TIMEOUT_SECONDS": .1}
        loader = loader or (lambda record: Mock())
        retriever = WorkspaceRetriever(self.store, self.workspace.id, config, loader, reranker)
        self.addCleanup(retriever._executor.shutdown, wait=True)
        return retriever

    def test_empty_workspace_returns_no_evidence(self):
        self.assertEqual(self.retriever().invoke("query"), [])

    def test_multi_document_candidates_use_one_global_reranker(self):
        for identity in ("a", "b"):
            self.store.register(document(self.workspace.id, identity))
        reranker = Mock()
        reranker.rerank.side_effect = lambda query, hits: hits[::-1]
        with patch("server.rag.workspace_retrieval.build_retriever", side_effect=lambda *_: Mock(invoke=Mock(return_value=[Document(page_content="method", metadata={"page": 0, "total_pages": 2})]))) as build:
            hits = self.retriever(reranker).invoke("methods")
        self.assertEqual({hit.metadata["document_id"] for hit in hits}, {"a", "b"})
        self.assertEqual(reranker.rerank.call_count, 1)
        self.assertTrue(all(call.args[1]["RERANKER_ENABLED"] is False for call in build.call_args_list))
        self.assertTrue(all("chunk_id" in hit.metadata for hit in hits))

    def test_failed_document_and_reranker_do_not_abort_workspace(self):
        self.store.register(document(self.workspace.id, "a"))
        self.store.register(document(self.workspace.id, "b"))
        def loader(record):
            if record.id == "a":
                raise FileNotFoundError("missing index")
            return Mock()
        reranker = Mock()
        reranker.rerank.side_effect = RuntimeError("model unavailable")
        with patch("server.rag.workspace_retrieval.build_retriever", return_value=Mock(invoke=Mock(return_value=[Document(page_content="method", metadata={"page": 0})]))):
            with self.assertLogs("server.rag.workspace_retrieval", level="WARNING"):
                hits = self.retriever(reranker, loader).invoke("query")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].metadata["document_id"], "b")


class ScriptedModel:
    script: dict = {}
    answer = "Answer"
    calls: dict = defaultdict(int)

    def __init__(self, **kwargs):
        self.outputs = {key: list(values) for key, values in self.script.items()}
        type(self).calls = defaultdict(int)

    def with_structured_output(self, schema, **kwargs):
        def invoke(messages):
            type(self).calls[schema.__name__] += 1
            values = self.outputs.get(schema.__name__, [])
            if not values:
                raise ValueError("No scripted structured output: " + schema.__name__)
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return schema.model_validate(value)
        return Mock(invoke=invoke)

    def invoke(self, messages):
        return AIMessage(content=type(self).answer)


def sample_evidence(identity="ev_real", document_id="a"):
    return Evidence(evidence_id=identity, source_type="workspace", document_id=document_id,
                    document_name=document_id + ".pdf", page=1, page_count=2, chunk_id="chunk",
                    content="The paper proposes a convolutional method.")


class WorkflowTests(unittest.TestCase):
    def run_graph(self, query="What is the title of paper_a?", *, enough=True, scope="workspace_only",
                  config=None, script=None, answer="Supported result [ev_real]", empty=False, local_source="workspace"):
        self.tool_calls = []
        def local(query: str):
            self.tool_calls.append("search_" + local_source)
            return {"evidence": [] if empty else [sample_evidence().model_copy(update={"source_type": local_source}).model_dump()]}
        def external(query: str):
            self.tool_calls.append("search_web")
            return {"evidence": [Evidence(evidence_id="ev_web", source_type="web", title="source", url="https://example.com", content="update").model_dump()]}
        tools = [StructuredTool.from_function(local, name="search_" + local_source, description="Search documents"),
                 StructuredTool.from_function(external, name="search_web", description="Search web")]
        outputs = {"QueryAnalysis": [{"standalone_query": query, "query_type": "document", "needs_retrieval": True, "selected_sources": ["pdf"]}],
                   "RetrievalGrade": [{"relevant": enough, "sufficient": enough, "confidence": .9, "reason": "missing experimental evidence"}] * 10,
                   "QueryRefinement": [{"refined_query": "better query", "sources": ["web"]}] * 2,
                   "VerificationResult": [{"grounded": True, "unsupported_claims": [], "confidence": .9}] * 2}
        outputs.update(script or {})
        ScriptedModel.script, ScriptedModel.answer = outputs, answer
        harness = ContextHarness(context_window_tokens=20000, input_budget_tokens=10000, max_output_tokens=512,
                                 safety_tokens=512, summary_tokens=256, recent_turns=4)
        settings = {"PLANNER_MAX_REPLAN": 0, **(config or {})}
        self.node_names = []
        with patch("server.agent.graph.ChatOpenAI", ScriptedModel):
            graph = build_agent("model", "key", tools, context_harness=harness, max_output_tokens=512,
                                request_timeout_seconds=10, workflow_config=settings)
            inputs = {"messages": [HumanMessage(content=query)], "workspace_id": "workspace", "source_scope": scope, "document_registry": {"a": 2}}
            for event in graph.stream(inputs, {"recursion_limit": settings.get("AGENT_MAX_GRAPH_STEPS", 33)}, stream_mode=["updates", "values"]):
                kind, value = event
                if kind == "updates":
                    self.node_names.extend(value)
                else:
                    result = value
            return result

    def test_simple_question_bypasses_planner(self):
        result = self.run_graph()
        self.assertEqual(ScriptedModel.calls["TaskPlan"], 0)
        self.assertEqual(result["task_complexity"], "simple")
        self.assertEqual(result["citations"][0]["evidence_id"], "ev_real")
        self.assertIn("[a.pdf, p.1]", result["final_answer"])

    def test_legacy_single_pdf_uses_new_workflow_and_citations(self):
        result = self.run_graph(local_source="pdf")
        self.assertEqual(self.tool_calls, ["search_pdf"])
        self.assertEqual(result["evidence"][0]["source_type"], "pdf")
        self.assertIn("[a.pdf, p.1]", result["final_answer"])

    def test_complex_question_enters_planner_and_executes_steps(self):
        script = {"TaskPlan": [{"goal": "Compare", "steps": [{"id": "one", "description": "Retrieve methods", "query": "methods", "sources": ["workspace"]},
                    {"id": "two", "description": "Retrieve metrics", "query": "metrics", "sources": ["workspace"]}]}]}
        result = self.run_graph("Compare the methods and performance of papers", script=script)
        self.assertEqual(ScriptedModel.calls["TaskPlan"], 1)
        self.assertEqual(result["retrieval_attempts"], 2)
        self.assertTrue(all(step["status"] == "completed" for step in result["task_plan"]["steps"]))

    def test_planner_steps_are_clamped_to_config(self):
        steps = [{"id": str(i), "description": "Get evidence", "query": str(i), "sources": ["workspace"]} for i in range(10)]
        result = self.run_graph("Compare papers", script={"TaskPlan": [{"goal": "Compare", "steps": steps}]}, config={"PLANNER_MAX_STEPS": 2})
        self.assertEqual(len(result["task_plan"]["steps"]), 2)

    def test_planner_failure_falls_back(self):
        with self.assertLogs("server.agent.research_workflow", level="WARNING"):
            result = self.run_graph("Compare papers", script={"TaskPlan": [ValueError("invalid JSON")]})
        self.assertEqual(result["retrieval_attempts"], 1)

    def test_insufficient_evidence_rewrites_at_most_twice(self):
        result = self.run_graph(enough=False)
        self.assertEqual(result["rewrite_count"], 2)
        self.assertEqual(result["retrieval_attempts"], 3)
        self.assertEqual(ScriptedModel.calls["RetrievalGrade"], 3)

    def test_sufficient_evidence_does_not_rewrite(self):
        result = self.run_graph()
        self.assertEqual(result["rewrite_count"], 0)
        self.assertEqual(ScriptedModel.calls["QueryRefinement"], 0)

    def test_workspace_only_blocks_model_selected_external_search(self):
        self.run_graph(enough=False)
        self.assertNotIn("search_web", self.tool_calls)

    def test_user_document_only_instruction_overrides_broader_scope(self):
        result = self.run_graph("According only to uploaded documents, what are the latest developments?", enough=False, scope="workspace_and_external")
        self.assertEqual(result["source_scope"], "workspace_only")
        self.assertNotIn("search_web", self.tool_calls)

    def test_external_fallback_after_local_rewrites(self):
        result = self.run_graph("latest developments", enough=False, scope="workspace_and_external")
        self.assertEqual(self.tool_calls, ["search_workspace"] * 3 + ["search_web"])
        self.assertTrue(result["external_fallback_done"])

    def test_external_only_never_searches_workspace(self):
        result = self.run_graph(scope="external", script={"QueryAnalysis": [{"standalone_query": "latest developments", "query_type": "web", "needs_retrieval": True, "selected_sources": ["web"]}]}, answer="Update [ev_web]")
        self.assertEqual(self.tool_calls, ["search_web"])
        self.assertIn("[Web 1]", result["final_answer"])

    def test_iteration_budget_terminates_loop(self):
        result = self.run_graph(enough=False, scope="workspace_and_external", config={"PLANNER_MAX_ITERATIONS": 2})
        self.assertEqual(result["retrieval_attempts"], 2)
        self.assertEqual(len(self.tool_calls), 2)

    def test_grounding_failure_revises_once_and_replaces_history_message(self):
        script = {"VerificationResult": [{"grounded": False, "unsupported_claims": ["unsupported result"], "confidence": .1}] * 2}
        result = self.run_graph(script=script)
        self.assertEqual(result["revision_count"], 1)
        self.assertEqual(ScriptedModel.calls["VerificationResult"], 2)
        self.assertEqual(len(result["messages"]), 2)

    def test_invalid_citation_not_returned(self):
        result = self.run_graph(answer="Result [ev_missing] [99] [fake.pdf, p.19]")
        self.assertEqual(result["citations"], [])
        self.assertNotIn("ev_missing", result["final_answer"])
        self.assertNotIn("p.19", result["final_answer"])

    def test_grading_parse_failure_is_bounded_and_uncertain(self):
        with self.assertLogs("server.agent.research_workflow", level="WARNING"):
            result = self.run_graph(script={"RetrievalGrade": [ValueError("invalid JSON")]})
        self.assertTrue(result["grading_failed"])
        self.assertFalse(result["evidence_sufficient"])
        self.assertEqual(result["retrieval_attempts"], 1)

    def test_empty_results_cannot_be_sufficient(self):
        result = self.run_graph(empty=True)
        self.assertFalse(result["evidence_sufficient"])

    def test_research_rewriter_cannot_jump_to_web_before_fallback(self):
        script = {"QueryAnalysis": [{"standalone_query": "Compare recent methods", "query_type": "research", "needs_retrieval": True, "selected_sources": ["workspace", "web"]}],
                  "TaskPlan": [{"goal": "Compare", "steps": [{"id": "step", "description": "Retrieve", "query": "methods", "sources": ["workspace"]}]}]}
        result = self.run_graph("Compare recent methods", enough=False, scope="workspace_and_external", script=script)
        self.assertEqual(self.tool_calls, ["search_workspace"] * 3 + ["search_web"])
        self.assertEqual(result["rewrite_count"], 2)

    def test_replan_budget_is_shared_with_executor_budget(self):
        plan = {"goal": "Compare", "steps": [{"id": "step", "description": "Retrieve methods", "query": "methods", "sources": ["workspace"]}]}
        result = self.run_graph("Compare papers", enough=False, script={"TaskPlan": [plan, plan]}, config={"PLANNER_MAX_REPLAN": 1})
        self.assertEqual(result["replan_count"], 1)
        self.assertEqual(result["retrieval_attempts"], 4)

    def test_academic_fallback_selects_arxiv(self):
        from server.agent.research_workflow import ResearchWorkflowNodes
        nodes = object.__new__(ResearchWorkflowNodes)
        nodes.available_sources = ("workspace", "web", "arxiv")
        result = nodes.external_fallback({"original_query": "Find recent research papers", "standalone_query": "papers", "source_scope": "workspace_and_external"})
        self.assertEqual(result["plan"][0]["sources"], ["arxiv"])

    def test_maximum_default_path_is_32_nodes_and_fits_33_budget(self):
        def plan(count):
            return {"goal": "Compare", "steps": [{"id": str(index), "description": "Retrieve", "query": str(index), "sources": ["workspace"]} for index in range(count)]}
        result = self.run_graph("Compare methods", enough=False, scope="workspace_and_external",
            config={"PLANNER_MAX_REPLAN": 1, "AGENT_MAX_GRAPH_STEPS": 33},
            script={"TaskPlan": [plan(3), plan(2)], "VerificationResult": [{"grounded": False, "unsupported_claims": ["unsupported"], "confidence": .1}] * 2})
        self.assertEqual(result["retrieval_attempts"], 8)
        self.assertEqual(result["rewrite_count"], 2)
        self.assertEqual(result["replan_count"], 1)
        self.assertTrue(result["external_fallback_done"])
        self.assertEqual(len(self.node_names), 32)
        self.assertTrue(all(step["status"] == "completed" for step in result["task_plan"]["steps"]))

    def test_six_step_plan_reserves_external_fallback_capacity(self):
        plan = {"goal": "Compare", "steps": [{"id": str(index), "description": "Retrieve", "query": str(index), "sources": ["workspace"]} for index in range(6)]}
        result = self.run_graph("Compare methods", enough=False, scope="workspace_and_external", script={"TaskPlan": [plan]})
        self.assertEqual(result["retrieval_attempts"], 8)
        self.assertTrue(result["external_fallback_done"])
        self.assertEqual(self.tool_calls[-1], "search_web")

    def test_direct_conceptual_comparison_does_not_override_analyzer(self):
        result = self.run_graph("Compare RAG and fine-tuning conceptually", script={"QueryAnalysis": [{"standalone_query": "Compare RAG and fine-tuning", "query_type": "direct", "needs_retrieval": False, "selected_sources": []}]}, answer="Conceptual answer [ev_fake]")
        self.assertEqual(self.tool_calls, [])
        self.assertEqual(ScriptedModel.calls["TaskPlan"], 0)
        self.assertEqual(len(self.node_names), 4)
        self.assertNotIn("ev_fake", result["final_answer"])

    def test_source_expansion_in_rewriter_does_not_authorize_replanner(self):
        plan = {"goal": "Compare", "steps": [{"id": "s", "description": "Retrieve", "query": "methods", "sources": ["workspace", "web"]}]}
        result = self.run_graph("Compare methods", enough=False, scope="workspace_and_external", config={"PLANNER_MAX_REPLAN": 1},
            script={"QueryAnalysis": [{"standalone_query": "Compare", "query_type": "research", "needs_retrieval": True, "selected_sources": ["workspace"]}], "TaskPlan": [plan, plan]})
        self.assertEqual(self.tool_calls, ["search_workspace"] * 4 + ["search_web"])

    def test_evidence_lifecycle_rejects_previous_turn_identity(self):
        first = self.run_graph()
        self.assertEqual(first["citations"][0]["evidence_id"], "ev_real")
        # Preserve a completed state and conversation, then make the next tool return a different identity.
        config = {"PLANNER_MAX_REPLAN": 0, "GROUNDING_CHECK_ENABLED": False}
        ScriptedModel.script = {"QueryAnalysis": [{"standalone_query": "new query", "query_type": "document", "needs_retrieval": True, "selected_sources": ["workspace"]}], "RetrievalGrade": [{"relevant": True, "sufficient": True, "confidence": 1, "reason": "supported"}]}
        ScriptedModel.answer = "New claim citing previous turn [ev_real]"
        def new_tool(query: str):
            return {"evidence": [sample_evidence("ev_new").model_dump()]}
        harness = ContextHarness(context_window_tokens=20000, input_budget_tokens=10000, max_output_tokens=512, safety_tokens=512, summary_tokens=256, recent_turns=4)
        with patch("server.agent.graph.ChatOpenAI", ScriptedModel):
            graph = build_agent("model", "key", [StructuredTool.from_function(new_tool, name="search_workspace", description="Search")], context_harness=harness, max_output_tokens=512, request_timeout_seconds=10, workflow_config=config)
            second = graph.invoke({**first, "messages": [*first["messages"], HumanMessage(content="new query")]}, {"recursion_limit": 33})
        self.assertEqual([item["evidence_id"] for item in second["evidence"]], ["ev_new"])
        self.assertEqual(second["citations"], [])
        self.assertNotIn("ev_real", second["final_answer"])


class CitationTests(unittest.TestCase):
    def test_evidence_preserves_source_and_converts_page(self):
        evidence = evidence_from_hit(Document(page_content="method", metadata={"page": 0, "source_type": "workspace", "document_id": "a", "document_name": "a.pdf", "total_pages": 2, "extraction_method": "paddleocr"}))
        self.assertEqual(evidence.page, 1)
        self.assertEqual(evidence.extraction_method, "paddleocr")

    def test_forged_metadata_and_deleted_documents_rejected(self):
        evidence = sample_evidence()
        citation = Citation(citation_id="1", evidence_id=evidence.evidence_id, document_name="a.pdf", page=19)
        self.assertEqual(validate_citations([citation], [evidence]), [])
        citation.page = 1
        self.assertEqual(validate_citations([citation], [evidence], {}), [])

    def test_previous_turn_evidence_is_rejected(self):
        citation = Citation(citation_id="1", evidence_id="ev_old", document_name="a.pdf", page=1)
        self.assertEqual(validate_citations([citation], [sample_evidence()]), [])

    def test_out_of_range_pages_rejected(self):
        evidence = sample_evidence().model_copy(update={"page": 9})
        answer, citations = render_citations("Fact [ev_real]", [evidence])
        self.assertEqual(citations, [])
        self.assertNotIn("p.9", answer)

    def test_task_plan_requires_actionable_steps(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TaskPlan.model_validate({"goal": "test", "steps": []})

    def test_invented_table_and_url_removed(self):
        answer, citations = render_citations("Table IV is at https://invented.test [ev_real]", [sample_evidence()])
        self.assertNotIn("Table IV", answer)
        self.assertNotIn("invented.test", answer)
        self.assertEqual(len(citations), 1)

    def test_arxiv_tool_preserves_actual_entry_id_metadata(self):
        from server.agent.tools import make_arxiv_tool
        with patch("server.agent.tools.ArxivRetriever") as retriever:
            retriever.return_value.invoke.return_value = [Document(page_content="abstract", metadata={"Title": "Research", "Entry ID": "https://arxiv.org/abs/1234.5678"})]
            result = make_arxiv_tool(structured=True).invoke({"query": "research"})
        self.assertEqual(result["evidence"][0]["url"], "https://arxiv.org/abs/1234.5678")


class AgentEvaluationTests(unittest.TestCase):
    def test_synthetic_three_document_acceptance_and_simple_bypass(self):
        from evaluation.benchmark_agent_workspace import DEFAULT_CASES, demo_response, evaluate
        import json
        cases = [json.loads(line) for line in DEFAULT_CASES.read_text().splitlines()]
        responses = {case["id"]: demo_response(case) for case in cases}
        compare = responses["compare"]
        self.assertEqual({item["document_id"] for item in compare["evidence"]}, {"a", "b", "c"})
        self.assertEqual(len(compare["citations"]), 3)
        self.assertIsNone(responses["title"]["plan"])
        metrics = evaluate(cases, responses)["metrics"]
        self.assertEqual(metrics["citation_validity"], 1)
        self.assertEqual(metrics["retrieval_success"], 1)

    def test_missing_evaluation_response_is_failure(self):
        from evaluation.benchmark_agent_workspace import evaluate
        case = {"id": "missing", "expected_tools": ["search_workspace"], "expected_documents": ["a"], "expected_complexity": "simple"}
        metrics = evaluate([case], {})["metrics"]
        self.assertEqual(metrics["planner_success"], 0)
        self.assertEqual(metrics["citation_validity"], 0)


class WorkspaceAPITests(unittest.TestCase):
    def test_rest_crud_multi_upload_and_session_schema(self):
        import server.main as main
        with tempfile.TemporaryDirectory() as directory:
            adapter = IndexAdapter()
            pipeline = DocumentIngestionPipeline(workspace=directory, max_upload_bytes=100000, index_adapter=adapter)
            manager = AgentSessionManager({"WORKSPACE_DIR": directory, "EMBEDDING_MODEL": "test"}, ingestion_pipeline=pipeline)
            with patch.object(main, "SESSION_MANAGER", manager):
                client = TestClient(main.app)
                created = client.post("/workspaces", json={"name": "Research"})
                self.assertEqual(created.status_code, 200)
                identity = created.json()["id"]
                self.assertEqual(client.get("/workspaces").json()[0]["id"], identity)
                uploaded = client.post(f"/workspaces/{identity}/documents", files=[("files", ("a.pdf", pdf_bytes(), "application/pdf")), ("files", ("bad.pdf", b"invalid", "application/pdf"))]).json()
                self.assertEqual([item["status"] for item in uploaded["documents"]], ["ready", "failed"])
                docs = client.get(f"/workspaces/{identity}/documents").json()
                session = client.post(f"/workspaces/{identity}/sessions").json()
                self.assertEqual(session["workspace_id"], identity)
                self.assertEqual(client.delete(f"/workspaces/{identity}/documents/{docs[0]['id']}").status_code, 204)
                self.assertEqual(client.delete(f"/workspaces/{identity}").status_code, 204)
                self.assertEqual(client.get(f"/workspaces/{identity}").status_code, 404)

    def test_chat_schema_keeps_legacy_fields_and_validates_scope(self):
        from server.main import ChatRequest
        from pydantic import ValidationError
        request = ChatRequest(session_id="session", message="question")
        self.assertIsNone(request.workspace_id)
        with self.assertRaises(ValidationError):
            ChatRequest(session_id="session", message="question", source_scope="invalid")


class WorkspaceUITests(unittest.TestCase):
    def test_workspace_sidebar_and_restored_sources_render(self):
        from streamlit.testing.v1 import AppTest
        def response(payload):
            mock = Mock(status_code=200)
            mock.json.return_value = payload
            return mock
        def get(url, **kwargs):
            if url.endswith("/sessions"):
                return response({"sessions": [{"session_id": "session", "file_id": "", "file_name": "Research", "workspace_id": "workspace", "turn_count": 1}]})
            if url.endswith("/messages"):
                return response({"messages": [{"role": "assistant", "content": "Supported method", "research": {"evidence": [sample_evidence().model_dump()], "plan": {"steps": [{"description": "Retrieve methods", "status": "completed"}]}}}]})
            if url.endswith("/workspaces"):
                return response([{"id": "workspace", "name": "Research"}])
            if url.endswith("/documents"):
                return response([{"id": "a", "display_name": "a.pdf", "status": "ready", "page_count": 2}])
            raise AssertionError(url)
        with patch("requests.get", side_effect=get):
            app = AppTest.from_file("client/app.py", default_timeout=10).run()
            app.radio[0].set_value("Workspace").run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(any(expander.label == "Sources / Evidence" for expander in app.expander))
        self.assertTrue(any(expander.label == "Task Plan" for expander in app.expander))
        self.assertEqual(app.session_state["workspace_id"], "workspace")


if __name__ == "__main__":
    unittest.main()
