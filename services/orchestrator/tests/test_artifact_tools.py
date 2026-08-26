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


def _ctx(*, tenant_id: UUID | None = None, user_id: UUID | None = None) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id if tenant_id is not None else uuid4(),
        run_id=uuid4(),
        user_id=user_id if user_id is not None else uuid4(),
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
    artifacts = await store.list_for_user(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
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

    assert [
        (e["name"], e["kind"], e["version"]) for e in recorded
    ] == [("report.md", "document", 1), ("report.md", "document", 2)]
    assert all(isinstance(e["created_at"], str) and e["created_at"] for e in recorded)


@pytest.mark.asyncio
async def test_save_artifact_without_recorder_is_unchanged() -> None:
    """未接线(单测/eval)时零行为变化 —— 登记照常,无清单记录。"""
    result = await SaveArtifactTool(store=InMemoryArtifactStore()).call(
        {"name": "a.md"}, ctx=_ctx()
    )
    assert result.meta == {"artifact": "a.md", "version": 1, "kind": "other"}
