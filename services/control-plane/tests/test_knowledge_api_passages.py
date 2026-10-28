"""B-175 P3-PR2 —— passages on ``/v1/knowledge``.

* ``GET …/documents/{id}/passages`` — the document's passages in order, each
  with its chunks; read scope as every knowledge read (a concrete tenant);
  every item carries at least the keys of the shared frontend fixture
  ``apps/admin-ui/src/api/__fixtures__/knowledge_passages.json``;
* ``PATCH …/passages/{pid}`` — a text change needs the revision it was read
  at (409 ``revision_conflict`` with the passage as it stands; 409
  ``document_busy`` while the document is being ingested; 422
  ``passage_too_long``); only the chunks whose text changed are embedded;
  ``enabled`` switches the passage;
* ``POST …/passages`` adds one at the end; ``GET …/revisions`` /
  ``POST …/restore`` show and restore its history; ``GET
  …/unapplied-edits`` lists edits a re-ingest could not put back;
* every write is audited on the document.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.chunking import PARENT_MAX_TOKENS
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "apps/admin-ui/src/api/__fixtures__/knowledge_passages.json"
)


def _paragraph(tag: str, words: int = 50) -> str:
    return " ".join(f"{tag}{i}" for i in range(words)) + "."


#: A chapter of ten paragraphs that fills one passage (a parent row; one
#: chunk per paragraph at the base's 128-token chunk size), then a short one
#: that does not fit beside it: a lone chunk.
_DOC = "\n\n".join(
    [
        "# Guide",
        *(_paragraph(f"p{tag}") for tag in "abcdefghij"),
        "# Notes",
        "The deductible is 500 dollars per year. " + _paragraph("nt", 20),
    ]
)


class _Counting(FakeEmbedder):
    model_key = "fake/model"

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.texts.extend(texts)
        return await super().embed(texts, tenant_id=tenant_id)


@dataclass
class Harness:
    client: AsyncClient
    runner: KnowledgeIngestionRunner
    store: InMemoryKnowledgeStore
    audit: InMemoryAuditLogStore
    embedder: _Counting
    document_id: str

    @property
    def base(self) -> str:
        return f"/v1/knowledge/bases/kb/documents/{self.document_id}"

    async def passages(self, **params: Any) -> list[dict[str, Any]]:
        response = await self.client.get(f"{self.base}/passages", params=params)
        assert response.status_code == 200, response.text
        passages: list[dict[str, Any]] = response.json()["passages"]
        return passages

    async def first(self) -> dict[str, Any]:
        return (await self.passages())[0]

    async def patch(self, passage_id: str, body: dict[str, Any]) -> Any:
        return await self.client.patch(f"{self.base}/passages/{passage_id}", json=body)

    async def audit_rows(self) -> list[tuple[str, str, str | None, dict[str, Any]]]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT, limit=100))
        return [
            (str(e.action), e.resource_type, e.resource_id, dict(e.details))
            for e in reversed(page.entries)
            if str(e.action).startswith("knowledge:passage")
        ]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    store = InMemoryKnowledgeStore()
    embedder = _Counting()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
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
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(audit),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=embedder)
    token = make_test_jwt(tenant_id=_TENANT, subject="user-a")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        created = await client.post(
            "/v1/knowledge/bases", json={"name": "kb", "chunk_max_tokens": 128}
        )
        assert created.status_code == 201
        uploaded = await client.post(
            "/v1/knowledge/bases/kb/documents",
            files={"file": ("guide.md", _DOC.encode(), "text/markdown")},
        )
        await runner.drain()
        yield Harness(client, runner, store, audit, embedder, str(uploaded.json()["id"]))


# -- reading -----------------------------------------------------------------------


async def test_passages_are_listed_in_order_with_their_chunks(harness: Harness) -> None:
    passages = await harness.passages()
    assert [(p["kind"], p["heading_path"]) for p in passages] == [
        ("parent", ["Guide"]),
        ("chunk", ["Notes"]),
    ]
    guide, notes = passages
    assert len(guide["children"]) > 1
    assert all(_paragraph(tag) in guide["content"] for tag in ("pa", "pj"))
    assert notes["children"] == [
        {
            "id": notes["id"],
            "chunk_index": notes["chunk_index"],
            "content": notes["content"],
            "heading_path": ["Notes"],
            "enabled": True,
            "disabled_reason": None,
        }
    ]
    assert (guide["revision"], guide["edited"], guide["enabled"]) == (1, False, True)
    paged = await harness.client.get(f"{harness.base}/passages", params={"offset": 1, "limit": 1})
    assert paged.json()["total"] == 2
    assert [p["id"] for p in paged.json()["passages"]] == [notes["id"]]


async def test_every_passage_carries_the_keys_the_frontend_reads(harness: Harness) -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["passages"][0]
    child_keys = set(expected["children"][0])
    for passage in await harness.passages():
        assert set(passage) >= set(expected), set(expected) - set(passage)
        for child in passage["children"]:
            assert set(child) >= child_keys, child_keys - set(child)
    page = (await harness.client.get(f"{harness.base}/passages")).json()
    assert set(page) >= set(fixture)


async def _review_in_list(harness: Harness) -> dict[str, int]:
    """The document's ``review`` in a paged and an unpaged document list
    (the same in both)."""
    lists = [
        await harness.client.get("/v1/knowledge/bases/kb/documents", params=params)
        for params in ({"limit": 50}, {})
    ]
    found = [
        next(d["review"] for d in page.json()["documents"] if d["id"] == harness.document_id)
        for page in lists
    ]
    assert found[0] == found[1]
    review: dict[str, int] = found[0]
    return review


async def test_the_document_list_says_nothing_waits_for_review(harness: Harness) -> None:
    assert await _review_in_list(harness) == {"inherited": 0, "unapplied": 0}


async def test_passages_are_read_with_the_tenant_scope(harness: Harness) -> None:
    admin = await grant_system_admin(harness.client)
    own = await harness.client.get(
        f"{harness.base}/passages", params={"tenant_id": str(_TENANT)}, headers=admin
    )
    assert own.status_code == 200, own.text
    assert len(own.json()["passages"]) == 2
    star = await harness.client.get(
        f"{harness.base}/passages", params={"tenant_id": "*"}, headers=admin
    )
    assert star.status_code == 400
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    denied = await harness.client.get(
        f"{harness.base}/passages", params={"tenant_id": str(_TENANT)}, headers=foreign
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


async def test_another_bases_document_is_not_found(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "other"})
    passage = await harness.first()
    elsewhere = f"/v1/knowledge/bases/other/documents/{harness.document_id}"
    assert (await harness.client.get(f"{elsewhere}/passages")).status_code == 404
    patched = await harness.client.patch(
        f"{elsewhere}/passages/{passage['id']}", json={"enabled": False}
    )
    assert patched.status_code == 404
    assert (await harness.first())["enabled"] is True
    unknown = await harness.patch(str(uuid4()), {"enabled": False})
    assert unknown.status_code == 404


# -- editing -----------------------------------------------------------------------


async def test_an_edit_embeds_only_the_chunks_it_changed(harness: Harness) -> None:
    passage = await harness.first()
    new_text = passage["content"].replace(_paragraph("pc"), "The waiting period is 30 days.")
    harness.embedder.texts.clear()

    response = await harness.patch(passage["id"], {"content": new_text, "revision": 1})

    assert response.status_code == 200, response.text
    edited = response.json()
    assert (edited["id"], edited["content"], edited["revision"], edited["edited"]) == (
        passage["id"],
        new_text,
        2,
        True,
    )
    assert len(harness.embedder.texts) == 1  # the one chunk holding the new sentence
    assert "The waiting period is 30 days." in harness.embedder.texts[0]
    hits = await harness.client.post(
        "/v1/knowledge/bases/kb/test", json={"query": "waiting period"}
    )
    assert any("The waiting period is 30 days." in r["content"] for r in hits.json()["results"])
    revisions = await harness.client.get(f"{harness.base}/passages/{passage['id']}/revisions")
    assert [(r["revision"], r["edited_by"]) for r in revisions.json()["revisions"]] == [
        (1, None),
        (2, "user-a"),
    ]
    assert (await harness.audit_rows()) == [
        (
            "knowledge:passage_edit",
            "knowledge_document",
            harness.document_id,
            {"kb": "kb", "passage_id": passage["id"], "revision": 2},
        )
    ]


async def test_a_text_change_needs_the_revision_it_was_read_at(harness: Harness) -> None:
    passage = await harness.first()
    missing = await harness.patch(passage["id"], {"content": "New text."})
    assert missing.status_code == 422
    await harness.patch(passage["id"], {"content": "First edit.", "revision": 1})
    harness.embedder.texts.clear()

    stale = await harness.patch(passage["id"], {"content": "Second edit.", "revision": 1})

    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert (detail["passage"]["content"], detail["passage"]["revision"]) == ("First edit.", 2)
    assert harness.embedder.texts == []  # refused before anything was embedded


async def test_no_passage_write_while_the_document_is_being_ingested(harness: Harness) -> None:
    passage = await harness.first()
    await harness.store.upsert_document(
        tenant_id=_TENANT,
        kb_id=(await harness.store.get_base(tenant_id=_TENANT, name="kb")).id,  # type: ignore[union-attr]
        filename="guide.md",
        content=b"# Guide\n\nA new version.",
    )
    harness.embedder.texts.clear()
    for response in (
        await harness.patch(passage["id"], {"content": "x", "revision": 1}),
        await harness.patch(passage["id"], {"enabled": False}),
        await harness.client.post(f"{harness.base}/passages", json={"content": "x"}),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "document_busy"
    assert harness.embedder.texts == []  # refused before anything was embedded


async def test_a_passage_over_twice_the_passage_cap_is_refused(harness: Harness) -> None:
    passage = await harness.first()
    huge = "\n\n".join(_paragraph(f"w{i}x", 60) for i in range(40))
    for response in (
        await harness.patch(passage["id"], {"content": huge, "revision": 1}),
        await harness.client.post(f"{harness.base}/passages", json={"content": huge}),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == {
            "error": "passage_too_long",
            "limit_tokens": 2 * PARENT_MAX_TOKENS,
        }


async def test_switching_a_passage_off_and_on(harness: Harness) -> None:
    notes = (await harness.passages())[1]
    off = await harness.patch(notes["id"], {"enabled": False})
    assert off.status_code == 200, off.text
    assert (off.json()["enabled"], off.json()["disabled_reason"]) == (False, "manual")
    assert [(c["enabled"], c["disabled_reason"]) for c in off.json()["children"]] == [
        (False, "manual")
    ]
    hits = await harness.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert all("deductible" not in r["content"] for r in hits.json()["results"])
    manual = await harness.passages(disabled_reason="manual")
    assert [p["id"] for p in manual] == [notes["id"]]
    assert await harness.passages(disabled_reason="inherited") == []
    on = await harness.patch(notes["id"], {"enabled": True})
    assert (on.json()["enabled"], on.json()["disabled_reason"]) == (True, None)
    assert [r[0] for r in await harness.audit_rows()] == [
        "knowledge:passage_toggle",
        "knowledge:passage_toggle",
    ]


async def test_add_restore_and_unapplied_edits(harness: Harness) -> None:
    added = await harness.client.post(
        f"{harness.base}/passages",
        json={"content": "  Claims are paid within 10 days.\n\n\n", "heading_path": ["Claims"]},
    )
    assert added.status_code == 201, added.text
    assert added.json()["content"] == "Claims are paid within 10 days."
    assert (await harness.passages())[-1]["id"] == added.json()["id"]

    passage = await harness.first()
    original = passage["content"]
    await harness.patch(passage["id"], {"content": "Edited.", "revision": 1})
    restored = await harness.client.post(
        f"{harness.base}/passages/{passage['id']}/restore",
        json={"revision": 1, "expected_revision": 2},
    )
    assert restored.status_code == 200, restored.text
    assert (restored.json()["content"], restored.json()["revision"]) == (original, 3)
    assert restored.json()["edited"] is False
    gone = await harness.client.post(
        f"{harness.base}/passages/{passage['id']}/restore",
        json={"revision": 9, "expected_revision": 3},
    )
    assert gone.status_code == 404
    assert [r[0] for r in await harness.audit_rows()] == [
        "knowledge:passage_add",
        "knowledge:passage_edit",
        "knowledge:passage_restore",
    ]

    unapplied = await harness.client.get(f"{harness.base}/unapplied-edits")
    assert unapplied.status_code == 200
    assert unapplied.json() == {"unapplied": [], "inherited": []}


async def test_a_reupload_lists_what_needs_review_and_it_can_be_discarded(
    harness: Harness,
) -> None:
    """Re-uploaded with two paragraphs changed: the Notes edit can no longer
    be put back (listed, with when it was left behind), and the switched-off
    Guide passage switches off the new one holding most of its text (listed
    for review). Discarding the unapplied edits deletes their history."""
    guide, notes = await harness.passages()
    await harness.patch(guide["id"], {"enabled": False})
    edited = (
        await harness.patch(notes["id"], {"content": "Deductible: 400.", "revision": 1})
    ).json()
    changed = _DOC.replace(_paragraph("pc"), "Paragraph c was rewritten in the file.").replace(
        "The deductible is 500 dollars per year.", "The deductible is 600 dollars per year."
    )
    uploaded = await harness.client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("guide.md", changed.encode(), "text/markdown")},
    )
    assert uploaded.status_code == 202
    await harness.runner.drain()

    review = (await harness.client.get(f"{harness.base}/unapplied-edits")).json()
    ((unapplied,),) = [review["unapplied"]]
    assert (unapplied["passage_id"], unapplied["revision"]) == (notes["id"], 2)
    assert unapplied["content"] == edited["content"]
    assert unapplied["orphaned_at"] is not None
    assert unapplied["switch_off_lost"] is False  # an edit, not a lost switch-off
    ((inherited,),) = [review["inherited"]]
    assert (inherited["enabled"], inherited["disabled_reason"]) == (False, "inherited")
    assert "Paragraph c was rewritten in the file." in inherited["content"]
    # The document list says so (B-175 P3-PR3: the row's 「待复核」).
    assert await _review_in_list(harness) == {"inherited": 1, "unapplied": 1}

    # Discarding deletes history: manifest:delete, which an operator lacks.
    operator = make_test_jwt(tenant_id=_TENANT, subject="user-b", roles=("operator",))
    denied = await harness.client.delete(
        f"{harness.base}/unapplied-edits", headers={"Authorization": f"Bearer {operator}"}
    )
    assert denied.status_code == 403
    discarded = await harness.client.delete(f"{harness.base}/unapplied-edits")
    assert discarded.status_code == 200, discarded.text
    assert discarded.json() == {"discarded": 2}
    after = (await harness.client.get(f"{harness.base}/unapplied-edits")).json()
    assert after["unapplied"] == []
    assert await _review_in_list(harness) == {"inherited": 1, "unapplied": 0}
    history = await harness.client.get(f"{harness.base}/passages/{notes['id']}/revisions")
    assert history.json() == {"revisions": []}
    rows = [r for r in await harness.audit_rows() if r[0] == "knowledge:passage_discard_unapplied"]
    assert [(r[1], r[2], r[3]) for r in rows] == [
        ("knowledge_document", harness.document_id, {"kb": "kb", "discarded": 2})
    ]
