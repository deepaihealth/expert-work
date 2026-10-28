"""B-206 K-2 — the pure parts of ``knowledge_read``: labels, pages, windows, find."""

from __future__ import annotations

from orchestrator.tools.knowledge_read_view import (
    around_window,
    find_units,
    label,
    normalize,
    pack_pages,
    squeeze,
    wanted_path,
)


def test_a_label_lists_chunk_numbers_as_runs() -> None:
    assert label("D12", [7]) == "D12#7"
    assert label("D12", [7, 8, 9]) == "D12#7-9"
    assert label("D12", [7, 8, 9, 41, 43, 44]) == "D12#7-9,41,43-44"
    assert label("D12", []) == "D12"


def test_a_page_fills_to_the_budget_and_always_takes_one_unit() -> None:
    assert pack_pages([10, 10, 10, 10], 25) == [(0, 2), (2, 4)]
    assert pack_pages([10, 40, 10], 25) == [(0, 1), (1, 2), (2, 3)]
    assert pack_pages([5, 5, 5, 5, 5], 100, max_per_page=2) == [(0, 2), (2, 4), (4, 5)]
    assert pack_pages([], 25) == []


def test_a_window_grows_nearest_first_and_each_side_stops_at_its_first_misfit() -> None:
    sizes = [10, 30, 10, 10, 10, 10]
    # target 2: before 1 is 30 (does not fit), after 3 and 4 fit; before stays shut.
    assert around_window(sizes, 2, 2, 39) == (0, 2)
    assert around_window(sizes, 2, 2, 40) == (1, 0)
    assert around_window(sizes, 0, 3, 100) == (0, 3)
    assert around_window(sizes, 5, 1, 5) == (0, 0)


def test_text_is_compared_without_case_width_or_spacing() -> None:
    assert normalize("  \uff21\uff22\uff23  Def\n") == "abc def"  # full-width ABC
    assert squeeze("17 个 月") == "17个月"
    assert wanted_path(" Chapter 2 >  > Storage ") == ("chapter 2", "storage")


def test_find_units_never_repeat_a_passage() -> None:
    texts = ["x", "hit", "x", "hit", "hit", "x"]
    assert find_units(texts, ["hit"]) == [
        [(0, "context_before"), (1, "match"), (2, "context_after")],
        [(3, "match")],
        [(4, "match"), (5, "context_after")],
    ]
    assert find_units(texts, ["hit", "nope"]) == []
