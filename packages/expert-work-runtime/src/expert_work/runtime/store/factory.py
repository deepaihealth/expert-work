# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/runtime/store/{provider,async_provider}.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - Async-only; sync path dropped
#   - SQLite backend dropped (Postgres-only per ADR-0004)
#   - No module-level singleton; DI via FastAPI lifespan or explicit ctx
#   - No DeerFlow config coupling; backend + DSN passed explicitly
# Last sync: 2026-05-11
# ============================================================

"""Factory for ``langgraph.store.base.BaseStore`` instances.

LangGraph's ``BaseStore`` is the long-term memory backend (key-value with
namespace + JSON value). Stream M2's memory-tier work consumes this.

Two backends:

- ``memory`` — ``langgraph.store.memory.InMemoryStore``
- ``postgres`` — ``langgraph.store.postgres.aio.AsyncPostgresStore``

Usage in FastAPI lifespan::

    from expert_work.runtime.store import make_store

    async with make_store("postgres", dsn) as store:
        app.state.store = store
        yield
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Literal

from langgraph.store.base import BaseStore

from expert_work.runtime._setup_retry import setup_with_retry

logger = logging.getLogger(__name__)

StoreBackend = Literal["memory", "postgres"]


@contextlib.asynccontextmanager
async def make_store(
    backend: StoreBackend,
    dsn: str | None = None,
) -> AsyncIterator[BaseStore]:
    """Yield a configured async LangGraph store; tear down on exit.

    :param backend: ``"memory"`` or ``"postgres"``
    :param dsn: ``postgresql://...`` (required for postgres backend).
                Use the **sync driver scheme** (``postgresql://`` or
                ``postgresql+psycopg://``) — ``AsyncPostgresStore`` manages
                its own async pool under the hood.
    :raises ValueError: backend unknown or postgres DSN missing
    """
    # Widen to ``str`` so the "unknown backend" branch is reachable to mypy.
    bk: str = backend
    if bk == "memory":
        from langgraph.store.memory import InMemoryStore

        logger.info("store.memory.init")
        yield InMemoryStore()
        return

    if bk == "postgres":
        if not dsn:
            msg = "store backend 'postgres' requires a non-empty dsn"
            raise ValueError(msg)

        from langgraph.store.postgres.aio import AsyncPostgresStore

        async with AsyncPostgresStore.from_conn_string(dsn) as store:
            await setup_with_retry(store)
            logger.info("store.postgres.ready")
            yield store
        return

    msg = f"unknown store backend: {bk!r}"
    raise ValueError(msg)
