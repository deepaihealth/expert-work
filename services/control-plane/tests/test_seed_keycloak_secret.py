"""Unit tests for the Keycloak admin-secret seed CLI — Stream R W4.

The DB wiring is exercised by the live runbook; here we cover the pure pieces:
value resolution precedence + that the seed writes to the vault under the
configured name.
"""

from __future__ import annotations

import pytest

from control_plane.seed_keycloak_secret import (
    SeedValueMissingError,
    resolve_secret_name,
    resolve_secret_value,
    seed_keycloak_admin_secret,
)


def test_resolve_prefers_arg_over_env() -> None:
    got = resolve_secret_value("from-arg", env={"EXPERT_WORK_KEYCLOAK_ADMIN_CLIENT_SECRET": "env"})
    assert got == "from-arg"


def test_resolve_falls_back_to_env() -> None:
    got = resolve_secret_value(None, env={"EXPERT_WORK_KEYCLOAK_ADMIN_CLIENT_SECRET": "env-secret"})
    assert got == "env-secret"


def test_resolve_treats_empty_arg_as_absent() -> None:
    got = resolve_secret_value("", env={"EXPERT_WORK_KEYCLOAK_ADMIN_CLIENT_SECRET": "env-secret"})
    assert got == "env-secret"


def test_resolve_raises_when_nothing_supplied() -> None:
    with pytest.raises(SeedValueMissingError):
        resolve_secret_value(None, env={})


class _FakeStore:
    """Minimal SecretStore double recording the last ``put``."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []

    async def put(self, name: str, value: str) -> None:
        self.puts.append((name, value))


@pytest.mark.asyncio
async def test_seed_writes_under_configured_name() -> None:
    store = _FakeStore()
    await seed_keycloak_admin_secret(
        store,  # type: ignore[arg-type]
        name="expert-work/platform/keycloak/admin-client-secret",
        value="sekret",
    )
    assert store.puts == [("expert-work/platform/keycloak/admin-client-secret", "sekret")]


def test_resolve_name_prefers_arg() -> None:
    assert (
        resolve_secret_name("expert-work/platform/oss/access-key", "kc-default")
        == "expert-work/platform/oss/access-key"
    )


def test_resolve_name_defaults_to_settings() -> None:
    assert resolve_secret_name(None, "kc-default") == "kc-default"


def test_resolve_name_treats_empty_as_absent() -> None:
    assert resolve_secret_name("", "kc-default") == "kc-default"


@pytest.mark.asyncio
async def test_amain_wires_name_flag_through_to_store(monkeypatch) -> None:
    """钉住 _amain 的 --name 接线:变异成写死 KC 名会让 OSS seed 写错 ref、
    CrashLoop 原样复发而纯函数测试仍绿(复审第三轮的观察)。"""
    import argparse
    from types import SimpleNamespace

    from control_plane import seed_keycloak_secret as mod

    store = _FakeStore()

    class _FakeEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        mod,
        "Settings",
        lambda: SimpleNamespace(
            secret_store_backend="sql_encrypted",
            secret_encryption_key=SimpleNamespace(get_secret_value=lambda: "a" * 44),
            db_dsn="postgresql+asyncpg://x",
            db_pgbouncer_mode=False,
            keycloak_admin_secret_name="kc-default",
        ),
    )
    monkeypatch.setattr(mod, "create_async_engine_from_config", lambda _cfg: _FakeEngine())
    monkeypatch.setattr(mod, "create_async_session_factory", lambda _e: object())
    monkeypatch.setattr(mod, "build_rls_sessionmaker", lambda _f: object())
    monkeypatch.setattr(mod, "build_kek_from_b64", lambda _v: b"kek")
    monkeypatch.setattr(mod, "SqlEncryptedSecretStore", lambda _sf, kek: store)

    args = argparse.Namespace(value="AKVALUE", dsn=None, name="expert-work/platform/oss/access-key")
    assert await mod._amain(args) == 0
    assert store.puts == [("expert-work/platform/oss/access-key", "AKVALUE")]
