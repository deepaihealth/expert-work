"""平台形态测试集(B-206 批 0,设计 §9.1):六种文档形态走真实的解析 → 切块 → 落段。

文档见 ``knowledge_form_fixtures.py``(全部虚构、零租户内容)。每篇按上传的原始字节
走 ``parse_document``,再走生产的 ``ingest_document_bytes``(切块 → 嵌入 → 落段),
切块参数取平台默认值。这里只断言**现在成立、B-206 之后也必须成立**的不变式:

* 不丢字:用户写进文档的每一行(每一格)都原样落进某个分段;
* 不超上限:分段的嵌入文本(文档名 + 标题路径 + 正文)不超过切块上限;
* Ⓐ 每个分段都带标题路径;任何形态都没有「只剩标题」的段;
* Ⓔ 对话不在一句话中间切开;
* Ⓓ 带表格行的分段都带表头,表格行不被切开、且排在表头之下;
* Ⓕ 没有文字层的文件:带错误码的解析失败,或零个分段 —— 不出别的异常、不出垃圾段。
* Ⓕ 纯图片的 Word / 幻灯片(B-209 PR-2):图片带着字节抽出来,不再留下占位符垃圾段;图能描述时
  文件按描述可检索,图被过滤时带原因失败、计数记在文档上。
* Ⓕ 混合 PDF(B-209):每页都有记录,扫描页认不出也不静默为空;认得出时按页序插回。

现在不成立、B-206 要修的标 ``xfail(strict=True)``:修好那天它变红,逼着把标记摘掉。
B-206 有意要改的(段长分布、父段数)不在这里断言,见文末 TODO。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.chunking import count_tokens
from control_plane.knowledge.ingestion import (
    failure_code,
    ingest_document_bytes,
    shutdown_parse_pool,
)
from control_plane.knowledge.parsing import DocumentParseError, parse_document
from control_plane.knowledge.recognition.clients import Reading
from control_plane.knowledge.recognition.figure_markup import FIGURE_LABEL
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DocumentPage,
    FigureStats,
    IngestErrorCode,
    KnowledgeChunk,
    PageDecision,
    PageStats,
    PageStatus,
)
from orchestrator.llm import Embedder, FakeEmbedder
from tests.knowledge_form_fixtures import (
    MIXED_DOCS,
    PICTURE_DOCS,
    SCANNED_DOCS,
    TEXT_FORM_DOCS,
    FormDoc,
)

#: Tokens are counted on the joined text and a join can merge with its
#: neighbour — the same slack ``test_knowledge_chunking`` allows the cap.
_CAP_SLACK = 4

_HEADING_LINE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_TABLE_SEPARATOR = re.compile(r"\|(\s*:?-+:?\s*\|)+")


class _TopicShiftEmbedder:
    """Every block reads as a new topic (alternating orthogonal vectors), so the
    semantic chunker splits wherever it may — the opposite extreme of
    :class:`FakeEmbedder`, whose hash vectors are never far enough apart to
    count as a topic shift. Production embeddings fall in between."""

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [(1.0, 0.0) if index % 2 == 0 else (0.0, 1.0) for index in range(len(texts))]


_EMBEDDERS = [
    pytest.param(FakeEmbedder(), id="no-topic-shift"),
    pytest.param(_TopicShiftEmbedder(), id="topic-shift-every-block"),
]


@dataclass(frozen=True)
class _Ingested:
    markdown: str  # what the parser handed the chunker
    chunks: list[KnowledgeChunk]


async def _ingest(
    doc: FormDoc,
    *,
    embedder: Embedder | None = None,
) -> _Ingested:
    """``doc`` through the upload path with the platform's default chunk size:
    parse, then the production chunk → embed → store; the stored chunks.

    Parsed here rather than in the parse process pool — the same
    ``parse_document``, without spawning worker processes."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=doc.filename, content=doc.raw
    )
    markdown = parse_document(doc.filename, doc.raw)
    await ingest_document_bytes(
        store=store,
        embedder=embedder or FakeEmbedder(),
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename=doc.filename,
        raw=doc.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        parsed_markdown=markdown,
    )
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=document.id, limit=10_000)
    return _Ingested(markdown, chunks)


def _params(docs: Sequence[FormDoc], xfail: dict[str, str] | None = None) -> list[object]:
    """``docs`` as test params; a doc whose slug is in ``xfail`` is expected to
    fail today for the given reason (strict: it turns red once fixed)."""
    xfail = xfail or {}
    return [
        pytest.param(
            doc,
            id=doc.id,
            marks=[pytest.mark.xfail(strict=True, reason=xfail[doc.slug])]
            if doc.slug in xfail
            else [],
        )
        for doc in docs
    ]


def _form(form: str) -> list[FormDoc]:
    return [doc for doc in TEXT_FORM_DOCS if doc.form == form]


def _squash(text: str) -> str:
    """``text`` without whitespace — chunk bodies are cut and re-joined there."""
    return re.sub(r"\s+", "", text)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _table_headers(markdown: str) -> set[str]:
    """Header rows of the Markdown tables in ``markdown`` (a row followed by a
    ``| --- |`` separator)."""
    lines = _lines(markdown)
    return {
        line
        for line, after in pairwise(lines)
        if line.startswith("|") and _TABLE_SEPARATOR.fullmatch(after)
    }


def _table_runs(body: str) -> list[list[str]]:
    """Runs of consecutive table lines (lines holding ``|``) in a chunk body."""
    runs: list[list[str]] = []
    current: list[str] = []
    for line in [*body.splitlines(), ""]:
        if "|" in line:
            current.append(line.strip())
        elif current:
            runs.append(current)
            current = []
    return runs


# ---------------------------------------------------------------------------
# 所有带文字的形态(Ⓐ ~ Ⓔ)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("embedder", _EMBEDDERS)
@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_no_text_is_lost(doc: FormDoc, embedder: Embedder) -> None:
    # Every line / cell the user wrote reaches a chunk body (a heading may
    # instead sit on a chunk's heading path) — whatever semantic splitting does.
    ingested = await _ingest(doc, embedder=embedder)
    bodies = _squash("".join(chunk.content for chunk in ingested.chunks))
    headings = {_squash(h) for chunk in ingested.chunks for h in chunk.heading_path}
    lost = [u for u in doc.units if _squash(u) not in bodies and _squash(u) not in headings]
    assert not lost


@pytest.mark.parametrize("embedder", _EMBEDDERS)
@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_no_chunk_exceeds_the_cap(doc: FormDoc, embedder: Embedder) -> None:
    # The cap bounds the whole embedded text: title, heading path and body (B-200).
    ingested = await _ingest(doc, embedder=embedder)
    sizes = [count_tokens(chunk.retrieval_text) for chunk in ingested.chunks]
    assert sizes
    assert max(sizes) <= DEFAULT_CHUNK_MAX_TOKENS + _CAP_SLACK


_HEADING_ONLY = (
    "B-206 K-3: an empty section (a heading with no text under it) becomes a chunk "
    "holding only the heading; short sections are to merge into the next one"
)


@pytest.mark.parametrize(
    "doc",
    _params(
        TEXT_FORM_DOCS,
        xfail={"handbook-zh-md": _HEADING_ONLY, "expense-policy-en-md": _HEADING_ONLY},
    ),
)
async def test_no_chunk_is_only_a_heading(doc: FormDoc) -> None:
    ingested = await _ingest(doc)
    headings = set(_HEADING_LINE.findall(ingested.markdown))
    heading_only = [c.content for c in ingested.chunks if set(_lines(c.content)) <= headings]
    assert not heading_only


# ---------------------------------------------------------------------------
# Ⓐ 有标题结构的长文档
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", _params(_form("A")))
async def test_structured_document_chunks_carry_their_heading_path(doc: FormDoc) -> None:
    ingested = await _ingest(doc)
    assert ingested.chunks
    assert all(chunk.heading_path for chunk in ingested.chunks)


# ---------------------------------------------------------------------------
# Ⓓ 表格
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", _params(_form("D")))
async def test_table_chunks_carry_the_header(doc: FormDoc) -> None:
    ingested = await _ingest(doc)
    headers = _table_headers(ingested.markdown)
    tables = sum(bool(_TABLE_SEPARATOR.fullmatch(line)) for line in _lines(ingested.markdown))
    with_rows = [c.content for c in ingested.chunks if _table_runs(c.content)]
    assert len(with_rows) > tables  # a table was split, so its header had to be repeated
    assert all(headers & set(_lines(body)) for body in with_rows)


@pytest.mark.parametrize("doc", _params(_form("D")))
async def test_table_rows_are_whole_and_under_their_header(doc: FormDoc) -> None:
    # Holds with the default overlap of 0 (B-206 K-3). An overlap carried into
    # the next chunk would put table rows ahead of the repeated header, and
    # could start inside a row (after the '.' of a decimal cell).
    ingested = await _ingest(doc)
    rows = {line for line in _lines(ingested.markdown) if line.startswith("|")}
    headers = _table_headers(ingested.markdown)
    runs = [run for chunk in ingested.chunks for run in _table_runs(chunk.content)]
    assert runs
    cut = [line for run in runs for line in run if line not in rows]
    assert not cut
    headless = [run for run in runs if run[0] not in headers]
    assert not headless


# ---------------------------------------------------------------------------
# Ⓔ 对话体
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    _params(
        _form("E"),
        xfail={
            "sales-chat-zh-txt": (
                "B-206 K-3 (dialogue chunked by line): a chat with no blank lines is one "
                "paragraph, hard-split at sentence marks — inside a multi-sentence line"
            )
        },
    ),
)
async def test_dialogue_lines_are_never_cut(doc: FormDoc) -> None:
    ingested = await _ingest(doc)
    utterances = set(doc.units)
    cut = [line for c in ingested.chunks for line in _lines(c.content) if line not in utterances]
    assert not cut


# ---------------------------------------------------------------------------
# Ⓕ 扫描件 / 图片
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", _params(SCANNED_DOCS))
async def test_scanned_document_yields_no_chunks(doc: FormDoc) -> None:
    try:
        ingested = await _ingest(doc)
    except DocumentParseError as exc:
        assert failure_code(exc) in {IngestErrorCode.PARSE_FAILED, IngestErrorCode.UNSUPPORTED_FILE}
    else:
        assert ingested.chunks == []


_OFFICE_SCANS = [doc for doc in SCANNED_DOCS if doc.slug in {"scan-docx", "scan-pptx"}]


@pytest.mark.parametrize("doc", _params(_OFFICE_SCANS))
def test_a_picture_only_office_file_leaves_no_placeholder_markup(doc: FormDoc) -> None:
    # B-209 PR-2 (was xfail strict): the picture used to parse to its markup —
    # ``![](data:image/png;base64...)`` / ``![scan](scan.jpg)`` — stored as one
    # junk chunk. Now it is read with its bytes; with nothing to describe it
    # leaves no text at all, so the file fails to parse instead.
    with pytest.raises(DocumentParseError) as raised:
        parse_document(doc.filename, doc.raw)
    assert failure_code(raised.value) is IngestErrorCode.PARSE_FAILED


class _Describe:
    """Fake vision model: every picture is a device photo."""

    async def read(self, image: bytes) -> Reading:
        assert image.startswith(b"\xff\xd8")
        return Reading(
            text='{"text": "none", "chart": false, "description": "设备的外观照片。"}',
            finish_reason="stop",
        )


def _describer(store: InMemoryKnowledgeStore) -> FigureDescriber:
    return FigureDescriber(
        store=store,
        settings=_PlatformRecognition(),
        api_keys=_key,
        client_factory=lambda choice, key, prompt, user, cap: _Describe(),
    )


async def _ingest_with_figures(doc: FormDoc) -> tuple[list[KnowledgeChunk], FigureStats | None]:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=doc.filename, content=doc.raw
    )
    try:
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
            figures=_describer(store),
        )
    finally:
        fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=document.id, limit=100)
    return chunks, fetched.figure_stats if fetched is not None else None


@pytest.mark.parametrize("doc", _params(_OFFICE_SCANS))
async def test_a_filtered_picture_fails_the_file_with_its_count(
    doc: FormDoc, _parse_pool: None
) -> None:
    # The scan here is a 64 px grey square: under the figure filters' 80 px floor.
    with pytest.raises(DocumentParseError, match="1 pictures, 1 filtered"):
        await _ingest_with_figures(doc)


@pytest.mark.parametrize("doc", _params(PICTURE_DOCS))
async def test_a_picture_only_office_file_is_searchable_by_its_description(
    doc: FormDoc, _parse_pool: None
) -> None:
    chunks, stats = await _ingest_with_figures(doc)
    # One chunk: the description (a slide deck keeps MarkItDown's slide marker).
    assert [f"> {FIGURE_LABEL} " in chunk.content for chunk in chunks] == [True]
    assert "设备的外观照片。" in chunks[0].content
    assert "data:image" not in chunks[0].content
    assert stats == FigureStats(extracted=1, described=1)


# ---------------------------------------------------------------------------
# Ⓕ 混合 PDF(B-209):文字页夹着扫描页 —— 不允许静默空页
# ---------------------------------------------------------------------------

_SCANNED_PAGE_TEXT = "第二章 薪酬\n\n工资于每月十日发放,遇节假日提前。"


@pytest.fixture(scope="module")
def _parse_pool() -> Iterator[None]:
    # The PDF path analyses and renders pages in the real parse pool.
    yield
    shutdown_parse_pool()


class _ScanReader:
    async def read(self, image: bytes) -> Reading:
        del image
        return Reading(text=_SCANNED_PAGE_TEXT)


async def _no_key(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    msg = f"no platform credential for provider {provider!r}"
    raise LookupError(msg)


async def _key(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


@dataclass
class _PlatformRecognition:
    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=ModelChoice("glm", "glm-ocr"),
            fallback=ModelChoice("qwen", "qwen3.7-flash"),
            max_pages=1000,
            figure=ModelChoice("qwen", "qwen3.8-flash"),
        )


async def _ingest_pdf(doc: FormDoc, *, recognized: bool) -> tuple[str, list[DocumentPage]]:
    """``doc`` through the production PDF path (page analysis, recognition,
    page records) — the chunks' text in order (each chunk's heading path, then
    its body: a heading may sit on the path) and the page records."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=doc.filename, content=doc.raw
    )
    recognizer = DocumentRecognizer(
        store=store,
        settings=_PlatformRecognition(),
        api_keys=_key if recognized else _no_key,
        client_factory=lambda choice, key: _ScanReader(),
    )
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
    )
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=document.id, limit=10_000)
    pages = await store.list_document_pages(tenant_id=tenant, document_id=document.id)
    assert pages is not None
    text = "".join("".join(chunk.heading_path) + chunk.content for chunk in chunks)
    return _squash(text), pages


@pytest.mark.parametrize("doc", _params(MIXED_DOCS))
async def test_mixed_pdf_has_no_silently_empty_page(doc: FormDoc, _parse_pool: None) -> None:
    # Nothing can read the scanned page: the text pages still ingest, and the
    # scanned page is on record as waiting — never a silent gap.
    bodies, pages = await _ingest_pdf(doc, recognized=False)
    assert [u for u in doc.units if _squash(u) not in bodies] == []
    assert [page.decision for page in pages] == [
        PageDecision.TEXT,
        PageDecision.OCR,
        PageDecision.TEXT,
    ]
    scanned = pages[1]
    assert scanned.status is PageStatus.PENDING
    # Tenant-visible: error classes only, never the resolver's text.
    assert scanned.error == (
        "glm/glm-ocr: credential_missing; qwen/qwen3.7-flash: credential_missing"
    )
    assert PageStats.of(pages).needs_recognition


@pytest.mark.parametrize("doc", _params(MIXED_DOCS))
async def test_mixed_pdf_scanned_page_is_read_in_place(doc: FormDoc, _parse_pool: None) -> None:
    bodies, pages = await _ingest_pdf(doc, recognized=True)
    assert [u for u in doc.units if _squash(u) not in bodies] == []
    scanned = _squash(_SCANNED_PAGE_TEXT)
    assert scanned in bodies
    # Page order: chapter 1 (page 1) < the scanned chapter 2 < chapter 3 (page 3).
    assert bodies.index(_squash(doc.units[0])) < bodies.index(scanned)
    assert bodies.index(scanned) < bodies.index(_squash(doc.units[2]))
    assert pages[1].status is PageStatus.RECOGNIZED
    assert pages[1].chars == len(scanned)


# ---------------------------------------------------------------------------
# TODO(B-206 K-3): 段长分布与切块结果校验 —— K-3 定下新默认(子段上限 400、语义断点关、
# 重叠 0、短节合并、对话按行成块)后在这里断言:各形态子段长度中位落在 200~400 token;
# 设计 §5.1 K-3 的四条校验(单块且文档长 > 2 倍上限 / 不足 50 字的碎块超过 25% /
# 最大块不足上限 1/4 / 有块超过 2 倍上限)。中位数现在不断言 —— 它正是 K-3 要改的。
# 若「重叠 = 0」改成独立开关而不是 DEFAULT_CHUNK_OVERLAP_TOKENS = 0,``_ingest`` 要跟着
# 改成取平台新默认,否则上面 overlap-default 的 xfail 不会翻红。
# TODO(B-206 K-1): 父段 —— Ⓐ 父段对齐章节、Ⓑ Ⓔ 按字数窗口、Ⓒ 整篇一个父段(只有一个
# 子段时不建父行)、Ⓓ 整表(带表头)为父段、Ⓕ 不适用。父行进库后 ``_ingest`` 只把可检索的
# 子段交给上面的断言,父段数与父段长在这里断言。现在没有父段(chunk_type 全是 text)。
# ---------------------------------------------------------------------------
