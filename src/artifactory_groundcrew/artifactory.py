"""The Artifactory REST calls groundcrew makes, and nothing else.

Every call is idempotent. Transport errors, 429 and 5xx are retried; anything
else is returned or raised to the caller, which decides what it means.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import log

if TYPE_CHECKING:
    from .config import Settings

logger = log.get("artifactory")

# Artifactory refuses AQL over 6000 characters.
AQL_MAX_CHARS = 5000

DOCKER_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


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
    # Who caused the file to be stored. In a remote's cache, the user whose
    # request fetched it.
    created_by: str = ""


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
        pip_credentials: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.ca_cert = verify if isinstance(verify, str) else ""
        self._pip_credentials = pip_credentials
        self._username = auth[0] if isinstance(auth, tuple) else None
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
        # pip only speaks basic auth. Artifactory takes an access token as the
        # password with an empty username. npm is given the same pair, and sends
        # a token as a bearer token.
        pip: Callable[[], tuple[str, str]] | None = None
        if settings.artifactory_username and settings.artifactory_password:
            auth = (settings.artifactory_username, settings.artifactory_password)
            pip = lambda: (settings.artifactory_username, settings.artifactory_password)  # noqa: E731
        elif settings.artifactory_token_file:
            auth = FileTokenAuth(settings.artifactory_token_file)
            pip = lambda: ("", Path(settings.artifactory_token_file).read_text().strip())  # noqa: E731
        elif settings.artifactory_token:
            headers["Authorization"] = f"Bearer {settings.artifactory_token}"
            pip = lambda: ("", settings.artifactory_token)  # noqa: E731
        verify: str | bool = settings.artifactory_ca_cert or True
        return cls(
            settings.artifactory_url, auth=auth, headers=headers, verify=verify,
            max_connections=settings.workers + 4, pip_credentials=pip,
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

    def whoami(self) -> str:
        """The user this client acts as, as Artifactory records it in ``created_by``. Empty if unknown."""
        if self._username is None:
            try:
                r = self._http.get(f"{self.base_url.removesuffix('/artifactory')}/access/api/v1/tokens/me")
                r.raise_for_status()
                self._username = r.json().get("subject", "").rpartition("/users/")[2]
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("whoami.failed", error=str(exc))
                self._username = ""
        return self._username

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
    def pypi_fetch(self, remote: str, path: str) -> bool:
        """Fetch a package through a PyPI remote's download route, caching it at ``path``. False on 404.

        The route pip uses, and the only one that caches at the path pip's
        report names: Sync Download on that path is a 404 until it is cached,
        and on ``packages/<path>`` caches a second copy under that prefix.
        """
        url = f"{self.base_url}/api/pypi/{quote(remote)}/packages/packages/{_q(path)}"
        with self._http.stream("GET", url) as r:
            if r.status_code == 404:
                return False
            r.raise_for_status()
            for _ in r.iter_bytes():
                pass
        return True

    # --- docker -------------------------------------------------------
    #
    # A Docker remote fills its cache only through the registry API. Sync
    # Download and plain GETs on its cache paths are 404s, and the remote then
    # remembers the miss for missedRetrievalCachePeriodSecs (30 minutes), so the
    # registry API answers 404 for that manifest too until it expires.

    @retry(**_RETRY)
    def docker_manifest(self, remote: str, image: str, ref: str) -> tuple[bytes, str] | None:
        """A manifest or index by tag or digest, and its media type. None on 404."""
        r = self._http.get(f"{self.base_url}/api/docker/{quote(remote)}/v2/{_q(image)}/manifests/{quote(ref)}",
                           headers={"Accept": DOCKER_ACCEPT})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "").split(";")[0].strip()

    @retry(**_RETRY)
    def docker_blob(self, remote: str, image: str, digest: str) -> bool:
        """Have a remote cache a blob. A HEAD fetches it upstream with no body sent here. False on 404."""
        r = self._http.head(f"{self.base_url}/api/docker/{quote(remote)}/v2/{_q(image)}/blobs/{quote(digest)}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    def docker_fetch(self, remote: str, path: str) -> bool:
        """Cache one file of a Docker remote's layout through the registry API. False on 404.

        ``<image>/<tag or sha256__digest>/[list.]manifest.json`` is a manifest,
        ``<image>/<folder>/sha256__<digest>`` a blob.
        """
        image, folder, name = path.rsplit("/", 2)
        if name in ("manifest.json", "list.manifest.json"):
            ref = folder.replace("__", ":", 1) if folder.startswith("sha256__") else folder
            return self.docker_manifest(remote, image, ref) is not None
        if name.startswith("sha256__"):
            return self.docker_blob(remote, image, name.replace("__", ":", 1))
        return False

    def pip_index(self, remote: str) -> str:
        """A PyPI remote's simple index URL, carrying credentials for pip."""
        url = f"{self.base_url}/api/pypi/{quote(remote)}/simple"
        if self._pip_credentials is None:
            return url
        user, secret = self._pip_credentials()
        scheme, rest = url.split("://", 1)
        return f"{scheme}://{quote(user, safe='')}:{quote(secret, safe='')}@{rest}"

    def npmrc(self, remote: str) -> str:
        """An npm user config pointing at a npm remote, carrying credentials."""
        registry = f"{self.base_url}/api/npm/{quote(remote)}/"
        lines = [f"registry={registry}"]
        if self._pip_credentials is not None:
            user, secret = self._pip_credentials()
            key = registry.split(":", 1)[1]
            if user:
                lines.append(f"{key}:_auth={base64.b64encode(f'{user}:{secret}'.encode()).decode()}")
            else:
                lines.append(f"{key}:_authToken={secret}")
        if self.ca_cert:
            lines.append(f"cafile={self.ca_cert}")
        return "\n".join(lines) + "\n"

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
    def set_properties(self, repo: str, path: str, props: dict[str, str]) -> None:
        """Set properties on one file, replacing any of the same keys."""
        value = ";".join(f"{k}={_escape(v)}" for k, v in props.items())
        r = self._http.put(f"{self.base_url}/api/storage/{quote(repo)}/{_q(path)}", params={"properties": value})
        r.raise_for_status()

    def list_properties(self, repo: str, prefix: str) -> dict[str, dict[str, str]]:
        """Files carrying a property under ``prefix``, keyed by path, with those properties."""
        crit = {"$and": [{"repo": {"$eq": repo}}, {"type": {"$eq": "file"}},
                         {"property.key": {"$match": f"{prefix}*"}}]}
        query = f'items.find({json.dumps(crit)}).include("repo","path","name","property.key","property.value")'
        out: dict[str, dict[str, str]] = {}
        for row in self.aql(query):
            folder = row.get("path") or "."
            path = row["name"] if folder == "." else f"{folder}/{row['name']}"
            props = out.setdefault(path, {})
            for p in row.get("properties", []):
                if p["key"].startswith(prefix):
                    props[p["key"]] = ",".join(filter(None, [props.get(p["key"]), p.get("value", "")]))
        return out

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
            '.include("repo","path","name","size","sha256","created_by")'
        )
        out: dict[str, LocalFile] = {}
        for row in self.aql(query):
            folder = row.get("path") or "."
            name = row["name"]
            path = name if folder == "." else f"{folder}/{name}"
            out[path] = LocalFile(size=int(row.get("size") or 0), sha256=row.get("sha256") or "",
                                  created_by=row.get("created_by") or "")
        return out


def _escape(value: str) -> str:
    # Property syntax separates with ; , | and =, escaped with a backslash.
    return re.sub(r"([\\,|=;])", r"\\\1", value)


def _copy_errors(r: httpx.Response) -> str:
    try:
        msgs = r.json().get("messages", [])
    except ValueError:
        return ""
    return "; ".join(m.get("message", "") for m in msgs if m.get("level") == "ERROR")
