"""Dossier de travail : extraction des fichiers produits et exécution des tests."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

FILE_BLOCK = re.compile(
    r"^###\s*FILE:\s*`?(?P<path>[^\s`]+)`?\s*\n```[^\n]*\n(?P<body>.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def parse_files(text: str) -> dict[str, str]:
    return {m.group("path"): m.group("body") for m in FILE_BLOCK.finditer(text)}


def render_files(files: dict[str, str]) -> str:
    return "\n".join(f"### FILE: {p}\n```\n{c}```\n" for p, c in sorted(files.items()))


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

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

    def run_tests(self, timeout: int = 120) -> tuple[bool, str]:
        """Lance les tests générés. Attention : exécute du code produit par une IA."""
        if importlib.util.find_spec("pytest"):
            cmd = [sys.executable, "-m", "pytest", "-q", "-x"]
        else:
            cmd = [sys.executable, "-m", "unittest", "discover", "-p", "test*.py"]
        try:
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"Les tests ont dépassé {timeout} s."
        output = (proc.stdout + proc.stderr).strip()
        if len(output) > 6000:
            output = output[:2000] + "\n…\n" + output[-4000:]
        return proc.returncode == 0, output
