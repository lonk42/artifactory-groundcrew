"""apt archives: InRelease/Release per suite, then Packages for the package list."""

from __future__ import annotations

from ..config import AptSource
from ..model import EnumerationError, Listing, RemoteFile, Upstream, decompress, sha256, verify

# Preference when a Packages index is published in several compressions.
_PACKAGES_PREFERENCE = ("Packages.xz", "Packages.gz", "Packages", "Packages.bz2")


def _release_dirs(src: AptSource) -> list[str]:
    return [src.base] if src.flat else [f"{src.base}dists/{suite}/" for suite in src.suites]


def probe_apt(up: Upstream, src: AptSource) -> dict[str, str]:
    """Read only each suite's InRelease (or Release when there is none)."""
    roots = {}
    for d in _release_dirs(src):
        for name in ("InRelease", "Release"):
            body = up.read_metadata(d + name)
            if body is not None:
                roots[d + name] = sha256(body)
                break
        else:
            raise EnumerationError(f"{d}: neither InRelease nor Release found upstream")
    return roots


def enumerate_apt(up: Upstream, src: AptSource) -> Listing:
    listing = Listing()
    seen: set[str] = set()
    for d in _release_dirs(src):
        _suite(up, src, d, listing, seen)
    if src.flat:
        # Published beside a flat repository for clients to fetch the key from.
        # Not named by Release, so optional and outside the index ordering.
        key = up.read_metadata(src.base + "Release.key")
        if key is not None:
            listing.metadata.insert(0, RemoteFile(path=src.base + "Release.key", size=len(key), sha256=sha256(key)))
    return listing


def _suite(up: Upstream, src: AptSource, d: str, listing: Listing, seen: set[str]) -> None:
    bodies = {name: up.read_metadata(d + name) for name in ("InRelease", "Release", "Release.gpg")}
    text = bodies["Release"] or _strip_clearsign(bodies["InRelease"])
    if text is None:
        raise EnumerationError(f"{d}: neither InRelease nor Release found upstream")
    release = parse_release(text.decode("utf-8", "replace"))
    by_hash = release.get("acquire-by-hash", "").lower() == "yes"

    if src.flat:
        wanted_dirs = {""}
    else:
        wanted_dirs = {f"{c}/binary-{a}" for c in src.components for a in [*src.architectures, "all"]}
        wanted_dirs |= {f"{c}/{x}" for c in src.components for x in src.extra_indexes}
    # Architectures the Release covers that the mirror does not want, so the
    # per-architecture files in extra index directories can be left out.
    unwanted_arches = set(release.get("architectures", "").split()) - {*src.architectures, "all"}
    indexes: list[RemoteFile] = []
    by_dir: dict[str, dict[str, RemoteFile]] = {}
    for name, (sha, size) in release["sha256"].items():
        folder = name.rsplit("/", 1)[0] if "/" in name else ""
        if folder not in wanted_dirs:
            continue
        if "/binary-" not in folder and _arch_of(name) in unwanted_arches:
            continue
        if src.flat and not name.startswith("Packages"):
            continue
        rf = RemoteFile(path=d + name, size=size, sha256=sha, optional=True)
        indexes.append(rf)
        by_dir.setdefault(folder, {})[name.rsplit("/", 1)[-1]] = rf
        if by_hash:
            prefix = f"{folder}/" if folder else ""
            indexes.append(RemoteFile(path=f"{d}{prefix}by-hash/SHA256/{sha}", size=size, sha256=sha, optional=True))

    for folder, files in sorted(by_dir.items()):
        if "/binary-" not in folder and not src.flat:
            continue
        pick = next((files[n] for n in _PACKAGES_PREFERENCE if n in files), None)
        if pick is None:
            continue
        raw = up.read_metadata(pick.path)
        if raw is None and by_hash:
            raw = up.read_metadata(f"{d}{folder + '/' if folder else ''}by-hash/SHA256/{pick.sha256}")
        if raw is None:
            # Listed but absent; try the next variant before giving up.
            for n in _PACKAGES_PREFERENCE:
                if n in files and files[n] is not pick:
                    raw = up.read_metadata(files[n].path)
                    if raw is not None:
                        pick = files[n]
                        break
        if raw is None:
            raise EnumerationError(f"{d}{folder}: no Packages index could be read")
        verify(pick.path, raw, pick.sha256)
        arches = {*src.architectures, "all"} if src.flat and src.architectures else None
        for pkg, arch in parse_packages(decompress(pick.path, raw).decode("utf-8", "replace")):
            if arches is not None and arch not in arches:
                continue
            path = src.base + pkg.path
            if path not in seen:
                seen.add(path)
                listing.packages.append(RemoteFile(path=path, size=pkg.size, sha256=pkg.sha256))

    listing.metadata.extend(indexes)
    # Release and its detached signature, then InRelease last: apt tries
    # InRelease first, so it must not name indexes that are not in place yet.
    for name in ("Release", "Release.gpg", "InRelease"):
        body = bodies[name]
        if body is not None:
            listing.metadata.append(RemoteFile(path=d + name, size=len(body), sha256=sha256(body), root=True))
    root_name = "InRelease" if bodies["InRelease"] is not None else "Release"
    listing.roots[d + root_name] = sha256(bodies[root_name])


def _arch_of(name: str) -> str:
    """The architecture a per-architecture index names: Commands-arm64.xz gives arm64."""
    stem = name.rsplit("/", 1)[-1].split(".", 1)[0]
    return stem.rsplit("-", 1)[-1] if "-" in stem else ""


def _strip_clearsign(body: bytes | None) -> bytes | None:
    if body is None:
        return None
    text = body.decode("utf-8", "replace")
    if "-----BEGIN PGP SIGNED MESSAGE-----" not in text:
        return body
    head, _, rest = text.partition("\n\n")
    signed, _, _ = rest.partition("-----BEGIN PGP SIGNATURE-----")
    # Dash-escaped lines ("- -----") are unescaped per RFC 4880.
    lines = [ln[2:] if ln.startswith("- ") else ln for ln in signed.splitlines()]
    return "\n".join(lines).encode()


def parse_release(text: str) -> dict:
    """Fields of a Release file, lower-cased, with the SHA256 table parsed.

    ``sha256`` maps a path relative to the suite directory to (sha256, size).
    """
    fields: dict = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and current:
            fields[current].append(line.strip())
            continue
        key, _, value = line.partition(":")
        current = key.strip().lower()
        fields[current] = [value.strip()] if value.strip() else []
    table: dict[str, tuple[str, int]] = {}
    for row in fields.get("sha256", []):
        parts = row.split()
        if len(parts) == 3:
            table[parts[2]] = (parts[0], int(parts[1]))
    out = {k: " ".join(v) for k, v in fields.items() if k != "sha256"}
    out["sha256"] = table
    return out


def parse_packages(text: str):
    """(RemoteFile, Architecture) per stanza of a Packages index."""
    for stanza in text.split("\n\n"):
        f: dict[str, str] = {}
        for line in stanza.splitlines():
            if line and line[0] not in " \t":
                k, _, v = line.partition(":")
                f[k.strip().lower()] = v.strip()
        if "filename" in f:
            yield RemoteFile(
                path=f["filename"],
                size=int(f["size"]) if f.get("size", "").isdigit() else None,
                sha256=f.get("sha256") or None,
            ), f.get("architecture", "")
