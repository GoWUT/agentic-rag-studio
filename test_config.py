import os
import unittest
from unittest.mock import patch

from server.config import load_config


class ConfigurationTest(unittest.TestCase):
    def test_pdf_upload_limit_must_be_positive(self):
        with patch("server.config.load_dotenv"), patch.dict(
            os.environ,
            {"MAX_PDF_UPLOAD_BYTES": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "MAX_PDF_UPLOAD_BYTES",
            ):
                load_config()

    def test_model_request_timeout_must_fit_inside_execution_timeout(self):
        environment = {
            "AGENT_EXECUTION_TIMEOUT_SECONDS": "30",
            "MODEL_REQUEST_TIMEOUT_SECONDS": "60",
        }

        with patch("server.config.load_dotenv"), patch.dict(
            os.environ,
            environment,
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "MODEL_REQUEST_TIMEOUT_SECONDS",
            ):
                load_config()

    def test_retrieval_retry_limit_can_be_zero_but_not_negative(self):
        with patch("server.config.load_dotenv"), patch.dict(
            os.environ,
            {"AGENT_MAX_RETRIEVAL_RETRIES": "0"},
            clear=True,
        ):
            self.assertEqual(load_config()["AGENT_MAX_RETRIEVAL_RETRIES"], 0)

        with patch("server.config.load_dotenv"), patch.dict(
            os.environ,
            {"AGENT_MAX_RETRIEVAL_RETRIES": "-1"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "AGENT_MAX_RETRIEVAL_RETRIES"):
                load_config()

if __name__ == "__main__":
    unittest.main()
