# B-129 只读查询结果"再查一次"收起 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新一轮开头,把旧轮次里"可重新查询的只读工具"的结果(≥500 字、没有副本)收成一行"再调一次"提示,不存副本。

**Architecture:** 复用 B-126 的 `prune_prior_turns`(同一边界、同一保护、同一门槛),在"找不到无损引用就跳过"那一步之前加一条分支:工具名在"可重新查询名单"里 → 用再查提示收起。名单由新函数 `requery_tool_names(registry)` 在 `agent_factory` 里所有工具注册完之后算出,经 `dataclasses.replace` 装进 `ToolResultPruner`;不改 `ToolSpec.is_read_only`。配置 `policies.tool_result_prune.requery` 默认开、默认值不落库。

**Tech Stack:** Python 3.12、pydantic v2、langchain-core messages、LangGraph、pytest(asyncio)。

**Spec:** `docs/superpowers/specs/2026-09-29-requery-prune-design.md`

## Global Constraints

- 可重新查询 = 内置工具 `ToolSpec.is_read_only is True`,或 MCP 工具 `tool_def.read_only is True`(服务器 `readOnlyHint: true`);`None` / `False` 一律不算。
- 唯一排除 `ask_image`(已在 `_NEVER_PRUNE_TOOLS`);`web_search` **在**名单内。
- **不改 `ToolSpec.is_read_only`**(它还驱动重试与并行调度)。
- 单条最小长度 **500 字**(`len(content) >= 500`)。
- 找回途径优先级:已有无损引用(技能引用 / 副本路径 / footer)> 再查一次 > 不动。
- 其余门槛全部沿用 B-126:新一轮开头、最近 4 条不动、上下文 < 30000 token 不动、合计省 < 5000 token 不动、`status == "error"` 不动、非字符串内容不动。
- 配置字段 `ToolResultPrunePolicy.requery: bool = True`,默认值不落库(加进 `_B126_OMIT_AT_DEFAULT`);`cross_turn: false` 时一并不生效。
- 提示文字面向全平台,**不含任何租户 / 客户内容**,英文,沿用 `<tool-result-pruned>` 标签。
- 测试里的名字用中性名(`fetch_record` / `lookup_x`),不出现任何客户或租户名。
- 本地跑 Python 一律:`export UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv`、`export PYTHONPATH=$(ls -d <worktree>/packages/*/src <worktree>/services/*/src | paste -sd: -)`、`uv run --no-sync …`;信绿之前先打印被改模块的 `__file__`。
- 每条新行为都要变异自证:改坏 → 看红 → 用 Edit/python 替换改回(不许 `git checkout` / `restore` / `stash`)→ 看绿 → `git diff` 干净。

## Review Focus

1. 同一条结果既有副本、工具又在名单里 → 必须走副本引用,不能被再查提示覆盖(读文件比重调 MCP 便宜、拿到的是当时原文)。→ Task 2 `test_copy_reference_wins_over_requery`。
2. 名单内工具的**失败结果**不收 → Task 2 `test_requery_skips_error_results`。
3. **本轮**的名单内结果不收(只动旧轮次)→ Task 2 `test_requery_never_touches_current_turn`。
4. MCP 工具没标或标了 `readOnlyHint: false` → 不进名单 → Task 3 `test_mcp_unannotated_or_write_tools_excluded`。
5. `requery=False` 时 B-126 原有的副本收起照常工作 → Task 2 `test_requery_off_keeps_b126_behaviour`。

---

### Task 1: 配置字段 `requery`

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py`(`_B126_OMIT_AT_DEFAULT` ~850、`ToolResultPrunePolicy` ~1016)
- Test: `packages/expert-work-protocol/tests/test_agent_spec.py`(`_B126_DEFAULTS` ~1231,文件末尾加新测)

**Interfaces:**
- Produces: `ToolResultPrunePolicy.requery: bool`(默认 `True`)。

- [ ] **Step 1: 写失败的测试**

在 `_B126_DEFAULTS` 字典里加一项 `"requery": True,`(让既有的 `test_b126_prune_policy_defaults` / `test_new_fields_omitted_at_default` 一并覆盖它),并在文件末尾加:

```python
def test_b129_requery_kept_when_disabled() -> None:
    dumped = ToolResultPrunePolicy(requery=False).model_dump(mode="json")
    assert dumped["requery"] is False
    assert "requery" not in ToolResultPrunePolicy().model_dump()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest -q packages/expert-work-protocol/tests/test_agent_spec.py -k "b126 or b129 or omitted"`
Expected: FAIL(`ToolResultPrunePolicy` 没有 `requery` 字段 / extra 被拒)

- [ ] **Step 3: 实现**

`_B126_OMIT_AT_DEFAULT` 加 `"requery": True,`;`ToolResultPrunePolicy` 在 `absolute_cap_tokens` 之后加:

```python
    #: B-129 —— 旧轮次里可重新查询的只读工具结果(无副本、≥500 字)收成「再调一次」提示。
    requery: bool = True
```

- [ ] **Step 4: 跑测试确认通过 + 存量指纹不变**

Run: `uv run --no-sync pytest -q packages/expert-work-protocol/tests/test_agent_spec.py packages/expert-work-persistence/tests/test_agent_spec_stored_json.py`
Expected: PASS(黄金 sha 不变 —— 默认值不落库)

- [ ] **Step 5: 变异自证** —— 把 `_B126_OMIT_AT_DEFAULT` 里的 `"requery": True,` 删掉 → `test_new_fields_omitted_at_default` 红;改回 → 绿。

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py packages/expert-work-protocol/tests/test_agent_spec.py
git commit -m "feat(protocol): tool_result_prune.requery switch (B-129)"
```

---

### Task 2: 清理器新增"再查一次"分支

**Files:**
- Modify: `services/orchestrator/src/orchestrator/context/tool_result_prune.py`(`PruneResult` ~97、`_cross_turn_stub` 之后新增 `_requery_stub`、`prune_prior_turns` ~391、`ToolResultPruner` ~472)
- Test: `services/orchestrator/tests/test_tool_result_prune_cross_turn.py`(复用文件里的 `_call` / `_res` / `_turn` / `_run` / `_BIG`)

**Interfaces:**
- Consumes: 无。
- Produces:
  - `PruneResult.requery_count: int = 0`
  - `prune_prior_turns(..., requery_tools: frozenset[str] = frozenset(), requery_min_chars: int = REQUERY_MIN_CHARS)`
  - `REQUERY_MIN_CHARS: int = 500`(模块级常量)
  - `ToolResultPruner.requery: bool = True`、`ToolResultPruner.requery_tools: frozenset[str] = frozenset()`

- [ ] **Step 1: 写失败的测试**(追加到 `test_tool_result_prune_cross_turn.py`)

```python
_MID = "m" * 2000  # 无副本、< PERSIST 门槛的典型业务查询结果
_RQ = frozenset({"fetch_record"})


def _plain_turn(tag: str, n: int, content: str = _MID, **kw: object) -> list[BaseMessage]:
    out: list[BaseMessage] = [HumanMessage(content=f"user {tag}")]
    for i in range(n):
        cid = f"{tag}-{i}"
        out += [_call(cid), _res(cid, content, persisted=False, **kw)]  # type: ignore[arg-type]
    out.append(AIMessage(content=f"answer {tag}"))
    return out


def _stubbed(res: PruneResult) -> list[ToolMessage]:
    return [
        m for m in res.messages
        if isinstance(m, ToolMessage) and str(m.content).startswith("<tool-result-pruned>")
    ]


def test_requery_stubs_old_readonly_results_without_copy() -> None:
    msgs = [*_plain_turn("a", 6), HumanMessage(content="now")]
    res = _run(msgs, requery_tools=_RQ)
    stubs = _stubbed(res)
    assert res.requery_count == len(stubs) == 5  # 最近 1 条受保护(_run: kept=1)
    assert "call the same tool again" in str(stubs[0].content)
    assert "m" * 50 not in str(stubs[0].content)  # 正文不外带


def test_requery_ignores_tools_not_in_list() -> None:
    msgs = [*_plain_turn("a", 6), HumanMessage(content="now")]
    assert _stubbed(_run(msgs, requery_tools=frozenset({"other"}))) == []


def test_requery_respects_min_chars() -> None:
    msgs = [*_plain_turn("a", 6, content="s" * 499), HumanMessage(content="now")]
    assert _run(msgs, requery_tools=_RQ, min_reclaim_tokens=1).requery_count == 0


def test_copy_reference_wins_over_requery() -> None:
    msgs = [*_turn("a", 6), HumanMessage(content="now")]  # 全部带副本路径
    res = _run(msgs, requery_tools=_RQ)
    assert res.requery_count == 0
    assert all("Saved to .tool_results/" in str(m.content) for m in _stubbed(res))


def test_requery_skips_error_results() -> None:
    msgs = [*_plain_turn("a", 6, status="error"), HumanMessage(content="now")]
    assert _run(msgs, requery_tools=_RQ).requery_count == 0


def test_requery_never_touches_current_turn() -> None:
    cur = [HumanMessage(content="now")]
    for i in range(6):
        cur += [_call(f"n-{i}"), _res(f"n-{i}", _MID, persisted=False)]
    msgs = [*_plain_turn("a", 6), *cur]
    res = _run(msgs, requery_tools=_RQ)
    boundary = current_turn_start(msgs)
    assert all(not str(m.content).startswith("<tool-result-pruned>")
               for m in res.messages[boundary:] if isinstance(m, ToolMessage))


def test_requery_is_stable_within_a_turn() -> None:
    base = [*_plain_turn("a", 6), HumanMessage(content="now")]
    first = _run(base, requery_tools=_RQ).messages
    later = _run([*base, _call("n-0"), _res("n-0", _MID, persisted=False)], requery_tools=_RQ)
    assert [m.content for m in later.messages[: len(first)]] == [m.content for m in first]


def test_requery_off_keeps_b126_behaviour() -> None:
    from orchestrator.context import ToolResultPruner

    msgs = [*_turn("a", 3), *_plain_turn("b", 3), HumanMessage(content="now")]
    pr = ToolResultPruner(
        context_window=10**9, recent_tool_results_kept=1, min_context_tokens=100,
        min_reclaim_tokens=100, requery=False, requery_tools=_RQ,
    )
    res = pr.apply(msgs)
    assert res.requery_count == 0
    assert res.pruned_count == 3  # a 轮 3 条带副本的照常收起
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest -q services/orchestrator/tests/test_tool_result_prune_cross_turn.py -k requery`
Expected: FAIL(`unexpected keyword argument 'requery_tools'` / `PruneResult` 无 `requery_count`)

- [ ] **Step 3: 实现**

`PruneResult` 末尾加:

```python
    #: B-129 —— 其中收成「再调一次」提示的条数(观测用)。
    requery_count: int = 0
```

模块级常量(放在 `_ARGS_HINT_LIMIT` 旁边):

```python
#: B-129 —— 可重新查询的结果至少多长才收(提示本身约 200~300 字,更短的几乎不省)。
REQUERY_MIN_CHARS = 500
```

`_cross_turn_stub` 之后新增:

```python
def _requery_stub(message: ToolMessage, *, args: Mapping[str, Any] | None) -> str:
    """B-129 —— 只读、可重新查询的旧结果的提示:只含工具名、参数提示、字数,不外带正文。"""
    content = message.content
    size = len(content) if isinstance(content, str) else 0
    head = f"[{message.name or 'tool'}]"
    if args:
        head += f" {_args_hint(args)}"
    return (
        f"{_PRUNE_TAG_OPEN}\n"
        f"{head} — {size:,} chars from an earlier turn, elided.\n"
        "Read-only lookup: call the same tool again with the same arguments as that earlier "
        "call to get the data back (it returns current data, which may have changed since).\n"
        f"{_PRUNE_TAG_CLOSE}"
    )
```

`prune_prior_turns` 签名加两个关键字参数:

```python
    requery_tools: frozenset[str] = frozenset(),
    requery_min_chars: int = REQUERY_MIN_CHARS,
```

循环里把

```python
        reference = _lossless_reference(m)
        if reference is None:
            continue
        stub = _cross_turn_stub(m, args=args_by_call.get(m.tool_call_id), reference=reference)
```

换成

```python
        reference = _lossless_reference(m)
        args = args_by_call.get(m.tool_call_id)
        if reference is not None:
            stub = _cross_turn_stub(m, args=args, reference=reference)
            is_requery = False
        elif m.name in requery_tools and len(content) >= requery_min_chars:
            stub = _requery_stub(m, args=args)
            is_requery = True
        else:
            continue
```

在 `replacements[i] = new` 之后加 `requery_count += is_requery`(循环前初始化 `requery_count = 0`),最后的返回改为:

```python
    return PruneResult(
        messages=out,
        pruned_count=len(replacements),
        reclaimed_tokens=reclaimed,
        requery_count=requery_count,
    )
```

docstring 追加一段:"B-129 —— 没有无损引用、但工具在 ``requery_tools`` 里且正文 ≥ ``requery_min_chars`` 的结果,收成「再调一次」提示(不外带正文)。优先级:无损引用 > 再查 > 不动。"

`ToolResultPruner` 加两个字段(放在 `absolute_cap_tokens` 之后):

```python
    #: B-129 —— 配置开关(``policies.tool_result_prune.requery``)与可重新查询的工具名单。
    requery: bool = True
    requery_tools: frozenset[str] = frozenset()
```

`apply` 里调用 `prune_prior_turns` 时加 `requery_tools=self.requery_tools if self.requery else frozenset(),`;接收处多取 `requery = cross.requery_count`(循环前 `requery = 0`);两处 `return PruneResult(...)` 都带上 `requery_count=requery`。

- [ ] **Step 4: 跑测试确认通过,再跑整个 prune 相关测试**

Run: `uv run --no-sync pytest -q services/orchestrator/tests -k "tool_result_prune"`
Expected: 全部 PASS

- [ ] **Step 5: 变异自证**(每条都要:改坏 → 红 → 改回 → 绿 → `git diff` 只剩本任务改动)
  - 把 `if reference is not None:` 分支与 `elif` 分支对调顺序 → `test_copy_reference_wins_over_requery` 红
  - `len(content) >= requery_min_chars` 改成 `> 0` → `test_requery_respects_min_chars` 红
  - `apply` 里 `if self.requery else frozenset()` 改成恒传 `self.requery_tools` → `test_requery_off_keeps_b126_behaviour` 红

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/context/tool_result_prune.py services/orchestrator/tests/test_tool_result_prune_cross_turn.py
git commit -m "feat(context): collapse old re-queryable read-only results to a call-again stub (B-129)"
```

---

### Task 3: 可重新查询名单 + 接线 + 观测

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/requery.py`
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py`(`ToolResultPruner(` ~1043 加 `requery=`;`graph = build_react_graph(` ~1231 之前补名单)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`(`_cm_cross_turn_reclaimed_tokens` ~352 旁加计数器;`context_gates` span ~722)
- Test: `services/orchestrator/tests/test_requery_tool_names.py`(新)、`services/orchestrator/tests/test_tool_result_prune_cross_turn_wiring.py`(追加)
- Modify: `docs/superpowers/ROADMAP.md`(B-128 行之后加 B-129 行)

**Interfaces:**
- Consumes: Task 2 的 `ToolResultPruner.requery` / `requery_tools`、`PruneResult.requery_count`;`ToolRegistry.all_specs()` / `ToolRegistry.get(name)`;`MCPTool.tool_def.read_only`。
- Produces: `requery_tool_names(registry: ToolRegistry) -> frozenset[str]`。

- [ ] **Step 1: 写失败的测试** —— `test_requery_tool_names.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.tools import ToolRegistry, ToolSpec
from orchestrator.tools.mcp import MCPTool, MCPToolDef, RecordingMCPClient
from orchestrator.tools.requery import requery_tool_names


@dataclass
class _Builtin:
    spec: ToolSpec

    async def call(self, args: Any, *, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _builtin(name: str, *, read_only: bool) -> _Builtin:
    return _Builtin(ToolSpec(name=name, description="d", parameters={}, is_read_only=read_only))


def _mcp_tool(name: str, read_only: bool | None) -> MCPTool:
    tool_def = MCPToolDef(name=name, description="d", input_schema={}, read_only=read_only)
    return MCPTool(client=RecordingMCPClient(), tool_def=tool_def, server_name="srv")


def test_builtin_read_only_tools_are_listed() -> None:
    reg = ToolRegistry()
    reg.register(_builtin("lookup_x", read_only=True))
    reg.register(_builtin("write_x", read_only=False))
    assert requery_tool_names(reg) == frozenset({"lookup_x"})


def test_mcp_read_only_hint_true_is_listed() -> None:
    reg = ToolRegistry()
    tool = _mcp_tool("fetch_record", True)
    reg.register(tool)
    assert tool.spec.name in requery_tool_names(reg)


def test_mcp_unannotated_or_write_tools_excluded() -> None:
    reg = ToolRegistry()
    reg.register(_mcp_tool("maybe", None))
    reg.register(_mcp_tool("writes", False))
    assert requery_tool_names(reg) == frozenset()


def test_ask_image_excluded_even_if_read_only() -> None:
    reg = ToolRegistry()
    reg.register(_builtin("ask_image", read_only=True))
    assert requery_tool_names(reg) == frozenset()


def test_deferred_tools_are_listed_too() -> None:
    reg = ToolRegistry()
    reg.register(_builtin("lookup_x", read_only=True), deferred=True)
    assert requery_tool_names(reg) == frozenset({"lookup_x"})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest -q services/orchestrator/tests/test_requery_tool_names.py`
Expected: FAIL(`No module named 'orchestrator.tools.requery'`)

- [ ] **Step 3: 实现 `tools/requery.py`**

```python
"""B-129 —— 哪些工具的旧结果可以收成「再调一次」提示。

只读、重新调用就能拿回数据的工具:内置工具看 ``ToolSpec.is_read_only``,MCP 工具看
服务器 ``list_tools`` 标注的 ``readOnlyHint``(``tool_def.read_only is True``;没标 /
标 ``False`` 都不算,与 B-122 给子智能体放行的判据同源)。

刻意不去改 ``ToolSpec.is_read_only`` 来表达这件事:它还决定失败自动重试
(``error_classifier``)与并行调度(``scheduling``),把 MCP 工具标成只读会顺带改掉这两处。
"""

from __future__ import annotations

from orchestrator.context.tool_result_prune import NEVER_PRUNE_TOOLS
from orchestrator.tools.mcp import MCPTool
from orchestrator.tools.registry import ToolRegistry

__all__ = ["requery_tool_names"]


def requery_tool_names(registry: ToolRegistry) -> frozenset[str]:
    names: set[str] = set()
    for spec in registry.all_specs():
        tool = registry.get(spec.name)
        read_only = (
            tool.tool_def.read_only is True if isinstance(tool, MCPTool) else spec.is_read_only
        )
        if read_only:
            names.add(spec.name)
    return frozenset(names - NEVER_PRUNE_TOOLS)
```

同时在 `tool_result_prune.py` 把 `_NEVER_PRUNE_TOOLS` 改名为公开的 `NEVER_PRUNE_TOOLS`(文件内所有引用一起改;`grep -rn "_NEVER_PRUNE_TOOLS" services/` 确认没有别处引用)。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest -q services/orchestrator/tests/test_requery_tool_names.py`
Expected: PASS

- [ ] **Step 5: 接线 `agent_factory.py`**

`ToolResultPruner(` 构造参数末尾加 `requery=trp_policy.requery,`。在 `graph = build_react_graph(` 那一行**之前**加:

```python
    # B-129 —— 名单要在所有工具注册完之后算(skill_view / 技能编写工具 / manage_task 都注册在
    # 上面的清理器构造之后)。
    if tool_result_pruner is not None:
        tool_result_pruner = replace(
            tool_result_pruner, requery_tools=requery_tool_names(registry)
        )
```

并加 import `from orchestrator.tools.requery import requery_tool_names`(`replace` 已从 `dataclasses` 导入)。

- [ ] **Step 6: 观测 `builder.py`**

`_cm_cross_turn_reclaimed_tokens` 定义之后加:

```python
#: B-129 —— 收成「再调一次」提示的旧结果条数,按模型调用累加(不按工具名打标签)。
_cm_requery_stub_total = expert_work_counter(
    "expert_work_cm_requery_stub_total",
    "Old read-only tool results collapsed to a call-again stub (B-129), summed over model calls.",
)
```

`context_gates` span 里 `gates_span.set_attribute("cm.cross_turn.reclaimed_tokens", ...)` 之后加:

```python
                gates_span.set_attribute("cm.cross_turn.requery_stubbed", pruned.requery_count)
                if pruned.requery_count:
                    _cm_requery_stub_total.inc(pruned.requery_count)
```

- [ ] **Step 7: 写接线测试**(追加到 `test_tool_result_prune_cross_turn_wiring.py`,复用该文件的 `_RecordingLLM` / `_invoke` 结构;`_pruner()` 另造一个带 `requery_tools=frozenset({"mcp_x"})` 的版本,历史用无副本、2000 字的 `mcp_x` 结果)

```python
@pytest.mark.asyncio
async def test_requery_stub_reaches_the_llm_and_checkpoint_is_intact() -> None:
    history: list[BaseMessage] = [HumanMessage(content="user a")]
    for i in range(3):
        cid = f"a-{i}"
        history += [
            AIMessage(content="", tool_calls=[{"id": cid, "name": "mcp_x", "args": {"k": cid}}]),
            ToolMessage(content="r" * 8000 + f"#{i}", tool_call_id=cid, name="mcp_x"),
        ]
    history += [AIMessage(content="answer a"), HumanMessage(content="now")]
    llm = _RecordingLLM()
    pruner = ToolResultPruner(
        context_window=10**9, recent_tool_results_kept=1, min_context_tokens=100,
        min_reclaim_tokens=100, requery_tools=frozenset({"mcp_x"}),
    )
    state = await _invoke_with(llm, history, str(uuid4()), pruner)
    prompt_tools = [m for m in llm.seen[0] if isinstance(m, ToolMessage)]
    assert sum("call the same tool again" in str(m.content) for m in prompt_tools) == 2
    saved = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert all(str(m.content).startswith("r" * 100) for m in saved)
```

`_invoke_with` = 现有 `_invoke` 的一个变体,把 `tool_result_pruner=_pruner()` 换成参数传入的 `pruner`。

- [ ] **Step 8: 跑测试 + 全量 orchestrator + lint + mypy**

Run:
```bash
uv run --no-sync pytest -q services/orchestrator/tests -k "requery or tool_result_prune"
uv run --no-sync pytest -q -p no:cacheprovider services/orchestrator/tests
uv run --no-sync ruff check <改过的文件> && uv run --no-sync ruff format --check <改过的文件>
uv run --no-sync mypy <改过的 src 文件>
```
Expected: 全部 PASS(本机 docker 集成 5 条 ERROR 属环境,如实记)

- [ ] **Step 9: 变异自证**
  - 先在 `services/orchestrator/tests/test_agent_factory.py` 的 `test_tool_result_pruner_threads_trp_policy_fields`(~2070)之后加一条,照它的 spy 写法同时抓 `tool_result_pruner` 与 `tool_registry`:

    ```python
    async def test_tool_result_pruner_gets_requery_tool_list(monkeypatch: Any) -> None:
        captured: dict[str, Any] = {}
        from orchestrator.agent_factory import build_react_graph as real
        from orchestrator.tools.requery import requery_tool_names

        def _spy(**kwargs: Any) -> Any:
            captured["pruner"] = kwargs.get("tool_result_pruner")
            captured["registry"] = kwargs.get("tool_registry")
            return real(**kwargs)

        monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)
        doc = deepcopy(_MINIMAL_SPEC)
        doc["spec"]["policies"] = {"tool_result_prune": {"requery": False}}
        spec = AgentSpec.model_validate(doc)
        async with make_checkpointer("memory") as cp:
            await _build(
                spec, secret_store=_secret_store(), checkpointer=cp, platform_tool_budget_enabled=True
            )
        pruner = captured["pruner"]
        expected = requery_tool_names(captured["registry"])
        assert expected  # 名单非空,否则下面的相等断言咬不住「没补名单」
        assert pruner.requery_tools == expected
        assert pruner.requery is False
    ```

    若 `expected` 为空(最小 manifest 没有只读工具),在 `doc["spec"]["tools"]` 里加一个只读内置工具(如 `{"name": "skill_view", "type": "builtin", "config": {}}` 或 `_MINIMAL_SPEC` 能构建的任一 `is_read_only` 内置工具)使其非空。
  - 删掉 `agent_factory` 里补名单那段 → 上面这条红;`requery=trp_policy.requery,` 删掉 → 同一条红(`requery is False` 断言)。
  - `requery_tool_names` 里 `is True` 改成 `is not False` → `test_mcp_unannotated_or_write_tools_excluded` 红。
  - builder 里 `set_attribute("cm.cross_turn.requery_stubbed", …)` 删掉:span 属性无既有断言手段则如实记录"观测行未被测试钉住"。

- [ ] **Step 10: ROADMAP**

B-128 行之后加:

```markdown
| B-129 | **(2026-09-29 立项;🔄 开发中,测试环境验收后另排班车)旧的只读查询结果收成「再调一次」**:业务系统 MCP 查询结果占 ai-health-plan 输入 27%,多为 1000~4000 字、没副本,B-126 收不了。新一轮开头把旧轮次里可重新查询的只读工具(内置 `is_read_only` / MCP `readOnlyHint: true`,只排除 `ask_image`)≥500 字、无副本的结果收成一行「再调一次」提示;有副本的仍走副本引用;不能重跑的(bash / 执行代码)不变。配置 `policies.tool_result_prune.requery` 默认开、可按 Agent 关。spec `docs/superpowers/specs/2026-09-29-requery-prune-design.md`,计划 `docs/superpowers/plans/2026-09-29-requery-prune.md`。 |
```

- [ ] **Step 11: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/requery.py services/orchestrator/src/orchestrator/context/tool_result_prune.py services/orchestrator/src/orchestrator/agent_factory.py services/orchestrator/src/orchestrator/graph_builder/builder.py services/orchestrator/tests/test_requery_tool_names.py services/orchestrator/tests/test_tool_result_prune_cross_turn_wiring.py docs/superpowers/ROADMAP.md
git commit -m "feat(context): wire re-queryable tool list into the pruner + metrics (B-129)"
```

---

### Task 4: 测试环境验收(不写代码,控制器自己做)

- [ ] 从本分支 `release.sh test` 发测试环境(control-plane / credential-proxy),smoke + 金丝雀 PASS。
- [ ] ai-health-plan 用现配置(glm-5.3)重放同一段 7 轮对话 **3 次**;再把 `requery` 设为 `false` 发布后重放 **3 次**(对照组);完了恢复默认。
- [ ] 每组都在第 5 轮后加一轮追问:"把第一次查到的空腹血糖历史记录列一下,并据此调整监测频率"。
- [ ] 判据(spec §5):成本中位数开比关低(目标 ≥10%);两组交付件都过技能校验;数字出处核对无退化;追问轮数字与原始数据一致(凭印象写错 = 0);统计被收起工具的重新调用次数;全部轮次 success。
- [ ] 结果写进 ROADMAP B-129 行与 PR 描述;通过后再议上哪班车。
