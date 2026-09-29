from __future__ import annotations

import httpx

from artifactory_groundcrew.artifactory import ArtifactoryClient


def client_with(handler) -> ArtifactoryClient:
    c = ArtifactoryClient("https://art.example/artifactory")
    c._http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return c


def test_copy_target_keeps_special_characters_literal():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["to"] = request.url.params["to"]
        return httpx.Response(200, json={"messages": [{"level": "INFO", "message": "copied"}]})

    client_with(handler).copy("r-cache", "shells:/fish:/a b+c.deb", "local")
    assert seen["to"] == "/local/shells:/fish:/a b+c.deb"
    assert seen["path"] == "/artifactory/api/copy/r-cache/shells:/fish:/a b+c.deb"


def test_copy_error_message_in_a_200_is_an_error():
    def handler(request):
        return httpx.Response(200, json={"messages": [{"level": "ERROR", "message": "nope"}]})

    try:
        client_with(handler).copy("a", "x", "b")
    except RuntimeError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("copy should have raised")


def test_list_files_projects_the_natural_key_and_joins_paths():
    def handler(request):
        body = request.content.decode()
        assert '.include("repo","path","name"' in body
        return httpx.Response(200, json={"results": [
            {"repo": "l", "path": ".", "name": "top.txt", "size": 1, "sha256": "a"},
            {"repo": "l", "path": "shells:/fish", "name": "f.deb", "size": 2, "sha256": "b"},
        ]})

    files = client_with(handler).list_files("l")
    assert set(files) == {"top.txt", "shells:/fish/f.deb"}
