"""Formatting for the CLI. The only module that turns data into text."""

from __future__ import annotations

from typing import Any


def table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(none)"
    cols = list(rows[0].keys())
    cells = [[_cell(r.get(c, "")) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    lines = ["  ".join(c.upper().ljust(w) for c, w in zip(cols, widths))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in cells]
    return "\n".join(line.rstrip() for line in lines)


def block(data: dict[str, Any]) -> str:
    width = max((len(k) for k in data), default=0)
    lines = []
    for k, v in data.items():
        if isinstance(v, list):
            lines.append(f"{k.ljust(width)}  {'' if v else '(none)'}")
            lines += [f"{''.ljust(width)}  {_cell(x)}" for x in v]
        else:
            lines.append(f"{k.ljust(width)}  {_cell(v)}")
    return "\n".join(lines)


def _cell(v: Any) -> str:
    s = " ".join(str(v).split())
    return s if len(s) <= 100 else s[:97] + "..."
