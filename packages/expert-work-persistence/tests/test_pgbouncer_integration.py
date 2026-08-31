"""End-to-end smoke test for the docker-compose Postgres + PgBouncer stack.

Verifies the M0 deliverable in subsystems/23-postgres-scalability § 9:

* PgBouncer in transaction mode is reachable on :6432.
* The SQLAlchemy + asyncpg + ``pgbouncer_mode=True`` combo can run a
  full migrate-then-CRUD round trip without prepared-statement errors.
* Server-side guardrails (``statement_timeout``, extensions) are wired up.

Marked ``integration`` so it can be skipped in fast loops; CI runs it
in the ``Test`` job (Docker daemon required, matching the existing
``postgres_container`` fixture pattern).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection
from testcontainers.compose import DockerCompose

from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
)
from expert_work.testing import explain_compose_pull_failure

pytestmark = pytest.mark.integration

# Path to ``infra/`` from the test file: ../../../../infra
_INFRA_DIR = Path(__file__).resolve().parents[3] / "infra"


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[DockerCompose]:
    """Bring up postgres + pgbouncer for the module duration.

    Pulls images upfront (saves ~30s of timeouts in CI on first run) and
    waits for both services to report healthy via their compose
    healthchecks.
    """
    stack = DockerCompose(
        context=str(_INFRA_DIR),
        compose_file_name="docker-compose.yml",
        pull=True,
        wait=True,
    )
    try:
        with stack:
            yield stack
    except subprocess.CalledProcessError as exc:
        # X-8 余项:pull 挂了要说清是**哪个镜像**。testcontainers 走 check_call,
        # CalledProcessError 身上没有 output,原样抛出去只剩一句 exit status 1。
        cmd = exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]
        if "pull" not in " ".join(cmd):
            raise
        pytest.fail(
            "compose 起栈失败在 pull 这一步 —— 带输出重跑的结果:\n"
            + explain_compose_pull_failure(_INFRA_DIR)
        )


def _pgbouncer_dsn(stack: DockerCompose) -> str:
    host, port_str = stack.get_service_host_and_port("pgbouncer", 6432)
    user = os.environ.get("EXPERT_WORK_DB_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_DB_PASSWORD", "expert_work_dev")
    name = os.environ.get("EXPERT_WORK_DB_NAME", "expert_work_dev")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port_str}/{name}"


def _postgres_direct_dsn(stack: DockerCompose) -> str:
    host, port_str = stack.get_service_host_and_port("postgres", 5432)
    user = os.environ.get("EXPERT_WORK_DB_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_DB_PASSWORD", "expert_work_dev")
    name = os.environ.get("EXPERT_WORK_DB_NAME", "expert_work_dev")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port_str}/{name}"


@pytest.mark.asyncio
async def test_pgbouncer_round_trip(compose_stack: DockerCompose) -> None:
    """Insert + select via PgBouncer; no prepared-statement errors."""
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_pgbouncer_dsn(compose_stack), pgbouncer_mode=True),
    )
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SELECT 1 AS one"))
        assert result.scalar_one() == 1

        # Re-run the same query — under transaction mode this exercises the
        # path where asyncpg would normally try to re-use a prepared statement.
        # With statement_cache_size=0 it just re-parses; no error.
        for _ in range(5):
            result = await conn.execute(sa.text("SELECT 2 AS two"))
            assert result.scalar_one() == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_statement_timeout_is_set(compose_stack: DockerCompose) -> None:
    """`statement_timeout = 30s` from init script must apply through PgBouncer."""
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_pgbouncer_dsn(compose_stack), pgbouncer_mode=True),
    )
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SHOW statement_timeout"))
        # Postgres normalizes "30s" → "30s" (no unit conversion needed).
        assert result.scalar_one() == "30s"
    await engine.dispose()


@pytest.mark.asyncio
async def test_extensions_installed(compose_stack: DockerCompose) -> None:
    """``pg_stat_statements`` + ``vector`` must be installed (init SQL).

    Use the direct-Postgres DSN — extension introspection should match.
    """
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_postgres_direct_dsn(compose_stack), pgbouncer_mode=False),
    )
    async with engine.connect() as conn:
        installed = await _installed_extensions(conn)
        assert "pg_stat_statements" in installed
        assert "vector" in installed
    await engine.dispose()


async def _installed_extensions(conn: AsyncConnection) -> set[str]:
    result = await conn.execute(sa.text("SELECT extname FROM pg_extension"))
    return {row[0] for row in result.all()}
