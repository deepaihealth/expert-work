"""Unit tests for the object-store addressing-style knobs on
:class:`EventLogArchiveSettings` (W2-PR1 Task 1 — OSS S3 compat)."""

from __future__ import annotations

import pytest

from event_log_archive_job.settings import EventLogArchiveSettings


def test_addressing_style_defaults_to_path() -> None:
    """No env overrides → legacy ``s3_use_path_style=True`` default
    derives to ``"path"``, matching pre-migration behavior."""
    settings = EventLogArchiveSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_s3_addressing_style == "path"


def test_addressing_style_explicit_field_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVENT_LOG_ARCHIVE_S3_ADDRESSING_STYLE", "virtual")
    settings = EventLogArchiveSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_s3_addressing_style == "virtual"


def test_legacy_bool_env_false_maps_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: existing deployments setting the old bool env to
    false must keep resolving to ``"auto"`` — the pre-migration
    ``"path" if use_path_style else "auto"`` ternary — not silently
    change behavior."""
    monkeypatch.setenv("EXPERT_WORK_EVENT_LOG_ARCHIVE_S3_USE_PATH_STYLE", "false")
    settings = EventLogArchiveSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_s3_addressing_style == "auto"


def test_legacy_bool_env_true_maps_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_EVENT_LOG_ARCHIVE_S3_USE_PATH_STYLE", "true")
    settings = EventLogArchiveSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.effective_s3_addressing_style == "path"
