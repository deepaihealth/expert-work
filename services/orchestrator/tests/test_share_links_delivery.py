"""B-326 —— a marked file reaches the integrator through the run's artifact list only.

Over the real ``run_agent`` + compiled graph:

* the SSE ``end`` frame's ``artifacts`` (and the run record) carry ``share_url`` /
  ``share_expires_at`` on the marked artifact only; an unmarked artifact of the same run
  has neither key; a run without ``share_file`` has entries exactly as before; the run
  record also keeps the internal ``_share`` record of the marked version, which the
  ``end`` frame never carries;
* marking and registering the same name in one run, in either order, keep the delivery
  fields on the version that was marked;
* the URL and the link id appear nowhere the model or memory can read: the tool result,
  graph state / checkpoint, the next turn's LLM request, the memory-extraction text;
* a file marked by a delegated child shows on the root run's list (shared recorder);
* a child of a root without share links neither sees nor can call ``share_file``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.persistence import InMemoryArtifactStore
from expert_work.persistence.artifact_share import InMemoryArtifactShareLinkStore
from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, RunManager
from expert_work.runtime.runs.store import InMemoryRunStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.graph_builder.memory import _extraction_inputs
from orchestrator.sse import run_agent, sse_consumer
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.artifact import ArtifactRecorder
from orchestrator.tools.registry import (
    WORKSPACE_LAYOUT_KEY,
    ToolBlockedError,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from orchestrator.tools.share_file import ShareFileTool, ShareLinkService

_BASE = "https://files.example.test"
_ROOT_TTL = 25
_AGENT = "report-agent"


@dataclass
class _ScriptedLLM:
    """Replies in order; records every request (messages + bound tool names)."""

    replies: list[AIMessage]
    requests: list[list[BaseMessage]] = field(default_factory=list)
    tools_seen: list[list[str]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Any = None,
    ) -> AIMessage:
        del output_schema, on_delta
        self.requests.append(list(messages))
        self.tools_seen.append([t.name for t in tools])
        return self.replies[min(len(self.requests), len(self.replies)) - 1]


@dataclass
class _NotesTool:
    """Stands in for ``save_artifact``: records an unmarked entry on the run's list."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="save_notes", description="stub")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args
        assert ctx.artifact_recorder is not None
        ctx.artifact_recorder(
            {"name": "notes.md", "kind": "document", "version": 1, "created_at": "t"}
        )
        return ToolResult(content="saved notes.md")


def _call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"id": f"call-{uuid4().hex[:6]}", "name": name, "args": args}]
    )


@dataclass
class _Conv:
    tenant: UUID = field(default_factory=uuid4)
    user: UUID = field(default_factory=uuid4)
    thread: UUID = field(default_factory=uuid4)

    def configurable(self, run: UUID) -> dict[str, Any]:
        return {
            "thread_id": str(self.thread),
            "tenant_id": str(self.tenant),
            "user_id": str(self.user),
            "run_id": str(run),
            "agent_key": _AGENT,
            WORKSPACE_LAYOUT_KEY: "conversation",
        }


async def _service_with_report(conv: _Conv) -> ShareLinkService:
    service = ShareLinkService(
        links=InMemoryArtifactShareLinkStore(),
        artifacts=InMemoryArtifactStore(),
        public_base_url=_BASE,
    )
    version_id = uuid4()
    await service.artifacts.save_version(
        tenant_id=conv.tenant,
        user_id=conv.user,
        agent_key=_AGENT,
        name="7天血糖报告.pdf",
        kind="document",
        path_in_workspace=f"agents/{_AGENT}/threads/{conv.thread}/work/7天血糖报告.pdf",
        created_in_thread="r",
        version_id=version_id,
        snapshot_key=str(version_id),
        size_bytes=3,
        sha256="0" * 64,
    )
    return service


async def _never() -> bool:
    return False


async def _wire_end_artifacts(
    bridge: InMemoryStreamBridge, record: Any, rm: RunManager
) -> list[dict[str, Any]] | None:
    """The ``end`` frame's ``artifacts`` as the live SSE stream sends them."""
    async for frame in sse_consumer(
        bridge=bridge, record=record, run_manager=rm, is_disconnected=_never
    ):
        text = frame.decode()
        if text.startswith("event: end"):
            data = next(line for line in text.splitlines() if line.startswith("data: "))
            artifacts: list[dict[str, Any]] | None = json.loads(data[6:]).get("artifacts")
            return artifacts
    raise AssertionError("no end frame")


async def _run(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    conv: _Conv,
    *,
    turns: int = 1,
) -> tuple[list[list[dict[str, Any]] | None], Any, InMemoryRunStore]:
    """Run ``turns`` turns on one conversation through ``run_agent``; returns each turn's
    end-frame artifacts, the final checkpoint values and the run store."""
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    store = InMemoryRunStore()
    ends: list[list[dict[str, Any]] | None] = []
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        rm = RunManager(store)
        for turn in range(turns):
            run = uuid4()
            record = await rm.create(
                run_id=run,
                thread_id=conv.thread,
                tenant_id=conv.tenant,
                on_disconnect=DisconnectMode.CANCEL,
            )
            bridge = InMemoryStreamBridge()
            await run_agent(
                bridge=bridge,
                run_manager=rm,
                record=record,
                graph=compiled,  # type: ignore[arg-type]
                graph_input={
                    "messages": [HumanMessage(content=f"turn {turn}: send me the report")],
                    "step_count": 0,
                    "max_steps": 8,
                },
                config={"configurable": conv.configurable(run)},
            )
            ends.append(await _wire_end_artifacts(bridge, record, rm))
        state = await compiled.aget_state({"configurable": {"thread_id": str(conv.thread)}})
    return ends, state.values, store


@pytest.mark.asyncio
async def test_marked_file_is_reported_on_the_run_and_never_reaches_a_model() -> None:
    conv = _Conv()
    service = await _service_with_report(conv)
    registry = ToolRegistry()
    registry.register(ShareFileTool(service=service, ttl_minutes=420))
    registry.register(_NotesTool())
    llm = _ScriptedLLM(
        replies=[
            _call("save_notes", {}),
            _call("share_file", {"name": "7天血糖报告.pdf"}),
            AIMessage(content="报告已发给您。"),
            AIMessage(content="好的,还有别的吗?"),
        ]
    )
    ends, values, store = await _run(llm, registry, conv, turns=2)

    [link] = list(service.links._rows.values())  # type: ignore[attr-defined]
    url = f"{_BASE}/v1/public/files/{link.id}/7%E5%A4%A9%E8%A1%80%E7%B3%96%E6%8A%A5%E5%91%8A.pdf"
    first = ends[0]
    assert first is not None
    by_name = {e["name"]: e for e in first}
    # Marked → the two delivery fields; unmarked → neither key.
    assert by_name["7天血糖报告.pdf"]["share_url"] == url
    assert by_name["7天血糖报告.pdf"]["share_expires_at"] == link.expires_at.isoformat()
    assert by_name["notes.md"] == {
        "name": "notes.md",
        "kind": "document",
        "version": 1,
        "created_at": "t",
    }
    assert "_share" not in by_name["7天血糖报告.pdf"]
    # Turn 2 marked nothing: its list is empty, no stray fields.
    assert ends[1] == []
    # The run record keeps what was marked (read paths re-check and renew from it).
    [marked_run] = [r for r in store._rows.values() if r.artifacts]
    assert marked_run.artifacts is not None
    [stored] = [e for e in marked_run.artifacts if "share_url" in e]
    assert stored["_share"] == {
        "artifact_id": str(link.artifact_id),
        "version_id": str(link.artifact_version_id),
        "agent_key": _AGENT,
    }

    # Nothing a model or memory reads holds the credential.
    record = json.dumps(
        [m.model_dump() for m in values["messages"]], default=str, ensure_ascii=False
    )
    turn2_request = json.dumps([m.content for m in llm.requests[-1]], ensure_ascii=False)
    memory_text = repr(_extraction_inputs(values["messages"], []))
    for blob in (record, turn2_request, memory_text):
        assert link.id not in blob
        assert "/v1/public/files/" not in blob
        assert _BASE not in blob


@pytest.mark.asyncio
async def test_a_run_without_share_file_reports_entries_exactly_as_before() -> None:
    conv = _Conv()
    registry = ToolRegistry()
    registry.register(_NotesTool())
    llm = _ScriptedLLM(replies=[_call("save_notes", {}), AIMessage(content="done")])
    ends, _, store = await _run(llm, registry, conv)
    expected = [{"name": "notes.md", "kind": "document", "version": 1, "created_at": "t"}]
    assert ends == [expected]
    [row] = list(store._rows.values())
    assert row.artifacts == expected


# ------------------------------------------------- delegated children


def _child_ctx(conv: _Conv, *, root_on: bool, recorder: ArtifactRecorder) -> ToolContext:
    parent_configurable = {
        **conv.configurable(uuid4()),
        "artifact_manifest_recorder": recorder,
    }
    parent_ctx = _build_tool_context(
        {"configurable": parent_configurable},
        share_links_ttl_minutes=_ROOT_TTL if root_on else None,
    )
    return _build_tool_context(_child_config(parent_ctx, sub_thread_id=uuid4(), sub_run_id=uuid4()))


@pytest.mark.asyncio
async def test_a_file_marked_by_a_child_shows_on_the_root_runs_list() -> None:
    conv = _Conv()
    service = await _service_with_report(conv)
    root_recorder = ArtifactRecorder()
    child_ctx = _child_ctx(conv, root_on=True, recorder=root_recorder)
    assert child_ctx.child_run
    before = datetime.now(UTC)
    # The child agent's own lifetime (60) is not the one used: the root's (25) is.
    await ShareFileTool(service=service, ttl_minutes=60).call(
        {"name": "7天血糖报告.pdf"}, ctx=child_ctx
    )
    [entry] = root_recorder.snapshot()
    assert entry["name"] == "7天血糖报告.pdf"
    link_id = str(entry["share_url"]).split("/")[-2]
    link = await service.links.get_public(link_id=link_id)
    assert link is not None
    # The link belongs to the root conversation (where the list is read back).
    assert link.thread_id == conv.thread
    # L6 —— minted with the root agent's lifetime.
    lifetime = link.expires_at - link.created_at
    assert lifetime == timedelta(minutes=_ROOT_TTL)
    assert link.created_at >= before


def test_a_root_without_share_links_passes_nothing_down() -> None:
    """L2 —— 根没开分享链接时子代的 configurable 里没有这个键(不进检查点元数据)。"""
    conv = _Conv()
    off = _build_tool_context({"configurable": conv.configurable(uuid4())})
    on = _build_tool_context(
        {"configurable": conv.configurable(uuid4())}, share_links_ttl_minutes=_ROOT_TTL
    )
    child_off = _child_config(off, sub_thread_id=uuid4(), sub_run_id=uuid4())["configurable"]
    child_on = _child_config(on, sub_thread_id=uuid4(), sub_run_id=uuid4())["configurable"]
    assert "share_links_ttl_minutes" not in child_off
    assert child_on["share_links_ttl_minutes"] == _ROOT_TTL


@pytest.mark.asyncio
async def test_a_child_of_a_root_without_share_links_cannot_mark() -> None:
    conv = _Conv()
    service = await _service_with_report(conv)
    child_ctx = _child_ctx(conv, root_on=False, recorder=ArtifactRecorder())
    assert child_ctx.share_links_ttl_minutes is None
    with pytest.raises(ToolBlockedError, match="does not deliver share links"):
        await ShareFileTool(service=service, ttl_minutes=60).call(
            {"name": "7天血糖报告.pdf"}, ctx=child_ctx
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child", "root_on", "visible"),
    [(True, False, False), (True, True, True), (False, False, True)],
)
async def test_agent_node_withholds_share_file_from_a_child_whose_root_is_off(
    child: bool, root_on: bool, visible: bool
) -> None:
    """The real bind list ``agent_node`` hands the model."""
    conv = _Conv()
    registry = ToolRegistry()
    registry.register(ShareFileTool(service=await _service_with_report(conv), ttl_minutes=60))
    llm = _ScriptedLLM(replies=[AIMessage(content="done")])
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    configurable: dict[str, Any] = {"thread_id": str(uuid4())}
    if child:
        configurable["child_run"] = True
        if root_on:
            configurable["share_links_ttl_minutes"] = _ROOT_TTL
    async with make_checkpointer("memory") as cp:
        await (
            GraphRunner(checkpointer=cp)
            .compile(graph)
            .ainvoke(
                {"messages": [HumanMessage(content="x")], "step_count": 0, "max_steps": 5},
                config={"configurable": configurable},
            )
        )
    assert ("share_file" in llm.tools_seen[0]) is visible


# ------------------------------------------------- one name, marked and re-registered


def _plain(version: int) -> dict[str, Any]:
    return {"name": "r.pdf", "kind": "document", "version": version, "created_at": f"t{version}"}


def _marked(version: int) -> dict[str, Any]:
    return {
        **_plain(version),
        "share_url": f"u{version}",
        "share_expires_at": "e",
        "_share": {"version_id": f"v{version}"},
    }


def _merged(version: int) -> dict[str, Any]:
    """Saved *and* marked in the same run: :meth:`ArtifactRecorder.snapshot` notes that in
    ``_share`` so a continuation can count the registration again."""
    entry = _marked(version)
    return {**entry, "_share": {**entry["_share"], "registered": True}}


def test_registering_the_marked_version_again_keeps_the_delivery_fields() -> None:
    """M4 —— 先标记、后登记同一版本(或先登记、后标记):分享字段留在那一项上。"""
    for calls in ([_marked(2), _plain(2)], [_plain(2), _marked(2)]):
        recorder = ArtifactRecorder()
        for entry in calls:
            recorder(entry)
        assert recorder.snapshot() == [_merged(2)]


def test_a_newer_registration_does_not_drop_the_marked_version() -> None:
    """M4 —— 标记 v1 之后又登记了 v2:模型已经告诉用户「发了」,v1 的链接要还在清单里。"""
    recorder = ArtifactRecorder()
    recorder(_marked(1))
    recorder(_plain(2))
    assert recorder.snapshot() == [_plain(2), _marked(1)]
    assert recorder.count == 1


def test_marking_an_older_version_does_not_rewrite_the_registered_one() -> None:
    """M4 —— 登记了 v3、标记的是 v1:v3 那项原样,v1 单独一项带分享字段。"""
    recorder = ArtifactRecorder()
    recorder(_plain(3))
    recorder(_marked(1))
    assert recorder.snapshot() == [_plain(3), _marked(1)]


def test_resume_seeding_keeps_the_mark_but_not_the_old_link() -> None:
    """M1 —— 续跑接着暂停前的清单记:标记还在(``_share``),但暂停前发出的链接不跟着走
    (可能已过期 / 智能体已关分享),由报清单的一方按当前配置重给。"""
    seeded = ArtifactRecorder()
    seeded.seed([_plain(3), _marked(1), {"bogus": True}, "x"])
    mark_only = {"name": "r.pdf", "kind": "document", "version": 1, "created_at": "t1"}
    assert seeded.snapshot() == [_plain(3), {**mark_only, "_share": {"version_id": "v1"}}]
    # Marking it again in the continuation brings fresh fields.
    seeded(_marked(1))
    assert seeded.snapshot() == [_plain(3), _marked(1)]


def test_a_file_saved_and_marked_before_a_pause_still_counts_after_it() -> None:
    """Round-6 M-1 —— 暂停前本 run 登记并标记了同一版本:续跑段的 B-226 计数仍算它
    (否则收尾指令会让模型说「没交付」,而 end 帧里明明带着链接);只标记的仍不算。"""
    before = ArtifactRecorder()
    before(_plain(1))
    before(_marked(1))
    assert before.count == 1

    resumed = ArtifactRecorder()
    resumed.seed(before.snapshot())
    assert resumed.count == 1
    mark_only = {k: v for k, v in _marked(1).items() if k not in ("share_url", "share_expires_at")}
    assert resumed.snapshot() == [{**mark_only, "_share": {"version_id": "v1", "registered": True}}]
    # A second pause/resume round trip is stable.
    again = ArtifactRecorder()
    again.seed(resumed.snapshot())
    assert again.count == 1
    assert again.snapshot() == resumed.snapshot()

    mark_only_run = ArtifactRecorder()
    mark_only_run.seed([_marked(1)])
    assert mark_only_run.count == 0


def test_a_mark_is_not_a_registration() -> None:
    """L8 —— 标记上一轮登记的文件不算这一轮「登记了产物」(B-226 计数)。"""
    earlier = ArtifactRecorder()
    earlier(_marked(1))
    assert earlier.count == 0
    this_run = ArtifactRecorder()
    this_run(_plain(1))
    this_run(_marked(1))
    assert this_run.count == 1


def test_a_childs_mark_lands_on_the_shared_list() -> None:
    root = ArtifactRecorder()
    root(_plain(2))
    root.scoped()(_marked(2))
    assert root.snapshot() == [_merged(2)]


def test_end_frame_data_never_carries_the_internal_record() -> None:
    from orchestrator.sse import end_frame_data

    data = end_frame_data(run_id=uuid4(), status="success", artifacts=[_marked(1), _plain(2)])
    marked = {k: v for k, v in _marked(1).items() if k != "_share"}
    assert data["artifacts"] == [marked, _plain(2)]


@pytest.mark.asyncio
async def test_sse_consumer_end_goes_through_the_artifact_view() -> None:
    """M1 —— 直播的 ``end`` 也经过视图(控制面在这里按当前配置续链接)。"""
    store = InMemoryRunStore()
    rm = RunManager(store)
    record = await rm.create(
        run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4(), on_disconnect=DisconnectMode.CANCEL
    )
    bridge = InMemoryStreamBridge()
    await bridge.publish_end(record.run_id, status="success", artifacts=[_marked(1)])
    seen: list[Any] = []

    async def view(artifacts: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        seen.append(artifacts)
        return [{**_plain(1), "share_url": "fresh", "share_expires_at": "later", "_share": {}}]

    frames = [
        f
        async for f in sse_consumer(
            bridge=bridge, record=record, run_manager=rm, is_disconnected=_never, artifact_view=view
        )
    ]
    end = next(f.decode() for f in frames if f.decode().startswith("event: end"))
    data = json.loads(next(line for line in end.splitlines() if line.startswith("data: "))[6:])
    assert seen == [[_marked(1)]]
    assert data["artifacts"] == [{**_plain(1), "share_url": "fresh", "share_expires_at": "later"}]
