"""B-206 11-09 尺子修正:评测自己不给模型加「用手上的答」这类压力(生产里没有这种消息)。"""

from __future__ import annotations

import pytest
from kb_agent_eval import READ_LIMIT_NOTE, SEARCH_LIMIT_NOTE  # type: ignore[import-not-found]


@pytest.mark.parametrize("note", [SEARCH_LIMIT_NOTE, READ_LIMIT_NOTE])
def test_limit_notes_state_the_cap_without_pushing_the_model_to_answer(note: str) -> None:
    lowered = note.lower()
    assert "limit" in lowered
    # 11-05 记录:旧措辞「answer with what you have」把模型往「有就答」推,生产里没有这句。
    for pushy in ("answer", "what you have", "anyway", "best you can", "回答", "作答"):
        assert pushy not in lowered, pushy
