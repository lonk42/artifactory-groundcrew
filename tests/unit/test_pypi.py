from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from artifactory_groundcrew import repos
from artifactory_groundcrew.artifactory import ArtifactoryClient
from artifactory_groundcrew.config import PypiTarget, Settings
from artifactory_groundcrew.enumerators import pypi
from artifactory_groundcrew.model import EnumerationError
from artifactory_groundcrew.state import State
from artifactory_groundcrew.sync import run_cycle

from .fakes import FakeArtifactory, sha

REQUESTS = "f9/9b/aa/requests-2.32.3-py3-none-any.whl"
IDNA = "58/a2/bb/idna-3.20-py3-none-any.whl"
CORE = "a8/7c/cc/pydantic_core-2.27.2-cp311-cp311-manylinux_2_17_x86_64.whl"
# A pull on a Mac: its own wheel, and the Linux one pip resolves for the target.
MAC = "11/aa/dd/Flask_Extra-1.0-cp313-cp313-macosx_11_0_arm64.whl"
LINUX = "22/bb/ee/flask_extra-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"
REJECTED = "33/cc/ff/idna-3.19-py3-none-any.whl"
FILES = {REQUESTS: b"REQ", IDNA: b"IDNA", CORE: b"CORE", MAC: b"MAC", LINUX: b"LINUX", REJECTED: b"OLD",
         ".pypi/flask-extra.html": b"INDEX"}
# What pip resolves for each requirement.
CLOSURES = {"requests==2.32.3": [REQUESTS, IDNA], "pydantic-core==2.27.2": [CORE], "flask-extra==1.0": [LINUX, IDNA]}


def mirror(requirements=("requests==2.32.3",), **kw) -> dict:
    return {"name": "py", "type": "pypi", "remote": "py-remote", "local": "py",
            "pypi": {"requirements": list(requirements),
                     "targets": [{"python": "3.11", "platforms": ["manylinux_2_28_x86_64"]}]}, **kw}


@pytest.fixture
def art():
    a = FakeArtifactory()
    a.upstream["py-remote"] = dict(FILES)
    a.pypi_remotes.add("py-remote")
    return a


@pytest.fixture
def pip(art, monkeypatch):
    """Stands in for pip: reports each closure, and downloads it through the remote as pip does."""
    runs: list[tuple[list[str], dict]] = []
    state = {"downloads": True, "fail": set()}

    def run(args, env):
        runs.append((args, env))
        req = args[-1]
        if req in state["fail"] or req not in CLOSURES:
            raise EnumerationError(f"pip could not resolve {req!r}")
        base = "https://art.example/artifactory/api/pypi/py-remote/packages/packages/"
        for path in CLOSURES[req]:
            if state["downloads"]:
                art.read("py-remote", path)
        return {"install": [{"download_info": {"url": base + p, "archive_info": {"hashes": {"sha256": sha(FILES[p])}}}}
                            for p in CLOSURES[req]]}

    monkeypatch.setattr(pypi, "run_pip", run)
    run.runs = runs
    run.state = state
    return run


def cycle(art, tmp_path, m, **kw):
    s = Settings(state_dir=tmp_path / "state", workers=4, mirrors=[m])
    return run_cycle(art, s, s.mirrors[0], State(s.state_dir), **kw)


def pull(art, path, user="alice"):
    """A client fetching through the remote, as the virtual repository does for what the local lacks."""
    art.user = user
    art.read("py-remote", path)
    art.user = "groundcrew"


def test_closure_is_copied_into_the_local_repository(art, pip, tmp_path):
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok" and r.fetched == 2
    assert art.repos["py"] == {REQUESTS: b"REQ", IDNA: b"IDNA"}
    # Already cached by pip, so nothing is fetched a second time.
    assert not [c for c in art.calls if c[0] == "pypi_fetch"]
    props = art.props[("py", IDNA)]
    assert props["groundcrew.source"] == "config" and props["groundcrew.requirement"] == "requests==2.32.3"
    assert "groundcrew.pulled_by" not in props


def test_a_package_pip_did_not_cache_is_fetched_through_the_pypi_route(art, pip, tmp_path):
    pip.state["downloads"] = False
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok"
    assert {c[2] for c in art.calls if c[0] == "pypi_fetch"} == {REQUESTS, IDNA}
    assert set(art.repos["py"]) == {REQUESTS, IDNA}


def test_removing_a_requirement_keeps_its_closure(art, pip, tmp_path):
    cycle(art, tmp_path, mirror(["requests==2.32.3", "pydantic-core==2.27.2"]))
    r = cycle(art, tmp_path, mirror(["pydantic-core==2.27.2"]))
    assert r.status == "ok" and r.deleted == 0
    assert set(art.repos["py"]) == {REQUESTS, IDNA, CORE}


def test_one_unresolvable_requirement_withholds_the_whole_cycle(art, pip, tmp_path):
    cycle(art, tmp_path, mirror(["requests==2.32.3", "pydantic-core==2.27.2"]))
    pip.state["fail"].add("pydantic-core==2.27.2")
    r = cycle(art, tmp_path, mirror(["requests==2.32.3", "pydantic-core==2.27.2"]), full=True)
    assert r.status == "enumeration-failed"
    assert CORE in art.repos["py"]


def test_each_requirement_is_resolved_on_its_own_for_each_target(art, pip, tmp_path):
    m = mirror(["requests==2.32.3", "pydantic-core==2.27.2"])
    m["pypi"]["targets"].append({"python": "3.12", "platforms": ["musllinux_1_2_aarch64"]})
    cycle(art, tmp_path, m)
    assert len(pip.runs) == 4
    args, env = pip.runs[0]
    assert "SECRET" not in " ".join(args) and "SECRET" in env["PIP_INDEX_URL"]
    assert args[-2:] == ["--", "requests==2.32.3"]
    assert args[args.index("--abi") + 1] == "cp311"


def test_a_file_resolved_outside_the_remote_refuses_the_cycle(art, monkeypatch, tmp_path):
    monkeypatch.setattr(pypi, "run_pip", lambda args, env: {
        "install": [{"download_info": {"url": "https://user:pw@files.example/x-1.0-py3-none-any.whl"}}]})
    r = cycle(art, tmp_path, mirror())
    assert r.status == "enumeration-failed"
    assert "pw" not in r.error


def test_manylinux_targets_admit_every_older_glibc():
    tags = pypi.platform_tags("manylinux_2_28_x86_64")
    assert tags[0] == "manylinux_2_28_x86_64" and "manylinux_2_17_x86_64" in tags and "manylinux_2_5_x86_64" in tags
    assert {"manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"} <= set(tags)
    assert "manylinux1_aarch64" not in pypi.platform_tags("manylinux_2_28_aarch64")
    assert pypi.platform_tags("manylinux2014_aarch64")[0] == "manylinux_2_17_aarch64"
    assert pypi.platform_tags("musllinux_1_2_x86_64") == ["musllinux_1_2_x86_64", "musllinux_1_1_x86_64", "musllinux_1_0_x86_64"]
    assert pypi.platform_tags("macosx_11_0_arm64") == ["macosx_11_0_arm64"]


def test_pip_args_never_carry_credentials(tmp_path):
    args = pypi.pip_args("requests", PypiTarget(python="3.12", platforms=["win_amd64"]), Path(tmp_path))
    assert "--only-binary=:all:" in args and args[args.index("--platform") + 1] == "win_amd64"
    assert not [a for a in args if "@" in a or "index" in a]


def test_tier_2_takes_no_scope_and_may_share_a_local_repository():
    with pytest.raises(ValueError, match="remove it"):
        Settings(mirrors=[mirror(scope="f9/")])
    Settings(mirrors=[mirror(), {**mirror(), "name": "other", "remote": "other-remote"}])


def test_a_pull_is_copied_and_its_version_resolved_for_every_target(art, pip, tmp_path):
    cycle(art, tmp_path, mirror())
    pull(art, MAC)
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok" and r.plan["demand"] == 1
    assert {MAC, LINUX} <= set(art.repos["py"])
    assert [a[-1] for a, _ in pip.runs[1:]] == ["flask-extra==1.0"]
    for path in (MAC, LINUX):
        props = art.props[("py", path)]
        assert (props["groundcrew.source"], props["groundcrew.requirement"], props["groundcrew.pulled_by"]) == (
            "demand", "flask-extra==1.0", "alice")
    # Already mirrored for the list, so not re-tagged.
    assert art.props[("py", IDNA)]["groundcrew.source"] == "config"


def test_the_list_is_resolved_daily_and_demand_every_cycle(art, pip, tmp_path):
    cycle(art, tmp_path, mirror())
    assert cycle(art, tmp_path, mirror()).status == "unchanged"
    assert len(pip.runs) == 1
    pull(art, MAC)
    cycle(art, tmp_path, mirror())
    assert [a[-1] for a, _ in pip.runs[1:]] == ["flask-extra==1.0"]
    cycle(art, tmp_path, mirror(), full=True)
    assert pip.runs[-1][0][-1] == "requests==2.32.3"


def test_groundcrews_own_downloads_and_artifactory_indexes_are_not_demand(art, pip, tmp_path):
    cycle(art, tmp_path, mirror())
    art.read("py-remote", REJECTED)
    pull(art, ".pypi/flask-extra.html")
    assert cycle(art, tmp_path, mirror()).status == "unchanged"
    assert REJECTED not in art.repos["py"]


def test_a_target_that_cannot_resolve_a_pull_still_copies_the_pulled_file(art, pip, tmp_path):
    cycle(art, tmp_path, mirror())
    pip.state["fail"].add("flask-extra==1.0")
    pull(art, MAC)
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok" and len(r.unresolved) == 1
    assert MAC in art.repos["py"] and LINUX not in art.repos["py"]


def test_requirement_from_file_names():
    assert pypi.requirement_of("Flask_Extra-1.0-cp313-cp313-macosx_11_0_arm64.whl") == "flask-extra==1.0"
    assert pypi.requirement_of("pkg-1.0-1-py3-none-any.whl") == "pkg==1.0"
    assert pypi.requirement_of("zope.interface-6.0.tar.gz") == "zope-interface==6.0"
    assert pypi.requirement_of("simple.html") is None


def test_repos_are_typed_pypi_with_the_registry_set():
    art = FakeArtifactory()
    repos.ensure(art, Settings(mirrors=[mirror(upstream_url="https://files.pythonhosted.org", virtual="py-virtual")]))
    assert {k: b["packageType"] for k, b in art.created.items()} == {"py": "pypi", "py-remote": "pypi", "py-virtual": "pypi"}
    assert art.created["py-remote"]["pyPIRegistryUrl"] == "https://pypi.org"


def test_pip_index_carries_a_token_as_the_password_of_an_empty_user(tmp_path):
    token = tmp_path / "token"
    token.write_text("tok/en+=\n")
    s = Settings(artifactory_url="https://art.example/artifactory", artifactory_token_file=str(token))
    with ArtifactoryClient.from_settings(s) as c:
        assert c.pip_index("py-remote") == "https://:tok%2Fen%2B%3D@art.example/artifactory/api/pypi/py-remote/simple"


def test_pypi_fetch_uses_the_pip_route(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(404 if "missing" in req.url.path else 200, content=b"wheel")

    c = ArtifactoryClient("https://art.example/artifactory", verify=False)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert c.pypi_fetch("py-remote", REQUESTS) is True
    assert c.pypi_fetch("py-remote", "missing.whl") is False
    assert seen[0] == f"/artifactory/api/pypi/py-remote/packages/packages/{REQUESTS}"
