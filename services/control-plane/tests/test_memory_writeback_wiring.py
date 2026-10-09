"""B-168 —— 记忆后台写回的 lifespan 接线。

(f) ``inline``(逃生口):不起 worker(本轮内同步写回,run 的行为与 PR A 一致),建图拿到
的 ``MemoryEnv`` 不带任务表;任务表 store 照样挂在 ``app.state`` 上 —— 清除用户 / 删会话
不管当前是哪种模式都要删任务行(切回 inline 前落下的行还在)。``background``(B2 起默认):
worker 随 lifespan 启停,建图拿到的 ``MemoryEnv`` 带任务表 + 本副本 worker 的 ``wake``。
"""

from __future__ import annotations

from typing import Any

import pytest

from control_plane import app as app_module
from control_plane.app import create_app
from control_plane.memory import MemoryWritebackWorker
from control_plane.settings import Settings
from expert_work.persistence.memory import MemoryWritebackJobStore
from tests.auth_fixtures import build_test_jwt_verifier


class _BuilderSpy:
    """记下 lifespan 交给 ``make_agent_builder`` 的 ``memory_env``,其余原样转交。"""

    def __init__(self) -> None:
        self.memory_envs: list[Any] = []
        self._real = app_module.make_agent_builder

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.memory_envs.append(kwargs.get("memory_env"))
        return self._real(*args, **kwargs)


@pytest.mark.asyncio
async def test_inline_starts_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _BuilderSpy()
    monkeypatch.setattr(app_module, "make_agent_builder", spy)
    app = create_app(
        settings=Settings(checkpointer_backend="memory", memory_writeback_mode="inline"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "memory_writeback_worker", None) is None
        assert isinstance(app.state.memory_writeback_job_store, MemoryWritebackJobStore)
    [env] = spy.memory_envs
    assert (env.writeback_jobs, env.wake_writeback) == (None, None)


@pytest.mark.asyncio
async def test_background_mode_starts_and_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _BuilderSpy()
    monkeypatch.setattr(app_module, "make_agent_builder", spy)
    app = create_app(
        settings=Settings(checkpointer_backend="memory"),  # background is the default
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        worker = app.state.memory_writeback_worker
        assert isinstance(worker, MemoryWritebackWorker)
        assert worker.is_running
        [env] = spy.memory_envs
        assert env.writeback_jobs is app.state.memory_writeback_job_store
        assert env.wake_writeback == worker.wake
    assert not worker.is_running
