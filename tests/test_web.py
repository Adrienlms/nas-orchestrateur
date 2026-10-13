import base64
import hashlib
import hmac
import http.client
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

from super_agent import web
from super_agent.web import (SESSION_COOKIE, SESSION_TTL, JobStore, check_session,
                             make_handler, make_session)

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


def start_server(data: Path, password: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(JobStore(data), password))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def raw_request(port, path, method="GET", body=None, headers=None):
    """Requête HTTP brute qui ne suit pas les redirections : (statut, en-têtes, corps)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body).encode()
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("content-type", "application/json")
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        content = resp.read()
        return resp.status, resp.headers, content
    finally:
        conn.close()


class TestWeb(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("super_agent.web.LOGIN_FAIL_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        (data / "config.toml").write_text(MOCK_TOML)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(JobStore(data), "secret"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
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

    def raw(self, path, method="GET", body=None, headers=None):
        return raw_request(self.server.server_port, path, method, body, headers)

    def login_cookie(self) -> str:
        status, headers, _ = self.raw("/api/login", "POST", {"password": "secret"})
        self.assertEqual(status, 200)
        return headers["set-cookie"].split(";")[0]

    # ---- tests existants -------------------------------------------------
    def test_password_required(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/jobs", auth=False)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

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
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call(f"/api/jobs/{job['id']}/file?path=../job.json")
        ctx.exception.close()

    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""})
    def test_repo_mode_validation(self):
        for body in ({"request": "x", "repo_url": "pas-une-url"},
                     {"request": "x", "repo_url": "https://github.com/a/b"}):   # sans GITHUB_TOKEN
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.call("/api/jobs", "POST", body)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

    def test_invalid_config_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/config", "PUT", {"toml": "[[models]\nbad"})
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()
        self.assertIn("gemini", self.call("/api/config")["toml"])

    # ---- page de connexion ----------------------------------------------
    def test_unauthenticated_redirects_or_401_without_popup(self):
        status, headers, _ = self.raw("/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        self.assertIsNone(headers.get("www-authenticate"))

        status, headers, body = self.raw("/api/jobs")
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("www-authenticate"))
        self.assertEqual(json.loads(body), {"error": "connexion requise"})

    def test_unauthenticated_post_api_is_401(self):
        status, headers, _ = self.raw("/api/jobs", "POST", {"request": "x"})
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("www-authenticate"))

    def test_login_page_is_public(self):
        status, headers, body = self.raw("/login")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b'type="password"', body)

    def test_wrong_password_gives_401_without_cookie(self):
        status, headers, body = self.raw("/api/login", "POST", {"password": "faux"})
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("set-cookie"))
        self.assertIsNone(headers.get("www-authenticate"))
        self.assertIn("error", json.loads(body))

    def test_wrong_password_waits_login_fail_delay(self):
        with mock.patch("super_agent.web.LOGIN_FAIL_DELAY", 0.3):
            start = time.monotonic()
            status, _, _ = self.raw("/api/login", "POST", {"password": "faux"})
            elapsed = time.monotonic() - start
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, 0.25)

    def test_login_body_missing_or_invalid_never_500(self):
        for body in (None, b"{pas du json", b"[1, 2]", b'"texte"'):
            status, headers, _ = self.raw("/api/login", "POST", body,
                                          {"content-type": "application/json"})
            self.assertIn(status, (400, 401), body)
            self.assertIsNone(headers.get("set-cookie"))

    def test_good_password_sets_session_cookie(self):
        status, headers, body = self.raw("/api/login", "POST", {"password": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        cookie = headers["set-cookie"]
        self.assertIn("sa_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn(f"Max-Age={SESSION_TTL}", cookie)

        session = cookie.split(";")[0]
        status, _, body = self.raw("/api/jobs", headers={"cookie": session})
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)
        status, _, _ = self.raw("/", headers={"cookie": session})
        self.assertEqual(status, 200)

    def test_cookie_among_other_cookies(self):
        session = self.login_cookie()
        status, _, _ = self.raw("/api/jobs", headers={"cookie": f"autre=1; {session}; x=y"})
        self.assertEqual(status, 200)

    def test_login_page_when_logged_in_redirects_home(self):
        session = self.login_cookie()
        status, headers, _ = self.raw("/login", headers={"cookie": session})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")

    def test_logout(self):
        status, headers, body = self.raw("/api/logout", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertIn("sa_session=", headers["set-cookie"])
        self.assertIn("Max-Age=0", headers["set-cookie"])

        status, headers, _ = self.raw("/logout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        self.assertIn("Max-Age=0", headers["set-cookie"])

    def test_bad_cookies_are_refused_without_500(self):
        expired = make_session("secret", now=time.time() - SESSION_TTL - 60)
        valid = make_session("secret")
        expiry, signature = valid.split(".")
        forged = expiry + "." + ("0" if signature[0] != "0" else "1") + signature[1:]
        later = f"{int(expiry) + 1000}.{signature}"
        cookies = [
            f"{SESSION_COOKIE}=",
            f"{SESSION_COOKIE}={expired}",
            f"{SESSION_COOKIE}={forged}",
            f"{SESSION_COOKIE}={later}",
            f"{SESSION_COOKIE}=sanspoint",
            f"{SESSION_COOKIE}=abc.{signature}",
            f"{SESSION_COOKIE}={make_session('autre-mot-de-passe')}",
            f'{SESSION_COOKIE}="non-termine',
            "n'importe quoi ;;; = = \x7f",
        ]
        for cookie in cookies:
            status, headers, _ = self.raw("/api/jobs", headers={"cookie": cookie})
            self.assertEqual(status, 401, cookie)
            self.assertIsNone(headers.get("www-authenticate"))
            status, headers, _ = self.raw("/", headers={"cookie": cookie})
            self.assertEqual(status, 303, cookie)
            self.assertEqual(headers["location"], "/login")

    def test_basic_auth_still_accepted(self):
        auth = "Basic " + base64.b64encode(b"qui:secret").decode()
        status, _, _ = self.raw("/api/jobs", headers={"authorization": auth})
        self.assertEqual(status, 200)
        bad = "Basic " + base64.b64encode(b"qui:faux").decode()
        status, headers, _ = self.raw("/api/jobs", headers={"authorization": bad})
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("www-authenticate"))


class TestWebWithoutPassword(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("super_agent.web.LOGIN_FAIL_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        (data / "config.toml").write_text(MOCK_TOML)
        self.server = start_server(data, "")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def raw(self, path, method="GET", body=None, headers=None):
        return raw_request(self.server.server_port, path, method, body, headers)

    def test_everything_open(self):
        status, headers, _ = self.raw("/login")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        status, _, _ = self.raw("/api/jobs")
        self.assertEqual(status, 200)
        status, _, _ = self.raw("/")
        self.assertEqual(status, 200)

    def test_login_api_ok_without_password(self):
        status, _, _ = self.raw("/api/login", "POST", {"password": "peu importe"})
        self.assertEqual(status, 200)


class TestSession(unittest.TestCase):
    def test_valid_token_accepted(self):
        self.assertTrue(check_session(make_session("secret"), "secret"))

    def test_token_format(self):
        token = make_session("secret", now=1000)
        expiry, sep, signature = token.partition(".")
        self.assertEqual(sep, ".")
        self.assertEqual(expiry, str(1000 + SESSION_TTL))
        key = hashlib.sha256(b"super-agent-session:" + b"secret").digest()
        self.assertEqual(signature, hmac.new(key, expiry.encode(), "sha256").hexdigest())

    def test_ttl_is_seven_days(self):
        self.assertEqual(SESSION_TTL, 7 * 24 * 3600)
        self.assertEqual(web.SESSION_COOKIE, "sa_session")

    def test_other_password_refused(self):
        self.assertFalse(check_session(make_session("secret"), "nouveau"))

    def test_expired_refused(self):
        token = make_session("secret", now=1000)
        expiry = 1000 + SESSION_TTL
        self.assertTrue(check_session(token, "secret", now=expiry - 1))
        self.assertFalse(check_session(token, "secret", now=expiry))
        self.assertFalse(check_session(token, "secret", now=expiry + 1))

    def test_forged_refused(self):
        token = make_session("secret")
        expiry, signature = token.split(".")
        # Dernier caractère de la signature remplacé par un autre.
        forged_signature = signature[:-1] + ("a" if signature[-1] != "a" else "b")
        forged_token = f"{expiry}.{forged_signature}"
        self.assertFalse(check_session(forged_token, "secret"))
        # Expiration repoussée sans re-signer.
        self.assertFalse(check_session(f"{int(expiry) + 1}.{signature}", "secret"))
        # Caractère ajouté à la signature.
        self.assertFalse(check_session(token + "0", "secret"))

    def test_garbage_refused(self):
        for token in ("n'importe quoi", "", ".", "123", "123.", ".abc", "abc.def",
                      "-5.abc", "1e9.abc", "12.34.56"):
            self.assertFalse(check_session(token, "secret"), token)


if __name__ == "__main__":
    unittest.main()
