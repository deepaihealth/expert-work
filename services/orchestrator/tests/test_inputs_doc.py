"""B-61 Task 1 —— inputs.json 文档的纯函数。"""

from __future__ import annotations

from uuid import UUID

import pytest

from expert_work.protocol import PromptVariableSpec
from orchestrator.tools.inputs_doc import (
    UrlSite,
    build_inputs_doc,
    inputs_abs_path,
    inputs_rel_path,
    iter_url_sites,
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
