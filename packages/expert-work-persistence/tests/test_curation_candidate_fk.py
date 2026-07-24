"""Integration: 0135 curation_candidate 悬空回退 + eval_dataset FK RESTRICT.

删除接口卫生修复第 3 批 Task 8。``curation_candidate.eval_dataset_id``(0034)
历史上是裸 UUID 列、无 FK —— 删 ``eval_dataset`` 父行后 PROMOTED candidate
永久悬空(无回退路径)。0135 一次性回退存量悬空 PROMOTED(status 回
``pending`` + 清指针)、清其余悬空指针,再补 ``ON DELETE RESTRICT`` FK 作
DB 级兜底;store 层把 RESTRICT 触发的 ``IntegrityError`` 语义化成
``EvalDatasetInUseError``(端点映射 409,姿态同
``test_mcp_catalog_oauth_fk_restrict.py``)。

测试策略说明(同 ``test_orphan_run_delivery_cleanup.py``):
``postgres_container`` 是 session 级共享容器,schema 早已在空数据上迁到
head —— 0135 的数据修正对后插入的数据不会再跑,且 head 状态下 FK 已生效、
悬空行根本插不进去。故数据修正测试在**一个最终回滚的事务**里:先 DROP 该
FK、手工插入悬空数据、经 alembic ``ScriptDirectory`` 加载 0135 模块直接执行
其模块级 UPDATE 常量(单一事实源,迁移文本变异测试必然看得见),断言后
ROLLBACK —— 共享容器的约束与数据原样恢复。

变异自验(手工执行,未固化进 CI):把 0135 里第一条 UPDATE 的
``NOT EXISTS`` 改成 ``EXISTS`` 重跑 —— 测试变红(非悬空 PROMOTED 被误回退,
``live_promoted`` 的保留断言失败);改回后复绿。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    EvalDatasetInUseError,
    SqlCurationCandidateStore,
    SqlEvalDatasetStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import CandidateStatus, CurationCandidateRecord, EvalDatasetRecord

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_REVISION = "0135_curation_candidate_fk"
_FK_NAME = "fk_curation_candidate_eval_dataset"
_BASE = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _upgraded_config(container: PostgresContainer) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(container))
    command.upgrade(cfg, "head")
    return cfg


def _insert_dataset(conn: Connection, *, dataset_id: UUID, tenant_id: UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO eval_dataset "
            "(id, tenant_id, agent_name, name, input, expected, source, created_at, updated_at) "
            "VALUES (:id, :tid, 'reporter', 'fk-probe-set', '{}'::jsonb, "
            "'{\"answer\": \"ok\"}'::jsonb, 'golden', now(), now())"
        ),
        {"id": dataset_id, "tid": tenant_id},
    )


def _insert_candidate(
    conn: Connection,
    *,
    candidate_id: UUID,
    tenant_id: UUID,
    status: str,
    eval_dataset_id: UUID | None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO curation_candidate "
            "(id, tenant_id, agent_name, thread_id, trajectory_key, outcome, signal,"
            " status, eval_dataset_id, detected_at, reviewed_at) "
            "VALUES (:id, :tid, 'reporter', :thread, :traj, 'failed', 'failed_outcome',"
            " :status, :ds, now(), now())"
        ),
        {
            "id": candidate_id,
            "tid": tenant_id,
            "thread": uuid4(),
            "traj": f"trajectories/{uuid4()}.jsonl",
            "status": status,
            "ds": eval_dataset_id,
        },
    )


def test_dangling_candidates_reverted_and_pointers_cleared(
    postgres_container: PostgresContainer,
) -> None:
    """悬空 PROMOTED 回退 PENDING + 清指针;悬空 DISMISSED 只清指针;非悬空不动."""
    cfg = _upgraded_config(postgres_container)

    # RED gate: raises if migration 0135 does not exist yet.
    migration = ScriptDirectory.from_config(cfg).get_revision(_REVISION).module

    tenant_id = uuid4()
    live_ds = uuid4()
    dangling_promoted = uuid4()
    dangling_dismissed = uuid4()
    live_promoted = uuid4()

    engine = create_engine(_sync_dsn(postgres_container))
    try:
        # Single transaction, rolled back at the end — the shared session
        # container gets its FK (and pristine data) back no matter what.
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE curation_candidate DROP CONSTRAINT {_FK_NAME}"))
            _insert_dataset(conn, dataset_id=live_ds, tenant_id=tenant_id)
            # ① Dangling PROMOTED — its dataset no longer exists.
            _insert_candidate(
                conn,
                candidate_id=dangling_promoted,
                tenant_id=tenant_id,
                status="promoted",
                eval_dataset_id=uuid4(),
            )
            # ② Dangling DISMISSED with a stale pointer.
            _insert_candidate(
                conn,
                candidate_id=dangling_dismissed,
                tenant_id=tenant_id,
                status="dismissed",
                eval_dataset_id=uuid4(),
            )
            # ③ Live PROMOTED — points at an existing dataset; must not move.
            _insert_candidate(
                conn,
                candidate_id=live_promoted,
                tenant_id=tenant_id,
                status="promoted",
                eval_dataset_id=live_ds,
            )

            # Execute the migration's UPDATE bodies — single source of truth.
            conn.execute(text(migration._REVERT_DANGLING_PROMOTED_SQL))
            conn.execute(text(migration._CLEAR_DANGLING_POINTER_SQL))

            rows = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    text(
                        "SELECT id, status, eval_dataset_id FROM curation_candidate "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": [dangling_promoted, dangling_dismissed, live_promoted]},
                )
            }
            conn.rollback()
    finally:
        engine.dispose()

    assert rows[dangling_promoted] == ("pending", None), (
        "dangling PROMOTED must revert to PENDING with its pointer cleared"
    )
    assert rows[dangling_dismissed] == ("dismissed", None), (
        "dangling DISMISSED must keep its status and only lose the pointer"
    )
    assert rows[live_promoted] == ("promoted", live_ds), (
        "a PROMOTED candidate pointing at a live dataset must be untouched"
    )


def test_fk_blocks_direct_sql_delete_of_referenced_dataset(
    postgres_container: PostgresContainer,
) -> None:
    """FK 生效:绕过 app 层直接 SQL 删被引用 dataset → ForeignKeyViolation."""
    _upgraded_config(postgres_container)

    tenant_id = uuid4()
    dataset_id = uuid4()

    engine = create_engine(_sync_dsn(postgres_container))
    try:
        with engine.connect() as conn:
            _insert_dataset(conn, dataset_id=dataset_id, tenant_id=tenant_id)
            _insert_candidate(
                conn,
                candidate_id=uuid4(),
                tenant_id=tenant_id,
                status="promoted",
                eval_dataset_id=dataset_id,
            )
            # RESTRICT is non-deferrable: the violation fires on the DELETE
            # statement itself.
            with pytest.raises(IntegrityError):
                conn.execute(text("DELETE FROM eval_dataset WHERE id = :id"), {"id": dataset_id})
            conn.rollback()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_store_delete_maps_restrict_to_in_use_error(
    postgres_container: PostgresContainer,
) -> None:
    """store 兜底:RESTRICT 触发的 IntegrityError → EvalDatasetInUseError,行保留."""
    _upgraded_config(postgres_container)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    sf = create_async_session_factory(engine)
    datasets = SqlEvalDatasetStore(sf)
    candidates = SqlCurationCandidateStore(sf)
    try:
        tenant_id = uuid4()
        dataset = EvalDatasetRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            agent_name="reporter",
            name="fk-probe-set",
            input={"prompt": "hello"},
            expected={"answer": "ok"},
            source="golden",
            created_at=_BASE,
            updated_at=_BASE,
        )
        await datasets.create(dataset)
        await candidates.upsert(
            CurationCandidateRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                agent_name="reporter",
                thread_id=uuid4(),
                trajectory_key=f"trajectories/{uuid4()}.jsonl",
                outcome="failed",
                signal="failed_outcome",
                status=CandidateStatus.PROMOTED,
                eval_dataset_id=dataset.id,
                detected_at=_BASE,
                reviewed_at=_BASE,
            )
        )

        # App-level guard bypassed — a direct store.delete() must surface the
        # RESTRICT violation as the semantic in-use error, not a raw
        # IntegrityError (and never silently cascade).
        with pytest.raises(EvalDatasetInUseError):
            await datasets.delete(dataset_id=dataset.id, tenant_id=tenant_id)

        # The dataset row survives the failed delete attempt.
        assert await datasets.get(dataset_id=dataset.id, tenant_id=tenant_id) is not None

        # Reverting the promoted candidate clears the reference and unblocks.
        reverted = await candidates.revert_promoted_for_dataset(
            dataset_id=dataset.id, tenant_id=tenant_id
        )
        assert reverted == 1
        assert await datasets.delete(dataset_id=dataset.id, tenant_id=tenant_id) is True
    finally:
        await engine.dispose()
