"""Abstract ``SkillStore`` repository — Stream J.7a (Mini-ADR J-23).

Two-table model:

* ``skill`` — named bundle with lifecycle state + latest_version pointer.
* ``skill_version`` — append-only version rows carrying prompt / tools.

Implementations:

* :class:`expert_work.persistence.skill.memory.InMemorySkillStore`
* :class:`expert_work.persistence.skill.sql.SqlSkillStore`

Tenant scoping is enforced by SQL RLS (migration 0029) + by the
``tenant_id`` argument every call carries. Cross-tenant probes return
``None`` rather than raise (same hides-cross-tenant rule as
ThreadMetaStore / ImageUploadStore).
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from expert_work.protocol import (
    DEFAULT_SKILL_LAZY_LOAD,
    ComponentType,
    EvolutionOrigin,
    KillSwitch,
    KillSwitchScope,
    PromoteRequestStatus,
    Skill,
    SkillEvalResult,
    SkillPredictionVerdict,
    SkillPromoteRequest,
    SkillRunUsage,
    SkillStatus,
    SkillVersion,
    SkillVisibility,
)
from expert_work.protocol.skill import supporting_files_to_jsonable
from expert_work.protocol.tenant_config import TenantPlan


class DuplicateSkillError(Exception):
    """``(tenant_id, name)`` already exists — admin POST collision.

    ``tenant_id`` is ``None`` for a platform-skill (NULL-tenant) collision
    (Stream X, Mini-ADR X-1).
    """

    def __init__(self, *, tenant_id: UUID | None, name: str) -> None:
        scope = f"tenant {tenant_id}" if tenant_id is not None else "the platform library"
        super().__init__(f"skill {name!r} already exists for {scope}")
        self.tenant_id = tenant_id
        self.name = name


class SkillNotFoundError(KeyError):
    """Skill id is unknown for this tenant — admin GET/PATCH 404 path."""


class SkillVersionNotFoundError(KeyError):
    """``(skill_id, version)`` row is absent — pin / rollback failure path."""


class PromoteRequestNotFoundError(KeyError):
    """Promote-request id is unknown for this tenant — approve/reject 404 path."""


class DuplicatePromoteRequestError(Exception):
    """A ``pending`` promote request already exists for this skill (SE-8).

    The ``uq_skill_promote_request_pending`` partial unique index keeps at
    most one open request per skill; a second ``request_skill_promote`` while
    one is still pending raises this.
    """

    def __init__(self, *, skill_id: UUID) -> None:
        super().__init__(f"a pending promote request already exists for skill {skill_id}")
        self.skill_id = skill_id


class SkillStore(abc.ABC):
    """Per-tenant skill library + version log.

    Three groups of operations:

    1. **Lifecycle** — create / list / get / patch-status / archive
       skill rows.
    2. **Version log** — append a new version + read versions by id or
       by ``(skill_id, version)``.
    3. **Build-time resolution** — bare-name lookup
       (:meth:`resolve_by_name`) and pinned lookup
       (:meth:`resolve_pinned`) used by the orchestrator's skill loader.
    """

    # ------------------------------------------------------------ skill (lifecycle)

    @abc.abstractmethod
    async def create_skill(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        name: str,
        description: str = "",
        category: str | None = None,
        required_tier: TenantPlan = TenantPlan.FREE,
        # Stream SE (Mini-ADR SE-A1 / SE-A3). Defaults preserve the M0
        # human-authored shape: tenant-visible, no agent provenance.
        visibility: SkillVisibility = "tenant",
        created_by_user_id: UUID | None = None,
        created_by_agent_name: str | None = None,
        forked_from: UUID | None = None,
        # Stream SE — SE-10 (Mini-ADR SE-A15). Default ``skill`` preserves
        # every historical caller; the three text-class components pass their
        # ``component_type`` (and ``target_tool_name`` for ``tool_description``).
        component_type: ComponentType = "skill",
        target_tool_name: str | None = None,
    ) -> Skill:
        """Insert a new skill row (status=draft, latest_version=0).

        Raises :class:`DuplicateSkillError` when ``(tenant_id, name)``
        already exists. ``required_tier`` defaults to ``FREE`` — only
        meaningful for platform skills (a tenant's own skills are always
        usable), but accepted on the tenant path for symmetry (Mini-ADR X-2).

        ``visibility`` / ``created_by_user_id`` / ``created_by_agent_name`` /
        ``forked_from`` (Stream SE) default to the human-authored shape;
        agent self-authored skills pass ``visibility='agent_private'`` + the
        owning per-user agent (``created_by_user_id`` + ``created_by_agent_name``).
        """

    @abc.abstractmethod
    async def get_skill(self, *, skill_id: UUID, tenant_id: UUID) -> Skill | None:
        """Return the skill row by id or ``None`` (cross-tenant probes hide)."""

    @abc.abstractmethod
    async def get_skill_by_name(self, *, tenant_id: UUID, name: str) -> Skill | None:
        """Return the skill row by ``(tenant_id, name)`` or ``None``."""

    @abc.abstractmethod
    async def shadowed_skill_names(self, *, tenant_id: UUID, names: Sequence[str]) -> set[str]:
        """Return the subset of ``names`` the tenant already owns a skill for
        (any status) — the merged-view name-shadow set. One round-trip,
        replacing a per-name ``get_skill_by_name`` loop over the platform
        library (which is an N+1)."""

    @abc.abstractmethod
    async def list_skills(
        self,
        *,
        tenant_id: UUID,
        status: SkillStatus | None = None,
        category: str | None = None,
        visibility: SkillVisibility | None = None,
        created_by_user_id: UUID | None = None,
        created_by_agent_name: str | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Skill], UUID | None]:
        """Page through tenant skills.

        Returns ``(rows, next_cursor)``. ``next_cursor`` is ``None``
        when the last page was returned. Cursor is the last row's
        ``id`` for keyset pagination (sorted by ``created_at DESC, id``).

        Stream SE (SE-8): ``visibility`` / ``created_by_user_id`` filter the
        agent-self-authored slice (e.g. "this user's agent_private skills")
        for the admin governance surface. Both default ``None`` (no filter),
        preserving the M0 list shape. ``created_by_agent_name`` narrows to
        skills authored by that agent (Stream H.6 Mini-ADR H-11 — feeds
        the per-agent Skills tab).
        """

    @abc.abstractmethod
    async def list_skills_all_tenants(
        self,
        *,
        status: SkillStatus | None = None,
        category: str | None = None,
        created_by_agent_name: str | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Skill], UUID | None]:
        """Cross-tenant page through skills — Stream N (Mini-ADR N-4).

        Identical shape to :meth:`list_skills` minus the ``tenant_id``
        filter. Caller MUST be inside ``bypass_rls_session()``
        (or :func:`applied_scope` with a :class:`CrossTenant`).
        """

    @abc.abstractmethod
    async def set_status(self, *, skill_id: UUID, tenant_id: UUID, status: SkillStatus) -> Skill:
        """Move the lifecycle state forward (or back to archived).

        Raises :class:`SkillNotFoundError` when the id is unknown for
        this tenant. Implementations also bump ``updated_at``.
        """

    # ------------------------------------------------------------ skill_version

    @abc.abstractmethod
    async def add_version(
        self,
        *,
        version_id: UUID,
        skill_id: UUID,
        tenant_id: UUID,
        prompt_fragment: str,
        tool_names: Sequence[str] = (),
        description: str = "",
        category: str | None = None,
        required_models: Sequence[str] = (),
        authored_by: str = "human",
        # Capability Uplift Sprint #3 (Mini-ADR U-16 / U-15 / U-21 / U-24).
        # Default values keep Stream J.7a callers (M0 JSON-API path) working
        # without touching them; the ZIP / supporting-files paths populate
        # all four explicitly.
        supporting_files: dict[str, dict[str, Any]] | None = None,
        lazy_load: bool = DEFAULT_SKILL_LAZY_LOAD,
        content_hash: bytes = b"",
        high_risk: bool = False,
        # Stream SE (Mini-ADR SE-A1). Evolution provenance — default None/0
        # keeps human-authored rows unchanged; Layer A/B paths populate them.
        evolution_origin: EvolutionOrigin | None = None,
        distilled_from_trajectory_key: str | None = None,
        distilled_from_candidate_id: UUID | None = None,
        evolution_round: int = 0,
    ) -> SkillVersion:
        """Append the next version to a skill.

        ``version`` is auto-assigned = ``skill.latest_version + 1``.
        Updates ``skill.latest_version`` + mirrors ``description`` /
        ``category`` onto the parent skill row. Raises
        :class:`SkillNotFoundError` if the skill is unknown.

        ``supporting_files`` / ``lazy_load`` / ``content_hash`` / ``high_risk``
        default to the pre-Sprint #3 shape so the Stream J.7a JSON-API
        path doesn't need to compute them. Callers that DO compute them
        (ZIP import + supporting-files single-file mutation API) must
        pass all four to avoid leaving a row with an empty
        ``content_hash`` (which would fire a spurious drift alert on the
        first ``skill_view``).
        """

    @abc.abstractmethod
    async def get_version(self, *, version_id: UUID, tenant_id: UUID) -> SkillVersion | None:
        """Return version row by id, or ``None`` (cross-tenant hides)."""

    @abc.abstractmethod
    async def get_version_by_number(
        self, *, skill_id: UUID, tenant_id: UUID, version: int
    ) -> SkillVersion | None:
        """Return version row by ``(skill_id, version)`` — pinned ref path."""

    @abc.abstractmethod
    async def list_versions(self, *, skill_id: UUID, tenant_id: UUID) -> list[SkillVersion]:
        """All versions of a skill, ordered ``version DESC``."""

    # ------------------------------------------------------------ resolve (loader)

    @abc.abstractmethod
    async def resolve_by_name(self, *, tenant_id: UUID, name: str) -> SkillVersion | None:
        """Bare-name resolution — current ``latest_version`` of an
        ``ACTIVE`` skill. Returns ``None`` when:

        * The name is unknown for this tenant
        * The skill exists but is in ``DRAFT`` or ``ARCHIVED`` state
        * The skill has no versions (latest_version=0)

        The orchestrator's skill loader treats ``None`` as
        ``SkillNotFoundError`` at the build-time boundary; the helper
        returns ``None`` so cross-tenant probes don't raise.
        """

    @abc.abstractmethod
    async def resolve_pinned(
        self, *, tenant_id: UUID, name: str, version: int
    ) -> SkillVersion | None:
        """Pinned ``name@version`` resolution.

        Returns the matching ``skill_version`` row regardless of the
        parent skill's lifecycle state (draft / active / archived all
        allowed — pinning is the reproducibility escape hatch). Returns
        ``None`` when either the name or the version row is absent.
        """

    # -------------------------------------------------- evolution (Stream SE)

    async def fork_skill(
        self,
        *,
        tenant_id: UUID,
        source_skill_id: UUID,
        new_name: str,
        by_user_id: UUID,
        by_agent_name: str,
        new_skill_id: UUID,
        new_version_id: UUID,
    ) -> Skill:
        """Fork a same-tenant source skill into a new agent-private skill.

        Concrete composition over the abstract primitives (Mini-ADR SE-A3,
        §15.7 "fork is the reuse path"): copy the source skill's *latest*
        version content into a brand-new ``agent_private`` skill (v1) owned
        by the per-user agent (``by_user_id`` + ``by_agent_name``) with
        ``forked_from = source_skill_id``. The new skill starts in ``DRAFT``
        like any freshly authored skill.

        Raises :class:`SkillNotFoundError` if the source skill is unknown
        for this tenant, :class:`SkillVersionNotFoundError` if it has no
        published version yet (``latest_version == 0``).

        Not atomic across the two writes (create + add_version) on the SQL
        backend — a crash between them leaves an empty DRAFT skill, which is
        harmless (no bare-name resolution until ACTIVE) and admin-cleanable.
        """
        source = await self.get_skill(skill_id=source_skill_id, tenant_id=tenant_id)
        if source is None:
            raise SkillNotFoundError(str(source_skill_id))
        src_version = await self.get_version_by_number(
            skill_id=source_skill_id, tenant_id=tenant_id, version=source.latest_version
        )
        if src_version is None:
            raise SkillVersionNotFoundError(f"{source_skill_id}@{source.latest_version}")
        await self.create_skill(
            skill_id=new_skill_id,
            tenant_id=tenant_id,
            name=new_name,
            description=src_version.description,
            category=src_version.category,
            visibility="agent_private",
            created_by_user_id=by_user_id,
            created_by_agent_name=by_agent_name,
            forked_from=source_skill_id,
        )
        await self.add_version(
            version_id=new_version_id,
            skill_id=new_skill_id,
            tenant_id=tenant_id,
            prompt_fragment=src_version.prompt_fragment,
            tool_names=src_version.tool_names,
            description=src_version.description,
            category=src_version.category,
            required_models=src_version.required_models,
            authored_by="agent",
            supporting_files=supporting_files_to_jsonable(src_version.supporting_files),
            lazy_load=src_version.lazy_load,
            content_hash=src_version.content_hash,
            high_risk=src_version.high_risk,
            evolution_origin="in_session",
        )
        forked = await self.get_skill(skill_id=new_skill_id, tenant_id=tenant_id)
        if forked is None:  # pragma: no cover — just-created row must exist
            raise SkillNotFoundError(str(new_skill_id))
        return forked

    @abc.abstractmethod
    async def record_eval_result(self, *, result: SkillEvalResult) -> SkillEvalResult:
        """Persist one replay-verification result (Mini-ADR SE-A2).

        ``result.tenant_id is None`` = a platform-skill evaluation (caller
        MUST be inside ``bypass_rls_session()``); otherwise the row is
        tenant-scoped under the standard RLS GUC.
        """

    @abc.abstractmethod
    async def list_eval_results(
        self, *, skill_id: UUID, tenant_id: UUID | None
    ) -> list[SkillEvalResult]:
        """All eval results for a skill, newest first. ``tenant_id=None``
        for a platform skill (caller inside ``bypass_rls_session()``)."""

    # ------------------------------------- run-usage attribution (SE-7d-1)

    @abc.abstractmethod
    async def record_skill_run_usage(self, *, usage: SkillRunUsage) -> SkillRunUsage:
        """Append one ``skill_run_usage`` row (Mini-ADR SE-A11, SE-7d-1).

        Best-effort attribution: a run (``usage.thread_id``) loaded
        ``usage.skill_version`` and ended with ``usage.outcome``. The
        rollback monitor (SE-7d-3) aggregates these per version.
        ``usage.tenant_id is None`` = platform skill (caller inside
        ``bypass_rls_session()``); otherwise tenant-scoped under the RLS GUC.
        """

    @abc.abstractmethod
    async def skill_run_usage_window(
        self,
        *,
        skill_id: UUID,
        skill_version: int,
        tenant_id: UUID | None,
        since: datetime,
    ) -> list[SkillRunUsage]:
        """Usage rows of runs that used ``(skill_id, skill_version)`` with
        ``created_at >= since`` — the rolling-window sample the regression-
        rollback judge (SE-7d-2) scores. Filtered **per version** so a
        rollback never连坐 the next (possibly human-fixed) version.
        ``tenant_id=None`` for a platform skill (caller inside
        ``bypass_rls_session()``).

        Returns full rows (not bare outcomes) so the rollback gate can
        join user feedback by ``thread_id`` (Stream HX-2, Mini-ADR
        HX-B2) before scoring."""

    # ----------------------------------- prediction-falsify ledger (SE-11)

    @abc.abstractmethod
    async def record_prediction_verdict(
        self, *, verdict: SkillPredictionVerdict
    ) -> SkillPredictionVerdict:
        """Append one ``skill_prediction_verdict`` row (Mini-ADR SE-A18/A19).

        Diagnostic record of how much of a promoted version's replay-predicted
        gain held in production. Written by the rollback monitor's sweep, in the
        same pass as ``decide_rollback`` (叠加不替代 — never gates archive).
        ``verdict.tenant_id is None`` = platform skill (caller inside
        ``bypass_rls_session()``)."""

    @abc.abstractmethod
    async def list_prediction_verdicts(
        self, *, skill_id: UUID, tenant_id: UUID | None
    ) -> list[SkillPredictionVerdict]:
        """All prediction verdicts for a skill, newest first. ``tenant_id=None``
        for a platform skill (caller inside ``bypass_rls_session()``)."""

    # ----------------------------------- promote approval flow (SE-8, SE-A13b)

    @abc.abstractmethod
    async def request_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        skill_id: UUID,
        skill_version: int,
        requested_by_user_id: UUID | None = None,
        requested_by_agent_name: str | None = None,
        reason: str = "",
    ) -> SkillPromoteRequest:
        """Open a ``pending`` agent_private→tenant promote request (SE-A13b).

        Raises :class:`SkillNotFoundError` if the skill is unknown for this
        tenant, :class:`DuplicatePromoteRequestError` if one is already pending
        for the skill (``uq_skill_promote_request_pending``). Orthogonal to
        ``skill.status`` — this only governs ``visibility``.
        """

    @abc.abstractmethod
    async def approve_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        decided_by_user_id: UUID,
        decision_reason: str = "",
    ) -> SkillPromoteRequest:
        """Approve a pending request → flip the skill's ``visibility`` to
        ``tenant`` (atomic) and mark the request ``approved``.

        Raises :class:`PromoteRequestNotFoundError` if the request id is
        unknown for this tenant, :class:`ValueError` if it is not ``pending``.
        """

    @abc.abstractmethod
    async def reject_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        decided_by_user_id: UUID,
        decision_reason: str = "",
    ) -> SkillPromoteRequest:
        """Reject a pending request — mark ``rejected``; skill ``visibility``
        stays ``agent_private``. Same error semantics as
        :meth:`approve_skill_promote`.
        """

    @abc.abstractmethod
    async def get_promote_request(
        self, *, request_id: UUID, tenant_id: UUID
    ) -> SkillPromoteRequest | None:
        """Return the promote request by id, or ``None`` (cross-tenant hides)."""

    @abc.abstractmethod
    async def list_promote_requests(
        self,
        *,
        tenant_id: UUID,
        status: PromoteRequestStatus | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
        # Backlog task 8 (SE-8) — mirrors ``list_skills``'s ``visibility``
        # filter: narrows to requests targeting a skill with this visibility.
        # ``None`` (default) = no filter, preserving the admin/system shape.
        # Filtered at the query layer (before the cursor page is cut), same
        # rule as ``list_skills``, so a filtered page never comes back short.
        skill_visibility: SkillVisibility | None = None,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        """Page through a tenant's promote requests (newest first), the SE-8
        review queue when ``status='pending'``. Keyset cursor = last row id.

        ``skill_visibility`` filters by the *target skill's* visibility (a
        join, not a column on this row) — e.g. ``"tenant"`` to hide requests
        aimed at ``agent_private`` skills from non-admin callers.
        """

    @abc.abstractmethod
    async def list_promote_requests_all_tenants(
        self,
        *,
        status: PromoteRequestStatus | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        """Cross-tenant review queue (Stream N shape). Same as
        :meth:`list_promote_requests` minus the tenant filter; caller MUST be
        inside ``bypass_rls_session()`` (system_admin cross-tenant read)."""

    # -------------------------------------- evolution kill-switch (SE-8, SE-A13c)

    @abc.abstractmethod
    async def get_kill_switch(
        self, *, scope: KillSwitchScope, tenant_id: UUID | None
    ) -> KillSwitch | None:
        """Return the kill-switch row for ``(scope, tenant_id)`` or ``None``.

        ``scope='global'`` → ``tenant_id`` MUST be ``None`` (caller inside
        ``bypass_rls_session()`` — the global row is a NULL-tenant platform row).
        """

    @abc.abstractmethod
    async def set_kill_switch(
        self,
        *,
        switch_id: UUID,
        scope: KillSwitchScope,
        tenant_id: UUID | None,
        engaged: bool,
        reason: str = "",
        actor_user_id: UUID | None = None,
    ) -> KillSwitch:
        """Upsert the kill-switch for ``(scope, tenant_id)`` (Mini-ADR SE-A13c).

        ``engaged=True`` engages the emergency stop (sets ``engaged_by/at``);
        ``False`` releases it (sets ``released_by/at``). ``switch_id`` is used
        only when inserting the first row for this scope. ``scope='global'``
        requires ``tenant_id is None`` and a ``bypass_rls_session()`` caller.
        """

    @abc.abstractmethod
    async def is_evolution_halted(self, *, tenant_id: UUID) -> bool:
        """``True`` if the global OR this tenant's kill-switch is engaged.

        The SE-7 promote gate reads this as the ``evolution_halted`` input to
        :func:`decide_promotion`. Called by the evolution worker (cross-tenant
        owner / ``bypass_rls_session()``) so it can see the global NULL-tenant
        row alongside the tenant row.
        """

    @abc.abstractmethod
    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — null a user's actor columns, KEEP the rows.

        A skill is a tenant asset, not per-user PII — the row is KEPT and only
        the 5 actor columns that named the purged user are nulled:

        * ``skill.created_by_user_id``
        * ``skill_promote_request.requested_by_user_id`` / ``decided_by_user_id``
        * ``skill_evolution_kill_switch.engaged_by_user_id`` / ``released_by_user_id``

        Returns the count of rows updated (summed across the 3 tables; a row
        matched on two of its own actor columns counts once). ONLY rows whose
        ``tenant_id`` equals the purged user's tenant are touched — platform
        (NULL-tenant ``skill``) and global (NULL-tenant kill-switch) rows are
        NEVER matched by the concrete ``tenant_id`` predicate, so a
        cross-tenant / platform actor column is left intact (correct isolation:
        we cannot prove those belong to this tenant's user)."""

    # -------------------------------------------------- platform (Stream X)
    #
    # Platform skills are NULL-tenant rows in the SAME ``skill`` /
    # ``skill_version`` tables (Mini-ADR X-1). They are addressed by these
    # explicit ``*_platform_*`` methods (which filter ``tenant_id IS NULL``)
    # rather than by passing ``tenant_id=None`` to the tenant methods — the
    # latter already means "all tenants" (cross-tenant system_admin reads).
    #
    # Every platform method MUST be invoked inside ``bypass_rls_session()``
    # (an UNSCOPED session): under the 0057 ``IS NOT DISTINCT FROM`` policy,
    # an unset ``app.tenant_id`` makes ``tenant_id IS NULL`` rows visible,
    # while any tenant-scoped session sees ZERO platform rows (the X-8 / W-8
    # isolation property). Wiring the bypass is the caller's job (X3 / X4).

    @abc.abstractmethod
    async def create_platform_skill(
        self,
        *,
        skill_id: UUID,
        name: str,
        description: str = "",
        category: str | None = None,
        required_tier: TenantPlan = TenantPlan.FREE,
    ) -> Skill:
        """Insert a platform (NULL-tenant) skill row.

        Records carry ``tenant_id=None``. Raises :class:`DuplicateSkillError`
        when a platform skill with ``name`` already exists. Caller MUST be
        inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def get_platform_skill(self, *, skill_id: UUID) -> Skill | None:
        """Return the platform skill by id (``tenant_id IS NULL``) or ``None``.

        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def get_platform_skill_by_name(self, *, name: str) -> Skill | None:
        """Return the platform skill by ``name`` or ``None``.

        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def list_platform_skills(
        self,
        *,
        status: SkillStatus | None = None,
        category: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Skill], int]:
        """Offset-page through platform (NULL-tenant) skills.

        Returns ``(page_items, total_matching)`` — ``total`` counts all rows
        matching the filters (drives the admin table's page count); the page is
        ``ORDER BY created_at DESC, id`` sliced by ``offset``/``limit``. ``q`` is
        a case-insensitive substring over ``name`` + ``description``; ``status``
        / ``category`` AND with it. Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def bulk_update_platform_skills(
        self,
        *,
        ids: Sequence[UUID] | None = None,
        filter_status: SkillStatus | None = None,
        filter_category: str | None = None,
        filter_q: str | None = None,
        set_status: SkillStatus | None = None,
        set_pinned: bool | None = None,
        update_category: bool = False,
        new_category: str | None = None,
    ) -> int:
        """Atomically patch many platform skills; returns the affected count.

        Exactly one selector: ``ids`` (an explicit list) OR the
        ``filter_*`` predicate (every NULL-tenant skill matching it — the
        "select all N matching" path). At least one action of ``set_status`` /
        ``set_pinned`` / ``update_category`` must be given. ``state_changed_at``
        is bumped only when ``set_status`` is provided. When ``update_category``
        is True the category column is written to ``new_category`` (``None``
        clears it); only the parent ``skill`` row is touched, never
        ``skill_version``. Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def add_platform_version(
        self,
        *,
        version_id: UUID,
        skill_id: UUID,
        prompt_fragment: str,
        tool_names: Sequence[str] = (),
        description: str = "",
        category: str | None = None,
        required_models: Sequence[str] = (),
        authored_by: str = "human",
        supporting_files: dict[str, dict[str, Any]] | None = None,
        lazy_load: bool = DEFAULT_SKILL_LAZY_LOAD,
        content_hash: bytes = b"",
        high_risk: bool = False,
    ) -> SkillVersion:
        """Append a version to a platform skill (``tenant_id=None``).

        Same semantics as :meth:`add_version`. Raises
        :class:`SkillNotFoundError` if the platform skill is unknown.
        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def get_platform_version(self, *, version_id: UUID) -> SkillVersion | None:
        """Return a platform version row by id, or ``None``.

        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def get_platform_version_by_number(
        self, *, skill_id: UUID, version: int
    ) -> SkillVersion | None:
        """Return a platform version by ``(skill_id, version)`` — pinned path.

        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def list_platform_versions(self, *, skill_id: UUID) -> list[SkillVersion]:
        """All versions of a platform skill, ordered ``version DESC``.

        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def set_platform_status(self, *, skill_id: UUID, status: SkillStatus) -> Skill:
        """Move a platform skill's lifecycle state. Raises
        :class:`SkillNotFoundError` if unknown. Caller MUST be inside
        ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def set_platform_pinned(self, *, skill_id: UUID, pinned: bool) -> Skill:
        """Admin pin / unpin a platform skill. Raises
        :class:`SkillNotFoundError` if unknown. Caller MUST be inside
        ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def set_platform_category(self, *, skill_id: UUID, category: str | None) -> Skill:
        """Set (or clear, with ``None``) a platform skill's category. Raises
        :class:`SkillNotFoundError` if unknown. Caller MUST be inside
        ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def list_platform_categories(self) -> list[str]:
        """Distinct non-empty categories across NULL-tenant skills, sorted.
        Feeds the admin list's category filter. Caller MUST be inside
        ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def resolve_platform_by_name(self, *, name: str) -> SkillVersion | None:
        """Bare-name resolution of a platform skill — current
        ``latest_version`` of an ``ACTIVE`` platform skill, else ``None``.
        Caller MUST be inside ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def resolve_platform_pinned(self, *, name: str, version: int) -> SkillVersion | None:
        """Pinned ``name@version`` resolution of a platform skill (any
        lifecycle state). Returns ``None`` when name or version is absent.
        Caller MUST be inside ``bypass_rls_session()``.
        """

    # -------------------------------------------------- Curator (Sprint #4)

    @abc.abstractmethod
    async def bump_last_used_at(self, *, skill_id: UUID, tenant_id: UUID) -> tuple[bool, bool]:
        """Mark a skill as just-used by an agent build or skill_view call.

        Atomic semantics (Mini-ADR U-27 / U-29):
        * ``active`` → ``last_used_at = now()``, status unchanged
        * ``stale`` → ``last_used_at = now()`` AND status → ``active``
          (auto-revive) AND ``state_changed_at = now()``
        * ``archived`` → no-op (cold storage; admin must unarchive)
        * ``draft`` → no-op (draft skills don't have an "active" lifecycle
          to lapse out of)
        * pinned → ``last_used_at = now()`` regardless, status unchanged

        Returns ``(updated, auto_revived)``:
        * ``updated`` — ``True`` iff at least one row was changed
        * ``auto_revived`` — ``True`` iff the row transitioned
          ``stale → active`` as part of this call

        Caller is responsible for application-layer throttling
        (:class:`control_plane.skill_activity.ThrottledActivityRecorder`);
        the store itself always executes the SQL when invoked.
        """

    @abc.abstractmethod
    async def curator_promote_active_to_stale(self, *, tenant_id: UUID, stale_days: int) -> int:
        """Batch-transition: ``active`` skills with last_used_at older than
        ``stale_days`` (and not pinned) → ``stale``.

        Returns the number of rows transitioned.
        """

    @abc.abstractmethod
    async def curator_promote_stale_to_archived(self, *, tenant_id: UUID, archive_days: int) -> int:
        """Batch-transition: ``stale`` skills with last_used_at older than
        ``archive_days`` (and not pinned) → ``archived``.

        Returns the number of rows transitioned.
        """

    @abc.abstractmethod
    async def set_pinned(self, *, skill_id: UUID, tenant_id: UUID, pinned: bool) -> Skill:
        """Admin pin / unpin. Raises :class:`SkillNotFoundError` if the
        id is unknown for this tenant. ``updated_at`` advances;
        ``state_changed_at`` does NOT (pin is metadata, not a state
        transition)."""

    @abc.abstractmethod
    async def curator_distinct_tenant_ids(self) -> list[UUID]:
        """Tenants that have at least one ``active`` or ``stale`` skill.

        The Curator's sweep iterates this list (vs every tenant on the
        platform) so a fresh tenant with no skills doesn't burn cycles.
        Caller MUST be inside ``bypass_rls_session()`` — Curator is a
        platform background worker, not a tenant-scoped request.

        Stream X (Mini-ADR X-3): platform (NULL-tenant) skills are EXCLUDED
        (``WHERE tenant_id IS NOT NULL``) — they are shared resources whose
        lifecycle is system_admin-managed, not subject to any one tenant's
        inactivity sweep.
        """

    @abc.abstractmethod
    async def count_pinned(self) -> int:
        """Total pinned skills across all tenants — for the
        ``expert_work_uplift_skill_pinned_total`` gauge. Caller MUST be inside
        ``bypass_rls_session()``."""
