"""B-222 PR-2 §5 —— parsing split in two: the estimate's free half, kept, and the paid half.

``parse_upload`` = ``local_parse`` (parse pools only: no model, nothing
written) + ``finish_parse`` (recognition, figure descriptions, page / figure
records). An ingest handed the estimate's local parse — through its snapshot —
ends exactly where a full ingest does, for every document form, and runs no
parse-pool step again. A snapshot that is too large or unreadable is no
snapshot.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pymupdf
import pytest
from PIL import Image

from control_plane.knowledge import parse_snapshot
from control_plane.knowledge.ingestion import (
    ingest_document_bytes,
    parse_locally,
    shutdown_parse_pool,
)
from control_plane.knowledge.parse_snapshot import decode_snapshot, encode_snapshot
from control_plane.knowledge.recognition import ingest_parse
from control_plane.knowledge.recognition.clients import Reading
from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import FigureCandidate
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import ParseSnapshot
from expert_work.protocol import DEFAULT_CHUNK_MAX_TOKENS, DEFAULT_CHUNK_OVERLAP_TOKENS
from orchestrator.llm import FakeEmbedder
from tests.figure_test_images import noise_png
from tests.knowledge_form_fixtures import MIXED_DOCS, PICTURE_DOCS, TEXT_FORM_DOCS, FormDoc

_PAGE_HEIGHT_PX = 2000


@pytest.fixture(scope="module", autouse=True)
def _parse_pool() -> Iterator[None]:
    yield
    shutdown_parse_pool()


@dataclass
class _Models:
    """Fake recognition and description models, counting their calls."""

    reads: int = 0
    descriptions: int = 0
    region: bool = True

    def reader(self, choice: ModelChoice, key: str) -> _Reader:
        del choice, key
        return _Reader(self)

    def describer(
        self, choice: ModelChoice, key: str, prompt: str, user: str, cap: int
    ) -> _Describer:
        del choice, key, prompt, user, cap
        return _Describer(self)


@dataclass
class _Reader:
    models: _Models

    async def read(self, image: bytes) -> Reading:
        self.models.reads += 1
        with Image.open(io.BytesIO(image)) as page:
            width, height = page.size
        if height != _PAGE_HEIGHT_PX or not self.models.region:
            return Reading(text="Scanned page text without pictures.")
        box = ", ".join(f"{v:.0f}" for v in (0.2 * width, 0.3 * height, 0.8 * width, 0.6 * height))
        return Reading(text=f"Before the picture.\n\n![](page=0,bbox=[{box}])\n\nAfter it.")


@dataclass
class _Describer:
    models: _Models

    async def read(self, image: bytes) -> Reading:
        self.models.descriptions += 1
        return Reading(
            text='{"text": "none", "chart": false, "description": "A device photo."}',
            finish_reason="stop",
        )


@dataclass
class _Settings:
    figure: ModelChoice | None = field(default_factory=lambda: ModelChoice("qwen", "qwen3.8-flash"))

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=ModelChoice("glm", "glm-ocr"), fallback=None, max_pages=1000, figure=self.figure
        )


async def _key(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


@dataclass(frozen=True)
class _Outcome:
    chunks: list[str]
    pages: list[object] | None
    figure_stats: object
    reads: int
    descriptions: int


def _scan_with_picture() -> bytes:
    paper = Image.new("RGB", (1240, 1754), (255, 255, 255))
    paper.paste(Image.open(io.BytesIO(noise_png(744, 526, seed=7))), (248, 526))
    out = io.BytesIO()
    paper.save(out, format="PNG")
    return out.getvalue()


def _illustrated_pdf() -> bytes:
    """A text page with a picture, then a scan with a picture on it."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Opening paragraph about the device and its parts.", fontsize=11)
    page.insert_image(pymupdf.Rect(72, 120, 372, 345), stream=noise_png(600, 450))
    page.insert_text((72, 400), "Closing paragraph after the picture of the device.", fontsize=11)
    scan = doc.new_page()
    scan.insert_image(scan.rect, stream=_scan_with_picture())
    return bytes(doc.tobytes())


_ILLUSTRATED = FormDoc("F", "illustrated-pdf", "device.pdf", _illustrated_pdf(), ())
_CASES = [*TEXT_FORM_DOCS, *PICTURE_DOCS, *MIXED_DOCS, _ILLUSTRATED]


async def _ingest(doc: FormDoc, *, via_snapshot: bool, figures_model: bool = True) -> _Outcome:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=doc.filename, content=doc.raw
    )
    models = _Models()
    settings = _Settings(figure=ModelChoice("qwen", "qwen3.8-flash") if figures_model else None)
    recognizer = DocumentRecognizer(
        store=store, settings=settings, api_keys=_key, client_factory=models.reader
    )
    figures = FigureDescriber(
        store=store,
        settings=settings,
        api_keys=_key,
        recognizer=recognizer,
        client_factory=models.describer,
    )
    local: LocalParse | None = None
    if via_snapshot:
        parsed = await parse_locally(
            store,
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            filename=doc.filename,
            raw=doc.raw,
            figures=figures,
        )
        assert models.reads == models.descriptions == 0  # the estimate's half calls no model
        snapshot = encode_snapshot(parsed)
        assert snapshot is not None
        local = decode_snapshot(snapshot)
        assert local == parsed
    await ingest_document_bytes(
        store=store,
        embedder=FakeEmbedder(),
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename=doc.filename,
        raw=doc.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        recognizer=recognizer,
        figures=figures,
        local=local,
    )
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=document.id, limit=10_000)
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None
    return _Outcome(
        chunks=[" > ".join(c.heading_path) + "\n" + c.content for c in chunks],
        pages=list(
            await store.list_document_pages(tenant_id=tenant, document_id=document.id) or []
        ),
        figure_stats=fetched.figure_stats,
        reads=models.reads,
        descriptions=models.descriptions,
    )


@pytest.mark.parametrize("doc", _CASES, ids=lambda d: d.id)
async def test_an_ingest_from_the_snapshot_ends_where_a_full_ingest_does(doc: FormDoc) -> None:
    full = await _ingest(doc, via_snapshot=False)
    split = await _ingest(doc, via_snapshot=True)
    assert split == full


async def test_the_illustrated_pdf_case_exercises_both_figure_kinds() -> None:
    # The equivalence above means something: both pictures were described,
    # the scanned page read once.
    full = await _ingest(_ILLUSTRATED, via_snapshot=False)
    assert full.reads == 1 and full.descriptions == 2


async def test_without_a_figure_model_the_snapshot_holds_no_figures() -> None:
    full = await _ingest(_ILLUSTRATED, via_snapshot=False, figures_model=False)
    split = await _ingest(_ILLUSTRATED, via_snapshot=True, figures_model=False)
    assert split == full
    assert full.descriptions == 0


async def test_an_ingest_from_the_snapshot_runs_no_parse_step_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=_ILLUSTRATED.filename, content=_ILLUSTRATED.raw
    )
    local = await parse_locally(
        store,
        tenant_id=tenant,
        kb_id=base.id,
        document_id=document.id,
        filename=_ILLUSTRATED.filename,
        raw=_ILLUSTRATED.raw,
        figures=None,
    )
    steps = ("analyse_pdf_within_cap", "parse_within_cap", "parse_office_figures_within_cap")

    def _no_parse(*args: object) -> object:
        raise AssertionError("parsed again")

    for step in steps:
        monkeypatch.setattr(ingest_parse, step, _no_parse)
    monkeypatch.setattr(ingest_parse, "_text_page_figures", _no_parse)
    count = await ingest_document_bytes(
        store=store,
        embedder=FakeEmbedder(),
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename=_ILLUSTRATED.filename,
        raw=_ILLUSTRATED.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        local=local,
    )
    assert count > 0


def test_a_snapshot_too_large_or_unreadable_is_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    local = LocalParse(kind="text", figures_on=False, markdown="x" * 50_000)
    snapshot = encode_snapshot(local)
    assert snapshot is not None
    assert decode_snapshot(snapshot) == local
    assert decode_snapshot(ParseSnapshot(format="local-parse/0", payload=snapshot.payload)) is None
    assert decode_snapshot(ParseSnapshot(format=snapshot.format, payload=b"not gzip")) is None
    monkeypatch.setattr(parse_snapshot, "SNAPSHOT_MAX_BYTES", len(snapshot.payload) - 1)
    assert encode_snapshot(local) is None


def test_images_past_the_cap_are_refused_before_anything_is_compressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2a review: 75 figures of ~800 KB took 3.4 s to compress only to be
    thrown away — the images alone already decide it."""
    image = b"\x89PNG" + bytes(2_000)
    extraction = FigureExtraction(
        candidates=(
            FigureCandidate(number=1, image=image, width=10, height=10, pixel_hash="p", pages=(1,)),
        ),
        extracted=1,
        filtered=0,
        unsupported=0,
        capped=0,
    )
    local = LocalParse(kind="office", figures_on=True, markdown="x", office_figures=extraction)
    monkeypatch.setattr(parse_snapshot, "SNAPSHOT_MAX_BYTES", len(image) - 1)

    def no_compress(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("compressed")

    monkeypatch.setattr(parse_snapshot.gzip, "compress", no_compress)
    assert encode_snapshot(local) is None


def test_a_snapshot_is_not_read_past_its_decoded_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    local = LocalParse(kind="text", figures_on=False, markdown="y" * 100_000)
    snapshot = encode_snapshot(local)
    assert snapshot is not None and len(snapshot.payload) < 10_000  # compresses well
    assert decode_snapshot(snapshot) == local
    monkeypatch.setattr(parse_snapshot, "SNAPSHOT_MAX_DECODED_BYTES", 50_000)
    assert decode_snapshot(snapshot) is None
    monkeypatch.undo()
    # Its checksum trailer cut off: the text is all there, but unverified.
    truncated = ParseSnapshot(format=snapshot.format, payload=snapshot.payload[:-4])
    assert decode_snapshot(truncated) is None


def test_a_lone_surrogate_survives_the_snapshot() -> None:
    local = LocalParse(kind="text", figures_on=False, markdown="before \ud800 after")
    snapshot = encode_snapshot(local)
    assert snapshot is not None
    assert decode_snapshot(snapshot) == local
