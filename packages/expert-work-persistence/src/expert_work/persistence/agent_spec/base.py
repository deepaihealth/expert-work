"""Abstract :class:`AgentSpecStore` — Stream B.5."""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from expert_work.protocol import (
    AgentSpec,
    AgentSpecRecord,
    AgentSpecRevisionRecord,
    AgentSpecStatus,
)


@dataclass(frozen=True)
class AgentSpecUpdateResult:
    """Outcome of :meth:`AgentSpecStore.update_spec` — Stream HX-5.

    ``revision`` is the history row this update appended, or ``None``
    for a no-op (the new sha equals the stored one — nothing changed,
    nothing recorded). ``prev_sha256`` feeds the MANIFEST_WRITE audit
    so every change carries its before/after pair.
    """

    record: AgentSpecRecord
    revision: int | None
    prev_sha256: str


class DuplicateAgentSpecError(Exception):
    """Raised when the unique ``(tenant_id, name, version)`` index trips
    on insert. The API layer maps it to ``HTTP 409``."""

    def __init__(self, *, tenant_id: UUID, name: str, version: str) -> None:
        super().__init__(
            f"agent_spec already exists: tenant_id={tenant_id} name={name} version={version}"
        )
        self.tenant_id = tenant_id
        self.name = name
        self.version = version


class AgentSpecPurgingError(DuplicateAgentSpecError):
    """B-278 —— the name is being purged (some version is ``purging``): no new
    version may be created under it until the purge finishes. A subclass of
    :class:`DuplicateAgentSpecError` so every create / fork call site that already
    maps "this name+version is taken" to 409 covers it; the API tells the two apart
    and answers ``AGENT_PURGING``."""


class AgentSpecBusyError(DuplicateAgentSpecError):
    """B-278 —— two writers on the same name collided (Postgres deadlock on the
    name's row locks). Retryable; the API answers 409 ``AGENT_BUSY``. A
    :class:`DuplicateAgentSpecError` subclass so create / fork's existing 409
    path covers it."""

    def __init__(self, *, tenant_id: UUID, name: str, version: str = "") -> None:
        super().__init__(tenant_id=tenant_id, name=name, version=version)
        self.args = (f"agent {name!r} is being changed concurrently; retry",)


class AgentSpecPurgeBlockedError(Exception):
    """B-278 —— the purge cannot go on: a version of the name is live (not
    ``deleted`` / ``purging``), or a live agent of the tenant lists it as a
    subagent. Raised by :meth:`AgentSpecStore.begin_purge` /
    :meth:`AgentSpecStore.purge_name` under the row lock; the API answers
    409 ``AGENT_PURGE_BLOCKED``."""

    def __init__(
        self, *, name: str, active_versions: list[str], referenced_by: list[str] | None = None
    ) -> None:
        super().__init__(
            f"agent {name!r} cannot be purged: live versions {active_versions}, "
            f"referenced by {referenced_by or []}"
        )
        self.name = name
        self.active_versions = active_versions
        self.referenced_by = referenced_by or []


def subagent_names(spec_json: Mapping[str, Any] | None) -> set[str]:
    """Names this stored manifest delegates to (``spec.subagents[].agent_ref``).

    Reads the persisted JSON as-is (no model validation) so a malformed sibling
    row can never make a purge check crash; an unparsable ref is ignored."""
    if not spec_json:
        return set()
    body = spec_json.get("spec")
    subs = body.get("subagents") if isinstance(body, Mapping) else None
    names: set[str] = set()
    for sub in subs if isinstance(subs, list) else []:
        ref = sub.get("agent_ref") if isinstance(sub, Mapping) else None
        if isinstance(ref, str) and ref.count("@") == 1:
            names.add(ref.split("@", 1)[0])
    return names


class AgentSpecStore(abc.ABC):
    """Per-tenant manifest registry.

    Every method takes ``tenant_id`` explicitly. Reads / writes filtered
    on a different tenant return ``None`` / ``False`` rather than the
    record, so cross-tenant existence never leaks.
    """

    @abc.abstractmethod
    async def create(
        self,
        *,
        tenant_id: UUID,
        spec: AgentSpec,
        spec_sha256: str,
        created_by: str,
    ) -> AgentSpecRecord:
        """Insert a new ``ACTIVE`` row; raises :class:`DuplicateAgentSpecError`
        if ``(tenant_id, name, version)`` already exists, and its subclass
        :class:`AgentSpecPurgingError` if any version of the name is being
        purged (checked under the name's row locks, B-278)."""

    @abc.abstractmethod
    async def get(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        include_deleted: bool = False,
    ) -> AgentSpecRecord | None:
        """Fetch one row; defaults skip soft-deleted entries (``deleted`` and,
        B-278, ``purging``)."""

    @abc.abstractmethod
    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        """Paginated list, newest first. B-278 —— ``status=DELETED`` also
        matches ``PURGING`` rows (the console's "deleted" filter shows a name
        whose purge has not finished)."""

    @abc.abstractmethod
    async def list_distinct_active_by_tenant(
        self, *, tenant_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentSpecRecord]:
        """每个 name 一条(最新的 ACTIVE 版本),按 name 排序,分页。

        阶段 3 (3.1) —— ``GET /v1/agent-catalog`` 列的是 **agent**,而
        :meth:`list_by_tenant` 分页的是**版本行**。一个 name 有多个 ACTIVE
        版本行是常态(发新版本只 create 一行,没有代码把旧版本降级),所以
        「先分页、再按 name 去重」会让去重后的页短于 ``limit`` —— 客户端按
        「不足一页即最后一页」的标准循环会**静默漏掉**后面的 agent,而响应里
        没有 ``total``,它无从察觉。

        每个 name 保留 ``created_at`` 最新的那一行——绝大多数情况下和
        ``agents.py:_resolve_session`` 用 ``limit=1``(即 ``list_by_tenant``,
        只 ``ORDER BY created_at DESC``,**没有** ``id`` 次级键)取到的是同一
        行。但 ``created_at`` 完全相同时,本方法用 ``id DESC`` 定序而
        ``_resolve_session`` 未定序,两者选出的行**可能不是同一个**——目录
        显示的 ``display_name`` / ``description`` 会跟实际被 run 端点执行的
        版本对不上。这是既有行为,不在本方法要解决的问题范围内。

        排序键是 ``name``(不是 ``created_at``):分页要求全序稳定,而
        ``created_at`` 在去重后不再唯一。同一 name 的 ``created_at`` 撞车时
        用 ``id`` 降序做次级 tie-break——两个后端必须用同一个 tie-break 键,
        否则同一批数据在两边可能选出不同的"最新"行。
        """

    @abc.abstractmethod
    async def count_distinct_active_by_tenant(self, *, tenant_id: UUID) -> int:
        """去重后(按 name)这个租户有多少个 ACTIVE agent——不分页。

        配 :meth:`list_distinct_active_by_tenant` 使用,让 ``GET
        /v1/agent-catalog`` 能在响应里给出 ``total``,客户端才有办法判断
        "还有没有下一页",而不是只能靠"这页数量 < limit"去猜——那个启发式
        在最后一页恰好凑满 limit 时会给出错误的"还有更多"信号。
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        """Cross-tenant list — Stream N (Mini-ADR N-4).

        Caller MUST be inside a ``bypass_rls_session()`` (or ``applied_scope``
        with a :class:`CrossTenant` resolution); otherwise RLS filters
        all rows out. Used only by ``system_admin`` requests with
        ``tenant_id=*``.
        """

    @abc.abstractmethod
    async def update_spec(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        spec: AgentSpec,
        spec_sha256: str,
        updated_by: str,
    ) -> AgentSpecUpdateResult | None:
        """Replace the spec payload + sha256 in place, appending one
        ``agent_spec_revision`` history row in the same transaction
        (Stream HX-5; a same-sha update is a recorded no-op — no new
        revision). Returns the result, or ``None`` if no row matched
        (404)."""

    # --- 未发布草稿:把「改配置」和「让改动生效」分成两个动作 -----------

    @abc.abstractmethod
    async def save_draft(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        spec: AgentSpec,
        spec_sha256: str,
        updated_by: str,
    ) -> AgentSpecRecord | None:
        """Write (or overwrite) the row's unpublished draft.

        Touches only the four draft columns — the live ``spec`` and its
        revision history are untouched, so no run is affected. Returns the
        refreshed record, or ``None`` if no live row matched (unknown /
        cross-tenant / soft-deleted).
        """

    @abc.abstractmethod
    async def discard_draft(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
    ) -> AgentSpecRecord | None:
        """Clear the row's draft. Returns the refreshed record, or ``None``
        if no live row matched. Discarding when there is no draft is a
        no-op, not an error — the caller's intent ("leave me with no
        draft") is already satisfied."""

    @abc.abstractmethod
    async def publish_draft(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        updated_by: str,
    ) -> AgentSpecUpdateResult | None:
        """Promote the draft to live and clear it, in one transaction.

        Same write semantics as :meth:`update_spec` (appends a revision;
        a same-sha promotion records nothing) plus the draft columns are
        cleared. Returns ``None`` when there is no live row **or no draft**
        — "publish what?" is a caller error, not a silent success.
        """

    @abc.abstractmethod
    async def list_revisions(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentSpecRevisionRecord]:
        """Revision history for one manifest, newest first — Stream HX-5."""

    @abc.abstractmethod
    async def get_revision(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        revision: int,
    ) -> AgentSpecRevisionRecord | None:
        """One revision snapshot, or ``None`` (unknown / cross-tenant)."""

    @abc.abstractmethod
    async def update_status(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        status: AgentSpecStatus,
    ) -> AgentSpecRecord | None:
        """Update status. Returns the updated record, or ``None`` if no
        row matched. Used by the soft-delete path and the deprecate UI.
        B-278 —— a ``deleted`` / ``purging`` row is never changed here (a stale
        delete must not flip ``purging`` back to ``deleted``): ``None``."""

    # --- B-278 恢复 / 彻底删除 ---------------------------------------------

    @abc.abstractmethod
    async def restore(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
    ) -> AgentSpecRecord | None:
        """Flip one soft-deleted version back to ``ACTIVE``.

        Conditional: only a row whose status is ``DELETED`` changes. Returns
        the restored record, or ``None`` when no deleted row matched (unknown,
        cross-tenant, or not deleted — the caller tells those apart with
        :meth:`get` ``include_deleted=True``)."""

    @abc.abstractmethod
    async def count_revisions_for_name(self, *, tenant_id: UUID, name: str) -> int:
        """How many ``agent_spec_revision`` rows every version of ``name`` has."""

    @abc.abstractmethod
    async def begin_purge(self, *, tenant_id: UUID, name: str) -> list[str]:
        """Mark every version of ``name`` ``PURGING`` — the persistent "a purge
        is under way" marker. Only if every version is ``DELETED`` or already
        ``PURGING`` (a re-run); otherwise nothing changes and
        :class:`AgentSpecPurgeBlockedError` is raised. One transaction, rows
        locked, re-checked after the update so a version created concurrently
        cannot slip past. While marked, :meth:`restore` changes nothing and
        :meth:`create` raises :class:`AgentSpecPurgingError`. Returns the
        versions marked (``[]`` when the name has no rows)."""

    @abc.abstractmethod
    async def purge_name(self, *, tenant_id: UUID, name: str) -> tuple[int, int]:
        """Hard-delete every version row of ``name`` and their revisions.

        One transaction: the version rows are locked; if any of them is live,
        or a live agent of the tenant lists ``name`` as a subagent, nothing is
        deleted and :class:`AgentSpecPurgeBlockedError` is raised. Only the
        locked versions' revisions go. Returns ``(version rows, revision rows)``
        removed — ``(0, 0)`` when the name is already gone (a re-run).
        Revisions go too because their ``(tenant, agent_name, agent_version,
        revision)`` unique key would otherwise make re-creating the name fail."""
