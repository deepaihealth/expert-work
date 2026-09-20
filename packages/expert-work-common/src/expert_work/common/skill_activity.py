"""Cross-service ``SkillActivityRecorder`` Protocol — Mini-ADR U-27.

The orchestrator's ``agent_factory._load_skills`` (build-time bind
event) and the ``skill_view`` tool (runtime read event) both need to
mark a skill as "just used" so the Sprint #4 Curator doesn't transition
it to ``stale``. The actual SQL write (and the throttle around it)
lives in the control-plane process (alongside the SkillStore), but the
orchestrator can't import from control-plane (one-way dep). This
Protocol is the contract the orchestrator depends on; ``control_plane.
skill_activity.ThrottledActivityRecorder`` is the production
implementation, injected through ``build_agent``.

The orchestrator may receive ``None`` (no recorder wired) — tests +
the eval CLI commonly leave it unset; in that case activity tracking
silently no-ops. The Curator's behavior degrades gracefully (the rows
just look "less recently used" than they really are) — never an
incorrectness, only a triage-thresholds tuning concern.

B-84 — 这两个事件过去在下游**分不开**: 都只写 ``skill.last_used_at``, 于是
那一列回答的是「这个技能还绑在某个 agent 上吗」, 不是「它被打开过吗」。
``kind`` 把它们在调用点分开:

* ``bind`` (默认) —— 行为与 B-84 之前逐行一致, 只 bump ``last_used_at``。
  默认值就是它, 所以构建路径那行调用一个字没改。
* ``view`` —— 除了照旧 bump ``last_used_at``, 再往 ``skill_run_usage`` 追加
  一条 ``outcome='viewed'`` 的证据行, 需要 :class:`SkillViewEvent` 随行。

证据记在 ``skill_run_usage`` 而不是 ``skill`` 行上, 有两个理由, 都是实测逼出来的:
对接方 ``ai-health-plan`` 绑的 19 个技能 **19/19 是平台技能**
(``skill.tenant_id IS NULL``)。写 ``skill`` 行 (a) 只答得了「有没有人打开过」,
答不了「**这个 agent** 打开过没有」; (b) 要让租户会话去写 NULL-tenant 行,
RLS enforce 上线后会静默变成 0 行。``skill_run_usage`` 的行是消费方租户自有的,
两个问题都不存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

#: What kind of activity is being reported. See the module docstring.
SkillActivityKind = Literal["bind", "view"]


@dataclass(frozen=True)
class SkillViewEvent:
    """``kind='view'`` 随行的证据 —— 写一行 ``skill_run_usage`` 需要的三个字段。

    单独成型而不是三个可选参数, 是因为它们**必须同时在**: 缺一个就拼不出行
    (``thread_id`` / ``agent_name`` 都是 NOT NULL)。``bind`` 不带它, 所以构建
    路径不用知道这三个字段存在。

    ``agent_name`` 是 ``spec.metadata.name``(与 SE-7d 写的那些行同一口径),
    **不是** ``ToolContext.agent_key`` —— 后者是 sanitize 过、带 8 位十六进制
    后缀的工作区键名, 两者对不上就连不成「这个 agent 读过什么」。
    """

    skill_version: int
    thread_id: UUID
    agent_name: str


@runtime_checkable
class SkillActivityRecorder(Protocol):
    """Best-effort activity bump for one skill.

    Implementations MUST NOT raise on the agent hot path — failures
    should be logged + swallowed (the Curator can't be allowed to
    cause an agent run to fail). Implementations SHOULD throttle so
    high-fanout callers (1000 agent runs / sec) don't amplify into
    1000 SQL UPDATEs / sec on the same skill row.
    """

    async def record(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        kind: SkillActivityKind = "bind",
        view: SkillViewEvent | None = None,
    ) -> None:
        """Mark ``skill_id`` as just-used. Returns nothing — the caller
        cannot do anything useful with success / failure information.

        ``kind`` says which event it was; see :data:`SkillActivityKind`.
        ``view`` carries the evidence a ``view`` row needs and is ignored
        for ``bind``; ``kind='view'`` with ``view=None`` degrades to a
        plain bind bump rather than raising (hot path).

        去重是实现方的事, 且两种 kind 必须**各算各的**: ``bind`` 每次 agent
        build 都发生, 共用一个窗口的话它会几乎吃掉每一次 ``view``, 于是一个
        正在被读的技能仍然看不到证据行 —— 那正是这个信号要消灭的假象。
        ``view`` 行按 ``(tenant_id, skill_id, thread_id)`` 去重就够: 我们要
        答的是「有没有打开过」, 不是「打开过几次」。
        """


__all__ = ["SkillActivityKind", "SkillActivityRecorder", "SkillViewEvent"]
