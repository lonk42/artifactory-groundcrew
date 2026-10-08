"""A list of requirements, resolved with their dependencies by pip through the remote.

pip runs ``install --dry-run --report`` against the remote's simple index, once
per requirement and target, and its report names every file in the closure.
Wheels only: building an sdist runs its code, and would build for this pod's
platform rather than the target's.

The remote serves no PEP 658 metadata, so pip downloads each wheel to read its
dependencies, which also caches it in the remote. The downloads go to a
temporary directory, shared within one listing and removed after it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

from ..config import PypiSource, PypiTarget
from ..model import EnumerationError, Listing, RemoteFile, Upstream

PIP_TIMEOUT_SECONDS = 1800

_MANYLINUX = re.compile(r"^manylinux_2_(\d+)_(\w+)$")
_MUSLLINUX = re.compile(r"^musllinux_1_(\d+)_(\w+)$")
_LEGACY = {"manylinux2014": 17, "manylinux2010": 12, "manylinux1": 5}
_CREDENTIALS = re.compile(r"://[^/@\s]*@")
_SDIST = (".tar.gz", ".zip", ".tar.bz2")


def probe_pypi(up: Upstream, src: PypiSource) -> dict[str, str]:
    # No root file says whether a new release changed a resolution.
    return {}


def enumerate_pypi(up: Upstream, src: PypiSource) -> Listing:
    files, _ = resolve(up, {req: "" for req in src.requirements}, src.targets, source="config")
    return Listing(packages=files)


def resolve(
    up: Upstream, requirements: dict[str, str], targets: list[PypiTarget], *, source: str, strict: bool = True,
) -> tuple[list[RemoteFile], list[str]]:
    """Every file pip resolves for each requirement on each target, tagged with its provenance.

    ``requirements`` maps each requirement to the user who asked for it, empty
    for the list. Strict, a failure is an EnumerationError; otherwise it is
    returned as a message and the other requirements still resolve.
    """
    marker = f"/api/pypi/{up.remote}/packages/packages/"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env.update(
        PIP_INDEX_URL=up.client.pip_index(up.remote),
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_INPUT="1",
    )
    if up.client.ca_cert:
        env["PIP_CERT"] = up.client.ca_cert

    files: list[RemoteFile] = []
    failures: list[str] = []
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="groundcrew-pip-") as tmp:
        for target in targets:
            for req, user in requirements.items():
                try:
                    report = run_pip(pip_args(req, target, Path(tmp)), env)
                except EnumerationError as exc:
                    if strict:
                        raise
                    failures.append(str(exc))
                    continue
                for item in report.get("install", []):
                    info = item.get("download_info", {})
                    url = info.get("url", "")
                    _, found, path = url.partition(marker)
                    if not found:
                        raise EnumerationError(f"{req!r}: pip resolved {_CREDENTIALS.sub('://', url)!r}, not through {marker}")
                    path = unquote(path)
                    if path not in seen:
                        seen.add(path)
                        sha = info.get("archive_info", {}).get("hashes", {}).get("sha256")
                        files.append(RemoteFile(path=path, sha256=sha, source=source, requirement=req, pulled_by=user))
    return files, failures


def requirement_of(filename: str) -> str | None:
    """``name==version`` from a wheel or sdist file name; None for anything else."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        name, version = (parts[0], parts[1]) if len(parts) >= 5 else ("", "")
    else:
        stem = next((filename[: -len(ext)] for ext in _SDIST if filename.endswith(ext)), "")
        name, _, version = stem.rpartition("-")
    if not name or not version:
        return None
    return f"{re.sub(r'[-_.]+', '-', name).lower()}=={version}"


def pip_args(req: str, target: PypiTarget, tmp: Path) -> list[str]:
    report = tmp / "report.json"
    args = [
        sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
        "--report", str(report), "--only-binary=:all:",
        "--python-version", target.python, "--implementation", "cp", "--abi", "cp" + target.python.replace(".", ""),
        "--target", str(tmp / "target"), "--cache-dir", str(tmp / "cache"),
    ]
    for tag in target.platforms:
        for t in platform_tags(tag):
            args += ["--platform", t]
    return [*args, "--", req]


def platform_tags(tag: str) -> list[str]:
    """A platform tag and every older one an installer on that platform accepts.

    pip does not widen a ``--platform`` it is given, so a manylinux_2_28 target
    would otherwise miss every manylinux_2_17 wheel.
    """
    for legacy, minor in _LEGACY.items():
        if tag.startswith(legacy + "_"):
            tag = f"manylinux_2_{minor}_{tag[len(legacy) + 1:]}"
    if m := _MANYLINUX.match(tag):
        minor, arch = int(m[1]), m[2]
        tags = [f"manylinux_2_{n}_{arch}" for n in range(minor, 4, -1)]
        for legacy, n in _LEGACY.items():
            if n <= minor and (legacy == "manylinux2014" or arch in ("x86_64", "i686")):
                tags.append(f"{legacy}_{arch}")
        return tags
    if m := _MUSLLINUX.match(tag):
        return [f"musllinux_1_{n}_{m[2]}" for n in range(int(m[1]), -1, -1)]
    return [tag]


def run_pip(args: list[str], env: dict[str, str]) -> dict:
    """Run pip and return its report. A failure to resolve is an EnumerationError."""
    report = Path(args[args.index("--report") + 1])
    report.unlink(missing_ok=True)
    req = args[-1]
    try:
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=PIP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise EnumerationError(f"pip took over {PIP_TIMEOUT_SECONDS}s resolving {req!r}") from exc
    if r.returncode != 0:
        lines = [ln for ln in r.stderr.splitlines() if ln.strip() and not ln.startswith("[notice]")]
        detail = _CREDENTIALS.sub("://", " ".join(lines[-3:]))[:500]
        raise EnumerationError(f"pip could not resolve {req!r} for {_target(args)}: {detail}")
    return json.loads(report.read_text())


def _target(args: list[str]) -> str:
    return f"Python {args[args.index('--python-version') + 1]} {args[args.index('--platform') + 1]}"
