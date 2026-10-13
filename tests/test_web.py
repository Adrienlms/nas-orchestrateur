import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from super_agent.web import JobStore, make_handler

MOCK_TOML = """
[settings]
max_review_rounds = 3

[providers.gemini]
type = "mock"

[providers.mistral]
type = "mock"

[[models]]
provider = "gemini"
name = "gemini-mock"
scores = { architecture = 9, code = 9, test = 8, review = 9 }

[[models]]
provider = "mistral"
name = "codestral-mock"
scores = { architecture = 6, code = 8, test = 8.5, review = 6 }
"""


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        (data / "config.toml").write_text(MOCK_TOML)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(JobStore(data), "secret"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def call(self, path, method="GET", body=None, auth=True):
        headers = {"content-type": "application/json"}
        if auth:
            headers["authorization"] = "Basic " + base64.b64encode(b"moi:secret").decode()
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if "json" in resp.headers["content-type"] else raw

    def test_password_required(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/jobs", auth=False)
        self.assertEqual(ctx.exception.code, 401)

    def test_job_runs_to_approval(self):
        job = self.call("/api/jobs", "POST", {"request": "calculatrice", "rounds": 3})
        for _ in range(100):
            detail = self.call(f"/api/jobs/{job['id']}")
            if detail["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(detail["status"], "done", detail.get("log"))
        self.assertTrue(detail["approved"])
        self.assertIn("calc.py", detail["files"])
        self.assertIn(b"ValueError", self.call(f"/api/jobs/{job['id']}/file?path=calc.py"))
        with self.assertRaises(urllib.error.HTTPError):
            self.call(f"/api/jobs/{job['id']}/file?path=../job.json")

    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""})
    def test_repo_mode_validation(self):
        for body in ({"request": "x", "repo_url": "pas-une-url"},
                     {"request": "x", "repo_url": "https://github.com/a/b"}):   # sans GITHUB_TOKEN
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.call("/api/jobs", "POST", body)
            self.assertEqual(ctx.exception.code, 400)

    def test_invalid_config_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/config", "PUT", {"toml": "[[models]\nbad"})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("gemini", self.call("/api/config")["toml"])


if __name__ == "__main__":
    unittest.main()
