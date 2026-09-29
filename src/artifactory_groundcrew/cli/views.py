"""Read-only data for the CLI. Returns plain dicts and never prints."""

from __future__ import annotations

import time
from typing import Any

from ..artifactory import ArtifactoryClient
from ..config import Settings
from ..state import State
from ..sync import plan as make_plan


def mirrors(settings: Settings) -> list[dict[str, Any]]:
    out = []
    for m in settings.mirrors:
        src = getattr(m, m.type)
        detail = {
            "yum": lambda s: ", ".join(s.paths),
            "apt": lambda s: f"{s.base} (flat) {','.join(s.architectures) or 'all arches'}" if s.flat
            else f"{s.base} {','.join(s.suites)} [{','.join(s.components)}] {','.join(s.architectures)}",
            "generic": lambda s: f"{len(s.paths)} file(s)",
        }[m.type](src)
        out.append({"name": m.name, "tier": m.tier, "type": m.type, "remote": m.remote, "local": m.local,
                    "scope": m.scope or "/", "interval": m.interval_seconds, "enabled": m.enabled, "source": detail})
    return out


def status(settings: Settings, state: State) -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for m in settings.mirrors:
        st = state.mirror(m.name)
        last = next(iter(state.cycles(m.name, limit=1)), {})
        out.append({
            "name": m.name,
            "status": st.get("status", "never run"),
            "last_ok": _ago(now, st.get("last_ok")),
            "last_run": _ago(now, st.get("last_run")),
            "last_cycle": last.get("status", ""),
            "fetched": last.get("fetched", 0),
            "deleted": last.get("deleted", 0),
            "error": last.get("error", ""),
        })
    return out


def cycles(state: State, mirror: str | None, limit: int) -> list[dict[str, Any]]:
    rows = state.cycles(mirror, limit)
    return [{
        "when": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r.get("started", 0))),
        "mirror": r.get("mirror", ""),
        "status": r.get("status", ""),
        "seconds": r.get("seconds", 0),
        "fetched": r.get("fetched", 0),
        "metadata": r.get("metadata", 0),
        "deleted": r.get("deleted", 0),
        "failed": r.get("failed_count", 0),
        "error": r.get("error", ""),
    } for r in rows]


def plan(client: ArtifactoryClient, settings: Settings, state: State, name: str, full: bool, show: int) -> dict[str, Any]:
    m = settings.mirror(name)
    p, _ = make_plan(client, settings, m, state, full=full)
    out: dict[str, Any] = {"mirror": name, "unchanged": p.unchanged, **p.summary()}
    out["sample_fetch"] = [f.path for f in p.fetch[:show]]
    out["sample_metadata"] = [f.path for f in p.metadata[:show]]
    out["sample_delete"] = p.delete[:show]
    return out


def _ago(now: float, ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(now - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return f"{s}s ago"
