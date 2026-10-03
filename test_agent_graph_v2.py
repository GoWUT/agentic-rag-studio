import json
import unittest
from collections import defaultdict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from server.agent.context_harness import ContextHarness
from server.agent.graph import build_agent
from server.agent.schemas import (
    EvidenceGrade,
    QueryAnalysis,
    QueryRefinement,
    ResearchPlan,
)


class StructuredScript:
    def __init__(self, model, schema):
        self.model = model
        self.schema = schema

    def invoke(self, messages):
        self.model.structured_messages[self.schema.__name__].append(list(messages))
        value = self.model.outputs[self.schema.__name__].pop(0)
        if isinstance(value, Exception):
            return {"raw": AIMessage(content="bad"), "parsed": None, "parsing_error": value}
        parsed = value if isinstance(value, self.schema) else self.schema.model_validate(value)
        return {"raw": AIMessage(content="json"), "parsed": parsed, "parsing_error": None}


class ScriptedChatModel:
    instances = []
    next_outputs = {}
    next_answer = "final answer"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.outputs = {key: list(value) for key, value in self.next_outputs.items()}
        self.answer = type(self).next_answer
        self.structured_messages = defaultdict(list)
        self.final_messages = None
        self.__class__.instances.append(self)

    def with_structured_output(self, schema, **kwargs):
        self.structured_options = kwargs
        return StructuredScript(self, schema)

    def invoke(self, messages):
        self.final_messages = list(messages)
        if callable(self.answer):
            return AIMessage(content=self.answer(messages))
        return AIMessage(content=self.answer)


class ToolProbe:
    def __init__(self, results=None):
        self.calls = defaultdict(list)
        self.results = results or {}

    def tools(self, *sources):
        tools = []
        for source in sources:
            name = {"pdf": "search_pdf", "web": "search_web", "arxiv": "search_arxiv"}[source]

            def run(query: str, *, _source=source):
                self.calls[_source].append(query)
                result = self.results.get(_source, f"{_source} evidence for {query}")
                if isinstance(result, Exception):
                    raise result
                return result

            tools.append(StructuredTool.from_function(
                func=run,
                name=name,
                description=f"Search {source}",
            ))
        return tools


class AgentGraphV2Test(unittest.TestCase):
    def setUp(self):
        ScriptedChatModel.instances.clear()
        self.harness = ContextHarness(
            context_window_tokens=4096,
            input_budget_tokens=2048,
            max_output_tokens=512,
            safety_tokens=256,
            summary_tokens=256,
            recent_turns=4,
        )

    def build(self, tools, outputs, answer="final answer", retries=1):
        ScriptedChatModel.next_outputs = outputs
        ScriptedChatModel.next_answer = answer
        from unittest.mock import patch
        patcher = patch("server.agent.graph.ChatOpenAI", ScriptedChatModel)
        patcher.start()
        self.addCleanup(patcher.stop)
        return build_agent(
            "model", "key", tools,
            context_harness=self.harness,
            max_output_tokens=512,
            request_timeout_seconds=30,
            max_retrieval_retries=retries,
        )

    def invoke(self, agent, messages):
        return agent.invoke({"messages": messages}, {"recursion_limit": 20})

    def test_direct_question_skips_all_retrieval_nodes(self):
        probe = ToolProbe()
        agent = self.build(probe.tools("pdf", "web", "arxiv"), {
            "QueryAnalysis": [{
                "standalone_query": "你好，你能做什么？",
                "query_type": "direct",
                "needs_retrieval": False,
                "selected_sources": [],
            }],
        }, answer="你好，我可以帮助你分析 PDF。")

        result = self.invoke(agent, [HumanMessage(content="你好，你能做什么？")])

        self.assertEqual(result["query_type"], "direct")
        self.assertEqual(sum(map(len, probe.calls.values())), 0)
        self.assertEqual(result["messages"][-1].content, "你好，我可以帮助你分析 PDF。")
        self.assertIsNotNone(self.harness.last_report)

    def test_document_question_uses_pdf_only(self):
        probe = ToolProbe()
        agent = self.build(probe.tools("pdf", "web", "arxiv"), {
            "QueryAnalysis": [{
                "standalone_query": "这篇论文提出的核心方法是什么？",
                "query_type": "document",
                "needs_retrieval": True,
                "selected_sources": ["pdf"],
            }],
            "ResearchPlan": [{"steps": [{
                "query": "wrong external plan",
                "sources": ["web", "arxiv"],
            }]}],
            "EvidenceGrade": [{
                "relevance": 0.95,
                "coverage": 0.9,
                "sufficient": True,
                "missing_information": [],
            }],
        })

        result = self.invoke(agent, [HumanMessage(content="这篇论文提出的核心方法是什么？")])

        self.assertEqual(probe.calls["pdf"], ["这篇论文提出的核心方法是什么？"])
        self.assertEqual(probe.calls["web"], [])
        self.assertEqual(probe.calls["arxiv"], [])
        self.assertTrue(result["evidence_sufficient"])

    def test_research_plan_reaches_pdf_and_external_research(self):
        probe = ToolProbe()
        agent = self.build(probe.tools("pdf", "arxiv"), {
            "QueryAnalysis": [{
                "standalone_query": "比较上传论文和近期 Agentic RAG 方法",
                "query_type": "research",
                "needs_retrieval": True,
                "selected_sources": ["pdf", "arxiv"],
            }],
            "ResearchPlan": [{"steps": [
                {"query": "上传论文的方法", "sources": ["pdf"]},
                {"query": "recent Agentic RAG methods", "sources": ["arxiv"]},
            ]}],
            "EvidenceGrade": [{
                "relevance": 0.9,
                "coverage": 0.85,
                "sufficient": True,
                "missing_information": [],
            }],
        })

        result = self.invoke(agent, [HumanMessage(content="比较上传论文和近期 Agentic RAG 方法")])

        self.assertEqual(probe.calls["pdf"], ["上传论文的方法"])
        self.assertEqual(probe.calls["arxiv"], ["recent Agentic RAG methods"])
        self.assertEqual(len(result["evidence_pool"]), 2)

    def test_insufficient_evidence_refines_once_then_stops(self):
        probe = ToolProbe()
        agent = self.build(probe.tools("pdf"), {
            "QueryAnalysis": [{
                "standalone_query": "论文中的主要实验结果是什么？",
                "query_type": "document",
                "needs_retrieval": True,
                "selected_sources": ["pdf"],
            }],
            "ResearchPlan": [{"steps": [{"query": "实验结果", "sources": ["pdf"]}]}],
            "EvidenceGrade": [
                {"relevance": 0.3, "coverage": 0.2, "sufficient": False,
                 "missing_information": ["baseline comparison", "ablation study"]},
                {"relevance": 0.4, "coverage": 0.3, "sufficient": False,
                 "missing_information": ["exact metrics"]},
            ],
            "QueryRefinement": [{
                "refined_query": "baseline comparison 与 ablation study 的具体性能结果",
                "sources": ["pdf"],
            }],
        })

        result = self.invoke(agent, [HumanMessage(content="论文中的主要实验结果是什么？")])

        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(len(probe.calls["pdf"]), 2)
        self.assertEqual(probe.calls["pdf"][-1], "baseline comparison 与 ablation study 的具体性能结果")
        self.assertFalse(result["evidence_sufficient"])
        self.assertEqual(len(result["evidence_pool"]), 2)
        self.assertEqual(
            [item["round"] for item in result["evidence_pool"]],
            [0, 1],
        )
        grader_calls = ScriptedChatModel.instances[-1].structured_messages[
            "EvidenceGrade"
        ]
        self.assertEqual(len(grader_calls), 2)
        second_round_context = json.loads(grader_calls[-1][-1].content)
        self.assertEqual(
            [item["round"] for item in second_round_context["evidence"]],
            [0, 1],
        )

    def test_web_unavailable_is_evidence_and_is_visible_to_generator(self):
        probe = ToolProbe({"web": "WEB_SEARCH_UNAVAILABLE: quota exceeded"})

        def answer(messages):
            content = "\n".join(str(message.content) for message in messages)
            return "Web Search unavailable" if "WEB_SEARCH_UNAVAILABLE" in content else "incorrect"

        agent = self.build(probe.tools("web"), {
            "QueryAnalysis": [{
                "standalone_query": "最近有什么更新？",
                "query_type": "web",
                "needs_retrieval": True,
                "selected_sources": ["web"],
            }],
            "ResearchPlan": [{"steps": [{"query": "recent updates", "sources": ["web"]}]}],
            "EvidenceGrade": [{
                "relevance": 0,
                "coverage": 0,
                "sufficient": True,
                "missing_information": ["web results"],
            }],
        }, answer=answer)

        result = self.invoke(agent, [HumanMessage(content="最近有什么更新？")])

        self.assertEqual(result["evidence_pool"][0]["status"], "unavailable")
        self.assertEqual(result["messages"][-1].content, "Web Search unavailable")

    def test_follow_up_uses_standalone_query_for_retrieval(self):
        probe = ToolProbe()
        standalone = "为什么当前论文中的 SFM 模块能够提升模型性能？"
        agent = self.build(probe.tools("pdf"), {
            "QueryAnalysis": [{
                "standalone_query": standalone,
                "query_type": "document",
                "needs_retrieval": True,
                "selected_sources": ["pdf"],
            }],
            "ResearchPlan": [{"steps": [{"query": standalone, "sources": ["pdf"]}]}],
            "EvidenceGrade": [{
                "relevance": 0.9, "coverage": 0.8, "sufficient": True,
                "missing_information": [],
            }],
        })

        result = self.invoke(agent, [
            HumanMessage(content="这篇论文的 SFM 模块是什么？"),
            AIMessage(content="SFM 是一个特征融合模块。"),
            HumanMessage(content="它为什么能提升性能？"),
        ])

        self.assertEqual(result["standalone_query"], standalone)
        self.assertEqual(probe.calls["pdf"], [standalone])

    def test_grader_parse_failure_does_not_start_unbounded_retry(self):
        probe = ToolProbe()
        agent = self.build(probe.tools("pdf"), {
            "QueryAnalysis": [{
                "standalone_query": "paper question",
                "query_type": "document",
                "needs_retrieval": True,
                "selected_sources": ["pdf"],
            }],
            "ResearchPlan": [{"steps": [{"query": "paper question", "sources": ["pdf"]}]}],
            "EvidenceGrade": [ValueError("invalid JSON")],
        })

        result = self.invoke(agent, [HumanMessage(content="paper question")])

        self.assertTrue(result["grading_failed"])
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(len(probe.calls["pdf"]), 1)


    def test_structured_output_fallbacks_remain_operational(self):
        probe = ToolProbe()
        agent = self.build(probe.tools("pdf"), {
            "QueryAnalysis": [ValueError("bad analyzer JSON")],
            "ResearchPlan": [ValueError("bad planner JSON")],
            "EvidenceGrade": [{
                "relevance": 0.8, "coverage": 0.8, "sufficient": True,
                "missing_information": [],
            }],
        })

        with self.assertLogs("server.agent.nodes", level="WARNING"):
            result = self.invoke(agent, [HumanMessage(content="paper fallback question")])

        self.assertEqual(result["query_type"], "document")
        self.assertEqual(probe.calls["pdf"], ["paper fallback question"])

    def test_one_failed_source_does_not_discard_other_evidence(self):
        probe = ToolProbe({"web": RuntimeError("offline")})
        agent = self.build(probe.tools("pdf", "web", "arxiv"), {
            "QueryAnalysis": [{
                "standalone_query": "cross-source question",
                "query_type": "research",
                "needs_retrieval": True,
                "selected_sources": ["pdf", "web", "arxiv"],
            }],
            "ResearchPlan": [{"steps": [{
                "query": "cross-source question",
                "sources": ["pdf", "web", "arxiv"],
            }]}],
            "EvidenceGrade": [{
                "relevance": 0.8, "coverage": 0.7, "sufficient": True,
                "missing_information": [],
            }],
        })

        with self.assertLogs("server.agent.nodes", level="WARNING"):
            result = self.invoke(agent, [HumanMessage(content="cross-source question")])

        statuses = {item["source"]: item["status"] for item in result["evidence_pool"]}
        self.assertEqual(statuses, {"pdf": "success", "web": "unavailable", "arxiv": "success"})
        self.assertEqual(len(result["messages"]), 2)
        self.assertTrue(all(type(message) in {HumanMessage, AIMessage} for message in result["messages"]))

    def test_large_evidence_is_bounded_before_final_generation(self):
        probe = ToolProbe({"pdf": "relevant evidence " + "x" * 20000})
        agent = self.build(probe.tools("pdf"), {
            "QueryAnalysis": [{
                "standalone_query": "paper question",
                "query_type": "document",
                "needs_retrieval": True,
                "selected_sources": ["pdf"],
            }],
            "ResearchPlan": [{"steps": [{"query": "paper question", "sources": ["pdf"]}]}],
            "EvidenceGrade": [{
                "relevance": 0.9, "coverage": 0.9, "sufficient": True,
                "missing_information": [],
            }],
        })

        self.invoke(agent, [HumanMessage(content="paper question")])

        self.assertLessEqual(
            self.harness.last_report.estimated_tokens_after,
            self.harness.input_budget_tokens,
        )
        self.assertGreater(self.harness.last_report.truncated_messages, 0)

if __name__ == "__main__":
    unittest.main()
