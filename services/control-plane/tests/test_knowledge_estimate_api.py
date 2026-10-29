"""B-222 PR-2 —— every paid ingest is estimated first and starts only once confirmed.

* every entry point (upload, same-name re-upload, re-ingest, 「补识别」, batch
  re-ingest, re-ingest-stale) lands in ``estimating``; the estimate calls no
  model and the document then waits ``awaiting_confirmation`` — ¥0.00 too;
* bytes another document of the same base holds wait as a duplicate, unparsed;
* ``POST …/documents/confirm`` by ``(document_id, estimate_id)`` (a
  compare-and-set) or ``all`` with the count and total the user saw (409 with
  the new summary otherwise; duplicates withdrawn with it);
* ``POST …/documents/{id}/estimate`` ingests a duplicate anyway;
* ``POST …/documents/cancel`` withdraws an estimate (a new upload deleted);
* the list carries the public estimate, ``confirm_by``, ``ingest_actual``, the
  base's ``estimate_summary`` and a ``document_id`` filter;
* writes are audited, ``manifest:write`` only, the caller's own tenant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery, IngestStage
from orchestrator.llm import FakeEmbedder
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)
from tests.knowledge_confirm import confirm_all, settle

_TENANT = DEFAULT_DEV_TENANT_ID
_DOCS = "/v1/knowledge/bases/kb/documents"
_MD = b"# Handbook\n\nThe deductible is 500 dollars per year."


def _headers(*roles: str) -> dict[str, str]:
    token = make_test_jwt(tenant_id=_TENANT, subject="user-a", roles=roles or ("admin",))
    return {"Authorization": f"Bearer {token}"}


class _CountingEmbedder(FakeEmbedder):
    """Counts the texts it is asked to embed (an estimate must ask for none)."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.embedded += len(texts)
        return await super().embed(texts, tenant_id=tenant_id)


class _Runner(KnowledgeIngestionRunner):
    """The real runner; also records what ``cancel_local`` was asked to stop."""

    def __init__(self, store: InMemoryKnowledgeStore, embedder: FakeEmbedder) -> None:
        super().__init__(store=store, embedder=embedder)
        self.stopped: list[UUID] = []

    def cancel_local(self, document_ids: Collection[UUID]) -> int:
        self.stopped.extend(document_ids)
        return super().cancel_local(document_ids)


class _Harness:
    def __init__(
        self,
        client: AsyncClient,
        store: InMemoryKnowledgeStore,
        runner: _Runner,
        embedder: _CountingEmbedder,
        audit: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.store = store
        self.runner = runner
        self.embedder = embedder
        self.audit = audit

    async def upload(self, name: str, body: bytes, base: str = "kb") -> dict[str, Any]:
        response = await self.client.post(
            f"/v1/knowledge/bases/{base}/documents", files={"file": (name, body, "text/markdown")}
        )
        assert response.status_code in (200, 202), response.text
        await self.runner.drain()  # the estimate runs
        data: dict[str, Any] = response.json()
        data["_status"] = response.status_code
        return data

    async def documents(self, base: str = "kb", **params: Any) -> list[dict[str, Any]]:
        response = await self.client.get(f"/v1/knowledge/bases/{base}/documents", params=params)
        assert response.status_code == 200, response.text
        documents: list[dict[str, Any]] = response.json()["documents"]
        return documents

    async def row(self, document_id: str) -> dict[str, Any]:
        (row,) = await self.documents(document_id=document_id)
        return row

    async def summary(self) -> dict[str, Any]:
        response = await self.client.get(_DOCS)
        summary: dict[str, Any] = response.json()["estimate_summary"]
        return summary

    async def audit_rows(self, action: str) -> list[tuple[str | None, str, dict[str, Any]]]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT, limit=200))
        return [
            (e.resource_id, e.actor_id, dict(e.details))
            for e in reversed(page.entries)
            if str(e.action) == action
        ]


async def _harness(*, with_runner: bool = True) -> AsyncIterator[_Harness]:
    store = InMemoryKnowledgeStore()
    embedder = _CountingEmbedder()
    runner = _Runner(store, embedder)
    audit = InMemoryAuditLogStore()
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner if with_runner else None,
        audit_logger=build_default_audit_logger(audit),
        jwt_verifier=build_test_jwt_verifier(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=_headers()
    ) as client:
        for name in ("kb", "other"):
            assert (
                await client.post("/v1/knowledge/bases", json={"name": name})
            ).status_code == 201
        yield _Harness(client, store, runner, embedder, audit)


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    async for built in _harness():
        yield built


@pytest.fixture
async def no_runner() -> AsyncIterator[_Harness]:
    async for built in _harness(with_runner=False):
        yield built


# -- the upload waits for its confirmation ----------------------------------------


async def test_an_upload_is_estimated_without_a_model_and_waits_even_at_zero(
    harness: _Harness,
) -> None:
    uploaded = await harness.upload("h.md", _MD)
    assert (uploaded["_status"], uploaded["status"]) == (202, "estimating")

    row = await harness.row(uploaded["id"])
    estimate = row["ingest_estimate"]
    assert row["status"] == "awaiting_confirmation"
    assert (row["chunk_count"], harness.embedder.embedded) == (0, 0)  # nothing ingested, no model
    # ¥0.00 (nothing priced here) still waits for a confirmation.
    assert (estimate["total_micros"], estimate["origin"], estimate["estimated"]) == (
        0,
        "upload",
        True,
    )
    assert row["confirm_by"] is not None and row["ingest_actual"] is None
    assert "restore" not in estimate  # the store-only part is never shown
    summary = await harness.summary()
    assert len(summary.pop("fingerprint")) == 64
    assert summary == {
        "estimating": 0,
        "awaiting": 1,
        "duplicates": 0,
        "expired": 0,
        "total_micros": 0,
        "unpriced": True,
        "earliest_confirm_by": row["confirm_by"],
    }
    upload_audit = await harness.audit_rows("knowledge:document_upload")
    assert upload_audit[0][2]["outcome"] == "estimating"


async def test_a_confirmed_upload_is_ingested_and_keeps_its_actual(harness: _Harness) -> None:
    uploaded = await harness.upload("h.md", _MD)
    estimate_id = (await harness.row(uploaded["id"]))["ingest_estimate"]["id"]

    response = await harness.client.post(
        f"{_DOCS}/confirm",
        json={"documents": [{"document_id": uploaded["id"], "estimate_id": estimate_id}]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"results": [{"document_id": uploaded["id"], "outcome": "confirmed"}]}
    await harness.runner.drain()

    row = await harness.row(uploaded["id"])
    assert (row["status"], row["chunk_count"] > 0, harness.embedder.embedded > 0) == (
        "ready",
        True,
        True,
    )
    assert row["ingest_estimate"]["confirmed_by"] == "user-a"
    assert (row["ingest_actual"]["estimate_id"], row["ingest_actual"]["outcome"]) == (
        estimate_id,
        "ready",
    )
    assert await harness.audit_rows("knowledge:document_confirm") == [
        (
            uploaded["id"],
            "user-a",
            {"kb": "kb", "filename": "h.md", "estimate_id": estimate_id, "total_micros": 0},
        )
    ]


async def test_confirm_by_list_says_why_each_was_not_confirmed(harness: _Harness) -> None:
    waiting = await harness.upload("a.md", _MD)
    copy = await harness.upload("b.md", _MD)  # same bytes: a duplicate
    ready = await harness.upload("c.md", b"# C\n\ntext c")
    await confirm_all(harness.client)
    await harness.runner.drain()
    elsewhere = await harness.upload("x.md", b"# X\n\ntext x", base="other")
    copy_estimate = (await harness.row(copy["id"]))["ingest_estimate"]["id"]
    asked = [
        (waiting["id"], str(uuid4())),  # not the estimate it waits with
        (copy["id"], copy_estimate),
        (ready["id"], str(uuid4())),
        (elsewhere["id"], str(uuid4())),
    ]
    # ``waiting`` was confirmed by the confirm-all above: re-uploaded changed,
    # it waits again under a new estimate.
    reupload = await harness.upload("a.md", _MD + b"\n\nMore.")
    assert reupload["id"] == waiting["id"]
    confirmed_before = len(await harness.audit_rows("knowledge:document_confirm"))

    response = await harness.client.post(
        f"{_DOCS}/confirm",
        json={"documents": [{"document_id": d, "estimate_id": e} for d, e in asked]},
    )

    assert response.status_code == 200, response.text
    assert {r["document_id"]: r["outcome"] for r in response.json()["results"]} == {
        waiting["id"]: "estimate_changed",
        copy["id"]: "duplicate",
        ready["id"]: "not_awaiting",
        elsewhere["id"]: "not_found",
    }
    # Nothing confirmed, nothing audited, nothing started.
    assert len(await harness.audit_rows("knowledge:document_confirm")) == confirmed_before
    assert (await harness.row(waiting["id"]))["status"] == "awaiting_confirmation"


async def test_confirm_all_needs_what_the_user_saw(harness: _Harness) -> None:
    first = await harness.upload("a.md", b"# A\n\ntext a")
    second = await harness.upload("b.md", b"# B\n\ntext b")
    copy = await harness.upload("c.md", b"# A\n\ntext a")
    summary = await harness.summary()
    assert (summary["awaiting"], summary["duplicates"]) == (2, 1)

    for wrong in (
        {"expected_count": 1, "expected_fingerprint": summary["fingerprint"]},
        {"expected_count": 2, "expected_fingerprint": "0" * 64},  # not the documents shown
    ):
        stale = await harness.client.post(
            f"{_DOCS}/confirm", json={"all": True, "expected_total_micros": 0, **wrong}
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["error"] == "estimate_changed"
        assert stale.json()["detail"]["estimate_summary"]["awaiting"] == 2
    assert {d["status"] for d in await harness.documents()} == {"awaiting_confirmation"}
    # Another copy arrives after the user was shown the first one.
    late = await harness.upload("d.md", b"# A\n\ntext a")

    done = await harness.client.post(
        f"{_DOCS}/confirm",
        json={
            "all": True,
            "expected_count": 2,
            "expected_total_micros": 0,
            "expected_fingerprint": summary["fingerprint"],
            "withdraw_duplicate_ids": [copy["id"]],
        },
    )
    assert done.status_code == 200, done.text
    assert sorted(done.json()["confirmed"]) == sorted([first["id"], second["id"]])
    # The duplicate shown is withdrawn with it (a new upload: deleted); the
    # late one waits.
    assert done.json()["withdrawn"] == [{"document_id": copy["id"], "outcome": "withdrawn_deleted"}]
    await harness.runner.drain()
    assert {d["id"]: d["status"] for d in await harness.documents()} == {
        first["id"]: "ready",
        second["id"]: "ready",
        late["id"]: "awaiting_confirmation",
    }
    assert [r[0] for r in await harness.audit_rows("knowledge:document_withdraw")] == [copy["id"]]
    assert sorted(r[0] or "" for r in await harness.audit_rows("knowledge:document_confirm")) == (
        sorted([first["id"], second["id"]])
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"all": True},
        {"all": True, "expected_count": 1},
        {"all": True, "expected_count": 1, "expected_total_micros": 0},  # no fingerprint
        {"all": True, "expected_count": 1, "expected_total_micros": 0, "documents": []},
        {"documents": []},
        {"documents": [{"document_id": str(uuid4()), "estimate_id": str(uuid4())}], "all": True},
        # Both forms at once, each complete.
        {
            "documents": [{"document_id": str(uuid4()), "estimate_id": str(uuid4())}],
            "all": True,
            "expected_count": 0,
            "expected_total_micros": 0,
            "expected_fingerprint": "x",
        },
    ],
)
async def test_a_confirm_is_a_list_or_all_with_its_totals(
    harness: _Harness, body: dict[str, Any]
) -> None:
    response = await harness.client.post(f"{_DOCS}/confirm", json=body)
    assert response.status_code == 422, response.text


# -- duplicates (K-21 ①) ------------------------------------------------------------


async def test_a_duplicate_waits_unparsed_and_can_be_ingested_anyway(harness: _Harness) -> None:
    original = await harness.upload("a.md", _MD)
    copy = await harness.upload("copy.md", _MD)
    elsewhere = await harness.upload("copy.md", _MD, base="other")

    row = await harness.row(copy["id"])
    assert (copy["_status"], copy["status"]) == (202, "awaiting_confirmation")
    duplicate_of = row["ingest_estimate"]["duplicate_of"]
    assert (duplicate_of["id"], duplicate_of["filename"]) == (original["id"], "a.md")
    assert row["ingest_estimate"]["estimated"] is False
    # Same bytes in another base: not a duplicate.
    (other,) = await harness.documents(base="other")
    assert (other["id"], other["ingest_estimate"]["duplicate_of"]) == (elsewhere["id"], None)

    anyway = await harness.client.post(f"{_DOCS}/{copy['id']}/estimate")
    assert anyway.status_code == 202, anyway.text
    assert anyway.json()["status"] == "estimating"
    await harness.runner.drain()
    row = await harness.row(copy["id"])
    assert row["status"] == "awaiting_confirmation"
    assert (row["ingest_estimate"]["estimated"], row["ingest_estimate"]["allow_duplicate"]) == (
        True,
        True,
    )
    assert row["ingest_estimate"]["duplicate_of"] is None
    rows = await harness.audit_rows("knowledge:document_reingest")
    assert rows == [
        (copy["id"], "user-a", {"kb": "kb", "filename": "copy.md", "ingest_anyway": True})
    ]
    await settle(harness.client, harness.runner)
    assert (await harness.row(copy["id"]))["status"] == "ready"


async def test_ingest_anyway_is_only_for_a_waiting_duplicate(harness: _Harness) -> None:
    waiting = await harness.upload("a.md", _MD)
    for document_id, expected in ((waiting["id"], "not_duplicate"),):
        response = await harness.client.post(f"{_DOCS}/{document_id}/estimate")
        assert (response.status_code, response.json()["detail"]["error"]) == (409, expected)
    await settle(harness.client, harness.runner)
    ready = await harness.client.post(f"{_DOCS}/{waiting['id']}/estimate")
    assert (ready.status_code, ready.json()["detail"]["error"]) == (409, "not_awaiting")
    elsewhere = await harness.upload("x.md", b"# X\n\nx", base="other")
    for document_id in (elsewhere["id"], str(uuid4())):
        assert (await harness.client.post(f"{_DOCS}/{document_id}/estimate")).status_code == 404


# -- every entry point estimates first ---------------------------------------------


async def test_reupload_reingest_and_batch_estimate_first_and_keep_serving(
    harness: _Harness,
) -> None:
    uploaded = await harness.upload("h.md", _MD)
    await settle(harness.client, harness.runner)
    embedded = harness.embedder.embedded

    reupload = await harness.upload("h.md", _MD + b"\n\nMore.")
    assert (reupload["_status"], reupload["status"]) == (202, "estimating")
    row = await harness.row(uploaded["id"])
    assert (row["status"], row["ingest_estimate"]["origin"], row["chunk_count"] > 0) == (
        "awaiting_confirmation",
        "reupload",
        True,
    )
    # The same new bytes again while they wait: nothing changes.
    again = await harness.upload("h.md", _MD + b"\n\nMore.")
    assert (again["_status"], again["unchanged"]) == (200, True)
    assert (await harness.row(uploaded["id"]))["ingest_estimate"]["id"] == row["ingest_estimate"][
        "id"
    ]
    await settle(harness.client, harness.runner)

    for path, origin in (
        (f"{_DOCS}/{uploaded['id']}/reingest", "reingest"),
        (f"{_DOCS}/batch", "batch_reingest"),
    ):
        body = {"action": "reingest", "document_ids": [uploaded["id"]]} if "batch" in path else None
        response = await harness.client.post(path, json=body)
        assert response.status_code in (200, 202), response.text
        await harness.runner.drain()
        row = await harness.row(uploaded["id"])
        assert (row["status"], row["ingest_estimate"]["origin"]) == (
            "awaiting_confirmation",
            origin,
        ), path
        await settle(harness.client, harness.runner)
    # Nothing was embedded before a confirmation: only the three confirmed ingests did.
    assert harness.embedder.embedded > embedded


async def test_no_estimate_starts_without_a_runner(no_runner: _Harness) -> None:
    for path, body in (
        (
            f"{_DOCS}/confirm",
            {
                "all": True,
                "expected_count": 0,
                "expected_total_micros": 0,
                "expected_fingerprint": "",
            },
        ),
        (f"{_DOCS}/{uuid4()}/estimate", None),
    ):
        response = await no_runner.client.post(path, json=body)
        assert response.status_code == 503, (path, response.text)


# -- withdrawing --------------------------------------------------------------------


async def test_cancel_withdraws_estimates(harness: _Harness) -> None:
    kept = await harness.upload("kept.md", _MD)
    await settle(harness.client, harness.runner)
    new = await harness.upload("new.md", b"# New\n\nnew text")
    reupload = await harness.upload("kept.md", _MD + b"\n\nMore.")
    assert reupload["id"] == kept["id"]

    response = await harness.client.post(
        f"{_DOCS}/cancel", json={"document_ids": [new["id"], kept["id"]]}
    )

    assert response.json()["results"] == [
        {"document_id": new["id"], "outcome": "withdrawn_deleted"},
        {"document_id": kept["id"], "outcome": "withdrawn"},
    ]
    rows = {d["id"]: d for d in await harness.documents()}
    assert set(rows) == {kept["id"]}  # the new upload is gone, file and all
    assert (rows[kept["id"]]["status"], rows[kept["id"]]["has_pending"]) == ("ready", False)
    assert set(harness.runner.stopped) == {UUID(new["id"]), UUID(kept["id"])}
    assert await harness.audit_rows("knowledge:document_withdraw") == [
        (new["id"], "user-a", {"kb": "kb", "outcome": "withdrawn_deleted"}),
        (kept["id"], "user-a", {"kb": "kb", "outcome": "withdrawn"}),
    ]
    assert await harness.audit_rows("knowledge:document_cancel") == []


# -- reading ------------------------------------------------------------------------


async def test_the_list_filters_by_document_id(harness: _Harness) -> None:
    a = await harness.upload("a.md", b"# A\n\ntext a")
    b = await harness.upload("b.md", b"# B\n\ntext b")
    elsewhere = await harness.upload("x.md", b"# X\n\nx", base="other")
    ids = [b["id"], elsewhere["id"], str(uuid4()), a["id"], b["id"]]
    listed = await harness.client.get(_DOCS, params={"document_id": ids})
    assert [d["id"] for d in listed.json()["documents"]] == [b["id"], a["id"]]
    assert listed.json()["total"] == 2
    paged = await harness.client.get(_DOCS, params={"document_id": ids, "limit": 1, "offset": 1})
    assert [d["id"] for d in paged.json()["documents"]] == [a["id"]]
    narrowed = await harness.client.get(_DOCS, params={"document_id": ids, "status": "ready"})
    assert narrowed.json()["documents"] == []
    too_many = await harness.client.get(_DOCS, params={"document_id": [str(uuid4())] * 201})
    assert too_many.status_code == 422


async def test_another_tenant_reads_the_estimate_but_cannot_confirm(harness: _Harness) -> None:
    uploaded = await harness.upload("h.md", _MD)
    estimate_id = (await harness.row(uploaded["id"]))["ingest_estimate"]["id"]
    admin = await grant_system_admin(harness.client)

    read = await harness.client.get(_DOCS, params={"tenant_id": str(_TENANT)}, headers=admin)
    assert read.status_code == 200, read.text
    (row,) = read.json()["documents"]
    assert (row["ingest_estimate"]["id"], row["confirm_by"] is not None) == (estimate_id, True)
    assert read.json()["estimate_summary"]["awaiting"] == 1
    # Writes go to the caller's own tenant, which has no such base.
    confirm = {"documents": [{"document_id": uploaded["id"], "estimate_id": estimate_id}]}
    for path, body in ((f"{_DOCS}/confirm", confirm), (f"{_DOCS}/{uploaded['id']}/estimate", None)):
        response = await harness.client.post(
            path, json=body, params={"tenant_id": str(_TENANT)}, headers=admin
        )
        assert response.status_code == 404, (path, response.text)
    assert (await harness.row(uploaded["id"]))["status"] == "awaiting_confirmation"


async def test_a_viewer_cannot_confirm(harness: _Harness) -> None:
    uploaded = await harness.upload("h.md", _MD)
    estimate_id = (await harness.row(uploaded["id"]))["ingest_estimate"]["id"]
    for path, body in (
        (
            f"{_DOCS}/confirm",
            {"documents": [{"document_id": uploaded["id"], "estimate_id": estimate_id}]},
        ),
        (f"{_DOCS}/{uploaded['id']}/estimate", None),
    ):
        response = await harness.client.post(path, json=body, headers=_headers("viewer"))
        assert response.status_code == 403, (path, response.text)
    assert (await harness.row(uploaded["id"]))["status"] == "awaiting_confirmation"


# -- B2b review fixes -------------------------------------------------------------


async def _indexing(harness: _Harness) -> str:
    """A ready document whose re-upload is having its chunks swapped in."""
    uploaded = await harness.upload("h.md", _MD)
    await settle(harness.client, harness.runner)
    base = await harness.store.get_base(tenant_id=_TENANT, name="kb")
    assert base is not None
    await harness.store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="h.md", content=b"# v2", content_sha256="v2"
    )
    document_id = UUID(uploaded["id"])
    claim = await harness.store.claim_document(
        tenant_id=_TENANT,
        document_id=document_id,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None
    assert await harness.store.set_document_stage(
        tenant_id=_TENANT,
        document_id=document_id,
        stage=IngestStage.INDEXING,
        generation=claim.generation,
    )
    return str(document_id)


async def test_nothing_restarts_a_document_writing_its_chunks(harness: _Harness) -> None:
    """Review M1: 409 ``document_busy`` (a batch skips it as ``busy``)."""
    document_id = await _indexing(harness)
    for response in (
        await harness.client.post(_DOCS, files={"file": ("h.md", b"# v3", "text/markdown")}),
        await harness.client.post(f"{_DOCS}/{document_id}/reingest"),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "document_busy"
    batch = await harness.client.post(
        f"{_DOCS}/batch", json={"action": "reingest", "document_ids": [document_id]}
    )
    assert batch.json()["skipped"] == [{"id": document_id, "reason": "busy"}]
    row = await harness.row(document_id)
    assert (row["status"], row["stage"]) == ("processing", "indexing")


async def test_withdrawing_or_deleting_an_original_frees_its_copies(harness: _Harness) -> None:
    """Review M5 (§7): the copy is estimated as a plain upload."""
    for remove in ("cancel", "delete"):
        original = await harness.upload(f"{remove}.md", f"# {remove}".encode())
        copy = await harness.upload(f"{remove}-copy.md", f"# {remove}".encode())
        assert (await harness.row(copy["id"]))["ingest_estimate"]["duplicate_of"] is not None
        if remove == "cancel":
            await harness.client.post(f"{_DOCS}/cancel", json={"document_ids": [original["id"]]})
        else:
            await harness.client.delete(f"{_DOCS}/{original['id']}")
        await harness.runner.drain()
        row = await harness.row(copy["id"])
        assert row["status"] == "awaiting_confirmation", remove
        assert row["ingest_estimate"]["duplicate_of"] is None, remove
        assert row["ingest_estimate"]["estimated"] is True, remove


async def test_ingest_anyway_refuses_a_duplicate_changed_meanwhile(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review L1: what was read is checked again with the row locked."""
    await harness.upload("a.md", _MD)
    copy = await harness.upload("copy.md", _MD)
    read = harness.store.get_document

    async def stale_read(*, tenant_id: UUID, document_id: UUID) -> Any:
        document = await read(tenant_id=tenant_id, document_id=document_id)
        assert document is not None and document.ingest_estimate is not None
        older = document.ingest_estimate.model_copy(update={"id": uuid4()})
        return document.model_copy(update={"ingest_estimate": older})

    monkeypatch.setattr(harness.store, "get_document", stale_read)
    response = await harness.client.post(f"{_DOCS}/{copy['id']}/estimate")
    monkeypatch.undo()
    assert (response.status_code, response.json()["detail"]["error"]) == (409, "estimate_changed")
    assert (await harness.row(copy["id"]))["status"] == "awaiting_confirmation"


async def test_discarding_a_waiting_new_uploads_new_bytes_estimates_the_first(
    harness: _Harness,
) -> None:
    """Review L2: not left failed with no estimate."""
    uploaded = await harness.upload("a.md", _MD)
    await harness.upload("a.md", _MD + b"\n\nMore.")
    response = await harness.client.post(f"{_DOCS}/{uploaded['id']}/discard-pending")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "estimating"
    await harness.runner.drain()
    row = await harness.row(uploaded["id"])
    assert (row["status"], row["has_pending"], row["ingest_estimate"]["origin"]) == (
        "awaiting_confirmation",
        False,
        "upload",
    )
