"""Tier 2 demand: what clients pulled through the remote that the mirror lacks.

A client of the virtual repository is served from the local repository first,
so a package file in the remote's cache that is not in the local repository was
pulled by someone who needed it. The pulled file is copied as it is, and for
PyPI its ``name==version`` is also resolved for every configured target (npm:
``name@version``, docker: ``image:tag``, for the configured platforms), so a
client of the local repository alone gets a complete closure.

Docker tags move, so the tags demand brought in are resolved again whenever the
list is, found from the properties on the local repository's files.
"""

from __future__ import annotations

from .artifactory import ArtifactoryClient, LocalFile
from .config import Mirror
from .enumerators import docker, npm, pypi
from .model import RemoteFile, Upstream

# Indexes Artifactory builds and keeps in typed repositories, and package
# metadata npm reads while resolving. Never package files.
ARTIFACTORY_OWNED = (".pypi/", ".npm/", ".jfrog/")


def pulled(cache: dict[str, LocalFile], have: set[str], me: str) -> dict[str, LocalFile]:
    """Cached package files not in ``have``, excluding Groundcrew's own downloads.

    Groundcrew's pip runs download through the remote too, including versions
    pip rejects, and those are not anyone's demand.
    """
    return {
        path: f for path, f in cache.items()
        if path not in have and not path.startswith(ARTIFACTORY_OWNED) and not (me and f.created_by == me)
    }


def expand(up: Upstream, mirror: Mirror, files: dict[str, LocalFile]) -> tuple[list[RemoteFile], list[str]]:
    """The files to copy for this demand, and a message per target that could not be resolved."""
    out: list[RemoteFile] = []
    wanted: dict[str, str] = {}
    for path, f in files.items():
        req = None
        if mirror.type == "pypi":
            req = pypi.requirement_of(path.rsplit("/", 1)[-1])
        elif mirror.type == "npm":
            req = npm.spec_of(path)
        elif mirror.type == "docker":
            req = docker.ref_of(path)
        out.append(RemoteFile(path=path, sha256=f.sha256 or None, source="demand",
                              requirement=req or path, pulled_by=f.created_by))
        if req:
            wanted.setdefault(req, f.created_by)
    failures: list[str] = []
    if wanted and mirror.type == "pypi":
        closure, failures = pypi.resolve(up, wanted, mirror.pypi.targets, source="demand", strict=False)
        out.extend(closure)
    elif wanted and mirror.type == "npm":
        closure, failures = npm.resolve(up, wanted, mirror.npm, source="demand", strict=False)
        out.extend(closure)
    elif wanted and mirror.type == "docker":
        closure, failures = docker.resolve(up, wanted, mirror.docker.platforms, source="demand", strict=False)
        out.extend(closure)
    return out, failures


def refresh(client: ArtifactoryClient, up: Upstream, mirror: Mirror) -> tuple[list[RemoteFile], list[str]]:
    """The current files of every Docker tag demand brought in, so a moved tag follows upstream."""
    if mirror.type != "docker":
        return [], []
    refs: dict[str, str] = {}
    for props in client.list_properties(mirror.local, "groundcrew.").values():
        if props.get("groundcrew.source") == "demand" and docker.ref_of_requirement(props.get("groundcrew.requirement", "")):
            refs.setdefault(props["groundcrew.requirement"], props.get("groundcrew.pulled_by", ""))
    for ref in mirror.docker.images:
        refs.pop(ref, None)
    return docker.resolve(up, refs, mirror.docker.platforms, source="demand", strict=False)
