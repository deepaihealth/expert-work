# Task 2 报告 — 闸与批写代码加固(4 处 + 测试)

## 范围

`services/orchestrator/src/orchestrator/tools/_budget.py`、
`tools/subagent.py`、`tools/spawn_worker.py`、`sse.py` 四处加固,
`tests/test_delegation_gate.py` + `tests/test_sse_persistence.py` 全新测试钉。

TDD 顺序:Step1(RED,新符号未定义,两测试文件均 collection ImportError)→
Step2(实现,GREEN)→Step3(全量验证)。中途做了一次额外的“反向验证”:
把 `sse.py` 的 M-1 修复临时替换回旧的(标新帧而非被丢帧)逻辑,确认
两个新 `_put_dropping_oldest` 单测立即变红,再改回正确实现——证明这两个
测试不是空转断言。误操作 `git checkout --` 曾一次性丢弃 `sse.py` 全部改动,
已按原样重建(diff 与丢弃前逐字节一致,已用 git diff 核对)。

## 实现明细

### 1. fail-open counter + 翻转日志(`_budget.py`)

- 新增无标签 counter `DELEGATION_GATE_FAIL_OPEN`
  (`expert_work_delegation_gate_fail_open_total`)。
- `DelegationGate.__init__` 新增 `self._provider_healthy: bool = True`。
- `_read_capacity` 的 `except Exception:` 分支:每次失败都 `.inc()`;
  仅在健康→失败翻转时(`self._provider_healthy` 从 True 翻 False)才
  `logger.warning(..., exc_info=True)` 一次,翻转后置 False,后续持续失败
  期间静默(counter 仍继续递增)。
- 读取成功分支:若之前是不健康状态(`not self._provider_healthy`),记
  `logger.info("delegation_gate.capacity_provider_recovered")` 并翻回 True。

### 2. gated counter 加 `tool` 标签(`_budget.py` + 两工具)

- `DELEGATIONS_GATED = expert_work_counter(..., ("tool",))`。
- `subagent.py`:`DELEGATIONS_GATED.labels(tool="subagent").inc()`。
- `spawn_worker.py`:`DELEGATIONS_GATED.labels(tool="spawn_worker").inc()`。
- 同步了唯一一处断言旧无标签 counter 的既有测试
  (`test_provider_timeout_error_not_miscounted_as_gate_full`)为
  `.labels(tool="subagent")`。

### 3. 闸等待不超 run deadline(`_budget.py` + 两工具)

- `DelegationGate.acquire(self, *, timeout_s: float | None = None) -> bool`:
  `None` 用 `self._timeout_s`;否则取 `min(self._timeout_s, timeout_s)`;
  取值 <= 0 直接返回 `False`(不进入 `asyncio.timeout`,不占坑)。
- 两工具 call 处:
  `remaining = ctx.deadline_at - time.monotonic() if ctx.deadline_at is not None else None`,
  `await gate.acquire(timeout_s=remaining)`。

### 4. sentinel 驱逐兜底 + M-1 drop 计数正名(`sse.py`)

抽了一个模块级共享同步 helper `_put_dropping_oldest(queue, item)`
(零 await),`_enqueue_event` 的 drop-oldest 分支与 `finally` 的 sentinel
put 分支都改调它——顺带把两处口径统一:

- 正常情形:`get_nowait()` 拿到的最老帧就是真实被丢的那帧,
  `_run_event_queue_dropped.labels(event_name=dropped.event_name).inc()`
  ——修了 M-1(旧代码错标成新塞入帧的 `event_name`,只是在纯同质
  "updates" 流量下巧合看不出来)。
- 兜底分支:若被驱逐的最老帧本身就是一个滞留的 `None` sentinel(现有
  单一 sentinel-at-shutdown 流程下理论不可达,但直接对小队列调用该
  helper 可构造),再 `get_nowait()` 一次拿真帧丢弃并计数
  (`task_done` + `.labels(event_name=real.event_name).inc()`),新
  item(真帧或新 sentinel)最终仍落队尾。sentinel 被挤出本身不计入
  drop 计数(它不是真实数据丢失)。
- `finally` 分支此前从不给 `_run_event_queue_dropped` 计数(哪怕真丢了
  一帧真数据腾位给 sentinel);统一后与 `_enqueue_event` 同口径,常规
  drop-oldest 情形现在也会计数——这是刻意的行为收口(brief 原文“理顺
  两处口径,谁被真丢谁上 counter”)。

### 5. flush-on-empty 测试钉(`test_sse_persistence.py`)

新增 `test_persist_writer_flushes_immediately_when_queue_goes_empty`:
直接对 `_persist_writer` 喂 1 条记录(不发 sentinel),轮询等待
`append_batch` 被调用,断言耗时 `< 0.05s`(`<< _PERSIST_FLUSH_INTERVAL_S`
的 0.1s),钉住“队空即刷”优化,防止回归把 100ms 尾税加回。

## 测试新增清单

`test_delegation_gate.py`(+15 个新测试,另同步 1 个既有测试的 counter
访问方式):
- fail-open 翻转日志:`test_read_capacity_failure_increments_counter_every_time_but_warns_once`、
  `test_read_capacity_recovery_logs_info_once_and_next_failure_warns_again`
- deadline 耦合(gate 层):`test_acquire_timeout_s_smaller_than_default_bounds_the_wait`、
  `test_acquire_timeout_s_larger_than_default_does_not_extend_the_wait`、
  `test_acquire_timeout_s_none_uses_gate_default`、
  `test_acquire_negative_timeout_s_returns_false_immediately`
- 工具层 wiring + 标签:`test_subagent_call_gate_refusal_is_soft_fail_and_skips_child_run`
  (加计数断言)、`test_subagent_call_passes_remaining_deadline_as_gate_timeout`、
  `test_subagent_call_gate_timeout_s_none_when_no_run_deadline`、
  `test_subagent_call_gate_wait_bounded_by_run_deadline_not_gate_default`,
  以及 spawn_worker 侧对称的 4 个

`test_sse_persistence.py`(+3 个新测试):
- `test_put_dropping_oldest_labels_the_evicted_frame_not_the_new_one`(M-1)
- `test_put_dropping_oldest_stray_sentinel_evicts_a_real_frame_instead`(兜底 + writer 正常退出)
- `test_persist_writer_flushes_immediately_when_queue_goes_empty`(flush-on-empty 钉)

`_FakeGate` 扩展了 `acquire(self, *, timeout_s: float | None = None)` +
`acquire_timeout_s: list[float | None]` 记录,供 wiring 断言用。

## 验证结果

- `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_delegation_gate.py services/orchestrator/tests/test_sse_persistence.py -q`
  → **52 passed**。
- `DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest services/orchestrator -q`
  → **1922 passed, 1 skipped**(skip 是无关的 `docx` 模块缺失,pre-existing)。
- `uv run ruff check services/orchestrator` → All checks passed。
- `uv run ruff format --check services/orchestrator` → 227 files already formatted。
- `uv run mypy services/orchestrator/src` → Success: no issues found in 83 source files。

## Concerns / 备注

- `_budget.py:_read_capacity` 的翻转日志文案沿用了英文风格(与文件既有注释/日志一致);
  恢复日志用了结构化 key `"delegation_gate.capacity_provider_recovered"`,与仓库其它
  `logger.info("<component>.<event>")` 惯例一致。
- `_put_dropping_oldest` 把 `finally` 分支此前“从不计数真实丢帧”的行为改成了“计数”
  ——这是行为收口而非纯 bug fix,已在报告里显式标出,供 T1(告警 expr)或后续 review 知悉;
  该 counter 系列(`expert_work_run_event_queue_dropped_total`)本就已在生产使用,数值口径
  变化不影响其标签集合或类型。
- 未触碰 30s 默认值本身,只做了 `min()` 耦合,符合铁律。
- sse 收尾路径(`finally` 分支)全程保持零 await(`_put_dropping_oldest` 内部只有
  `get_nowait`/`put_nowait`/`task_done`,无 `await`)。
