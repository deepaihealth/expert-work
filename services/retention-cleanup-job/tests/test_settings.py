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


def test_memory_writeback_job_retention_defaults_to_30_days() -> None:
    """B-168 —— 记忆后台写回任务行收尾后留 30 天(控制台看得到),之后清掉。"""
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.memory_writeback_job_retention_days == 30


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


def test_knowledge_revision_orphans_are_kept_30_days() -> None:
    """B-175 P3-PR2 —— 重新入库没能套回去的人工修改,修订留 30 天(与检索记录同一期限)。"""
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.knowledge_revision_orphan_days == 30


def test_knowledge_task_retention_defaults_to_90_days() -> None:
    """B-175 L2-PR3 —— 知识库任务行收尾后留 90 天。"""
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.knowledge_task_retention_days == 90


def test_knowledge_retrieval_log_retention_defaults_to_30_days() -> None:
    """B-175 P2-PR4 — user ruling 2026-10-27: retrieval logs are kept 30 days."""
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.knowledge_retrieval_log_retention_days == 30


def test_knowledge_retrieval_log_retention_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_RETENTION_KNOWLEDGE_RETRIEVAL_LOG_RETENTION_DAYS", "0")
    with pytest.raises(ValueError, match="knowledge_retrieval_log_retention_days"):
        RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]


# ------------------------------------------------ B-63


@pytest.mark.parametrize(
    "env_key",
    [
        "EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS",
        "EXPERT_WORK_RETENTION_ARTIFACT_RETENTION_DAYS",
        "EXPERT_WORK_RETENTION_CONVERSATION_WORKSPACE_RETENTION_DAYS",
    ],
)
def test_file_retention_above_one_year_is_clamped_not_refused(
    monkeypatch: pytest.MonkeyPatch, env_key: str
) -> None:
    """B-63 —— 上限 365。已经配了更大值的部署不能因此每晚拒跑:夹到 365,记下原值给 main 打
    warning。"""
    monkeypatch.setenv(env_key, "3650")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    field = env_key.removeprefix("EXPERT_WORK_RETENTION_").lower()
    assert getattr(settings, field) == 365
    assert settings.clamped_retention == {field: 3650}


def test_file_retention_within_a_year_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS", "365")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_ARTIFACT_RETENTION_DAYS", "30")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.upload_retention_days, settings.artifact_retention_days) == (365, 30)
    assert settings.clamped_retention == {}


def test_clamped_retention_is_logged_as_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from retention_cleanup_job.main import warn_clamped_retention

    monkeypatch.setenv("EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS", "400")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    with caplog.at_level("WARNING", logger="retention_cleanup_job.main"):
        warn_clamped_retention(settings)
    assert (
        "retention.retention_days_clamped field=upload_retention_days configured=400 used=365"
        in (caplog.text)
    )


@pytest.mark.parametrize(
    "env_key",
    [
        "EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS",
        "EXPERT_WORK_RETENTION_ARTIFACT_RETENTION_DAYS",
        "EXPERT_WORK_RETENTION_CONVERSATION_WORKSPACE_RETENTION_DAYS",
    ],
)
def test_file_retention_below_a_week_is_raised_to_seven_not_refused(
    monkeypatch: pytest.MonkeyPatch, env_key: str
) -> None:
    """B-63 —— 与租户可设的范围一致(7-365)。低于 7 天的配置抬到 7(只会多留),记下原值。"""
    monkeypatch.setenv(env_key, "3")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    field = env_key.removeprefix("EXPERT_WORK_RETENTION_").lower()
    assert getattr(settings, field) == 7
    assert settings.clamped_retention == {field: 3}


def test_the_retired_image_retention_env_is_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B-283 —— 图片与附件同一个天数;还配着老的图片天数就打 warning,值不进作业。"""
    from retention_cleanup_job.main import warn_retired_image_retention

    monkeypatch.setenv("EXPERT_WORK_RETENTION_IMAGE_RETENTION_DAYS", "180")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    with caplog.at_level("WARNING", logger="retention_cleanup_job.main"):
        warn_retired_image_retention(settings)
    assert "retention.image_retention_days_ignored configured=180" in caplog.text
    unset = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    caplog.clear()
    monkeypatch.delenv("EXPERT_WORK_RETENTION_IMAGE_RETENTION_DAYS")
    warn_retired_image_retention(RetentionCleanupSettings(_env_file=None))  # type: ignore[call-arg]
    assert "image_retention_days_ignored" not in caplog.text
    assert unset.image_retention_days == 180


def test_conversation_workspace_defaults_to_90_days_and_500_per_run() -> None:
    """B-282 —— 平台默认 90 天(与协议的 ``FILE_RETENTION_DEFAULT_DAYS`` 一致),每轮 500 段。"""
    from expert_work.protocol import FILE_RETENTION_DEFAULT_DAYS

    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.conversation_workspace_retention_days == FILE_RETENTION_DEFAULT_DAYS
    assert settings.conversation_workspace_batch_size == 500


def test_the_b284_gates_are_kept_raw_and_parsed_strictly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-284 —— 两道闸原样存字符串(写错不让作业起不来),由 ``agent_roots`` 严格解析:日期只认
    ``YYYY-MM-DD``(数字不当 unix 时间)、切换时刻必须带时区。"""
    from datetime import UTC, datetime

    from retention_cleanup_job.agent_roots import parse_cutover, parse_shared_delete_after

    unset = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert (unset.shared_delete_after, unset.agent_root_cutover) == (None, None)
    monkeypatch.setenv("EXPERT_WORK_RETENTION_SHARED_DELETE_AFTER", "20261201")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_AGENT_ROOT_CUTOVER", "2026-10-30T23:48:00+08:00")
    settings = RetentionCleanupSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.shared_delete_after == "20261201"

    assert parse_shared_delete_after("2026-12-06") == (datetime(2026, 12, 6, tzinfo=UTC), None)
    assert parse_shared_delete_after("") == (None, None)
    for bad in ("20261201", "2026/12/01", "2026-02-30", "2026-11-05", "1"):
        when, problem = parse_shared_delete_after(bad)
        assert when is None and problem is not None, bad
    when, problem = parse_cutover(settings.agent_root_cutover)
    assert problem is None and when is not None
    assert when.astimezone(UTC) == datetime(2026, 10, 30, 15, 48, tzinfo=UTC)
    for bad in ("1730303460", "2026-10-30T23:48:00", "not a time"):
        when, problem = parse_cutover(bad)
        assert when is None and problem is not None, bad
