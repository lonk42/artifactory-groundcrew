from __future__ import annotations

import httpx

from artifactory_groundcrew import artifactory
from artifactory_groundcrew.artifactory import ArtifactoryClient


def client_with(handler) -> ArtifactoryClient:
    c = ArtifactoryClient("https://art.example/artifactory")
    c._http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return c


def first_page(rows):
    """An AQL endpoint holding one page of rows."""
    def handler(request):
        return httpx.Response(200, json={"results": rows if ".offset(0)." in request.content.decode() else []})
    return handler


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
        return first_page([
            {"repo": "l", "path": ".", "name": "top.txt", "size": 1, "sha256": "a"},
            {"repo": "l", "path": "shells:/fish", "name": "f.deb", "size": 2, "sha256": "b"},
        ])(request)

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
    c = client_with(first_page(rows))
    assert c.list_properties("py", "groundcrew.") == {"a/b.whl": {"groundcrew.source": "config,demand"}}


def test_aql_reads_pages_until_one_is_empty(monkeypatch):
    monkeypatch.setattr(artifactory, "AQL_PAGE", 2)
    rows = [{"repo": "l", "path": "d", "name": f"{i}.rpm", "size": i, "sha256": str(i)} for i in range(5)]
    asked = []

    def handler(request):
        body = request.content.decode()
        assert '.sort({"$asc":["repo","path","name"]})' in body
        offset = int(body.split(".offset(")[1].split(")")[0])
        asked.append(offset)
        # A short page is not the end: a property projection groups rows by file.
        page = rows[offset:offset + 2] if offset != 2 else rows[2:3]
        return httpx.Response(200, json={"results": page})

    assert set(client_with(handler).list_files("l")) == {f"d/{i}.rpm" for i in range(3)} | {"d/4.rpm"}
    assert asked == [0, 2, 4, 6]


def test_list_property_projects_only_that_property():
    def handler(request):
        assert '"@groundcrew.mtime")' in request.content.decode()
        return first_page([{"repo": "l", "path": "9", "name": "a.rpm",
                            "properties": [{"key": "groundcrew.mtime", "value": "1000"}]}])(request)

    assert client_with(handler).list_property("l", "groundcrew.mtime") == {"9/a.rpm": "1000"}
