import unittest

from fastapi.testclient import TestClient

from server.main import app


class BackendRootTest(unittest.TestCase):
    def test_root_redirects_to_streamlit_frontend(self):
        response = TestClient(app).get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"],
            "http://127.0.0.1:8501",
        )


if __name__ == "__main__":
    unittest.main()
