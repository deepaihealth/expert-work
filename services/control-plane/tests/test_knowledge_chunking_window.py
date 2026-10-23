"""Topic detection embeds blocks window by window (``SEMANTIC_EMBED_WINDOW``).

Holding one vector per block for a whole document took the server ~40 KiB per
block — 600 MiB for a 5 MB text file — although only adjacent blocks are ever
compared. The chunks must come out exactly as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import chunking
from control_plane.knowledge.chunking import SEMANTIC_EMBED_WINDOW, chunk_markdown_semantic

_TENANT = uuid4()


class _TopicEmbedder:
    """``[1, 0]`` for a block about alpha, ``[0, 1]`` for one about beta —
    adjacent blocks of different topics are a semantic break."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        self.calls.append(len(texts))
        return [(1.0, 0.0) if "alpha" in text else (0.0, 1.0) for text in texts]


def _paragraphs(alpha: int, beta: int) -> str:
    return "\n\n".join(
        [f"alpha note {i} about the alpha project." for i in range(alpha)]
        + [f"beta note {i} about the beta budget." for i in range(beta)]
    )


async def _chunks(text: str, embedder: _TopicEmbedder) -> list[str]:
    pieces = await chunk_markdown_semantic(
        text, max_tokens=512, overlap_tokens=0, embedder=embedder, tenant_id=_TENANT
    )
    return [piece.body for piece in pieces]


@pytest.fixture
def _window(monkeypatch: pytest.MonkeyPatch) -> int:
    """A small window, so a few dozen blocks span several."""
    monkeypatch.setattr(chunking, "SEMANTIC_EMBED_WINDOW", 7)
    return 7


def test_the_window_is_bounded_and_batch_aligned() -> None:
    assert SEMANTIC_EMBED_WINDOW <= 1000
    assert all(SEMANTIC_EMBED_WINDOW % batch == 0 for batch in (10, 25, 50, 100))


async def test_no_request_holds_more_than_one_window_of_blocks(_window: int) -> None:
    embedder = _TopicEmbedder()
    await _chunks(_paragraphs(25, 5), embedder)
    assert embedder.calls == [7, 7, 7, 7, 2]  # every block, once, a window at a time


async def test_a_topic_shift_on_a_window_boundary_is_still_found(_window: int) -> None:
    # The first beta block opens the second window: its vector is compared
    # with the last vector of the first window, carried across.
    embedder = _TopicEmbedder()
    chunks = await _chunks(_paragraphs(_window, 5), embedder)
    assert embedder.calls[0] == _window
    assert not any("alpha" in chunk and "beta" in chunk for chunk in chunks)
    assert any(chunk.startswith("beta note 0 ") for chunk in chunks)


async def test_the_chunks_are_those_of_one_request_for_every_block(
    _window: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "\n\n".join(
        [_paragraphs(9, 4), _paragraphs(3, 11), _paragraphs(7, 7), _paragraphs(1, 6)]
    )
    windowed_embedder = _TopicEmbedder()
    windowed = await _chunks(text, windowed_embedder)
    assert len(windowed_embedder.calls) > 1
    monkeypatch.setattr(chunking, "SEMANTIC_EMBED_WINDOW", 10**9)  # one request for all
    whole_embedder = _TopicEmbedder()
    whole = await _chunks(text, whole_embedder)
    assert len(whole_embedder.calls) == 1
    assert windowed == whole
