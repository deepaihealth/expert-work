# 调试台沙箱 exec 结果结构化显示(PR-D)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 调试台能显示沙箱 exec(exec_python / bash)的 stdout / stderr / 退出码 —— 主时间线与子 agent(worker)子时间线都能。

**Architecture:** 根因不是迁移,是 spotlight datamark(`\s+` → `▁ `)销毁了换行,而前端 `parseExecResult` 靠换行从渲染文本抠结构,且失败后返回 truthy 空对象堵死原文兜底 → UI 只剩红色 `exit_code: ?`。修法:后端在 `format_sandbox_outcome` 的 `meta` 里补 `stdout` / `stderr`(截断后、**未经 spotlight** 的原文),复用现成的 `ToolResult.meta → ToolMessage.artifact → SSE` 结构化通道(`manage_task.trigger_id` 同款,builder.py 零改动);前端优先读 artifact,没有 artifact(老 run 回放 / 错误路径)才 fallback 正则,且解析完全失败不再挂空 `execResult`,恢复原文兜底分支可达;worker 子时间线在 `_summarize_message` 里把 exec artifact 摘要(excerpt 500)带上,前端 worker 行内渲染。

**Tech Stack:** Python(orchestrator,pytest)+ TypeScript(admin-ui,vitest + React Testing Library)。

## Global Constraints

- **不改 spotlight / datamark 本体,不改 `_render` 文本模板** —— LLM 看到的 `content` 一个字节不变(`services/orchestrator/src/orchestrator/tools/sandbox.py:479-496`)。
- **不改 `graph_builder/builder.py`** —— `meta → artifact` 通道已存在(builder.py:2840-2848),本 PR 只往 meta 里加字段。
- **artifact wire 契约(本 PR 钉死)**:sandbox exec 工具的 `ToolMessage.artifact = {"exit_code": int, "timed_out": bool, "truncated": bool, "stdout": str, "stderr": str}`;`stdout` / `stderr` 是 `_truncate` 后(cap = `output_char_cap`,默认 20_000,截断时尾缀 `...[truncated]`)的原始流,**不经 spotlight**。
- **worker 帧保持摘要语义**:worker update 帧里 exec 字段一律 `_excerpt(…, WORKER_RESULT_EXCERPT)`(=500)截断,不带全量。
- **前端解析失败不挂 `execResult`**:`parseExecResult` 返回 `{stdout:"", stderr:"", exitCode:null}` 的全空形状时不赋值,让 `ToolTimeline.tsx` 的 `else if (entry.resultPreview)` 原文兜底分支可达。
- 验证命令:orchestrator 侧 `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/<file> -q`;前端侧 `cd apps/admin-ui && npm run test`(vitest run)+ `npm run typecheck`(tsc -b --noEmit)。编辑器诊断可能 stale,一律以真实命令输出定论。
- 本仓 ruff select 不含 SLF001 —— 不要写多余的 `# noqa`(会挂 RUF100)。
- commit 信息遵循 conventional commits,不加 Co-Authored-By(全局已禁)。

---

### Task 1: 后端 —— `format_sandbox_outcome` meta 补 stdout / stderr

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py:499-507`(`format_sandbox_outcome` 的 return)
- Test: `services/orchestrator/tests/test_exec_python_tool.py`
- Test: `services/orchestrator/tests/test_bash_tool.py`

**Interfaces:**
- Consumes: 现有 `SandboxOutcome`(frozen dataclass:`stdout` / `stderr` / `exit_code` / `timed_out`,sandbox.py:63-70)、`_truncate(text, cap) -> tuple[str, bool]`(sandbox.py:595-599,超 cap 时尾缀 `_TRUNCATION_MARKER = "...[truncated]"`)。
- Produces: `ToolResult.meta` 新增键 `"stdout"` / `"stderr"`(截断后字符串)。Task 2 前端按此 wire 契约读 `artifact.stdout` / `artifact.stderr`;Task 3 worker 摘要按此读 `msg.artifact`。

- [ ] **Step 1: 写失败测试(exec_python 两条)**

在 `services/orchestrator/tests/test_exec_python_tool.py` 的 `test_exec_python_runs_code_and_returns_output` 之后追加:

```python
@pytest.mark.asyncio
async def test_exec_python_meta_carries_streams() -> None:
    # PR-D — the debug console reads stdout/stderr from the structured
    # ``meta`` (→ ToolMessage.artifact) because the rendered ``content``
    # is spotlight-datamarked (newlines destroyed) on the wire.
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="42\n", stderr="boom\n", exit_code=3, timed_out=False)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "print(6 * 7)"}, ctx=_ctx())

    assert result.meta["stdout"] == "42\n"
    assert result.meta["stderr"] == "boom\n"
    assert result.meta["exit_code"] == 3


@pytest.mark.asyncio
async def test_exec_python_meta_streams_are_capped() -> None:
    # meta rides the SSE / audit / trace path — it carries the same
    # head-truncated streams the rendered content shows, never the raw 1MB.
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="x" * 50_000, stderr="", exit_code=0, timed_out=False)
    )
    tool = ExecPythonTool(client=client)

    result = await tool.call({"code": "print('x' * 50000)"}, ctx=_ctx())

    assert result.meta["truncated"] is True
    assert result.meta["stdout"].endswith("...[truncated]")
    assert len(result.meta["stdout"]) == DEFAULT_OUTPUT_CHAR_CAP + len("...[truncated]")
    assert result.meta["stderr"] == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_exec_python_tool.py -q`
Expected: 新增两条 FAIL(`KeyError: 'stdout'`),其余 PASS。

- [ ] **Step 3: 写 bash 侧失败测试(一条)**

在 `services/orchestrator/tests/test_bash_tool.py` 的 `test_bash_runs_command_via_subprocess_wrapper` 之后追加:

```python
@pytest.mark.asyncio
async def test_bash_meta_carries_streams() -> None:
    # bash shares format_sandbox_outcome with exec_python — same structured
    # meta contract (PR-D).
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(stdout="hello\n", stderr="warn\n", exit_code=0, timed_out=False)
    )
    tool = BashTool(client=client)

    result = await tool.call({"command": "echo hello"}, ctx=_ctx())

    assert result.meta["stdout"] == "hello\n"
    assert result.meta["stderr"] == "warn\n"
```

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_bash_tool.py -q`
Expected: 新增一条 FAIL(`KeyError: 'stdout'`)。

- [ ] **Step 4: 最小实现**

`services/orchestrator/src/orchestrator/tools/sandbox.py` 的 `format_sandbox_outcome` return 改为(只加两行,docstring 的 meta 说明句同步补一句):

```python
    truncated = cut_out or cut_err
    return ToolResult(
        content=_render(stdout, stderr),
        meta={
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "truncated": truncated,
            # PR-D — the debug console renders stdout/stderr from here
            # (→ ToolMessage.artifact): the ``content`` string is spotlight-
            # datamarked on the wire, so its newlines don't survive. These are
            # the same head-truncated streams ``content`` renders, pre-mark.
            "stdout": stdout,
            "stderr": stderr,
        },
        full_content=_render(outcome.stdout, outcome.stderr) if truncated else None,
    )
```

docstring 第一段 `and surfaces ``exit_code`` / ``timed_out`` in both the text and the structured ``meta``.` 改为 `and surfaces ``exit_code`` / ``timed_out`` — plus the truncated ``stdout`` / ``stderr`` themselves — in the structured ``meta`` (the debug console's data path; the rendered text is datamarked on the wire).`(保持其余不动)。

- [ ] **Step 5: 跑两个测试文件确认全绿**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_exec_python_tool.py tests/test_bash_tool.py -q`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/sandbox.py services/orchestrator/tests/test_exec_python_tool.py services/orchestrator/tests/test_bash_tool.py
git commit -m "feat(sandbox): exec 结果 meta 补 stdout/stderr——调试台结构化显示数据源(PR-D)"
```

---

### Task 2: 前端主时间线 —— 优先读 artifact,fallback 失败恢复原文兜底

**Files:**
- Modify: `apps/admin-ui/src/api/tool_timeline.ts`(`ToolCallEntry` 接口、结果侧 artifact 读取块 :302-313、末尾 exec 归因循环 :318-323)
- Modify: `apps/admin-ui/src/components/ToolTimeline.tsx:104-108`(`hadUntrusted` 判据)
- Test: `apps/admin-ui/src/api/__tests__/tool_timeline.test.ts`
- Test: `apps/admin-ui/src/components/__tests__/ToolTimeline.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 artifact wire 契约 `{exit_code: int, timed_out: bool, truncated: bool, stdout: str, stderr: str}`;现有 `ExecResult` 接口(`{stdout: string; stderr: string; exitCode: number | null}`,tool_timeline.ts:59-63);现有测试 helper `aiCall2` / `toolResult` / `toolResultWithArtifact` / `updates`(tool_timeline.test.ts)。
- Produces: `ToolCallEntry.execArtifact?: ExecResult`(内部中转字段);`entry.execResult` 语义变更 —— artifact 优先、正则解析全空时**不赋值**。渲染组件 `ToolTimeline.tsx` 无接口变化。

- [ ] **Step 1: 写失败测试(parser 三条)**

在 `apps/admin-ui/src/api/__tests__/tool_timeline.test.ts` 的 `describe("parseToolCalls exec attribution")` 内追加:

```ts
  it("prefers the artifact's structured exec fields over text parsing", () => {
    // Wire shape after PR-D: format_sandbox_outcome.meta carries the raw
    // (pre-datamark) streams; the content string arrives datamark-mangled.
    const events = [
      updates("agent", [aiCall2("c1", "exec_python", { code: "print(1)" })]),
      updates("tools", [
        toolResultWithArtifact("c1", "«UNTRUSTED nonce=x»\nstdout:▁ 1▁ exit_code:▁ 0\n«/UNTRUSTED nonce=x»", {
          exit_code: 0,
          timed_out: false,
          truncated: false,
          stdout: "1\n",
          stderr: "",
        }),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toEqual({ stdout: "1\n", stderr: "", exitCode: 0 });
  });

  it("leaves execResult unset on a datamark-mangled preview with no artifact", () => {
    // Legacy runs (pre-PR-D frames) have no exec artifact and a mangled
    // preview — the raw-preview fallback branch must stay reachable, so no
    // truthy-but-empty ExecResult may be attached.
    const events = [
      updates("agent", [aiCall2("c1", "exec_python", { code: "print(1)" })]),
      updates("tools", [toolResult("c1", "stdout:▁ 1▁ exit_code:▁ 0")]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toBeUndefined();
    expect(entry.resultPreview).toBe("stdout:▁ 1▁ exit_code:▁ 0");
  });

  it("still parses a clean legacy preview without an artifact", () => {
    const events = [
      updates("agent", [aiCall2("c1", "bash", { command: "echo 1" })]),
      updates("tools", [toolResult("c1", "stdout:\n1\n\nexit_code: 0")]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toEqual({ stdout: "1", stderr: "", exitCode: 0 });
  });
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd apps/admin-ui && npx vitest run src/api/__tests__/tool_timeline.test.ts`
Expected: 第 1 条 FAIL(execResult 为全空解析产物而非 artifact 值)、第 2 条 FAIL(execResult 是 truthy 空对象)、第 3 条 PASS。

- [ ] **Step 3: 实现 parser 侧**

`apps/admin-ui/src/api/tool_timeline.ts` 三处:

(a)`ToolCallEntry` 接口在 `execResult?: ExecResult;` 之后加:

```ts
  /** Structured exec fields lifted from the result's ``artifact``
   *  (``format_sandbox_outcome.meta`` — PR-D). Set only when the wire frame
   *  carried them; wins over text parsing in the attribution pass. */
  execArtifact?: ExecResult;
```

(b)结果侧 artifact 读取块(现 :302-313,`entry.action` 赋值之后)追加:

```ts
          // PR-D — sandbox exec tools stash their raw (pre-datamark) streams
          // here; the rendered content's newlines don't survive spotlight.
          const exit = rec.exit_code;
          if (typeof exit === "number" && Number.isFinite(exit)) {
            entry.execArtifact = {
              stdout: typeof rec.stdout === "string" ? rec.stdout : "",
              stderr: typeof rec.stderr === "string" ? rec.stderr : "",
              exitCode: exit,
            };
          }
```

(c)末尾归因循环(现 :318-323)整体替换为:

```ts
  const entries = order.map((id) => byId.get(id) as ToolCallEntry);
  for (const entry of entries) {
    if (entry.isMcp || !SANDBOX_TOOLS.has(entry.toolName)) continue;
    if (entry.execArtifact) {
      entry.execResult = entry.execArtifact;
      continue;
    }
    if (!entry.resultPreview) continue;
    const parsed = parseExecResult(entry.resultPreview);
    // A fully-empty parse means the preview was datamark-mangled (legacy
    // frames) — leave execResult unset so the raw-preview fallback renders.
    if (parsed.exitCode !== null || parsed.stdout !== "" || parsed.stderr !== "") {
      entry.execResult = parsed;
    }
  }
```

- [ ] **Step 4: 跑 parser 测试确认全绿**

Run: `cd apps/admin-ui && npx vitest run src/api/__tests__/tool_timeline.test.ts`
Expected: 全 PASS(含既有 `parseExecResult` 4 条与 exec attribution 既有 2 条)。

- [ ] **Step 5: 写渲染层失败测试(badge 保留)**

在 `apps/admin-ui/src/components/__tests__/ToolTimeline.test.tsx` 现有 exec 渲染用例旁追加。该文件已有 helper:`baseEntry(over)`(构造 `ToolCallEntry`)、`renderFireCard(entry)`(`<App>` 包一层渲染 `ToolCallCard`)、`openResultPanel()`(点开"结果"折叠面板 —— badge 在折叠头上常显,exit-code Tag 在面板体内):

```tsx
  it("keeps the untrusted badge when exec fields come from the artifact", () => {
    // Artifact-sourced stdout is raw (no ▁ glyph) — the badge must fall back
    // to sniffing the datamarked resultPreview.
    renderFireCard(
      baseEntry({
        rawName: "exec_python",
        toolName: "exec_python",
        resultPreview: "stdout:▁ 1▁ exit_code:▁ 0",
        execResult: { stdout: "1\n", stderr: "", exitCode: 0 },
      }),
    );
    expect(screen.getByTestId("tool-untrusted")).toBeInTheDocument();
    openResultPanel();
    expect(screen.getByTestId("tool-exit-code").textContent).toContain("0");
  });
```

- [ ] **Step 6: 跑渲染测试确认失败**

Run: `cd apps/admin-ui && npx vitest run src/components/__tests__/ToolTimeline.test.tsx`
Expected: 新用例 FAIL(`tool-untrusted` 缺失 —— artifact 版 stdout 无 ▁,现判据不看 resultPreview)。

- [ ] **Step 7: 实现渲染侧**

`apps/admin-ui/src/components/ToolTimeline.tsx` 的 `hadUntrusted`(现 :104-108)改为:

```ts
    const hadUntrusted =
      stdoutClean.hadUntrusted ||
      stderrClean.hadUntrusted ||
      stdout.includes("▁") ||
      stderr.includes("▁") ||
      // PR-D — artifact-sourced streams are raw (never datamarked); the
      // spotlight evidence now lives only in the mangled resultPreview.
      (entry.resultPreview?.includes("▁") ?? false);
```

- [ ] **Step 8: 前端全量验证**

Run: `cd apps/admin-ui && npm run test && npm run typecheck`
Expected: vitest 全 PASS,tsc 零错误。

- [ ] **Step 9: Commit**

```bash
git add apps/admin-ui/src/api/tool_timeline.ts apps/admin-ui/src/components/ToolTimeline.tsx apps/admin-ui/src/api/__tests__/tool_timeline.test.ts apps/admin-ui/src/components/__tests__/ToolTimeline.test.tsx
git commit -m "fix(admin-ui): exec 结果优先读 artifact 结构化字段——datamark 失配时恢复原文兜底(PR-D)"
```

---

### Task 3: worker 子时间线 —— exec artifact 摘要贯通

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/_worker_events.py:144-149`(`_summarize_message` 的 ToolMessage 分支)
- Modify: `apps/admin-ui/src/api/worker_timeline.ts`(`WorkerMessageSummary` + `summarizeMessages`)
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/StepTimeline.tsx:445`(worker 行内渲染)
- Test: `services/orchestrator/tests/test_worker_events.py`
- Test: `apps/admin-ui/src/api/__tests__/worker_timeline.test.ts`

**Interfaces:**
- Consumes: Task 1 的 artifact wire 契约(`msg.artifact` 上的 `exit_code` / `timed_out` / `stdout` / `stderr`);现有 `_excerpt(text, limit)` / `WORKER_RESULT_EXCERPT = 500`(_worker_events.py:27-29,108-109)。
- Produces: worker update 帧 ToolMessage 摘要新增可选键 `"exec" = {"exit_code": int, "timed_out": bool, "stdout_excerpt": str, "stderr_excerpt": str}`;前端 `WorkerMessageSummary.exec?: WorkerExecSummary`。

- [ ] **Step 1: 写后端失败测试(两条)**

在 `services/orchestrator/tests/test_worker_events.py` 现有 update-frame 用例之后追加(该文件已 import `ToolMessage` / `build_worker_update_frame` / `WORKER_RESULT_EXCERPT` / `_IDENT`;缺哪个补哪个 import):

```python
def test_update_frame_carries_exec_artifact_summary() -> None:
    # PR-D — a worker's exec_python/bash result keeps its structured fields
    # (excerpted to the frame's summary budget); the content excerpt alone is
    # datamark-mangled and unparseable.
    msg = ToolMessage(
        content="stdout:▁ 1▁ exit_code:▁ 0",
        tool_call_id="tc-1",
        name="exec_python",
        artifact={
            "exit_code": 0,
            "timed_out": False,
            "truncated": False,
            "stdout": "1\n" * 600,
            "stderr": "",
        },
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    exec_summary = tool["exec"]
    assert exec_summary["exit_code"] == 0
    assert exec_summary["timed_out"] is False
    assert exec_summary["stdout_excerpt"].startswith("1\n")
    assert len(exec_summary["stdout_excerpt"]) == WORKER_RESULT_EXCERPT + 1  # +1 = "…"
    assert exec_summary["stderr_excerpt"] == ""


def test_update_frame_ignores_non_exec_artifact() -> None:
    # manage_task-style artifacts (no exit_code) must not grow an exec key.
    msg = ToolMessage(
        content="ok",
        tool_call_id="tc-2",
        name="manage_task",
        artifact={"trigger_id": "t1", "action": "create"},
    )
    frame = build_worker_update_frame(
        _IDENT, wseq=1, node="tools", writes={"messages": [msg]}, duration_ms=5
    )
    (tool,) = frame["data"]["messages"]
    assert "exec" not in tool
```

- [ ] **Step 2: 跑后端测试确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_worker_events.py -q`
Expected: 第一条 FAIL(`KeyError: 'exec'`),第二条 PASS。

- [ ] **Step 3: 后端最小实现**

`services/orchestrator/src/orchestrator/tools/_worker_events.py` 的 ToolMessage 分支(现 :144-149)改为:

```python
    if isinstance(msg, ToolMessage):
        summary = {
            "type": "tool",
            "name": msg.name or "",
            "tool_result_excerpt": _excerpt(_text(msg.content), WORKER_RESULT_EXCERPT),
        }
        # PR-D — sandbox exec results carry structured streams in ``artifact``
        # (format_sandbox_outcome.meta); the content excerpt is datamarked and
        # unparseable. Excerpt them to the same summary budget as the content.
        artifact = getattr(msg, "artifact", None)
        exit_code = artifact.get("exit_code") if isinstance(artifact, dict) else None
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            summary["exec"] = {
                "exit_code": exit_code,
                "timed_out": bool(artifact.get("timed_out", False)),
                "stdout_excerpt": _excerpt(str(artifact.get("stdout", "")), WORKER_RESULT_EXCERPT),
                "stderr_excerpt": _excerpt(str(artifact.get("stderr", "")), WORKER_RESULT_EXCERPT),
            }
        return summary
```

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_worker_events.py -q`
Expected: 全 PASS。

- [ ] **Step 4: 写前端失败测试**

在 `apps/admin-ui/src/api/__tests__/worker_timeline.test.ts` 里追加。该文件已有 helper `wf(kind, over, data)`(构造 worker SSE 帧,缺省 `worker_id: "w-1"` / `parent_tool_call_id: "call-1"`):

```ts
  it("lifts the exec artifact summary off a worker tool message", () => {
    const map = parseWorkerFrames([
      wf("update", { wseq: 1 }, {
        node: "tools",
        _duration_ms: 5,
        messages: [{
          type: "tool",
          name: "exec_python",
          tool_result_excerpt: "stdout:▁ 1▁ exit_code:▁ 0",
          exec: { exit_code: 0, timed_out: false, stdout_excerpt: "1\n", stderr_excerpt: "" },
        }],
      }),
    ]);
    const [w] = map.get("call-1") ?? [];
    expect(w.steps[0].messages[0].exec).toEqual({
      exitCode: 0,
      timedOut: false,
      stdoutExcerpt: "1\n",
      stderrExcerpt: "",
    });
  });
```

Run: `cd apps/admin-ui && npx vitest run src/api/__tests__/worker_timeline.test.ts`
Expected: 新用例 FAIL(`m.exec` undefined)。

- [ ] **Step 5: 前端实现**

`apps/admin-ui/src/api/worker_timeline.ts` 两处:

(a)接口:

```ts
/** PR-D — excerpted structured exec fields riding a worker's tool summary. */
export interface WorkerExecSummary {
  exitCode: number;
  timedOut: boolean;
  stdoutExcerpt: string;
  stderrExcerpt: string;
}
```

`WorkerMessageSummary` 加 `exec?: WorkerExecSummary;`。

(b)`summarizeMessages` 的 `out.push(msg)` 之前加:

```ts
    const ex = r.exec;
    if (typeof ex === "object" && ex !== null) {
      const e = ex as Record<string, unknown>;
      if (typeof e.exit_code === "number") {
        msg.exec = {
          exitCode: e.exit_code,
          timedOut: e.timed_out === true,
          stdoutExcerpt: typeof e.stdout_excerpt === "string" ? e.stdout_excerpt : "",
          stderrExcerpt: typeof e.stderr_excerpt === "string" ? e.stderr_excerpt : "",
        };
      }
    }
```

`apps/admin-ui/src/pages/agent_detail/playground/StepTimeline.tsx:445` 的一行改为(exec 优先,fallback 原 excerpt):

```tsx
                  {m.exec
                    ? `→ exit ${m.exec.exitCode}${m.exec.stdoutExcerpt ? ` · ${m.exec.stdoutExcerpt}` : ""}${m.exec.stderrExcerpt ? ` ⚠ ${m.exec.stderrExcerpt}` : ""}`
                    : m.toolResultExcerpt
                      ? `→ ${m.toolResultExcerpt}`
                      : ""}
```

- [ ] **Step 6: 前端全量验证**

Run: `cd apps/admin-ui && npm run test && npm run typecheck`
Expected: 全 PASS(含 `StepTimeline.worker.test.tsx` 既有用例)、tsc 零错误。

- [ ] **Step 7: 后端全量回归(worker 相关)**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_worker_events.py tests/test_sse_worker_events.py tests/test_worker_event_bridge.py tests/test_spawn_worker.py -q`
Expected: 全 PASS。

- [ ] **Step 8: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/_worker_events.py services/orchestrator/tests/test_worker_events.py apps/admin-ui/src/api/worker_timeline.ts apps/admin-ui/src/pages/agent_detail/playground/StepTimeline.tsx apps/admin-ui/src/api/__tests__/worker_timeline.test.ts
git commit -m "feat(worker): 子时间线贯通 exec artifact 摘要——子 agent 沙箱结果可见(PR-D)"
```
