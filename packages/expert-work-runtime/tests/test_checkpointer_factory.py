"""Tests for the checkpointer factory (memory backend + error paths).

The Postgres backend is exercised separately by ``test_checkpointer_postgres.py``
which requires testcontainers Docker.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.checkpointer.factory import _build_checkpointer_pool


@pytest.mark.asyncio
async def test_memory_backend_yields_in_memory_saver() -> None:
    # Stream HX-4 — the factory wraps every backend in the timing proxy.
    from expert_work.runtime.checkpointer.timing import TimingCheckpointSaver

    async with make_checkpointer("memory") as cp:
        assert isinstance(cp, TimingCheckpointSaver)
        assert isinstance(cp._inner, InMemorySaver)


@pytest.mark.asyncio
async def test_unknown_backend_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown checkpointer backend"):
        async with make_checkpointer("redis"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_postgres_backend_requires_dsn() -> None:
    with pytest.raises(ValueError, match="requires a non-empty dsn"):
        async with make_checkpointer("postgres"):
            pass

    with pytest.raises(ValueError, match="requires a non-empty dsn"):
        async with make_checkpointer("postgres", dsn=""):
            pass


# BUG-18 — the postgres backend must hold a self-healing AsyncConnectionPool,
# not the single long-lived connection ``from_conn_string`` opens.

_DSN = "postgresql://user:pw@db.example:5432/checkpoints"


@pytest.mark.asyncio
async def test_build_pool_is_not_opened_at_build_time() -> None:
    pool = _build_checkpointer_pool(_DSN)
    # open=False: nothing is dialed until the factory awaits pool.open().
    assert pool.closed
    assert pool.conninfo == _DSN


@pytest.mark.asyncio
async def test_build_pool_validates_connections_at_checkout() -> None:
    pool = _build_checkpointer_pool(_DSN)
    # check_connection is the self-heal: dead connections (DB restart, idle
    # LB kill) are discarded and re-dialed instead of failing forever.
    assert pool._check is AsyncConnectionPool.check_connection


@pytest.mark.asyncio
async def test_build_pool_connection_kwargs_match_from_conn_string() -> None:
    pool = _build_checkpointer_pool(_DSN)
    assert pool.kwargs == {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }


@pytest.mark.asyncio
async def test_build_pool_sizing() -> None:
    pool = _build_checkpointer_pool(_DSN)
    assert pool.min_size == 1
    assert pool.max_size == 8


@pytest.mark.asyncio
async def test_postgres_factory_opens_pool_and_closes_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from expert_work.runtime.checkpointer import factory
    from expert_work.runtime.checkpointer.timing import TimingCheckpointSaver

    events: list[str] = []

    class FakePool:
        async def open(self, wait: bool = False) -> None:
            events.append(f"open(wait={wait})")

        async def close(self) -> None:
            events.append("close")

    # ``Any`` keeps mypy from flagging the identity check against the
    # saver's ``Conn``-typed attribute.
    fake_pool: Any = FakePool()

    async def fake_setup(saver: object) -> None:
        events.append("setup")

    monkeypatch.setattr(factory, "_build_checkpointer_pool", lambda dsn: fake_pool)
    monkeypatch.setattr(factory, "setup_with_retry", fake_setup)

    async with factory.make_checkpointer("postgres", _DSN) as cp:
        assert isinstance(cp, TimingCheckpointSaver)
        # The saver is fed the pool itself — every checkpoint IO checks out
        # of the pool rather than serializing on one connection.
        inner = cp._inner
        assert isinstance(inner, AsyncPostgresSaver)
        assert inner.conn is fake_pool
        assert events == ["open(wait=True)", "setup"]
    assert events == ["open(wait=True)", "setup", "close"]
