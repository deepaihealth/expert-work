"""Unit tests for the object-store addressing-style knobs on
:class:`SandboxSupervisorSettings` (W2-PR1 Task 1 — OSS S3 compat)."""

from __future__ import annotations

import pytest

from sandbox_supervisor.settings import SandboxSupervisorSettings


def test_addressing_style_defaults_to_path() -> None:
    """No env overrides → legacy ``object_store_use_path_style=True``
    default derives to ``"path"``, matching pre-migration behavior."""
    settings = SandboxSupervisorSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_object_store_addressing_style == "path"


def test_addressing_style_explicit_field_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_SANDBOX_OBJECT_STORE_ADDRESSING_STYLE", "virtual")
    settings = SandboxSupervisorSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_object_store_addressing_style == "virtual"


def test_legacy_bool_env_false_maps_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: existing deployments setting the old bool env to
    false must keep resolving to ``"auto"`` — the pre-migration
    ``"path" if use_path_style else "auto"`` ternary — not silently
    change behavior."""
    monkeypatch.setenv("EXPERT_WORK_SANDBOX_OBJECT_STORE_USE_PATH_STYLE", "false")
    settings = SandboxSupervisorSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_object_store_addressing_style == "auto"


def test_legacy_bool_env_true_maps_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_SANDBOX_OBJECT_STORE_USE_PATH_STYLE", "true")
    settings = SandboxSupervisorSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_object_store_addressing_style == "path"
