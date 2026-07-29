"""``SandboxSupervisorSettings`` — env-driven knobs for the F.1 service.

Defaults aim at the local docker-compose fixture. The DB DSN points at
Postgres directly (port 5432), not PgBouncer — the supervisor issues
plain inserts / updates with no need for the bouncer pool.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SandboxSupervisorSettings(BaseSettings):
    """Resolved runtime settings; cheap to construct in tests."""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_SANDBOX_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "sandbox_supervisor"
    log_level: str = "INFO"

    # ------------------------------------------------------------------ db
    db_dsn: str = "postgresql+asyncpg://expert_work:expert_work_dev@localhost:5432/expert_work_dev"
    db_echo: bool = False

    # -------------------------------------------------------------- sandbox
    #: The single sandbox image (built from infra/sandbox-image): Python +
    #: office/data/media libs + soffice/poppler/ffmpeg/node + CJK fonts. The
    #: former ``minimal``/``office`` variant split was collapsed into this one
    #: image (sandbox-image-consolidation).
    sandbox_image: str = "expert-work-sandbox:dev"
    #: OCI runtime — `runc` for dev / macOS, `runsc` (gVisor) for Linux prod.
    oci_runtime: Literal["runc", "runsc"] = "runc"
    #: Stream HX-10 — host-visible path to a pinned seccomp profile JSON
    #: (``infra/sandbox-image/seccomp-profile.json``, mounted into the host
    #: at deploy time). ``None`` rides the host Docker default profile (dev).
    #: When set, startup validates the file fail-closed (exists + valid JSON)
    #: — a configured-but-unloadable profile is a security misconfig, not a
    #: transient fault, so the supervisor refuses to start.
    seccomp_profile_path: str | None = None
    #: Stream HX-10-F1 — static hostname→IP entries emitted as ``--add-host``
    #: on every sandbox container, format ``name=ip[,name2=ip2…]`` (e.g.
    #: ``credential-proxy.internal=172.30.0.10``). Needed under gVisor:
    #: runsc's netstack has no Docker embedded DNS (google/gvisor#7469), so
    #: name resolution must come from ``/etc/hosts``. Empty = no flags (dev /
    #: runc). A malformed value raises at startup — like the seccomp path,
    #: a configured-but-broken addressing entry is a misconfig, not a
    #: transient fault: silently skipping it would strand the sandbox with
    #: no proxy route under runsc.
    extra_hosts: str = ""
    #: sandbox-egress §3.3 — the transparent egress proxy the sandbox routes
    #: outbound through when its agent's egress policy is not "none". The proxy
    #: runs in the credential-proxy process (port below) and is reached over the
    #: ``credential-proxy.internal`` host already in ``extra_hosts``.
    egress_proxy_host: str = "credential-proxy.internal"
    egress_proxy_port: int = Field(default=8081, gt=0, le=65535)
    #: HMAC secret shared with the credential-proxy (which verifies the token).
    #: Dev default; set a real value in deploy (must match the proxy's).
    egress_token_secret: str = "dev-egress-token-secret-rotate-me"  # noqa: S105 — dev default
    #: Per-sandbox egress token TTL. Generous vs the idle reap TTL so a reused
    #: sandbox's baked token never expires mid-life.
    egress_token_ttl_s: int = Field(default=24 * 60 * 60, gt=0, le=7 * 24 * 60 * 60)
    #: Host identifier recorded on each sandbox_instance row. M0 single-node.
    node_name: str = "local"

    @property
    def parsed_extra_hosts(self) -> dict[str, str]:
        """``extra_hosts`` parsed to ``{hostname: ip}``; raises on bad syntax."""
        result: dict[str, str] = {}
        for entry in self.extra_hosts.split(","):
            entry = entry.strip()
            if not entry:
                continue
            hostname, sep, ip = entry.partition("=")
            hostname, ip = hostname.strip(), ip.strip()
            if not sep or not hostname or not ip:
                msg = (
                    f"malformed EXPERT_WORK_SANDBOX_EXTRA_HOSTS entry {entry!r} "
                    "(expected 'hostname=ip[,hostname2=ip2…]')"
                )
                raise ValueError(msg)
            result[hostname] = ip
        return result

    # ----------------------------------------------------------- resources
    default_cpu: float = Field(default=1.0, gt=0, le=16)
    # 512 MB OOM-killed (exit 137) weasyprint CJK PDF rendering: the cgroup
    # limit counts the process RSS (~350 MB for a typical report) PLUS the
    # tmpfs-backed /workspace + /tmp AND the page cache for the ~100-200 MB
    # Noto CJK font files weasyprint loads to subset/embed. 1 GB gives that
    # workload comfortable headroom; very large/image-heavy jobs can still
    # request more via the per-acquire ``memory_mb`` override.
    default_memory_mb: int = Field(default=1024, gt=0, le=65536)
    default_pids_limit: int = Field(default=128, gt=0, le=4096)
    default_timeout_s: int = Field(default=30, gt=0, le=300)
    #: How long ``acquire`` waits for the runner's readiness line before
    #: treating the launch as failed.
    runner_ready_timeout_s: float = Field(default=15.0, gt=0, le=120)

    # -------------------------------------------------------------- quota
    #: Per-tenant sandbox cap applied when the tenant has no
    #: ``tenant_quota`` row for the ``sandboxes`` dimension. With J.15
    #: warm sessions a sandbox stays ``IN_USE`` across a user's whole
    #: active window, so this cap is effectively the number of
    #: concurrently-active users a tenant supports (STREAM-J-DESIGN § 9).
    default_max_sandboxes: int = Field(default=50, gt=0, le=1000)

    # ------------------------------------------------------- pool (HX-6)
    #: Warm-pool target (STREAM-HX-DESIGN § 7.2-①): how many READY containers
    #: the replenisher keeps pre-launched for the single image. ``0`` disables
    #: the pool (dev / CI). Defensively clamped to [0, 16] rather than rejected
    #: — a bad value must not take the supervisor down (fail-open).
    pool_size: int = 2

    @field_validator("pool_size")
    @classmethod
    def _clamp_pool_size(cls, value: int) -> int:
        return max(0, min(16, value))

    # -------------------------------------------------------------- reaper
    reaper_interval_s: float = Field(default=10.0, gt=0, le=300)
    #: Idle TTL for a warm per-user sandbox session (Stream J.15). The
    #: reaper destroys a session whose last ``exec`` (``last_used_at``)
    #: is older than this — freeing compute; the persistent volume is
    #: kept, so the next message cold-starts a fresh container on it.
    session_idle_ttl_s: int = Field(default=15 * 60, gt=0, le=24 * 60 * 60)

    # ------------------------------------------------------- workspace J.15-补强-1
    #: Per-workspace volume size ceiling (Mini-ADR J-29 第 1 项). Mirrored
    #: into ``user_workspace.size_limit_bytes`` at row creation; supervisor
    #: rejects ``acquire()`` when ``size_bytes >= size_limit_bytes``.
    #: Default 10 GiB matches migration 0026's server_default.
    default_workspace_size_limit_mb: int = Field(default=10 * 1024, gt=0, le=1024 * 1024)

    # ------------------------------------------------------ workspace J.15-补强-2
    #: When set, the supervisor periodically archives soft-deleted
    #: workspaces (Mini-ADR J-36 lifecycle 第 2 → 第 3 档) and writes a
    #: daily snapshot of each active workspace (Mini-ADR J-29 第 2 项).
    workspace_lifecycle_enabled: bool = True
    #: ObjectStore key prefix for J-36 archives. Layout:
    #: ``{prefix}/{tenant_id}/{user_id}/{volume_name}.tar.gz``.
    workspace_archive_prefix: str = "volume-archive"
    #: ObjectStore key prefix for J-29 第 2 项 daily backups. Layout:
    #: ``{prefix}/{tenant_id}/{user_id}/{YYYY-MM-DD}/{volume_name}.tar.gz``.
    workspace_backup_prefix: str = "volume-backups"
    #: Days a daily backup snapshot is retained before retention-cleanup
    #: prunes it. The archive (J-36) is kept until 90-day hard-delete
    #: (推 M1 per Mini-ADR J-36); this knob is the rolling window only.
    workspace_backup_retention_days: int = Field(default=7, gt=0, le=365)
    #: Local hour-of-day (0-23) the daily backup sweep runs. Default 03:00
    #: lands in off-peak. Set to ``-1`` to disable the daily backup loop
    #: (archive-on-soft-delete still runs every reaper tick).
    workspace_backup_hour: int = Field(default=3, ge=-1, le=23)
    #: Hard cap on the in-memory tar.gz buffer per volume archive (see
    #: :meth:`DockerClient.archive_volume`). 1.5 GiB matches the
    #: practical single-shot ObjectStore.put ceiling — multipart is M1.
    workspace_archive_max_inflight_bytes: int = Field(default=1536 * 1024 * 1024, gt=0)

    # -------------------------------------------------------- object store
    #: Object-store backend for the J.15 archive + backup pipelines.
    #: ``memory`` is the dev / CI default; ``s3-compatible`` plugs into
    #: MinIO / Aliyun OSS for prod (ADR-0004).
    object_store_backend: Literal["memory", "s3-compatible"] = "memory"
    object_store_endpoint_url: str = ""
    object_store_region: str = "cn-hangzhou"
    object_store_bucket: str = "expert-work-volume-backups"
    object_store_access_key: str = ""
    object_store_secret_key: str = ""
    #: Deprecated — superseded by ``object_store_addressing_style``. Kept
    #: for env back-compat: existing deployments setting this bool keep
    #: getting the pre-migration ``"path" if true else "auto"`` behavior
    #: (see ``effective_object_store_addressing_style``).
    object_store_use_path_style: bool = True
    #: S3 addressing style — set explicitly to override the legacy bool
    #: above. ``"path"`` for MinIO local dev, ``"virtual"`` for Aliyun OSS
    #: prod (W0 real-bucket finding: OSS rejects path-style addressing).
    #: ``None`` (default) falls back to the legacy bool for back-compat.
    object_store_addressing_style: Literal["path", "virtual", "auto"] | None = None

    @property
    def effective_object_store_addressing_style(self) -> Literal["path", "virtual", "auto"]:
        """Explicit ``object_store_addressing_style`` wins; otherwise
        derived from the legacy ``object_store_use_path_style`` bool
        (``True``→``"path"``, ``False``→``"auto"`` — the pre-migration
        ternary in ``factory.py``) so existing deployments keep working
        unchanged."""
        if self.object_store_addressing_style is not None:
            return self.object_store_addressing_style
        return "path" if self.object_store_use_path_style else "auto"
