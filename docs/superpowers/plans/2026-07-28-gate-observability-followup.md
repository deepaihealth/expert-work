# 委托闸可观测性 + 加固 follow-up 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** PR3(#1062)终审 follow-up 池里「现在就修」的 8 项:指标可见性(死 panel/告警/面板/文档)+ 闸与 sse 批写小加固。

**Architecture:** 两个零交集 task 并行:T1 纯观测配置(tools/observability + docs),T2 orchestrator 代码加固 + 测试。

**来源:** PR3 终审报告(`.superpowers/sdd/progress.md` follow-up 池)+ 2026-07-28 调研裁决。

## Global Constraints

- 分支 `gate-obs-followup`(基于 main 161a2219)。
- CI 门:`ruff check` 全库 + `ruff format --check` + CI-scope mypy + orchestrator pytest(`DOCKER_HOST= uv run`)。
- 指标名规则 `^expert_work_[a-z][a-z0-9_]*$`;新 counter 一律经 `expert_work_counter` 包装器(不裸 import prometheus_client)。
- 提交 conventional commits,无 attribution。

## 并行波次

| Task | 文件域 | 冲突 |
|---|---|---|
| T1 观测配置 | `tools/observability/{rules,dashboards}/`、`docs/architecture/subsystems/20-observability.md` | 零 |
| T2 代码加固 | `services/orchestrator/src/orchestrator/{tools/_budget.py,tools/subagent.py,tools/spawn_worker.py,sse.py}` + tests | 零 |

Wave 1:T1 ∥ T2。

---

### Task 1: 观测配置(死 panel 修复 + 新指标接线 + 文档)

**Files:**
- Modify: `tools/observability/dashboards/01-overview.json`、`02-orchestrator.json`、`03-sandbox.json`
- Modify: `tools/observability/rules/sli.yml`、`tools/observability/rules/alerts.yml`
- Modify: `docs/architecture/subsystems/20-observability.md`

**要求:**

1. **修 3 处死 panel expr**(改名前旧指标名,已核实真名):
   - `02-orchestrator.json:63` 与 `01-overview.json` 同款:`expert_work_llm_tokens_total` → `expert_work_llm_token_usage_total`(真名出处 `packages/expert-work-runtime/src/expert_work/runtime/middleware/token_usage.py:65`;改 expr 时核对 label 集是否也变了,以真代码为准)
   - `02-orchestrator.json:129`:`expert_work_tool_call_duration_seconds_bucket` → `expert_work_tool_latency_seconds` 对应 bucket(真名出处 `services/orchestrator/src/orchestrator/graph_builder/builder.py:241`;histogram 后缀按真类型)
   - `03-sandbox.json`:`expert_work_sandbox_pool_size` → `expert_work_sandbox_pool_ready`(出处 `services/sandbox-supervisor/src/sandbox_supervisor/pool.py:75`)
2. **新指标接线**(PR3 三个 + T2 将新增的 fail_open,名字定死 `expert_work_delegation_gate_fail_open_total`):
   - `sli.yml` 加 recording:`expert_work:sli:delegations_gated:rate_5m = rate(expert_work_delegations_gated_total[5m])`(照文件既有命名风格)
   - `alerts.yml` 的 `expert_work_gate_watch` 组加两条(阈值风格照组内既有条目,注释写明"基线未知,先保守观察级"):①gated 15 分钟增量 > 0 → warning「委托闸出现饱和拒绝」;②fail_open 15 分钟增量 > 0 → warning「闸容量配置读取失败,fail-open 中」
   - `02-orchestrator.json` 加一行 panel(gated rate + queue_dropped rate + fail_open rate 三条曲线,布局照现有 panel JSON 结构最小改)
3. **`20-observability.md`**:①修 M-4 双写过时描述(`:309` 附近「run_agent 双写中 RunEventStore.append 成功」→ 有界队列+后台批写 append_batch,drop-oldest,终态前 drain);②指标注册表(`:176-245`)补登记:`expert_work_delegations_gated_total`(+tool 标签,T2 加)、`expert_work_run_event_queue_dropped_total`、`expert_work_delegation_gate_fail_open_total`、`expert_work_built_agent_cache_entries`(PR2 漏登)。
4. **自检**:改完用 `python3 -c "import json; json.load(open(...))"` 校验三张 JSON;`python3 -c "import yaml; yaml.safe_load(...)"` 校验两个 yml;grep 全仓确认修后的指标名与代码发射点一致(每个名字给一个真代码出处行)。

- [ ] Step 1: 逐文件修改 + 自检命令跑通
- [ ] Step 2: Commit `fix(observability): 修 3 处死 panel 指标名 + 委托闸三指标接线(recording/告警/面板/注册表)+ 批写文档更新`

---

### Task 2: 闸与批写代码加固(4 处 + 测试)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/_budget.py`
- Modify: `services/orchestrator/src/orchestrator/tools/subagent.py`、`tools/spawn_worker.py`
- Modify: `services/orchestrator/src/orchestrator/sse.py`
- Test: `services/orchestrator/tests/test_delegation_gate.py`、`tests/test_sse_persistence.py`

**要求(TDD,每项先测后码):**

1. **fail-open counter + 翻转日志**(_budget.py):
   - 新 counter `expert_work_delegation_gate_fail_open_total`(无标签,`expert_work_counter` 包装器)。
   - `_read_capacity` except 分支:counter inc;日志改「状态翻转时才打」——加 `self._provider_healthy: bool = True` 字段,健康→失败翻转时 `logger.warning(..., exc_info=True)` 一次,持续失败期间静默(只 inc counter),失败→恢复翻转时 `logger.info("delegation_gate.capacity_provider_recovered")`。
   - 测试:provider 连抛 3 次 → counter +3 但 warning 只 1 条(caplog);恢复后再失败 → 再 1 条。
2. **gated counter 加 tool 标签**(_budget.py 定义处 + 两工具 `.inc()` 处):
   - `DELEGATIONS_GATED = expert_work_counter(..., ("tool",))`;`subagent.py` 用 `.labels(tool="subagent").inc()`,`spawn_worker.py` 用 `.labels(tool="spawn_worker").inc()`。
   - 既有测试若断言无标签 counter 需同步;T1 的告警 expr 用 `sum(rate(...))` 不受标签影响(已在 T1 要求里)。
3. **闸等待不超 run deadline**(_budget.py + 两工具):
   - `DelegationGate.acquire(self, *, timeout_s: float | None = None) -> bool`——None 用 `self._timeout_s`,否则 `min(self._timeout_s, timeout_s)`(下限钳 0:剩余已负直接 False)。
   - 两工具 call 处:`remaining = ctx.deadline_at - time.monotonic() if ctx.deadline_at is not None else None`,`await gate.acquire(timeout_s=remaining)`。入口 deadline 检查已保证 remaining>0 才走到这。
   - 测试:deadline 剩 0.05s、闸满 → acquire 在 ~0.05s 返回 False(非 30s);deadline 充裕 → 仍 30s 语义(用小 timeout_s 构造验证 min 取向两侧)。
4. **sentinel 驱逐兜底**(sse.py finally 的 QueueFull 分支):
   - drop-oldest 拿到的若是 `None`(sentinel 自己):再 get_nowait 一个真帧丢弃(task_done+dropped counter,标真帧 event_name),然后 put record/None 保 sentinel 仍在队尾。写成小 helper 或就地注释清楚;保持全同步(此路径零 await 铁律)。
   - 同时修 **M-1**:`_enqueue_event` drop-oldest 分支的 counter 改标**被丢帧**的 event_name(`dropped = persist_queue.get_nowait()`,`_run_event_queue_dropped.labels(event_name=dropped.event_name if dropped is not None else "sentinel").inc()`——sentinel 被丢时按上面兜底逻辑不该计入 dropped,理顺两处口径,谁被真丢谁上 counter)。
   - 测试:①塞满队列后 enqueue,断言 counter 标的是最老帧的名字而非新帧;②构造 finally 时队满且队头是 sentinel 的场景(直接对小队列调收尾逻辑),断言 sentinel 仍最终入队、writer 能正常退出。
5. **flush-on-empty 测试钉**(test_sse_persistence.py):
   - 直接单测 `_persist_writer`:小队列喂 1 条(不发 sentinel),断言落库耗时远小于 `_PERSIST_FLUSH_INTERVAL_S`(如 <50ms)——钉住「队空即刷」,防重构把 100ms 尾税加回。

- [ ] Step 1(RED):按 1-5 写全部新测试,跑 FAIL
- [ ] Step 2(GREEN):实现,`DOCKER_HOST= uv run pytest services/orchestrator/tests/test_delegation_gate.py services/orchestrator/tests/test_sse_persistence.py -x` 绿
- [ ] Step 3: orchestrator 全测 + ruff 两道 + `uv run mypy services/orchestrator/src`
- [ ] Step 4: Commit `fix(orchestrator): 闸加固三处(fail-open counter+tool 标签+deadline 耦合)+ sse 批写两处收口(sentinel 兜底+drop 计数正名)+ flush-on-empty 测试钉`

---

## 验证(整 PR)

- T1 自检命令全过 + T2 测试全绿;CI 门全绿。
- 无行为回归:T2 改动全部有既有测试回归覆盖(gate 25 测 + sse 12 测)。
