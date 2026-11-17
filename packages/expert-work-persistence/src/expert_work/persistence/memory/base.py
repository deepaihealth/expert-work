"""Abstract ``MemoryStore`` repository — Stream J.3 long-term memory.

Implementations:
- :class:`expert_work.persistence.memory.memory.InMemoryMemoryStore`
- :class:`expert_work.persistence.memory.sql.SqlMemoryStore`

The store deals in *vectors* — embedding text into a vector is the
caller's job (Stream J.3 PR2 wires the embedder). This keeps the
persistence layer free of any embedding-model dependency.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from expert_work.common.threat_patterns import ThreatFinding, scan_for_threats
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import EffectiveOrigin, MemoryOrigin, effective_origin

#: B-300 —— 不是用户更正的行(汇总行、别人改写过的行)置信度的上限:1.0 只留给用户更正
#: (更正接口独占的哨兵值)。
NON_CORRECTION_MAX_CONFIDENCE = 0.99
#: B-300 ② —— 来自助手的记忆置信度上限(与提炼第二次的封顶同一个数)。
ASSISTANT_DERIVED_MAX_CONFIDENCE = 0.6
#: B-300 重做 §6.2 —— 用户确认的记忆置信度上限(内容是模型把两行合起来写的、来源是模型判的,
#: 所以低于用户说的可以到的值;高于来自助手的,显示上分得开)。
USER_CONFIRMED_MAX_CONFIDENCE = 0.8
#: 有效来源里算「用户一方」的三种(拦截规则、混合簇判断、召回行的确认日期共用)。
USER_SIDE_ORIGINS = frozenset({"user_stated", "user_corrected", "user_confirmed"})

#: B-300 重做 §6.2 —— 来源层级:拦截规则、确定性「重复」、混合簇判断共用这一张表。
_ORIGIN_TIERS: Mapping[EffectiveOrigin, int] = {
    "user_corrected": 3,
    "user_stated": 2,
    "user_confirmed": 1,
    "assistant_derived": 0,
    "unrecorded": 0,
}


def origin_tier(item: MemoryItem) -> int:
    """一条记忆自己的来源层级(不看汇总成员):用户更正 3 > 用户说的 2 > 用户确认 1 >
    来自助手 = 未记录 0。"""
    return _ORIGIN_TIERS[effective_origin(item)]


#: B-300 重做 §6.5(用户 2026-11-16「先不能,评测过线再放开」)—— 用户确认的候选能不能退用户说的
#: 记忆。默认不能:新行照写、旧行不动、两条连线。评测线过了、用户说放开时只改这一处。
CONFIRMED_RETIRES_STATED = False

#: 一条旧记忆没被退的原因(存储在行锁里重判时给的;写回的预判用同一个 :func:`may_retire`):
#: ``gone`` 不存在 / 已删 / 已作废 / 已不是当前有效;``summary`` 是汇总行;``corrected`` 控制台
#: 更正过;``assistant`` 助手一方的候选对用户一方的记忆;``confirmed_vs_stated`` 见上;
#: ``not_correction`` 第一次提炼的候选不带更正标记对用户一方的记忆(第 10 版,§7.1 第 19 行)。
RetireRefusal = Literal[
    "gone", "summary", "corrected", "assistant", "confirmed_vs_stated", "not_correction"
]

_CORRECTED_TIER = _ORIGIN_TIERS["user_corrected"]
_STATED_TIER = _ORIGIN_TIERS["user_stated"]
#: 第 10 版(§7.1 第 19 行)—— 不带更正标记的第一次候选退不掉的层级:用户说的、用户确认。
_CORRECTION_ONLY_TIERS = frozenset({_STATED_TIER, _ORIGIN_TIERS["user_confirmed"]})


def tier_of(origin: EffectiveOrigin) -> int:
    """一种有效来源的层级(:data:`_ORIGIN_TIERS`)。"""
    return _ORIGIN_TIERS[origin]


def may_retire(
    candidate: EffectiveOrigin,
    target_tier: int,
    *,
    target_is_summary: bool,
    identical: bool = False,
    needs_correction: bool = False,
) -> RetireRefusal | None:
    """B-300 重做 §7.8 —— 来源为 ``candidate`` 的候选能不能退一条层级为 ``target_tier`` 的旧记忆
    (汇总行的层级按成员算,:func:`origin_standing`);``None`` = 能。只看来源,不看置信度。

    * 汇总行 → 不能(候选通常只否认其中一件);唯一的例外是 ``identical``:内容与候选逐字相同、
      且层级低于候选(同一句话换成来源更强的那条,没有东西会丢);
    * 控制台更正过的 → 谁也不能;
    * 助手一方(来自助手 / 未记录)的候选对用户一方的记忆 → 不能;
    * 用户确认的候选对用户说的记忆 → 默认不能(:data:`CONFIRMED_RETIRES_STATED`);
    * ``needs_correction``(第 10 版,§7.1 第 19 行;写回按候选一侧定:第一次提炼的候选、不带更正
      标记、模型把这一条列成不再成立)→ 用户说的、用户确认的不能;
    * 其余 → 能。先后、事件日期、同一条消息这三项只在写回里查。"""
    own = _ORIGIN_TIERS[candidate]
    if target_is_summary and not (identical and target_tier < own):
        return "summary"
    if target_tier >= _CORRECTED_TIER:
        return "corrected"
    if own == 0 and target_tier > 0:
        return "assistant"
    if (
        candidate == "user_confirmed"
        and target_tier == _STATED_TIER
        and not CONFIRMED_RETIRES_STATED
    ):
        return "confirmed_vs_stated"
    if needs_correction and target_tier in _CORRECTION_ONLY_TIERS:
        return "not_correction"
    return None


def is_mixed_origin(members: Sequence[MemoryItem]) -> bool:
    """B-300 §6.2 —— 一组记忆里既有「来自助手」,又有用户一方的;重做 §6.3 起,用户一方里
    层级不同的成员同簇也算(一条可能误标的用户确认不借汇总拿到用户说的保护)。

    来源未记录的不算哪一边。整理时这样的簇不汇总。"""
    origins = {effective_origin(m) for m in members}
    user_tiers = {origin_tier(m) for m in members if effective_origin(m) in USER_SIDE_ORIGINS}
    return len(user_tiers) > 1 or ("assistant_derived" in origins and bool(user_tiers))


@dataclass(frozen=True)
class SummaryFields:
    """B-300 D6 前半 + §6.2 —— 一条汇总行从成员继承的出处、分数与来源。"""

    source_thread_id: str | None
    source_run_id: str | None
    importance: float
    confidence: float
    origin: MemoryOrigin | None


def summary_fields(members: Sequence[MemoryItem]) -> SummaryFields:
    """汇总行带的出处和分数(两个存储实现在写汇总的事务里、按重读到的成员算):

    * 来源会话 / 来源 run 取成员里最新(``created_at`` 最大)的那一条的;
    * 重要度取成员最大值;置信度取成员最小值,上限 0.99(1.0 只留给用户更正);
    * 成员全是「来自助手」→ 来源记「来自助手」、置信度再压到 0.6 以内;否则来源为空
      (汇总句是模型新写的文字,不给「用户消息里说的」)。
    """

    def _created(item: MemoryItem) -> float:
        return item.created_at.timestamp() if item.created_at is not None else float("-inf")

    newest = max(members, key=_created)
    all_assistant = all(effective_origin(m) == "assistant_derived" for m in members)
    confidence = min(min(m.confidence for m in members), NON_CORRECTION_MAX_CONFIDENCE)
    if all_assistant:
        confidence = min(confidence, ASSISTANT_DERIVED_MAX_CONFIDENCE)
    return SummaryFields(
        source_thread_id=newest.source_thread_id,
        source_run_id=newest.source_run_id,
        importance=max(m.importance for m in members),
        confidence=confidence,
        origin="assistant_derived" if all_assistant else None,
    )


#: 嵌套汇总向下查成员的层数上限;查到上限还有没查的成员,按「受保护」处理(保守方向)。
MAX_SUMMARY_DEPTH = 8

#: 取「用户后来的说法」时顺取代链(``superseded_by``)最多走这么多步去找仍有效的那条;走不到就
#: 当作没有可用的说法(也防环)。
MAX_STATEMENT_HOPS = 5

#: 按 id 取一批记忆(不看是否仍有效、是否已删):受保护判断向下查汇总成员用。
MemberLoader = Callable[[Sequence[UUID]], Awaitable[Sequence[MemoryItem]]]


#: 汇总成员查不完时按这个算(保守方向:等于用户更正,任何提炼出的候选都退不掉它)。
FAIL_CLOSED_TIER = _CORRECTED_TIER


async def origin_standing(item: MemoryItem, load: MemberLoader) -> int:
    """B-300 重做 §6.3 —— 「谁能退谁」看的目标层级(第 9 版起不带置信度)。

    * 不是汇总行:它自己的层级;
    * 汇总行:它自己与成员 —— 一层层往下,成员本身是汇总的接着查它的成员 —— 里最高的层级。
      成员早已被汇总、取代或过期,照样算(汇总句里还有它的内容)。

    ``visited`` 防环;``MAX_SUMMARY_DEPTH`` 层之后还有没查的成员 → :data:`FAIL_CLOSED_TIER`。"""
    top = origin_tier(item)
    visited = {item.id}
    frontier = [sid for sid in item.consolidated_from if sid not in visited]
    for _depth in range(MAX_SUMMARY_DEPTH):
        if not frontier:
            break
        visited.update(frontier)
        members = await load(frontier)
        top = max([top, *(origin_tier(m) for m in members)])
        frontier = list(
            dict.fromkeys(sid for m in members for sid in m.consolidated_from if sid not in visited)
        )
    return FAIL_CLOSED_TIER if frontier else top


@dataclass(frozen=True)
class BlockedMemory:
    """B-300 D2 — one memory text the strict write scan refused.

    Carries what an audit row may hold — the item id, the text length and the
    matched rule categories — and the findings themselves; never the text.
    """

    item_id: UUID
    content_length: int
    findings: tuple[ThreatFinding, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({str(f.category) for f in self.findings}))


@dataclass(frozen=True)
class MemoryWriteResult:
    """B-300 D2 — what :meth:`MemoryStore.write` did not store, item by item.

    B-300 D12 — ``written`` is how many rows actually landed: an item whose
    ``(tenant, user, content_hash)`` already has a live row is skipped (the
    dedup no-op), and a blocked item is not written either."""

    blocked: tuple[BlockedMemory, ...] = ()
    written: int = 0


def scan_memory_content(item_id: UUID, content: str) -> BlockedMemory | None:
    """The one strict scan every automatic memory write goes through (B-300 D2):
    ``write`` / ``supersede`` / ``write_consolidated``. ``None`` = clean."""
    findings = scan_for_threats(content, scope="strict")
    if not findings:
        return None
    return BlockedMemory(item_id=item_id, content_length=len(content), findings=tuple(findings))


def split_blocked(items: Sequence[MemoryItem]) -> tuple[list[MemoryItem], list[BlockedMemory]]:
    """Scan a batch: the items that pass, and one :class:`BlockedMemory` per refused item."""
    clean: list[MemoryItem] = []
    blocked: list[BlockedMemory] = []
    for item in items:
        hit = scan_memory_content(item.id, item.content)
        if hit is None:
            clean.append(item)
        else:
            blocked.append(hit)
    return clean, blocked


class MemoryInjectionBlockedError(Exception):
    """Capability Uplift Sprint #2 (Mini-ADR U-3) — write rejected.

    B-300 D2: raised by the single-item writes — :meth:`MemoryStore.supersede`
    and :meth:`MemoryStore.write_consolidated` — when the new text matches the
    ``strict`` threat-pattern set, before anything is changed (the old row stays
    current, no summary is written). :meth:`MemoryStore.write` no longer raises:
    it stores the clean items and returns the blocked ones.

    Carries the blocked items so the caller can emit one
    ``MEMORY_INJECTION_BLOCKED`` audit row per item with its own context.
    """

    def __init__(self, blocked: Sequence[BlockedMemory]) -> None:
        super().__init__(f"{len(blocked)} memory item(s) blocked by strict threat scan")
        self.blocked = tuple(blocked)


@dataclass(frozen=True)
class SupersedeResult:
    """B-300 重做 §7.8 —— :meth:`MemoryStore.supersede` 做了什么。

    ``item``:新写的行;没有插入(``inserted`` 为假)时是它撞上的那条内容相同、层级不低于它的
    有效行(那句话已经由它说着,被退的旧行连到它)。``retired``:作废了的旧行(含
    ``collision_replaced`` 那一条);``refused``:没退的旧行各自的原因;``flagged``:打上「要重写」
    标记的汇总行(可能是被点到的那条已被重写出的新汇总行);``collision_replaced``:撞上唯一索引、
    层级低于候选而在同一次事务里被退掉的那条。"""

    item: MemoryItem
    inserted: bool = True
    retired: tuple[UUID, ...] = ()
    refused: Mapping[UUID, RetireRefusal] = field(default_factory=dict)
    flagged: tuple[UUID, ...] = ()
    collision_replaced: UUID | None = None


@dataclass(frozen=True)
class RetireResult:
    """B-300 重做 §7.8 —— :meth:`MemoryStore.retire`(只退不写)做了什么。``anchor_live`` 为假 =
    要连过去的那条已有的行此刻不是同一用户的有效行、或层级低于候选:什么都没改。"""

    anchor_live: bool
    retired: tuple[UUID, ...] = ()
    refused: Mapping[UUID, RetireRefusal] = field(default_factory=dict)


@dataclass(frozen=True)
class FlaggedSummary:
    """B-300 重做 §7.12 —— 一条打了「要重写」标记、仍有效的汇总行,连同重写要看的材料:它的成员
    (不论此刻是否有效)与用户后来的说法:``supersedes`` 指向它的用户一方的行(记录或最后再说的
    时间晚于它),加上它自己的 ``supersedes`` 指着的那一行(打标时记下的依据);其中已被更晚一句
    取代的,顺 ``superseded_by`` 走到仍有效的用户一方后继(至多 :data:`MAX_STATEMENT_HOPS` 步);
    被删掉的、走不到这样后继的不算。"""

    summary: MemoryItem
    members: tuple[MemoryItem, ...]
    statements: tuple[MemoryItem, ...]


#: :meth:`MemoryStore.rewrite_summary` 的结果(§7.12 第 3 步)。
SummaryRewriteOutcome = Literal[
    "rewritten", "emptied", "unchanged", "collided", "stale", "corrected_member"
]


class ConsolidationSourcesChangedError(RuntimeError):
    """B-300 D1 — a summary's members changed after the cluster was picked.

    Raised by :meth:`MemoryStore.write_consolidated` when, re-read under a row
    lock in the write's own transaction, a member is no longer current
    (deleted, superseded, expired, archived) or was already folded into another
    summary. Nothing is written; the next sweep re-reads the members.
    """


class MemoryStore(abc.ABC):
    """Cross-session memory repository, scoped to ``(tenant_id, user_id)``."""

    @abc.abstractmethod
    async def write(self, items: Sequence[MemoryItem]) -> MemoryWriteResult:
        """Persist long-term memories. Each item carries its own ``id``
        and ``embedding``.

        B-300 D2 — every item goes through :func:`scan_memory_content`; a
        blocked item is dropped on its own (the rest of the batch is written)
        and returned in :attr:`MemoryWriteResult.blocked` for the caller to
        audit. Nothing is raised for a blocked item."""

    @abc.abstractmethod
    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        query_embedding: Sequence[float],
        query_text: str | None = None,
        kind: Literal["fact", "episodic"] | None = None,
        agent_name: str | None = None,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[MemoryItem]:
        """Return the user's ``limit`` memories nearest ``query_embedding``,
        closest first. Soft-deleted rows are excluded (Stream K.K6).

        Stream Agent-Templates (M1-5c): when ``agent_name`` is given, episodic
        memories are scoped to that agent — the result includes shared ``fact``
        rows (``agent_name IS NULL``) plus ``episodic`` rows owned by this agent,
        so one agent's events never leak into another's recall. ``None`` (the
        admin / legacy path) applies no agent scoping.

        Capability Uplift Sprint #6 (Mini-ADR U-5): when ``query_text``
        is ``None`` the retrieval is pure-vector (cosine distance against
        the pgvector column) — the pre-Sprint-#6 behavior, kept for
        backward compatibility. When ``query_text`` is a non-empty string
        the retrieval is **hybrid**: vector recall is fused with Postgres
        full-text recall via Reciprocal Rank Fusion (``k=60``). Empty /
        whitespace-only ``query_text`` degrades to the vector path.

        Stream P5b — ``as_of`` (when given) time-travels retrieval: instead of
        "currently valid" it returns rows that were valid AT that instant
        (``valid_at <= as_of`` and neither superseded nor expired as of then).
        ``None`` (default) is the current-time behaviour. ``deleted_at`` and
        the archived/consolidated filters still apply — as_of only time-travels
        the bi-temporal (valid/invalid/expired) dimension."""

    @abc.abstractmethod
    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        kind: Literal["fact", "episodic"] | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """Stream K.K6 — list a user's live memories, newest first.

        Soft-deleted rows are filtered out so the UI / API caller never
        sees a forgotten memory. ``kind`` optionally narrows. The
        ``embedding`` field is still populated (callers may project it
        away at the API boundary).

        Stream P5b-2c — ``as_of`` (when given) time-travels the bi-temporal
        (valid/invalid/expired) dimension the same way :meth:`retrieve` does:
        instead of "currently live" it returns rows that were valid AT that
        instant. ``None`` (default) applies NO bi-temporal filter — today's
        behavior (only ``deleted_at`` + optional ``kind``) is unchanged, so
        e.g. a superseded or not-yet-valid row still surfaces for direct
        inspection (mirrors how ``supersede()``'s closed old row is reached
        via this method)."""

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        kind: Literal["fact", "episodic"] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """Cross-tenant memory list — Stream N (Mini-ADR N-4).

        Caller MUST be inside ``bypass_rls_session()``. No ``user_id``
        filter — the platform admin view aggregates every user's
        memories across every tenant. Soft-deleted rows are excluded,
        newest first.
        """

    @abc.abstractmethod
    async def get_live(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> MemoryItem | None:
        """B-300 D8 — one live memory by id, scoped like :meth:`update_content`.

        Returns ``None`` for unknown id / wrong tenant / wrong user / already
        soft-deleted — exactly the rows :meth:`update_content` would refuse, so
        the console's write endpoints can 404 before they scan or re-embed."""

    @abc.abstractmethod
    async def update_content(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        content: str,
        embedding: Sequence[float],
        kind: Literal["fact", "episodic"] | None = None,
        confidence: float | None = None,
        embedding_model: str | None = None,
        clear_origin: bool = False,
        origin: MemoryOrigin | None = None,
        max_confidence: float | None = None,
    ) -> MemoryItem | None:
        """Stream K.K6 — rewrite a live memory's content / kind.

        B-300 PR 2 — ``embedding_model`` (the model that computed ``embedding``;
        ``None`` = not known) always replaces the row's value, since the vector
        is replaced. The source column: ``clear_origin`` sets it to NULL,
        otherwise a given ``origin`` replaces it, otherwise it is kept (a user
        correction keeps it — its effective source is the 1.0 confidence).
        ``max_confidence`` caps the row's confidence as stored (review
        MEDIUM-2: someone else's edit takes the row out of the 1.0 sentinel).
        B-300 redesign — ``clear_origin`` also clears ``confirmed_at`` (the row is
        no longer on the user's side); the evidence pointer and event date stay
        (they say where the memory came from and which day it is about).

        The caller must re-embed before calling — the store does not
        own an embedder. Soft-deleted rows are not updatable; returns
        ``None`` for unknown id / wrong tenant / wrong user / already
        soft-deleted.

        Stream Memory-Enhance (M-4) — ``confidence`` (when given) overwrites the
        row's confidence; the user-correction endpoint sets it to 1.0.
        CONTRACT (RT-2 PR-2): 1.0 is that endpoint's EXCLUSIVE sentinel — the
        injection budget's correction guarantee keys on ``confidence >= 1.0``
        and the extraction write path caps at 0.99 (orchestrator
        ``memory._MAX_EXTRACTED_CONFIDENCE``); no other caller may write 1.0."""

    @abc.abstractmethod
    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """Stream K.K6 — stamp ``deleted_at`` (the forget action).

        Idempotent: returns ``True`` even when already deleted. Returns
        ``False`` for unknown id / wrong tenant / wrong user."""

    @abc.abstractmethod
    async def bump_access(self, *, tenant_id: UUID, user_id: UUID, ids: Sequence[UUID]) -> None:
        """P5a — mark memories as retrieved: stamp ``last_used_at=now`` and
        increment ``access_count`` for each id. Best-effort at the call site
        (recall must not fail on a bump error). Empty ``ids`` is a no-op.
        Tenant- AND user-scoped as a defensive predicate."""

    @abc.abstractmethod
    async def supersede(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        new_item: MemoryItem,
        old_ids: Sequence[UUID] = (),
        identical_ids: Sequence[UUID] = (),
        flag_summary_ids: Sequence[UUID] = (),
        link_id: UUID | None = None,
        needs_correction_ids: Sequence[UUID] = (),
    ) -> SupersedeResult:
        """P5b / B-300 redesign §7.8 — write ``new_item`` and, in the same transaction,
        retire zero or more older rows (``invalid_at=now``, ``superseded_by`` = the new
        row). **The write never depends on a retirement**: a target that cannot be
        retired is skipped with its reason and the new row is written all the same.
        Tenant- AND user-scoped. The old rows are KEPT (history) but leave recall.

        ``old_ids`` are locked in id order and each is re-judged under the lock: unknown /
        deleted / already superseded / no longer current (folded into a summary, expired,
        archived) → skipped, ``gone``; otherwise :func:`may_retire` with the new row's
        effective origin and the target's tier by members (``summary`` / ``corrected`` /
        ``assistant`` / ``confirmed_vs_stated``). ``needs_correction_ids`` (rev 10, §7.1
        row 19; targets and summaries to flag) are the ones the caller holds to the
        correction rule — a first-pass candidate without a correction mark that the model
        said replaces them: each is re-judged with ``needs_correction`` on its tier **now**,
        so a row that turned user-side meanwhile (the owner edited it in the console) is
        refused ``not_correction`` and a summary whose members did is not flagged.
        ``identical_ids`` (a subset of
        ``old_ids``) are rows whose content is the same as the new one: for them the
        summary rule has its one exception (same sentence, weaker source).

        Dedup index (tenant + user + content hash + agent name, kind and origin not
        included): when another row already holds the same content **and is currently
        valid** (the recall predicate), its tier decides — not lower than the new row's →
        nothing is inserted, the retired rows link to it and it is returned
        (``inserted=False``); lower → it is retired as well (``collision_replaced``) and
        the new row is inserted. A holder that only occupies the index — a member folded
        into a summary is not recalled — does not count as "already there": it is marked
        superseded by the new row to give up the slot (not reported in ``retired``) and
        the new row is inserted.

        ``flag_summary_ids`` (§7.12 step 1; the caller passes summaries the model itself
        listed as no longer true, for a user-side candidate — a new row that is not on
        the user's side flags nothing): **each** still-current summary
        whose members hold no console correction gets ``review_flagged_at=now`` and its
        own ``supersedes`` set to the new row's id — the row the rewrite goes by (the
        column has no other use on a summary row; a rewritten summary does not inherit
        it); one that was rewritten meanwhile is followed one step along
        ``superseded_by`` to the current summary. Only when the new row is inserted.

        The new row's ``supersedes``: the first flagged summary (the rewrite finds the
        user's statement through this link; what the row retired is still found through
        the retired rows' ``superseded_by``); else the first retired row; else the first
        target refused as ``confirmed_vs_stated`` or ``not_correction``; else ``link_id``
        (when that row exists for this user). Except for a retired row, what it points at
        is still valid — the link only says "about the same thing"; whether a row was
        replaced is read from that row's ``invalid_at``.

        B-300 D2 — ``new_item`` goes through the strict scan; when blocked,
        :class:`MemoryInjectionBlockedError` is raised before anything changes.
        B-300 redesign §9.2 — ``confirmed_at`` exists only on user-side rows."""

    @abc.abstractmethod
    async def retire(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        old_ids: Sequence[UUID],
        anchor_id: UUID,
        candidate_origin: EffectiveOrigin,
        needs_correction_ids: Sequence[UUID] = (),
    ) -> RetireResult:
        """B-300 redesign §7.8 — retire without writing: the candidate repeats the live
        row ``anchor_id``, and ``old_ids`` are no longer needed. Same locks and the same
        re-judgement as :meth:`supersede` (``needs_correction_ids`` included); retired rows
        get ``superseded_by=anchor_id``.
        The anchor must be this user's currently valid row whose tier (by members) is not
        lower than the candidate's — otherwise nothing changes (``anchor_live=False``)."""

    @abc.abstractmethod
    async def write_history(
        self, *, tenant_id: UUID, user_id: UUID, anchor_id: UUID, new_item: MemoryItem
    ) -> MemoryItem | None:
        """B-300 redesign §7.1 row 11 — store ``new_item`` as a history row: already
        superseded when it lands (``invalid_at=now``, ``superseded_by=anchor_id``), no
        other row touched. ``None`` (nothing written) when the anchor is no longer this
        user's currently valid row — the caller then writes a normal row. A history row
        does not take part in the dedup index. Idempotent: when a history row with the
        same content already hangs off this anchor (a retried job), that row is returned
        and nothing is written — its ``id`` differs from ``new_item.id``. Strict scan as
        :meth:`supersede`."""

    @abc.abstractmethod
    async def flag_summary(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        summary_id: UUID,
        statement_id: UUID,
        needs_correction: bool = False,
    ) -> UUID | None:
        """B-300 redesign §7.12 step 1, for a candidate that is **not written** because it
        repeats ``statement_id``: the user denied something the summary says, so the
        summary is flagged all the same, with the row the candidate repeats as the
        statement to rewrite by. That row must be this user's currently valid user-side
        row; the summary is judged as in :meth:`supersede` (members without a console
        correction; one step along ``superseded_by`` when it was rewritten meanwhile;
        ``needs_correction`` as for ``needs_correction_ids``).
        The basis is recorded on the **summary** (its ``supersedes`` = ``statement_id``);
        the statement row itself is not changed. Returns the flagged summary's id, or
        ``None`` when nothing was flagged."""

    @abc.abstractmethod
    async def expire(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """P5b — retraction: stamp ``expired_at=now`` on a fact that stopped
        being true in the world with NO successor (orthogonal to ``supersede``'s
        ``invalid_at``). The row is KEPT (history/audit) but excluded from
        default retrieve. Returns ``False`` for unknown id / wrong tenant-user.
        Tenant- AND user-scoped. Used by consolidation's due review; the write-back
        no longer expires anything (B-300 redesign §7.3).

        B-300 review MEDIUM-1 — the target row is locked (``FOR UPDATE``) before
        it is changed."""

    @abc.abstractmethod
    async def origin_standing(
        self, *, tenant_id: UUID, user_id: UUID, memory_id: UUID
    ) -> int | None:
        """B-300 redesign §6.3 — :func:`origin_standing` (the tier by members) for one
        row, its summary members read whatever their current validity. Unknown id → None."""

    @abc.abstractmethod
    async def refresh_confirmed_at(
        self, *, tenant_id: UUID, user_id: UUID, memory_id: UUID, confirmed_at: datetime
    ) -> bool:
        """B-300 redesign §7.1 row 8 — the user side said this memory again: move its
        ``confirmed_at`` to ``confirmed_at`` (never backwards). Only a row that is not
        deleted, superseded or expired and whose effective origin is on the user's
        side is changed; returns whether it was."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — soft-delete EVERY live memory for a user.

        Bulk ``soft_delete``: stamps ``deleted_at`` on all of the user's
        not-yet-deleted rows and returns the count newly deleted (a re-purge
        returns 0 — already-deleted rows are skipped). Tenant- AND user-scoped:
        the ``(tenant_id, user_id)`` predicate never touches another tenant's
        or user's memories. The rows are KEPT (recoverable within the retention
        window); a later retention sweep hard-deletes them."""

    @abc.abstractmethod
    async def hard_delete_expired(self, *, before: datetime, limit: int = 1000) -> int:
        """PR1 Task 1 — retention job: physically DELETE soft-deleted rows
        whose ``deleted_at`` is older than ``before``.

        Cross-tenant: NO ``(tenant_id, user_id)`` predicate — unlike every
        other method on this interface, this is the platform-wide retention
        sweep's own method (consumed by the ``retention-cleanup-job``), not
        a per-tenant repository call. Deletes at most ``limit`` rows, oldest
        ``deleted_at`` first, and returns the number of rows actually
        removed. Live rows (``deleted_at IS NULL``) are never touched."""

    # ------------------------------------------------------------------
    # Capability Uplift Sprint #7 — MemoryConsolidator interface.
    # Mini-ADRs U-33 / U-34 / U-37 / U-40. The control-plane
    # ``MemoryConsolidator`` worker is the only caller of these methods
    # in M0; M1-K Admin UI may extend.
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def consolidator_distinct_tenant_ids(self) -> list[UUID]:
        """Return all tenant ids with at least one live transient memory row.

        Used by the ``MemoryConsolidator`` worker to enumerate tenants
        worth scanning per tick (skipping tenants with no transient data).
        Mirrors :meth:`SkillStore.curator_distinct_tenant_ids` (Sprint #4
        Mini-ADR U-26 pattern). Soft-deleted rows are excluded."""

    @abc.abstractmethod
    async def distinct_users(
        self,
        *,
        tenant_id: UUID,
        after: UUID | None = None,
        limit: int | None = None,
    ) -> list[UUID]:
        """Return distinct ``user_id`` with at least one transient row.

        Used by the consolidator worker to enumerate users worth
        scanning per tick (skipping users with no fresh writes).
        Soft-deleted rows are excluded.

        B-300 D4 — ordered by user id; ``after`` keeps only ids greater than it
        (the consolidation cursor), ``limit`` caps the count."""

    @abc.abstractmethod
    async def get_consolidation_cursor(self, *, tenant_id: UUID) -> UUID | None:
        """B-300 D4 — the last user the consolidator swept in this tenant, or
        ``None`` before the first one."""

    @abc.abstractmethod
    async def set_consolidation_cursor(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """B-300 D4 — move the tenant's consolidation cursor to ``user_id``."""

    @abc.abstractmethod
    async def list_rejected_clusters(self, *, tenant_id: UUID, user_id: UUID) -> set[str]:
        """Member-set hashes of the clusters the consolidation model rejected
        for this user (B-300 PR A rejected-cluster markers)."""

    @abc.abstractmethod
    async def record_rejected_cluster(
        self, *, tenant_id: UUID, user_id: UUID, member_hash: str, reason: str
    ) -> None:
        """Mark a rejected cluster (by its member-set hash); re-marking the
        same set refreshes its reason and time."""

    @abc.abstractmethod
    async def prune_rejected_clusters(self, *, tenant_id: UUID, before: datetime) -> int:
        """Drop the tenant's markers (every user) recorded before ``before``;
        returns how many."""

    @abc.abstractmethod
    async def delete_rejected_clusters_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Drop every marker of a user (user purge); returns how many."""

    @abc.abstractmethod
    async def list_transient(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        max_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        """Sprint #7 SUB-PASS 1 candidate fetch — return live transient
        items written within ``max_age_days``, oldest first, capped at
        ``limit``. Skips items already consolidated_into something.

        B-300 D1 — only currently-valid rows: the same predicate as the
        current-time :meth:`retrieve` (no superseded / expired / archived row)."""

    @abc.abstractmethod
    async def vector_neighbors(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        embedding: Sequence[float],
        cosine_max: float,
        limit: int,
    ) -> list[MemoryItem]:
        """Sprint #7 SUB-PASS 1 cluster builder — return live transient
        items within ``cosine_max`` cosine distance of ``embedding``,
        closest first, capped at ``limit``. Skips items already
        ``consolidated_into`` something; B-300 D1 — currently-valid rows only,
        as :meth:`list_transient`.

        Implementations should use the existing pgvector HNSW index
        (no new index for Sprint #7)."""

    @abc.abstractmethod
    async def write_consolidated(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        content: str,
        embedding: Sequence[float],
        source_ids: Sequence[UUID],
        source_hashes: Mapping[UUID, str],
        embedding_model: str | None = None,
    ) -> MemoryItem:
        """Sprint #7 SUB-PASS 1 commit — write a new consolidated item
        with ``status='consolidated'`` + ``consolidated_from=source_ids``
        and atomically mark every source's ``consolidated_into`` to the
        new id. The new item's ``kind`` is always ``"fact"`` (episodic
        consolidation is out-of-scope for Sprint #7).

        Idempotent under retry: if any source already has
        ``consolidated_into`` set the entire operation aborts and the
        method raises so the worker can skip the cluster on the next tick.

        B-300 — the members are re-read (row-locked) in the write's own
        transaction; when any is missing or no longer current
        :class:`ConsolidationSourcesChangedError` is raised and nothing is
        written (D1). ``source_hashes`` carries each member's ``content_hash``
        as it was read when the cluster was picked; a member corrected in place
        meanwhile (still current, new text) counts as changed too — the summary
        was decided on the old text. ``content`` goes through the strict write scan first;
        blocked → :class:`MemoryInjectionBlockedError`, nothing written (D2).

        B-300 PR 2 — the summary row takes its provenance, scores and source
        from the re-read members (:func:`summary_fields`) and records
        ``embedding_model``. Members that turned out mixed-source (assistant and
        user side, :func:`is_mixed_origin`) raise
        :class:`ConsolidationSourcesChangedError` — such clusters are never
        summarized; the consolidator filters them when it picks clusters, this
        re-check covers a member corrected meanwhile."""

    @abc.abstractmethod
    async def list_purge_candidates(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        min_age_days: int,
        limit: int,
    ) -> list[MemoryItem]:
        """Sprint #7 SUB-PASS 2 candidate fetch — return live transient
        items older than ``min_age_days`` that have never been retrieved
        (``access_count == 0``), have never been
        reviewed (``last_reviewed_at IS NULL``) and that the user never said
        again (``confirmed_at IS NULL``, B-300 redesign — a restated memory is
        not noise).

        Oldest first, capped at ``limit``. The 3 filters together are
        Mini-ADR U-37's purge protections."""

    @abc.abstractmethod
    async def mark_reviewed(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """Sprint #7 SUB-PASS 2 commit — stamp ``last_reviewed_at`` so
        future ticks skip this item, and clear ``review_flagged_at``
        (Stream HX-2 — a reviewed flag is a consumed flag). Returns
        ``False`` for unknown id / wrong tenant / wrong user."""

    @abc.abstractmethod
    async def flag_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str,
    ) -> int:
        """Stream HX-2 (Mini-ADR HX-B3) — stamp ``review_flagged_at`` on
        every live transient item extracted from ``source_thread_id``.

        Returns the number of items flagged. Idempotent: re-flagging
        refreshes the timestamp. Consolidated parents are not touched
        (their transient sources are; cascade re-review is out of scope).
        """

    @abc.abstractmethod
    async def list_review_flagged(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        """Stream HX-2 — live transient items with ``review_flagged_at``
        set, oldest flag first, capped at ``limit``. Unlike
        :meth:`list_purge_candidates` there is no age / retrieval / prior
        review filter — a user 👎 re-opens even a previously-reviewed
        item.
        """

    @abc.abstractmethod
    async def list_flagged_summaries(
        self, *, tenant_id: UUID, user_id: UUID, limit: int
    ) -> list[FlaggedSummary]:
        """B-300 redesign §7.12 — this user's currently valid summary rows
        (``status='consolidated'``) with ``review_flagged_at`` set, oldest flag first,
        each with its members and the user's later statements. The thumbs-down flag on
        transient rows (:meth:`list_review_flagged`) is the same column and never mixes
        with this: the two read different ``status`` values."""

    @abc.abstractmethod
    async def rewrite_summary(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        summary_id: UUID,
        flagged_at: datetime,
        content: str | None,
        embedding: Sequence[float] = (),
        embedding_model: str | None = None,
        unchanged: bool = False,
    ) -> SummaryRewriteOutcome:
        """B-300 redesign §7.12 step 3 — replace a flagged summary, under its row lock.

        * no longer current, or its ``review_flagged_at`` differs from ``flagged_at`` (a
          new denial was linked while the rewrite ran) → ``stale``, nothing changes, the
          flag stays;
        * a member (down the tree) was corrected in the console meanwhile →
          ``corrected_member``: summary untouched, flag cleared;
        * ``unchanged`` → flag cleared, nothing else;
        * ``content is None`` (nothing of it remains true) → the summary is retired with
          ``superseded_by`` = the user's latest later statement; ``emptied``. With no
          such statement left there is nothing to link it to: ``stale``, nothing changes;
        * another **currently valid** row already holds ``content`` → nothing inserted,
          the summary is retired with ``superseded_by`` = that row, members untouched;
          ``collided``. A holder that is not recalled (one of the summary's own members,
          say) gives up the index slot instead and the new summary is stored;
        * otherwise a new summary row is inserted (same ``consolidated_from``; source and
          scores recomputed from the members, :func:`summary_fields`), the old one is
          retired and linked to it, the members' ``consolidated_into`` moves to it and
          the flag is gone; the new row does not inherit links to the old one;
          ``rewritten``.

        ``content`` goes through the strict scan first; blocked →
        :class:`MemoryInjectionBlockedError`, nothing changed (the caller clears the flag)."""

    @abc.abstractmethod
    async def defer_summary_rewrite(
        self, *, tenant_id: UUID, user_id: UUID, summary_id: UUID
    ) -> bool:
        """B-300 redesign §7.12 — a rewrite attempt failed: stamp ``last_reviewed_at=now``
        on the summary and keep its flag (a summary has no review window, so the column
        has no other use on it). Returns whether a row was stamped."""

    @abc.abstractmethod
    async def list_due_for_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
    ) -> list[MemoryItem]:
        """P5b-2b ⑦ — live facts whose predicted validity window is due for
        re-review: ``expected_valid_days IS NOT NULL`` and
        ``COALESCE(last_reviewed_at, created_at) + expected_valid_days days
        <= now``, excluding deleted / invalidated (superseded) / expired rows.
        Oldest-due first, capped at ``limit``. Unlike
        :meth:`list_purge_candidates` there is NO access_count / status
        filter — a retrieved, in-use fact is exactly what predictive review
        must revisit."""

    @abc.abstractmethod
    async def renew_review(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
        expected_valid_days: int | None,
    ) -> bool:
        """P5b-2b ⑦ still-true verdict — stamp ``last_reviewed_at=now`` (re-arms
        the due-scan window from now) and set ``expected_valid_days`` to the
        given value (None clears it → the row drops out of future due-scans).
        Returns False for unknown id / wrong tenant / wrong user / deleted."""

    @abc.abstractmethod
    async def archive(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        memory_id: UUID,
    ) -> bool:
        """Reserved for M2-C archive pipeline (Stream K.K6 vNext).

        Sprint #7 (Mini-ADR U-40) reserves the interface +
        ``status='archived'`` semantics so M2-C can land without a
        schema migration. Sprint #7 implementations raise
        ``NotImplementedError`` to make the gap loud — better than a
        silent no-op."""

    # --- B-278 彻底删除智能体 ---------------------------------------------

    @abc.abstractmethod
    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """B-278 —— rows ``delete_for_agent`` would remove (purge preview)."""

    @abc.abstractmethod
    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """B-278 —— hard-delete every row whose ``agent_name`` is exactly this
        name (agent purge). Tenant-scoped; returns the rows removed.

        Shared memories (``agent_name IS NULL``) are never touched, even when
        their source conversation belonged to this agent — they are the
        user's, not the agent's. Soft-deleted rows go too: the purge is
        irreversible, it does not wait for the retention sweep."""
