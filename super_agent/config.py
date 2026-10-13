"""Chargement de la configuration et assemblage du routeur."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Callable

from .providers import build_providers
from .router import HealthState, ModelSpec, Router

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def load_config(path: str | Path | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, "rb") as f:
        return tomllib.load(f)


def build_router(config: dict, log: Callable[[str], None] = print,
                 state_dir: str | Path = ".") -> Router:
    settings = config.get("settings", {})
    models = [ModelSpec(m["provider"], m["name"], m.get("cost", 3), m.get("scores", {}))
              for m in config.get("models", [])]
    state_file = settings.get("state_file")
    state = HealthState(Path(state_dir) / state_file if state_file else None)
    return Router(models, build_providers(config), settings, state, log)
