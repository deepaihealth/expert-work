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

## 未做的事

- 没有跑 `tools/bench/entry_latency.py` 的 after 基线——按你的分工，这一步你在真栈上做。
- 没有跑 integration 标记的测试、没有跑 orchestrator/control-plane 之外的全量 suite——按你的范围指示跳过。
- 没有接 `subagent_runtime.py`/judge/quality 三处独立 `build_llm_router` 调用点——见上"接线深度"末尾的说明,如果你希望这些也吃到连接复用,是同一套 `http_client=` 透传模式,改动量不大但不在这次改动里。
