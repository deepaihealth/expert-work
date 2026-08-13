"""In-memory ``SkillStore`` — Stream J.7a (Mini-ADR J-23).

Used by unit tests + the dev default. Semantics mirror
:class:`SqlSkillStore`; concurrency safety relies on the asyncio single-
thread model (one in-flight operation per store instance).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from expert_work.persistence.skill.base import (
    DuplicatePromoteRequestError,
    DuplicateSkillError,
    PromoteRequestNotFoundError,
    SkillNotFoundError,
    SkillStore,
)
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
from expert_work.protocol.tenant_config import TenantPlan


def _paginate_skills(
    rows: list[Skill],
    *,
    status: SkillStatus | None,
    category: str | None,
    cursor: UUID | None,
    limit: int,
    visibility: SkillVisibility | None = None,
    created_by_user_id: UUID | None = None,
    created_by_agent_name: str | None = None,
) -> tuple[list[Skill], UUID | None]:
    """Shared keyset-pagination helper for ``list_skills`` + ``list_skills_all_tenants``."""
    if status is not None:
        rows = [r for r in rows if r.status == status]
    if category is not None:
        rows = [r for r in rows if r.category == category]
    if visibility is not None:
        rows = [r for r in rows if r.visibility == visibility]
    if created_by_user_id is not None:
        rows = [r for r in rows if r.created_by_user_id == created_by_user_id]
    if created_by_agent_name is not None:
        rows = [r for r in rows if r.created_by_agent_name == created_by_agent_name]
    rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    if cursor is not None:
        try:
            cut_idx = next(i for i, r in enumerate(rows) if r.id == cursor)
            rows = rows[cut_idx + 1 :]
        except StopIteration:
            rows = []
    page = rows[: limit + 1]
    if len(page) > limit:
        return page[:limit], page[limit - 1].id
    return page, None


def _paginate_promote_requests(
    rows: list[SkillPromoteRequest],
    *,
    status: PromoteRequestStatus | None,
    cursor: UUID | None,
    limit: int,
) -> tuple[list[SkillPromoteRequest], UUID | None]:
    """Keyset-pagination helper for the SE-8 promote-request review queue."""
    if status is not None:
        rows = [r for r in rows if r.status == status]
    rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    if cursor is not None:
        try:
            cut_idx = next(i for i, r in enumerate(rows) if r.id == cursor)
            rows = rows[cut_idx + 1 :]
        except StopIteration:
            rows = []
    page = rows[: limit + 1]
    if len(page) > limit:
        return page[:limit], page[limit - 1].id
    return page, None


class InMemorySkillStore(SkillStore):
    """Single-process skill registry. Process-local; no concurrency guard."""

    def __init__(self) -> None:
        self._skills: dict[UUID, Skill] = {}
        self._versions: list[SkillVersion] = []
        self._eval_results: list[SkillEvalResult] = []  # Stream SE (SE-A2)
        self._run_usage: list[SkillRunUsage] = []  # Stream SE (SE-A11, SE-7d-1)
        self._prediction_verdicts: list[SkillPredictionVerdict] = []  # Stream SE (SE-11)
        self._promote_requests: dict[UUID, SkillPromoteRequest] = {}  # SE-8 (SE-A13b)
        self._kill_switches: list[KillSwitch] = []  # SE-8 (SE-A13c)

    # ------------------------------------------------------------ skill

    async def create_skill(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        name: str,
        description: str = "",
        category: str | None = None,
        required_tier: TenantPlan = TenantPlan.FREE,
        visibility: SkillVisibility = "tenant",
        created_by_user_id: UUID | None = None,
        created_by_agent_name: str | None = None,
        forked_from: UUID | None = None,
        component_type: ComponentType = "skill",
        target_tool_name: str | None = None,
    ) -> Skill:
        return await self._create_skill_row(
            skill_id=skill_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            category=category,
            required_tier=required_tier,
            visibility=visibility,
            created_by_user_id=created_by_user_id,
            created_by_agent_name=created_by_agent_name,
            forked_from=forked_from,
            component_type=component_type,
            target_tool_name=target_tool_name,
        )

    async def _create_skill_row(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID | None,
        name: str,
        description: str,
        category: str | None,
        required_tier: TenantPlan,
        visibility: SkillVisibility = "tenant",
        created_by_user_id: UUID | None = None,
        created_by_agent_name: str | None = None,
        forked_from: UUID | None = None,
        component_type: ComponentType = "skill",
        target_tool_name: str | None = None,
    ) -> Skill:
        for existing in self._skills.values():
            if existing.tenant_id == tenant_id and existing.name == name:
                raise DuplicateSkillError(tenant_id=tenant_id, name=name)
        now = datetime.now(UTC)
        skill = Skill(
            id=skill_id,
            tenant_id=tenant_id,
            name=name,
            status=SkillStatus.DRAFT,
            latest_version=0,
            description=description,
            category=category,
            required_tier=required_tier,
            visibility=visibility,
            created_by_user_id=created_by_user_id,
            created_by_agent_name=created_by_agent_name,
            forked_from=forked_from,
            component_type=component_type,
            target_tool_name=target_tool_name,
            # Sprint #4 — match the SQL ``server_default=text("now()")``
            # so the in-memory and Postgres backends emit the same
            # DTO shape on first read.
            state_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        self._skills[skill_id] = skill
        return skill

    async def get_skill(self, *, skill_id: UUID, tenant_id: UUID) -> Skill | None:
        row = self._skills.get(skill_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_skill_by_name(self, *, tenant_id: UUID, name: str) -> Skill | None:
        for row in self._skills.values():
            if row.tenant_id == tenant_id and row.name == name:
                return row
        return None

    async def shadowed_skill_names(self, *, tenant_id: UUID, names: Sequence[str]) -> set[str]:
        wanted = set(names)
        return {
            row.name
            for row in self._skills.values()
            if row.tenant_id == tenant_id and row.name in wanted
        }

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
        rows = [r for r in self._skills.values() if r.tenant_id == tenant_id]
        return _paginate_skills(
            rows,
            status=status,
            category=category,
            visibility=visibility,
            created_by_user_id=created_by_user_id,
            created_by_agent_name=created_by_agent_name,
            cursor=cursor,
            limit=limit,
        )

    async def list_skills_all_tenants(
        self,
        *,
        status: SkillStatus | None = None,
        category: str | None = None,
        created_by_agent_name: str | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Skill], UUID | None]:
        # Stream N — no tenant filter.
        rows = list(self._skills.values())
        return _paginate_skills(
            rows,
            status=status,
            category=category,
            created_by_agent_name=created_by_agent_name,
            cursor=cursor,
            limit=limit,
        )

    async def set_status(self, *, skill_id: UUID, tenant_id: UUID, status: SkillStatus) -> Skill:
        row = await self.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        if row is None:
            raise SkillNotFoundError(str(skill_id))
        now = datetime.now(UTC)
        updated = row.model_copy(
            update={"status": status, "updated_at": now, "state_changed_at": now}
        )
        self._skills[skill_id] = updated
        return updated

    # ------------------------------------------------------------ skill_version

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
        supporting_files: dict[str, dict[str, Any]] | None = None,
        lazy_load: bool = DEFAULT_SKILL_LAZY_LOAD,
        content_hash: bytes = b"",
        high_risk: bool = False,
        evolution_origin: EvolutionOrigin | None = None,
        distilled_from_trajectory_key: str | None = None,
        distilled_from_candidate_id: UUID | None = None,
        evolution_round: int = 0,
    ) -> SkillVersion:
        parent = await self.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        return await self._append_version(
            parent=parent,
            version_id=version_id,
            skill_id=skill_id,
            tenant_id=tenant_id,
            prompt_fragment=prompt_fragment,
            tool_names=tool_names,
            description=description,
            category=category,
            required_models=required_models,
            authored_by=authored_by,
            supporting_files=supporting_files,
            lazy_load=lazy_load,
            content_hash=content_hash,
            high_risk=high_risk,
            evolution_origin=evolution_origin,
            distilled_from_trajectory_key=distilled_from_trajectory_key,
            distilled_from_candidate_id=distilled_from_candidate_id,
            evolution_round=evolution_round,
        )

    async def _append_version(
        self,
        *,
        parent: Skill | None,
        version_id: UUID,
        skill_id: UUID,
        tenant_id: UUID | None,
        prompt_fragment: str,
        tool_names: Sequence[str],
        description: str,
        category: str | None,
        required_models: Sequence[str],
        authored_by: str,
        supporting_files: dict[str, dict[str, Any]] | None,
        lazy_load: bool,
        content_hash: bytes,
        high_risk: bool,
        evolution_origin: EvolutionOrigin | None = None,
        distilled_from_trajectory_key: str | None = None,
        distilled_from_candidate_id: UUID | None = None,
        evolution_round: int = 0,
    ) -> SkillVersion:
        if parent is None:
            raise SkillNotFoundError(str(skill_id))
        next_version = parent.latest_version + 1
        now = datetime.now(UTC)
        if authored_by not in {"human", "agent"}:
            msg = f"authored_by must be 'human' or 'agent' (got {authored_by!r})"
            raise ValueError(msg)
        from expert_work.protocol.skill import SkillSupportingFile  # local to avoid cycle

        typed_supporting = {
            path: SkillSupportingFile(**meta) for path, meta in (supporting_files or {}).items()
        }
        version = SkillVersion(
            id=version_id,
            skill_id=skill_id,
            tenant_id=tenant_id,
            version=next_version,
            prompt_fragment=prompt_fragment,
            tool_names=tuple(tool_names),
            description=description or parent.description,
            category=category if category is not None else parent.category,
            required_models=tuple(required_models),
            authored_by=authored_by,  # type: ignore[arg-type]
            supporting_files=typed_supporting,
            lazy_load=lazy_load,
            content_hash=content_hash,
            high_risk=high_risk,
            evolution_origin=evolution_origin,
            distilled_from_trajectory_key=distilled_from_trajectory_key,
            distilled_from_candidate_id=distilled_from_candidate_id,
            evolution_round=evolution_round,
            created_at=now,
        )
        self._versions.append(version)
        # Mirror description / category onto parent + bump latest_version.
        self._skills[skill_id] = parent.model_copy(
            update={
                "latest_version": next_version,
                "description": description or parent.description,
                "category": category if category is not None else parent.category,
                "updated_at": now,
            }
        )
        return version

    async def get_version(self, *, version_id: UUID, tenant_id: UUID) -> SkillVersion | None:
        for v in self._versions:
            if v.id == version_id and v.tenant_id == tenant_id:
                return v
        return None

    async def get_version_by_number(
        self, *, skill_id: UUID, tenant_id: UUID, version: int
    ) -> SkillVersion | None:
        for v in self._versions:
            if v.skill_id == skill_id and v.tenant_id == tenant_id and v.version == version:
                return v
        return None

    async def list_versions(self, *, skill_id: UUID, tenant_id: UUID) -> list[SkillVersion]:
        versions = [
            v for v in self._versions if v.skill_id == skill_id and v.tenant_id == tenant_id
        ]
        versions.sort(key=lambda v: v.version, reverse=True)
        return versions

    # ------------------------------------------------------------ resolve

    async def resolve_by_name(self, *, tenant_id: UUID, name: str) -> SkillVersion | None:
        skill = await self.get_skill_by_name(tenant_id=tenant_id, name=name)
        if skill is None or skill.status != SkillStatus.ACTIVE or skill.latest_version == 0:
            return None
        return await self.get_version_by_number(
            skill_id=skill.id, tenant_id=tenant_id, version=skill.latest_version
        )

    async def resolve_pinned(
        self, *, tenant_id: UUID, name: str, version: int
    ) -> SkillVersion | None:
        skill = await self.get_skill_by_name(tenant_id=tenant_id, name=name)
        if skill is None:
            return None
        return await self.get_version_by_number(
            skill_id=skill.id, tenant_id=tenant_id, version=version
        )

    # ------------------------------------------------------------ evolution (Stream SE)

    async def record_eval_result(self, *, result: SkillEvalResult) -> SkillEvalResult:
        self._eval_results.append(result)
        return result

    async def list_eval_results(
        self, *, skill_id: UUID, tenant_id: UUID | None
    ) -> list[SkillEvalResult]:
        rows = [
            r for r in self._eval_results if r.skill_id == skill_id and r.tenant_id == tenant_id
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    async def record_skill_run_usage(self, *, usage: SkillRunUsage) -> SkillRunUsage:
        self._run_usage.append(usage)
        return usage

    async def skill_run_usage_window(
        self,
        *,
        skill_id: UUID,
        skill_version: int,
        tenant_id: UUID | None,
        since: datetime,
    ) -> list[SkillRunUsage]:
        rows = [
            r
            for r in self._run_usage
            if r.skill_id == skill_id
            and r.skill_version == skill_version
            and r.tenant_id == tenant_id
            and r.created_at >= since
        ]
        rows.sort(key=lambda r: r.created_at)
        return rows

    # ----------------------------------- prediction-falsify ledger (SE-11)

    async def record_prediction_verdict(
        self, *, verdict: SkillPredictionVerdict
    ) -> SkillPredictionVerdict:
        self._prediction_verdicts.append(verdict)
        return verdict

    async def list_prediction_verdicts(
        self, *, skill_id: UUID, tenant_id: UUID | None
    ) -> list[SkillPredictionVerdict]:
        rows = [
            v
            for v in self._prediction_verdicts
            if v.skill_id == skill_id and v.tenant_id == tenant_id
        ]
        rows.sort(key=lambda v: v.created_at, reverse=True)
        return rows

    # ----------------------------------- promote approval flow (SE-8, SE-A13b)

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
        skill = await self.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        if skill is None:
            raise SkillNotFoundError(str(skill_id))
        for existing in self._promote_requests.values():
            if (
                existing.skill_id == skill_id
                and existing.tenant_id == tenant_id
                and existing.status == "pending"
            ):
                raise DuplicatePromoteRequestError(skill_id=skill_id)
        req = SkillPromoteRequest(
            id=request_id,
            tenant_id=tenant_id,
            skill_id=skill_id,
            skill_version=skill_version,
            status="pending",
            requested_by_user_id=requested_by_user_id,
            requested_by_agent_name=requested_by_agent_name,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        self._promote_requests[request_id] = req
        return req

    async def _decide_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        status: PromoteRequestStatus,
        decided_by_user_id: UUID,
        decision_reason: str,
    ) -> SkillPromoteRequest:
        req = await self.get_promote_request(request_id=request_id, tenant_id=tenant_id)
        if req is None:
            raise PromoteRequestNotFoundError(str(request_id))
        if req.status != "pending":
            msg = f"promote request {request_id} is {req.status}, not pending"
            raise ValueError(msg)
        decided = req.model_copy(
            update={
                "status": status,
                "decided_by_user_id": decided_by_user_id,
                "decided_at": datetime.now(UTC),
                "decision_reason": decision_reason,
            }
        )
        self._promote_requests[request_id] = decided
        return decided

    async def approve_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        decided_by_user_id: UUID,
        decision_reason: str = "",
    ) -> SkillPromoteRequest:
        decided = await self._decide_promote(
            request_id=request_id,
            tenant_id=tenant_id,
            status="approved",
            decided_by_user_id=decided_by_user_id,
            decision_reason=decision_reason,
        )
        # Flip the skill's visibility agent_private→tenant (atomic with decision).
        skill = self._skills.get(decided.skill_id)
        if skill is not None and skill.tenant_id == tenant_id:
            self._skills[decided.skill_id] = skill.model_copy(
                update={"visibility": "tenant", "updated_at": datetime.now(UTC)}
            )
        return decided

    async def reject_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        decided_by_user_id: UUID,
        decision_reason: str = "",
    ) -> SkillPromoteRequest:
        return await self._decide_promote(
            request_id=request_id,
            tenant_id=tenant_id,
            status="rejected",
            decided_by_user_id=decided_by_user_id,
            decision_reason=decision_reason,
        )

    async def get_promote_request(
        self, *, request_id: UUID, tenant_id: UUID
    ) -> SkillPromoteRequest | None:
        req = self._promote_requests.get(request_id)
        if req is None or req.tenant_id != tenant_id:
            return None
        return req

    async def list_promote_requests(
        self,
        *,
        tenant_id: UUID,
        status: PromoteRequestStatus | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
        skill_visibility: SkillVisibility | None = None,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        rows = [r for r in self._promote_requests.values() if r.tenant_id == tenant_id]
        if skill_visibility is not None:
            # Backlog task 8 — same join-by-visibility semantics as the SQL
            # backend: a request whose target skill is missing (shouldn't
            # happen — FK-equivalent invariant) is excluded, matching an
            # INNER JOIN.
            rows = [
                r
                for r in rows
                if (skill := self._skills.get(r.skill_id)) is not None
                and skill.visibility == skill_visibility
            ]
        return _paginate_promote_requests(rows, status=status, cursor=cursor, limit=limit)

    async def list_promote_requests_all_tenants(
        self,
        *,
        status: PromoteRequestStatus | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        rows = list(self._promote_requests.values())
        return _paginate_promote_requests(rows, status=status, cursor=cursor, limit=limit)

    # -------------------------------------- evolution kill-switch (SE-8, SE-A13c)

    async def get_kill_switch(
        self, *, scope: KillSwitchScope, tenant_id: UUID | None
    ) -> KillSwitch | None:
        for sw in self._kill_switches:
            if sw.scope == scope and sw.tenant_id == tenant_id:
                return sw
        return None

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
        now = datetime.now(UTC)
        existing = await self.get_kill_switch(scope=scope, tenant_id=tenant_id)
        if existing is None:
            sw = KillSwitch(
                id=switch_id,
                scope=scope,
                tenant_id=tenant_id,
                engaged=engaged,
                reason=reason,
                engaged_by_user_id=actor_user_id if engaged else None,
                engaged_at=now if engaged else None,
                released_by_user_id=None if engaged else actor_user_id,
                released_at=None if engaged else now,
                updated_at=now,
            )
            self._kill_switches.append(sw)
            return sw
        update: dict[str, object] = {"engaged": engaged, "reason": reason, "updated_at": now}
        if engaged:
            update["engaged_by_user_id"] = actor_user_id
            update["engaged_at"] = now
        else:
            update["released_by_user_id"] = actor_user_id
            update["released_at"] = now
        replaced = existing.model_copy(update=update)
        self._kill_switches = [
            replaced if (s.scope == scope and s.tenant_id == tenant_id) else s
            for s in self._kill_switches
        ]
        return replaced

    async def is_evolution_halted(self, *, tenant_id: UUID) -> bool:
        for sw in self._kill_switches:
            if not sw.engaged:
                continue
            if sw.scope == "global" or (sw.scope == "tenant" and sw.tenant_id == tenant_id):
                return True
        return False

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        touched = 0
        # skill.created_by_user_id — only tenant-owned rows (NULL-tenant
        # platform skills never equal the concrete tenant_id).
        for skill_id, row in list(self._skills.items()):
            if row.tenant_id == tenant_id and row.created_by_user_id == user_id:
                self._skills[skill_id] = row.model_copy(update={"created_by_user_id": None})
                touched += 1
        # skill_promote_request.{requested_by_user_id, decided_by_user_id}.
        for req_id, req in list(self._promote_requests.items()):
            if req.tenant_id != tenant_id:
                continue
            update: dict[str, object | None] = {}
            if req.requested_by_user_id == user_id:
                update["requested_by_user_id"] = None
            if req.decided_by_user_id == user_id:
                update["decided_by_user_id"] = None
            if update:
                self._promote_requests[req_id] = req.model_copy(update=update)
                touched += 1
        # skill_evolution_kill_switch.{engaged_by_user_id, released_by_user_id}
        # — only tenant-scope rows (global rows carry NULL tenant_id).
        for i, sw in enumerate(self._kill_switches):
            if sw.tenant_id != tenant_id:
                continue
            sw_update: dict[str, object | None] = {}
            if sw.engaged_by_user_id == user_id:
                sw_update["engaged_by_user_id"] = None
            if sw.released_by_user_id == user_id:
                sw_update["released_by_user_id"] = None
            if sw_update:
                self._kill_switches[i] = sw.model_copy(update=sw_update)
                touched += 1
        return touched

    # ------------------------------------------------------------ platform (Stream X)
    #
    # Platform rows have ``tenant_id is None``. Mirrors the SQL store's
    # ``tenant_id IS NULL`` filter.

    async def create_platform_skill(
        self,
        *,
        skill_id: UUID,
        name: str,
        description: str = "",
        category: str | None = None,
        required_tier: TenantPlan = TenantPlan.FREE,
    ) -> Skill:
        return await self._create_skill_row(
            skill_id=skill_id,
            tenant_id=None,
            name=name,
            description=description,
            category=category,
            required_tier=required_tier,
        )

    async def get_platform_skill(self, *, skill_id: UUID) -> Skill | None:
        row = self._skills.get(skill_id)
        if row is None or row.tenant_id is not None:
            return None
        return row

    async def get_platform_skill_by_name(self, *, name: str) -> Skill | None:
        for row in self._skills.values():
            if row.tenant_id is None and row.name == name:
                return row
        return None

    async def list_platform_skills(
        self,
        *,
        status: SkillStatus | None = None,
        category: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Skill], int]:
        rows = self._platform_matches(status=status, category=category, q=q)
        # created_at DESC, id ASC (mirror the SQL ordering).
        rows.sort(key=lambda r: str(r.id))
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    def _platform_matches(
        self, *, status: SkillStatus | None, category: str | None, q: str | None
    ) -> list[Skill]:
        rows = [r for r in self._skills.values() if r.tenant_id is None]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        if category is not None:
            rows = [r for r in rows if r.category == category]
        if q:
            ql = q.lower()
            rows = [
                r
                for r in rows
                if ql in (r.name or "").lower() or ql in (r.description or "").lower()
            ]
        return rows

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
        if ids is None and filter_status is None and filter_category is None and filter_q is None:
            raise ValueError("bulk_update_platform_skills requires ids or a filter")
        if set_status is None and set_pinned is None and not update_category:
            raise ValueError(
                "bulk_update_platform_skills requires set_status, set_pinned, or update_category"
            )
        if ids is not None:
            targets = [
                self._skills[i]
                for i in ids
                if i in self._skills and self._skills[i].tenant_id is None
            ]
        else:
            targets = self._platform_matches(
                status=filter_status, category=filter_category, q=filter_q
            )
        now = datetime.now(UTC)
        update: dict[str, Any] = {"updated_at": now}
        if set_status is not None:
            update["status"] = set_status
            update["state_changed_at"] = now
        if set_pinned is not None:
            update["pinned"] = set_pinned
        if update_category:
            update["category"] = new_category
        for row in targets:
            self._skills[row.id] = row.model_copy(update=update)
        return len(targets)

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
        parent = await self.get_platform_skill(skill_id=skill_id)
        return await self._append_version(
            parent=parent,
            version_id=version_id,
            skill_id=skill_id,
            tenant_id=None,
            prompt_fragment=prompt_fragment,
            tool_names=tool_names,
            description=description,
            category=category,
            required_models=required_models,
            authored_by=authored_by,
            supporting_files=supporting_files,
            lazy_load=lazy_load,
            content_hash=content_hash,
            high_risk=high_risk,
        )

    async def get_platform_version(self, *, version_id: UUID) -> SkillVersion | None:
        for v in self._versions:
            if v.id == version_id and v.tenant_id is None:
                return v
        return None

    async def get_platform_version_by_number(
        self, *, skill_id: UUID, version: int
    ) -> SkillVersion | None:
        for v in self._versions:
            if v.skill_id == skill_id and v.tenant_id is None and v.version == version:
                return v
        return None

    async def list_platform_versions(self, *, skill_id: UUID) -> list[SkillVersion]:
        versions = [v for v in self._versions if v.skill_id == skill_id and v.tenant_id is None]
        versions.sort(key=lambda v: v.version, reverse=True)
        return versions

    async def set_platform_status(self, *, skill_id: UUID, status: SkillStatus) -> Skill:
        row = await self.get_platform_skill(skill_id=skill_id)
        if row is None:
            raise SkillNotFoundError(str(skill_id))
        now = datetime.now(UTC)
        updated = row.model_copy(
            update={"status": status, "updated_at": now, "state_changed_at": now}
        )
        self._skills[skill_id] = updated
        return updated

    async def set_platform_pinned(self, *, skill_id: UUID, pinned: bool) -> Skill:
        row = await self.get_platform_skill(skill_id=skill_id)
        if row is None:
            raise SkillNotFoundError(str(skill_id))
        updated = row.model_copy(update={"pinned": pinned, "updated_at": datetime.now(UTC)})
        self._skills[skill_id] = updated
        return updated

    async def set_platform_category(self, *, skill_id: UUID, category: str | None) -> Skill:
        row = await self.get_platform_skill(skill_id=skill_id)
        if row is None:
            raise SkillNotFoundError(str(skill_id))
        normalized = category.strip() if category else None
        updated = row.model_copy(
            update={"category": normalized or None, "updated_at": datetime.now(UTC)}
        )
        self._skills[skill_id] = updated
        return updated

    async def list_platform_categories(self) -> list[str]:
        cats = {s.category for s in self._skills.values() if s.tenant_id is None and s.category}
        return sorted(cats)

    async def resolve_platform_by_name(self, *, name: str) -> SkillVersion | None:
        skill = await self.get_platform_skill_by_name(name=name)
        if skill is None or skill.status != SkillStatus.ACTIVE or skill.latest_version == 0:
            return None
        return await self.get_platform_version_by_number(
            skill_id=skill.id, version=skill.latest_version
        )

    async def resolve_platform_pinned(self, *, name: str, version: int) -> SkillVersion | None:
        skill = await self.get_platform_skill_by_name(name=name)
        if skill is None:
            return None
        return await self.get_platform_version_by_number(skill_id=skill.id, version=version)

    # ------------------------------------------------------------ Curator (Sprint #4)

    async def bump_last_used_at(self, *, skill_id: UUID, tenant_id: UUID) -> tuple[bool, bool]:
        row = await self.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        if row is None:
            return (False, False)
        # archived / draft never advance via activity; only admin status
        # PATCH can move them. Pinned still bumps last_used_at — pin is
        # "don't auto-transition", not "don't track".
        if row.status not in (SkillStatus.ACTIVE, SkillStatus.STALE):
            return (False, False)
        now = datetime.now(UTC)
        auto_revived = row.status == SkillStatus.STALE
        updated = row.model_copy(
            update={
                "last_used_at": now,
                "status": SkillStatus.ACTIVE if auto_revived else row.status,
                "state_changed_at": now if auto_revived else row.state_changed_at,
                "updated_at": now,
            }
        )
        self._skills[skill_id] = updated
        return (True, auto_revived)

    async def curator_promote_active_to_stale(self, *, tenant_id: UUID, stale_days: int) -> int:
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=stale_days)
        n = 0
        for skill_id, row in list(self._skills.items()):
            if row.tenant_id != tenant_id:
                continue
            if row.status != SkillStatus.ACTIVE or row.pinned:
                continue
            # NULL last_used_at means "never been used since migration";
            # rely on backfill to have populated it. Belt-and-braces: a
            # row with NULL last_used_at is treated as "infinitely
            # stale" (transition).
            if row.last_used_at is None or row.last_used_at < cutoff:
                now = datetime.now(UTC)
                self._skills[skill_id] = row.model_copy(
                    update={
                        "status": SkillStatus.STALE,
                        "state_changed_at": now,
                        "updated_at": now,
                    }
                )
                n += 1
        return n

    async def curator_promote_stale_to_archived(self, *, tenant_id: UUID, archive_days: int) -> int:
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=archive_days)
        n = 0
        for skill_id, row in list(self._skills.items()):
            if row.tenant_id != tenant_id:
                continue
            if row.status != SkillStatus.STALE or row.pinned:
                continue
            if row.last_used_at is None or row.last_used_at < cutoff:
                now = datetime.now(UTC)
                self._skills[skill_id] = row.model_copy(
                    update={
                        "status": SkillStatus.ARCHIVED,
                        "state_changed_at": now,
                        "updated_at": now,
                    }
                )
                n += 1
        return n

    async def set_pinned(self, *, skill_id: UUID, tenant_id: UUID, pinned: bool) -> Skill:
        row = await self.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        if row is None:
            raise SkillNotFoundError(str(skill_id))
        updated = row.model_copy(update={"pinned": pinned, "updated_at": datetime.now(UTC)})
        self._skills[skill_id] = updated
        return updated

    async def curator_distinct_tenant_ids(self) -> list[UUID]:
        seen: set[UUID] = set()
        for row in self._skills.values():
            # Stream X (Mini-ADR X-3): skip platform (NULL-tenant) skills —
            # they are never swept by per-tenant inactivity.
            if row.tenant_id is not None and row.status in (
                SkillStatus.ACTIVE,
                SkillStatus.STALE,
            ):
                seen.add(row.tenant_id)
        return sorted(seen)

    async def count_pinned(self) -> int:
        return sum(1 for row in self._skills.values() if row.pinned)


def _new_id() -> UUID:
    return uuid4()
