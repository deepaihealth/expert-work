"""ObjectStore.put_stream 契约(spec 波3 § 4.2)——memory 直测 + S3 用 fake client 测攒片。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from expert_work.runtime.storage import InMemoryObjectStore, ObjectNotFoundError, ObjectStoreError
from expert_work.runtime.storage.s3_compatible import S3CompatibleObjectStore


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


@pytest.mark.asyncio
async def test_memory_put_stream_round_trip() -> None:
    store = InMemoryObjectStore()
    await store.put_stream("a/b.tar.gz", _chunks(b"hello ", b"world"))
    assert await store.get("a/b.tar.gz") == b"hello world"


@pytest.mark.asyncio
async def test_memory_put_stream_empty_stream_creates_empty_object() -> None:
    store = InMemoryObjectStore()
    await store.put_stream("empty", _chunks())
    assert await store.get("empty") == b""


class _FakeExceptions:
    class ClientError(Exception):
        pass

    NoSuchKey = ClientError


class _FakeMultipartClient:
    """记录 multipart 调用序列;upload_part 失败可注入。"""

    def __init__(self, *, fail_part: int | None = None) -> None:
        self.exceptions = _FakeExceptions()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.parts: dict[int, bytes] = {}
        self.aborted = False
        self.completed = False
        self.single_put: bytes | None = None
        self._fail_part = fail_part

    async def put_object(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kw))
        self.single_put = kw["Body"]
        return {}

    async def create_multipart_upload(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("create", kw))
        return {"UploadId": "upl-1"}

    async def upload_part(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("upload_part", kw))
        if self._fail_part == kw["PartNumber"]:
            raise self.exceptions.ClientError("injected")
        self.parts[kw["PartNumber"]] = kw["Body"]
        return {"ETag": f'"etag-{kw["PartNumber"]}"'}

    async def complete_multipart_upload(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("complete", kw))
        self.completed = True
        return {}

    async def abort_multipart_upload(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("abort", kw))
        self.aborted = True
        return {}


@pytest.mark.asyncio
async def test_s3_small_stream_degrades_to_single_put() -> None:
    """整流 < 一片 → 不开 multipart,单次 put_object(顺带覆盖空流)。"""
    client = _FakeMultipartClient()
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=10)
    await store.put_stream("k", _chunks(b"tiny"), content_type="application/gzip")
    assert client.single_put == b"tiny"
    assert not any(name == "create" for name, _ in client.calls)
    put_kw = next(kw for name, kw in client.calls if name == "put_object")
    assert put_kw["ContentType"] == "application/gzip"


@pytest.mark.asyncio
async def test_s3_rebatches_chunks_into_exact_parts() -> None:
    """攒片:调用方 chunk 尺寸任意,落到 S3 的每片(除末片)恰好 part_size。"""
    client = _FakeMultipartClient()
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=4)
    await store.put_stream("k", _chunks(b"ab", b"cdefg", b"hij"))  # 10 bytes → 4+4+2
    assert client.parts == {1: b"abcd", 2: b"efgh", 3: b"ij"}
    assert client.completed
    complete_kw = next(kw for name, kw in client.calls if name == "complete")
    assert complete_kw["MultipartUpload"]["Parts"] == [
        {"ETag": '"etag-1"', "PartNumber": 1},
        {"ETag": '"etag-2"', "PartNumber": 2},
        {"ETag": '"etag-3"', "PartNumber": 3},
    ]


@pytest.mark.asyncio
async def test_s3_part_failure_aborts_and_raises() -> None:
    client = _FakeMultipartClient(fail_part=2)
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=4)
    with pytest.raises(ObjectStoreError):
        await store.put_stream("k", _chunks(b"abcdefghij"))
    assert client.aborted
    assert not client.completed


@pytest.mark.asyncio
async def test_s3_producer_error_aborts_and_propagates() -> None:
    """chunks 迭代器自己炸(打包线程错误传播路径)→ abort + 原样抛。"""

    async def _boom() -> AsyncIterator[bytes]:
        yield b"abcd"
        raise RuntimeError("tar failed")

    client = _FakeMultipartClient()
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=4)
    with pytest.raises(RuntimeError, match="tar failed"):
        await store.put_stream("k", _boom())
    assert client.aborted


# ---------------------------------------------------------------------------
# get() — non-NoSuchKey ClientError must wrap into ObjectStoreError too
# (external-upload-unification 终审修复 A1: ``put`` / ``put_stream`` /
# ``list_prefix`` already give this guarantee; ``get`` didn't).
# ---------------------------------------------------------------------------


class _FakeGetExceptions:
    class ClientError(Exception):
        pass

    class _NoSuchKeyError(ClientError):
        pass

    NoSuchKey = _NoSuchKeyError


class _FakeGetClient:
    """Minimal fake exposing only what ``get`` touches — a distinct
    ``NoSuchKey`` subclass of ``ClientError`` (unlike ``_FakeExceptions``
    above, which aliases them) so the two branches can be told apart."""

    def __init__(self, *, error: Exception) -> None:
        self.exceptions = _FakeGetExceptions()
        self._error = error

    async def get_object(self, **kw: Any) -> dict[str, Any]:
        raise self._error


@pytest.mark.asyncio
async def test_s3_get_wraps_non_not_found_client_error() -> None:
    """凭证 / bucket 策略 / 存储侧 5xx 等「对象没丢,但读不动」的错误必须
    包成 ``ObjectStoreError``,不能让裸 botocore 异常逃逸——``NoSuchKey`` 分支
    已经处理「对象丢了」,这里测的是它的兄弟分支。"""
    client = _FakeGetClient(error=_FakeGetExceptions.ClientError("access denied"))
    store = S3CompatibleObjectStore(client=client, bucket="b")
    with pytest.raises(ObjectStoreError) as exc_info:
        await store.get("k")
    assert not isinstance(exc_info.value, ObjectNotFoundError)


@pytest.mark.asyncio
async def test_s3_get_still_raises_object_not_found_for_no_such_key() -> None:
    """回归:``NoSuchKey`` 分支没被上面新加的 ``except ClientError`` 挤掉
    ——顺序反了这条就会红(``NoSuchKey`` 是 ``ClientError`` 的子类)。"""
    client = _FakeGetClient(error=_FakeGetExceptions.NoSuchKey("not found"))
    store = S3CompatibleObjectStore(client=client, bucket="b")
    with pytest.raises(ObjectNotFoundError):
        await store.get("k")
