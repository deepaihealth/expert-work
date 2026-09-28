# 跨轮上下文降本(B-126)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新一轮开始时无损收起更早轮次的大块工具结果,有损步骤改成「窗口百分比与 20 万取小」,压缩摘要写回检查点复用 —— 让 100 万窗口模型的长对话不再越滚越贵。

**Architecture:** 改造现有三道上下文闸(CM-12 `ToolResultPruner` → CM-2 `WorkingWindow` → L2 `ContextCompressor`),不新造机制。跨轮清理是一个只看「本轮开始前历史」的纯函数,所以同一轮内每次调用结果逐字相同、缓存前缀稳定;只改发给模型的视图,检查点历史不动。摘要通过新的 `AgentState.context_summary` 通道随检查点保存。

**Tech Stack:** Python 3.12、LangGraph(`add_messages`)、pydantic v2(`extra="forbid"` manifest 模型)、pytest(`uv run --no-sync`)。

**Spec:** `docs/superpowers/specs/2026-09-28-cross-turn-context-pruning-design.md`

## Global Constraints

- 默认开启,所有 Agent 生效;`policies.tool_result_prune.cross_turn=false` 可关(D1)。
- 只改发给模型的视图,检查点里的 `messages` 一字不改(D2)。
- 同一轮内清理决定逐字不变;决定只依赖 `messages[:current_turn_start+1]`(D3)。
- 不做「清理前记笔记」(D4)。
- 跨轮清理只收有无损找回途径的结果:外置 footer / `artifact` 持久化路径 / 成功的 `skill_view` 正文;无副本的一律不动(D6)。
- 默认值:`recent_tool_results_kept=4`、`min_context_tokens=30000`、`min_reclaim_tokens=5000`、`absolute_cap_tokens=200000`(CM-12 兜底、CM-2、L2 三处同值)。
- 新增 manifest 字段取默认值时**不落库**(同 `PromptVariableSpec._omit_default_render` 写法),存量 manifest 的 `compute_spec_sha256` 不变 —— 回滚到旧版本不会被 `extra="forbid"` 拒。
- 测试命令一律:`UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest <path> -q`(worktree 内;必须带 `--no-sync`)。
- 每条新断言必须「删掉实现 → 红 → 恢复 → 绿」自证,并用 `git diff` 确认变异真的落地、还原真的还原。
- 不用 `git checkout` / `git restore` / `git stash` 撤销;变异前先 commit。
- 提交信息结尾两行:`Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` 与 `Claude-Session: https://claude.ai/code/session_01DNzoY8vc8jDMsib4wFGFpN`。

## Rulings(计划阶段对 spec 的裁定)

- **R1 — spec §3.5「副本按对话分目录」改为「不搬目录,靠构造保证」。** 核代码发现:外置副本已按 run 分目录(`.tool_results/<run_id>/…`,`overflow_rel_path`);`search_files` 已过滤保留前缀(`nas_workspace_store._walk_and_match` 调 `is_reserved_workspace_path`);工作区树摘要也过滤。跨轮清理留下的占位只引用**本对话历史里已有**的路径,不可能引用别的对话的 run。改目录结构会连带留存 job 的孤儿扫描(按 run 目录名)、会话 purge、`figure_lookup` 的跨 run 缓存查找三处,风险大于收益。保留的暴露面(模型用 `list_dir` 显式列 `.tool_results`、沙箱里 `ls`)是**现状就有**、不因本改动扩大 —— 另立待办,不并入本计划。本裁定已回写 spec §3.5。代价:若日后要求物理隔离,需单做一次目录迁移。
- **R2 — 跨轮清理的「总量」按本轮开始时计。** 用 `messages[:boundary+1]`(旧轮次 + 本轮用户消息)估算,而不是整个视图 —— 否则本轮中途涨过 3 万会在轮中触发一次清理、打断缓存。代价:本轮自己涨得很大时跨轮清理不补清(由 20 万兜底接)。
- **R3 — 跨轮去重只在旧轮次内部做。** 与本轮结果比对会让决定随本轮增长而变。
- **R4 — `status == "error"` 的 ToolMessage 一律不清。** 安全拦截 / 漂移占位本就无副本(D6 已排除),这条是显式防线。

## Review Focus

1. **轮次边界判错** —— 平台注入的隐藏 HumanMessage(本轮输入块、计划、工作区摘要等)若被当成真实用户消息,会把「本轮」切短、在轮中误清。期望:只有 `is_hidden(m)` 为假的 HumanMessage 算边界。测试在 Task 2(`test_hidden_human_is_not_a_turn_boundary`)。
2. **同一轮内视图漂移** —— 本轮新增工具结果后再算一次,旧轮次部分必须逐字相同。测试在 Task 2(`test_view_is_identical_across_calls_in_a_turn`)与 Task 3 图级测试。
3. **摘要复用拿错段** —— 检查点里的摘要覆盖到的消息 id 不在当前视图里(被 P-1 取代、被窗口裁掉)时必须不复用、按常规压缩。测试在 Task 5(`test_stale_cached_summary_is_ignored`)。
4. **旧版本回滚** —— 新字段取默认值时不能出现在 `model_dump(mode="json")` 里。测试在 Task 1(`test_new_fields_omitted_at_default`)。
5. **工具结果 content 是列表(多模态)** —— 不能崩、不能清。测试在 Task 2(`test_non_string_content_untouched`)。

## PR 切分

| PR | Tasks | 内容 |
|---|---|---|
| PR1 | 1、2、3 | 配置字段 + 跨轮无损清理 + 接线与观测 |
| PR2 | 4、5 | 有损步骤绝对上限 + 摘要写回复用 |
| — | 6 | 测试环境真栈验收(控制方执行,两 PR 合入并发测试环境后) |

---

### Task 1: Manifest 配置字段

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py`(`ContextCompressionPolicy` ~849、`WorkingMemoryPolicy` ~903、`ToolResultPrunePolicy` ~951)
- Test: `packages/expert-work-protocol/tests/test_agent_spec.py`

**Interfaces:**
- Produces:
  - `ToolResultPrunePolicy.cross_turn: bool = True`
  - `ToolResultPrunePolicy.min_context_tokens: int = 30_000`(`ge=0`)
  - `ToolResultPrunePolicy.min_reclaim_tokens: int = 5_000`(`ge=0`)
  - `ToolResultPrunePolicy.absolute_cap_tokens: int = 200_000`(`gt=0`)
  - `WorkingMemoryPolicy.absolute_cap_tokens: int = 200_000`(`gt=0`)
  - `ContextCompressionPolicy.absolute_cap_tokens: int = 200_000`(`gt=0`)
  - 以上字段取默认值时不出现在 `model_dump()` 输出里

- [ ] **Step 1: 写失败测试**

追加到 `packages/expert-work-protocol/tests/test_agent_spec.py`:

```python
from expert_work.protocol.agent_spec import (
    ContextCompressionPolicy,
    ToolResultPrunePolicy,
    WorkingMemoryPolicy,
)

_B126_DEFAULTS = {
    "cross_turn": True,
    "min_context_tokens": 30_000,
    "min_reclaim_tokens": 5_000,
    "absolute_cap_tokens": 200_000,
}


def test_b126_prune_policy_defaults() -> None:
    p = ToolResultPrunePolicy()
    for k, v in _B126_DEFAULTS.items():
        assert getattr(p, k) == v


def test_b126_lossy_gate_caps_default() -> None:
    assert WorkingMemoryPolicy().absolute_cap_tokens == 200_000
    assert ContextCompressionPolicy().absolute_cap_tokens == 200_000


def test_new_fields_omitted_at_default() -> None:
    # 回滚纪律:旧版本 extra="forbid" 读到新字段会拒 —— 默认值不能落库。
    for model in (ToolResultPrunePolicy(), WorkingMemoryPolicy(), ContextCompressionPolicy()):
        dumped = model.model_dump(mode="json")
        for k in _B126_DEFAULTS:
            assert k not in dumped, f"{type(model).__name__}.{k} leaked at default"


def test_new_fields_kept_when_non_default() -> None:
    dumped = ToolResultPrunePolicy(cross_turn=False, min_reclaim_tokens=1).model_dump(mode="json")
    assert dumped["cross_turn"] is False
    assert dumped["min_reclaim_tokens"] == 1
    assert "min_context_tokens" not in dumped
    assert WorkingMemoryPolicy(absolute_cap_tokens=64_000).model_dump(mode="json")[
        "absolute_cap_tokens"
    ] == 64_000


def test_new_fields_validate() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ToolResultPrunePolicy(absolute_cap_tokens=0)
    with pytest.raises(ValidationError):
        ToolResultPrunePolicy(min_reclaim_tokens=-1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest packages/expert-work-protocol/tests/test_agent_spec.py -q -k "b126 or new_fields"`
Expected: FAIL(`AttributeError` / 字段不存在)

- [ ] **Step 3: 实现**

在 `agent_spec.py` 顶部确认已有 `from pydantic import ... model_serializer, SerializerFunctionWrapHandler`(`PromptVariableSpec` 已在用;没有就补)。模块级加:

```python
#: B-126 —— 新增字段取默认值时不落库(回滚纪律,同 ``PromptVariableSpec._omit_default_render``)。
_B126_OMIT_AT_DEFAULT: dict[str, object] = {
    "cross_turn": True,
    "min_context_tokens": 30_000,
    "min_reclaim_tokens": 5_000,
    "absolute_cap_tokens": 200_000,
}


def _omit_b126_defaults(data: dict[str, Any]) -> dict[str, Any]:
    for key, default in _B126_OMIT_AT_DEFAULT.items():
        if key in data and data[key] == default:
            data.pop(key)
    return data
```

`ToolResultPrunePolicy` 追加字段与序列化器:

```python
    #: B-126 —— 新一轮开头无损收起更早轮次的大块工具结果(见 spec §3.2)。
    cross_turn: bool = True
    #: 本轮开始时(旧轮次 + 本轮用户消息)估算低于此值不清。
    min_context_tokens: int = Field(default=30_000, ge=0)
    #: 这一次能省的合计低于此值不清(不值得打断缓存)。
    min_reclaim_tokens: int = Field(default=5_000, ge=0)
    #: 同一轮内逐次清理的兜底门槛 = min(context_window × threshold_pct, 本值)。
    absolute_cap_tokens: int = Field(default=200_000, gt=0)

    @model_serializer(mode="wrap")
    def _omit_default_b126(  # type: ignore[no-untyped-def]
        self, handler: SerializerFunctionWrapHandler
    ):
        """默认值不落库(B-126,回滚纪律)。"""
        return _omit_b126_defaults(handler(self))
```

`WorkingMemoryPolicy` 与 `ContextCompressionPolicy` 各追加:

```python
    #: B-126 —— 门槛 = min(context_window × threshold_pct, 本值);大窗口模型不再等到 70%。
    absolute_cap_tokens: int = Field(default=200_000, gt=0)

    @model_serializer(mode="wrap")
    def _omit_default_b126(  # type: ignore[no-untyped-def]
        self, handler: SerializerFunctionWrapHandler
    ):
        """默认值不落库(B-126,回滚纪律)。"""
        return _omit_b126_defaults(handler(self))
```

- [ ] **Step 4: 跑测试确认通过,并跑本包全量**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest packages/expert-work-protocol/tests -q`
Expected: PASS(含既有 spec sha / JSON schema 快照测试;若有 JSON schema 快照因新字段变化而红,按快照测试自身的更新说明重生成,并在提交信息里注明)

- [ ] **Step 5: 自证咬得住**

临时删掉 `ToolResultPrunePolicy._omit_default_b126` → `test_new_fields_omitted_at_default` 必须红;`git diff` 确认删除落地;恢复 → 绿;`git diff` 为空。

- [ ] **Step 6: Commit**

```bash
git add packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py packages/expert-work-protocol/tests/test_agent_spec.py
git commit -m "feat(protocol): B-126 cross-turn prune + absolute-cap policy fields (omitted at default)"
```

---

### Task 2: 跨轮无损清理(纯函数)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/context/tool_result_prune.py`
- Modify: `services/orchestrator/src/orchestrator/context/__init__.py`(导出)
- Test: `services/orchestrator/tests/test_tool_result_prune_cross_turn.py`(新建)

**Interfaces:**
- Consumes: 现有 `PruneResult`、`_is_already_pruned`、`_artifact_path`、`skill_view_reference`、`OVERFLOW_FOOTER_TAG_OPEN`、`render_overflow_footer`、`estimate_tokens`、`is_hidden`(`expert_work.common.conversation_channel`)
- Produces:
  - `PruneResult` 增加字段 `reclaimed_tokens: int = 0`
  - `current_turn_start(messages: Sequence[BaseMessage]) -> int`
  - `prune_prior_turns(messages: Sequence[BaseMessage], *, recent_tool_results_kept: int, min_context_tokens: int, min_reclaim_tokens: int, estimator: TokenEstimator | None = None) -> PruneResult`

- [ ] **Step 1: 写失败测试**

新建 `services/orchestrator/tests/test_tool_result_prune_cross_turn.py`:

```python
"""B-126 —— 跨轮无损清理:只看本轮开始前的历史,同一轮内逐字不变。

估算用 legacy ``chars // 4``(estimator=None),门槛取小值让几 KB 内容就能过线。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI
from orchestrator.context import current_turn_start, prune_prior_turns
from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY

_BIG = "x" * 8000  # 2000 tokens via chars // 4


def _call(cid: str, name: str = "form_get_field_detail", args: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {"form_code": cid}, "id": cid, "type": "tool_call"}],
    )


def _res(cid: str, content: object = None, *, persisted: bool = True, name: str = "form_get_field_detail", status: str = "success") -> ToolMessage:
    art = {TOOL_RESULT_PATH_ARTIFACT_KEY: f".tool_results/run-1/{cid}-{name}.txt"} if persisted else None
    return ToolMessage(
        content=f"{_BIG}#{cid}" if content is None else content,
        tool_call_id=cid,
        name=name,
        artifact=art,
        status=status,
    )


def _turn(tag: str, n: int, **kw: object) -> list[BaseMessage]:
    out: list[BaseMessage] = [HumanMessage(content=f"user {tag}")]
    for i in range(n):
        cid = f"{tag}-{i}"
        out += [_call(cid), _res(cid, **kw)]  # type: ignore[arg-type]
    out.append(AIMessage(content=f"answer {tag}"))
    return out


def _run(msgs: list[BaseMessage], **kw: int):
    params = {"recent_tool_results_kept": 1, "min_context_tokens": 100, "min_reclaim_tokens": 100}
    params.update(kw)
    return prune_prior_turns(msgs, **params)


def test_current_turn_start_is_last_real_human() -> None:
    msgs = [*_turn("a", 1), HumanMessage(content="now")]
    assert current_turn_start(msgs) == len(msgs) - 1


def test_hidden_human_is_not_a_turn_boundary() -> None:
    hidden = HumanMessage(content="<inputs/>", additional_kwargs={HIDE_FROM_UI: True})
    msgs = [*_turn("a", 1), HumanMessage(content="now"), hidden, _call("b-0"), _res("b-0")]
    assert current_turn_start(msgs) == len(_turn("a", 1))


def test_prior_turn_results_collapse_current_turn_untouched() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 2)]
    r = _run(msgs)
    tools = [m for m in r.messages if isinstance(m, ToolMessage)]
    # 旧轮 a 的 3 条中最近 1 条受保护 → 清 2 条;本轮 b 的 2 条一律不动。
    assert r.pruned_count == 2
    assert tools[0].content.startswith("<tool-result-pruned>")
    assert tools[1].content.startswith("<tool-result-pruned>")
    assert tools[2].content.startswith(_BIG)
    assert all(t.content.startswith(_BIG) for t in tools[3:])
    assert r.reclaimed_tokens > 0


def test_stub_names_tool_args_and_recovery_path() -> None:
    r = _run([*_turn("a", 2), *_turn("b", 1)])
    stub = next(m for m in r.messages if isinstance(m, ToolMessage)).content
    assert "form_get_field_detail" in stub
    assert '"form_code": "a-0"' in stub
    assert ".tool_results/run-1/a-0-form_get_field_detail.txt" in stub


def test_view_is_identical_across_calls_in_a_turn() -> None:
    base = [*_turn("a", 3), HumanMessage(content="now"), _call("b-0"), _res("b-0")]
    grown = [*base, _call("b-1"), _res("b-1"), _call("b-2"), _res("b-2")]
    r1, r2 = _run(base), _run(grown)
    assert r1.messages == r2.messages[: len(r1.messages)]


def test_no_prior_turn_is_noop() -> None:
    msgs = _turn("a", 5)
    r = _run(msgs)
    assert r.pruned_count == 0 and r.messages == msgs


def test_under_min_context_is_noop() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs, min_context_tokens=10**9)
    assert r.pruned_count == 0 and r.messages == msgs


def test_under_min_reclaim_is_noop() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs, min_reclaim_tokens=10**9)
    assert r.pruned_count == 0 and r.messages == msgs


def test_min_context_counts_only_turn_start() -> None:
    # 本轮自己很大,但旧轮 + 本轮用户消息很小 → 不清(R2)。
    small_prior = [HumanMessage(content="hi"), _call("a-0"), _res("a-0", "tiny", persisted=True)]
    msgs = [*small_prior, HumanMessage(content="now"), *[m for i in range(5) for m in (_call(f"b-{i}"), _res(f"b-{i}"))]]
    r = _run(msgs, min_context_tokens=1000)
    assert r.pruned_count == 0


def test_no_recovery_path_is_never_pruned() -> None:
    msgs = [*_turn("a", 3, persisted=False), *_turn("b", 1)]
    r = _run(msgs)
    assert r.pruned_count == 0


def test_error_results_are_never_pruned() -> None:
    msgs = [*_turn("a", 3, status="error"), *_turn("b", 1)]
    r = _run(msgs)
    assert r.pruned_count == 0


def test_non_string_content_untouched() -> None:
    prior = [HumanMessage(content="u"), _call("a-0"), _res("a-0", [{"type": "text", "text": _BIG}]), _call("a-1"), _res("a-1")]
    r = _run([*prior, *_turn("b", 1)], recent_tool_results_kept=0)
    tools = [m for m in r.messages if isinstance(m, ToolMessage)]
    assert isinstance(tools[0].content, list)


def test_assistant_messages_never_touched() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs)
    before = [m.content for m in msgs if isinstance(m, AIMessage)]
    after = [m.content for m in r.messages if isinstance(m, AIMessage)]
    assert before == after


def test_pairing_and_ids_preserved() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    r = _run(msgs)
    assert len(r.messages) == len(msgs)
    for a, b in zip(msgs, r.messages, strict=True):
        assert type(a) is type(b)
        if isinstance(a, ToolMessage):
            assert (a.tool_call_id, a.name, a.id) == (b.tool_call_id, b.name, b.id)


def test_idempotent() -> None:
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    once = _run(msgs).messages
    twice = _run(once)
    assert twice.messages == once and twice.pruned_count == 0


def test_stubs_only_reference_paths_already_in_history() -> None:
    # R1 —— 占位引用的路径必须来自被清的那条消息自身(本对话历史),不引入外来路径。
    msgs = [*_turn("a", 3), *_turn("b", 1)]
    own_paths = {m.artifact[TOOL_RESULT_PATH_ARTIFACT_KEY] for m in msgs if isinstance(m, ToolMessage) and m.artifact}
    for m in _run(msgs).messages:
        if isinstance(m, ToolMessage) and m.content.startswith("<tool-result-pruned>"):
            assert any(p in m.content for p in own_paths)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests/test_tool_result_prune_cross_turn.py -q`
Expected: FAIL(`ImportError: cannot import name 'current_turn_start'`)

- [ ] **Step 3: 实现**

`tool_result_prune.py`:

1. import 追加:

```python
import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from expert_work.common.conversation_channel import is_hidden
```

2. `PruneResult` 加字段(frozen dataclass,放在 `pruned_count` 之后):

```python
    #: B-126 —— 本次跨轮清理估算省下的 token(观测用;非跨轮路径为 0)。
    reclaimed_tokens: int = 0
```

3. 追加函数(放在 `prune_old_tool_results` 之后、`ToolResultPruner` 之前):

```python
_ARGS_HINT_LIMIT = 120


def current_turn_start(messages: Sequence[BaseMessage]) -> int:
    """B-126 —— 本轮起点 = 最后一条**真实**用户消息的下标;没有则 0。

    平台注入的隐藏 HumanMessage(本轮输入块 / 计划 / 工作区摘要 …)带
    ``HIDE_FROM_UI``,不算边界 —— 与 CM-2 ``trim_to_recent_turns`` 同一口径。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and not is_hidden(m):
            return i
    return 0


def _args_hint(args: Mapping[str, Any]) -> str:
    text = json.dumps(dict(args), ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= _ARGS_HINT_LIMIT else text[: _ARGS_HINT_LIMIT - 1] + "…"


def _lossless_reference(message: ToolMessage) -> str | None:
    """无损找回途径 —— 技能引用 / 外置 footer / 持久化路径;都没有则 ``None``。"""
    reference = skill_view_reference(message)
    if reference is not None:
        return reference
    content = message.content
    if isinstance(content, str):
        footer_at = content.find(OVERFLOW_FOOTER_TAG_OPEN)
        if footer_at != -1:
            return content[footer_at:].strip()
    path = _artifact_path(message)
    if path is not None and isinstance(content, str):
        return render_overflow_footer(rel=path, total_chars=len(content)).strip()
    return None


def _cross_turn_stub(message: ToolMessage, *, args: Mapping[str, Any] | None, reference: str) -> str:
    content = message.content
    size = len(content) if isinstance(content, str) else 0
    head = f"[{message.name or 'tool'}]"
    if args:
        head += f" {_args_hint(args)}"
    return (
        f"{_PRUNE_TAG_OPEN}\n"
        f"{head} — {size:,} chars from an earlier turn, elided; recover below if needed.\n"
        f"{reference}\n"
        f"{_PRUNE_TAG_CLOSE}"
    )


def prune_prior_turns(
    messages: Sequence[BaseMessage],
    *,
    recent_tool_results_kept: int,
    min_context_tokens: int,
    min_reclaim_tokens: int,
    estimator: TokenEstimator | None = None,
) -> PruneResult:
    """B-126 —— 新一轮开头无损收起更早轮次的大块工具结果。

    决定只依赖 ``messages[:boundary + 1]``(旧轮次 + 本轮用户消息),所以同一轮内
    每次调用结果逐字相同、缓存前缀稳定(spec D3 / R2)。只收有无损找回途径的结果
    (D6),``status == "error"`` 与非字符串内容一律不动(R4)。去重只在旧轮次内部做
    (R3)。返回新列表,不改入参。
    """
    msgs = list(messages)
    boundary = current_turn_start(msgs)
    if boundary == 0:
        return PruneResult(messages=msgs, pruned_count=0)
    if estimate_tokens(msgs[: boundary + 1], estimator=estimator) < min_context_tokens:
        return PruneResult(messages=msgs, pruned_count=0)

    prior_tool_idxs = [i for i in range(boundary) if isinstance(msgs[i], ToolMessage)]
    protected = set(prior_tool_idxs[max(0, len(prior_tool_idxs) - recent_tool_results_kept) :])
    args_by_call: dict[str, Mapping[str, Any]] = {}
    for i in range(boundary):
        m = msgs[i]
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                if tc.get("id"):
                    args_by_call[tc["id"]] = tc.get("args") or {}
    last_occurrence: dict[str, int] = {}
    for i in prior_tool_idxs:
        c = msgs[i].content
        if isinstance(c, str):
            last_occurrence[c] = i

    replacements: dict[int, ToolMessage] = {}
    reclaimed = 0
    for i in prior_tool_idxs:
        m = msgs[i]
        assert isinstance(m, ToolMessage)
        content = m.content
        if not isinstance(content, str) or _is_already_pruned(content):
            continue
        if getattr(m, "status", "success") == "error":
            continue
        is_duplicate = last_occurrence.get(content, i) > i
        if i in protected and not is_duplicate:
            continue
        reference = _lossless_reference(m)
        if reference is None:
            continue
        stub = _cross_turn_stub(m, args=args_by_call.get(m.tool_call_id), reference=reference)
        new = _rebuild(m, stub)
        saved = estimate_tokens([m], estimator=estimator) - estimate_tokens([new], estimator=estimator)
        if saved <= 0:
            continue
        replacements[i] = new
        reclaimed += saved

    if not replacements or reclaimed < min_reclaim_tokens:
        return PruneResult(messages=msgs, pruned_count=0)
    out = [replacements.get(i, m) for i, m in enumerate(msgs)]
    return PruneResult(messages=out, pruned_count=len(replacements), reclaimed_tokens=reclaimed)
```

4. `context/__init__.py` 按现有风格追加导出:

```python
from orchestrator.context.tool_result_prune import (
    current_turn_start as current_turn_start,
)
from orchestrator.context.tool_result_prune import (
    prune_prior_turns as prune_prior_turns,
)
```

并把 `"current_turn_start"`、`"prune_prior_turns"` 加进 `__all__`。

- [ ] **Step 4: 跑测试确认通过 + 既有 CM-12 测试不回归**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests/test_tool_result_prune_cross_turn.py services/orchestrator/tests/test_tool_result_prune.py -q`
Expected: PASS

- [ ] **Step 5: 自证咬得住(逐条,每次 git diff 确认落地与还原)**

- 把 `current_turn_start` 里的 `and not is_hidden(m)` 删掉 → `test_hidden_human_is_not_a_turn_boundary` 红。
- 把 `estimate_tokens(msgs[: boundary + 1], …)` 改成 `estimate_tokens(msgs, …)` → `test_min_context_counts_only_turn_start` 红。
- 把 `if reference is None: continue` 改成用有损 stub → `test_no_recovery_path_is_never_pruned` 红。
- 删掉 `status == "error"` 那两行 → `test_error_results_are_never_pruned` 红。
- 把 `protected` 改成按整个 `msgs` 的工具结果计算 → `test_view_is_identical_across_calls_in_a_turn` 红。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/context/tool_result_prune.py services/orchestrator/src/orchestrator/context/__init__.py services/orchestrator/tests/test_tool_result_prune_cross_turn.py
git commit -m "feat(context): B-126 cross-turn lossless prune of prior turns' tool results"
```

---

### Task 3: 接线、兜底门槛与观测

**Files:**
- Modify: `services/orchestrator/src/orchestrator/context/tool_result_prune.py`(`ToolResultPruner`)
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py`(~1036-1046 CM-12 构造)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`(~338 指标区、~716-719 `context_gates` span)
- Test: `services/orchestrator/tests/test_tool_result_prune.py`(追加)、`services/orchestrator/tests/test_tool_result_prune_cross_turn_wiring.py`(新建)

**Interfaces:**
- Consumes: Task 1 字段、Task 2 `prune_prior_turns` / `PruneResult.reclaimed_tokens`
- Produces:
  - `ToolResultPruner` 新字段:`cross_turn: bool = True`、`min_context_tokens: int = 30_000`、`min_reclaim_tokens: int = 5_000`、`absolute_cap_tokens: int = 200_000`
  - `ToolResultPruner.threshold_tokens == min(int(context_window * threshold_pct), absolute_cap_tokens)`
  - `ToolResultPruner.apply()` 先跨轮、再兜底;返回的 `PruneResult.pruned_count` 为两者之和、`reclaimed_tokens` 为跨轮那部分

- [ ] **Step 1: 写失败测试(单元)**

追加到 `services/orchestrator/tests/test_tool_result_prune.py`:

```python
def test_b126_threshold_is_min_of_pct_and_cap() -> None:
    p = ToolResultPruner(context_window=1_000_000, threshold_pct=0.7, absolute_cap_tokens=200_000)
    assert p.threshold_tokens == 200_000
    small = ToolResultPruner(context_window=100_000, threshold_pct=0.7, absolute_cap_tokens=200_000)
    assert small.threshold_tokens == 70_000


def test_b126_cross_turn_runs_under_legacy_threshold() -> None:
    from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY as K

    msgs: list[BaseMessage] = [HumanMessage(content="first")]
    for i in range(3):
        msgs += [_ai_call(f"a{i}"), _tool(f"{_BIG}#{i}", call_id=f"a{i}", artifact={K: f".tool_results/r/a{i}.txt"})]
    msgs += [AIMessage(content="ok"), HumanMessage(content="second")]
    p = ToolResultPruner(
        context_window=10**9, recent_tool_results_kept=1,
        min_context_tokens=100, min_reclaim_tokens=100,
    )
    r = p.apply(msgs)
    assert r.pruned_count == 2 and r.reclaimed_tokens > 0


def test_b126_cross_turn_off_restores_legacy() -> None:
    from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY as K

    msgs: list[BaseMessage] = [HumanMessage(content="first")]
    for i in range(3):
        msgs += [_ai_call(f"a{i}"), _tool(f"{_BIG}#{i}", call_id=f"a{i}", artifact={K: f".tool_results/r/a{i}.txt"})]
    msgs += [HumanMessage(content="second")]
    p = ToolResultPruner(context_window=10**9, cross_turn=False, min_context_tokens=0, min_reclaim_tokens=0)
    assert p.apply(msgs).pruned_count == 0
```

- [ ] **Step 2: 写失败测试(图级)**

新建 `services/orchestrator/tests/test_tool_result_prune_cross_turn_wiring.py`(结构照 `test_working_window_wiring.py`):

```python
"""B-126 —— 跨轮清理经编译图生效:第 2 轮起旧结果收起;同一轮内各次调用前缀逐字
相同;检查点历史完整;两段对话互不引用对方路径。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.context import ToolResultPruner
from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY

_BIG = "q" * 8000


@dataclass
class _RecordingLLM:
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]) -> AIMessage:
        del tools
        self.seen.append(list(messages))
        return AIMessage(content="done")


def _prior(tag: str, n: int) -> list[BaseMessage]:
    out: list[BaseMessage] = [HumanMessage(content=f"user {tag}")]
    for i in range(n):
        cid = f"{tag}-{i}"
        out.append(AIMessage(content="", tool_calls=[{"id": cid, "name": "mcp_x", "args": {"k": cid}}]))
        out.append(ToolMessage(
            content=f"{_BIG}#{tag}{i}", tool_call_id=cid, name="mcp_x",
            artifact={TOOL_RESULT_PATH_ARTIFACT_KEY: f".tool_results/{tag}/{cid}.txt"},
        ))
    out.append(AIMessage(content=f"answer {tag}"))
    return out


def _pruner() -> ToolResultPruner:
    return ToolResultPruner(
        context_window=10**9, recent_tool_results_kept=1, min_context_tokens=100, min_reclaim_tokens=100,
    )


async def _invoke(llm: _RecordingLLM, history: list[BaseMessage], thread: str):
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry(), tool_result_pruner=_pruner())
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": thread}}
        return await compiled.ainvoke({"messages": history, "step_count": 0, "max_steps": 5}, config=cfg)


@pytest.mark.asyncio
async def test_prior_turn_collapsed_in_prompt_checkpoint_intact() -> None:
    history = [*_prior("a", 3), HumanMessage(content="now")]
    llm = _RecordingLLM()
    state = await _invoke(llm, history, str(uuid4()))
    prompt_tools = [m for m in llm.seen[0] if isinstance(m, ToolMessage)]
    assert sum(1 for m in prompt_tools if str(m.content).startswith("<tool-result-pruned>")) == 2
    saved_tools = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert all(str(m.content).startswith(_BIG) for m in saved_tools)


@pytest.mark.asyncio
async def test_two_threads_never_reference_each_other() -> None:
    llm_a, llm_b = _RecordingLLM(), _RecordingLLM()
    await _invoke(llm_a, [*_prior("custA", 3), HumanMessage(content="now")], "thread-A")
    await _invoke(llm_b, [*_prior("custB", 3), HumanMessage(content="now")], "thread-B")
    text_b = "\n".join(str(m.content) for m in llm_b.seen[0])
    assert "custA" not in text_b
```

同一轮内前缀稳定已由 Task 2 `test_view_is_identical_across_calls_in_a_turn` 在纯函数层钉住;图级再加一条需要带工具的多步脚本 LLM,放在 Task 6 真栈里用缓存命中率验。

- [ ] **Step 3: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests/test_tool_result_prune.py services/orchestrator/tests/test_tool_result_prune_cross_turn_wiring.py -q -k "b126 or prior_turn or two_threads"`
Expected: FAIL(`TypeError: unexpected keyword 'absolute_cap_tokens'` 等)

- [ ] **Step 4: 实现 `ToolResultPruner`**

替换 `ToolResultPruner` 的字段、`threshold_tokens`、`apply`:

```python
    context_window: int
    threshold_pct: float = 0.7
    recent_tool_results_kept: int = 4
    estimator: TokenEstimator | None = None
    #: B-126 —— 跨轮无损清理与兜底门槛(见 ``ToolResultPrunePolicy``)。
    cross_turn: bool = True
    min_context_tokens: int = 30_000
    min_reclaim_tokens: int = 5_000
    absolute_cap_tokens: int = 200_000

    @property
    def threshold_tokens(self) -> int:
        """同一轮内逐次清理的兜底门槛 = min(窗口 × 百分比, 绝对上限)(B-126)。"""
        return min(int(self.context_window * self.threshold_pct), self.absolute_cap_tokens)

    def should_prune(self, messages: Sequence[BaseMessage]) -> bool:
        return estimate_tokens(messages, estimator=self.estimator) >= self.threshold_tokens

    def apply(self, messages: Sequence[BaseMessage]) -> PruneResult:
        """先跨轮无损清理(新一轮开头),再按兜底门槛逐次清理。"""
        msgs = list(messages)
        pruned = 0
        reclaimed = 0
        if self.cross_turn:
            cross = prune_prior_turns(
                msgs,
                recent_tool_results_kept=self.recent_tool_results_kept,
                min_context_tokens=self.min_context_tokens,
                min_reclaim_tokens=self.min_reclaim_tokens,
                estimator=self.estimator,
            )
            msgs, pruned, reclaimed = cross.messages, cross.pruned_count, cross.reclaimed_tokens
            if pruned:
                logger.info(
                    "tool_result_prune.cross_turn count=%d reclaimed_tokens=%d", pruned, reclaimed
                )
        if not self.should_prune(msgs):
            return PruneResult(messages=msgs, pruned_count=pruned, reclaimed_tokens=reclaimed)
        result = prune_old_tool_results(msgs, recent_tool_results_kept=self.recent_tool_results_kept)
        if result.pruned_count:
            logger.info(
                "tool_result_prune.pruned count=%d kept=%d",
                result.pruned_count,
                self.recent_tool_results_kept,
            )
        return PruneResult(
            messages=result.messages,
            pruned_count=pruned + result.pruned_count,
            reclaimed_tokens=reclaimed,
        )
```

并更新模块 docstring 的「Token-gated」一条:补一句「B-126:跨轮部分不看窗口,按本轮开始时的绝对量 + 最小清理量触发」。

- [ ] **Step 5: 工厂接线**

`agent_factory.py` CM-12 构造处改为:

```python
        tool_result_pruner = ToolResultPruner(
            context_window=_resolved_context_window(spec.spec.model),
            threshold_pct=trp_policy.threshold_pct,
            recent_tool_results_kept=trp_policy.recent_tool_results_kept,
            estimator=estimator,
            cross_turn=trp_policy.cross_turn,
            min_context_tokens=trp_policy.min_context_tokens,
            min_reclaim_tokens=trp_policy.min_reclaim_tokens,
            absolute_cap_tokens=trp_policy.absolute_cap_tokens,
        )
```

- [ ] **Step 6: 观测**

`builder.py` 指标区(`_cm_working_window_dropped_turns` 之后)加:

```python
#: B-126 —— 跨轮无损清理省下的 token(估算)累计;配合 run 级日志算账。
_cm_cross_turn_reclaimed_tokens = expert_work_counter(
    "expert_work_cm_cross_turn_reclaimed_tokens_total",
    "Estimated tokens reclaimed by the cross-turn tool-result prune (B-126).",
)
```

`context_gates` span 改为 `with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "context_gates") as gates_span:`,pruner 那两行改为:

```python
            if tool_result_pruner is not None:
                pruned = tool_result_pruner.apply(messages)
                messages = pruned.messages
                if pruned.reclaimed_tokens:
                    _cm_cross_turn_reclaimed_tokens.inc(pruned.reclaimed_tokens)
                gates_span.set_attribute("cm.tool_result_prune.count", pruned.pruned_count)
                gates_span.set_attribute("cm.cross_turn.reclaimed_tokens", pruned.reclaimed_tokens)
```

- [ ] **Step 7: 跑测试 + orchestrator 上下文相关全量**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests -q -k "prune or window or compressor or context or factory"`
Expected: PASS

- [ ] **Step 8: 自证** —— 把 `apply` 里 `if self.cross_turn:` 改成 `if False:` → `test_b126_cross_turn_runs_under_legacy_threshold` 与 `test_prior_turn_collapsed_in_prompt_checkpoint_intact` 红;工厂里去掉 `cross_turn=` 等四行不会红(默认值相同)—— 为此在 `services/orchestrator/tests/` 里找到现有测 `ToolResultPruner` 工厂接线的测试(`git grep -n "tool_result_pruner" services/orchestrator/tests`),追加一条:manifest 配 `tool_result_prune: {min_reclaim_tokens: 7}` 时工厂产出的 pruner `min_reclaim_tokens == 7`,并用同样手法自证。

- [ ] **Step 9: Commit**

```bash
git add -A services/orchestrator
git commit -m "feat(context): B-126 wire cross-turn prune, 200k fallback cap, reclaim metric"
```

---

### Task 4: 有损步骤的绝对上限

**Files:**
- Modify: `services/orchestrator/src/orchestrator/context/working_window.py`(`WorkingWindow`)
- Modify: `services/orchestrator/src/orchestrator/context/compressor.py`(`ContextCompressor.threshold_tokens`)
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py`(~997-1020 两处构造)
- Test: `services/orchestrator/tests/test_working_window.py`、`services/orchestrator/tests/test_context_compressor.py`(追加)

**Interfaces:**
- Consumes: Task 1 `WorkingMemoryPolicy.absolute_cap_tokens`、`ContextCompressionPolicy.absolute_cap_tokens`
- Produces: `WorkingWindow.absolute_cap_tokens: int = 200_000`、`ContextCompressor.absolute_cap_tokens: int = 200_000`;两者 `threshold_tokens == min(int(context_window * threshold_pct), absolute_cap_tokens)`

- [ ] **Step 1: 写失败测试**

`test_working_window.py` 追加:

```python
def test_b126_window_threshold_capped() -> None:
    from orchestrator.context import WorkingWindow

    assert WorkingWindow(context_window=1_000_000).threshold_tokens == 200_000
    assert WorkingWindow(context_window=100_000).threshold_tokens == 70_000
    assert WorkingWindow(context_window=1_000_000, absolute_cap_tokens=64_000).threshold_tokens == 64_000
```

`test_context_compressor.py` 追加(沿用该文件已有的假 `llm_caller` 夹具;若无,用 `async def _never(**_): raise AssertionError`):

```python
def test_b126_compressor_threshold_capped() -> None:
    from orchestrator.context import ContextCompressor

    async def _never(**_: object):  # noqa: ANN202
        raise AssertionError("summariser must not be called")

    assert ContextCompressor(llm_caller=_never, context_window=1_000_000).threshold_tokens == 200_000
    assert ContextCompressor(llm_caller=_never, context_window=100_000).threshold_tokens == 70_000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests/test_working_window.py services/orchestrator/tests/test_context_compressor.py -q -k b126`
Expected: FAIL(`threshold_tokens == 700000`)

- [ ] **Step 3: 实现**

两个 dataclass 各加字段并改 `threshold_tokens`:

```python
    #: B-126 —— 门槛 = min(窗口 × 百分比, 本值);大窗口模型不再等到 70%。
    absolute_cap_tokens: int = 200_000

    @property
    def threshold_tokens(self) -> int:
        return min(int(self.context_window * self.threshold_pct), self.absolute_cap_tokens)
```

(`ContextCompressor` 的字段放在 `estimator` 之后、`_summary_failures` 之前 —— `_summary_failures` 是 `init=False`,不影响位置参数。)

`agent_factory.py` 两处构造各加 `absolute_cap_tokens=cc_policy.absolute_cap_tokens` / `absolute_cap_tokens=wm_policy.absolute_cap_tokens`。

- [ ] **Step 4: 跑测试 + 相关全量**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests -q -k "window or compressor or context_pressure or factory"`
Expected: PASS。`context_pressure`(0.75 × 窗口的预算提示)**不改**:它只加一句提示、不删内容;若有测试断言「压力提示阈值 > 压缩阈值」之类的相对关系而因本改动变红,在该测试里按新语义改断言并在提交信息写明。

- [ ] **Step 5: 自证** —— 把两处 `min(...)` 改回只取百分比 → 两条 b126 测试红;还原。

- [ ] **Step 6: Commit**

```bash
git add -A services/orchestrator
git commit -m "feat(context): B-126 cap lossy gates at min(window*pct, 200k)"
```

---

### Task 5: 摘要写回检查点并复用

**Files:**
- Modify: `services/orchestrator/src/orchestrator/state.py`(`AgentState`)
- Modify: `services/orchestrator/src/orchestrator/context/compressor.py`(`compress`、`_compress_once` 调用处)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`(~870-935 压缩调用、~1373 `update_mw`、~1420 `update_plain`)
- Test: `services/orchestrator/tests/test_context_compressor.py`(追加)、`services/orchestrator/tests/test_context_compressor_integration.py`(追加)

**Interfaces:**
- Produces:
  - `orchestrator.context.compressor.CachedSummary`(frozen dataclass:`through_id: str`、`text: str`),导出于 `orchestrator.context`
  - `ContextCompressor.compress(..., cached_summary: CachedSummary | None = None, on_summary: Callable[[CachedSummary], None] | None = None)`
  - `AgentState.context_summary: NotRequired[dict[str, str] | None]`(`{"through_id": ..., "text": ...}`,默认覆盖 reducer)

- [ ] **Step 1: 写失败测试(单元)**

`test_context_compressor.py` 追加:

```python
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from orchestrator.context import CachedSummary, ContextCompressor


def _conv(n: int, pad: int = 400) -> list:
    out = []
    for i in range(n):
        out.append(HumanMessage(content=f"u{i} " + "z" * pad, id=f"h{i}"))
        out.append(AIMessage(content=f"a{i} " + "z" * pad, id=f"a{i}"))
    return out


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *, messages, tools):  # noqa: ANN001
        del messages, tools
        self.calls += 1
        return AIMessage(content="SUMMARY")


@pytest.mark.asyncio
async def test_summary_is_reported_with_through_id() -> None:
    llm = _CountingLLM()
    got: list[CachedSummary] = []
    c = ContextCompressor(llm_caller=llm, context_window=1000, threshold_pct=0.5, head_keep=1, tail_keep=2)
    await c.compress(_conv(10), on_summary=got.append)
    assert llm.calls == 1
    assert got and got[-1].through_id and "SUMMARY" in got[-1].text


@pytest.mark.asyncio
async def test_cached_summary_reused_without_llm_call() -> None:
    llm = _CountingLLM()
    got: list[CachedSummary] = []
    c = ContextCompressor(llm_caller=llm, context_window=1000, threshold_pct=0.5, head_keep=1, tail_keep=2)
    msgs = _conv(10)
    await c.compress(msgs, on_summary=got.append)
    llm.calls = 0
    await c.compress(msgs, cached_summary=got[-1])
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_stale_cached_summary_is_ignored() -> None:
    llm = _CountingLLM()
    c = ContextCompressor(llm_caller=llm, context_window=1000, threshold_pct=0.5, head_keep=1, tail_keep=2)
    stale = CachedSummary(through_id="not-in-view", text="<context-summary>OLD</context-summary>")
    out = await c.compress(_conv(10), cached_summary=stale)
    assert llm.calls == 1
    assert "OLD" not in "\n".join(str(m.content) for m in out)
```

(`threshold_pct=0.5` / `pad=400` 按 legacy `chars // 4` 估:10 轮约 2000 token > 500;复用后「头 1 + 摘要 + 尾 2」约 400 token < 500。实现时若这组数与该文件里已有夹具的估算口径对不上,以「第一次必须压、复用后必须在门槛下」为准调 pad,不改断言。)

- [ ] **Step 2: 写失败测试(图级)**

`test_context_compressor_integration.py` 追加一条:同一 thread 连跑两轮超门槛对话,第二轮 `llm_caller` 的摘要调用次数为 0(复用),且 `state["context_summary"]["through_id"]` 非空。沿用该文件现有的编译图 / 计数 LLM 写法;第二轮只在输入里多加一条短 HumanMessage,使超门槛部分仍全在已摘要段内。

- [ ] **Step 3: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests/test_context_compressor.py services/orchestrator/tests/test_context_compressor_integration.py -q -k "summary"`
Expected: FAIL(`ImportError: CachedSummary`)

- [ ] **Step 4: 实现 compressor**

`compressor.py` 追加:

```python
@dataclass(frozen=True)
class CachedSummary:
    """B-126 —— 写回检查点的滚动摘要:``text`` 是整段 ``<context-summary>`` SystemMessage
    内容,``through_id`` 是它覆盖到的最后一条消息的 id。"""

    through_id: str
    text: str


def _apply_cached_summary(
    messages: Sequence[BaseMessage], cached: CachedSummary, *, head_keep: int, tail_keep: int
) -> list[BaseMessage] | None:
    """把中段里 ``through_id`` 及之前的消息换成缓存摘要;``through_id`` 不在中段则返回 ``None``。"""
    split = _split(messages, head_keep=head_keep, tail_keep=tail_keep)
    ids = [getattr(m, "id", None) for m in split.middle]
    if cached.through_id not in ids:
        return None
    cut = ids.index(cached.through_id) + 1
    summary = SystemMessage(content=cached.text)
    return [*split.leading_systems, *split.head, summary, *split.middle[cut:], *split.tail]
```

`compress` 签名追加两个关键字参数 `cached_summary: CachedSummary | None = None`、`on_summary: Callable[[CachedSummary], None] | None = None`(`Callable` 从 `collections.abc` 导入)。在 `current: list[BaseMessage] = list(messages)` 之后插入:

```python
        if cached_summary is not None:
            applied = _apply_cached_summary(
                current, cached_summary, head_keep=self.head_keep, tail_keep=self.tail_keep
            )
            if applied is not None:
                current = applied
```

在循环里 `current = await self._compress_once(current, on_pre_compaction=on_pre_compaction)` 这一行**之前**记下将被摘要的中段末条 id,之后上报:

```python
            pending_through = _last_middle_id(current, head_keep=self.head_keep, tail_keep=self.tail_keep)
            current = await self._compress_once(current, on_pre_compaction=on_pre_compaction)
            if on_summary is not None and pending_through is not None:
                on_summary(CachedSummary(through_id=pending_through, text=_summary_text_of(current)))
```

辅助函数:

```python
def _last_middle_id(messages: Sequence[BaseMessage], *, head_keep: int, tail_keep: int) -> str | None:
    middle = _split(messages, head_keep=head_keep, tail_keep=tail_keep).middle
    for m in reversed(middle):
        mid = getattr(m, "id", None)
        if mid and not (isinstance(m, SystemMessage) and _SUMMARY_TAG_OPEN in str(m.content)):
            return str(mid)
    return None


def _summary_text_of(messages: Sequence[BaseMessage]) -> str:
    for m in messages:
        if isinstance(m, SystemMessage) and str(m.content).startswith(_SUMMARY_TAG_OPEN):
            return str(m.content)
    return ""
```

注意:复用时中段里那条缓存摘要 SystemMessage 没有 id;`_last_middle_id` 跳过摘要消息本身,所以更新模式(CM-7 `_extract_prior_summary`)下上报的 `through_id` 是新事件的末条 id —— 正是下一次要对齐的位置。

`context/__init__.py` 导出 `CachedSummary`。

- [ ] **Step 5: 实现状态通道与 agent_node 接线**

`state.py` `AgentState` 追加:

```python
    #: B-126 —— L2 滚动摘要写回检查点:``{"through_id": <消息 id>, "text": <整段摘要>}``。
    #: 下一次压缩时被覆盖的历史未变就直接复用,不再每步重写(spec §3.7)。默认覆盖 reducer。
    context_summary: NotRequired[dict[str, str] | None]
```

`builder.py` 压缩调用处(`messages = await context_compressor.compress(...)`)改为:

```python
            cached_raw = state.get("context_summary") or None
            cached = (
                CachedSummary(through_id=cached_raw["through_id"], text=cached_raw["text"])
                if cached_raw and cached_raw.get("through_id") and cached_raw.get("text")
                else None
            )
            new_summary: list[CachedSummary] = []
            messages = await context_compressor.compress(
                messages,
                on_pre_compaction=on_pre_compaction,
                on_compacted=on_compacted,
                streak_key=str(compress_thread_id) if compress_thread_id else None,
                reserved=reserved,
                cached_summary=cached,
                on_summary=new_summary.append,
            )
```

在 agent_node 作用域里、压缩分支之前初始化 `summary_update: dict[str, str] | None = None`,压缩后 `if new_summary: summary_update = {"through_id": new_summary[-1].through_id, "text": new_summary[-1].text}`。在 `update_mw` 与 `update_plain` 两处返回前(与 `demoted_tools` 同一位置)各加:

```python
            if summary_update is not None:
                update_mw["context_summary"] = summary_update
```

(`update_plain` 同理。)`from orchestrator.context import CachedSummary` 加到 builder 的既有 context import 里。

- [ ] **Step 6: 跑测试 + 相关全量**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest services/orchestrator/tests -q -k "compressor or context or checkpoint or state"`
Expected: PASS

- [ ] **Step 7: 自证** —— 删掉 `if cached_summary is not None:` 整块 → `test_cached_summary_reused_without_llm_call` 与图级复用测试红;把 `_apply_cached_summary` 的 `not in ids` 判断改成恒 False 会让 `test_stale_cached_summary_is_ignored` 红;各自还原并 `git diff` 确认。

- [ ] **Step 8: Commit**

```bash
git add -A services/orchestrator
git commit -m "feat(context): B-126 persist rolling summary in checkpoint and reuse it"
```

---

### Task 6: 测试环境真栈验收(控制方执行;生产由用户)

两个 PR 合入并发测试环境后执行。本任务不改业务代码,只改 ROADMAP。

- [ ] **Step 1: 核实 skill_view 预览问题(spec §7)** —— 在测试环境查 health-plan-report 技能被 `skill_view` 读 `reference/content-schema.md`(16,352 字)那次的 ToolMessage 长度与是否带 `<tool-result-overflow>` footer(用会话 scratchpad `accept/tlen.py` 同款脚本,对 thread `d3016385` 的 run `58d1cb1e`)。属实则 ROADMAP 另立缺陷行,不在本计划修。

- [ ] **Step 2: 对照组** —— 在 ai-health-plan 的 spec 上临时设 `policies.tool_result_prune.cross_turn=false`(脚本同 `ahp-opt/set_prompt39.py` 写法,备份当前 spec),用 `accept/rp39.py` 以新用户身份重放张女士对话 7 轮。

- [ ] **Step 3: 实验组** —— 恢复默认(删掉 `cross_turn` 字段),同输入再重放一次。

- [ ] **Step 4: 出表**(`accept/cost39.sql` + `ctx39.sql` 同款查询):

| 指标 | 通过标准 |
|---|---|
| 第 3 轮起每轮首次调用输入 token | 明显下降(预计 7~9 万 → 3~4 万) |
| 7 轮总成本 | 比对照组降 ≥30% |
| 同一轮内缓存命中率 | 不低于对照组 |
| 成品 | 质检全过,封面 / 客户信息 / 内容与对照组一致 |
| 找回次数 | 统计 `read_file .tool_results/…` 与重复 MCP 调用次数,不得把省下的吃回去 |

- [ ] **Step 5: 串客户专项** —— 同一员工身份,先出张女士方案,再开新对话出另一测试客户方案;检查后者 run_event 的全部 prompt 与交付件不含张女士称呼 / 指标 / `.tool_results/<张女士的 run>` 路径。

- [ ] **Step 6: 回归** —— 金丝雀 Agent 跑一次、任一 sop2 类 Agent 跑一次,PASS。

- [ ] **Step 7: 给深护智康 MCP 团队** —— 把 spec 附录五整理成一页说明发给用户转交(`form_get_field_detail` 可选 `detail_level=brief`,不传行为不变)。

- [ ] **Step 8: ROADMAP** —— `docs/superpowers/ROADMAP.md` 加 **B-126** 行:spec / 计划路径、PR 号、测试环境对照数据与日期、「生产待用户发令」。

```bash
git add docs/superpowers/ROADMAP.md
git commit -m "docs(roadmap): B-126 cross-turn context pruning — test-env acceptance"
```

---

## Self-Review(计划作者已做)

- **Spec 覆盖**:D1 → Task 1 默认 + Task 3 开关;D2 → Task 3 图级「检查点完整」;D3 / R2 → Task 2 两条确定性测试;D4 → 无任务(不做);D5 → Task 2 `min_context_tokens` / Task 3 / Task 4;D6 → Task 2 `_lossless_reference`;D7 → R1 + Task 2 `test_stubs_only_reference_paths_already_in_history` + Task 3 两对话测试;D8 → Task 5;D9 → Task 6 Step 7;§3.9 观测 → Task 3 Step 6;§4.3 真栈 → Task 6;§7 待核实 → Task 6 Step 1 与 Review Focus 1。
- **占位扫描**:Task 5 Step 2 与 Task 3 Step 8 的图级 / 工厂测试给了断言要点与参照文件,未贴全代码 —— 这两处的夹具写法取决于既有测试文件的现成 helper,照抄现有结构比凭空写更不易错;断言本身已写明。
- **类型一致**:`prune_prior_turns(...) -> PruneResult`、`PruneResult.reclaimed_tokens`、`ToolResultPruner` 四个新字段、`CachedSummary(through_id, text)`、`compress(..., cached_summary=, on_summary=)`、`AgentState.context_summary` 在定义处与使用处一致。
