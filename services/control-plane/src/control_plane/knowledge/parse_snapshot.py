"""B-222 PR-2 §5 —— an estimate's local parse, kept so the confirmed ingest does not parse again.

A :class:`~control_plane.knowledge.recognition.ingest_parse.LocalParse` encoded as
gzip JSON (figure images base64) into a ``knowledge_parse_snapshot`` row. The
snapshot is an optimisation: one compressed past :data:`SNAPSHOT_MAX_BYTES` is
not kept, and one this version cannot read (another ``format``, a damaged
payload) is ignored — the ingest parses again, free of charge, and its estimate
is checked again (§6.3).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import logging
import zlib
from typing import Any, Final

from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import FigureCandidate
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recognition.pages import PdfAnalysis, PdfPage
from expert_work.persistence.knowledge.base import ParseSnapshot
from expert_work.protocol import PageDecision, PageReason

logger = logging.getLogger(__name__)

#: ``knowledge_parse_snapshot.format`` of what this module writes.
SNAPSHOT_FORMAT: Final = "local-parse/1"
#: A snapshot compressed past this is not kept (§5).
SNAPSHOT_MAX_BYTES: Final = 32 * 1024 * 1024


def encode_snapshot(local: LocalParse) -> ParseSnapshot | None:
    """``local`` as a snapshot; ``None`` when it is too large to keep."""
    body = {
        "kind": local.kind,
        "figures_on": local.figures_on,
        "markdown": local.markdown,
        "office_figures": _extraction(local.office_figures),
        "analysis": _analysis(local.analysis),
        "pdf_figures": _extraction(local.pdf_figures),
        "text_limit": local.text_limit,
    }
    # Parsed text may hold lone surrogates (cleaned when it is chunked).
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8", "surrogatepass")
    payload = gzip.compress(raw, compresslevel=6)
    if len(payload) > SNAPSHOT_MAX_BYTES:
        logger.info("knowledge.snapshot_too_large bytes=%d", len(payload))
        return None
    return ParseSnapshot(format=SNAPSHOT_FORMAT, payload=payload)


def decode_snapshot(snapshot: ParseSnapshot) -> LocalParse | None:
    """The local parse in ``snapshot``; ``None`` when this version cannot read it."""
    if snapshot.format != SNAPSHOT_FORMAT:
        return None
    try:
        body = json.loads(gzip.decompress(snapshot.payload).decode("utf-8", "surrogatepass"))
        return LocalParse(
            kind=body["kind"],
            figures_on=bool(body["figures_on"]),
            markdown=body["markdown"],
            office_figures=_read_extraction(body["office_figures"]),
            analysis=_read_analysis(body["analysis"]),
            pdf_figures=_read_extraction(body["pdf_figures"]),
            text_limit=int(body["text_limit"]),
        )
    except (OSError, EOFError, zlib.error, binascii.Error, ValueError, KeyError, TypeError):
        logger.warning("knowledge.snapshot_unreadable", exc_info=True)
        return None


def _extraction(extraction: FigureExtraction | None) -> dict[str, Any] | None:
    if extraction is None:
        return None
    return {
        "candidates": [
            {
                "number": c.number,
                "image": base64.b64encode(c.image).decode("ascii"),
                "width": c.width,
                "height": c.height,
                "pixel_hash": c.pixel_hash,
                "pages": list(c.pages),
            }
            for c in extraction.candidates
        ],
        "extracted": extraction.extracted,
        "filtered": extraction.filtered,
        "unsupported": extraction.unsupported,
        "capped": extraction.capped,
    }


def _read_extraction(body: dict[str, Any] | None) -> FigureExtraction | None:
    if body is None:
        return None
    return FigureExtraction(
        candidates=tuple(
            FigureCandidate(
                number=int(c["number"]),
                image=base64.b64decode(c["image"], validate=True),
                width=int(c["width"]),
                height=int(c["height"]),
                pixel_hash=str(c["pixel_hash"]),
                pages=tuple(int(p) for p in c["pages"]),
            )
            for c in body["candidates"]
        ),
        extracted=int(body["extracted"]),
        filtered=int(body["filtered"]),
        unsupported=int(body["unsupported"]),
        capped=int(body["capped"]),
    )


def _analysis(analysis: PdfAnalysis | None) -> dict[str, Any] | None:
    if analysis is None:
        return None
    return {
        "pages": [
            {
                "index": p.index,
                "decision": p.decision.value,
                "reason": p.reason.value if p.reason is not None else None,
                "chars": p.chars,
                "images": p.images,
                "text": p.text,
            }
            for p in analysis.pages
        ],
        "markdown": analysis.markdown,
    }


def _read_analysis(body: dict[str, Any] | None) -> PdfAnalysis | None:
    if body is None:
        return None
    return PdfAnalysis(
        pages=tuple(
            PdfPage(
                index=int(p["index"]),
                decision=PageDecision(p["decision"]),
                reason=PageReason(p["reason"]) if p["reason"] is not None else None,
                chars=int(p["chars"]),
                images=int(p["images"]),
                text=str(p["text"]),
            )
            for p in body["pages"]
        ),
        markdown=body["markdown"],
    )
