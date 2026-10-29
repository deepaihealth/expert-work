"""B-222 PR-2 —— the estimate / actual DTOs and the two rules every layer shares:
how a usage row is priced, and when a confirmed estimate is asked again."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from expert_work.protocol.knowledge_estimate import (
    EstimateModels,
    EstimateOrigin,
    IngestEstimate,
    needs_reconfirmation,
    token_micros,
)

_NOW = datetime(2026, 10, 28, tzinfo=UTC)


def _estimate(total: int, models: EstimateModels | None = None) -> IngestEstimate:
    chosen = models or EstimateModels(embedding="qwen/text-embedding-v4")
    return IngestEstimate(
        id=uuid4(),
        origin=EstimateOrigin.UPLOAD,
        created_at=_NOW,
        total_micros=total,
        models=chosen,
        models_fingerprint=chosen.fingerprint(),
    )


def test_a_usage_row_is_priced_like_the_rollup() -> None:
    # 1,234,567 input at ¥0.5 / M (500,000 micros) + 1,000 output at ¥2 / M,
    # divided once and floored.
    assert token_micros(1_234_567, 1_000, 500_000, 2_000_000) == 619_283
    assert token_micros(1, 0, 999_999, 0) == 0
    # Divided once, not per term (0.6 + 0.6 → 1, never 0 + 0).
    assert token_micros(1, 1, 600_000, 600_000) == 1
    assert token_micros(0, 0, 500_000, 2_000_000) == 0


def test_the_fingerprint_follows_every_model() -> None:
    base = EstimateModels(embedding="a/e", recognition="b/r", fallback="c/f", figure="d/g")
    assert base.fingerprint() == EstimateModels(**base.model_dump()).fingerprint()
    for field in ("embedding", "recognition", "fallback", "figure"):
        changed = EstimateModels(**{**base.model_dump(), field: "x/y"})
        assert changed.fingerprint() != base.fingerprint(), field
    # A model moving to another role is a change too.
    swapped = EstimateModels(embedding="a/e", recognition="c/f", fallback="b/r", figure="d/g")
    assert swapped.fingerprint() != base.fingerprint()


def test_another_model_always_asks_again() -> None:
    confirmed = _estimate(100_000)
    fresh = _estimate(100_000, EstimateModels(embedding="qwen/another-embedding"))
    assert needs_reconfirmation(confirmed, fresh)


@pytest.mark.parametrize(
    ("confirmed", "fresh", "ask"),
    [
        (100_000, 120_000, False),  # +20 % is not over 20 %
        (100_000, 120_001, True),  # over 20 % and ≥ ¥0.01
        (40_000, 49_000, False),  # +22.5 % but only ¥0.009
        (40_000, 50_000, True),  # +25 % and exactly ¥0.01
        (0, 9_999, False),  # from ¥0.00: under ¥0.01
        (0, 10_000, True),  # from ¥0.00: ¥0.01
        (100_000, 90_000, False),  # cheaper
    ],
)
def test_a_rise_asks_again_only_past_both_thresholds(confirmed: int, fresh: int, ask: bool) -> None:
    assert needs_reconfirmation(_estimate(confirmed), _estimate(fresh)) is ask


def test_unknown_fields_and_notes_from_a_newer_release_are_tolerated() -> None:
    raw = {
        "id": str(uuid4()),
        "origin": "upload",
        "created_at": _NOW.isoformat(),
        "notes": ["some_future_note"],
        "restore": {"status": "ready"},
        "something_new": 1,
    }
    estimate = IngestEstimate.model_validate(raw)
    assert estimate.notes == ("some_future_note",)
    assert "restore" not in estimate.model_dump()
