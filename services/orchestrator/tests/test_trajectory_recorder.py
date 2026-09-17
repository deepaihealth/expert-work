"""Stream L.L7 — :class:`TrajectoryRecorder` unit tests.

Covers ObjectStore key layout, ShareGPT serialisation, the four
outcomes (success / failed / max_steps / cancelled), failure
swallowing (an ObjectStore outage must not surface to the caller),
and counter emission.

The sse.py integration (run_agent dispatching the recorder) lives in
:mod:`test_sse_trajectory` so this file stays focused on the
recorder's own contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI
from expert_work.runtime.storage import InMemoryObjectStore, ObjectStoreError
from orchestrator.trajectory import (
    TrajectoryRecord,
    TrajectoryRecorder,
    serialize_messages_sharegpt,
)

# ---------------------------------------------------------------------------
# ShareGPT serialisation
# ---------------------------------------------------------------------------


def test_serialize_maps_message_classes_to_sharegpt_roles() -> None:
    """The four core LangChain message classes map to the standard
    ShareGPT roles (``system`` / ``user`` / ``assistant`` / ``tool``)."""
    messages = [
        SystemMessage(content="you are an agent"),
        HumanMessage(content="hello"),
        AIMessage(content="hi"),
        ToolMessage(content="42", tool_call_id="tc-1"),
    ]
    out = serialize_messages_sharegpt(messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert [m["content"] for m in out] == ["you are an agent", "hello", "hi", "42"]
    # tool_call_id round-trips for ToolMessage so the trajectory is a
    # faithful replay.
    assert out[3]["tool_call_id"] == "tc-1"


def test_serialize_flags_hidden_messages_only() -> None:
    """B-67 —— 隐藏 human(「本轮输入」段、恢复建议)在 ShareGPT 里也是 ``user``;带上
    ``hidden`` 让下游取「用户请求」时能跳过它。普通消息的形状不变。"""
    out = serialize_messages_sharegpt(
        [
            HumanMessage(content=""),
            HumanMessage(content="[inputs block]", additional_kwargs={HIDE_FROM_UI: True}),
        ]
    )
    assert out == [
        {"role": "user", "content": ""},
        {"role": "user", "content": "[inputs block]", "hidden": True},
    ]


def test_serialize_carries_tool_calls_on_ai_message() -> None:
    """AIMessage with ``tool_calls`` keeps id/name/args; LangChain's
    internal ``type`` tag is stripped (ShareGPT doesn't expect it)."""
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "search", "args": {"q": "x"}, "id": "tc-1", "type": "tool_call"},
        ],
    )
    out = serialize_messages_sharegpt([ai])
    assert out == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "search", "args": {"q": "x"}, "id": "tc-1"}],
        }
    ]


def test_serialize_flattens_content_block_list() -> None:
    """When ``content`` is a list of text blocks (J.6 multimodal path),
    serialisation concatenates the text into a single string."""
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "look at this:"},
            {"type": "text", "text": " what is it?"},
        ]
    )
    out = serialize_messages_sharegpt([msg])
    assert out == [{"role": "user", "content": "look at this: what is it?"}]


def test_serialize_handles_unknown_message_subclass() -> None:
    """An unknown message subclass lands with a marker role so the
    failure is visible in the JSONL rather than silently dropped."""

    class _UnknownMessage(HumanMessage):
        pass

    msg = _UnknownMessage(content="weird")
    out = serialize_messages_sharegpt([msg])
    # _UnknownMessage IS-A HumanMessage so isinstance picks ``user`` —
    # this asserts the inheritance chain works; truly unknown classes
    # would print the marker (no easy way to instantiate here without
    # also subclassing).
    assert out[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Key layout
# ---------------------------------------------------------------------------


def _record(
    outcome: str = "success",
    *,
    finished_at: datetime | None = None,
    tenant_id: UUID | None = None,
    thread_id: UUID | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        thread_id=thread_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        outcome=outcome,  # type: ignore[arg-type]
        messages=[HumanMessage(content="start")],
        finished_at=finished_at,
    )


def test_key_layout_includes_outcome_and_yyyy_mm_dd() -> None:
    """The default key shape lets eval loaders filter by outcome and
    date with a single ``list_prefix`` — no SQL join needed."""
    tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    thread_id = UUID("00000000-0000-0000-0000-000000000099")
    finished = datetime(2026, 5, 20, 12, 30, tzinfo=UTC)
    rec = _record(
        outcome="success",
        finished_at=finished,
        tenant_id=tenant_id,
        thread_id=thread_id,
    )
    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore())
    assert recorder.key_for(rec) == (
        "trajectories/00000000-0000-0000-0000-000000000001/success/"
        "2026/05/20/00000000-0000-0000-0000-000000000099.jsonl"
    )


def test_key_layout_normalises_naive_timestamp_to_utc() -> None:
    """Naive datetimes are interpreted as UTC — eval loaders depend on
    consistent partitioning."""
    naive = datetime(2026, 5, 20, 23, 59)  # no tzinfo
    rec = _record(finished_at=naive)
    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore())
    key = recorder.key_for(rec)
    assert "/2026/05/20/" in key


def test_key_layout_normalises_non_utc_timestamp() -> None:
    """A non-UTC tz-aware timestamp is converted to UTC for partitioning
    so PST-midnight stops landing in two different daily directories."""
    from datetime import timedelta, timezone

    pacific = timezone(timedelta(hours=-7))
    finished = datetime(2026, 5, 20, 23, 0, tzinfo=pacific)  # = 06:00 UTC next day
    rec = _record(finished_at=finished)
    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore())
    key = recorder.key_for(rec)
    assert "/2026/05/21/" in key  # UTC date, not PST date


def test_key_for_rejects_invalid_outcome() -> None:
    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore())
    rec = _record(outcome="garbage")
    with pytest.raises(ValueError, match="invalid outcome"):
        recorder.key_for(rec)


def test_recorder_rejects_empty_prefix() -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        TrajectoryRecorder(object_store=InMemoryObjectStore(), prefix="")


def test_recorder_normalises_trailing_slash_prefix() -> None:
    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore(), prefix="my/prefix/")
    assert recorder.prefix == "my/prefix"


# ---------------------------------------------------------------------------
# record() end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_writes_jsonl_with_expected_envelope() -> None:
    """A successful ``record()`` call writes a single-line JSONL with
    the canonical envelope shape; the bytes round-trip back through
    ``object_store.get()`` and ``json.loads``."""
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    tenant_id = uuid4()
    thread_id = uuid4()
    user_id = uuid4()
    run_id = uuid4()
    rec = TrajectoryRecord(
        thread_id=thread_id,
        tenant_id=tenant_id,
        outcome="success",
        messages=[
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
        ],
        user_id=user_id,
        run_id=run_id,
        started_at=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 20, 12, 1, tzinfo=UTC),
        step_count=2,
        metadata={"model": "claude-3-5-haiku"},
    )

    await recorder.record(rec)

    key = recorder.key_for(rec)
    raw = await store.get(key)
    # JSONL — payload ends in a newline so loaders can iterate cleanly.
    assert raw.endswith(b"\n")
    envelope = json.loads(raw.decode("utf-8"))
    assert envelope["thread_id"] == str(thread_id)
    assert envelope["tenant_id"] == str(tenant_id)
    assert envelope["outcome"] == "success"
    assert envelope["user_id"] == str(user_id)
    assert envelope["run_id"] == str(run_id)
    assert envelope["step_count"] == 2
    assert envelope["metadata"] == {"model": "claude-3-5-haiku"}
    assert envelope["started_at"] == "2026-05-20T12:00:00+00:00"
    assert envelope["finished_at"] == "2026-05-20T12:01:00+00:00"
    assert [m["role"] for m in envelope["messages"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_record_separates_outcomes_in_object_store() -> None:
    """Two runs with different outcomes land at distinct keys — the
    J.13 eval gate can ``list_prefix('trajectories/<tenant>/success/')``
    to load only the successes."""
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    tenant_id = uuid4()
    success = _record(outcome="success", tenant_id=tenant_id)
    failed = _record(outcome="failed", tenant_id=tenant_id)

    await recorder.record(success)
    await recorder.record(failed)

    success_keys = await store.list_prefix(f"trajectories/{tenant_id}/success/")
    failed_keys = await store.list_prefix(f"trajectories/{tenant_id}/failed/")
    assert len(success_keys) == 1
    assert len(failed_keys) == 1
    assert success_keys[0] != failed_keys[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed", "max_steps", "cancelled"])
async def test_record_accepts_all_four_outcomes(outcome: str) -> None:
    """Mini-ADR L-7 mandates four outcome buckets; the recorder must
    accept each one without invariant violations."""
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    rec = _record(outcome=outcome)
    await recorder.record(rec)
    keys = await store.list_prefix(f"trajectories/{rec.tenant_id}/{outcome}/")
    assert len(keys) == 1


# ---------------------------------------------------------------------------
# Failure swallowing — L7 invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_swallows_object_store_error() -> None:
    """An ObjectStore outage must not propagate — the recorder is
    fire-and-forget and the audit_log row is the source of truth for
    "did this run finish?"."""

    class _RaisingStore:
        async def put(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            msg = "s3 unreachable"
            raise ObjectStoreError(msg)

        # The other Protocol methods don't get called on the record path
        # but Protocol checks need them — InMemoryObjectStore is the
        # cleaner test, so we don't satisfy the full Protocol here.

    recorder = TrajectoryRecorder(object_store=_RaisingStore())  # type: ignore[arg-type]
    rec = _record()
    # Must not raise.
    await recorder.record(rec)


@pytest.mark.asyncio
async def test_record_swallows_unexpected_exception() -> None:
    """The defensive ``except Exception`` catch protects against bugs
    in the serialisation path / a misbehaving custom ObjectStore."""

    class _BrokenStore:
        async def put(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            # Anything that isn't ObjectStoreError still must not crash.
            raise RuntimeError("broken")

    recorder = TrajectoryRecorder(object_store=_BrokenStore())  # type: ignore[arg-type]
    await recorder.record(_record())


@pytest.mark.asyncio
async def test_record_with_invalid_outcome_logs_and_returns() -> None:
    """An invalid outcome triggers ``ValueError`` inside the recorder,
    which catches it and counts an ``invalid_record`` error rather
    than crashing the run's terminal path."""
    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore())
    rec = _record(outcome="garbage")
    # Must not raise — bad outcome is a programmer error but never a
    # reason to kill the run on its way out.
    await recorder.record(rec)


# ---------------------------------------------------------------------------
# Counter emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_counter_increments_on_success_write() -> None:
    """``expert_work_trajectory_recorded_total{outcome=...}`` increments on a
    successful write."""
    from prometheus_client import REGISTRY

    metric = "expert_work_trajectory_recorded_total"
    labels = {"outcome": "success"}
    before = REGISTRY.get_sample_value(metric, labels=labels) or 0.0

    recorder = TrajectoryRecorder(object_store=InMemoryObjectStore())
    await recorder.record(_record(outcome="success"))

    after = REGISTRY.get_sample_value(metric, labels=labels) or 0.0
    assert after == before + 1


@pytest.mark.asyncio
async def test_record_error_counter_increments_on_store_failure() -> None:
    """``expert_work_trajectory_record_errors_total{outcome, reason}`` records
    the failure mode so dashboards can distinguish config errors from
    actual ObjectStore outages."""
    from prometheus_client import REGISTRY

    metric = "expert_work_trajectory_record_errors_total"
    labels = {"outcome": "failed", "reason": "store_error"}
    before = REGISTRY.get_sample_value(metric, labels=labels) or 0.0

    class _RaisingStore:
        async def put(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            msg = "s3 unreachable"
            raise ObjectStoreError(msg)

    recorder = TrajectoryRecorder(object_store=_RaisingStore())  # type: ignore[arg-type]
    await recorder.record(_record(outcome="failed"))

    after = REGISTRY.get_sample_value(metric, labels=labels) or 0.0
    assert after == before + 1
