from __future__ import annotations

import httpx
import pytest

from artifactory_groundcrew import repos
from artifactory_groundcrew.artifactory import ArtifactoryClient
from artifactory_groundcrew.config import DockerFollow, Settings
from artifactory_groundcrew.enumerators import docker
from artifactory_groundcrew.state import State
from artifactory_groundcrew.sync import run_cycle

from .fakes import FakeArtifactory, docker_image

IMAGE = "library/busybox"


def mirror(images=(f"{IMAGE}:1.37.0",), platforms=("linux/amd64",)) -> dict:
    return {"name": "dk", "type": "docker", "remote": "dk-remote", "local": "dk",
            "docker": {"images": list(images), "platforms": list(platforms)}}


@pytest.fixture
def art():
    return FakeArtifactory()


def cycle(art, tmp_path, m, **kw):
    s = Settings(state_dir=tmp_path / "state", workers=4, mirrors=[m])
    return run_cycle(art, s, s.mirrors[0], State(s.state_dir), **kw)


def pull(art, paths, user="alice"):
    """A client pulling through the remote: the tag's index and one platform's files."""
    art.user = user
    for path in paths:
        art.docker_fetch("dk-remote", path)
    art.user = "groundcrew"


def test_a_tag_is_copied_for_the_configured_platforms_with_its_attestation(art, tmp_path):
    folders = docker_image(art, "dk-remote", IMAGE, "1.37.0")
    r = cycle(art, tmp_path, mirror())
    assert r.status == "ok"
    local = set(art.repos["dk"])
    assert f"{IMAGE}/1.37.0/list.manifest.json" in local
    assert sum(p.startswith(f"{IMAGE}/{folders['linux/amd64']}/") for p in local) == 3
    assert not any(p.startswith(f"{IMAGE}/{folders['linux/arm64']}/") for p in local)
    # The index, and manifest plus two blobs for each of the image and its attestation.
    assert len(local) == 7
    assert art.props[("dk", f"{IMAGE}/1.37.0/list.manifest.json")]["groundcrew.requirement"] == f"{IMAGE}:1.37.0"


def test_a_docker_remote_is_never_asked_through_sync_download(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0")
    cycle(art, tmp_path, mirror(platforms=()))
    assert not [c for c in art.calls if c[0] == "download"]
    assert len(art.repos["dk"]) == 13


def test_a_single_platform_tag_keeps_its_files_in_the_tag_folder(art, tmp_path):
    docker_image(art, "dk-remote", "hashicorp/http-echo", "0.2.3", platforms=("linux/amd64",))
    cycle(art, tmp_path, mirror([ "hashicorp/http-echo:0.2.3"], platforms=("linux/arm64",)))
    assert {p.rsplit("/", 1)[0] for p in art.repos["dk"]} == {"hashicorp/http-echo/0.2.3"}
    assert len(art.repos["dk"]) == 3


def test_second_cycle_fetches_nothing_since_digests_match(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0")
    cycle(art, tmp_path, mirror())
    r = cycle(art, tmp_path, mirror(), full=True)
    assert r.status == "ok" and r.fetched == 0


def test_a_moved_tag_is_followed_when_the_list_is_resolved(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "latest")
    cycle(art, tmp_path, mirror([f"{IMAGE}:latest"]))
    old = art.repos["dk"][f"{IMAGE}/latest/list.manifest.json"]
    new_folders = docker_image(art, "dk-remote", IMAGE, "latest", seed="v2")
    r = cycle(art, tmp_path, mirror([f"{IMAGE}:latest"]), full=True)
    assert r.status == "ok" and r.deleted == 0
    assert art.repos["dk"][f"{IMAGE}/latest/list.manifest.json"] != old
    assert any(p.startswith(f"{IMAGE}/{new_folders['linux/amd64']}/") for p in art.repos["dk"])


def test_a_pulled_tag_is_copied_and_resolved_for_the_configured_platforms(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0")
    folders = docker_image(art, "dk-remote", "library/alpine", "3.20")
    m = mirror(platforms=("linux/amd64", "linux/arm64"))
    cycle(art, tmp_path, m)
    arm = folders["linux/arm64"]
    pull(art, ["library/alpine/3.20/list.manifest.json",
               *[p for p in art.docker_files["dk-remote"] if p.startswith(f"library/alpine/{arm}/")]])
    r = cycle(art, tmp_path, m)
    assert r.status == "ok" and r.plan["demand"] == 4
    assert any(p.startswith(f"library/alpine/{folders['linux/amd64']}/") for p in art.repos["dk"])
    props = art.props[("dk", "library/alpine/3.20/list.manifest.json")]
    assert (props["groundcrew.source"], props["groundcrew.requirement"], props["groundcrew.pulled_by"]) == (
        "demand", "library/alpine:3.20", "alice")


def test_a_pulled_tag_that_moves_is_refreshed_with_the_list(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0")
    docker_image(art, "dk-remote", "library/alpine", "latest")
    cycle(art, tmp_path, mirror())
    pull(art, ["library/alpine/latest/list.manifest.json"])
    cycle(art, tmp_path, mirror())
    folders = docker_image(art, "dk-remote", "library/alpine", "latest", seed="v2")
    r = cycle(art, tmp_path, mirror(), full=True)
    assert r.status == "ok"
    assert any(p.startswith(f"library/alpine/{folders['linux/amd64']}/") for p in art.repos["dk"])


def test_no_section_copies_only_what_clients_pull(art, tmp_path):
    docker_image(art, "dk-remote", "library/alpine", "3.20")
    m = {k: v for k, v in mirror().items() if k != "docker"}
    r = cycle(art, tmp_path, m)
    assert r.status == "ok" and not art.repos.get("dk")
    pull(art, ["library/alpine/3.20/list.manifest.json"])
    r = cycle(art, tmp_path, m)
    assert r.status == "ok"
    assert "library/alpine/3.20/list.manifest.json" in art.repos["dk"]


def test_a_missing_tag_withholds_the_cycle(art, tmp_path):
    assert cycle(art, tmp_path, mirror()).status == "enumeration-failed"


def test_image_references_need_a_namespace_and_a_tag():
    with pytest.raises(ValueError, match="library/busybox"):
        Settings(mirrors=[mirror(["busybox:1.37.0"])])
    with pytest.raises(ValueError, match="needs a tag"):
        Settings(mirrors=[mirror([IMAGE])])
    with pytest.raises(ValueError, match="needs a tag"):
        Settings(mirrors=[mirror([f"{IMAGE}@sha256:abc"])])
    Settings(mirrors=[mirror(["registry.example:5000/team/app:1.0"])])


def follow(**rule) -> dict:
    return {**mirror(images=()), "docker": {"follow": [{"image": IMAGE, **rule}], "platforms": ["linux/amd64"]}}


def test_a_rule_copies_the_tags_it_selects(art, tmp_path):
    for tag in ("1.36.1", "1.37.0", "1.37.0-musl", "latest"):
        docker_image(art, "dk-remote", IMAGE, tag)
    r = cycle(art, tmp_path, follow(match=r"\d+\.\d+\.\d+", newest=1))
    assert r.status == "ok"
    assert {p.split("/")[2] for p in art.repos["dk"] if p.endswith("list.manifest.json")} == {"1.37.0"}
    props = art.props[("dk", f"{IMAGE}/1.37.0/list.manifest.json")]
    assert (props["groundcrew.source"], props["groundcrew.requirement"]) == ("config", f"{IMAGE}:1.37.0")


def test_a_new_tag_is_picked_up_within_one_interval(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0")
    m = follow(match=r"[\d.]+", **{"from": "1.37"})
    cycle(art, tmp_path, m)
    assert cycle(art, tmp_path, m).status == "unchanged"
    docker_image(art, "dk-remote", IMAGE, "1.38.0")
    r = cycle(art, tmp_path, m)
    assert r.status == "ok"
    assert f"{IMAGE}/1.38.0/list.manifest.json" in art.repos["dk"]


def test_a_rule_for_a_missing_image_withholds_the_cycle(art, tmp_path):
    assert cycle(art, tmp_path, follow(match=".*", newest=1)).status == "enumeration-failed"


def test_a_tag_both_pulled_and_selected_is_resolved_once(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0")
    m = follow(match=r"[\d.]+", newest=1)
    pull(art, [f"{IMAGE}/1.37.0/list.manifest.json"])
    cycle(art, tmp_path, m)
    art.calls.clear()
    cycle(art, tmp_path, m, full=True)
    assert [c for c in art.calls if c[0] == "manifest" and c[2] == f"{IMAGE}:1.37.0"] == [("manifest", "dk-remote", f"{IMAGE}:1.37.0")]


def test_a_blob_shared_by_two_manifests_is_copied_once(art, tmp_path):
    docker_image(art, "dk-remote", IMAGE, "1.37.0", platforms=("linux/amd64",))
    docker_image(art, "dk-remote", IMAGE, "1.38.0", platforms=("linux/amd64",))
    cycle(art, tmp_path, mirror())
    # Both tags' manifests name the same blobs.
    r = cycle(art, tmp_path, mirror([f"{IMAGE}:1.37.0", f"{IMAGE}:1.38.0"]))
    assert r.status == "ok"
    assert {p.rsplit("/", 1)[1] for p in art.repos["dk"] if p.startswith(f"{IMAGE}/1.38.0/")} == {"manifest.json"}


def rule(**kw) -> DockerFollow:
    return DockerFollow(image=IMAGE, **kw)


def test_selection_is_in_natural_sort_order():
    tags = ["1.10.0", "1.9.2", "1.2.0", "latest", "1.10.0-rc1"]
    assert docker.select(tags, rule(match=r"[\d.]+", newest=2)) == ["1.9.2", "1.10.0"]
    assert docker.select(tags, rule(match=r".*", newest=10)) == ["1.2.0", "1.9.2", "1.10.0", "1.10.0-rc1", "latest"]
    assert docker.select(["16.4-alpine3.20", "16.4-alpine3.19", "16.4"], rule(match=r".*alpine.*", newest=1)) == ["16.4-alpine3.20"]


def test_from_is_a_floor_that_need_not_exist_and_match_is_whole_tag():
    tags = ["0.16.2", "0.17.0", "0.17.1", "0.18.0", "v0.18.0"]
    assert docker.select(tags, rule(match=r"[\d.]+", **{"from": "0.17"})) == ["0.17.0", "0.17.1", "0.18.0"]
    assert docker.select(tags, rule(match=r"\d", **{"from": "0"})) == []
    assert docker.select(tags, rule(match=r"[\d.]+", **{"from": "0.17.0"}, newest=1)) == ["0.18.0"]


def test_rules_need_a_bound_a_valid_regex_and_no_tag():
    with pytest.raises(ValueError, match="needs from or newest"):
        rule(match=".*")
    with pytest.raises(ValueError, match="not a valid regex"):
        rule(match="(", newest=1)
    with pytest.raises(ValueError, match="takes no tag"):
        DockerFollow(image=f"{IMAGE}:1.0", match=".*", newest=1)
    with pytest.raises(ValueError, match="library/busybox"):
        DockerFollow(image="busybox", match=".*", newest=1)
    DockerFollow(image="registry.example:5000/team/app", match=".*", newest=1)


def test_docker_tags_follows_next_links_on_the_remote():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "last=" not in str(req.url):
            return httpx.Response(200, json={"tags": ["a", "b"]},
                                  headers={"link": '</v2/library/busybox/tags/list?last=b&n=2>; rel="next"'})
        return httpx.Response(200, json={"tags": ["c"]})

    c = ArtifactoryClient("https://art.example/artifactory", verify=False)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert c.docker_tags("r", IMAGE) == ["a", "b", "c"]
    base = "https://art.example/artifactory/api/docker/r/v2/library/busybox/tags/list"
    assert seen == [base, f"{base}?last=b&n=2"]
    c._http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404)))
    assert c.docker_tags("r", IMAGE) is None


def test_tag_references_from_paths():
    assert docker.ref_of("library/busybox/1.37.0/list.manifest.json") == "library/busybox:1.37.0"
    assert docker.ref_of("a/b/c/v1/manifest.json") == "a/b/c:v1"
    assert docker.ref_of("library/busybox/sha256__abc/manifest.json") is None
    assert docker.ref_of("library/busybox/1.37.0/sha256__abc") is None


def test_platforms_match_os_architecture_and_optional_variant():
    assert docker._platform_ok({"os": "linux", "architecture": "arm", "variant": "v7"}, ["linux/arm"])
    assert not docker._platform_ok({"os": "linux", "architecture": "arm", "variant": "v6"}, ["linux/arm/v7"])
    assert not docker._platform_ok({"os": "unknown", "architecture": "unknown"}, ["linux/amd64"])
    assert docker._platform_ok({}, [])


def test_repos_are_typed_docker_with_token_authentication():
    art = FakeArtifactory()
    repos.ensure(art, Settings(mirrors=[{**mirror(), "upstream_url": "https://registry-1.docker.io"}]))
    assert art.created["dk-remote"]["packageType"] == "docker"
    assert art.created["dk-remote"]["enableTokenAuthentication"] is True


def test_docker_fetch_maps_cache_paths_to_registry_calls():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/vnd.oci.image.index.v1+json"})

    c = ArtifactoryClient("https://art.example/artifactory", verify=False)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert c.docker_fetch("r", "library/busybox/1.37.0/list.manifest.json")
    assert c.docker_fetch("r", "library/busybox/sha256__aa/manifest.json")
    assert c.docker_fetch("r", "library/busybox/sha256__aa/sha256__bb")
    base = "/artifactory/api/docker/r/v2/library/busybox"
    assert seen == [("GET", f"{base}/manifests/1.37.0"), ("GET", f"{base}/manifests/sha256:aa"),
                    ("HEAD", f"{base}/blobs/sha256:bb")]
