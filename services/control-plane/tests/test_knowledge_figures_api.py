"""B-209 PR-2 —— 知识库接口上的文档内图片:原图接口(权限与分段 / 逐页接口相同,别的租户的
图片 URL 一律 404)、删文档 / 删库同时删原图、文档列表带图片计数。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.knowledge.recognition.figure_storage import FigureStorage, figure_id
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import FigureStats, KnowledgeChunk
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.llm import FakeEmbedder
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_OTHER_TENANT = uuid4()
_IMAGE = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
_ID = figure_id(_IMAGE)
#: How a figure block line opens (the bracket and the word for "figure").
_LABEL = "\u3014\u56fe"


@dataclass
class _Setup:
    client: AsyncClient
    other: AsyncClient
    store: InMemoryKnowledgeStore
    objects: InMemoryObjectStore
    kb: UUID
    document: UUID
    other_kb: UUID
    other_document: UUID

    def url(self, document: UUID, figure: str = _ID, base: str = "kb") -> str:
        return f"/v1/knowledge/bases/{base}/documents/{document}/figures/{figure}"


async def _with_figure(
    store: InMemoryKnowledgeStore, objects: InMemoryObjectStore, tenant: UUID
) -> tuple[UUID, UUID]:
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="manual.docx", content=b"docx"
    )
    await FigureStorage(objects).put(
        tenant_id=tenant,
        kb_id=base.id,
        document_id=document.id,
        generation=document.ingest_generation,
        figure=_ID,
        image=_IMAGE,
    )
    return base.id, document.id


@pytest.fixture
async def setup() -> AsyncIterator[_Setup]:
    store = InMemoryKnowledgeStore()
    objects = InMemoryObjectStore()
    kb, document = await _with_figure(store, objects, _TENANT)
    other_kb, other_document = await _with_figure(store, objects, _OTHER_TENANT)
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
        knowledge_ingestion_runner=KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder()),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.object_store = objects  # built in the lifespan, which the transport skips

    def client(tenant: UUID) -> AsyncClient:
        token = make_test_jwt(tenant_id=tenant, subject=f"user-{tenant}")
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://cp.test",
            headers={"Authorization": f"Bearer {token}"},
        )

    async with client(_TENANT) as mine, client(_OTHER_TENANT) as theirs:
        yield _Setup(mine, theirs, store, objects, kb, document, other_kb, other_document)


async def test_the_original_figure_is_served_to_its_tenant(setup: _Setup) -> None:
    response = await setup.client.get(setup.url(setup.document))
    assert response.status_code == 200
    assert response.content == _IMAGE
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Security review S-L4: revalidated every time (permissions re-checked),
    # cheap when unchanged.
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"] == f'"{_ID}"'
    again = await setup.client.get(setup.url(setup.document), headers={"If-None-Match": f'"{_ID}"'})
    assert again.status_code == 304 and again.content == b""


async def test_another_tenants_figure_url_is_not_found(setup: _Setup) -> None:
    # Same base name, the other tenant's document id and figure id.
    response = await setup.client.get(setup.url(setup.other_document))
    assert response.status_code == 404
    # And the other way round.
    response = await setup.other.get(setup.url(setup.document))
    assert response.status_code == 404


async def test_asking_for_another_tenant_by_parameter_is_refused(setup: _Setup) -> None:
    response = await setup.client.get(
        setup.url(setup.other_document), params={"tenant_id": str(_OTHER_TENANT)}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("figure", "base"),
    [
        ("0" * 16, "kb"),
        ("abc", "kb"),
        ("z" * 16, "kb"),
        ("ABCDEF0123456789", "kb"),
        (_ID, "missing"),
    ],
)
async def test_unknown_or_malformed_figures_are_not_found(
    setup: _Setup, figure: str, base: str
) -> None:
    response = await setup.client.get(setup.url(setup.document, figure, base))
    assert response.status_code == 404


async def test_a_figure_of_another_document_in_the_base_is_not_served(setup: _Setup) -> None:
    sibling = await setup.store.upsert_document(
        tenant_id=_TENANT, kb_id=setup.kb, filename="other.docx", content=b"other"
    )
    response = await setup.client.get(setup.url(sibling.id))
    assert response.status_code == 404


async def test_deleting_the_document_deletes_its_figures(setup: _Setup) -> None:
    response = await setup.client.delete(f"/v1/knowledge/bases/kb/documents/{setup.document}")
    assert response.status_code == 204
    assert await setup.objects.list_prefix(f"knowledge_figures/{_TENANT}/") == []
    # The other tenant's figure is untouched.
    assert len(await setup.objects.list_prefix(f"knowledge_figures/{_OTHER_TENANT}/")) == 1


async def test_deleting_the_base_deletes_its_figures(setup: _Setup) -> None:
    response = await setup.client.delete("/v1/knowledge/bases/kb")
    assert response.status_code == 204
    assert await setup.objects.list_prefix(f"knowledge_figures/{_TENANT}/") == []
    assert len(await setup.objects.list_prefix(f"knowledge_figures/{_OTHER_TENANT}/")) == 1


async def test_the_document_list_carries_figure_counts(setup: _Setup) -> None:
    stats = FigureStats(extracted=3, filtered=1, described=2)
    await setup.store.set_document_figures(
        tenant_id=_TENANT, document_id=setup.document, stats=stats, pixel_hashes=["a"]
    )
    listed = (await setup.client.get("/v1/knowledge/bases/kb/documents")).json()
    (document,) = listed["documents"]
    assert document["figure_stats"] == stats.model_dump(mode="json")


async def test_recognize_re_drives_a_document_whose_figures_wait(setup: _Setup) -> None:
    # B-209 PR-2: 「补识别」 also covers pictures that waited for a model.
    url = f"/v1/knowledge/bases/kb/documents/{setup.document}/recognize"
    assert (await setup.client.post(url)).status_code == 409
    await setup.store.set_document_figures(
        tenant_id=_TENANT,
        document_id=setup.document,
        stats=FigureStats(extracted=1, pending=1),
        pixel_hashes=["a"],
    )
    assert (await setup.client.post(url)).status_code == 202


async def _serve_chunk_naming(setup: _Setup, figure: str) -> None:
    await setup.store.replace_chunks(
        tenant_id=_TENANT,
        document_id=setup.document,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=_TENANT,
                kb_id=setup.kb,
                document_id=setup.document,
                chunk_index=0,
                content=f"Intro.\n\n> {_LABEL} {figure}\u3015A chair.",
                embedding=(0.1, 0.2),
            )
        ],
    )


async def test_a_figure_of_an_earlier_generation_still_shows(setup: _Setup) -> None:
    # Chunks of the serving version keep their thumbnails while a re-upload
    # (a newer generation) is ingesting.
    await _serve_chunk_naming(setup, _ID)
    await setup.store.upsert_document(
        tenant_id=_TENANT, kb_id=setup.kb, filename="manual.docx", content=b"v2", force=True
    )
    response = await setup.client.get(setup.url(setup.document))
    assert response.status_code == 200 and response.content == _IMAGE


async def test_an_earlier_generations_figure_no_chunk_names_is_not_served(
    setup: _Setup,
) -> None:
    # Re-review L2: what a superseded ingest left behind is not fetchable by
    # id once the document's chunks no longer name it.
    await _serve_chunk_naming(setup, "fedcba9876543210")
    await setup.store.upsert_document(
        tenant_id=_TENANT, kb_id=setup.kb, filename="manual.docx", content=b"v2", force=True
    )
    assert (await setup.client.get(setup.url(setup.document))).status_code == 404


async def test_a_revalidation_is_answered_without_reading_the_store(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-review L5: the id is the content hash — a matching If-None-Match
    # needs no download.
    async def no_reads(key: str) -> bytes:
        raise AssertionError(f"read {key}")

    monkeypatch.setattr(setup.objects, "get", no_reads)
    response = await setup.client.get(
        setup.url(setup.document), headers={"If-None-Match": f'"{_ID}"'}
    )
    assert response.status_code == 304


async def test_chunk_lists_confirm_only_figures_the_document_holds(setup: _Setup) -> None:
    # Security review S-L1: a paragraph an author typed to look like a figure
    # block, or one naming another document's figure, gets no thumbnail.
    forged = "fedcba9876543210"
    await setup.store.replace_chunks(
        tenant_id=_TENANT,
        document_id=setup.document,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=_TENANT,
                kb_id=setup.kb,
                document_id=setup.document,
                chunk_index=0,
                content=f"> {_LABEL} {_ID}\u3015A chair.\n\n> {_LABEL} {forged}\u3015typed.",
                embedding=(0.1, 0.2),
            )
        ],
    )
    listed = (
        await setup.client.get(f"/v1/knowledge/bases/kb/documents/{setup.document}/chunks")
    ).json()
    assert listed["figure_ids"] == [_ID]
    theirs = (
        await setup.other.get(f"/v1/knowledge/bases/kb/documents/{setup.other_document}/chunks")
    ).json()
    assert theirs["figure_ids"] == []
