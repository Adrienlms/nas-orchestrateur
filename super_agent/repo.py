"""Travail sur un dépôt GitHub existant : clone, contexte, diff, branche et PR.

Le jeton GitHub (variable GITHUB_TOKEN) n'est jamais écrit dans .git/config :
il passe en en-tête HTTP à chaque commande git. Les modifications partent
toujours sur une nouvelle branche et une pull request ; rien n'est poussé
sur la branche principale, c'est toi qui décides de fusionner.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".txt", ".json", ".yml", ".yaml", ".js", ".ts", ".tsx",
                 ".jsx", ".html", ".css", ".sh", ".cfg", ".ini", ".go", ".rs", ".java", ".rb",
                 ".php", ".c", ".h", ".cpp", ".sql", ""}


class RepoError(ValueError):
    pass


def parse_github_url(url: str) -> tuple[str, str]:
    match = GITHUB_URL.match(url.strip())
    if not match:
        raise RepoError("adresse attendue : https://github.com/propriétaire/dépôt")
    return match.group(1), match.group(2)


class GitRepo:
    def __init__(self, url: str, path: str | Path, token: str | None = None,
                 log=print, api_base: str = "https://api.github.com"):
        self.url = url.strip().rstrip("/")
        self.owner, self.name = parse_github_url(self.url) if url.startswith("https://github.com") \
            else ("local", Path(url).name)
        self.path = Path(path)
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.log = log
        self.api_base = api_base

    # ---- git -----------------------------------------------------------
    def _git(self, *args: str, cwd: Path | None = None) -> str:
        auth = []
        if self.token and self.url.startswith("https://"):
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            auth = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
        proc = subprocess.run(["git", *auth, *args], cwd=cwd or self.path, capture_output=True,
                              text=True, timeout=600,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip().replace(self.token or "\0", "***")
            raise RepoError(f"git {args[0]} a échoué : {message[-500:]}")
        return proc.stdout

    def clone(self, branch: str = "main"):
        self.log(f"Clonage de {self.url} ({branch})…")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._git("clone", "--depth", "1", "--branch", branch, self.url, str(self.path),
                  cwd=self.path.parent)
        self.base_branch = branch

    def files(self) -> list[str]:
        return [f for f in self._git("ls-files").splitlines()
                if not (set(Path(f).parts) & SKIP_DIRS) and Path(f).name not in SKIP_FILES]

    def context(self, budget: int = 120_000) -> str:
        """Arborescence complète + contenu des fichiers texte, dans la limite du budget."""
        files = self.files()
        tree = "\n".join(files)
        # README d'abord, puis les petits fichiers avant les gros.
        ordered = sorted(files, key=lambda f: (not Path(f).name.lower().startswith("readme"),
                                               (self.path / f).stat().st_size, f))
        parts, used, skipped = [], len(tree), []
        for rel in ordered:
            target = self.path / rel
            if target.suffix.lower() not in TEXT_SUFFIXES or not target.is_file():
                continue
            try:
                content = target.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if used + len(content) > budget:
                skipped.append(rel)
                continue
            parts.append(f"### FILE: {rel}\n```\n{content}```\n")
            used += len(content)
        note = (f"\n(Fichiers non inclus faute de place : {', '.join(skipped)})\n" if skipped else "")
        return f"Arborescence du dépôt :\n{tree}\n\nContenu :\n{''.join(parts)}{note}"

    def _clean(self):
        """Retire les caches créés par l'exécution des tests avant de comparer ou valider."""
        for pattern in ("__pycache__", ".pytest_cache"):
            for d in self.path.rglob(pattern):
                if d.is_dir() and ".git" not in d.parts:
                    shutil.rmtree(d, ignore_errors=True)

    def diff(self) -> str:
        self._clean()
        self._git("add", "-A")
        return self._git("diff", "--cached", "--stat") + "\n" + self._git("diff", "--cached")

    def commit_and_push(self, branch: str, message: str) -> bool:
        self._git("checkout", "-b", branch)
        self._clean()
        self._git("add", "-A")
        if not self._git("status", "--porcelain").strip():
            return False
        self._git("-c", "user.name=Super Agent", "-c", "user.email=super-agent@localhost",
                  "commit", "-m", message)
        self.log(f"Envoi de la branche {branch}…")
        self._git("push", "origin", branch)
        return True

    # ---- GitHub API ----------------------------------------------------
    def open_pull_request(self, branch: str, title: str, body: str, draft: bool) -> str:
        payload = {"title": title, "head": branch, "base": self.base_branch, "body": body,
                   "draft": draft}
        req = urllib.request.Request(
            f"{self.api_base}/repos/{self.owner}/{self.name}/pulls", method="POST",
            data=json.dumps(payload).encode(),
            headers={"authorization": f"Bearer {self.token}", "accept": "application/vnd.github+json",
                     "content-type": "application/json", "user-agent": "super-agent"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())["html_url"]
        except urllib.error.HTTPError as e:
            raise RepoError(f"création de la PR refusée ({e.code}) : "
                            f"{e.read().decode(errors='replace')[:300]}") from None
