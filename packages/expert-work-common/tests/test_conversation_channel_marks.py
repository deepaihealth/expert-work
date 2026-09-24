"""B-64 —— 段落标记常量本身的性质(与行为无关的那部分)。

``INPUTS_BLOCK_MARK`` / ``WORKSPACE_BLOCK_MARK`` / ``FIGURE_BLOCK_MARK`` 各自的
消费逻辑分别钉在各自消费方的测试里(``test_inputs_block.py`` /
``test_workspace_block_injection.py``);这里只钉「三者互不相同」这一件事 ——
三个标记若字符串撞车,``additional_kwargs`` 上的判定就会把一种段落误判成另一种。
"""

from __future__ import annotations

from expert_work.common.conversation_channel import (
    FIGURE_BLOCK_MARK,
    INPUTS_BLOCK_MARK,
    WORKSPACE_BLOCK_MARK,
)


def test_figure_mark_is_distinct_from_the_other_two() -> None:
    assert FIGURE_BLOCK_MARK not in {INPUTS_BLOCK_MARK, WORKSPACE_BLOCK_MARK}
