# Task 4: bench 脚本 + 单测 —— 报告

Scope: 计划文档 Task 4 的 Step 1-4 + Step 6。**Step 5(起真栈跑第一次基线)按范围调整跳过**——起全栈需要 docker compose + Keycloak + 配好 LLM 凭据的测试 agent，会跟主仓 compose 抢端口；协调者自己跑这一步。交付物是脚本 + 单测，不是基线数据。

commit: `aa574310`（父提交 `39e0cccb`，`git merge --ff-only perf-latency-observability` 落到 `d81e81d2..39e0cccb`）

## 做了什么

1. **`tools/bench/conftest.py`**：逐字照抄 `tools/eval/conftest.py` 的 sys.path shim（把 `tools/bench` 塞进 `sys.path`，因为 `tools/` 不是包）。
2. **`tools/bench/test_entry_latency.py`**：Step 1 给的两个 `aggregate()` 测试逐字照抄（median/p95 命门 + 缺席段不当 0 算的命门），另加 3 个我自己写的 `extract_run_metrics()` 测试（见下）。共 5 个测试，全绿。
3. **`tools/bench/entry_latency.py`**：`Segment`/`aggregate()` 逐字照抄 Step 3 给的代码（**没有改一个字**——两个命门测试的断言直接依赖这个实现的精确行为）。取数 + CLI 部分是我按需求自己设计的（见下"实现假设"）。
4. **`tools/bench/baselines/README.md`**：占位说明（无假数据），命名约定 `<date>-<label>.yaml`。
5. **`tools/bench/README.md`**：Files 表列出的顶层 README，运行前置条件 + 用法。
6. **Step 6 commit**：`aa574310`（commit message 没有照抄计划文档里"+ 改造前基线"的字面文案——因为 Step 5 跳过了，没有基线产出，写"+ 单测"更准确）。

### `extract_run_metrics()`（我新增的纯函数，非计划文档字面给出）

计划文档 Step 3 只写了 `aggregate()`；"取数"部分的实现（怎么从 trace facade 的 JSON 里抽出每段耗时）需要我自己设计。签名：

```python
def extract_run_metrics(trace: dict[str, Any]) -> dict[str, float]
```

输入是 `GET /v1/sessions/{tid}/runs/{rid}/trace` 返回的 JSON（`status == "ok"` 时）。做两件事：
- **segments**：遍历 `spans`，取 `group == "entry"` 的 span，用 `label` 做 key、`latencyMs` 做值。
- **`first_output`**（内部 key `FIRST_OUTPUT_KEY`，写 YAML 前从聚合结果里 pop 出来单独成段）：`kind == "llm"` 的 span 里最早的 `startMs`，作为"入口链走完、开始生成"的近似值。

3 个新测试覆盖：entry-group 过滤不漏出 LLM span（命门：LLM 的 `group` 是 `None`，不能混进 segments）、first_output 取最早而非任意一个 LLM span、没有 LLM span 时不编造 0。

## 两个命门的落实

1. **`aggregate()` 跳过缺席段**：逐字用计划给的实现（`values = sorted(run[name] for run in runs if name in run)`），没有改。`test_aggregate_tolerates_a_segment_missing_from_some_runs` 断言 `记忆重排` 只按出现过的 2 轮算中位数（200.0）且 `n == 2`，不是 3 轮里把缺席当 0 拉低。

2. **`meta` 段带 commit + host + agent + runs**：见 `_amain()` 里的 `meta` dict，四个字段都是必填（`commit`/`host` 自动算出，`agent`/`runs` 来自 CLI 参数）。`note` 可选（`--note`）。

   **一个设计偏离，需要你确认**：计划文档说"照 `tools/eval/baselines/` 里已有文件的 `meta.fingerprints` 形状写"，但读了 `tools/eval/baselines/longmem_baseline.yaml` 后发现 `meta.fingerprints` 是 `{tier}/{benchmark}: {fingerprint dict}` 的嵌套结构——那是因为 `longmem_baseline.yaml` 是一个**持续累积**的文件（`update_baseline()` 每次跑一个 tier/benchmark 组合就往里合并一段，同一个文件装了 locomo/longmemeval 好几个基准的历史)。我们的 bench 脚本每次调用只产出**一份单一快照**（一个 agent、一个 prompt、N 轮），不是累积多基准文件，所以嵌套 `fingerprints: {key: {...}}` 没有实际收益——我改用了计划文档 Step 3 自己给出的**扁平 `meta:`** 形状（`commit`/`host`/`agent`/`runs`/`note`），这本身也满足两个命门要求的字段。如果你的本意就是要那种可累积、多 key 嵌套的 `fingerprints` 结构（比如以后一个文件要装多个 agent 的基线），这里需要改，成本不高（加一层 `meta.setdefault("fingerprints", {})[f"{agent}/{note or 'run'}"] = {...}`）。

## 需要真栈验证的假设（实现取数部分时做的）

我没有能跑的真栈，下面这些假设都是读代码推出来的，**没有实际打过真请求验证**：

1. **会话创建**：`POST /v1/sessions`，body `{"agent_name": ..., "agent_version": ...}`，响应 `{"success": true, "data": {"thread_id": ..., ...}}`（`ThreadMeta.model_dump()`）。这个我有较高信心——照抄了 `tools/eval/verify_live.py::_create_session` 的逐字实现，那个脚本大概率被真跑过。

2. **触发一轮 run**：`POST /v1/sessions/{thread_id}/runs`，body `{"input": <prompt>}`（用默认 `mode: "stream"`，不是 `mode: "queue"`——queue 模式返回 202 后要靠某个实例上的 `RunQueueWorker` 异步执行，我不确定 dev stack 默认起没起这个 worker，stream 模式更确定能跑）。响应是 SSE（`text/event-stream`），脚本把它整个 drain 完（不解析内容，只等流关闭 = run 跑完）。**假设**：响应 headers 里有 `X-Expert-Work-Run-Id`（`runs.py:907` 附近确认这个 header 存在，且在 SSE body 开始前就能读到——httpx 的 `client.stream()` 上下文管理器进入时 headers 已到）。这个我**没有**照抄 verify_live.py（它不需要 run_id，只解析消息内容），是我自己加的，风险相对更高，建议你第一次跑的时候重点看这里会不会拿到 header。

3. **trace 拉取**：`GET /v1/sessions/{thread_id}/runs/{run_id}/trace`，响应 `{"status": "ok"|"not_ready"|"no_trace"|"unavailable", "trace": {...}, "spans": [...]}`（读的是 `trace_facade.py::fetch_and_normalize` 的返回，比较确定，因为你直接指了这个文件）。**假设**：Langfuse 异步落盘会导致 `status == "not_ready"`，脚本按 1 秒间隔轮询到 `--trace-timeout-s`（默认 30s）超时。这个轮询窗口是我拍的，没有真实数据支撑——如果真栈上 Langfuse 落盘经常超过 30s，第一次跑多半会报 `trace never became ready`，调大这个 flag 即可，不是脚本逻辑错。

4. **span 字段**：`group`/`label`/`latencyMs`/`kind`/`startMs` 字段名和取值——这个抄的是 `trace_facade.py::_span_as_dict` 的字面 camelCase key 名，加上你在任务里给的 `group == "entry"` 契约，信心较高。

5. **鉴权**：`Authorization: Bearer <EXPERT_WORK_API_TOKEN>`，跟 `verify_live.py` 一样的 env var 约定（`EXPERT_WORK_API_URL` + `EXPERT_WORK_API_TOKEN`）。

## 协调者怎么跑这个脚本

**前置条件**（脚本本身不做任何环境探测，缺了会直接失败）：
1. 本地全栈已起（docker compose + control-plane + orchestrator + Keycloak + Langfuse），且**没有**跟主仓的 compose 抢端口。
2. 至少一个 tenant 下有一个 ACTIVE 状态、绑了真实模型凭据的 agent（`name@version`）——脚本会真的触发 LLM 调用，不是 mock。
3. 一个有效的 bearer token（走 Keycloak dev 登录拿，参照 `docs/runbooks/canonical-agent-e2e-test.md` 第 101 行那段 `curl .../protocol/openid-connect/token`）。
4. 一个纯文本文件装固定 prompt（脚本不自带默认 prompt——`tools/bench/prompts/` 目录我没建，因为计划文档的 File Structure 表没列它，这属于你要跑基线时自己决定的测试输入）。

**命令**：

```bash
export EXPERT_WORK_API_URL=http://localhost:8000   # 你的 control-plane URL
export EXPERT_WORK_API_TOKEN=<dev-login bearer token>

mkdir -p tools/bench/prompts
echo "帮我总结一下今天的待办事项" > tools/bench/prompts/fixed.txt   # 或你自己的固定 prompt

uv run python tools/bench/entry_latency.py \
    --agent <你的测试 agent>@<version> \
    --prompt-file tools/bench/prompts/fixed.txt \
    --runs 10 \
    --out tools/bench/baselines/$(date +%F)-before.yaml \
    --note "before 连接池改造"
```

可选 flag：`--trace-timeout-s`（默认 30，Langfuse 落盘慢就调大）、`--base-url`（不想 export env var 时用这个覆盖）。

**跑起来后重点看什么**（对应上面"需要真栈验证的假设"）：
- 有没有卡在某一轮报 `run response carried no X-Expert-Work-Run-Id header`（假设 2 错了）。
- 有没有大量 `trace never became ready`（假设 3 的超时窗口太短，或者假设 2/3 的整个链路有问题）。
- 输出 YAML 里 `segments` 是不是 8 个入口链段都出现了、`n` 是不是等于 `--runs`（如果某段的 `n` 系统性小于 runs 数但那个 agent 明明配了对应功能，说明 `group == "entry"` 过滤或 label 映射跟这次真实 span 数据对不上）。
- **spec §7 的风险项**（计划文档 Step 5 提到的）：脚本本身不测这个，但你跑的时候顺手对比一下"开 span 前后"的 `first_output` median——如果 span 本身让首字慢了 > 50ms，回头砍掉 `resolve_mode` 这类毫秒级的段（这是 Task 1 的活，不是本脚本能改的）。

脚本对单轮失败是容错的（`httpx.HTTPError` / `RuntimeError` 会被捕获、打印警告、跳过该轮，不会整批失败）——`aggregate()` 的"缺席跳过"语义天然兼容这种部分失败。

## 验证结果

```
uv run pytest tools/bench/test_entry_latency.py -v
# 5 passed in 0.02s

uv run ruff check
# All checks passed!（无路径参数，全库含 tests）

uv run ruff format --check
# 1476 files already formatted
```

三条命令均绿。中途 `ruff format --check` 第一次跑时对新增的两个文件报了需要 reformat（模块 docstring 后要空一行——我最初照抄计划文档给的代码块字面没有这行空行），跑 `ruff format` 修完后复跑三条命令全绿。另外 `test_entry_latency.py` 里计划给的 `# noqa: E402` 被 `ruff check` 的 `RUF100` 判定为多余（因为用的是 conftest.py shim 而不是文件内联 `sys.path.insert`，没有真正的 E402 触发条件）——删掉了这个注释。

`tools/` 不在 CI 的 mypy 范围内，没跑 mypy，但按要求全函数写了类型注解（含 `from __future__ import annotations`）。

## 未做的事 / 已知限制

- **没有真栈验证**（按你的范围调整，Step 5 跳过）——上面列的 5 条假设都需要你第一次跑的时候核实。
- **没有创建 `tools/bench/prompts/fixed.txt`**——计划文档的 File Structure 表没列这个文件，固定 prompt 内容是"跑基线"这个动作自己的输入决定，不是脚本实现的一部分，留给你按测试 agent 的实际能力挑一个合适的 prompt。
- **`meta` 用扁平结构而非 `fingerprints` 嵌套**——见上面"两个命门的落实"第 2 条，需要你确认这个设计选择。
- **`first_output` 是近似值**——只从 trace facade 的 span 数据反推（最早 LLM span 的 startMs），不是 Task 3 新增的 `first_output_seconds` Prometheus 直方图本身（脚本没有查 Prometheus，只查 trace facade——你在任务描述里指的参照端点也只有这一个）。如果这个近似跟真实 `first_output_seconds` 数值对不上，需要另外决定要不要让脚本改查 Prometheus。
