"""Canonical LangGraph state shape for orchestrator graphs.

Per [STREAM-E-DESIGN § 2.3](../../../../docs/streams/STREAM-E-DESIGN.md),
fields are added incrementally across the Stream E sub-PRs:

- **E.1**: ``messages`` (LangGraph reducer-style append)
- **E.6**: ``step_count`` + ``max_steps`` for the ReAct loop guard

Every ``AgentState`` channel is checkpointed (dill), so **non-serialisable
runtime objects do not live here**. They travel via the
``config["configurable"]`` channel instead — it is per-invocation and not
checkpointed:

- Tenant binding (``tenant_id`` / ``session_id`` / ``run_id``) — LangGraph idiom.
- ``cancellation_token`` (E.15) — backed by a live ``asyncio.Event``.
- The ``LLMRouter`` holds its own provider chain + fallback state (E.11).
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from expert_work.protocol import (
    ApprovalRequest,
    MemoryItem,
    Plan,
    Reflection,
    SubAgentInvocation,
)

# CM-1 — the channel's element type lives in the ``tools`` layer (same
# layer as the L-4 ``mutation_classifier`` this generalises), so a normal
# runtime import is cycle-free and lets ``get_type_hints(AgentState)``
# resolve the annotation when LangGraph introspects the state schema.
from orchestrator.tools.error_classifier import ClassifiedToolError

#: Default ReAct hard limit — see Mini-ADR E-6 in the design doc + the
#: "ReAct 无限循环" risk row. Manifest may override per-agent.
DEFAULT_MAX_STEPS = 20


def _merge_promoted(existing: list[str] | None, new: list[str] | dict[str, list[str]]) -> list[str]:
    """Reducer for :attr:`AgentState.promoted_tools` — Stream TE-6 / HX-12.

    ``find_tools`` writes the names of deferred tools it just retrieved; this
    reducer unions them into the run's accumulated set, deduplicating while
    keeping a stable order (``existing`` first, then names from ``new`` not
    already present). Accumulating across turns means a tool stays promoted
    once retrieved. The state lives on the LangGraph channel — per-thread,
    checkpointed — so promotion never leaks into the cached registry.

    Stream HX-12 (Mini-ADR HX-I5) adds a removal shape for the demotion
    path: ``new`` may be ``{"add": [...], "remove": [...]}``. The plain
    ``list`` shape keeps the original add-only semantics so every existing
    write site is untouched; removal never deletes the tool from the
    registry's deferred pool — a demoted tool is re-promotable any time.
    """
    if isinstance(new, dict):
        to_add = list(new.get("add", []))
        to_remove = set(new.get("remove", []))
    else:
        to_add = list(new)
        to_remove = set()
    out: list[str] = [name for name in (existing or []) if name not in to_remove]
    seen = set(out)
    for name in to_add:
        if name not in seen and name not in to_remove:
            out.append(name)
            seen.add(name)
    return out


def _merge_last_used(existing: dict[str, int] | None, new: dict[str, int]) -> dict[str, int]:
    """Reducer for :attr:`AgentState.promoted_tool_last_used` — Stream HX-12.

    Per-key max merge: ``tools_node`` stamps the current ``step_count`` for
    every promoted tool that dispatched (and for names freshly promoted in
    the batch, so each entry has a baseline). The demotion gate compares
    these stamps against the current step to find stale promotions.
    """
    out = dict(existing or {})
    for name, step in new.items():
        if step > out.get(name, -1):
            out[name] = step
    return out


def _merge_viewed_figures(left: list[str], right: list[str]) -> list[str]:
    """跨轮累积已看过的页 ref —— union 去重,按**最近一次看到**排序。

    次序是滑窗的前提:``_figure_block_tail`` 取「最新 N 条」靠的是列表次序。

    Task 7 改了口径:重看同一页**要**把它挪到队尾(原来是按首次看到排、重看不挪)。
    原来的顾虑是「挪到队尾会把一张更早看过、模型还在用的图挤出窗口」—— 这个顾虑
    不成立:被重看的那页若本来就在最新 N 条里,挪到队尾之后最新 N 条还是同一批,
    谁也没被挤出去;只有它本来已经退出窗口时,挪动才会改变窗口 —— 而那正是想要的。
    反过来,按首次看到排有两个真问题,都因为同一个 run 里重读同一页拿到的是
    **逐字相同**的 ref:

    * 退出窗口的页再怎么重读也回不来 —— 而占位文字告诉模型的正是「再调一次
      read_page」;
    * 文档改了又改回去(内容哈希 A → B → A),重读得到的 A 版 ref 仍停在 B 前面,
      ``_figure_block_tail`` 就会把 B 当成当前版本、把 A 标成旧版本 —— 反了。
    """
    # 同一批里重复出现的,按**最后一次**出现的位置算(``[A, B, A]`` → ``[B, A]``)。
    fresh = list(reversed(dict.fromkeys(reversed(right))))
    moved = set(fresh)
    return [ref for ref in left if ref not in moved] + fresh


class AgentState(TypedDict):
    """State threaded through every orchestrator LangGraph node.

    ``messages`` uses LangGraph's ``add_messages`` reducer so nodes
    returning ``{"messages": [...]}`` append to (rather than overwrite)
    the conversation history. ``step_count`` and ``max_steps`` use the
    default overwrite reducer — the agent node sets the new count each
    turn, and ``max_steps`` is configured once at graph construction.

    ``plan`` (Stream J.1) is set once by the ``planner`` node when the
    manifest's ``workflow.type`` is ``plan_execute``; it is absent for
    plain ``react`` graphs. ``NotRequired`` so the ReAct input shape is
    unchanged — readers use ``state.get("plan")``.

    ``reflections`` (Stream J.2) accumulates one :class:`Reflection` per
    ``reflect`` node entry — an ``operator.add`` reducer appends. Absent
    unless the manifest carries a ``reflection:`` block.

    ``recalled_memories`` (Stream J.3) is set once by the ``memory_recall``
    node — the long-term memories ``agent_node`` renders into its system
    context. Absent unless the manifest enables long-term memory.

    ``step_count_refund_pending`` (Stream L.L5 / Mini-ADR L-5) is the
    narrow channel a ``tools_node`` writes when one or more tools
    returned :attr:`~orchestrator.tools.registry.ToolResult.refund_iterations`
    greater than zero. The next ``agent_node`` subtracts it from
    ``step_count`` (clamped at 0) before computing the post-turn count,
    then resets the channel to ``0``. Keeps refund accounting
    observable and auditable instead of letting tools rewrite
    ``step_count`` directly.

    ``tool_failures`` (Stream CM-1, generalising L.L4) accumulates
    :class:`~orchestrator.tools.error_classifier.ClassifiedToolError`
    rows for tool calls that failed in the most recent ``tools`` batch —
    both error-path failures (classified at the catch site from the real
    exception) and the success-path ``mutation_not_landed`` case folded
    in from L-4's mutation classifier. The next ``agent_node`` reads the
    list, emits a ``<recovery-advisory>`` ``HumanMessage`` with grounded
    per-tool recovery guidance, and resets the channel to ``[]``.
    Defaults to empty; tools_node only writes when at least one tool
    failed.

    ``subagent_invocations`` (Stream J.4-补强-2 / Mini-ADR J-40)
    accumulates one
    :class:`~expert_work.protocol.subagent.SubAgentInvocation` per
    SubAgentTool delegation — every outcome path (success / max_steps /
    cancelled / future timed_out) appends a terminal-state row via the
    ``operator.add`` reducer. Lets the parent's LangGraph checkpoint
    carry the full delegation history (audit + J.13 eval replay), and
    feeds future M2-B fan-in aggregation (iteration_used sum /
    llm_call_count sum / wall_clock_ms max). Absent unless the manifest
    declares ``subagents``.

    ``pending_approval`` (Stream J.8 / Mini-ADR J-24) carries the
    :class:`~expert_work.protocol.approval.ApprovalRequest` a run is
    paused on — ``tools_node`` writes it before the run routes to END
    (RunStatus.PAUSED). The overwrite reducer applies: a resume clears
    it back to ``None``. Absent on a run that has never paused.

    ``approval_resume`` (Stream J.8-step3b) is the transient channel the
    resume endpoint writes via ``aupdate_state`` — a
    ``{"decision", "modified_args"}`` dict. ``tools_node`` reads it on
    re-entry to apply the human verdict (approve dispatches the gated
    tool_call, modify rewrites its args, reject synthesises a rejection
    ``ToolMessage``) and clears it back to ``None``.

    ``approval_outcome`` (Stream J.8-step3b) is the terminal signal a
    declarative-gate *reject* sets — ``_after_tools`` routes the run to
    END when it is ``"rejected"`` (the platform vetoed the run). An
    agent-initiated ``ask_for_approval`` reject leaves it unset so the
    run loops back to the agent.

    ``promoted_tools`` (Stream TE-6) carries the names of deferred tools
    the ``find_tools`` meta-tool has retrieved this run. ``find_tools``
    writes via :attr:`ToolResult.state_updates`; the ``_merge_promoted``
    reducer union-dedupes across turns. The next ``agent_node`` adds the
    matching deferred specs to the LLM bind so the promoted tools become
    callable. Per-thread + checkpointed, so promotion stays isolated to
    the run and never mutates the cached registry. Absent (treated as ``[]``)
    until ``find_tools`` first promotes — zero behaviour change when no
    tool is deferred.

    ``last_projection_hash`` (Stream CM-0 / Mini-ADR CM-A3) is the content
    digest of the most recent ``DB → /workspace`` projection (PLAN.md /
    TODO.md / MEMORY.md). ``tools_node`` passes it to the
    :class:`~orchestrator.context.WorkspaceProjector` as the only-if-changed
    baseline and writes the new digest back, so an unchanged turn skips the
    sandbox round-trip. Absent until the first projection; ``None`` means
    nothing has been projected yet.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    max_steps: int
    #: 本轮用户附件 —— 文档的工作区路径(``uploads/<name>``)与图片的
    #: ``expert_work://image/...`` 引用。委派时结构性地进子代的种子消息:子代
    #: 不继承本对话,``[file attached: …]`` 那一行和贴在消息上的图片它都看不到。
    #:
    #: 放在 state 而不是 ``config["configurable"]``,是为了让 checkpoint 替我们
    #: 保住它:审批续跑与 orphan 复活都是 ``graph_input=None`` 从检查点恢复,
    #: 续的是**同一轮**,附件理应还在。
    #:
    #: 因此 ``build_run_graph_input`` 必须**每一轮都设**,哪怕是空列表 ——
    #: 对话是长线程,省略这个键时 LangGraph 保留检查点里的旧值,上一轮的附件
    #: 会漏进这一轮的子代。
    turn_documents: NotRequired[list[str]]
    turn_image_refs: NotRequired[list[str]]
    plan: NotRequired[Plan | None]
    reflections: NotRequired[Annotated[list[Reflection], add]]
    recalled_memories: NotRequired[list[MemoryItem]]
    step_count_refund_pending: NotRequired[int]
    #: Stream CM-9 (Mini-ADR CM-J5) — transient escalation signal. Set by
    #: agent_node when the loop-detection middleware flags a repeat
    #: (``ctx.payload["loop_detected"]``); the NEXT agent step consumes it
    #: (one turn on the escalated, higher-effort caller) and resets it.
    escalate_next: NotRequired[bool]
    #: No-progress stop — consecutive loop-detection trips. agent_node
    #: increments it on each turn the loop middleware flags a repeat and
    #: resets it to 0 on a clean turn. Once it reaches ``max_no_progress``
    #: (> 0) the node forces the same tool-less graceful wrap-up ``max_steps``
    #: uses, stopping a stuck run early instead of grinding to ``max_steps``.
    no_progress_streak: NotRequired[int]
    #: Per-run consecutive-no-progress cap (0 = off). Configured once at graph
    #: construction from ``policies.max_no_progress``; mirrors ``max_steps``.
    max_no_progress: NotRequired[int]
    #: Stream CM-11 — the plan goal as of the previous agent turn. agent_node
    #: sets it each turn to the current ``plan.goal`` (``None`` for react
    #: graphs). A change versus the live plan goal — a re-plan, or a human
    #: PLAN.md edit ingested via CM-0 — fires one escalated "re-calibrate"
    #: turn (Mini-ADR CM-M1). Absent on the first plan turn, so the initial
    #: decomposition (already deep-thought by the planner) never re-fires.
    last_plan_goal: NotRequired[str | None]
    tool_failures: NotRequired[list[ClassifiedToolError]]
    #: B-85 ③ / B-84 第 3 条 —— **整个 run** 里还没被抵消掉的非 transient 工具失败,
    #: 按首次出现的顺序排。
    #:
    #: 与 ``tool_failures`` 的区别是它**不按轮重置**:``tool_failures`` 被
    #: ``agent_node`` 读完、发完 ``<recovery-advisory>`` 就清空,走到 END 时
    #: 恒为空,拿不到。
    #:
    #: 记账键是 ``(资源空间, 标识)``,**按写的是哪份东西记,不按哪个工具写的它**:
    #: ``save_artifact`` → ``("artifact", name)``、``write_file`` / ``edit_file``
    #: → ``("file", path)``、取不到路径的 → ``("tool", 工具名)``。
    #:
    #: * 本批出现非瞬态失败 → 以该键记一条;同键已有就**保留先出现的那条**
    #:   (第一条错误信息比最后一条有用);
    #: * 本批有同键的**成功**调用 → 把该键那条删掉;
    #: * transient 一律不进账 —— 可重试的抖动不是「没做成」的证据。
    #:
    #: 前身是「只看最后一批」的 ``last_batch_failures``,它漏掉了整整一格:批 1
    #: 调工具 A 失败、批 2 调工具 B 成功、然后模型给出文字答复 —— 终局那一批是
    #: 干净的,于是判 ``completed=true``,而 A 从来没成功过。照 hermes-agent 的
    #: ``turn_explainers._record_file_mutation_result`` 改成按键记账 + 抵消
    #: (差别是它只管文件变更类工具,这里管全部工具)。
    #:
    #: **已知局限**:取不到路径的工具只有工具名这一层粒度 —— 一次失败的
    #: ``exec_python`` 会被另一次跑完全不相干脚本的 ``exec_python`` 抵消掉。
    #: 方向是少报不是多报,扩更多工具的路径提取不在本条范围内。
    #:
    #: 为什么写在 ``tools`` 节点而不是 ``agent_node``:后者分不清「这批工具全成功」
    #: 与「这一轮压根没跑工具」,而 ``tools`` 节点只在真跑过一批时才执行 ——
    #: 没跑工具的轮次天然不动这个通道,不会把前面的欠账抹掉。
    unresolved_failures: NotRequired[list[ClassifiedToolError]]
    #: B-85 ③ —— run 从哪个出口结束的,由**知道答案的那一行**盖章。
    #:
    #: 封闭取值:``text_response`` / ``max_steps`` / ``no_progress`` /
    #: ``token_budget`` / ``approval_pending`` / ``approval_rejected``。
    #:
    #: 不在 ``_should_continue`` / ``_after_tools`` 里盖 —— 它们是 LangGraph 的
    #: conditional-edge 函数,只返回路由、不写 state;而且撞预算的三种情况走的是
    #: 「一次无工具的收尾轮」,到了路由那一步与自然结束**逐字相同**,分不出来。
    exit_reason: NotRequired[str]
    subagent_invocations: NotRequired[Annotated[list[SubAgentInvocation], add]]
    pending_approval: NotRequired[ApprovalRequest | None]
    approval_resume: NotRequired[dict[str, Any] | None]
    approval_outcome: NotRequired[str | None]
    promoted_tools: NotRequired[Annotated[list[str], _merge_promoted]]
    #: Stream HX-12 — step_count stamp of each promoted tool's last dispatch
    #: (baseline = the step it was promoted). Feeds the demotion gate: a
    #: promoted tool unused for N turns is dropped from ``promoted_tools``
    #: when the compressor fires (it stays in the deferred pool — only the
    #: per-turn bind slims down).
    promoted_tool_last_used: NotRequired[Annotated[dict[str, int], _merge_last_used]]
    last_projection_hash: NotRequired[str | None]
    #: 动态子智能体委派增强(层 1)— identity hash (goal + step descriptions,
    #: statuses excluded) of the last plan the ``tools_node`` nudged the agent
    #: about after an ``update_plan``. Dedupe key: one delegation nudge per
    #: plan version — a re-issue or a pure progress-marking update never
    #: re-nudges; a structurally new plan may. Absent until the first nudge;
    #: only ever written when the registry carries ``spawn_worker``.
    delegation_nudge_plan_hash: NotRequired[str | None]
    #: B-35(plan_first 分发轮)— identity hash (goal + step descriptions +
    #: execution markers, statuses excluded) of the last plan version a
    #: dispatch turn ran for. Dedupe key: one dispatch turn per plan version;
    #: a structural replan (new/changed delegate steps) re-fires. All three
    #: channels are only ever written when the build has ``plan_first`` on.
    plan_first_dispatch_plan_hash: NotRequired[str | None]
    #: B-35 — True while the just-finished agent turn was a dispatch turn;
    #: ``_should_continue`` routes a tool-less reply back to ``agent``
    #: (retry / degrade) instead of ending the run.
    plan_first_dispatch_active: NotRequired[bool]
    #: B-35 — 0 on a fresh dispatch turn, 1 once the single retry was spent;
    #: the next refusal degrades (full tools restored) instead of looping.
    plan_first_dispatch_retries: NotRequired[int]
    #: B-64 —— ``read_page`` 渲出来、已经给过模型的页 ref,按**最近一次**看到的
    #: 次序(见 :func:`_merge_viewed_figures`)。跨 run 累积:run 的起始输入
    #: (``control_plane.api.runs``)不写这个键,检查点里的旧值于是整条会话一直在。
    #: 检查点里只有这些字符串(几十字节一条),**图片字节从不落库**:
    #: 块每轮由 ``_figure_block_tail`` 重建,与工作区快照同一口径(CM-C4)。
    viewed_figures: NotRequired[Annotated[list[str], _merge_viewed_figures]]
