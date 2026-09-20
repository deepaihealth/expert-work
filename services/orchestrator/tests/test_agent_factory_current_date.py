"""Dynamic-context current-date injection.

Wires the long-dead ``dynamic_context.inject_current_date`` flag into the
system prompt. Day granularity is deliberate: the prompt is frozen onto
``BuiltAgent`` at build time, so a wall-clock timestamp would bust the
prompt-cache prefix on every run and read stale by the time the model sees
it. Exact time-of-day is deferred to ``exec_python``.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orchestrator.agent_factory import (
    _assemble_system_prompt,
    _current_date_block,
    _resolve_agent_timezone,
)


def test_current_date_block_day_granular_with_timezone() -> None:
    now = datetime(2026, 6, 30, 14, 3, 27, tzinfo=ZoneInfo("Asia/Shanghai"))
    block = _current_date_block(now)
    assert block == (
        "The current date is Tuesday, 2026-06-30 (timezone Asia/Shanghai). "
        "Treat this as today's date when answering. For the exact current time "
        "of day, call the exec_python tool rather than guessing."
    )
    # No wall-clock time leaks in (would bust cache + read stale). The sentence
    # carries no ':' at all, so its absence proves no HH:MM slipped through.
    assert ":" not in block
    # Nudges exec_python for the time-of-day case.
    assert "exec_python" in block


def test_current_date_block_utc_label() -> None:
    now = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
    block = _current_date_block(now)
    assert block.startswith("The current date is Thursday, 2026-01-01 (timezone UTC).")


def test_current_date_block_weekday_is_locale_free() -> None:
    # Formatted via a constant table, not strftime("%A"), so a non-English
    # CI locale cannot localise the weekday.
    for day, name in ((29, "Monday"), (30, "Tuesday"), (7, "Sunday")):
        now = datetime(2026, 6, day, tzinfo=ZoneInfo("UTC"))
        assert f", 2026-06-{day:02d}" in _current_date_block(now)
        assert name in _current_date_block(now)


def test_assemble_includes_current_date_block() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], current_date="DATELINE")
    assert prompt.startswith("BASE")
    assert "# Current date" in prompt
    assert "DATELINE" in prompt


def test_assemble_without_current_date_leaves_base_unchanged() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], current_date=None)
    # B-85 ③ —— 原来这里断言的是 ``prompt == base``。那只是「自己那个块不出现」
    # 的廉价代理,而平台从此恒追加一段 completion contract(无开关),代理失效。
    # 改成断言真正在乎的那件事:base 原样在最前 + 自己那个块确实不在。
    assert prompt.startswith("BASE")
    assert "# Current date" not in prompt


def test_resolve_timezone_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXPERT_WORK_TIMEZONE", raising=False)
    assert _resolve_agent_timezone().key == "Asia/Shanghai"


def test_resolve_timezone_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_TIMEZONE", "America/New_York")
    assert _resolve_agent_timezone().key == "America/New_York"


def test_resolve_timezone_invalid_falls_back_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_TIMEZONE", "Not/AZone")
    assert _resolve_agent_timezone().key == "UTC"
