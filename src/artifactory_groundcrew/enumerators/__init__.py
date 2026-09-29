"""One enumerator per repository format.

``probe`` reads only the root metadata, cheaply, so an unchanged upstream can be
recognised without reading its package index. ``enumerate`` lists everything.
"""

from __future__ import annotations

from typing import Callable

from ..config import Mirror
from ..model import Listing, Upstream
from .apt import enumerate_apt, probe_apt
from .generic import enumerate_generic, probe_generic
from .yum import enumerate_yum, probe_yum

_PROBES: dict[str, Callable] = {"yum": probe_yum, "apt": probe_apt, "generic": probe_generic}
_ENUMERATORS: dict[str, Callable] = {"yum": enumerate_yum, "apt": enumerate_apt, "generic": enumerate_generic}


def probe(up: Upstream, mirror: Mirror) -> dict[str, str]:
    return _PROBES[mirror.type](up, getattr(mirror, mirror.type))


def enumerate_mirror(up: Upstream, mirror: Mirror) -> Listing:
    return _ENUMERATORS[mirror.type](up, getattr(mirror, mirror.type))
