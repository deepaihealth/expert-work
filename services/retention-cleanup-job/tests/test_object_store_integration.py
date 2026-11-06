"""B-286 —— 真库:作业用 control-plane 同一个 ``sql_encrypted`` SecretStore(``encrypted_secret`` 表 +
``EXPERT_WORK_SECRET_ENCRYPTION_KEY``)解出对象存储密钥;解不出来时整个作业照跑完、以 3 退出。"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import DatabaseConfig, create_async_engine_from_config
from expert_work.runtime.secret_store.sql_encrypted import SqlEncryptedSecretStore
from retention_cleanup_job import main as main_module
from retention_cleanup_job.main import EXIT_PASS_FAILED, build_session_factory
from retention_cleanup_job.object_store import plan_object_store
from retention_cleanup_job.settings import RetentionCleanupSettings

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_KEK = base64.b64encode(b"k" * 32).decode()
_ACCESS = "LTAI-test-access"
_SECRET = "oss-secret-never-logged"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def sf(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    for name in list(os.environ):
        if name.startswith("EXPERT_WORK_"):
            monkeypatch.delenv(name)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    admin = create_engine(_sync_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("DELETE FROM encrypted_secret WHERE tenant_id IS NULL"))
    finally:
        admin.dispose()
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


def _shared_env(monkeypatch: pytest.MonkeyPatch, *, kek: str = _KEK) -> None:
    """The test overlay shape: refs in the configmap, KEK in control-plane-secrets."""
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_BACKEND", "s3-compatible")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ENDPOINT_URL", "https://oss.example.internal")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_BUCKET", "expert-work-test")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ADDRESSING_STYLE", "virtual")
    monkeypatch.setenv(
        "EXPERT_WORK_OBJECT_STORE_ACCESS_KEY_REF", "secret://expert-work/platform/oss/access-key"
    )
    monkeypatch.setenv(
        "EXPERT_WORK_OBJECT_STORE_SECRET_KEY_REF", "secret://expert-work/platform/oss/secret-key"
    )
    monkeypatch.setenv("EXPERT_WORK_SECRET_STORE_BACKEND", "sql_encrypted")
    monkeypatch.setenv("EXPERT_WORK_SECRET_ENCRYPTION_KEY", kek)


async def _seed_secrets(sf: async_sessionmaker[AsyncSession]) -> None:
    """What the control plane's secrets UI writes (same class, same KEK)."""
    vault = SqlEncryptedSecretStore(sf, kek=base64.b64decode(_KEK))
    await vault.put("expert-work/platform/oss/access-key", _ACCESS)
    await vault.put("expert-work/platform/oss/secret-key", _SECRET)


@pytest.mark.asyncio
async def test_the_job_decrypts_the_control_planes_object_store_keys(
    sf: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_secrets(sf)
    _shared_env(monkeypatch)

    plan = await plan_object_store(RetentionCleanupSettings(), sf)

    assert (plan.source, plan.unavailable) == ("shared", False)
    assert plan.config is not None
    assert (plan.config.access_key, plan.config.secret_key) == (_ACCESS, _SECRET)


@pytest.mark.asyncio
async def test_a_wrong_key_is_unavailable_not_a_crash(
    sf: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_secrets(sf)
    _shared_env(monkeypatch, kek=base64.b64encode(b"x" * 32).decode())

    plan = await plan_object_store(RetentionCleanupSettings(), sf)

    assert (plan.unavailable, plan.error) == (True, "SecretStoreError")


@pytest.mark.asyncio
async def test_the_whole_job_runs_and_fails_loudly_when_keys_are_missing(
    sf: async_sessionmaker[AsyncSession],
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No secrets in the vault: every other pass still runs, the image pass is skipped, the
    process exits 3 (the Job shows Failed — no alert is wired to it); the backend is logged once."""
    _shared_env(monkeypatch)
    monkeypatch.setenv("EXPERT_WORK_RETENTION_DB_DSN", _async_dsn(postgres_container))
    monkeypatch.setattr(main_module, "configure_logging", lambda *_a, **_k: None)
    caplog.set_level(logging.INFO)

    code = await main_module._amain()

    messages = [r.getMessage() for r in caplog.records]
    backend_lines = [m for m in messages if m.startswith("retention.object_store ")]
    assert backend_lines == [
        "retention.object_store backend=s3-compatible source=shared available=false "
        "error=SecretNotFoundError — image bytes are not deleted this run"
    ]
    assert any(
        m.startswith("retention_cleanup_job.done ") and "failed_passes=images" in m
        for m in messages
    )
    assert code == EXIT_PASS_FAILED


@pytest.mark.asyncio
async def test_the_whole_job_uses_the_resolved_store(
    sf: async_sessionmaker[AsyncSession],
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed_secrets(sf)
    _shared_env(monkeypatch)
    monkeypatch.setenv("EXPERT_WORK_RETENTION_DB_DSN", _async_dsn(postgres_container))
    monkeypatch.setattr(main_module, "configure_logging", lambda *_a, **_k: None)
    caplog.set_level(logging.INFO)

    code = await main_module._amain()

    messages = [r.getMessage() for r in caplog.records]
    assert [m for m in messages if m.startswith("retention.object_store ")] == [
        "retention.object_store backend=s3-compatible source=shared available=true "
        "bucket=expert-work-test"
    ]
    assert all(_SECRET not in m and _ACCESS not in m for m in messages)
    assert code == 0


@pytest.mark.asyncio
async def test_a_bad_shared_value_still_runs_every_other_pass(
    sf: async_sessionmaker[AsyncSession],
    postgres_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A value the shared settings reject (here the addressing style) skips only the image pass:
    the job still reaches ``done`` and exits 3 instead of dying before the first pass."""
    await _seed_secrets(sf)
    _shared_env(monkeypatch)
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ADDRESSING_STYLE", "sideways")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_DB_DSN", _async_dsn(postgres_container))
    monkeypatch.setattr(main_module, "configure_logging", lambda *_a, **_k: None)
    caplog.set_level(logging.INFO)

    code = await main_module._amain()

    messages = [r.getMessage() for r in caplog.records]
    assert [m for m in messages if m.startswith("retention.object_store ")] == [
        "retention.object_store backend=s3-compatible source=shared available=false "
        "error=ValidationError — image bytes are not deleted this run"
    ]
    assert any(
        m.startswith("retention_cleanup_job.done ") and "failed_passes=images" in m
        for m in messages
    )
    assert code == EXIT_PASS_FAILED
