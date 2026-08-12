"""Tests for the shared SSE event producer (``api/_run_event_stream.py``).

Task 4 (external-API v1 P1) extracted ``_stream_replay`` / ``_stream_live``
out of ``api/runs.py`` into ``build_event_producer`` so the console and
external endpoints share one implementation instead of two copies drifting
apart. The HTTP-level integration suites (``test_runs_api.py`` /
``test_external_events.py``) both drive **in-memory** stores that
structurally cannot observe whether the resolved tenant ``scope`` was
actually applied around the replay read: ``InMemoryRunEventStore.list``
takes no ``tenant_id`` argument at all (RLS scoping is a Postgres-only
concept, per that store's own module docstring). That means a call site
that quietly regresses from ``scope=lambda: applied_scope(scope)`` to
``scope=None`` passes every existing HTTP test unchanged — confirmed by a
code-review round on this task (Important 3): mutating the console call
site to ``scope=None`` left ``test_runs_api.py`` + ``test_external_events.py``
at 75/75 green.

These two tests close that gap without needing a real Postgres/RLS
integration test:

1. ``build_event_producer`` itself actually enters whatever scope factory
   it is given, exactly once, around the replay read.
2. The console call site (``api/runs.py``) really does pass a live,
   invokable scope factory — not ``None`` — to ``build_event_producer``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

import control_plane.api.runs as runs_module
from control_plane.api._run_event_stream import build_event_producer
from expert_work.runtime.runs import InMemoryRunEventStore, make_event_record
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from tests.test_runs_api import _seed_completed_run, audit_store, runs_client  # noqa: F401


@pytest.mark.asyncio
async def test_replay_enters_the_given_scope_factory_exactly_once() -> None:
    """Direct unit test on ``build_event_producer`` — a caller-supplied scope
    factory must be invoked exactly once around the durable-store read."""
    run_id = uuid4()
    event_store = InMemoryRunEventStore()
    await event_store.append(
        make_event_record(run_id=run_id, seq=1, event_name="metadata", data={})
    )

    entered = 0

    @asynccontextmanager
    async def _spy_scope() -> AsyncIterator[None]:
        nonlocal entered
        entered += 1
        yield

    producer = build_event_producer(
        run_id=run_id,
        is_terminal=True,
        event_store=event_store,
        stream_bridge=InMemoryStreamBridge(),
        since_seq=None,
        scope=_spy_scope,
    )
    frames = [chunk async for chunk in producer]
    assert frames  # sanity: the replay actually produced real frames
    assert entered == 1


@pytest.mark.asyncio
async def test_console_events_endpoint_passes_a_live_scope_factory(
    runs_client: AsyncClient,  # noqa: F811 -- pytest fixture injection, not a redefinition
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins ``api/runs.py``'s call site — the one place that genuinely
    differs from the external caller (which deliberately passes ``None``).
    A regression to ``scope=None`` here silently drops the resolved-tenant
    binding on the replay DB read; per the module docstring, no HTTP-level
    assertion against the in-memory store can ever catch that, so this test
    spies on the call site directly instead.
    """
    captured: dict[str, Any] = {}
    real_build_event_producer = runs_module.build_event_producer

    def _spy(**kwargs: Any) -> AsyncIterator[bytes]:
        captured.update(kwargs)
        return real_build_event_producer(**kwargs)

    monkeypatch.setattr(runs_module, "build_event_producer", _spy)

    thread_id, run_id = await _seed_completed_run(runs_client)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/events")
    assert resp.status_code == 200, resp.text

    assert "scope" in captured
    scope_factory = captured["scope"]
    assert scope_factory is not None
    # It must be a genuine, still-usable factory — entering the context
    # manager it returns must not raise.
    async with scope_factory():
        pass
