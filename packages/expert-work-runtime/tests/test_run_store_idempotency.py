"""幂等键:同租户同键只能有一行,并发第二插必须抛冲突。

Parametrized over both ``RunStore`` backends — the idempotency predicate
must be byte-identical between the SQL partial unique index and the
in-memory double's dup check (External-API-v1 P2 block 1-C).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
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


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


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
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(container)))
    yield SqlRunStore(create_async_session_factory(engine))


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


@pytest.mark.asyncio
async def test_find_by_key(run_store: RunStore, make_run_info: Callable[..., RunInfo]) -> None:
    tenant = uuid4()
    info = make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1")
    await run_store.create(info)
    got = await run_store.find_by_idempotency_key(tenant_id=tenant, key="k1")
    assert got is not None and got.run_id == info.run_id and got.request_digest == "d1"
    assert await run_store.find_by_idempotency_key(tenant_id=tenant, key="nope") is None
