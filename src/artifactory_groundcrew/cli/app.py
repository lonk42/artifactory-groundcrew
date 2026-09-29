"""The ``groundcrew`` operator command. Argument wiring only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import log, repos
from ..artifactory import ArtifactoryClient
from ..config import load
from ..state import State
from ..sync import run_cycle
from . import render, views


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="groundcrew", description="Operate artifactory-groundcrew.")
    p.add_argument("--config", type=Path, default=Path(os.environ.get("GROUNDCREW_CONFIG", "/etc/groundcrew/config.yaml")))
    p.add_argument("--json", action="store_true", help="print JSON instead of tables")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mirrors", help="list configured mirrors")
    sub.add_parser("status", help="last result per mirror")
    c = sub.add_parser("cycles", help="recent cycles from the ledger")
    c.add_argument("mirror", nargs="?")
    c.add_argument("-n", type=int, default=20)
    pl = sub.add_parser("plan", help="what a cycle would do, without doing it")
    pl.add_argument("mirror")
    pl.add_argument("--full", action="store_true", help="list everything even if upstream is unchanged")
    pl.add_argument("--show", type=int, default=5, help="sample paths to print per list")
    s = sub.add_parser("sync", help="run one cycle now")
    s.add_argument("mirror")
    s.add_argument("--full", action="store_true", help="list everything even if upstream is unchanged")
    r = sub.add_parser("repos", help="show the repositories mirrors need")
    r.add_argument("--apply", action="store_true", help="create the missing ones")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log.configure("INFO" if args.verbose else "WARNING", "console", stream=sys.stderr)
    settings = load(args.config)
    state = State(settings.state_dir, settings.ledger_keep_rows)

    def out(data, kind: str) -> None:
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print(getattr(render, kind)(data))

    if args.cmd == "mirrors":
        out(views.mirrors(settings), "table")
        return 0
    if args.cmd == "status":
        out(views.status(settings, state), "table")
        return 0
    if args.cmd == "cycles":
        out(views.cycles(state, args.mirror, args.n), "table")
        return 0

    with ArtifactoryClient.from_settings(settings) as client:
        if args.cmd == "plan":
            out(views.plan(client, settings, state, args.mirror, args.full, args.show), "block")
            return 0
        if args.cmd == "sync":
            result = run_cycle(client, settings, settings.mirror(args.mirror), state, full=args.full)
            out(result.row(), "block")
            return 0 if result.status in ("ok", "unchanged") else 1
        if args.cmd == "repos":
            data = [a.__dict__ for a in repos.ensure(client, settings, dry_run=not args.apply)]
            out(data, "table")
            return 1 if any(a["action"] == "conflict" for a in data) else 0
    return 2
