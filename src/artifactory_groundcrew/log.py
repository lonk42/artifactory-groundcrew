"""Logging: one readable line per event by default, structlog JSON on request."""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

import structlog


def _console(_: Any, __: str, event: dict[str, Any]) -> str:
    ts = event.pop("timestamp", "")
    level = event.pop("level", "info").upper()[:5]
    name = event.pop("event", "")
    mirror = event.pop("mirror", None)
    event.pop("logger", None)
    tag = f" mirror={mirror}" if mirror else ""
    rest = " ".join(f"{k}={_fmt(v)}" for k, v in event.items())
    return f"{ts} {level:<5}{tag} {name}{(' ' + rest) if rest else ''}"


def _fmt(v: Any) -> str:
    s = str(v)
    s = " ".join(s.split())
    return f'"{s}"' if " " in s else s


def configure(level: str = "INFO", fmt: str = "console", stream: IO[str] | None = None) -> None:
    stream = stream or sys.stdout
    renderer = structlog.processors.JSONRenderer() if fmt == "json" else _console
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )
    # httpx logs every request at INFO; keep it out of the operator's view.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get(name: str) -> Any:
    return structlog.get_logger(name)
