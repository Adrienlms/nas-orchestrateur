import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from super_agent.__main__ import DEMO_CONFIG
from super_agent.config import build_router
from super_agent.orchestrator import Orchestrator
from super_agent.repo import GitRepo, RepoError, parse_github_url
from super_agent.web import JobStore
from super_agent.workspace import Workspace, parse_deletions, safe_env
from tests.test_web import MOCK_TOML

quiet = lambda *_: None


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


class FakeGitHub(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeGitHub.requests.append({"path": self.path, "auth": self.headers["authorization"], **body})
        data = json.dumps({"html_url": "https://github.com/moi/depot/pull/7"}).encode()
        self.send_response(201)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)


class TestRepoMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        seed = root / "seed"
        seed.mkdir()
        (seed / "README.md").write_text("# Calculatrice\n")
        (seed / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (seed / "obsolete.py").write_text("x = 1\n")
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "initial")
        self.origin = root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.origin)], check=True)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_team_changes_repo_and_opens_pull_request(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        FakeGitHub.requests.clear()

        repo = GitRepo(str(self.origin), self.root / "clone", token="jeton-secret", log=quiet,
                       api_base=f"http://127.0.0.1:{server.server_port}")
        repo.clone("main")
        context = repo.context()
        self.assertIn("### FILE: calc.py", context)
        self.assertIn("# Calculatrice", context)

        router = build_router(DEMO_CONFIG, quiet)
        report = Orchestrator(router, Workspace(repo.path), 3, log=quiet, repo_context=context,
                              diff=repo.diff, artifacts_dir=self.root).run("ajoute divide")
        self.assertTrue(report.approved)
        self.assertTrue((self.root / "REPORT.md").exists())
        self.assertFalse((repo.path / "REPORT.md").exists())   # le rapport ne pollue pas le dépôt

        self.assertTrue(repo.commit_and_push("super-agent/abc", "ajoute divide"))
        branches = subprocess.run(["git", "branch", "--list"], cwd=self.origin,
                                  capture_output=True, text=True).stdout
        self.assertIn("super-agent/abc", branches)
        pushed = subprocess.run(["git", "show", "--name-only", "--format=", "super-agent/abc"],
                                cwd=self.origin, capture_output=True, text=True).stdout
        self.assertIn("calc.py", pushed)
        self.assertNotIn("__pycache__", pushed)
        self.assertNotIn("jeton-secret", (repo.path / ".git" / "config").read_text())

        url = repo.open_pull_request("super-agent/abc", "Ajoute divide", "corps", draft=False)
        self.assertEqual(url, "https://github.com/moi/depot/pull/7")
        sent = FakeGitHub.requests[0]
        self.assertEqual((sent["base"], sent["head"], sent["draft"]), ("main", "super-agent/abc", False))
        self.assertEqual(sent["auth"], "Bearer jeton-secret")

    def test_web_job_in_repo_mode(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        FakeGitHub.requests.clear()
        data = self.root / "data"
        data.mkdir()
        (data / "config.toml").write_text(MOCK_TOML)
        store = JobStore(data)
        store.github_api = f"http://127.0.0.1:{server.server_port}"
        job = store.create("ajoute divide", {"rounds": 3, "run_tests": True,
                                             "repo_url": str(self.origin), "base_branch": "main"})
        for _ in range(200):
            job = store.get(job["id"])
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "done", (store.dir(job["id"]) / "log.txt").read_text())
        self.assertEqual(job["pr_url"], "https://github.com/moi/depot/pull/7")
        self.assertIn("calc.py", job["changed"])
        self.assertEqual(FakeGitHub.requests[0]["head"], f"super-agent/{job['id']}")

    def test_nothing_to_push_when_unchanged(self):
        repo = GitRepo(str(self.origin), self.root / "clone", token="", log=quiet)
        repo.clone("main")
        self.assertFalse(repo.commit_and_push("super-agent/vide", "rien"))

    def test_deletions(self):
        repo = GitRepo(str(self.origin), self.root / "clone", token="", log=quiet)
        repo.clone("main")
        ws = Workspace(repo.path)
        self.assertEqual(ws.delete(parse_deletions("### DELETE: obsolete.py\n")), ["obsolete.py"])
        self.assertIn("obsolete.py", repo.diff())
        with self.assertRaises(ValueError):
            ws.delete(["../origin.git/HEAD"])

    def test_context_respects_budget(self):
        repo = GitRepo(str(self.origin), self.root / "clone", token="", log=quiet)
        repo.clone("main")
        context = repo.context(budget=60)
        self.assertIn("calc.py", context)            # toujours dans l'arborescence
        self.assertIn("non inclus", context)

    def test_bad_branch_reports_error(self):
        repo = GitRepo(str(self.origin), self.root / "clone", token="", log=quiet)
        with self.assertRaises(RepoError):
            repo.clone("n-existe-pas")


class TestHelpers(unittest.TestCase):
    def test_parse_github_url(self):
        self.assertEqual(parse_github_url("https://github.com/Adrienlms/nas-orchestrateur.git"),
                         ("Adrienlms", "nas-orchestrateur"))
        with self.assertRaises(RepoError):
            parse_github_url("git@github.com:a/b.git")

    def test_generated_tests_do_not_see_secrets(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "x", "GEMINI_API_KEY": "y",
                                          "CLAUDE_CODE_OAUTH_TOKEN": "z", "PATH": "/bin"}):
            env = safe_env()
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["PATH"], "/bin")


if __name__ == "__main__":
    unittest.main()
