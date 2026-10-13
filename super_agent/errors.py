"""Erreurs normalisées des fournisseurs d'IA.

Chaque fournisseur renvoie ses erreurs à sa façon. On les ramène ici à quelques
catégories qui décident de ce que fait le routeur : basculer, patienter, ou
abandonner.
"""
from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status
        self.retry_after = retry_after


class QuotaExhausted(ProviderError):
    """Crédit ou quota épuisé : on met le fournisseur de côté longtemps."""


class RateLimited(ProviderError):
    """Trop de requêtes : on met le fournisseur de côté quelques secondes."""


class TransientError(ProviderError):
    """Panne passagère (5xx, surcharge, réseau) : on essaie ailleurs."""


class AuthError(ProviderError):
    """Clé absente ou refusée : le fournisseur est désactivé pour la session."""


class FatalRequestError(ProviderError):
    """Requête invalide : changer de fournisseur ne réglerait rien."""


class NoModelAvailable(Exception):
    """Aucun modèle utilisable pour cette tâche (tous épuisés ou en pause)."""


_QUOTA_HINTS = (
    "credit balance",        # Anthropic : "Your credit balance is too low"
    "insufficient_quota",    # OpenAI
    "billing",
    "quota exceeded",
    "exceeded your current quota",
    "resource_exhausted",    # Google
    "payment required",
    "insufficient credits",  # OpenRouter
)


def classify(provider: str, status: int, body: str,
             retry_after: float | None = None) -> ProviderError:
    """Transforme une réponse HTTP en erreur typée."""
    text = (body or "").lower()
    looks_like_quota = any(hint in text for hint in _QUOTA_HINTS)

    if status == 402 or (status in (400, 403, 429) and looks_like_quota):
        return QuotaExhausted(provider, f"quota ou crédit épuisé ({status})", status, retry_after)
    if status == 429:
        return RateLimited(provider, "limite de débit atteinte (429)", status, retry_after)
    if status in (401, 403):
        return AuthError(provider, f"authentification refusée ({status})", status)
    if status >= 500 or status in (408, 409, 529):
        return TransientError(provider, f"erreur temporaire ({status})", status, retry_after)
    return FatalRequestError(provider, f"requête refusée ({status}) : {body[:300]}", status)
