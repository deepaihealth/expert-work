"""B-61 Task 5b —— ``spec_json`` 的落库形态与读回宽容(spec §七)。

写入严格、读回宽容:YAML / 接口进来的配置照旧 ``extra="forbid"`` 挡错字,
而**我们自己写出去又读回来的** ``spec_json`` 多一个不认识的键时忽略并打日志,
不让整个 agent 起不来。三处读回点(线上行、草稿、历史版本)都走同一个 helper。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from expert_work.persistence import stored_spec as stored_spec_module
from expert_work.persistence.agent_spec.sql import _revision_to_record, _row_to_record
from expert_work.persistence.models import (
    AgentSpecRevisionRow,
    AgentSpecRow,
    PlatformAgentTemplateRow,
)
from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.persistence.platform_agent_template.sql import (
    _row_to_record as _template_row_to_record,
)
from expert_work.persistence.stored_spec import load_stored_spec
from expert_work.protocol import AgentSpec, AgentSpecStatus, MCPToolSpec

_LOGGER_NAME = "expert_work.persistence.stored_spec"

_SHA = "0" * 64

#: 一份**存量**形状的 manifest:带一个 MCP 工具条目,没有 ``arg_bindings`` 键
#: (B-61 之前的库里就长这样)。
_STORED: dict[str, Any] = {
    "apiVersion": "expert-work/v1",
    "kind": "Agent",
    "metadata": {"name": "planner", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "glm", "name": "glm-5.3"},
        "system_prompt": {"template": "x"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
        "tools": [{"type": "mcp", "servers": ["deepcare"]}],
    },
}

#: ``_STORED`` 在 B-61 **之前**(a45dae71,即给 ``MCPToolSpec`` 加 ``arg_bindings``
#: 那个提交的上一个)算出来的指纹,用当时那版协议源码实测得出。
#:
#: 加一个字段不该改存量 manifest 的指纹:``run.agent_spec_sha256`` 与
#: ``agent_spec_revision.spec_sha256`` 的等值 join 是契约(见 ``run_trace.py``),
#: 指纹一变,每个存量 agent 在下次保存之前都静默 join 不上。
_DIGEST_BEFORE_B61 = "c95b3fba67e7c51f06463c8323be09622d4212aff494d9c9f9e3f089b7211f5f"


def _stored() -> dict[str, Any]:
    return deepcopy(_STORED)


def _row(
    *, spec_json: dict[str, Any], draft_spec_json: dict[str, Any] | None = None
) -> AgentSpecRow:
    now = datetime.now(UTC)
    return AgentSpecRow(
        id=uuid4(),
        tenant_id=uuid4(),
        name="planner",
        version="1.0.0",
        spec_json=spec_json,
        spec_sha256=_SHA,
        status=AgentSpecStatus.ACTIVE.value,
        created_by="someone",
        created_at=now,
        updated_at=now,
        draft_spec_json=draft_spec_json,
        draft_sha256=_SHA if draft_spec_json is not None else None,
        draft_updated_at=now if draft_spec_json is not None else None,
        draft_updated_by="someone" if draft_spec_json is not None else None,
    )


def _revision_row(*, spec_json: dict[str, Any]) -> AgentSpecRevisionRow:
    return AgentSpecRevisionRow(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_name="planner",
        agent_version="1.0.0",
        revision=1,
        spec_json=spec_json,
        spec_sha256=_SHA,
        actor_id="someone",
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# 指纹:加字段不能改存量 manifest 的落库内容
# ---------------------------------------------------------------------------


def test_the_spec_digest_of_an_unconfigured_manifest_is_unchanged() -> None:
    assert compute_spec_sha256(AgentSpec.model_validate(_stored())) == _DIGEST_BEFORE_B61


def test_configuring_a_binding_does_change_the_digest() -> None:
    """反面对照:serializer 拿掉的只是「空」这一种情况。少了这条,上一条可能
    只是「指纹对什么都一样」的重言式。两份 manifest 只差一个真绑定。"""
    base = _stored()
    base["spec"]["system_prompt"] = {"template": "x", "jinja": True, "variables": [{"name": "a"}]}
    bound = deepcopy(base)
    bound["spec"]["tools"][0]["arg_bindings"] = [
        {"server": "deepcare", "tool": "t", "args": {"p": "a"}}
    ]
    assert compute_spec_sha256(AgentSpec.model_validate(base)) != compute_spec_sha256(
        AgentSpec.model_validate(bound)
    )


# ---------------------------------------------------------------------------
# 读回宽容
# ---------------------------------------------------------------------------


def test_reading_back_a_row_with_an_unknown_field_ignores_it() -> None:
    """写入严格、读回宽容:自己写出去又读回来的数据,多一个不认识的键不该让
    agent 起不来。"""
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = ["x"]
    loaded = load_stored_spec(row_json)
    assert loaded.spec.model.name == "glm-5.3"
    entry = loaded.spec.tools[0]
    assert isinstance(entry, MCPToolSpec)
    # 剔的只是那个不认识的键,同一层其它内容原样还在。
    assert entry.servers == ["deepcare"]


def test_reading_back_an_intact_row_returns_it_unchanged() -> None:
    loaded = load_stored_spec(_stored())
    assert loaded.metadata.name == "planner"


def test_reading_back_logs_the_key_it_ignored_but_never_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """忽略必须留痕,否则就是把「数据真坏了」也一起吞掉。**只记键名,不记值**
    —— 这个平台上值里出现过客户的真实姓名。"""
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = "Zhang-San-lives-here"
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        load_stored_spec(row_json)
    messages = [r.getMessage() for r in caplog.records]
    assert any("from_a_future_version" in m for m in messages), messages
    assert not any("Zhang-San-lives-here" in m for m in messages), messages


def test_reading_back_does_not_mutate_the_stored_payload() -> None:
    """``row.spec_json`` 是 SQLAlchemy 手里的那个对象 —— 就地改它会把一次「读」
    变成一次隐形的写。"""
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = 1
    before = deepcopy(row_json)
    load_stored_spec(row_json)
    assert row_json == before


def test_reading_back_a_row_broken_for_another_reason_still_raises() -> None:
    """不是「多一个键」的失败一律原样抛 —— 无条件剔键会把真正的数据损坏一起
    吞掉,那比要修的问题更糟。"""
    row_json = _stored()
    del row_json["spec"]["model"]["name"]
    with pytest.raises(ValidationError) as excinfo:
        load_stored_spec(row_json)
    assert [(e["type"], e["loc"]) for e in excinfo.value.errors()] == [
        ("missing", ("spec", "model", "name"))
    ]


def test_a_broken_row_that_also_has_an_unknown_key_propagates_the_original_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """夹带一个不认识的键不代表这行能救:抛出来的必须还是**原来那个**错误
    (两条都在),不是剔完键重试后只剩一半的那个;也不能打「我忽略了什么」的
    日志 —— 什么都没修好。"""
    row_json = _stored()
    del row_json["spec"]["model"]["name"]
    row_json["spec"]["tools"][0]["from_a_future_version"] = 1
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with pytest.raises(ValidationError) as excinfo:
            load_stored_spec(row_json)
    assert {e["type"] for e in excinfo.value.errors()} == {"missing", "extra_forbidden"}
    assert caplog.records == []


def test_a_key_that_cannot_be_located_still_raises_without_a_fake_ignore_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """review M-2 —— ``if not dropped: raise`` 这条护栏没有天然触发路径:pydantic
    的 ``extra_forbidden`` 错误项的 ``loc`` 从不指向一个已经不存在的键或越界下标,
    构造不出真实 payload 去撞它。用 seam(monkeypatch 掉 ``_drop_key_at`` 让它恒
    返回 ``None``,模拟「有 extra_forbidden 错误,但一个键都剔不掉」)直接测
    护栏本身:必须**直接**抛出 ``ValidationError``、且不打「忽略了 0 个键」的假
    日志。光断言 ``pytest.raises(ValidationError)`` 不够 —— 去掉这道护栏后,
    ``cleaned`` 里的键并没有真被剔掉,重试的 ``model_validate`` 一样会因为同一个
    ``extra_forbidden`` 再抛一次同类错误,两条路径都「抛了 ValidationError」;
    唯一能分开两条路径的信号是护栏被跳过时会先打一条虚假的
    ``ignoring 0 unknown key(s)`` warning——这道护栏就是防这条假日志的。"""
    monkeypatch.setattr(stored_spec_module, "_drop_key_at", lambda payload, loc: None)
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = 1
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with pytest.raises(ValidationError):
            load_stored_spec(row_json)
    assert caplog.records == []


def test_input_validation_is_still_strict() -> None:
    """YAML / 接口这条路不受影响 —— 宽容只给我们自己写出去的数据,严格该管的
    是人写的输入。"""
    doc = _stored()
    doc["spec"]["tools"][0]["bogus_key"] = 1
    with pytest.raises(ValidationError) as excinfo:
        AgentSpec.model_validate(doc)
    assert [(e["type"], e["loc"]) for e in excinfo.value.errors()] == [
        ("extra_forbidden", ("spec", "tools", 0, "mcp", "bogus_key"))
    ]


# ---------------------------------------------------------------------------
# 三处读回点都得真的用上它
# ---------------------------------------------------------------------------


def test_the_live_row_read_back_is_lenient() -> None:
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = 1
    record = _row_to_record(_row(spec_json=row_json))
    assert record.spec.metadata.name == "planner"


def test_the_draft_read_back_is_lenient() -> None:
    draft_json = _stored()
    draft_json["spec"]["tools"][0]["from_a_future_version"] = 1
    record = _row_to_record(_row(spec_json=_stored(), draft_spec_json=draft_json))
    assert record.draft is not None
    assert record.draft.spec.metadata.name == "planner"


def test_the_revision_read_back_is_lenient() -> None:
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = 1
    record = _revision_to_record(_revision_row(spec_json=row_json))
    assert record.spec.metadata.name == "planner"


def test_the_platform_template_read_back_is_lenient() -> None:
    """平台模板目录是**第四处**读回点:同样是我们自己写进 JSONB 的 manifest,
    同样按 ``AgentSpec`` 读回来。漏掉它,回滚就沿这条没人记得检查的路径继续坏
    —— 模板目录整个起不来,又回到手改 JSONB。"""
    row_json = _stored()
    row_json["spec"]["tools"][0]["from_a_future_version"] = 1
    now = datetime.now(UTC)
    row = PlatformAgentTemplateRow(
        id=uuid4(),
        tenant_id=None,
        name="planner",
        version="1.0.0",
        spec_json=row_json,
        spec_sha256=_SHA,
        display_name="Planner",
        description="",
        category="general",
        icon=None,
        required_tier="free",
        status="draft",
        enabled=True,
        created_by="someone",
        created_at=now,
        updated_at=now,
    )
    record = _template_row_to_record(row)
    assert record.spec.metadata.name == "planner"
