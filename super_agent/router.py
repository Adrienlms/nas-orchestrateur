"""Choix du modèle le plus compétent et bascule entre fournisseurs.

Le routeur classe les modèles configurés selon leur note pour le type de tâche
(architecture, code, tests, relecture), écarte ceux dont le fournisseur est
indisponible, puis essaie les candidats dans l'ordre jusqu'à obtenir une réponse.

Quand un fournisseur renvoie « quota épuisé », il est mis en pause (par défaut
une heure) et cette pause est enregistrée sur disque : le prochain lancement ne
le réessaiera pas inutilement.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .errors import (AuthError, BudgetExceeded, FatalRequestError, NoModelAvailable, ProviderError,
                     QuotaExhausted, RateLimited, TransientError)
from .providers import ChatResult, Provider

TASK_KINDS = ("architecture", "code", "test", "review")


@dataclass
class ModelSpec:
    provider: str
    name: str
    cost: int = 3                      # 0 = gratuit … 5 = très cher
    scores: dict[str, float] = field(default_factory=dict)

    def score(self, kind: str) -> float:
        return self.scores.get(kind, self.scores.get("default", 5))


class HealthState:
    """Pauses des fournisseurs, persistées dans un petit fichier JSON."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.pauses: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self.pauses = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                self.pauses = {}

    def pause(self, provider: str, seconds: float, reason: str, persist: bool = True):
        self.pauses[provider] = {"until": time.time() + seconds, "reason": reason,
                                 "persist": persist}
        self._save()

    def paused(self, provider: str) -> dict | None:
        info = self.pauses.get(provider)
        if info and info["until"] > time.time():
            return info
        return None

    def clear(self, provider: str | None = None):
        if provider:
            self.pauses.pop(provider, None)
        else:
            self.pauses.clear()
        self._save()

    def _save(self):
        if not self.path:
            return
        keep = {k: v for k, v in self.pauses.items()
                if v.get("persist", True) and v["until"] > time.time()}
        self.path.write_text(json.dumps(keep, indent=2))


class Router:
    def __init__(self, models: list[ModelSpec], providers: dict[str, Provider],
                 settings: dict | None = None, state: HealthState | None = None,
                 log: Callable[[str], None] = print):
        self.models = models
        self.providers = providers
        self.settings = settings or {}
        self.state = state or HealthState(None)
        self.log = log
        self.usage: list[dict] = []

    # ---- sélection -------------------------------------------------------
    def candidates(self, kind: str, avoid_provider: str | None = None) -> list[ModelSpec]:
        usable = [m for m in self.models
                  if m.provider in self.providers
                  and self.providers[m.provider].available()
                  and not self.state.paused(m.provider)]
        priority = lambda m: self.providers[m.provider].cfg.get("priority", 50)

        if self.settings.get("strategy", "best") == "economy":
            # Le moins cher parmi ceux jugés assez bons, puis les autres.
            min_score = self.settings.get("min_score", 7)
            good = sorted((m for m in usable if m.score(kind) >= min_score),
                          key=lambda m: (m.cost, -m.score(kind), priority(m)))
            rest = sorted((m for m in usable if m.score(kind) < min_score),
                          key=lambda m: (-m.score(kind), m.cost))
            ranked = good + rest
        else:
            ranked = sorted(usable, key=lambda m: (-m.score(kind), priority(m), m.cost))

        if avoid_provider:
            # Regard croisé : on préfère un autre fournisseur, sans s'interdire le même.
            ranked = ([m for m in ranked if m.provider != avoid_provider]
                      + [m for m in ranked if m.provider == avoid_provider])
        return ranked

    # ---- appel avec bascule --------------------------------------------
    def complete(self, kind: str, system: str, messages: list[dict],
                 avoid_provider: str | None = None) -> ChatResult:
        max_tokens = self.settings.get("max_tokens", 8000)
        attempts = []
        for model in self.candidates(kind, avoid_provider):
            if self.state.paused(model.provider):   # mis en pause pendant cette boucle
                continue
            provider = self.providers[model.provider]
            try:
                self._check_budget()
                result = provider.chat(model.name, system, messages, max_tokens)
            except QuotaExhausted as e:
                secs = e.retry_after or self.settings.get("quota_cooldown_minutes", 60) * 60
                self.state.pause(model.provider, secs, "quota épuisé")
                self._fallback_note(model, e, attempts)
            except RateLimited as e:
                secs = e.retry_after or self.settings.get("rate_limit_cooldown_seconds", 60)
                self.state.pause(model.provider, secs, "limite de débit", persist=False)
                self._fallback_note(model, e, attempts)
            except TransientError as e:
                secs = self.settings.get("transient_cooldown_seconds", 20)
                self.state.pause(model.provider, secs, "erreur temporaire", persist=False)
                self._fallback_note(model, e, attempts)
            except AuthError as e:
                self.state.pause(model.provider, 10 ** 9, "clé refusée", persist=False)
                self._fallback_note(model, e, attempts)
            except FatalRequestError:
                raise
            except ProviderError as e:
                self._fallback_note(model, e, attempts)
            else:
                self._record(kind, result)
                return result
        detail = "; ".join(attempts) or "aucun fournisseur configuré avec une clé"
        raise NoModelAvailable(f"aucun modèle disponible pour « {kind} » ({detail})")

    # ---- session agent (Claude Code avec outils) ------------------------
    def agent_provider(self) -> Provider | None:
        """Le premier fournisseur prêt capable de travailler en session avec outils."""
        ready = [p for name, p in self.providers.items()
                 if p.supports_agent and p.available() and not self.state.paused(name)]
        return min(ready, key=lambda p: p.cfg.get("priority", 50), default=None)

    def run_agent(self, model: str, system: str, prompt: str, cwd: str,
                  max_turns: int, resume: str | None = None) -> ChatResult:
        provider = self.agent_provider()
        if provider is None:
            raise NoModelAvailable("aucun abonnement Claude Code prêt pour le mode agent")
        self._check_budget()
        try:
            result = provider.agent(model, system, prompt, cwd, max_turns=max_turns,
                                    resume=resume)
        except QuotaExhausted as e:
            secs = e.retry_after or self.settings.get("quota_cooldown_minutes", 60) * 60
            self.state.pause(provider.name, secs, "quota épuisé")
            raise NoModelAvailable(str(e)) from None
        except AuthError as e:
            self.state.pause(provider.name, 10 ** 9, "clé refusée", persist=False)
            raise NoModelAvailable(str(e)) from None
        self._record("agent", result)
        return result

    # ---- consommation ---------------------------------------------------
    def _check_budget(self):
        limit = self.settings.get("max_calls_per_task", 12)
        if limit and len(self.usage) >= limit:
            raise BudgetExceeded(f"budget atteint : {limit} appels pour cette tâche "
                                 "(réglage max_calls_per_task)")

    def _record(self, kind: str, result: ChatResult):
        self.usage.append({"kind": kind, "provider": result.provider, "model": result.model,
                           "in": result.input_tokens, "out": result.output_tokens,
                           "cost": result.cost_usd, "turns": result.turns})

    def _fallback_note(self, model: ModelSpec, err: Exception, attempts: list[str]):
        attempts.append(f"{model.provider}/{model.name}: {err}")
        self.log(f"  ↪ {model.provider}/{model.name} indisponible ({err}), bascule…")


def status_report(router: Router) -> dict:
    """État des fournisseurs et classement par tâche (utilisé par la CLI et l'interface)."""
    providers = []
    for name, p in router.providers.items():
        pause = router.state.paused(name)
        if not p.available():
            state, detail = "off", ("pas de clé ou commande absente" if p.cfg.get("enabled", True)
                                    else "désactivé")
        elif pause:
            state, detail = "paused", pause["reason"]
        else:
            state, detail = "ready", ""
        providers.append({"name": name, "type": p.cfg.get("type", "openai_compat"),
                          "state": state, "detail": detail,
                          "until": pause["until"] if pause else None})
    ranking = {kind: [{"provider": m.provider, "model": m.name, "score": m.score(kind)}
                      for m in router.candidates(kind)[:3]] for kind in TASK_KINDS}
    return {"providers": providers, "ranking": ranking}
