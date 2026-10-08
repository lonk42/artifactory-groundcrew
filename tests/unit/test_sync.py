from __future__ import annotations

import pytest

from artifactory_groundcrew.config import Settings
from artifactory_groundcrew.state import State
from artifactory_groundcrew.sync import plan, run_cycle

from .fakes import FakeArtifactory, apt_repo, yum_repo

ROOT = "9/BaseOS/x86_64/os/"


def settings(tmp_path, mirror: dict, **kw) -> Settings:
    return Settings(state_dir=tmp_path / "state", workers=4, mirrors=[mirror], **kw)


def yum_mirror(paths=(ROOT,), **kw) -> dict:
    return {"name": "rocky", "type": "yum", "remote": "r", "local": "l", "yum": {"paths": list(paths)}, **kw}


@pytest.fixture
def art():
    return FakeArtifactory()


def cycle(art, s, **kw):
    return run_cycle(art, s, s.mirrors[0], State(s.state_dir), **kw)


def test_yum_first_sync_mirrors_everything_byte_for_byte(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A", "Packages/b.rpm": b"BB"})
    s = settings(tmp_path, yum_mirror())
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert art.repos["l"] == art.upstream["r"]


def test_packages_are_published_before_metadata_and_repomd_last(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    s = settings(tmp_path, yum_mirror())
    cycle(art, s)
    copies = [p for op, repo, p in art.calls if op == "copy" and repo == "l"]
    assert copies[0].endswith(".rpm")
    assert copies[-2:] == [f"{ROOT}repodata/repomd.xml", f"{ROOT}repodata/repomd.xml.asc"]


def test_failed_package_withholds_metadata_and_deletions(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A", "Packages/b.rpm": b"B"})
    art.repos["l"] = {f"{ROOT}Packages/old.rpm": b"OLD"}
    art.fail_download.add(f"{ROOT}Packages/b.rpm")
    s = settings(tmp_path, yum_mirror())
    res = cycle(art, s)
    assert res.status == "failed"
    assert f"{ROOT}repodata/repomd.xml" not in art.repos["l"]
    assert f"{ROOT}Packages/old.rpm" in art.repos["l"]


def test_unchanged_upstream_is_skipped_after_one_repomd_read(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    s = settings(tmp_path, yum_mirror())
    assert cycle(art, s).status == "ok"
    art.calls.clear()
    assert cycle(art, s).status == "unchanged"
    reads = [p for op, _, p in art.calls if op == "read"]
    assert reads == [f"{ROOT}repodata/repomd.xml"]


def test_upstream_change_is_seen_through_a_cache_that_never_refreshes(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    s = settings(tmp_path, yum_mirror())
    cycle(art, s)
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a2.rpm": b"A2"})
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert art.repos["l"] == art.upstream["r"]


def test_upstream_removal_deletes_any_share_of_the_mirror(art, tmp_path):
    pkgs = {f"Packages/p{i}.rpm": bytes([i]) for i in range(40)}
    art.upstream["r"] = yum_repo(ROOT, pkgs)
    s = settings(tmp_path, yum_mirror())
    cycle(art, s)
    for i in range(20):
        del pkgs[f"Packages/p{i}.rpm"]
    art.upstream["r"] = yum_repo(ROOT, pkgs)
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert res.deleted == 21  # 20 packages and the replaced primary
    assert art.repos["l"] == art.upstream["r"]


def test_removing_a_path_from_the_list_deletes_its_content(art, tmp_path):
    other = "9/AppStream/x86_64/os/"
    art.upstream["r"] = {**yum_repo(ROOT, {"Packages/a.rpm": b"A"}), **yum_repo(other, {"Packages/b.rpm": b"B"})}
    s = settings(tmp_path, yum_mirror(paths=(ROOT, other)))
    cycle(art, s)
    assert f"{other}Packages/b.rpm" in art.repos["l"]
    s = settings(tmp_path, yum_mirror(paths=(ROOT,)))
    assert cycle(art, s).status == "ok"
    assert not any(p.startswith(other) for p in art.repos["l"])
    assert f"{ROOT}Packages/a.rpm" in art.repos["l"]


def test_files_outside_scope_are_never_touched(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    art.repos["l"] = {"unrelated/file.bin": b"keep"}
    s = settings(tmp_path, yum_mirror(scope="9/"))
    cycle(art, s)
    assert art.repos["l"]["unrelated/file.bin"] == b"keep"


def test_dry_run_changes_nothing(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    s = settings(tmp_path, yum_mirror())
    res = cycle(art, s, dry_run=True)
    assert res.status == "dry-run"
    assert res.plan["fetch"] == 1
    assert "l" not in art.repos


def test_checksum_mismatch_in_primary_refuses_the_cycle(art, tmp_path):
    files = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    primary = next(p for p in files if "primary" in p)
    body = bytearray(files[primary])
    body[len(body) // 2] ^= 0xFF
    files[primary] = bytes(body)
    art.upstream["r"] = files
    s = settings(tmp_path, yum_mirror())
    res = cycle(art, s)
    assert res.status == "enumeration-failed"
    assert "l" not in art.repos


def apt_mirror(base="debian", suite="bookworm", **kw) -> dict:
    return {
        "name": "deb", "type": "apt", "remote": "r", "local": "l",
        "apt": {"base": base, "suites": [suite], "components": ["main"], "architectures": ["amd64"]}, **kw,
    }


def test_apt_mirror_is_byte_for_byte_and_skips_absent_listed_indexes(art, tmp_path):
    pkgs = {"pool/main/h/hello_1_amd64.deb": b"HELLO"}
    art.upstream["r"] = apt_repo("debian/", "bookworm", pkgs)
    s = settings(tmp_path, apt_mirror())
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert art.repos["l"] == art.upstream["r"]
    copies = [p for op, repo, p in art.calls if op == "copy" and repo == "l"]
    assert copies[-1] == "debian/dists/bookworm/InRelease"
    assert not any("contrib" in p for p in copies)


def test_apt_by_hash_files_are_mirrored(art, tmp_path):
    art.upstream["r"] = apt_repo("debian/", "bookworm", {"pool/main/a_1_amd64.deb": b"A"}, by_hash=True)
    s = settings(tmp_path, apt_mirror())
    assert cycle(art, s).status == "ok"
    assert any("/by-hash/SHA256/" in p for p in art.repos["l"])


def test_yum_zstd_primary_is_read(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"}, compression="zst")
    s = settings(tmp_path, yum_mirror())
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert art.repos["l"] == art.upstream["r"]


def test_apt_xz_packages_are_read(art, tmp_path):
    art.upstream["r"] = apt_repo("debian/", "bookworm", {"pool/main/a_1_amd64.deb": b"A"}, compression="xz")
    s = settings(tmp_path, apt_mirror())
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert "debian/pool/main/a_1_amd64.deb" in art.repos["l"]


def test_apt_cnf_and_dep11_are_mirrored_for_wanted_architectures_only(art, tmp_path):
    # Ubuntu lists these per architecture. apt fetches cnf once command-not-found
    # is installed, and a 404 on it fails the whole update.
    extras = {
        "main/cnf/Commands-amd64.xz": b"CNF-AMD",
        "main/cnf/Commands-arm64.xz": b"CNF-ARM",
        "main/dep11/Components-amd64.yml.gz": b"DEP-AMD",
        "main/dep11/Components-arm64.yml.gz": b"DEP-ARM",
        "main/dep11/icons-64x64.tar.gz": b"ICONS",
        "main/i18n/Translation-en.xz": b"EN",
    }
    art.upstream["r"] = apt_repo("ubuntu/", "noble", {"pool/main/a_1_amd64.deb": b"A"},
                                 extras=extras, architectures="amd64 arm64")
    s = settings(tmp_path, apt_mirror(base="ubuntu", suite="noble"))
    assert cycle(art, s).status == "ok"
    d = "ubuntu/dists/noble/"
    got = {p[len(d):] for p in art.repos["l"] if p.startswith(d + "main/") and "/binary-" not in p}
    assert got == {"main/cnf/Commands-amd64.xz", "main/dep11/Components-amd64.yml.gz",
                   "main/dep11/icons-64x64.tar.gz", "main/i18n/Translation-en.xz"}


def test_identical_indexes_sharing_a_by_hash_path_are_planned_once(art, tmp_path):
    # Ubuntu's empty dep11 icon tarballs all hash the same.
    extras = {f"main/dep11/icons-{n}.tar": b"EMPTY" for n in ("48x48", "64x64", "128x128")}
    art.upstream["r"] = apt_repo("debian/", "bookworm", {"pool/main/a_1_amd64.deb": b"A"}, by_hash=True, extras=extras)
    s = settings(tmp_path, apt_mirror())
    p, _ = plan(art, s, s.mirrors[0], State(s.state_dir))
    paths = [m.path for m in p.metadata]
    assert len(paths) == len(set(paths))
    assert sum("/dep11/by-hash/" in x for x in paths) == 1


def generic_mirror(paths=("tools/a.tgz", "tools/b.tgz")) -> dict:
    return {"name": "g", "type": "generic", "remote": "r", "local": "l", "generic": {"paths": list(paths)}}


def test_generic_list_removal_keeps_content(art, tmp_path):
    art.upstream["r"] = {"tools/a.tgz": b"A", "tools/b.tgz": b"B"}
    assert cycle(art, settings(tmp_path, generic_mirror())).status == "ok"
    assert art.props[("l", "tools/a.tgz")]["groundcrew.source"] == "config"
    r = cycle(art, settings(tmp_path, generic_mirror(["tools/a.tgz"])))
    assert r.status == "ok" and r.deleted == 0
    assert sorted(art.repos["l"]) == ["tools/a.tgz", "tools/b.tgz"]


def test_generic_demand_copies_the_pulled_file(art, tmp_path):
    art.upstream["r"] = {"tools/a.tgz": b"A", "tools/other.tgz": b"O"}
    s = settings(tmp_path, generic_mirror(["tools/a.tgz"]))
    cycle(art, s)
    art.user = "alice"
    art.read("r", "tools/other.tgz")
    r = cycle(art, s)
    assert r.status == "ok" and r.plan["demand"] == 1
    assert art.repos["l"]["tools/other.tgz"] == b"O"
    assert art.props[("l", "tools/other.tgz")] | {"groundcrew.ingested": ""} == {
        "groundcrew.source": "demand", "groundcrew.requirement": "tools/other.tgz",
        "groundcrew.pulled_by": "alice", "groundcrew.ingested": ""}


def test_config_change_defeats_the_unchanged_shortcut(art, tmp_path):
    art.upstream["r"] = apt_repo("debian/", "bookworm", {"pool/main/a_1_amd64.deb": b"A"})
    s = settings(tmp_path, apt_mirror())
    cycle(art, s)
    m = apt_mirror()
    m["apt"]["extra_indexes"] = []
    s2 = settings(tmp_path, m)
    p, _ = plan(art, s2, s2.mirrors[0], State(s2.state_dir))
    assert not p.unchanged


def test_second_process_is_locked_out(art, tmp_path):
    art.upstream["r"] = yum_repo(ROOT, {"Packages/a.rpm": b"A"})
    s = settings(tmp_path, yum_mirror())
    state = State(s.state_dir)
    with state.lock("rocky") as held:
        assert held
        assert cycle(art, s).status == "locked"


def test_flat_apt_mirror_filters_architectures(art, tmp_path):
    from .fakes import flat_apt_repo

    base = "shells/Debian_12/"
    art.upstream["r"] = flat_apt_repo(base, {
        "amd64/fish_4_amd64.deb": ("amd64", b"AMD"),
        "arm64/fish_4_arm64.deb": ("arm64", b"ARM"),
        "all/fish-doc_4_all.deb": ("all", b"DOC"),
    })
    m = {"name": "fish", "type": "apt", "remote": "r", "local": "l",
         "apt": {"base": base, "flat": True, "architectures": ["amd64"]}}
    s = settings(tmp_path, m)
    res = cycle(art, s)
    assert res.status == "ok", res.error
    assert sorted(p for p in art.repos["l"] if p.endswith(".deb")) == [
        f"{base}all/fish-doc_4_all.deb", f"{base}amd64/fish_4_amd64.deb"]
    assert f"{base}Release.key" in art.repos["l"]
    assert f"{base}Sources" not in art.repos["l"]
    copies = [p for op, repo, p in art.calls if op == "copy" and repo == "l"]
    assert copies[-1] == f"{base}Release.gpg"
