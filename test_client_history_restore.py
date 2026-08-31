import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest


class ClientHistoryRestoreTest(unittest.TestCase):
    def test_latest_persisted_session_is_restored_on_startup(self):
        sessions_response = Mock()
        sessions_response.status_code = 200
        sessions_response.json.return_value = {
            "sessions": [
                {
                    "session_id": "session-1",
                    "file_id": "file-1",
                    "file_name": "paper.pdf",
                    "created_at": "2026-08-28T00:00:00+00:00",
                    "updated_at": "2026-08-28T00:00:00+00:00",
                    "turn_count": 1,
                }
            ]
        }
        history_response = Mock()
        history_response.status_code = 200
        history_response.json.return_value = {
            "session_id": "session-1",
            "messages": [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
            ],
        }

        with patch(
            "requests.get",
            side_effect=[sessions_response, history_response],
        ):
            app = AppTest.from_file(
                "client/app.py",
                default_timeout=10,
            ).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(app.session_state["session_id"], "session-1")
        self.assertEqual(
            app.session_state["chat"],
            [("user", "Question"), ("assistant", "Answer")],
        )


if __name__ == "__main__":
    unittest.main()
