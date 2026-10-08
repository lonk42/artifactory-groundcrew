"""A list of image tags, walked through the remote's registry API.

Each tag's manifest is read through the remote, which caches it. A
multi-platform index names one manifest per platform, kept by the configured
platforms along with the attestation manifests that refer to them. Every file
is listed at the path the remote caches it under, with its digest as checksum:

- ``<image>/<tag>/list.manifest.json`` for an index,
- ``<image>/sha256__<digest>/manifest.json`` and its blobs for each platform,
- ``<image>/<tag>/manifest.json`` and its blobs for a single-platform tag.

Blobs are not read here; the sync has the remote fetch the ones it lacks.
"""

from __future__ import annotations

import json

from ..config import DockerSource
from ..model import EnumerationError, Listing, RemoteFile, Upstream, sha256

INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}


def probe_docker(up: Upstream, src: DockerSource) -> dict[str, str]:
    # Tags move, but reading one is the listing itself.
    return {}


def enumerate_docker(up: Upstream, src: DockerSource) -> Listing:
    files, _ = resolve(up, {ref: "" for ref in src.images}, src.platforms, source="config")
    return Listing(packages=files)


def resolve(
    up: Upstream, refs: dict[str, str], platforms: list[str], *, source: str, strict: bool = True,
) -> tuple[list[RemoteFile], list[str]]:
    """Every file each ``image:tag`` names for the platforms, tagged with its provenance.

    ``refs`` maps each reference to the user who asked for it, empty for the
    list. Strict, a failure is an EnumerationError; otherwise it is returned as
    a message and the other references still resolve.
    """
    files: list[RemoteFile] = []
    failures: list[str] = []
    for ref, user in refs.items():
        image, _, tag = ref.rpartition(":")
        try:
            found = _tag(up, image, tag, platforms)
        except EnumerationError as exc:
            if strict:
                raise
            failures.append(str(exc))
            continue
        files += [RemoteFile(path=p, size=size, sha256=digest, source=source, requirement=ref, pulled_by=user)
                  for p, digest, size in found]
    return files, failures


def ref_of(path: str) -> str | None:
    """``image:tag`` from a tag's manifest path in a Docker repository; None for anything else."""
    parts = path.split("/")
    if len(parts) < 3 or parts[-1] not in ("manifest.json", "list.manifest.json") or parts[-2].startswith("sha256__"):
        return None
    return f"{'/'.join(parts[:-2])}:{parts[-2]}"


def ref_of_requirement(requirement: str) -> bool:
    """Whether a provenance requirement is an ``image:tag``, not a file path a demand copied as-is."""
    name, _, tag = requirement.rpartition(":")
    return bool(name) and bool(tag) and "/" not in tag


def _tag(up: Upstream, image: str, tag: str, platforms: list[str]) -> list[tuple[str, str, int | None]]:
    got = up.client.docker_manifest(up.remote, image, tag)
    if got is None:
        raise EnumerationError(f"{image}:{tag}: not found through the remote")
    body, media = got
    if media in INDEX_TYPES:
        index = json.loads(body)
        entries = index.get("manifests", [])
        kept = [m for m in entries if _platform_ok(m.get("platform") or {}, platforms)]
        digests = {m["digest"] for m in kept}
        kept += [m for m in entries if m not in kept
                 and m.get("annotations", {}).get("vnd.docker.reference.digest") in digests]
        out = [(f"{image}/{tag}/list.manifest.json", sha256(body), len(body))]
        for m in kept:
            out += _platform(up, image, m["digest"])
        return out
    if media in MANIFEST_TYPES:
        return _files(image, tag, body)
    raise EnumerationError(f"{image}:{tag}: unsupported manifest type {media!r}")


def _platform(up: Upstream, image: str, digest: str) -> list[tuple[str, str, int | None]]:
    got = up.client.docker_manifest(up.remote, image, digest)
    if got is None:
        raise EnumerationError(f"{image}@{digest}: listed by its index but not found through the remote")
    return _files(image, digest.replace(":", "__", 1), got[0])


def _files(image: str, folder: str, body: bytes) -> list[tuple[str, str, int | None]]:
    manifest = json.loads(body)
    out = [(f"{image}/{folder}/manifest.json", sha256(body), len(body))]
    for blob in [manifest.get("config", {}), *manifest.get("layers", [])]:
        algo, _, hexdigest = blob.get("digest", "").partition(":")
        if algo != "sha256":
            raise EnumerationError(f"{image}: blob digest {blob.get('digest')!r} is not sha256")
        out.append((f"{image}/{folder}/sha256__{hexdigest}", hexdigest, blob.get("size")))
    return out


def _platform_ok(platform: dict, wanted: list[str]) -> bool:
    """Whether an index entry's platform is one of ``os/arch[/variant]``; any when none are configured."""
    if not wanted:
        return True
    have = [platform.get("os", ""), platform.get("architecture", ""), platform.get("variant", "")]
    for w in wanted:
        parts = w.split("/")
        if parts[:2] == have[:2] and (len(parts) < 3 or parts[2] == have[2]):
            return True
    return False
