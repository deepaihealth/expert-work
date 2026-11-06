"""B-285 —— the one place trajectory object keys are built and parsed."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from expert_work.protocol.trajectory_keys import (
    MAX_RUN_DAYS,
    child_trajectory_prefix,
    parse_trajectory_key,
    trajectory_days,
    trajectory_key,
)

# Fixed ids: they appear in parametrize ids, which must be identical on every
# xdist worker.
_T = UUID("00000000-0000-4000-8000-0000000000a1")
_C = UUID("00000000-0000-4000-8000-0000000000c1")
_S = UUID("00000000-0000-4000-8000-0000000000d1")


def test_conversation_and_child_keys_and_their_parse() -> None:
    at = datetime(2026, 11, 6, 23, 30, tzinfo=timezone(timedelta(hours=8)))  # 15:30 UTC
    own = trajectory_key(tenant_id=_T, outcome="success", at=at, thread_id=_C)
    child = trajectory_key(tenant_id=_T, outcome="failed", at=at, thread_id=_S, conversation_id=_C)
    assert own == f"trajectories/{_T}/success/2026/11/06/{_C}.jsonl"
    assert child == f"trajectories/{_T}/failed/2026/11/06/{_C}/{_S}.jsonl"
    assert child.startswith(
        child_trajectory_prefix(
            tenant_id=_T, outcome="failed", day=date(2026, 11, 6), conversation_id=_C
        )
    )

    parsed = parse_trajectory_key(own)
    assert parsed is not None
    assert (parsed.tenant_id, parsed.outcome, parsed.day) == (_T, "success", date(2026, 11, 6))
    assert (parsed.conversation_id, parsed.thread_id, parsed.is_child) == (_C, _C, False)
    parsed_child = parse_trajectory_key(child)
    assert parsed_child is not None
    assert (parsed_child.conversation_id, parsed_child.thread_id) == (_C, _S)
    assert parsed_child.is_child


@pytest.mark.parametrize(
    "key",
    [
        "audit_logs/x/2026/11/06/1.json",
        f"trajectories/{_T}/weird/2026/11/06/{_C}.jsonl",
        f"trajectories/{_T}/success/2026/13/06/{_C}.jsonl",
        f"trajectories/{_T}/success/2026/11/06/not-a-uuid.jsonl",
        f"trajectories/{_T}/success/2026/11/06/{_C}.json",
        f"trajectories/{_T}/success/2026/11/06/{_C}/{_S}/{_S}.jsonl",
    ],
)
def test_other_keys_are_not_recognised(key: str) -> None:
    assert parse_trajectory_key(key) is None


def test_an_unknown_outcome_is_refused() -> None:
    with pytest.raises(ValueError):
        trajectory_key(tenant_id=_T, outcome="weird", at=datetime.now(UTC), thread_id=_C)


def test_days_cover_the_write_skew_around_midnight() -> None:
    # A run ending 00:20 UTC: its trajectory may be dated the previous day too.
    end = datetime(2026, 11, 6, 0, 20, tzinfo=UTC)
    assert trajectory_days(created_at=end - timedelta(minutes=5), ended_at=end) == [
        date(2026, 11, 5),
        date(2026, 11, 6),
    ]
    # Midday: one day only.
    noon = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    assert trajectory_days(created_at=noon, ended_at=noon) == [date(2026, 11, 6)]


def test_days_are_clamped_to_the_newest() -> None:
    end = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    days = trajectory_days(created_at=end - timedelta(days=40), ended_at=end)
    assert len(days) == MAX_RUN_DAYS
    assert days[-1] == date(2026, 11, 30)
