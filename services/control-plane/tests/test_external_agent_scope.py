"""``_external_agent_scope`` 的单元判据 —— B-50 PR4。

对外平面的 agent 收口全部落在这三个纯函数上,所以它们单独有一组测试:
六个 handler 的端到端用例证明「接上了」,这里证明「接的这个东西是对的」。
"""

from __future__ import annotations

import pytest

from control_plane.api._external_agent_scope import (
    agent_code_by_key,
    agent_key_for_code,
    external_to_storage,
    storage_to_external,
)
from expert_work.protocol.agent_key import sanitize_agent_key


def test_agent_key_for_code_is_exactly_sanitize_agent_key() -> None:
    """写入侧是 ``sanitize_agent_key(spec.metadata.name)``,读出侧必须逐字节同源。

    ``agent_code`` **就是** ``spec.metadata.name``(``_external.py`` 的
    ``load_owned_thread`` 拿 ``meta.agent_name != agent_code`` 判 404),所以
    这里不查库:查库只多出一条失败路径(spec 被删/改名 → 算出另一个 key →
    **归属正确**的产物 404),而那正是对接方的 P0。
    """
    for code in ("ai-health-plan", "sop2-designer", "A/B", "中文名", "x" * 200):
        assert agent_key_for_code(code) == sanitize_agent_key(code)


def test_agent_key_for_code_does_not_normalize_case() -> None:
    """大小写归一会让两个真正不同的 agent 折进同一棵树。"""
    assert agent_key_for_code("Plan") != agent_key_for_code("plan")


def test_projection_round_trips() -> None:
    """出口剥前缀、入口加前缀 —— 双向闭合。

    只做一边是那种「列表看着对、下载全挂」的形态。
    """
    key = agent_key_for_code("agent-a")
    stored = external_to_storage("客户案例/x.md", agent_key=key)
    assert stored == f"agents/{key}/客户案例/x.md"
    assert storage_to_external(stored, agent_key=key) == "客户案例/x.md"


def test_storage_to_external_drops_other_agents() -> None:
    mine, theirs = agent_key_for_code("agent-a"), agent_key_for_code("agent-b")
    assert storage_to_external(f"agents/{theirs}/b.md", agent_key=mine) is None


def test_storage_to_external_drops_shared_and_flat_legacy() -> None:
    """``shared/`` 与搬迁前的扁平残留都不对外投影。

    ``shared/`` 装的是反推不出归属的 legacy —— 第三方按 ``agent_code`` 提问,
    答案里混进「不知道谁的历史文件」没有意义。扁平残留同理(搬迁跑完就没了)。
    """
    key = agent_key_for_code("agent-a")
    assert storage_to_external("shared/MEMORY.md", agent_key=key) is None
    assert storage_to_external("MEMORY.md", agent_key=key) is None


def test_storage_to_external_does_not_match_a_key_prefix() -> None:
    """``agents/<key>x/…`` 不能被当成 ``agents/<key>/…``。

    前缀比较少一个分隔符就会把 key 是另一个 key 前缀的 agent 串进来。
    ``sanitize_agent_key`` 的 8 位 digest 让这种撞形在真数据上极罕见,但
    谓词的正确性不该依赖输入分布。
    """
    key = agent_key_for_code("agent-a")
    assert storage_to_external(f"agents/{key}x/b.md", agent_key=key) is None


def test_storage_to_external_rejects_the_bare_agent_dir() -> None:
    """``agents/<key>/`` 自己不是一个文件条目,剥完是空串,不能当成有效路径。"""
    key = agent_key_for_code("agent-a")
    assert storage_to_external(f"agents/{key}/", agent_key=key) is None
    assert storage_to_external(f"agents/{key}", agent_key=key) is None


def test_external_to_storage_without_agent_key_is_identity() -> None:
    """空 ``agent_key`` 回落用户根 —— 与工具层 ``agent_workspace_root('')`` 同款语义。"""
    assert external_to_storage("x.md", agent_key="") == "x.md"
    assert storage_to_external("x.md", agent_key="") == "x.md"


def test_agent_code_by_key_maps_back_to_codes() -> None:
    codes = ["ai-health-plan", "sop2-designer"]
    mapping = agent_code_by_key(codes)
    assert mapping == {agent_key_for_code(c): c for c in codes}


def test_agent_code_by_key_ignores_blank_names() -> None:
    """``thread_meta.agent_name`` 可空;空串不能变成一个 ``agent-<digest>`` 条目。

    ``sanitize_agent_key('')`` 是 ``agent-e3b0c442``(``or 'agent'`` 兜底),
    放进映射表会凭空造出一个第三方看得见、却对不上任何真 agent 的 code。
    """
    assert agent_code_by_key(["", "  ", "real"]) == {agent_key_for_code("real"): "real"}


@pytest.mark.parametrize("bad", ["../x", "/abs", "a\x00b", ""])
def test_external_to_storage_is_not_a_validation_bypass(bad: str) -> None:
    """投影**不校验** —— 校验必须在加前缀之前对原串做。

    这条钉的是调用顺序:先拼前缀再校验的话,``../x`` 会被拼成
    ``agents/<key>/../x``,``..`` 还在但已经climb不出用户根,看起来「合法」
    ——于是校验放行,实际读到的是别的 agent 的目录。这里断言投影自己**不**
    承担这个责任,handler 必须先过 ``_safe_workspace_relpath``。
    """
    key = agent_key_for_code("agent-a")
    assert external_to_storage(bad, agent_key=key) == f"agents/{key}/{bad}"
