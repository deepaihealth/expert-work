"""B-267 — the fixed-result ``web_search`` backend (``web_search_fixture``).

A manifest's ``web_search`` entry may pick ``config.fixture: <name>``; with the
platform's fixture directory configured the tool answers from
``<dir>/<name>.json``. Anything else is a build error, never a silent fall back
to real search. The model sees the same tool: name, description and the
rendering of results are the real ``web_search``'s.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from expert_work.protocol import BuiltinToolSpec
from orchestrator import AgentFactoryError
from orchestrator.tools import ToolContext
from orchestrator.tools.assembly import ToolEnv, build_tool_registry
from orchestrator.tools.web_search import RecordingTavilyClient, WebSearchTool
from orchestrator.tools.web_search_fixture import (
    FixtureWebSearchClient,
    WebSearchFixtureError,
    load_web_search_fixture,
)

_LOGGER = "orchestrator.tools.web_search_fixture"

_DEW = {"title": "Dew point", "url": "https://example.org/dew", "content": "Dew forms."}
_RH = {"title": "Relative humidity", "url": "https://example.org/rh", "content": "RH is a ratio."}
_GENERIC = {"title": "Humidity", "url": "https://example.org/h", "content": "Humidity overview."}

_FIXTURE: dict[str, Any] = {
    "entries": [
        {"id": "humidity-generic", "match": [["湿度"]], "results": [_GENERIC]},
        {"id": "dew-point", "match": [["露点"], ["dew", "point"]], "results": [_DEW]},
        {"id": "relative-humidity", "match": [["相对", "湿度"]], "results": [_RH, _GENERIC]},
        {"id": "dew-point-dup", "match": [["露点"]], "results": [_GENERIC]},
    ]
}


def _write(tmp_path: Path, name: str = "demo", body: object = _FIXTURE) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4())


# --------------------------------------------------------------------------- matching


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "entry"),
    [
        # One list fully present → that entry.
        ("什么是露点", "dew-point"),
        # Wording is normalised: case, width, spaces and punctuation do not matter.
        ("DEW-Point?", "dew-point"),
        ("\uff24\uff45\uff57\u3000\uff50\uff4f\uff49\uff4e\uff54", "dew-point"),
        # Most matched keywords wins: 相对+湿度 (2) beats 湿度 (1).
        ("相对湿度是什么意思", "relative-humidity"),
        ("湿度 相对 定义", "relative-humidity"),
        # Ties go to file order: dew-point precedes dew-point-dup.
        ("露点温度", "dew-point"),
        # Only one keyword of a two-keyword list → that list does not match.
        ("湿度", "humidity-generic"),
    ],
)
async def test_the_entry_with_the_most_matched_keywords_wins(
    tmp_path: Path, query: str, entry: str
) -> None:
    client = load_web_search_fixture(str(_write(tmp_path).parent), "demo")
    raw = await client.search(query=query, max_results=5, tenant_id=None)
    assert raw["fixture"] == {"fixture": "demo", "fixture_entry": entry, "fixture_miss": False}


@pytest.mark.asyncio
async def test_results_come_back_in_file_order_capped(tmp_path: Path) -> None:
    client = load_web_search_fixture(str(_write(tmp_path).parent), "demo")
    raw = await client.search(query="相对湿度", max_results=1, tenant_id=None)
    assert raw["results"] == [_RH]
    raw = await client.search(query="相对湿度", max_results=5, tenant_id=None)
    assert raw["results"] == [_RH, _GENERIC]


@pytest.mark.asyncio
async def test_no_match_is_an_empty_result_marked_as_a_miss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = load_web_search_fixture(str(_write(tmp_path).parent), "demo")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        raw = await client.search(query="虚构的书名 作者", max_results=5, tenant_id=None)
    assert raw["results"] == []
    assert raw["fixture"] == {"fixture": "demo", "fixture_entry": None, "fixture_miss": True}
    [record] = [r for r in caplog.records if r.name == _LOGGER]
    assert record.getMessage() == (
        'web_search.fixture fixture=demo entry=- miss=true query="虚构的书名 作者"'
    )


@pytest.mark.asyncio
async def test_every_query_is_logged_with_its_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = load_web_search_fixture(str(_write(tmp_path).parent), "demo")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await client.search(query="什么是露点", max_results=5, tenant_id=None)
    [record] = [r for r in caplog.records if r.name == _LOGGER]
    assert record.getMessage() == (
        'web_search.fixture fixture=demo entry=dew-point miss=false query="什么是露点"'
    )


# --------------------------------------------------------------------------- loading


@pytest.mark.parametrize(
    "name",
    ["", "../demo", "a/b", "a\\b", ".demo", "demo.json", "dé", "x" * 65, "/etc/passwd"],
)
def test_unsafe_names_are_refused(tmp_path: Path, name: str) -> None:
    _write(tmp_path)
    with pytest.raises(WebSearchFixtureError, match="fixture name"):
        load_web_search_fixture(str(tmp_path), name)


def test_a_non_string_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WebSearchFixtureError, match="fixture name"):
        load_web_search_fixture(str(tmp_path), 3)


def test_no_directory_is_refused() -> None:
    with pytest.raises(WebSearchFixtureError, match="not available in this deployment"):
        load_web_search_fixture(None, "demo")


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WebSearchFixtureError, match="'nope' not found"):
        load_web_search_fixture(str(tmp_path), "nope")


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        {"entries": "x"},
        {"entries": [{"id": "a", "match": [], "results": []}]},
        {"entries": [{"id": "a", "match": [[]], "results": []}]},
        {"entries": [{"id": "a", "match": [["  "]], "results": []}]},
        {"entries": [{"id": "a", "match": [["k"]], "results": [{"title": "t"}]}]},
        {"entries": [{"id": "a", "match": [["k"]], "results": [], "extra": 1}]},
        {
            "entries": [
                {"id": "a", "match": [["k"]], "results": []},
                {"id": "a", "match": [["j"]], "results": []},
            ]
        },
    ],
    ids=[
        "not-json",
        "entries-not-list",
        "no-match-lists",
        "empty-match-list",
        "blank-keyword",
        "result-missing-fields",
        "unknown-key",
        "duplicate-id",
    ],
)
def test_a_malformed_file_is_refused(tmp_path: Path, body: object) -> None:
    path = tmp_path / "bad.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    with pytest.raises(WebSearchFixtureError, match="fixture 'bad' is invalid"):
        load_web_search_fixture(str(tmp_path), "bad")


def test_a_symlink_out_of_the_directory_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside, "secret")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "demo.json").symlink_to(outside / "secret.json")
    with pytest.raises(WebSearchFixtureError, match="'demo' not found"):
        load_web_search_fixture(str(fixtures), "demo")


def test_a_symlink_inside_the_directory_is_followed(tmp_path: Path) -> None:
    """A Kubernetes configMap volume mounts each key as ``<key> -> ..data/<key>``."""
    data = tmp_path / "..data"
    data.mkdir()
    _write(data)
    (tmp_path / "demo.json").symlink_to(data / "demo.json")
    assert isinstance(load_web_search_fixture(str(tmp_path), "demo"), FixtureWebSearchClient)


# --------------------------------------------------------------------------- assembly


async def _registry(env: ToolEnv, config: dict[str, Any]) -> WebSearchTool:
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="web_search", config=config)], tool_env=env
    )
    tool = registry.get("web_search")
    assert isinstance(tool, WebSearchTool)
    return tool


@pytest.mark.asyncio
async def test_a_fixture_replaces_the_real_backend(tmp_path: Path) -> None:
    _write(tmp_path)
    real = RecordingTavilyClient(results=[_DEW])
    env = ToolEnv(web_search_client=real, web_search_fixtures_dir=str(tmp_path))
    tool = await _registry(env, {"fixture": "demo", "max_results": 3})
    assert isinstance(tool.client, FixtureWebSearchClient)
    assert tool.default_max_results == 3
    await tool.call({"query": "露点"}, ctx=_ctx())
    assert real.last_query is None


@pytest.mark.asyncio
async def test_a_fixture_needs_no_real_backend(tmp_path: Path) -> None:
    _write(tmp_path)
    tool = await _registry(ToolEnv(web_search_fixtures_dir=str(tmp_path)), {"fixture": "demo"})
    assert isinstance(tool.client, FixtureWebSearchClient)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixtures_dir", "name", "message"),
    [
        (None, "demo", "not available in this deployment"),
        ("DIR", "missing", "'missing' not found"),
        ("DIR", "../demo", "fixture name"),
    ],
    ids=["no-directory", "missing-file", "unsafe-name"],
)
async def test_a_bad_fixture_is_a_build_error_not_real_search(
    tmp_path: Path, fixtures_dir: str | None, name: str, message: str
) -> None:
    _write(tmp_path)
    env = ToolEnv(
        web_search_client=RecordingTavilyClient(results=[_DEW]),
        web_search_fixtures_dir=str(tmp_path) if fixtures_dir == "DIR" else None,
    )
    with pytest.raises(AgentFactoryError, match=message):
        await _registry(env, {"fixture": name})


@pytest.mark.asyncio
@pytest.mark.parametrize("knowledge_first", [False, True])
async def test_the_model_sees_the_same_tool(tmp_path: Path, knowledge_first: bool) -> None:
    _write(tmp_path)
    fixture = WebSearchTool(
        client=load_web_search_fixture(str(tmp_path), "demo"), knowledge_first=knowledge_first
    )
    real = WebSearchTool(client=RecordingTavilyClient(), knowledge_first=knowledge_first)
    assert fixture.spec == real.spec


@pytest.mark.asyncio
async def test_results_render_like_real_search(tmp_path: Path) -> None:
    _write(tmp_path)
    fixture = WebSearchTool(client=load_web_search_fixture(str(tmp_path), "demo"))
    real = WebSearchTool(client=RecordingTavilyClient(results=[_RH, _GENERIC]))
    got = await fixture.call({"query": "相对湿度"}, ctx=_ctx())
    want = await real.call({"query": "相对湿度"}, ctx=_ctx())
    assert got.content == want.content
    assert got.meta == {
        **want.meta,
        "fixture": "demo",
        "fixture_entry": "relative-humidity",
        "fixture_miss": False,
    }


@pytest.mark.asyncio
async def test_a_miss_reads_like_an_empty_real_search(tmp_path: Path) -> None:
    _write(tmp_path)
    fixture = WebSearchTool(client=load_web_search_fixture(str(tmp_path), "demo"))
    real = WebSearchTool(client=RecordingTavilyClient(results=[]))
    got = await fixture.call({"query": "nothing here"}, ctx=_ctx())
    want = await real.call({"query": "nothing here"}, ctx=_ctx())
    assert got.content == want.content == "(no results)"
    assert "fixture" not in got.content
    assert got.meta == {**want.meta, "fixture": "demo", "fixture_entry": None, "fixture_miss": True}


@pytest.mark.asyncio
async def test_real_search_meta_is_unchanged() -> None:
    real = WebSearchTool(client=RecordingTavilyClient(results=[_DEW]))
    got = await real.call({"query": "dew"}, ctx=_ctx())
    assert got.meta == {"truncated": False, "n_results": 1}
