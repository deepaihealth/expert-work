"""Pydantic models for the supervisor's HTTP API — STREAM-F-DESIGN § 4.1.

The M0 ``AcquireRequest`` is a subset of subsystems/14 § 3.3: no
``isolation_level`` branch and no ``purpose`` — M0 sandboxes are always
``shared``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SeedFile(BaseModel):
    """One file to materialize into the sandbox ``/workspace`` at acquire time.

    skill-runtime §5.1 — an agent's activated skill files (SKILL.md + scripts +
    reference) so bundled scripts run as authored. ``path`` is relative under
    ``/workspace`` (e.g. ``skills/pptx/SKILL.md``); ``content_b64`` is the raw
    bytes, base64-encoded.
    """

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1, max_length=256)
    content_b64: str


class AcquireRequest(BaseModel):
    """Request body for ``POST /v1/sandboxes:acquire``."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    thread_id: str
    #: skill-runtime §5.1 — files to seed into ``/workspace`` before first exec.
    #: Empty (default) → no seeding (pre-feature behaviour).
    seed_files: list[SeedFile] = Field(default_factory=list)
    #: Owning user (Stream J.15). When set, the sandbox mounts that
    #: user's persistent workspace volume at ``/workspace``; omitted →
    #: an ephemeral tmpfs workspace (the pre-J.15 behaviour).
    user_id: UUID | None = None
    #: DEPRECATED (sandbox-image-consolidation) — the variant split was
    #: collapsed into one image. Kept for back-compat so an older orchestrator
    #: that still sends it doesn't 422; the value is ignored (every acquire uses
    #: the single ``sandbox_image``).
    image_variant: str | None = None
    #: sandbox-egress §3.3 — the agent's egress policy. ``"proxy"``/``"direct"``
    #: → the sandbox is given ``HTTPS_PROXY`` + a per-sandbox token so its code
    #: can reach the public internet through the audited egress proxy.
    #: ``"none"`` / omitted → no egress env (sandbox stays proxy-only/isolated).
    egress: str | None = None
    #: Agent identity bound into the egress token (audit attribution).
    agent_name: str | None = None
    agent_version: str | None = None
    #: sandbox-egress Phase 2 — optional per-agent host allowlist embedded in
    #: the egress token; empty → any public host (audited).
    egress_allowlist: list[str] = Field(default_factory=list)
    #: Optional per-agent host denylist embedded in the token; blocks these
    #: hosts even under the default allow-all (takes precedence over allowlist).
    egress_denylist: list[str] = Field(default_factory=list)
    #: Optional per-call resource overrides; omitted → the service defaults.
    cpu: float | None = Field(default=None, gt=0, le=16)
    memory_mb: int | None = Field(default=None, gt=0, le=65536)
    pids_limit: int | None = Field(default=None, gt=0, le=4096)
    timeout_s: int | None = Field(default=None, gt=0, le=300)


class AcquireResponse(BaseModel):
    """Response body for a successful acquire."""

    model_config = ConfigDict(frozen=True)

    sandbox_id: UUID
    container_id: str
    #: M0 has no warm pool, so every acquire is a cold start.
    cold_start: bool = True
    acquired_at: datetime


class ExecRequest(BaseModel):
    """Request body for ``POST /v1/sandboxes/{id}:exec``."""

    model_config = ConfigDict(frozen=True)

    code: str
    #: Per-call execution timeout; omitted → the sandbox's own default.
    timeout_s: int | None = Field(default=None, gt=0, le=300)
    #: sandbox migration wave 2 (spec 决策 10) — per-agent env overrides for
    #: this one exec call (currently just ``PYTHONUSERBASE`` isolation, built
    #: by ``orchestrator.tools.sandbox.agent_key_envs``). Empty (default) →
    #: no override, pre-feature behaviour. Not a general escape hatch: the
    #: orchestrator is the only caller and only ever sends this one key.
    envs: dict[str, str] = Field(default_factory=dict)


class ExecResponse(BaseModel):
    """Response body for a code execution — the runner's captured outcome."""

    model_config = ConfigDict(frozen=True)

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


class DestroyRequest(BaseModel):
    """Request body for ``POST /v1/sandboxes/{id}:destroy``."""

    model_config = ConfigDict(frozen=True)

    reason: str = "destroy"


class HealthResponse(BaseModel):
    """Response body for ``GET /v1/health``."""

    model_config = ConfigDict(frozen=True)

    status: str
    docker_ok: bool


class ReapRequest(BaseModel):
    """Request body for ``POST /v1/sandboxes:reap`` — Stream P (Mini-ADR P-14)."""

    model_config = ConfigDict(frozen=True)

    #: ``True`` reaps every active session regardless of idle age; ``False``
    #: runs the normal idle-TTL sweep.
    force: bool = False


class ReapResponse(BaseModel):
    """Response body for ``POST /v1/sandboxes:reap``."""

    model_config = ConfigDict(frozen=True)

    reaped_count: int
