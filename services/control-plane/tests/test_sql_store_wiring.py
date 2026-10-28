"""Unit tests for the ADR B-6 SQL store cutover wiring.

The ``Sql*Store`` classes are integration-tested against a real Postgres
in ``packages/expert-work-persistence/tests/test_sql_*.py``. Here we cover the
control-plane side: the ``store_backend`` setting and the ``create_app``
branch that swaps every store to its Postgres-backed implementation off
one RLS-wrapped sessionmaker. ``create_async_engine`` is lazy, so these
assertions need no database.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from control_plane.app import create_app
from control_plane.settings import Settings
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore, SqlAgentSpecStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore, SqlAuditLogStore
from expert_work.persistence.auth import (
    InMemoryApiKeyStore,
    InMemoryRoleBindingStore,
    InMemoryServiceAccountStore,
    SqlApiKeyStore,
    SqlRoleBindingStore,
    SqlServiceAccountStore,
)
from expert_work.persistence.feedback_store import DbFeedbackStore, InMemoryFeedbackStore
from expert_work.persistence.knowledge import (
    InMemoryKnowledgeRetrievalLogStore,
    SqlKnowledgeRetrievalLogStore,
)
from expert_work.persistence.quota import (
    InMemoryTenantQuotaStore,
    InMemoryTokenReservationStore,
    SqlTenantQuotaStore,
    SqlTokenReservationStore,
)
from expert_work.persistence.tenant_config import (
    InMemoryTenantConfigStore,
    SqlTenantConfigStore,
)
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore, SqlThreadMetaStore
from tests.auth_fixtures import build_test_jwt_verifier


def _sql_settings() -> Settings:
    """Settings with the SQL backend on. The DSN is never connected to —
    ``create_async_engine`` opens no socket until first use."""
    return Settings(
        store_backend="sql",
        db_dsn="postgresql+asyncpg://expert_work@localhost:6432/expert_work",
    )


def test_store_backend_defaults_to_memory() -> None:
    assert Settings().store_backend == "memory"


def test_memory_backend_uses_in_memory_stores() -> None:
    app = create_app(
        settings=Settings(store_backend="memory"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    assert app.state.db_engine is None
    assert isinstance(app.state.agent_spec_repo, InMemoryAgentSpecStore)
    assert isinstance(app.state.thread_meta_repo, InMemoryThreadMetaStore)
    assert isinstance(app.state.feedback_store, InMemoryFeedbackStore)
    assert isinstance(app.state.service_account_repo, InMemoryServiceAccountStore)
    assert isinstance(app.state.api_key_repo, InMemoryApiKeyStore)
    assert isinstance(app.state.role_binding_repo, InMemoryRoleBindingStore)
    assert isinstance(app.state.tenant_quota_repo, InMemoryTenantQuotaStore)
    assert isinstance(app.state.token_reservation_repo, InMemoryTokenReservationStore)
    assert isinstance(app.state.tenant_config_repo, InMemoryTenantConfigStore)
    assert isinstance(app.state.audit_logger._store, InMemoryAuditLogStore)
    assert isinstance(app.state.knowledge_retrieval_log_store, InMemoryKnowledgeRetrievalLogStore)


@pytest.mark.asyncio
async def test_sql_backend_wires_every_store() -> None:
    """``store_backend='sql'`` swaps all ten stores to their Sql* impl."""
    app = create_app(
        settings=_sql_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    try:
        assert isinstance(app.state.db_engine, AsyncEngine)
        assert isinstance(app.state.agent_spec_repo, SqlAgentSpecStore)
        assert isinstance(app.state.thread_meta_repo, SqlThreadMetaStore)
        assert isinstance(app.state.feedback_store, DbFeedbackStore)
        assert isinstance(app.state.service_account_repo, SqlServiceAccountStore)
        assert isinstance(app.state.api_key_repo, SqlApiKeyStore)
        assert isinstance(app.state.role_binding_repo, SqlRoleBindingStore)
        assert isinstance(app.state.tenant_quota_repo, SqlTenantQuotaStore)
        assert isinstance(app.state.token_reservation_repo, SqlTokenReservationStore)
        assert isinstance(app.state.tenant_config_repo, SqlTenantConfigStore)
        assert isinstance(app.state.audit_logger._store, SqlAuditLogStore)
        # B-175 P2-PR4.
        assert isinstance(app.state.knowledge_retrieval_log_store, SqlKnowledgeRetrievalLogStore)
    finally:
        await app.state.db_engine.dispose()


@pytest.mark.asyncio
async def test_injected_repo_overrides_sql_backend() -> None:
    """An explicitly injected repo wins over the ``sql`` backend; the
    non-injected stores still pick it up."""
    injected = InMemoryAgentSpecStore()
    app = create_app(
        settings=_sql_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        agent_spec_repo=injected,
    )
    try:
        assert app.state.agent_spec_repo is injected
        assert isinstance(app.state.thread_meta_repo, SqlThreadMetaStore)
    finally:
        await app.state.db_engine.dispose()
