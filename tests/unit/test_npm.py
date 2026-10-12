from __future__ import annotations

import pytest

from artifactory_groundcrew import repos
from artifactory_groundcrew.artifactory import ArtifactoryClient
from artifactory_groundcrew.config import NpmSource, Settings
from artifactory_groundcrew.enumerators import npm
from artifactory_groundcrew.model import EnumerationError
from artifactory_groundcrew.state import State
from artifactory_groundcrew.sync import run_cycle

from .fakes import FakeArtifactory

ESBUILD = "esbuild/-/esbuild-0.24.2.tgz"
LINUX = "@esbuild/linux-x64/-/linux-x64-0.24.2.tgz"
DARWIN = "@esbuild/darwin-arm64/-/darwin-arm64-0.24.2.tgz"
MS = "ms/-/ms-2.1.3.tgz"
DEBUG = "debug/-/debug-4.4.0.tgz"
FILES = {ESBUILD: b"E", LINUX: b"L", DARWIN: b"D", MS: b"M", DEBUG: b"DBG", ".npm/debug/package.json": b"{}"}
BASE = "https://art.example/artifactory/api/npm/js-remote/"
# What npm's lockfile names for each spec: path -> (os, cpu).
LOCKS = {
    "esbuild@0.24.2": {ESBUILD: (None, None), LINUX: (["linux"], ["x64"]), DARWIN: (["darwin"], ["arm64"])},
    "debug@4.4.0": {DEBUG: (None, None), MS: (None, None)},
}


def mirror(packages=("esbuild@0.24.2",), **kw) -> dict:
    return {"name": "js", "type": "npm", "remote": "js-remote", "local": "js",
            "npm": {"packages": list(packages), **kw}}


@pytest.fixture
def art():
    a = FakeArtifactory()
    a.upstream["js-remote"] = dict(FILES)
    return a


@pytest.fixture
def runs(monkeypatch):
    seen: list[str] = []
    fail: set[str] = set()

    def run(spec, project, npmrc, cache):
        seen.append(spec)
        assert "SECRET" in npmrc.read_text() and oct(npmrc.stat().st_mode)[-3:] == "600"
        if spec in fail or spec not in LOCKS:
            raise EnumerationError(f"npm could not resolve {spec!r}")
        packages = {"": {"name": "root"}}
        for path, (os_, cpu) in LOCKS[spec].items():
            entry = {"resolved": BASE + path, "integrity": "sha512-x"}
            if os_:
                entry.update(os=os_, cpu=cpu, optional=True)
            packages[f"node_modules/{path.split('/-/')[0]}"] = entry
        packages["node_modules/bundled"] = {"inBundle": True}
        return {"packages": packages}

    monkeypatch.setattr(npm, "run_npm", run)
    seen_fail = seen, fail
    return seen_fail


def cycle(art, tmp_path, m, **kw):
    s = Settings(state_dir=tmp_path / "state", workers=4, mirrors=[m])
    return run_cycle(art, s, s.mirrors[0], State(s.state_dir), **kw)


def test_closure_for_every_platform_is_copied_and_tagged(art, runs, tmp_path):
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok" and r.fetched == 3
    assert set(art.repos["js"]) == {ESBUILD, LINUX, DARWIN}
    assert art.props[("js", LINUX)]["groundcrew.requirement"] == "esbuild@0.24.2"


def test_os_and_cpu_keep_only_matching_platform_packages(art, runs, tmp_path):
    cycle(art, tmp_path, mirror(os=["linux"], cpu=["x64", "arm64"]))
    assert set(art.repos["js"]) == {ESBUILD, LINUX}


def test_a_pulled_tarball_brings_its_dependencies(art, runs, tmp_path):
    seen, _ = runs
    cycle(art, tmp_path, mirror())
    art.user = "alice"
    art.read("js-remote", DEBUG)
    art.read("js-remote", ".npm/debug/package.json")
    art.user = "groundcrew"
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok" and r.plan["demand"] == 1
    assert {DEBUG, MS} <= set(art.repos["js"])
    assert seen[-1] == "debug@4.4.0"
    assert art.props[("js", MS)]["groundcrew.pulled_by"] == "alice"


def test_no_section_copies_only_what_clients_pull(art, runs, tmp_path):
    seen, _ = runs
    m = {k: v for k, v in mirror().items() if k != "npm"}
    assert cycle(art, tmp_path, m).status == "ok" and not art.repos.get("js")
    art.user = "alice"
    art.read("js-remote", DEBUG)
    art.user = "groundcrew"
    r = cycle(art, tmp_path, m)
    assert r.status == "ok" and {DEBUG, MS} <= set(art.repos["js"])
    assert seen == ["debug@4.4.0"]


def test_an_unresolvable_list_entry_withholds_the_cycle(art, runs, tmp_path):
    _, fail = runs
    fail.add("esbuild@0.24.2")
    assert cycle(art, tmp_path, mirror()).status == "enumeration-failed"
    assert "js" not in art.repos


def test_a_tarball_resolved_outside_the_remote_refuses_the_cycle(art, monkeypatch, tmp_path):
    monkeypatch.setattr(npm, "run_npm", lambda *a: {"packages": {
        "node_modules/x": {"resolved": "https://registry.npmjs.org/x/-/x-1.0.0.tgz"}}})
    assert cycle(art, tmp_path, mirror()).status == "enumeration-failed"


def test_spec_from_tarball_paths():
    assert npm.spec_of(LINUX) == "@esbuild/linux-x64@0.24.2"
    assert npm.spec_of("ms/-/ms-2.1.3.tgz") == "ms@2.1.3"
    assert npm.spec_of("pkg/-/pkg-1.0.0-beta.1.tgz") == "pkg@1.0.0-beta.1"
    assert npm.spec_of(".npm/ms/package.json") is None


def test_platform_filter_follows_npm_negation():
    src = NpmSource(packages=["x"], os=["linux"])
    assert npm._wanted({"os": ["!win32"]}, src)
    assert not npm._wanted({"os": ["!linux"]}, src)
    assert npm._wanted({"os": ["darwin"]}, NpmSource(packages=["x"]))


def test_repos_are_typed_npm():
    art = FakeArtifactory()
    m = {**mirror(), "upstream_url": "https://registry.npmjs.org", "virtual": "js-virtual"}
    repos.ensure(art, Settings(mirrors=[m]))
    assert {k: b["packageType"] for k, b in art.created.items()} == {"js": "npm", "js-remote": "npm", "js-virtual": "npm"}


def test_npmrc_uses_a_bearer_token_or_basic_auth(tmp_path):
    s = Settings(artifactory_url="https://art.example/artifactory", artifactory_token="tok")
    with ArtifactoryClient.from_settings(s) as c:
        assert c.npmrc("js-remote").splitlines() == [
            "registry=https://art.example/artifactory/api/npm/js-remote/",
            "//art.example/artifactory/api/npm/js-remote/:_authToken=tok"]
    s = Settings(artifactory_url="https://art.example/artifactory", artifactory_username="u", artifactory_password="p")
    with ArtifactoryClient.from_settings(s) as c:
        assert c.npmrc("js-remote").splitlines()[1] == "//art.example/artifactory/api/npm/js-remote/:_auth=dTpw"
