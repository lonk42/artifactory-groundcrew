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


def test_property_values_are_escaped_once():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["properties"])
        return httpx.Response(204)

    client_with(handler).set_properties("py", "a/b.whl", {"groundcrew.requirement": "x>=1,<2;y|z"})
    assert seen == [r"groundcrew.requirement=x>\=1\,<2\;y\|z"]


def test_whoami_reads_the_token_subject_and_tolerates_failure():
    c = client_with(lambda req: httpx.Response(200, json={"subject": "jfac@abc/users/groundcrew"}))
    assert c.whoami() == "groundcrew"
    assert client_with(lambda req: httpx.Response(403)).whoami() == ""


def test_list_properties_keeps_only_the_prefix_and_joins_values():
    rows = [{"repo": "py", "path": "a", "name": "b.whl", "properties": [
        {"key": "groundcrew.source", "value": "config"}, {"key": "groundcrew.source", "value": "demand"},
        {"key": "pypi.name", "value": "b"}]}]
    c = client_with(lambda req: httpx.Response(200, json={"results": rows}))
    assert c.list_properties("py", "groundcrew.") == {"a/b.whl": {"groundcrew.source": "config,demand"}}
