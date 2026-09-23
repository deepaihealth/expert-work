"""Tool Protocol + ``ToolRegistry`` — Stream E.6.

Concrete tool adapters (``web_search`` E.7, ``http`` E.8, ``mcp:*`` E.9,
``exec_python`` F.4) all implement :class:`Tool` and register here. The
ReAct graph (``orchestrator.graph_builder``) reads
:meth:`ToolRegistry.specs` to hand the LLM the list of callable tools,
and dispatches by name via :meth:`ToolRegistry.get`.

Tool ``call`` exceptions are wrapped into ``ToolMessage(error=...)`` by
the graph's ``tools`` node (per Mini-ADR E-12 in
[STREAM-E-DESIGN](../../../../../docs/streams/STREAM-E-DESIGN.md)) —
adapters can raise freely; the LLM sees the error as a tool result and
reasons about retry / different args / final answer.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from expert_work.protocol import Plan
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.tools._budget import DelegationGate, WorkerSpawnBudget
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.ranking import build_document, rank_tools

#: Stream TE-1 — a tool's effect on the world. Descriptive metadata only in
#: TE-1; intended to drive the side-effect-aware scheduler / approval gate
#: (TE-4) and per-tool audit (TE-2). Three levels:
#: - ``read_only``: observes state only (file read, search) — intended to be
#:   safe to parallelise.
#: - ``reversible``: mutates recoverable state (write/overwrite an artifact or
#:   workspace file that can be re-written) — intended to serialise on path
#:   conflict.
#: - ``irreversible``: effects that cannot be cleanly undone (shell command,
#:   sending an email, destructive ops) — intended to be forced serial +
#:   approval-gated.
#: ``None`` on a :class:`ToolSpec` means "derive from ``is_read_only``" (see
#: :attr:`ToolSpec.resolved_side_effect`) so existing tools keep their behaviour.
SideEffectLevel = Literal["read_only", "reversible", "irreversible"]


#: Stream TE-6 — cap on a ``find_tools`` query length before it is compiled as
#: a regex. A model-derived pattern past this falls back to substring matching,
#: bounding the catastrophic-backtracking (ReDoS) surface.
_MAX_SEARCH_QUERY_LEN = 200


def _haystack(spec: ToolSpec) -> str:
    """Lower-cased ``name + description`` for Stream TE-6 substring matching."""
    return f"{spec.name}\n{spec.description}".lower()


@dataclass(frozen=True)
class ToolSpec:
    """Static descriptor of a tool — handed to the LLM for tool selection.

    ``is_read_only`` and ``path_args`` (Stream L.L6) feed the ReAct
    ``tools`` node's adaptive parallel scheduler. Read-only tools
    without overlapping paths run concurrently; conflicting calls
    serialise. Defaults (``False`` / ``()``) are deliberately
    conservative — a third-party tool that doesn't opt in stays on the
    sequential path it had before L6. See [STREAM-L-DESIGN § 3.L6](
    ../../../../../docs/streams/STREAM-L-DESIGN.md) + Mini-ADR L-6.

    ``side_effect`` and ``idempotent`` (Stream TE-1) add a richer
    side-effect classification consumed later by the side-effect-driven
    scheduler / approval gate (TE-4) and per-tool audit (TE-2). Both
    default to a value that preserves current behaviour: ``side_effect``
    derives from ``is_read_only`` via :attr:`resolved_side_effect` and
    ``idempotent`` defaults ``False``. See [STREAM-TE-DESIGN § TE-ADR-1](
    ../../../../../docs/streams/STREAM-TE-DESIGN.md).
    """

    name: str
    description: str
    #: JSON Schema for the tool's ``args`` parameter.
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: Stream L.L6 — when ``True``, multiple invocations of this tool
    #: (and concurrent invocations of other read-only tools) may run in
    #: parallel without conflict. Tools that mutate filesystem,
    #: ``AgentState``, sandbox, or any third-party state MUST keep the
    #: default ``False`` — they serialise against any tool that touches
    #: the same path (or against every other call, when no path is
    #: declared).
    is_read_only: bool = False
    #: Stream L.L6 — argument names whose values are filesystem-like
    #: paths the tool reads or writes. The scheduler detects conflicts
    #: between two tool calls by comparing the resolved values of
    #: these args. Empty tuple means "this tool has no per-call path";
    #: combined with ``is_read_only=False`` that yields the worst-case
    #: "conflicts with every other tool" stance (e.g., ``update_plan``
    #: writes ``AgentState.plan``, a global channel).
    path_args: tuple[str, ...] = ()
    #: Mini-ADR J-40 (J.4-补强-2) — a tool that **mutates** but whose
    #: invocations are nevertheless independent of one another. Multiple
    #: calls to such a tool (including against the same name) may share
    #: a stage and run via ``asyncio.gather``. ``SubAgentTool`` is the
    #: canonical example: each delegation spins up a fresh child
    #: ``thread_id`` / sandbox session, so two sub-agent calls don't
    #: collide. Defaults to ``False`` — third-party tools stay on the
    #: ``is_read_only`` path.
    is_parallel_safe: bool = False
    #: Stream J.7a (Mini-ADR J-23) — name of the skill that contributed
    #: this tool to the agent's registry, or ``None`` when the tool is
    #: declared directly in the manifest's ``tools:`` block. The dispatch
    #: path uses this to label the ``expert_work_skill_call_total`` /
    #: ``expert_work_skill_call_errors_total`` metrics so per-skill usage can
    #: be observed (Mini-ADR J-23 § 15.4 telemetry 双 counter).
    from_skill: str | None = None
    #: Stream TE-1 — explicit side-effect classification. ``None`` means
    #: "derive from ``is_read_only``" (see :attr:`resolved_side_effect`),
    #: which keeps every existing tool's behaviour unchanged. A tool whose
    #: effects cannot be cleanly undone (e.g. ``bash``, ``send_email``,
    #: destructive MCP ops) declares ``"irreversible"`` so the TE-4
    #: scheduler forces it serial and the approval gate triggers on it.
    #: This is purely descriptive metadata until TE-4 wires it into
    #: scheduling/gating — TE-1 adds no behavioural change.
    side_effect: SideEffectLevel | None = None
    #: Stream TE-1 — whether repeating this call with the same args is safe
    #: (no additional effect). Read-only tools are inherently idempotent;
    #: a write that overwrites to a fixed content is too, but e.g. an
    #: "append" or "send" is not. Conservative default ``False``. Reserved
    #: for retry/self-correction logic; not yet consumed in TE-1.
    idempotent: bool = False
    #: Stream HX-13 (Mini-ADR HX-J2) — vendor-native disclosure marker, set
    #: per-bind by ``agent_node`` (via ``dataclasses.replace`` copies, never
    #: on the registry's own specs). One flag serves both vendor tiers:
    #: Anthropic sends marked tools with ``defer_loading: true`` (server-side
    #: tool search); OpenAI/Azure excludes marked tools from the
    #: ``tool_choice.allowed_tools`` subset (full schema stays on the wire).
    #: Default ``False`` — the application tier (HX-12) never sets it.
    defer_loading: bool = False
    #: B-61 §5.2 — the JSON Schema the **callee** enforces, when that differs
    #: from the one the model is shown. ``None`` (every tool but a bound MCP
    #: one) means ``parameters`` IS the whole contract.
    #:
    #: Why two: binding a parameter deletes it from ``parameters`` so the model
    #: cannot fill it (and cannot retype it wrong) — but the server on the other
    #: end never agreed to that narrowing. It still declares the parameter, still
    #: lists it in ``required``, and with ``additionalProperties: false`` (the zod
    #: / MCP TS SDK default, and pydantic ``extra="forbid"``) it would reject the
    #: platform-filled value as an unknown property. Pre-dispatch validation must
    #: therefore run against THIS schema, not the narrowed one; validating against
    #: the narrowed one makes a correctly configured binding undispatchable.
    #:
    #: Note what this is NOT: an exemption. The bound parameter is still validated
    #: — type, enum, pattern and all — it is just validated against the contract
    #: the callee published. Skipping it instead would leave the platform-injected
    #: value unchecked by anyone.
    dispatch_parameters: Mapping[str, Any] | None = None

    @property
    def resolved_side_effect(self) -> SideEffectLevel:
        """Effective side-effect level: explicit value, else derived.

        When :attr:`side_effect` is unset, derive a conservative level
        from :attr:`is_read_only` so legacy tools that never declared a
        level still classify correctly: read-only tools are ``read_only``,
        everything else is ``reversible`` (not ``irreversible`` — a tool
        must opt in to the gated tier explicitly, preserving today's
        behaviour where no tool is auto-gated).
        """
        if self.side_effect is not None:
            return self.side_effect
        return "read_only" if self.is_read_only else "reversible"


#: B-61 §5.4 + B-65 —— 一条绑定落空的三种原因,见 :class:`UnmatchedArgBinding`。
UnmatchedReason = Literal["tool_missing", "params_absent", "name_collision"]


@dataclass(frozen=True)
class UnmatchedArgBinding:
    """B-61 §5.4 —— 一条参数绑定没能落到真实工具上,以及落空到什么程度。

    只带**名字**:server、裸工具名、参数名。绑定的值是客户的真实资料(项目号、
    姓名),这条记录会走进保存响应和运行期日志,一个值都不能带。

    ``reason`` 分开三种落空。后果都一样 —— 参数回到模型手里,模型继续手抄长串 ——
    但给配置的人的话完全不同:

    * ``tool_missing``:这个工具在组装出来的目录里根本不存在(server / tool 名字
      写错,或那台服务器这次没挂上)。
    * ``params_absent``:工具在,只是它已经不声明这几个参数了(上游改了接口)。
    * ``name_collision`` (B-65):工具在、参数也在、绑定当时确实落上了,但它折叠出来
      的 wire 名被**另一个**工具占走了(``mcp_tool_name`` 非法字符折 ``_`` 且截断到
      64 字符,``register`` 又按名字覆盖),后注册的那个把这条绑定顶掉了。对配置的人
      说「目录里没有这个工具」是假话 —— 工具明明在,坏的是名字撞了。
    """

    server: str
    tool: str
    #: 落空的参数名。``tool_missing`` / ``name_collision`` 时是这条绑定的全部参数。
    params: tuple[str, ...]
    reason: UnmatchedReason


@dataclass(frozen=True)
class ToolCatalogEntry:
    """One row of the registry's "everything registered" projection (PR-A.3).

    The console's Schema tab wants the JSON Schema the model was handed
    (``parameters``) plus provenance — including tools that are deferred
    (not in ``specs()``) so a promoted-on-demand call still resolves.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    #: ``source_of(name)`` — ``"builtin"`` / ``"mcp:<server>"`` / ...
    source: str
    from_skill: str | None
    deferred: bool


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation context threaded from the ReAct ``tools`` node.

    Most fields are optional because E.6 / E.7 tools didn't need any
    of them; E.8 HTTPTool is the first to require ``tenant_id`` (for
    the per-tenant allowlist lookup). Future tools read ``run_id`` for
    audit attribution. ``user_id`` (Stream J.15) scopes ``exec_python``'s
    persistent workspace volume — ``None`` when the run has no user
    binding. ``cancellation_token`` (Stream J.4) lets a tool propagate
    the run's cancellation into work it spawns — notably ``SubAgentTool``
    threading it into a child agent run.
    """

    tenant_id: UUID | None = None
    run_id: UUID | None = None
    user_id: UUID | None = None
    #: originating conversation thread — set on the normal + trigger run paths;
    #: manage_task stamps it as a task's delivery target (Spec 1).
    thread_id: UUID | None = None
    #: True when this run was started by the scheduler (fire_trigger). The
    #: manage_task tool is filtered from the LLM bind list and refuses to run
    #: under it (self-scheduling guardrail, Spec 1 D-13).
    trigger_origin: bool = False
    #: Stream MCP-OAUTH (OA-3b-后续) — the caller's OAuth subject id (the JWT
    #: ``sub`` / ``mcp_oauth_connection.user_id``), distinct from ``user_id``
    #: (the ``tenant_user.id`` UUID). Carried so a ``SubAgentTool`` /
    #: ``spawn_worker`` child can resolve the SAME per-user OAuth pool as the
    #: parent — the child build keys its OAuth pool on this, not ``user_id``.
    #: ``None`` when the caller has no OAuth identity (service principals).
    oauth_user_id: str | None = None
    cancellation_token: CancellationToken | None = None
    #: Stream K.K8 / P3 — current plan. Set by the planner node
    #: (``plan_execute``) or by the ``update_plan`` tool, which any agent can
    #: call to create or revise a plan. ``update_plan`` keeps ``plan.goal`` on
    #: a revise unless a new goal is supplied. ``None`` only before the first
    #: plan is established.
    plan: Plan | None = None
    #: Mini-ADR J-40 (J.4-补强-2) — wall-clock deadline (``time.monotonic``
    #: timestamp) for the *current run including any sub-agent recursion*.
    #: Established once in ``sse.run_agent`` from the manifest's
    #: ``policies.run_deadline_s``; ``SubAgentTool`` propagates the value
    #: to child config unchanged (child does not reset). A tool that
    #: opts in checks ``deadline_at - time.monotonic() <= 0`` before
    #: doing expensive work and short-circuits with a cancel. ``None``
    #: when no deadline is configured.
    deadline_at: float | None = None
    #: 1.3 Orchestrator-Worker — the per-run dynamic-worker spawn budget
    #: (cumulative count cap + concurrency semaphore), created once per run
    #: and shared across every ``spawn_worker`` call. ``None`` when the
    #: feature is unwired (tests / eval) — workers still run, bounded by
    #: depth + iteration cap + deadline + the per-tenant quota engine.
    worker_spawn_budget: WorkerSpawnBudget | None = None
    #: B2 worker 可观测性 — async sink publishing ``worker`` SSE frames into
    #: the parent run's bridge + event store. Injected per-run by
    #: ``sse.run_agent`` via ``WORKER_EVENT_SINK_KEY``; ``None`` when unwired
    #: (tests / eval) — child runs then emit no frames.
    worker_event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    #: B2 — id of the tool_call this invocation serves (AIMessage
    #: ``tool_calls[].id``), set per-dispatch in ``_invoke_tool`` via
    #: ``dataclasses.replace``. Worker frames carry it so the frontend can
    #: attach the worker sub-timeline to the pending tool card.
    tool_call_id: str | None = None
    #: B3 — 全树共享 token 池(run_agent 注入,None=未启用)。
    token_budget: TokenBudget | None = None
    #: B3 — guard marker 帧 sink(下传给子树,None=未接线)。
    guard_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    #: 二期 PR3(spec P4)— process-wide delegation concurrency gate (subagent
    #: + spawn_worker), a single process-level singleton shared across every
    #: run and every delegation depth (下传给子树,见 ``_child_run._child_config``).
    #: Injected per-run by ``sse.run_agent`` via ``DELEGATION_GATE_KEY``;
    #: ``None`` when unwired (tests / eval / no config service) — delegations
    #: run ungated, same as before this gate existed.
    delegation_gate: DelegationGate | None = None
    #: 产物清单契约 —— per-run artifact-registration recorder. ``save_artifact``
    #: 每次成功登记调用一次(同步,append 语义);run 终局时 ``sse.run_agent``
    #: 把累积清单随终态一并固化并放上 ``end`` 帧。Injected per-run via
    #: ``ARTIFACT_RECORDER_KEY``;``None`` when unwired (tests / eval) —
    #: 登记照常发生,只是本 run 无清单记录。
    artifact_recorder: Callable[[dict[str, Any]], None] | None = None
    #: 本轮用户上传文档的工作区路径(``uploads/<name>``),按上传顺序。
    #:
    #: 委派出去的子代**不继承本对话**——它只看得见 ``task`` 字符串,所以
    #: ``[file attached: …]`` 那一行它从来看不到。把本轮附件从这里结构性地
    #: 带进子代的种子消息(见 ``_child_run.build_seed_content``),而不是指望
    #: 主 Agent 记得把路径抄进 ``task``:真栈上出现过主 Agent 漏抄、worker 只
    #: 能按文件名在工作区里猜、结果挑中上一轮的历史文档做了一整轮无效分析。
    #:
    #: 空元组 = 本轮没有文档,或走的是不带新一轮输入的路径(审批续跑 / orphan
    #: 复活 / 触发器)——那些路径本来就没有"本轮用户附件"。
    turn_documents: tuple[str, ...] = ()
    #: 本轮用户上传图片的 ``expert_work://image/...`` 引用,按上传顺序。
    #:
    #: 与文档分开,因为送达子代的机制由**子代自己的模型**决定,而不是父的
    #: (``dynamic_workers.model`` 可以把 worker 换成另一个模型):
    #:
    #: * 子代模型原生多模态 → 引用变成 ``image_ref`` content block 进种子消息,
    #:   由 provider 在调用前解析成真图(J.6 Path A);
    #: * 子代模型不看图但继承了 ``vision:`` 块(于是有 ``ask_image``)→ 引用以
    #:   文本列出,子代拿它调 ``ask_image``(J.6 Path B);
    #: * 两者都不是 → 不列。子代既没有原生视觉也没有 ``ask_image``,列出来只是
    #:   给它一串用不上的字符串。
    turn_image_refs: tuple[str, ...] = ()
    #: 工作区分层 —— 本次调用属于哪个 agent。值是 ``sanitize_agent_key(spec.metadata.name)``,
    #: 与 ``/opt/skills/<agent_key>`` / ``PYTHONUSERBASE`` 用的是同一个,**不要新造第二种算法**。
    #:
    #: 空串 = 没绑 agent,与 ``agent_key_envs("")`` 的「空串=不注入」语义一致。真实会
    #: 出现空串的只有合成执行路径(``eval_engine_live`` 的对抗评测建的是内联 spec、
    #: 连 tenant_id 都没有),用户的每一条 run 入口都必须填上 —— 见
    #: ``control-plane/tests/test_agent_key_plumbing.py`` 的入口穷举。
    agent_key: str = ""
    #: B-61 §4.4 —— ``EXPERT_WORK_INPUTS`` 该指向**哪个 run** 的 ``inputs.json``。
    #:
    #: 新开一轮的主 run 留 ``None``(用 ``run_id`` 自己);审批续跑段是新 run_id,
    #: 入口经 ``run_agent(inputs_run_id=…)`` 指回这一轮首段。委派出的子代(worker / 静态子
    #: Agent)每次都会新铸一个 ``sub_run_id``,而 inputs 节点**故意不为子 run 写
    #: 文件**——于是子代的 exec 会拿到一个 ``inputs/<sub_run_id>/inputs.json``
    #: 的悬空路径,工具描述又告诉模型那个文件在,模型读不到就退回手抄 URL:正是
    #: B-61 要消灭的那个失败。子代与父共用同一个 agent_key、同一个
    #: ``/workspace``,所以把**父的** run id 传下来,指针就落在父那份真文件上
    #: (与 agent_key / 技能种子路径同一个取值口径:用父的)。
    inputs_run_id: UUID | None = None
    #: B-61 §5.3 —— 本轮声明变量的值(变量名 → 值)。
    #:
    #: 唯一用途是 ``_child_config`` 把它往子代传:委派出去的 worker / 静态子
    #: Agent 跑的是同一个 ``tools_node``,子代的 config 里没有 ``prompt_inputs``
    #: 时,``apply_arg_bindings`` 对每个被绑参数走「本轮没给值 → 删掉」那条分支,
    #: 而该参数已经从子代的 schema 里剥掉、模型也补不上 —— 那个工具在子代身上
    #: 就永远缺一个必填参数。绑定属于**父**的 spec,子代干的也是父 agent 的活,
    #: 所以取值口径与 ``agent_key`` / ``inputs_run_id`` 一致:用父的。
    #:
    #: 主 run 的填值不读这里,它直接读 ``config["configurable"]``(那是这些值的
    #: 出处);``None`` = 这条 run 没有声明变量。
    prompt_inputs: Mapping[str, Any] | None = None


#: Stream K.K8 — keys a tool is allowed to write back to ``AgentState``
#: via :attr:`ToolResult.state_updates`. Limiting the set prevents a tool
#: from inadvertently rewriting unrelated channels (``messages``,
#: ``step_count`` …); add a key here when a new tool needs to mutate a
#: specific channel.
#:
#: Channels:
#: - ``plan`` — Stream J.1 / K.K8 ``update_plan``
#: - ``subagent_invocations`` — Stream J.4-补强-2 / Mini-ADR J-40
#:   ``SubAgentTool`` appends one :class:`SubAgentInvocation` per
#:   delegation outcome (the state.py channel uses ``operator.add``).
#: - ``promoted_tools`` — Stream TE-6 ``find_tools`` writes the names of
#:   deferred tools it just retrieved so the next ``agent_node`` adds them
#:   to the LLM bind (the state.py channel uses ``_merge_promoted`` to
#:   union-dedupe across turns).
#: - ``viewed_figures`` —— B-64 ``read_page`` 追加它刚渲出来的页 ref
#:   (state.py 的通道用 ``_merge_viewed_figures`` 跨轮 union,按最近一次看到排序)。
TOOL_ALLOWED_STATE_KEYS: frozenset[str] = frozenset(
    {"plan", "subagent_invocations", "promoted_tools", "viewed_figures"}
)


@dataclass(frozen=True)
class ToolResult:
    """Result of a successful tool dispatch.

    ``content`` is fed back to the LLM as a ``ToolMessage`` body.
    ``meta`` carries truncation flags and any per-tool metadata (per
    Mini-ADR E-10 — caller knows e.g. ``meta.truncated=True`` ↔ output
    was cut).

    ``state_updates`` (Stream K.K8) is the narrow channel through which
    a tool may write back to :class:`AgentState`. The tools node
    promotes only keys in :data:`TOOL_ALLOWED_STATE_KEYS`; other keys
    are silently dropped (so a malformed or compromised tool can't
    rewrite ``messages`` or ``step_count``).

    ``refund_iterations`` (Stream L.L5 / Mini-ADR L-5) lets a tool ask
    the ReAct loop to refund iterations from the agent's ``step_count``
    budget. Internal-chain tools like ``update_plan`` (K.K8) shouldn't
    burn user-visible budget for housekeeping calls. The tools node
    accumulates this across the batch into
    ``step_count_refund_pending``; the next agent node subtracts it
    before computing the new ``step_count`` (clamped at 0 — refund
    never produces negative). Must be ``>= 0`` — a tool can't reverse
    the polarity and *consume* budget through this channel.

    ``full_content`` (Stream CM-5, Mini-ADR CM-F1/F3) carries the
    complete un-truncated rendering when ``content`` was cut, so the
    tools node can externalize it to the user's workspace and leave a
    recoverable reference instead of losing the overflow forever. Only
    tools whose output is otherwise unrecoverable may set it (bash /
    exec_python / http / mcp); read-only tools must leave it ``None`` —
    their sources are re-readable, and the exemption is the
    persist→read→persist loop guard.
    """

    content: str
    meta: Mapping[str, Any] = field(default_factory=dict)
    state_updates: Mapping[str, Any] = field(default_factory=dict)
    refund_iterations: int = 0
    full_content: str | None = None

    def __post_init__(self) -> None:
        # Frozen dataclass — direct setattr is disabled. The check runs
        # at construction time so a misbehaving tool fails loudly rather
        # than silently corrupting the agent's iteration budget.
        if self.refund_iterations < 0:
            msg = (
                f"ToolResult.refund_iterations must be >= 0 (got "
                f"{self.refund_iterations}); a tool cannot consume the "
                f"agent's iteration budget through this channel."
            )
            raise ValueError(msg)


@runtime_checkable
class Tool(Protocol):
    """Async callable wrapped with its static spec.

    ``spec`` is declared read-only so both a plain attribute (MCPTool's
    ``field(init=False)``) and a ``@property`` (WebSearchTool / HTTPTool)
    satisfy the Protocol.
    """

    @property
    def spec(self) -> ToolSpec:
        """The tool's static descriptor — handed to the LLM for selection."""

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        """Dispatch the tool with the given args and return a
        :class:`ToolResult`. ``ctx`` carries tenant binding etc. so
        per-tenant policies (E.8 allowlist, F.6 secret resolution) can
        run inside the tool. Implementations may raise; the ReAct graph's
        tools node wraps any exception into a ``ToolMessage(status='error')``
        (Mini-ADR E-12) — never let it propagate to the runner."""


class ToolNotFoundError(KeyError):
    """Raised by :meth:`ToolRegistry.get_required` when ``name`` isn't
    registered. The graph's ``tools`` node turns this into a
    ``ToolMessage(error=...)`` rather than propagating."""


class ToolBlockedError(RuntimeError):
    """Raised when a tool's policy denies the call (e.g. URL not in
    the per-tenant HTTP allowlist; tenant_id missing for a
    tenant-scoped tool). The graph's ``tools`` node wraps it into a
    ``ToolMessage(status='error')`` per Mini-ADR E-12 and the
    surrounding orchestrator writes a ``tool:blocked`` audit row."""


class ToolRegistry:
    """In-memory tool catalogue.

    M0 instantiates one per ``orchestrator`` process at startup;
    register all tools available to any agent. Per-agent / per-tenant
    filtering (``http_tool_allowlist`` / ``mcp_servers``) happens at
    dispatch / spec-resolution time — the registry itself is just a
    lookup table.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        #: Stream TE-6 — names of tools registered as *deferred* (the tool
        #: RAG mechanism). Deferred tools are excluded from :meth:`specs`
        #: (so they don't bloat every turn's LLM ``tools`` list) and are
        #: surfaced only via :meth:`search` / ``find_tools``. They stay
        #: dispatchable through :meth:`get` once promoted.
        self._deferred: set[str] = set()
        #: Stream HX-12 — tool provenance ("builtin" / "skill" /
        #: "mcp:<server>"), shown in find_tools results so the model can
        #: tell where a capability comes from.
        self._sources: dict[str, str] = {}
        #: Stream HX-12 — lazily-built BM25 corpus over the deferred pool;
        #: invalidated on every register (cheap: rebuilt on next search).
        self._ranking_corpus: list[tuple[str, list[str]]] | None = None
        #: B-61 §5.3 — 平台接管的 MCP 工具参数:wire 名(``mcp__<server>__<tool>``)
        #: → ``{参数名: 声明变量名}``。``tools_node`` 按 tool_call 里出现的那个名
        #: 直接查这张表,所以键必须是折叠后的 wire 名(折叠规则只有
        #: ``register_mcp_tools`` 那一处知道)。
        self._arg_bindings: dict[str, dict[str, str]] = {}
        #: B-61 §5.4 — 落空的绑定。两个消费者:随 ``BuiltAgent`` 走到保存时的
        #: 试建(当场告诉配置的人),以及 ``tools_node`` 的运行期兜底告警 ——
        #: 保存之后对方才下线 / 改名的那条路上,运行期是唯一还能说话的地方。
        self._unmatched_arg_bindings: list[UnmatchedArgBinding] = []
        #: B-61 §5.4(复评 N-1)— 真落在了某个已注册工具上的绑定,``(server, 裸
        #: 工具名)``。「落没落地」只能等**所有** mcp 条目都注册完再回答:一份
        #: manifest 可以有多个条目,绑定表是跨条目并起来的,而某个兄弟条目的
        #: ``allow_tools`` 会把别人绑的那个工具挡在它那一次循环之外 —— 拿单次注册
        #: 的剩余项当答案,就会对一条**好好落了**的绑定谎报「目录里没这个工具」。
        self._landed_arg_bindings: dict[tuple[str, str], str] = {}

    def register(self, tool: Tool, *, deferred: bool = False, source: str | None = None) -> None:
        """Register a tool by its spec ``name``. Re-registering replaces.

        Stream TE-6 — ``deferred=True`` marks the tool as *latent*: it is
        omitted from :meth:`specs` (the per-turn LLM bind) and exposed only
        through :meth:`search` until ``find_tools`` promotes it. The tool
        remains fully dispatchable via :meth:`get` / :meth:`get_required`
        so a promoted call still routes. Default ``False`` keeps every
        existing tool active — zero behaviour change.

        Stream HX-12 — ``source`` records provenance for find_tools result
        labelling (``"mcp:<server>"`` / ``"skill"``; default ``"builtin"``).
        """
        name = tool.spec.name
        self._tools[name] = tool
        if deferred:
            self._deferred.add(name)
        else:
            # Re-registering a previously-deferred name as active un-defers it.
            self._deferred.discard(name)
        if source is not None:
            self._sources[name] = source
        self._ranking_corpus = None

    def source_of(self, name: str) -> str:
        """Provenance label for find_tools listings (Stream HX-12)."""
        return self._sources.get(name, "builtin")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_required(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            msg = f"unknown tool: {name!r}"
            raise ToolNotFoundError(msg)
        return tool

    def specs(self) -> list[ToolSpec]:
        """Active (non-deferred) specs in registration order — handed to the LLM.

        Stream TE-6 — deferred tools are excluded so they don't inflate the
        per-turn ``tools`` list (Context Bloat). With no deferred tools this
        returns every registered spec, identical to pre-TE-6 behaviour.
        """
        return [tool.spec for name, tool in self._tools.items() if name not in self._deferred]

    def all_specs(self) -> list[ToolSpec]:
        """Every registered spec — active *and* deferred — in registration order.

        Stream TE-6 — scheduling / approval-gating classify by spec, so they
        must see deferred tools too (a deferred irreversible tool stays gated
        once promoted, and a promoted tool still schedules correctly). With no
        deferred tools this equals :meth:`specs`.
        """
        return [tool.spec for tool in self._tools.values()]

    def catalog(self) -> tuple[ToolCatalogEntry, ...]:
        """Every registered tool — active and deferred — in registration order."""
        return tuple(
            ToolCatalogEntry(
                name=name,
                description=tool.spec.description,
                # 深拷贝:投影是给控制面序列化的,嵌套对象不能与喂给 LLM 的
                # 真 spec 共享(frozen entry 指向可变 dict 等于没 frozen)。
                parameters=copy.deepcopy(dict(tool.spec.parameters)),
                source=self.source_of(name),
                from_skill=tool.spec.from_skill,
                deferred=name in self._deferred,
            )
            for name, tool in self._tools.items()
        )

    def bind_tool_args(self, name: str, bound: Mapping[str, str]) -> None:
        """B-61 — 记下 ``name`` 这个工具被平台接管的参数(参数名 → 声明变量名)。

        ``bound`` 为空表示「这个名字没有绑定」,连带**清掉**之前可能存在的那条:
        ``register`` 按名字覆盖注册,wire 名又会被截断到 64 字符,两台服务器的
        工具折叠成同一个名是可能的。留着旧表项就会让后来者顶着前者的绑定跑。
        """
        if bound:
            self._arg_bindings[name] = dict(bound)
        else:
            self._arg_bindings.pop(name, None)

    def arg_bindings(self) -> dict[str, dict[str, str]]:
        """B-61 §5.3 — ``tools_node`` 填值时查的那张表,每次一份新的拷贝。

        与 :meth:`catalog` 同一口径。交出活字典等于把「下游只读」写成一条靠
        约定成立的不变式,而这张表活在 ``BuiltAgent`` 缓存里 —— 谁就地改一下,
        之后每一个 run 拿到的都是被污染的绑定。
        """
        return {name: dict(bound) for name, bound in self._arg_bindings.items()}

    def note_unmatched_arg_binding(
        self, server: str, tool: str, params: tuple[str, ...], *, reason: UnmatchedReason
    ) -> None:
        """B-61 §5.4 — 记一条落空的绑定。``params`` 只有名字,绝不含值。

        同一条记两次就丢掉后一条:一份 manifest 可以有多个 ``mcp`` 条目,
        每一条都会把同一台服务器再注册一遍,于是同一条落空的绑定会被发现
        多次 —— 那是同一个事实,说一次就够(说两次只会让告警看起来像两个问题)。
        """
        record = UnmatchedArgBinding(server=server, tool=tool, params=params, reason=reason)
        if record not in self._unmatched_arg_bindings:
            self._unmatched_arg_bindings.append(record)

    def unmatched_arg_bindings(self) -> tuple[UnmatchedArgBinding, ...]:
        """本次构建里落空的绑定(整条没匹配上的 + 参数漂移的)。"""
        return tuple(self._unmatched_arg_bindings)

    def note_landed_arg_binding(self, server: str, tool: str, wire_name: str) -> None:
        """B-61 §5.4 — 这条绑定落在了一个真实注册的工具上,落在 ``wire_name`` 这个键下。

        ``server`` / ``tool`` 是**裸名**(配置的人写的那两个),``wire_name`` 是注册用的
        折叠名。B-65 —— wire 名是**传下来**的、不是在这里重算的:折叠规则(非法字符折
        ``_``、截断 64)只有 ``register_mcp_tools`` 一处知道(裁定 X),判定侧学第二遍
        就是留一条会静默走散的缝。

        同一 ``(server, tool)`` 再次落地会覆盖 —— 后一次注册的就是活着的那个。
        """
        self._landed_arg_bindings[server, tool] = wire_name

    def landed_arg_bindings(self) -> dict[tuple[str, str], str]:
        """真落了地的绑定:``(server, 裸工具名) → 注册用的 wire 名``。

        B-65 —— 只有「落地时的 wire 名」还不够判它现在是否仍然有效:两个工具折成同一个
        wire 名时,后注册的那个会把前者的绑定从 ``arg_bindings`` 里清掉(见
        :meth:`bind_tool_args`),而这里的记录还在。判定侧拿这个名回查
        :meth:`arg_bindings`,名字不在了就是被顶掉了。
        """
        return dict(self._landed_arg_bindings)

    def deferred_specs(self, names: Iterable[str]) -> list[ToolSpec]:
        """Specs for the given ``names`` that are actually deferred.

        Stream TE-6 — ``agent_node`` calls this with the run's promoted-tool
        names to add just-retrieved deferred tools to the LLM bind. Names that
        aren't registered or aren't deferred (e.g. an already-active tool) are
        dropped. Order follows ``names``.
        """
        out: list[ToolSpec] = []
        for name in names:
            if name in self._deferred:
                tool = self._tools.get(name)
                if tool is not None:
                    out.append(tool.spec)
        return out

    def search(self, query: str) -> list[ToolSpec]:
        """Retrieve matching *deferred* tool specs for ``find_tools`` (Stream TE-6).

        Active tools are never returned — they're already in the bind, so
        there's nothing to retrieve. The query syntax mirrors deer-flow's
        ``tool_search``, with HX-12 adding a ranked natural-language mode:

        - ``select:a,b,c`` — exact name match (comma-separated).
        - ``+keyword rest...`` — require ``keyword`` (case-insensitive) in the
          name or description, then further filter by every remaining word.
        - otherwise — BM25-ranked retrieval over names / descriptions /
          parameter names (CJK-aware, best match first; Stream HX-12). A
          zero-overlap query falls back to the pre-HX-12 regex / substring
          path, so retrieval is never worse than before.
        """
        # Iterate ``_tools`` (registration-ordered) so results are
        # deterministic; ``_deferred`` is an unordered set.
        candidates = [tool.spec for name, tool in self._tools.items() if name in self._deferred]
        stripped = query.strip()
        if not stripped:
            return []

        if stripped.startswith("select:"):
            wanted = {n.strip() for n in stripped[len("select:") :].split(",") if n.strip()}
            return [spec for spec in candidates if spec.name in wanted]

        if stripped.startswith("+"):
            terms = stripped[1:].split()
            if not terms:
                return []
            lowered_terms = [t.lower() for t in terms]
            return [
                spec
                for spec in candidates
                if all(term in _haystack(spec) for term in lowered_terms)
            ]

        # Stream HX-12 — ranked natural-language retrieval first; an empty
        # result (zero lexical overlap) drops to the legacy path below.
        ranked_names = rank_tools(stripped, self._ranking_documents())
        if ranked_names:
            by_name = {spec.name: spec for spec in candidates}
            return [by_name[name] for name in ranked_names if name in by_name]

        # Stream TE-6 — the query is model-derived; an over-long pattern is the
        # ReDoS surface (catastrophic backtracking). Cap it: anything past the
        # limit degrades to a plain substring match (never compiled as a regex).
        if len(stripped) > _MAX_SEARCH_QUERY_LEN:
            needle = stripped[:_MAX_SEARCH_QUERY_LEN].lower()
            return [spec for spec in candidates if needle in _haystack(spec)]
        try:
            pattern = re.compile(stripped, re.IGNORECASE)
        except re.error:
            needle = stripped.lower()
            return [spec for spec in candidates if needle in _haystack(spec)]
        return [
            spec
            for spec in candidates
            if pattern.search(spec.name) or pattern.search(spec.description)
        ]

    def _ranking_documents(self) -> list[tuple[str, list[str]]]:
        """Lazily (re)build the BM25 corpus over the deferred pool (HX-12)."""
        if self._ranking_corpus is None:
            self._ranking_corpus = [
                (
                    name,
                    build_document(
                        name,
                        tool.spec.description,
                        list((tool.spec.parameters or {}).get("properties", {}) or {}),
                    ),
                )
                for name, tool in self._tools.items()
                if name in self._deferred
            ]
        return self._ranking_corpus

    def has_deferred(self) -> bool:
        """Whether any tool is registered deferred (Stream TE-6).

        The assembler uses this to auto-register ``find_tools`` only when
        there is something to discover (TE-6b)."""
        return bool(self._deferred)

    def deferred_names(self) -> list[str]:
        """Names of every deferred tool, in registration order (Stream HX-12).

        The assembler's small-pool escape hatch iterates these to estimate
        the pool's schema size and, under the threshold, re-register each
        tool active."""
        return [name for name in self._tools if name in self._deferred]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
