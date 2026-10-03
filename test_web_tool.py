import unittest
from unittest.mock import patch
import os

import requests

from server.agent.tools import GoogleSerperAPIWrapper, make_web_tool


class WebToolTest(unittest.TestCase):
    def test_serper_http_error_becomes_a_tool_result(self):
        response = requests.Response()
        response.status_code = 403
        error = requests.HTTPError(
            "403 Client Error: Forbidden",
            response=response,
        )

        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key"}):
            with patch.object(
                GoogleSerperAPIWrapper,
                "results",
                side_effect=error,
            ):
                result = make_web_tool("test-key").invoke(
                    {"query": "Beijing weather"}
                )

        self.assertIn("WEB_SEARCH_UNAVAILABLE", result)
        self.assertIn("HTTP 403", result)
        self.assertIn("Do not fabricate", result)


if __name__ == "__main__":
    unittest.main()
