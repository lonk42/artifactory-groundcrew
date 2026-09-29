from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from artifactory_groundcrew import repos
from artifactory_groundcrew.config import Settings, load
from artifactory_groundcrew.enumerators.apt import _strip_clearsign, parse_release

from .fakes import FakeArtifactory

YUM = {"name": "rocky", "type": "yum", "remote": "rocky-remote", "local": "rocky",
       "upstream_url": "https://dl.rockylinux.org/pub/rocky", "virtual": "rocky-virtual",
       "yum": {"paths": ["9/BaseOS/x86_64/os"]}}


def test_env_overrides_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"workers": 3, "artifactory_url": "http://file/artifactory", "mirrors": [YUM]}))
    monkeypatch.setenv("GROUNDCREW_WORKERS", "12")
    s = load(path)
    assert s.workers == 12
    assert s.artifactory_url == "http://file/artifactory"
    assert s.mirrors[0].yum.paths == ["9/BaseOS/x86_64/os/"]
    assert s.mirrors[0].upstream_url.endswith("/")


def test_section_must_match_type():
    with pytest.raises(ValidationError, match="no apt: section"):
        Settings(mirrors=[{**YUM, "type": "apt"}])


def test_overlapping_scopes_in_one_local_repo_are_refused():
    a = {**YUM, "name": "a", "scope": "9/"}
    b = {**YUM, "name": "b", "scope": "9/BaseOS/"}
    with pytest.raises(ValidationError, match="overlapping scopes"):
        Settings(mirrors=[a, b])
    Settings(mirrors=[{**a, "scope": "9/BaseOS/"}, {**b, "scope": "9/AppStream/"}])


def test_release_parsing_and_clearsign():
    signed = (
        b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        b"Suite: stable\nAcquire-By-Hash: yes\nSHA256:\n abc 12 main/binary-amd64/Packages.gz\n"
        b"- -----not a signature\n"
        b"-----BEGIN PGP SIGNATURE-----\nX\n-----END PGP SIGNATURE-----\n"
    )
    text = _strip_clearsign(signed).decode()
    assert "-----not a signature" in text and "Hash: SHA512" not in text
    rel = parse_release(text)
    assert rel["suite"] == "stable"
    assert rel["acquire-by-hash"] == "yes"
    assert rel["sha256"] == {"main/binary-amd64/Packages.gz": ("abc", 12)}


def test_repos_created_on_source_with_remote_and_virtual():
    art = FakeArtifactory()
    s = Settings(create_repositories=True, mirrors=[YUM])
    actions = repos.ensure(art, s)
    assert [(a.key, a.action) for a in actions] == [
        ("rocky", "create"), ("rocky-remote", "create"), ("rocky-virtual", "create")]
    remote = art.created["rocky-remote"]
    assert remote["unusedArtifactsCleanupPeriodHours"] == 0
    assert remote["metadataRetrievalTimeoutSecs"] <= remote["retrievalCachePeriodSecs"]
    assert art.created["rocky-virtual"]["repositories"] == ["rocky", "rocky-remote"]
    assert all(a.action == "exists" for a in repos.ensure(art, s))


def test_destination_gets_local_and_virtual_only():
    art = FakeArtifactory()
    s = Settings(role="destination", create_repositories=True, mirrors=[YUM])
    repos.ensure(art, s)
    assert sorted(art.created) == ["rocky", "rocky-virtual"]
    assert art.created["rocky-virtual"]["repositories"] == ["rocky"]


def test_existing_repository_of_the_wrong_kind_is_reported_not_changed():
    art = FakeArtifactory()
    art.created["rocky"] = {"rclass": "local", "packageType": "rpm"}
    actions = repos.ensure(art, Settings(mirrors=[YUM]), dry_run=True)
    assert actions[0].action == "conflict"
    assert art.created["rocky"]["packageType"] == "rpm"


def test_cli_reports_an_unreachable_artifactory_in_one_line(tmp_path, capsys, monkeypatch):
    import httpx

    from artifactory_groundcrew.cli import app

    class Refused:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __getattr__(self, name):
            def fail(*a, **k):
                raise httpx.ConnectError("[Errno 111] Connection refused")
            return fail

    monkeypatch.setattr(app.ArtifactoryClient, "from_settings", classmethod(lambda cls, s: Refused()))
    monkeypatch.setattr(app.log, "configure", lambda *a, **k: None)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "mirrors:\n  - {name: g, type: generic, remote: r, local: l, generic: {paths: [a]}}\n"
    )
    for cmd in (["repos"], ["plan", "g"]):
        assert app.main(["--config", str(cfg), *cmd]) == 1
        err = capsys.readouterr().err
        assert "ConnectError" in err and "Traceback" not in err
