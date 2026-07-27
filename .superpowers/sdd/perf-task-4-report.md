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
- **~~`first_output` 是近似值~~**——已按协调者回复改名为 `first_llm_start`，见下方追加小节。

---

## 追加：假设核验结果 + `first_output` → `first_llm_start` 改名

协调者核验了 5 条实现假设：

- **假设 2（`X-Expert-Work-Run-Id` header）—— 成立**。协调者查了 `runs.py:907/1494/1551/1565`，`:1543` 附近的注释明确写着这个 header 在流式和续跑两条路径上都要发，是维持的稳定契约，不是巧合。
- **假设 1（会话创建 envelope）、假设 4（span 字段名）—— 接受**，判断合理。
- **假设 3（trace 轮询 30s 超时是猜的）—— 接受**，`--trace-timeout-s` 已经做成可配置，协调者跑基线时会拿到真实值再回填默认值。
- **`meta` 用 flat 而非 nested `fingerprints` —— 接受**，理由（`longmem_baseline.yaml` 的嵌套是因为文件要累积多个 benchmark，本脚本每次产出一份独立快照）成立。

**假设 5 需要改**：问题不在"用最早 LLM span 的 startMs 做代理指标"这个近似本身（协调者确认一期要量的 TLS 握手节省发生在 LLM 调用内部，被 span latency 完整覆盖，用这个代理做优化前后对比依然有效），而在**命名撞车**——Task 3 的 Prometheus 直方图 `expert_work_first_output_seconds` 测的是"第一个 token 帧到达 / 第一个 agent updates 帧"，跟"最早 LLM span 开始"隔着一段模型 prefill 时间（几百毫秒到几秒）。同名不同义会让二期有人拿这两个数字互相对照，误判成"哪里坏了"。

按协调者给的三步改法执行：

1. **字段改名**：`first_output` → `first_llm_start`（YAML 输出的 top-level key、内部常量 `FIRST_OUTPUT_KEY` → `FIRST_LLM_START_KEY`、内部字符串值 `__first_output_ms__` → `__first_llm_start_ms__`、局部变量名、docstring、测试函数名/文档字符串全部同步）。用 `grep -rn "first_output" tools/bench/` 复核，剩下的两处引用（`entry_latency.py` 里）都是**主动解释"我们不叫这个名字、为什么不叫"**的说明性文字（提到 Task 3 的 `expert_work_first_output_seconds` 作对比），不是本脚本自己在用这个名字，符合协调者"别留半边"的要求。
2. **`tools/bench/README.md` 新增一节** `first_llm_start` is NOT `first_output_seconds`：解释两个指标测的是不同时刻（trace 里没有首字 span，Task 3 的首字打点是 Prometheus-only、无对应 span）、中间隔一段 prefill、脚本内部前后对比有效但不能拿去对 Grafana 数字。
3. **CLI help 检查**：`--help` 输出里本来就没有涉及这个词的 flag（只在 YAML 输出字段和代码文档里出现），无需改。

### 验证结果（改名后）

```
uv run pytest tools/bench/test_entry_latency.py -v
# 5 passed in 0.02s

uv run ruff check
# All checks passed!

uv run ruff format --check
# 1476 files already formatted
```

三条命令同步跑、全绿，没有后台化/轮询。

改名 + README 说明 + 本追加一起提交（commit sha 见 PR/对话回复）。

---

## 追加：代码审查 3 Important + 2 Minor 修复

Spec 判 ✅，代码质量审查提了 3 个 Important + 2 个 Minor，全部成立，已修。

### Important-1：`resp.json()` 解析异常打崩整个脚本，丢光已有数据

根因：`_fetch_trace()` 里 `resp.json()` 在"2xx 但 body 不是合法 JSON"时抛 `json.JSONDecodeError`（`ValueError` 子类），逃出了原来 `except (httpx.HTTPError, RuntimeError)` 的捕获范围；而聚合+落盘原来只在 for 循环跑完之后做一次——跑到第 8 轮崩溃，前 7 轮真实 LLM 调用拿到的数据一个数字都不落盘。

修法（两部分都做了，不是二选一）：

1. **异常捕获收窄补 `ValueError`**：把整个 for 循环体抽成新函数 `run_rounds()`（`entry_latency.py`），`except (httpx.HTTPError, RuntimeError, ValueError) as exc` 覆盖 JSON 解析失败。
2. **增量落盘**：新增 `_write_result(out_path, per_run, meta_base, *, failed_runs)`，每轮跑完都调一次(通过 `run_rounds()` 的 `on_round_done` 回调),不再是"循环跑完才写一次"。每次调用整体重写文件(用当前累积的 `per_run` 重新聚合),所以中途崩溃时磁盘上已经是最新的部分结果,不会丢已经跑过的真实轮次。

新测试 `test_run_rounds_survives_a_malformed_trace_response_body`：用 `httpx.MockTransport` 打桩,3 轮里第 3 轮的 trace GET 返回 `content=b"<html>not json</html>"`(2xx,body 不是 JSON)。断言 `per_run == [{"记忆召回": 100.0}, {"记忆召回": 100.0}, {}]`(前两轮真实数据在,第三轮退化成空字典而不是抛异常)且 `failed_runs == 1`。

### Important-2：全军覆没也产出「看起来合法」的 YAML

根因：`--agent` 拼错版本号→10 轮全 404→`per_run` 全 `{}`→`aggregate()` 合法地返回空字典→脚本照样 `wrote ...` + exit 0。产出的 `segments: {}` 跟"这个 agent 本来就没有入口链 span"长得一模一样，`meta.runs` 记的还是请求轮数不是成功轮数，反而像在担保"这是 10 轮真数据"。

修法：

1. **`meta` 加 `successful_runs` / `failed_runs`**：`_write_result()` 每次调用都从 `len(per_run) - failed_runs` 现算，不再只有一个语义模糊的 `runs`(那个字段保留表示"请求了几轮"，新增两个字段表示"实际成功/失败几轮")。
2. **新增 `_exit_status(successful_runs, total_runs) -> (exit_code, warning)`**：`successful_runs == 0` → exit 1 + stdout 警告(协调者给的具体场景：typo 的 `--agent`);`successful_runs / total_runs < 0.5` → exit 0(部分真实数据仍然有用,呼应之前"单轮失败容错"的设计)但同样打印 stdout 警告。警告用 `print()` 默认写 stdout,不是只写 stderr(协调者原话"打印警告到 stdout(不只 stderr)")。

新测试两个：`test_write_result_distinguishes_zero_success_from_zero_spans`(3 轮全失败时 `meta.successful_runs == 0`、`meta.failed_runs == 3`、`meta.runs` 仍是请求的 3 不变)+ `test_exit_status_all_failed_returns_nonzero_and_warns`(`_exit_status(0, 10)` 返回非零 code + 带"10"的警告文案)。另加一个补充测试 `test_exit_status_full_success_is_clean` 确认正常情况不误报。

### Important-3：prompt 没进 meta

根因：协调者审查指令里列的"机器/agent/prompt/runs 数"四轴，前三个(commit/host/agent/runs)都进了 meta，prompt 完全没记。`-before.yaml` 和 `-after.yaml` 如果指向了内容不同的 prompt 文件，两份 meta 长得一模一样，prompt 长度差异导致的延迟差会被误判成"连接池改造的效果"——这条最致命，直接破坏这个脚本存在的意义。

修法：`meta` 新增两个字段——`prompt_file`(`--prompt-file` 的原始路径字符串)+ `prompt_sha256`(新增纯函数 `_prompt_fingerprint()`，`hashlib.sha256(prompt.encode()).hexdigest()[:12]`，同 `_git_commit()` 的短哈希风格)。哈希覆盖"路径相同但内容被编辑过"这种路径本身查不出来的情况。

新测试 `test_prompt_fingerprint_changes_with_content`：两段不同中文 prompt 哈希不同，同一段内容哈希确定性一致。（协调者原话只点名 Important-1、-2 各补一个测试，Important-3 的测试是我主动加的——新增的纯函数照项目"新函数要有单测"的惯例补了一个，成本很低。）

### Minor-4：mypy strict 类型缺口

`_run_once()` 里 `run_id = resp.headers.get(...)` 之前没有显式类型标注，httpx 的 `Headers.get()` stub 返回 `Any`，`if not run_id: raise` 的收窄对 `Any` 不生效，`return run_id` 触发 mypy strict 的 "Returning Any from function declared to return str"。改成 `run_id: str | None = resp.headers.get(...)`。验证：`uv run mypy tools/bench/entry_latency.py --strict` → `Success: no issues found in 1 source file`(`tools/` 不在 CI mypy 范围内，这条是额外验证，不是三条硬性命令之一)。

### Minor-5：README 补语义边界

`tools/bench/README.md` 的 `first_llm_start` is NOT `first_output_seconds` 一节加了一段：`first_llm_start` 取的是所有 `kind=="llm"` span 里最早的一个，不特指主生成调用——配了 query-rewrite 之类辅助 LLM 调用的 agent(发生在 `memory.recall` 内部，必然早于主生成)，这个代理指标拿到的其实是辅助调用的启动时刻。对连接池改造的 before/after 有效性没有影响(TLS 复用对所有出站 LLM 调用生效)，但跨 agent 比较"到主生成开始"的延迟时会误导。`extract_run_metrics()` 的 docstring 同步加了这句。

### 验证结果

```
uv run pytest tools/bench/test_entry_latency.py -v
# 10 passed in 0.02s(新增 5 个:run_rounds 容错 1 个、Important-2 相关 3 个、prompt 哈希 1 个)

uv run ruff check
# All checks passed!

uv run ruff format --check
# 1476 files already formatted
```

三条命令同步跑、全绿，没有后台化/轮询。中途 `ruff format --check` 对 `test_entry_latency.py` 报了一行超长(`run_rounds(client, "thread-1", "hello", 3, trace_timeout_s=5.0)` 那行)，跑 `ruff format` 折行后复跑全绿。

### 未变的部分

上面追加小节确认过的 4 条假设(1/2/3/4)、扁平 `meta` 设计、Step 5 跳过范围、`first_llm_start` 改名——这次审查没有再动，原文保留在上面两节。
