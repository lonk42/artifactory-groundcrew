"""Whole upstream trees, listed by the quick-fedora-mirror file list upstream publishes.

The list (``fullfiletimelist-<name>``) names every file, directory and symlink
under the upstream root, each with its modification time and size. A symlink's
size is the length of its target, and the target itself is not given.

A symlink to a file is mirrored as the file: Artifactory fetches its path, and
upstream's web server follows the link. A symlink to a directory, such as Rocky's
``9`` naming ``9.8``, is mirrored as a copy of that directory under its own name.
Its target is the sibling directory of the right name length whose files the
link serves, checked by reading one file through the link. A directory link
inside another link's target is mirrored only at its own path.

Files under ``repodata/`` are metadata, published after every other file, with
``repomd.xml`` and its key and signature last. Each ``repomd.xml`` is read, and
must name only files the list has, or upstream is mid-update. It and its key and
signature are compared by checksum; everything else by mtime and size.
"""

from __future__ import annotations

import io
import re
from dataclasses import replace

from .. import log
from ..config import TreeSource
from ..model import EnumerationError, Listing, RemoteFile, Upstream, sha256
from .yum import repomd_files

logger = log.get("tree")

# Read through a directory link to identify its target; a larger file is not read.
PROBE_MAX_BYTES = 16 << 20
# In publishing order: a client reads repomd.xml, then its signature.
_ROOTS = {"repomd.xml.key": 0, "repomd.xml": 1, "repomd.xml.asc": 2}


def probe_tree(up: Upstream, src: TreeSource) -> dict[str, str]:
    """The file list changes whenever anything in the tree does."""
    return {src.filelist: sha256(_filelist(up, src))}


def enumerate_tree(up: Upstream, src: TreeSource, scope: str = "") -> Listing:
    body = _filelist(up, src)
    # The list is read once per cycle; free it once parsed.
    up.kept.pop(src.filelist, None)

    def want(path: str) -> bool:
        return path.startswith(scope) and src.selects(path)

    dirs: dict[str, set[str]] = {}
    links: dict[str, tuple[int, int]] = {}
    for mtime, kind, size, path in _entries(src.filelist, body):
        if kind == "d":
            parent, _, name = path.rpartition("/")
            dirs.setdefault(parent, set()).add(name)
        elif kind == "l":
            links[path] = (mtime, size)

    # Directories a link could name: siblings whose name is as long as its target.
    targets: dict[str, list[str]] = {}
    for link, (_, size) in links.items():
        parent, _, _ = link.rpartition("/")
        targets[link] = sorted((_join(parent, n) for n in dirs.get(parent, ()) if len(n) == size), key=_natural, reverse=True)
    # Candidate targets by first path component, so each file is checked against few.
    by_top: dict[str, list[tuple[str, list[str]]]] = {}
    by_target: dict[str, list[str]] = {}
    for link, cands in targets.items():
        for c in cands:
            by_target.setdefault(c, []).append(link)
    for c, ls in by_target.items():
        by_top.setdefault(c.partition("/")[0], []).append((c, ls))

    files: dict[str, RemoteFile] = {}
    # Per candidate target: relative path -> (mtime, size, kind), for files a link would serve.
    under: dict[str, dict[str, tuple[int, int | None, str]]] = {}
    sibling_files: dict[str, set[int]] = {}
    link_parents = {link.rpartition("/")[0] for link in links}
    for mtime, kind, size, path in _entries(src.filelist, body):
        if kind == "d":
            continue
        if kind == "f":
            parent, _, name = path.rpartition("/")
            if parent in link_parents:
                sibling_files.setdefault(parent, set()).add(len(name))
            if want(path):
                files[path] = RemoteFile(path=path, size=size, mtime=mtime)
        for c, ls in by_top.get(path.partition("/")[0], ()):
            if path.startswith(c + "/"):
                rel = path[len(c) + 1:]
                if any(want(f"{link}/{rel}") for link in ls):
                    under.setdefault(c, {})[rel] = (mtime, size if kind == "f" else None, kind)

    # A link naming no directory may name another such link, which upstream follows too.
    for link in links:
        if not targets[link]:
            parent, _, name = link.rpartition("/")
            sibling_files.setdefault(parent, set()).add(len(name))
    file_links: set[str] = set()
    dir_links: list[str] = []
    for link, (mtime, size) in sorted(links.items()):
        is_dir = bool(targets[link])
        is_file = size in sibling_files.get(link.rpartition("/")[0], ())
        if is_file and not is_dir:
            file_links.add(link)
            if want(link):
                files[link] = RemoteFile(path=link, mtime=mtime)
        elif is_dir and not is_file:
            dir_links.append(link)
        elif want(link):
            reason = "a file and a directory beside it both fit its target" if is_dir else \
                "nothing beside it has a name of its target's length"
            logger.warning("tree.link_skipped", link=link, reason=reason)

    for link in dir_links:
        if not any(under.get(c) for c in targets[link]):
            continue
        target = _resolve(up, link, targets[link], under)
        for rel, (mtime, size, kind) in under[target].items():
            # A directory link inside the target is mirrored only at its own path.
            if kind == "l" and f"{target}/{rel}" not in file_links:
                continue
            path = f"{link}/{rel}"
            if want(path):
                files[path] = RemoteFile(path=path, size=size, mtime=mtime)

    return _listing(up, src, body, files)


def _filelist(up: Upstream, src: TreeSource) -> bytes:
    body = up.read_metadata(src.filelist, keep=True)
    if body is None:
        raise EnumerationError(f"{src.filelist}: not found upstream")
    return body


def _entries(name: str, body: bytes):
    """(mtime, kind, size, path) for each line of the list's [Files] section."""
    section = ""
    version = ""
    for n, raw in enumerate(io.BytesIO(body), 1):
        line = raw.rstrip(b"\r\n")
        if line.startswith(b"[") and line.endswith(b"]"):
            section = line[1:-1].decode()
            if section == "Files" and version != "2":
                raise EnumerationError(f"{name}: file list version {version or 'missing'} is not supported; only 2 is")
            continue
        if not line:
            continue
        if section == "Version":
            version = line.decode().strip()
        elif section == "Files":
            parts = line.split(b"\t", 3)
            try:
                mtime, kind, size, path = int(parts[0]), parts[1][:1].decode(), int(parts[2]), parts[3].decode()
            except (IndexError, ValueError) as exc:
                raise EnumerationError(f"{name}:{n}: not a file list line") from exc
            yield mtime, kind, size, path
    if not version:
        raise EnumerationError(f"{name}: not a quick-fedora-mirror file list")


def _resolve(up: Upstream, link: str, cands: list[str], under: dict[str, dict]) -> str:
    """The candidate directory a link serves, read through the link.

    A file is read whose path and size, within the candidates, only one has.
    """
    seen: dict[tuple[str, int], int] = {}
    for c in cands:
        for rel, (_, size, kind) in under.get(c, {}).items():
            if kind == "f":
                seen[(rel, size)] = seen.get((rel, size), 0) + 1
    for c in cands:
        unique = sorted((size, rel) for rel, (_, size, kind) in under.get(c, {}).items()
                        if kind == "f" and seen[(rel, size)] == 1 and size <= PROBE_MAX_BYTES)
        if not unique:
            continue
        size, rel = unique[0]
        body = up.read_metadata(f"{link}/{rel}")
        if body is not None and len(body) == size:
            logger.info("tree.link_resolved", link=link, target=c)
            return c
    raise EnumerationError(f"{link}: symlink target not identified among {cands}")


def _listing(up: Upstream, src: TreeSource, body: bytes, files: dict[str, RemoteFile]) -> Listing:
    listing = Listing(roots={src.filelist: sha256(body)})
    roots: list[RemoteFile] = []
    for path in [p for p in files if p.endswith("/repodata/repomd.xml") or p == "repodata/repomd.xml"]:
        repomd = up.read_metadata(path)
        if repomd is None:
            raise EnumerationError(f"{path}: in the file list but not found upstream; upstream may be mid-update")
        root = path[: -len("repodata/repomd.xml")]
        named, _ = repomd_files(root, path, repomd)
        for f in named:
            if f.path not in files:
                raise EnumerationError(
                    f"{path}: names {f.path}, which the file list lacks or the mirror's patterns exclude")
            files[f.path] = replace(f, mtime=files[f.path].mtime)
        files[path] = replace(files[path], size=len(repomd), sha256=sha256(repomd))
        # Small, and fixed-name: compared by checksum, so a signature never lags its repomd.xml.
        for extra in (f"{path}.asc", f"{path}.key"):
            if extra in files:
                data = up.read_metadata(extra)
                if data is None:
                    raise EnumerationError(f"{extra}: in the file list but not found upstream; upstream may be mid-update")
                files[extra] = replace(files[extra], size=len(data), sha256=sha256(data))
    for f in files.values():
        name = f.path.rpartition("/")[2]
        if not _is_metadata(f.path):
            listing.packages.append(f)
        elif name in _ROOTS:
            roots.append(replace(f, root=True))
        else:
            listing.metadata.append(f)
    roots.sort(key=lambda f: (f.path.rpartition("/")[0], _ROOTS[f.path.rpartition("/")[2]]))
    listing.metadata.extend(roots)
    return listing


def _is_metadata(path: str) -> bool:
    return path.startswith("repodata/") or "/repodata/" in path


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _natural(path: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path)]
