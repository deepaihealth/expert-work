"""B-267 —— the rollback helper lists the stored manifests that turn the
per-turn knowledge probe off (live or draft), and nothing else; ids only.

Before running locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from control_plane.knowledge.probe_opt_outs import OptOut, list_opt_outs
from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.agent_spec.sql import SqlAgentSpecStore
from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.protocol import AgentSpec

pytestmark = pytest.mark.integration

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_DB = "knowledge_probe_opt_outs"
_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "a", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "x"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
        "knowledge": {"knowledge_base_refs": ["kb"]},
    },
}


def _spec(name: str, *, probe: bool | None) -> AgentSpec:
    doc = deepcopy(_MANIFEST)
    doc["metadata"]["name"] = name
    if probe is not None:
        doc["spec"]["knowledge"]["probe"] = probe
    return AgentSpec.model_validate(doc)


@pytest.fixture
async def sessions(
    postgres_container: PostgresContainer,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield create_async_session_factory(engine)
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


async def test_only_opt_outs_are_listed(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAgentSpecStore(sessions)
    tenant = uuid4()
    for name, probe in (("off", False), ("on", True), ("plain", None), ("drafted", None)):
        spec = _spec(name, probe=probe)
        await store.create(
            tenant_id=tenant, spec=spec, spec_sha256=compute_spec_sha256(spec), created_by="u"
        )
    draft = _spec("drafted", probe=False)
    await store.save_draft(
        tenant_id=tenant,
        name="drafted",
        version="1.0.0",
        spec=draft,
        spec_sha256=compute_spec_sha256(draft),
        updated_by="u",
    )
    found = await list_opt_outs(sessions)
    assert sorted((o.draft, o.tenant_id) for o in found) == [(False, tenant), (True, tenant)]
    assert {o.table for o in found} == {"agent_spec"}
    line = OptOut("agent_spec", uuid4(), tenant, draft=True).line()
    assert line.startswith("table=agent_spec id=") and line.endswith("draft=true")
