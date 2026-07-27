# Perf Task 1 报告 —— 入口链 span 发射 + 单源契约

## 0. 前置说明:brief 文件缺失

`.superpowers/sdd/perf-task-1-brief.md` 在本 worktree 里不存在。追查发现:
- 本 worktree(`worktree-agent-a459185bdef7efdbb`)在派发时是从 main(`d81e81d2`)cut 的,`.superpowers/sdd/` 目录下只有其他批次(删除卫生 PR5 等)遗留的 tracked 报告文件,没有 `perf-*` brief。
- 计划文档实际躺在另一个 sibling 分支 `perf-latency-observability`(`bf9015c6`,基于同一个 `d81e81d2`),文件是 `docs/superpowers/plans/2026-07-27-agent-latency-observability.md`,里面 `## Task 1: 入口链 span 发射 + 单源契约` 一节(该文件第 82-397 行)就是本次任务的完整需求 —— 内容与调度者转述的 brief 摘要(Step 1-13、8 个 span 名、EXPECTED 集合、代码块)逐字吻合,判定为同一份材料的不同存放位置。
- 处理方式:用 `git show bf9015c6:docs/superpowers/plans/2026-07-27-agent-latency-observability.md` 取出该 Task 1 章节作为唯一需求来源,未凭空发挥。若这不是调度者期望的来源,请指出实际该用哪份文件。

## 1. 做了什么

### 1.1 `packages/expert-work-common/src/expert_work/common/observability/tracing.py`
在 `LLM_SPAN_PURPOSES` dict 之后插入 `TRACED_SPANS: frozenset[str]`,8 个 span 名,全部复用既有的 `_llm_span_name(component, action)` 构造函数。**未**按 brief 里"可选项"新增 `_span_name = _llm_span_name` 别名 —— 按调度者的裁决,直接复用 `_llm_span_name`,docstring 里说明该名字带 `llm` 只是历史包袱。

### 1.2 `packages/expert-work-common/src/expert_work/common/observability/__init__.py`
导出 `TRACED_SPANS`(import + `__all__`),按现有 isort 分组(CONSTANTS → Classes → functions,组内字母序)插入到 `LLM_SPAN_PURPOSES` 之后、`TRACEPARENT_HEADER` 之前。

### 1.3 `services/orchestrator/src/orchestrator/graph_builder/memory.py`
`memory_recall_node` 里布 6 个 span:
- 父 span `memory.recall` 包住两处 no-op 早退(无 tenant/user、无 task)**之后**的整段逻辑(和 brief 一致:早退分支不发空 span)。
- 子 span:`resolve_mode`(包 `_resolve_memory_recall_mode`)、`embed`(包 `embedder.embed` 调用)、`retrieve`(包 `memory_store.retrieve`)、`rerank`(包 `_rerank_memories`,只在 reranker 被装配时触发)、`bump_access`(包 `memory_store.bump_access`,只在有命中时触发)。
- 纯缩进 + 插入 `with` 语句,**没有改动任何业务逻辑**(abstain 阈值分支、MMR、verify_reads 分支原样保留,只是缩进层级变了)。`git diff` 可确认:每一处非空行改动要么是新增的 `with expert_work_span(...)`,要么是纯缩进偏移。

### 1.4 `services/orchestrator/src/orchestrator/graph_builder/workspace_ingest.py`
`workspace_ingest_node` 里,`tenant_id is None` 早退之后的全部函数体包进 `with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "workspace_ingest")`。加了对应 import。

### 1.5 `services/orchestrator/src/orchestrator/graph_builder/builder.py`
`agent_node` 里把 `tool_result_pruner` 和 `working_window` 两道门包进 `with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "context_gates")`。`compress` 的 span(它自己的 `orchestrator.compress`,LLM 调用)不在这个块内,按 brief 要求原样不动。`expert_work_span` 已在文件顶部导入,未新增 import。

### 1.6 `services/control-plane/src/control_plane/runtime.py`
`ResolvingEmbedder.embed` 和 `ResolvingReranker.rerank` 里加 `resolve_ms` / `secret_ms` 两个 span attribute,挂在调用方(orchestrator 的 `memory.embed` / `memory.rerank` span)上,用 `trace.get_current_span()` 取当前 span(没有活跃 span 时是 no-op,已用现有单测验证不炸)。
- `ResolvingEmbedder.embed`:严格照 brief 代码块实现,`t0/t1/t2` 三个时间点,两个 attribute。
- `ResolvingReranker.rerank`:brief 只给了文字描述("两条子路径各自打点"),没给代码块。实现为:`resolve_provider` 调用前后打 `resolve_ms`(两条子路径共享,因为 `resolve_provider` 在分支之前);`secret_store.get` 只在 DashScope 分支里调用,所以 `secret_ms` 只在那个分支设置。LLM router 分支没有 `secret_ms`(它不直接读 secret_store,`build_llm_router` 内部怎么解凭据是它自己的事,这次不展开)。
- 新增 `import time` 和 `from opentelemetry import trace`,按 isort 分组插入。

**没有改动**的相关文件:`services/orchestrator/src/orchestrator/sse.py`(明确是另一个并行 task 的地盘,完全没碰);`DynamicResolvingEmbedder` / `DynamicResolvingReranker`(runtime.py 里紧邻 `ResolvingEmbedder`/`ResolvingReranker` 的另外两个类,brief Step 11 只点名了 `ResolvingEmbedder`/`ResolvingReranker`,没有要求改这两个,按"不做超出要求的事"原则跳过 —— 见下文"发现但没改"）。

### 1.7 测试:`services/orchestrator/tests/test_entry_chain_spans.py`(新建)
- `test_traced_spans_covers_every_entry_chain_span`:parity 断言,`TRACED_SPANS == EXPECTED`(8 个名字逐字来自 brief)。
- `test_memory_recall_emits_the_full_entry_chain`:一次命中的 recall 驱动 `make_memory_recall_node`,断言 5 个 span 名字(recall + 4 子)都出现。
- `test_recall_children_nest_under_the_recall_span`:**命门测试**,断言 4 个子 span 的 `parent.span_id` 都等于 `recall` 父 span 的 `context.span_id`。这是调度者点名的关键测试,严格照裁决实现(不只断言名字出现),而且比 brief 伪代码里"只测 embed 一个子 span"更严格 —— 循环断言全部 4 个子 span,防止某个子 span 被漏挂父子关系而只挂对了其中一个。
- `test_workspace_ingest_emits_named_span`(brief Step 5 没有明确要求,自己补的):驱动 `make_workspace_ingest_node`,断言 `workspace_ingest` span 出现。
- `test_context_gates_emits_named_span`(同上,自己补的):通过 `build_react_graph` + `GraphRunner` 跑一轮真实 graph(不装配 pruner/window,验证 span 在两个门都是 no-op 时依然发射,因为它包住的是两个 `if` 检查本身而不是检查通过后的分支),断言 `context_gates` span 出现。

补这两个测试的理由:brief 里 Step 1 给的模块 docstring原文是"这里断言每个名字都真的被发射(对着 InMemorySpanExporter)"——如果只写 Step 5 给的两个测试(只覆盖 recall 家族的 5 个 span),`workspace_ingest` 和 `context_gates` 这两个 span 名字只在 `TRACED_SPANS` 集合里出现过,从未被真正断言"发射"过,docstring 的说法就不成立。补测试成本不高(`workspace_ingest` 有现成的 `RecordingSupervisorClient` 测试基建可以直接节点级调用;`context_gates` 借用 `test_working_window_wiring.py` 已有的"编译一个最小 graph 跑一轮"套路)。

## 2. 测试怎么跑的、输出

第一轮跑法(后台 + 全套 control-plane)撞了 600 秒看门狗被中断 —— 教训是**同步跑、缩小范围**,不要把长跑命令丢进后台再轮询等待。以下是中断后按调度者指定的四步顺序重新同步跑出的结果:

```bash
uv run pytest services/orchestrator/tests/test_entry_chain_spans.py -v
# 5 passed in 0.88s

uv run pytest services/orchestrator/tests/ -v -k "memory or span or workspace_ingest"
# 172 passed, 1679 deselected in 1.50s

uv run ruff check
# All checks passed!  (无路径参数,全库 + tests)

uv run mypy packages services/orchestrator/src
# Success: no issues found in 762 source files
```

外加一个可选的窄范围 control-plane 检查(验证 `runtime.py` 没被改坏,同步跑,没有跑全套):
```bash
uv run pytest services/control-plane/tests/ -k "embedder or rerank or resolving" -v
# 24 passed, 2122 deselected, 7 warnings in 1.31s
```

之前(被打断前)还额外同步跑过两轮,记录在案供参考:
```bash
uv run pytest -v -m "not integration" --timeout=120 --timeout-method=thread -k "memory or span or trace or workspace_ingest"
# 904 passed, 5855 deselected in 42.27s

uv run pytest -v -m "not integration" --timeout=60 --timeout-method=thread \
  services/orchestrator/tests/test_agent_node_gate_order.py \
  services/orchestrator/tests/test_working_window_wiring.py
# 4 passed in 0.74s —— context_gates 两道门(pruner/window)的行为级回归测试,确认没破坏

uv run pytest -v -m "not integration" --timeout=60 --timeout-method=thread \
  services/control-plane/tests/test_resolving_callers.py \
  services/control-plane/tests/test_dynamic_resolver.py \
  services/control-plane/tests/test_runtime.py
# 40 passed in 0.13s
```

**没有跑**:control-plane 全套测试(按调度者指示明确排除,且 mypy 的 CI 范围本来就不含 control-plane)。

## 3. 对哪里不确定

1. **brief 文件缺失本身**——见第 0 节。我用了 sibling 分支上的计划文档章节替代,内容核对下来和调度者转述的摘要(exact span 名字、EXPECTED 集合、Step 编号、代码块)完全一致,但我无法 100% 确认这就是调度者原本要我读的那份文件的完整版本(它可能在生成 brief 时做过裁剪/调整,而我读到的是裁剪前的原始计划)。
2. **`ResolvingReranker.rerank` 的计时实现是我补的**,不是照抄 brief 代码块(brief 对这块只有文字描述,没给代码)。我的实现让两条分支(DashScope / LLM router)都设置 `resolve_ms`,但只有 DashScope 分支设置 `secret_ms`。这个不对称本身是 brief 文字要求的("两个分支各自打点"),但"各自打点"具体打成什么形状是我的解读,值得复核。
3. **补的两个测试(workspace_ingest / context_gates)超出了 brief Step 5 字面列出的范围**。这是我基于 docstring 原文的字面要求主动补的,不是指令明确要求的。如果调度者认为这属于"给你没要求的东西"而不是"补全文档承诺",可以直接删掉这两个测试 —— 不会影响 parity 测试或另外两个命门测试。
4. **流程事故**:代码改完、四类校验(自己的新测试 / 受影响既有测试 / ruff / mypy)全部跑绿之后,我把一次"确认没有更大范围回归"的验证性 `pytest` 扔进了后台(整个 control-plane 测试目录,外加一个轮询 `until` 循环等它),导致撞上 600 秒看门狗被中断,期间没有任何 commit —— 所有改动短暂悬在工作区。中断后按调度者指示改为全同步、窄范围重跑,已确认四步全绿,随即立即提交,没有再犯。

## 4. 发现但没改的东西

- `services/control-plane/src/control_plane/runtime.py` 里,`DynamicResolvingEmbedder.embed`(约 906 行起)和 `DynamicResolvingReranker.rerank`(约 924 行起)有两处**已存在的、与本次改动无关**的 mypy 类型错误:
  ```
  Argument "provider" to "resolve_provider" of "CredentialsResolver" has incompatible type "str";
  expected "Literal['anthropic', 'openai', 'azure', 'self-hosted', 'kimi', 'glm', 'deepseek', 'qwen', 'doubao']"
  ```
  用 `git stash` 验证过:在我改动之前(`d81e81d2` 基线)这两个错误就存在(行号 895/923,我改动后因为上面插入了几行代码位移到 906/... )。这两个类是"读实时平台配置的 embedder/reranker",不在 brief Step 11 点名的 `ResolvingEmbedder`/`ResolvingReranker` 范围内,没有加计时也没有修类型错误 —— 按"只碰要求你碰的"原则留着没动。
- `DynamicResolvingEmbedder`/`DynamicResolvingReranker` 本身也没有加 `resolve_ms`/`secret_ms` 计时(brief Step 11 只点名了 `ResolvingEmbedder`/`ResolvingReranker` 两个类)。如果二期 embedder/reranker 的凭据缓存优化也要覆盖 Dynamic 版本,这里的计时是缺失的。

## 5. Commit

`d50e6e73` —— `feat(observability): 入口链 8 个 span + TRACED_SPANS 单源`(本 worktree 分支 `worktree-agent-a459185bdef7efdbb`,基线 `d81e81d2`)。
`e104c4ed` —— `docs: perf-task-1 报告补 commit sha`。

## 6. Follow-up:生产路径纠偏(DynamicResolvingEmbedder / DynamicResolvingReranker)

**背景**:上面 1.6 节记录的 `ResolvingEmbedder`/`ResolvingReranker` 打点是照 brief Step 11 字面实现的,但调度者核对 `services/control-plane/src/control_plane/app.py:1307`、`:1322` 后发现 —— **生产路径 wire 的其实是 `DynamicResolvingEmbedder`/`DynamicResolvingReranker`**,`ResolvingEmbedder`/`ResolvingReranker` 的构造工厂 `resolve_embedder()`/`resolve_reranker()` 在 `services/` 下没有任何非测试调用点。也就是说打在 `ResolvingEmbedder`/`ResolvingReranker` 上的 `resolve_ms`/`secret_ms` 永远不会出现在生产的 `memory.embed`/`memory.rerank` span 上。这是 brief 指错了目标类,不是本 task 实现有误 —— 已按调度者指示补齐 Dynamic 那一对,`ResolvingEmbedder`/`ResolvingReranker` 上的打点保留不删(同一语义两处实现,约束要全处一起加,为将来切回这两个类打好底)。

**改的地方**(均在 `services/control-plane/src/control_plane/runtime.py`):
- `DynamicResolvingEmbedder.embed`:比 `ResolvingEmbedder.embed` 多一次 DB 读(`effective_embedding_config()` 拉平台配置),所以是三段计时而非两段:`config_ms`(读配置)+ `resolve_ms`(凭据解析)+ `secret_ms`(vault 读)。`cfg is None` 的早退(直接 `raise AgentFactoryError`)在 `config_ms` 计时点之后、不打任何 attribute —— 没有 embed 可言。
- `DynamicResolvingReranker.rerank`:同款,`config_ms` + `resolve_ms` 在三条早退路径(`documents` 为空 / `cfg is None` / `CredentialsResolverError`)都跳过之后才打;`secret_ms` 只在 DashScope 分支打(LLM router 分支不直接读 secret_store),和 `ResolvingReranker.rerank` 的不对称处理保持一致。

**验证**(同步跑,未后台化):
```bash
uv run pytest services/orchestrator/tests/test_entry_chain_spans.py -v
# 5 passed in 0.87s —— 未受影响(这批改动只碰 control-plane)

uv run pytest services/control-plane/tests/ -k "embedder or rerank or resolving" -v
# 24 passed, 2122 deselected in 1.29s —— 和纠偏前跑的 24/24 一致,没有回归

uv run ruff check
# All checks passed!
```
另外自查了一遍 `uv run mypy services/control-plane/src/control_plane/runtime.py`(non-CI,自愿):还是那两处与本次改动无关的既有错误(行号因插入代码从 895/923 位移到 920/961,内容不变,`git stash` 已在 1.6 节验证过是基线本来就有的)。

**记一笔、没动代码**:`resolve_embedder()`/`resolve_reranker()` 这两个工厂函数(构造被打点的 `ResolvingEmbedder`/`ResolvingReranker`)在 `services/` 下疑似是死代码 —— 生产路径不经过它们。调度者已确认记入 backlog,本批不清理,这里只是留痕。

**Commit**:`a116c7a6` —— `fix(observability): 补 DynamicResolvingEmbedder/Reranker 计时(生产实际 wire 的类)`。
