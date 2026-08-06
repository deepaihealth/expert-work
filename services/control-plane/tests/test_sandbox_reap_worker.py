"""Tests for :class:`SandboxReapWorker` —— 波 1 全分支终审 Important-1。

在这个 worker 之前,``AgentSandboxClient.reap`` 在全仓的唯一调用方是
``POST /v1/sandboxes/reap`` 这个管理端点——``_IDLE_TTL_S`` / ``last_used_at``
/ ``list_active(only_idle=True)`` 全套空闲清扫机制从来没有被任何东西定时
驱动过。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from control_plane import sandbox_reap_worker as mod
from control_plane.sandbox_reap_worker import SandboxReapWorker


@dataclass
class FakeRuntime:
    """只记录 ``reap`` 调用的 ``SandboxRuntime`` 替身。

    ``SandboxRuntime`` 的另外四个方法(acquire/release/destroy/exec)这里
    刻意不实现:worker 只该碰 ``reap``,真多碰一个会当场 ``AttributeError``,
    而不是被一个"能吞下任何调用"的宽松 mock 静默放过。
    """

    calls: list[bool] = field(default_factory=list)
    reaped: int = 0
    error: BaseException | None = None

    async def reap(self, *, force: bool) -> int:
        self.calls.append(force)
        if self.error is not None:
            raise self.error
        return self.reaped


@pytest.mark.asyncio
async def test_sweep_once_runs_a_non_forced_reap() -> None:
    """``force=False`` 是这里唯一正确的取值:``force=True`` 会不看空闲时间
    地拆掉表里每一个活跃沙箱(运维强制拆除的语义),周期任务用它等于每隔
    几分钟把所有人正在用的热会话杀一遍。"""
    runtime = FakeRuntime(reaped=3)
    worker = SandboxReapWorker(runtime=runtime)  # type: ignore[arg-type]

    assert await worker.sweep_once() == 3
    assert runtime.calls == [False]


@pytest.mark.asyncio
async def test_sweep_once_swallows_failures() -> None:
    """一次清扫失败不能把任务打死——那正是这个 worker 要防的故障模式
    (循环体抛异常 → task 结束 → 之后再也不清扫,且悄无声息)。"""
    runtime = FakeRuntime(error=RuntimeError("e2b unreachable"))
    worker = SandboxReapWorker(runtime=runtime)  # type: ignore[arg-type]

    assert await worker.sweep_once() == 0
    assert runtime.calls == [False]


@pytest.mark.asyncio
async def test_loop_sweeps_repeatedly_and_stops_cleanly() -> None:
    runtime = FakeRuntime()
    worker = SandboxReapWorker(runtime=runtime, interval_s=0.01)  # type: ignore[arg-type]

    worker.start()
    assert worker.is_running
    for _ in range(200):
        if len(runtime.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    await worker.stop()

    assert len(runtime.calls) >= 2, "周期任务必须真的反复跑,不是只跑一次"
    assert all(force is False for force in runtime.calls)
    assert not worker.is_running


@pytest.mark.asyncio
async def test_a_failing_cycle_does_not_end_the_loop() -> None:
    """失败一次之后下一个周期还得继续——``sweep_once`` 吞异常这件事必须在
    循环里也成立,不只是单独调用时。"""
    runtime = FakeRuntime(error=RuntimeError("e2b unreachable"))
    worker = SandboxReapWorker(runtime=runtime, interval_s=0.01)  # type: ignore[arg-type]

    worker.start()
    for _ in range(200):
        if len(runtime.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    still_running = worker.is_running
    await worker.stop()

    assert len(runtime.calls) >= 3
    assert still_running, "连续失败不能让任务提前结束"


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    runtime = FakeRuntime()
    worker = SandboxReapWorker(runtime=runtime, interval_s=10.0)  # type: ignore[arg-type]

    worker.start()
    first = worker._task
    worker.start()

    assert worker._task is first
    await worker.stop()


def test_default_interval_is_lazier_than_the_supervisor_reaper() -> None:
    """默认间隔是常量不是配置项(同 ``ApprovalGaugeWorker._INTERVAL_S``)。
    它比本地 docker supervisor 自己 reaper 的 10s 松得多是有意的:被回收的
    对象有 15 分钟空闲 TTL,平台侧超时还在底下兜底,扫得再密只是白加负载。
    这条断言防的是有人把它随手改成秒级。"""
    from control_plane.sandbox_reap_worker import _INTERVAL_S
    from expert_work.persistence.sandbox_instance_store import _IDLE_TTL_S

    assert 60.0 <= _INTERVAL_S < _IDLE_TTL_S, (
        f"清扫间隔 {_INTERVAL_S}s 应当明显小于空闲 TTL {_IDLE_TTL_S}s"
        "(否则一个已空闲的沙箱要多等一整个周期才被回收),但也不该密到秒级。"
    )


@pytest.mark.asyncio
async def test_stop_is_bounded_when_a_sweep_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡死的 sweep 不能把关机拖到 SIGKILL —— stop() 等一小会儿就取消它。

    lifespan 顺序 await 每个 worker 的 stop();这里少了上界,一个连不上
    E2B 的 sweep 就能把整个进程的关机挂到 K8s 优雅期耗尽。
    """
    worker = SandboxReapWorker(runtime=FakeRuntime(), interval_s=0.01)  # type: ignore[arg-type]
    entered = asyncio.Event()

    async def _never_returns() -> int:
        entered.set()
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(worker, "sweep_once", _never_returns)
    monkeypatch.setattr(mod, "_STOP_TIMEOUT_S", 0.05, raising=False)

    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)

    # 修复前:stop() 永远等下去,这里超时失败。
    await asyncio.wait_for(worker.stop(), timeout=2)
    assert worker._task is None
