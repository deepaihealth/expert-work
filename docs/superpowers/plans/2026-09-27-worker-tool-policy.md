# Worker Tool Policy (B-122) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make dynamic worker sub-agents unable to deliver, self-modify, or write outside the workspace, as a platform default that needs no per-agent prompt text.

**Architecture:** A single policy module (`orchestrator/tools/worker_policy.py`) owns the deny list, the read-only HTTP methods and the ops rollback valve. The control plane marks worker builds with an in-process `ToolEnv.worker_policy` flag. `build_tool_registry` reads the flag to drop `save_artifact`, keep only read-only MCP tools and restrict http methods. `synthesize_worker_spec` strips the denied builtins from the worker spec and appends the restriction text to the worker prompt. The parent-side delegation block and the `spawn_worker` description gain matching text. No AgentSpec/manifest field is added.

**Tech Stack:** Python 3.12, pydantic v2, pytest (asyncio), MCP python SDK, uv.

**Spec:** `docs/superpowers/specs/2026-09-27-worker-tool-policy-design.md`

## Global Constraints

- Only dynamic workers (`spawn_worker`) change. Main agents and static sub-agents (`spec.subagents`) must behave exactly as before.
- **No new field on any AgentSpec / manifest model.** These models are `extra="forbid"`, which is a rollback hazard. The worker marker is an in-process `ToolEnv` field only.
- One rule source: every deny list, method set and valve read lives in `services/orchestrator/src/orchestrator/tools/worker_policy.py`. Nothing else re-spells those names.
- Ops rollback valve: env `EXPERT_WORK_WORKER_TOOL_POLICY`. Default on. Values `0`/`false`/`off`/`no` (case-insensitive) turn it off. When off, the worker's tools and prompt, the parent delegation block, and the `spawn_worker` description are all **byte-identical** to today.
- MCP tools without a `readOnlyHint` of exactly `True` count as writes and are not registered for workers.
- Worker http allows only `GET`, `HEAD`, `OPTIONS`.
- Platform-facing prompt text is English, in the style of the existing worker prompt.
- Logs record counts and names of tools only, never argument values.
- Every new assertion is mutation-verified: break the implementation → red → restore → green. Record it in the task report.
- Tests run from the worktree root:
  `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv PYTHONPATH=$PWD/services/orchestrator/src:$PWD/services/control-plane/src uv run --no-sync pytest <paths> -q`
  Always pass `--no-sync`. Before trusting green, print `orchestrator.tools.worker_policy.__file__` once and confirm it points into the worktree.
- Lint and types: `uv run --no-sync ruff check services/orchestrator services/control-plane` and `uv run --no-sync ruff format --check …`. Run mypy with the CI scope: `uv run --no-sync mypy services/orchestrator/src`.
- Commits end with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DNzoY8vc8jDMsib4wFGFpN
  ```

## Review Focus

1. A parent that **explicitly declares** `save_artifact` (or `ask_for_approval`) in `spec.tools` must still produce a worker without it. The declared path is covered in Task 4 and Task 3, not only the base-capability path.
2. An MCP server whose `annotations` arrive as a plain dict, or whose `readOnlyHint` is a non-bool such as `"true"` or `1`, must be treated as unannotated and dropped for workers. Covered in Task 2.
3. A grand-worker (a worker spawned by a worker) must carry the same restrictions. Covered in Task 4 (`worker_tool_env` reused).
4. A static sub-agent build and a main-agent build must keep `save_artifact`, every MCP tool and all http methods. Covered in Task 3 and Task 4.
5. A worker build whose parent has `arg_bindings` on write tools that the worker filters out (ai-health-plan binds `employee_code` on the two `cpwx_send_*` tools) must not log or record "unmatched binding" warnings. Covered in Task 3.
6. A lowercase `"post"` from a worker must still be rejected, since method names are normalised. Covered in Task 1.

---

### Task 1: Policy module + read-only HTTP methods

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/worker_policy.py`
- Modify: `services/orchestrator/src/orchestrator/tools/http.py` (`HTTPTool` dataclass fields, `spec`, `_require_method`)
- Test: `services/orchestrator/tests/test_worker_policy.py`

**Interfaces:**
- Produces:
  - `WORKER_POLICY_ENV: str = "EXPERT_WORK_WORKER_TOOL_POLICY"`
  - `WORKER_DENIED_BUILTINS: frozenset[str]` = `SKILL_AUTHORING_BUILTINS | {"save_artifact", "ask_for_approval", "manage_task"}`
  - `READ_ONLY_HTTP_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})`
  - `def worker_policy_enabled() -> bool`
  - `HTTPTool.allowed_methods: frozenset[str]`, defaulting to every method.

- [ ] **Step 1: Write the failing tests**

```python
"""B-122 —— worker 工具边界的规则来源 + http 方法收窄。"""

from __future__ import annotations

import pytest

from orchestrator.tools.http import HTTPTool
from orchestrator.tools.skill_authoring import SKILL_AUTHORING_BUILTINS
from orchestrator.tools.worker_policy import (
    READ_ONLY_HTTP_METHODS,
    WORKER_DENIED_BUILTINS,
    WORKER_POLICY_ENV,
    worker_policy_enabled,
)


def _allow_all(_tenant: object) -> list[str]:
    return []


def test_denied_builtins_cover_delivery_and_self_modification() -> None:
    assert {"save_artifact", "ask_for_approval", "manage_task"} <= WORKER_DENIED_BUILTINS
    # B 类直接复用技能创作的集合,不另抄一份名字
    assert SKILL_AUTHORING_BUILTINS <= WORKER_DENIED_BUILTINS
    # 读类与工作区工具不在禁用之列
    assert not {"read_file", "write_file", "exec_python", "list_artifacts"} & WORKER_DENIED_BUILTINS


@pytest.mark.parametrize("raw", ["0", "false", "OFF", " no "])
def test_valve_off_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(WORKER_POLICY_ENV, raw)
    assert worker_policy_enabled() is False


@pytest.mark.parametrize("raw", [None, "", "1", "true", "on"])
def test_valve_defaults_on(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(WORKER_POLICY_ENV, raw)
    assert worker_policy_enabled() is True


def test_http_default_exposes_every_method() -> None:
    tool = HTTPTool(allowlist_provider=_allow_all)
    assert "POST" in tool.spec.parameters["properties"]["method"]["enum"]
    assert tool._require_method({"method": "post"}) == "POST"


def test_http_read_only_narrows_schema_and_rejects_writes() -> None:
    tool = HTTPTool(allowlist_provider=_allow_all, allowed_methods=READ_ONLY_HTTP_METHODS)
    assert tool.spec.parameters["properties"]["method"]["enum"] == ["GET", "HEAD", "OPTIONS"]
    assert tool._require_method({"method": "get"}) == "GET"
    for method in ("post", "PUT", "patch", "DELETE"):
        with pytest.raises(ValueError, match="worker sub-agents"):
            tool._require_method({"method": method})
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `… pytest services/orchestrator/tests/test_worker_policy.py -q`
Expected: FAIL with `ModuleNotFoundError: orchestrator.tools.worker_policy`.

- [ ] **Step 3: Implement the policy module**

`services/orchestrator/src/orchestrator/tools/worker_policy.py`:

```python
"""B-122 —— 动态子智能体(worker)的工具边界:唯一的规则来源。

worker 此前继承父的全部工具,只剥 ``manage_task``;不该做的事全靠各 Agent 的
提示词自己写,而写提示词的人不一定知道 ``save_artifact`` / MCP 写工具是什么
(实证:ai-health-plan 的 worker 自己把中间稿登记成了交付物)。这里把边界收成
平台默认:worker 不往外交付、不永久改变 Agent 自己、不对外写。
见 ``docs/superpowers/specs/2026-09-27-worker-tool-policy-design.md``。

名单只在这里写一次;assembly / 控制面 / 提示词都从这里取。
"""

from __future__ import annotations

import os

from orchestrator.tools.skill_authoring import SKILL_AUTHORING_BUILTINS

#: 运维回滚阀(不是面向配置者的开关)。关掉后 worker 与父侧文案逐字节回到 B-122 之前。
WORKER_POLICY_ENV = "EXPERT_WORK_WORKER_TOOL_POLICY"
_OFF_VALUES = frozenset({"0", "false", "off", "no"})

#: A 类(往外交付:登记交付物、向人发起审批)+ B 类(永久改变 Agent 自己:
#: 技能 / 行为补丁 / 记忆 / 定时任务)。技能那一族直接复用它的来源集合。
WORKER_DENIED_BUILTINS: frozenset[str] = SKILL_AUTHORING_BUILTINS | frozenset(
    {"save_artifact", "ask_for_approval", "manage_task"}
)

#: C 类 —— worker 的 http 只许读。POST / PUT 这些就是对外写,与 MCP 写工具同性质。
READ_ONLY_HTTP_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def worker_policy_enabled() -> bool:
    """回滚阀:默认开;``0`` / ``false`` / ``off`` / ``no``(不分大小写)为关。"""
    return os.environ.get(WORKER_POLICY_ENV, "").strip().lower() not in _OFF_VALUES
```

- [ ] **Step 4: Make http honour `allowed_methods`**

In `services/orchestrator/src/orchestrator/tools/http.py`:

1. Add a field after `header_char_cap` in the `HTTPTool` dataclass:
   ```python
       #: B-122 —— 这个实例放行的方法。默认全部;worker 构建传只读集合
       #: (``worker_policy.READ_ONLY_HTTP_METHODS``),schema 的 enum 随之收窄。
       allowed_methods: frozenset[str] = _ALLOWED_METHODS
   ```
2. In `spec`, change `"enum": sorted(_ALLOWED_METHODS),` to `"enum": sorted(self.allowed_methods),`.
3. Replace the body of `_require_method` with:
   ```python
       def _require_method(self, args: Mapping[str, Any]) -> str:
           raw = args.get("method", "GET")
           if not isinstance(raw, str):
               msg = "'method' must be a string"
               raise ValueError(msg)
           upper = raw.strip().upper()
           if upper in _ALLOWED_METHODS and upper not in self.allowed_methods:
               # B-122 —— 方法本身合法,只是这个实例不放行(worker):说清为什么、交给谁。
               msg = (
                   f"HTTP {upper} is not available to worker sub-agents: writes to "
                   "outside systems stay with the orchestrator. Report what should "
                   "be sent in your final message instead."
               )
               raise ValueError(msg)
           if upper not in _ALLOWED_METHODS:
               msg = f"unsupported HTTP method {raw!r}; allowed: {sorted(self.allowed_methods)}"
               raise ValueError(msg)
           return upper
   ```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `… pytest services/orchestrator/tests/test_worker_policy.py services/orchestrator/tests/test_http_tool.py -q`
Expected: PASS. If `test_http_tool.py` does not exist, run `git grep -l "HTTPTool(" services/orchestrator/tests` and run those files.

- [ ] **Step 6: Mutation self-check**

Apply each mutation, confirm it goes red, then restore it:
- Drop `"save_artifact"` from the set. Expected: `test_denied_builtins_…` goes red.
- Change the `spec` enum back to `_ALLOWED_METHODS`. Expected: the narrows test goes red.
- Delete the new `if upper in _ALLOWED_METHODS and …` branch. Expected: the rejects test goes red.

After restoring, confirm `git diff` shows only the intended changes.

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/worker_policy.py services/orchestrator/src/orchestrator/tools/http.py services/orchestrator/tests/test_worker_policy.py
git commit -m "feat(worker-policy): policy module + read-only http methods for workers (B-122)"
```

---

### Task 2: Carry MCP `readOnlyHint` and filter to read-only on request

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/mcp.py`
  - `MCPToolDef` (~line 234)
  - `_materialize_tool_defs` (~line 257)
  - `register_mcp_tools` (~line 953)
- Test: `services/orchestrator/tests/test_worker_policy_mcp.py`

**Interfaces:**
- Consumes: none.
- Produces:
  - `MCPToolDef.read_only: bool | None = None`, set from the server's `readOnlyHint`.
  - `register_mcp_tools(..., read_only_only: bool = False)`. When `True`, it registers only tools whose `read_only is True`.

- [ ] **Step 1: Write the failing tests**

```python
"""B-122 —— MCP 读写标注带进 MCPToolDef;worker 只注册只读的。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from orchestrator.tools import MCPToolDef, RecordingMCPClient
from orchestrator.tools.mcp import _materialize_tool_defs, register_mcp_tools
from orchestrator.tools.registry import ToolRegistry


class _Ann:
    def __init__(self, read_only: Any) -> None:
        self.readOnlyHint = read_only


class _Raw:
    def __init__(self, name: str, annotations: Any = None) -> None:
        self.name = name
        self.description = name
        self.inputSchema: dict[str, Any] = {"type": "object", "properties": {}}
        self.annotations = annotations


def test_materialize_reads_read_only_hint() -> None:
    defs = _materialize_tool_defs(
        [
            _Raw("q", _Ann(True)),
            _Raw("w", _Ann(False)),
            _Raw("none"),
            _Raw("as_dict", {"readOnlyHint": True}),
            _Raw("string_true", _Ann("true")),
            _Raw("one", _Ann(1)),
        ],
        server="s",
    )
    by = {d.name: d.read_only for d in defs}
    # 只有「对象属性 == True 且是 bool」才算只读;字典形态、字符串、1 都当没标
    assert by == {
        "q": True,
        "w": False,
        "none": None,
        "as_dict": None,
        "string_true": None,
        "one": None,
    }


def _defs() -> tuple[MCPToolDef, ...]:
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    return (
        MCPToolDef(name="read_it", description="r", input_schema=schema, read_only=True),
        MCPToolDef(name="send_it", description="w", input_schema=schema, read_only=False),
        MCPToolDef(name="unknown", description="u", input_schema=schema),
    )


@pytest.mark.asyncio
async def test_default_registers_everything() -> None:
    registry = ToolRegistry()
    names = await register_mcp_tools(
        server_name="srv", client=RecordingMCPClient(tools=_defs()), registry=registry
    )
    assert len(names) == 3


@pytest.mark.asyncio
async def test_read_only_only_keeps_annotated_read_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()
    with caplog.at_level(logging.INFO, logger="orchestrator.tools.mcp"):
        names = await register_mcp_tools(
            server_name="srv",
            client=RecordingMCPClient(tools=_defs()),
            registry=registry,
            read_only_only=True,
        )
    assert names == ["mcp__srv__read_it"]
    assert "mcp__srv__send_it" not in registry
    assert "mcp__srv__unknown" not in registry
    line = next(r.getMessage() for r in caplog.records if "worker_read_only_filter" in r.getMessage())
    assert "kept=1" in line and "dropped_write=1" in line and "dropped_unannotated=1" in line
```

Before writing the assertion on `names`, check the wire-name format with `mcp_tool_name` in `mcp.py`: `mcp__<server>__<tool>`. Adjust the expected string only if the helper spells it differently.

- [ ] **Step 2: Run it and confirm it fails**

Run: `… pytest services/orchestrator/tests/test_worker_policy_mcp.py -q`
Expected: FAIL. Constructing `MCPToolDef` with `read_only=` raises `TypeError: unexpected keyword argument 'read_only'`.

- [ ] **Step 3: Implement the changes**

1. Add a field to `MCPToolDef`:
   ```python
       #: B-122 —— 服务器 ``list_tools`` 标注里的 ``readOnlyHint``:``True`` 只读,
       #: ``False`` 有写操作,``None`` 没标(worker 一律当写)。
       read_only: bool | None = None
   ```
2. Above `_materialize_tool_defs`, add:
   ```python
   def _read_only_hint(tool: Any) -> bool | None:
       """``Tool.annotations.readOnlyHint`` —— 只认 SDK 对象上的真 bool。

       没标、字典形态、字符串 ``"true"``、``1`` 一律 ``None``:worker 按「没标 = 当写」
       处理,宁可少给一个读工具,也不把写工具认成只读。
       """
       annotations = getattr(tool, "annotations", None)
       value = getattr(annotations, "readOnlyHint", None) if annotations is not None else None
       return value if isinstance(value, bool) else None
   ```
   Note that a plain `dict` has no `readOnlyHint` attribute, so `getattr` returns `None`. That is intended.
3. In `_materialize_tool_defs`, change the final append to:
   ```python
           defs.append(
               MCPToolDef(
                   name=str(t.name),
                   description=description,
                   input_schema=schema,
                   read_only=_read_only_hint(t),
               )
           )
   ```
4. In `register_mcp_tools`:
   - Add the keyword parameter `read_only_only: bool = False` after `arg_bindings`.
   - Extend the docstring: "``read_only_only`` (B-122) registers only tools whose server marked them ``readOnlyHint: true`` — the worker build's view; unannotated tools count as writes."
   - Inside the loop, right after the `allow_tools` check, add:
     ```python
             if read_only_only and tool_def.read_only is not True:
                 if tool_def.read_only is False:
                     dropped_write += 1
                 else:
                     dropped_unannotated += 1
                 continue
     ```
   - Initialise `dropped_write = dropped_unannotated = 0` before the loop.
   - After the loop, before `return`, add:
     ```python
         if read_only_only:
             logger.info(
                 "mcp.worker_read_only_filter server=%s kept=%d dropped_write=%d dropped_unannotated=%d",
                 server_name,
                 len(registered),
                 dropped_write,
                 dropped_unannotated,
             )
     ```
   - A tool skipped here never lands its binding. That is intended: Task 3 skips the unmatched-binding judgement for worker builds.

- [ ] **Step 4: Run it and confirm it passes**

Run: `… pytest services/orchestrator/tests/test_worker_policy_mcp.py services/orchestrator/tests/test_mcp_tool.py services/orchestrator/tests/test_mcp_arg_bindings_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Mutation self-check**
  - Make `_read_only_hint` return `bool(value)`. Expected: the `string_true` / `one` assertions go red.
  - Change the filter to `tool_def.read_only is False`, so unannotated tools are kept. Expected: the `unknown` assertion goes red.

  Restore both and check `git diff`.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/mcp.py services/orchestrator/tests/test_worker_policy_mcp.py
git commit -m "feat(worker-policy): carry MCP readOnlyHint and filter to read-only tools on request (B-122)"
```

---

### Task 3: `ToolEnv.worker_policy` in the registry assembler

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/assembly.py`
  - `ToolEnv` (~line 113)
  - `build_tool_registry` (~line 208)
  - `_register_http` (~line 842)
  - `_register_mcp` (the four `register_mcp_tools(` calls)
  - `_register_base_capabilities` (~line 648)
- Test: `services/orchestrator/tests/test_worker_policy_assembly.py`

**Interfaces:**
- Consumes:
  - `READ_ONLY_HTTP_METHODS` and `WORKER_DENIED_BUILTINS` (Task 1)
  - `register_mcp_tools(read_only_only=...)` (Task 2)
- Produces: `ToolEnv.worker_policy: bool = False`. When `True`:
  - `save_artifact`, `ask_for_approval` and any other `WORKER_DENIED_BUILTINS` entry are never registered, whether declared or base;
  - MCP registers read-only tools only;
  - http gets read-only methods;
  - the unmatched-binding judgement is skipped.

- [ ] **Step 1: Write the failing tests**

Model the sandbox/artifact stubs on the existing base-capability tests. Run `git grep -n "_register_base_capabilities\|BASE_CAPABILITY_BUILTINS" services/orchestrator/tests` and reuse the smallest fixture that gives a `ToolEnv` with `sandbox_runtime`, `artifact_store` and `workspace_store` wired. If no reusable fixture exists, use `unittest.mock.Mock()` for those three: registration only stores them.

```python
"""B-122 —— worker 构建(ToolEnv.worker_policy=True)的工具表。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest

from expert_work.protocol import ArgBindingSpec, BuiltinToolSpec, HTTPToolSpec, MCPToolSpec
from orchestrator.tools import (
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    ToolEnv,
    build_tool_registry,
)

pytestmark = pytest.mark.asyncio

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"employee_code": {"type": "string"}, "text": {"type": "string"}},
}


async def _env(*, worker: bool) -> ToolEnv:
    pool = MCPServerPool()
    await pool.add(
        "deepcare",
        RecordingMCPClient(
            tools=(
                MCPToolDef(name="form_list", description="r", input_schema=_SCHEMA, read_only=True),
                MCPToolDef(name="cpwx_send", description="w", input_schema=_SCHEMA, read_only=False),
            )
        ),
    )
    env = ToolEnv(
        sandbox_runtime=Mock(),
        artifact_store=Mock(),
        workspace_store=Mock(),
        allowlist_provider=lambda _t: [],
        mcp_pool=pool,
    )
    return replace(env, worker_policy=worker)


_TOOLS = [
    BuiltinToolSpec(name="save_artifact"),  # 显式声明也要剥(Review Focus 1)
    HTTPToolSpec(),
    MCPToolSpec(
        servers=["deepcare"],
        arg_bindings=[
            ArgBindingSpec(server="deepcare", tool="cpwx_send", args={"employee_code": "employee_code"})
        ],
    ),
]


async def test_main_build_is_unchanged() -> None:
    registry = await build_tool_registry(_TOOLS, tool_env=await _env(worker=False))
    assert "save_artifact" in registry
    assert "mcp__deepcare__cpwx_send" in registry
    assert "POST" in registry.get_required("http").spec.parameters["properties"]["method"]["enum"]


async def test_worker_build_drops_delivery_write_mcp_and_write_http(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        registry = await build_tool_registry(_TOOLS, tool_env=await _env(worker=True))
    assert "save_artifact" not in registry
    assert "list_artifacts" in registry  # 读产物不受限
    assert "write_file" in registry  # 工作区照常可写
    assert "mcp__deepcare__form_list" in registry
    assert "mcp__deepcare__cpwx_send" not in registry
    assert registry.get_required("http").spec.parameters["properties"]["method"]["enum"] == [
        "GET",
        "HEAD",
        "OPTIONS",
    ]
    # 被策略滤掉的写工具上的绑定,不能报「绑定落空」(Review Focus 5)
    assert registry.unmatched_arg_bindings() == ()
    assert not [r for r in caplog.records if "arg_binding_unmatched" in r.getMessage()]


async def test_worker_build_skips_declared_ask_for_approval() -> None:
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="ask_for_approval")], tool_env=await _env(worker=True)
    )
    assert "ask_for_approval" not in registry
```

Adjust constructor details to the real signatures before running. Check `ArgBindingSpec` field names in `expert_work/protocol/agent_spec.py` (~line 1181) and the return type of `unmatched_arg_bindings()` (it may be a list or a tuple; compare with `== ()` or `== []` accordingly). The assertions themselves must not change.

- [ ] **Step 2: Run it and confirm it fails**

Run: `… pytest services/orchestrator/tests/test_worker_policy_assembly.py -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'worker_policy'` from `replace`.

- [ ] **Step 3: Implement the changes**

1. Import at the top of `assembly.py`:
   ```python
   from orchestrator.tools.worker_policy import READ_ONLY_HTTP_METHODS, WORKER_DENIED_BUILTINS
   ```
2. Add the last field of `ToolEnv`, after `workspace_lock`:
   ```python
       #: B-122 —— 这是一次动态子智能体(worker)构建。控制面在 ``make_worker_build_fn``
       #: 里置 True(孙级复用同一个 env,自动带上);主 Agent、静态子 Agent 恒为 False。
       #: 为 True 时:不注册 ``WORKER_DENIED_BUILTINS``(含基础能力 ``save_artifact``)、
       #: MCP 只注册标了只读的、http 只放行读方法、不做「绑定落空」判定。
       #: 进程内字段,**不是** manifest 字段(extra="forbid" 的回滚坑)。
       worker_policy: bool = False
   ```
3. In `build_tool_registry`'s entry loop, skip denied builtins for workers:
   ```python
       for entry in tool_specs:
           if isinstance(entry, BuiltinToolSpec):
               if tool_env.worker_policy and entry.name in WORKER_DENIED_BUILTINS:
                   continue
               _register_builtin(...)
   ```
   Keep the existing `_register_builtin` call arguments unchanged.
4. Wrap the whole unmatched-binding judgement block, from `landed = registry.landed_arg_bindings()` through the end of the `for binding in all_arg_bindings:` loop, in `if not tool_env.worker_policy:`. Put this comment above it:
   ```python
       # B-122 —— worker 构建不做这项判定:它的 MCP 注册按只读滤过,被滤掉的写工具
       # 上的绑定必然「没落地」,报出来就是对配置者说假话。同一份 manifest 的主构建
       # (保存时的试建 + 每次主 run)照常判定,覆盖不丢。
   ```
5. In `_register_base_capabilities`, first line inside the loop:
   ```python
           if env.worker_policy and name in WORKER_DENIED_BUILTINS:
               continue
   ```
6. In `_register_http`, pass the methods:
   ```python
       registry.register(
           HTTPTool(
               allowlist_provider=env.allowlist_provider,
               denylist_provider=env.denylist_provider,
               **({"allowed_methods": READ_ONLY_HTTP_METHODS} if env.worker_policy else {}),
           )
       )
   ```
7. In `_register_mcp`, add `read_only_only=env.worker_policy,` to each of the four `register_mcp_tools(` calls. Confirm the count with `grep -c "read_only_only=env.worker_policy" …assembly.py` → `4`.

- [ ] **Step 4: Run it and confirm it passes**

Run: `… pytest services/orchestrator/tests/test_worker_policy_assembly.py services/orchestrator/tests/test_mcp_arg_bindings_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Mutation self-check**

Remove each of these, one at a time, and confirm the named test goes red:
- The skip in `_register_base_capabilities` → `test_worker_build_drops…` (`save_artifact` present). This works only because the entry loop keeps skipping the declared one. If it stays green, remove `save_artifact` from `_TOOLS` in a scratch copy to prove the base path on its own.
- The entry-loop skip → `test_worker_build_skips_declared_ask_for_approval`.
- One `read_only_only=` pass-through (the `mcp_pool` one) → `cpwx_send` present.
- The http kwargs → enum red.
- The `if not tool_env.worker_policy:` guard → the unmatched-binding assertion.

Restore everything and check `git diff`.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/assembly.py services/orchestrator/tests/test_worker_policy_assembly.py
git commit -m "feat(worker-policy): worker builds drop delivery tools, write MCP tools and write http methods (B-122)"
```

---

### Task 4: Control plane — worker spec, worker prompt, worker env flag

**Files:**
- Modify: `services/control-plane/src/control_plane/subagent_runtime.py`
  - `_worker_system_prompt` (~line 65)
  - `synthesize_worker_spec` (~line 135)
  - `make_worker_build_fn` (`_build` and `worker_tool_env`, ~line 586-685)
- Test: `services/control-plane/tests/test_worker_tool_policy.py`
- Check: `services/control-plane/tests/test_worker_spec_synthesis.py` and `services/control-plane/tests/test_subagent_runtime.py` still pass.

**Interfaces:**
- Consumes: `WORKER_DENIED_BUILTINS` and `worker_policy_enabled` (Task 1); `ToolEnv.worker_policy` (Task 3).
- Produces:
  - `synthesize_worker_spec(..., worker_policy: bool = True)`:
    - `True` strips every `WORKER_DENIED_BUILTINS` builtin and appends `_WORKER_POLICY_PROMPT` to the worker prompt;
    - `False` strips only `manage_task` and produces the exact pre-B-122 prompt.
  - `_worker_system_prompt(role, *, restricted: bool = False)`.
  - `make_worker_build_fn` sets `worker_tool_env.worker_policy = worker_policy_enabled()`.

- [ ] **Step 1: Write the failing tests**

```python
"""B-122 —— worker spec / 提示词 / 构建 env 的工具边界。"""

from __future__ import annotations

import pytest

from control_plane.subagent_runtime import _worker_system_prompt, synthesize_worker_spec
from expert_work.protocol import AgentSpec, BuiltinToolSpec
from orchestrator.tools.worker_policy import WORKER_DENIED_BUILTINS

_SANDBOX = {
    "resources": {"cpu": "1.0", "memory": "1Gi"},
    "network": {"egress": "proxy", "allowlist": []},
    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
}


def _parent(tools: list[dict[str, object]]) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "deepseek", "name": "deepseek-v4-pro"},
                "system_prompt": {"template": "You are the parent."},
                "sandbox": _SANDBOX,
                "tools": tools,
                "workflow": {"type": "react", "max_iterations": 12},
            },
        }
    )


_DENIED = [{"type": "builtin", "name": n, "config": {}} for n in sorted(WORKER_DENIED_BUILTINS)]
_KEPT = [{"type": "builtin", "name": "web_search", "config": {}}, {"type": "http"}]


def _names(spec: AgentSpec) -> set[str]:
    return {getattr(t, "name", None) or t.type for t in spec.spec.tools}


def test_worker_spec_strips_every_denied_builtin() -> None:
    w = synthesize_worker_spec(
        _parent(_DENIED + _KEPT), role=None, max_iterations=8, allowed_toolsets=[]
    )
    assert _names(w) == {"web_search", "http"}


def test_worker_spec_valve_off_strips_only_manage_task() -> None:
    w = synthesize_worker_spec(
        _parent(_DENIED + _KEPT),
        role=None,
        max_iterations=8,
        allowed_toolsets=[],
        worker_policy=False,
    )
    assert _names(w) == (WORKER_DENIED_BUILTINS - {"manage_task"}) | {"web_search", "http"}


def test_worker_prompt_states_the_boundary() -> None:
    text = synthesize_worker_spec(
        _parent(_KEPT), role="designer", max_iterations=8, allowed_toolsets=[]
    ).spec.system_prompt.template
    assert "deliberately not available to you" in text
    assert "never talk to the end user" in text
    assert "every file you created or modified" in text


def test_worker_prompt_valve_off_is_byte_identical() -> None:
    w = synthesize_worker_spec(
        _parent(_KEPT), role="designer", max_iterations=8, allowed_toolsets=[], worker_policy=False
    )
    assert w.spec.system_prompt.template == _worker_system_prompt("designer")
    assert "deliberately not available" not in w.spec.system_prompt.template
```

Then add, in the same file, a build-level test for `make_worker_build_fn`. Model it on `test_worker_build_marks_skills_as_inherited` in `services/control-plane/tests/test_subagent_runtime.py` (~line 993): it patches `build_agent` and captures its kwargs. Assert that the captured `tool_env.worker_policy is True` by default, and `False` when `monkeypatch.setenv("EXPERT_WORK_WORKER_TOOL_POLICY", "off")` is set **before** `make_worker_build_fn` is called. Also assert that a grand-worker call through the captured `tool_env.worker_build_fn` carries `worker_policy is True` (Review Focus 3). Copy that test's fixture setup verbatim; do not invent a new harness.

Also add a static sub-agent check (Review Focus 4), modelled on `test_child_builder_strips_manage_task_from_the_spec` (`test_subagent_runtime.py` ~line 593): the `tool_env` that `make_child_agent_builder` passes to `build_agent` has `worker_policy is False`, and the static child spec still contains a declared `save_artifact`. This passes before and after the change — it is a guard. Prove it bites by temporarily setting `worker_policy=True` in the child builder's env: it must go red.

- [ ] **Step 2: Run it and confirm it fails**

Run: `… pytest services/control-plane/tests/test_worker_tool_policy.py -q`
Expected: FAIL. Denied builtins are still present, `worker_policy` is an unknown keyword, and the prompt phrases are missing.

- [ ] **Step 3: Implement the changes**

1. Imports in `subagent_runtime.py`:
   ```python
   from orchestrator.tools.worker_policy import WORKER_DENIED_BUILTINS, worker_policy_enabled
   ```
2. Worker prompt. Add a module constant above `_worker_system_prompt`:
   ```python
   #: B-122 —— worker 工具边界的提示词侧。结构上已经不给这些工具(assembly /
   #: synthesize),这段让模型知道「缺工具是有意的」,别拿 bash / exec_python 绕,
   #: 并在小结里列出改过的文件(替代「禁止改文件」,两家业界实现都这样做)。
   _WORKER_POLICY_PROMPT = (
       " You are not the orchestrator and you never talk to the end user: your "
       "final message goes only to the orchestrator, which decides what reaches "
       "the user."
       " Some capabilities are deliberately not available to you and stay with "
       "the orchestrator: registering deliverables, sending messages or otherwise "
       "writing to systems outside this workspace, and changing the agent's "
       "memory, skills or schedules. Do not try to work around a missing tool "
       "(for example by making outbound requests from bash or exec_python); if "
       "the task needs one, say so in your final message."
       " End your final message with a list of every file you created or "
       "modified in the workspace — its path and one line on what it holds."
   )
   ```
   Change the signature to `def _worker_system_prompt(role: str | None, *, restricted: bool = False) -> str:`. Assign the existing concatenated string to a local `base`, then `return base + _WORKER_POLICY_PROMPT if restricted else base`. Do not change a character of the existing text.
3. `synthesize_worker_spec`:
   - Add the keyword parameter `worker_policy: bool = True`.
   - Set `denied = WORKER_DENIED_BUILTINS if worker_policy else frozenset({"manage_task"})`.
   - Replace the tools comprehension condition `not (isinstance(t, BuiltinToolSpec) and t.name == "manage_task")` with `not (isinstance(t, BuiltinToolSpec) and t.name in denied)`.
   - Replace `SystemPromptSpec(template=_worker_system_prompt(role))` with `SystemPromptSpec(template=_worker_system_prompt(role, restricted=worker_policy))`.
   - Add one docstring paragraph: "B-122 — ``worker_policy`` strips :data:`WORKER_DENIED_BUILTINS` (delivery + self-modification) and appends the boundary text; ``False`` is the ops rollback shape (only ``manage_task`` stripped, prompt byte-identical)."
4. `make_worker_build_fn`:
   - Change the last lines to:
     ```python
         # B-122 —— 回滚阀在工厂创建时读一次;孙级 worker 复用这个 env,限制一路带下去。
         worker_tool_env = replace(
             base_tool_env, worker_build_fn=_build, worker_policy=worker_policy_enabled()
         )
     ```
   - Inside `_build`, pass `worker_policy=worker_tool_env.worker_policy` to `synthesize_worker_spec(...)`.
   - Right after `worker_spec = synthesize_worker_spec(...)`, add:
     ```python
         if worker_tool_env.worker_policy:
             stripped = sorted(
                 t.name
                 for t in parent_spec.spec.tools
                 if isinstance(t, BuiltinToolSpec) and t.name in WORKER_DENIED_BUILTINS
             )
             logger.info(
                 "worker_policy.applied role=%s depth=%d stripped_declared=%s base_save_artifact=dropped http=read_only mcp=read_only",
                 role or "general",
                 depth,
                 stripped,
             )
     ```

- [ ] **Step 4: Run it and confirm it passes**

Run: `… pytest services/control-plane/tests/test_worker_tool_policy.py services/control-plane/tests/test_worker_spec_synthesis.py services/control-plane/tests/test_subagent_runtime.py -q`
Expected: PASS. Two existing tests in `test_worker_spec_synthesis.py` assert text, not the absence of the new paragraph, so they should still pass: `test_worker_prompt_states_shared_workspace` and `test_worker_prompt_tells_it_to_offload_bulk_output_to_a_file`. If a test breaks, it is because it pinned the old exact prompt. In that case pass `worker_policy=False` in that test only when its intent is "legacy shape"; otherwise update the expectation and say why in the report.

- [ ] **Step 5: Mutation self-check**

Remove each of these, one at a time, and confirm red:
- The `denied` switch: always use `{"manage_task"}`.
- The `restricted=` argument.
- The `worker_policy=worker_policy_enabled()` in `replace`.

Restore and check `git diff`.

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/subagent_runtime.py services/control-plane/tests/test_worker_tool_policy.py
git commit -m "feat(worker-policy): worker spec strips delivery/self-modification tools, prompt states the boundary (B-122)"
```

---

### Task 5: Parent side — delegation block and `spawn_worker` description

**Files:**
- Modify:
  - `services/orchestrator/src/orchestrator/agent_factory.py` (`_WORKER_DELEGATION_BLOCK` ~line 1789 and the append at ~line 1976)
  - `services/orchestrator/src/orchestrator/tools/spawn_worker.py` (description in `SpawnWorkerTool.spec`, ~line 142)
- Test: `services/orchestrator/tests/test_worker_policy_parent_text.py`
- Check: `services/orchestrator/tests/test_agent_factory_worker_delegation.py`, `services/orchestrator/tests/test_agent_factory.py`, `services/orchestrator/tests/test_spawn_worker.py`

**Interfaces:**
- Consumes: `worker_policy_enabled` (Task 1).
- Produces:
  - `_worker_delegation_block() -> str` in `agent_factory.py`: the unchanged `_WORKER_DELEGATION_BLOCK`, plus `_WORKER_POLICY_PARENT_ADDENDUM` when the valve is on.
  - The `spawn_worker` description tool sentence switches with the valve.

- [ ] **Step 1: Write the failing tests**

```python
"""B-122 —— 父侧文案与 worker 实际边界一致;阀关逐字节回到现状。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from orchestrator.agent_factory import _WORKER_DELEGATION_BLOCK, _worker_delegation_block
from orchestrator.tools.spawn_worker import SpawnWorkerTool
from orchestrator.tools.worker_policy import WORKER_POLICY_ENV


def _desc() -> str:
    return SpawnWorkerTool(builder=Mock(), child_depth=1).spec.description


def test_parent_block_tells_it_to_verify_register_and_take_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    block = _worker_delegation_block()
    assert block.startswith(_WORKER_DELEGATION_BLOCK)
    assert "not deliverables until you register them yourself" in block
    assert "do that part yourself" in block


def test_parent_block_valve_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKER_POLICY_ENV, "off")
    assert _worker_delegation_block() == _WORKER_DELEGATION_BLOCK


def test_spawn_worker_description_matches_worker_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    desc = _desc()
    assert "same tool set as you" not in desc
    assert "read-only MCP tools" in desc
    monkeypatch.setenv(WORKER_POLICY_ENV, "off")
    assert "Workers carry the same tool set as you — including MCP tools — and can fetch data on their own." in _desc()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `… pytest services/orchestrator/tests/test_worker_policy_parent_text.py -q`
Expected: FAIL with `ImportError: _worker_delegation_block`.

- [ ] **Step 3: Implement the changes**

1. In `agent_factory.py`, add `from orchestrator.tools.worker_policy import worker_policy_enabled`. Below `_WORKER_DELEGATION_BLOCK`, add:
   ```python
   #: B-122 —— worker 已拿不到登记交付物 / 对外写 / 改 Agent 自己的工具:父侧要知道
   #: 小结是自述、文件登记了才算交付、worker 做不了的自己接。阀关时不追加。
   _WORKER_POLICY_PARENT_ADDENDUM = (
       " A worker's final message is its own report: check its key claims — open "
       "the files it lists — before relying on them or passing them on. Files a "
       "worker writes are not deliverables until you register them yourself with "
       "save_artifact. If a worker fails, returns something unusable, or says it "
       "needed a tool it does not have, do that part yourself instead of "
       "abandoning the task."
   )


   def _worker_delegation_block() -> str:
       """委派块全文;B-122 回滚阀关时逐字节等于 :data:`_WORKER_DELEGATION_BLOCK`。"""
       if worker_policy_enabled():
           return _WORKER_DELEGATION_BLOCK + _WORKER_POLICY_PARENT_ADDENDUM
       return _WORKER_DELEGATION_BLOCK
   ```
   At the append site (~line 1977), change `_WORKER_DELEGATION_BLOCK` to `_worker_delegation_block()`.
2. In `spawn_worker.py`, add `from orchestrator.tools.worker_policy import worker_policy_enabled` and two module constants:
   ```python
   _TOOLS_SENTENCE_LEGACY = (
       "Workers carry the same tool set as you — including MCP tools — and can "
       "fetch data on their own. "
   )
   #: B-122 —— 与 worker 实际拿到的工具一致;说「same tool set」就是对模型说假话。
   _TOOLS_SENTENCE_RESTRICTED = (
       "Workers carry your read-side tools — including read-only MCP tools — and "
       "can fetch data on their own, but they cannot register deliverables, write "
       "to systems outside the workspace, or change the agent's memory, skills or "
       "schedules; keep those steps here. "
   )
   ```
   In the description concatenation, replace the two adjacent literals `"Workers carry the same tool set as "` and `"you — including MCP tools — and can fetch data on their own. "` with the expression `+ (_TOOLS_SENTENCE_RESTRICTED if worker_policy_enabled() else _TOOLS_SENTENCE_LEGACY) +`. Put parentheses around the neighbouring literal groups so the implicit string concatenation stays correct.

   Then prove the legacy text is byte-identical. Before editing, on the base commit, run:
   `python -c "from unittest.mock import Mock; from orchestrator.tools.spawn_worker import SpawnWorkerTool; print(SpawnWorkerTool(builder=Mock(), child_depth=1).spec.description)" > /tmp/desc_before.txt`
   After editing, run the same with `EXPERT_WORK_WORKER_TOOL_POLICY=off`, write it to `/tmp/desc_after.txt`, and `cmp` the two files. They must be identical.

- [ ] **Step 4: Run it and confirm it passes**

Run: `… pytest services/orchestrator/tests/test_worker_policy_parent_text.py services/orchestrator/tests/test_agent_factory_worker_delegation.py services/orchestrator/tests/test_agent_factory.py services/orchestrator/tests/test_spawn_worker.py -q`
Expected: PASS.

- [ ] **Step 5: Mutation self-check**

Remove each of these, one at a time, and confirm red:
- Make `_worker_delegation_block` always return the base.
- Make the description always use `_TOOLS_SENTENCE_LEGACY`.

Restore and check `git diff`.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/agent_factory.py services/orchestrator/src/orchestrator/tools/spawn_worker.py services/orchestrator/tests/test_worker_policy_parent_text.py
git commit -m "feat(worker-policy): parent delegation text and spawn_worker description match the worker boundary (B-122)"
```

---

### Task 6: Whole-suite gate

**Files:** none new.

- [ ] **Step 1: Full suites**

Run:
```
DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock … pytest -n 8 services/orchestrator/tests > $SCRATCH/b122-orch.log 2>&1; tail -2 $SCRATCH/b122-orch.log
DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock … pytest -n 8 services/control-plane/tests > $SCRATCH/b122-cp.log 2>&1; tail -2 $SCRATCH/b122-cp.log
```
Expected: 0 failed. A failure that also reproduces on `origin/main` (e.g. testcontainers Postgres not starting locally) is environmental. Show it by running the same test on a clean `origin/main` worktree, and name it in the report.

- [ ] **Step 2: Lint, format, types**

Run `ruff check`, `ruff format --check` and `mypy services/orchestrator/src` (see Global Constraints). Expected: clean.

- [ ] **Step 3: Cross-check the rule source**

Run `git grep -n '"save_artifact", "ask_for_approval"\|READ_ONLY_HTTP_METHODS\s*=' services`. Expected: definitions only in `worker_policy.py`.

- [ ] **Step 4: ROADMAP status**

In `docs/superpowers/ROADMAP.md` row B-122, change 「(2026-09-27 立项,方向已拍板)」 to 「(2026-09-27 立项;代码 PR #<n>,待真栈验收)」. Commit it with the final task.

---

## Real-stack acceptance (after merge; needs the deep-ai-health MCP with annotations deployed)

Not a code task. Run it once the owner has deployed `deep-ai-health-mcp-service` `f365a1e` to its test environment and this branch is on the test cluster (`tools/deploy/release.sh test` from the worktree).

1. ai-health-plan, with the employee asking to "派一个子智能体专门设计版式方案":
   - The worker's events show **no** `save_artifact` call.
   - `end.artifacts` has no intermediate draft; the orchestrator registered the deliverable.
2. Same run: the worker really calls one deep-ai-health read tool (e.g. `form_list_by_project`, with a real test project), and its tool list has no `cpwx_send_*`. Check the `mcp.worker_read_only_filter` log line: `kept=31 dropped_write=9`.
3. Normal main-agent plan (docx) behaves as before: same tool count, deliverables present.
4. Ask the worker to also "把方案发给客户". There is no write-tool call in its events; its final message hands the step back.
5. Afterwards, propose removing the two now-redundant bans from the ai-health-plan prompt (「不调写操作 MCP」「不渲染或生成最终成品」). This is a separate change that needs the user's approval.
