"""Integration tests for :class:`S3CompatibleObjectStore` against MinIO.

Boots the same ``infra/docker-compose.yml`` stack used by the PgBouncer
integration test and exercises the real aiobotocore code path. The
``compose_stack`` fixture lives in ``conftest.py`` so it's shared across
the storage-integration test files.

The dev bucket is created **inside the fixture** rather than by a
docker-compose one-shot helper — a separate ``minio-init`` service exits
right after success, which trips ``docker compose up --wait`` (treats
stopped containers as failures). Creating the bucket via the S3 API
keeps the test self-contained and avoids the wait race.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from testcontainers.compose import DockerCompose

from expert_work.runtime.storage import (
    ObjectNotFoundError,
    ObjectStore,
    S3CompatibleConfig,
    make_object_store,
)
from expert_work.runtime.storage.s3_compatible import S3CompatibleObjectStore

pytestmark = pytest.mark.integration


def _config(stack: DockerCompose) -> S3CompatibleConfig:
    host, port_str = stack.get_service_host_and_port("minio", 9000)
    user = os.environ.get("EXPERT_WORK_MINIO_ROOT_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_MINIO_ROOT_PASSWORD", "expert_work_dev_minio")
    bucket = os.environ.get("EXPERT_WORK_MINIO_BUCKET", "expert-work-dev")
    return S3CompatibleConfig(
        endpoint_url=f"http://{host}:{port_str}",
        region="us-east-1",
        bucket=bucket,
        access_key=user,
        secret_key=password,
        addressing_style="path",
    )


async def _ensure_bucket(store: ObjectStore, bucket: str) -> None:
    """Create the bucket if it does not exist.

    Production buckets are provisioned by IaC, so ``ObjectStore`` does not
    expose ``create_bucket``. The test bootstraps via the underlying boto
    client through a structural attribute access — legitimate test-only
    escape hatch.
    """
    raw = getattr(store, "_client", None)
    if raw is None:  # pragma: no cover — defensive
        msg = "fixture requires S3CompatibleObjectStore for bucket bootstrap"
        raise RuntimeError(msg)
    try:
        await raw.head_bucket(Bucket=bucket)
    except Exception:
        await raw.create_bucket(Bucket=bucket)


@pytest.fixture
async def store(compose_stack: DockerCompose) -> AsyncIterator[ObjectStore]:
    """Yield an ``ObjectStore`` pointed at the live MinIO instance.

    Ensures the dev bucket exists on first use (idempotent across reruns).
    """
    config = _config(compose_stack)
    async with make_object_store("s3-compatible", config) as s:
        await _ensure_bucket(s, config.bucket)
        yield s


@pytest.mark.asyncio
async def test_put_get_delete_round_trip(store: ObjectStore) -> None:
    payload = b"hello world"
    await store.put("t1/uploads/hello.txt", payload, content_type="text/plain")
    assert await store.get("t1/uploads/hello.txt") == payload

    await store.delete("t1/uploads/hello.txt")
    with pytest.raises(ObjectNotFoundError):
        await store.get("t1/uploads/hello.txt")


@pytest.mark.asyncio
async def test_list_prefix(store: ObjectStore) -> None:
    await store.put("list-prefix/a.txt", b"a")
    await store.put("list-prefix/b.txt", b"b")
    await store.put("other/c.txt", b"c")

    listed = await store.list_prefix("list-prefix/")
    assert "list-prefix/a.txt" in listed
    assert "list-prefix/b.txt" in listed
    assert all(k.startswith("list-prefix/") for k in listed)


@pytest.mark.asyncio
async def test_presigned_url_format(store: ObjectStore) -> None:
    url = await store.presigned_url("t1/uploads/foo.txt", expires_in=60)
    # Pre-signed URLs always carry an X-Amz-Signature query param under
    # SigV4; this is the cheapest assertion that signing actually ran.
    assert "X-Amz-Signature" in url


@pytest.mark.asyncio
async def test_delete_missing_is_idempotent(store: ObjectStore) -> None:
    # Must not raise; ObjectStore contract.
    await store.delete("definitely-missing-key")


@pytest.mark.asyncio
async def test_put_stream_small_round_trip(store: ObjectStore) -> None:
    async def _chunks() -> AsyncIterator[bytes]:
        yield b"stream-"
        yield b"payload"

    await store.put_stream("stream/small.bin", _chunks())
    assert await store.get("stream/small.bin") == b"stream-payload"


@pytest.mark.asyncio
async def test_put_stream_multipart_round_trip(store: ObjectStore) -> None:
    """> 2 片真 multipart:5 MiB 分片 x 11 MiB 载荷 → 2 full + 1 tail。"""
    client = getattr(store, "_client", None)
    bucket = getattr(store, "_bucket", None)
    assert client is not None
    assert bucket is not None
    small_parts = S3CompatibleObjectStore(
        client=client, bucket=bucket, multipart_part_size=5 * 1024 * 1024
    )
    payload = os.urandom(11 * 1024 * 1024)

    async def _chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(payload), 1024 * 1024):
            yield payload[i : i + 1024 * 1024]

    await small_parts.put_stream("stream/big.bin", _chunks(), content_type="application/gzip")
    assert await store.get("stream/big.bin") == payload
