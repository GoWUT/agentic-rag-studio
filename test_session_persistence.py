from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from server.rag.ingestion import IngestionResult
from server.sessions import AgentSessionManager, SessionStore


class FakeIngestionPipeline:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.received_stream = None

    def ingest(self, *, file_name, stream):
        self.received_stream = stream
        return IngestionResult(
            file_id="file-1",
            file_name=file_name,
            size_bytes=512,
            pdf_path=self.workspace / "file-1.pdf",
            index_dir=self.workspace / "chroma_file-1",
            vectorstore=object(),
            status="ready",
            reused=False,
        )


class SessionPersistenceTest(unittest.TestCase):
    def test_create_session_delegates_document_work_to_ingestion_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            ingestion = FakeIngestionPipeline(workspace)
            manager = AgentSessionManager(
                {
                    "WORKSPACE_DIR": directory,
                    "EMBEDDING_MODEL": "unused",
                    "SERPER_API_KEY": "unused",
                    "MODEL_NAME": "unused",
                    "DEEPSEEK_API_KEY": "unused",
                },
                ingestion_pipeline=ingestion,
            )
            manager._make_runtime = MagicMock(return_value={})
            stream = BytesIO(b"pdf bytes")

            response = manager.create_session("paper.pdf", stream)

            self.assertIs(ingestion.received_stream, stream)
            self.assertEqual(response["file_id"], "file-1")
            self.assertEqual(response["index_status"], "ready")
            self.assertFalse(response["index_reused"])
            self.assertEqual(response["size_bytes"], 512)
            record = manager.store.get(response["session_id"])
            self.assertIsNotNone(record)
            self.assertEqual(record.embedding_model, "unused")

    def test_session_remembers_the_embedding_model_used_by_its_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.sqlite3")

            store.create(
                session_id="session-1",
                file_id="file-1",
                file_name="paper.pdf",
                pdf_path="paper.pdf",
                chroma_dir="chroma_file-1",
                embedding_model="BAAI/bge-small-zh-v1.5",
            )

            self.assertEqual(
                store.get("session-1").embedding_model,
                "BAAI/bge-small-zh-v1.5",
            )

    def test_messages_survive_a_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.sqlite3"
            store = SessionStore(database)
            store.create(
                session_id="session-1",
                file_id="file-1",
                file_name="paper.pdf",
                pdf_path="paper.pdf",
                chroma_dir="chroma_file-1",
            )
            store.save_messages(
                "session-1",
                [
                    HumanMessage(content="What is the sample size?"),
                    AIMessage(content="The sample size is 120."),
                ],
            )

            restored = SessionStore(database)

            self.assertEqual(restored.list()[0].turn_count, 1)
            self.assertEqual(
                [message.content for message in restored.load_messages("session-1")],
                ["What is the sample size?", "The sample size is 120."],
            )

    def test_public_history_hides_internal_tool_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.sqlite3"
            store = SessionStore(database)
            store.create(
                session_id="session-1",
                file_id="file-1",
                file_name="paper.pdf",
                pdf_path="paper.pdf",
                chroma_dir="chroma_file-1",
            )
            store.save_messages(
                "session-1",
                [
                    HumanMessage(content="What is the sample size?"),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "search_pdf",
                                "args": {"query": "sample size"},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    ToolMessage(
                        content="PDF RAG Results: 120 participants",
                        tool_call_id="call-1",
                    ),
                    AIMessage(content="The sample size is 120."),
                ],
            )
            manager = AgentSessionManager(
                {
                    "WORKSPACE_DIR": directory,
                    "EMBEDDING_MODEL": "unused",
                    "SERPER_API_KEY": "unused",
                    "MODEL_NAME": "unused",
                    "DEEPSEEK_API_KEY": "unused",
                },
                store=store,
            )

            self.assertEqual(
                manager.get_history("session-1"),
                [
                    {
                        "role": "user",
                        "content": "What is the sample size?",
                    },
                    {
                        "role": "assistant",
                        "content": "The sample size is 120.",
                    },
                ],
            )


if __name__ == "__main__":
    unittest.main()
