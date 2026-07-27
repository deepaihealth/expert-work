# Task 3: first_output 指标 —— 报告

## 做了什么

1. **新增测试** `services/orchestrator/tests/test_first_output_metric.py`(4 个测试,brief Step 1 给的是带 `...` 的骨架,自己补了真正的 fixture):
   - `_TokenGraph`:`astream()` 内部从 `config["configurable"][TOKEN_SINK_KEY]` 取出 `run_agent` 真实装配的 `_publish_token` 闭包并调用它,验证的是生产代码路径,不是测试替身。
   - `_NodeOnlyGraph`:只发 `updates` 节点帧,不碰 token sink;带 `chunk_delay_s` 可在两帧之间插入真实延迟。
   - `test_token_frame_records_source_token` / `test_agent_updates_frame_records_source_node_when_no_tokens` / `test_records_at_most_once_per_run`:按 brief 骨架实现。
   - `test_recall_chunk_does_not_count_as_first_output`(命门测试):不满足于只断言计数 +1(一个记 recall、一个记 agent 的错误实现计数看起来完全一样,无法证伪)。改用 `memory_recall` → `agent` 两帧之间插入 0.05s 真实延迟,断言 `_first_output_seconds_sum{source="node"}` 的增量 `>= 0.075s`(1.5× 延迟,留调度抖动余量)。如果实现退化成"认任意第一帧",这个断言会因为观测值接近 0.05s(只经过一次延迟)而非 0.1s(两次延迟)失败。

2. **`sse.py:113` 附近**:老的 `_session_ttft_seconds` 改名为 `_session_first_node_seconds`(metric 名 `expert_work_session_ttft_seconds` → `expert_work_session_first_node_seconds`),按 brief 给的注释说明改名原因。新增 `_first_output_seconds`(`expert_work_first_output_seconds{source}`)。

3. **`_publish_token`(原 `:379`,现约 `:397`)**:新增闭包外部变量 `first_output_recorded = False`(声明在 `_publish_token` 定义之前),函数体内 `nonlocal` 改写 + 首帧打点 `source="token"`。

4. **node 路径(原 `:470`,现约 `:494`)**:`_session_ttft_seconds.observe(ttft)` → `_session_first_node_seconds.observe(ttft)`(只有一处这样的调用,不是 brief 说的两处 —— brief 提到的 `:474` 那行实际是 `_durable_resume_seconds.observe(ttft)`,不同指标,没有动)。`jsonable_chunk = _to_jsonable(chunk)` 之后插入:仅当 `not first_output_recorded and "agent" in jsonable_chunk` 才打 `source="node"`。

5. **`test_sse.py`**(brief 范围外,但被迫改):`test_run_agent_observes_session_ttft_histogram` 直接 `from orchestrator.sse import _session_ttft_seconds`,改名后会 `ImportError`。改成引用 `_session_first_node_seconds`,函数名同步改为 `test_run_agent_observes_session_first_node_histogram`,docstring 去掉过时的 "TTFT" 措辞。这是我自己的改名唯一破坏的既有引用(全仓 grep 确认),不是碰"别的一律不碰"里指的并行任务文件。

## 关于 Step 5 那个"实施顺序陷阱"的处理(与你给的提示不同的地方)

Brief 原文说"把 `:437` 的赋值提到 `:379` 之前，紧跟 `event_seq` 初始化"。我**没有**照字面搬动 `ttft_started = time.monotonic()` 这一行,理由:

- `event_seq` 初始化(`event_seq = 0`)在 `try:` 块之外,发生在 `await run_manager.set_status(run_id, RunStatus.RUNNING)` **之前**。真把 `time.monotonic()` 搬到那里,基准点会退回到比 RUNNING 还早、比 metadata 帧发布还早的时刻 —— 正是你提醒我要避开的坑。
- 现有的行内注释明确写着计时器要在 metadata 帧发布**之后**起表(`"The metadata frame above is server-synthesised, not LLM output, so we measure from this point"`),搬早了会静默改变指标语义,把 metadata 发布的开销也计进去。
- brief Step 4 给出的代码块本身也没有真的搬动这一行 —— 它只是在 `_publish_token` 里新增了对 `ttft_started` 的读取(闭包在**调用时**才读,原有位置已经够用),真正新增初始化的只有 `first_output_recorded`。

结论:`ttft_started = time.monotonic()` 保持在原来的相对位置不变(紧跟 metadata 发布之后、`while True` 循环之前)。`_publish_token` 通过 Python 闭包晚绑定读到它,不需要挪动,也不需要 `nonlocal ttft_started`(只读不写)。这是一处主动偏离 brief 字面指令的决定,如果这个判断有误请指出。

## 测试怎么跑的、输出

```
uv run pytest services/orchestrator/tests/test_first_output_metric.py -v
# 改动前(未定义指标):4 failed —— get_sample_value 返回 None → 计数恒为 0
# 改动后:4 passed in 0.69s

uv run pytest -v -m "not integration" -k "sse or run_agent or first_output"
# 304 passed, 6454 deselected

uv run pytest services/orchestrator/tests -m "not integration" -q
# 1848 passed, 1 skipped(缺 docx 模块,与本改动无关), 1 deselected

uv run ruff check
# All checks passed!

uv run ruff format --check <改动的3个文件>
# sse.py 需要 reformat(我新增的一行长度触发换行规则),已跑 `ruff format` 修复,复跑 --check 全绿

uv run mypy packages services/orchestrator/src
# Success: no issues found in 762 source files
```

## 发现但没改的东西

`grep -rl "session_ttft"` 全仓命中以下文件(均在 `services/orchestrator/src/orchestrator/sse.py` 之外,按任务范围没有碰):

- `infra/observability/prometheus.yml`
- `tools/observability/dashboards/02-orchestrator.json`
- `tools/observability/dashboards/01-overview.json`
- `tools/observability/rules/sli.yml`
- `tools/observability/rules/alerts.yml`
- `docs/ITERATION-PLAN.md`
- `docs/streams/STREAM-K-DESIGN.md`
- `docs/streams/STREAM-M-DESIGN.md`
- `docs/runbooks/slo.md`
- `docs/streams/STREAM-P-DESIGN.md`
- `docs/runbooks/m0-m1-gate.md`
- `docs/runbooks/canonical-agent-e2e-test.md`
- `docs/architecture/subsystems/20-observability.md`

这些引用的都是老 metric 名 `expert_work_session_ttft_seconds`。改名后,这些 Prometheus 查询 / Grafana 面板 / 告警规则 / SLO 文档全部会读到一个不再产出数据的 series(该 metric 名不再被 emit),需要后续单独一批任务同步(不在本 task 范围,brief 也没要求动这些文件)。**这是本次改动最大的运维影响,值得单独提醒。**

## 不确定的地方

1. 上面"Step 5 陷阱"那节的偏离决定 —— 我判断字面照搬会破坏计时语义,选择了保留原位置。逻辑上我认为是对的(4 个新测试 + 全量 sse 测试都绿,且 mypy 干净),但这确实不是 brief 字面写的做法,标出来供复核。
2. `test_recall_chunk_does_not_count_as_first_output` 用了 0.05s 延迟 + 1.5× 阈值做时序断言,理论上有极小概率在极端调度抖动下 flaky(和仓库里已有的 `chunk_delay_s` 模式一致,但比单纯计数断言多了一点不确定性)。如果不想要这点时序脆弱性,可以退化成纯计数断言,但那样就测不出"命门"要防的那类 bug 了 —— 保留了时序版本。
3. 上面列出的 dashboards/alerts/docs 那批文件是否要在本 task 顺手改掉,还是留给后续批次,没有定论,按"只改 sse.py + 自己的新测试文件"的范围要求没有动。

## 未涉及范围

按要求只改了 `services/orchestrator/src/orchestrator/sse.py`、新增的 `services/orchestrator/tests/test_first_output_metric.py`,以及因改名被迫同步的 `services/orchestrator/tests/test_sse.py` 一处引用。没有碰 `graph_builder/memory.py`、`workspace_ingest.py`、`builder.py`、`control-plane/runtime.py`(并行任务的文件)。

---

## 追加:告警 / SLI / 面板 / docs 连带修复(commit `83ade7d2`)

上一节"发现但没改的东西"列的 13 个文件里,coordinator 复核后判定其中 3 个不能留 follow-up:

- `tools/observability/rules/alerts.yml:85` 的 `ExpertWorkGateTTFTOverBudget` 引用了改名后不再产出的 `expert_work_session_ttft_seconds_bucket` —— `histogram_quantile` 对空 series 返回 NaN,`NaN > 2.0` 恒为 false,告警静默失效,不报错也不触发。
- `tools/observability/rules/sli.yml:34` 的 recording rule `expert_work:sli:session_ttft:p95_5m`(SLO 3)同样断供。

已按 coordinator 给的具体方案补了一个 commit,改了 9 个文件:

1. **`tools/observability/rules/alerts.yml`**:`ExpertWorkGateTTFTOverBudget` → `ExpertWorkGateFirstNodeOverBudget`,指标名同步改成 `expert_work_session_first_node_seconds_bucket`。**阈值 2.0s 不动**(测的还是同一个东西,只是名字诚实了)。summary/description 里的 "Session TTFT" → "First graph-node"。`runbook_url` 未动。

2. **`tools/observability/rules/sli.yml`**:record 名 `expert_work:sli:session_ttft:p95_5m` → `expert_work:sli:session_first_node:p95_5m`,指标名同步改。原注释说"observed at the first agent `updates` chunk"是假话(实现认的是任意第一个节点,不特指 agent)——已改成准确描述,并点出这正是本 task 在修的口径 bug,顺带指向新增的 `first_output` SLI。

3. **同一文件新增**:`expert_work:sli:first_output:p95_5m`(逐字用 coordinator 给的 expr/注释),只做记录不配告警——阈值等生产数据攒够再定,老的 first_node 告警继续兜底。

4. **`infra/observability/prometheus.yml:26`**:注释里的指标名列举同步改。

5. **docs(只改活文档,按 coordinator 名单)**:
   - `docs/runbooks/slo.md`:SLO 表第 3 行 "Session TTFT P95" → "Session 首个图节点 P95",标注前名。
   - `docs/runbooks/m0-m1-gate.md`:daily-check 的 curl 命令 + 阈值表 + NO-GO 选项里的 "TTFT" 三处,统一加"(前名 TTFT)"并换成新指标名。
   - `docs/architecture/subsystems/20-observability.md`:职责列表的 "TTFT" 措辞、`slo_definition` 示例 SQL 的注释值、metric 清单表(顺带把"RUNNING 到首个 **agent** chunk"这句本身就不准确的描述改成"任意节点,非特指 agent")、SLO 表,共 4 处。
   - `docs/runbooks/canonical-agent-e2e-test.md`:看了内容,是操作手册(Phase 6 手工冒烟步骤,里面的 curl 命令是要人真的跑的),改了里面的 curl query。
   - `docs/streams/STREAM-*.md`、`docs/ITERATION-PLAN.md`:按 coordinator 指示**没有动**——历史设计文档,保留当时决定的记录。

6. **主动追加,coordinator 未点名但确认"改是对的"**:`tools/observability/dashboards/01-overview.json`、`02-orchestrator.json` 两个 Grafana 面板的 `expr`(`expert_work:sli:session_ttft:p95_5m` → `...session_first_node:p95_5m`)和 `title`("Session TTFT P95" → "Session first-node P95")。判断依据:这两个面板直接引用的是我在 sli.yml 里改名的那条 recording rule,不改的话面板会静默变空,和 alerts.yml/sli.yml 是同一类"改名连带断供"问题,不是新的语义判断,只是把已经决定的 rename 传播到最后一个消费者。

### promtool 校验

`promtool` 本地不存在(`command -v promtool` exit 1,它是 Prometheus 发行包自带的二进制,本机未装,也没有走 Docker 拉镜像 —— 第一次尝试用 `docker run prom/prometheus promtool check rules` 起了后台任务等待,被判定为踩了"起后台任务+轮询"的坑,已改为同步命令)。按 coordinator 的兜底方案,只跑了 YAML 语法校验:

```
python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]; print('YAML OK:', sys.argv[1:])" tools/observability/rules/alerts.yml tools/observability/rules/sli.yml
# YAML OK: ['tools/observability/rules/alerts.yml', 'tools/observability/rules/sli.yml']
```

两个 dashboard JSON 也单独过了 `json.load()` 语法校验(见下)。PromQL expr 的语义正确性(指标名有没有指对)没有工具能自动查,coordinator 说会自己 review expr,这里不重复背书。

```
python3 -c "
import json
for f in ['tools/observability/dashboards/01-overview.json', 'tools/observability/dashboards/02-orchestrator.json']:
    with open(f) as fh: json.load(fh)
    print(f, 'valid JSON')
"
# 两个文件都 valid JSON
```

### 全仓复核

```
grep -rln "session_ttft" .
# tools/observability/rules/sli.yml         — 新注释里提"改名前叫 session_ttft"的历史说明,预期
# docs/ITERATION-PLAN.md                    — coordinator 指示不动
# docs/streams/STREAM-M-DESIGN.md           — 同上
# docs/streams/STREAM-K-DESIGN.md           — 同上
# docs/streams/STREAM-P-DESIGN.md           — 同上
# .superpowers/sdd/perf-task-3-report.md    — 本报告自身的记录文字
```

无遗漏的功能性引用。

### commit

`83ade7d2` — `fix(observability): 老 ttft 指标改名的告警/SLI/面板连带修复`(9 files changed, 42 insertions, 26 deletions),父提交 `353316df`。
