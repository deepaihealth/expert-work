# perf-latency-observability 终审阻塞项修复报告

终审对 `perf-latency-observability` 分支提出 8 条阻塞项（M-1~M-8）+ 2 条顺手项，全部机械修复。

## Merge

`git merge --ff-only perf-latency-observability` — 快进合并，无冲突（`b6c74bfd`）。

## Commits

1. `2c20d5de` — `fix(admin-ui): firstLlm 排除入口链内嵌套的 LLM span(M-4)`
   （唯一的逻辑修，单独提交）
2. `30233af0` — `docs+fix: perf-latency-observability 终审阻塞项收尾(M-1/2/3/5/6/7/8 + 2 顺手)`
   （文档/文案/顺手项，合并提交）

## 逐条处置

| # | 处置 | 文件 |
|---|---|---|
| M-4 | firstLlm 候选沿 `parentId` 走到根，排除祖先链撞到 `entryIds` 的嵌套 span；加回归用例 | `apps/admin-ui/src/pages/agent_detail/playground/entry_breakdown.ts`, `__tests__/entry_breakdown.test.ts` |
| M-1 | "尚无基线文件" 占位句换成指向 before/after 两个真实文件的一句话 | `tools/bench/baselines/README.md` |
| M-2 | `meta.note` 追加 p95 不可比说明（无预热探针，冷启动拉飞 p95，看 median）+ 5/8 段原因（零召回、无持久工作区）；数字未动 | `tools/bench/baselines/2026-07-27-after.yaml` |
| M-3 | `breakdown_title` 中英文案从「首字/First output」改「入口链 + 首次生成 / Entry chain + first generation」（避免跟 `expert_work_first_output_seconds` 撞名）；bench README 补第三只钟（分解条总数）的口径对照 + label-耦合警告；design doc §6.2 验收表改成不要求总数相等 | `zh-CN.ts`, `en.ts`, `tools/bench/README.md`, `docs/superpowers/specs/2026-07-27-agent-latency-observability-design.md` |
| M-5 | text panel markdown 从 "TTFT and durable-resume are emitted by the SSE worker" 改成 first-node 口径措辞，照 :71/:96 用词 | `tools/observability/dashboards/02-orchestrator.json` |
| M-6 | 文件头注释从"TTFT 规则待发指标出现才落地"改成现状描述，不再跟 :39 已存在的改名规则矛盾 | `tools/observability/rules/sli.yml` |
| M-7 | Gate SLO 表指标名改 `expert_work_session_first_node_seconds`，加改名注（2026-07-27，语义=首个图节点完成，原名误导） | `docs/streams/STREAM-M-DESIGN.md` |
| M-8 | 指标注册表补 `expert_work_first_output_seconds` 一行（histogram，label `source`），组标题「(10)」改「(11)」 | `docs/architecture/subsystems/20-observability.md` |
| 顺手1 | `keepalive_expiry` 60.0→55.0，注释补 AWS ALB 默认 idle 超时共振点说明 | `services/control-plane/src/control_plane/app.py` |
| 顺手2 | `resolve_embedder`/`resolve_reranker` 两个零调用点工厂删掉 `http` 透传参数（含 docstring 对应段落）；`ResolvingEmbedder`/`ResolvingReranker` 类字段保留；`test_runtime.py` 未传该参数，无需同步改 | `services/control-plane/src/control_plane/runtime.py` |

## M-4 测试先红后绿证据

RED（在 `entry_breakdown.ts` 尚未修复前跑新回归用例）：

```
FAIL  src/pages/agent_detail/playground/__tests__/entry_breakdown.test.ts
  > buildBreakdown > ignores llm spans nested inside an entry-chain span when picking firstLlm
AssertionError: expected [ '记忆召回', 'query-rewrite LLM' ] to deeply equal [ '记忆召回', 'LLM 调用' ]
- Expected
+ Received
  [
    "记忆召回",
-   "LLM 调用",
+   "query-rewrite LLM",
  ]
Tests  1 failed | 3 passed (4)
```

GREEN（应用 `isNestedInEntry` 过滤后重跑同一文件）：

```
Test Files  1 passed (1)
     Tests  4 passed (4)
```

## 验证命令结论

1. `pnpm vitest run src/pages/agent_detail/playground/__tests__/` → **19 test files passed, 126 tests passed**
2. `pnpm tsc -b --noEmit`（apps/admin-ui）→ **无输出，通过**
3. `uv run pytest services/control-plane/tests/ -k "runtime" -q` → **74 passed**；`uv run pytest tools/bench/ -q` → **11 passed**
4. `uv run ruff check` → **All checks passed!**；`uv run ruff format --check` → **1478 files already formatted**
5. `python3 -c "import yaml,sys;[yaml.safe_load(open(f)) for f in [...]]"` → **OK**（sli.yml + after.yaml）；`json.load(...)`（02-orchestrator.json）→ **OK**

## 备注（未在验证清单内，仅供参考）

对 `runtime.py` / `test_runtime.py` 跑了一次单文件 `mypy`（非 CI 全库范围）作额外确认，出现 19 条报错，但全部与本次改动无关（`provider` Literal 类型、`InMemorySecretStore` 缺 `list_versions` 协议成员、`type: ignore` 未使用等，行号均不在本次改动范围）——单文件 mypy 缺少 CI 的完整 `mypy_path` 上下文会有假阳性，与仓库既有教训一致，未计入验证结论。
