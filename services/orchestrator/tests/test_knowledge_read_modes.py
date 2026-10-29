"""B-206 K-2 — ``knowledge_read`` by heading (``section``) and by words (``find``).

A section is the passages whose chunks sit under the heading named (one heading
at any depth, or an ``A > B`` path), compared without case or width; a heading
under several sections asks which. Find returns the passages that contain every
word given, in any order, ignoring case, width and spacing, each with one
passage on either side — never one twice — at most 20 matches a page; nothing
found counts each word. Seeding helpers: :mod:`test_knowledge_read`.
"""

from __future__ import annotations

import pytest

from .test_knowledge_read import PassageSpec, _flat, _lib, _manual


def _chapters() -> list[PassageSpec]:
    return [
        (("Guide", "Chapter 1"), ["c1 one"]),
        (("Guide", "Chapter 1", "Storage"), ["c1 storage"]),
        (("Guide", "Chapter 2"), ["c2 one"]),
        (("Guide", "Chapter 2", "Storage"), ["c2 storage"]),
        # Full-width "Chapter 3".
        (("Guide", "\uff23\uff48\uff41\uff50\uff54\uff45\uff52 3"), ["c3 one"]),
    ]


# ---------------------------------------------------------------------------
# Section
# ---------------------------------------------------------------------------


async def test_a_section_reads_the_passages_under_a_heading() -> None:
    lib = await _lib()
    await lib.add("guide.md", _chapters())

    result = await lib.read(document="D1", section="chapter 2")

    assert "c2 one" in result.content and "c2 storage" in result.content
    assert "c1 one" not in result.content and "c3 one" not in result.content
    assert result.meta["mode"] == "section"
    assert 'section "Guide > Chapter 2": 2 passages · page 1 of 1' in result.content


async def test_a_section_matches_without_regard_to_case_or_width() -> None:
    lib = await _lib()
    await lib.add("guide.md", _chapters())

    result = await lib.read(document="D1", section="CHAPTER 3")

    assert "c3 one" in result.content and "c2 one" not in result.content


async def test_a_heading_under_two_sections_asks_which() -> None:
    lib = await _lib()
    await lib.add("guide.md", _chapters())

    ambiguous = await lib.read(document="D1", section="storage")
    chosen = await lib.read(document="D1", section="Chapter 2 > Storage")

    assert ambiguous.meta["status"] == "ambiguous"
    assert "Guide > Chapter 1 > Storage" in ambiguous.content
    assert "Guide > Chapter 2 > Storage" in ambiguous.content
    assert "c1 storage" not in ambiguous.content
    assert "c2 storage" in chosen.content and "c1 storage" not in chosen.content


async def test_an_unknown_section_lists_the_headings() -> None:
    lib = await _lib()
    await lib.add("guide.md", _chapters())
    await lib.add("plain.txt", _flat(2))

    missing = await lib.read(document="D1", section="Appendix")
    plain = await lib.read(document="D2", section="Appendix")

    assert missing.meta["status"] == "no_section"
    assert "Guide > Chapter 1" in missing.content and "Guide > Chapter 2" in missing.content
    assert plain.meta["status"] == "no_section"
    assert "has no headings" in plain.content


# ---------------------------------------------------------------------------
# Find
# ---------------------------------------------------------------------------


async def test_find_needs_every_word_in_any_order_ignoring_case_width_and_spacing() -> None:
    lib = await _lib()
    await lib.add(
        "terms.md",
        [
            ((), ["保修期为 17 个月,ACME 型号"]),
            ((), ["only acme here"]),
            ((), ["unrelated"]),
            ((), ["another 17个月 without the brand"]),
        ],
    )

    # Full-width "acme".
    result = await lib.read(document="D1", find="  17个月   \uff41\uff43\uff4d\uff45 ")

    assert "保修期为 17 个月" in result.content
    assert "[D1#0] · match" in result.content
    assert [(p["ref"], p["role"]) for p in result.meta["passages"]] == [
        ("D1#0", "match"),
        ("D1#1", "context_after"),
    ]
    assert result.meta["matches"] == 1


async def test_find_merges_neighbours_and_never_repeats_a_passage() -> None:
    lib = await _lib()
    await lib.add("p.md", [((), [body]) for body in ["x0", "hit1", "x2", "hit3", "x4", "x5"]])

    result = await lib.read(document="D1", find="hit")

    assert [(p["ref"], p["role"]) for p in result.meta["passages"]] == [
        ("D1#0", "context_before"),
        ("D1#1", "match"),
        ("D1#2", "context_after"),
        ("D1#3", "match"),
        ("D1#4", "context_after"),
    ]


async def test_a_heading_in_a_breadcrumb_is_found() -> None:
    lib = await _lib()
    await lib.add("guide.md", _chapters())

    result = await lib.read(document="D1", find="storage chapter 2")

    assert [p["ref"] for p in result.meta["passages"] if p["role"] == "match"] == ["D1#3"]


async def test_find_without_a_hit_counts_each_word() -> None:
    lib = await _lib()
    await lib.add("terms.md", [((), ["alpha beta"]), ((), ["alpha"]), ((), ["gamma"])])

    result = await lib.read(document="D1", find="alpha delta")

    assert result.meta["status"] == "no_match"
    assert result.meta["passages"] == []
    assert "alpha: 2 passages" in result.content and "delta: 0 passages" in result.content
    assert result.notice is not None and "every word" in result.notice


async def test_find_pages_twenty_matches_at_most() -> None:
    lib = await _lib()
    await lib.add("many.md", [((), [f"hit {i:02d}"]) for i in range(45)])

    first = await lib.read(document="D1", find="hit")
    third = await lib.read(document="D1", find="hit", page=3)

    assert first.meta["matches"] == 45 and first.meta["pages"] == 3
    assert sum(p["role"] == "match" for p in first.meta["passages"]) == 20
    assert sum(p["role"] == "match" for p in third.meta["passages"]) == 5
    assert first.notice is not None and "page=2" in first.notice


async def test_find_keeps_every_hit_and_only_the_neighbours_that_fit() -> None:
    """A page holds its hits whole (one too large for the page stands alone);
    a neighbour joins only when it fits what the page has left."""
    lib = await _lib()
    big = 3000
    bodies = [f"x{i} " + "." * big for i in range(6)]
    bodies[1] = "hit one " + "." * big
    bodies[3] = "hit three " + "." * big
    bodies[5] = "hit five " + "." * (big * 3)
    await lib.add("big.md", [((), [body]) for body in bodies])

    small = await lib.read(document="D1", find="hit")
    wide = await lib.read(document="D1", find="hit", max_tokens=16000)

    # max_tokens 4000 fills to 3,200: each hit alone, no neighbour, on its own page.
    assert small.meta["pages"] == 3
    assert [(p["ref"], p["role"]) for p in small.meta["passages"]] == [("D1#1", "match")]
    assert small.meta["tokens"] <= 3200
    last = await lib.read(document="D1", find="hit", page=3)
    assert [p["ref"] for p in last.meta["passages"]] == ["D1#5"]  # larger than the page
    # 12,800: hits 1 and 3 (about 6,000; hit 5, about 9,000, starts page 2), then
    # D1#0 and D1#2 fit; D1#4 does not.
    assert [(p["ref"], p["role"]) for p in wide.meta["passages"]] == [
        ("D1#0", "context_before"),
        ("D1#1", "match"),
        ("D1#2", "context_after"),
        ("D1#3", "match"),
    ]
    assert wide.meta["tokens"] <= 12800


async def test_section_and_find_together_are_refused() -> None:
    lib = await _lib()
    await _manual(lib)
    with pytest.raises(ValueError, match="section or find"):
        await lib.read(document="D2", section="a", find="b")
