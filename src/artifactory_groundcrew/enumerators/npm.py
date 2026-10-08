"""A list of package specs, resolved with their dependencies by npm through the remote.

npm runs ``install --package-lock-only`` in an empty project, once per spec, and
its lockfile names every tarball in the closure. Resolution reads only package
metadata, so no tarball is downloaded until the sync fetches it. Scripts never run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote

from ..config import NpmSource
from ..model import EnumerationError, Listing, RemoteFile, Upstream

NPM_TIMEOUT_SECONDS = 1800

_SECRETS = re.compile(r"(://[^/@\s]*@|_auth(Token)?=\S+)")


def probe_npm(up: Upstream, src: NpmSource) -> dict[str, str]:
    # No root file says whether a new release changed a resolution.
    return {}


def enumerate_npm(up: Upstream, src: NpmSource) -> Listing:
    files, _ = resolve(up, {spec: "" for spec in src.packages}, src, source="config")
    return Listing(packages=files)


def resolve(
    up: Upstream, specs: dict[str, str], src: NpmSource, *, source: str, strict: bool = True,
) -> tuple[list[RemoteFile], list[str]]:
    """Every tarball npm resolves for each spec, filtered to the configured platforms.

    ``specs`` maps each spec to the user who asked for it, empty for the list.
    Strict, a failure is an EnumerationError; otherwise it is returned as a
    message and the other specs still resolve.
    """
    marker = f"/api/npm/{up.remote}/"
    files: list[RemoteFile] = []
    failures: list[str] = []
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="groundcrew-npm-") as tmp:
        npmrc = Path(tmp) / "npmrc"
        npmrc.write_text(up.client.npmrc(up.remote))
        npmrc.chmod(0o600)
        for n, (spec, user) in enumerate(specs.items()):
            try:
                lock = run_npm(spec, Path(tmp) / f"p{n}", npmrc, Path(tmp) / "cache")
            except EnumerationError as exc:
                if strict:
                    raise
                failures.append(str(exc))
                continue
            for key, entry in lock.get("packages", {}).items():
                url = entry.get("resolved", "")
                if not key or not url or not _wanted(entry, src):
                    continue
                _, found, path = url.partition(marker)
                if not found:
                    raise EnumerationError(f"{spec!r}: npm resolved {_SECRETS.sub('', url)!r}, not through {marker}")
                path = unquote(path)
                if path not in seen:
                    seen.add(path)
                    files.append(RemoteFile(path=path, source=source, requirement=spec, pulled_by=user))
    return files, failures


def _wanted(entry: dict, src: NpmSource) -> bool:
    """Whether a lockfile entry's ``os`` and ``cpu`` admit any configured platform."""
    return _admits(entry.get("os"), src.os) and _admits(entry.get("cpu"), src.cpu)


def _admits(declared: list[str] | None, configured: list[str]) -> bool:
    if not declared or not configured:
        return True
    allowed = [d for d in declared if not d.startswith("!")]
    denied = {d[1:] for d in declared if d.startswith("!")}
    return any((not allowed or c in allowed) and c not in denied for c in configured)


def spec_of(path: str) -> str | None:
    """``name@version`` from a tarball's path in a npm repository; None for anything else."""
    name, sep, filename = path.partition("/-/")
    base = name.rsplit("/", 1)[-1]
    if not sep or not filename.startswith(base + "-") or not filename.endswith(".tgz"):
        return None
    return f"{name}@{filename[len(base) + 1:-4]}"


def run_npm(spec: str, project: Path, npmrc: Path, cache: Path) -> dict:
    """Resolve one spec in an empty project and return the lockfile."""
    npm = shutil.which("npm")
    if npm is None:
        raise EnumerationError("npm is not installed")
    project.mkdir(parents=True)
    (project / "package.json").write_text("{}")
    env = {k: v for k, v in os.environ.items() if not k.lower().startswith("npm_config_")}
    args = [npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund",
            "--userconfig", str(npmrc), "--cache", str(cache), "--", spec]
    try:
        r = subprocess.run(args, cwd=project, env=env, capture_output=True, text=True, timeout=NPM_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise EnumerationError(f"npm took over {NPM_TIMEOUT_SECONDS}s resolving {spec!r}") from exc
    if r.returncode != 0:
        lines = [ln for ln in r.stderr.splitlines() if ln.startswith("npm error") and "A complete log" not in ln]
        detail = _SECRETS.sub("", " ".join(ln.removeprefix("npm error").strip() for ln in lines[:3]))[:500]
        raise EnumerationError(f"npm could not resolve {spec!r}: {detail}")
    return json.loads((project / "package-lock.json").read_text())
