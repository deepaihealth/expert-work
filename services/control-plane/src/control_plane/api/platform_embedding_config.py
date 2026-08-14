"""``/v1/platform/embedding-config`` — platform embedding/rerank selection (Stream T).

system_admin-only view of the EFFECTIVE platform embedding + rerank
provider/model selection, alongside the selectable options derived from the
catalog filtered to configured providers (so the admin UI fills both dropdowns
in a single call). This module hosts the GET (read) surface; the PUT (write)
surface lives alongside it (Task 3).

Gating mirrors :mod:`control_plane.api.platform_config`: ``principal``
arrives via the shared :func:`control_plane.api._authz._principal` dependency
and handlers gate inline on ``principal.is_system_admin`` (platform-level; no
RBAC ``tenant`` resource — same precedent as ``platform_config.py``). Responses
use the ``{"success", "data", "error"}`` envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from control_plane.api._authz import _principal, console_only
from control_plane.audit import emit
from control_plane.platform_embedding_config import PlatformEmbeddingConfigService
from control_plane.platform_secrets import PlatformSecretsService
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import (
    PROVIDER_CATALOG,
    AuditAction,
    Principal,
    models_for_provider,
)
from expert_work.runtime.audit.logger import AuditLogger


class PlatformEmbeddingConfigWrite(BaseModel):
    """Write payload for the platform embedding/rerank selection (Stream T).

    Only provider/model **names** — never secret values. ``rerank_*`` are
    optional but must be supplied together (validated in the handler)."""

    model_config = ConfigDict(extra="forbid")
    embedding_provider: str
    embedding_model: str
    rerank_provider: str | None = None
    rerank_model: str | None = None


def _require_system_admin(principal: Principal) -> None:
    if not principal.is_system_admin:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PLATFORM_SCOPE_FORBIDDEN",
                "message": "only a system admin may manage the platform embedding config",
            },
        )


def _get_embedding_config_service(request: Request) -> PlatformEmbeddingConfigService:
    return request.app.state.platform_embedding_config_service  # type: ignore[no-any-return]


def _get_secrets_service(request: Request) -> PlatformSecretsService:
    return request.app.state.platform_secrets_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _model_has_capability(provider: str, model: str, *, kind: str) -> bool:
    """True iff ``model`` exists for ``provider`` with the ``kind`` flag set.

    ``kind`` is the ``ModelEntry`` capability flag (``"embeddings"`` /
    ``"rerank"``) that must be ``True``."""
    for entry in models_for_provider(provider):
        if entry.name == model:
            return getattr(entry, kind) is True
    return False


def _pair_to_dict(pair: tuple[str, str] | None) -> dict[str, str] | None:
    if pair is None:
        return None
    provider, model = pair
    return {"provider": provider, "model": model}


def _available(configured: set[str], *, kind: str) -> list[dict[str, str]]:
    """Catalog options for ``configured`` providers, filtered by capability.

    ``kind`` is ``"embeddings"`` or ``"rerank"`` — the ``ModelEntry`` flag that
    must be ``True`` for the model to be selectable.
    """
    options: list[dict[str, str]] = []
    for provider in PROVIDER_CATALOG:
        if provider not in configured:
            continue
        for entry in models_for_provider(provider):
            if getattr(entry, kind) is True:
                options.append({"provider": provider, "model": entry.name})
    return options


def build_platform_embedding_config_router() -> APIRouter:
    router = APIRouter(prefix="/v1/platform/embedding-config", tags=["platform_config"])

    @router.get("")
    async def get_platform_embedding_config(
        principal: Annotated[Principal, Depends(_principal)],
        embedding_config_service: Annotated[
            PlatformEmbeddingConfigService, Depends(_get_embedding_config_service)
        ],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
    ) -> dict[str, object]:
        """Effective embedding/rerank selection + the selectable options.

        ``embedding`` / ``rerank`` are ``{"provider", "model"}`` or ``null``;
        ``available_embedding`` / ``available_rerank`` list the capable catalog
        models for every configured platform provider."""
        _require_system_admin(principal)
        embedding = await embedding_config_service.effective_embedding_config()
        rerank = await embedding_config_service.effective_rerank_config()
        configured = set(await secrets_service.effective_provider_credentials())
        return {
            "success": True,
            "data": {
                "embedding": _pair_to_dict(embedding),
                "rerank": _pair_to_dict(rerank),
                "available_embedding": _available(configured, kind="embeddings"),
                "available_rerank": _available(configured, kind="rerank"),
            },
            "error": None,
        }

    # 阶段 1.2 —— deliberately role-agnostic (see the handler docstring), but
    # that means "any *employee*", not "any API key": a third party has no
    # business probing the platform's embedding configuration at all.
    @router.get("/status", dependencies=[Depends(console_only())])
    async def get_platform_embedding_config_status(
        principal: Annotated[Principal, Depends(_principal)],
        embedding_config_service: Annotated[
            PlatformEmbeddingConfigService, Depends(_get_embedding_config_service)
        ],
    ) -> dict[str, object]:
        """Whether the platform has an effective embedding config.

        Role-agnostic (any authenticated principal): exposes only a boolean so
        an agent-creator — typically a tenant admin, who cannot read the full
        system_admin-only config — can block+guide before building a
        long-term-memory agent. No provider/model names are returned."""
        embedding = await embedding_config_service.effective_embedding_config()
        return {
            "success": True,
            "data": {"configured": embedding is not None},
            "error": None,
        }

    @router.put("")
    async def put_platform_embedding_config(
        payload: PlatformEmbeddingConfigWrite,
        principal: Annotated[Principal, Depends(_principal)],
        embedding_config_service: Annotated[
            PlatformEmbeddingConfigService, Depends(_get_embedding_config_service)
        ],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Set the effective embedding/rerank selection. system_admin-only.

        Validates the selection against the catalog + configured provider keys,
        persists it (which invalidates the cache → immediate effect), and emits
        a ``PLATFORM_EMBEDDING_CONFIG_UPDATED`` audit row carrying only
        provider/model names (no secrets)."""
        _require_system_admin(principal)

        # 1. rerank must be supplied as a pair (both or neither).
        if (payload.rerank_provider is None) != (payload.rerank_model is None):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_RERANK_PAIR",
                    "message": "provide both 'rerank_provider' and 'rerank_model', or neither",
                },
            )

        configured = set(await secrets_service.effective_provider_credentials())

        # 2. embedding provider key configured?
        if payload.embedding_provider not in configured:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "EMBEDDING_PROVIDER_KEY_MISSING",
                    "message": (
                        f"configure the {payload.embedding_provider!r} provider key in "
                        "platform credentials first"
                    ),
                },
            )

        # 3. embedding model is an embedding-capable catalog model?
        if not _model_has_capability(
            payload.embedding_provider, payload.embedding_model, kind="embeddings"
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_EMBEDDING_MODEL",
                    "message": (
                        f"{payload.embedding_model!r} is not an embedding model for "
                        f"provider {payload.embedding_provider!r}"
                    ),
                },
            )

        # 4. rerank (when given): provider key configured + rerank-capable model.
        if payload.rerank_provider is not None and payload.rerank_model is not None:
            if payload.rerank_provider not in configured:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "RERANK_PROVIDER_KEY_MISSING",
                        "message": (
                            f"configure the {payload.rerank_provider!r} provider key in "
                            "platform credentials first"
                        ),
                    },
                )
            if not _model_has_capability(
                payload.rerank_provider, payload.rerank_model, kind="rerank"
            ):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "INVALID_RERANK_MODEL",
                        "message": (
                            f"{payload.rerank_model!r} is not a rerank model for "
                            f"provider {payload.rerank_provider!r}"
                        ),
                    },
                )

        await embedding_config_service.put(
            embedding_provider=payload.embedding_provider,
            embedding_model=payload.embedding_model,
            rerank_provider=payload.rerank_provider,
            rerank_model=payload.rerank_model,
            updated_by=principal.subject_id,
        )

        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_EMBEDDING_CONFIG_UPDATED,
            resource_type="platform_credential",
            resource_id="embedding-config",
            trace_id=current_trace_id_hex(),
            details={
                "embedding_provider": payload.embedding_provider,
                "embedding_model": payload.embedding_model,
                "rerank_provider": payload.rerank_provider,
                "rerank_model": payload.rerank_model,
            },
        )

        embedding = await embedding_config_service.effective_embedding_config()
        rerank = await embedding_config_service.effective_rerank_config()
        return {
            "success": True,
            "data": {
                "embedding": _pair_to_dict(embedding),
                "rerank": _pair_to_dict(rerank),
            },
            "error": None,
        }

    return router
