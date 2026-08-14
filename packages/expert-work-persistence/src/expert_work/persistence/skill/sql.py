"""SQLAlchemy-backed ``SkillStore`` — Stream J.7a (Mini-ADR J-23)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import (
    SkillEvalResultRow,
    SkillEvolutionKillSwitchRow,
    SkillPredictionVerdictRow,
    SkillPromoteRequestRow,
    SkillRow,
    SkillRunUsageRow,
    SkillVersionRow,
)
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
from expert_work.protocol.skill import SkillSupportingFile
from expert_work.protocol.tenant_config import TenantPlan


def _skill_row_to_dto(row: SkillRow) -> Skill:
    return Skill(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        status=SkillStatus(row.status),
        latest_version=row.latest_version,
        description=row.description,
        category=row.category,
        # Stream X (Mini-ADR X-2) — minimum plan tier (platform skills).
        required_tier=TenantPlan(row.required_tier),
        # Capability Uplift Sprint #4 (Mini-ADR U-25). Existing rows
        # carry default values per migration 0043 backfill
        # (pinned=false, last_used_at=updated_at, state_changed_at=updated_at).
        pinned=bool(row.pinned),
        last_used_at=row.last_used_at,
        state_changed_at=row.state_changed_at,
        # Stream SE (Mini-ADR SE-A1) — ownership / lineage. Existing rows
        # carry migration 0065/0066 defaults (visibility='tenant', NULL owner).
        visibility=row.visibility,  # type: ignore[arg-type]
        created_by_user_id=row.created_by_user_id,
        created_by_agent_name=row.created_by_agent_name,
        forked_from=row.forked_from,
        # Stream SE (SE-10) — component type. Existing rows carry the
        # migration 0069 default (component_type='skill', NULL target).
        component_type=row.component_type,  # type: ignore[arg-type]
        target_tool_name=row.target_tool_name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version_row_to_dto(row: SkillVersionRow) -> SkillVersion:
    raw_supporting = dict(row.supporting_files or {})
    typed_supporting = {path: SkillSupportingFile(**meta) for path, meta in raw_supporting.items()}
    return SkillVersion(
        id=row.id,
        skill_id=row.skill_id,
        tenant_id=row.tenant_id,
        version=row.version,
        prompt_fragment=row.prompt_fragment,
        tool_names=tuple(row.tool_names or ()),
        description=row.description,
        category=row.category,
        required_models=tuple(row.required_models or ()),
        authored_by=row.authored_by,  # type: ignore[arg-type]
        # Capability Uplift Sprint #3 fields. Existing M0 rows have
        # default-empty values per migration 0042 backfill.
        supporting_files=typed_supporting,
        lazy_load=bool(row.lazy_load),
        content_hash=bytes(row.content_hash or b""),
        high_risk=bool(row.high_risk),
        # Stream SE (Mini-ADR SE-A1) — evolution provenance. Existing rows
        # carry migration 0065 defaults (origin NULL, round 0).
        evolution_origin=row.evolution_origin,  # type: ignore[arg-type]
        distilled_from_trajectory_key=row.distilled_from_trajectory_key,
        distilled_from_candidate_id=row.distilled_from_candidate_id,
        evolution_round=row.evolution_round,
        created_at=row.created_at,
    )


def _eval_result_row_to_dto(row: SkillEvalResultRow) -> SkillEvalResult:
    return SkillEvalResult(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        baseline_score=row.baseline_score,
        skill_score=row.skill_score,
        delta=row.delta,
        n_cases=row.n_cases,
        replay_source=row.replay_source,  # type: ignore[arg-type]
        verdict=row.verdict,  # type: ignore[arg-type]
        high_risk=bool(row.high_risk),
        evolution_round=row.evolution_round,
        created_at=row.created_at,
    )


def _run_usage_row_to_dto(row: SkillRunUsageRow) -> SkillRunUsage:
    return SkillRunUsage(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        thread_id=row.thread_id,
        agent_name=row.agent_name,
        outcome=row.outcome,  # type: ignore[arg-type]
        created_at=row.created_at,
    )


def _prediction_verdict_row_to_dto(row: SkillPredictionVerdictRow) -> SkillPredictionVerdict:
    return SkillPredictionVerdict(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        verdict=row.verdict,  # type: ignore[arg-type]
        predicted_delta=row.predicted_delta,
        realized_delta=row.realized_delta,
        realized_fraction=row.realized_fraction,
        baseline_score=row.baseline_score,
        skill_score=row.skill_score,
        observed_rate=row.observed_rate,
        n_window=row.n_window,
        created_at=row.created_at,
    )


def _promote_request_row_to_dto(row: SkillPromoteRequestRow) -> SkillPromoteRequest:
    return SkillPromoteRequest(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        status=row.status,  # type: ignore[arg-type]
        requested_by_user_id=row.requested_by_user_id,
        requested_by_agent_name=row.requested_by_agent_name,
        reason=row.reason,
        decided_by_user_id=row.decided_by_user_id,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
        created_at=row.created_at,
    )


def _kill_switch_row_to_dto(row: SkillEvolutionKillSwitchRow) -> KillSwitch:
    return KillSwitch(
        id=row.id,
        scope=row.scope,  # type: ignore[arg-type]
        tenant_id=row.tenant_id,
        engaged=bool(row.engaged),
        reason=row.reason,
        engaged_by_user_id=row.engaged_by_user_id,
        engaged_at=row.engaged_at,
        released_by_user_id=row.released_by_user_id,
        released_at=row.released_at,
        updated_at=row.updated_at,
    )


class SqlSkillStore(SkillStore):
    """Postgres-backed skill registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

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
        now = datetime.now(UTC)
        async with self._sf() as session:
            row = SkillRow(
                id=skill_id,
                tenant_id=tenant_id,
                name=name,
                status=SkillStatus.DRAFT.value,
                latest_version=0,
                description=description,
                category=category,
                required_tier=required_tier.value,
                visibility=visibility,
                created_by_user_id=created_by_user_id,
                created_by_agent_name=created_by_agent_name,
                forked_from=forked_from,
                component_type=component_type,
                target_tool_name=target_tool_name,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                # ``skill_tenant_name_uniq`` (COALESCE) violation — POST collision.
                raise DuplicateSkillError(tenant_id=tenant_id, name=name) from exc
            await session.refresh(row)
            return _skill_row_to_dto(row)

    async def get_skill(self, *, skill_id: UUID, tenant_id: UUID) -> Skill | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillRow).where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
        return _skill_row_to_dto(row) if row is not None else None

    async def get_skill_by_name(self, *, tenant_id: UUID, name: str) -> Skill | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillRow).where(SkillRow.tenant_id == tenant_id, SkillRow.name == name)
                )
            ).scalar_one_or_none()
        return _skill_row_to_dto(row) if row is not None else None

    async def shadowed_skill_names(self, *, tenant_id: UUID, names: Sequence[str]) -> set[str]:
        wanted = list(dict.fromkeys(names))
        if not wanted:
            return set()
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(SkillRow.name).where(
                            SkillRow.tenant_id == tenant_id,
                            SkillRow.name.in_(wanted),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return set(rows)

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
        return await self._list_skills(
            tenant_id=tenant_id,
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
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        return await self._list_skills(
            tenant_id=None,
            status=status,
            category=category,
            created_by_agent_name=created_by_agent_name,
            cursor=cursor,
            limit=limit,
        )

    async def _list_skills(
        self,
        *,
        tenant_id: UUID | None,
        status: SkillStatus | None,
        category: str | None,
        cursor: UUID | None,
        limit: int,
        visibility: SkillVisibility | None = None,
        created_by_user_id: UUID | None = None,
        created_by_agent_name: str | None = None,
    ) -> tuple[list[Skill], UUID | None]:
        async with self._sf() as session:
            stmt = select(SkillRow).order_by(SkillRow.created_at.desc(), SkillRow.id)
            if tenant_id is not None:
                stmt = stmt.where(SkillRow.tenant_id == tenant_id)
            if status is not None:
                stmt = stmt.where(SkillRow.status == status.value)
            if category is not None:
                stmt = stmt.where(SkillRow.category == category)
            if visibility is not None:
                stmt = stmt.where(SkillRow.visibility == visibility)
            if created_by_user_id is not None:
                stmt = stmt.where(SkillRow.created_by_user_id == created_by_user_id)
            if created_by_agent_name is not None:
                stmt = stmt.where(SkillRow.created_by_agent_name == created_by_agent_name)
            if cursor is not None:
                cur_stmt = select(SkillRow).where(SkillRow.id == cursor)
                if tenant_id is not None:
                    cur_stmt = cur_stmt.where(SkillRow.tenant_id == tenant_id)
                cur_row = (await session.execute(cur_stmt)).scalar_one_or_none()
                if cur_row is not None:
                    stmt = stmt.where(
                        (SkillRow.created_at < cur_row.created_at)
                        | ((SkillRow.created_at == cur_row.created_at) & (SkillRow.id > cur_row.id))
                    )
            stmt = stmt.limit(limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
        items = [_skill_row_to_dto(r) for r in rows]
        if len(items) > limit:
            return items[:limit], items[limit - 1].id
        return items, None

    async def set_status(self, *, skill_id: UUID, tenant_id: UUID, status: SkillStatus) -> Skill:
        async with self._sf() as session:
            now = datetime.now(UTC)
            result = await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                .values(status=status.value, updated_at=now, state_changed_at=now)
                .returning(SkillRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise SkillNotFoundError(str(skill_id))
            await session.commit()
        return _skill_row_to_dto(row)

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
        if authored_by not in {"human", "agent"}:
            msg = f"authored_by must be 'human' or 'agent' (got {authored_by!r})"
            raise ValueError(msg)
        now = datetime.now(UTC)
        async with self._sf() as session:
            parent = (
                await session.execute(
                    select(SkillRow).where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            if parent is None:
                raise SkillNotFoundError(str(skill_id))
            next_version = parent.latest_version + 1
            new_description = description or parent.description
            new_category = category if category is not None else parent.category
            version_row = SkillVersionRow(
                id=version_id,
                tenant_id=tenant_id,
                skill_id=skill_id,
                version=next_version,
                prompt_fragment=prompt_fragment,
                tool_names=list(tool_names),
                description=new_description,
                category=new_category,
                required_models=list(required_models),
                authored_by=authored_by,
                # Capability Uplift Sprint #3 fields. Default-empty values
                # keep Stream J.7a JSON-API path safe; ZIP / supporting-files
                # mutation paths compute + pass real values.
                supporting_files=supporting_files or {},
                lazy_load=lazy_load,
                content_hash=content_hash,
                high_risk=high_risk,
                # Stream SE (SE-A1) — evolution provenance.
                evolution_origin=evolution_origin,
                distilled_from_trajectory_key=distilled_from_trajectory_key,
                distilled_from_candidate_id=distilled_from_candidate_id,
                evolution_round=evolution_round,
                created_at=now,
            )
            session.add(version_row)
            parent.latest_version = next_version
            parent.description = new_description
            parent.category = new_category
            parent.updated_at = now
            await session.commit()
            await session.refresh(version_row)
            return _version_row_to_dto(version_row)

    async def get_version(self, *, version_id: UUID, tenant_id: UUID) -> SkillVersion | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillVersionRow).where(
                        SkillVersionRow.id == version_id,
                        SkillVersionRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _version_row_to_dto(row) if row is not None else None

    async def get_version_by_number(
        self, *, skill_id: UUID, tenant_id: UUID, version: int
    ) -> SkillVersion | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillVersionRow).where(
                        SkillVersionRow.skill_id == skill_id,
                        SkillVersionRow.tenant_id == tenant_id,
                        SkillVersionRow.version == version,
                    )
                )
            ).scalar_one_or_none()
        return _version_row_to_dto(row) if row is not None else None

    async def list_versions(self, *, skill_id: UUID, tenant_id: UUID) -> list[SkillVersion]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(SkillVersionRow)
                        .where(
                            SkillVersionRow.skill_id == skill_id,
                            SkillVersionRow.tenant_id == tenant_id,
                        )
                        .order_by(SkillVersionRow.version.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_version_row_to_dto(r) for r in rows]

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
        async with self._sf() as session:
            row = SkillEvalResultRow(
                id=result.id,
                tenant_id=result.tenant_id,
                skill_id=result.skill_id,
                skill_version=result.skill_version,
                baseline_score=result.baseline_score,
                skill_score=result.skill_score,
                delta=result.delta,
                n_cases=result.n_cases,
                replay_source=result.replay_source,
                verdict=result.verdict,
                high_risk=result.high_risk,
                evolution_round=result.evolution_round,
                created_at=result.created_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _eval_result_row_to_dto(row)

    async def list_eval_results(
        self, *, skill_id: UUID, tenant_id: UUID | None
    ) -> list[SkillEvalResult]:
        async with self._sf() as session:
            stmt = (
                select(SkillEvalResultRow)
                .where(SkillEvalResultRow.skill_id == skill_id)
                .order_by(SkillEvalResultRow.created_at.desc())
            )
            stmt = (
                stmt.where(SkillEvalResultRow.tenant_id == tenant_id)
                if tenant_id is not None
                else stmt.where(SkillEvalResultRow.tenant_id.is_(None))
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_eval_result_row_to_dto(r) for r in rows]

    async def record_skill_run_usage(self, *, usage: SkillRunUsage) -> SkillRunUsage:
        async with self._sf() as session:
            row = SkillRunUsageRow(
                id=usage.id,
                tenant_id=usage.tenant_id,
                skill_id=usage.skill_id,
                skill_version=usage.skill_version,
                thread_id=usage.thread_id,
                agent_name=usage.agent_name,
                outcome=usage.outcome,
                created_at=usage.created_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _run_usage_row_to_dto(row)

    async def skill_run_usage_window(
        self,
        *,
        skill_id: UUID,
        skill_version: int,
        tenant_id: UUID | None,
        since: datetime,
    ) -> list[SkillRunUsage]:
        async with self._sf() as session:
            stmt = (
                select(SkillRunUsageRow)
                .where(
                    SkillRunUsageRow.skill_id == skill_id,
                    SkillRunUsageRow.skill_version == skill_version,
                    SkillRunUsageRow.created_at >= since,
                )
                .order_by(SkillRunUsageRow.created_at.asc())
            )
            stmt = (
                stmt.where(SkillRunUsageRow.tenant_id == tenant_id)
                if tenant_id is not None
                else stmt.where(SkillRunUsageRow.tenant_id.is_(None))
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_run_usage_row_to_dto(r) for r in rows]

    # ----------------------------------- prediction-falsify ledger (SE-11)

    async def record_prediction_verdict(
        self, *, verdict: SkillPredictionVerdict
    ) -> SkillPredictionVerdict:
        async with self._sf() as session:
            row = SkillPredictionVerdictRow(
                id=verdict.id,
                tenant_id=verdict.tenant_id,
                skill_id=verdict.skill_id,
                skill_version=verdict.skill_version,
                verdict=verdict.verdict,
                predicted_delta=verdict.predicted_delta,
                realized_delta=verdict.realized_delta,
                realized_fraction=verdict.realized_fraction,
                baseline_score=verdict.baseline_score,
                skill_score=verdict.skill_score,
                observed_rate=verdict.observed_rate,
                n_window=verdict.n_window,
                created_at=verdict.created_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _prediction_verdict_row_to_dto(row)

    async def list_prediction_verdicts(
        self, *, skill_id: UUID, tenant_id: UUID | None
    ) -> list[SkillPredictionVerdict]:
        async with self._sf() as session:
            stmt = (
                select(SkillPredictionVerdictRow)
                .where(SkillPredictionVerdictRow.skill_id == skill_id)
                .order_by(SkillPredictionVerdictRow.created_at.desc())
            )
            stmt = (
                stmt.where(SkillPredictionVerdictRow.tenant_id == tenant_id)
                if tenant_id is not None
                else stmt.where(SkillPredictionVerdictRow.tenant_id.is_(None))
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_prediction_verdict_row_to_dto(r) for r in rows]

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
        async with self._sf() as session:
            parent = (
                await session.execute(
                    select(SkillRow).where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            if parent is None:
                raise SkillNotFoundError(str(skill_id))
            row = SkillPromoteRequestRow(
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
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                # uq_skill_promote_request_pending — one open request per skill.
                raise DuplicatePromoteRequestError(skill_id=skill_id) from exc
            await session.refresh(row)
            return _promote_request_row_to_dto(row)

    async def approve_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        decided_by_user_id: UUID,
        decision_reason: str = "",
    ) -> SkillPromoteRequest:
        async with self._sf() as session:
            row = await self._load_pending_request(
                session, request_id=request_id, tenant_id=tenant_id
            )
            now = datetime.now(UTC)
            row.status = "approved"
            row.decided_by_user_id = decided_by_user_id
            row.decided_at = now
            row.decision_reason = decision_reason
            # Flip the skill's visibility agent_private→tenant (atomic).
            await session.execute(
                update(SkillRow)
                .where(SkillRow.id == row.skill_id, SkillRow.tenant_id == tenant_id)
                .values(visibility="tenant", updated_at=now)
            )
            await session.commit()
            await session.refresh(row)
            return _promote_request_row_to_dto(row)

    async def reject_skill_promote(
        self,
        *,
        request_id: UUID,
        tenant_id: UUID,
        decided_by_user_id: UUID,
        decision_reason: str = "",
    ) -> SkillPromoteRequest:
        async with self._sf() as session:
            row = await self._load_pending_request(
                session, request_id=request_id, tenant_id=tenant_id
            )
            row.status = "rejected"
            row.decided_by_user_id = decided_by_user_id
            row.decided_at = datetime.now(UTC)
            row.decision_reason = decision_reason
            await session.commit()
            await session.refresh(row)
            return _promote_request_row_to_dto(row)

    @staticmethod
    async def _load_pending_request(
        session: AsyncSession, *, request_id: UUID, tenant_id: UUID
    ) -> SkillPromoteRequestRow:
        row = (
            await session.execute(
                select(SkillPromoteRequestRow).where(
                    SkillPromoteRequestRow.id == request_id,
                    SkillPromoteRequestRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise PromoteRequestNotFoundError(str(request_id))
        if row.status != "pending":
            msg = f"promote request {request_id} is {row.status}, not pending"
            raise ValueError(msg)
        return row

    async def get_promote_request(
        self, *, request_id: UUID, tenant_id: UUID
    ) -> SkillPromoteRequest | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillPromoteRequestRow).where(
                        SkillPromoteRequestRow.id == request_id,
                        SkillPromoteRequestRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _promote_request_row_to_dto(row) if row is not None else None

    async def list_promote_requests(
        self,
        *,
        tenant_id: UUID,
        status: PromoteRequestStatus | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
        skill_visibility: SkillVisibility | None = None,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        return await self._list_promote_requests(
            tenant_id=tenant_id,
            status=status,
            cursor=cursor,
            limit=limit,
            skill_visibility=skill_visibility,
        )

    async def list_promote_requests_all_tenants(
        self,
        *,
        status: PromoteRequestStatus | None = None,
        cursor: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        # No ``skill_visibility`` filter here, same as ``list_skills_all_tenants``
        # — this path is system_admin-only (``ensure_tenant_scope`` gates
        # ``CrossTenant``), so there is no non-admin caller to filter for.
        return await self._list_promote_requests(
            tenant_id=None, status=status, cursor=cursor, limit=limit
        )

    async def _list_promote_requests(
        self,
        *,
        tenant_id: UUID | None,
        status: PromoteRequestStatus | None,
        cursor: UUID | None,
        limit: int,
        skill_visibility: SkillVisibility | None = None,
    ) -> tuple[list[SkillPromoteRequest], UUID | None]:
        async with self._sf() as session:
            stmt = select(SkillPromoteRequestRow).order_by(
                SkillPromoteRequestRow.created_at.desc(), SkillPromoteRequestRow.id
            )
            if skill_visibility is not None:
                # Backlog task 8 — join to the target skill so a non-admin
                # caller's page never contains a request aimed at an
                # ``agent_private`` skill. Query-layer filter (before the
                # ``LIMIT``/cursor cut), same rule as ``list_skills``.
                stmt = stmt.join(SkillRow, SkillRow.id == SkillPromoteRequestRow.skill_id).where(
                    SkillRow.visibility == skill_visibility
                )
            if tenant_id is not None:
                stmt = stmt.where(SkillPromoteRequestRow.tenant_id == tenant_id)
            if status is not None:
                stmt = stmt.where(SkillPromoteRequestRow.status == status)
            if cursor is not None:
                cur_stmt = select(SkillPromoteRequestRow).where(SkillPromoteRequestRow.id == cursor)
                if tenant_id is not None:
                    cur_stmt = cur_stmt.where(SkillPromoteRequestRow.tenant_id == tenant_id)
                cur_row = (await session.execute(cur_stmt)).scalar_one_or_none()
                if cur_row is not None:
                    stmt = stmt.where(
                        (SkillPromoteRequestRow.created_at < cur_row.created_at)
                        | (
                            (SkillPromoteRequestRow.created_at == cur_row.created_at)
                            & (SkillPromoteRequestRow.id > cur_row.id)
                        )
                    )
            stmt = stmt.limit(limit + 1)
            rows = (await session.execute(stmt)).scalars().all()
        items = [_promote_request_row_to_dto(r) for r in rows]
        if len(items) > limit:
            return items[:limit], items[limit - 1].id
        return items, None

    # -------------------------------------- evolution kill-switch (SE-8, SE-A13c)

    async def get_kill_switch(
        self, *, scope: KillSwitchScope, tenant_id: UUID | None
    ) -> KillSwitch | None:
        async with self._sf() as session:
            stmt = select(SkillEvolutionKillSwitchRow).where(
                SkillEvolutionKillSwitchRow.scope == scope
            )
            stmt = (
                stmt.where(SkillEvolutionKillSwitchRow.tenant_id == tenant_id)
                if tenant_id is not None
                else stmt.where(SkillEvolutionKillSwitchRow.tenant_id.is_(None))
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _kill_switch_row_to_dto(row) if row is not None else None

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
        async with self._sf() as session:
            stmt = select(SkillEvolutionKillSwitchRow).where(
                SkillEvolutionKillSwitchRow.scope == scope
            )
            stmt = (
                stmt.where(SkillEvolutionKillSwitchRow.tenant_id == tenant_id)
                if tenant_id is not None
                else stmt.where(SkillEvolutionKillSwitchRow.tenant_id.is_(None))
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = SkillEvolutionKillSwitchRow(
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
                session.add(row)
            else:
                row.engaged = engaged
                row.reason = reason
                row.updated_at = now
                if engaged:
                    row.engaged_by_user_id = actor_user_id
                    row.engaged_at = now
                else:
                    row.released_by_user_id = actor_user_id
                    row.released_at = now
            await session.commit()
            await session.refresh(row)
            return _kill_switch_row_to_dto(row)

    async def is_evolution_halted(self, *, tenant_id: UUID) -> bool:
        async with self._sf() as session:
            stmt = (
                select(SkillEvolutionKillSwitchRow.id)
                .where(
                    SkillEvolutionKillSwitchRow.engaged.is_(True),
                    or_(
                        SkillEvolutionKillSwitchRow.scope == "global",
                        and_(
                            SkillEvolutionKillSwitchRow.scope == "tenant",
                            SkillEvolutionKillSwitchRow.tenant_id == tenant_id,
                        ),
                    ),
                )
                .limit(1)
            )
            hit = (await session.execute(stmt)).scalar_one_or_none()
        return hit is not None

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        # KEEP the rows (skills are tenant assets); null only the actor columns.
        # Every predicate carries a concrete ``tenant_id``, so NULL-tenant
        # platform skills and global kill-switch rows are NEVER matched.
        skill_stmt = (
            update(SkillRow)
            .where(
                SkillRow.tenant_id == tenant_id,
                SkillRow.created_by_user_id == user_id,
            )
            .values(created_by_user_id=None)
        )
        promote_stmt = (
            update(SkillPromoteRequestRow)
            .where(
                SkillPromoteRequestRow.tenant_id == tenant_id,
                or_(
                    SkillPromoteRequestRow.requested_by_user_id == user_id,
                    SkillPromoteRequestRow.decided_by_user_id == user_id,
                ),
            )
            .values(
                requested_by_user_id=case(
                    (SkillPromoteRequestRow.requested_by_user_id == user_id, None),
                    else_=SkillPromoteRequestRow.requested_by_user_id,
                ),
                decided_by_user_id=case(
                    (SkillPromoteRequestRow.decided_by_user_id == user_id, None),
                    else_=SkillPromoteRequestRow.decided_by_user_id,
                ),
            )
        )
        kill_switch_stmt = (
            update(SkillEvolutionKillSwitchRow)
            .where(
                SkillEvolutionKillSwitchRow.tenant_id == tenant_id,
                or_(
                    SkillEvolutionKillSwitchRow.engaged_by_user_id == user_id,
                    SkillEvolutionKillSwitchRow.released_by_user_id == user_id,
                ),
            )
            .values(
                engaged_by_user_id=case(
                    (SkillEvolutionKillSwitchRow.engaged_by_user_id == user_id, None),
                    else_=SkillEvolutionKillSwitchRow.engaged_by_user_id,
                ),
                released_by_user_id=case(
                    (SkillEvolutionKillSwitchRow.released_by_user_id == user_id, None),
                    else_=SkillEvolutionKillSwitchRow.released_by_user_id,
                ),
            )
        )
        touched = 0
        async with self._sf() as session:
            for stmt in (skill_stmt, promote_stmt, kill_switch_stmt):
                result = await session.execute(stmt)
                touched += int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        return touched

    # ------------------------------------------------------------ platform (Stream X)
    #
    # NULL-tenant rows in the same tables. ``WHERE tenant_id IS NULL``
    # everywhere. Caller MUST be inside ``bypass_rls_session()`` so the
    # 0057 ``IS NOT DISTINCT FROM`` policy lets the NULL rows through.

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
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillRow).where(SkillRow.id == skill_id, SkillRow.tenant_id.is_(None))
                )
            ).scalar_one_or_none()
        return _skill_row_to_dto(row) if row is not None else None

    async def get_platform_skill_by_name(self, *, name: str) -> Skill | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillRow).where(SkillRow.tenant_id.is_(None), SkillRow.name == name)
                )
            ).scalar_one_or_none()
        return _skill_row_to_dto(row) if row is not None else None

    def _platform_filters(
        self, status: SkillStatus | None, category: str | None, q: str | None
    ) -> list[Any]:
        """Shared WHERE predicate for the platform (NULL-tenant) list + bulk."""
        preds: list[Any] = [SkillRow.tenant_id.is_(None)]
        if status is not None:
            preds.append(SkillRow.status == status.value)
        if category is not None:
            preds.append(SkillRow.category == category)
        if q:
            like = f"%{q}%"
            preds.append(or_(SkillRow.name.ilike(like), SkillRow.description.ilike(like)))
        return preds

    async def list_platform_skills(
        self,
        *,
        status: SkillStatus | None = None,
        category: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Skill], int]:
        preds = self._platform_filters(status, category, q)
        async with self._sf() as session:
            total = (
                await session.execute(select(func.count()).select_from(SkillRow).where(*preds))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(SkillRow)
                        .where(*preds)
                        .order_by(SkillRow.created_at.desc(), SkillRow.id)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_skill_row_to_dto(r) for r in rows], int(total)

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

        now = datetime.now(UTC)
        values: dict[str, Any] = {"updated_at": now}
        if set_status is not None:
            values["status"] = set_status.value
            values["state_changed_at"] = now
        if set_pinned is not None:
            values["pinned"] = set_pinned
        if update_category:
            values["category"] = new_category

        if ids is not None:
            id_list = list(ids)
            if not id_list:
                return 0
            where = [SkillRow.tenant_id.is_(None), SkillRow.id.in_(id_list)]
        else:
            where = self._platform_filters(filter_status, filter_category, filter_q)

        async with self._sf() as session:
            result = await session.execute(update(SkillRow).where(*where).values(**values))
            await session.commit()
        # CursorResult.rowcount — the affected count for an UPDATE.
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

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
        if authored_by not in {"human", "agent"}:
            msg = f"authored_by must be 'human' or 'agent' (got {authored_by!r})"
            raise ValueError(msg)
        now = datetime.now(UTC)
        async with self._sf() as session:
            parent = (
                await session.execute(
                    select(SkillRow).where(SkillRow.id == skill_id, SkillRow.tenant_id.is_(None))
                )
            ).scalar_one_or_none()
            if parent is None:
                raise SkillNotFoundError(str(skill_id))
            next_version = parent.latest_version + 1
            new_description = description or parent.description
            new_category = category if category is not None else parent.category
            version_row = SkillVersionRow(
                id=version_id,
                tenant_id=None,
                skill_id=skill_id,
                version=next_version,
                prompt_fragment=prompt_fragment,
                tool_names=list(tool_names),
                description=new_description,
                category=new_category,
                required_models=list(required_models),
                authored_by=authored_by,
                supporting_files=supporting_files or {},
                lazy_load=lazy_load,
                content_hash=content_hash,
                high_risk=high_risk,
                created_at=now,
            )
            session.add(version_row)
            parent.latest_version = next_version
            parent.description = new_description
            parent.category = new_category
            parent.updated_at = now
            await session.commit()
            await session.refresh(version_row)
            return _version_row_to_dto(version_row)

    async def get_platform_version(self, *, version_id: UUID) -> SkillVersion | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillVersionRow).where(
                        SkillVersionRow.id == version_id,
                        SkillVersionRow.tenant_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return _version_row_to_dto(row) if row is not None else None

    async def get_platform_version_by_number(
        self, *, skill_id: UUID, version: int
    ) -> SkillVersion | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(SkillVersionRow).where(
                        SkillVersionRow.skill_id == skill_id,
                        SkillVersionRow.tenant_id.is_(None),
                        SkillVersionRow.version == version,
                    )
                )
            ).scalar_one_or_none()
        return _version_row_to_dto(row) if row is not None else None

    async def list_platform_versions(self, *, skill_id: UUID) -> list[SkillVersion]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(SkillVersionRow)
                        .where(
                            SkillVersionRow.skill_id == skill_id,
                            SkillVersionRow.tenant_id.is_(None),
                        )
                        .order_by(SkillVersionRow.version.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_version_row_to_dto(r) for r in rows]

    async def set_platform_status(self, *, skill_id: UUID, status: SkillStatus) -> Skill:
        async with self._sf() as session:
            now = datetime.now(UTC)
            result = await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.tenant_id.is_(None))
                .values(status=status.value, updated_at=now, state_changed_at=now)
                .returning(SkillRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise SkillNotFoundError(str(skill_id))
            await session.commit()
        return _skill_row_to_dto(row)

    async def set_platform_pinned(self, *, skill_id: UUID, pinned: bool) -> Skill:
        async with self._sf() as session:
            result = await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.tenant_id.is_(None))
                .values(pinned=pinned, updated_at=datetime.now(UTC))
                .returning(SkillRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise SkillNotFoundError(str(skill_id))
            await session.commit()
        return _skill_row_to_dto(row)

    async def set_platform_category(self, *, skill_id: UUID, category: str | None) -> Skill:
        # Empty string normalizes to NULL so "cleared" and "never set" are one
        # state (the filter + distinct list treat NULL as uncategorized).
        normalized = category.strip() if category else None
        async with self._sf() as session:
            result = await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.tenant_id.is_(None))
                .values(category=normalized or None, updated_at=datetime.now(UTC))
                .returning(SkillRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise SkillNotFoundError(str(skill_id))
            await session.commit()
        return _skill_row_to_dto(row)

    async def list_platform_categories(self) -> list[str]:
        async with self._sf() as session:
            result = await session.execute(
                select(SkillRow.category)
                .where(SkillRow.tenant_id.is_(None), SkillRow.category.isnot(None))
                .distinct()
                .order_by(SkillRow.category)
            )
            return [c for c in result.scalars().all() if c]

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
        """Atomic activity bump + stale→active auto-revive (Mini-ADR U-27/U-29).

        Single transaction: SELECT prior status (with row lock) → UPDATE
        conditionally flips ``stale`` to ``active``. archived / draft
        rows are filtered by the WHERE clause → no-op + return
        (False, False). Pinned rows still bump last_used_at — pin means
        "don't auto-transition", not "don't track activity".
        """
        async with self._sf() as session:
            prior = (
                await session.execute(
                    select(SkillRow.status)
                    .where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if prior is None or prior not in ("active", "stale"):
                # archived / draft / missing → cold path no-op
                await session.commit()
                return (False, False)
            now = datetime.now(UTC)
            auto_revived = prior == "stale"
            await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                .values(
                    last_used_at=now,
                    status="active" if auto_revived else prior,
                    state_changed_at=now if auto_revived else SkillRow.state_changed_at,
                    updated_at=now,
                )
            )
            await session.commit()
        return (True, auto_revived)

    async def curator_promote_active_to_stale(self, *, tenant_id: UUID, stale_days: int) -> int:
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=stale_days)
        async with self._sf() as session:
            now = datetime.now(UTC)
            result = await session.execute(
                update(SkillRow)
                .where(
                    SkillRow.tenant_id == tenant_id,
                    SkillRow.status == "active",
                    SkillRow.pinned.is_(False),
                    # NULL last_used_at sweeps as if "infinitely stale" so
                    # rows created before the migration backfill don't
                    # linger as active forever (defensive: migration 0043
                    # backfills last_used_at to updated_at, so this branch
                    # only catches application-level inserts that forgot
                    # to seed the column).
                    (SkillRow.last_used_at.is_(None) | (SkillRow.last_used_at < cutoff)),
                )
                .values(status="stale", state_changed_at=now, updated_at=now)
            )
            await session.commit()
        # ``Result.rowcount`` is only typed on ``CursorResult``; the
        # base ``Result`` mypy sees from ``session.execute(update(...))``
        # exposes it at runtime but not at the type level. Cast through.
        return int(getattr(result, "rowcount", 0) or 0)

    async def curator_promote_stale_to_archived(self, *, tenant_id: UUID, archive_days: int) -> int:
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=archive_days)
        async with self._sf() as session:
            now = datetime.now(UTC)
            result = await session.execute(
                update(SkillRow)
                .where(
                    SkillRow.tenant_id == tenant_id,
                    SkillRow.status == "stale",
                    SkillRow.pinned.is_(False),
                    (SkillRow.last_used_at.is_(None) | (SkillRow.last_used_at < cutoff)),
                )
                .values(status="archived", state_changed_at=now, updated_at=now)
            )
            await session.commit()
        # ``Result.rowcount`` is only typed on ``CursorResult``; the
        # base ``Result`` mypy sees from ``session.execute(update(...))``
        # exposes it at runtime but not at the type level. Cast through.
        return int(getattr(result, "rowcount", 0) or 0)

    async def set_pinned(self, *, skill_id: UUID, tenant_id: UUID, pinned: bool) -> Skill:
        async with self._sf() as session:
            result = await session.execute(
                update(SkillRow)
                .where(SkillRow.id == skill_id, SkillRow.tenant_id == tenant_id)
                .values(pinned=pinned, updated_at=datetime.now(UTC))
                .returning(SkillRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise SkillNotFoundError(str(skill_id))
            await session.commit()
        return _skill_row_to_dto(row)

    async def curator_distinct_tenant_ids(self) -> list[UUID]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(SkillRow.tenant_id)
                    .where(
                        SkillRow.status.in_(["active", "stale"]),
                        # Stream X (Mini-ADR X-3): platform (NULL-tenant) skills
                        # are shared resources, never swept by per-tenant
                        # inactivity. Exclude them from the Curator's tenant list.
                        SkillRow.tenant_id.isnot(None),
                    )
                    .distinct()
                )
            ).all()
        return [r[0] for r in rows if r[0] is not None]

    async def count_pinned(self) -> int:
        from sqlalchemy import func

        async with self._sf() as session:
            result = (
                await session.execute(
                    select(func.count()).select_from(SkillRow).where(SkillRow.pinned.is_(True))
                )
            ).scalar_one()
        return int(result or 0)
