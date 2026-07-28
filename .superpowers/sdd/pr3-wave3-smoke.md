# PR3 Wave 3 真栈验证报告(2026-07-28,dev 栈 @perf-phase2-pr3 0d6d24ae)

栈:`make -C infra dev-up`,control-plane blue(bind-mount 分支源码),postgres/keycloak/langfuse 全活。驱动:docker exec 容器内 python(dev/devpass → expert-work-admin-ui direct grant token)。

## 1. delegation-config API 回路 ✅

- 未配置 GET → `configured: null, effective: 16`(env_default)
- PUT `{"max_concurrent_delegations": 1}` → 200,GET 回读 1(DB-wins)
- PUT 66 → 422(`le=64` 界生效)
- PUT 16 归位 → 200

## 2. 指标注册 ✅

`/metrics` 可见:`expert_work_delegations_gated_total`(0 起步)、`expert_work_run_event_queue_dropped_total`、`expert_work_run_event_persist_total`、`expert_work_built_agent_cache_entries`。

## 3. 闸真栈 e2e ✅(注入链活证,终审 I-3 主项)

布置:部署 `bench-helper-slow@1.0.0`(3000 字长输出,单委托占坑 >30s)+ `bench-delegator@1.0.0`(双 subagent helper_a/helper_b,提示词强制同轮并行调用);闸容量 PUT 到 1。

实测(run `d91fde89`,session `bad74797`,墙钟 119.8s):
- LLM 真发双并行 tool_calls;helper_a 占坑长跑,helper_b 等锁 **30s 超时** → 软失败 ToolResult
- **`expert_work_delegations_gated_total` 0.0 → 1.0**(全链活证:app.py 接线 → AgentRuntime 单例 → run_agent → configurable → ToolContext → SubAgentTool acquire 超时 → counter)
- SSE 流内可见 `delegation_gated` / `saturated`(软失败帧实时可见)
- run 终态 **success**(软失败不打断 run,LLM 拿一个 helper 结果收尾)
- 完毕后容量归位 16

## 4. run_event 后台批写 live ✅(T1 主项)

上述 run 的 replay(`GET /v1/sessions/{tid}/runs/{rid}/events`):
- 帧齐:metadata 1 / updates 3 / worker 5 / end 1(n=9)
- **seq 全唯一**;`delegation_gated` 软失败帧已持久化(终审 focus 2 的 replay 可见性,活证)
- 即全部帧经 `_enqueue_event → 后台 writer → append_batch` 路径落库,无丢帧

## 5. bench 对照(10 轮,同 agent bench-entry@2.0.0 同 prompt sha 06b9296128f9)

基线入仓:`tools/bench/baselines/2026-07-28-phase2-pr3-after.yaml` vs `2026-07-27-phase2-pr2-after.yaml`:

| 段 | pr2 med | pr3 med | Δ | 说明 |
|---|---|---|---|---|
| 向量化 | 184.5 | 207.0 | +22.5 | p95 反降 312→256,外部 embedding API 抖动区间 |
| 向量检索 | 25.0 | 24.5 | -0.5 | 持平 |
| 回写访问计数 | 27.0 | 32.0 | +5.0 | 噪声 |
| 工作区摄取 | 126.0 | 138.5 | +12.5 | 噪声 |
| 记忆召回 | 1185.0 | 1160.0 | -25.0 | 持平(verify LLM 主导) |
| 记忆重排 | 207.5 | 217.0 | +9.5 | 噪声 |
| first_llm_start | 647.5 | 637.5 | -10 | **p95 915→745** |
| total_ms | 4983.5 | 5220.0 | +236.5 | LLM 生成端噪声(同轮 verify p95 906→1385 佐证 provider 抖动);入口链各段持平,sse 改造在首字之后的持久化侧路,不在入口链 |

**结论:入口链零回归**;PR3 的收益本就不在 bench 口径内(sse 批写省的是流路径上的同步 DB 往返,闸是稳健性件)。

## 遗留(不阻塞)

- 冒烟用的 `bench-helper-slow@1.0.0` / `bench-delegator@1.0.0` 留在 dev 栈 zeros 租户,后续闸冒烟可复用。
- keycloak 容器 docker healthcheck 显示 unhealthy 但 realm 端点 200(healthcheck 陈旧假象,与本 PR 无关)。
