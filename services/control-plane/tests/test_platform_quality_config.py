"""Unit tests for the RT-5 PR-3b quality-config resolver + service (§14)."""

from __future__ import annotations

import pytest

from control_plane.platform_quality_config import (
    PlatformQualityConfigService,
    resolve_effective_quality_config,
)
from control_plane.settings import Settings
from expert_work.persistence.platform_quality_config import (
    InMemoryPlatformQualityConfigStore,
    PlatformQualityConfigRow,
)
from expert_work.protocol.model_catalog import catalog_entry


def _settings(*, enable: bool = True) -> Settings:
    return Settings(env="dev", auth_mode="dev", enable_quality_monitor=enable)


def _full_row(*, enabled: bool) -> PlatformQualityConfigRow:
    return PlatformQualityConfigRow(
        enabled=enabled,
        sampling_rate_pct=100,
        daily_cap=42,
        monitor_interval_s=15,
        monitor_batch_size=7,
        judge_provider="qwen",
        judge_model="qwen3.7-max",
        drift_interval_s=20,
        drift_recent_window_h=1,
        drift_baseline_window_h=2,
        drift_min_samples=3,
        drift_threshold=0.25,
        drift_cooldown_h=6,
        updated_by="u1",
    )


def test_no_row_falls_back_to_env_defaults_and_is_disabled() -> None:
    cfg = resolve_effective_quality_config(_settings(enable=True), None)
    # No row → enabled off (opt-in via UI) even though the deploy gate is on.
    assert cfg.enabled is False
    # Params come from the env defaults.
    assert cfg.sampling_rate_pct == 5
    assert cfg.judge_provider == "anthropic"
    assert cfg.drift_threshold == 0.15


def test_row_wins_and_enabled_is_env_and_ui() -> None:
    cfg = resolve_effective_quality_config(_settings(enable=True), _full_row(enabled=True))
    assert cfg.enabled is True
    assert cfg.sampling_rate_pct == 100
    assert cfg.judge_provider == "qwen"
    assert cfg.drift_min_samples == 3
    assert cfg.drift_threshold == 0.25


def test_deploy_gate_off_forces_disabled_regardless_of_row() -> None:
    cfg = resolve_effective_quality_config(_settings(enable=False), _full_row(enabled=True))
    assert cfg.enabled is False  # env hard-off wins


def test_ui_toggle_off_disables_even_with_deploy_gate_on() -> None:
    cfg = resolve_effective_quality_config(_settings(enable=True), _full_row(enabled=False))
    assert cfg.enabled is False


def test_default_judge_model_is_a_catalog_model() -> None:
    # B-112 —— the fallback judge used to be the dated ``claude-haiku-4-5-20251001``,
    # which is not in the model catalog (the entry is ``claude-haiku-4-5``): the
    # catalog's capability bits and the rate card never applied to it.
    settings = _settings()
    assert catalog_entry(settings.quality_judge_provider, settings.quality_judge_model) is not None


def test_null_field_falls_back_per_field() -> None:
    # A partial row (a knob left null) falls back to the env default for that
    # field only — the rest of the row still wins.
    row = PlatformQualityConfigRow(
        enabled=True,
        sampling_rate_pct=None,  # → env default 5
        daily_cap=None,
        monitor_interval_s=None,
        monitor_batch_size=None,
        judge_provider="qwen",
        judge_model=None,  # → env default model
        drift_interval_s=None,
        drift_recent_window_h=None,
        drift_baseline_window_h=None,
        drift_min_samples=None,
        drift_threshold=None,
        drift_cooldown_h=None,
        updated_by=None,
    )
    cfg = resolve_effective_quality_config(_settings(enable=True), row)
    assert cfg.enabled is True
    assert cfg.sampling_rate_pct == 5  # fell back
    assert cfg.judge_provider == "qwen"  # row won
    assert cfg.judge_model == "claude-haiku-4-5"  # fell back


@pytest.mark.asyncio
async def test_service_caches_then_invalidates_on_put() -> None:
    ticks = [0.0]

    def clock() -> float:
        return ticks[0]

    store = InMemoryPlatformQualityConfigStore()
    svc = PlatformQualityConfigService(
        store=store, settings=_settings(enable=True), ttl_seconds=30.0, clock=clock
    )
    # First read: no row → disabled.
    assert (await svc.effective()).enabled is False
    # Write directly to the store (not via the service) — the cache still serves
    # the stale value within the TTL.
    await store.put(_full_row(enabled=True))
    assert (await svc.effective()).enabled is False  # cached
    ticks[0] = 31.0  # TTL expired
    assert (await svc.effective()).enabled is True  # reloaded
    # A put through the service invalidates immediately.
    await svc.put(_full_row(enabled=False))
    assert (await svc.effective()).enabled is False
