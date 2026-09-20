"""J.7a Skill 静态启用 — DTOs.

Mini-ADR J-23 + 2026-05-21 修订 (STREAM-J-DESIGN § 15). M0 = prompt
片段 + tools 子集 (不含 code 字段)+ 版本化 + draft 闸门 + admin
CRUD API + ZIP import/export + ``name@version`` 版本固定.

These DTOs are the wire shape between control-plane (admin API),
orchestrator (skill loader at build time), and expert-work-persistence
(``SkillStore`` ABC).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from expert_work.protocol.tenant_config import TenantPlan

__all__ = [
    "HIGH_RISK_TOOLS",
    "SKILL_USAGE_VIEWED",
    "ComponentType",
    "EvalVerdict",
    "EvolutionOrigin",
    "KillSwitch",
    "KillSwitchScope",
    "PredictionVerdict",
    "PromoteRequestStatus",
    "ReplaySource",
    "Skill",
    "SkillAuthoredBy",
    "SkillEvalResult",
    "SkillPackageLayoutError",
    "SkillPredictionVerdict",
    "SkillPromoteRequest",
    "SkillRef",
    "SkillRunUsage",
    "SkillStatus",
    "SkillSupportingFile",
    "SkillUsageOutcome",
    "SkillVersion",
    "SkillViewGap",
    "SkillVisibility",
    "canonicalize_skill_content",
    "compute_content_hash",
    "is_high_risk_skill_version",
    "parse_skill_ref",
    "supporting_files_to_jsonable",
]


# ── Capability Uplift Sprint #3 (Mini-ADR U-24) ──────────────────────────
# Tools that escalate a skill's blast-radius to "needs human review before
# activate". Includes any tool that lets the skill execute arbitrary code
# (exec_python / exec_shell) or make uncontrolled network egress (http).
# A skill with any of these in ``tool_names`` flips ``high_risk = True``
# and the publish gate at PATCH /v1/skills/{id} status=active rejects
# non-admin actors.
HIGH_RISK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "exec_python",
        "exec_shell",
        "http",
    }
)


class SkillPackageLayoutError(ValueError):
    """Raised by ZIP / SKILL.md parsers when the input is structurally
    invalid (missing SKILL.md / bad path / banned extension / etc).

    The control-plane layer catches this and returns a **generic** 400
    so attackers don't get an oracle that reveals which check fired
    (Mini-ADR U-18 / U-21 Oracle defense). The full violation detail
    is recorded in the audit row for SecOps triage.
    """


class SkillStatus(StrEnum):
    """``skill.status`` lifecycle states.

    ``DRAFT`` — newly created or freshly re-authored; not visible to a
    manifest that references the skill by bare ``name`` (only pinned
    ``name@version`` lookups can see draft).

    ``ACTIVE`` — current published version; bare ``name`` references
    resolve to ``skill.latest_version`` (which must be active when the
    skill is in this state).

    ``STALE`` — Capability Uplift Sprint #4 (Mini-ADRs U-26 / U-29).
    Auto-marked by the Curator worker when an ``ACTIVE`` skill has not
    seen ``bind`` or ``view`` activity for ``tenant_config.skill_stale_days``
    (default 30). Bare ``name`` references still resolve to the latest
    version *and* auto-revive the skill to ``ACTIVE`` via the
    ``bump_last_used_at`` SQL — the "asleep, wake on touch" semantic.
    Distinct from ``DRAFT`` so operators can tell "never published" from
    "published but went cold".

    ``ARCHIVED`` — retired skills; bare ``name`` references reject at
    build time but historical pinned ``name@N`` references still resolve
    (reproducibility — agents pinned to old versions keep working).
    Sprint #4 makes this auto-reachable from ``STALE`` after
    ``skill_archive_days`` (default 90); Curator never deletes — admin
    must explicitly unarchive (PATCH ``status`` back to ``ACTIVE``).
    """

    DRAFT = "draft"
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


SkillAuthoredBy = Literal["human", "agent"]

#: RT-ADR-11 — default progressive-disclosure mode for a newly authored skill
#: version. ``True`` = body lazy-loaded via ``skill_view`` (only a one-line
#: ``<skill .../>`` summary sits in the system prompt); ``False`` = full
#: SKILL.md body eager-inlined. Flipped from the Mini-ADR U-15 default (eager)
#: to align with deer-flow / Hermes / Anthropic progressive disclosure and to
#: cut fixed system-prompt overhead. Every authoring entry point (protocol DTO,
#: persistence ``add_version`` / ``add_platform_version``, ZIP-import parse, DB
#: ``server_default``) references this so the policy has a single source of
#: truth. Existing rows store an explicit value, so flipping the default does
#: not touch them (STREAM-RT-DESIGN §9).
DEFAULT_SKILL_LAZY_LOAD: Final[bool] = True

# ── Stream SE — 自我进化 skill ────────────────────────────────────────────
# ``visibility`` — Mini-ADR SE-A1 (落实 J.7b-1 §15.7). ``agent_private`` =
# 仅创建它的 agent 实例可见(自著默认);``tenant`` = 租户内共享(需经治理门
# promote)。平台 skill(tenant_id NULL)始终视为 tenant 可见。
SkillVisibility = Literal["agent_private", "tenant"]
# ``evolution_origin`` — 一个版本是怎么来的。``None`` = 人写(历史/admin);
# ``in_session`` = agent 在 run 内自著(Layer A);``distilled`` = 后台后验
# 蒸馏 worker 产出(Layer B,SPARK)。
EvolutionOrigin = Literal["in_session", "distilled"]
# 重放验证(SE-4)的判定与来源。
EvalVerdict = Literal["pass", "fail", "inconclusive"]
ReplaySource = Literal["trajectory", "eval_dataset"]
# SE-8(Mini-ADR SE-A13b)promote 审批流状态:agent_private→tenant 可见性提升
# 的审批单生命周期。``superseded`` = 同 skill 新建了更新的请求或版本已变。
PromoteRequestStatus = Literal["pending", "approved", "rejected", "superseded"]
# SE-8(Mini-ADR SE-A13c)紧急停 kill-switch 的作用域:``global`` = 全平台
# 自动通道(tenant_id NULL);``tenant`` = 单租户(tenant_id 非空)。
KillSwitchScope = Literal["global", "tenant"]
# SE-10(Mini-ADR SE-A15)进化对象的组件类型。``skill`` = 历史/默认(prompt
# 片段 + tool 子集的可复用技能);其余三类是 SE-10 扩展的**无执行风险文本组件**,
# 复用同一套 SkillVersion 载体 + with-vs-without 重放验证门:``system_prompt`` =
# agent 级行为补丁片段;``tool_description`` = 对某个已绑定工具的用法澄清(纯文本,
# 不改实现/参数,补充对象记在 ``Skill.target_tool_name``);``memory_entry`` =
# 可复用的长期记忆事实/偏好。代码类组件(工具实现/中间件/子agent)**不进化**。
ComponentType = Literal["skill", "system_prompt", "tool_description", "memory_entry"]
# SE-11(Mini-ADR SE-A18/A19)预测—证伪 verdict:promote 时的重放预测(eval delta)
# 在上线后兑现了多少。``insufficient`` 不落库(窗口不足时跳过),故持久化 Literal
# 仅含五个终态。
PredictionVerdict = Literal["effective", "partially_effective", "ineffective", "mixed", "harmful"]


class SkillVersion(BaseModel):
    """One row of ``skill_version`` — an immutable published version.

    ``prompt_fragment`` is Markdown-multipara allowed (the loader wraps
    it in ``<skill name="..." version="...">...</skill>`` before
    splicing into the agent's system prompt — see Mini-ADR J-23 §
    15.6 (c) 红线).

    ``tool_names`` is the tool subset the skill activates. The build-
    time merger rejects a manifest whose two skills declare overlapping
    tool names (``SkillConflictError``).

    ``required_models`` — when non-empty, the agent's primary
    ``model.name`` MUST appear in this list or the build fails. Empty
    means "no compatibility constraint".
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    skill_id: UUID
    # Stream X — Mini-ADR X-1/X-2. ``None`` = platform (NULL-tenant) skill
    # version; non-NULL = tenant-owned. X2 relaxes the column to NULLABLE so
    # the platform-curated library shares the ``skill_version`` table.
    tenant_id: UUID | None
    version: int = Field(ge=1)
    prompt_fragment: str
    tool_names: tuple[str, ...] = ()
    description: str = ""
    category: str | None = None
    required_models: tuple[str, ...] = ()
    authored_by: SkillAuthoredBy = "human"
    # Capability Uplift Sprint #3 (Mini-ADR U-16) — supporting files
    # under arbitrary subdirectories. Map of path → SkillSupportingFile.
    # The DB column is JSONB; this DTO uses ``dict`` rather than
    # ``Mapping`` so Pydantic can construct from raw JSON payloads.
    supporting_files: dict[str, SkillSupportingFile] = Field(default_factory=dict)
    # Mini-ADR U-15: per-skill progressive disclosure flag. False = body
    # eager-loaded into system prompt; True = body lazy-loaded via
    # ``skill_view`` tool. RT-ADR-11 flipped the default to lazy (see
    # ``DEFAULT_SKILL_LAZY_LOAD``).
    lazy_load: bool = DEFAULT_SKILL_LAZY_LOAD
    # Mini-ADR U-21: blake2b-32 hash of canonicalized content. Recomputed
    # at ``skill_view`` time; mismatch fires SKILL_DRIFT_DETECTED.
    # bytes(b"") on records written before the migration backfill;
    # consumers should compute via :func:`compute_content_hash` rather
    # than rely on whatever is in the row.
    content_hash: bytes = b""
    # Mini-ADR U-24: high-risk publish gate. True when tool_names ∩
    # HIGH_RISK_TOOLS ≠ ∅ or any supporting_files path starts with
    # "scripts/".
    high_risk: bool = False
    # ── Stream SE — 进化溯源(Mini-ADR SE-A1)。NULL/默认 = 人写历史行。 ──
    # ``evolution_origin`` 区分 in_session(Layer A 自著)/ distilled(Layer B
    # 蒸馏);``distilled_from_*`` 指回产生这个版本的真实证据(轨迹 + candidate);
    # ``evolution_round`` 是 co-evolve 的迭代轮次(SE-6,生成↔验证有界轮)。
    evolution_origin: EvolutionOrigin | None = None
    distilled_from_trajectory_key: str | None = None
    distilled_from_candidate_id: UUID | None = None
    evolution_round: int = Field(default=0, ge=0)
    created_at: datetime


class SkillSupportingFile(BaseModel):
    """One entry in :attr:`SkillVersion.supporting_files`.

    Stored in Postgres as a JSONB object under the file's relative path.
    Two storage shapes coexist (skill-asset-store):

    * **inline** (legacy + small-package fallback): ``content`` carries the
      base64-encoded raw bytes; ``storage_key`` is ``None``.
    * **external**: bytes live in the object store under ``storage_key``
      (``skill-assets/…``); ``content`` is empty and ``sha256`` carries the
      hex digest of the raw bytes for integrity checks on fetch.

    Readers must go through :attr:`is_external` and fetch external bytes
    from the object store, verifying them against ``sha256``. The content
    hash (Mini-ADR U-21) is computed over the PERSIST shape: an external
    entry contributes its ``storage_key``/``sha256`` instead of raw bytes,
    so drift detection covers the manifest while per-fetch digest checks
    cover the bytes. A backfill that externalizes existing rows therefore
    also rewrites the stored ``content_hash`` in the same transaction.

    See Mini-ADR U-16 in ``docs/streams/STREAM-UPLIFT-DESIGN.md`` § 4.3.4.
    """

    model_config = ConfigDict(frozen=True)

    content: str = ""  # base64 of raw bytes; empty when external
    size: int = Field(ge=0)  # raw byte length (for cap checks + UI display)
    mime: str = ""
    #: Object-store key when the bytes are externalized; ``None`` = inline.
    storage_key: str | None = None
    #: Hex blake2b-256 of the raw bytes — set for external entries so a
    #: fetched object can be integrity-checked before use.
    sha256: str = ""

    @property
    def is_external(self) -> bool:
        return self.storage_key is not None


class Skill(BaseModel):
    """One row of ``skill`` — the named bundle.

    ``latest_version`` is the version number that bare ``name``
    references resolve to. When ``status == ACTIVE`` that version must
    be the highest-numbered active version; admin path mutations keep
    the invariant.

    ``description`` / ``category`` mirror the latest version's metadata
    so admin list responses don't need a join to render the listing.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    # Stream X — Mini-ADR X-1/X-2. ``None`` = platform (NULL-tenant) skill;
    # non-NULL = tenant-owned. X2 relaxes the column to NULLABLE so the
    # platform-curated library shares the ``skill`` table.
    tenant_id: UUID | None
    name: str
    status: SkillStatus
    latest_version: int = Field(ge=0)  # 0 only between create + first version insert
    description: str = ""
    category: str | None = None
    # Stream X — Mini-ADR X-2. Minimum plan tier a tenant needs to use this
    # skill. Only meaningful for platform skills (X2 makes ``tenant_id``
    # nullable; ``None`` = platform). A tenant's own skills are always usable.
    # Gated at bind / list time via ``tier_satisfies`` — never on the hot path.
    required_tier: TenantPlan = TenantPlan.FREE
    # Capability Uplift Sprint #4 — Mini-ADR U-25.
    # ``pinned`` is the operator's "do not Curator-touch" escape hatch.
    # ``last_used_at`` is the throttled (1h/skill) activity timestamp
    # bumped by ``_load_skills`` (build-time bind) + ``skill_view`` (runtime).
    # ``state_changed_at`` advances on every Curator transition + every
    # manual PATCH status; the runbook uses it to answer "when did this
    # skill go stale?" without joining the audit log.
    pinned: bool = False
    last_used_at: datetime | None = None
    state_changed_at: datetime | None = None
    # ── Stream SE — 归属 / 血缘(Mini-ADR SE-A1,落实 J.7b-1 §15.7)。 ──
    # ``visibility`` 默认 ``tenant`` 保持 M0 现状(人写 skill 租户内共享);
    # agent 自著走 ``agent_private`` 隔离。owner = **per-user 持久 agent** =
    # (tenant_id, created_by_user_id, created_by_agent_name):某用户的某 agent
    # 自著的 skill 只对"该用户的该 agent"可见,跨 manifest 版本稳定(用 agent_name
    # 而非版本级 spec id)。human 创建时两者皆 NULL。``forked_from`` 记 fork 血缘
    # 源 skill_id。
    visibility: SkillVisibility = "tenant"
    created_by_user_id: UUID | None = None
    created_by_agent_name: str | None = None
    forked_from: UUID | None = None
    # ── Stream SE — SE-10 进化对象扩展(Mini-ADR SE-A15)。 ──
    # ``component_type`` 区分这是一个普通 skill 还是三类文本 harness 组件之一;
    # 默认 ``skill`` 保持全部历史行为不变。``target_tool_name`` 仅
    # ``component_type='tool_description'`` 时非空,记被补充说明的工具名(装配期
    # 把片段追加到该工具的 description,见 agent_factory)。
    component_type: ComponentType = "skill"
    target_tool_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check_component_type(self) -> Skill:
        """``target_tool_name`` 与 ``component_type='tool_description'`` 互为充要。"""
        if self.component_type == "tool_description":
            if not self.target_tool_name:
                msg = "tool_description component requires a non-empty target_tool_name"
                raise ValueError(msg)
        elif self.target_tool_name is not None:
            msg = (
                f"target_tool_name is only valid for component_type "
                f"'tool_description', not {self.component_type!r}"
            )
            raise ValueError(msg)
        return self


class SkillEvalResult(BaseModel):
    """One row of ``skill_eval_result`` — Stream SE (Mini-ADR SE-A2).

    重放验证(SE-4)的**可溯账**:把一个候选 skill 版本注入原任务重跑,
    对比"装该 skill(treatment)"vs"不装(baseline)"的打分。这是"尽量
    全自动"治理的安全根 —— 任何自动 promote 到 active 的非高危 skill,
    必须有一条 ``verdict='pass'`` 证据(SE-A0:验证门单一收口)。

    ``tenant_id is None`` = 平台 skill 的验证结果(沿用 0057 NULL-tenant)。
    ``delta = skill_score - baseline_score``;判定规则(delta≥θ ∧ n≥N ∧
    无新增失败 → pass)在 SE-4 的 runner 里,本 DTO 只承载结果。
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID | None
    skill_id: UUID
    skill_version: int = Field(ge=1)
    baseline_score: float
    skill_score: float
    delta: float
    n_cases: int = Field(ge=0)
    replay_source: ReplaySource
    verdict: EvalVerdict
    high_risk: bool = False
    evolution_round: int = Field(default=0, ge=0)
    created_at: datetime


#: ``skill_run_usage.outcome`` 的取值域 —— ``eval_dataset.TrajectoryOutcome``
#: 的**超集**。
#:
#: 前四个是 run 的终局(SE-7d-1 原有语义,由 run 收尾时写入)。``viewed``(B-84)
#: 不是终局,它记的是**过程中**发生的一件事: 模型真的调了 ``skill_view`` 打开了
#: 这个技能。两者同表是因为问题同形 ——「这个租户的这个 agent 在这个 thread 里
#: 碰过这个技能版本没有」—— 但它们**绝不能混进同一个样本**:
#:
#: :func:`control_plane.skill_rollback.decide_rollback` 算的是
#: ``successes / len(非 cancelled)``。一条 ``viewed`` 落进那个列表就是一个
#: 「非 success」样本,会凭空拉低成功率、把健康的技能版本自动 archive 掉。
#: 所以 :meth:`SkillStore.skill_run_usage_window`(回滚判定唯一的读取口)
#: **过滤掉** ``viewed``,回滚闸门本身再过滤一次 —— 两道,因为这条路的失败
#: 后果是静默删技能。
#:
#: 刻意不扩 ``eval_dataset.TrajectoryOutcome`` 本身: 那个词表是 run/trajectory/curation
#: 三个域共用的「一次运行怎么结束的」,``viewed`` 在那里没有意义。
SkillUsageOutcome = Literal["success", "failed", "max_steps", "cancelled", "viewed"]

#: 写 ``skill_run_usage`` 的 ``viewed`` 行时用的 outcome 字面量, 单点定义,
#: 免得写入侧和过滤侧各自抄一遍字符串然后漂移。
SKILL_USAGE_VIEWED: Final[Literal["viewed"]] = "viewed"


class SkillViewGap(BaseModel):
    """B-84 —— 「绑了但这个 agent 从没打开过」的只读盘点结果。

    ``unviewed`` 是该 agent 绑定清单里, 在观测窗口内**没有**任何
    ``skill_run_usage.outcome='viewed'`` 行的技能(租户自有与平台技能都算 ——
    ``viewed`` 行的 ``tenant_id`` 是**消费方租户**, 所以平台技能对每个租户
    各自可量)。``untracked_names`` 是压根解析不到技能行的名字: 拼错的、已删的。

    两者分开摆是刻意的。``untracked_names`` 里的名字**不是**「没人读过」, 是
    「这个名字根本没对应到东西」; 混进 ``unviewed`` 就是拿「没有观测对象」
    冒充「观测到没人读」。

    ``unviewed`` 命中要连着 B-84 的上线时间一起读: ``skill_run_usage`` 在本改动
    之前从没为 ``skill_view`` 写过行, 所以「没有 viewed 行」的准确含义是
    「上线之后没记到读取」, 不是「有史以来没读过」。
    """

    model_config = ConfigDict(frozen=True)

    unviewed: tuple[Skill, ...] = ()
    untracked_names: tuple[str, ...] = ()


class SkillRunUsage(BaseModel):
    """One row of ``skill_run_usage`` — Stream SE (Mini-ADR SE-A11, SE-7d-1).

    上线后归因的 **skill-centric** 事实:某次 run(``thread_id``)装载了某个
    skill 的具体 ``skill_version``,以及那次 run 的 ``outcome``。回归回滚监控
    (SE-7d-3)按 ``(skill_id, skill_version)`` 聚合滚动窗口的成功率 —— 显著
    下降即自动 archive。

    为什么是专表而非借 trajectory metadata:回滚判定是 skill-centric 查询,
    trajectory 是 run-centric 存储,把前者架在后者全扫上是建模错配(详见
    ``docs/streams/STREAM-SE-DESIGN.md`` § 4.4)。``skill_version`` 让回滚
    **按版本判定、不连坐**下一个(可能是人审改好的)版本。

    ``tenant_id is None`` = 平台 skill 的使用记录(沿用 0057 NULL-tenant);
    ``agent_name`` 是熔断 scope key ``{tenant}:{agent}`` 的一半(SE-7c/d-3)。

    B-84 给 ``outcome`` 加了第五个取值 ``viewed`` —— 见 :data:`SkillUsageOutcome`。
    ``viewed`` 行由 ``skill_view`` 工具写, ``tenant_id`` 填**消费方租户**(平台
    技能也一样, 所以不再出现「平台技能量不到」的盲区), 且**不参与**回滚判定。
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID | None
    skill_id: UUID
    skill_version: int = Field(ge=1)
    thread_id: UUID
    agent_name: str
    outcome: SkillUsageOutcome
    created_at: datetime


class SkillPromoteRequest(BaseModel):
    """One row of ``skill_promote_request`` — Stream SE (Mini-ADR SE-A13b).

    agent_private→tenant 可见性提升的审批单,与 ``skill.status``(draft→active)
    **正交**:status 管"发布到哪条生命周期",本表管"可见范围从私有提升到租户
    共享"。承载可查的待审队列(``status='pending'``)+ 决策审计 + 多次申请历史。

    审批通过(``approved``)时,store 原子地把 skill 的 ``visibility``
    agent_private→tenant。``requested_by_*`` 记发起的 per-user agent
    (agent 经 propose_skill_to_tenant 工具发起);admin 代发起则两者可空。
    权限(SE-8):租户管理员审本租户,系统管理员审所有租户。
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID  # agent_private→tenant 恒在租户内,无平台行
    skill_id: UUID
    skill_version: int = Field(ge=1)
    status: PromoteRequestStatus
    requested_by_user_id: UUID | None = None
    requested_by_agent_name: str | None = None
    reason: str = ""
    decided_by_user_id: UUID | None = None
    decided_at: datetime | None = None
    decision_reason: str = ""
    created_at: datetime


class KillSwitch(BaseModel):
    """One row of ``skill_evolution_kill_switch`` — Stream SE (Mini-ADR SE-A13c).

    持久的"紧急停":人工把自动 generate/promote 流水线降级为全人审。补 SE-7b
    in-process ``CircuitBreaker``(per-worker、重启即丢、多副本不一致)的持久化
    缺口 —— kill-switch 跨重启/副本一致。``decide_promotion`` 读它作 halt 输入。

    与 archive(停单个已有 skill)**正交**:archive 下线一个 skill;kill-switch
    停"自动造/上线**新** skill"的整条通道,不动已有 skill。``scope='global'``
    时 ``tenant_id is None``(平台行,沿用 0057 NULL-tenant;仅 system_admin 可改);
    ``scope='tenant'`` 时 ``tenant_id`` 为该租户(租户管理员可改本租户)。
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    scope: KillSwitchScope
    tenant_id: UUID | None  # None ⇔ scope='global'
    engaged: bool
    reason: str = ""
    engaged_by_user_id: UUID | None = None
    engaged_at: datetime | None = None
    released_by_user_id: UUID | None = None
    released_at: datetime | None = None
    updated_at: datetime


class SkillPredictionVerdict(BaseModel):
    """One row of ``skill_prediction_verdict`` — Stream SE (SE-11, Mini-ADR SE-A18/A19).

    The predict→falsify discipline borrowed from agentic-harness-engineering's
    Change Manifest. The replay (``skill_eval_result``) predicted a lift from
    ``baseline_score`` (no skill) to ``skill_score`` (with skill). After
    promotion, the rollback monitor's rolling outcome window gives
    ``observed_rate``; this row records how much of the predicted gain held
    (``realized_fraction``) and the resulting label.

    Diagnostic only (SE-A19): the verdict NEVER decides archive — that stays
    with the binomial ``decide_rollback``. ``tenant_id is None`` = platform
    skill (sweep runs under owner bypass; the row is NULL-tenant like
    ``skill_eval_result``). One row per sweep that judged the version.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID | None
    skill_id: UUID
    skill_version: int = Field(ge=1)
    verdict: PredictionVerdict
    predicted_delta: float  # skill_score - baseline_score (the replay prediction)
    realized_delta: float  # observed_rate - baseline_score
    realized_fraction: float
    baseline_score: float
    skill_score: float
    observed_rate: float
    n_window: int = Field(ge=0)
    created_at: datetime


class SkillRef(BaseModel):
    """Parsed form of an ``AgentSpec.skills`` element.

    A manifest's ``skills: list[str]`` may carry either a bare name
    (``"foo"``) or a pinned reference (``"foo@3"``). The validator on
    ``AgentSpecBody.skills`` parses each entry into this DTO so the
    orchestrator's skill loader gets a typed shape.

    ``version is None`` ⇒ bind ``skill.latest_version`` (skill must be
    in ``ACTIVE`` state). ``version is not None`` ⇒ pin to the exact
    ``skill_version.version`` (draft / active / archived all allowed —
    pinning is the reproducibility escape hatch).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1)


#: Validator regex for ``AgentSpec.skills`` elements — see § 15.3.
#:
#: ``name`` allows lowercase letters / digits / dash / underscore, must
#: start with a letter, up to 64 chars; optional ``@N`` pins to a
#: specific ``skill_version.version`` (1-based positive integer).
SKILL_REF_PATTERN: str = r"^[a-z][a-z0-9_-]{0,63}(@[1-9][0-9]*)?$"


def parse_skill_ref(raw: str) -> SkillRef:
    """Parse a manifest ``skills`` entry into a :class:`SkillRef`.

    Mini-ADR J-23 (§ 15.3) — accepts ``"name"`` or ``"name@version"``;
    invalid input raises :class:`ValueError`. The orchestrator's skill
    loader uses this; the protocol-level ``AgentSpecBody.skills``
    validator delegates to it.
    """
    import re

    if not re.fullmatch(SKILL_REF_PATTERN, raw):
        msg = (
            f"skill ref {raw!r} is invalid; expected 'name' or 'name@version' "
            f"(name = [a-z][a-z0-9_-]{{0,63}}, version = positive int)"
        )
        raise ValueError(msg)
    if "@" in raw:
        name, version_str = raw.split("@", 1)
        return SkillRef(name=name, version=int(version_str))
    return SkillRef(name=raw, version=None)


# ─── Capability Uplift Sprint #3 helpers (Mini-ADR U-21 / U-24) ──────────


def is_high_risk_skill_version(
    *,
    tool_names: Iterable[str],
    supporting_file_paths: Iterable[str],
) -> bool:
    """Compute the ``high_risk`` flag for a skill version (Mini-ADR U-24).

    High-risk when **either**:

    1. ``tool_names`` intersects :data:`HIGH_RISK_TOOLS` (one of
       ``exec_python`` / ``exec_shell`` / ``http`` — tools that grant
       arbitrary code execution or unfiltered network egress); **or**
    2. Any supporting-file path starts with ``"scripts/"`` — convention
       for executable code intended to be picked up by ``exec_*`` tools.

    M0 reality: all skill mutations are admin-only so the publish gate
    is transparent. Will activate with M1-K J.7b-1 agent-self-authored
    skills, where an agent could declare ``exec_python`` and quietly
    drop a backdoor in ``scripts/diagnose.py``.
    """
    if HIGH_RISK_TOOLS & set(tool_names):
        return True
    return any(path.startswith("scripts/") for path in supporting_file_paths)


def canonicalize_skill_content(
    prompt_fragment: str,
    supporting_files: Mapping[str, object] | None = None,
) -> bytes:
    """Stable byte sequence for content hashing (Mini-ADR U-21).

    The hash is computed at write time + recomputed at every
    ``skill_view`` call;mismatch fires SKILL_DRIFT_DETECTED (almost
    certainly a SQL-injection or internal-actor signal). Hashing
    deterministically requires a canonical ordering of the
    ``supporting_files`` JSONB (Python dict insertion order is unstable
    when the row round-trips through Postgres).

    The serialization rule MUST match what migration 0042 backfill uses
    to seed existing M0 rows — otherwise every M0 skill_view would
    immediately fire a spurious P0 alert.
    """
    sorted_files = json.dumps(
        supporting_files or {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return prompt_fragment.encode("utf-8") + b"\x00" + sorted_files.encode("utf-8")


def compute_content_hash(
    prompt_fragment: str,
    supporting_files: Mapping[str, object] | None = None,
) -> bytes:
    """``blake2b(_canonicalize(...), digest_size=32)`` — Mini-ADR U-21.

    32-byte digest is enough for collision resistance against accidental
    drift detection (we are not protecting against intentional collision
    crafting; the hash exists to detect tampering, not to be a MAC).
    """
    canonical = canonicalize_skill_content(prompt_fragment, supporting_files)
    return hashlib.blake2b(canonical, digest_size=32).digest()


def supporting_files_to_jsonable(
    supporting_files: Mapping[str, SkillSupportingFile],
) -> dict[str, dict[str, object]]:
    """Typed DTO → plain JSON shape for hashing AND DB persist.

    Keys sorted so JSON serialization is deterministic across Python
    dict ordering; matches what :func:`canonicalize_skill_content`
    expects. Inline entries serialize exactly like the legacy shape
    (``content,size,mime``) so every pre-asset-store row keeps its
    original content hash; external entries additionally carry
    ``storage_key`` + ``sha256`` (with empty ``content``), making the
    hash cover the manifest while per-fetch digest checks cover the
    bytes.
    """
    out: dict[str, dict[str, object]] = {}
    for path, sf in sorted(supporting_files.items()):
        entry: dict[str, object] = {
            "content": sf.content,
            "size": sf.size,
            "mime": sf.mime,
        }
        if sf.storage_key is not None:
            entry["storage_key"] = sf.storage_key
            entry["sha256"] = sf.sha256
        out[path] = entry
    return out
