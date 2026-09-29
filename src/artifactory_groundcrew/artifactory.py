"""The Artifactory REST calls groundcrew makes, and nothing else.

Every call is idempotent. Transport errors, 429 and 5xx are retried; anything
else is returned or raised to the caller, which decides what it means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import log

if TYPE_CHECKING:
    from .config import Settings

logger = log.get("artifactory")

# Artifactory refuses AQL over 6000 characters.
AQL_MAX_CHARS = 5000


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


_RETRY = dict(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)


class FileTokenAuth(httpx.Auth):
    """Bearer token read from a file on every request, so rotation needs no restart."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def auth_flow(self, request: httpx.Request):
        token = self.path.read_text().strip()
        if not token:
            raise RuntimeError(f"token file {self.path} is empty")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


@dataclass(frozen=True)
class LocalFile:
    size: int
    sha256: str


class CopyError(RuntimeError):
    pass


def _q(path: str) -> str:
    return quote(path.lstrip("/"), safe="/")


class ArtifactoryClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: httpx.Auth | tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
        verify: str | bool = True,
        timeout: float = 120.0,
        max_connections: int = 16,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            auth=auth,
            headers=headers or {},
            verify=verify,
            timeout=httpx.Timeout(timeout, connect=15.0),
            # Large files are served as a 302 to object storage.
            follow_redirects=True,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ArtifactoryClient":
        auth: httpx.Auth | tuple[str, str] | None = None
        headers: dict[str, str] = {}
        if settings.artifactory_username and settings.artifactory_password:
            auth = (settings.artifactory_username, settings.artifactory_password)
        elif settings.artifactory_token_file:
            auth = FileTokenAuth(settings.artifactory_token_file)
        elif settings.artifactory_token:
            headers["Authorization"] = f"Bearer {settings.artifactory_token}"
        verify: str | bool = settings.artifactory_ca_cert or True
        return cls(
            settings.artifactory_url, auth=auth, headers=headers, verify=verify,
            max_connections=settings.workers + 4,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ArtifactoryClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- system -------------------------------------------------------

    @retry(**_RETRY)
    def ping(self) -> bool:
        r = self._http.get(f"{self.base_url}/api/system/ping")
        r.raise_for_status()
        return r.text.strip() == "OK"

    # --- repositories -------------------------------------------------

    @retry(**_RETRY)
    def get_repository(self, key: str) -> dict[str, Any] | None:
        r = self._http.get(f"{self.base_url}/api/repositories/{quote(key)}")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    @retry(**_RETRY)
    def create_repository(self, key: str, body: dict[str, Any]) -> None:
        r = self._http.put(f"{self.base_url}/api/repositories/{quote(key)}", json=body)
        r.raise_for_status()

    # --- content ------------------------------------------------------

    @retry(**_RETRY)
    def read(self, repo: str, path: str) -> bytes | None:
        """GET a file's bytes; None on 404. Through a remote, this caches it."""
        r = self._http.get(f"{self.base_url}/{quote(repo)}/{_q(path)}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content

    @retry(**_RETRY)
    def expire(self, remote: str, path: str) -> None:
        """Remove a file from a remote's cache so the next request fetches upstream.

        A Generic remote never re-fetches a cached file on its own.
        """
        r = self._http.delete(f"{self.base_url}/{quote(remote)}-cache/{_q(path)}")
        if r.status_code == 404:
            return
        r.raise_for_status()

    @retry(**_RETRY)
    def sync_download(self, remote: str, path: str) -> bool:
        """Have Artifactory fetch a file into the remote's cache. False on 404."""
        r = self._http.get(
            f"{self.base_url}/api/download/{quote(remote)}/{_q(path)}", params={"content": "none"}
        )
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    @retry(**_RETRY)
    def copy(self, src_repo: str, path: str, dst_repo: str) -> None:
        """Copy one file, overwriting the destination. Free: blobs are stored by checksum."""
        # The target is a query parameter, which httpx encodes itself. Quoting it
        # here as well stores the file under a literal "%3A" instead of ":".
        r = self._http.post(
            f"{self.base_url}/api/copy/{quote(src_repo)}/{_q(path)}",
            params={"to": f"/{dst_repo}/{path.lstrip('/')}"},
        )
        if r.status_code >= 500:
            r.raise_for_status()
        if r.status_code >= 400 or _copy_errors(r):
            raise CopyError(f"copy {src_repo}/{path} -> {dst_repo}: HTTP {r.status_code} {_copy_errors(r) or r.text[:300]}")

    @retry(**_RETRY)
    def delete(self, repo: str, path: str) -> int:
        """Delete a file. A 404 is the desired end state and is not an error."""
        r = self._http.delete(f"{self.base_url}/{quote(repo)}/{_q(path)}")
        if r.status_code == 404:
            return 404
        r.raise_for_status()
        return r.status_code

    # --- listing ------------------------------------------------------

    @retry(**_RETRY)
    def aql(self, query: str) -> list[dict[str, Any]]:
        if len(query) > AQL_MAX_CHARS:
            raise ValueError(f"AQL query is {len(query)} chars, over the {AQL_MAX_CHARS} budget")
        r = self._http.post(
            f"{self.base_url}/api/search/aql", content=query, headers={"Content-Type": "text/plain"}
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def list_files(self, repo: str) -> dict[str, LocalFile]:
        """Every file in a repository, keyed by path.

        The projection always carries repo, path and name: AQL collapses
        adjacent duplicate rows over whatever fields are projected, so a
        projection without the natural key silently returns short.
        """
        crit = {"$and": [{"repo": {"$eq": repo}}, {"type": {"$eq": "file"}}]}
        query = (
            f"items.find({json.dumps(crit)})"
            '.include("repo","path","name","size","sha256")'
        )
        out: dict[str, LocalFile] = {}
        for row in self.aql(query):
            folder = row.get("path") or "."
            name = row["name"]
            path = name if folder == "." else f"{folder}/{name}"
            out[path] = LocalFile(size=int(row.get("size") or 0), sha256=row.get("sha256") or "")
        return out


def _copy_errors(r: httpx.Response) -> str:
    try:
        msgs = r.json().get("messages", [])
    except ValueError:
        return ""
    return "; ".join(m.get("message", "") for m in msgs if m.get("level") == "ERROR")
