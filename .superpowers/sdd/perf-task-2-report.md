# Perf Task 2 报告 —— trace facade 中文标签 + group 字段

## 0. 前置

`git merge --ff-only perf-latency-observability` 已执行(fast-forward,`d81e81d2` → `6a73af06`)。需求来源:`docs/superpowers/plans/2026-07-27-agent-latency-observability.md` 的 `## Task 2: trace facade 中文标签 + group 字段` 一节(第 400-522 行),以及顶部 `## Global Constraints`(第 11-30 行)。

## 1. 做了什么

文件:`services/control-plane/src/control_plane/api/trace_facade.py`

1. **`TraceSpan` dataclass**(原 `:45` 附近)追加 `group: str | None = None` 字段,放在 `purpose` 之后,带默认值 —— 按需求"现有构造点不需要每处都改"。
2. **`_SPAN_LABELS`**(紧跟 `_LLM_LABELS` 之后)8 个 key 逐字照抄需求代码块,与 `TRACED_SPANS` 8 个成员一一对应。
3. **`_classify`** 签名从 `tuple[str, str]` 改成 `tuple[str, str, str | None]`,逐字照抄需求代码块(GENERATION/tool_call/session.run 三支 group 均为 `None`,命中 `_SPAN_LABELS` 的 span 返回 `"entry"`,否则回落 `_clean_label` + `None`)。
4. **数据流管线补全(需求 Step 列表没写全,但不做完 group 到不了前端)**:
   - `_ParsedObs` 新增 `group: str | None` 字段(无默认值,唯一构造点是关键字传参)。
   - `_parse_observation` 里 `kind, label = _classify(...)` 改成 `kind, label, group = _classify(...)`,并把 `group=group` 传进 `_ParsedObs(...)`。
   - `normalize_trace` 里唯一的 `TraceSpan(...)` 构造点加 `group=parsed.group`(不需要 override:LLM wrapper 合并逻辑只处理 `LLM_SPAN_PURPOSES` 里的 span,和 entry 链的 `TRACED_SPANS` 名字空间不相交,所以合并/省略路径不会碰到 group)。
   - `_span_as_dict`(HTTP 响应的唯一序列化出口,`runs.py:1222` 直接 `JSONResponse(content=fetch_and_normalize(...))`)加 `"group": span.group,`。这一步不在需求给的行号范围(`45-66, 300-410`)内,但没有它 `group` 永远到不了前端 JSON —— Task 6 的 `apps/admin-ui/src/api/trace_facade.ts` 依赖后端 JSON 已经带 `group` 键,不补这行整个 task 名存实亡。

文件:`services/control-plane/tests/test_trace_facade_normalize.py`

- 追加 `test_span_labels_cover_every_traced_span`(parity,逐字照抄需求)。
- 追加 `test_entry_chain_spans_carry_the_entry_group` —— **需求给的示例代码用了本文件不存在的 `_normalize`/`_fake_observations` helper 和属性访问(`s.id`/`s.group`)**,已改用文件顶部真实存在的 `_obs`/`_trace` + `normalize_trace()`(返回 dict,camelCase/驼峰 key,用 `s["group"]` 取值),断言内容(recall span 带 `group="entry"` 且 label 是"记忆召回",tool_call span 的 group 是 `None`)与需求一致。

## 2. `_classify` 调用点排查

```
grep -n "_classify(" services/control-plane/src/control_plane/api/trace_facade.py
296:    kind, label, group = _classify(obs_type, name)
427:def _classify(obs_type: str, name: str) -> tuple[str, str, str | None]:
```

全库 grep(`services/` `packages/` `apps/`)只多出一个同名但无关的 `services/control-plane/src/control_plane/curation_worker.py:_classify`(不同模块、不同签名,处理评价打分,与 trace facade 无关,未碰)。

**结论:trace_facade.py 里 `_classify(` 只有 1 个真实调用点(`_parse_observation` 内),已改。** `TraceSpan(` 构造点 1 处、`_ParsedObs(` 构造点 1 处,均已同步更新。

## 3. 测试与校验(全部同步跑,未后台化)

```bash
uv run pytest services/control-plane/tests/test_trace_facade_normalize.py -v
# 34 passed(32 既有 + 2 新增),无回归

uv run pytest services/control-plane/tests/ -k "trace" -v
# 60 passed, 2 failed — 2 个失败是 test_eval_engine_live.py 的
# ModuleNotFoundError: No module named 'tools',Global Constraints 里
# 明确列为"已知测试噪音(非回归,单独跑是绿的)",与本次改动无关。

uv run ruff check
# 第一轮报 I001 import 顺序(新测试里 control_plane/expert_work 两个 import
# 顺序反了),手动改成字母序后 All checks passed!

uv run ruff format --check
# 1473 files already formatted —— 独立跑通,没有触发"加 with 块导致折叠"那类坑
# (本次改动没有新增 with 块 / 缩进层级变化,只是加字段和加 tuple 元素)
```

额外自愿跑(非 CI 强制,CI mypy 范围不含 control-plane):
```bash
uv run mypy services/control-plane/src/control_plane/api/trace_facade.py
# Success: no issues found in 1 source file
```

Step 7 要求的收尾校验:
```bash
uv run pytest -v -m "not integration" -k "trace_facade"
# 56 passed, 6709 deselected
```

## 4. 对哪里不确定 / 顾虑

1. **`_span_as_dict` 的改动超出了需求给的行号范围**(`45-66, 300-410`),但如第 1 节所述,不改这行 `group` 字段就是死数据 —— 后端算出来了,HTTP JSON 里却没有,Task 6 前端拿到的永远是 `undefined`。判断这是需求行号标注的疏漏而非有意排除,已经改了并在报告里明写,方便复核者否决。
2. **Step 1 测试代码块里的 `_normalize`/`_fake_observations` helper 在本文件不存在**,按任务说明"用该测试文件既有的构造 helper(读文件顶部照抄)"改用了 `_obs`/`_trace`/`normalize_trace`,断言的语义(recall 带 entry group + 正确 label,tool_call 不带)与需求逐字一致,只是取值方式从属性访问换成 dict key 访问。
3. `_ParsedObs` 新增字段放在 `purpose` 之后(而不是紧跟 `label`),纯粹是为了和 `TraceSpan` dataclass 里 `group` 紧跟 `purpose` 的位置保持视觉一致,无功能影响。
4. 未触碰 Task 6(前端 `TraceSpan` 类型、`entry_breakdown.ts`、`TraceView.tsx`)——按分工不越界。

## 5. Commit

（见下方回复中的 commit sha）
