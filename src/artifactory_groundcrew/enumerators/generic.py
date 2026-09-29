"""An explicit list of files, for upstreams with no index to read."""

from __future__ import annotations

from ..config import GenericSource
from ..model import Listing, RemoteFile, Upstream


def probe_generic(up: Upstream, src: GenericSource) -> dict[str, str]:
    # No index to read; the list itself is the only thing that changes.
    return {}


def enumerate_generic(up: Upstream, src: GenericSource) -> Listing:
    return Listing(packages=[RemoteFile(path=p) for p in src.paths])
