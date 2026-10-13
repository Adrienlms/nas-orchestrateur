"""Connecteurs vers les fournisseurs d'IA.

Deux protocoles suffisent pour couvrir presque tout le marché :
- l'API Messages d'Anthropic ;
- le format « chat completions » d'OpenAI, que parlent aussi Google Gemini,
  Mistral, Groq, OpenRouter et Ollama (modèles locaux).

Aucune dépendance externe : on utilise urllib de la bibliothèque standard.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .errors import AuthError, ProviderError, QuotaExhausted, RateLimited, TransientError, classify


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class Provider:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.base_url = cfg.get("base_url", "").rstrip("/")
        self.timeout = cfg.get("timeout", 300)
        key_env = cfg.get("api_key_env")
        self.api_key = os.environ.get(key_env, "") if key_env else ""
        self.needs_key = bool(key_env)

    def available(self) -> bool:
        if not self.cfg.get("enabled", True):
            return False
        return bool(self.api_key) or not self.needs_key

    def chat(self, model: str, system: str, messages: list[dict],
             max_tokens: int) -> ChatResult:
        raise NotImplementedError

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            retry_after = e.headers.get("retry-after") if e.headers else None
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            raise classify(self.name, e.code, body, retry_after) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TransientError(self.name, f"réseau injoignable : {e}") from None


class AnthropicProvider(Provider):
    def __init__(self, name: str, cfg: dict):
        super().__init__(name, cfg)
        self.base_url = self.base_url or "https://api.anthropic.com"

    def chat(self, model, system, messages, max_tokens):
        payload = {"model": model, "max_tokens": max_tokens, "system": system,
                   "messages": messages, **self.cfg.get("extra", {})}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        data = self._post(f"{self.base_url}/v1/messages", headers, payload)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return ChatResult(text, self.name, model,
                          usage.get("input_tokens", 0), usage.get("output_tokens", 0))


class OpenAICompatProvider(Provider):
    def chat(self, model, system, messages, max_tokens):
        token_param = self.cfg.get("max_tokens_param", "max_tokens")
        payload = {"model": model,
                   "messages": [{"role": "system", "content": system}, *messages],
                   token_param: max_tokens, **self.cfg.get("extra", {})}
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(f"{self.base_url}/chat/completions", headers, payload)
        choices = data.get("choices") or []
        if not choices:
            raise TransientError(self.name, f"réponse vide : {str(data)[:200]}")
        usage = data.get("usage") or {}
        return ChatResult(choices[0]["message"].get("content") or "", self.name, model,
                          usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


class ClaudeCodeProvider(Provider):
    """Passe par la commande `claude` (Claude Code) connectée à ton abonnement.

    Les appels consomment alors le quota de l'abonnement Pro ou Max (la fenêtre
    qui se recharge toutes les 5 heures) au lieu des crédits payants de l'API.
    Prérequis : Claude Code installé et connecté une fois avec `claude` puis /login.
    """

    _LIMIT = re.compile(r"(usage limit|limit reached|hit your limit|rate limit)", re.I)

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.command = cfg.get("command", "claude")

    def available(self) -> bool:
        return self.cfg.get("enabled", True) and shutil.which(self.command) is not None

    def chat(self, model, system, messages, max_tokens):
        prompt = "\n\n".join(m["content"] for m in messages)
        cmd = [self.command, "-p", "--output-format", "json", "--model", model,
               "--system-prompt", system, "--tools", "", "--no-session-persistence",
               *self.cfg.get("extra_args", [])]
        # Sans cette ligne, Claude Code utiliserait la clé d'API (payante) si elle est définie.
        env = {k: v for k, v in os.environ.items()
               if k != "ANTHROPIC_API_KEY" and not (k.startswith("CLAUDE_CODE_") and not v)}
        with tempfile.TemporaryDirectory() as cwd:   # dossier vide : rien à lire ni modifier
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                      env=env, cwd=cwd, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise TransientError(self.name, f"pas de réponse après {self.timeout} s") from None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {"is_error": True, "result": (proc.stdout + proc.stderr).strip()}
        text = str(data.get("result", ""))
        if data.get("is_error") or proc.returncode != 0:
            raise self._error(text or proc.stderr)
        usage = data.get("usage") or {}
        return ChatResult(text, self.name, model,
                          usage.get("input_tokens", 0), usage.get("output_tokens", 0))

    def _error(self, text: str) -> ProviderError:
        if self._LIMIT.search(text):
            # Claude Code indique parfois l'heure de recharge : "…limit reached|1760371200"
            reset = re.search(r"\|(\d{10})", text)
            wait = int(reset.group(1)) - time.time() if reset else None
            return QuotaExhausted(self.name, f"quota de l'abonnement atteint ({text[:120]})",
                                  retry_after=wait if wait and wait > 0 else None)
        if re.search(r"(login|api key|authenticat)", text, re.I):
            return AuthError(self.name, f"Claude Code n'est pas connecté : {text[:200]}")
        return TransientError(self.name, f"échec de Claude Code : {text[:300]}")


class MockProvider(Provider):
    """Faux fournisseur pour tester tout le système sans clé ni réseau.

    `fail_with` ("quota", "rate", "transient", "auth") et `fail_times`
    simulent des pannes, pour vérifier la bascule.
    """

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.calls = 0
        self._failures_left = cfg.get("fail_times", 0)
        self._round = {"developer": 0, "reviewer": 0}

    def chat(self, model, system, messages, max_tokens):
        self.calls += 1
        if self._failures_left:
            self._failures_left -= 1
            kind = self.cfg.get("fail_with", "quota")
            error = {"quota": QuotaExhausted, "rate": RateLimited,
                     "transient": TransientError, "auth": AuthError}.get(kind, ProviderError)
            raise error(self.name, f"panne simulée ({kind})")
        role = re.search(r"\[role:(\w+)\]", system)
        role = role.group(1) if role else "general"
        return ChatResult(self._answer(role), self.name, model)

    def _answer(self, role: str) -> str:
        if role == "architect":
            return ("## Spécification\nModule `calc.py` avec `add(a, b)` et `divide(a, b)`.\n"
                    "`divide` lève `ValueError` si b vaut 0.\n\n## Fichiers\n- calc.py\n- test_calc.py\n")
        if role == "developer":
            self._round["developer"] += 1
            guard = ("    if b == 0:\n        raise ValueError(\"division par zéro\")\n"
                     if self._round["developer"] > 1 else "")
            return ("### FILE: calc.py\n```python\n"
                    "def add(a, b):\n    return a + b\n\n\n"
                    f"def divide(a, b):\n{guard}    return a / b\n```\n")
        if role == "tester":
            return ("### FILE: test_calc.py\n```python\nimport unittest\n\nfrom calc import add, divide\n\n\n"
                    "class TestCalc(unittest.TestCase):\n"
                    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
                    "    def test_divide_by_zero(self):\n"
                    "        with self.assertRaises(ValueError):\n            divide(1, 0)\n\n\n"
                    "if __name__ == \"__main__\":\n    unittest.main()\n```\n")
        if role == "reviewer":
            self._round["reviewer"] += 1
            if self._round["reviewer"] == 1:
                return json.dumps({"verdict": "revise", "score": 5, "issues": [
                    {"target": "code", "severity": "major",
                     "description": "divide(1, 0) lève ZeroDivisionError au lieu de ValueError."}]})
            return json.dumps({"verdict": "approve", "score": 9, "issues": []})
        return "OK"


PROVIDER_TYPES = {
    "anthropic": AnthropicProvider,
    "claude_code": ClaudeCodeProvider,
    "openai_compat": OpenAICompatProvider,
    "mock": MockProvider,
}


def build_providers(config: dict) -> dict[str, Provider]:
    providers = {}
    for name, cfg in config.get("providers", {}).items():
        cls = PROVIDER_TYPES.get(cfg.get("type", "openai_compat"))
        if cls is None:
            raise ValueError(f"type de fournisseur inconnu pour {name} : {cfg.get('type')}")
        providers[name] = cls(name, cfg)
    return providers
