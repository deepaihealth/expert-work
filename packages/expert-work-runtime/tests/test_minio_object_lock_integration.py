"""Integration tests for D.1b Object Lock against a real MinIO instance.

Reuses the session-scoped ``compose_stack`` fixture from ``conftest.py``
(shared with ``test_minio_integration.py``). The retention bucket is
provisioned inside this module's fixture with
``ObjectLockEnabledForBucket=True`` — bucket creation is the only way
to enable Object Lock on a MinIO bucket, so we need a dedicated bucket
distinct from the regular dev one.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from testcontainers.compose import DockerCompose

from expert_work.runtime.storage import (
    ObjectStore,
    S3CompatibleConfig,
    make_object_store,
)

pytestmark = pytest.mark.integration


def _config(stack: DockerCompose, bucket: str) -> S3CompatibleConfig:
    host, port_str = stack.get_service_host_and_port("minio", 9000)
    user = os.environ.get("EXPERT_WORK_MINIO_ROOT_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_MINIO_ROOT_PASSWORD", "expert_work_dev_minio")
    return S3CompatibleConfig(
        endpoint_url=f"http://{host}:{port_str}",
        region="us-east-1",
        bucket=bucket,
        access_key=user,
        secret_key=password,
        addressing_style="path",
    )


async def _ensure_worm_bucket(store: ObjectStore, bucket: str) -> None:
    """Create the Object-Lock-enabled bucket if it does not exist.

    Object Lock requires the bucket to be created with
    ``ObjectLockEnabledForBucket=True``; it cannot be turned on later.
    We probe ``head_bucket`` first so this fixture is idempotent across
    test re-runs against the same MinIO container.
    """
    raw = getattr(store, "_client", None)
    if raw is None:  # pragma: no cover — defensive
        msg = "worm bucket fixture requires S3CompatibleObjectStore"
        raise RuntimeError(msg)
    try:
        await raw.head_bucket(Bucket=bucket)
    except Exception:
        await raw.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)


@pytest.fixture
async def worm_store(compose_stack: DockerCompose) -> AsyncIterator[ObjectStore]:
    bucket = os.environ.get("EXPERT_WORK_MINIO_WORM_BUCKET", "expert-work-dev-worm")
    config = _config(compose_stack, bucket)
    async with make_object_store("s3-compatible", config) as s:
        await _ensure_worm_bucket(s, bucket)
        yield s


def _retain_until(seconds: float) -> datetime:
    """Return ``now + seconds``; MinIO rejects times in the past."""
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_put_with_compliance_lock_round_trip(worm_store: ObjectStore) -> None:
    """A compliance-locked object stores cleanly and is fetchable."""
    key = f"d1b/round-trip/{datetime.now(tz=UTC).timestamp()}.bin"
    await worm_store.put(
        key,
        b"audit-row-1",
        retain_until=_retain_until(120),
        lock_mode="compliance",
    )
    assert await worm_store.get(key) == b"audit-row-1"


@pytest.mark.asyncio
async def test_compliance_lock_blocks_version_deletion(worm_store: ObjectStore) -> None:
    """A compliance-locked version cannot be deleted before retention elapses.

    This is the **real** WORM guarantee: S3 / MinIO Object Lock pins
    individual *versions* against delete-before-expiry. Same-key
    re-puts are allowed (each one is a new version), so we don't
    test for those — the WORM-backup worker (D.1c) keys by audit row
    id, which is monotonic, and retries on the same id are guarded
    by ``backup_acked`` rather than by S3 put-side rejection.
    """
    key = f"d1b/delete-blocked/{datetime.now(tz=UTC).timestamp()}.bin"
    retain_until = _retain_until(120)
    await worm_store.put(
        key,
        b"original",
        retain_until=retain_until,
        lock_mode="compliance",
    )

    raw = worm_store._client  # type: ignore[attr-defined]
    bucket = worm_store._bucket  # type: ignore[attr-defined]
    head = await raw.head_object(Bucket=bucket, Key=key)
    version_id = head["VersionId"]

    # Attempting to delete the locked version inside the retention
    # window is the protected operation. MinIO returns 403
    # AccessDenied; botocore surfaces that as ClientError.
    with pytest.raises(raw.exceptions.ClientError):
        await raw.delete_object(Bucket=bucket, Key=key, VersionId=version_id)

    # Original payload is still readable.
    assert await worm_store.get(key) == b"original"


@pytest.mark.asyncio
async def test_head_object_returns_retention_metadata(worm_store: ObjectStore) -> None:
    """MinIO surfaces ``ObjectLockMode`` + ``ObjectLockRetainUntilDate`` on HEAD."""
    key = f"d1b/head-meta/{datetime.now(tz=UTC).timestamp()}.bin"
    retain_until = _retain_until(120)
    await worm_store.put(
        key,
        b"payload",
        retain_until=retain_until,
        lock_mode="compliance",
    )

    raw = worm_store._client  # type: ignore[attr-defined]
    bucket = worm_store._bucket  # type: ignore[attr-defined]
    head = await raw.head_object(Bucket=bucket, Key=key)
    assert head.get("ObjectLockMode") == "COMPLIANCE"
    assert "ObjectLockRetainUntilDate" in head


@pytest.mark.asyncio
async def test_put_without_lock_args_still_works(worm_store: ObjectStore) -> None:
    """The retention bucket happily accepts unlocked objects too.

    Bucket-level Object Lock = "available for use", not "every put must
    use it". Callers other than the WORM-backup worker can keep using
    plain puts against the same bucket.
    """
    key = f"d1b/no-lock/{datetime.now(tz=UTC).timestamp()}.bin"
    await worm_store.put(key, b"plain", content_type="application/octet-stream")
    assert await worm_store.get(key) == b"plain"
