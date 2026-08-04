"""Integration tests for ``SqlSandboxInstanceStore`` against a real Postgres

(波 1 Task 7 — Agent Sandbox warm-session CAS, migration 0141).

The concurrent-claim test is the one thing an in-memory fake structurally
cannot verify: whether the ``sandbox_instance (tenant_id, user_id) WHERE
state = 'IN_USE' AND destroyed_at IS NULL AND user_id IS NOT NULL`` partial
unique index (migration 0141) actually serialises two real, simultaneous
transactions into exactly one winner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.sandbox_instance_store import SqlSandboxInstanceStore

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def store(postgres_container: PostgresContainer) -> Iterator[SqlSandboxInstanceStore]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    factory = create_async_session_factory(engine)
    yield SqlSandboxInstanceStore(factory)


@pytest.mark.asyncio
async def test_claim_warm_first_caller_wins(store: SqlSandboxInstanceStore) -> None:
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()

    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)

    assert result is None
    assert await store.get_container_id(sandbox_id=sandbox_id) is None


@pytest.mark.asyncio
async def test_claim_warm_second_caller_sees_ready_container(
    store: SqlSandboxInstanceStore,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None
    await store.set_container_id(sandbox_id=first_id, container_id="sbx-ready")

    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)

    # Review fix (Important-3): the loser gets back the WINNER's real row
    # id (first_id), not its own second_id — acquire() needs a persisted id
    # to hand its caller, and second_id was never inserted anywhere.
    assert result == (first_id, "sbx-ready")
    # The loser's own row was never inserted (its INSERT conflicted).
    assert await store.get_container_id(sandbox_id=second_id) is None


@pytest.mark.asyncio
async def test_claim_warm_second_caller_raises_when_winner_not_ready(
    store: SqlSandboxInstanceStore,
) -> None:
    """task-7-report.md's correction to the brief/design-spec prose: a
    conflicting claim whose owner hasn't finished ``create()`` yet
    (``container_id`` still NULL — E2B cold start is 35-40s, a wide,
    common window) must fail loudly, not hand back an unusable value or
    let the caller silently build a second sandbox for this session.
    """
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None
    # Deliberately do NOT call set_container_id — simulates "still creating".

    with pytest.raises(RuntimeError, match="already being created"):
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())


@pytest.mark.asyncio
async def test_concurrent_claim_warm_exactly_one_winner(store: SqlSandboxInstanceStore) -> None:
    """The real-concurrency case Task 7's brief calls out: two coroutines
    ``claim_warm`` the SAME ``(tenant, user)`` at once, only one may win the
    partial unique index. Neither has created anything yet, so per
    ``SqlSandboxInstanceStore.claim_warm``'s documented contract the loser
    observes a not-ready claim and raises — the in-memory fake cannot prove
    this (it never races two real transactions against the same DB
    constraint), only a real Postgres can.
    """
    tenant_id, user_id = uuid4(), uuid4()
    sandbox_a, sandbox_b = uuid4(), uuid4()

    results = await asyncio.gather(
        store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_a),
        store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_b),
        return_exceptions=True,
    )

    winners = [r for r in results if r is None]
    losers = [r for r in results if isinstance(r, RuntimeError)]
    assert len(winners) == 1, f"exactly one caller should win the claim, got {results!r}"
    assert len(losers) == 1, f"the other should observe a not-ready claim, got {results!r}"
    # No stray winner-row leaked from the losing attempt.
    won_id = sandbox_a if results[0] is None else sandbox_b
    lost_id = sandbox_b if won_id is sandbox_a else sandbox_a
    assert await store.get_container_id(sandbox_id=won_id) is None
    assert await store.get_container_id(sandbox_id=lost_id) is None


@pytest.mark.asyncio
async def test_concurrent_claim_warm_after_ready_all_see_same_winner(
    store: SqlSandboxInstanceStore,
) -> None:
    """Once a warm session is ready, N concurrent late-arriving claims must
    all resolve to the SAME container_id — no duplicate row, no crash."""
    tenant_id, user_id = uuid4(), uuid4()
    winner_id = uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=winner_id)
    assert won is None
    await store.set_container_id(sandbox_id=winner_id, container_id="sbx-warm")

    results = await asyncio.gather(
        *(
            store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())
            for _ in range(5)
        )
    )

    assert results == [(winner_id, "sbx-warm")] * 5


@pytest.mark.asyncio
async def test_drop_warm_frees_slot_for_new_claim(store: SqlSandboxInstanceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None

    await store.drop_warm(tenant_id=tenant_id, user_id=user_id)

    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)
    assert result is None, "dropping the dead claim must free the 0141 partial-index slot"


@pytest.mark.asyncio
async def test_mark_destroyed_frees_slot_for_new_claim(store: SqlSandboxInstanceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None
    await store.set_container_id(sandbox_id=first_id, container_id="sbx-1")

    await store.mark_destroyed(sandbox_id=first_id, reason="ops")

    assert await store.get_container_id(sandbox_id=first_id) == "sbx-1", (
        "mark_destroyed keeps the historical container_id — only state/"
        "destroyed_at/destroy_reason transition"
    )
    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)
    assert result is None, "destroying the old session must free the slot for a fresh claim"


@pytest.mark.asyncio
async def test_get_container_id_unknown_sandbox_returns_none(
    store: SqlSandboxInstanceStore,
) -> None:
    assert await store.get_container_id(sandbox_id=uuid4()) is None
