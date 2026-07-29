"""Unit tests for the object-store addressing-style knob on
:class:`RetentionCleanupSettings` (W2-PR1 Task 1 — OSS S3 compat)."""

from __future__ import annotations

import pytest

from retention_cleanup_job.settings import RetentionCleanupSettings


def test_addressing_style_defaults_to_path() -> None:
    """No existing field to preserve here — this service's main.py never
    passed ``use_path_style`` at all (always silently defaulted to the
    factory's ``True``/``"path"``), so the new field's own default of
    ``"path"`` reproduces that same historical behavior."""
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.object_store_addressing_style == "path"


def test_addressing_style_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_ADDRESSING_STYLE", "virtual")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.object_store_addressing_style == "virtual"
