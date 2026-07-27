# Task 5: 连接池复用（8 处）—— 报告

状态：完成。8 处 HTTP 调用点（`openai.py` chat+stream / `anthropic.py` messages+stream / `embedder.py` / `rerank.py` / `web_search.py` / `sandbox.py`）全部接入共享 `_client_for` 助手；生产侧从 control-plane lifespan 到 6 个 client 类的注入链路全通（详见下方"接线深度"）。

commit: 待创建（本报告写完后连同代码一起提交，见文末）。

## 六条验证命令结论

1. `uv run pytest services/orchestrator/tests/test_http_client_reuse.py -v` —— **4 passed**。两个命门（共享 client 不关闭 / 流式 `read=None` per-request 语义）各有专门断言，均通过。
2. `uv run pytest services/orchestrator/tests/ -v -k "provider or llm or embedder or rerank or web_search or sandbox"` —— **357 passed, 1502 deselected**。受影响面（provider/embedder/rerank/web_search/sandbox 全部测试文件）零回归，包括 `test_agent_factory.py` 里的 `_build_provider`/self_hosted/azure/compat 分支测试。
3. `uv run pytest services/control-plane/tests/ -k "runtime or embedder or rerank" -v` —— **92 passed, 2056 deselected**。`ResolvingEmbedder`/`ResolvingReranker`/`DynamicResolvingEmbedder`/`DynamicResolvingReranker`/`make_agent_builder`/`subagent_runtime` 相关测试零回归。
4. `uv run ruff check`（全库无路径参数）—— **All checks passed!**
5. `uv run ruff format --check`（全库，独立步骤）—— **1478 files already formatted**。
6. `uv run mypy packages services/orchestrator/src` —— **Success: no issues found in 763 source files**。（另外我也跑了 Global Constraints 列的更宽范围 `packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src`，784 个源文件同样全绿。）

六条命令逐条同步跑、无后台化/轮询。

## 做了什么

### 共享助手（新建）

`services/orchestrator/src/orchestrator/llm/providers/_http.py`：`client_for(shared, *, timeout, transport)` 一个 `@asynccontextmanager`。`shared is not None` → `yield shared`（**不 close**）；否则 `async with httpx.AsyncClient(timeout=timeout, transport=transport)`（逐字节等价改造前行为）。命名上没照抄计划文档给的 `_client_for`（前导下划线）—— 这个新模块本身就叫 `_http.py`（跟同目录 `_errors.py`/`_metrics.py`/`_streaming.py` 一个命名法：模块名前导下划线表示"包内私有"，模块内导出符号不必再加一层下划线），所以函数名是 `client_for`。六个 client 类全部 `from orchestrator.llm.providers._http import client_for`，没有一份是抄出来的独立实现。

### 6 个 client 类逐个改造

- `HTTPOpenAIClient`（`openai.py`）：`chat_completions` + `stream_chat_completions` 两条路径都换成 `client_for(self.http, ...)`，**且都在 `.post()`/`.stream()` 调用上补了 per-request `timeout=`**（流式那条本来就该传 `read=None` 的 `Timeout` 对象；非流式那条原本靠 client 构造时的 `timeout=` 默认值,现在共享 client 分支没有这个默认——lifespan 建的共享 client 不传 `timeout=`,httpx 会退到内置 5 秒默认,所以两条路径都必须显式 per-request 传，不只是流式那条）。
- `HTTPAnthropicClient`（`anthropic.py`）：同上，`messages` + `stream_messages` 两条路径。
- `HTTPEmbeddingClient`（`embedder.py`，`frozen=True`）：加 `http` 字段（frozen 不影响新增带默认值的末位字段）。
- `HTTPDashScopeRerankClient`（`rerank.py`，`frozen=True`）：同上。
- `SearXNGClient`（`web_search.py`，plan 文档里叫 `TavilyClient` 是 Protocol 名，真正的生产实现是这个类）：同上。
- `HTTPSupervisorClient`（`sandbox.py`）：这处形状最特殊，单独说明见下。

### `sandbox.py` 的特殊处理

原 `_make_client(self) -> httpx.AsyncClient` 是个"工厂方法"，5 处调用点都写成 `async with self._make_client() as client:`。如果直接把 `_make_client()` 改成"命中共享分支时返回 `self.http`"，这 5 处 `async with` 会在退出时对**共享 client 本身**调 `__aexit__`（= `aclose()`）—— 正是命门 1 要防的坑，而且这种改法連静态检查都看不出来。

实际做法：把 `_make_client` 本身变成 `@asynccontextmanager` 方法，内部 `async with client_for(self.http, ...) as client: yield client`。5 处调用点 `async with self._make_client() as client:` **一个字都不用改**——因为「调用一个 async context manager 工厂、再 `async with` 它」这个语法形状本来就没变，变的只是 `_make_client()` 内部怎么决定用哪个 client。`exec()` 那处不走 `_make_client`（它有自己的 `read_timeout`，跟 `self.timeout_s` 不同），单独换成 `client_for(self.http, timeout=read_timeout, transport=self.transport)`。

sandbox.py 一共 6 个 httpx 调用点（`_post` 内的 post、`exec` 的 post、`read/list/write/delete_workspace_file` 的 get/get/put/request），**每一个**都补了 per-request `timeout=`（`_post`/`read`/`list`/`write`/`delete` 用 `self.timeout_s`，`exec` 用它自己算出来的 `read_timeout`）——这条路径最容易漏,因为原代码从来没在调用点显式传过 timeout,全靠 client 构造时的默认值,共享 client 化之后这个隐式默认值就没了。

### `openai_compatible.py` 的 7 个工厂

`make_kimi_client` / `make_glm_client` / `make_deepseek_client` / `make_qwen_client` / `make_doubao_client` / `make_self_hosted_client` / `make_azure_client` 全部加 `http: httpx.AsyncClient | None = None` 参数，透传进各自的 `HTTPOpenAIClient(...)`。

### 接线深度 —— 不止"加字段"，从 lifespan 到 client 构造点全通

Task 5 的 spec 原文说"没接线的路径字段是 None，行为逐字节不变……不需要一次性改完所有调用点"——这句话字面上给了"只加字段、不接生产线"的空间。但我判断：如果 `build_agent` 的 `http_client` kwarg 只停在 `_build_provider` 这一层、没人在真实调用链上传非 None 值，那 8 处里"分量最大"的两处（openai/anthropic 聊天补全，每轮 run 必打的那条）**在生产环境里永远走 fallback 分支，整个 task 对生产延迟零影响**——这跟需求"消掉每次调用的 TLS 握手"直接矛盾，也正是这个仓库记忆里踩过的教训（"opt-in 路径必须真有 opt 入口否则等于死代码"）。所以我把接线做完整了，而不是止步于"字段存在":

- `build_agent`（`agent_factory.py`）新增 `http_client` kwarg → 经 `build_step_routers` + 2 处 `build_llm_router`（escalated / VL）→ `_build_provider` → `HTTPAnthropicClient`/`HTTPOpenAIClient`/7 个 compat 工厂/`self_hosted`/`azure` 分支，全部透传。
- control-plane `make_agent_builder` 新增 `http_client` kwarg，`_build` 闭包传给 `build_agent(...)`。
- control-plane `app.py` 的 lifespan：`shared_http = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=64, max_connections=256))` → `app.state.shared_http` → `stack.push_async_callback(shared_http.aclose)`（LIFO，最后关，其余 cleanup 回调若还要发 HTTP 请求不会因为 client 先关而炸）→ 唯一一处真正把 `shared_http` 递给 `make_agent_builder(..., http_client=shared_http)` 的调用点（`resolved_agent_runtime.agent_builder = make_agent_builder(...)`）。
- `ResolvingEmbedder`/`DynamicResolvingEmbedder`/`ResolvingReranker`/`DynamicResolvingReranker` 四个类（同一语义两对实现，四个都接，没有只接 Dynamic 那对生产 wire）都加了 `http` 字段；`app.py` 构造 `DynamicResolvingEmbedder`/`DynamicResolvingReranker` 时传 `http=shared_http`。两个 Reranker 类的 LLM-rerank 分支（非 DashScope，走 `build_llm_router`）也把 `http_client=self.http` 带过去了——这条是我顺手接的，不在 Step 7 的字面清单里，但同一个 `build_llm_router` 调用点就在改动的代码块里，不接等于留一条肉眼可见的缝。
- `resolve_web_search_client`/`build_supervisor_client`（`runtime.py`）都加 `http` 参数；`app.py` 里前者直接传 `http=shared_http`，后者因为**在 lifespan 之前**（`create_app` 同步体内）就被构造出来了，`shared_http` 还不存在，所以改成lifespan 里 `shared_http` 就绪后**原地 mutate** `resolved_supervisor_client.http = shared_http`（`HTTPSupervisorClient` 不是 `frozen=True`，这条 mutate 安全；且 `app.state.supervisor_client`、`base_tool_env` 里的 `supervisor_client=` 都引用的同一个对象，mutate 一处全部生效）。

**没接的**：`subagent_runtime.py` 里两处 `build_agent(...)`（子代理 / spawn_worker 构建）、`aux_model_adapter.py` / `quality_judge.py` / `runtime.py::_build_judge_caller` 里另外 3 处独立的 `build_llm_router(...)` 调用（辅助 LLM 用途：judge/quality）。这些都保持 `http_client=None`，走原 per-call fallback，不是回归——只是没有从"分量最大的两个高频调用点"进一步铺到"低频/子路径调用点"。协调者如果要在真栈上重点看连接复用效果，主 agent 的聊天补全 + embed + rerank + web_search + sandbox 五条路径应该都已经吃到复用；子代理/worker 构建、以及 judge/quality 这类辅助 LLM 调用**不会**。

## 我对哪处语义最没把握

**共享 client 的默认 timeout**（`app.py` 里 `httpx.AsyncClient(limits=...)`，没传 `timeout=`）——httpx 不传时退到内置 `Timeout(5.0)`（5 秒 connect+read+write+pool 各 5 秒）。我在**每一个**改造点的每一次 `.post()/.get()/.put()/.request()/.stream()` 调用上都补了 per-request `timeout=`（覆盖掉这个 5 秒默认），逻辑上应该滴水不漏——`ruff`/`mypy`/357+92 条测试都绿，但这些测试全部走 `MockTransport`，不会真的因为 timeout 太短而超时，所以**测试本身证明不了"我有没有漏补某一个调用点的 timeout"**，只能证明"接口签名和 mock 场景下行为没变"。如果真栈上出现某条链路（尤其是 `sandbox.py` 的 6 个调用点之一，那是我改动面最大、per-request timeout 补丁最密集的地方）在共享连接池模式下报 `httpx.ReadTimeout` 而 per-call 模式下不报，第一嫌疑就是这里漏补了一处。建议真栈验证时留意 sandbox 的 `exec`（跑 `pip install` 这类长命令）和长上下文 LLM 首字慢的场景。

其次是 `resolved_supervisor_client.http = shared_http` 这处原地 mutate——我读代码确认了 `HTTPSupervisorClient` 不是 frozen、`app.state.supervisor_client` 和 `base_tool_env` 引用的是同一个对象、mutate 发生在 lifespan 内且早于任何请求进来，逻辑链条自认站得住,但这是本 task 唯一一处"构造在先、注入在后"的非声明式接线,如果协调者的启动路径跟我读到的 `app.py` 不一致（比如某个部署禁用了 `sandbox_supervisor_url`,`resolved_supervisor_client` 是 None,mutate 分支被跳过——这个我已经用 `if resolved_supervisor_client is not None:` 挡了，但没有真栈验证过这条分支）,值得跑一次真实的 `exec_python`/`bash` 工具调用确认没有静默退回未共享状态。

## 未做的事（第一轮交付时）

- 没有跑 `tools/bench/entry_latency.py` 的 after 基线——按你的分工，这一步你在真栈上做。
- 没有跑 integration 标记的测试、没有跑 orchestrator/control-plane 之外的全量 suite——按你的范围指示跳过。
- 没有接 `subagent_runtime.py`/judge/quality 三处独立 `build_llm_router` 调用点——见上"接线深度"末尾的说明,如果你希望这些也吃到连接复用,是同一套 `http_client=` 透传模式,改动量不大但不在这次改动里。

---

## 追加：Opus 审查回来后的修复（第二轮）

审查结论：实现本身零遗漏零漂移（12 个调用点 per-request timeout 全对，共享分支变异验证杀得死，`resolve_ms` 打点未动，CodeQL 干净）。发现全在设计层，按①-⑤ 逐条修。

### ① 共享 client 构造（`app.py`，一处改三件事）

- **`timeout=None`**（原来没显式写，隐式吃 httpx 内置 `Timeout(5.0)`）。改成显式 `None` + 长注释：12 处 per-request timeout 一处都不删,兜底行为选温和的那个(router 的 first_token/idle timeout 或 run deadline 兜,不是 httpx 5s 静默铡断)。触发了 `ruff` 的 bandit 规则 `S113`(probable use of httpx call with timeout set to None),加了 `# noqa: S113` + 注释说明是故意的。
- **`max_connections=None`**（原来是 256）。256 是改造顺带引入的全新全局并发闸——池满会让 `httpx.PoolTimeout` 被现有错误分类逻辑翻译成 `LLMNetworkError`/"supervisor unreachable"，伪装成 provider 故障触发重试风暴，而改造前语义本就是无上限。`max_keepalive_connections=64` 保留不动（握手收益所在）。
- **空 cookie 策略**：`shared_http.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))`。共享 client 的持久 cookie jar 是这次改造新增的跨租户状态通道——手工验证过（脚本见下）：不设策略时，A 请求的 `Set-Cookie` 会被存进 jar，B 请求会带上它一起发出去。加空策略后验证「A 请求后 `dict(client.cookies)` 为空、B 请求也不带 cookie」。`import http.cookiejar` 加在文件顶部 stdlib import 块。

验证脚本（跑过，见下方"验证结果"）：手工起一个 `httpx.AsyncClient` + `MockTransport` 返回 `Set-Cookie`，确认设置策略前会存、设置后不存。

### ② `app.py:1204` 就地变异加 isinstance 门 + 删除 `runtime.py` 死参数

- `if resolved_supervisor_client is not None:` → `if isinstance(resolved_supervisor_client, HTTPSupervisorClient):`。加了 `from orchestrator.tools import HTTPSupervisorClient` 导入。`SupervisorClient`（Protocol）没有 `http` 成员，`mypy services/control-plane/src/control_plane/app.py` 之前在这行报 `attr-defined`（CI 不扫 control-plane，本地没人看得到）；改用 isinstance 后这个错误消失（验证见下）。
- `runtime.py` 的 `build_supervisor_client` 删掉了 `http` 参数——它构造在 `create_app` 同步体内、早于 lifespan 的 `shared_http` 存在，没有任何调用方能传非 None 值给它，是个死参数。检查过唯一调用点（`app.py:680`）和两个测试（`test_checkpointer_wiring.py:220/224/230`）都没用过这个 kwarg，删除零影响。

### ③ 参数化 timeout 存在性测试（`test_http_client_reuse.py`，把命门从"人不出错"变成"机器保证"）

新增 `test_every_call_site_passes_a_per_request_timeout`，用 `pytest.mark.parametrize` 遍历 **13 个真实调用点**（不是审查原话里的 12——我数了一遍实际改过 `timeout=` 的行：openai 2 + anthropic 2 + embedder 1 + rerank 1 + web_search 1 + sandbox 6 `_post`/`exec`/`read`/`list`/`write`/`delete` = 13。把这个差异写出来而不是悄悄按 12 交——多出来的那一个是 sandbox 的 `_post`，它被 `acquire`/`release`/`destroy`/`reap`/`mark_workspace_deleted` 五个方法共用，算作一个独立调用点）。

每个 case：注入一个 `_Recorder` transport（记录 `request.extensions["timeout"]`，统一返回 `httpx.Response(200, json={})`——审计了每个类的响应体消费逻辑，`json={}` 对全部 13 条路径都是合法输入，不需要为每个 case 定制响应形状），跑该类的对应方法，断言：
1. `request.extensions["timeout"]` 存在且等于该路径期望值（每个 client 用独立的 `timeout_s=12.5` 构造，跟其他默认值区分开，防止"凑巧对上默认值"的假阳性）；
2. 流式两条（`openai.stream_chat_completions`/`anthropic.stream_messages`）额外断言 `timeout["read"] is None`；
3. 共享 client 全程 `not is_closed`。

**自检结果（按要求手工做的，不是纸面推演）**：
- 临时删掉 `openai.py` `chat_completions` 里的 `timeout=self.timeout_s,`：`test_every_call_site_passes_a_per_request_timeout[openai.chat_completions]` **从绿变红**——`AssertionError: openai.chat_completions: timeout mismatch — got {'connect': 5.0, 'read': 5.0, 'write': 5.0, 'pool': 5.0}`（正是 httpx 内置 5s 默认，跟①的分析完全对上）。恢复后复绿。
- 临时删掉 `sandbox.py` `exec()` 里的 `timeout=read_timeout,`：`test_every_call_site_passes_a_per_request_timeout[sandbox.exec]` **从绿变红**——同样是 5.0s 默认，跟期望的 315.0s（`_MAX_EXEC_TIMEOUT_S + _EXEC_HTTP_BUFFER_S`）差了 63 倍。恢复后复绿，`git diff` 确认两个文件跟改动前逐字节一致。

### ④ 两处未接线在本轮补齐

- **`runtime.py::_build_judge_caller`**：加 `http_client` 参数，转发到 `build_llm_router(judge_spec, secret_store=secret_store, http_client=http_client)`。`_make_output_judge`/`_make_action_judge` 同步加参数转发。`make_agent_builder._build` 里两处调用点（`_make_output_judge(...)`/`_make_action_judge(...)`）传 `http_client=http_client`——这是闭包局部变量，`make_agent_builder` 本身已有这个参数（第一轮加的），零新增外部依赖。
- **`subagent_runtime.py`**：`make_child_agent_builder`/`make_worker_build_fn` 都加 `http_client: httpx.AsyncClient | None = None` 参数，转发到各自的 `build_agent(...)` 调用。`app.py` 里两处调用点（`make_child_agent_builder(...)`/`make_worker_build_fn(...)`）都在跟 `make_agent_builder(..., http_client=shared_http)` 同一个 lifespan 代码块里，补 `http_client=shared_http` 是纯透传，没有新建变量。
- **`aux_model_adapter.py`/`quality_judge.py` 两处按你的指示不动**——低频后台 worker，你记 ledger 作为后续 follow-up。

至此，除了这两处显式记为 follow-up 的低频路径，一期 Task 5 涉及的所有 `build_agent`/`build_llm_router` 调用链都已经把 `http_client` 一路传到底。

### ⑤ 三个 Minor

- `_http.py` 的 `client_for`：docstring 补了一段说明共享分支静默丢弃 `timeout`/`transport`；加了 `assert transport is None or shared is None`（+ `# noqa: S101`）防"注入 MockTransport 却因为 shared 也非 None 而被静默忽略,实际打到 shared 自己的 transport(生产环境里可能是真网络)"这个测试陷阱。顺手把 docstring 里写错的 `:func:`_client_for`` 改成实际符号名 `:func:`client_for``（我在第一轮就没照抄计划文档的下划线命名，这条是清理遗留的错误自引用，不是新决定）。检查过全仓库没有任何调用点同时传 `http=` 和 `transport=`，这条 assert 对现有代码零影响（跑全量测试验证过）。
- `runtime.py` 的静态 `resolve_embedder`/`resolve_reranker` 两个工厂补了 `http: httpx.AsyncClient | None = None` 参数并转发进 `ResolvingEmbedder(..., http=http)`/`ResolvingReranker(..., http=http)`。这两个工厂目前唯一的调用方是测试（生产 wire 是 `DynamicResolvingEmbedder`/`DynamicResolvingReranker`，在 `app.py` 里直接构造），加这个参数是为了不让 `ResolvingEmbedder`/`ResolvingReranker` 的 `http` 字段在"静态"这条腿上变成无 opt 路径的死字段。
- `app.state.shared_http`：保留（当前无读方），行内加注释 `# ops introspection hook; no reader yet`。

### 验证结果（第二轮）

```
uv run pytest services/orchestrator/tests/test_http_client_reuse.py -v
# 17 passed（原 4 个 + 新增 13 个参数化 case）

uv run pytest services/control-plane/tests/ -k "runtime or embedder or rerank or subagent or judge" -v
# 120 passed, 2028 deselected

uv run ruff check
# All checks passed!（含新增的 S113 noqa）

uv run ruff format --check
# 1478 files already formatted

uv run mypy packages services/orchestrator/src
# Success: no issues found in 763 source files

uv run mypy services/control-plane/src/control_plane/app.py 2>&1 | grep -v from_url
# (无输出 —— :1204 的 attr-defined 已消失,只剩 4 条既有的 from_url 噪音,已被过滤掉)
```

六条命令逐条同步跑、无后台化/轮询。

另外顺带跑了 `services/orchestrator/tests/ -v -k "provider or llm or embedder or rerank or web_search or sandbox"`（第一轮的受影响面回归网）：**366 passed, 1506 deselected**，零回归。

### 参数化测试的自检结果（明确回答）

**红**。删 `openai.py:307`(`chat_completions` 的 `timeout=`)和删 `sandbox.py` `exec()` 的 `timeout=`，对应 parametrize case 各自独立变红，报错信息精确指向"退化成了 httpx 内置 5.0s 默认"。两次都验证了恢复后文件与改动前逐字节一致（`git diff` 无输出）。
