"""Per-tenant advisory-lock critical section — W1-PR2 Task 4.

``curation.py`` / ``triggers.py`` / ``webhook_endpoints.py`` each guard a
create endpoint with a "count existing rows, reject if the tenant is at
cap, else insert" check. Count-then-insert across two DB round-trips is
TOCTOU-vulnerable: with N replicas serving the same tenant, two (or more)
requests can each read the same under-cap count concurrently and each
insert, overshooting the configured cap by up to N-1 rows. A Python-level
lock doesn't help — a multi-replica deployment has one lock per process,
not one per tenant across the fleet.

``pg_try_advisory_xact_lock`` makes the count-check + insert single-flight
per ``(tenant_id, resource_kind)`` across every replica — same primitive
:class:`~control_plane.quality_drift_worker.QualityDriftWorker` /
:class:`~control_plane.memory_consolidator.MemoryConsolidator` /
:class:`~control_plane.skill_curator.SkillCurator` use for their
single-flight worker cycles (classids 8615 / 8616 / 8617 respectively;
``workspace_lock.py`` uses 1, ``mcp_oauth_refresh_lock.py`` uses 2 — this
module's classid is a new, distinct value so none of these ever share a
key). Unlike those worker locks — which skip the cycle entirely on a miss
— this is a request-serving critical section: a request that can't get the
lock retries once after a short delay, then fails the request with 429
rather than blocking indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("expert_work.control_plane.tenant_resource_lock")

#: Advisory-lock classid for per-tenant quota-check critical sections.
#: Distinct from the sibling single-flight-worker classids (workspace_lock.py
#: uses 1, mcp_oauth_refresh_lock.py uses 2, quality_drift_worker.py uses
#: 8615, memory_consolidator.py uses 8616, skill_curator.py uses 8617).
_TENANT_RESOURCE_LOCK_CLASSID = 8618

#: One short retry before giving up — a losing request fails fast with 429
#: (the caller retries), it doesn't queue behind an unbounded wait.
_RETRY_DELAY_S = 0.05


@asynccontextmanager
async def tenant_resource_lock(
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: UUID,
    resource_kind: str,
) -> AsyncIterator[None]:
    """Serialise a tenant's count-then-insert quota check across replicas.

    Wrap both the count check *and* the subsequent insert in this context
    so the two DB round-trips run as one critical section per
    ``(tenant_id, resource_kind)``. Tries the non-blocking
    ``pg_try_advisory_xact_lock`` once, retries once after a short delay,
    then raises ``HTTPException(429)`` — a losing request fails fast
    instead of queueing.

    ``session_factory=None`` (single-process / in-memory test stack) is a
    no-op: with one process there is no cross-replica race to guard
    against, so no lock is taken (mirrors ``QualityDriftWorker`` /
    ``MemoryConsolidator`` / ``SkillCurator``).
    """
    if session_factory is None:
        yield
        return

    key = f"{tenant_id}:{resource_kind}"
    async with session_factory() as lock_session:
        got = await _try_acquire(lock_session, key)
        if not got:
            await asyncio.sleep(_RETRY_DELAY_S)
            got = await _try_acquire(lock_session, key)
        if not got:
            # No lock held by this session — rollback just closes out the
            # session's own (empty) transaction, it releases nothing.
            await lock_session.rollback()
            logger.info(
                "tenant_resource_lock.contended",
                extra={"tenant_id": str(tenant_id), "resource_kind": resource_kind},
            )
            raise HTTPException(
                status_code=429,
                detail=f"{resource_kind} quota check is busy for this tenant, retry",
            )
        try:
            yield
        finally:
            # rollback ends the txn → releases the xact advisory lock.
            await lock_session.rollback()


async def _try_acquire(session: AsyncSession, key: str) -> bool:
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
        {"cid": _TENANT_RESOURCE_LOCK_CLASSID, "k": key},
    )
    return bool(result.scalar_one())
