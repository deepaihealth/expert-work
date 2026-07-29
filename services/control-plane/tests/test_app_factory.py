"""Smoke tests for :func:`control_plane.app.create_app`."""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from tests.auth_fixtures import build_test_jwt_verifier


def test_create_app_returns_fastapi_instance() -> None:
    app = create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        jwt_verifier=build_test_jwt_verifier(),
    )
    assert isinstance(app, FastAPI)
    assert app.title == "Expert Work Control Plane"


def test_create_app_attaches_settings_and_lifecycle() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    lc = Lifecycle()
    app = create_app(settings=settings, lifecycle=lc, jwt_verifier=build_test_jwt_verifier())
    assert app.state.settings is settings
    assert app.state.lifecycle is lc
    assert app.state.health_provider is not None
    assert app.state.jwt_verifier is not None


def test_create_app_accepts_prod_auth_mode_after_c1() -> None:
    """After C.1, ``auth_mode=prod`` boots — JWT middleware enforces auth."""
    settings = Settings(_env_file=None, auth_mode="prod")  # type: ignore[call-arg]
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    assert isinstance(app, FastAPI)


def test_health_and_metrics_routes_registered() -> None:
    app = create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        jwt_verifier=build_test_jwt_verifier(),
    )
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/healthz/live" in paths
    assert "/healthz/ready" in paths
    assert "/healthz/startup" in paths
    assert "/metrics" in paths


def test_multi_replica_without_quota_redis_url_fails_startup() -> None:
    """W1-PR2 Task 5 — a multi-replica deploy (``single_instance=False``)
    with no ``EXPERT_WORK_QUOTA_REDIS_URL`` must fail fast at startup
    instead of silently falling back to the in-process rate limiter /
    quota engine: each replica would then keep its own independent bucket,
    which is equivalent to no limit at all under horizontal scale-out
    (ratelimit/in_process.py's docstring). The guard lives in ``lifespan``,
    so it only fires once the app actually starts (``TestClient`` context
    entry), not at bare ``create_app()`` construction time.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, single_instance=False, quota_redis_url=None
    )
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    with pytest.raises(RuntimeError, match="EXPERT_WORK_QUOTA_REDIS_URL"), TestClient(app):
        pass


def test_multi_replica_without_hmac_salt_fails_startup() -> None:
    """Final-review I-2 — a multi-replica deploy (``single_instance=False``)
    with ``EXPERT_WORK_QUOTA_REDIS_URL`` set but no
    ``EXPERT_WORK_APIKEY_RATE_LIMIT_HMAC_SALT`` must also fail fast at
    startup: without a shared salt, each replica mints its own random HMAC
    key (``RateLimitMiddleware``), so the same ``X-API-Key`` hashes to a
    different bucket per replica and the apikey-dimension limit is
    effectively multiplied by the replica count instead of enforced.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        single_instance=False,
        quota_redis_url="redis://localhost:6379/0",
        apikey_rate_limit_hmac_salt=None,
    )
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    with (
        pytest.raises(RuntimeError, match="EXPERT_WORK_APIKEY_RATE_LIMIT_HMAC_SALT"),
        TestClient(app),
    ):
        pass


def test_multi_replica_with_quota_redis_url_boots() -> None:
    """Same multi-replica settings, but with the Redis URL and HMAC salt
    configured — startup must succeed (the guards only fire on the
    missing-config cases)."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        single_instance=False,
        quota_redis_url="redis://localhost:6379/0",
        apikey_rate_limit_hmac_salt="test-hmac-salt",
    )
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    with TestClient(app):
        pass


def test_enable_scheduler_false_skips_single_replica_workers() -> None:
    """Multi-replica deploy (Task 3) — ``EXPERT_WORK_ENABLE_SCHEDULER=false``
    (surfaced here as the settings value ``main.py`` forwards to
    ``create_app``) means this replica starts neither the trigger
    scheduler, the SkillCurator, nor the MemoryConsolidator. The workers
    are only attached to ``app.state`` during the lifespan startup, so the
    assertions run inside the ``TestClient`` context manager.
    """
    settings = Settings(_env_file=None, enable_scheduler=False)  # type: ignore[call-arg]
    app = create_app(
        settings=settings,
        enable_scheduler=settings.enable_scheduler,
        jwt_verifier=build_test_jwt_verifier(),
    )
    with TestClient(app):
        assert getattr(app.state, "trigger_scheduler", None) is None
        assert getattr(app.state, "skill_curator", None) is None
        assert getattr(app.state, "memory_consolidator", None) is None


def test_enable_curation_worker_false_skips_curation_worker() -> None:
    settings = Settings(_env_file=None, enable_curation_worker=False)  # type: ignore[call-arg]
    app = create_app(
        settings=settings,
        enable_curation_worker=settings.enable_curation_worker,
        jwt_verifier=build_test_jwt_verifier(),
    )
    with TestClient(app):
        assert getattr(app.state, "curation_worker", None) is None


def test_enable_reaper_false_skips_quota_reaper() -> None:
    settings = Settings(_env_file=None, enable_reaper=False)  # type: ignore[call-arg]
    app = create_app(
        settings=settings,
        enable_reaper=settings.enable_reaper,
        jwt_verifier=build_test_jwt_verifier(),
    )
    with TestClient(app):
        assert app.state.quota_reaper is None


def test_main_module_forwards_settings_switches_to_create_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main.py`` is the uvicorn entrypoint (``create_app()`` with no args
    previously — Task 3 wires it to read the three env-backed switches off
    ``Settings`` and forward them explicitly)."""
    monkeypatch.setenv("EXPERT_WORK_ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("EXPERT_WORK_ENABLE_CURATION_WORKER", "false")
    monkeypatch.setenv("EXPERT_WORK_ENABLE_REAPER", "false")

    captured: dict[str, object] = {}

    def _fake_create_app(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("control_plane.app.create_app", _fake_create_app)

    import control_plane.main as main_module

    importlib.reload(main_module)

    assert captured["enable_scheduler"] is False
    assert captured["enable_curation_worker"] is False
    assert captured["enable_reaper"] is False
    forwarded_settings = captured["settings"]
    assert isinstance(forwarded_settings, Settings)
    assert forwarded_settings.enable_scheduler is False

    # Undo the env vars + the create_app patch, then reload once more with
    # the real create_app so later tests that (re-)import control_plane.main
    # see a genuine app, not this test's fake sentinel.
    monkeypatch.undo()
    importlib.reload(main_module)
