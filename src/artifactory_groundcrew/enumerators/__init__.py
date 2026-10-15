"""One enumerator per repository format.

``probe`` reads only the root metadata, cheaply, so an unchanged upstream can be
recognised without reading its package index. ``enumerate`` lists everything.
"""

from __future__ import annotations

from typing import Callable

from ..config import Mirror
from ..model import Listing, Upstream
from .apt import enumerate_apt, probe_apt
from .docker import enumerate_docker, probe_docker
from .generic import enumerate_generic, probe_generic
from .npm import enumerate_npm, probe_npm
from .pypi import enumerate_pypi, probe_pypi
from .tree import enumerate_tree, probe_tree
from .yum import enumerate_yum, probe_yum

_PROBES: dict[str, Callable] = {"yum": probe_yum, "apt": probe_apt, "tree": probe_tree, "generic": probe_generic, "pypi": probe_pypi,
                               "npm": probe_npm, "docker": probe_docker}
_ENUMERATORS: dict[str, Callable] = {
    "yum": enumerate_yum, "apt": enumerate_apt, "tree": enumerate_tree, "generic": enumerate_generic, "pypi": enumerate_pypi,
    "npm": enumerate_npm, "docker": enumerate_docker,
}


def probe(up: Upstream, mirror: Mirror) -> dict[str, str]:
    return _PROBES[mirror.type](up, getattr(mirror, mirror.type))


def enumerate_mirror(up: Upstream, mirror: Mirror) -> Listing:
    if mirror.type == "tree":
        # A tree lists only what its scope owns, since nothing outside it is ever deleted.
        return enumerate_tree(up, mirror.tree, mirror.scope)
    return _ENUMERATORS[mirror.type](up, getattr(mirror, mirror.type))
