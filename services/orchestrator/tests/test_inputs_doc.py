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
    with_local_path,
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


def test_with_local_path_is_immutable_and_lands_beside_the_url() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("materials")],
        inputs={"materials": [{"description": "视频", "url": "https://example.com/b.mp4"}]},
    )
    assert doc is not None
    site = iter_url_sites(doc)[0]
    updated = with_local_path(doc, site, f"inputs/{RUN}/files/materials.0.mp4")
    assert updated["variables"]["materials"]["value"][0]["local_path"] == (
        f"inputs/{RUN}/files/materials.0.mp4"
    )
    # 原文档没被改(不可变)
    assert "local_path" not in doc["variables"]["materials"]["value"][0]


def test_with_local_path_none_writes_an_explicit_null() -> None:
    doc = build_inputs_doc(
        run_id=RUN, variables=[_var("org_logo")], inputs={"org_logo": "https://example.com/a.jpg"}
    )
    assert doc is not None
    site = iter_url_sites(doc)[0]
    updated = with_local_path(doc, site, None)
    assert updated["variables"]["org_logo"]["local_path"] is None
    # 原文档没被改(不可变,顶层分支)
    assert "local_path" not in doc["variables"]["org_logo"]


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


def test_iter_url_sites_is_stable_across_backfill() -> None:
    """已回填过 local_path 的文档再扫一遍,URL 位置不应变化(``local_path`` 本身
    不是 URL,也不该被当成新的一层容器递归进去)。"""
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
    sites_before = iter_url_sites(doc)

    backfilled = doc
    for site in sites_before:
        backfilled = with_local_path(backfilled, site, f"inputs/{RUN}/files/{site.var_name}")

    assert iter_url_sites(backfilled) == sites_before


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
