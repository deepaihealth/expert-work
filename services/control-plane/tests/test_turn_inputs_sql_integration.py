"""``resolve_turn_inputs`` 在生产那一套 SQL store 上 —— 真 Postgres + Alembic schema。

三个 store 都是生产用的 SQL 实现(``SqlRunStore`` / ``SqlRunEventStore`` /
``SqlApprovalStore``)。内存版的行为由 ``test_turn_inputs_resolve.py`` 钉住;这里只证
只有真库才答得了的事:``run_event`` 的 JSONB 帧原样读回、``list_by_thread`` 的定序撑得住
审批链回溯、审批单经 ``mark_decided`` 落下的 ``continuation_run_id`` 读得回来、
``event_names`` 过滤在 SQL 上取得到 ``system_prompt`` 帧。

跑法::

    export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
    uv run --no-sync pytest \
        services/control-plane/tests/test_turn_inputs_sql_integration.py -m integration -q
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from control_plane.turn_inputs import TurnInputs, resolve_turn_inputs
from expert_work.persistence import (
    DatabaseConfig,
    SqlApprovalStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import ApprovalRecord, ApprovalStatus
from expert_work.runtime.runs import (
    DisconnectMode,
    RunInfo,
    RunStatus,
    SqlRunEventStore,
    SqlRunStore,
    make_event_record,
)
from orchestrator.sse import SYSTEM_PROMPT_EVENT

pytestmark = pytest.mark.integration

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_T0 = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)


def _dsn(container: PostgresContainer, driver: str) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", f"+{driver}").replace(
        "postgresql://", f"postgresql+{driver}://", 1
    )


def _row(run_id: UUID, *, tenant: UUID, thread: UUID, status: RunStatus, minute: int) -> RunInfo:
    at = _T0 + timedelta(minutes=minute)
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant,
        thread_id=thread,
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=minute > 0,
        error=None,
        created_at=at,
        updated_at=at,
        finished_at=None,
    )


@pytest.mark.asyncio
async def test_a_continuation_resolves_its_turns_inputs_from_postgres(
    postgres_container: PostgresContainer,
) -> None:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _dsn(postgres_container, "psycopg"))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_dsn(postgres_container, "asyncpg"))
    )
    try:
        factory = create_async_session_factory(engine)
        runs, events = SqlRunStore(factory), SqlRunEventStore(factory)
        approvals = SqlApprovalStore(factory)
        tenant, thread = uuid4(), uuid4()
        r0, c1 = uuid4(), uuid4()
        inputs = {"pc": "PRJ-7f3a-0192", "materials": [{"url": "https://x.example/a.png"}]}

        # 故意倒着建行:定序必须来自 created_at,而不是插入顺序。
        await runs.create(
            _row(c1, tenant=tenant, thread=thread, status=RunStatus.RUNNING, minute=1)
        )
        await runs.create(_row(r0, tenant=tenant, thread=thread, status=RunStatus.PAUSED, minute=0))
        await events.append(
            make_event_record(run_id=r0, seq=1, event_name="metadata", data={"run_id": str(r0)})
        )
        await events.append(
            make_event_record(
                run_id=r0,
                seq=2,
                event_name=SYSTEM_PROMPT_EVENT,
                data={"text": "项目 …", "inputs": inputs},
            )
        )
        await approvals.create(
            ApprovalRecord(
                id=uuid4(),
                tenant_id=tenant,
                run_id=r0,
                thread_id=thread,
                request_id="approval:sql",
                node="tools",
                reason_kind="policy_gate",
                action_summary="gated",
                requested_at=_T0,
                timeout_at=_T0 + timedelta(hours=24),
                status=ApprovalStatus.PENDING,
            )
        )
        # 与审批端点同一条写法:CAS 决策时原子地落下续跑 run_id。
        assert await approvals.mark_decided(
            run_id=r0,
            tenant_id=tenant,
            status=ApprovalStatus.APPROVED,
            decided_by="tester",
            decided_at=_T0,
            modified_args=None,
            continuation_run_id=c1,
        )

        got = await resolve_turn_inputs(
            run_id=c1,
            thread_id=thread,
            tenant_id=tenant,
            runs=runs,
            approvals=approvals,
            event_store=events,
        )
        assert got == TurnInputs(root_run_id=r0, inputs=inputs)
    finally:
        await engine.dispose()
