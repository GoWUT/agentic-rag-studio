import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest


class ClientChatResponseTest(unittest.TestCase):
    def test_chat_response_reads_answer_without_overwriting_session(self):
        sessions_response = Mock()
        sessions_response.status_code = 200
        sessions_response.json.return_value = {"sessions": []}

        with patch("requests.get", return_value=sessions_response):
            app = AppTest.from_file("client/app.py", default_timeout=10).run()
        app.session_state["session_id"] = "test-session"
        app.chat_input[0].set_value("What is in the PDF?")

        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "answer": "The PDF contains a smoke test.",
            "context": {
                "model_context_window_tokens": 1_000_000,
                "input_budget_tokens": 60_000,
                "max_output_tokens": 4_096,
                "safety_tokens": 4_096,
                "estimated_tokens_before": 1_200,
                "estimated_tokens_after": 900,
                "compacted_messages": 6,
                "truncated_messages": 1,
                "strategy": "compacted",
            },
            "execution": {
                "run_id": "run-123",
                "attempts": 2,
                "duration_ms": 345.6,
                "max_graph_steps": 20,
            },
        }

        with patch("requests.post", return_value=response):
            app.run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.session_state["session_id"], "test-session")
        self.assertEqual(
            app.session_state["chat"][-1],
            ("assistant", "The PDF contains a smoke test."),
        )
        self.assertEqual(
            app.session_state["context_report"]["strategy"],
            "compacted",
        )
        self.assertEqual(
            app.session_state["execution_report"]["run_id"],
            "run-123",
        )


if __name__ == "__main__":
    unittest.main()
