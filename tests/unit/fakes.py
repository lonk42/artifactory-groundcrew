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
        # PyPI remotes: Sync Download answers only from the cache, as on a live
        # instance, and pypi_fetch is the route that fetches upstream.
        self.pypi_remotes: set[str] = set()
        self.base_url = "https://art.example/artifactory"
        self.ca_cert = ""
        # The user requests act as, recorded as created_by on what they cache.
        self.user = "groundcrew"
        self.owner: dict[tuple[str, str], str] = {}
        self.props: dict[tuple[str, str], dict[str, str]] = {}
        # Docker remotes: (image, ref) -> (body, media type, cache path), and
        # every file's bytes by cache path.
        self.docker_refs: dict[str, dict[tuple[str, str], tuple[bytes, str, str]]] = {}
        self.docker_files: dict[str, dict[str, bytes]] = {}
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
                self.owner[(f"{repo}-cache", path)] = self.user
            return cache[path]
        return self.repos.get(repo, {}).get(path)

    def expire(self, remote, path):
        self._log("expire", remote, path)
        self.repos.setdefault(f"{remote}-cache", {}).pop(path, None)

    def sync_download(self, remote, path):
        self._log("download", remote, path)
        if path in self.fail_download:
            raise RuntimeError(f"simulated failure for {path}")
        if remote in self.pypi_remotes:
            return path in self.repos.get(f"{remote}-cache", {})
        return self.read(remote, path) is not None

    def pypi_fetch(self, remote, path):
        self._log("pypi_fetch", remote, path)
        return self.read(remote, path) is not None

    def whoami(self):
        return "groundcrew"

    def set_properties(self, repo, path, props):
        self._log("props", repo, path)
        self.props[(repo, path)] = dict(props)

    def _cache(self, remote, path, data):
        self.repos.setdefault(f"{remote}-cache", {})[path] = data
        self.owner[(f"{remote}-cache", path)] = self.user

    def docker_manifest(self, remote, image, ref):
        self._log("manifest", remote, f"{image}:{ref}")
        got = self.docker_refs.get(remote, {}).get((image, ref))
        if got is None:
            return None
        body, media, path = got
        self._cache(remote, path, body)
        return body, media

    def docker_tags(self, remote, image):
        """Tags in reverse publishing order, since registries promise no order."""
        self._log("tags", remote, image)
        tags = [ref for img, ref in self.docker_refs.get(remote, {}) if img == image and not ref.startswith("sha256:")]
        return tags[::-1] or None

    def docker_fetch(self, remote, path):
        self._log("docker_fetch", remote, path)
        data = self.docker_files.get(remote, {}).get(path)
        if data is None:
            return False
        self._cache(remote, path, data)
        return True

    def list_properties(self, repo, prefix):
        return {path: {k: v for k, v in props.items() if k.startswith(prefix)}
                for (r, path), props in self.props.items() if r == repo}

    def npmrc(self, remote):
        return f"registry={self.base_url}/api/npm/{remote}/\n//art.example/artifactory/api/npm/{remote}/:_authToken=SECRET\n"

    def pip_index(self, remote):
        return f"https::SECRET@art.example/artifactory/api/pypi/{remote}/simple"

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
        return {p: LocalFile(size=len(b), sha256=hashlib.sha256(b).hexdigest(), created_by=self.owner.get((repo, p), ""))
                for p, b in self.repos.get(repo, {}).items()}

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


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def docker_image(art: FakeArtifactory, remote: str, image: str, tag: str,
                 platforms=("linux/amd64", "linux/arm64"), seed: str = "") -> dict[str, str]:
    """Publish a tag behind a fake Docker remote, laid out as Artifactory caches it.

    One platform is a single-platform tag. Several make an index with one
    attestation manifest per platform. Returns each platform's manifest folder.
    """
    import json

    refs = art.docker_refs.setdefault(remote, {})
    files = art.docker_files.setdefault(remote, {})

    def manifest(folder, label):
        blobs = {f"{label}-config{seed}".encode(), f"{label}-layer{seed}".encode()}
        config, layer = sorted(blobs)
        body = json.dumps({"mediaType": OCI_MANIFEST,
                           "config": {"digest": f"sha256:{sha(config)}", "size": len(config)},
                           "layers": [{"digest": f"sha256:{sha(layer)}", "size": len(layer)}]}).encode()
        for b in (config, layer):
            files[f"{image}/{folder}/sha256__{sha(b)}"] = b
        files[f"{image}/{folder}/manifest.json"] = body
        return body

    if len(platforms) == 1:
        body = manifest(tag, platforms[0])
        refs[(image, tag)] = (body, OCI_MANIFEST, f"{image}/{tag}/manifest.json")
        return {platforms[0]: tag}
    entries, folders = [], {}
    for plat in platforms:
        for kind in ("image", "attestation"):
            probe = manifest("tmp", f"{plat}-{kind}")
            digest = sha(probe)
            folder = f"sha256__{digest}"
            for k in [k for k in files if k.startswith(f"{image}/tmp/")]:
                files[f"{image}/{folder}/{k.rsplit('/', 1)[1]}"] = files.pop(k)
            refs[(image, f"sha256:{digest}")] = (probe, OCI_MANIFEST, f"{image}/{folder}/manifest.json")
            os_, arch = plat.split("/")[:2]
            if kind == "image":
                folders[plat] = folder
                entries.append({"digest": f"sha256:{digest}", "platform": {"os": os_, "architecture": arch}})
            else:
                image_digest = entries[-1]["digest"]
                entries.append({"digest": f"sha256:{digest}", "platform": {"os": "unknown", "architecture": "unknown"},
                                "annotations": {"vnd.docker.reference.type": "attestation-manifest",
                                                "vnd.docker.reference.digest": image_digest}})
    index = json.dumps({"mediaType": OCI_INDEX, "manifests": entries}).encode()
    refs[(image, tag)] = (index, OCI_INDEX, f"{image}/{tag}/list.manifest.json")
    files[f"{image}/{tag}/list.manifest.json"] = index
    return folders
