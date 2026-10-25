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
B-206 K-3 之后另断言切块四条校验(WeKnora 同款)与长文档段长中位(不碎、不超上限);
B-206 K-1 的父段形状见文末。
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.chunking import (
    PARENT_MAX_TOKENS,
    SHORT_SECTION_TOKENS,
    ParentChunk,
    chunk_markdown_parented,
    count_tokens,
)
from control_plane.knowledge.ingestion import (
    failure_code,
    ingest_document_bytes,
    shutdown_parse_pool,
)
from control_plane.knowledge.parsing import (
    DocumentParseError,
    is_plain_text_file,
    parse_document,
    strip_lone_surrogates,
)
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
    chunk_retrieval_text,
    knowledge_document_title,
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


def _sentences(text: str) -> list[str]:
    """``text`` cut after each sentence end (CJK or ASCII full stop, ! or ?)."""
    return [piece for piece in re.split(r"(?<=[\u3002\uff01\uff1f.!?])", text) if piece]


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
    # Checked sentence by sentence: a paragraph cut mid-text repeats its last
    # sentence(s) at the head of the next chunk (B-206 K-3), so the joined
    # bodies hold it whole but not as one unbroken run.
    ingested = await _ingest(doc, embedder=embedder)
    bodies = _squash("".join(chunk.content for chunk in ingested.chunks))
    headings = {_squash(h) for chunk in ingested.chunks for h in chunk.heading_path}
    lost = [
        sentence
        for unit in doc.units
        if _squash(unit) not in headings
        for sentence in _sentences(_squash(unit))
        if sentence not in bodies
    ]
    assert not lost


@pytest.mark.parametrize("embedder", _EMBEDDERS)
@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_no_chunk_exceeds_the_cap(doc: FormDoc, embedder: Embedder) -> None:
    # The cap bounds the whole embedded text: title, heading path and body (B-200).
    ingested = await _ingest(doc, embedder=embedder)
    sizes = [count_tokens(chunk.retrieval_text) for chunk in ingested.chunks]
    assert sizes
    assert max(sizes) <= DEFAULT_CHUNK_MAX_TOKENS + _CAP_SLACK


@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_no_chunk_is_only_a_heading(doc: FormDoc) -> None:
    # An empty section (a heading with no text under it) joins the next one
    # instead of becoming a chunk of just its heading (B-206 K-3).
    ingested = await _ingest(doc)
    headings = set(_HEADING_LINE.findall(ingested.markdown))
    heading_only = [c.content for c in ingested.chunks if set(_lines(c.content)) <= headings]
    assert not heading_only


@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_chunking_passes_the_four_checks(doc: FormDoc) -> None:
    # B-206 K-3 (WeKnora's chunk validator): none of the four shapes of a
    # chunking gone wrong.
    ingested = await _ingest(doc)
    cap = DEFAULT_CHUNK_MAX_TOKENS
    sizes = [count_tokens(chunk.retrieval_text) for chunk in ingested.chunks]
    length = count_tokens(ingested.markdown)
    assert not (len(sizes) == 1 and length > 2 * cap)  # a long document left in one chunk
    fragments = sum(len(chunk.content.strip()) < 50 for chunk in ingested.chunks)
    assert fragments <= len(sizes) / 4  # fragments under 50 characters
    assert not (length > cap and max(sizes) < cap // 4)  # every chunk tiny
    assert max(sizes) <= 2 * cap  # a chunk far over the cap


@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_a_long_document_is_neither_fragmented_nor_over_the_cap(doc: FormDoc) -> None:
    # Median chunk of a document longer than the cap: at least the ~50 tokens
    # under which a section joins its neighbour (B-206 K-3; a literal, so the
    # check does not move with the constant it checks), at most the cap. The
    # 200~400-token median of K-3 is judged on the dev set.
    ingested = await _ingest(doc)
    if count_tokens(ingested.markdown) <= DEFAULT_CHUNK_MAX_TOKENS:
        assert len(ingested.chunks) == 1
        return
    median = statistics.median(count_tokens(c.retrieval_text) for c in ingested.chunks)
    assert 50 <= median <= DEFAULT_CHUNK_MAX_TOKENS


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


@pytest.mark.parametrize("doc", _params(_form("E")))
async def test_dialogue_lines_are_never_cut(doc: FormDoc) -> None:
    # A chat with no blank lines is one paragraph; it is cut between lines,
    # not at the sentence marks inside one (B-206 K-3).
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
# 父段(B-206 K-1):先切父段、再在父段里切子段 —— Ⓐ 父段对齐章节、Ⓑ Ⓔ 按字数窗口、
# Ⓒ 整篇一个父段(只有一个子段时不建父行)、Ⓓ 整表(带表头)为父段;Ⓕ 没有文字,不适用。
# 父行进库之前,父段由切块器按入库的参数直接切出,入库落的分段就是它们的子段。
# ---------------------------------------------------------------------------

_LEVELED_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _parented(doc: FormDoc) -> list[ParentChunk]:
    """``doc``'s parents, cut with the arguments the upload path chunks it with."""
    return chunk_markdown_parented(
        strip_lone_surrogates(parse_document(doc.filename, doc.raw)),
        max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        plain_text=is_plain_text_file(doc.filename),
        doc_title=knowledge_document_title(doc.filename),
    )


def _parent_tokens(parent: ParentChunk, doc: FormDoc, body: str | None = None) -> int:
    """A parent measured as a chunk is: title, heading path, body."""
    text = parent.body if body is None else body
    title = knowledge_document_title(doc.filename)
    return count_tokens(chunk_retrieval_text(parent.heading_path, text, title))


@pytest.mark.usefixtures("no_semantic_breaks")
@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
async def test_the_stored_chunks_are_the_children_of_the_parents(doc: FormDoc) -> None:
    ingested = await _ingest(doc)
    children = [child for parent in _parented(doc) for child in parent.children]
    assert [(c.content, tuple(c.heading_path)) for c in ingested.chunks] == [
        (c.body, c.heading_path) for c in children
    ]


@pytest.mark.parametrize("doc", _params(TEXT_FORM_DOCS))
def test_parents_hold_all_text_within_the_parent_cap(doc: FormDoc) -> None:
    parents = _parented(doc)
    assert all(_parent_tokens(parent, doc) <= PARENT_MAX_TOKENS for parent in parents)
    texts = [_squash("".join(parent.heading_path) + parent.body) for parent in parents]
    assert [u for u in doc.units if not any(_squash(u) in text for text in texts)] == []
    for parent, text in zip(parents, texts, strict=True):
        cells = [
            cell for c in parent.children for line in _lines(c.body) for cell in line.split("|")
        ]
        assert [cell for cell in cells if _squash(cell) not in text] == []


@pytest.mark.parametrize("doc", _params(_form("A")))
def test_structured_parents_follow_chapters(doc: FormDoc) -> None:
    # A level-1 / level-2 heading opens a new parent once the one before is at
    # least half full: inside a parent one comes before its half — or opens a
    # short end joined back (the empty headings closing a document).
    markdown = parse_document(doc.filename, doc.raw)
    chapters = {text for marks, text in _LEVELED_HEADING.findall(markdown) if len(marks) <= 2}
    parents = _parented(doc)
    assert all(parent.heading_path for parent in parents)
    assert (len(parents) > 1) == (count_tokens(markdown) > PARENT_MAX_TOKENS)
    for parent in parents:
        paragraphs = parent.body.split("\n\n")
        for index, paragraph in enumerate(paragraphs):
            if paragraph in chapters:
                before = _parent_tokens(parent, doc, "\n\n".join(paragraphs[:index]))
                after = count_tokens("\n\n".join(paragraphs[index + 1 :]))
                assert before < PARENT_MAX_TOKENS // 2 or after < SHORT_SECTION_TOKENS


@pytest.mark.parametrize("doc", _params(_form("B") + _form("E")))
def test_unstructured_parents_are_windows_by_size(doc: FormDoc) -> None:
    parents = _parented(doc)
    assert all(parent.heading_path == () for parent in parents)
    markdown = parse_document(doc.filename, doc.raw)
    assert (len(parents) > 1) == (count_tokens(markdown) > PARENT_MAX_TOKENS)
    assert all(_parent_tokens(p, doc) >= PARENT_MAX_TOKENS // 2 for p in parents[:-1])


@pytest.mark.parametrize("doc", _params(_form("E")))
def test_dialogue_parents_hold_whole_utterances(doc: FormDoc) -> None:
    utterances = set(doc.units)
    cut = [line for p in _parented(doc) for line in _lines(p.body) if line not in utterances]
    assert not cut


@pytest.mark.parametrize("doc", _params(_form("C")))
def test_a_short_document_is_one_parent_and_no_parent_row(doc: FormDoc) -> None:
    [parent] = _parented(doc)
    assert len(parent.children) == 1
    assert parent.is_trivial


@pytest.mark.parametrize("doc", _params(_form("D")))
def test_a_table_parent_holds_rows_under_one_header(doc: FormDoc) -> None:
    markdown = parse_document(doc.filename, doc.raw)
    headers = _table_headers(markdown)
    rows = [
        line
        for line in _lines(markdown)
        if line.startswith("|") and line not in headers and not _TABLE_SEPARATOR.fullmatch(line)
    ]
    parents = _parented(doc)
    runs = [run for parent in parents for run in _table_runs(parent.body)]
    assert all(run[0] in headers and _TABLE_SEPARATOR.fullmatch(run[1]) for run in runs)
    assert sorted(line for run in runs for line in run[2:]) == sorted(rows)  # each row once
    # A table within the parent cap is one parent's, whole.
    tables = ["\n".join(run) for run in _table_runs(markdown)]
    small = [table for table in tables if count_tokens(table) < PARENT_MAX_TOKENS - 100]
    assert all(any(table in parent.body for parent in parents) for table in small)
