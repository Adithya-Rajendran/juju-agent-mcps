"""Unit tests for juju_docs.server.search_juju_docs.

The Read the Docs search backend is mocked with respx so the tests are
hermetic.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from juju_docs import server as jd

SEARCH_URL = f"{jd.DOCS_BASE}{jd.SEARCH_PATH}"


def _resp(results: list[dict]) -> dict:
    return {
        "count": len(results),
        "next": None,
        "previous": None,
        "results": results,
    }


def _result(
    *,
    title: str,
    path: str,
    version: str = "3.6",
    blocks: list[dict] | None = None,
    domain: str | None = None,
) -> dict:
    return {
        "type": "page",
        "project": jd.DOCS_PROJECT,
        "project_alias": "juju",
        "version": version,
        "title": title,
        "path": path,
        "domain": domain or jd.DOCS_BASE,
        "highlights": {"title": [title]},
        "blocks": blocks or [],
    }


@respx.mock
async def test_search_returns_mapped_fields() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([
                _result(
                    title="Relation (integration)",
                    path="/juju/3.6/reference/relation/",
                    blocks=[{
                        "type": "section",
                        "id": "relation-taxonomy",
                        "title": "Relation taxonomy",
                        "content": "Use juju integrate to add a relation.",
                        "highlights": {
                            "content": [
                                "Use juju integrate to add a "
                                "<span>relation</span>."
                            ],
                        },
                    }],
                )
            ]),
        )
    )

    out = await jd.search_juju_docs("relations", max_results=5)

    assert out["ok"] is True
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["title"] == "Relation (integration)"
    assert r["url"] == (
        "https://documentation.ubuntu.com/juju/3.6/reference/relation/"
    )
    assert "juju integrate" in r["excerpt"]
    assert "<span>" not in r["excerpt"]
    assert r["category"] == "reference"
    assert r["juju_version_hint"] == "juju-3"


@respx.mock
async def test_search_sends_project_and_version_params() -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_resp([])),
    )
    out = await jd.search_juju_docs("anything")
    assert out == {"ok": True, "results": []}
    assert route.called
    request = route.calls.last.request
    assert request.url.params["q"] == "anything"
    assert request.url.params["project"] == jd.DOCS_PROJECT
    assert request.url.params["version"] == jd.DOCS_VERSION


@respx.mock
@pytest.mark.parametrize(
    "version,expected",
    [
        ("2.9", "juju-2"),
        ("3.6", "juju-3"),
        ("4.0", "juju-4"),
        ("latest", None),
        ("", None),
    ],
)
async def test_version_hint_extraction(version, expected) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([
                _result(title="T", path="/juju/x/howto/x/", version=version),
            ]),
        )
    )
    out = await jd.search_juju_docs("anything")
    assert out["results"][0]["juju_version_hint"] == expected


@respx.mock
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/juju/3.6/tutorial/getting-started/", "tutorial"),
        ("/juju/3.6/howto/manage-applications/", "howto"),
        ("/juju/3.6/reference/relation/", "reference"),
        ("/juju/3.6/explanation/architecture/", "explanation"),
        ("/juju/3.6/", None),
        ("", None),
    ],
)
async def test_category_extraction_from_path(path, expected) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([_result(title="T", path=path)]),
        )
    )
    out = await jd.search_juju_docs("anything")
    assert out["results"][0]["category"] == expected


@respx.mock
async def test_category_filter_is_substring_case_insensitive() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([
                _result(title="A", path="/juju/3.6/howto/a/"),
                _result(title="B", path="/juju/3.6/reference/b/"),
            ]),
        )
    )
    out = await jd.search_juju_docs("x", category="HOWTO")
    assert [r["title"] for r in out["results"]] == ["A"]


@respx.mock
async def test_max_results_caps_output() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([
                _result(title=f"T{i}", path=f"/juju/3.6/howto/t{i}/")
                for i in range(10)
            ]),
        )
    )
    out = await jd.search_juju_docs("x", max_results=3)
    assert len(out["results"]) == 3


@respx.mock
async def test_non_200_is_reported_as_failure() -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
    assert await jd.search_juju_docs("x") == {
        "ok": False, "reason": "docs search: HTTP 503",
    }


@respx.mock
async def test_network_error_is_reported_as_failure() -> None:
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("nope"))
    assert await jd.search_juju_docs("x") == {
        "ok": False, "reason": "docs search: ConnectError: nope",
    }


@respx.mock
@pytest.mark.parametrize("content", [b"<html>not json</html>", b"[1, 2]"])
async def test_invalid_json_body_is_reported_as_failure(content) -> None:
    # 200 OK with a non-JSON or non-object body (e.g. a Cloudflare error
    # page leaking through).
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, content=content, headers={"content-type": "text/html"},
        ),
    )
    assert await jd.search_juju_docs("x") == {
        "ok": False, "reason": "docs search: invalid JSON response",
    }


@respx.mock
@pytest.mark.parametrize("query", ["", "   "])
async def test_empty_query_is_rejected_without_http(query) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_resp([])),
    )
    out = await jd.search_juju_docs(query)
    assert out == {"ok": False, "reason": "query is required"}
    assert not route.called


@respx.mock
async def test_zero_max_results_is_rejected_without_http() -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_resp([])),
    )
    out = await jd.search_juju_docs("x", max_results=0)
    assert out == {"ok": False, "reason": "max_results must be >= 1"}
    assert not route.called


@respx.mock
async def test_user_agent_points_at_this_repo() -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_resp([])),
    )
    await jd.search_juju_docs("x")
    ua = route.calls.last.request.headers["User-Agent"]
    assert ua == jd.USER_AGENT
    assert "github.com/Adithya-Rajendran/juju-agent-mcps" in ua


def test_tool_registered_with_description() -> None:
    tools = {t.name: t for t in jd.mcp._tool_manager.list_tools()}  # noqa: SLF001
    assert set(tools) == {"search_juju_docs"}
    assert "ok: false" in tools["search_juju_docs"].description


@respx.mock
async def test_result_without_blocks_falls_back_to_title_excerpt() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([
                _result(title="No blocks", path="/juju/3.6/howto/x/", blocks=[]),
            ]),
        )
    )
    out = await jd.search_juju_docs("x")
    assert out["results"][0]["excerpt"] == "No blocks"


@respx.mock
async def test_block_without_highlights_uses_raw_content() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=_resp([
                _result(
                    title="T",
                    path="/juju/3.6/howto/x/",
                    blocks=[{
                        "type": "section",
                        "id": "s",
                        "title": "S",
                        "content": "raw content body",
                        "highlights": {},
                    }],
                ),
            ]),
        )
    )
    out = await jd.search_juju_docs("x")
    assert out["results"][0]["excerpt"] == "raw content body"
