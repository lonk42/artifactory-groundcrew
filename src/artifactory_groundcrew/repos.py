"""Optional repository creation, for the instance groundcrew points at.

Creation is idempotent and additive: a repository that exists is left exactly
as it is, and one that exists with the wrong class or package type is reported
rather than changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import log
from .artifactory import ArtifactoryClient
from .config import Mirror, Settings

logger = log.get("repos")

_DESCRIPTION = "Managed by artifactory-groundcrew"


@dataclass
class RepoAction:
    key: str
    rclass: str
    action: str  # "create", "exists", "conflict"
    detail: str = ""


def wanted(settings: Settings) -> list[tuple[str, dict[str, Any]]]:
    """(key, body) for every repository the configured mirrors need, in creation order."""
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def add(key: str, body: dict[str, Any]) -> None:
        if key and key not in seen:
            seen.add(key)
            out.append((key, body))

    for m in settings.mirrors:
        add(m.local, {"rclass": "local", "packageType": "generic", "description": _DESCRIPTION})
        add(m.remote, _remote_body(m))
        if m.virtual:
            add(m.virtual, {
                "rclass": "virtual", "packageType": "generic", "description": _DESCRIPTION,
                "repositories": [m.local, m.remote], "defaultDeploymentRepo": m.local,
            })
    return out


def _remote_body(m: Mirror) -> dict[str, Any]:
    return {
        "rclass": "remote",
        "packageType": "generic",
        "url": m.upstream_url,
        "description": _DESCRIPTION,
        "storeArtifactsLocally": True,
        # Never age cached files out. Groundcrew expires metadata itself.
        "unusedArtifactsCleanupPeriodHours": 0,
        # Artifactory refuses a metadata timeout larger than the cache period.
        "retrievalCachePeriodSecs": 7200,
        "metadataRetrievalTimeoutSecs": 60,
    }


def ensure(client: ArtifactoryClient, settings: Settings, *, dry_run: bool = False) -> list[RepoAction]:
    actions: list[RepoAction] = []
    for key, body in wanted(settings):
        if body["rclass"] == "remote" and not body["url"]:
            actions.append(RepoAction(key, "remote", "conflict", "no upstream_url configured for this mirror"))
            continue
        existing = client.get_repository(key)
        if existing is None:
            if not dry_run:
                client.create_repository(key, body)
                logger.info("repo.created", key=key, rclass=body["rclass"])
            actions.append(RepoAction(key, body["rclass"], "create"))
            continue
        got_class = (existing.get("rclass") or "").lower()
        got_type = (existing.get("packageType") or "").lower()
        if got_class != body["rclass"] or got_type != "generic":
            detail = f"exists as {got_class or '?'} {got_type or '?'}, wanted {body['rclass']} generic"
            logger.warning("repo.conflict", key=key, detail=detail)
            actions.append(RepoAction(key, body["rclass"], "conflict", detail))
        else:
            actions.append(RepoAction(key, body["rclass"], "exists"))
    return actions
