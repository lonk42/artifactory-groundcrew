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
            "tree": lambda s: f"{s.filelist} include {len(s.include) or 'all'}, exclude {len(s.exclude)}",
            "generic": lambda s: f"{len(s.paths)} file(s)",
            "pypi": lambda s: f"{len(s.requirements)} requirement(s) for "
            + ", ".join(f"{t.python} {'/'.join(t.platforms)}" for t in s.targets),
            "npm": lambda s: f"{len(s.packages)} package(s) for "
            + (f"{'/'.join(s.os) or 'any os'} {'/'.join(s.cpu) or 'any cpu'}"),
            "docker": lambda s: f"{len(s.images)} image(s) for {', '.join(s.platforms) or 'all platforms'}",
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
        pause = state.paused(m.name)
        out.append({
            "name": m.name,
            "status": st.get("status", "never run"),
            "paused": f"{pause['scope']}, {_ago(now, pause.get('since'))}" if pause else "",
            "run_requested": _ago(now, req.get("since")) if (req := state.requested(m.name)) else "",
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


def provenance(client: ArtifactoryClient, settings: Settings, name: str, source: str | None) -> list[dict[str, Any]]:
    """Why each tier 2 file was mirrored, from the properties set when it was copied in."""
    m = settings.mirror(name)
    rows = []
    for path, props in sorted(client.list_properties(m.local, "groundcrew.").items()):
        if source and props.get("groundcrew.source") != source:
            continue
        rows.append({
            # Docker file names repeat across images and platforms; package names do not.
            "file": path if m.type == "docker" else path.rsplit("/", 1)[-1],
            "source": props.get("groundcrew.source", ""),
            "requirement": props.get("groundcrew.requirement", ""),
            "pulled_by": props.get("groundcrew.pulled_by", ""),
            "ingested": props.get("groundcrew.ingested", ""),
        })
    return rows


def _ago(now: float, ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(now - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return f"{s}s ago"
