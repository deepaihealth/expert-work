"""Pydantic models for the supervisor's HTTP API — STREAM-F-DESIGN § 4.1.

The M0 ``AcquireRequest`` is a subset of subsystems/14 § 3.3: no
``isolation_level`` branch and no ``purpose`` — M0 sandboxes are always
``shared``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: B-60 —— ``ExecRequest.agent_root`` 唯一合法的前缀。与 orchestrator 的
#: ``workspace_paths.agent_nas_root()`` 拼出来的形状同义,但这里刻意是自己的字面量:
#: supervisor 不 import orchestrator,而这道闸要挡的正是「送进来的不是那个函数拼的」。
_AGENT_ROOT_PREFIX = "/mnt/workspace/agents/"

#: B-236 —— ``ExecRequest.compat_dirs`` 的上限与不许出现的名字。与 runner 那段 sh 的
#: ``case`` 同一组(这里是 HTTP 边界的第一道,脚本自己还会再查一遍)。
_MAX_COMPAT_DIRS = 64
_RESERVED_COMPAT_NAMES = frozenset(
    {"", ".", "..", "threads", "uploads", "inputs", ".tool_results", "shared", "skills", "agents"}
)


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
    #: B-60 —— the agent's directory on the NAS mount (``/mnt/workspace/agents/<key>``),
    #: bind-mounted as ``/workspace`` inside the private mount namespace the runner
    #: creates for this one exec. Per-call for the same reason as ``envs``: one warm
    #: session serves every agent of a ``(tenant, user)``. ``None`` → unbound: the
    #: whole user root becomes ``/workspace`` (pre-feature view). Not a cwd — the
    #: child's cwd is always ``/workspace``.
    agent_root: str | None = None
    #: B-236 —— 对话视图:``/workspace`` 是 ``<agent_root>/threads/<conv_id>/work``,
    #: ``/agent`` 是 ``agent_root`` 本身。``None`` → 老视图(两者都是 ``agent_root``)。
    #: 只在绑了 agent(``agent_root`` 非空)时成立。
    conv_id: UUID | None = None
    #: B-236 —— 过渡期映射进 ``/workspace`` 的 agent 根一级目录名。
    compat_dirs: list[str] = Field(default_factory=list, max_length=_MAX_COMPAT_DIRS)
    #: B-236 —— 本对话的线程 id:两种视图里 ``<agent_root>/threads/`` 都只留它那一格。
    #: ``None`` → 一格都不留。
    thread_id: UUID | None = None

    @field_validator("compat_dirs")
    @classmethod
    def _compat_dirs_are_plain_top_level_names(cls, value: list[str]) -> list[str]:
        """422 任何不是「agent 根下一级、非保留」的名字 —— 它们会被拼成 bind 源与目标。"""
        for name in value:
            if "/" in name or "\0" in name or name in _RESERVED_COMPAT_NAMES:
                msg = f"compat_dirs entry is not a plain directory name: {name!r}"
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _conversation_view_needs_an_agent_root(self) -> ExecRequest:
        has_view = self.conv_id is not None or self.thread_id is not None or self.compat_dirs
        if has_view and self.agent_root is None:
            msg = "conv_id / thread_id / compat_dirs need agent_root (the view is per agent)"
            raise ValueError(msg)
        if self.compat_dirs and self.conv_id is None:
            msg = "compat_dirs need conv_id (they only exist in the conversation view)"
            raise ValueError(msg)
        return self

    @field_validator("agent_root")
    @classmethod
    def _agent_root_is_an_agent_dir_on_the_nas_mount(cls, value: str | None) -> str | None:
        """422 anything that is not ``/mnt/workspace/agents/<…>`` without ``..``.

        The value is a *mount source* the runner binds as ``/workspace`` inside the
        exec's namespace — the one field on this API that names a host-visible path.
        The orchestrator only ever sends ``agent_nas_root()`` output, but "the only
        caller is well-behaved" is not a boundary: validate at the boundary
        (``/`` or ``/mnt/workspace`` itself would hand a bound exec the whole user
        root, and a ``..`` segment walks out of the mount entirely).
        """
        if value is None:
            return None
        if not value.startswith(_AGENT_ROOT_PREFIX):
            msg = f"agent_root must start with {_AGENT_ROOT_PREFIX!r}: {value!r}"
            raise ValueError(msg)
        if ".." in value.split("/"):
            msg = f"agent_root must not contain a '..' segment: {value!r}"
            raise ValueError(msg)
        return value


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
