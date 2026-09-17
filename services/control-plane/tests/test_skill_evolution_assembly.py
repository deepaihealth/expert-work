"""Tests for the SE-6d pure assembly helpers."""

from __future__ import annotations

from typing import Any

from control_plane.skill_evolution_assembly import (
    SIGNAL_TIER_CALIBRATED,
    SIGNAL_TIER_HARD,
    SIGNAL_TIER_UNVERIFIED,
    extract_task_prompt,
    first_user_message,
    is_screen_sampled,
    select_signal_tier,
)


def test_signal_tier_hard_when_verifier_present() -> None:
    assert select_signal_tier(has_hard_verifier=True, judge_calibrated=False) == SIGNAL_TIER_HARD


def test_signal_tier_calibrated_when_no_verifier_but_calibrated() -> None:
    tier = select_signal_tier(has_hard_verifier=False, judge_calibrated=True)
    assert tier == SIGNAL_TIER_CALIBRATED


def test_signal_tier_unverified_otherwise() -> None:
    tier = select_signal_tier(has_hard_verifier=False, judge_calibrated=False)
    assert tier == SIGNAL_TIER_UNVERIFIED


def test_extract_task_prompt_prefers_known_keys() -> None:
    assert extract_task_prompt({"prompt": "do X"}) == "do X"
    assert extract_task_prompt({"message": "hi"}) == "hi"
    assert extract_task_prompt({"unrelated": "nope"}) is None
    assert extract_task_prompt({"prompt": "   "}) is None


def test_first_user_message() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "  summarise this  "},
        {"role": "assistant", "content": "ok"},
    ]
    assert first_user_message(messages) == "summarise this"


def test_first_user_message_skips_hidden_entries() -> None:
    """B-67 —— 只带 inputs 的 run 用户消息正文为空,下一条 user 是隐藏「本轮输入」段;
    重放任务不能拿它当 prompt。"""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "[inputs block]", "hidden": True},
        {"role": "assistant", "content": "ok"},
    ]
    assert first_user_message(messages) is None
    assert first_user_message([*messages, {"role": "user", "content": "real ask"}]) == "real ask"


def test_first_user_message_none_when_absent() -> None:
    assert first_user_message([{"role": "assistant", "content": "hi"}]) is None


# ---------------------------------------------------------------------------
# SE-16 (SE-A45) — deterministic screen sampling
# ---------------------------------------------------------------------------


def test_is_screen_sampled_bounds() -> None:
    assert is_screen_sampled("any-key", 0) is False
    assert is_screen_sampled("any-key", -5) is False
    assert is_screen_sampled("any-key", 100) is True
    assert is_screen_sampled("any-key", 150) is True


def test_is_screen_sampled_is_deterministic() -> None:
    """Same key + same rate always lands on the same side — a retry re-sweep
    cannot re-roll its way past the sample."""
    for key in (f"traj/{i}" for i in range(50)):
        first = is_screen_sampled(key, 30)
        assert all(is_screen_sampled(key, 30) is first for _ in range(3))


def test_is_screen_sampled_rate_roughly_holds() -> None:
    keys = [f"tenant-a/success/2026/07/02/{i}.jsonl" for i in range(2000)]
    hits = sum(1 for k in keys if is_screen_sampled(k, 50))
    assert 850 <= hits <= 1150  # ~50% with generous slack (hash, not RNG)
    subset = [k for k in keys if is_screen_sampled(k, 5)]
    # A 5% sample must be a strict subset of the 50% sample (bucket < pct).
    assert all(is_screen_sampled(k, 50) for k in subset)


def test_replay_config_carries_tenant_and_user() -> None:
    """Live pilot finding #5 — TokenUsageMiddleware reads tenant_id/user_id
    from ``config.configurable``; a replay config without them silently
    skips metering for every with/without graph run."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from control_plane.skill_evolution_wiring import _make_replay_config_factory
    from expert_work.protocol import CurationCandidateRecord

    tenant, user = uuid4(), uuid4()
    candidate = CurationCandidateRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="assistant",
        user_id=user,
        thread_id=uuid4(),
        trajectory_key=f"k/{uuid4()}",
        outcome="success",
        signal="positive_feedback",
        detected_at=datetime.now(UTC),
    )
    factory = _make_replay_config_factory(candidate)

    cfg = factory("case-1", True)
    configurable: Any = cfg["configurable"]
    assert configurable["tenant_id"] == str(tenant)
    assert configurable["user_id"] == str(user)
    assert configurable["thread_id"].startswith("se-replay-")
    # Fresh thread id per call — replays never share checkpointer state.
    assert factory("case-1", False)["configurable"]["thread_id"] != configurable["thread_id"]


def test_replay_config_omits_absent_user() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from control_plane.skill_evolution_wiring import _make_replay_config_factory
    from expert_work.protocol import CurationCandidateRecord

    candidate = CurationCandidateRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_name="assistant",
        thread_id=uuid4(),
        trajectory_key=f"k/{uuid4()}",
        outcome="success",
        signal="positive_feedback",
        detected_at=datetime.now(UTC),
    )
    cfg = _make_replay_config_factory(candidate)("case-1", True)
    configurable: Any = cfg["configurable"]
    assert "user_id" not in configurable
    assert configurable["tenant_id"] == str(candidate.tenant_id)
