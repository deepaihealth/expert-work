"""Length and score helpers every rerank client shares — B-206 K-7 / K-14.

What the model catalog's :class:`~expert_work.protocol.model_catalog.RerankCaps`
limits mean for one request's text, counted in the model's own unit
(:data:`~expert_work.protocol.model_catalog.LengthUnit`): characters for a
``"char"`` model, which the platform counts exactly; UTF-8 bytes for a
``"token"`` model, which bound its tokens from above (a token is never shorter
than one byte).
"""

from __future__ import annotations

from collections.abc import Sequence

from expert_work.protocol.model_catalog import RerankCaps


def text_size(text: str, *, by_char: bool) -> int:
    """``text``'s length in characters, or in UTF-8 bytes."""
    return len(text) if by_char else len(text.encode("utf-8"))


def cut(text: str, limit: int | None, *, by_char: bool) -> str:
    """``text`` within ``limit`` characters, or ``limit`` UTF-8 bytes (never
    splitting a character); as it is when there is no limit."""
    if limit is None:
        return text
    if by_char:
        return text[:limit]
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def fit_total(
    query: str, documents: Sequence[str], instruction: str | None, caps: RerankCaps | None
) -> list[str]:
    """The leading ``documents`` a request surely stays within
    ``caps.max_total_tokens`` with — all of them when no total is declared.

    The platform cannot count the vendor's tokens, so it counts what bounds them:
    UTF-8 bytes (a token is never shorter than one byte; see
    :data:`~expert_work.protocol.model_catalog.LengthUnit`), or characters for a
    ``"char"`` model. The query and instruction are charged to every document in
    case the vendor counts each query-document pair. That bound is several times
    the real count for prose, so it is used only after the vendor has refused."""
    if caps is None or caps.max_total_tokens is None:
        return list(documents)
    by_char = caps.length_unit == "char"
    per_document = text_size(query, by_char=by_char) + (
        text_size(instruction, by_char=by_char) if instruction is not None else 0
    )
    room = caps.max_total_tokens
    fitted: list[str] = []
    for document in documents:
        room -= per_document + text_size(document, by_char=by_char)
        if room < 0:
            break
        fitted.append(document)
    return fitted


def relevance(value: object) -> float | None:
    """A result row's ``relevance_score``; ``None`` when missing or not a number."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None
