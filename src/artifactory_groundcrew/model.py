"""What an enumerator reports, and how upstream files are read."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import lzma
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import zstandard

if TYPE_CHECKING:
    from .artifactory import ArtifactoryClient


class EnumerationError(RuntimeError):
    """Upstream could not be listed completely. The cycle must not act on it."""


@dataclass(frozen=True)
class RemoteFile:
    """A file upstream publishes, at its path relative to the remote's root."""

    path: str
    size: int | None = None
    sha256: str | None = None
    # Listed by the index but allowed to be absent upstream (apt Release files
    # list uncompressed variants that often do not exist, and by-hash copies).
    optional: bool = False
    # Published after every other metadata file, in list order: the file a
    # client reads first (repomd.xml, InRelease) and its signatures.
    root: bool = False


@dataclass
class Listing:
    packages: list[RemoteFile] = field(default_factory=list)
    metadata: list[RemoteFile] = field(default_factory=list)
    # Root metadata as read this cycle, path -> sha256, for the up-to-date check.
    roots: dict[str, str] = field(default_factory=dict)


class Upstream:
    """Reads upstream files through a remote repository.

    Metadata is expired from the remote's cache before each read, since a
    Generic remote never re-fetches a cached file. Paths read this way are
    remembered as fresh, so the sync does not fetch them a second time.
    """

    def __init__(self, client: "ArtifactoryClient", remote: str) -> None:
        self.client = client
        self.remote = remote
        self.fresh: set[str] = set()

    def read_metadata(self, path: str) -> bytes | None:
        self.client.expire(self.remote, path)
        data = self.client.read(self.remote, path)
        if data is not None:
            self.fresh.add(path)
        return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decompress(name: str, data: bytes) -> bytes:
    """Decompress by file extension; unknown extensions pass through."""
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".bz2"):
        return bz2.decompress(data)
    if name.endswith(".zst"):
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as r:
            return r.read()
    return data


def verify(path: str, data: bytes, expected: str | None, check: Callable[[bytes], str] = sha256) -> None:
    """Refuse an index whose checksum disagrees with the file that names it.

    A mismatch means upstream is mid-update or a cache is stale; either way the
    listing is not trustworthy this cycle.
    """
    if expected and check(data) != expected:
        raise EnumerationError(f"{path}: checksum does not match its index; upstream may be mid-update")
