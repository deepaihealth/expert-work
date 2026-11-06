"""B-286 —— 留存作业用与 control-plane 同一套 ``EXPERT_WORK_OBJECT_STORE_*`` 配置和 SecretStore
解出对象存储;解不出来只让依赖它的 pass 记跳过,不让作业崩。

真库(``sql_encrypted`` 的 ``encrypted_secret`` 表)那一半在 ``test_object_store_integration.py``。
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryImageUploadStore
from expert_work.runtime.storage import InMemoryObjectStore, S3CompatibleObjectStore
from retention_cleanup_job import object_store as object_store_module
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.object_store import (
    ObjectStorePlan,
    SharedStorageSettings,
    enter_object_store,
    log_plan,
    plan_object_store,
)
from retention_cleanup_job.settings import RetentionCleanupSettings

_ACCESS_REF = "secret://expert-work/platform/oss/access-key"
_SECRET_REF = "secret://expert-work/platform/oss/secret-key"
_ACCESS = "AKIA-test-access"
_SECRET = "test-secret-value-never-logged"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only what each test sets: the dev box / CI may export ``EXPERT_WORK_*``."""
    import os

    for name in list(os.environ):
        if name.startswith("EXPERT_WORK_"):
            monkeypatch.delenv(name)


def _shared_env(monkeypatch: pytest.MonkeyPatch, env_file: Path | None = None) -> None:
    """What the CronJob gets from ``control-plane-config`` via envFrom (test overlay shape)."""
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_BACKEND", "s3-compatible")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ENDPOINT_URL", "https://oss.example.internal")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_BUCKET", "expert-work-test")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_REGION", "cn-hangzhou")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ADDRESSING_STYLE", "virtual")
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ACCESS_KEY_REF", _ACCESS_REF)
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_SECRET_KEY_REF", _SECRET_REF)
    if env_file is not None:
        monkeypatch.setenv("EXPERT_WORK_SECRET_STORE_BACKEND", "local_dev")
        monkeypatch.setenv("EXPERT_WORK_SECRET_STORE_ENV_FILE", str(env_file))


def _secrets_file(tmp_path: Path, *, with_secret: bool = True) -> Path:
    path = tmp_path / "secrets.env"
    lines = [f"expert-work/platform/oss/access-key={_ACCESS}"]
    if with_secret:
        lines.append(f"expert-work/platform/oss/secret-key={_SECRET}")
    path.write_text("\n".join(lines) + "\n")
    return path


async def _plan() -> ObjectStorePlan:
    return await plan_object_store(
        RetentionCleanupSettings(),
        lambda: None,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


async def test_the_shared_control_plane_settings_drive_the_object_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path))

    plan = await _plan()

    assert (plan.backend, plan.source, plan.unavailable, plan.error) == (
        "s3-compatible",
        "shared",
        False,
        None,
    )
    assert plan.config is not None
    assert (
        plan.config.endpoint_url,
        plan.config.bucket,
        plan.config.region,
        plan.config.addressing_style,
        plan.config.access_key,
        plan.config.secret_key,
    ) == (
        "https://oss.example.internal",
        "expert-work-test",
        "cn-hangzhou",
        "virtual",
        _ACCESS,
        _SECRET,
    )


async def test_the_plan_repr_leaves_out_the_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The plan (and the S3 config inside it) can end up in a log line or a traceback."""
    _shared_env(monkeypatch, _secrets_file(tmp_path))

    plan = await _plan()

    assert plan.config is not None
    assert "expert-work-test" in repr(plan)
    assert _ACCESS not in repr(plan)
    assert _SECRET not in repr(plan)


async def test_nothing_configured_means_the_memory_backend() -> None:
    plan = await _plan()

    assert (plan.backend, plan.source, plan.unavailable) == ("memory", "shared", False)


async def test_an_explicit_retention_override_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``EXPERT_WORK_RETENTION_OBJECT_STORE_*`` (plain keys) keeps working for local / standalone
    deployments, and when set it is what the job uses."""
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_BACKEND", "s3-compatible")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_ACCESS_KEY", "minio-ak")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_SECRET_KEY", "minio-sk")

    plan = await _plan()

    assert plan.source == "retention_override"
    assert plan.config is not None
    assert (plan.config.endpoint_url, plan.config.access_key) == ("http://minio:9000", "minio-ak")


async def test_an_explicit_retention_memory_override_turns_it_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_BACKEND", "memory")

    plan = await _plan()

    assert (plan.backend, plan.source, plan.unavailable) == ("memory", "retention_override", False)


# ---------------------------------------------------------------------------
# Unresolvable refs → unavailable plan, never an exception
# ---------------------------------------------------------------------------


async def test_a_missing_secret_is_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path, with_secret=False))

    plan = await _plan()

    assert (plan.backend, plan.unavailable, plan.error) == (
        "s3-compatible",
        True,
        "SecretNotFoundError",
    )


async def test_sql_encrypted_without_its_key_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shared_env(monkeypatch)
    monkeypatch.setenv("EXPERT_WORK_SECRET_STORE_BACKEND", "sql_encrypted")

    plan = await _plan()

    assert (plan.unavailable, plan.error) == (True, "RuntimeError")


async def test_an_incomplete_shared_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    monkeypatch.delenv("EXPERT_WORK_OBJECT_STORE_ENDPOINT_URL")

    plan = await _plan()

    assert (plan.unavailable, plan.error) == (True, "RuntimeError")


async def test_an_incomplete_retention_override_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Used to raise ``ValueError`` out of ``main`` and kill every other pass too."""
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_BACKEND", "s3-compatible")

    plan = await _plan()

    assert (plan.unavailable, plan.error) == (True, "incomplete_config")


async def test_a_bad_shared_value_is_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Low-2 —— a value the shared settings reject used to raise out of ``main`` before any pass
    ran; now only the image pass is skipped."""
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    monkeypatch.setenv("EXPERT_WORK_OBJECT_STORE_ADDRESSING_STYLE", "sideways")
    with pytest.raises(ValueError):
        SharedStorageSettings()

    plan = await _plan()

    assert (plan.backend, plan.unavailable, plan.error) == (
        "s3-compatible",
        True,
        "ValidationError",
    )


async def test_override_values_without_the_backend_are_named_in_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Low-6 —— ``EXPERT_WORK_RETENTION_OBJECT_STORE_*`` only applies with ``…_BACKEND`` set;
    without it they are ignored, so say which ones (names only, never the values)."""
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("EXPERT_WORK_RETENTION_OBJECT_STORE_SECRET_KEY", "minio-sk-value")
    caplog.set_level(logging.WARNING, logger="retention_cleanup_job.object_store")

    plan = await _plan()

    assert (plan.backend, plan.source) == ("memory", "shared")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "retention.object_store_override_ignored vars="
        "EXPERT_WORK_RETENTION_OBJECT_STORE_ENDPOINT_URL,"
        "EXPERT_WORK_RETENTION_OBJECT_STORE_SECRET_KEY "
    )
    assert "minio-sk-value" not in warnings[0]
    assert "minio:9000" not in warnings[0]


async def test_no_override_warning_when_nothing_is_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="retention_cleanup_job.object_store")

    await _plan()

    assert caplog.records == []


# ---------------------------------------------------------------------------
# Factory wiring + the one log line per run
# ---------------------------------------------------------------------------


async def test_a_resolved_plan_builds_the_s3_store_for_that_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    plan = await _plan()

    async with AsyncExitStack() as stack:
        store, after = await enter_object_store(stack, plan)

        assert isinstance(store, S3CompatibleObjectStore)
        assert store._bucket == "expert-work-test"
        assert after == plan


async def test_the_memory_plan_builds_no_store() -> None:
    async with AsyncExitStack() as stack:
        store, after = await enter_object_store(stack, await _plan())

    assert store is None
    assert after.unavailable is False


async def test_a_store_that_cannot_be_built_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    plan = await _plan()

    def broken(*_: Any, **__: Any) -> Any:
        raise OSError("no route")

    monkeypatch.setattr(object_store_module, "make_object_store", broken)
    async with AsyncExitStack() as stack:
        store, after = await enter_object_store(stack, plan)

    assert store is None
    assert (after.unavailable, after.error) == (True, "OSError")


async def test_the_run_logs_the_backend_once_without_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _shared_env(monkeypatch, _secrets_file(tmp_path))
    caplog.set_level(logging.INFO, logger="retention_cleanup_job.object_store")

    log_plan(await _plan())
    log_plan(ObjectStorePlan(backend="s3-compatible", source="shared", error="SecretNotFoundError"))

    lines = [r.getMessage() for r in caplog.records]
    assert lines[0].startswith(
        "retention.object_store backend=s3-compatible source=shared available=true"
    )
    assert lines[1].startswith(
        "retention.object_store backend=s3-compatible source=shared available=false "
        "error=SecretNotFoundError"
    )
    assert all(_SECRET not in line and _ACCESS not in line for line in lines)
    assert all("secret://" not in line for line in lines)


# ---------------------------------------------------------------------------
# The image pass: unavailable → skipped + failed pass, rows untouched
# ---------------------------------------------------------------------------


async def _old_image(images: InMemoryImageUploadStore) -> tuple[Any, Any]:
    image_id, tenant = uuid4(), uuid4()
    await images.insert(
        image_id=image_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key="tenants/x/uploads/old.png",
        size_bytes=3,
        mime_type="image/png",
        sha256="x",
    )
    images._rows[image_id] = images._rows[image_id].model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(days=200)}
    )
    return image_id, tenant


async def test_an_unavailable_store_skips_the_image_pass_and_fails_the_run() -> None:
    """Deleting the rows without the bytes would orphan the bytes forever: keep the rows, report
    the pass as failed (exit code 3), let every other pass run.

    A working store is wired on purpose: with ``object_store=None`` the pass returns early on its
    own and the row-kept assertion could not go red. Here only the ``unavailable`` marker stands
    between the row and its deletion."""
    images = InMemoryImageUploadStore()
    store = InMemoryObjectStore()
    image_id, tenant = await _old_image(images)
    await store.put("tenants/x/uploads/old.png", b"OLD", content_type="image/png")
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        image_upload_store=images,
        object_store=store,
        object_store_unavailable="SecretNotFoundError",
        upload_retention_days=90,
    )

    assert await job._delete_expired_images() == (0, 0, 0)
    assert job._failed_passes == ["images"]
    assert await images.get(image_id=image_id, tenant_id=tenant) is not None
    assert await store.get("tenants/x/uploads/old.png") == b"OLD"


async def test_an_available_store_still_reaps_images() -> None:
    images = InMemoryImageUploadStore()
    store = InMemoryObjectStore()
    image_id, tenant = await _old_image(images)
    await store.put("tenants/x/uploads/old.png", b"OLD", content_type="image/png")
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        image_upload_store=images,
        object_store=store,
        upload_retention_days=90,
    )

    assert await job._delete_expired_images() == (1, 1, 0)
    assert job._failed_passes == []
    assert await images.get(image_id=image_id, tenant_id=tenant) is None
