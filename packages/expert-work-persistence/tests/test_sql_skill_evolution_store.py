"""Integration: SkillStore evolution API on real Postgres — Stream SE (SE-2).

Pins on a real PG (migration 0065 applied) that:

* the ownership/lineage columns (``visibility`` / ``created_by_user_id`` /
  ``created_by_agent_name`` / ``forked_from``) and evolution-provenance columns
  (``evolution_origin`` / ``distilled_from_*`` / ``evolution_round``)
  round-trip through the SQL store;
* ``fork_skill`` copies the source's latest version into a new
  ``agent_private`` skill;
* ``skill_eval_result`` rows round-trip and are tenant-isolated by RLS
  (the SE-A2 evidence table must not leak across tenants).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlSkillStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.protocol import SkillEvalResult

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
APP_ROLE = "expert_work_app"
APP_PASSWORD = "expert_work_app_test_pw"  # test-only fixture password


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    new_netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _provision_app_role(sync_dsn: str) -> None:
    admin_engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            ).first()
            if exists is None:
                conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
            conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
    finally:
        admin_engine.dispose()


@pytest.fixture
def skill_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlSkillStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlSkillStore(sf), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


@pytest.mark.asyncio
async def test_evolution_columns_round_trip(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_store
    tenant, user_id, src, cand = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        current_tenant_id_var.set(tenant)
        sid = uuid4()
        await store.create_skill(
            skill_id=sid,
            tenant_id=tenant,
            name=f"agent-{uuid4().hex[:8]}",
            visibility="agent_private",
            created_by_user_id=user_id,
            created_by_agent_name="researcher",
            forked_from=src,
        )
        got = await store.get_skill(skill_id=sid, tenant_id=tenant)
        assert got is not None
        assert got.visibility == "agent_private"
        assert got.created_by_user_id == user_id
        assert got.created_by_agent_name == "researcher"
        assert got.forked_from == src

        v = await store.add_version(
            version_id=uuid4(),
            skill_id=sid,
            tenant_id=tenant,
            prompt_fragment="distilled body",
            authored_by="agent",
            evolution_origin="distilled",
            distilled_from_trajectory_key="t/abc.jsonl",
            distilled_from_candidate_id=cand,
            evolution_round=2,
        )
        assert v.evolution_origin == "distilled"
        assert v.distilled_from_candidate_id == cand
        assert v.evolution_round == 2
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_fork_skill_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_store
    tenant, user_id = uuid4(), uuid4()
    try:
        current_tenant_id_var.set(tenant)
        src = uuid4()
        await store.create_skill(skill_id=src, tenant_id=tenant, name=f"src-{uuid4().hex[:8]}")
        await store.add_version(
            version_id=uuid4(),
            skill_id=src,
            tenant_id=tenant,
            prompt_fragment="source body",
            tool_names=("web_search",),
        )
        new_sid, new_vid = uuid4(), uuid4()
        forked = await store.fork_skill(
            tenant_id=tenant,
            source_skill_id=src,
            new_name=f"fork-{uuid4().hex[:8]}",
            by_user_id=user_id,
            by_agent_name="my-agent",
            new_skill_id=new_sid,
            new_version_id=new_vid,
        )
        assert forked.visibility == "agent_private"
        assert forked.forked_from == src
        assert forked.latest_version == 1
        v = await store.get_version_by_number(skill_id=new_sid, tenant_id=tenant, version=1)
        assert v is not None and v.prompt_fragment == "source body"
        assert v.tool_names == ("web_search",)
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_eval_result_round_trip_and_tenant_isolation(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_store
    tenant_a, tenant_b = uuid4(), uuid4()
    skill_a = uuid4()
    try:
        current_tenant_id_var.set(tenant_a)
        # The eval row FK-references skill.id, so the skill must exist first.
        await store.create_skill(
            skill_id=skill_a, tenant_id=tenant_a, name=f"eval-{uuid4().hex[:8]}"
        )
        await store.record_eval_result(
            result=SkillEvalResult(
                id=uuid4(),
                tenant_id=tenant_a,
                skill_id=skill_a,
                skill_version=1,
                baseline_score=0.4,
                skill_score=0.85,
                delta=0.45,
                n_cases=12,
                replay_source="trajectory",
                verdict="pass",
                created_at=datetime.now(UTC),
            )
        )
        rows = await store.list_eval_results(skill_id=skill_a, tenant_id=tenant_a)
        assert len(rows) == 1
        assert rows[0].verdict == "pass"
        assert rows[0].delta == pytest.approx(0.45)

        # RLS: tenant B sees nothing for tenant A's skill.
        current_tenant_id_var.set(tenant_b)
        leaked = await store.list_eval_results(skill_id=skill_a, tenant_id=tenant_b)
        assert leaked == []
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


# ── SE-8-1: promote-approval flow (migration 0068) ────────────────────────


@pytest.mark.asyncio
async def test_promote_request_approve_flips_visibility_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_store
    tenant, user_id, admin = uuid4(), uuid4(), uuid4()
    try:
        current_tenant_id_var.set(tenant)
        sid = uuid4()
        await store.create_skill(
            skill_id=sid,
            tenant_id=tenant,
            name=f"priv-{uuid4().hex[:8]}",
            visibility="agent_private",
            created_by_user_id=user_id,
            created_by_agent_name="researcher",
        )
        await store.add_version(
            version_id=uuid4(),
            skill_id=sid,
            tenant_id=tenant,
            prompt_fragment="body",
            authored_by="agent",
        )
        rid = uuid4()
        req = await store.request_skill_promote(
            request_id=rid,
            tenant_id=tenant,
            skill_id=sid,
            skill_version=1,
            requested_by_user_id=user_id,
            requested_by_agent_name="researcher",
            reason="tenant-wide useful",
        )
        assert req.status == "pending"

        decided = await store.approve_skill_promote(
            request_id=rid, tenant_id=tenant, decided_by_user_id=admin, decision_reason="ok"
        )
        assert decided.status == "approved" and decided.decided_by_user_id == admin
        # Visibility flipped agent_private→tenant atomically.
        skill = await store.get_skill(skill_id=sid, tenant_id=tenant)
        assert skill is not None and skill.visibility == "tenant"
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_promote_request_pending_uniqueness_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    from expert_work.persistence.skill.base import DuplicatePromoteRequestError

    store, engine = skill_store
    tenant = uuid4()
    try:
        current_tenant_id_var.set(tenant)
        sid = uuid4()
        await store.create_skill(
            skill_id=sid, tenant_id=tenant, name=f"u-{uuid4().hex[:8]}", visibility="agent_private"
        )
        await store.add_version(
            version_id=uuid4(), skill_id=sid, tenant_id=tenant, prompt_fragment="b"
        )
        await store.request_skill_promote(
            request_id=uuid4(), tenant_id=tenant, skill_id=sid, skill_version=1
        )
        with pytest.raises(DuplicatePromoteRequestError):
            await store.request_skill_promote(
                request_id=uuid4(), tenant_id=tenant, skill_id=sid, skill_version=1
            )
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_promote_request_tenant_isolation_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_store
    tenant_a, tenant_b = uuid4(), uuid4()
    try:
        current_tenant_id_var.set(tenant_a)
        sid = uuid4()
        await store.create_skill(
            skill_id=sid,
            tenant_id=tenant_a,
            name=f"iso-{uuid4().hex[:8]}",
            visibility="agent_private",
        )
        await store.add_version(
            version_id=uuid4(), skill_id=sid, tenant_id=tenant_a, prompt_fragment="b"
        )
        rid = uuid4()
        await store.request_skill_promote(
            request_id=rid, tenant_id=tenant_a, skill_id=sid, skill_version=1
        )
        rows_a, _ = await store.list_promote_requests(tenant_id=tenant_a, status="pending")
        assert [r.id for r in rows_a] == [rid]

        # RLS: tenant B sees nothing.
        current_tenant_id_var.set(tenant_b)
        rows_b, _ = await store.list_promote_requests(tenant_id=tenant_b, status="pending")
        assert rows_b == []
        assert await store.get_promote_request(request_id=rid, tenant_id=tenant_b) is None
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_promote_requests_skill_visibility_filters_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    """Backlog task 8 — SQL-backend parity with the in-memory store's
    ``skill_visibility`` filter (a join to the target skill's row, not a
    column on the request itself)."""
    store, engine = skill_store
    tenant = uuid4()
    try:
        current_tenant_id_var.set(tenant)
        sid_priv = uuid4()
        await store.create_skill(
            skill_id=sid_priv,
            tenant_id=tenant,
            name=f"priv-{uuid4().hex[:8]}",
            visibility="agent_private",
        )
        await store.add_version(
            version_id=uuid4(), skill_id=sid_priv, tenant_id=tenant, prompt_fragment="b"
        )
        sid_pub = uuid4()
        await store.create_skill(skill_id=sid_pub, tenant_id=tenant, name=f"pub-{uuid4().hex[:8]}")
        await store.add_version(
            version_id=uuid4(), skill_id=sid_pub, tenant_id=tenant, prompt_fragment="b"
        )
        rid_priv, rid_pub = uuid4(), uuid4()
        await store.request_skill_promote(
            request_id=rid_priv, tenant_id=tenant, skill_id=sid_priv, skill_version=1
        )
        await store.request_skill_promote(
            request_id=rid_pub, tenant_id=tenant, skill_id=sid_pub, skill_version=1
        )

        filtered, _ = await store.list_promote_requests(tenant_id=tenant, skill_visibility="tenant")
        assert [r.id for r in filtered] == [rid_pub]

        unfiltered, _ = await store.list_promote_requests(tenant_id=tenant)
        assert {r.id for r in unfiltered} == {rid_priv, rid_pub}
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_promote_requests_cursor_survives_visibility_filter_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    """Backlog task 9 (I-1) — SQL-backend pin, parity with the in-memory
    store's ``test_list_promote_requests_cursor_survives_visibility_filter``.

    ``pub_req`` (tenant-visible) created first, ``priv_req``
    (``agent_private``) second — newest-first order is
    ``[priv_req, pub_req]``. Paginating with ``skill_visibility="tenant"``
    and ``cursor=priv_req.id`` must resolve ``priv_req``'s pivot (SQL's
    ``cur_stmt`` looks the cursor id up unfiltered, tenant-scoped only) and
    return the next tenant-visible row after it, not an empty page. SQL
    already got this right before I-1 (only the in-memory backend's cursor
    resolution was wrong); this test pins that correct behavior so a future
    change to ``_list_promote_requests`` can't quietly regress it without
    the two backends ever being compared directly.
    """
    store, engine = skill_store
    tenant = uuid4()
    try:
        current_tenant_id_var.set(tenant)
        sid_pub = uuid4()
        await store.create_skill(skill_id=sid_pub, tenant_id=tenant, name=f"pub-{uuid4().hex[:8]}")
        await store.add_version(
            version_id=uuid4(), skill_id=sid_pub, tenant_id=tenant, prompt_fragment="b"
        )
        rid_pub = uuid4()
        await store.request_skill_promote(
            request_id=rid_pub, tenant_id=tenant, skill_id=sid_pub, skill_version=1
        )

        sid_priv = uuid4()
        await store.create_skill(
            skill_id=sid_priv,
            tenant_id=tenant,
            name=f"priv-{uuid4().hex[:8]}",
            visibility="agent_private",
        )
        await store.add_version(
            version_id=uuid4(), skill_id=sid_priv, tenant_id=tenant, prompt_fragment="b"
        )
        rid_priv = uuid4()
        await store.request_skill_promote(
            request_id=rid_priv, tenant_id=tenant, skill_id=sid_priv, skill_version=1
        )

        items, cursor = await store.list_promote_requests(
            tenant_id=tenant, skill_visibility="tenant", cursor=rid_priv
        )
        assert [r.id for r in items] == [rid_pub]
        assert cursor is None
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


# ── SE-8-1: persistent kill-switch (migration 0068) ───────────────────────


@pytest.mark.asyncio
async def test_kill_switch_round_trip_and_halt_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_store
    tenant, admin = uuid4(), uuid4()
    try:
        # Tenant-scoped switch: engage under the tenant's RLS scope.
        current_tenant_id_var.set(tenant)
        sw = await store.set_kill_switch(
            switch_id=uuid4(),
            scope="tenant",
            tenant_id=tenant,
            engaged=True,
            reason="runaway",
            actor_user_id=admin,
        )
        assert sw.engaged is True and sw.engaged_by_user_id == admin
        got = await store.get_kill_switch(scope="tenant", tenant_id=tenant)
        assert got is not None and got.engaged is True
        assert await store.is_evolution_halted(tenant_id=tenant) is True

        # Upsert toggles the same row off.
        released = await store.set_kill_switch(
            switch_id=uuid4(), scope="tenant", tenant_id=tenant, engaged=False, actor_user_id=admin
        )
        assert released.id == sw.id and released.engaged is False
        assert await store.is_evolution_halted(tenant_id=tenant) is False
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()

    # Global switch (NULL-tenant row) requires the unscoped owner session;
    # under tenant scope the IS-NOT-DISTINCT-FROM policy hides NULL rows. The
    # in-process + SQL parity for the global OR tenant logic is covered by the
    # in-memory suite; here we assert the tenant-scoped path end-to-end.


# ── Batch category assignment (bulk_update_platform_skills) ───────────────


@pytest.mark.asyncio
async def test_bulk_set_and_clear_category_real_pg(
    skill_store: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    # Platform (NULL-tenant) rows — visible/writable under the unscoped
    # session that ``reset_rls`` (autouse, above) already puts every test
    # in this file into, same as the sibling
    # test_sql_mcp_connector_catalog_store.py platform-table suite.
    store, engine = skill_store
    try:
        a = await store.create_platform_skill(skill_id=uuid4(), name=f"a-{uuid4().hex[:8]}")
        b = await store.create_platform_skill(
            skill_id=uuid4(), name=f"b-{uuid4().hex[:8]}", category="old"
        )
        # set on ids
        n = await store.bulk_update_platform_skills(
            ids=[a.id], update_category=True, new_category="研发"
        )
        assert n == 1
        # clear
        m = await store.bulk_update_platform_skills(
            ids=[b.id], update_category=True, new_category=None
        )
        assert m == 1
        got_a = await store.get_platform_skill(skill_id=a.id)
        got_b = await store.get_platform_skill(skill_id=b.id)
        assert got_a is not None and got_a.category == "研发"
        assert got_b is not None and got_b.category is None
    finally:
        await engine.dispose()
