"""One mirror cycle: probe, plan, then packages, metadata, deletions, in that order.

The ordering is the guarantee. Metadata is published only once every package it
names is in place, and deletions run only after the metadata that stopped naming
them. A cycle with any failed package publishes nothing further, so the local
repository keeps serving its previous consistent state and the next cycle retries.

Tier 2 only adds: its list, resolved at least daily, and what clients pulled
through the remote since the last cycle. Each file it copies is tagged with why.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import demand, log
from .artifactory import ArtifactoryClient, LocalFile
from .config import Mirror, Settings
from .enumerators import docker, enumerate_mirror, probe
from .model import EnumerationError, Listing, RemoteFile, Upstream
from .state import State

logger = log.get("sync")

# Re-list everything at least this often even when upstream roots are unchanged,
# so files removed from the local repository by hand come back.
FULL_CHECK_SECONDS = 86400
PROGRESS_EVERY = 500
# A progress line at least this often while a stage runs, even if nothing finished.
PROGRESS_SECONDS = 5
# Failures logged one by one per stage; the rest are counted in the stage summary.
FAILURES_LOGGED = 50
# Upstream's modification time, on each file whose listing has no checksum.
MTIME_PROPERTY = "groundcrew.mtime"


class NotFoundUpstream(RuntimeError):
    pass


@dataclass
class Plan:
    mirror: str
    unchanged: bool = False
    listed: int = 0
    fetch: list[RemoteFile] = field(default_factory=list)
    metadata: list[RemoteFile] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    # Files to fetch that the local repository holds an older version of: the
    # remote's cached copy is stale too, and is expired first.
    refresh: set[str] = field(default_factory=set)
    in_scope: int = 0
    roots: dict[str, str] = field(default_factory=dict)
    # The configured list was listed or resolved in full this cycle.
    full: bool = False
    demand: int = 0
    unresolved: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "listed": self.listed,
            "fetch": len(self.fetch),
            "fetch_bytes": sum(f.size or 0 for f in self.fetch),
            "metadata": len(self.metadata),
            "delete": len(self.delete),
            "in_scope": self.in_scope,
            **({"demand": self.demand, "unresolved": len(self.unresolved)} if self.demand else {}),
        }


@dataclass
class Result:
    mirror: str
    status: str
    started: float
    seconds: float = 0.0
    fetched: int = 0
    metadata: int = 0
    deleted: int = 0
    failed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    error: str = ""
    plan: dict = field(default_factory=dict)

    def row(self) -> dict:
        d = asdict(self)
        d["failed_count"] = len(self.failed)
        d["failed"] = self.failed[:20]
        d["unresolved"] = self.unresolved[:20]
        d["error"] = " ".join(self.error.split())[:300]
        return {k: v for k, v in d.items() if v not in ("", [], {}, None)}


def _needs(want: RemoteFile, have: LocalFile | None, mtime: str | None = None) -> bool:
    if have is None:
        return True
    if want.sha256 and have.sha256:
        return want.sha256 != have.sha256
    if want.mtime is not None and mtime != str(want.mtime):
        return True
    if want.size is not None:
        return want.size != have.size
    return False


def plan(client: ArtifactoryClient, settings: Settings, mirror: Mirror, state: State, *, full: bool = False) -> tuple[Plan, Upstream]:
    up = Upstream(client, mirror.remote)
    p = Plan(mirror=mirror.name)
    prev = state.mirror(mirror.name)

    roots = probe(up, mirror)
    p.roots = roots
    stale = time.time() - prev.get("last_full", 0) > FULL_CHECK_SECONDS
    current = (
        not full
        and not stale
        and prev.get("status") == "ok"
        and prev.get("fingerprint") == mirror.fingerprint()
        and prev.get("roots") == roots
    )
    if mirror.tier == 2:
        return _plan_tier2(client, mirror, p, up, resolve_list=not current), up
    if current and roots:
        p.unchanged = True
        return p, up

    p.full = True
    listing = enumerate_mirror(up, mirror)
    p.roots = listing.roots or roots
    local = client.list_files(mirror.local)
    wanted = {f.path for f in listing.packages} | {m.path for m in listing.metadata}

    # One entry per path. Identical index files share a by-hash path, and two
    # workers on one path race: one expires it from the cache as the other copies.
    packages = list({f.path: f for f in listing.packages}.values())
    metadata = list({m.path: m for m in listing.metadata}.values())
    mtimes: dict[str, str | None] = {}
    if mirror.type == "tree":
        mtimes = {path: props.get(MTIME_PROPERTY) for path, props in client.list_properties(mirror.local, MTIME_PROPERTY).items()}
    p.listed = len(packages)
    p.fetch = [f for f in packages if _needs(f, local.get(f.path), mtimes.get(f.path))]
    p.metadata = [m for m in metadata if _needs(m, local.get(m.path), mtimes.get(m.path))]
    p.refresh = {f.path for f in p.fetch if f.mtime is not None and f.path in local}
    scoped = [path for path in local if path.startswith(mirror.scope) and not path.startswith(demand.ARTIFACTORY_OWNED)]
    p.in_scope = len(scoped)
    p.delete = sorted(path for path in scoped if path not in wanted)
    return p, up


def _plan_tier2(client: ArtifactoryClient, mirror: Mirror, p: Plan, up: Upstream, *, resolve_list: bool) -> Plan:
    """The list, when due, and demand. Nothing is deleted."""
    listing = enumerate_mirror(up, mirror) if resolve_list else Listing()
    p.full = resolve_list
    local = client.list_files(mirror.local)
    listed = {f.path for f in listing.packages}
    pulled = demand.pulled(client.list_files(f"{mirror.remote}-cache"), set(local) | listed, client.whoami())
    p.demand = len(pulled)
    wanted, p.unresolved = demand.expand(up, mirror, pulled) if pulled else ([], [])
    if resolve_list:
        moved, failures = demand.refresh(client, up, mirror, {f.requirement for f in listing.packages})
        wanted += moved
        p.unresolved += failures
    # The list's reason wins over demand's for a file both want.
    packages = list({f.path: f for f in [*wanted, *listing.packages]}.values())
    p.listed = len(packages)
    p.fetch = [f for f in packages if _needs(f, local.get(f.path))]
    if mirror.type == "docker":
        p.fetch = docker.one_copy_per_blob(p.fetch, local)
    if not resolve_list and not pulled:
        p.unchanged = True
    return p


def _parallel(workers: int, items: list, fn: Callable, label: str, mirror: str) -> list[tuple[object, Exception]]:
    """Run fn over items; return (item, error) for each failure."""
    failures: list[tuple[object, Exception]] = []
    done = 0
    last_log = time.monotonic()
    queued = iter(items)
    # A bounded window of submitted work: a future per item of a large stage
    # costs gigabytes before the first one finishes.
    window = workers * 4
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures: dict = {}

        def top_up() -> None:
            for it in queued:
                futures[ex.submit(fn, it)] = it
                if len(futures) >= window:
                    return

        top_up()
        pending = set(futures)
        while pending:
            due = max(0.0, last_log + PROGRESS_SECONDS - time.monotonic())
            finished, pending = wait(pending, timeout=due, return_when=FIRST_COMPLETED)
            milestone = False
            for fut in finished:
                done += 1
                milestone = milestone or done % PROGRESS_EVERY == 0
                item = futures.pop(fut)
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - every failure is recorded, none is fatal here
                    failures.append((item, exc))
                    if len(failures) <= FAILURES_LOGGED:
                        logger.warning("item.failed", mirror=mirror, stage=label, path=_path(item), error=_describe(exc))
            top_up()
            pending = set(futures)
            if pending and (milestone or time.monotonic() - last_log >= PROGRESS_SECONDS):
                last_log = time.monotonic()
                logger.info("progress", mirror=mirror, stage=label, done=done, total=len(items), failed=len(failures))
    if items:
        unlogged = max(0, len(failures) - FAILURES_LOGGED)
        getattr(logger, "warning" if failures else "info")(
            "stage.done", mirror=mirror, stage=label, total=len(items), failed=len(failures),
            **({"unlogged_failures": unlogged} if unlogged else {}))
    return failures


def _path(item: object) -> str:
    return getattr(item, "path", str(item))


def _describe(exc: Exception) -> str:
    """One line: the exception type and message, without httpx's multi-line help text."""
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:300]


def execute(client: ArtifactoryClient, settings: Settings, mirror: Mirror, p: Plan, up: Upstream, result: Result) -> None:
    remote, cache, local = mirror.remote, f"{mirror.remote}-cache", mirror.local

    def stamp(f: RemoteFile) -> None:
        if f.mtime is not None and not f.sha256:
            client.set_properties(local, f.path, {MTIME_PROPERTY: str(f.mtime)})

    def fetch(f: RemoteFile) -> None:
        # pip's listing has usually cached a PyPI package already. One it has
        # not can only be fetched through the remote's PyPI download route.
        # A Docker remote is only ever asked through the registry API: a Sync
        # Download 404 there is remembered, and hides the manifest for 30 minutes.
        if f.path in p.refresh and f.path not in up.fresh:
            client.expire(remote, f.path)
        if mirror.type == "docker":
            found = client.docker_fetch(remote, f.path)
        else:
            found = client.sync_download(remote, f.path) or (mirror.type == "pypi" and client.pypi_fetch(remote, f.path))
        if not found:
            raise NotFoundUpstream("404 through the remote")
        client.copy(cache, f.path, local)
        stamp(f)
        if f.source:
            props = {"groundcrew.source": f.source, "groundcrew.requirement": f.requirement,
                     "groundcrew.ingested": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            if f.pulled_by:
                props["groundcrew.pulled_by"] = f.pulled_by
            client.set_properties(local, f.path, props)

    failures = _parallel(settings.workers, p.fetch, fetch, "packages", mirror.name)
    result.fetched = len(p.fetch) - len(failures)
    if failures:
        result.status = "failed"
        result.failed = [f"{_path(it)}: {_describe(exc)}" for it, exc in failures]
        result.error = f"{len(failures)} package(s) failed; metadata and deletions withheld"
        return

    absent: list[str] = []

    def meta(m: RemoteFile) -> None:
        if m.path not in up.fresh:
            client.expire(remote, m.path)
            if not client.sync_download(remote, m.path):
                if m.optional:
                    absent.append(m.path)
                    return
                raise NotFoundUpstream("404 through the remote")
        client.copy(cache, m.path, local)
        stamp(m)

    body = [m for m in p.metadata if not m.root]
    roots = [m for m in p.metadata if m.root]
    failures = _parallel(settings.workers, body, meta, "metadata", mirror.name)
    if not failures:
        for m in roots:
            try:
                meta(m)
            except Exception as exc:  # noqa: BLE001
                failures.append((m, exc))
                break
    result.metadata = len(p.metadata) - len(failures) - len(absent)
    if failures:
        result.status = "failed"
        result.failed = [f"{_path(it)}: {_describe(exc)}" for it, exc in failures]
        result.error = "metadata failed; deletions withheld"
        return

    failures = _parallel(settings.workers, p.delete, lambda path: client.delete(local, path), "deletions", mirror.name)
    result.deleted = len(p.delete) - len(failures)
    if failures:
        result.status = "failed"
        result.failed = [f"{_path(it)}: {_describe(exc)}" for it, exc in failures]
        result.error = f"{len(failures)} deletion(s) failed"
        return
    result.status = "ok"


def run_cycle(client: ArtifactoryClient, settings: Settings, mirror: Mirror, state: State, *, full: bool = False, dry_run: bool = False) -> Result:
    result = Result(mirror=mirror.name, status="failed", started=time.time())
    t0 = time.monotonic()
    with state.lock(mirror.name) as held:
        if not held:
            result.status = "locked"
            result.error = "another process is syncing this mirror"
            return result
        try:
            p, up = plan(client, settings, mirror, state, full=full)
            result.plan = p.summary()
            result.unresolved = p.unresolved
            if p.unchanged:
                result.status = "unchanged"
            elif dry_run:
                result.status = "dry-run"
            else:
                logger.info("cycle.planned", mirror=mirror.name, **p.summary())
                execute(client, settings, mirror, p, up, result)
        except EnumerationError as exc:
            result.status = "enumeration-failed"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a cycle never takes the daemon down
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        result.seconds = round(time.monotonic() - t0, 1)

        if not dry_run:
            prev = state.mirror(mirror.name)
            now = time.time()
            new = {**prev, "status": result.status, "last_run": now}
            if result.status == "ok":
                new.update(last_ok=now)
                if p.full:
                    new.update(fingerprint=mirror.fingerprint(), roots=p.roots, last_full=now)
            elif result.status == "unchanged":
                new.update(status="ok", last_ok=now)
            state.save_mirror(mirror.name, new)
            state.record(result.row())
    level = "info" if result.status in ("ok", "unchanged", "dry-run") else "warning"
    for msg in result.unresolved:
        logger.warning("demand.unresolved", mirror=mirror.name, detail=msg)
    getattr(logger, level)("cycle.done", mirror=mirror.name, status=result.status, seconds=result.seconds,
                           fetched=result.fetched, metadata=result.metadata, deleted=result.deleted,
                           **({"failed": len(result.failed)} if result.failed else {}),
                           **({"error": result.error} if result.error else {}))
    return result
