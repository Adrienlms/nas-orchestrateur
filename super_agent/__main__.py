"""Ligne de commande.

    python -m super_agent run "Écris un convertisseur CSV → JSON" --out projets/csv
    python -m super_agent status          # fournisseurs actifs, pauses, classement
    python -m super_agent reset           # efface les pauses mémorisées
    python -m super_agent demo            # démonstration hors ligne (faux fournisseurs)
    python -m super_agent web --port 8080 # interface web
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time

from .config import build_router, load_config
from .errors import NoModelAvailable
from .agent_mode import make_runner
from .orchestrator import Orchestrator, usage_line
from .router import status_report
from .workspace import Workspace

DEMO_CONFIG = {
    "settings": {"max_review_rounds": 3, "state_file": None},
    "providers": {
        # Simule un Claude dont le crédit est épuisé dès le premier appel.
        "claude": {"type": "mock", "priority": 1, "fail_with": "quota", "fail_times": 1},
        "gemini": {"type": "mock", "priority": 2},
        "mistral": {"type": "mock", "priority": 3},
    },
    "models": [
        {"provider": "claude", "name": "claude-mock", "cost": 3,
         "scores": {"architecture": 10, "code": 10, "test": 9, "review": 10}},
        {"provider": "gemini", "name": "gemini-mock", "cost": 0,
         "scores": {"architecture": 9, "code": 9, "test": 8, "review": 9}},
        {"provider": "mistral", "name": "codestral-mock", "cost": 1,
         "scores": {"architecture": 6, "code": 8.5, "test": 8.5, "review": 6}},
    ],
}


def cmd_run(args):
    config = load_config(args.config)
    if args.strategy:
        config.setdefault("settings", {})["strategy"] = args.strategy
    router = build_router(config)
    rounds = args.rounds or config.get("settings", {}).get("max_review_rounds", 2)
    ws = Workspace(args.out)
    report = make_runner(router, ws, rounds, run_tests=not args.no_tests, mode=args.mode).run(args.request)
    print(f"\n{'✅ Approuvé' if report.approved else '⚠ Non approuvé'} — "
          f"fichiers et REPORT.md dans {ws.root}\n{usage_line(router.usage)}")


def cmd_status(args):
    report = status_report(build_router(load_config(args.config)))
    print("Fournisseurs :")
    for p in report["providers"]:
        state = {"ready": "prêt", "off": "inactif", "paused": "en pause"}[p["state"]]
        if p["until"]:
            state += f" {int(p['until'] - time.time())} s"
        print(f"  {p['name']:<18} {state} {('(' + p['detail'] + ')') if p['detail'] else ''}")
    for kind, ranked in report["ranking"].items():
        names = ", ".join(f"{m['provider']}/{m['model']} ({m['score']})" for m in ranked) or "aucun"
        print(f"{kind:<13} → {names}")


def cmd_reset(args):
    build_router(load_config(args.config)).state.clear()
    print("Pauses effacées.")


def cmd_demo(args):
    out = args.out or tempfile.mkdtemp(prefix="super_agent_demo_")
    router = build_router(DEMO_CONFIG)
    report = Orchestrator(router, Workspace(out), 3).run("Une mini calculatrice add/divide")
    print(f"\n{'✅ Approuvé' if report.approved else '⚠ Non approuvé'} — voir {out}/REPORT.md")


def cmd_web(args):
    from .web import serve
    serve(args.host, args.port)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="super_agent")
    parser.add_argument("--config", help="chemin du config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="faire réaliser une demande par l'équipe d'agents")
    run.add_argument("request")
    run.add_argument("--out", default="sortie", help="dossier de travail (défaut : sortie)")
    run.add_argument("--rounds", type=int, help="tours de relecture maximum")
    run.add_argument("--strategy", choices=["best", "economy"])
    run.add_argument("--mode", choices=["agent", "team"],
                     help="agent : une session Claude Code avec outils (défaut) ; team : l'équipe multi-IA")
    run.add_argument("--no-tests", action="store_true",
                     help="ne pas exécuter les tests générés")
    run.set_defaults(func=cmd_run)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("reset").set_defaults(func=cmd_reset)
    demo = sub.add_parser("demo")
    demo.add_argument("--out")
    demo.set_defaults(func=cmd_demo)

    web = sub.add_parser("web", help="lancer l'interface web")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8080)
    web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except NoModelAvailable as e:
        sys.exit(f"Arrêt : {e}")


if __name__ == "__main__":
    main()
