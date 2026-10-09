"""B-168 —— 记忆后台写回的 lifespan 接线。

(f) 默认 ``inline``:不起 worker(本轮内同步写回,run 的行为与 PR A 一致);任务表
store 照样挂在 ``app.state`` 上 —— 清除用户 / 删会话不管当前是哪种模式都要删任务行
(切回 inline 前落下的行还在)。``background``:worker 随 lifespan 启停。
"""

from __future__ import annotations

import pytest

from control_plane.app import create_app
from control_plane.memory import MemoryWritebackWorker
from control_plane.settings import Settings
from expert_work.persistence.memory import MemoryWritebackJobStore
from tests.auth_fixtures import build_test_jwt_verifier


@pytest.mark.asyncio
async def test_inline_default_starts_no_worker() -> None:
    app = create_app(
        settings=Settings(checkpointer_backend="memory"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "memory_writeback_worker", None) is None
        assert isinstance(app.state.memory_writeback_job_store, MemoryWritebackJobStore)


@pytest.mark.asyncio
async def test_background_mode_starts_and_stops_the_worker() -> None:
    app = create_app(
        settings=Settings(checkpointer_backend="memory", memory_writeback_mode="background"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        worker = app.state.memory_writeback_worker
        assert isinstance(worker, MemoryWritebackWorker)
        assert worker.is_running
    assert not worker.is_running
