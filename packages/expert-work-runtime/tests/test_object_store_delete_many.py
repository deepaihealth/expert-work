"""B-209 PR-2 —— ``delete_many``: a document's / base's figures go in batches,
not one request per object (S3 ``DeleteObjects`` takes 1,000 keys)."""

from __future__ import annotations

from typing import Any

import pytest

from expert_work.runtime.storage import (
    InMemoryObjectStore,
    ObjectStoreError,
    S3CompatibleObjectStore,
)


async def test_memory_delete_many_removes_only_the_keys_given() -> None:
    store = InMemoryObjectStore()
    for key in ("a/1", "a/2", "b/1"):
        await store.put(key, b"x")
    await store.delete_many(["a/1", "a/2", "missing"])
    assert await store.list_prefix("") == ["b/1"]


class _FakeClient:
    def __init__(self, *, errors: list[dict[str, str]] | None = None) -> None:
        self.batches: list[list[str]] = []
        self._errors = errors or []

    async def delete_objects(self, **kw: Any) -> dict[str, Any]:
        assert kw["Bucket"] == "bucket"
        assert kw["Delete"]["Quiet"] is True
        self.batches.append([item["Key"] for item in kw["Delete"]["Objects"]])
        return {"Errors": self._errors}


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
