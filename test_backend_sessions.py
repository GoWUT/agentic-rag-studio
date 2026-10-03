import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import server.main as main_module
from server.agent.context_harness import ContextReport
from server.agent.execution_harness import (
    ExecutionConfigurationError,
    ExecutionLimitError,
    ExecutionReport,
    ExecutionTimeoutError,
    ExecutionUnavailableError,
)
from server.rag.ingestion import (
    IndexBuildError,
    IndexStatus,
    UploadTooLargeError,
)
from server.sessions import AgentReply


class FakeSessionManager:
    def list_sessions(self):
        return [
            {
                "session_id": "session-1",
                "file_id": "file-1",
                "file_name": "paper.pdf",
                "created_at": "2026-08-28T00:00:00+00:00",
                "updated_at": "2026-08-28T00:00:00+00:00",
                "turn_count": 1,
            }
        ]

    def get_history(self, session_id):
        return [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]

    def get_index_status(self, file_id):
        if file_id != "file-1":
            return None
        return IndexStatus(
            file_id="file-1",
            file_name="paper.pdf",
            size_bytes=512,
            status="ready",
            error=None,
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:01:00+00:00",
        )


class OfflineSessionManager(FakeSessionManager):
    def ask(self, session_id, message):
        raise ExecutionUnavailableError("provider unavailable")


class TimeoutSessionManager(FakeSessionManager):
    def ask(self, session_id, message):
        raise ExecutionTimeoutError("execution timed out")


class LimitSessionManager(FakeSessionManager):
    def ask(self, session_id, message):
        raise ExecutionLimitError("step budget exceeded")


class ConfigurationSessionManager(FakeSessionManager):
    def ask(self, session_id, message):
        raise ExecutionConfigurationError("invalid provider credentials")


class SuccessfulSessionManager(FakeSessionManager):
    def ask(self, session_id, message):
        return AgentReply(
            answer="Answer",
            context=ContextReport(
                model_context_window_tokens=1_000_000,
                input_budget_tokens=60_000,
                max_output_tokens=4_096,
                safety_tokens=4_096,
                estimated_tokens_before=1_200,
                estimated_tokens_after=900,
                compacted_messages=6,
                truncated_messages=1,
                strategy="compacted",
            ),
            execution=ExecutionReport(
                run_id="run-123",
                attempts=2,
                duration_ms=345.6,
                max_graph_steps=20,
            ),
        )


class OversizedUploadSessionManager(FakeSessionManager):
    def create_session(self, file_name, file_content):
        raise UploadTooLargeError("Uploaded PDF exceeds the limit")


class StreamingUploadSessionManager(FakeSessionManager):
    def __init__(self):
        self.received_stream = False

    def create_session(self, file_name, file_content):
        self.received_stream = hasattr(file_content, "read")
        return {
            "session_id": "session-1",
            "file_id": "file-1",
            "file_name": file_name,
            "created_at": "2026-08-29T00:00:00+00:00",
            "updated_at": "2026-08-29T00:00:00+00:00",
            "turn_count": 0,
            "index_status": "ready",
            "index_reused": False,
            "size_bytes": 512,
        }


class FailedIndexUploadSessionManager(FakeSessionManager):
    def create_session(self, file_name, file_content):
        raise IndexBuildError("provider secret: simulated failure")


class BackendSessionsTest(unittest.TestCase):
    def test_index_build_failure_returns_a_safe_error(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            FailedIndexUploadSessionManager(),
        ):
            client = TestClient(
                main_module.app,
                raise_server_exceptions=False,
            )
            response = client.post(
                "/upload_pdf",
                files={"file": ("paper.pdf", b"pdf", "application/pdf")},
            )

        self.assertEqual(response.status_code, 500)
        self.assertIn("index", response.json()["detail"].lower())
        self.assertNotIn("secret", response.text.lower())

    def test_returns_persisted_index_status(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            FakeSessionManager(),
        ):
            client = TestClient(main_module.app)
            response = client.get("/indexes/file-1")
            missing = client.get("/indexes/missing")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["size_bytes"], 512)
        self.assertEqual(missing.status_code, 404)

    def test_upload_passes_a_stream_to_the_ingestion_pipeline(self):
        manager = StreamingUploadSessionManager()
        with patch.object(main_module, "SESSION_MANAGER", manager):
            client = TestClient(main_module.app)
            response = client.post(
                "/upload_pdf",
                files={"file": ("paper.pdf", b"content", "application/pdf")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(manager.received_stream)
        self.assertEqual(response.json()["index_status"], "ready")
        self.assertFalse(response.json()["index_reused"])

    def test_oversized_pdf_returns_payload_too_large(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            OversizedUploadSessionManager(),
        ):
            client = TestClient(main_module.app)
            response = client.post(
                "/upload_pdf",
                files={"file": ("large.pdf", b"too large", "application/pdf")},
            )

        self.assertEqual(response.status_code, 413)

    def test_lists_sessions_and_returns_public_history(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            FakeSessionManager(),
        ):
            client = TestClient(main_module.app)
            sessions_response = client.get("/sessions")
            history_response = client.get(
                "/sessions/session-1/messages"
            )

        self.assertEqual(sessions_response.status_code, 200)
        self.assertEqual(
            sessions_response.json()["sessions"][0]["file_name"],
            "paper.pdf",
        )
        self.assertEqual(
            history_response.json()["messages"][-1],
            {"role": "assistant", "content": "Answer"},
        )

    def test_chat_reports_language_model_connection_failure(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            OfflineSessionManager(),
        ):
            client = TestClient(
                main_module.app,
                raise_server_exceptions=False,
            )
            response = client.post(
                "/chat",
                json={"session_id": "session-1", "message": "Question"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("language model", response.json()["detail"].lower())

    def test_chat_reports_total_execution_timeout(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            TimeoutSessionManager(),
        ):
            client = TestClient(
                main_module.app,
                raise_server_exceptions=False,
            )
            response = client.post(
                "/chat",
                json={"session_id": "session-1", "message": "Question"},
            )

        self.assertEqual(response.status_code, 504)
        self.assertIn("time", response.json()["detail"].lower())

    def test_chat_reports_agent_step_budget_exhaustion(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            LimitSessionManager(),
        ):
            client = TestClient(
                main_module.app,
                raise_server_exceptions=False,
            )
            response = client.post(
                "/chat",
                json={"session_id": "session-1", "message": "Question"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("step", response.json()["detail"].lower())

    def test_chat_hides_provider_authentication_details(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            ConfigurationSessionManager(),
        ):
            client = TestClient(
                main_module.app,
                raise_server_exceptions=False,
            )
            response = client.post(
                "/chat",
                json={"session_id": "session-1", "message": "Question"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("configuration", response.json()["detail"].lower())
        self.assertNotIn("credentials", response.text.lower())

    def test_chat_returns_context_harness_statistics(self):
        with patch.object(
            main_module,
            "SESSION_MANAGER",
            SuccessfulSessionManager(),
        ):
            client = TestClient(main_module.app)
            response = client.post(
                "/chat",
                json={"session_id": "session-1", "message": "Question"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Answer")
        self.assertEqual(response.json()["context"]["strategy"], "compacted")
        self.assertEqual(
            response.json()["context"]["estimated_tokens_after"],
            900,
        )
        self.assertEqual(response.json()["execution"]["run_id"], "run-123")
        self.assertEqual(response.json()["execution"]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
