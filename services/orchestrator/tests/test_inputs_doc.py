"""B-61 Task 1 —— inputs.json 文档的纯函数。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from expert_work.protocol import PromptVariableSpec
from orchestrator.tools import inputs_doc
from orchestrator.tools.inputs_doc import (
    INPUTS_FILENAME,
    MAX_PARSE_BYTES,
    MAX_PARSE_DEPTH,
    UrlSite,
    build_inputs_doc,
    inputs_abs_dir,
    inputs_abs_path,
    inputs_rel_path,
    iter_url_sites,
    link_names,
    linked_sites,
    parse_json_value,
    root_key,
    slugify,
    url_suffix,
)

RUN = UUID("382f6f5a-55c4-49be-ac05-32fa143f010d")


def _var(name: str, *, trusted: bool = True, required: bool = True) -> PromptVariableSpec:
    return PromptVariableSpec(name=name, trusted=trusted, required=required)


def test_paths_are_run_scoped_and_relative_to_the_exec_view() -> None:
    assert inputs_rel_path(RUN) == f"inputs/{RUN}/inputs.json"
    assert inputs_abs_path(RUN) == f"/workspace/inputs/{RUN}/inputs.json"


def test_every_variable_becomes_an_object_carrying_value_and_trusted() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("project_code"), _var("notes", trusted=False)],
        inputs={"project_code": "PRJ001", "notes": "客户说…"},
    )
    assert doc is not None
    assert doc["run_id"] == str(RUN)
    assert doc["variables"]["project_code"] == {"value": "PRJ001", "trusted": True}
    assert doc["variables"]["notes"] == {"value": "客户说…", "trusted": False}


def test_no_declared_variables_means_no_document() -> None:
    assert build_inputs_doc(run_id=RUN, variables=[], inputs={}) is None


def test_optional_variable_not_supplied_is_absent_not_null() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("a"), _var("b", required=False)],
        inputs={"a": "x"},
    )
    assert doc is not None
    assert "b" not in doc["variables"]


def test_url_sites_are_found_at_the_top_level_and_nested() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("org_logo"), _var("materials")],
        inputs={
            "org_logo": "https://example.com/a.jpg",
            "materials": [
                {"description": "视频", "url": "https://example.com/b.mp4"},
                {"description": "无链接"},
            ],
        },
    )
    assert doc is not None
    sites = iter_url_sites(doc)
    assert sites == [
        UrlSite(var_name="org_logo", path=(), url="https://example.com/a.jpg"),
        UrlSite(var_name="materials", path=(0, "url"), url="https://example.com/b.mp4"),
    ]


def test_non_http_strings_are_not_url_sites() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("a")],
        inputs={"a": "ftp://example.com/x  and  not-a-url"},
    )
    assert doc is not None
    assert iter_url_sites(doc) == []


def test_top_level_url_variable_keeps_value_as_the_url_string() -> None:
    doc = build_inputs_doc(
        run_id=RUN, variables=[_var("org_logo")], inputs={"org_logo": "https://example.com/a.jpg"}
    )
    assert doc is not None
    assert doc["variables"]["org_logo"]["value"] == "https://example.com/a.jpg"


@pytest.mark.parametrize("bad", [{"a": object()}, {"a": {1, 2}}])
def test_non_json_values_are_rejected_loudly(bad: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        build_inputs_doc(run_id=RUN, variables=[_var("a")], inputs=bad)


def test_local_path_key_supplied_by_caller_is_not_a_url_site() -> None:
    """``inputs`` 是租户直接传的 JSON,调用方可能自己就带了一个叫 ``local_path``
    的字段(值是 URL)。这个字段不该被当成预拉目标——只有 ``url`` 才算。"""
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("materials")],
        inputs={
            "materials": [
                {
                    "description": "x",
                    "url": "https://ok/a.mp4",
                    "local_path": "https://attacker/b.mp4",
                }
            ]
        },
    )
    assert doc is not None
    assert iter_url_sites(doc) == [
        UrlSite(var_name="materials", path=(0, "url"), url="https://ok/a.mp4"),
    ]


def test_a_bare_url_inside_a_list_is_not_a_fetchable_site() -> None:
    """终审 finding 1 —— ``{"images": ["https://a"]}`` 里那条 URL 旁边**没有**地方
    记 ``local_path``(要记只能改写字符串自己),所以它不算 site:平台不预拉,模型
    照旧自己下载。沙箱侧在这个位置会 ``cursor["local_path"] = rel`` 打在一个 list
    上直接 ``TypeError``,一条这样的 URL 曾足以让整轮预拉的结果全丢,所以两侧的
    walker 必须同时守这条规则(沙箱侧见
    ``test_prefetch_script.test_site_walk_matches_the_host_side_implementation``)。
    """
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("images"), _var("nested")],
        inputs={
            "images": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
            "nested": [["https://example.com/c.jpg"]],
        },
    )
    assert doc is not None
    assert iter_url_sites(doc) == []


def test_caller_supplied_local_path_is_nulled_in_the_document() -> None:
    """终审 finding 6 —— 两个 walker 都拒绝去**预拉**调用方自带的 ``local_path``,
    但值留在文档里就让工具描述那句「``local_path`` 非空 = 平台已经下载到本地」变成
    谎话(模型会拿着 ``https://attacker/…`` 当本地文件用)。构造文档时就清成 null。
    """
    inputs = {
        "materials": [{"url": "https://ok/a.mp4", "local_path": "https://attacker/b.mp4"}],
        "logo": {"url": "https://ok/c.jpg", "local_path": "../../etc/passwd"},
    }
    doc = build_inputs_doc(run_id=RUN, variables=[_var("materials"), _var("logo")], inputs=inputs)
    assert doc is not None
    assert doc["variables"]["materials"]["value"][0]["local_path"] is None
    assert doc["variables"]["logo"]["value"]["local_path"] is None
    # 键保留(形状不变)、只清值;预拉命中时由沙箱脚本写回真实相对路径。
    assert "local_path" in doc["variables"]["logo"]["value"]
    # 调用方给的那份 inputs 没被改(不可变)。
    assert inputs["logo"]["local_path"] == "../../etc/passwd"


# ---------------------------------------------------------------------------
# B-67 Task 1 —— §4.3 JSON 字符串值 / §4.1 链接命名 / §4.2 目录路径
# ---------------------------------------------------------------------------


def test_json_string_value_is_parsed_into_value_parsed_and_scanned() -> None:
    """`materials` 这类契约传的是 JSON **数组字符串**;`value` 一个字不动(契约不变),
    解析结果写到同级 `value_parsed`,URL 扫描与回填都看它。"""
    raw = '[{"description": "示范视频", "url": "https://x/a.mp4"}]'
    doc = build_inputs_doc(run_id=RUN, variables=[_var("materials")], inputs={"materials": raw})
    assert doc is not None
    entry = doc["variables"]["materials"]
    assert entry["value"] == raw
    assert entry["value_parsed"] == [{"description": "示范视频", "url": "https://x/a.mp4"}]
    assert root_key(entry) == "value_parsed"
    assert iter_url_sites(doc) == [
        UrlSite(var_name="materials", path=(0, "url"), url="https://x/a.mp4")
    ]


@pytest.mark.parametrize("raw", ['"x"', "42", "not json", "[1", "", "  {oops", "null"])
def test_strings_that_are_not_json_containers_get_no_value_parsed(raw: str) -> None:
    doc = build_inputs_doc(run_id=RUN, variables=[_var("a")], inputs={"a": raw})
    assert doc is not None
    assert "value_parsed" not in doc["variables"]["a"]
    assert root_key(doc["variables"]["a"]) == "value"
    assert parse_json_value(raw) is None


def test_a_real_list_is_not_parsed_twice() -> None:
    doc = build_inputs_doc(
        run_id=RUN, variables=[_var("a")], inputs={"a": [{"url": "https://x/1.png"}]}
    )
    assert doc is not None
    assert "value_parsed" not in doc["variables"]["a"]
    assert parse_json_value([1]) is None


def test_oversized_json_string_is_not_parsed() -> None:
    raw = "[" + ",".join(["1"] * 40_000) + "]"
    assert len(raw.encode("utf-8")) > MAX_PARSE_BYTES
    assert parse_json_value(raw) is None


def test_caller_local_path_inside_a_json_string_is_nulled_too() -> None:
    """`value_parsed` 与 `value` 同受 `_null_local_paths` 闸(spec §八)。"""
    raw = '[{"url": "https://ok/a.mp4", "local_path": "https://attacker/b.mp4"}]'
    doc = build_inputs_doc(run_id=RUN, variables=[_var("m")], inputs={"m": raw})
    assert doc is not None
    assert doc["variables"]["m"]["value_parsed"][0]["local_path"] is None
    assert doc["variables"]["m"]["value"] == raw
    assert iter_url_sites(doc) == [UrlSite(var_name="m", path=(0, "url"), url="https://ok/a.mp4")]


def test_link_names_by_shape() -> None:
    """顶层 → `<var><ext>`;dict 字段 → `<var>.<key><ext>`;列表项 → `<var>/<下标>-<slug><ext>`。"""
    assert link_names("org_logo", [((), "https://x/cover-1726394851207.png", None)]) == [
        "org_logo.png"
    ]
    assert link_names("brand", [(("logo",), "https://x/l.jpg", None)]) == ["brand.logo.jpg"]
    assert link_names(
        "materials",
        [
            ((0, "url"), "https://x/a.mp4", "示范视频"),
            ((2, "url"), "https://x/b.pdf", "饮食指南"),
        ],
    ) == ["materials/0-示范视频.mp4", "materials/2-饮食指南.pdf"]
    # dict 里套列表:下标之前的键进名字,下标之后的不进。
    assert link_names("nested", [(("a", 0, "url"), "https://x/n.png", "x")]) == ["nested.a/0-x.png"]


def test_slug_keeps_word_chars_and_drops_everything_else() -> None:
    assert slugify("示范 视频/../x.mp4") == "示范视频xmp4"
    assert slugify("x" * 50) == "x" * 40
    assert slugify(None) == ""
    assert slugify("") == ""
    assert link_names("m", [((0, "url"), "https://x/a.mp4", "!!!")]) == ["m/0.mp4"]


def test_url_without_a_usable_suffix_gets_none() -> None:
    assert url_suffix("https://x/post") == ""
    assert url_suffix("https://x/a.tar.gz") == ".gz"
    assert url_suffix("https://x/a.toolong7") == ""
    assert url_suffix("https://x/a.p%20") == ""
    assert url_suffix("https://x/a.PNG?sig=1") == ".PNG"
    assert link_names("page", [((), "https://x/post", None)]) == ["page"]


@pytest.mark.parametrize(
    "url",
    [
        "https://[oops/logo.png",  # urlparse: Invalid IPv6 URL
        "https://例子／路径＠x/a.png",  # noqa: RUF001 — urlparse: NFKC 校验
        "http://[v1.x/a.jpg",
    ],
)
def test_url_suffix_never_raises_on_malformed_urls(url: str) -> None:
    assert url_suffix(url) == ""


_ODD_PIECES = [
    "[",
    "]",
    "[::1]",
    "／",  # noqa: RUF001
    "＠",  # noqa: RUF001
    "：",  # noqa: RUF001
    "%",
    "%zz",
    " ",
    "\t",
    "\x00",
    "\x7f",
    "\ud800",
    "?",
    "#",
    "@",
    ":",
    "..",
    "/",
    "图片",
    ".png",
    "\\",
    '"',
    "{",
    "}",
]


def _odd_strings() -> list[str]:
    """确定的一小组 + 固定种子的随机组合(括号、全角、%、空白、控制字符、超长)。"""
    fixed = [
        "https://[oops/logo.png",
        "https://h/x.png]",
        "http://[::1]:99999/x",
        "https://[v1.x]/a",
        "https://例子／路径＠x/a.png",  # noqa: RUF001
        "https://h/\ud800.png",
        "https://" + "[" * 5000,
        "https://h/" + "a" * 100_000 + ".png",
        "https://",
        "http://",
    ]
    import random

    rng = random.Random(20260917)  # noqa: S311 — 固定种子造测试数据
    generated = [
        rng.choice(["https://", "http://"])
        + "".join(rng.choice(_ODD_PIECES) for _ in range(rng.randint(1, 12)))
        for _ in range(400)
    ]
    return fixed + generated


def _shapes(url: str) -> list[Any]:
    return [
        url,
        {"logo": url},
        [{"url": url, "description": url}],
        '[{"url": ' + '"' + url.replace("\\", "\\\\").replace('"', '\\"') + '"}]',
        {url: {"url": url}},
    ]


def test_linked_sites_never_raises_on_odd_strings() -> None:
    """C1b:渲染层 / 「本轮输入」段 / 守卫 / 沙箱预拉都经这里,一个畸形值不能让它抛。"""
    from orchestrator.tools import prefetch_script

    for url in _odd_strings():
        for value in _shapes(url):
            sites = linked_sites("v", value)
            assert isinstance(sites, list)
            parsed = parse_json_value(value)
            root = parsed if parsed is not None else value
            sandbox = prefetch_script._linked_sites("v", root)
            assert [s.link for s in sites] == [link for _p, _u, link in sandbox]


def test_colliding_link_names_are_numbered_in_order() -> None:
    sites = [((0, "url"), "https://x/a.png", "封面"), ((0, "thumb"), "https://x/t.png", "封面")]
    assert link_names("m", sites) == ["m/0-封面.png", "m/0-封面-2.png"]


def test_tenant_segments_cannot_escape_the_run_dir() -> None:
    """dict 键与 description 都是租户数据;进文件名前必须净化到只剩 `[\\w-]`。"""
    names = link_names(
        "v",
        [
            (("../../etc", "passwd"), "https://x/p", None),
            ((0, "url"), "https://x/a.png", "../../../root"),
            (("",), "https://x/e.png", None),
        ],
    )
    for name in names:
        assert ".." not in name
        assert not name.startswith("/")
    assert names == ["v.etc.passwd", "v/0-root.png", "v._.png"]


def test_linked_sites_from_a_raw_json_string_use_the_same_names() -> None:
    raw = '[{"description": "示范视频", "url": "https://x/a.mp4"}, {"description": "无链接"}]'
    out = linked_sites("materials", raw)
    assert [(s.site.path, s.site.url, s.link) for s in out] == [
        ((0, "url"), "https://x/a.mp4", "materials/0-示范视频.mp4")
    ]
    assert linked_sites("note", "hi") == []
    assert [s.link for s in linked_sites("org_logo", "https://x/l.png")] == ["org_logo.png"]


def test_inputs_abs_dir_is_the_run_dir_under_the_exec_view() -> None:
    assert inputs_abs_dir(RUN) == f"/workspace/inputs/{RUN}"
    assert inputs_abs_path(RUN).startswith(inputs_abs_dir(RUN) + "/")


# ---------------------------------------------------------------------------
# B-67 PR1 终审 —— 链接名不占平台文件名(F1)/ 顺延不撞名(F5)/ 深嵌套不炸 run(F2)
# ---------------------------------------------------------------------------


def test_link_names_never_take_the_platform_manifest_names() -> None:
    """变量叫 ``inputs`` 是合法的。``inputs`` + ``.json``、``inputs.json`` + ``.tmp`` 恰好是
    run 目录里的清单本身、和沙箱脚本原子改写清单用的临时文件:占了,建链接会删掉清单,或
    ``_rewrite`` 顺着链接把整份清单写进按 agent 共享的缓存条目(跨 run 泄漏)。这两个名字
    预先算「已占用」,撞上就按 ``-2`` 顺延。"""
    assert inputs_doc._RESERVED_NAMES == {INPUTS_FILENAME, f"{INPUTS_FILENAME}.tmp"}
    assert link_names("inputs", [((), "https://x/a.json", None)]) == ["inputs-2.json"]
    assert link_names("inputs", [(("json",), "https://x/b.tmp", None)]) == ["inputs.json-2.tmp"]
    assert [s.link for s in linked_sites("inputs", "https://x/a.json")] == ["inputs-2.json"]


def test_numbering_never_lands_on_a_name_already_taken() -> None:
    """键 ``a`` / ``a!`` / ``a-2``:第二个顺延成 ``v.a-2.png``,第三个自己就叫这个名字 ——
    按「同 stem 计数」顺延会让两个 site 共用一个链接。第四个 ``a?`` 要连跳两次才落到空位。"""
    keys = ["a", "a!", "a-2", "a?"]
    sites = [((key,), f"https://x/{i}.png", None) for i, key in enumerate(keys)]
    assert link_names("v", sites) == ["v.a.png", "v.a-2.png", "v.a-2-2.png", "v.a-3.png"]


def _nested(inner: Any, levels: int) -> Any:
    """把 ``inner`` 再包 ``levels`` 层列表。"""
    for _ in range(levels):
        inner = [inner]
    return inner


@pytest.mark.parametrize("depth", [MAX_PARSE_DEPTH + 1, 1200, 32_000])
def test_a_json_string_nested_too_deep_is_not_parsed(depth: int) -> None:
    """几 KB 的 ``[[[…]]]`` 在 64 KiB 解析上限之内,解析出来的值却让递归的
    ``_null_local_paths`` / ``_walk`` ``RecursionError``(再深些 ``json.loads`` 自己就抛)
    —— 整轮 run 跟着失败。超过 :data:`MAX_PARSE_DEPTH` 层一律不算 JSON 容器。"""
    raw = "[" * depth + "]" * depth
    assert len(raw.encode("utf-8")) <= MAX_PARSE_BYTES
    assert parse_json_value(raw) is None
    doc = build_inputs_doc(run_id=RUN, variables=[_var("a")], inputs={"a": raw})
    assert doc is not None
    assert "value_parsed" not in doc["variables"]["a"]
    assert linked_sites("a", raw) == []


def test_json_string_nesting_up_to_the_bound_is_still_parsed() -> None:
    """边界:恰好 :data:`MAX_PARSE_DEPTH` 层容器(最里层的对象也算一层)照常解析、照常命名。"""
    raw = "[" * (MAX_PARSE_DEPTH - 1) + '{"url": "https://x/a.png"}' + "]" * (MAX_PARSE_DEPTH - 1)
    assert parse_json_value(raw) is not None
    assert [s.link for s in linked_sites("a", raw)] == ["a/0.png"]
    deeper = "[" + raw + "]"
    assert parse_json_value(deeper) is None
    assert linked_sites("a", deeper) == []


def test_a_real_value_nested_too_deep_yields_no_sites() -> None:
    """同一道闸对调用方直接给的 list / dict 也成立:渲染层与守卫会在 run 创建 / 每次
    exec 对**原始值**调 ``linked_sites``,那里不能递归爆栈;清单侧的 ``iter_url_sites``
    与沙箱脚本同样不扫它(两边说的站点必须是同一组)。"""
    shallow = _nested({"url": "https://x/a.png"}, MAX_PARSE_DEPTH - 1)
    deep = [shallow]
    assert [s.link for s in linked_sites("a", shallow)] == ["a/0.png"]
    assert linked_sites("a", deep) == []
    assert linked_sites("a", _nested({"url": "https://x/a.png"}, 1200)) == []
    doc = {
        "variables": {
            "deep": {"value": deep, "trusted": True},
            "very_deep": {"value": _nested([], 1200), "trusted": True},
            "ok": {"value": shallow, "trusted": True},
        }
    }
    assert [site.var_name for site in iter_url_sites(doc)] == ["ok"]
