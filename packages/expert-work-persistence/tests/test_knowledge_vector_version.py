"""pgvector version parsing for the HNSW scan settings — B-175 wave 2 PR2.

``SqlKnowledgeStore`` turns on ``hnsw.iterative_scan`` only when the installed
``vector`` extension is at least 0.8; anything it cannot read gets ef_search
only.
"""

from __future__ import annotations

import pytest

from expert_work.persistence.knowledge.sql import (
    _ITERATIVE_SCAN_MIN_VERSION,
    _parse_vector_version,
)


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("0.8.0.2", (0, 8, 0, 2)),  # vendor builds append a fourth part
        ("0.8.0", (0, 8, 0)),
        ("0.10.0", (0, 10, 0)),
        ("0.7.4", (0, 7, 4)),
        (None, ()),  # extension row missing
        ("", ()),
    ],
)
def test_parse_vector_version(raw: str | None, parsed: tuple[int, ...]) -> None:
    assert _parse_vector_version(raw) == parsed


@pytest.mark.parametrize(
    ("raw", "iterative"),
    [
        ("0.8.0", True),
        ("0.8.0.2", True),
        ("0.10.0", True),  # numeric, not lexical: 10 > 8
        ("1.0.0", True),
        ("0.7.4", False),
        ("0.5.1", False),
        (None, False),  # missing extension → ef_search only
    ],
)
def test_iterative_scan_gate(raw: str | None, iterative: bool) -> None:
    assert (_parse_vector_version(raw) >= _ITERATIVE_SCAN_MIN_VERSION) is iterative
