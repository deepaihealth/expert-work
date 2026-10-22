"""B-209 PR-2 —— ``delete_many``: a document's / base's figures go in batches,
not one request per object (S3 ``DeleteObjects`` takes 1,000 keys)."""

from __future__ import annotations

import base64
import hashlib
import types
from datetime import UTC, datetime
from typing import Any

import botocore.session
import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError

from expert_work.runtime.storage import (
    InMemoryObjectStore,
    ObjectStoreError,
    S3CompatibleObjectStore,
)
from expert_work.runtime.storage.s3_compatible import register_oss_compat


async def test_memory_delete_many_removes_only_the_keys_given() -> None:
    store = InMemoryObjectStore()
    for key in ("a/1", "a/2", "b/1"):
        await store.put(key, b"x")
    await store.delete_many(["a/1", "a/2", "missing"])
    assert await store.list_prefix("") == ["b/1"]


def _client_error(operation: str, code: str = "InvalidDigest") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


class _FakeClient:
    exceptions = types.SimpleNamespace(ClientError=ClientError)

    def __init__(
        self,
        *,
        errors: list[dict[str, str]] | None = None,
        reject_batches: bool | str = False,
        reject_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.batches: list[list[str]] = []
        self.single: list[str] = []
        self._errors = errors or []
        self._reject_batches = reject_batches
        self._reject_keys = reject_keys

    async def delete_objects(self, **kw: Any) -> dict[str, Any]:
        assert kw["Bucket"] == "bucket"
        assert kw["Delete"]["Quiet"] is True
        keys = [item["Key"] for item in kw["Delete"]["Objects"]]
        if self._reject_batches:
            code = (
                self._reject_batches if isinstance(self._reject_batches, str) else "InvalidDigest"
            )
            raise _client_error("DeleteObjects", code)
        self.batches.append(keys)
        return {"Errors": [error for error in self._errors if error["Key"] in keys]}

    async def delete_object(self, **kw: Any) -> dict[str, Any]:
        if kw["Key"] in self._reject_keys:
            raise _client_error("DeleteObject")
        self.single.append(kw["Key"])
        return {}


async def test_s3_delete_many_sends_batches_of_a_thousand() -> None:
    client = _FakeClient()
    store = S3CompatibleObjectStore(client=client, bucket="bucket")
    keys = [f"k/{n}" for n in range(2_001)]
    await store.delete_many(keys)
    assert [len(batch) for batch in client.batches] == [1000, 1000, 1]
    assert [key for batch in client.batches for key in batch] == keys


async def test_s3_delete_many_reports_keys_it_could_not_delete() -> None:
    client = _FakeClient(errors=[{"Key": "k/1", "Code": "AccessDenied"}])
    store = S3CompatibleObjectStore(client=client, bucket="bucket")
    with pytest.raises(ObjectStoreError, match="1 keys"):
        await store.delete_many(["k/1"])


async def test_a_rejected_batch_is_deleted_key_by_key() -> None:
    # Review H3: an S3-compatible service refusing DeleteObjects as botocore
    # builds it costs requests, not the delete.
    client = _FakeClient(reject_batches=True)
    store = S3CompatibleObjectStore(client=client, bucket="bucket")
    await store.delete_many(["k/1", "k/2"])
    assert client.single == ["k/1", "k/2"]


async def test_a_refused_single_delete_is_an_object_store_error() -> None:
    client = _FakeClient(reject_batches=True, reject_keys=frozenset({"k/1"}))
    store = S3CompatibleObjectStore(client=client, bucket="bucket")
    with pytest.raises(ObjectStoreError, match="k/1"):
        await store.delete_many(["k/1", "k/2"])
    assert client.single == ["k/2"]  # a refused key does not stop the rest (L9)
    with pytest.raises(ObjectStoreError):
        await store.delete("k/1")


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_: Any) -> Any:
        yield self._body


_DELETE_RESULT = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></DeleteResult>'
)


def _text(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


def test_a_batch_delete_carries_a_signed_content_md5() -> None:
    # Review H3: Aliyun OSS requires Content-MD5 on a batch delete; botocore
    # only sends a CRC32. The factory's client hooks it in before signing.
    client = botocore.session.get_session().create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://storage.test",
        aws_access_key_id="key",
        aws_secret_access_key="secret",  # a dummy: nothing is sent
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    register_oss_compat(client.meta.events)
    sent: list[Any] = []

    def capture(request: Any, **_: Any) -> AWSResponse:
        sent.append(request)
        return AWSResponse(request.url, 200, {}, _Raw(_DELETE_RESULT))

    client.meta.events.register("before-send.s3.DeleteObjects", capture)
    client.delete_objects(Bucket="bucket", Delete={"Objects": [{"Key": "a"}], "Quiet": True})
    (request,) = sent
    body = request.body if isinstance(request.body, bytes) else request.body.encode()
    expected = base64.b64encode(hashlib.md5(body).digest()).decode()  # noqa: S324
    assert _text(request.headers["Content-MD5"]) == expected
    authorization = _text(request.headers["Authorization"])
    assert "content-md5" in authorization.split("SignedHeaders=")[1].split(",")[0].split(";")


async def test_listing_with_modification_times() -> None:
    store = InMemoryObjectStore()
    before = datetime.now(UTC)
    await store.put("a/2", b"x")
    await store.put("a/1", b"x")
    await store.put("b/1", b"x")
    listed = await store.list_prefix_modified("a/")
    assert [key for key, _ in listed] == ["a/1", "a/2"]
    assert all(before <= modified <= datetime.now(UTC) for _, modified in listed)


@pytest.mark.parametrize("code", ["AccessDenied", "SlowDown"])
async def test_only_a_rejected_request_shape_falls_back_to_single_deletes(code: str) -> None:
    # Re-review round 4 (H3): a refused or throttled batch is raised, not
    # turned into a thousand more requests.
    client = _FakeClient(reject_batches=code)
    store = S3CompatibleObjectStore(client=client, bucket="bucket")
    with pytest.raises(ObjectStoreError, match=code):
        await store.delete_many(["k/1", "k/2"])
    assert client.single == []


async def test_every_batch_is_sent_before_the_failures_are_raised() -> None:
    # Re-review round 4 (L9): keys refused in the first batch do not stop the
    # second; one error names them all.
    client = _FakeClient(errors=[{"Key": "k/0", "Code": "AccessDenied"}, {"Key": "k/1500"}])
    store = S3CompatibleObjectStore(client=client, bucket="bucket")
    with pytest.raises(ObjectStoreError, match=r"2 keys \(first: 'k/0'\)"):
        await store.delete_many([f"k/{n}" for n in range(2_001)])
    assert [len(batch) for batch in client.batches] == [1000, 1000, 1]
