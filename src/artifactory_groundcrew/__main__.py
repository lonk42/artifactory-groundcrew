"""The daemon: sync each mirror when it is due, forever.

It does not exit on errors. An unreachable Artifactory idles the loop and
retries; a configuration error parks the process and restates the reason every
300 seconds, so a misconfiguration is visible in the log rather than as a
restart count. SIGTERM ends it normally.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from . import __version__, log, repos
from .artifactory import ArtifactoryClient
from .config import Settings, load
from .state import State
from .sync import run_cycle

logger = log.get("main")
PARK_SECONDS = 300

_stopping = False


def _stop(*_: object) -> None:
    global _stopping
    _stopping = True


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stopping and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def _park(reason: str, **kw: object) -> int:
    while not _stopping:
        logger.error("parked", reason=reason, **kw)
        _sleep(PARK_SECONDS)
    return 0


def config_path() -> Path:
    return Path(os.environ.get("GROUNDCREW_CONFIG", "/etc/groundcrew/config.yaml"))


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.configure(os.environ.get("GROUNDCREW_LOG_LEVEL", "INFO"), os.environ.get("GROUNDCREW_LOG_FORMAT", "console"))
    try:
        settings = load(config_path())
    except (ValidationError, ValueError, OSError) as exc:
        return _park("invalid configuration", error=str(exc))
    log.configure(settings.log_level, settings.log_format)
    logger.info("started", version=__version__, mirrors=len(settings.mirrors),
                create_repositories=settings.create_repositories, url=settings.artifactory_url)
    try:
        return _loop(settings)
    except Exception as exc:  # noqa: BLE001 - never exit on an unexpected error
        return _park("unhandled error", error=f"{type(exc).__name__}: {exc}")


def _loop(settings: Settings) -> int:
    state = State(settings.state_dir, settings.ledger_keep_rows)
    client = ArtifactoryClient.from_settings(settings)
    repos_done = False
    attempt = 0
    while not _stopping:
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            logger.warning("artifactory.unreachable", attempt=attempt, error=str(exc))
            _sleep(settings.idle_seconds)
            continue
        if attempt:
            logger.info("artifactory.recovered", attempts=attempt)
            attempt = 0

        if settings.create_repositories and not repos_done:
            try:
                actions = repos.ensure(client, settings)
                conflicts = [a for a in actions if a.action == "conflict"]
                for a in conflicts:
                    logger.warning("repo.conflict", key=a.key, detail=a.detail)
                logger.info("repos.ensured", created=sum(a.action == "create" for a in actions),
                            existing=sum(a.action == "exists" for a in actions), conflicts=len(conflicts))
                repos_done = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("repos.failed", error=f"{type(exc).__name__}: {exc}")

        for mirror in settings.mirrors:
            if _stopping:
                break
            if not mirror.enabled or not _due(state, mirror.name, mirror.interval_seconds, settings.retry_seconds):
                continue
            run_cycle(client, settings, mirror, state)
        _sleep(settings.idle_seconds)
    logger.info("stopped")
    return 0


def _due(state: State, name: str, interval: int, retry: int) -> bool:
    """Due after its interval, or sooner after a cycle that did not succeed."""
    prev = state.mirror(name)
    wait = interval if prev.get("status", "ok") == "ok" else min(interval, retry)
    return time.time() - prev.get("last_run", 0) >= wait


if __name__ == "__main__":
    sys.exit(main())
