"""``S3CompatibleObjectStore`` — aiobotocore-backed implementation.

Works against MinIO (local dev), Aliyun OSS (prod / staging), AWS S3,
Tencent COS, and any other S3-compatible endpoint. The choice of backend
is communicated entirely through ``endpoint_url`` + region + credentials
at construction time; no application code changes for migrations.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, Literal

from expert_work.runtime.storage.base import (
    LockMode,
    ObjectNotFoundError,
    ObjectStoreError,
    validate_lock_args,
)

logger = logging.getLogger(__name__)

#: S3 multipart 分片大小(spec § 4.2:常驻内存 ≤ 单分片;S3 硬下限 5 MiB,
#: 10k 片上限 → 64 MiB x 10k = 640 GiB 单对象天花板,远超工作区配额量级)。
_MULTIPART_PART_SIZE = 64 * 1024 * 1024


class S3CompatibleObjectStore:
    """Operate on one S3-style bucket via an injected aiobotocore client.

    The client lifecycle is managed by the factory (``make_object_store``)
    so this class stays a pure operations surface — no connection state,
    no global session. ``client`` is typed ``Any`` because aiobotocore
    ships no py.typed marker; the runtime methods used are the standard
    S3 API and are stable across botocore releases.
    """

    def __init__(
        self, client: Any, bucket: str, *, multipart_part_size: int = _MULTIPART_PART_SIZE
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._multipart_part_size = multipart_part_size

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        retain_until: datetime | None = None,
        lock_mode: LockMode | None = None,
    ) -> None:
        validate_lock_args(retain_until, lock_mode)
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type is not None:
            kwargs["ContentType"] = content_type
        if metadata:
            # S3 metadata keys must be ASCII; let the SDK validate. Pass
            # through verbatim — callers carry the contract.
            kwargs["Metadata"] = dict(metadata)
        if retain_until is not None and lock_mode is not None:
            # ``ObjectLockRetainUntilDate`` accepts a datetime; aiobotocore
            # serializes to ISO 8601 with the offset. S3 / MinIO require
            # the bucket itself to have Object Lock enabled at create-time;
            # if it isn't, the put surfaces an InvalidRequest which we
            # forward as ObjectStoreError (no clean way to anticipate
            # without a HEAD trip). ``ObjectLockMode`` is upper-case.
            kwargs["ObjectLockRetainUntilDate"] = retain_until
            kwargs["ObjectLockMode"] = lock_mode.upper()
        try:
            await self._client.put_object(**kwargs)
        except self._client.exceptions.ClientError as exc:
            # A retention-protected re-put on the same key surfaces as
            # ``AccessDenied``; bucket-not-Object-Lock-enabled as
            # ``InvalidRequest``. Wrap both into a single class so call
            # sites don't have to know about botocore exception shapes.
            msg = f"put_object failed for key={key!r}: {exc}"
            raise ObjectStoreError(msg) from exc

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        part_size = self._multipart_part_size
        buf = bytearray()
        upload_id: str | None = None
        parts: list[dict[str, Any]] = []

        async def _flush_part(payload: bytes) -> None:
            nonlocal upload_id
            if upload_id is None:
                create_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
                if content_type is not None:
                    create_kwargs["ContentType"] = content_type
                created = await self._client.create_multipart_upload(**create_kwargs)
                upload_id = created["UploadId"]
            number = len(parts) + 1
            resp = await self._client.upload_part(
                Bucket=self._bucket, Key=key, PartNumber=number,
                UploadId=upload_id, Body=payload,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": number})

        try:
            async for chunk in chunks:
                buf.extend(chunk)
                while len(buf) >= part_size:
                    await _flush_part(bytes(buf[:part_size]))
                    del buf[:part_size]
            if upload_id is None:
                # 整流不足一片(含空流)——单次 put 更省一轮 multipart 往返。
                put_kwargs: dict[str, Any] = {
                    "Bucket": self._bucket, "Key": key, "Body": bytes(buf),
                }
                if content_type is not None:
                    put_kwargs["ContentType"] = content_type
                await self._client.put_object(**put_kwargs)
                return
            if buf:
                await _flush_part(bytes(buf))
            await self._client.complete_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except self._client.exceptions.ClientError as exc:
            await self._abort_quietly(key, upload_id)
            msg = f"put_stream failed for key {key!r}"
            raise ObjectStoreError(msg) from exc
        except BaseException:
            # 生产侧(tar 线程)异常原样上抛,但 multipart 残骸要清。
            await self._abort_quietly(key, upload_id)
            raise

    async def _abort_quietly(self, key: str, upload_id: str | None) -> None:
        if upload_id is None:
            return
        try:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id
            )
        except Exception:  # abort 尽力而为,别掩埋原始异常
            logger.warning("put_stream.abort_failed key=%s", key)

    async def get(self, key: str) -> bytes:
        try:
            response = await self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey as exc:
            msg = f"object not found: {key!r}"
            raise ObjectNotFoundError(msg) from exc

        async with response["Body"] as stream:
            data: bytes = await stream.read()
            return data

    async def delete(self, key: str) -> None:
        # S3 DeleteObject is idempotent — no error when key is absent —
        # matching the Protocol contract.
        await self._client.delete_object(Bucket=self._bucket, Key=key)

    async def list_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                k = obj.get("Key")
                if k is not None:
                    keys.append(k)
        keys.sort()
        return keys

    async def presigned_url(
        self,
        key: str,
        *,
        expires_in: int = 3600,
        method: Literal["GET", "PUT"] = "GET",
    ) -> str:
        operation = "get_object" if method == "GET" else "put_object"
        try:
            url = await self._client.generate_presigned_url(
                ClientMethod=operation,
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:
            # ``generate_presigned_url`` shouldn't raise for valid inputs,
            # but credentials / endpoint misconfig surface here; wrap so
            # callers see one exception class.
            msg = f"failed to sign URL for key={key!r}: {exc}"
            raise ObjectStoreError(msg) from exc

        # botocore returns ``str``; assert for the type checker since the
        # stub types are slightly loose.
        if not isinstance(url, str):  # pragma: no cover — defensive
            msg = f"expected str URL, got {type(url).__name__}"
            raise ObjectStoreError(msg)
        return url
