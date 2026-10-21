"""B-209 PR-1 —— 知识库接口上的逐页记录与「补识别」。

上传一份混合 PDF(文字页 + 扫描页)时识别模型还没配 → 扫描页记「待识别」、文档带计数;
配好之后「补识别」→ 这一页被识别、文档计数更新;没有待识别 / 失败页的文档补识别回 409。
识别模型是假的(不联网)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from uuid import UUID

import pymupdf
import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, shutdown_parse_pool
from control_plane.knowledge.recognition.clients import Reading, RecognitionClient
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from orchestrator.llm import FakeEmbedder
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_RECOGNIZED = "第三章 考勤\n\n迟到超过三十分钟按事假半天计。"


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


def _mixed_pdf(scans: int = 1) -> bytes:
    picture = pymupdf.open()
    picture.new_page().insert_text((72, 72), "Scanned words on paper " * 6, fontsize=14)
    scan = picture[0].get_pixmap(dpi=72).tobytes("png")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Leave requests go to the manager. " * 8, fontsize=8)
    for _ in range(scans):
        page = doc.new_page()
        page.insert_image(page.rect, stream=scan)
    return bytes(doc.tobytes())


@dataclass
class _Switchable:
    """Platform settings an admin can switch on mid-test."""

    on: bool = False
    max_pages: int = 1000

    async def recognition_settings(self) -> RecognitionSettings:
        primary = ModelChoice("glm", "glm-ocr") if self.on else None
        return RecognitionSettings(primary=primary, fallback=None, max_pages=self.max_pages)


class _Reader:
    async def read(self, image: bytes) -> Reading:
        del image
        return Reading(text=_RECOGNIZED)


async def _keys(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


Setup = tuple[AsyncClient, KnowledgeIngestionRunner, _Switchable]


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    store = InMemoryKnowledgeStore()
    settings = _Switchable()

    def factory(choice: ModelChoice, api_key: str) -> RecognitionClient:
        del choice, api_key
        return _Reader()

    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=FakeEmbedder(),
        recognizer=DocumentRecognizer(
            store=store, settings=settings, api_keys=_keys, client_factory=factory
        ),
    )
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
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject='user-a')}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=headers
    ) as client:
        yield client, runner, settings


async def _upload(
    client: AsyncClient, runner: KnowledgeIngestionRunner, name: str, raw: bytes
) -> str:
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.post(
        "/v1/knowledge/bases/kb/documents", files={"file": (name, raw, "application/pdf")}
    )
    assert resp.status_code in (200, 202), resp.text
    await runner.drain()
    document_id: str = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][
        0
    ]["id"]
    return document_id


async def test_pending_pages_are_listed_and_recognized_on_request(setup: Setup) -> None:
    client, runner, settings = setup
    document_id = await _upload(client, runner, "handbook.pdf", _mixed_pdf())

    listed = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert listed["status"] == "ready"
    assert listed["page_count"] == 2
    assert listed["page_stats"] == {
        "pages_total": 2,
        "pages_ocr": 0,
        "pages_failed": 0,
        "pages_pending": 1,
        "pages_over_limit": 0,
    }
    pages = (await client.get(f"/v1/knowledge/bases/kb/documents/{document_id}/pages")).json()
    assert [(p["page"], p["decision"], p["status"]) for p in pages["pages"]] == [
        (1, "text", None),
        (2, "ocr", "pending"),
    ]
    assert pages["pages"][1]["error"] == "not_configured"
    assert (pages["total"], pages["offset"], pages["limit"]) == (2, 0, 200)

    settings.on = True  # the admin configures 「文档识别」
    resp = await client.post(f"/v1/knowledge/bases/kb/documents/{document_id}/recognize")
    assert resp.status_code == 202, resp.text
    await runner.drain()

    listed = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert listed["status"] == "ready"
    assert listed["page_stats"]["pages_ocr"] == 1
    assert listed["page_stats"]["pages_pending"] == 0
    chunks = (await client.get(f"/v1/knowledge/bases/kb/documents/{document_id}/chunks")).json()
    assert "迟到超过三十分钟" in "".join(c["content"] for c in chunks["chunks"])

    # Nothing left to recognize.
    again = await client.post(f"/v1/knowledge/bases/kb/documents/{document_id}/recognize")
    assert again.status_code == 409


async def test_recognize_a_document_without_page_records_is_409(setup: Setup) -> None:
    client, runner, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("notes.md", b"# Notes\n\nPlain text only.", "text/markdown")},
    )
    await runner.drain()
    document = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert document["page_stats"] is None
    resp = await client.post(f"/v1/knowledge/bases/kb/documents/{document['id']}/recognize")
    assert resp.status_code == 409
    pages = (await client.get(f"/v1/knowledge/bases/kb/documents/{document['id']}/pages")).json()
    assert pages == {"pages": [], "total": 0, "offset": 0, "limit": 200, "page_stats": None}


async def test_recognize_and_pages_of_a_missing_document_are_404(setup: Setup) -> None:
    client, _, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    ghost = "00000000-0000-0000-0000-000000000000"
    assert (
        await client.post(f"/v1/knowledge/bases/kb/documents/{ghost}/recognize")
    ).status_code == 404
    assert (await client.get(f"/v1/knowledge/bases/kb/documents/{ghost}/pages")).status_code == 404


async def test_pages_over_the_limit_are_recognized_once_the_limit_is_raised(setup: Setup) -> None:
    # Review: over-limit pages used to be stuck — 「补识别」 refused them.
    client, runner, settings = setup
    settings.on, settings.max_pages = True, 1
    document_id = await _upload(client, runner, "book.pdf", _mixed_pdf(scans=2))
    listed = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert (listed["page_stats"]["pages_ocr"], listed["page_stats"]["pages_over_limit"]) == (1, 1)

    settings.max_pages = 1000  # the admin raises the limit
    resp = await client.post(f"/v1/knowledge/bases/kb/documents/{document_id}/recognize")
    assert resp.status_code == 202, resp.text
    await runner.drain()
    listed = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert (listed["page_stats"]["pages_ocr"], listed["page_stats"]["pages_over_limit"]) == (2, 0)


async def test_page_records_are_paged(setup: Setup) -> None:
    client, runner, settings = setup
    settings.on = True
    document_id = await _upload(client, runner, "book.pdf", _mixed_pdf(scans=2))
    url = f"/v1/knowledge/bases/kb/documents/{document_id}/pages"
    page = (await client.get(url, params={"offset": 1, "limit": 1})).json()
    assert [p["page"] for p in page["pages"]] == [2]
    assert (page["total"], page["offset"], page["limit"]) == (3, 1, 1)
    capped = (await client.get(url, params={"limit": 100_000})).json()
    assert capped["limit"] == 1000
