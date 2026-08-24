"""Integration tests for Postgres backends of checkpointer + store.

These are minimal smoke tests — we only verify the factory connects to
Postgres, the ``setup()`` migration runs, and the yielded saver/store
exposes the expected public API. End-to-end checkpoint/state round-trip
is covered by LangGraph's own test suite; duplicating it here would
couple us tightly to their internal TypedDict shapes.
"""

from __future__ import annotations

import asyncio
import re

import psycopg
import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from testcontainers.postgres import PostgresContainer

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.store import make_store

pytestmark = pytest.mark.integration


def _sync_dsn(container: PostgresContainer) -> str:
    """LangGraph savers/stores take a sync-style DSN and manage their own
    async pool. Strip the testcontainers ``+psycopg2`` driver suffix."""
    return str(container.get_connection_url()).replace("+psycopg2", "")


@pytest.mark.asyncio
async def test_postgres_checkpointer_setup_and_api(
    postgres_container: PostgresContainer,
) -> None:
    dsn = _sync_dsn(postgres_container)
    async with make_checkpointer("postgres", dsn) as cp:
        assert isinstance(cp, BaseCheckpointSaver)
        # Async API surface that the orchestrator depends on
        assert callable(cp.aput)
        assert callable(cp.aget_tuple)
        assert callable(cp.alist)


@pytest.mark.asyncio
async def test_postgres_checkpointer_concurrent_setup_no_race(
    postgres_container: PostgresContainer,
) -> None:
    """Concurrent first-run ``setup()`` must not race ``CREATE TYPE``.

    The blue/green control-plane pair (and rolling deploys) start replicas
    that all call ``make_checkpointer`` at once. On a fresh database the
    first setup runs ``CREATE TYPE`` — without the advisory lock two of them
    collide with ``duplicate key ... "pg_type_typname_nsp_index"``. We
    reproduce by pointing N concurrent factories at a brand-new database.
    """
    base = _sync_dsn(postgres_container)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute("DROP DATABASE IF EXISTS ckp_race")
        await admin.execute("CREATE DATABASE ckp_race")
    race_dsn = re.sub(r"/[^/?]+(\?|$)", r"/ckp_race\1", base)

    async def _setup_once() -> bool:
        async with make_checkpointer("postgres", race_dsn) as cp:
            return callable(cp.aput)

    results = await asyncio.gather(*[_setup_once() for _ in range(5)])
    assert all(results)


@pytest.mark.asyncio
async def test_postgres_checkpointer_pool_put_get_round_trip(
    postgres_container: PostgresContainer,
) -> None:
    """BUG-18 — the pool-backed saver must actually read/write checkpoints.

    The factory now hands ``AsyncPostgresSaver`` a psycopg
    ``AsyncConnectionPool`` instead of the single connection
    ``from_conn_string`` held; prove a put/get round-trip works through
    the pool against a real Postgres.
    """
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint

    dsn = _sync_dsn(postgres_container)
    config: RunnableConfig = {"configurable": {"thread_id": "bug18-pool", "checkpoint_ns": ""}}

    async with make_checkpointer("postgres", dsn) as cp:
        checkpoint = empty_checkpoint()
        await cp.aput(config, checkpoint, {"source": "input", "step": -1, "parents": {}}, {})
        tup = await cp.aget_tuple(config)
        assert tup is not None
        assert tup.checkpoint["id"] == checkpoint["id"]


@pytest.mark.asyncio
async def test_postgres_store_put_get_round_trip(
    postgres_container: PostgresContainer,
) -> None:
    dsn = _sync_dsn(postgres_container)
    namespace = ("tenant-x", "memories")

    async with make_store("postgres", dsn) as store:
        assert isinstance(store, BaseStore)
        await store.aput(namespace, key="m1", value={"text": "hello"})
        item = await store.aget(namespace, "m1")
        assert item is not None
        assert item.value == {"text": "hello"}
