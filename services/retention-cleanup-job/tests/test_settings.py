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


# ------------------------------------------------ 留存链 PR2(波 3 线 R)


def test_retention_chain_defaults() -> None:
    """用户 2026-09-09 拍板:产物 90 天、上传 90 天、已删工作区库行 90 天。
    workspace_root 默认 None(碰文件的规则整体跳过)。"""
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.artifact_retention_days == 90
    assert settings.upload_retention_days == 90
    assert settings.workspace_archive_retention_days == 90
    assert settings.workspace_root is None


def test_retention_chain_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS", "30")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_WORKSPACE_ROOT", "/mnt/workspaces")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.upload_retention_days == 30
    assert settings.workspace_root == "/mnt/workspaces"


def test_upload_retention_days_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS", "0")
    with pytest.raises(ValueError, match="upload_retention_days"):
        RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
