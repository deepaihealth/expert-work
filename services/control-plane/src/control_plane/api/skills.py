"""``/v1/skills`` — Stream J.7a admin CRUD + ZIP import/export.

Mini-ADR J-23 § 15.5 endpoints:

* ``POST   /v1/skills``                                   create skill (draft)
* ``POST   /v1/skills/{id}/versions``                     append version
* ``PATCH  /v1/skills/{id}``                              draft|active|archived
* ``GET    /v1/skills?status=&category=&cursor=&limit=``  list (cursor paging)
* ``GET    /v1/skills/{id}``                              get one
* ``GET    /v1/skills/{id}/versions``                     list versions
* ``GET    /v1/skills/{id}/versions/{n}``                 get single version
* ``POST   /v1/skills/import``                            multipart .skill ZIP
* ``GET    /v1/skills/{id}/versions/{n}/export``          download ZIP

All write paths pass content through the regex deny-list moderation
(``_skill_moderation``); all ZIP paths go through the size + zip-slip
guards in ``_skill_zip``. Tenant scoping is at the request layer
(``request.state.tenant_id``); RLS at the SQL layer is the second
safety net.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from control_plane.api._authz import console_only, require
from control_plane.api._skill_moderation import (
    ModerationError,
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_zip import (
    ALLOWED_EXTENSIONS,
    MAX_PATH_DEPTH,
    TEXT_EXTENSIONS,
    SkillZipError,
    build_skill_zip,
    parse_skill_zip,
)
from control_plane.audit import emit as audit_emit
from control_plane.auth.rbac import _collect_roles, is_admin
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.tenancy import TenantConfigNotConfiguredError
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    bypass_rls_session,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.common.uplift_metrics import (
    record_skill_blocked,
    record_skill_high_risk_event,
    record_threat_pattern_hits,
)
from expert_work.persistence import (
    DuplicateSkillError,
    SkillNotFoundError,
    SkillStore,
    TenantSkillSubscriptionNotFoundError,
    TenantSkillSubscriptionStore,
)
from expert_work.protocol import (
    SKILL_REF_PATTERN,
    AuditAction,
    AuditResult,
    Principal,
    Role,
    Skill,
    SkillStatus,
    SkillVersion,
    SkillVisibility,
    TenantPlan,
    TenantSkillSubscriptionRecord,
    tier_satisfies,
)
from expert_work.protocol.skill import (
    SkillPackageLayoutError,
    SkillSupportingFile,
    compute_content_hash,
    is_high_risk_skill_version,
    supporting_files_to_jsonable,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.skill_assets import (
    ObjectStore as SkillAssetObjectStore,
)
from expert_work.runtime.skill_assets import (
    SkillAssetError,
    fetch_supporting_file,
    fetch_supporting_files,
)

logger = logging.getLogger("expert_work.control_plane.skills")


class _CreateSkillBody(BaseModel):
    """``POST /v1/skills`` request body."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(default="", max_length=1024)
    category: str | None = Field(default=None, max_length=64)


class _AddVersionBody(BaseModel):
    """``POST /v1/skills/{id}/versions`` request body."""

    prompt_fragment: str = Field(min_length=1)
    tool_names: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=1024)
    category: str | None = Field(default=None, max_length=64)
    required_models: list[str] = Field(default_factory=list)
    authored_by: str = Field(default="human", pattern=r"^(human|agent)$")


class _PutPromptBody(BaseModel):
    """``PUT /v1/skills/{id}/versions/{v}/prompt`` request body.

    skill-authoring-ia Phase D-2 — edit ``SKILL.md`` (the prompt fragment)
    in place. Only the prompt body is sent; the new version inherits every
    other field (tool_names / supporting_files / required_models /
    lazy_load / …) from the base version, so editing the prompt never drops
    bundled scripts/references.
    """

    prompt_fragment: str = Field(min_length=1)


class _PatchStatusBody(BaseModel):
    """``PATCH /v1/skills/{id}`` request body.

    All fields are optional so admins can patch one knob at a time.
    Capability Uplift Sprint #4 (Mini-ADR U-30) extends this with
    ``pinned`` — operator's "do not Curator-touch" escape hatch. At
    least one of ``status`` / ``pinned`` must be set; an empty patch
    rejects with 422.
    """

    status: SkillStatus | None = None
    # Mini-ADR U-30. ``True`` opts the skill out of every Curator
    # transition forever (unless the admin un-pins it). ``False``
    # restores the default lifecycle. Stays nullable so the same
    # endpoint can carry just-a-status patches without touching pinned.
    pinned: bool | None = None


class _PutSupportingFileBody(BaseModel):
    """``PUT /v1/skills/{id}/versions/{v}/supporting-files/{path:path}`` body.

    Mini-ADR U-17 supporting-files API. Every mutation creates a new
    ``SkillVersion`` (D3 immutability), copying the prior version's
    other fields and replacing / adding the named file.
    """

    content: str = Field(min_length=0)  # base64 of raw bytes
    size: int = Field(ge=0)
    mime: str = Field(default="", max_length=128)


# Path-validation lists for the supporting-files single-file mutation API —
# REUSE the canonical U-18 ZIP-validator sets so the two never drift (a skill
# imported with ``.xsd`` files nested 6 dirs deep, e.g. anthropics/skills pptx,
# must also be readable/editable through this single-file API). Previously these
# were hand-duplicated + drifted (depth 3, no ``.xsd``) → "invalid supporting
# file path" on legitimately-imported nested files.
_SUPPORTING_FILE_EXT_ALLOWLIST: frozenset[str] = ALLOWED_EXTENSIONS
_SUPPORTING_FILE_TEXT_EXTS: frozenset[str] = TEXT_EXTENSIONS
_MAX_SUPPORTING_FILE_SIZE: int = 1 * 1024 * 1024  # 1 MB per file
_MAX_SUPPORTING_PATH_LEN: int = 256
_SUPPORTING_PATH_SEGMENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.\-]+$")


def _validate_supporting_file_path(path: str) -> str:
    """Mini-ADR U-18 path validator for the single-file API.

    Raises :class:`SkillPackageLayoutError` with a **generic** message
    on any violation — Oracle defense. The caller logs the real reason
    via audit.
    """
    if len(path) >= _MAX_SUPPORTING_PATH_LEN:
        raise SkillPackageLayoutError("invalid supporting file path")
    if "\\" in path or path.startswith("/") or ".." in path.split("/"):
        raise SkillPackageLayoutError("invalid supporting file path")
    segments = path.split("/")
    # Dir-depth cap mirrors the ZIP validator (``len(dirs) > MAX_PATH_DEPTH``);
    # segments = dirs + filename, so subtract one.
    if len(segments) - 1 > MAX_PATH_DEPTH:
        raise SkillPackageLayoutError("invalid supporting file path")
    for segment in segments:
        if not _SUPPORTING_PATH_SEGMENT_RE.fullmatch(segment):
            raise SkillPackageLayoutError("invalid supporting file path")
    ext = Path(path).suffix.lower()
    if ext not in _SUPPORTING_FILE_EXT_ALLOWLIST:
        raise SkillPackageLayoutError("invalid supporting file path")
    return path


def _get_skill_store(request: Request) -> SkillStore:
    return request.app.state.skill_store  # type: ignore[no-any-return]


def _get_skill_subscription_store(request: Request) -> TenantSkillSubscriptionStore:
    # Skill Marketplace Phase 1 — subscribe/unsubscribe markers (semantic A).
    return request.app.state.skill_subscription_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_object_store(request: Request) -> SkillAssetObjectStore | None:
    """The DURABLE object store for skill assets, or ``None``.

    Local copy of ``platform_skills._get_object_store`` (importing it here
    would be circular — ``platform_skills`` imports from this module).

    The lifespan sets ``app.state.skill_asset_store`` only when the object
    store backend is s3-compatible — the in-memory backend loses objects on
    restart, so externalizing skill bytes to it would corrupt skills. With
    ``None`` imports stay inline (tighter caps) and reads degrade cleanly.
    """
    return getattr(request.app.state, "skill_asset_store", None)


async def _invalidate_tenant_skills(request: Request, tenant_id: UUID) -> None:
    """Evict the tenant's built agents after a tenant-skill write (PR-E3b).

    Skill content is baked into ``BuiltAgent`` at build time (prompt
    fragments + seed files), so any write that changes what the build-time
    resolver returns — new skill (name-shadowing), new version, status flip,
    visibility flip — must drop the tenant's stale builds, or the edit stays
    invisible until the build-cache TTL expires. Local eviction first; the
    ``agent_build`` broadcast makes peer replicas do the same (the
    publisher's own handler re-running locally is harmless). ``getattr``
    guards test setups without a runtime/bus.
    """
    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is not None:
        runtime.invalidate_tenant(tenant_id)
    bus = getattr(request.app.state, "invalidation_bus", None)
    if bus is not None:
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(tenant_id)))


def _skill_dict(skill: Skill) -> dict[str, Any]:
    return {
        "id": str(skill.id),
        # W3 cross-tenant drilldown — the "*" aggregate list row needs its
        # owning tenant so the UI can deep-link with a concrete tenant_id
        # (list/detail share this serializer). NULL for platform skills.
        "tenant_id": str(skill.tenant_id) if skill.tenant_id is not None else None,
        "name": skill.name,
        "status": skill.status.value,
        "latest_version": skill.latest_version,
        "description": skill.description,
        "category": skill.category,
        # Stream X (X4 / X-1). Surface the entitlement tier so both the
        # platform CRUD responses and the X-6 tenant merged view carry it
        # (additive / backward-compatible).
        "required_tier": skill.required_tier.value,
        # Capability Uplift Sprint #4 (Mini-ADR U-25 / U-30). UI needs
        # these to render the 📌 pin icon + "distance to stale" hint
        # without a separate fetch.
        "pinned": skill.pinned,
        "last_used_at": (
            skill.last_used_at.isoformat() if skill.last_used_at is not None else None
        ),
        "state_changed_at": (
            skill.state_changed_at.isoformat() if skill.state_changed_at is not None else None
        ),
        # Stream SE (SE-8) — ownership / lineage so the admin governance
        # surface can render visibility / owner / fork source without a
        # second fetch. Additive / backward-compatible.
        "visibility": skill.visibility,
        "created_by_user_id": (
            str(skill.created_by_user_id) if skill.created_by_user_id is not None else None
        ),
        "created_by_agent_name": skill.created_by_agent_name,
        "forked_from": str(skill.forked_from) if skill.forked_from is not None else None,
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }


def _subscription_dict(record: TenantSkillSubscriptionRecord) -> dict[str, Any]:
    # Skill Marketplace Phase 1 — raw response (skills router is all-raw, no
    # success/data envelope).
    return {
        "id": str(record.id),
        "platform_skill_id": str(record.platform_skill_id),
        "enabled": record.enabled,
        "created_at": record.created_at.isoformat(),
        "created_by": record.created_by,
    }


def _version_dict(version: SkillVersion) -> dict[str, Any]:
    # supporting_files: metadata-only (path → {size, mime}); body is
    # base64 in the DB and can be megabytes — UI fetches one file at a
    # time via the GET supporting-files endpoint when the user clicks.
    files_meta: dict[str, dict[str, Any]] = {
        path: {"size": entry.size, "mime": entry.mime}
        for path, entry in version.supporting_files.items()
    }
    return {
        "id": str(version.id),
        "skill_id": str(version.skill_id),
        "version": version.version,
        "prompt_fragment": version.prompt_fragment,
        "tool_names": list(version.tool_names),
        "description": version.description,
        "category": version.category,
        "required_models": list(version.required_models),
        "authored_by": version.authored_by,
        "supporting_files": files_meta,
        "lazy_load": version.lazy_load,
        "high_risk": version.high_risk,
        # Stream SE (SE-8) — evolution provenance for the SkillDetail lineage
        # view. Additive / backward-compatible.
        "evolution_origin": version.evolution_origin,
        "distilled_from_trajectory_key": version.distilled_from_trajectory_key,
        "distilled_from_candidate_id": (
            str(version.distilled_from_candidate_id)
            if version.distilled_from_candidate_id is not None
            else None
        ),
        "evolution_round": version.evolution_round,
        "created_at": version.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# SE-8 owner gate — 租户内跨终端用户数据泄露修复(backlog task 6)
# ---------------------------------------------------------------------------


def _require_skill_owner_scope(skill: Skill, principal: Principal) -> None:
    """403 unless ``principal`` may access an ``agent_private`` skill.

    ``skill.created_by_user_id`` is a ``tenant_user`` id — the per-user
    AGENT that owns an agent-self-authored skill. Every caller reaching this
    router is a **member** (JWT) instead, a disjoint identity space from
    ``tenant_user`` — so "principal is the owner" can never be true here.
    The only carve-out is a tenant admin, mirroring the
    ``resolve_target_user_id`` precedent (``api/_user_scope.py``: no match →
    admin may still act, anyone else asking for someone else's resource is a
    403). ``tenant``-visibility skills are unaffected — this only gates the
    ``agent_private`` slice.
    """
    if skill.visibility == "agent_private" and not is_admin(principal):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SKILL_SCOPE_FORBIDDEN",
                "message": "this skill is agent-private; only a tenant admin may access it",
            },
        )


def build_skills_router() -> APIRouter:
    """Stream J.7a admin CRUD + ZIP import/export router."""
    router = APIRouter(prefix="/v1/skills", tags=["skills"], dependencies=[Depends(console_only())])

    @router.post("", response_model=None, dependencies=[Depends(require("manifest", "write"))])
    async def create_skill(
        body: _CreateSkillBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        try:
            skill = await store.create_skill(
                skill_id=uuid4(),
                tenant_id=tenant_id,
                name=body.name,
                description=body.description,
                category=body.category,
            )
        except DuplicateSkillError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"skill {body.name!r} already exists for this tenant",
            ) from exc

        # Even a bare draft changes resolution: a tenant row name-shadows the
        # platform library (R2), draft included.
        await _invalidate_tenant_skills(request, tenant_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_CREATE,
            resource_type="skill",
            resource_id=str(skill.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"name": skill.name, "category": skill.category},
        )
        return JSONResponse(status_code=201, content=_skill_dict(skill))

    @router.post(
        "/{skill_id}/versions",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def add_version(
        skill_id: UUID,
        body: _AddVersionBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")

        # Mini-ADR J-23 § 15.6 admin moderation.
        try:
            moderate_prompt_fragment(body.prompt_fragment)
            moderate_tool_names(body.tool_names)
            moderate_required_models(body.required_models)
        except ModerationError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        # Capability Uplift Sprint #3 — Mini-ADR U-21 write-time strict scan
        # on prompt_fragment (the JSON-API path is the third "content into
        # the system" surface alongside ZIP import + supporting-files API).
        findings = scan_for_threats(body.prompt_fragment, scope="strict")
        if findings:
            record_threat_pattern_hits(findings, scope="strict")
            record_skill_blocked(phase="supporting_file_api")
            await audit_emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.DENIED,
                trace_id=current_trace_id_hex(),
                details={
                    "finding_count": len(findings),
                    "findings": [
                        {"pattern_id": f.pattern_id, "category": f.category} for f in findings
                    ],
                    "source": "json_api",
                },
            )
            raise HTTPException(status_code=400, detail="invalid skill content")

        # Mini-ADR U-21 / U-24 — compute content_hash + high_risk at write
        # time. JSON-API path produces empty supporting_files (the path is
        # the legacy structured-create endpoint; ZIP / supporting-files API
        # produce non-empty ones).
        content_hash = compute_content_hash(body.prompt_fragment, {})
        high_risk = is_high_risk_skill_version(tool_names=body.tool_names, supporting_file_paths=[])

        # SE-8 owner gate — must run before the mutation, not just before the
        # response, so a denied caller cannot create a version as a side effect.
        target_skill = await store.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        if target_skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(target_skill, request.state.principal)

        try:
            version = await store.add_version(
                version_id=uuid4(),
                skill_id=skill_id,
                tenant_id=tenant_id,
                prompt_fragment=body.prompt_fragment,
                tool_names=body.tool_names,
                description=body.description,
                category=body.category,
                required_models=body.required_models,
                authored_by=body.authored_by,
                content_hash=content_hash,
                high_risk=high_risk,
            )
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="skill not found") from exc

        await _invalidate_tenant_skills(request, tenant_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_VERSION_CREATE,
            resource_type="skill",
            resource_id=str(skill_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "version": version.version,
                "tool_names": list(version.tool_names),
                "source": "json_api",
            },
        )
        return JSONResponse(status_code=201, content=_version_dict(version))

    @router.get(
        "/{skill_id}/versions/{version}/supporting-files/{file_path:path}",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def get_supporting_file(
        skill_id: UUID,
        version: int,
        file_path: str,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — admin-UI file read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Admin UI single-file content fetch (Mini-ADR U-20).

        ``_version_dict`` only returns supporting-file *metadata* (path,
        size, mime) to keep skill detail responses small. The UI fetches
        each file's base64 content lazily through this endpoint when the
        user clicks a file in the tree.

        Returns ``{"content": <base64>, "size": <int>, "mime": <str>}``.
        Skips U-21 context-scope re-scan on purpose — admin operators
        viewing through the UI must see the literal stored bytes
        (including substrings that would be blocked at agent runtime) so
        they can audit / triage threat-scanner findings. The drift hash
        is enforced at ``skill_view`` (agent path), not here (admin path).
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skills/{skill_id}/versions/{version}/supporting-files/{file_path}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )

        # U-18 path validation — same allowlist enforcement as the
        # mutation surfaces, so a probe of an invalid path returns 400
        # rather than 404 (consistent oracle).
        try:
            _validate_supporting_file_path(file_path)
        except SkillPackageLayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async with applied_scope(scope):
            row = await store.get_version_by_number(
                skill_id=skill_id, tenant_id=scope.tenant_id, version=version
            )
            if row is None:
                raise HTTPException(status_code=404, detail="skill version not found")
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
        # M-1 (backlog task 7) — fail-closed: a ``skill`` row that can't be
        # resolved for a version that just resolved is unreachable today (the
        # FK should prevent an orphan), but a silent skip-the-gate on
        # ``None`` doesn't match this file's own convention elsewhere
        # (``skill is None`` → 404 before proceeding).
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)
        entry = row.supporting_files.get(file_path)
        if entry is None:
            raise HTTPException(status_code=404, detail="supporting file not found")
        # Dual-read (skill-asset-store): inline rows return their stored
        # base64 verbatim; external rows fetch + digest-verify the object.
        content_b64 = entry.content
        if entry.is_external:
            try:
                raw = await fetch_supporting_file(entry, object_store=_get_object_store(request))
            except SkillAssetError as exc:
                raise HTTPException(
                    status_code=502, detail="supporting file asset unavailable"
                ) from exc
            content_b64 = base64.b64encode(raw).decode("ascii")
        return JSONResponse(
            status_code=200,
            content={
                "content": content_b64,
                "size": entry.size,
                "mime": entry.mime,
            },
        )

    @router.put(
        "/{skill_id}/versions/{version}/supporting-files/{file_path:path}",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def put_supporting_file(
        skill_id: UUID,
        version: int,
        file_path: str,
        body: _PutSupportingFileBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Mini-ADR U-17 — add or replace a single supporting file.

        Creates a **new SkillVersion** that mirrors ``version``'s fields
        plus the new/replaced file. Runs U-18 path validation + U-21
        write-time threat scan + U-24 high_risk recompute.
        """
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")

        # U-18 path validation
        try:
            _validate_supporting_file_path(file_path)
        except SkillPackageLayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Size cap on raw bytes (declared) — defense in depth alongside
        # the JSONB total-size CHECK constraint on the table.
        if body.size > _MAX_SUPPORTING_FILE_SIZE:
            raise HTTPException(status_code=400, detail="invalid supporting file path")

        prior = await store.get_version_by_number(
            skill_id=skill_id, tenant_id=tenant_id, version=version
        )
        if prior is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        skill = await store.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        # M-1 (backlog task 7) — fail-closed (see ``get_supporting_file`` above).
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)

        # Validate base64 + size invariant (declared `size` must match)
        try:
            raw = base64.b64decode(body.content, validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="invalid supporting file path") from exc
        if len(raw) != body.size:
            raise HTTPException(status_code=400, detail="invalid supporting file path")

        # U-21 write-time strict scan (text extensions only).
        ext = Path(file_path).suffix.lower()
        if ext in _SUPPORTING_FILE_TEXT_EXTS:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                # Declared text extension but content isn't UTF-8 —
                # suspect (binary disguised as text). Treat as scan
                # finding equivalent for audit purposes.
                record_skill_blocked(phase="supporting_file_api")
                await audit_emit(
                    audit,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                    resource_type="skill_supporting_file",
                    resource_id=f"{skill_id}/{version}/{file_path}",
                    result=AuditResult.DENIED,
                    trace_id=current_trace_id_hex(),
                    details={"reason": "text_extension_binary_content"},
                )
                raise HTTPException(status_code=400, detail="invalid supporting file path") from exc
            findings = scan_for_threats(text, scope="strict")
            if findings:
                record_threat_pattern_hits(findings, scope="strict")
                record_skill_blocked(phase="supporting_file_api")
                await audit_emit(
                    audit,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                    resource_type="skill_supporting_file",
                    resource_id=f"{skill_id}/{version}/{file_path}",
                    result=AuditResult.DENIED,
                    trace_id=current_trace_id_hex(),
                    details={
                        "finding_count": len(findings),
                        "findings": [
                            {"pattern_id": f.pattern_id, "category": f.category} for f in findings
                        ],
                    },
                )
                raise HTTPException(status_code=400, detail="invalid supporting file path")

        # Build merged supporting_files map. ``supporting_files_to_jsonable``
        # already serializes deterministically (sorted keys).
        merged = supporting_files_to_jsonable(prior.supporting_files)
        merged[file_path] = {
            "content": body.content,
            "size": body.size,
            "mime": body.mime,
        }

        new_paths = list(merged.keys())
        new_high_risk = is_high_risk_skill_version(
            tool_names=prior.tool_names, supporting_file_paths=new_paths
        )
        new_hash = compute_content_hash(prior.prompt_fragment, merged)

        new_version = await store.add_version(
            version_id=uuid4(),
            skill_id=skill_id,
            tenant_id=tenant_id,
            prompt_fragment=prior.prompt_fragment,
            tool_names=list(prior.tool_names),
            description=prior.description,
            category=prior.category,
            required_models=list(prior.required_models),
            authored_by=prior.authored_by,
            supporting_files=merged,
            lazy_load=prior.lazy_load,
            content_hash=new_hash,
            high_risk=new_high_risk,
        )

        await _invalidate_tenant_skills(request, tenant_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_SUPPORTING_FILE_UPLOADED,
            resource_type="skill_supporting_file",
            resource_id=f"{skill_id}/{new_version.version}/{file_path}",
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "from_version": prior.version,
                "to_version": new_version.version,
                "path": file_path,
                "size": body.size,
                "high_risk_after": new_high_risk,
            },
        )
        return JSONResponse(status_code=201, content=_version_dict(new_version))

    @router.delete(
        "/{skill_id}/versions/{version}/supporting-files/{file_path:path}",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def delete_supporting_file(
        skill_id: UUID,
        version: int,
        file_path: str,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Mini-ADR U-17 — remove a single supporting file (new version)."""
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")

        try:
            _validate_supporting_file_path(file_path)
        except SkillPackageLayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        prior = await store.get_version_by_number(
            skill_id=skill_id, tenant_id=tenant_id, version=version
        )
        if prior is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        skill = await store.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        # M-1 (backlog task 7) — fail-closed (see ``get_supporting_file`` above).
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)
        if file_path not in prior.supporting_files:
            raise HTTPException(status_code=404, detail="supporting file not found")

        merged = supporting_files_to_jsonable(prior.supporting_files)
        merged.pop(file_path)
        new_paths = list(merged.keys())
        new_high_risk = is_high_risk_skill_version(
            tool_names=prior.tool_names, supporting_file_paths=new_paths
        )
        new_hash = compute_content_hash(prior.prompt_fragment, merged)

        new_version = await store.add_version(
            version_id=uuid4(),
            skill_id=skill_id,
            tenant_id=tenant_id,
            prompt_fragment=prior.prompt_fragment,
            tool_names=list(prior.tool_names),
            description=prior.description,
            category=prior.category,
            required_models=list(prior.required_models),
            authored_by=prior.authored_by,
            supporting_files=merged,
            lazy_load=prior.lazy_load,
            content_hash=new_hash,
            high_risk=new_high_risk,
        )

        await _invalidate_tenant_skills(request, tenant_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_SUPPORTING_FILE_REMOVED,
            resource_type="skill_supporting_file",
            resource_id=f"{skill_id}/{new_version.version}/{file_path}",
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "from_version": prior.version,
                "to_version": new_version.version,
                "path": file_path,
                "high_risk_after": new_high_risk,
            },
        )
        return JSONResponse(status_code=200, content=_version_dict(new_version))

    @router.put(
        "/{skill_id}/versions/{version}/prompt",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def put_prompt(
        skill_id: UUID,
        version: int,
        body: _PutPromptBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Edit ``SKILL.md`` (prompt_fragment) → new version (Phase D-2).

        Inherits all other fields from ``version`` (mirrors
        ``put_supporting_file``), so editing the prompt never drops bundled
        supporting files. Runs the same moderation + U-21 strict scan +
        U-24 high_risk recompute as the JSON-API add-version path.
        """
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")

        try:
            moderate_prompt_fragment(body.prompt_fragment)
        except ModerationError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        prior = await store.get_version_by_number(
            skill_id=skill_id, tenant_id=tenant_id, version=version
        )
        if prior is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        skill = await store.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        # M-1 (backlog task 7) — fail-closed (see ``get_supporting_file`` above).
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)

        findings = scan_for_threats(body.prompt_fragment, scope="strict")
        if findings:
            record_threat_pattern_hits(findings, scope="strict")
            record_skill_blocked(phase="supporting_file_api")
            await audit_emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.DENIED,
                trace_id=current_trace_id_hex(),
                details={
                    "finding_count": len(findings),
                    "findings": [
                        {"pattern_id": f.pattern_id, "category": f.category} for f in findings
                    ],
                    "source": "prompt_edit",
                },
            )
            raise HTTPException(status_code=400, detail="invalid skill content")

        merged = supporting_files_to_jsonable(prior.supporting_files)
        new_high_risk = is_high_risk_skill_version(
            tool_names=prior.tool_names, supporting_file_paths=list(merged.keys())
        )
        new_hash = compute_content_hash(body.prompt_fragment, merged)

        new_version = await store.add_version(
            version_id=uuid4(),
            skill_id=skill_id,
            tenant_id=tenant_id,
            prompt_fragment=body.prompt_fragment,
            tool_names=list(prior.tool_names),
            description=prior.description,
            category=prior.category,
            required_models=list(prior.required_models),
            authored_by=prior.authored_by,
            supporting_files=merged,
            lazy_load=prior.lazy_load,
            content_hash=new_hash,
            high_risk=new_high_risk,
        )

        await _invalidate_tenant_skills(request, tenant_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_VERSION_CREATE,
            resource_type="skill",
            resource_id=str(skill_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "from_version": prior.version,
                "to_version": new_version.version,
                "source": "prompt_edit",
            },
        )
        return JSONResponse(status_code=201, content=_version_dict(new_version))

    @router.patch(
        "/{skill_id}", response_model=None, dependencies=[Depends(require("manifest", "write"))]
    )
    async def patch_status(
        skill_id: UUID,
        body: _PatchStatusBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        if body.status is None and body.pinned is None:
            raise HTTPException(
                status_code=422,
                detail="patch body must set at least one of: status, pinned",
            )
        prior = await store.get_skill(skill_id=skill_id, tenant_id=tenant_id)
        if prior is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(prior, request.state.principal)

        # ── Capability Uplift Sprint #4 (Mini-ADR U-30) ──────────────
        # Pin a high-risk skill = handing it a free pass to skip
        # Curator review forever. Combined with M1-K J.7b-1 agent-self-
        # authored skills, that's an attack vector — agent creates a
        # high-risk skill, asks the platform to pin it, and from then
        # on the Curator can't auto-archive it. Refuse the combination
        # unless the caller is admin/system_admin; pin defaults to NO
        # for high-risk rows.
        if body.pinned is True and prior.latest_version > 0:
            latest_for_pin = await store.get_version_by_number(
                skill_id=skill_id,
                tenant_id=tenant_id,
                version=prior.latest_version,
            )
            if latest_for_pin is not None and latest_for_pin.high_risk:
                principal_for_pin = getattr(request.state, "principal", None)
                roles_for_pin = (
                    _collect_roles(principal_for_pin) if principal_for_pin is not None else set()
                )
                if Role.ADMIN not in roles_for_pin and Role.SYSTEM_ADMIN not in roles_for_pin:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "pinning a high-risk skill requires tenant admin or system admin role"
                        ),
                    )

        # ── Capability Uplift Sprint #3 (Mini-ADR U-24) ──────────────
        # High-risk publish gate: when activating, look up the version
        # that's becoming live and check its ``high_risk`` flag. If
        # high-risk + caller is not ADMIN / SYSTEM_ADMIN → 403 + audit.
        # M0 reality: all skill mutations are admin-only so this almost
        # never fires; the gate activates with M1-K J.7b-1 agent-self-
        # authored skills.
        if body.status == SkillStatus.ACTIVE and prior.latest_version > 0:
            latest = await store.get_version_by_number(
                skill_id=skill_id,
                tenant_id=tenant_id,
                version=prior.latest_version,
            )
            if latest is not None and latest.high_risk:
                principal = getattr(request.state, "principal", None)
                roles = _collect_roles(principal) if principal is not None else set()
                if Role.ADMIN not in roles and Role.SYSTEM_ADMIN not in roles:
                    record_skill_high_risk_event(event="activation_blocked")
                    await audit_emit(
                        audit,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action=AuditAction.SKILL_HIGH_RISK_ACTIVATION_BLOCKED,
                        resource_type="skill",
                        resource_id=str(skill_id),
                        result=AuditResult.DENIED,
                        trace_id=current_trace_id_hex(),
                        details={
                            "version": latest.version,
                            "tool_names": list(latest.tool_names),
                            "has_scripts_subdir": any(
                                p.startswith("scripts/") for p in latest.supporting_files
                            ),
                        },
                    )
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "high-risk skill requires tenant admin or system admin role to activate"
                        ),
                    )

        updated = prior
        if body.status is not None:
            try:
                updated = await store.set_status(
                    skill_id=skill_id, tenant_id=tenant_id, status=body.status
                )
            except SkillNotFoundError as exc:
                raise HTTPException(status_code=404, detail="skill not found") from exc

            await audit_emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.SKILL_STATUS_CHANGE,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details={"from": prior.status.value, "to": updated.status.value},
            )
            # Live pilot finding #8 — a status flip changes the auto-attach
            # set (SE-A42) and the resolver's active-version answer without a
            # spec-version bump, so the BuiltAgent cache would serve stale
            # builds until a restart. PR-E3b: unified helper adds the bus
            # broadcast so peer replicas drop theirs too. The pinned branch
            # below stays un-wired on purpose — ``pinned`` is consumed only
            # by the skill Curator (skip list) and the UI, never by
            # ``make_skill_resolver`` / the agent build.
            if updated.status is not prior.status:
                await _invalidate_tenant_skills(request, tenant_id)

        # Sprint #4 (Mini-ADR U-30) — pin / unpin. Distinct audit
        # actions so SecOps can filter on either side.
        if body.pinned is not None and body.pinned != updated.pinned:
            try:
                updated = await store.set_pinned(
                    skill_id=skill_id, tenant_id=tenant_id, pinned=body.pinned
                )
            except SkillNotFoundError as exc:
                raise HTTPException(status_code=404, detail="skill not found") from exc
            await audit_emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=(AuditAction.SKILL_PINNED if body.pinned else AuditAction.SKILL_UNPINNED),
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details={"pinned": body.pinned},
            )

        # If we just activated a high-risk skill with the right role,
        # leave a positive audit + metric trail (Mini-ADR U-24).
        if body.status == SkillStatus.ACTIVE and prior.latest_version > 0:
            latest_after = await store.get_version_by_number(
                skill_id=skill_id,
                tenant_id=tenant_id,
                version=prior.latest_version,
            )
            if latest_after is not None and latest_after.high_risk:
                record_skill_high_risk_event(event="activated")
                await audit_emit(
                    audit,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=AuditAction.SKILL_HIGH_RISK_ACTIVATED,
                    resource_type="skill",
                    resource_id=str(skill_id),
                    result=AuditResult.SUCCESS,
                    trace_id=current_trace_id_hex(),
                    details={
                        "version": latest_after.version,
                        "tool_names": list(latest_after.tool_names),
                    },
                )

        return JSONResponse(status_code=200, content=_skill_dict(updated))

    @router.get("", response_model=None, dependencies=[Depends(require("manifest", "read"))])
    async def list_skills(
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        sub_store: Annotated[TenantSkillSubscriptionStore, Depends(_get_skill_subscription_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        status: Annotated[SkillStatus | None, Query()] = None,
        category: Annotated[str | None, Query()] = None,
        cursor: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
        # Stream SE (SE-8) — agent-self-authored slice for the governance
        # surface (e.g. "this user's agent_private skills"). Single-tenant only.
        visibility: Annotated[SkillVisibility | None, Query()] = None,
        created_by_user_id: Annotated[UUID | None, Query()] = None,
        # Stream H.6 (Mini-ADR H-11) — skills authored by a given agent
        # (feeds the per-agent Skills tab).
        created_by_agent_name: Annotated[str | None, Query(min_length=1)] = None,
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skills",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        # SE-8 owner gate — a non-admin caller may never see ``agent_private``
        # rows. CrossTenant (``tenant_id=*``) is system_admin-only, and
        # ``is_admin`` is True for system_admin too, so this only narrows the
        # SingleTenant branch below. Filtered at the store query layer (WHERE
        # clause, before the cursor page is cut) — never post-fetch — so a
        # filtered page never comes back short.
        principal_is_admin = is_admin(request.state.principal)
        # Stream X (X-6) merged view — the tenant's own skills ("items")
        # plus the platform-curated NULL-tenant library it can see
        # ("platform_items"), each tagged with ``source`` + ``entitled``.
        platform_items: list[dict[str, Any]] = []
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                rows, next_cursor = await store.list_skills_all_tenants(
                    status=status,
                    category=category,
                    created_by_agent_name=created_by_agent_name,
                    cursor=cursor,
                    limit=limit,
                )
            else:
                if not principal_is_admin and visibility == "agent_private":
                    # Explicit ask for a slice this caller may never see →
                    # empty page, not a silent narrow to "tenant" and not a
                    # 403 (a 403 here would break the list endpoint for the
                    # non-admin common case; see brief §3).
                    effective_visibility: SkillVisibility | None = None
                    rows, next_cursor = [], None
                else:
                    effective_visibility = visibility if principal_is_admin else "tenant"
                    rows, next_cursor = await store.list_skills(
                        tenant_id=scope.tenant_id,
                        status=status,
                        category=category,
                        visibility=effective_visibility,
                        created_by_user_id=created_by_user_id,
                        created_by_agent_name=created_by_agent_name,
                        cursor=cursor,
                        limit=limit,
                    )
                # Resolve the tenant's plan under its own RLS scope
                # (``tenant_config`` is a tenant-scoped table); an
                # unconfigured tenant is treated as FREE.
                try:
                    plan = (
                        await request.app.state.tenant_config_service.get(tenant_id=scope.tenant_id)
                    ).plan
                except TenantConfigNotConfiguredError:
                    plan = TenantPlan.FREE
                # Skill Marketplace Phase 2 — the tenant's active subscription
                # set (enabled rows only) drives the merged-view ``subscribed``
                # flag. Read in the tenant RLS scope (subscription table is
                # tenant-scoped); semantic A, so this never affects binding.
                subs = await sub_store.list_for_tenant(tenant_id=scope.tenant_id)
                subscribed_ids = {s.platform_skill_id for s in subs if s.enabled}
                # Only ACTIVE platform skills are bindable. The library is
                # small; a single 200 cap is acceptable here.
                async with bypass_rls_session():
                    p_rows, _ = await store.list_platform_skills(
                        status=SkillStatus.ACTIVE, limit=200
                    )
                # Name-shadowing (R2): a tenant skill of the same name hides the
                # platform one. One batch lookup in tenant scope (outside the
                # bypass block above) — the platform library can be large, so a
                # per-row ``get_skill_by_name`` would be an N+1.
                shadowed = await store.shadowed_skill_names(
                    tenant_id=scope.tenant_id, names=[p.name for p in p_rows]
                )
                for p in p_rows:
                    if p.name in shadowed:
                        continue
                    entry = _skill_dict(p)
                    entry["source"] = "platform"
                    # Show both entitled and not-entitled rows (UI renders a
                    # lock badge on the latter) — do not filter by tier.
                    entry["entitled"] = tier_satisfies(plan, p.required_tier)
                    entry["subscribed"] = p.id in subscribed_ids
                    platform_items.append(entry)

        items: list[dict[str, Any]] = []
        for r in rows:
            entry = _skill_dict(r)
            # In the cross-tenant (system_admin ``tenant_id=*``) path
            # ``list_skills_all_tenants`` has no tenant filter, so it also
            # returns NULL-tenant platform rows — label by ``tenant_id`` so
            # those aren't mislabeled ``tenant``. The normal tenant path only
            # ever sees its own (non-NULL) rows, so this stays ``tenant`` there.
            entry["source"] = "platform" if r.tenant_id is None else "tenant"
            entry["entitled"] = True
            items.append(entry)

        return JSONResponse(
            status_code=200,
            content={
                "items": items,
                "platform_items": platform_items,
                "next_cursor": str(next_cursor) if next_cursor is not None else None,
                "cross_tenant": isinstance(scope, CrossTenant),
            },
        )

    def _require_subscribe_role(request: Request) -> None:
        # Skill Marketplace Phase 1 — selecting a platform skill is a tenant
        # configuration action (admin / operator / system_admin). Inline role
        # gate follows the skills.py convention (no require() / rbac.py matrix).
        principal = getattr(request.state, "principal", None)
        roles = _collect_roles(principal) if principal is not None else set()
        if not ({Role.ADMIN, Role.OPERATOR, Role.SYSTEM_ADMIN} & roles):
            raise HTTPException(
                status_code=403,
                detail="subscribing to a platform skill requires admin or operator role",
            )

    @router.post(
        "/{platform_skill_id}/subscribe",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def subscribe_skill(
        platform_skill_id: UUID,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        sub_store: Annotated[TenantSkillSubscriptionStore, Depends(_get_skill_subscription_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Subscribe the tenant to a platform skill (semantic A: accounting/UX
        marker, does NOT gate the runtime resolver). Idempotent — re-subscribing
        a soft-cancelled row re-enables it."""
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        _require_subscribe_role(request)

        # Target must be an ACTIVE platform (NULL-tenant) skill. Platform rows
        # are invisible under tenant RLS, so read inside a bypass session.
        async with bypass_rls_session():
            target = await store.get_platform_skill(skill_id=platform_skill_id)
        if target is None or target.status != SkillStatus.ACTIVE:
            raise HTTPException(status_code=404, detail="platform skill not found or not active")

        record = await sub_store.subscribe(
            tenant_id=tenant_id,
            platform_skill_id=platform_skill_id,
            created_by=actor_id,
        )
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_SUBSCRIBED,
            resource_type="skill",
            resource_id=str(platform_skill_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"name": target.name},
        )
        return JSONResponse(status_code=200, content=_subscription_dict(record))

    @router.delete(
        "/{platform_skill_id}/subscribe",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def unsubscribe_skill(
        platform_skill_id: UUID,
        request: Request,
        sub_store: Annotated[TenantSkillSubscriptionStore, Depends(_get_skill_subscription_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Cancel a subscription via soft-stop (``enabled=false``) — the row is
        kept for the audit trail; re-subscribing flips it back on."""
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        _require_subscribe_role(request)

        try:
            record = await sub_store.set_enabled(
                tenant_id=tenant_id,
                platform_skill_id=platform_skill_id,
                enabled=False,
            )
        except TenantSkillSubscriptionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="subscription not found") from exc
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_UNSUBSCRIBED,
            resource_type="skill",
            resource_id=str(platform_skill_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
        )
        return JSONResponse(status_code=200, content=_subscription_dict(record))

    @router.get(
        "/{skill_id}", response_model=None, dependencies=[Depends(require("manifest", "read"))]
    )
    async def get_skill(
        skill_id: UUID,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's skill; "*" is meaningless (one owning tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skills/{skill_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)
        return JSONResponse(status_code=200, content=_skill_dict(skill))

    @router.get(
        "/{skill_id}/versions",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_versions(
        skill_id: UUID,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skills/{skill_id}/versions",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
            if skill is None:
                raise HTTPException(status_code=404, detail="skill not found")
            _require_skill_owner_scope(skill, request.state.principal)
            versions = await store.list_versions(skill_id=skill_id, tenant_id=scope.tenant_id)
        return JSONResponse(
            status_code=200, content={"items": [_version_dict(v) for v in versions]}
        )

    @router.get(
        "/{skill_id}/versions/{version_number}",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def get_version(
        skill_id: UUID,
        version_number: int,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skills/{skill_id}/versions/{version_number}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            version = await store.get_version_by_number(
                skill_id=skill_id, tenant_id=scope.tenant_id, version=version_number
            )
            if version is None:
                raise HTTPException(status_code=404, detail="skill version not found")
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
        # M-1 (backlog task 7) — fail-closed (see ``get_supporting_file`` above).
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)
        return JSONResponse(status_code=200, content=_version_dict(version))

    @router.post(
        "/import", response_model=None, dependencies=[Depends(require("manifest", "write"))]
    )
    async def import_skill(
        request: Request,
        file: Annotated[UploadFile, File()],
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Multipart ``.skill`` ZIP — create skill (if absent) + first
        version, OR add a version to an existing skill of the same name."""
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        blob = await file.read()
        try:
            payload = parse_skill_zip(blob)
        except SkillZipError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Validate name against the same regex AgentSpec.skills uses,
        # so a bad-name ZIP cannot create an unreferenceable skill.
        import re

        if not re.fullmatch(r"^[a-z][a-z0-9_-]{0,63}$", payload.name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"skill name {payload.name!r} fails validation "
                    f"({SKILL_REF_PATTERN} without @version suffix)"
                ),
            )

        # Moderation gate before any DB write.
        try:
            moderate_prompt_fragment(payload.prompt_fragment)
            moderate_tool_names(payload.tool_names)
            moderate_required_models(payload.required_models)
        except ModerationError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        existing = await store.get_skill_by_name(tenant_id=tenant_id, name=payload.name)
        # SE-8 owner gate (backlog task 7, C-2) — a name collision with an
        # ``agent_private`` skill must not be resolved silently: the
        # idempotency-hit branch below would echo the victim's
        # ``created_by_user_id`` / ``visibility``, and the fall-through would
        # append the caller's ZIP content as a new version on the victim's
        # skill. Gate immediately after resolution, before either the read
        # (idempotency response) or the write (``add_version``) below.
        if existing is not None:
            _require_skill_owner_scope(existing, request.state.principal)

        # OFFICE-3 idempotency: if the latest version already carries this exact
        # content_hash, the re-import is a no-op — return it (200, created=False)
        # rather than churning an identical duplicate version.
        if existing is not None and existing.latest_version > 0:
            latest = await store.get_version_by_number(
                skill_id=existing.id, tenant_id=tenant_id, version=existing.latest_version
            )
            if latest is not None and latest.content_hash == payload.content_hash:
                return JSONResponse(
                    status_code=200,
                    content={
                        "skill": _skill_dict(existing),
                        "version": _version_dict(latest),
                        "created": False,
                    },
                )

        if existing is None:
            try:
                existing = await store.create_skill(
                    skill_id=uuid4(),
                    tenant_id=tenant_id,
                    name=payload.name,
                    description=payload.description,
                    category=payload.category,
                )
            except DuplicateSkillError as exc:
                # Race — another import won the create; resolve + add version.
                logger.info("skills.import_race name=%s", payload.name)
                existing = await store.get_skill_by_name(tenant_id=tenant_id, name=payload.name)
                if existing is None:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                # SE-8 owner gate (backlog task 7, C-2) — this endpoint never
                # creates ``agent_private`` rows itself, but re-check anyway:
                # this is a second, independent resolution of ``existing``
                # (the initial ``existing is not None`` gate above only
                # covers the first ``get_skill_by_name`` call) and it also
                # flows straight into ``add_version`` below.
                _require_skill_owner_scope(existing, request.state.principal)
            await audit_emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.SKILL_CREATE,
                resource_type="skill",
                resource_id=str(existing.id),
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details={
                    "name": existing.name,
                    "category": existing.category,
                    "source": "zip_import",
                },
            )

        # PR B latent bug fix (PR C): the ZIP import path was previously
        # dropping ``supporting_files`` / ``lazy_load`` / ``content_hash`` /
        # ``high_risk`` — fields ``parse_skill_zip`` already computed but
        # nothing forwarded to ``add_version``. Without them, imported
        # skills had empty file trees in the Admin UI and the U-21 drift
        # check would fire on every read.
        version = await store.add_version(
            version_id=uuid4(),
            skill_id=existing.id,
            tenant_id=tenant_id,
            prompt_fragment=payload.prompt_fragment,
            tool_names=payload.tool_names,
            description=payload.description,
            category=payload.category,
            required_models=payload.required_models,
            authored_by="human",
            supporting_files=supporting_files_to_jsonable(payload.supporting_files),
            lazy_load=payload.lazy_load,
            content_hash=payload.content_hash,
            high_risk=payload.high_risk,
        )
        # The idempotency hit above (identical content_hash) returned before
        # any write — only this write path invalidates.
        await _invalidate_tenant_skills(request, tenant_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_VERSION_CREATE,
            resource_type="skill",
            resource_id=str(existing.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "version": version.version,
                "tool_names": list(version.tool_names),
                "source": "zip_import",
            },
        )
        return JSONResponse(
            status_code=201,
            content={
                "skill": _skill_dict(existing),
                "version": _version_dict(version),
                "created": True,
            },
        )

    @router.get(
        "/{skill_id}/versions/{version_number}/export",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def export_version(
        skill_id: UUID,
        version_number: int,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — download is a read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skills/{skill_id}/versions/{version_number}/export",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            version = await store.get_version_by_number(
                skill_id=skill_id, tenant_id=scope.tenant_id, version=version_number
            )
            if version is None:
                raise HTTPException(status_code=404, detail="skill version not found")
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        _require_skill_owner_scope(skill, request.state.principal)
        # Dual-read (skill-asset-store): inflate external entries back to the
        # inline shape so the exported ZIP carries real bytes — a round-trip
        # (export → import) stays lossless either way.
        supporting_files = version.supporting_files
        if any(sf.is_external for sf in supporting_files.values()):
            try:
                raw_map = await fetch_supporting_files(
                    supporting_files, object_store=_get_object_store(request)
                )
            except SkillAssetError as exc:
                raise HTTPException(
                    status_code=502, detail="supporting file assets unavailable"
                ) from exc
            supporting_files = {
                path: SkillSupportingFile(
                    content=base64.b64encode(raw_map[path]).decode("ascii"),
                    size=sf.size,
                    mime=sf.mime,
                )
                for path, sf in supporting_files.items()
            }
        blob = build_skill_zip(
            name=skill.name,
            description=version.description,
            category=version.category,
            required_models=version.required_models,
            prompt_fragment=version.prompt_fragment,
            tool_names=version.tool_names,
            supporting_files=supporting_files,
            # Round-trip the disclosure mode so an export→re-import keeps the
            # skill lazy/eager as authored (now that lazy is the default, an
            # omitted ``lazy`` would silently re-import a lazy skill as eager).
            lazy=version.lazy_load,
            version=version.version,
        )
        return Response(
            content=blob,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{skill.name}-v{version.version}.skill"'
                )
            },
        )

    return router
