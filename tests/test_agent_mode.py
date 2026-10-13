import tempfile
import unittest
from pathlib import Path

from super_agent.agent_mode import AgentRunner, make_runner
from super_agent.config import build_router
from super_agent.errors import BudgetExceeded
from super_agent.orchestrator import Orchestrator
from super_agent.workspace import Workspace

quiet = lambda *_: None


def config(agent=True, **claude):
    return {
        "settings": {"state_file": None},
        "providers": {
            "claude": {"type": "mock", "priority": 0, "agent": agent, **claude},
            "gemini": {"type": "mock", "priority": 2},
        },
        "models": [
            {"provider": "claude", "name": "sonnet", "cost": 0,
             "scores": {"architecture": 9, "code": 9, "test": 9, "review": 9}},
            {"provider": "gemini", "name": "flash", "cost": 0,
             "scores": {"architecture": 7, "code": 7, "test": 7, "review": 7}},
        ],
    }


class TestAgentMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name) / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_agent_fixes_failing_tests_in_the_same_session(self):
        router = build_router(config(), quiet)
        runner = make_runner(router, self.ws, 3, log=quiet)
        self.assertIsInstance(runner, AgentRunner)
        report = runner.run("une calculatrice add/divide")

        self.assertTrue(report.approved)
        self.assertIn("calc.py", report.files)
        self.assertIn("raise ValueError", (self.ws.root / "calc.py").read_text())
        claude, gemini = router.providers["claude"], router.providers["gemini"]
        # 1 : demande ; 2 : reprise avec l'échec des tests ; 3 : reprise après la relecture.
        self.assertEqual(len(claude.prompts), 3)
        self.assertIn("Résultat des tests", claude.prompts[1])
        # La relecture part chez l'autre fournisseur et ne voit que les fichiers produits.
        self.assertTrue(gemini.prompts)
        self.assertIn("### FILE: calc.py", gemini.prompts[0])
        self.assertEqual(router.usage[0]["kind"], "agent")
        self.assertIn("Équivalent API", (self.ws.root / "REPORT.md").read_text())

    def test_without_subscription_the_team_takes_over(self):
        router = build_router(config(agent=False), quiet)
        self.assertIsInstance(make_runner(router, self.ws, 2, log=quiet), Orchestrator)

    def test_quota_at_start_falls_back_to_team(self):
        router = build_router(config(fail_with="quota", fail_times=1), quiet)
        report = make_runner(router, self.ws, 3, log=quiet).run("une calculatrice")
        self.assertTrue(report.approved)
        self.assertTrue(router.state.paused("claude"))
        self.assertTrue(any("architect" in s for s in report.steps))

    def test_call_budget_stops_the_task(self):
        cfg = config()
        cfg["settings"]["max_calls_per_task"] = 1
        router = build_router(cfg, quiet)
        router.complete("code", "", [])
        with self.assertRaises(BudgetExceeded):
            router.complete("code", "", [])

    def test_team_sends_repo_context_only_to_architect_and_developer(self):
        router = build_router(config(agent=False), quiet)
        context = "CONTENU-DU-DEPOT"
        Orchestrator(router, self.ws, 1, log=quiet, repo_context=context).run("x")
        prompts = router.providers["claude"].prompts + router.providers["gemini"].prompts
        with_context = [p for p in prompts if context in p]
        self.assertEqual(len(with_context), 2)          # architecte + développeur, pas les autres

    def test_no_tests_is_not_a_failure(self):
        (self.ws.root / "a.py").write_text("x = 1\n")
        ok, _ = self.ws.run_tests()
        self.assertIsNone(ok)


if __name__ == "__main__":
    unittest.main()
