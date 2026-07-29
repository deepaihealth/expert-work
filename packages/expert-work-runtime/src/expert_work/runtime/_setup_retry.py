"""Shared retry helper for LangGraph ``setup()`` concurrent-DDL races.

Used by both the checkpointer factory (:mod:`expert_work.runtime.checkpointer.factory`)
and the store factory (:mod:`expert_work.runtime.store.factory`) — both call a
LangGraph-provided ``setup()`` that is not concurrency-safe on first run.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Replicas that start at the same time (dev compose brings the blue + green
# control-plane pair up together; a rolling deploy can briefly overlap them)
# all call ``AsyncPostgresSaver.setup()`` at once. LangGraph's first-run setup
# is NOT concurrency-safe — two ``CREATE TYPE`` race and the loser fails with
# ``duplicate key value violates unique constraint "pg_type_typname_nsp_index"``
# (or a transient ``DeadlockDetected``). setup() is idempotent *across* runs,
# though: once the winner has created + recorded a migration, a re-run skips it.
# So we simply retry on those concurrency errors until the loser sees the
# winner's objects already present. Bounded; re-raises anything else / on exhaust.
_SETUP_MAX_ATTEMPTS = 8
_SETUP_RETRY_BASE_DELAY_S = 0.1


async def setup_with_retry(target: Any) -> None:
    """Run ``target.setup()``, retrying transient concurrent-DDL collisions.

    Two replicas calling first-run setup at once race ``CREATE TYPE`` /
    ``CREATE TABLE``; the loser raises a uniqueness/duplicate or deadlock
    error. setup() is idempotent across runs, so the loser just retries until
    it observes the winner's objects. Any other error (or running out of
    attempts) propagates.
    """
    import asyncio

    import psycopg.errors

    transient = (
        psycopg.errors.UniqueViolation,
        psycopg.errors.DuplicateObject,
        psycopg.errors.DuplicateTable,
        psycopg.errors.DeadlockDetected,
    )
    for attempt in range(_SETUP_MAX_ATTEMPTS):
        try:
            await target.setup()
            return
        except transient:
            if attempt == _SETUP_MAX_ATTEMPTS - 1:
                raise
            logger.info("checkpointer.postgres.setup_retry attempt=%d", attempt + 1)
            await asyncio.sleep(_SETUP_RETRY_BASE_DELAY_S * (attempt + 1))
