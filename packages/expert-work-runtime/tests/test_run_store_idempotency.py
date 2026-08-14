"""幂等键:同租户同键只能有一行,并发第二插必须抛冲突。

Parametrized over both ``RunStore`` backends — the idempotency predicate
must be byte-identical between the SQL partial unique index and the
in-memory double's dup check (External-API-v1 P2 block 1-C).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunIdempotencyConflict,
    RunInfo,
    RunStatus,
    RunStore,
    SqlRunStore,
)

PERSISTENCE_ROOT = Path(__file__).resolve().parents[2] / "expert-work-persistence"
ALEMBIC_INI = PERSISTENCE_ROOT / "alembic.ini"

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)

#: The idempotency partial unique index's name, quoted exactly the way
#: Postgres embeds a constraint name in its error text. A caller sending
#: this as the literal ``Idempotency-Key`` value is the regression probe
#: for the free-text-matching bug fixed in review (see
#: ``test_check_violation_with_lookalike_key_is_not_misclassified``).
_INDEX_NAME_LOOKALIKE_KEY = '"uq_agent_run_tenant_idempotency_key"'


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _build_sql_run_store(container: PostgresContainer) -> SqlRunStore:
    """Migrate ``container`` to head and wire a ``SqlRunStore`` onto it.

    Shared by the parametrized ``run_store`` fixture and the SQL-only
    regression tests below (CHECK-violation misclassification, real
    concurrency) that need a raw ``SqlRunStore`` without the ``memory``
    parametrization — those two properties don't exist on the in-memory
    double (no CHECK constraints, no real cross-connection races).
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(container)))
    return SqlRunStore(create_async_session_factory(engine))


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
def run_store(request: pytest.FixtureRequest) -> Iterator[RunStore]:
    """Both ``RunStore`` backends. The ``sql`` param is dynamically marked
    ``integration`` (needs Docker) so ``pytest -m "not integration"`` still
    exercises the ``memory`` param without a container.
    """
    if request.param == "memory":
        yield InMemoryRunStore()
        return

    container: PostgresContainer = request.getfixturevalue("postgres_container")
    yield _build_sql_run_store(container)


@pytest.fixture
def make_run_info() -> Callable[..., RunInfo]:
    def _make(
        *,
        tenant_id: UUID,
        idempotency_key: str | None,
        request_digest: str | None = None,
        run_id: UUID | None = None,
        thread_id: UUID | None = None,
    ) -> RunInfo:
        return RunInfo(
            run_id=run_id or uuid4(),
            tenant_id=tenant_id,
            thread_id=thread_id or uuid4(),
            user_id=None,
            status=RunStatus.PENDING,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=_BASE,
            updated_at=_BASE,
            finished_at=None,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )

    return _make


@pytest.mark.asyncio
async def test_same_key_second_insert_conflicts(
    run_store: RunStore, make_run_info: Callable[..., RunInfo]
) -> None:
    tenant = uuid4()
    await run_store.create(
        make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1")
    )
    with pytest.raises(RunIdempotencyConflict):
        await run_store.create(
            make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1")
        )


@pytest.mark.asyncio
async def test_same_key_different_tenant_is_allowed(
    run_store: RunStore, make_run_info: Callable[..., RunInfo]
) -> None:
    await run_store.create(
        make_run_info(tenant_id=uuid4(), idempotency_key="k1", request_digest="d1")
    )
    await run_store.create(
        make_run_info(tenant_id=uuid4(), idempotency_key="k1", request_digest="d1")
    )


@pytest.mark.asyncio
async def test_null_key_rows_do_not_collide(
    run_store: RunStore, make_run_info: Callable[..., RunInfo]
) -> None:
    """部分唯一索引只覆盖非 NULL —— 不带 key 的普通 run 必须能建任意多个。"""
    tenant = uuid4()
    for _ in range(3):
        await run_store.create(make_run_info(tenant_id=tenant, idempotency_key=None))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_index_where_clause_is_present(postgres_container: PostgresContainer) -> None:
    """Review finding — Postgres unique indexes default to ``NULLS
    DISTINCT`` (multiple NULL rows are already allowed even on a
    non-partial unique index), so ``[sql]``'s half of
    ``test_null_key_rows_do_not_collide`` would still pass even if the
    migration's ``WHERE idempotency_key IS NOT NULL`` partial clause were
    silently dropped — only the ``[memory]`` half actually exercises that
    property. This asserts the ``WHERE`` clause directly against
    ``pg_indexes`` so a regression in the migration is caught even though
    the behavioural test above cannot catch it.
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": "uq_agent_run_tenant_idempotency_key"},
        )
        indexdef = result.scalar_one()
    assert "WHERE" in indexdef and "idempotency_key IS NOT NULL" in indexdef


@pytest.mark.asyncio
async def test_find_by_key(run_store: RunStore, make_run_info: Callable[..., RunInfo]) -> None:
    tenant = uuid4()
    info = make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1")
    await run_store.create(info)
    got = await run_store.find_by_idempotency_key(tenant_id=tenant, key="k1")
    assert got is not None and got.run_id == info.run_id and got.request_digest == "d1"
    assert await run_store.find_by_idempotency_key(tenant_id=tenant, key="nope") is None


class _BogusStatus:
    """A ``status`` stand-in whose ``.value`` violates
    ``agent_run_status_valid`` — not a real :class:`RunStatus` member (the
    enum itself refuses an unknown value at construction), but it satisfies
    ``info.status.value`` exactly the way ``SqlRunStore.create`` reads it,
    so the bogus value travels through the *real* ``create`` code path and
    Postgres itself raises the CHECK violation, rather than the test faking
    an ``IntegrityError`` by hand.
    """

    value = "not_a_real_status"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_violation_with_lookalike_key_is_not_misclassified(
    postgres_container: PostgresContainer, make_run_info: Callable[..., RunInfo]
) -> None:
    """Regression (review finding) — a CHECK-constraint failure must never
    be misclassified as an idempotency conflict, even when the caller's
    ``Idempotency-Key`` happens to look exactly like the partial index's
    constraint name.

    Postgres echoes the *entire failing row* into ``DETAIL: Failing row
    contains (...)`` on every constraint violation — including whatever the
    caller put in ``idempotency_key``. Matching that free text against the
    index name (instead of the structured ``constraint_name`` one hop down
    on ``exc.orig.__cause__``) would let any caller who sends
    ``Idempotency-Key: "uq_agent_run_tenant_idempotency_key"`` turn an
    unrelated CHECK/NOT NULL failure into a silently-wrong "idempotency
    conflict" — the failed row was never written, so a later
    ``find_by_idempotency_key`` lookup returns ``None`` and a real failure
    gets swallowed as if it were a harmless replay.
    """
    store = _build_sql_run_store(postgres_container)
    tenant = uuid4()
    info = make_run_info(tenant_id=tenant, idempotency_key=_INDEX_NAME_LOOKALIKE_KEY)
    object.__setattr__(info, "status", _BogusStatus())

    with pytest.raises(IntegrityError) as exc_info:
        await store.create(info)
    assert not isinstance(exc_info.value, RunIdempotencyConflict)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_create_same_key_exactly_one_winner(
    postgres_container: PostgresContainer, make_run_info: Callable[..., RunInfo]
) -> None:
    """Real concurrency (review finding) — the docstring says "并发第二插必须
    抛冲突", but every other test in this file proves it only with two
    *sequential* inserts, which cannot distinguish the atomic-insert design
    from a racy check-then-create. This drives 8 independent sessions at the
    same ``(tenant_id, idempotency_key)`` via ``asyncio.gather`` — real
    concurrent connections, not sequential awaits — and asserts exactly one
    winner, the rest ``RunIdempotencyConflict``, and that the winner is the
    row ``find_by_idempotency_key`` actually returns.
    """
    store = _build_sql_run_store(postgres_container)
    tenant = uuid4()
    key = "concurrent-k1"
    run_ids = [uuid4() for _ in range(8)]

    async def _attempt(run_id: UUID) -> UUID | RunIdempotencyConflict:
        info = make_run_info(tenant_id=tenant, idempotency_key=key, run_id=run_id)
        try:
            await store.create(info)
        except RunIdempotencyConflict as exc:
            return exc
        return run_id

    results = await asyncio.gather(*(_attempt(run_id) for run_id in run_ids))
    winners = [r for r in results if isinstance(r, UUID)]
    conflicts = [r for r in results if isinstance(r, RunIdempotencyConflict)]
    assert len(winners) == 1
    assert len(conflicts) == 7

    found = await store.find_by_idempotency_key(tenant_id=tenant, key=key)
    assert found is not None
    assert found.run_id == winners[0]
