"""Unit tests for the ``save_artifact`` / ``list_artifacts`` tools — Stream J.9."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryArtifactStore
from expert_work.protocol import BuiltinToolSpec
from orchestrator.errors import AgentFactoryError
from orchestrator.tools import (
    ListArtifactsTool,
    SaveArtifactTool,
    ToolBlockedError,
    ToolContext,
    ToolEnv,
    build_tool_registry,
)


def _ctx(
    *, tenant_id: UUID | None = None, user_id: UUID | None = None, agent_key: str = ""
) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id if tenant_id is not None else uuid4(),
        run_id=uuid4(),
        user_id=user_id if user_id is not None else uuid4(),
        agent_key=agent_key,
    )


# ---------------------------------------------------------------------------
# save_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_artifact_records_version_one() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store)
    ctx = _ctx()

    result = await tool.call({"name": "report.md", "kind": "document"}, ctx=ctx)

    assert result.meta == {"artifact": "report.md", "version": 1, "kind": "document"}
    assert "report.md" in result.content
    # B — the result tells the model the user can download it (so it references
    # the artifact by name instead of fabricating a link the UI renders for it).
    assert "download" in result.content.lower()
    artifacts = await store.list_for_user(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key=None
    )
    assert len(artifacts) == 1
    assert artifacts[0].kind == "document"


@pytest.mark.asyncio
async def test_save_artifact_appends_version_on_resave() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store)
    ctx = _ctx()

    await tool.call({"name": "report.md"}, ctx=ctx)
    second = await tool.call({"name": "report.md"}, ctx=ctx)

    assert second.meta["version"] == 2


@pytest.mark.asyncio
async def test_save_artifact_defaults_path_to_name_and_kind_to_other() -> None:
    store = InMemoryArtifactStore()
    result = await SaveArtifactTool(store=store).call({"name": "data.csv"}, ctx=_ctx())
    assert result.meta["kind"] == "other"


@pytest.mark.asyncio
async def test_save_artifact_rejects_unsafe_path() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore())
    with pytest.raises(ValueError, match="relative workspace path"):
        await tool.call({"name": "x", "path": "/etc/passwd"}, ctx=_ctx())
    with pytest.raises(ValueError, match="relative workspace path"):
        await tool.call({"name": "x", "path": "../escape"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_rejects_unknown_kind() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore())
    with pytest.raises(ValueError, match="'kind' must be one of"):
        await tool.call({"name": "x", "kind": "spreadsheet"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_requires_name() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore())
    with pytest.raises(ValueError, match="non-empty 'name'"):
        await tool.call({"name": "  "}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_requires_user_binding() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore())
    with pytest.raises(ToolBlockedError, match="tenant \\+ user binding"):
        await tool.call({"name": "x"}, ctx=ToolContext(tenant_id=uuid4(), user_id=None))


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_artifacts_reports_saved_artifacts() -> None:
    store = InMemoryArtifactStore()
    ctx = _ctx()
    await SaveArtifactTool(store=store).call({"name": "report.md", "kind": "document"}, ctx=ctx)

    result = await ListArtifactsTool(store=store).call({}, ctx=ctx)

    assert result.meta["n_artifacts"] == 1
    assert "report.md" in result.content
    assert "v1" in result.content


@pytest.mark.asyncio
async def test_list_artifacts_empty() -> None:
    result = await ListArtifactsTool(store=InMemoryArtifactStore()).call({}, ctx=_ctx())
    assert result.meta["n_artifacts"] == 0
    assert "no artifacts" in result.content


@pytest.mark.asyncio
async def test_list_artifacts_requires_user_binding() -> None:
    tool = ListArtifactsTool(store=InMemoryArtifactStore())
    with pytest.raises(ToolBlockedError, match="tenant \\+ user binding"):
        await tool.call({}, ctx=ToolContext(tenant_id=uuid4(), user_id=None))


# ---------------------------------------------------------------------------
# assembly — the artifact builtins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_builtins_assembled_when_store_present() -> None:
    env = ToolEnv(artifact_store=InMemoryArtifactStore())
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="save_artifact"), BuiltinToolSpec(name="list_artifacts")],
        tool_env=env,
    )
    assert isinstance(registry.get("save_artifact"), SaveArtifactTool)
    assert isinstance(registry.get("list_artifacts"), ListArtifactsTool)


@pytest.mark.asyncio
async def test_artifact_builtin_missing_store_raises() -> None:
    with pytest.raises(AgentFactoryError, match="artifact store"):
        await build_tool_registry([BuiltinToolSpec(name="save_artifact")], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_save_artifact_feeds_the_manifest_recorder() -> None:
    """产物清单契约 —— 登记成功即喂 ctx.artifact_recorder 一条清单项。"""
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store)
    recorded: list[dict[str, object]] = []
    base = _ctx()
    from dataclasses import replace as _replace

    ctx = _replace(base, artifact_recorder=recorded.append)

    await tool.call({"name": "report.md", "kind": "document"}, ctx=ctx)
    await tool.call({"name": "report.md", "kind": "document"}, ctx=ctx)

    assert [(e["name"], e["kind"], e["version"]) for e in recorded] == [
        ("report.md", "document", 1),
        ("report.md", "document", 2),
    ]
    assert all(isinstance(e["created_at"], str) and e["created_at"] for e in recorded)


@pytest.mark.asyncio
async def test_save_artifact_without_recorder_is_unchanged() -> None:
    """未接线(单测/eval)时零行为变化 —— 登记照常,无清单记录。"""
    result = await SaveArtifactTool(store=InMemoryArtifactStore()).call(
        {"name": "a.md"}, ctx=_ctx()
    )
    assert result.meta == {"artifact": "a.md", "version": 1, "kind": "other"}


# ---------------------------------------------------------------------------
# B-50 —— 产物按 agent 分层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_agents_same_artifact_name_do_not_overwrite() -> None:
    """同一用户下两个 agent 各存一个同名产物 —— 两条独立行,各自 v1。

    这是 B-50 的核心缺陷:旧唯一键 ``(tenant, user, name)`` 让第二次 save 走
    ``ON CONFLICT DO UPDATE``,合并成一行、版本号累加、第一个 agent 的字节被
    第二个覆盖。用户以为有两份报告,实际只剩一份。
    """
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    for agent_key in ("plan-aaaaaaaa", "sop-bbbbbbbb"):
        await SaveArtifactTool(store=store).call(
            {"name": "报告.docx"},
            ctx=_ctx(tenant_id=tenant_id, user_id=user_id, agent_key=agent_key),
        )

    rows = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, agent_key=None)

    assert len(rows) == 2, "两个 agent 的同名产物合并成一行了"
    assert {r.agent_key for r in rows} == {"plan-aaaaaaaa", "sop-bbbbbbbb"}
    assert [r.latest_version for r in rows] == [1, 1], "版本号累加 = 走了合并分支"


@pytest.mark.asyncio
async def test_list_artifacts_only_shows_the_calling_agents_own() -> None:
    """``list_artifacts`` 的描述写着「你存的」—— 现在它说的是实话。

    改之前它返回该用户名下**全部** agent 的产物:喂给模型的事实是错的。
    """
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    for agent_key, name in (("plan-aaaaaaaa", "计划.docx"), ("sop-bbbbbbbb", "评审.docx")):
        await SaveArtifactTool(store=store).call(
            {"name": name}, ctx=_ctx(tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)
        )

    out = await ListArtifactsTool(store=store).call(
        {}, ctx=_ctx(tenant_id=tenant_id, user_id=user_id, agent_key="sop-bbbbbbbb")
    )

    assert "评审.docx" in out.content
    assert "计划.docx" not in out.content, "列到了另一个 agent 的产物"


@pytest.mark.asyncio
async def test_path_in_workspace_carries_the_agent_prefix() -> None:
    """``path_in_workspace`` 是下载时真去读的**物理**路径,必须跟着 ``write_file``
    的落盘位置走。

    PR2 时这条断言是反的(钉「不加前缀」)—— 那是有意的暂缓:文件工具的根目录
    要到 PR3 才改,早一步加前缀就是登记一个没有文件的路径 = 每次下载 404。
    PR3 把根目录改了,前缀在同一个 PR 里补上,两边始终指同一个地方。

    形状是 ``agents/<key>/<path>`` —— **没有** ``artifacts/`` 那一段:
    ``save_artifact`` 不搬字节,没有任何东西往那个目录写(spec §四 09-12 勘误)。
    """
    store = InMemoryArtifactStore()
    ctx = _ctx(agent_key="plan-aaaaaaaa")
    await SaveArtifactTool(store=store).call({"name": "报告.docx"}, ctx=ctx)

    version = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key="plan-aaaaaaaa", name="报告.docx"
    )

    assert version is not None
    assert version.path_in_workspace == "agents/plan-aaaaaaaa/报告.docx"


@pytest.mark.asyncio
async def test_path_in_workspace_stays_flat_without_agent_key() -> None:
    """未绑 agent(空串)保持扁平路径 —— 不能拼出 ``agents//x`` 这种第三形状,
    它既不是旧位置也不是新位置,搬迁脚本两边都认不出来。"""
    store = InMemoryArtifactStore()
    ctx = _ctx(agent_key="")
    await SaveArtifactTool(store=store).call({"name": "报告.docx"}, ctx=ctx)

    version = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key="", name="报告.docx"
    )

    assert version is not None
    assert version.path_in_workspace == "报告.docx"


@pytest.mark.asyncio
async def test_explicit_path_arg_also_gets_the_prefix() -> None:
    """``path`` 显式给了也一样要前缀 —— 它同样是相对 agent 根的。"""
    store = InMemoryArtifactStore()
    ctx = _ctx(agent_key="plan-aaaaaaaa")
    await SaveArtifactTool(store=store).call(
        {"name": "报告.docx", "path": "out/报告.docx"}, ctx=ctx
    )

    version = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key="plan-aaaaaaaa", name="报告.docx"
    )

    assert version is not None
    assert version.path_in_workspace == "agents/plan-aaaaaaaa/out/报告.docx"
