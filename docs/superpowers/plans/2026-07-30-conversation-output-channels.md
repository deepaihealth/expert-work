# 对话输出语义频道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** assistant 输出按结构规则标 `commentary`/`final` 频道,贯穿 `GET /messages`(第三方)与调试台序列渲染,过场旁白不再混进正文。

**Architecture:** 频道是纯结构推导(段内末条且无 tool_calls = final,其余 = commentary),后端在 `read_turns` 打标进 REST 响应,前端在 `turn_summary` 用同一规则从 updates 帧推导;SSE 帧格式零改动。Spec: `docs/superpowers/specs/2026-07-30-conversation-output-channels-design.md`。

**Tech Stack:** Python (FastAPI/LangGraph checkpoint) + React/TS (antd)。

## Global Constraints

- 频道判定**只准用结构事实**(`tool_calls` 是否非空、是否段内末条)——禁止任何基于文本内容/长度/顺序的推导(五家实测判死,见 spec)。
- 频道词汇恰为 `"commentary" | "final"`(assistant),user 恒 `null`;不引入其他值。
- SSE 帧格式、token 流(useTokenStream)、`thread_message` mirror 表结构不动。
- `finalText === null` 的「找审批闸」信号角色必须保留(PlaygroundTab 依赖)。
- commentary 段 UI 上**完整可见**(弱化样式,不删除、不折叠丢失)。
- 后端测试跑 CI 同款:`uv run pytest <file>`;orchestrator 测试须 `DOCKER_HOST= uv run pytest`(本机 docker sock 不在默认位);前端 `pnpm -C apps/admin-ui test -- --run <file>` + 收尾 `pnpm -C apps/admin-ui exec tsc -b --noEmit`。
- 提交遵循 conventional commits,无 attribution 尾行。

---

### Task 1: 后端 — `MessageTurn.channel` + `read_turns` 分段打标 + `/messages` 响应

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/thread_message/base.py:22-34`
- Modify: `services/control-plane/src/control_plane/transcript.py:55-74`
- Modify: `services/control-plane/src/control_plane/api/runs.py:1347`
- Test: `services/control-plane/tests/test_transcript_mirror_sweep.py`(read_turns 测试所在文件,:65-150 现有三个)

**Interfaces:**
- Consumes: 现有 `MessageTurn(seq, role, content)`、`read_turns(checkpointer, thread_id, *, include_hidden)`。
- Produces: `MessageTurn` 新增 `channel: str | None = None`(默认值,mirror sweep / quality_monitor_worker 零改动);`/messages` 响应行形状 `{"role", "content", "channel"}`。Task 4 的 `HistoryMessage.channel` 与此对齐。

- [ ] **Step 1: 写失败测试**(追加到 `test_transcript_mirror_sweep.py`,仿 :65 现有测试的 checkpointer stub 写法——先读该文件现有 `test_read_turns_*` 怎么构造 fake checkpointer,复用同款):

```python
async def test_read_turns_channels_commentary_and_final() -> None:
    """段内末条且无 tool_calls = final;带 tool_calls / 非末条 = commentary。"""
    msgs = [
        HumanMessage(content="写两章综述"),
        AIMessage(content="先搜第一章资料", tool_calls=[_tc("web_search")]),
        AIMessage(content="第一章正文…现在搜第二章", tool_calls=[_tc("web_search")]),
        AIMessage(content="第二章正文,全文完。"),
        HumanMessage(content="再补个结论"),
        AIMessage(content="补充搜索", tool_calls=[_tc("web_search")]),
    ]
    turns = await read_turns(_checkpointer_with(msgs), THREAD_ID)
    assert [(t.role, t.channel) for t in turns] == [
        ("user", None),
        ("assistant", "commentary"),
        ("assistant", "commentary"),
        ("assistant", "final"),
        ("user", None),
        # 第二段末条带 tool_calls(暂停/未完轮)→ 无 final
        ("assistant", "commentary"),
    ]


async def test_read_turns_channel_segments_reset_at_user_boundary() -> None:
    """reflect 打回形态:候选答案被追加消息取代后自动变 commentary。"""
    msgs = [
        HumanMessage(content="任务"),
        AIMessage(content="候选答案 v1"),  # 无 tool_calls,但非段末条
        HumanMessage(
            content="[Reflection] 不够好",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
        AIMessage(content="答案 v2"),
    ]
    turns = await read_turns(_checkpointer_with(msgs), THREAD_ID, include_hidden=False)
    assert [(t.role, t.channel) for t in turns] == [
        ("user", None),
        ("assistant", "commentary"),  # 隐藏行剔除后仍非末条
        ("assistant", "final"),
    ]
```

`_tc(name)` helper 返回 `{"name": name, "args": {}, "id": "call_1"}`(LangChain tool_call dict);`_checkpointer_with` / `THREAD_ID` 按该文件现有测试的构件名对齐(如已有同功能构件,直接复用,不重复造)。

- [ ] **Step 2: 跑测确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_transcript_mirror_sweep.py -k channel -v`
Expected: FAIL(`MessageTurn.__init__` 无 channel / 断言差异)

- [ ] **Step 3: 实现**

`base.py` dataclass 加字段(注释指到 spec):

```python
@dataclass(frozen=True)
class MessageTurn:
    """One user/assistant text turn extracted from a thread's checkpoint.

    ``seq`` is the message's index in the checkpoint's append-only
    ``messages`` channel — stable across reads (``add_messages`` reducer),
    so mirror writes are idempotent on ``(thread_id, seq)``. Non-text turns
    (tool/system) are skipped at extraction, leaving gaps in ``seq``.
    """

    seq: int
    role: str  # "user" | "assistant"
    content: str
    #: Structural output channel for assistant turns — "final" (last turn of
    #: its user-delimited segment AND no tool_calls) or "commentary"
    #: (everything else); always None for user turns. See
    #: docs/superpowers/specs/2026-07-30-conversation-output-channels-design.md.
    channel: str | None = None
```

`transcript.py` `read_turns` 收集循环改两遍(先收集含 `has_tool_calls`,再按段回填 channel)。保持现有过滤语义(hidden / 空文本)不变:

```python
    raw = (tup.checkpoint.get("channel_values") or {}).get("messages", [])
    collected: list[tuple[int, str, str, bool]] = []
    for seq, m in enumerate(raw):
        mtype = getattr(m, "type", None)
        if mtype not in ("human", "ai"):
            continue
        if not include_hidden:
            kwargs = getattr(m, "additional_kwargs", None) or {}
            if kwargs.get("expert_work_hide_from_ui"):
                continue
        text = message_text(getattr(m, "content", ""))
        if not text.strip():
            continue
        has_tool_calls = mtype == "ai" and bool(getattr(m, "tool_calls", None))
        collected.append((seq, mtype, text, has_tool_calls))
    out: list[MessageTurn] = []
    for i, (seq, mtype, text, has_tool_calls) in enumerate(collected):
        if mtype == "human":
            out.append(MessageTurn(seq=seq, role="user", content=text))
            continue
        # Channel is structural (spec): an assistant turn is "final" iff it is
        # the last visible turn of its user-delimited segment AND carries no
        # tool_calls; every other assistant turn is "commentary".
        nxt = collected[i + 1] if i + 1 < len(collected) else None
        last_in_segment = nxt is None or nxt[1] == "human"
        channel = "final" if last_in_segment and not has_tool_calls else "commentary"
        out.append(MessageTurn(seq=seq, role="assistant", content=text, channel=channel))
    return out
```

模块 docstring(:1-15)补一句频道语义与 spec 指针。

`runs.py:1347` 响应行加字段:

```python
        out = [{"role": t.role, "content": t.content, "channel": t.channel} for t in turns]
```

- [ ] **Step 4: 跑测确认通过 + 回归**

Run: `cd services/control-plane && uv run pytest tests/test_transcript_mirror_sweep.py tests/test_runs_api.py -v`
Expected: 全 PASS(现有三个 read_turns 测试不许改断言——channel 对它们透明)

- [ ] **Step 5: Commit**

```bash
git add packages/expert-work-persistence/src/expert_work/persistence/thread_message/base.py services/control-plane/src/control_plane/transcript.py services/control-plane/src/control_plane/api/runs.py services/control-plane/tests/test_transcript_mirror_sweep.py
git commit -m "feat(api): /messages assistant 行加结构频道 channel=commentary|final"
```

---

### Task 2: 后端 — reflect revise feedback 标 hide_from_ui

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/reflect.py:218-228`
- Test: `services/orchestrator/tests/test_reflect.py`

**Interfaces:**
- Consumes: `read_turns` 的 `expert_work_hide_from_ui` 过滤(Task 1 之前已存在,transcript.py:65-68)。
- Produces: revise 路径注入的 `HumanMessage` 携带 `additional_kwargs={"expert_work_hide_from_ui": True}`。

- [ ] **Step 1: 写失败测试**(追加到 `test_reflect.py`;先读该文件现有 revise 用例怎么驱动 `reflect_node`,复用其 fixture/stub):

```python
async def test_revise_feedback_hidden_from_ui() -> None:
    """revise 注入的 [Reflection] HumanMessage 必须标 hide_from_ui —— 否则
    /messages 出现假 user 消息,污染第三方对话历史并破坏历史轮 order-pairing。"""
    updates = await _run_reflect_returning(verdict="revise")  # 按现有用例的驱动方式取 updates
    injected = updates["messages"][0]
    assert injected.additional_kwargs.get("expert_work_hide_from_ui") is True
```

- [ ] **Step 2: 跑测确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_reflect.py -k hidden -v`
Expected: FAIL(additional_kwargs 空)

- [ ] **Step 3: 实现**(reflect.py:220-225):

```python
        if reflection.verdict == "revise":
            updates["messages"] = [
                HumanMessage(
                    content=f"[Reflection] {reflection.critique}\n\n"
                    "Address the feedback above, then continue.",
                    # Orchestrator-authored scaffolding (RT-ADR-9): keep it
                    # in-prompt and in the faithful record, but out of the UI
                    # bubble view / GET /messages — an unhidden feedback row
                    # renders as a user message the user never sent.
                    additional_kwargs={"expert_work_hide_from_ui": True},
                )
            ]
```

- [ ] **Step 4: 跑测确认通过 + 回归**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_reflect.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/src/orchestrator/graph_builder/reflect.py services/orchestrator/tests/test_reflect.py
git commit -m "fix(reflect): revise feedback 标 hide_from_ui——不再以假 user 消息污染对话历史"
```

---

### Task 3: 前端 — `turn_summary` segments + `finalText` 语义收紧

**Files:**
- Modify: `apps/admin-ui/src/api/turn_summary.ts`
- Test: `apps/admin-ui/src/api/__tests__/turn_summary.test.ts`

**Interfaces:**
- Consumes: updates 帧 AI 消息 dict 的 `tool_calls`(tool_timeline.ts:223 同款读法)。
- Produces(Task 4 依赖,签名逐字):

```ts
export type SegmentChannel = "commentary" | "final";
export interface AnswerSegment {
  text: string;
  channel: SegmentChannel;
}
// TurnSummary 变更:assistantTexts: string[] 删除,替换为
//   segments: AnswerSegment[]
// finalText 语义收紧:= final 段文本;无 final 段(末条 AI 文本消息带
// tool_calls,或无 AI 文本)→ null。null 信号角色(审批闸探测)保留。
```

- [ ] **Step 1: 写失败测试**(改造 `turn_summary.test.ts`;现有 assistantTexts 断言同步替换为 segments。fixture 用五家实测形态,构造 updates 事件的 helper 按该文件现有写法复用):

```ts
it("glm 形态:旁白/正文各自与 tool_calls 同帧 → 全 commentary,末条无 tool_calls = final", () => {
  const s = summarizeTurn([
    updatesEvent([aiMsg("好的!我将分两章完成", { toolCalls: 1 })]),
    updatesEvent([aiMsg("第一章正文…现在撰写第二章", { toolCalls: 1 })]),
    updatesEvent([aiMsg("第二章正文,全文完。")]),
  ]);
  expect(s.segments).toEqual([
    { text: "好的!我将分两章完成", channel: "commentary" },
    { text: "第一章正文…现在撰写第二章", channel: "commentary" },
    { text: "第二章正文,全文完。", channel: "final" },
  ]);
  expect(s.finalText).toBe("第二章正文,全文完。");
});

it("暂停/未完轮:末条 AI 文本带 tool_calls → 无 final,finalText null", () => {
  const s = summarizeTurn([
    updatesEvent([aiMsg("先搜资料", { toolCalls: 1 })]),
  ]);
  expect(s.segments).toEqual([{ text: "先搜资料", channel: "commentary" }]);
  expect(s.finalText).toBeNull();
});

it("qwen/doubao 形态:中间轮 content 全空 → 无 commentary 段,只有 final", () => {
  const s = summarizeTurn([
    updatesEvent([aiMsg("", { toolCalls: 1, reasoning: "想一想" })]),
    updatesEvent([aiMsg("最终答案")]),
  ]);
  expect(s.segments).toEqual([{ text: "最终答案", channel: "final" }]);
  expect(s.reasoning).toEqual(["想一想"]);
});
```

`aiMsg(text, opts)` helper 产出 `{type:"ai", content:text, tool_calls:[…] , additional_kwargs:{reasoning_content}}` 形状的消息 dict(`toolCalls: n` → n 个 `{name:"web_search",args:{},id:"call_i"}`)。

- [ ] **Step 2: 跑测确认失败**

Run: `pnpm -C apps/admin-ui test -- --run src/api/__tests__/turn_summary.test.ts`
Expected: FAIL(`segments` 不存在)

- [ ] **Step 3: 实现**——收集循环里把 `assistantTexts.push(text)` 替换为暂存 `{text, hasToolCalls}`(`hasToolCalls = Array.isArray(mm.tool_calls) && mm.tool_calls.length > 0`),循环结束后一次回填:

```ts
  // Channel is structural (spec 2026-07-30-conversation-output-channels):
  // within this turn, only the LAST assistant text message can be "final",
  // and only when it carries no tool_calls; everything else is commentary.
  const segments: AnswerSegment[] = pending.map((p, i) => ({
    text: p.text,
    channel:
      i === pending.length - 1 && !p.hasToolCalls ? "final" : "commentary",
  }));
  const finalSegment = segments.length > 0 ? segments[segments.length - 1] : null;
  const finalText = finalSegment?.channel === "final" ? finalSegment.text : null;
```

原 `finalText = text`(last-wins,:141)与 `assistantTexts` 相关行删除;模块头注释同步改写(#8 的 join 语义说明替换为频道语义 + spec 指针)。

- [ ] **Step 4: 跑测确认通过**

Run: `pnpm -C apps/admin-ui test -- --run src/api/__tests__/turn_summary.test.ts`
Expected: 全 PASS(此刻 TurnCard 仍引用 assistantTexts,tsc 会红——Task 4 收口,本 task 只保测试绿)

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/api/turn_summary.ts apps/admin-ui/src/api/__tests__/turn_summary.test.ts
git commit -m "feat(ui): turn_summary 按结构规则产出 commentary/final segments,finalText 收紧为 final 段"
```

---

### Task 4: 前端 — TurnCard 序列渲染 + 历史 fallback 分段

**Files:**
- Modify: `apps/admin-ui/src/components/turn/TurnCard.tsx`(:349-359 聚合、:518-528 fallback 渲染、:594-624 答案区)
- Modify: `apps/admin-ui/src/api/sessions.ts:114-117`(HistoryMessage)
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts`
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx:1332`、`apps/admin-ui/src/pages/ConversationDetail.tsx:498`(传参改名)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts`、`zh-CN.ts`(1 键)
- Test: `apps/admin-ui/src/components/turn/__tests__/TurnCard.test.tsx`、`apps/admin-ui/src/pages/agent_detail/playground/__tests__/history_turns.test.ts`

**Interfaces:**
- Consumes: Task 3 的 `AnswerSegment`/`SegmentChannel`/`summary.segments`/`finalText`;Task 1 的 `/messages` 行 `channel` 字段。
- Produces:

```ts
// sessions.ts
export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  /** Structural output channel (backend read_turns): assistant rows carry
   *  "commentary" | "final"; user rows are null. Absent on old payloads. */
  channel?: "commentary" | "final" | null;
}
// history_turns.ts
export interface FallbackLine { text: string; channel: "commentary" | "final" | null; }
export interface HistoryTurn {
  key: string;
  input: string;
  fallbackLines: FallbackLine[];   // 原 fallbackAnswer: string
  runId: string;
  status: string;
}
// TurnCard props: fallbackAnswer?: string → fallbackLines?: FallbackLine[]
```

- [ ] **Step 1: 写失败测试**(TurnCard.test.tsx 现有 fallback/答案区用例同步改;新增):

```tsx
it("答案区按段渲染:commentary 弱化行,final 走 Markdown 正文", () => {
  render(<TurnCard turn={settledTurnWith([
    aiUpdates("第一章资料已获取,现在撰写第一章正文。", { toolCalls: 1 }),
    aiUpdates("# 第一章\n正文内容"),
  ])} {...baseProps} />);
  const commentary = screen.getAllByTestId("turn-segment-commentary");
  expect(commentary).toHaveLength(1);
  expect(commentary[0]).toHaveTextContent("第一章资料已获取");
  // final 段经 MarkdownView 渲染出标题元素,且不含旁白文本
  const answer = screen.getByTestId("playground-turn-answer-scroll");
  expect(within(answer).getByRole("heading", { name: "第一章" })).toBeInTheDocument();
});

it("无 final 段(末条带 tool_calls)只渲染 commentary,不显示 no_text 占位", () => {
  render(<TurnCard turn={settledTurnWith([
    aiUpdates("先搜资料", { toolCalls: 1 }),
  ])} {...baseProps} />);
  expect(screen.getByTestId("turn-segment-commentary")).toHaveTextContent("先搜资料");
  expect(screen.queryByText(/turn_no_text/)).not.toBeInTheDocument();
});
```

`history_turns.test.ts`:现有断言 `fallbackAnswer` 改 `fallbackLines`(逐条 `{text, channel}`,channel 取自 HistoryMessage)。

- [ ] **Step 2: 跑测确认失败**

Run: `pnpm -C apps/admin-ui test -- --run src/components/turn/__tests__/TurnCard.test.tsx src/pages/agent_detail/playground/__tests__/history_turns.test.ts`
Expected: FAIL

- [ ] **Step 3: 实现**

TurnCard :349-359 聚合替换:

```tsx
  // Channelled segments (spec 2026-07-30): commentary rows render de-emphasised
  // in sequence order; the final row is the answer body. The old join("\n\n")
  // flattened narration INTO the answer — that's exactly the bug.
  const segments = summary.segments;
  const hasText = segments.length > 0;
  const fullText = segments.map((s) => s.text).join("\n\n");
```

答案区(:594-624)改序列渲染;commentary 行组件内联(240 clamp + FullTextTrigger,StepTimeline 同惯例;`MessageSquareText` 图标,lucide 已在用):

```tsx
        {hasText ? (
          <>
            <div
              ref={answerScrollRef}
              style={{ maxHeight: 420, overflowY: "auto" }}
              data-testid="playground-turn-answer-scroll"
            >
              {segments.map((seg, i) => {
                const isLast = i === segments.length - 1;
                // While streaming, the newest segment is still a candidate
                // answer — render it plainly; earlier segments are already
                // superseded (a later message exists), so they are
                // commentary regardless of their settled channel.
                const asCommentary =
                  turn.status === "running" ? !isLast : seg.channel === "commentary";
                if (asCommentary) {
                  return (
                    <div
                      key={i}
                      style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 6 }}
                      data-testid="turn-segment-commentary"
                    >
                      <MessageSquareText size={12} style={{ marginTop: 3, flexShrink: 0, color: "var(--ew-text-tertiary)" }} aria-label={t("playground.segment_commentary")} />
                      <Text type="secondary" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                        {seg.text.length > 240 ? `${seg.text.slice(0, 240)}…` : seg.text}
                      </Text>
                    </div>
                  );
                }
                return turn.status === "running" ? (
                  <Text key={i} style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
                    {seg.text}
                  </Text>
                ) : (
                  <MarkdownView key={i}>{seg.text}</MarkdownView>
                );
              })}
            </div>
            <FullTextTrigger
              onClick={() => setFullText({ title: t("playground.view_full_text"), text: fullText })}
            />
          </>
        ) : turn.status === "running" ? (
          <Text style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
            {t("playground.turn_running")}
          </Text>
        ) : turn.status !== "error" ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("playground.turn_no_text")}
          </Text>
        ) : null}
```

(`answerScrollRef` 的流式跟滚 effect 依赖从 `answer` 改为 `segments`。)

fallback 渲染(:518-528)改行数组,复用同款分支:

```tsx
        {fallbackLines && fallbackLines.length > 0 ? (
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            {fallbackLines.map((l, i) =>
              l.channel === "commentary" ? (
                <div key={i} style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 6 }} data-testid="turn-segment-commentary">
                  <MessageSquareText size={12} style={{ marginTop: 3, flexShrink: 0, color: "var(--ew-text-tertiary)" }} aria-label={t("playground.segment_commentary")} />
                  <Text type="secondary" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                    {l.text.length > 240 ? `${l.text.slice(0, 240)}…` : l.text}
                  </Text>
                </div>
              ) : (
                <MarkdownView key={i}>{l.text}</MarkdownView>
              ),
            )}
          </div>
        ) : (
```

(else 分支维持原 fallback-空态渲染。channel `null`/`"final"` 都走 MarkdownView——老 payload 无 channel 时整体当正文,不比现状差。)

`history_turns.ts` `buildHistoryTurns`:answers 收集改 `{text: m.content, channel: m.channel ?? null}` 行数组,`fallbackAnswer: p.answer` → `fallbackLines: p.answers`;头注释同步。PlaygroundTab:1332 / ConversationDetail:498 传参改 `fallbackLines={h.fallbackLines}`。

i18n 两 locale 各加 1 键(playground 节内):`segment_commentary: "过程输出"` / `segment_commentary: "Progress note"`(先 grep 两 locale 确认键不撞——同名键 esbuild 静默覆盖)。

- [ ] **Step 4: 跑测确认通过 + 全量收口**

Run:
```bash
pnpm -C apps/admin-ui test -- --run src/components/turn src/pages/agent_detail/playground/__tests__/history_turns.test.ts src/api/__tests__/turn_summary.test.ts
pnpm -C apps/admin-ui exec tsc -b --noEmit
pnpm -C apps/admin-ui test -- --run
```
Expected: 全 PASS + tsc 零错(assistantTexts 残留引用在此暴露并清除)

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src
git commit -m "feat(ui): 调试台答案区按频道序列渲染——commentary 弱化行+final 正文,历史 fallback 同步分段"
```

---

## Self-Review 记录

- Spec 覆盖:后端 channel(T1)/reflect 污染(T2)/segments(T3)/TurnCard+fallback+对话详情页(T4,components/turn 家族共享故 ConversationDetail 自动同步)/SSE 零改动(无任务,符合 spec)。API 文档:仓内无独立对外 API 文档文件,推导规则以 runs.py 端点 docstring + spec 承载(T1 内)。
- 类型一致:`AnswerSegment`/`SegmentChannel`(T3 产出,T4 消费)、`FallbackLine`/`fallbackLines`(T4 内自洽)、`channel` 词汇三处逐字相同。
- 无占位符;每个代码步骤有完整代码。
