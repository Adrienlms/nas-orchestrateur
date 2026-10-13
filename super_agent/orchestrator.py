"""L'orchestrateur : répartit le travail et pilote la boucle critique → correction.

Déroulé :
1. l'architecte rédige la spécification ;
2. le développeur écrit le code, le testeur écrit les tests ;
3. les tests sont exécutés ;
4. le relecteur (de préférence chez un autre fournisseur que le développeur)
   juge le tout et renvoie des remarques ;
5. tant que ce n'est pas approuvé, le développeur et/ou le testeur corrigent,
   dans la limite de `max_rounds` tours.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

from .agents import ARCHITECT, DEVELOPER, REVIEWER, TESTER, Agent
from .router import Router
from .workspace import Workspace, parse_deletions, parse_files, render_files

REPO_RULES = (
    "Tu travailles sur un dépôt existant, dont le contenu est fourni plus haut. Respecte son "
    "architecture, son style et ses conventions, et fais le changement le plus petit qui répond "
    "à la demande. Ne renvoie que les fichiers que tu crées ou modifies, chacun en entier. Pour "
    "supprimer un fichier, écris une ligne « ### DELETE: chemin/du/fichier ».")


@dataclass
class RoundResult:
    number: int
    tests_ok: bool | None
    test_output: str
    review: dict


@dataclass
class Report:
    request: str
    spec: str = ""
    files: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    rounds: list[RoundResult] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    approved: bool = False
    summary: str = ""


def parse_review(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            review = json.loads(match.group(0))
            review.setdefault("issues", [])
            review["verdict"] = str(review.get("verdict", "revise")).lower()
            return review
        except json.JSONDecodeError:
            pass
    return {"verdict": "revise", "score": None,
            "issues": [{"target": "code", "severity": "major", "description": text.strip()[:2000]}]}


class Orchestrator:
    def __init__(self, router: Router, workspace: Workspace, max_rounds: int = 3,
                 run_tests: bool = True, log: Callable[[str], None] = print,
                 repo_context: str | None = None, diff: Callable[[], str] | None = None,
                 artifacts_dir: Path | None = None):
        """`repo_context` (contenu d'un dépôt existant) active le mode dépôt :
        les agents modifient ce code au lieu de partir de zéro, et le relecteur
        juge le diff fourni par `diff`."""
        self.router = router
        self.ws = workspace
        self.max_rounds = max_rounds
        self.run_tests = run_tests
        self.log = log
        self.repo_context = repo_context
        self.diff = diff
        self.artifacts = Path(artifacts_dir) if artifacts_dir else workspace.root

    def _with_repo(self, prompt: str) -> str:
        if not self.repo_context:
            return prompt
        return f"{self.repo_context}\n\n{REPO_RULES}\n\n{prompt}"

    def _changes(self, report: Report) -> str:
        if self.diff:
            return f"Diff par rapport au dépôt :\n{self.diff()[:60_000]}"
        return f"Fichiers :\n{render_files(report.files)}"

    def _ask(self, agent: Agent, prompt: str, report: Report, avoid: str | None = None,
             with_repo: bool = False):
        """`with_repo` joint le contenu du dépôt : coûteux, réservé à ceux qui en ont besoin."""
        self.log(f"▶ {agent.role} ({agent.kind})")
        content = self._with_repo(prompt) if with_repo else prompt
        result = self.router.complete(agent.kind, agent.system,
                                      [{"role": "user", "content": content}],
                                      avoid_provider=avoid)
        step = f"{agent.role} → {result.provider}/{result.model}"
        report.steps.append(step)
        self.log(f"  ✓ {step}")
        return result

    def _apply(self, text: str, report: Report, role: str):
        files, deletions = parse_files(text), parse_deletions(text)
        if not files and not deletions:
            self.log(f"  ⚠ {role} n'a renvoyé aucun fichier exploitable")
        report.files.update(files)
        self.ws.write(files)
        for rel in self.ws.delete(deletions):
            report.files.pop(rel, None)
            report.deleted.append(rel)
            self.log(f"  supprimé : {rel}")

    def run(self, request: str) -> Report:
        report = Report(request)

        report.spec = self._ask(ARCHITECT, f"Demande :\n{request}", report, with_repo=True).text
        (self.artifacts / "SPEC.md").write_text(report.spec)

        dev = self._ask(DEVELOPER, f"Spécification :\n{report.spec}", report, with_repo=True)
        dev_provider = dev.provider
        self._apply(dev.text, report, "developer")

        tests = self._ask(TESTER, f"Spécification :\n{report.spec}\n\nCode :\n"
                                  f"{render_files(report.files)}", report)
        self._apply(tests.text, report, "tester")

        for number in range(1, self.max_rounds + 1):
            tests_ok, test_output = (self.ws.run_tests() if self.run_tests
                                     else (None, "tests non exécutés"))
            self.log(f"  tests : {'OK' if tests_ok else 'ÉCHEC' if tests_ok is False else '—'}")

            review_text = self._ask(
                REVIEWER,
                f"Spécification :\n{report.spec}\n\n{self._changes(report)}\n\n"
                f"Résultat des tests :\n{test_output}", report, avoid=dev_provider).text
            review = parse_review(review_text)
            report.rounds.append(RoundResult(number, tests_ok, test_output, review))
            self.log(f"  relecture : {review['verdict']} (note {review.get('score')})")

            if review["verdict"] == "approve" and tests_ok is not False:
                report.approved = True
                break
            if number == self.max_rounds:
                break

            feedback = lambda target: "\n".join(
                f"- [{i.get('severity', '?')}] {i.get('description', '')}"
                for i in review["issues"] if i.get("target", "code") == target)
            code_fb, tests_fb = feedback("code"), feedback("tests")
            if not code_fb and not tests_fb:
                code_fb = "- Les tests échouent ou la relecture est négative : corrige le code."
            context = (f"Spécification :\n{report.spec}\n\nFichiers actuels :\n"
                       f"{render_files(report.files)}\n\nRésultat des tests :\n{test_output}\n\n")

            if code_fb:
                fix = self._ask(DEVELOPER, context + f"Remarques à corriger :\n{code_fb}", report,
                                with_repo=True)
                self._apply(fix.text, report, "developer")
            if tests_fb:
                fix = self._ask(TESTER, context + f"Remarques sur les tests :\n{tests_fb}", report)
                self._apply(fix.text, report, "tester")

        (self.artifacts / "REPORT.md").write_text(format_report(report, self.router.usage))
        return report


def format_report(report: Report, usage: list[dict]) -> str:
    lines = [f"# Rapport Super Agent\n", f"**Demande :** {report.request}\n",
             f"**Résultat :** {'approuvé' if report.approved else 'non approuvé'} "
             f"après {len(report.rounds)} tour(s) de relecture\n", "## Qui a fait quoi\n"]
    lines += [f"- {s}" for s in report.steps]
    if report.summary:
        lines += ["\n## Résumé de l'agent\n", report.summary.strip()]
    lines.append("\n## Tours de relecture\n")
    for r in report.rounds:
        state = {True: "OK", False: "échec", None: "non exécutés"}[r.tests_ok]
        lines.append(f"### Tour {r.number} : {r.review['verdict']} "
                     f"(note {r.review.get('score')}), tests {state}")
        lines += [f"- [{i.get('target')}/{i.get('severity')}] {i.get('description')}"
                  for i in r.review["issues"]]
        lines.append("")
    lines.append(f"## Consommation\n\n{usage_line(usage)}\n")
    lines.append("## Fichiers\n")
    lines += [f"- {p}" for p in sorted(report.files)]
    lines += [f"- {p} (supprimé)" for p in sorted(report.deleted)]
    return "\n".join(lines) + "\n"


def usage_summary(usage: list[dict]) -> dict:
    return {"calls": len(usage), "turns": sum(u.get("turns", 1) for u in usage),
            "in": sum(u["in"] for u in usage), "out": sum(u["out"] for u in usage),
            "cost": round(sum(u.get("cost", 0) for u in usage), 4)}


def usage_line(usage: list[dict]) -> str:
    """Résumé lisible ; le coût équivalent API donne l'ordre de grandeur du quota consommé."""
    u = usage_summary(usage)
    line = (f"{u['calls']} appels ({u['turns']} échanges avec le modèle), {u['in']} jetons lus "
            f"(contexte et cache compris), {u['out']} écrits.")
    if u["cost"]:
        line += (f" Équivalent API : {u['cost']:.2f} $ (non facturé avec l'abonnement, mais "
                 "c'est ce qui pèse sur ton quota).")
    return line
