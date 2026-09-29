"""An in-memory Artifactory with one upstream behind each remote.

It keeps the behaviour the design depends on: a remote caches what it fetches
and never re-fetches a cached file until that file is expired from the cache.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import threading

import zstandard
from artifactory_groundcrew.artifactory import CopyError, LocalFile


class FakeArtifactory:
    def __init__(self) -> None:
        self.upstream: dict[str, dict[str, bytes]] = {}  # remote -> path -> bytes
        self.repos: dict[str, dict[str, bytes]] = {}     # "<remote>-cache" and locals
        self.calls: list[tuple[str, str, str]] = []
        self.fail_download: set[str] = set()
        self.created: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _log(self, op: str, repo: str, path: str) -> None:
        with self._lock:
            self.calls.append((op, repo, path))

    def ping(self) -> bool:
        return True

    def get_repository(self, key):
        return self.created.get(key)

    def create_repository(self, key, body):
        self.created[key] = body

    def read(self, repo, path):
        self._log("read", repo, path)
        if repo in self.upstream:
            cache = self.repos.setdefault(f"{repo}-cache", {})
            if path not in cache:
                if path not in self.upstream[repo]:
                    return None
                cache[path] = self.upstream[repo][path]
            return cache[path]
        return self.repos.get(repo, {}).get(path)

    def expire(self, remote, path):
        self._log("expire", remote, path)
        self.repos.setdefault(f"{remote}-cache", {}).pop(path, None)

    def sync_download(self, remote, path):
        self._log("download", remote, path)
        if path in self.fail_download:
            raise RuntimeError(f"simulated failure for {path}")
        return self.read(remote, path) is not None

    def copy(self, src, path, dst):
        self._log("copy", dst, path)
        data = self.repos.get(src, {}).get(path)
        if data is None:
            raise CopyError(f"{src}/{path} does not exist")
        self.repos.setdefault(dst, {})[path] = data

    def delete(self, repo, path):
        self._log("delete", repo, path)
        return 204 if self.repos.get(repo, {}).pop(path, None) is not None else 404

    def list_files(self, repo):
        return {p: LocalFile(size=len(b), sha256=hashlib.sha256(b).hexdigest()) for p, b in self.repos.get(repo, {}).items()}

    def close(self):
        pass


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def yum_repo(root: str, packages: dict[str, bytes], signed: bool = True, compression: str = "gz") -> dict[str, bytes]:
    """A minimal but real-shaped yum repository: repomd.xml, primary.xml.<compression>, packages."""
    pkg_xml = "".join(
        f'<package type="rpm"><name>{name.split("/")[-1]}</name>'
        f'<checksum type="sha256" pkgid="YES">{sha(body)}</checksum>'
        f'<size package="{len(body)}" installed="1" archive="1"/>'
        f'<location href="{name}"/></package>'
        for name, body in packages.items()
    )
    compress = {"gz": gzip.compress, "zst": zstandard.ZstdCompressor().compress}[compression]
    primary = compress(
        (
            '<?xml version="1.0"?><metadata xmlns="http://linux.duke.edu/metadata/common" '
            f'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="{len(packages)}">{pkg_xml}</metadata>'
        ).encode()
    )
    other = bz2.compress(b"<otherdata/>")
    p_name = f"repodata/{sha(primary)}-primary.xml.{compression}"
    o_name = f"repodata/{sha(other)}-other.xml.bz2"
    repomd = (
        '<?xml version="1.0"?><repomd xmlns="http://linux.duke.edu/metadata/repo">'
        f'<data type="primary"><checksum type="sha256">{sha(primary)}</checksum>'
        f'<location href="{p_name}"/><size>{len(primary)}</size></data>'
        f'<data type="other"><checksum type="sha256">{sha(other)}</checksum>'
        f'<location href="{o_name}"/><size>{len(other)}</size></data></repomd>'
    ).encode()
    files = {f"{root}{p_name}": primary, f"{root}{o_name}": other, f"{root}repodata/repomd.xml": repomd}
    if signed:
        files[f"{root}repodata/repomd.xml.asc"] = b"SIG:" + sha(repomd).encode()
    files.update({root + name: body for name, body in packages.items()})
    return files


def apt_repo(base: str, suite: str, packages: dict[str, bytes], by_hash: bool = False,
             compression: str = "gz", extras: dict[str, bytes] | None = None,
             architectures: str = "amd64") -> dict[str, bytes]:
    """A minimal apt archive with one component, amd64 Packages, and InRelease.

    ``extras`` are further index files, relative to the suite directory, that
    Release lists and upstream publishes.
    """
    stanzas = "\n\n".join(
        f"Package: {name.split('/')[-1].split('_')[0]}\nFilename: {name}\nSize: {len(body)}\nSHA256: {sha(body)}"
        for name, body in packages.items()
    ) + "\n"
    plain = stanzas.encode()
    gz = {"gz": gzip.compress, "xz": lzma.compress}[compression](plain)
    extras = extras or {}
    lines = [
        f" {sha(gz)} {len(gz)} main/binary-amd64/Packages.{compression}",
        # Listed but never published, as Ubuntu does with uncompressed indexes.
        f" {sha(plain)} {len(plain)} main/binary-amd64/Packages",
        # A component the mirror does not ask for.
        f" {sha(b'x')} 1 contrib/binary-amd64/Packages.gz",
        *(f" {sha(body)} {len(body)} {name}" for name, body in extras.items()),
    ]
    release = (
        f"Suite: {suite}\nArchitectures: {architectures}\nComponents: main contrib\n"
        + ("Acquire-By-Hash: yes\n" if by_hash else "")
        + "SHA256:\n" + "\n".join(lines) + "\n"
    ).encode()
    inrelease = (
        b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n" + release
        + b"-----BEGIN PGP SIGNATURE-----\nSIG\n-----END PGP SIGNATURE-----\n"
    )
    d = f"{base}dists/{suite}/"
    files = {
        f"{d}Release": release,
        f"{d}Release.gpg": b"SIG",
        f"{d}InRelease": inrelease,
        f"{d}main/binary-amd64/Packages.{compression}": gz,
        **{d + name: body for name, body in extras.items()},
    }
    if by_hash:
        files[f"{d}main/binary-amd64/by-hash/SHA256/{sha(gz)}"] = gz
    files.update({base + name: body for name, body in packages.items()})
    return files


def flat_apt_repo(base: str, packages: dict[str, tuple[str, bytes]]) -> dict[str, bytes]:
    """A flat apt repository (``deb URL /``): Release and Packages in the base directory."""
    stanzas = "\n\n".join(
        f"Package: x\nArchitecture: {arch}\nFilename: {name}\nSize: {len(body)}\nSHA256: {sha(body)}"
        for name, (arch, body) in packages.items()
    ) + "\n"
    plain = stanzas.encode()
    gz = gzip.compress(plain)
    release = (
        "Archive: flat\nSHA256:\n"
        f" {sha(plain)} {len(plain)} Packages\n {sha(gz)} {len(gz)} Packages.gz\n"
        f" {sha(b'src')} 3 Sources\n"
    ).encode()
    files = {f"{base}Release": release, f"{base}Release.gpg": b"SIG", f"{base}Release.key": b"KEY",
             f"{base}Packages": plain, f"{base}Packages.gz": gz}
    files.update({base + name: body for name, (_, body) in packages.items()})
    return files
