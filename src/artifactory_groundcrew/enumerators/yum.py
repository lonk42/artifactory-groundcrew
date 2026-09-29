"""yum/dnf repositories: repomd.xml, then primary for the package list."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET

from ..config import YumSource
from ..model import EnumerationError, Listing, RemoteFile, Upstream, decompress, sha256, verify

_REPO = "{http://linux.duke.edu/metadata/repo}"
_COMMON = "{http://linux.duke.edu/metadata/common}"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
# Signature and key files some repositories publish beside repomd.xml.
_ROOT_EXTRAS = ("repomd.xml.key", "repomd.xml.asc")


def probe_yum(up: Upstream, src: YumSource) -> dict[str, str]:
    """Read only each repomd.xml. Unchanged roots mean an unchanged repository."""
    roots = {}
    for root in src.paths:
        path = f"{root}repodata/repomd.xml"
        body = up.read_metadata(path)
        if body is None:
            raise EnumerationError(f"{path}: not found upstream")
        roots[path] = sha256(body)
    return roots


def enumerate_yum(up: Upstream, src: YumSource) -> Listing:
    listing = Listing()
    seen: set[str] = set()
    for root in src.paths:
        _one(up, root, listing, seen)
    return listing


def _one(up: Upstream, root: str, listing: Listing, seen: set[str]) -> None:
    repomd_path = f"{root}repodata/repomd.xml"
    repomd = up.read_metadata(repomd_path)
    if repomd is None:
        raise EnumerationError(f"{repomd_path}: not found upstream")

    data_files: list[RemoteFile] = []
    primary: RemoteFile | None = None
    for data in ET.fromstring(repomd).iter(f"{_REPO}data"):
        loc = data.find(f"{_REPO}location")
        if loc is None:
            continue
        if loc.get(_XML_BASE):
            raise EnumerationError(f"{repomd_path}: xml:base locations are not supported")
        ck = data.find(f"{_REPO}checksum")
        size = data.findtext(f"{_REPO}size")
        rf = RemoteFile(
            path=root + loc.get("href", ""),
            size=int(size) if size else None,
            sha256=ck.text.strip() if ck is not None and ck.get("type") == "sha256" and ck.text else None,
        )
        data_files.append(rf)
        if data.get("type") == "primary":
            primary = rf
    if primary is None:
        raise EnumerationError(f"{repomd_path}: no primary metadata")

    raw = up.read_metadata(primary.path)
    if raw is None:
        raise EnumerationError(f"{primary.path}: listed in repomd.xml but not found upstream")
    verify(primary.path, raw, primary.sha256)
    for pkg in _packages(root, decompress(primary.path, raw), primary.path):
        if pkg.path not in seen:
            seen.add(pkg.path)
            listing.packages.append(pkg)

    listing.metadata.extend(data_files)
    extras = []
    for name in _ROOT_EXTRAS:
        path = f"{root}repodata/{name}"
        body = up.read_metadata(path)
        if body is not None:
            extras.append(RemoteFile(path=path, size=len(body), sha256=sha256(body), root=True))
    # A client reads repomd.xml, then its signature; publish them in that order,
    # with any key first.
    keys = [e for e in extras if e.path.endswith(".key")]
    sigs = [e for e in extras if not e.path.endswith(".key")]
    listing.metadata.extend(keys)
    listing.metadata.append(RemoteFile(path=repomd_path, size=len(repomd), sha256=sha256(repomd), root=True))
    listing.metadata.extend(sigs)
    listing.roots[repomd_path] = sha256(repomd)


def _packages(root: str, xml: bytes, source: str):
    for _, el in ET.iterparse(io.BytesIO(xml), events=("end",)):
        if el.tag != f"{_COMMON}package":
            continue
        loc = el.find(f"{_COMMON}location")
        if loc is None:
            el.clear()
            continue
        if loc.get(_XML_BASE):
            raise EnumerationError(f"{source}: xml:base package locations are not supported")
        size = el.find(f"{_COMMON}size")
        ck = el.find(f"{_COMMON}checksum")
        yield RemoteFile(
            path=root + loc.get("href", ""),
            size=int(size.get("package")) if size is not None and size.get("package") else None,
            sha256=ck.text.strip() if ck is not None and ck.get("type") == "sha256" and ck.text else None,
        )
        el.clear()
