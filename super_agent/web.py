"""Interface web de Super Agent (bibliothèque standard uniquement).

    python -m super_agent web --port 8080

Variables d'environnement :
    SUPER_AGENT_DATA      dossier des données (config, historique)   défaut : ./data
    SUPER_AGENT_PASSWORD  mot de passe de l'interface ; vide = pas de mot de passe

Quand un mot de passe est défini, une page de connexion (/login) ouvre une
session par cookie, valable 7 jours. L'en-tête « Authorization: Basic » reste
accepté pour les scripts. /logout déconnecte.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import tomllib
import traceback
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import DEFAULT_CONFIG, build_router, load_config
from .orchestrator import Orchestrator
from .repo import GitRepo, parse_github_url
from .router import status_report
from .workspace import Workspace

STATIC = Path(__file__).resolve().parent / "static"

SESSION_COOKIE = "sa_session"
SESSION_TTL = 7 * 24 * 3600
LOGIN_FAIL_DELAY = 1.0          # secondes d'attente après un mauvais mot de passe

# Un verrou par adresse IP : les échecs d'un même client passent un par un (sans
# cela, des requêtes parallèles contourneraient le délai), sans bloquer les
# autres clients. Limite connue : derrière un proxy inverse, tous les clients
# partagent la même IP et donc le même verrou ; acceptable sur un NAS en réseau local.
_FAIL_LOCKS: dict[str, threading.Lock] = {}
_FAIL_LOCKS_GUARD = threading.Lock()
_FAIL_LOCKS_MAX = 1024


def _login_failed(client: str):
    """Freine la force brute, quel que soit le moyen d'authentification essayé."""
    with _FAIL_LOCKS_GUARD:
        if len(_FAIL_LOCKS) > _FAIL_LOCKS_MAX:       # évite une croissance sans fin
            for ip in [ip for ip, lock in _FAIL_LOCKS.items() if not lock.locked()]:
                del _FAIL_LOCKS[ip]
        lock = _FAIL_LOCKS.setdefault(client, threading.Lock())
    with lock:
        time.sleep(LOGIN_FAIL_DELAY)


# ---- session sans état ---------------------------------------------------
def _session_signature(expiry: int, password: str) -> str:
    key = hashlib.sha256(b"super-agent-session:" + password.encode()).digest()
    return hmac.new(key, str(expiry).encode(), "sha256").hexdigest()


def make_session(password: str, now: float | None = None) -> str:
    """Jeton « expiration.signature », invalide dès que le mot de passe change."""
    expiry = int((time.time() if now is None else now) + SESSION_TTL)
    return f"{expiry}.{_session_signature(expiry, password)}"


def check_session(token: str, password: str, now: float | None = None) -> bool:
    if not token or not isinstance(token, str):
        return False
    expiry_text, sep, signature = token.partition(".")
    if not sep or not re.fullmatch(r"[0-9]{1,12}", expiry_text):
        return False
    expiry = int(expiry_text)
    expected = _session_signature(expiry, password)
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return False
    return expiry > (time.time() if now is None else now)


def _session_cookie(token: str, max_age: int) -> str:
    # Pas d'attribut Secure : l'accès sur le NAS se fait en HTTP sur le réseau local.
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"


def write_json(path: Path, data: dict):
    """Écriture atomique : un lecteur ne voit jamais un fichier à moitié écrit."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


class JobStore:
    """Historique des tâches, une par dossier : job.json, log.txt, workspace/."""

    def __init__(self, data: Path):
        self.data = data
        self.root = data / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = data / "config.toml"
        if not self.config_path.exists():
            shutil.copy(DEFAULT_CONFIG, self.config_path)
        self.lock = threading.Lock()
        self.github_api = os.environ.get("SUPER_AGENT_GITHUB_API", "https://api.github.com")
        self.queue: queue.Queue[str] = queue.Queue()
        for job in self.list():                       # tâches coupées par un redémarrage
            if job["status"] in ("queued", "running"):
                self.update(job["id"], status="failed", error="interrompue par un redémarrage")
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- stockage -------------------------------------------------------
    def dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise KeyError(job_id)
        path = self.root / job_id
        if not path.exists():
            raise KeyError(job_id)
        return path

    def get(self, job_id: str) -> dict:
        return json.loads((self.dir(job_id) / "job.json").read_text())

    def list(self) -> list[dict]:
        jobs = []
        for d in self.root.iterdir():
            try:
                jobs.append(json.loads((d / "job.json").read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(jobs, key=lambda j: j["created"], reverse=True)

    def update(self, job_id: str, **fields) -> dict:
        with self.lock:
            job = self.get(job_id)
            job.update(fields)
            write_json(self.dir(job_id) / "job.json", job)
            return job

    def create(self, request: str, options: dict) -> dict:
        job_id = uuid.uuid4().hex[:12]
        path = self.root / job_id
        (path / "workspace").mkdir(parents=True)
        job = {"id": job_id, "request": request, "options": options, "status": "queued",
               "created": time.time(), "started": None, "finished": None,
               "approved": None, "error": None, "steps": [], "changed": [], "pr_url": None}
        write_json(path / "job.json", job)
        (path / "log.txt").write_text("")
        self.queue.put(job_id)
        return job

    def delete(self, job_id: str):
        if self.get(job_id)["status"] == "running":
            raise ValueError("impossible de supprimer une tâche en cours")
        shutil.rmtree(self.dir(job_id))

    def log(self, job_id: str, line: str):
        with open(self.dir(job_id) / "log.txt", "a") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")

    # ---- exécution ------------------------------------------------------
    def _worker(self):
        while True:
            job_id = self.queue.get()
            try:
                self._run(job_id)
            except KeyError:
                pass                                   # supprimée avant de démarrer

    def _run(self, job_id: str):
        job = self.update(job_id, status="running", started=time.time())
        log = lambda line: self.log(job_id, line)
        try:
            config = load_config(self.config_path)
            opts = job["options"]
            if opts.get("strategy"):
                config.setdefault("settings", {})["strategy"] = opts["strategy"]
            router = build_router(config, log, state_dir=self.data)
            rounds = int(opts.get("rounds") or config.get("settings", {}).get("max_review_rounds", 3))
            if opts.get("repo_url"):
                self._run_repo(job, router, rounds, log)
            else:
                orch = Orchestrator(router, Workspace(self.dir(job_id) / "workspace"), rounds,
                                    run_tests=opts.get("run_tests", True), log=log)
                report = orch.run(job["request"])
                self.update(job_id, status="done", approved=report.approved, steps=report.steps,
                            finished=time.time())
        except Exception as e:                          # noqa: BLE001 — on garde la trace pour l'utilisateur
            log("ERREUR : " + "".join(traceback.format_exception_only(e)).strip())
            self.update(job_id, status="failed", error=str(e), finished=time.time())

    def _run_repo(self, job: dict, router, rounds: int, log):
        """Mode dépôt : clone, modifications par l'équipe, puis branche + pull request."""
        opts, job_dir = job["options"], self.dir(job["id"])
        repo = GitRepo(opts["repo_url"], job_dir / "repo", log=log, api_base=self.github_api)
        repo.clone(opts.get("base_branch") or "main")
        ws = Workspace(repo.path, test_command=opts.get("test_command") or None)
        orch = Orchestrator(router, ws, rounds, run_tests=opts.get("run_tests", True), log=log,
                            repo_context=repo.context(), diff=repo.diff, artifacts_dir=job_dir)
        report = orch.run(job["request"])
        changed = sorted(report.files) + [f"{p} (supprimé)" for p in report.deleted]
        self.update(job["id"], steps=report.steps, approved=report.approved, changed=changed)

        branch = f"super-agent/{job['id']}"
        title = job["request"].strip().splitlines()[0][:72]
        if not repo.commit_and_push(branch, f"{title}\n\nProposé par Super Agent (tâche {job['id']})."):
            log("Aucune modification à proposer.")
        else:
            body = ("Proposé par Super Agent. Rien n'est fusionné sans ton accord.\n\n"
                    + (job_dir / "REPORT.md").read_text())
            url = repo.open_pull_request(branch, title, body[:60_000], draft=not report.approved)
            log(f"Pull request ouverte : {url}")
            self.update(job["id"], pr_url=url)
        self.update(job["id"], status="done", finished=time.time())


PUBLIC_ROUTES = {("GET", "/login"), ("POST", "/api/login"),
                 ("POST", "/api/logout"), ("GET", "/logout")}


def make_handler(store: JobStore, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SuperAgent"

        def log_message(self, fmt, *args):            # journal discret
            pass

        # ---- utilitaires --------------------------------------------
        def _client(self) -> str:
            return self.client_address[0] if self.client_address else "?"

        def _session_token(self) -> str:
            header = self.headers.get("cookie", "")
            if not header:
                return ""
            cookies = http.cookies.SimpleCookie()
            try:
                cookies.load(header)
            except http.cookies.CookieError:
                return ""
            morsel = cookies.get(SESSION_COOKIE)
            return morsel.value if morsel else ""

        def _has_session(self) -> bool:
            return check_session(self._session_token(), password)

        def _authorized(self) -> bool:
            if not password:
                return True
            if self._has_session():
                return True
            header = self.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    given = None
                if given is not None and hmac.compare_digest(given.encode(), password.encode()):
                    return True
                _login_failed(self._client())           # même frein que POST /api/login
            return False

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str, extra: dict | None = None):
            self.send_response(303)
            self.send_header("location", location)
            self.send_header("content-length", "0")
            self.send_header("cache-control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, data, status: int = 200, extra: dict | None = None):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json",
                       extra)

        def _error(self, status: int, message: str):
            self._json({"error": message}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            if length > 1_000_000:
                raise ValueError("requête trop volumineuse")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("objet JSON attendu")
            return body

        def _dispatch(self, method: str):
            url = urlparse(self.path)
            if (method, url.path) not in PUBLIC_ROUTES and not self._authorized():
                if method == "GET" and not url.path.startswith("/api/"):
                    return self._redirect("/login")
                return self._error(401, "connexion requise")
            for pattern, handler_method, func in ROUTES:
                match = re.fullmatch(pattern, url.path)
                if match and handler_method == method:
                    try:
                        return func(self, *match.groups(), query=parse_qs(url.query))
                    except KeyError:
                        return self._error(404, "introuvable")
                    except (ValueError, tomllib.TOMLDecodeError) as e:
                        return self._error(400, str(e))
            self._error(404, "introuvable")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ---- connexion ----------------------------------------------
        def login_page(self, query):
            # Seul le cookie compte ici : un ancien en-tête Basic gardé en cache par
            # le navigateur ne doit pas retarder l'affichage de la page de connexion.
            if not password or self._has_session():
                return self._redirect("/")
            self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")

        def login(self, query):
            given = str(self._body().get("password", ""))
            if not password:
                return self._json({"ok": True})
            if hmac.compare_digest(given.encode(), password.encode()):
                cookie = _session_cookie(make_session(password), SESSION_TTL)
                return self._json({"ok": True}, extra={"set-cookie": cookie})
            _login_failed(self._client())               # freine la force brute
            self._error(401, "mot de passe incorrect")

        def logout_api(self, query):
            self._json({"ok": True}, extra={"set-cookie": _session_cookie("", 0)})

        def logout_page(self, query):
            self._redirect("/login", {"set-cookie": _session_cookie("", 0)})

        # ---- routes -------------------------------------------------
        def index(self, query):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

        def status(self, query):
            router = build_router(load_config(store.config_path), lambda _: None, store.data)
            self._json({**status_report(router), "now": time.time(),
                        "auth": bool(password), "github": bool(os.environ.get("GITHUB_TOKEN"))})

        def reset(self, query):
            build_router(load_config(store.config_path), lambda _: None, store.data).state.clear()
            self._json({"ok": True})

        def jobs(self, query):
            self._json(store.list())

        def create_job(self, query):
            body = self._body()
            request = str(body.get("request", "")).strip()
            if not request:
                raise ValueError("la demande est vide")
            options = {"strategy": body.get("strategy") if body.get("strategy") in ("best", "economy") else None,
                       "rounds": max(1, min(10, int(body.get("rounds") or 3))),
                       "run_tests": bool(body.get("run_tests", True))}
            repo_url = str(body.get("repo_url") or "").strip()
            if repo_url:
                parse_github_url(repo_url)              # refuse une adresse invalide
                if not os.environ.get("GITHUB_TOKEN"):
                    raise ValueError("GITHUB_TOKEN n'est pas défini dans le conteneur")
                branch = str(body.get("base_branch") or "main").strip()
                if not re.fullmatch(r"[\w./-]+", branch):
                    raise ValueError("nom de branche invalide")
                options.update(repo_url=repo_url, base_branch=branch,
                               test_command=str(body.get("test_command") or "").strip())
            self._json(store.create(request, options), 201)

        def job(self, job_id, query):
            job = store.get(job_id)
            d = store.dir(job_id)
            if job["options"].get("repo_url"):
                files = [f for f in job.get("changed", []) if not f.endswith("(supprimé)")]
                report = d / "REPORT.md"
            else:
                ws = d / "workspace"
                files = sorted(str(p.relative_to(ws)) for p in ws.rglob("*")
                               if p.is_file() and "__pycache__" not in p.parts)
                report = ws / "REPORT.md"
            self._json({**job, "log": (d / "log.txt").read_text()[-50_000:], "files": files,
                        "report": report.read_text() if report.exists() else None})

        def job_file(self, job_id, query):
            job = store.get(job_id)
            ws = (store.dir(job_id) / ("repo" if job["options"].get("repo_url") else "workspace")).resolve()
            target = (ws / query.get("path", [""])[0]).resolve()
            if not target.is_relative_to(ws) or not target.is_file():
                raise KeyError(job_id)
            self._send(200, target.read_bytes(), "text/plain; charset=utf-8")

        def job_zip(self, job_id, query):
            job = store.get(job_id)
            ws = store.dir(job_id) / ("repo" if job["options"].get("repo_url") else "workspace")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in ws.rglob("*"):
                    if p.is_file() and not {"__pycache__", ".git"} & set(p.parts):
                        z.write(p, p.relative_to(ws))
            self._send(200, buf.getvalue(), "application/zip",
                       {"content-disposition": f'attachment; filename="super-agent-{job_id}.zip"'})

        def delete_job(self, job_id, query):
            store.delete(job_id)
            self._json({"ok": True})

        def get_config(self, query):
            self._json({"toml": store.config_path.read_text()})

        def put_config(self, query):
            text = str(self._body().get("toml", ""))
            parsed = tomllib.loads(text)                # refuse un fichier invalide
            build_router(parsed, lambda _: None)        # et une config inutilisable
            store.config_path.write_text(text)
            self._json({"ok": True})

    ROUTES = [
        (r"/", "GET", Handler.index),
        (r"/login", "GET", Handler.login_page),
        (r"/logout", "GET", Handler.logout_page),
        (r"/api/login", "POST", Handler.login),
        (r"/api/logout", "POST", Handler.logout_api),
        (r"/api/status", "GET", Handler.status),
        (r"/api/reset", "POST", Handler.reset),
        (r"/api/jobs", "GET", Handler.jobs),
        (r"/api/jobs", "POST", Handler.create_job),
        (r"/api/jobs/(\w+)", "GET", Handler.job),
        (r"/api/jobs/(\w+)", "DELETE", Handler.delete_job),
        (r"/api/jobs/(\w+)/file", "GET", Handler.job_file),
        (r"/api/jobs/(\w+)/zip", "GET", Handler.job_zip),
        (r"/api/config", "GET", Handler.get_config),
        (r"/api/config", "PUT", Handler.put_config),
    ]
    return Handler


def serve(host: str = "0.0.0.0", port: int = 8080):
    data = Path(os.environ.get("SUPER_AGENT_DATA", "data")).resolve()
    store = JobStore(data)
    password = os.environ.get("SUPER_AGENT_PASSWORD", "")
    server = ThreadingHTTPServer((host, port), make_handler(store, password))
    print(f"Super Agent : http://{host}:{port}  (données : {data}"
          f"{', mot de passe actif' if password else ', sans mot de passe'})", flush=True)
    server.serve_forever()
