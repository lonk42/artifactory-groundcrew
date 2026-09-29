"""Durable state: per-mirror results, the cycle ledger, and per-mirror locks.

Nothing here is a source of truth for mirror contents. Upstream and the local
repository are re-read every cycle; this only remembers what the last cycle saw,
so an unchanged upstream can be skipped and an operator can see what happened.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


class State:
    def __init__(self, root: Path, ledger_keep_rows: int = 5000) -> None:
        self.root = root
        self.ledger_keep_rows = ledger_keep_rows

    def mirror_path(self, name: str) -> Path:
        return self.root / "mirrors" / f"{name}.json"

    def mirror(self, name: str) -> dict[str, Any]:
        return read_json(self.mirror_path(name)) or {}

    def save_mirror(self, name: str, data: dict[str, Any]) -> None:
        write_json_atomic(self.mirror_path(name), data)

    @property
    def ledger_path(self) -> Path:
        return self.root / "cycles.jsonl"

    def record(self, row: dict[str, Any]) -> None:
        """Append one cycle row. Best effort: a breadcrumb must never fail a cycle."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            self._trim()
        except OSError:
            pass

    def _trim(self) -> None:
        # Trim with slack so the file is not rewritten on every append.
        lines = self.ledger_path.read_text().splitlines()
        if len(lines) > self.ledger_keep_rows + 1000:
            tmp = self.ledger_path.with_name(f"cycles.jsonl.tmp-{os.getpid()}")
            tmp.write_text("\n".join(lines[-self.ledger_keep_rows:]) + "\n")
            os.replace(tmp, self.ledger_path)

    def cycles(self, mirror: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.ledger_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if mirror is None or row.get("mirror") == mirror:
                rows.append(row)
                if len(rows) >= limit:
                    break
        return rows

    @contextlib.contextmanager
    def lock(self, name: str) -> Iterator[bool]:
        """Non-blocking per-mirror lock. Yields False when another process holds it."""
        path = self.root / "locks" / f"{name}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                f.seek(0)
                f.truncate()
                f.write(f"{os.getpid()} {int(time.time())}\n")
                f.flush()
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
