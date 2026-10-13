"""Dossier de travail : extraction des fichiers produits et exécution des tests."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HEADER_LINE = re.compile(r"^###\s*(?:FILE|DELETE):", re.MULTILINE)
FILE_HEADER = re.compile(r"^###\s*FILE:\s*`?(?P<path>[^\s`]+)`?\s*$", re.MULTILINE)
OPEN_FENCE = re.compile(r"^(?P<fence>`{3,})[^`\n]*$", re.MULTILINE)
DELETE_LINE = re.compile(r"^###\s*DELETE:\s*`?(?P<path>[^\s`]+)`?\s*$", re.MULTILINE)
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)


def parse_files(text: str) -> dict[str, str]:
    """Extrait les blocs « ### FILE: chemin » suivis d'un bloc de code.

    Le contenu va jusqu'à la DERNIÈRE clôture de la section (avant l'en-tête
    suivant) : un fichier Markdown qui contient lui-même des blocs ```…```
    reste donc entier. Un bloc jamais refermé (réponse coupée) est ignoré
    plutôt qu'écrit à moitié.
    """
    files = {}
    starts = [m.start() for m in HEADER_LINE.finditer(text)] + [len(text)]
    for begin, end in zip(starts, starts[1:]):
        section = text[begin:end]
        header = FILE_HEADER.match(section)
        opening = OPEN_FENCE.search(section, header.end()) if header else None
        if not opening:
            continue
        fence = opening.group("fence")
        body_start = opening.end() + 1
        closings = [m for m in re.finditer(rf"^{fence}`*[ \t]*$", section[body_start:], re.MULTILINE)]
        if not closings:
            continue
        files[header.group("path")] = section[body_start:body_start + closings[-1].start()]
    return files


def fence_for(content: str) -> str:
    """Clôture plus longue que toute suite de ` du contenu (blocs imbriqués)."""
    longest = max((len(run) for run in re.findall(r"`{3,}", content)), default=0)
    return "`" * max(3, longest + 1)


def parse_deletions(text: str) -> list[str]:
    return [m.group("path") for m in DELETE_LINE.finditer(text)]


def safe_env() -> dict[str, str]:
    """Environnement des tests générés, sans clés d'API ni jetons.

    Les GIT_CONFIG_* partent ensemble : retirer seulement GIT_CONFIG_KEY_0 casserait git.
    """
    return {k: v for k, v in os.environ.items()
            if not SECRET_ENV.search(k) and not k.startswith("GIT_CONFIG_")}


def render_file(path: str, content: str) -> str:
    fence = fence_for(content)
    body = content if content.endswith("\n") or not content else content + "\n"
    return f"### FILE: {path}\n{fence}\n{body}{fence}\n"


def render_files(files: dict[str, str]) -> str:
    return "\n".join(render_file(p, c) for p, c in sorted(files.items()))


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

    def snapshot(self) -> dict[str, str]:
        """Empreinte de chaque fichier, pour savoir ensuite ce qu'un agent a changé."""
        prints = {}
        for path in self.root.rglob("*"):
            rel = path.relative_to(self.root)
            if path.is_file() and not set(rel.parts) & IGNORED_DIRS:
                prints[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return prints

    def changes(self, before: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        """Fichiers créés ou modifiés depuis `before` (avec leur contenu) et fichiers supprimés."""
        after = self.snapshot()
        changed = {}
        for rel, digest in sorted(after.items()):
            if before.get(rel) != digest:
                try:
                    changed[rel] = (self.root / rel).read_text()
                except UnicodeDecodeError:
                    changed[rel] = "(fichier binaire)"
        return changed, sorted(set(before) - set(after))

    def _default_test_command(self) -> list[str] | None:
        if (self.root / "package.json").exists() and not any(self.root.glob("*.py")):
            return ["npm", "test", "--silent"]
        if importlib.util.find_spec("pytest"):
            return [sys.executable, "-m", "pytest", "-q", "-x"]
        if (self.root / "tests").is_dir():
            return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        return [sys.executable, "-m", "unittest", "discover", "-p", "test*.py"]

    def run_tests(self, timeout: int = 120) -> tuple[bool | None, str]:
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
        if proc.returncode == 5:                   # unittest et pytest : aucun test trouvé
            return None, output or "aucun test trouvé"
        return proc.returncode == 0, output
