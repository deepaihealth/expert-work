# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/runtime/checkpointer/{provider,async_provider}.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - Async-only (we are an async stack throughout); sync path dropped
#   - SQLite backend dropped (Postgres-only per ADR-0004)
#   - No module-level singleton (DeerFlow's global _checkpointer + _checkpointer_ctx);
#     dependency injection via FastAPI lifespan or explicit context manager
#   - No DeerFlow config-system coupling; backend + DSN passed explicitly
# Last sync: 2026-05-11
# ============================================================

"""Factory for ``langgraph.types.Checkpointer`` instances.

Two backends:

- ``memory`` — ``langgraph.checkpoint.memory.InMemorySaver`` (tests, dev)
- ``postgres`` — ``langgraph.checkpoint.postgres.aio.AsyncPostgresSaver``

Usage in FastAPI lifespan::

    from expert_work.runtime.checkpointer import make_checkpointer

    async with make_checkpointer("postgres", dsn) as checkpointer:
        app.state.checkpointer = checkpointer
        yield
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from expert_work.runtime._setup_retry import setup_with_retry

logger = logging.getLogger(__name__)

CheckpointerBackend = Literal["memory", "postgres"]

# Fixed sizing (BUG-18): checkpoint IO is short per-step bursts; 8 concurrent
# connections is ample headroom without extra DB load. Deliberately not a
# setting — no deployment has needed to tune it.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 8


def _build_checkpointer_pool(dsn: str) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
    """Build — but do not open — the connection pool for the postgres backend.

    Connection kwargs must match what ``AsyncPostgresSaver.from_conn_string``
    passes to ``AsyncConnection.connect``. ``check_connection`` validates each
    connection at checkout, so ones killed underneath us (DB restart, network
    blip, LB idle timeout) are discarded and re-dialed instead of poisoning
    every checkpoint read/write until pod restart.
    """
    return AsyncConnectionPool(
        dsn,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
        open=False,
    )


@contextlib.asynccontextmanager
async def make_checkpointer(
    backend: CheckpointerBackend,
    dsn: str | None = None,
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Yield a configured async LangGraph checkpointer; tear down on exit.

    :param backend: ``"memory"`` (tests / dev) or ``"postgres"`` (prod)
    :param dsn: ``postgresql://...`` connection string. Required for postgres.
                Use the **sync driver scheme** (``postgresql://`` or
                ``postgresql+psycopg://``) — the factory opens a psycopg
                ``AsyncConnectionPool`` over it and feeds that to
                ``AsyncPostgresSaver``.
    :raises ValueError: backend unknown or postgres DSN missing
    """
    # Widen to ``str`` so the trailing "unknown backend" path is reachable to
    # both mypy and runtime (type-erased callers, e.g. config strings, are
    # the typical source of bad values).
    # Stream HX-4 (Mini-ADR HX-D3) — both backends leave the factory
    # wrapped in the timing proxy so the IO histogram exists on every
    # deployment shape (and tests exercise the same call path as prod).
    from expert_work.runtime.checkpointer.timing import TimingCheckpointSaver

    bk: str = backend
    if bk == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("checkpointer.memory.init")
        yield TimingCheckpointSaver(InMemorySaver())
        return

    if bk == "postgres":
        if not dsn:
            msg = "checkpointer backend 'postgres' requires a non-empty dsn"
            raise ValueError(msg)

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # BUG-18 — ``from_conn_string`` held ONE connection for the app's
        # whole lifetime: a dead TCP connection meant every checkpoint IO
        # failed until pod restart, and all IO serialized on it. The pool
        # pre-pings at checkout and re-dials dead connections.
        pool = _build_checkpointer_pool(dsn)
        await pool.open(wait=True)
        try:
            saver = AsyncPostgresSaver(pool)
            await setup_with_retry(saver)
            logger.info("checkpointer.postgres.ready")
            yield TimingCheckpointSaver(saver)
        finally:
            await pool.close()
        return

    msg = f"unknown checkpointer backend: {bk!r}"
    raise ValueError(msg)
