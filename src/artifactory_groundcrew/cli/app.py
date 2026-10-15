"""The ``groundcrew`` operator command. Argument wiring only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from .. import log, repos
from ..artifactory import ArtifactoryClient
from ..config import load
from ..model import EnumerationError
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
    pa = sub.add_parser("pause", help="stop the daemon starting cycles, for one mirror or all")
    pa.add_argument("mirror", nargs="?", help="omit to pause every mirror")
    pa.add_argument("--reason", default="", help="shown by status and logged by the daemon")
    re_ = sub.add_parser("resume", help="lift a pause set by pause")
    re_.add_argument("mirror", nargs="?", help="omit to lift the global pause")
    rn = sub.add_parser("run", help="ask the daemon to run a cycle at its next loop, paused or not")
    rn.add_argument("mirror")
    rn.add_argument("--full", action="store_true", help="list everything even if upstream is unchanged")
    s = sub.add_parser("sync", help="run one cycle now in this process, paused or not")
    s.add_argument("mirror")
    s.add_argument("--full", action="store_true", help="list everything even if upstream is unchanged")
    pv = sub.add_parser("provenance", help="why each file of a tier 2 mirror was mirrored")
    pv.add_argument("mirror")
    pv.add_argument("--source", choices=["config", "demand"], help="only files from this source")
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
    if args.cmd in ("pause", "resume", "run"):
        if args.mirror and args.mirror not in {m.name for m in settings.mirrors}:
            print(f"groundcrew: no mirror named {args.mirror!r}", file=sys.stderr)
            return 2
        if args.cmd == "run":
            state.request(args.mirror, args.full)
            print(f"requested a cycle of {args.mirror}; the daemon starts it within {settings.idle_seconds}s"
                  " of finishing any cycle in progress. Follow it with: groundcrew cycles " + args.mirror)
            return 0
        target = args.mirror or "all mirrors"
        if args.cmd == "pause":
            state.pause(args.mirror, args.reason)
            print(f"paused {target}; a cycle already running finishes first")
        elif state.resume(args.mirror):
            print(f"resumed {target}")
        else:
            print(f"{target} was not paused", file=sys.stderr)
        if args.cmd == "resume" and args.mirror and state.paused(args.mirror):
            print(f"{args.mirror} is still held by the pause on all mirrors", file=sys.stderr)
        return 0

    try:
        return _with_client(args, settings, state, out)
    except httpx.HTTPError as exc:
        print(f"groundcrew: {settings.artifactory_url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except EnumerationError as exc:
        print(f"groundcrew: upstream could not be listed: {exc}", file=sys.stderr)
        return 1


def _with_client(args, settings, state, out) -> int:
    with ArtifactoryClient.from_settings(settings) as client:
        if args.cmd == "plan":
            out(views.plan(client, settings, state, args.mirror, args.full, args.show), "block")
            return 0
        if args.cmd == "sync":
            result = run_cycle(client, settings, settings.mirror(args.mirror), state, full=args.full)
            out(result.row(), "block")
            return 0 if result.status in ("ok", "unchanged") else 1
        if args.cmd == "provenance":
            out(views.provenance(client, settings, args.mirror, args.source), "table")
            return 0
        if args.cmd == "repos":
            data = [a.__dict__ for a in repos.ensure(client, settings, dry_run=not args.apply)]
            out(data, "table")
            return 1 if any(a["action"] == "conflict" for a in data) else 0
    return 2
