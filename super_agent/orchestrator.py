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
from dataclasses import dataclass, field
from typing import Callable

from .agents import ARCHITECT, DEVELOPER, REVIEWER, TESTER, Agent
from .router import Router
from .workspace import Workspace, parse_files, render_files


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
    rounds: list[RoundResult] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    approved: bool = False


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
                 run_tests: bool = True, log: Callable[[str], None] = print):
        self.router = router
        self.ws = workspace
        self.max_rounds = max_rounds
        self.run_tests = run_tests
        self.log = log

    def _ask(self, agent: Agent, prompt: str, report: Report, avoid: str | None = None):
        self.log(f"▶ {agent.role} ({agent.kind})")
        result = self.router.complete(agent.kind, agent.system,
                                      [{"role": "user", "content": prompt}], avoid_provider=avoid)
        step = f"{agent.role} → {result.provider}/{result.model}"
        report.steps.append(step)
        self.log(f"  ✓ {step}")
        return result

    def _apply(self, text: str, report: Report, role: str):
        files = parse_files(text)
        if not files:
            self.log(f"  ⚠ {role} n'a renvoyé aucun fichier exploitable")
        report.files.update(files)
        self.ws.write(files)

    def run(self, request: str) -> Report:
        report = Report(request)

        report.spec = self._ask(ARCHITECT, f"Demande :\n{request}", report).text
        (self.ws.root / "SPEC.md").write_text(report.spec)

        dev = self._ask(DEVELOPER, f"Spécification :\n{report.spec}", report)
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
                f"Spécification :\n{report.spec}\n\nFichiers :\n{render_files(report.files)}\n\n"
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
                fix = self._ask(DEVELOPER, context + f"Remarques à corriger :\n{code_fb}", report)
                self._apply(fix.text, report, "developer")
            if tests_fb:
                fix = self._ask(TESTER, context + f"Remarques sur les tests :\n{tests_fb}", report)
                self._apply(fix.text, report, "tester")

        (self.ws.root / "REPORT.md").write_text(format_report(report, self.router.usage))
        return report


def format_report(report: Report, usage: list[dict]) -> str:
    lines = [f"# Rapport Super Agent\n", f"**Demande :** {report.request}\n",
             f"**Résultat :** {'approuvé' if report.approved else 'non approuvé'} "
             f"après {len(report.rounds)} tour(s) de relecture\n", "## Qui a fait quoi\n"]
    lines += [f"- {s}" for s in report.steps]
    lines.append("\n## Tours de relecture\n")
    for r in report.rounds:
        state = {True: "OK", False: "échec", None: "non exécutés"}[r.tests_ok]
        lines.append(f"### Tour {r.number} : {r.review['verdict']} "
                     f"(note {r.review.get('score')}), tests {state}")
        lines += [f"- [{i.get('target')}/{i.get('severity')}] {i.get('description')}"
                  for i in r.review["issues"]]
        lines.append("")
    total_in = sum(u["in"] for u in usage)
    total_out = sum(u["out"] for u in usage)
    lines.append(f"## Consommation\n\n{len(usage)} appels, {total_in} jetons en entrée, "
                 f"{total_out} en sortie.\n")
    lines.append("## Fichiers\n")
    lines += [f"- {p}" for p in sorted(report.files)]
    return "\n".join(lines) + "\n"
