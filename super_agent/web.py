"""Interface web de Super Agent (bibliothèque standard uniquement).

    python -m super_agent web --port 8080

Variables d'environnement :
    SUPER_AGENT_DATA      dossier des données (config, historique)   défaut : ./data
    SUPER_AGENT_PASSWORD  mot de passe de l'interface (utilisateur libre) ; vide = pas de mot de passe
"""
from __future__ import annotations

import base64
import hmac
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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import DEFAULT_CONFIG, build_router, load_config
from .orchestrator import Orchestrator
from .router import status_report
from .workspace import Workspace

STATIC = Path(__file__).resolve().parent / "static"


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
               "approved": None, "error": None, "steps": []}
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
            orch = Orchestrator(router, Workspace(self.dir(job_id) / "workspace"), rounds,
                                run_tests=opts.get("run_tests", True), log=log)
            report = orch.run(job["request"])
            self.update(job_id, status="done", approved=report.approved, steps=report.steps,
                        finished=time.time())
        except Exception as e:                          # noqa: BLE001 — on garde la trace pour l'utilisateur
            log("ERREUR : " + "".join(traceback.format_exception_only(e)).strip())
            self.update(job_id, status="failed", error=str(e), finished=time.time())


def make_handler(store: JobStore, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SuperAgent"

        def log_message(self, fmt, *args):            # journal discret
            pass

        # ---- utilitaires --------------------------------------------
        def _authorized(self) -> bool:
            if not password:
                return True
            header = self.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    return False
                return hmac.compare_digest(given, password)
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

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def _error(self, status: int, message: str):
            self._json({"error": message}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            if length > 1_000_000:
                raise ValueError("requête trop volumineuse")
            return json.loads(self.rfile.read(length) or b"{}")

        def _dispatch(self, method: str):
            if not self._authorized():
                return self._send(401, b"Mot de passe requis", "text/plain; charset=utf-8",
                                  {"www-authenticate": 'Basic realm="Super Agent"'})
            url = urlparse(self.path)
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

        # ---- routes -------------------------------------------------
        def index(self, query):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

        def status(self, query):
            router = build_router(load_config(store.config_path), lambda _: None, store.data)
            self._json({**status_report(router), "now": time.time(),
                        "auth": bool(password)})

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
            self._json(store.create(request, options), 201)

        def job(self, job_id, query):
            job = store.get(job_id)
            d = store.dir(job_id)
            ws = d / "workspace"
            files = sorted(str(p.relative_to(ws)) for p in ws.rglob("*")
                           if p.is_file() and "__pycache__" not in p.parts)
            report = ws / "REPORT.md"
            self._json({**job, "log": (d / "log.txt").read_text()[-50_000:], "files": files,
                        "report": report.read_text() if report.exists() else None})

        def job_file(self, job_id, query):
            ws = (store.dir(job_id) / "workspace").resolve()
            target = (ws / query.get("path", [""])[0]).resolve()
            if not target.is_relative_to(ws) or not target.is_file():
                raise KeyError(job_id)
            self._send(200, target.read_bytes(), "text/plain; charset=utf-8")

        def job_zip(self, job_id, query):
            ws = store.dir(job_id) / "workspace"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in ws.rglob("*"):
                    if p.is_file() and "__pycache__" not in p.parts:
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
