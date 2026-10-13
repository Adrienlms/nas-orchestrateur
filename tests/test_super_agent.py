import os
import stat
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from super_agent.__main__ import DEMO_CONFIG
from super_agent.config import build_router, load_config
from super_agent.errors import (AuthError, FatalRequestError, NoModelAvailable, QuotaExhausted,
                                RateLimited, TransientError, classify)
from super_agent.providers import ClaudeCodeProvider
from super_agent.orchestrator import Orchestrator, parse_review
from super_agent.workspace import Workspace, parse_files, render_files

quiet = lambda *_: None


def mock_config(**overrides):
    providers = {
        "claude": {"type": "mock", "priority": 1},
        "gemini": {"type": "mock", "priority": 2},
    }
    for name, cfg in overrides.items():
        providers[name].update(cfg)
    return {
        "settings": {"state_file": None, "strategy": "best", "min_score": 7},
        "providers": providers,
        "models": [
            {"provider": "claude", "name": "opus", "cost": 5, "scores": {"code": 10, "review": 10}},
            {"provider": "claude", "name": "haiku", "cost": 1, "scores": {"code": 7.5, "review": 6}},
            {"provider": "gemini", "name": "flash", "cost": 0, "scores": {"code": 8, "review": 8}},
        ],
    }


class TestClassify(unittest.TestCase):
    def test_anthropic_credit(self):
        body = '{"error":{"message":"Your credit balance is too low to access the API"}}'
        self.assertIsInstance(classify("a", 400, body), QuotaExhausted)

    def test_openai_insufficient_quota(self):
        self.assertIsInstance(classify("o", 429, '{"error":{"code":"insufficient_quota"}}'),
                              QuotaExhausted)

    def test_other_statuses(self):
        self.assertIsInstance(classify("x", 429, "slow down", 3), RateLimited)
        self.assertIsInstance(classify("x", 402, ""), QuotaExhausted)
        self.assertIsInstance(classify("x", 401, ""), AuthError)
        self.assertIsInstance(classify("x", 529, "overloaded"), TransientError)
        self.assertIsInstance(classify("x", 400, "bad param"), FatalRequestError)


class TestRouter(unittest.TestCase):
    def test_best_strategy_picks_highest_score(self):
        router = build_router(mock_config(), quiet)
        self.assertEqual(router.candidates("code")[0].name, "opus")

    def test_economy_strategy_picks_cheapest_good_enough(self):
        config = mock_config()
        config["settings"]["strategy"] = "economy"
        router = build_router(config, quiet)
        self.assertEqual([m.name for m in router.candidates("code")], ["flash", "haiku", "opus"])

    def test_quota_exhausted_switches_provider_and_pauses_it(self):
        router = build_router(mock_config(claude={"fail_with": "quota", "fail_times": 1}), quiet)
        result = router.complete("code", "[role:general]", [])
        self.assertEqual(result.provider, "gemini")
        self.assertTrue(router.state.paused("claude"))
        # haiku n'est pas essayé : tout le fournisseur claude est en pause
        self.assertEqual(router.providers["claude"].calls, 1)

    def test_pause_is_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            config = mock_config(claude={"fail_with": "quota", "fail_times": 1})
            config["settings"]["state_file"] = "state.json"
            build_router(config, quiet, d).complete("code", "", [])
            reloaded = build_router({**mock_config(), "settings": config["settings"]}, quiet, d)
            self.assertTrue(reloaded.state.paused("claude"))
            self.assertEqual(reloaded.candidates("code")[0].provider, "gemini")

    def test_rate_limit_pause_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            config = mock_config(claude={"fail_with": "rate", "fail_times": 1})
            config["settings"]["state_file"] = "state.json"
            build_router(config, quiet, d).complete("code", "", [])
            reloaded = build_router({**mock_config(), "settings": config["settings"]}, quiet, d)
            self.assertIsNone(reloaded.state.paused("claude"))

    def test_avoid_provider_for_cross_review(self):
        router = build_router(mock_config(), quiet)
        self.assertEqual(router.candidates("review", avoid_provider="claude")[0].provider, "gemini")

    def test_all_exhausted(self):
        config = mock_config(claude={"fail_with": "quota", "fail_times": 5},
                             gemini={"fail_with": "transient", "fail_times": 5})
        with self.assertRaises(NoModelAvailable):
            build_router(config, quiet).complete("code", "", [])

    def test_provider_without_key_is_skipped(self):
        config = load_config()
        router = build_router({**config, "settings": {"state_file": None}}, quiet)
        for name in ("anthropic", "openai", "google"):
            router.providers[name].api_key = ""
        self.assertFalse(any(m.provider in ("anthropic", "openai", "google")
                             for m in router.candidates("code")))


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
if os.environ.get("ANTHROPIC_API_KEY"):
    print(json.dumps({"is_error": True, "result": "paid key leaked"})); sys.exit(1)
if os.environ.get("FAKE_MODE") == "limit":
    print(json.dumps({"is_error": True, "result": "Claude AI usage limit reached|4102444800"})); sys.exit(1)
print(json.dumps({"is_error": False, "result": "bonjour", "usage": {"input_tokens": 1}}))
"""


class TestClaudeCode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = Path(self.dir.name) / "claude"
        path.write_text(FAKE_CLAUDE)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.provider = ClaudeCodeProvider("abo", {"command": str(path)})

    def tearDown(self):
        self.dir.cleanup()

    def test_uses_subscription_not_paid_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-payant"}):
            self.assertEqual(self.provider.chat("opus", "s", [{"role": "user", "content": "x"}], 10).text,
                             "bonjour")

    def test_usage_limit_pauses_until_reset(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "limit"}):
            with self.assertRaises(QuotaExhausted) as ctx:
                self.provider.chat("opus", "s", [{"role": "user", "content": "x"}], 10)
        self.assertGreater(ctx.exception.retry_after, 0)


class TestWorkspace(unittest.TestCase):
    def test_parse_files(self):
        text = "Voici :\n### FILE: pkg/a.py\n```python\nx = 1\n```\n### FILE: `b.txt`\n```\nhi\n```\n"
        self.assertEqual(parse_files(text), {"pkg/a.py": "x = 1\n", "b.txt": "hi\n"})

    def test_markdown_with_nested_code_blocks_stays_whole(self):
        readme = "# Titre\n\n```bash\npython -m demo\n```\n\nSuite importante\n"
        text = (f"### FILE: README.md\n```markdown\n{readme}```\n\n"
                "### FILE: a.py\n```python\nx = 1\n```\n")
        self.assertEqual(parse_files(text), {"README.md": readme, "a.py": "x = 1\n"})

    def test_four_backtick_fence(self):
        text = "### FILE: doc.md\n````md\n```\ncode\n```\n````\n"
        self.assertEqual(parse_files(text), {"doc.md": "```\ncode\n```\n"})

    def test_unclosed_block_is_ignored(self):
        text = "### FILE: a.py\n```python\nx = 1\n### FILE: b.py\n```\ny = 2\n```\n"
        self.assertEqual(parse_files(text), {"b.py": "y = 2\n"})

    def test_render_then_parse_round_trip(self):
        files = {"README.md": "# T\n\n```bash\nls\n```\nfin\n", "x.py": "a = 1"}
        parsed = parse_files(render_files(files))
        self.assertEqual(parsed, {"README.md": files["README.md"], "x.py": "a = 1\n"})

    def test_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                Workspace(d).write({"../evil.py": "x"})

    def test_parse_review_fallback(self):
        self.assertEqual(parse_review("pas du JSON")["verdict"], "revise")
        self.assertEqual(parse_review('ok {"verdict": "APPROVE", "score": 9}')["verdict"], "approve")


class TestOrchestrator(unittest.TestCase):
    def test_demo_runs_critique_loop_with_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            router = build_router(DEMO_CONFIG, quiet)
            report = Orchestrator(router, Workspace(d), 3, log=quiet).run("calculatrice")
            self.assertTrue(report.approved)
            self.assertEqual(len(report.rounds), 2)
            self.assertFalse(report.rounds[0].tests_ok)
            self.assertTrue(report.rounds[1].tests_ok)
            # Claude épuisé : personne ne l'utilise ; le relecteur n'est pas le développeur
            self.assertFalse(any("claude/" in s for s in report.steps))
            dev = next(s for s in report.steps if s.startswith("developer"))
            rev = next(s for s in report.steps if s.startswith("reviewer"))
            self.assertNotEqual(dev.split("→")[1].split("/")[0], rev.split("→")[1].split("/")[0])
            self.assertTrue((Path(d) / "REPORT.md").exists())


if __name__ == "__main__":
    unittest.main()
