from __future__ import annotations

import pytest
from pydantic import ValidationError

from artifactory_groundcrew.config import Settings, TreeSource
from artifactory_groundcrew.state import State
from artifactory_groundcrew.sync import MTIME_PROPERTY, run_cycle

from .fakes import FakeArtifactory, tree_upstream, yum_repo

LIST = "fullfiletimelist-test"


def release(minor: str, packages: dict[str, bytes]) -> dict[str, bytes]:
    root = f"{minor}/BaseOS/x86_64/os/"
    return {
        **yum_repo(root, packages),
        f"{root}RPM-GPG-KEY-Rocky-9": b"KEY9",
        f"{root}media.repo": b"AAAA",
        f"{minor}/isos/x86_64/Rocky-{minor}-dvd.iso": b"ISO" + minor.encode(),
    }


def rocky(current: str = "9.2", packages=None, mtimes=None, retired=("9.1",)) -> dict[str, bytes]:
    files = {f"{m}/README.txt": b"moved to the vault\n" for m in retired}
    files.update(release(current, packages or {"Packages/a.rpm": b"A"}))
    files["RPM-GPG-KEY-Rocky-9"] = b"KEY9"
    links = {"9": current, f"{current}/isos/x86_64/Rocky-9-latest-dvd.iso": f"Rocky-{current}-dvd.iso"}
    return tree_upstream(files, links, mtimes, LIST)


def tree_mirror(**tree) -> dict:
    return {"name": "rocky", "type": "tree", "remote": "r", "local": "l",
            "tree": {"filelist": LIST, "include": ["9/**", "9.*/**", "RPM-GPG-KEY-*"], **tree}}


@pytest.fixture
def art():
    return FakeArtifactory()


def cycle(art, tmp_path, mirror, **kw):
    s = Settings(state_dir=tmp_path / "state", workers=4, mirrors=[mirror])
    return run_cycle(art, s, s.mirrors[0], State(s.state_dir), **kw)


def test_tree_mirrors_the_selected_files_with_directory_links_as_copies(art, tmp_path):
    art.upstream["r"] = rocky()
    res = cycle(art, tmp_path, tree_mirror(exclude=["**/isos/**"]))
    assert res.status == "ok", res.error
    want = {p: b for p, b in art.upstream["r"].items() if p != LIST and "/isos/" not in p}
    assert art.repos["l"] == want
    assert art.repos["l"]["9/BaseOS/x86_64/os/Packages/a.rpm"] == b"A"
    assert art.repos["l"]["9/BaseOS/x86_64/os/RPM-GPG-KEY-Rocky-9"] == b"KEY9"
    assert "9.1/README.txt" in art.repos["l"]


def test_a_file_link_is_mirrored_as_the_file(art, tmp_path):
    art.upstream["r"] = rocky()
    assert cycle(art, tmp_path, tree_mirror()).status == "ok"
    assert art.repos["l"]["9/isos/x86_64/Rocky-9-latest-dvd.iso"] == b"ISO9.2"
    assert art.repos["l"]["9.2/isos/x86_64/Rocky-9-latest-dvd.iso"] == b"ISO9.2"


def test_packages_come_before_metadata_and_each_repomd_after_its_files(art, tmp_path):
    art.upstream["r"] = rocky()
    cycle(art, tmp_path, tree_mirror())
    copies = [p for op, repo, p in art.calls if op == "copy" and repo == "l"]
    first_meta = min(i for i, p in enumerate(copies) if "/repodata/" in p)
    assert all("/repodata/" not in p for p in copies[:first_meta])
    assert all("/repodata/" in p for p in copies[first_meta:])
    roots = [p for p in copies if "/repodata/repomd.xml" in p]
    assert copies[-len(roots):] == roots
    assert roots[:2] == ["9.2/BaseOS/x86_64/os/repodata/repomd.xml", "9.2/BaseOS/x86_64/os/repodata/repomd.xml.asc"]


def test_a_new_minor_moves_the_link_and_the_retired_one_follows_upstream(art, tmp_path):
    art.upstream["r"] = rocky()
    assert cycle(art, tmp_path, tree_mirror()).status == "ok"
    # Retargeting a symlink recreates it, so its mtime changes.
    art.upstream["r"] = rocky("9.3", {"Packages/b.rpm": b"B"}, retired=("9.1", "9.2"),
                              mtimes={"9.3/isos/x86_64/Rocky-9-latest-dvd.iso": 2000})
    res = cycle(art, tmp_path, tree_mirror())
    assert res.status == "ok", res.error
    local = art.repos["l"]
    assert local["9/BaseOS/x86_64/os/Packages/b.rpm"] == b"B"
    assert "9/BaseOS/x86_64/os/Packages/a.rpm" not in local
    assert not any(p.startswith("9.2/") and p != "9.2/README.txt" for p in local)
    assert local["9/isos/x86_64/Rocky-9-latest-dvd.iso"] == b"ISO9.3"
    assert local == {p: b for p, b in art.upstream["r"].items() if p != LIST}


def test_a_changed_file_of_the_same_size_is_fetched_again(art, tmp_path):
    art.upstream["r"] = rocky()
    assert cycle(art, tmp_path, tree_mirror()).status == "ok"
    path = "9.2/BaseOS/x86_64/os/media.repo"
    assert art.props[("l", path)] == {MTIME_PROPERTY: "1000"}
    up = rocky(mtimes={path: 2000, "9/BaseOS/x86_64/os/media.repo": 2000})
    up[path] = up["9/BaseOS/x86_64/os/media.repo"] = b"BBBB"
    art.upstream["r"] = up
    res = cycle(art, tmp_path, tree_mirror())
    assert res.status == "ok", res.error
    assert res.fetched == 2
    assert art.repos["l"][path] == b"BBBB"
    assert art.props[("l", path)] == {MTIME_PROPERTY: "2000"}


def test_an_unchanged_file_list_is_skipped(art, tmp_path):
    art.upstream["r"] = rocky()
    assert cycle(art, tmp_path, tree_mirror()).status == "ok"
    art.calls.clear()
    assert cycle(art, tmp_path, tree_mirror()).status == "unchanged"
    assert [op for op, _, _ in art.calls if op not in ("expire", "read")] == []


def test_a_repomd_naming_an_excluded_file_fails_enumeration(art, tmp_path):
    art.upstream["r"] = rocky()
    res = cycle(art, tmp_path, tree_mirror(exclude=["**/*-other.xml.bz2"]))
    assert res.status == "enumeration-failed"
    assert "patterns exclude" in res.error
    assert "l" not in art.repos


def test_a_link_target_is_identified_by_reading_through_it(art, tmp_path):
    files = {**release("9.1", {"Packages/old.rpm": b"OLD"}), **release("9.2", {"Packages/new.rpm": b"NEW"})}
    art.upstream["r"] = tree_upstream(files, {"9": "9.1"}, filelist=LIST)
    res = cycle(art, tmp_path, tree_mirror(include=["9/**"]))
    assert res.status == "ok", res.error
    assert art.repos["l"]["9/BaseOS/x86_64/os/Packages/old.rpm"] == b"OLD"
    assert not any(p.endswith("new.rpm") for p in art.repos["l"])


def test_files_outside_the_scope_are_neither_listed_nor_deleted(art, tmp_path):
    art.upstream["r"] = rocky()
    art.repos["l"] = {"other/keep.txt": b"K", "9/stray.txt": b"S"}
    res = cycle(art, tmp_path, {**tree_mirror(include=[]), "scope": "9/"})
    assert res.status == "ok", res.error
    local = art.repos["l"]
    assert local["other/keep.txt"] == b"K"
    assert "9/stray.txt" not in local
    assert "9/BaseOS/x86_64/os/Packages/a.rpm" in local
    assert not any(p.startswith("9.2/") or p.startswith("RPM-") for p in local)


def test_an_unknown_file_list_version_fails_enumeration(art, tmp_path):
    art.upstream["r"] = {LIST: b"[Version]\n3\n\n[Files]\n1\tf\t1\ta\n"}
    res = cycle(art, tmp_path, tree_mirror())
    assert res.status == "enumeration-failed"
    assert "version 3" in res.error


@pytest.mark.parametrize("pattern, path, matches", [
    ("9/**", "9/BaseOS/x86_64/os/a.rpm", True),
    ("9/**", "9.8/BaseOS/a.rpm", False),
    ("9.*/**", "9.8/BaseOS/a.rpm", True),
    ("*.rpm", "9/a.rpm", False),
    ("**/*.rpm", "9/a.rpm", True),
    ("**/*.rpm", "a.rpm", True),
    ("**/isos/**", "9.8/isos/x86_64/dvd.iso", True),
    ("RPM-GPG-KEY-Rocky-?", "RPM-GPG-KEY-Rocky-9", True),
    ("RPM-GPG-KEY-Rocky-[!8]", "RPM-GPG-KEY-Rocky-8", False),
    (r"re:^9\.\d+/", "9.8/BaseOS/a.rpm", True),
    (r"re:/debug/", "9.8/BaseOS/x86_64/debug/tree/a.rpm", True),
    (r"re:^9/", "9.8/a", False),
])
def test_patterns_are_globs_over_the_whole_path_or_searched_regexes(pattern, path, matches):
    assert TreeSource(filelist=LIST, include=[pattern]).selects(path) is matches


def test_exclude_wins_over_include():
    src = TreeSource(filelist=LIST, include=["9/**"], exclude=["**/debug/**"])
    assert src.selects("9/BaseOS/x86_64/os/a.rpm")
    assert not src.selects("9/BaseOS/x86_64/debug/tree/a.rpm")


def test_an_invalid_regex_is_refused():
    with pytest.raises(ValidationError, match="not valid"):
        TreeSource(filelist=LIST, include=["re:("])
