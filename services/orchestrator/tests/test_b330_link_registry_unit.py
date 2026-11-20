"""B-330 —— 链接登记的纯函数:登记、平台段、检查换回、结果遮回、守卫提示。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote
from uuid import UUID

import pytest

from orchestrator.graph_builder.input_literal_guard import (
    LiteralHit,
    find_literal,
    literal_guard_message,
)
from orchestrator.tools.link_registry import (
    MAX_LINKS,
    NOTE_RECENT,
    LinkRefusal,
    check_call,
    check_link_calls,
    link_table,
    links_note,
    mask_links,
    refusal_message,
    register_turn,
    rewrite_link_ids,
)

CONV = UUID("3f9a1c2e-0000-4000-8000-000000000001")
OTHER_CONV = UUID("0badc0de-0000-4000-8000-000000000002")
NOW = datetime(2026, 11, 20, 6, 5, tzinfo=UTC)
FORM = "mcp__shenhu__form_save_customer_values"
URL_1 = "https://oss.example.invalid/a/report-1.PDF?Expires=1&Signature=ab%2Fc"
URL_2 = "https://oss.example.invalid/a/scan-2.jpg?Signature=zz"
ID_1 = "expert_work://link/3f9a1c2e-1"
ID_2 = "expert_work://link/3f9a1c2e-2"


def _register(
    inputs: dict[str, Any], existing: list[dict[str, Any]] | None = None, **kw: Any
) -> Any:
    return register_turn(
        existing or [],
        inputs=inputs,
        trusted_names=kw.pop("trusted", {"attachment_urls", "materials"}),
        conversation_id=kw.pop("conversation", CONV),
        run_id=None,
        now=NOW,
        **kw,
    )


def _links(*urls: str) -> list[dict[str, Any]]:
    outcome = _register({"attachment_urls": list(urls)})
    assert outcome.links is not None
    return list(outcome.links)


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------


def test_json_array_string_list_and_nested_values_are_all_found() -> None:
    outcome = _register(
        {
            "attachment_urls": json.dumps([URL_1]),
            "materials": [
                {"description": "示范", "url": URL_2, "local_path": "https://x.invalid/p"}
            ],
            "plain": "不是链接",
            "logo": "https://oss.example.invalid/logo.png 公司 LOGO",
        },
        trusted={"attachment_urls", "materials"},
        turn_documents=["uploads/attachment-9.pdf"],
        turn_image_count=2,
    )
    assert outcome.links is not None
    rows = [(e["id"], e["url"], e["variable"], e["where"], e["ext"]) for e in outcome.links]
    assert rows == [
        (ID_1, URL_1, "attachment_urls", "第 1 项", "pdf"),
        (ID_2, URL_2, "materials", "第 1 项里的字段", "jpg"),
        # untrusted(没在 trusted 里)不记扩展名;只取第一段
        (
            "expert_work://link/3f9a1c2e-3",
            "https://oss.example.invalid/logo.png",
            "logo",
            "整个值",
            None,
        ),
    ]
    assert outcome.links[0]["turn_files"] == ["uploads/attachment-9.pdf"]
    assert outcome.links[0]["turn_images"] == 2


def test_dedupe_keeps_the_first_record_and_numbers_continue() -> None:
    first = _links(URL_1)
    again = _register({"note": URL_1, "other": [URL_2]}, existing=first)
    assert again.links is not None
    assert [(e["id"], e["url"], e["variable"]) for e in again.links] == [
        (ID_1, URL_1, "attachment_urls"),
        (ID_2, URL_2, "other"),
    ]
    assert _register({"note": URL_1}, existing=first).links is None


def test_cap_and_depth_never_fail_the_turn() -> None:
    """T17 —— 第 201 条不登记;递归太深的变量跳过。"""
    existing = _register({"u": [f"https://x.invalid/{i}" for i in range(64)]}).links or []
    while len(existing) < MAX_LINKS:
        batch = [f"https://x.invalid/{len(existing)}-{i}" for i in range(64)]
        existing = _register({"u": batch}, existing=existing).links or existing
    assert len(existing) == MAX_LINKS
    full = _register({"u": ["https://x.invalid/new"]}, existing=existing)
    assert full.links is None and full.dropped_cap == 1
    note = links_note(existing, has_mcp=True)
    assert note is not None and f"登记已满 {MAX_LINKS} 条" in note

    deep: Any = URL_1
    for _ in range(100):
        deep = [deep]
    outcome = _register({"deep": deep, "ok": URL_2})
    assert outcome.dropped_depth == 1
    assert outcome.links is not None and [e["url"] for e in outcome.links] == [URL_2]


def test_at_most_64_new_links_per_turn() -> None:
    outcome = _register({"u": [f"https://x.invalid/{i}" for i in range(70)]})
    assert outcome.added == 64 and outcome.dropped_cap == 6


# ---------------------------------------------------------------------------
# T10 —— 平台段
# ---------------------------------------------------------------------------


def test_note_is_byte_stable_and_lists_only_the_latest_ten() -> None:
    links = _register({"u": [f"https://x.invalid/f{i}.pdf" for i in range(12)]}).links or []
    first = links_note(links, has_mcp=True)
    assert first == links_note([dict(e) for e in links], has_mcp=True)
    assert first is not None
    lines = [line for line in first.splitlines() if line.startswith("- ")]
    assert len(lines) == NOTE_RECENT
    assert lines[0].startswith("- expert_work://link/3f9a1c2e-3：")  # noqa: RUF001
    assert "另有 2 条更早的链接，编号仍然有效。" in first  # noqa: RUF001
    assert "https://" not in first
    # 北京时间
    assert "11-20 14:05 收到" in first
    assert links_note([], has_mcp=True) is None
    assert links_note(links, has_mcp=False) is None


def test_untrusted_lines_have_no_extension() -> None:
    links = _register({"note_url": URL_1}, trusted=set()).links or []
    note = links_note(links, has_mcp=True)
    assert note is not None and ".pdf" not in note.lower()


# ---------------------------------------------------------------------------
# T2 / T3 / T11 / T12 —— 检查与换回
# ---------------------------------------------------------------------------


def test_rewrite_walks_every_leaf_and_one_json_layer_without_mutating_input() -> None:
    table = link_table(_links(URL_1, URL_2))
    args = {
        "fields": [{"field_code": "a", "value": f"  {ID_1} "}, {"nested": [{"v": ID_2}]}],
        "payload": json.dumps({"files": [ID_1, ID_2]}),
        "note": "普通文字",
    }
    snapshot = json.dumps(args)
    new_args, used = rewrite_link_ids(FORM, args, table)
    assert json.dumps(args) == snapshot
    assert new_args["fields"] == [{"field_code": "a", "value": URL_1}, {"nested": [{"v": URL_2}]}]
    assert json.loads(new_args["payload"]) == {"files": [URL_1, URL_2]}
    assert used == (ID_1, ID_2, ID_1, ID_2)


def test_bound_params_are_skipped() -> None:
    table = link_table(_links(URL_1))
    new_args, used = rewrite_link_ids(FORM, {"attachments": ID_1}, table, skip={"attachments"})
    assert new_args == {"attachments": ID_1} and used == ()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("expert_work://link/3f9a1c2e-9", "unknown"),
        ("expert_work://link/0badc0de-1", "foreign"),
        (f"{ID_1};{ID_2}", "embedded"),
        (f"附件见 {ID_1}", "embedded"),
        (json.dumps([f"{ID_1} 和 {ID_2}"]), "embedded"),
    ],
)
def test_refusal_reasons(value: str, reason: str) -> None:
    links = _links(URL_1, URL_2)
    refusal = check_call(FORM, {"fields": [{"value": value}]}, link_table(links))
    assert refusal is not None and refusal.reason == reason and refusal.arg == "fields"
    text = refusal_message(refusal, links)
    assert "https://" not in text
    if reason in ("unknown", "foreign"):
        assert ID_1 in text and ID_2 in text
    if reason == "embedded":
        assert "请写成数组" in text


def test_ids_in_dict_keys_are_rewritten_or_refused_like_any_leaf() -> None:
    """审查 LOW 1:字典的键也是字符串叶子。"""
    table = link_table(_links(URL_1))
    new_args, used = rewrite_link_ids(FORM, {"files": {ID_1: "报告"}}, table)
    assert new_args == {"files": {URL_1: "报告"}} and used == (ID_1,)
    refusal = check_call(FORM, {"files": {"expert_work://link/0badc0de-1": "x"}}, table)
    assert refusal is not None and refusal.reason == "foreign" and refusal.arg == "files"
    http = check_call("http", {"headers": {ID_1: "1"}}, table)
    assert http is not None and http.reason == "http"


def test_next_number_follows_the_largest_existing_one() -> None:
    """审查 LOW 2:清洗掉的坏条目不会让新编号和还在的编号撞上。"""
    existing = [
        {"id": ID_1, "url": URL_1},
        {"id": None, "url": "https://bad.invalid/x"},  # 坏条目,清洗时丢掉
        {"id": "expert_work://link/3f9a1c2e-3", "url": "https://oss.example.invalid/c.pdf"},
    ]
    from orchestrator.tools.link_registry import valid_links

    outcome = _register({"u": URL_2}, existing=list(valid_links(existing)))
    assert outcome.links is not None
    assert outcome.links[-1]["id"] == "expert_work://link/3f9a1c2e-4"


def test_a_check_that_crashes_refuses_with_a_readable_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查 LOW 4:检查本身出异常时按拒绝处理,提示里不写一个空参数名。"""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("orchestrator.tools.link_registry.check_call", _boom)
    refusals = check_link_calls([{"name": FORM, "args": {"v": ID_1}}], _links(URL_1), {})
    refusal = refusals[0]
    assert isinstance(refusal, LinkRefusal)
    text = refusal_message(refusal, _links(URL_1))
    assert "参数  " not in text and "出错" in text
    assert check_link_calls([{"name": FORM, "args": {"v": "plain"}}], _links(URL_1), {}) == {}


def test_empty_registry_http_tool_and_platform_tools() -> None:
    empty = check_call(FORM, {"v": ID_1}, {})
    assert empty is not None and empty.reason == "empty"
    table = link_table(_links(URL_1))
    http = check_call("http", {"body": {"x": [ID_1]}}, table)
    assert http is not None and http.reason == "http"
    assert check_call("write_file", {"content": ID_1}, table) is None
    assert check_call("spawn_worker", {"task": f"用 {ID_1}"}, table) is None
    assert check_call(FORM, {"v": "https://other.invalid/x"}, table) is None


# ---------------------------------------------------------------------------
# 结果遮回
# ---------------------------------------------------------------------------


URL_CN = "https://oss.example.invalid/客户/报告-1.pdf?a=1&Signature=Ab%2bc%3D(x)!"


def test_mask_replaces_every_form_longest_first() -> None:
    longer = URL_1 + "&extra=1"
    links = _links(URL_1, longer, URL_CN)
    id_cn = "expert_work://link/3f9a1c2e-3"
    forms = [
        URL_1,
        json.dumps(URL_1)[1:-1].replace("/", "\\/"),
        quote(URL_1, safe=""),
        quote(URL_1),
        URL_1.replace("&", "&amp;"),
        URL_1.replace("&", "\\u0026"),  # Go encoding/json
        URL_1.replace("/", "\\/").replace("&", "&amp;"),
        unquote(URL_1),  # 服务端把百分号解码后回显
        URL_1.replace("%2F", "%2f"),
        URL_1 + ".",  # 句末标点不挡
        f"({URL_1})",
        longer,
    ]
    lines = mask_links("\n".join(forms), links).splitlines()
    assert lines[:-3] == [ID_1] * 9
    assert lines[-3:] == [f"{ID_1}.", f"({ID_1})", ID_2]
    # 非 ASCII 路径:ensure_ascii 的 ``\u`` 写法、大小写混用的百分号、encodeURIComponent
    cn_forms = [
        json.dumps(URL_CN, ensure_ascii=True)[1:-1],
        quote(URL_CN, safe="").replace("%2B", "%2b"),
        quote(URL_CN, safe="!'()*~"),
        URL_CN.replace("%2b", "%2B"),
    ]
    assert mask_links("\n".join(cn_forms), links).splitlines() == [id_cn] * 4
    assert mask_links("nothing here", links) == "nothing here"
    assert mask_links("x", []) == "x"


def test_encoded_forms_end_at_an_outer_delimiter() -> None:
    """复审:百分号编码的链接后面跟 ``&`` / ``#`` 是外层分隔符,要遮;``%26`` 这类还算链接的一部分。"""
    links = _links(URL_1)
    enc = quote(URL_1, safe="")
    component = quote(URL_1, safe="!'()*~")
    text = "\n".join(
        [
            f"?src={enc}&page=2",
            f"?src={enc}#p2",
            f"?src={component}&page=2",
            f"?src={quote(URL_1)}&page=2",
            f"?src={quote(URL_1)}/next",  # quote(url) 保留 ``/``:后面接 ``/`` 还在链接里
            f"[预览](x?src={enc})",
            f"<a href='x?src={enc}'>",
            f"?src={enc}!",
            f"…{enc}. 下一句",
            f"…{enc}.pdf",  # ``.`` 后接字母数字:还在链接里,不遮
            f"?src={enc}%26page=2",  # 已知认不出:编码后的分隔符,和更长的链接分不开
        ]
    )
    lines = mask_links(text, links).splitlines()
    assert lines[:4] == [f"?src={ID_1}&page=2", f"?src={ID_1}#p2"] * 1 + [f"?src={ID_1}&page=2"] * 2
    assert lines[4] == f"?src={quote(URL_1)}/next"
    assert lines[5:10] == [
        f"[预览](x?src={ID_1})",
        f"<a href='x?src={ID_1}'>",
        f"?src={ID_1}!",
        f"…{ID_1}. 下一句",
        f"…{enc}.pdf",
    ]
    assert lines[10] == f"?src={enc}%26page=2"
    # 原样写法后面跟 ``&``:和一条更长的链接分不开,不遮(已知限制)
    raw = f"?src={URL_1}&page=2"
    assert mask_links(raw, links) == raw


def test_mask_leaves_unrelated_and_longer_urls_alone() -> None:
    """审查 M1:登记里的链接是别的链接的前缀时,不许把那条更长的链接遮坏。"""
    links = _register(
        {
            "site": "https://",  # 没有主机名:不登记
            "home": "https://shenhu.example.invalid",
            "doc": "https://oss.example.invalid/a.pdf",
        }
    ).links
    assert links is not None
    assert [e["url"] for e in links] == [
        "https://shenhu.example.invalid",
        "https://oss.example.invalid/a.pdf",
    ]
    text = json.dumps(
        [
            "https://example.invalid/doc",
            "https://shenhu.example.invalid/docs/b.pdf",
            "https://oss.example.invalid/a.pdf.bak",
            "https://oss.example.invalid/a.pdf?v=2",
            "https:\\/\\/oss.example.invalid\\/a.pdf\\/x",
        ]
    )
    assert mask_links(text, links) == text
    escaped = r'{"u": "https:\/\/oss.example.invalid\/a.pdf\/x", "v": "https://oss.example.invalid/a.pdf&b=1"}'
    assert mask_links(escaped, links) == escaped
    home_id, doc_id = links[0]["id"], links[1]["id"]
    assert mask_links("见 https://shenhu.example.invalid 。", links) == f"见 {home_id} 。"
    assert mask_links('{"u": "https://oss.example.invalid/a.pdf"}', links) == f'{{"u": "{doc_id}"}}'


def test_mask_stays_cheap_with_a_full_registry() -> None:
    """审查 M2:登记满 200 条(同一台主机)、结果 100 KB,遮回要快、内存要小(宽松上限)。"""
    import time
    import tracemalloc

    urls = [
        f"https://oss.example.invalid/t/{i:03d}/report-{i}.pdf?Signature=s{i}%2Fx"
        for i in range(64)
    ]
    links = _register({"u": urls}).links or []
    while len(links) < MAX_LINKS:
        batch = [f"https://oss.example.invalid/t/{len(links)}-{i}.jpg?Sig={i}" for i in range(64)]
        links = _register({"u": batch}, existing=links).links or links
    assert len(links) == MAX_LINKS
    body = ("x" * 1000 + " ") * 100 + " ".join(urls[:5]) + " " + quote(urls[7], safe="")
    tracemalloc.start()
    started = time.perf_counter()
    masked = mask_links(body, links)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert "oss.example.invalid" not in masked
    assert elapsed < 2.0, elapsed
    assert peak < 32 * 1024 * 1024, peak


# ---------------------------------------------------------------------------
# T15 —— 字面量守卫的提示
# ---------------------------------------------------------------------------


def test_literal_guard_message_gives_ids_only_when_the_variable_has_links() -> None:
    var_hit = LiteralHit(FORM, "fields", "variable", "attachment_urls", True)
    path_hit = find_literal(
        FORM,
        {"pics": ["inputs/current/attachment_urls/0-report.pdf"]},
        names={"attachment_urls"},
    )
    assert path_hit is not None and path_hit.kind == "local_path"
    by_var = {"attachment_urls": [ID_1]}
    for hit in (var_hit, path_hit):
        assert ID_1 in literal_guard_message(hit, link_ids=by_var)
        plain = literal_guard_message(hit)
        assert ID_1 not in plain
        assert plain == literal_guard_message(hit, link_ids={"other": [ID_2]})
    assert "原始链接" in literal_guard_message(path_hit)
    named = find_literal(
        FORM, {"pic": "inputs/current/attachment_urls.pdf"}, names={"attachment_urls"}
    )
    assert named is not None and ID_1 in literal_guard_message(named, link_ids=by_var)


async def test_mcp_tool_masks_before_trimming_and_keeps_the_full_copy_masked() -> None:
    """溢出写进工作区的那份全文(``full_content``)也是遮过的。"""
    from orchestrator.tools.mcp import MCPTool, MCPToolDef, RecordingMCPClient
    from orchestrator.tools.registry import ToolContext

    links = _links(URL_1)
    body = "head " + URL_1 + " " + "x" * 400 + " " + quote(URL_1, safe="")
    tool = MCPTool(
        client=RecordingMCPClient(responses={"t": body}),
        tool_def=MCPToolDef(name="t", description="d", input_schema={}),
        server_name="s",
        content_char_cap=100,
    )
    result = await tool.call({}, ctx=ToolContext(conversation_links=tuple(links)))
    assert result.full_content is not None
    for text in (result.content, result.full_content):
        assert "oss.example.invalid" not in text and "oss.example.invalid" not in unquote(text)
    assert result.full_content.count(ID_1) == 2
