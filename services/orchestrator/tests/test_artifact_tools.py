"""Unit tests for the ``save_artifact`` / ``list_artifacts`` tools — Stream J.9."""

from __future__ import annotations

import json
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
from orchestrator.tools.file_ops import FileOpError
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome


def _ctx(
    *, tenant_id: UUID | None = None, user_id: UUID | None = None, agent_key: str = ""
) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id if tenant_id is not None else uuid4(),
        run_id=uuid4(),
        user_id=user_id if user_id is not None else uuid4(),
        agent_key=agent_key,
    )


def _sandbox(envelope: dict[str, object] | None = None) -> RecordingSandboxRuntime:
    """假沙箱 —— ``save_artifact`` 登记前那次 stat 的回包。

    默认「文件在,10 字节」。传 ``{"ok": False, "error": "not_found"}`` 就是
    「agent 说存了个它从没写出来的文件」,也就是测试环境实测到的那个形态。
    """
    client = RecordingSandboxRuntime()
    client.outcome = SandboxOutcome(
        stdout=json.dumps(envelope if envelope is not None else {"ok": True, "size": 10}),
        stderr="",
        exit_code=0,
        timed_out=False,
    )
    return client


# ---------------------------------------------------------------------------
# save_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_artifact_records_version_one() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store, client=_sandbox())
    ctx = _ctx()

    result = await tool.call({"name": "report.md", "kind": "document"}, ctx=ctx)

    assert result.meta == {
        "artifact": "report.md",
        "version": 1,
        "kind": "document",
    }
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
    tool = SaveArtifactTool(store=store, client=_sandbox())
    ctx = _ctx()

    await tool.call({"name": "report.md"}, ctx=ctx)
    second = await tool.call({"name": "report.md"}, ctx=ctx)

    assert second.meta["version"] == 2


@pytest.mark.asyncio
async def test_save_artifact_defaults_path_to_name_and_kind_to_other() -> None:
    store = InMemoryArtifactStore()
    result = await SaveArtifactTool(store=store, client=_sandbox()).call(
        {"name": "data.csv"}, ctx=_ctx()
    )
    assert result.meta["kind"] == "other"


@pytest.mark.asyncio
async def test_save_artifact_rejects_unsafe_path() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox())
    with pytest.raises(ValueError, match="relative workspace path"):
        await tool.call({"name": "x", "path": "/etc/passwd"}, ctx=_ctx())
    with pytest.raises(ValueError, match="relative workspace path"):
        await tool.call({"name": "x", "path": "../escape"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_rejects_unknown_kind() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox())
    with pytest.raises(ValueError, match="'kind' must be one of"):
        await tool.call({"name": "x", "kind": "spreadsheet"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_requires_name() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox())
    with pytest.raises(ValueError, match="non-empty 'name'"):
        await tool.call({"name": "  "}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_requires_user_binding() -> None:
    tool = SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox())
    with pytest.raises(ToolBlockedError, match="tenant \\+ user binding"):
        await tool.call({"name": "x"}, ctx=ToolContext(tenant_id=uuid4(), user_id=None))


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_artifacts_reports_saved_artifacts() -> None:
    store = InMemoryArtifactStore()
    ctx = _ctx()
    await SaveArtifactTool(store=store, client=_sandbox()).call(
        {"name": "report.md", "kind": "document"}, ctx=ctx
    )

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
    env = ToolEnv(artifact_store=InMemoryArtifactStore(), sandbox_runtime=_sandbox())
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="save_artifact"), BuiltinToolSpec(name="list_artifacts")],
        tool_env=env,
    )
    assert isinstance(registry.get("save_artifact"), SaveArtifactTool)
    assert isinstance(registry.get("list_artifacts"), ListArtifactsTool)


@pytest.mark.asyncio
async def test_save_artifact_builtin_requires_a_sandbox_runtime() -> None:
    """没有沙箱通道就没有「文件」这个概念,登记只会造出下载 404 的死产物。

    和 read_file / write_file 同一个闸,刻意不做成「没通道就跳过校验」——
    那种可选开关正是这条修复要堵的洞。"""
    env = ToolEnv(artifact_store=InMemoryArtifactStore())
    with pytest.raises(AgentFactoryError, match="sandbox runtime"):
        await build_tool_registry([BuiltinToolSpec(name="save_artifact")], tool_env=env)


@pytest.mark.asyncio
async def test_artifact_builtin_missing_store_raises() -> None:
    with pytest.raises(AgentFactoryError, match="artifact store"):
        await build_tool_registry([BuiltinToolSpec(name="save_artifact")], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_save_artifact_feeds_the_manifest_recorder() -> None:
    """产物清单契约 —— 登记成功即喂 ctx.artifact_recorder 一条清单项。"""
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store, client=_sandbox())
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
    result = await SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox()).call(
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
        await SaveArtifactTool(store=store, client=_sandbox()).call(
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
        await SaveArtifactTool(store=store, client=_sandbox()).call(
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
    await SaveArtifactTool(store=store, client=_sandbox()).call({"name": "报告.docx"}, ctx=ctx)

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
    await SaveArtifactTool(store=store, client=_sandbox()).call({"name": "报告.docx"}, ctx=ctx)

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
    await SaveArtifactTool(store=store, client=_sandbox()).call(
        {"name": "报告.docx", "path": "out/报告.docx"}, ctx=ctx
    )

    version = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key="plan-aaaaaaaa", name="报告.docx"
    )

    assert version is not None
    assert version.path_in_workspace == "agents/plan-aaaaaaaa/out/报告.docx"


# ---------------------------------------------------------------------------
# save_artifact 登记前必须确认文件真的在
#
# 实证(测试环境 2026-09-14):thread a7524313 的 `空白hyq_20260914144350.pptx`
# 在库里有 artifact + artifact_version 行、path 指向
# `agents/ai-health-plan-30817804/空白hyq_20260914144350.pptx`,而 NAS 上那个
# 目录里只有同名 .json,pptx 从来没落盘。run 报 success,产物在对话里可见,
# 点下载 404。根因是 save_artifact 从不看那个文件在不在。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_artifact_refuses_when_the_file_was_never_written() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store, client=_sandbox({"ok": False, "error": "not_found"}))
    ctx = _ctx()

    with pytest.raises(FileOpError) as excinfo:
        await tool.call({"name": "deck.pptx"}, ctx=ctx)

    # 错误话术要让模型知道「是你没写出来」,而不是「平台坏了」
    assert "deck.pptx" in str(excinfo.value)
    assert "nothing was registered" in str(excinfo.value)
    # 关键:一行都不许落库 —— 落了就又是一条列表可见、下载 404 的死产物
    assert (
        await store.list_for_user(tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key=None)
        == []
    )


@pytest.mark.asyncio
async def test_save_artifact_refuses_a_directory() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store, client=_sandbox({"ok": False, "error": "not_a_file"}))
    ctx = _ctx()

    with pytest.raises(FileOpError):
        await tool.call({"name": "outputs"}, ctx=ctx)

    assert (
        await store.list_for_user(tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key=None)
        == []
    )


@pytest.mark.asyncio
async def test_save_artifact_blocks_a_path_escaping_the_workspace() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(
        store=store, client=_sandbox({"ok": False, "error": "path_escapes_workspace"})
    )

    with pytest.raises(ToolBlockedError):
        await tool.call({"name": "x", "path": "ok.txt"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_save_artifact_stats_under_the_agent_scope_root() -> None:
    """stat 用的根必须与写文件时同一个,否则「写的路径」和「登记的路径」会分家。"""
    client = _sandbox()
    await SaveArtifactTool(store=InMemoryArtifactStore(), client=client).call(
        {"name": "deck.pptx"}, ctx=_ctx(agent_key="ai-health-plan-30817804")
    )

    code = client.execs[-1][1]
    # 带上尾随逗号 —— ``"ws": "/workspace"`` 本身是 ``"ws": "/workspace/agents/…"``
    # 的子串,不钉逗号的话「stat 用视图根而不是 agent 专属路径」这条会恒真
    # (同 test_file_ops.py 的 _VIEW_WS 一样的坑)。
    assert '"ws": "/workspace",' in code
    assert "deck.pptx" in code


@pytest.mark.asyncio
async def test_save_artifact_stats_the_explicit_path_not_the_name() -> None:
    """传了 path 就该 stat path —— 名字只是逻辑名,可以跟文件名不同。"""
    client = _sandbox()
    await SaveArtifactTool(store=InMemoryArtifactStore(), client=client).call(
        {"name": "第十天食谱", "path": "out/day10.pptx"}, ctx=_ctx()
    )

    code = client.execs[-1][1]
    assert "out/day10.pptx" in code


# ---------------------------------------------------------------------------
# B-60 —— 拆 #1551 的认领分支:exec 已经写不到用户根,「认领」是带着洞形状的死代码
# (spec §4.8)。save_artifact 现在只 stat 视图,不搬文件、结果也不再带 location。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_artifact_refuses_the_reserved_shared_segment() -> None:
    with pytest.raises(ValueError, match="reserved"):
        await SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox()).call(
            {"name": "x", "path": "shared/x.md"}, ctx=_ctx(agent_key="me-aaaaaaaa")
        )


@pytest.mark.asyncio
async def test_save_artifact_result_has_no_claim_note_or_location() -> None:
    result = await SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox()).call(
        {"name": "a.md"}, ctx=_ctx(agent_key="me-aaaaaaaa")
    )
    assert "moved into your agent workspace" not in result.content
    assert "location" not in result.meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "expected_rel"),
    [
        ("/workspace/deck.pptx", "deck.pptx"),
        ("/workspace/agents/me-aaaaaaaa/deck.pptx", "deck.pptx"),
        ("/workspace/out/deck.pptx", "out/deck.pptx"),
    ],
)
async def test_save_artifact_folds_absolute_paths_like_the_file_tools(
    given: str, expected_rel: str
) -> None:
    """模型照着工具描述回传绝对路径 —— 与 file_ops._require_path 同一条折叠规则。"""
    store = InMemoryArtifactStore()
    client = _sandbox()
    ctx = _ctx(agent_key="me-aaaaaaaa")

    await SaveArtifactTool(store=store, client=client).call(
        {"name": "deck.pptx", "path": given}, ctx=ctx
    )

    assert f'"rel": {json.dumps(expected_rel)}' in client.execs[-1][1]
    latest = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key=ctx.agent_key, name="deck.pptx"
    )
    assert latest is not None
    assert latest.path_in_workspace == f"agents/me-aaaaaaaa/{expected_rel}"


@pytest.mark.asyncio
async def test_save_artifact_rejects_another_agents_tree() -> None:
    with pytest.raises(ValueError, match="reserved agents/ tree"):
        await SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox()).call(
            {"name": "x", "path": "agents/someone-else-bbbbbbbb/x.md"},
            ctx=_ctx(agent_key="me-aaaaaaaa"),
        )
