"""Dossier de travail : extraction des fichiers produits et exécution des tests."""
from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

FILE_BLOCK = re.compile(
    r"^###\s*FILE:\s*`?(?P<path>[^\s`]+)`?\s*\n```[^\n]*\n(?P<body>.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


DELETE_LINE = re.compile(r"^###\s*DELETE:\s*`?(?P<path>[^\s`]+)`?\s*$", re.MULTILINE)
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)


def parse_files(text: str) -> dict[str, str]:
    return {m.group("path"): m.group("body") for m in FILE_BLOCK.finditer(text)}


def parse_deletions(text: str) -> list[str]:
    return [m.group("path") for m in DELETE_LINE.finditer(text)]


def safe_env() -> dict[str, str]:
    """Environnement des tests générés, sans clés d'API ni jetons."""
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


def render_files(files: dict[str, str]) -> str:
    return "\n".join(f"### FILE: {p}\n```\n{c}```\n" for p, c in sorted(files.items()))


class Workspace:
    def __init__(self, root: str | Path, test_command: str | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.test_command = test_command

    def _safe(self, rel: str) -> Path:
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"chemin refusé hors du dossier de travail : {rel}")
        return target

    def write(self, files: dict[str, str]) -> list[str]:
        written = []
        for rel, content in files.items():
            target = self._safe(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            written.append(rel)
        return written

    def delete(self, paths: list[str]) -> list[str]:
        deleted = []
        for rel in paths:
            target = self._safe(rel)
            if target.is_file():
                target.unlink()
                deleted.append(rel)
        return deleted

    def _default_test_command(self) -> list[str] | None:
        if (self.root / "package.json").exists() and not any(self.root.glob("*.py")):
            return ["npm", "test", "--silent"]
        if importlib.util.find_spec("pytest"):
            return [sys.executable, "-m", "pytest", "-q", "-x"]
        if (self.root / "tests").is_dir():
            return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        return [sys.executable, "-m", "unittest", "discover", "-p", "test*.py"]

    def run_tests(self, timeout: int = 120) -> tuple[bool, str]:
        """Lance les tests générés. Attention : exécute du code produit par une IA."""
        cmd = shlex.split(self.test_command) if self.test_command else self._default_test_command()
        try:
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True,
                                  timeout=timeout, env=safe_env())
        except subprocess.TimeoutExpired:
            return False, f"Les tests ont dépassé {timeout} s."
        except FileNotFoundError:
            return False, f"Commande de test introuvable : {cmd[0]}"
        output = (proc.stdout + proc.stderr).strip()
        if len(output) > 6000:
            output = output[:2000] + "\n…\n" + output[-4000:]
        return proc.returncode == 0, output
