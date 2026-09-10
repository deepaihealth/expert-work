"""P-1 —— 轮边界划不出来时,拒绝理由必须是它自己的错误码。

``locate_turn`` 有**两处**会因为「这一轮的区间不可用」而拒绝:

1. 链首 checkpoint 不是入口写入(``source != "input"``)—— 历史损坏,或审批链没
   串全(审批单被清掉、PAUSED 前驱串不回去);
2. 区间算出来了,却和当前历史对不上(``not (0 <= start <= end <= current_len)``)。

两处曾经都借用 ``RUN_NOT_LAST``,而那个码在对外文档里教对接方「去 run 列表取这段
会话最新的那个 ``run_id`` 再试」—— 可这两支里目标**就是**最后一轮,照做会拿回同一
个 id、同一个 422,反复查列表反复重试也绕不出去。所以它们共用自己的
``RUN_BOUNDARY_UNRESOLVED``(同一个失败类,同一种处置,不需要两个码)。

单元测:``locate_turn`` 拿不到 psycopg 池就走 ``_bounds_history`` 回退,只要一个
能吐出快照的假 graph 就够,不必起 Postgres(真库那条路由
``test_supersede_kernel_integration.py`` 覆盖)。第 2 支在真实路径上要靠历史被外力
截短才会发生,构造不出来,所以直接用假快照把 ``end`` 顶到 ``current_len`` 之外。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig

from control_plane.supersede import SupersedeError, locate_turn


class _Snapshot:
    """``_bounds_history`` 只读这三样:``config`` / ``parent_config`` / ``metadata``。"""

    def __init__(self, checkpoint_id: str, *, source: str) -> None:
        self.config: RunnableConfig = {"configurable": {"checkpoint_id": checkpoint_id}}
        self.parent_config: RunnableConfig = {"configurable": {"checkpoint_id": "parent-1"}}
        self.metadata = {"source": source}


class _Graph:
    """没有 checkpointer(``_checkpoint_pool`` 返回 ``None``)→ 走 history 回退。"""

    checkpointer = None

    def __init__(self, source: str) -> None:
        self._source = source

    async def aget_state_history(
        self, config: RunnableConfig, **_: Any
    ) -> AsyncIterator[_Snapshot]:
        del config
        yield _Snapshot("cp-1", source=self._source)

    async def aget_state(self, config: RunnableConfig) -> Any:  # pragma: no cover
        raise AssertionError(f"边界这一支必须在读快照之前就拒掉:{config}")


def _cfg() -> RunnableConfig:
    return {"configurable": {"thread_id": str(uuid4()), "tenant_id": str(uuid4())}}


@pytest.mark.asyncio
async def test_non_input_chain_head_raises_its_own_code_not_run_not_last() -> None:
    with pytest.raises(SupersedeError) as excinfo:
        await locate_turn(
            _Graph("loop"), _cfg(), run_ids=[uuid4()], current_len=3, current_plan=None
        )
    assert excinfo.value.code == "RUN_BOUNDARY_UNRESOLVED"
    # 反向也钉住:借用 RUN_NOT_LAST 正是被销掉的那个写法,回退到它必须红。
    assert excinfo.value.code != "RUN_NOT_LAST"
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_input_chain_head_does_not_take_the_boundary_branch() -> None:
    """判据不是重言式:``source == "input"`` 时不该走到这条拒绝上。

    没有这一条,把 ``!=`` 写成恒真也照样绿 —— 那样这个测试就只是在证明
    「``locate_turn`` 会抛异常」,而不是「它在这个条件下抛」。
    """
    with pytest.raises(AssertionError, match="必须在读快照之前"):
        await locate_turn(
            _Graph("input"), _cfg(), run_ids=[uuid4()], current_len=3, current_plan=None
        )


class _SnapshotWithMessages:
    """``locate_turn`` 读边界快照时只取 ``values["messages"]`` 的长度。"""

    def __init__(self, count: int) -> None:
        self.values: dict[str, Any] = {"messages": [object()] * count, "plan": None}


class _BoundedGraph:
    """链首是入口写入(过第一支),但两个边界快照的长度与 ``current_len`` 对不上。"""

    checkpointer = None

    def __init__(self, *, start: int, end: int) -> None:
        self._lengths = {"parent-1": start, "cp-1": end}

    async def aget_state_history(
        self, config: RunnableConfig, **_: Any
    ) -> AsyncIterator[_Snapshot]:
        del config
        yield _Snapshot("cp-1", source="input")

    async def aget_state(self, config: RunnableConfig) -> _SnapshotWithMessages:
        checkpoint_id = str((config.get("configurable") or {})["checkpoint_id"])
        return _SnapshotWithMessages(self._lengths[checkpoint_id])


@pytest.mark.asyncio
async def test_boundary_outside_current_history_uses_the_same_code() -> None:
    """第 2 支:``end`` 落在 ``current_len`` 之外(历史比这一轮的最后一个快照还短)。

    与第 1 支同一个失败类、同一种处置,所以是同一个码而不是 ``RUN_NOT_LAST`` ——
    这一支里目标同样就是最后一轮,让调用方去查 run 列表只会拿回同一个 id。
    """
    with pytest.raises(SupersedeError) as excinfo:
        await locate_turn(
            _BoundedGraph(start=2, end=5),
            _cfg(),
            run_ids=[uuid4()],
            current_len=3,
            current_plan=None,
        )
    assert excinfo.value.code == "RUN_BOUNDARY_UNRESOLVED"
    assert excinfo.value.code != "RUN_NOT_LAST"
    assert excinfo.value.status_code == 422
    assert "outside the current history" in excinfo.value.message


@pytest.mark.asyncio
async def test_boundary_inside_current_history_is_accepted() -> None:
    """同样防重言式:区间落在历史之内时必须**正常返回**,不该也抛。

    没有这一条,把不变式写成恒假(永远拒)也照样绿。
    """
    location = await locate_turn(
        _BoundedGraph(start=2, end=3),
        _cfg(),
        run_ids=[uuid4()],
        current_len=3,
        current_plan=None,
    )
    assert (location.start, location.end) == (2, 3)
