"""Mode agent : une seule session Claude Code qui travaille elle-même dans le dossier.

Au lieu de faire passer tout le dépôt dans chaque message de quatre agents
(architecte, développeur, testeur, relecteur), une session Claude Code avec
outils lit uniquement les fichiers utiles, modifie les lignes nécessaires et
garde son contexte d'un tour à l'autre. C'est bien plus économe en quota et
donne de meilleurs résultats sur un code existant.

Déroulé :
1. la session agent réalise la demande dans le dossier de travail ;
2. Super Agent lance les tests (dans un environnement sans secrets) ;
3. un relecteur, de préférence chez un autre fournisseur, juge le diff seul ;
4. si les tests échouent ou si la relecture demande des corrections, la même
   session reprend avec ces remarques, dans la limite de `max_rounds` tours.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .agents import REVIEWER
from .errors import BudgetExceeded, NoModelAvailable
from .orchestrator import Orchestrator, Report, RoundResult, format_report, parse_review
from .router import Router
from .workspace import Workspace, render_files

AGENT_SYSTEM = """Tu travailles en autonomie pour Super Agent, dans le dossier courant.
- Commence par repérer les fichiers utiles (Glob, Grep) et ne lis que ceux-là.
- Respecte l'architecture, le style et les conventions existants ; fais le
  changement le plus petit qui répond vraiment à la demande.
- Modifie les fichiers avec Edit plutôt que de les réécrire en entier.
- Ajoute ou mets à jour des tests automatisés quand c'est pertinent.
- Tu ne peux pas lancer de commandes : Super Agent exécute les tests après toi
  et te renverra les échecs éventuels.
- Ne touche pas aux secrets, ne crée pas de fichiers hors du dossier courant.
Termine par un résumé court en français : ce que tu as changé et pourquoi."""

NEW_PROJECT = ("Le dossier est vide : crée le projet demandé, le plus simple possible "
               "(Python et bibliothèque standard par défaut, tests unittest dans des fichiers "
               "test_*.py).")


class AgentRunner:
    def __init__(self, router: Router, workspace: Workspace, max_rounds: int = 2,
                 run_tests: bool = True, log: Callable[[str], None] = print,
                 diff: Callable[[], str] | None = None, artifacts_dir: Path | None = None,
                 fallback: Callable[[], Orchestrator] | None = None):
        """`fallback` fournit l'équipe classique si l'abonnement s'épuise dès le départ."""
        self.router = router
        self.fallback = fallback
        self.ws = workspace
        self.max_rounds = max_rounds
        self.run_tests = run_tests
        self.log = log
        self.diff = diff
        self.artifacts = Path(artifacts_dir) if artifacts_dir else workspace.root
        self.model = router.settings.get("agent_model", "sonnet")
        self.max_turns = router.settings.get("agent_max_turns", 40)
        self.review = router.settings.get("agent_review", True)

    def _work(self, prompt: str, report: Report, resume: str | None = None):
        self.log(f"▶ agent ({self.model}{', reprise' if resume else ''})")
        result = self.router.run_agent(self.model, AGENT_SYSTEM, prompt, str(self.ws.root),
                                       self.max_turns, resume=resume)
        step = f"agent → {result.provider}/{result.model} ({result.turns} échanges)"
        report.steps.append(step)
        report.summary = result.text
        self.log(f"  ✓ {step}")
        return result

    def _changes_text(self, before: dict) -> str:
        if self.diff:
            return f"Diff par rapport au dépôt :\n{self.diff()[:60_000]}"
        changed, _ = self.ws.changes(before)
        return f"Fichiers produits :\n{render_files(changed)[:60_000]}"

    def _review(self, request: str, changes: str, test_output: str, report: Report,
                avoid: str) -> dict | None:
        if not self.review:
            return None
        self.log("▶ reviewer (review)")
        try:
            result = self.router.complete(
                "review", REVIEWER.system,
                [{"role": "user", "content": f"Demande :\n{request}\n\n{changes}\n\n"
                                             f"Résultat des tests :\n{test_output}"}],
                avoid_provider=avoid)
        except (NoModelAvailable, BudgetExceeded) as e:
            self.log(f"  relecture impossible ({e}) : seuls les tests comptent")
            return None
        step = f"reviewer → {result.provider}/{result.model}"
        report.steps.append(step)
        self.log(f"  ✓ {step}")
        return parse_review(result.text)

    def run(self, request: str) -> Report:
        report = Report(request)
        before = self.ws.snapshot()
        prompt = f"Demande :\n{request}" + ("" if before else f"\n\n{NEW_PROJECT}")
        try:
            work = self._work(prompt, report)
        except NoModelAvailable as e:
            if not self.fallback:
                raise
            self.log(f"  {e} : l'équipe multi-fournisseurs prend le relais")
            return self.fallback().run(request)
        session, agent_provider = work.session_id, work.provider

        for number in range(1, self.max_rounds + 1):
            tests_ok, test_output = (self.ws.run_tests() if self.run_tests
                                     else (None, "tests non exécutés"))
            self.log(f"  tests : {'OK' if tests_ok else 'ÉCHEC' if tests_ok is False else '—'}")
            if tests_ok is False:
                # Inutile de payer une relecture : on sait déjà ce qu'il faut corriger.
                review = {"verdict": "revise", "score": None, "issues": [
                    {"target": "code", "severity": "major", "description": "Les tests échouent."}]}
            else:
                review = self._review(request, self._changes_text(before), test_output, report,
                                      avoid=agent_provider)
                review = review or {"verdict": "approve", "score": None, "issues": []}
            report.rounds.append(RoundResult(number, tests_ok, test_output, review))
            self.log(f"  relecture : {review['verdict']} (note {review.get('score')})")

            if review["verdict"] == "approve" and tests_ok is not False:
                report.approved = True
                break
            if number == self.max_rounds:
                break
            remarks = "\n".join(f"- [{i.get('severity', '?')}] {i.get('description', '')}"
                                for i in review["issues"])
            try:
                self._work(f"Corrige ton travail.\n\nRésultat des tests :\n{test_output}\n\n"
                           f"Remarques :\n{remarks}", report, resume=session)
            except BudgetExceeded as e:
                self.log(f"  arrêt : {e}")
                break

        report.files, report.deleted = self.ws.changes(before)
        (self.artifacts / "REPORT.md").write_text(format_report(report, self.router.usage))
        return report


def make_runner(router: Router, workspace: Workspace, max_rounds: int, run_tests: bool = True,
                log: Callable[[str], None] = print, mode: str | None = None,
                repo_context: Callable[[], str] | None = None,
                diff: Callable[[], str] | None = None, artifacts_dir: Path | None = None):
    """Mode agent si un abonnement Claude Code est prêt, sinon l'équipe multi-fournisseurs."""
    team = lambda: Orchestrator(router, workspace, max_rounds, run_tests, log,
                                repo_context=repo_context() if repo_context else None, diff=diff,
                                artifacts_dir=artifacts_dir)
    mode = mode or router.settings.get("mode", "agent")
    if mode == "agent":
        if router.agent_provider():
            return AgentRunner(router, workspace, max_rounds, run_tests, log, diff, artifacts_dir,
                               fallback=team)
        log("Mode agent indisponible (Claude Code absent ou en pause) : équipe multi-fournisseurs.")
    return team()
