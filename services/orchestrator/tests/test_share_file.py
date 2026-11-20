"""B-326 —— ``share_file``:把本会话登记的文件标记为交给用户。

按本 agent、本用户、**本会话**找产物(默认本会话里最新的一版),铸(或复用)一条钉死在那一版上
的链接,记到这一轮 run 的产物清单那一项上(``share_url`` / ``share_expires_at``)。给模型的
工具结果只有确认文字:不含地址、不含链接 id。别的用户 / agent / 租户 / 会话的产物一律「没有」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryArtifactStore
from expert_work.persistence.artifact_share import InMemoryArtifactShareLinkStore
from expert_work.protocol import ShareLinksSpec
from orchestrator.errors import AgentFactoryError
from orchestrator.tools import ToolBlockedError, ToolContext, ToolEnv, build_tool_registry
from orchestrator.tools.artifact import ArtifactRecorder
from orchestrator.tools.file_ops import FileOpError
from orchestrator.tools.share_file import (
    SHARE_FILE_TOOL_NAME,
    ShareFileTool,
    ShareLinkService,
    normalize_public_base_url,
)

_BASE = "https://files.example.test"
_AGENT = "report-agent"
#: The conversation the tool runs in (B-236 per-conversation workspace).
_THREAD = UUID("11111111-1111-4111-8111-111111111111")


def _service() -> ShareLinkService:
    return ShareLinkService(
        links=InMemoryArtifactShareLinkStore(),
        artifacts=InMemoryArtifactStore(),
        public_base_url=_BASE,
    )


async def _save(
    service: ShareLinkService,
    *,
    tenant: UUID,
    user: UUID,
    name: str = "report.pdf",
    agent_key: str = _AGENT,
    snapshot: bool = True,
    thread: UUID | None = _THREAD,
) -> Any:
    """Register a version the way ``save_artifact`` does from conversation ``thread``
    (``None`` = a file at the agent root, outside any conversation directory)."""
    version_id = uuid4()
    where = f"threads/{thread}/work/{name}" if thread is not None else name
    return await service.artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        agent_key=agent_key,
        name=name,
        kind="document",
        path_in_workspace=f"agents/{agent_key}/{where}",
        created_in_thread="t",
        version_id=version_id,
        snapshot_key=str(version_id) if snapshot else None,
        size_bytes=3 if snapshot else None,
        sha256="0" * 64 if snapshot else None,
    )


def _ctx(tenant: UUID, user: UUID, **overrides: Any) -> ToolContext:
    fields: dict[str, Any] = {
        "tenant_id": tenant,
        "user_id": user,
        "run_id": uuid4(),
        "thread_id": _THREAD,
        "agent_key": _AGENT,
        "workspace_layout": "conversation",
        "artifact_recorder": ArtifactRecorder(),
    }
    fields.update(overrides)
    return ToolContext(**fields)


def _entry(ctx: ToolContext) -> dict[str, Any]:
    """The run's artifact-list entry the call marked."""
    recorder = ctx.artifact_recorder
    assert isinstance(recorder, ArtifactRecorder)
    [entry] = recorder.snapshot()
    return entry


async def _only_link(service: ShareLinkService, ctx: ToolContext) -> Any:
    """The link row behind the marked entry's ``share_url`` (``…/files/<id>/<name>``)."""
    link_id = str(_entry(ctx)["share_url"]).split("/")[-2]
    link = await service.links.get_public(link_id=link_id)
    assert link is not None
    return link


# ----------------------------------------------------------------- the tool


@pytest.mark.asyncio
async def test_marks_the_latest_and_tells_the_model_nothing_secret() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user)
    v2 = await _save(service, tenant=tenant, user=user)
    ctx = _ctx(tenant, user)
    before = datetime.now(UTC)
    result = await ShareFileTool(service=service, ttl_minutes=30).call(
        {"name": "report.pdf"}, ctx=ctx
    )
    link = await _only_link(service, ctx)
    # The model sees plain confirmation text: no URL, no link id (the credential).
    for seen in (result.content, repr(result.meta)):
        assert _BASE not in seen
        assert "http" not in seen
        assert "/v1/public/files/" not in seen
        assert link.id not in seen
    assert "do not write a link" in result.content.lower()
    # The run's artifact list carries the delivery fields.
    entry = _entry(ctx)
    assert entry == {
        "name": "report.pdf",
        "kind": "document",
        "version": 2,
        "created_at": entry["created_at"],
        "share_url": f"{_BASE}/v1/public/files/{link.id}/report.pdf",
        "share_expires_at": link.expires_at.isoformat(),
        # Internal: what was marked (the run record keeps it; nothing outward shows it).
        "_share": {
            "artifact_id": str(v2.artifact_id),
            "version_id": str(v2.id),
            "agent_key": _AGENT,
        },
    }
    assert link.artifact_version_id == v2.id
    assert link.version == 2
    assert link.file_name == "report.pdf"  # the cosmetic last URL segment
    assert link.agent_key == _AGENT
    assert link.run_id == ctx.run_id
    assert link.thread_id == ctx.thread_id
    assert before + timedelta(minutes=30) <= link.expires_at
    assert link.expires_at <= datetime.now(UTC) + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_marking_again_reuses_the_live_link() -> None:
    """At most one live link per (version, conversation)."""
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user)
    tool = ShareFileTool(service=service, ttl_minutes=30)
    first, second = _ctx(tenant, user), _ctx(tenant, user)
    await tool.call({"name": "report.pdf"}, ctx=first)
    await tool.call({"name": "report.pdf"}, ctx=second)
    assert _entry(first)["share_url"] == _entry(second)["share_url"]


@pytest.mark.asyncio
async def test_share_pins_the_requested_version() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    v1 = await _save(service, tenant=tenant, user=user)
    await _save(service, tenant=tenant, user=user)
    ctx = _ctx(tenant, user)
    await ShareFileTool(service=service, ttl_minutes=120).call(
        {"name": "report.pdf", "version": 1}, ctx=ctx
    )
    link = await _only_link(service, ctx)
    assert link.artifact_version_id == v1.id
    assert link.version == 1
    assert _entry(ctx)["version"] == 1


_OTHER_THREAD = UUID("22222222-2222-4222-8222-222222222222")


@pytest.mark.asyncio
async def test_never_shares_another_conversations_file() -> None:
    """C1 —— one platform user serving many end customers: customer B's same-named
    report (saved later, so it is the user's "latest") must never be shared from
    customer A's conversation — not as the latest, and not by explicit version."""
    service = _service()
    tenant, user = uuid4(), uuid4()
    mine = await _save(service, tenant=tenant, user=user, thread=_THREAD)
    theirs = await _save(service, tenant=tenant, user=user, thread=_OTHER_THREAD)
    assert theirs.version == 2  # B's is the user's latest version of the name
    tool = ShareFileTool(service=service, ttl_minutes=120)

    in_a = _ctx(tenant, user)
    await tool.call({"name": "report.pdf"}, ctx=in_a)
    link = await _only_link(service, in_a)
    assert link.artifact_version_id == mine.id

    with pytest.raises(FileOpError, match="not saved in this conversation"):
        await tool.call({"name": "report.pdf", "version": theirs.version}, ctx=_ctx(tenant, user))

    # From B's conversation, only B's version is reachable.
    in_b = _ctx(tenant, user, thread_id=_OTHER_THREAD)
    await tool.call({"name": "report.pdf"}, ctx=in_b)
    link_b = await _only_link(service, in_b)
    assert link_b.artifact_version_id == theirs.id
    # B never gets A's link, and A's link stays scoped to A.
    assert link_b.id != link.id
    assert link_b.thread_id == _OTHER_THREAD
    assert link.thread_id == _THREAD
    with pytest.raises(FileOpError, match="not saved in this conversation"):
        await tool.call(
            {"name": "report.pdf", "version": mine.version},
            ctx=_ctx(tenant, user, thread_id=_OTHER_THREAD),
        )


@pytest.mark.asyncio
async def test_name_only_saved_elsewhere_is_not_found_here() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user, thread=_OTHER_THREAD)
    with pytest.raises(
        FileOpError, match=r"no file named 'report\.pdf' was saved in this conversation"
    ):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, user)
        )


@pytest.mark.asyncio
async def test_agent_root_file_has_no_conversation_marker() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user, thread=None)
    with pytest.raises(FileOpError, match="was saved in this conversation"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, user)
        )


@pytest.mark.asyncio
async def test_old_layout_conversation_cannot_share() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user)
    with pytest.raises(FileOpError, match="no workspace directory of its own"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, user, workspace_layout="agent_root")
        )


@pytest.mark.asyncio
async def test_unknown_version_is_a_tool_error() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user)
    with pytest.raises(FileOpError, match=r"version 7 of 'report\.pdf' was not saved"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf", "version": 7}, ctx=_ctx(tenant, user)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, True, "2"])
async def test_bad_version_argument_rejected(bad: object) -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user)
    with pytest.raises(ValueError, match="positive integer"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf", "version": bad}, ctx=_ctx(tenant, user)
        )


@pytest.mark.asyncio
async def test_another_users_artifact_is_not_found() -> None:
    service = _service()
    tenant, owner, other = uuid4(), uuid4(), uuid4()
    await _save(service, tenant=tenant, user=owner)
    with pytest.raises(FileOpError, match="no file named"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, other)
        )


@pytest.mark.asyncio
async def test_another_agents_artifact_is_not_found() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user, agent_key="other-agent")
    with pytest.raises(FileOpError, match="no file named"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, user)
        )


@pytest.mark.asyncio
async def test_another_tenants_artifact_is_not_found() -> None:
    service = _service()
    user = uuid4()
    await _save(service, tenant=uuid4(), user=user)
    with pytest.raises(FileOpError, match="no file named"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(uuid4(), user)
        )


@pytest.mark.asyncio
async def test_version_without_snapshot_cannot_be_pinned() -> None:
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user, snapshot=False)
    with pytest.raises(FileOpError, match="before per-version copies"):
        await ShareFileTool(service=service, ttl_minutes=120).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, user)
        )


@pytest.mark.asyncio
async def test_requires_user_and_agent_binding() -> None:
    service = _service()
    tool = ShareFileTool(service=service, ttl_minutes=120)
    with pytest.raises(ToolBlockedError):
        await tool.call({"name": "x"}, ctx=_ctx(uuid4(), uuid4(), user_id=None))
    with pytest.raises(ToolBlockedError):
        await tool.call({"name": "x"}, ctx=_ctx(uuid4(), uuid4(), agent_key=""))


@pytest.mark.asyncio
async def test_link_is_recorded_under_the_turn_root_run() -> None:
    """审批续跑段 / 子代铸的链接,审计用的 ``run_id`` 记在这一轮首段的 run 上。"""
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user, thread=_THREAD)
    await _save(service, tenant=tenant, user=user, thread=_OTHER_THREAD)
    root, child_parent = uuid4(), uuid4()
    tool = ShareFileTool(service=service, ttl_minutes=120)
    resumed = _ctx(tenant, user, turn_root_run_id=root)
    child = _ctx(tenant, user, thread_id=_OTHER_THREAD, inputs_run_id=child_parent)
    await tool.call({"name": "report.pdf"}, ctx=resumed)
    await tool.call({"name": "report.pdf"}, ctx=child)
    assert (await _only_link(service, resumed)).run_id == root
    assert (await _only_link(service, child)).run_id == child_parent


# ------------------------------------------------------------ registration


def _env(service: ShareLinkService | None, *, worker: bool = False) -> ToolEnv:
    return ToolEnv(share_links=service, worker_policy=worker)


@pytest.mark.asyncio
async def test_opt_in_off_registers_no_tool() -> None:
    for block in (None, ShareLinksSpec(enabled=False)):
        registry = await build_tool_registry([], tool_env=_env(_service()), share_links=block)
        assert registry.get(SHARE_FILE_TOOL_NAME) is None


@pytest.mark.asyncio
async def test_opt_in_on_registers_tool_with_ttl() -> None:
    registry = await build_tool_registry(
        [], tool_env=_env(_service()), share_links=ShareLinksSpec(enabled=True, ttl_minutes=45)
    )
    tool = registry.get(SHARE_FILE_TOOL_NAME)
    assert isinstance(tool, ShareFileTool)
    assert tool.ttl_minutes == 45
    # No lifetime in the description: a delegated child mints with its root's (L6).
    assert "minutes" not in tool.spec.description


@pytest.mark.asyncio
async def test_opt_in_without_platform_wiring_fails_the_build() -> None:
    with pytest.raises(AgentFactoryError, match="EXPERT_WORK_PUBLIC_BASE_URL"):
        await build_tool_registry([], tool_env=_env(None), share_links=ShareLinksSpec(enabled=True))


@pytest.mark.asyncio
async def test_dynamic_worker_never_gets_the_tool() -> None:
    registry = await build_tool_registry(
        [], tool_env=_env(_service(), worker=True), share_links=ShareLinksSpec(enabled=True)
    )
    assert registry.get(SHARE_FILE_TOOL_NAME) is None


def test_public_base_url_validation() -> None:
    assert normalize_public_base_url(" https://a.example/ ") == "https://a.example"
    assert normalize_public_base_url("https://a.example/prefix") == "https://a.example/prefix"
    for bad in (
        "",
        "a.example",
        "ftp://a",
        "https://a.example/x y",
        'https://a"b',
        "https://",
        "https://a.example/x?a=1",
        "https://a.example/#frag",
        "https://a.example/\x01",
    ):
        with pytest.raises(ValueError):
            normalize_public_base_url(bad)


@pytest.mark.asyncio
async def test_without_an_artifact_list_nothing_is_minted() -> None:
    """L5 —— 没有产物清单的 run(链接交不到用户手上):拒绝,不铸链接。"""
    service = _service()
    tenant, user = uuid4(), uuid4()
    await _save(service, tenant=tenant, user=user)
    with pytest.raises(ToolBlockedError, match="no artifact list"):
        await ShareFileTool(service=service, ttl_minutes=30).call(
            {"name": "report.pdf"}, ctx=_ctx(tenant, user, artifact_recorder=None)
        )
    assert service.links._rows == {}  # type: ignore[attr-defined]
