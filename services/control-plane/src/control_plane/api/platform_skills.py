"""``/v1/platform/skills`` — Stream X (X4) platform skill library CRUD.

system_admin-only CRUD over the platform-curated **NULL-tenant** skill
library that the X-6 merged ``GET /v1/skills`` view surfaces to tenants.
Every handler:

* resolves the principal from ``request.state.principal`` (skills have no
  RBAC resource, so there is no ``require(...)`` dependency to lean on) and
  re-checks ``principal.is_system_admin`` inline via
  the shared ``platform_only()`` dependency — same 403 ``PLATFORM_SCOPE_FORBIDDEN`` shape
  as ``platform_config.py`` / ``mcp_catalog.py``;
* drives every store call inside ``bypass_rls_session()`` because the rows
  are tenant-less and the RLS policy would otherwise hide them from a
  normally-scoped session (the W-8 trap);
* tags audit details with ``{"scope": "platform", ...}`` (Mini-ADR X-7),
  reusing the existing ``SKILL_*`` audit actions — never a new ``AuditAction``
  value (avoids the protocol+control-plane double-Literal drift), and
  **never** the skill ``prompt_fragment`` body (metadata only).
"""

from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from control_plane.api import _skill_github
from control_plane.api._authz import platform_only
from control_plane.api._skill_moderation import (
    ModerationError,
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_runtime import classify_skill_runtime
from control_plane.api._skill_zip import (
    ALLOWED_EXTENSIONS,
    MAX_ENTRIES,
    MAX_ENTRIES_ASSET,
    MAX_FILE_BYTES,
    MAX_FILE_BYTES_ASSET,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_BYTES_ASSET,
    SkillPackageError,
    SkillZipError,
    build_skill_zip,
    parse_skill_zip,
)
from control_plane.api.skills import (
    _MAX_SUPPORTING_FILE_SIZE,
    _SUPPORTING_FILE_TEXT_EXTS,
    _get_audit,
    _get_skill_store,
    _PutSupportingFileBody,
    _skill_dict,
    _validate_supporting_file_path,
    _version_dict,
)
from control_plane.audit import emit as audit_emit
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.common.uplift_metrics import (
    record_skill_blocked,
    record_threat_pattern_hits,
)
from expert_work.persistence import (
    DuplicateSkillError,
    SkillNotFoundError,
    SkillStore,
)
from expert_work.protocol import (
    AuditAction,
    AuditResult,
    Principal,
    SkillStatus,
    TenantPlan,
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
    externalize_supporting_files,
    fetch_supporting_file,
    fetch_supporting_files,
)

#: Hard cap on skills imported in one batch request. Bounds the per-request DB
#: write fan-out; a repo with more skills than this is imported in chunks.
_MAX_BATCH_SKILLS = 100

#: Pre-seeded platform-skill categories. The ``/categories`` endpoint offers
#: these before any skill is labelled so the picker is never empty on a fresh
#: install; operators may still free-type new categories. Stored verbatim (the
#: value is a cross-tenant operational label, not translated UI copy), so seeds
#: and hand-typed values are the same kind of string and never split a bucket.
SEED_PLATFORM_CATEGORIES: tuple[str, ...] = (
    "研发",
    "设计",
    "写作",
    "数据",
    "办公",
    "医疗",
    "效率",
    "通用",
)


#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform skill library"


class _CreatePlatformSkillBody(BaseModel):
    """``POST /v1/platform/skills`` request body."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(default="", max_length=1024)
    category: str | None = Field(default=None, max_length=64)
    required_tier: TenantPlan = TenantPlan.FREE


class _AddPlatformVersionBody(BaseModel):
    """``POST /v1/platform/skills/{id}/versions`` request body."""

    prompt_fragment: str = Field(min_length=1)
    tool_names: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=1024)
    category: str | None = Field(default=None, max_length=64)
    required_models: list[str] = Field(default_factory=list)
    authored_by: str = Field(default="human", pattern=r"^(human|agent)$")


class _PutPlatformPromptBody(BaseModel):
    """``PUT /v1/platform/skills/{id}/versions/{v}/prompt`` body — edit
    ``SKILL.md`` in place; new version inherits all other fields
    (skill-authoring-ia Phase D-2)."""

    prompt_fragment: str = Field(min_length=1)


class _ImportFromGithubBody(BaseModel):
    """``POST /v1/platform/skills/import-from-github`` request body.

    ``source``: ``owner/repo`` shorthand, a ``github.com`` URL, or a
    ``skills.sh`` URL (the latter also supplies ``skill``). ``skill``: which
    skill folder to pick in a multi-skill repo (the ``npx skills --skill <name>``
    convention). ``ref``: branch/tag/SHA (default: the repo's default branch).
    """

    source: str = Field(min_length=1, max_length=512)
    skill: str | None = Field(default=None, max_length=128)
    ref: str | None = Field(default=None, max_length=256)


class _ListGithubSkillsBody(BaseModel):
    """``POST /v1/platform/skills/list-github-skills`` request body.

    Discover what skills a source contains WITHOUT importing — drives the
    import dialog's picker so the operator sees the available skills as soon as
    a source is entered (no import-to-discover round-trip). Same ``source`` /
    ``ref`` semantics as the import endpoint; no ``skill`` selector.
    """

    source: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=256)


class _BatchImportFromGithubBody(BaseModel):
    """``POST /v1/platform/skills/import-from-github/batch`` request body.

    Like the single import but ``skills`` is a list of folder paths/basenames to
    import in one pass. The repo archive is downloaded once; each skill is
    ingested independently so one bad skill (failed scan / invalid frontmatter)
    doesn't abort the batch (partial success).
    """

    source: str = Field(min_length=1, max_length=512)
    skills: list[str] = Field(min_length=1, max_length=_MAX_BATCH_SKILLS)
    ref: str | None = Field(default=None, max_length=256)


class _PatchPlatformSkillBody(BaseModel):
    """``PATCH /v1/platform/skills/{id}`` request body.

    ``status`` / ``pinned`` are lifecycle controls; ``category`` is the
    organizing label (empty string clears it). ``required_tier`` is still
    create-time only.
    """

    status: SkillStatus | None = None
    pinned: bool | None = None
    category: str | None = Field(default=None, max_length=64)


class _BatchFilter(BaseModel):
    """Selector for "apply to ALL matching" bulk edits (server-side, across
    pages). Same fields as the list query; AND-combined."""

    status: SkillStatus | None = None
    category: str | None = Field(default=None, max_length=128)
    q: str | None = Field(default=None, max_length=200)


class _BatchPlatformSkillsBody(BaseModel):
    """``POST /v1/platform/skills/batch`` request body.

    The patch (``set_status`` and/or ``set_pinned`` and/or ``set_category``)
    is applied to exactly one selector: ``ids`` (the page's checkbox
    selection) OR ``filter`` (every NULL-tenant skill matching it). Validated
    in the handler.
    """

    set_status: SkillStatus | None = None
    set_pinned: bool | None = None
    set_category: str | None = Field(default=None, max_length=64)
    ids: list[UUID] | None = Field(default=None, max_length=_MAX_BATCH_SKILLS)
    filter: _BatchFilter | None = None


def _principal(request: Request) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PLATFORM_SCOPE_FORBIDDEN",
                "message": "only a system admin may manage the platform skill library",
            },
        )
    return principal


# Maps the path-free ``_ZipRejectReason`` enum to an operator-facing hint. The
# parse layer keeps its public message generic (Oracle defense) and hides the
# real cause; platform import is system_admin-only, so surfacing the *reason*
# (never the path/internal_message) turns a useless "invalid skill package" 400
# into something an admin can act on.
_SKILL_REJECT_HINT: dict[str, str] = {
    "missing_skill_md": "no SKILL.md at the selected folder root (wrong folder?)",
    "invalid_frontmatter": (
        "SKILL.md frontmatter is missing or malformed (needs name + description)"
    ),
    "path_traversal": "package contains an unsafe path (.. traversal)",
    "symlink": "package contains a symlink entry, which is not allowed",
    "absolute_path": "package contains an absolute path, which is not allowed",
    "invalid_chars": "a file name has illegal characters/length (only a-z A-Z 0-9 . _ -)",
    "depth_exceeded": "folder nesting is deeper than 6 levels",
    "extension_not_allowed": (
        # Derived from the live allowlist so the hint can never drift from
        # what the validator actually enforces.
        f"package has a disallowed file type (allowed: {' '.join(sorted(ALLOWED_EXTENSIONS))})"
    ),
    "file_too_large": (
        f"a single file exceeds the size limit ({MAX_FILE_BYTES // (1024 * 1024)} MiB inline; "
        f"{MAX_FILE_BYTES_ASSET // (1024 * 1024)} MiB with the asset store)"
    ),
    "total_too_large": (
        f"the package exceeds the total size limit ({MAX_TOTAL_BYTES // (1024 * 1024)} MiB "
        f"inline; {MAX_TOTAL_BYTES_ASSET // (1024 * 1024)} MiB with the asset store)"
    ),
    "too_many_entries": (
        f"the package has too many files (limit {MAX_ENTRIES} inline; "
        f"{MAX_ENTRIES_ASSET} with the asset store)"
    ),
    "prompt_injection": "skill content tripped the prompt-injection scanner",
    "legacy_format": "legacy skill layout is not supported",
    "bad_zip": "the downloaded content is not a valid zip",
    "binary_in_text_file": "a text file contains binary content",
}


async def _ingest_platform_skill_payload(
    *,
    blob: bytes,
    store: SkillStore,
    audit: AuditLogger,
    principal: Principal,
    source: str,
    origin: str | None = None,
    object_store: SkillAssetObjectStore | None = None,
) -> tuple[dict[str, object], int]:
    """Core platform-import pipeline: parse a ``.skill`` zip → name check →
    moderation → Mini-ADR U-21 strict threat scan → idempotent create/version
    → audit. Returns ``(response_body, status_code)`` (201 created / 200 already
    present); raises :class:`HTTPException` on any validation failure. The
    single-import wrapper turns this into a ``JSONResponse``; the batch endpoint
    catches the ``HTTPException`` per skill to keep going (partial success).
    """
    try:
        payload = parse_skill_zip(blob, asset_tier=object_store is not None)
    except SkillZipError as exc:
        # ``reason`` is the path-free reject enum; ``internal_message`` carries
        # the offending path and stays server-side (audit only). For a bare
        # ``SkillPackageLayoutError`` (no reason) fall back to generic.
        reason = getattr(exc, "reason", None) if isinstance(exc, SkillPackageError) else None
        internal = getattr(exc, "internal_message", None) if reason else None
        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_PACKAGE_REJECTED,
            resource_type="skill",
            resource_id="unknown",
            result=AuditResult.DENIED,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "reason": reason or "unknown",
                "internal": internal,
                "source": source,
                **({"origin": origin} if origin else {}),
            },
        )
        if reason is not None:
            message = _SKILL_REJECT_HINT.get(reason, "invalid skill package")
            # Extensions (never paths) are safe to surface — name exactly what
            # got blocked so the operator can fix the package in one round.
            blocked = getattr(exc, "blocked_extensions", ())
            if blocked:
                message += f" — blocked: {' '.join(blocked)}"
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "SKILL_PACKAGE_INVALID",
                    "reason": reason,
                    "message": message,
                },
            ) from exc
        raise HTTPException(status_code=400, detail="invalid skill package") from exc

    if not re.fullmatch(r"^[a-z][a-z0-9_-]{0,63}$", payload.name):
        raise HTTPException(
            status_code=400,
            detail=f"skill name {payload.name!r} fails validation",
        )

    # skill-runtime §5.2 — advisory runtime classification (non-blocking) so the
    # operator learns at import time whether bundled scripts can run here.
    runtime = classify_skill_runtime(payload).as_dict()

    # Moderation gate before any DB write.
    try:
        moderate_prompt_fragment(payload.prompt_fragment)
        moderate_tool_names(payload.tool_names)
        moderate_required_models(payload.required_models)
    except ModerationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    # Mini-ADR U-21 write-time strict scan (same as add_platform_version).
    findings = scan_for_threats(payload.prompt_fragment, scope="strict")
    if findings:
        record_threat_pattern_hits(findings, scope="strict")
        record_skill_blocked(phase=f"platform_{source}")
        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
            resource_type="skill",
            resource_id=payload.name,
            result=AuditResult.DENIED,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "finding_count": len(findings),
                "findings": [
                    {"pattern_id": f.pattern_id, "category": f.category} for f in findings
                ],
                "source": source,
                **({"origin": origin} if origin else {}),
            },
        )
        raise HTTPException(status_code=400, detail="invalid skill content")

    # skill-asset-store: externalize supporting-file bytes AFTER the gates
    # (never upload rejected content) and BEFORE hashing/idempotency — the
    # content hash must cover the exact shape that gets persisted, and the
    # idempotency probe must compare like with like. Content-addressed keys
    # make a redundant upload on an idempotent re-import a harmless no-op.
    supporting_files = await externalize_supporting_files(
        payload.supporting_files, object_store=object_store
    )
    files_jsonable = supporting_files_to_jsonable(supporting_files)
    content_hash = compute_content_hash(payload.prompt_fragment, files_jsonable)

    created_skill = False
    async with bypass_rls_session():
        existing = await store.get_platform_skill_by_name(name=payload.name)

        # Idempotency (mirrors the tenant import path): if the latest version
        # already carries this exact content_hash, the re-import is a no-op —
        # return it (200, created=False).
        if existing is not None and existing.latest_version > 0:
            latest = await store.get_platform_version_by_number(
                skill_id=existing.id, version=existing.latest_version
            )
            if latest is not None and latest.content_hash == content_hash:
                return {
                    "skill": _skill_dict(existing),
                    "version": _version_dict(latest),
                    "created": False,
                    "runtime": runtime,
                }, 200

        if existing is None:
            try:
                existing = await store.create_platform_skill(
                    skill_id=uuid4(),
                    name=payload.name,
                    description=payload.description,
                    category=payload.category,
                    required_tier=TenantPlan.FREE,
                )
                created_skill = True
            except DuplicateSkillError as exc:
                # Race — another import won the create; resolve + add.
                existing = await store.get_platform_skill_by_name(name=payload.name)
                if existing is None:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        version = await store.add_platform_version(
            version_id=uuid4(),
            skill_id=existing.id,
            prompt_fragment=payload.prompt_fragment,
            tool_names=payload.tool_names,
            description=payload.description,
            category=payload.category,
            required_models=payload.required_models,
            authored_by="human",
            supporting_files=files_jsonable,
            lazy_load=payload.lazy_load,
            content_hash=content_hash,
            high_risk=payload.high_risk,
        )

    if created_skill:
        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_CREATE,
            resource_type="skill",
            resource_id=str(existing.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "name": existing.name,
                "category": existing.category,
                "source": source,
                **({"origin": origin} if origin else {}),
            },
        )
    await audit_emit(
        audit,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
        action=AuditAction.SKILL_VERSION_CREATE,
        resource_type="skill",
        resource_id=str(existing.id),
        result=AuditResult.SUCCESS,
        trace_id=current_trace_id_hex(),
        details={
            "scope": "platform",
            "version": version.version,
            "tool_names": list(version.tool_names),
            "source": source,
            **({"origin": origin} if origin else {}),
        },
    )
    return {
        "skill": _skill_dict(existing),
        "version": _version_dict(version),
        "created": True,
        "runtime": runtime,
    }, 201


async def _ingest_platform_skill_blob(
    *,
    blob: bytes,
    store: SkillStore,
    audit: AuditLogger,
    principal: Principal,
    source: str,
    origin: str | None = None,
    object_store: SkillAssetObjectStore | None = None,
) -> JSONResponse:
    """Single-import wrapper around :func:`_ingest_platform_skill_payload` —
    used by the ZIP upload and single GitHub import."""
    content, status = await _ingest_platform_skill_payload(
        blob=blob,
        store=store,
        audit=audit,
        principal=principal,
        source=source,
        origin=origin,
        object_store=object_store,
    )
    return JSONResponse(status_code=status, content=content)


def _get_object_store(request: Request) -> SkillAssetObjectStore | None:
    """The DURABLE object store for skill assets, or ``None``.

    The lifespan sets ``app.state.skill_asset_store`` only when the object
    store backend is s3-compatible — the in-memory backend loses objects on
    restart, so externalizing skill bytes to it would corrupt skills. With
    ``None`` imports stay inline (tighter caps) and reads degrade cleanly.
    """
    return getattr(request.app.state, "skill_asset_store", None)


def _http_detail_message(detail: object) -> str:
    """Flatten an ``HTTPException.detail`` (str or structured dict) into a short
    operator-facing reason for a batch result row."""
    if isinstance(detail, dict):
        msg = detail.get("message")
        reason = detail.get("reason")
        if isinstance(msg, str):
            return f"{msg} ({reason})" if isinstance(reason, str) else msg
        return str(detail)
    return str(detail)


def _result_skill_name(content: dict[str, object]) -> str | None:
    skill = content.get("skill")
    if isinstance(skill, dict):
        name = skill.get("name")
        return name if isinstance(name, str) else None
    return None


def _result_version(content: dict[str, object]) -> int | None:
    version = content.get("version")
    if isinstance(version, dict):
        v = version.get("version")
        return v if isinstance(v, int) else None
    return None


def build_platform_skills_router() -> APIRouter:
    """Stream X (X4) platform skill library CRUD router."""
    router = APIRouter(
        prefix="/v1/platform/skills",
        tags=["platform_skills"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.post("", response_model=None)
    async def create_platform_skill(
        body: _CreatePlatformSkillBody,
        request: Request,
    ) -> JSONResponse:
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)
        try:
            async with bypass_rls_session():
                skill = await store.create_platform_skill(
                    skill_id=uuid4(),
                    name=body.name,
                    description=body.description,
                    category=body.category,
                    required_tier=body.required_tier,
                )
        except DuplicateSkillError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"platform skill {body.name!r} already exists",
            ) from exc

        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_CREATE,
            resource_type="skill",
            resource_id=str(skill.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "name": skill.name,
                "category": skill.category,
                "required_tier": skill.required_tier.value,
            },
        )
        return JSONResponse(status_code=201, content=_skill_dict(skill))

    @router.post("/{skill_id}/versions", response_model=None)
    async def add_platform_version(
        skill_id: Annotated[UUID, Path()],
        body: _AddPlatformVersionBody,
        request: Request,
    ) -> JSONResponse:
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)

        # Same moderation gate as the tenant add-version path.
        try:
            moderate_prompt_fragment(body.prompt_fragment)
            moderate_tool_names(body.tool_names)
            moderate_required_models(body.required_models)
        except ModerationError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        # Mini-ADR U-21 write-time strict scan on prompt_fragment.
        findings = scan_for_threats(body.prompt_fragment, scope="strict")
        if findings:
            record_threat_pattern_hits(findings, scope="strict")
            record_skill_blocked(phase="supporting_file_api")
            await audit_emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.DENIED,
                trace_id=current_trace_id_hex(),
                details={
                    "scope": "platform",
                    "finding_count": len(findings),
                    "findings": [
                        {"pattern_id": f.pattern_id, "category": f.category} for f in findings
                    ],
                    "source": "json_api",
                },
            )
            raise HTTPException(status_code=400, detail="invalid skill content")

        # Mini-ADR U-21 / U-24 — content_hash + high_risk at write time.
        content_hash = compute_content_hash(body.prompt_fragment, {})
        high_risk = is_high_risk_skill_version(tool_names=body.tool_names, supporting_file_paths=[])

        try:
            async with bypass_rls_session():
                version = await store.add_platform_version(
                    version_id=uuid4(),
                    skill_id=skill_id,
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

        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_VERSION_CREATE,
            resource_type="skill",
            resource_id=str(skill_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "version": version.version,
                "tool_names": list(version.tool_names),
                "source": "json_api",
            },
        )
        return JSONResponse(status_code=201, content=_version_dict(version))

    @router.post("/import", response_model=None)
    async def import_platform_skill(
        file: Annotated[UploadFile, File()],
        request: Request,
    ) -> JSONResponse:
        """OFFICE-3: multipart ``.skill`` ZIP → platform (NULL-tenant) skill.

        system_admin only. Same ZIP parse + moderation + Mini-ADR U-21 strict
        threat scan as the tenant ``POST /v1/skills/import``, but writes a
        tenant-less row via ``bypass_rls_session()``. content_hash idempotent
        (same semantics as the tenant path): re-importing identical content
        returns ``200`` + ``created: false`` with the existing latest version,
        instead of churning a duplicate version.
        """
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)
        blob = await file.read()
        return await _ingest_platform_skill_blob(
            blob=blob,
            store=store,
            audit=audit,
            principal=principal,
            source="zip_import",
            object_store=_get_object_store(request),
        )

    @router.post("/import-from-github", response_model=None)
    async def import_platform_skill_from_github(
        body: _ImportFromGithubBody,
        request: Request,
    ) -> JSONResponse:
        """方案 A: fetch a skill from a public GitHub repo → platform library.

        system_admin only. Resolves ``source`` (owner/repo · github.com URL ·
        skills.sh URL) + optional ``skill`` selector, downloads the repo archive
        from the fixed ``codeload.github.com`` host (SSRF面=单域名), scans it for
        the matching ``SKILL.md`` folder, repacks it, then runs the SAME
        moderation + strict threat scan + idempotent create/version pipeline as
        the ZIP upload. GitHub-only / platform-only / public repos (M0).
        """
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)

        try:
            src = _skill_github.resolve_github_source(body.source, skill=body.skill, ref=body.ref)
            archive = await _skill_github.download_github_archive(src)
            blob = _skill_github.select_skill_zip(archive, skill=src.skill)
        except _skill_github.GithubImportError as exc:
            # Structured detail so the UI can render candidates as a picker
            # instead of a raw error string.
            detail: dict[str, object] = {
                "code": "SKILL_AMBIGUOUS" if exc.candidates else "GITHUB_IMPORT_ERROR",
                "message": exc.message,
            }
            if exc.candidates is not None:
                detail["candidates"] = exc.candidates
            raise HTTPException(status_code=exc.status, detail=detail) from exc

        origin = f"{src.owner}/{src.repo}@{src.ref}" + (f"#{src.skill}" if src.skill else "")
        return await _ingest_platform_skill_blob(
            blob=blob,
            store=store,
            audit=audit,
            principal=principal,
            source="github_import",
            origin=origin,
            object_store=_get_object_store(request),
        )

    @router.post("/list-github-skills", response_model=None)
    async def list_github_skills(
        body: _ListGithubSkillsBody,
        request: Request,
    ) -> JSONResponse:
        """List the importable skill folders in a GitHub source — no import.

        system_admin only. Resolves + downloads the repo archive (same fixed
        ``codeload.github.com`` host as import) and returns the candidate skill
        paths so the UI can populate its picker before the operator commits to
        an import. A single-skill repo returns one candidate; a multi-skill repo
        returns all.
        """
        _principal(request)
        try:
            src = _skill_github.resolve_github_source(body.source, ref=body.ref)
            archive = await _skill_github.download_github_archive(src)
            candidates = _skill_github.list_skill_candidates(archive)
        except _skill_github.GithubImportError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"code": "GITHUB_IMPORT_ERROR", "message": exc.message},
            ) from exc
        # Raw (non-enveloped) body — matches the import endpoints' convention.
        return JSONResponse(content={"candidates": candidates})

    @router.post("/import-from-github/batch", response_model=None)
    async def import_platform_skills_from_github_batch(
        body: _BatchImportFromGithubBody,
        request: Request,
    ) -> JSONResponse:
        """方案 A batch: import multiple skills from one public GitHub repo.

        system_admin only. Resolves + downloads the repo archive **once**, then
        for each requested ``skills`` entry selects/repacks/ingests that folder
        independently. Partial success: a skill that fails (select miss / scan /
        invalid frontmatter) is reported as ``failed`` with a path-free reason
        and the rest continue. Always returns ``200`` with a per-skill result
        list; resolve/download failures (bad source, repo not found) are fatal
        and surface as the usual structured error.
        """
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)

        # Resolve + download once. These failures are fatal for the whole batch.
        try:
            src = _skill_github.resolve_github_source(body.source, ref=body.ref)
            archive = await _skill_github.download_github_archive(src)
        except _skill_github.GithubImportError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"code": "GITHUB_IMPORT_ERROR", "message": exc.message},
            ) from exc

        # De-dupe while preserving order so a double-checked candidate is ingested once.
        seen: set[str] = set()
        requested: list[str] = []
        for skill_name in body.skills:
            if skill_name not in seen:
                seen.add(skill_name)
                requested.append(skill_name)

        results: list[dict[str, object]] = []
        for skill_path in requested:
            origin = f"{src.owner}/{src.repo}@{src.ref}#{skill_path}"
            try:
                blob = _skill_github.select_skill_zip(archive, skill=skill_path)
            except _skill_github.GithubImportError as exc:
                results.append({"skill": skill_path, "status": "failed", "reason": exc.message})
                continue
            try:
                content, _status = await _ingest_platform_skill_payload(
                    blob=blob,
                    store=store,
                    audit=audit,
                    principal=principal,
                    source="github_batch_import",
                    origin=origin,
                    object_store=_get_object_store(request),
                )
            except HTTPException as exc:
                results.append(
                    {
                        "skill": skill_path,
                        "status": "failed",
                        "reason": _http_detail_message(exc.detail),
                    }
                )
                continue
            results.append(
                {
                    "skill": skill_path,
                    "status": "created" if content.get("created") else "exists",
                    "name": _result_skill_name(content),
                    "version": _result_version(content),
                    "runtime": content.get("runtime"),
                }
            )

        return JSONResponse(status_code=200, content={"results": results})

    @router.patch("/{skill_id}", response_model=None)
    async def patch_platform_skill(
        skill_id: Annotated[UUID, Path()],
        body: _PatchPlatformSkillBody,
        request: Request,
    ) -> JSONResponse:
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)
        if body.status is None and body.pinned is None and body.category is None:
            raise HTTPException(
                status_code=422,
                detail="patch body must set at least one of: status, pinned, category",
            )

        async with bypass_rls_session():
            prior = await store.get_platform_skill(skill_id=skill_id)
        if prior is None:
            raise HTTPException(status_code=404, detail="skill not found")

        updated = prior
        if body.status is not None:
            try:
                async with bypass_rls_session():
                    updated = await store.set_platform_status(skill_id=skill_id, status=body.status)
            except SkillNotFoundError as exc:
                raise HTTPException(status_code=404, detail="skill not found") from exc
            await audit_emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.SKILL_STATUS_CHANGE,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details={
                    "scope": "platform",
                    "from": prior.status.value,
                    "to": updated.status.value,
                },
            )

        if body.pinned is not None and body.pinned != updated.pinned:
            try:
                async with bypass_rls_session():
                    updated = await store.set_platform_pinned(skill_id=skill_id, pinned=body.pinned)
            except SkillNotFoundError as exc:
                raise HTTPException(status_code=404, detail="skill not found") from exc
            await audit_emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=(AuditAction.SKILL_PINNED if body.pinned else AuditAction.SKILL_UNPINNED),
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details={"scope": "platform", "pinned": body.pinned},
            )

        # ``category`` present (incl. "" to clear) → re-label. Store normalizes
        # a blank to NULL, so the effective new value may be None.
        if body.category is not None:
            new_category = body.category.strip() or None
            if new_category != prior.category:
                try:
                    async with bypass_rls_session():
                        updated = await store.set_platform_category(
                            skill_id=skill_id, category=new_category
                        )
                except SkillNotFoundError as exc:
                    raise HTTPException(status_code=404, detail="skill not found") from exc
                await audit_emit(
                    audit,
                    tenant_id=principal.tenant_id,
                    actor_id=principal.subject_id,
                    action=AuditAction.SKILL_CATEGORY_CHANGED,
                    resource_type="skill",
                    resource_id=str(skill_id),
                    result=AuditResult.SUCCESS,
                    trace_id=current_trace_id_hex(),
                    details={
                        "scope": "platform",
                        "from": prior.category,
                        "to": new_category,
                    },
                )

        return JSONResponse(status_code=200, content=_skill_dict(updated))

    @router.get("/categories", response_model=None)
    async def list_platform_skill_categories(request: Request) -> JSONResponse:
        """Categories offered to the admin list filter + detail picker: the seed
        taxonomy first (stable order, so the picker is never empty on a fresh
        install), then any hand-typed categories not already in the seed set."""
        _principal(request)
        store = _get_skill_store(request)
        async with bypass_rls_session():
            existing = await store.list_platform_categories()
        seed = set(SEED_PLATFORM_CATEGORIES)
        categories = [*SEED_PLATFORM_CATEGORIES, *(c for c in existing if c not in seed)]
        return JSONResponse(status_code=200, content={"categories": categories})

    @router.get("", response_model=None)
    async def list_platform_skills(
        request: Request,
        status: Annotated[SkillStatus | None, Query()] = None,
        category: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> JSONResponse:
        _principal(request)
        store = _get_skill_store(request)
        async with bypass_rls_session():
            rows, total = await store.list_platform_skills(
                status=status, category=category, q=q, offset=offset, limit=limit
            )
        return JSONResponse(
            status_code=200,
            content={"items": [_skill_dict(r) for r in rows], "total": total},
        )

    @router.post("/batch", response_model=None)
    async def batch_update_platform_skills(
        body: _BatchPlatformSkillsBody,
        request: Request,
    ) -> JSONResponse:
        """Bulk lock/unlock/archive/activate/re-label platform skills (system_admin).

        Applies the patch to either an explicit ``ids`` list (a page's selection)
        or to every skill matching ``filter`` (the "select all N matching" path).
        The patch may also carry ``set_category`` to re-label (or, via an empty
        string, clear) the category. Returns ``{updated}`` — the affected row
        count — and writes a single audit row (no per-row spam)."""
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)
        if body.set_status is None and body.set_pinned is None and body.set_category is None:
            raise HTTPException(
                status_code=422,
                detail="batch body must set at least one of: set_status, set_pinned, set_category",
            )
        has_ids = body.ids is not None
        has_filter = body.filter is not None
        if has_ids == has_filter:
            raise HTTPException(
                status_code=422, detail="batch body requires exactly one of: ids, filter"
            )
        update_category = body.set_category is not None
        new_category = (
            (body.set_category.strip() or None) if body.set_category is not None else None
        )
        async with bypass_rls_session():
            updated = await store.bulk_update_platform_skills(
                ids=body.ids,
                filter_status=body.filter.status if body.filter else None,
                filter_category=body.filter.category if body.filter else None,
                filter_q=body.filter.q if body.filter else None,
                set_status=body.set_status,
                set_pinned=body.set_pinned,
                update_category=update_category,
                new_category=new_category,
            )
        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_STATUS_CHANGE,
            resource_type="skill",
            resource_id="batch",
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "mode": "ids" if has_ids else "filter",
                "updated": updated,
                "set_status": body.set_status.value if body.set_status else None,
                "set_pinned": body.set_pinned,
                "set_category": new_category if update_category else None,
            },
        )
        return JSONResponse(status_code=200, content={"updated": updated})

    @router.get("/{skill_id}", response_model=None)
    async def get_platform_skill(
        skill_id: Annotated[UUID, Path()],
        request: Request,
    ) -> JSONResponse:
        _principal(request)
        store = _get_skill_store(request)
        async with bypass_rls_session():
            skill = await store.get_platform_skill(skill_id=skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        return JSONResponse(status_code=200, content=_skill_dict(skill))

    @router.get("/{skill_id}/versions", response_model=None)
    async def list_platform_versions(
        skill_id: Annotated[UUID, Path()],
        request: Request,
    ) -> JSONResponse:
        _principal(request)
        store = _get_skill_store(request)
        async with bypass_rls_session():
            skill = await store.get_platform_skill(skill_id=skill_id)
            if skill is None:
                raise HTTPException(status_code=404, detail="skill not found")
            versions = await store.list_platform_versions(skill_id=skill_id)
        return JSONResponse(
            status_code=200, content={"items": [_version_dict(v) for v in versions]}
        )

    @router.get("/{skill_id}/versions/{version_number}", response_model=None)
    async def get_platform_version(
        skill_id: Annotated[UUID, Path()],
        version_number: int,
        request: Request,
    ) -> JSONResponse:
        _principal(request)
        store = _get_skill_store(request)
        async with bypass_rls_session():
            version = await store.get_platform_version_by_number(
                skill_id=skill_id, version=version_number
            )
        if version is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        return JSONResponse(status_code=200, content=_version_dict(version))

    # ── Supporting files (Stream X / skill-authoring-ia Phase A) ──────────
    # Mirror the tenant ``/v1/skills`` supporting-file surface so the platform
    # admin can iterate a curated skill's bundled scripts/references in-UI
    # instead of re-importing a ZIP for every edit. NULL-tenant rows ⇒ every
    # store call inside ``bypass_rls_session()`` + ``is_system_admin`` gate.

    @router.get(
        "/{skill_id}/versions/{version}/supporting-files/{file_path:path}",
        response_model=None,
    )
    async def get_platform_supporting_file(
        skill_id: Annotated[UUID, Path()],
        version: int,
        file_path: str,
        request: Request,
    ) -> JSONResponse:
        """Single-file content fetch (base64). Mirrors the tenant path; skips
        the U-21 re-scan on purpose so operators see the literal stored bytes
        for audit/triage."""
        _principal(request)
        store = _get_skill_store(request)
        try:
            _validate_supporting_file_path(file_path)
        except SkillPackageLayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with bypass_rls_session():
            row = await store.get_platform_version_by_number(skill_id=skill_id, version=version)
        if row is None:
            raise HTTPException(status_code=404, detail="skill version not found")
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
            content={"content": content_b64, "size": entry.size, "mime": entry.mime},
        )

    @router.put(
        "/{skill_id}/versions/{version}/supporting-files/{file_path:path}",
        response_model=None,
    )
    async def put_platform_supporting_file(
        skill_id: Annotated[UUID, Path()],
        version: int,
        file_path: str,
        body: _PutSupportingFileBody,
        request: Request,
    ) -> JSONResponse:
        """Add or replace one supporting file → new platform version (U-17).
        U-18 path validation + U-21 write-time strict scan + U-24 high_risk
        recompute, exactly as the tenant path."""
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)

        try:
            _validate_supporting_file_path(file_path)
        except SkillPackageLayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.size > _MAX_SUPPORTING_FILE_SIZE:
            raise HTTPException(status_code=400, detail="invalid supporting file path")

        async with bypass_rls_session():
            prior = await store.get_platform_version_by_number(skill_id=skill_id, version=version)
        if prior is None:
            raise HTTPException(status_code=404, detail="skill version not found")

        try:
            raw = base64.b64decode(body.content, validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="invalid supporting file path") from exc
        if len(raw) != body.size:
            raise HTTPException(status_code=400, detail="invalid supporting file path")

        ext = PurePosixPath(file_path).suffix.lower()
        if ext in _SUPPORTING_FILE_TEXT_EXTS:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                record_skill_blocked(phase="supporting_file_api")
                await audit_emit(
                    audit,
                    tenant_id=principal.tenant_id,
                    actor_id=principal.subject_id,
                    action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                    resource_type="skill_supporting_file",
                    resource_id=f"{skill_id}/{version}/{file_path}",
                    result=AuditResult.DENIED,
                    trace_id=current_trace_id_hex(),
                    details={"scope": "platform", "reason": "text_extension_binary_content"},
                )
                raise HTTPException(status_code=400, detail="invalid supporting file path") from exc
            findings = scan_for_threats(text, scope="strict")
            if findings:
                record_threat_pattern_hits(findings, scope="strict")
                record_skill_blocked(phase="supporting_file_api")
                await audit_emit(
                    audit,
                    tenant_id=principal.tenant_id,
                    actor_id=principal.subject_id,
                    action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                    resource_type="skill_supporting_file",
                    resource_id=f"{skill_id}/{version}/{file_path}",
                    result=AuditResult.DENIED,
                    trace_id=current_trace_id_hex(),
                    details={
                        "scope": "platform",
                        "finding_count": len(findings),
                        "findings": [
                            {"pattern_id": f.pattern_id, "category": f.category} for f in findings
                        ],
                    },
                )
                raise HTTPException(status_code=400, detail="invalid supporting file path")

        merged = supporting_files_to_jsonable(prior.supporting_files)
        merged[file_path] = {"content": body.content, "size": body.size, "mime": body.mime}
        new_paths = list(merged.keys())
        new_high_risk = is_high_risk_skill_version(
            tool_names=prior.tool_names, supporting_file_paths=new_paths
        )
        new_hash = compute_content_hash(prior.prompt_fragment, merged)

        async with bypass_rls_session():
            new_version = await store.add_platform_version(
                version_id=uuid4(),
                skill_id=skill_id,
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

        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_SUPPORTING_FILE_UPLOADED,
            resource_type="skill_supporting_file",
            resource_id=f"{skill_id}/{new_version.version}/{file_path}",
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
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
    )
    async def delete_platform_supporting_file(
        skill_id: Annotated[UUID, Path()],
        version: int,
        file_path: str,
        request: Request,
    ) -> JSONResponse:
        """Remove one supporting file → new platform version (U-17)."""
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)

        try:
            _validate_supporting_file_path(file_path)
        except SkillPackageLayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async with bypass_rls_session():
            prior = await store.get_platform_version_by_number(skill_id=skill_id, version=version)
        if prior is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        if file_path not in prior.supporting_files:
            raise HTTPException(status_code=404, detail="supporting file not found")

        merged = supporting_files_to_jsonable(prior.supporting_files)
        merged.pop(file_path)
        new_paths = list(merged.keys())
        new_high_risk = is_high_risk_skill_version(
            tool_names=prior.tool_names, supporting_file_paths=new_paths
        )
        new_hash = compute_content_hash(prior.prompt_fragment, merged)

        async with bypass_rls_session():
            new_version = await store.add_platform_version(
                version_id=uuid4(),
                skill_id=skill_id,
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

        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_SUPPORTING_FILE_REMOVED,
            resource_type="skill_supporting_file",
            resource_id=f"{skill_id}/{new_version.version}/{file_path}",
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "from_version": prior.version,
                "to_version": new_version.version,
                "path": file_path,
                "high_risk_after": new_high_risk,
            },
        )
        return JSONResponse(status_code=200, content=_version_dict(new_version))

    @router.get("/{skill_id}/versions/{version_number}/export", response_model=None)
    async def export_platform_version(
        skill_id: Annotated[UUID, Path()],
        version_number: int,
        request: Request,
    ) -> Response:
        """Export a platform version as a ``.skill`` ZIP. Unlike the tenant
        export, the bundle includes ``supporting_files`` (scripts/references)
        so a round-trip is lossless — that bundle is the whole point of the
        platform editor."""
        _principal(request)
        store = _get_skill_store(request)
        async with bypass_rls_session():
            version = await store.get_platform_version_by_number(
                skill_id=skill_id, version=version_number
            )
            skill = await store.get_platform_skill(skill_id=skill_id)
        if version is None:
            raise HTTPException(status_code=404, detail="skill version not found")
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
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

    @router.put("/{skill_id}/versions/{version}/prompt", response_model=None)
    async def put_platform_prompt(
        skill_id: Annotated[UUID, Path()],
        version: int,
        body: _PutPlatformPromptBody,
        request: Request,
    ) -> JSONResponse:
        """Edit ``SKILL.md`` (prompt_fragment) → new platform version
        (Phase D-2). Inherits all other fields incl. supporting_files, so
        editing the prompt never drops bundled files."""
        principal = _principal(request)
        store = _get_skill_store(request)
        audit = _get_audit(request)

        try:
            moderate_prompt_fragment(body.prompt_fragment)
        except ModerationError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        async with bypass_rls_session():
            prior = await store.get_platform_version_by_number(skill_id=skill_id, version=version)
        if prior is None:
            raise HTTPException(status_code=404, detail="skill version not found")

        findings = scan_for_threats(body.prompt_fragment, scope="strict")
        if findings:
            record_threat_pattern_hits(findings, scope="strict")
            record_skill_blocked(phase="supporting_file_api")
            await audit_emit(
                audit,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
                resource_type="skill",
                resource_id=str(skill_id),
                result=AuditResult.DENIED,
                trace_id=current_trace_id_hex(),
                details={
                    "scope": "platform",
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

        async with bypass_rls_session():
            new_version = await store.add_platform_version(
                version_id=uuid4(),
                skill_id=skill_id,
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

        await audit_emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SKILL_VERSION_CREATE,
            resource_type="skill",
            resource_id=str(skill_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "scope": "platform",
                "from_version": prior.version,
                "to_version": new_version.version,
                "source": "prompt_edit",
            },
        )
        return JSONResponse(status_code=201, content=_version_dict(new_version))

    return router
