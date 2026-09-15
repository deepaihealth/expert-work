"""B-61 Task 5 —— arg_bindings 的形状与跨字段校验。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from expert_work.protocol import AgentSpec, MCPToolSpec


def _manifest(
    *,
    variables: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    servers: list[str] | None = None,
    allow_tools: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "apiVersion": "expert-work/v1",
        "kind": "Agent",
        "metadata": {"name": "t", "version": "1.0.0", "tenant": "acme"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "glm", "name": "glm-5.3"},
            "system_prompt": {"template": "x", "jinja": True, "variables": variables},
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
            "tools": [
                {
                    "type": "mcp",
                    "servers": ["deepcare"] if servers is None else servers,
                    "allow_tools": [] if allow_tools is None else allow_tools,
                    "arg_bindings": bindings,
                },
            ],
        },
    }


def test_binding_to_a_declared_variable_is_accepted() -> None:
    spec = AgentSpec.model_validate(
        _manifest(
            variables=[{"name": "project_code"}],
            bindings=[
                {
                    "server": "deepcare",
                    "tool": "customer_search",
                    "args": {"project_code": "project_code"},
                }
            ],
        )
    )
    entry = spec.spec.tools[0]
    assert isinstance(entry, MCPToolSpec)
    assert entry.arg_bindings[0].args == {"project_code": "project_code"}


def test_binding_to_an_undeclared_variable_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a declared prompt variable"):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "project_code"}],
                bindings=[
                    {"server": "deepcare", "tool": "customer_search", "args": {"p": "typo_code"}}
                ],
            )
        )


def test_deleting_a_variable_that_a_binding_uses_is_rejected() -> None:
    """删变量与绑定悬空是同一条校验的两面 —— 删掉 project_code 后就是上一条。"""
    with pytest.raises(ValidationError, match="not a declared prompt variable"):
        AgentSpec.model_validate(
            _manifest(
                variables=[],
                bindings=[
                    {"server": "deepcare", "tool": "customer_search", "args": {"p": "project_code"}}
                ],
            )
        )


def test_two_bindings_for_the_same_server_and_tool_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate arg_bindings"):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[
                    {"server": "deepcare", "tool": "t", "args": {"x": "a"}},
                    {"server": "deepcare", "tool": "t", "args": {"y": "a"}},
                ],
            )
        )


def test_binding_to_a_server_outside_the_entry_allowlist_is_rejected() -> None:
    """服务器名打错 → 绑定谁都匹配不上,参数原样回到模型手里再被抄错一遍。

    和「变量没声明」同类:配置漂移,不是用户在界面上的手误。
    """
    with pytest.raises(ValidationError, match="is not among this mcp entry's servers"):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[{"server": "deepcar", "tool": "t", "args": {"x": "a"}}],
                servers=["deepcare"],
            )
        )


def test_binding_to_a_tool_outside_the_entry_allowlist_is_rejected() -> None:
    with pytest.raises(ValidationError, match="is not among this mcp entry's allow_tools"):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[{"server": "deepcare", "tool": "customer_serch", "args": {"x": "a"}}],
                allow_tools=["customer_search"],
            )
        )


def test_empty_servers_and_allow_tools_mean_all_so_any_binding_is_accepted() -> None:
    spec = AgentSpec.model_validate(
        _manifest(
            variables=[{"name": "a"}],
            bindings=[{"server": "anything", "tool": "whatever", "args": {"x": "a"}}],
            servers=[],
            allow_tools=[],
        )
    )
    entry = spec.spec.tools[0]
    assert isinstance(entry, MCPToolSpec)
    assert entry.arg_bindings[0].server == "anything"


def test_empty_args_is_rejected() -> None:
    # 必须钉住 loc + 原因:裸 ``pytest.raises(ValidationError)`` 在 fixture 将来
    # 任何一个字段变非法时都照样绿,等于验不出 ``args`` 的 min_length。
    with pytest.raises(
        ValidationError,
        match=r"arg_bindings\.0\.args\n\s+Dictionary should have at least 1 item",
    ):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[{"server": "deepcare", "tool": "t", "args": {}}],
            )
        )


def test_error_locates_the_offending_tool_entry_and_binding() -> None:
    """校验器挂在 ``AgentSpecBody`` 上,pydantic 的 loc 只到 ``spec`` —— 手编
    YAML 的人得从消息正文里拿到 ``tools[i].arg_bindings[j]`` 才有地方可去。"""
    doc = _manifest(
        variables=[{"name": "a"}],
        bindings=[
            {"server": "deepcare", "tool": "ok", "args": {"x": "a"}},
            {"server": "deepcare", "tool": "bad", "args": {"y": "nope"}},
        ],
    )
    # 前面再插一个非 mcp 条目,确认下标数的是 ``tools`` 的位置而不是 mcp 的序号。
    doc["spec"]["tools"].insert(0, {"type": "http"})
    with pytest.raises(ValidationError, match=r"spec\.tools\[1\]\.arg_bindings\[1\]"):
        AgentSpec.model_validate(doc)


def test_duplicate_error_points_at_the_first_binding_too() -> None:
    with pytest.raises(
        ValidationError, match=r"already bound at spec\.tools\[0\]\.arg_bindings\[0\]"
    ):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[
                    {"server": "deepcare", "tool": "t", "args": {"x": "a"}},
                    {"server": "deepcare", "tool": "t", "args": {"y": "a"}},
                ],
            )
        )


def test_default_is_empty_so_existing_manifests_are_untouched() -> None:
    doc = _manifest(variables=[], bindings=[])
    # 存量 manifest 根本没有这个 key —— 显式传 [] 验不出「默认空」。
    del doc["spec"]["tools"][0]["arg_bindings"]
    spec = AgentSpec.model_validate(doc)
    entry = spec.spec.tools[0]
    assert isinstance(entry, MCPToolSpec)
    assert entry.arg_bindings == []


# ---------------------------------------------------------------------------
# Task 5b —— 落库形态(空绑定不写出去),spec §七
# ---------------------------------------------------------------------------


def test_an_unconfigured_mcp_entry_does_not_serialize_arg_bindings() -> None:
    """空绑定不落库 —— 旧版本读回来才不会撞 ``extra="forbid"``。

    存库走 ``model_dump()``,**默认值会被物化**:不拦这一下,字段一上线,
    每个带 MCP 的 agent 只要被保存过就带上 ``arg_bindings: []``,与有没有配
    绑定无关,回滚时那些 agent 全起不来(spec §七)。
    """
    doc = _manifest(variables=[], bindings=[])
    # 存量 manifest 根本没有这个 key —— 用真实的存量形状。
    del doc["spec"]["tools"][0]["arg_bindings"]
    spec = AgentSpec.model_validate(doc)
    entry = spec.model_dump(by_alias=True, mode="json")["spec"]["tools"][0]
    assert entry["type"] == "mcp"
    assert "arg_bindings" not in entry
    # 只掉这一个键:同样默认为空的 ``allow_tools`` 必须照旧落库。它要是也没了,
    # 说明改的是全局 ``exclude_defaults`` 口径,那会动到每个字段的落库形态。
    assert entry["allow_tools"] == []


def test_a_configured_binding_still_serializes() -> None:
    spec = AgentSpec.model_validate(
        _manifest(
            variables=[{"name": "a"}],
            bindings=[{"server": "deepcare", "tool": "t", "args": {"p": "a"}}],
        )
    )
    entry = spec.model_dump(by_alias=True, mode="json")["spec"]["tools"][0]
    assert entry["arg_bindings"] == [{"server": "deepcare", "tool": "t", "args": {"p": "a"}}]


def test_a_configured_manifest_round_trips_through_serialization() -> None:
    """存进去再读回来必须是同一份配置 —— serializer 改的只是「空的时候不写」。"""
    spec = AgentSpec.model_validate(
        _manifest(
            variables=[{"name": "a"}],
            bindings=[{"server": "deepcare", "tool": "t", "args": {"p": "a"}}],
        )
    )
    reloaded = AgentSpec.model_validate(spec.model_dump(by_alias=True, mode="json"))
    assert reloaded == spec


def test_input_validation_is_still_strict() -> None:
    """YAML / 接口这条路不受影响 —— 严格该管的是**人写的输入**。"""
    doc = _manifest(variables=[], bindings=[])
    doc["spec"]["tools"][0]["bogus_key"] = 1
    with pytest.raises(ValidationError) as excinfo:
        AgentSpec.model_validate(doc)
    # 钉到具体那一条:裸 ``pytest.raises(ValidationError)`` 在 fixture 将来任何
    # 字段变非法时都照样绿,验不出「多的键被拒了」这件事。
    assert [(e["type"], e["loc"]) for e in excinfo.value.errors()] == [
        ("extra_forbidden", ("spec", "tools", 0, "mcp", "bogus_key"))
    ]
