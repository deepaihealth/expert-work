"""Control Plane settings — Stream B.1.

Pydantic v2 ``BaseSettings`` with ``EXPERT_WORK_`` env prefix. The default
values point at the local ``infra/docker-compose.yml`` PgBouncer so a
fresh checkout boots without env tweaks.

ADR B-5 (STREAM-B-DESIGN.md § 3): ``auth_mode`` is the gate between
header-trust dev mode and the prod-mode startup guard that refuses to
boot until C.1 OIDC middleware lands.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from expert_work.protocol import PROVIDER_CATALOG, Provider, Tool
from orchestrator.tools.sandbox_image_contract import MAX_TIMEOUT_S

#: Default tenant UUID assigned to header-less dev requests. ``00...00`` is
#: deliberately the nil UUID so it sticks out in audit_log dumps.
DEFAULT_DEV_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000000")

#: Default actor id for dev requests with no ``X-Expert-Work-Actor`` header.
DEFAULT_DEV_ACTOR_ID: str = "anonymous"


class Settings(BaseSettings):
    """Resolved runtime settings; cheap to construct in tests."""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_",
        case_sensitive=False,
        # Service file overrides come from environments/<env>.yaml; this
        # only honours real env vars. Keep it strict so typos surface.
        extra="ignore",
    )

    # ------------------------------------------------------------------ identity
    service_name: str = "control_plane"
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    # ------------------------------------------------------------------ auth (B-5)
    auth_mode: Literal["dev", "prod"] = "dev"
    default_dev_tenant_id: UUID = DEFAULT_DEV_TENANT_ID
    default_dev_actor_id: str = DEFAULT_DEV_ACTOR_ID

    # ------------------------------------------------------------------ database
    db_dsn: str = "postgresql+asyncpg://expert_work:expert_work_dev@localhost:6432/expert_work_dev"
    db_pgbouncer_mode: bool = True
    db_echo: bool = False

    #: Persistence backend for the control-plane stores — agent_spec /
    #: thread_meta / audit / api_key / role_binding / service_account /
    #: tenant_quota / token_reservation / tenant_config / feedback
    #: (ADR B-6). ``memory`` (M0 dev / tests — state is lost on restart)
    #: or ``sql`` (durable — Postgres via ``db_dsn``). ``sql`` requires
    #: the schema to exist: run the Alembic migrations first (the
    #: ``migrate`` one-shot service in ``infra/docker-compose.yml``).
    store_backend: Literal["memory", "sql"] = "memory"

    # ------------------------------------------------------------------ otel
    otlp_traces_endpoint: str | None = None

    # -------------------------------------------------------- langfuse (HX-7)
    #: Self-hosted Langfuse instance for agent-level traces (ADR-0005).
    #: All three present -> the SDK-backed client; anything missing keeps
    #: the M0 RecordingLangfuseClient (Mini-ADR HX-G3 fail-open). Injected
    #: via env using this class's ``EXPERT_WORK_`` prefix --
    #: EXPERT_WORK_LANGFUSE_{HOST,PUBLIC_KEY,SECRET_KEY} -- never committed
    #: to compose files or code (compose only passes the ${...} through).
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    #: Mini-ADR OBS-L1 — scrub secrets + conversational PII out of every
    #: prompt/completion before it lands in Langfuse/ClickHouse. Default on
    #: for both dev and prod (fail-safe: a mis-config over-redacts, never
    #: leaks PII). The escape hatch is an explicit env override to false.
    langfuse_pii_masking_enabled: bool = True

    # ------------------------------------------------------------------ runtime guards
    # ADR B-1: in-process rate-limiter assumes a single replica. Override
    # to ``False`` only after C.6 ships the Redis backend.
    single_instance: bool = True

    #: Multi-replica deploy — per-replica opt-out for the "single-replica"
    #: background tasks ``create_app`` gates behind its ``enable_scheduler``
    #: parameter: the J.10 ``TriggerScheduler``, ``SkillCurator`` (Sprint
    #: #4), and ``MemoryConsolidator`` (Sprint #7). The latter two now take
    #: a ``pg_try_advisory_xact_lock`` on ``run_once`` so a second replica
    #: racing in loses the lock instead of duplicating work; setting this
    #: ``False`` stops *this* replica from even starting them (e.g. to pin
    #: them to one replica in a fleet). Wired in ``main.py`` from
    #: ``EXPERT_WORK_ENABLE_SCHEDULER``. Default ``True`` keeps today's
    #: every-replica-runs-it behavior.
    enable_scheduler: bool = True

    #: Multi-replica deploy — per-replica opt-out for the J.12 trajectory
    #: ``CurationWorker`` (``create_app``'s ``enable_curation_worker``
    #: parameter). ``False`` stops this replica from running it. Wired in
    #: ``main.py`` from ``EXPERT_WORK_ENABLE_CURATION_WORKER``.
    enable_curation_worker: bool = True

    #: Multi-replica deploy — per-replica opt-out for the quota
    #: ``ReservationReaper`` (``create_app``'s ``enable_reaper`` parameter).
    #: ``False`` stops this replica from running it. Wired in ``main.py``
    #: from ``EXPERT_WORK_ENABLE_REAPER``.
    enable_reaper: bool = True

    health_check_timeout_s: float = Field(default=5.0, gt=0)

    # Stream 16.3 — application-layer backpressure (overload guard). Sheds new
    # requests with 503 + Retry-After once this many are already in flight in
    # the heavy inner chain, so a request flood bounds memory / event-loop load
    # instead of collapsing the replica. Default-on with a generous cap that a
    # healthy single replica never reaches under normal load; tune per deploy.
    # ``0`` disables the guard. Distinct from the per-tenant rate limit (B.2 /
    # C.6, which is fairness, 429) — this is global overload protection (503).
    backpressure_max_in_flight: int = Field(default=512, ge=0)
    backpressure_retry_after_s: int = Field(default=1, gt=0)

    # ------------------------------------------------------------------ rate limit (B.2)
    # Gateway-tier limiter (subsystems/16 § 5.1, layer 1). Per-tenant
    # (layer 2, C.6) and per-provider (layer 3, E.6) tiers stack on top.
    rate_limit_enabled: bool = True
    rate_limit_burst: int = Field(default=60, gt=0)
    rate_limit_per_second: float = Field(default=20.0, gt=0)

    #: Shared HMAC salt for deriving the apikey rate-limit bucket id
    #: (``RateLimitMiddleware``). Multi-replica deploys MUST set this to the
    #: same value on every replica: without it, each process mints its own
    #: random key at startup, so the same ``X-API-Key`` header hashes to a
    #: different bucket per replica and the limiter never accumulates
    #: enough hits on one Redis bucket to trip behind a load balancer.
    #: ``None`` (default) falls back to a per-process random key — safe
    #: only for a single replica. ``SecretStr`` so it never renders in
    #: logs / repr.
    apikey_rate_limit_hmac_salt: SecretStr | None = None

    # ------------------------------------------------------------------ cancellation (B.3)
    # ADR B-2: 50 ms cadence + 50 ms scheduler drift + 100 ms handler
    # deadline_check ≈ 200 ms detection budget (verification gate #3).
    cancellation_poll_interval_s: float = Field(default=0.05, gt=0)

    # ------------------------------------------------------------------ run trigger
    #: Path to the ``.env``-style file the local-dev SecretStore reads
    #: (F.6 / ADR-0007). ``None`` → an empty store: agent runs fail at
    #: provider-key resolution with a clear error until a file is set.
    secret_store_env_file: str | None = None

    #: Stream Q (Mini-ADR Q-1) — which SecretStore backend the app wires.
    #: ``local_dev`` (default; reads ``secret_store_env_file``) /
    #: ``sql_encrypted`` (envelope-encrypted values in Postgres — admins paste
    #: keys via the web UI; requires ``store_backend == "sql"`` +
    #: ``secret_encryption_key``) / ``aliyun_kms`` (deploy-time follow-up).
    secret_store_backend: Literal["local_dev", "sql_encrypted", "aliyun_kms"] = "local_dev"  # noqa: S105 — backend selector enum value, not a password

    #: Stream Q (Mini-ADR Q-2) — base64-encoded 32-byte AES-256 KEK for the
    #: ``sql_encrypted`` backend. ``SecretStr`` so it never renders in logs /
    #: repr. Required (and length-validated at boot) when
    #: ``secret_store_backend == "sql_encrypted"``.
    secret_encryption_key: SecretStr | None = None

    # ------------------------------------------------------------------ checkpointer (E.1)
    #: LangGraph checkpointer backend. ``memory`` (M0 dev / tests — run
    #: state is lost on restart) or ``postgres`` (durable — a run's
    #: graph state survives a process restart). ``postgres`` requires
    #: ``checkpointer_dsn``; the app refuses to boot without it.
    checkpointer_backend: Literal["memory", "postgres"] = "memory"

    #: libpq connection string for the Postgres checkpointer. Use the
    #: sync-driver scheme (``postgresql://...``) — ``AsyncPostgresSaver``
    #: manages its own async pool. ``None`` is only valid when
    #: ``checkpointer_backend`` is ``memory``.
    checkpointer_dsn: str | None = None

    # ------------------------------------------------------------------ tools (E.7)
    #: Base URL of the self-hosted SearXNG instance backing the builtin
    #: ``web_search`` tool (design ``web-search-searxng-builtin-and-tavily-mcp``).
    #: SearXNG is free + needs no API key, so setting this makes
    #: ``web_search`` available with zero credential config. It is the only
    #: builtin web-search backend (M2 — premium Tavily moved to the platform
    #: MCP catalog). ``None`` → no builtin backend; an agent declaring
    #: ``web_search`` then fails at build with a clear error.
    #: Reached over the internal network only; never expose SearXNG publicly.
    web_search_searxng_base_url: str | None = None

    #: Path to a JSON file listing the platform's MCP servers —
    #: ``[{"name", "command": [...], "env": {...}}]`` (STREAM-E-DESIGN
    #: Mini-ADR E-17). The orchestrator launches one subprocess per
    #: entry, so the list is operator-controlled — never tenant input.
    #: ``None`` → an empty MCP pool (no servers).
    mcp_servers_config_file: str | None = None

    #: Base URL of the Sandbox Supervisor's HTTP API — backs the built-in
    #: ``exec_python`` tool (Stream F). ``None`` → ``exec_python`` is
    #: unavailable; an agent that declares it fails at build time with a
    #: clear error.
    sandbox_supervisor_url: str | None = None

    #: 波 1 —— 沙箱后端选择。``"supervisor"`` 走本地 docker supervisor
    #: (``sandbox_supervisor_url``);``"agent_sandbox"`` 走 ACS Agent
    #: Sandbox(E2B SDK)。``None`` 时按老行为推断:设了
    #: ``sandbox_supervisor_url`` 即视作 ``"supervisor"``,否则沙箱能力关闭。
    sandbox_backend: Literal["supervisor", "agent_sandbox"] | None = None
    #: Agent Sandbox 接入 —— gateway 域名(E2B SDK 的 ``domain``)。
    sandbox_e2b_domain: str | None = None
    #: Agent Sandbox 接入 —— API key。
    sandbox_e2b_api_key: str | None = None
    #: Agent Sandbox 接入 —— SandboxSet 模板名(池领取的来源)。
    sandbox_e2b_template: str | None = None
    #: Agent Sandbox 接入 —— 沙箱出网走的 credential-proxy 主机 + 端口
    #: (sandbox-egress §3.3)。默认值与 sandbox-supervisor 自己的
    #: ``egress_proxy_host``/``egress_proxy_port`` 同形态,但集群内可达性
    #: 由 K8s Service 名解出,不是同一个进程内配置。
    sandbox_egress_proxy_host: str = "credential-proxy.expert-work.svc.cluster.local"
    sandbox_egress_proxy_port: int = 8081
    #: HMAC secret,铸沙箱的出网 token(``mint_egress_token``)。必须与
    #: credential-proxy 侧的 ``EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET``
    #: 一致 —— 两边各自配置,这里独立起一个配置项而不是复用
    #: sandbox-supervisor 的同名 env(control-plane 与 sandbox-supervisor
    #: 是两个不同的部署单元,没有共享 env 的机制)。Dev 默认值与
    #: credential-proxy / sandbox-supervisor 的 dev 默认一致,便于本地联调。
    sandbox_egress_token_secret: str = "dev-egress-token-secret-rotate-me"  # noqa: S105

    #: 铸沙箱出网 token 的有效期。与 sandbox-supervisor 的
    #: ``egress_token_ttl_s`` 解析到**同一个**环境变量
    #: ``EXPERT_WORK_SANDBOX_EGRESS_TOKEN_TTL_S``(两侧前缀不同、字段名不同,
    #: 拼出来是同一个名字,同 ``sandbox_egress_token_secret`` 的套路)——
    #: ``test_shared_egress_settings_resolve_to_the_same_env_var`` 钉住这条。
    #:
    #: 补这个旋钮之前,云侧(``AgentSandboxClient``)只能吃 dataclass 默认值:
    #: 运维在 supervisor 上调短 TTL,两个后端铸出的 token 有效期就差着倍数,
    #: 而当时只比默认值的漂移闸完全看不见。上界与 supervisor 侧同款
    #: (``le=7*24*60*60``);下界**不**同款——supervisor 没有
    #: ``_max_warm_age_s()`` 这道热会话自愈闸,``gt=0`` 对它是安全的,但云后端
    #: 的 ``AgentSandboxClient._max_warm_age_s()``(``egress_token_ttl_s // 2``)
    #: 要求 ``_max_warm_age_s() + MAX_TIMEOUT_S < egress_token_ttl_s``
    #: (``test_max_warm_age_leaves_room_under_the_egress_token_ttl``)才不至于
    #: 让热会话先撞出网 407 才轮到强制重建——反解出 ``ttl > 2 * MAX_TIMEOUT_S``。
    #: 下界钉这个不变式的边界而不是随手挑一个数,``MAX_TIMEOUT_S`` 优先从
    #: orchestrator 那侧 import(而不是复述成新的字面量,又造一份会漂的副本)
    #: ——control-plane 已经依赖 orchestrator 作为库(``runtime.py`` 就 import
    #: 了 ``AgentSandboxClient``),这里不是新增的依赖方向。
    #: ``test_max_warm_age_leaves_room_at_the_ttl_floor`` 钉住"下界本身"仍然
    #: 满足这条不变式,而不只是钉默认值。
    sandbox_egress_token_ttl_s: int = Field(
        default=24 * 60 * 60, gt=2 * MAX_TIMEOUT_S, le=7 * 24 * 60 * 60
    )

    #: ``secret://`` reference to the embedding API key — backs long-term
    #: memory (Stream J.3). ``None`` → no embedder; an agent that declares
    #: ``memory.long_term`` fails at build time with a clear error.
    #:
    #: Stream O (Mini-ADR O-5) — **deprecated**: prefer
    #: ``platform_provider_credentials[<embedding_provider>]``. Removal
    #: scheduled M1 Q? after the embedder migrates to
    #: :class:`CredentialsResolver`.
    embedding_api_key_ref: str | None = None

    #: Embedding model name for long-term memory (Stream J.3). The default
    #: is qwen DashScope's ``text-embedding-v4`` (1024-dim — matches
    #: ``EXPERT_WORK_EMBEDDING_DIM``).
    embedding_model: str = "text-embedding-v4"

    #: Stream O (Mini-ADR O-9) — the catalog provider whose credential the
    #: embedder resolves through :class:`CredentialsResolver`. The embedder
    #: previously carried only a model name + a hardcoded base URL; the
    #: provider id is what keys the per-tenant credential lookup. Default
    #: ``qwen`` matches the default ``embedding_model``.
    embedding_provider: Provider = "qwen"

    #: Knowledge-retrieval reranker (Stream J.5). ``rerank_api_key_ref``
    #: ``None`` → no reranker; hybrid search then returns the RRF-fused
    #: order without an LLM rerank pass. ``rerank_provider`` is an
    #: OpenAI-compatible provider id (``ModelSpec.provider``).
    #:
    #: Stream O (Mini-ADR O-5) — ``rerank_api_key_ref`` **deprecated**;
    #: prefer ``platform_provider_credentials[rerank_provider]``.
    #: Removal scheduled M1 Q? after the reranker migrates to
    #: :class:`CredentialsResolver`.
    rerank_api_key_ref: str | None = None
    rerank_provider: str = "qwen"
    rerank_model: str = "qwen-plus"

    # ------------------------------------------------------------------ object storage (J.6)
    #: Object-store backend for uploaded images (Stream J.6). ``"memory"``
    #: (default) keeps a fresh checkout / CI booting without MinIO;
    #: ``"s3-compatible"`` points at MinIO / OSS / S3 and requires the
    #: ``object_store_*`` fields below.
    object_store_backend: Literal["memory", "s3-compatible"] = "memory"
    object_store_endpoint_url: str | None = None
    object_store_region: str = "us-east-1"
    object_store_bucket: str = "expert-work"
    #: ``secret://`` references to the S3 access / secret keys — required
    #: when ``object_store_backend`` is ``"s3-compatible"``.
    object_store_access_key_ref: str | None = None
    object_store_secret_key_ref: str | None = None
    #: S3 addressing style. ``"path"`` (default) matches MinIO local dev.
    #: 阿里云 OSS prod MUST use ``"virtual"`` — W0 real-bucket testing found
    #: OSS rejects path-style addressing (``SecondLevelDomainForbidden``)
    #: and does not reliably resolve ``"auto"`` to virtual-hosted style.
    object_store_addressing_style: Literal["path", "virtual", "auto"] = "path"

    # ------------------------------------------------------------------ multimodal input (J.6)
    #: Per-image upload size cap (default 10 MiB) and per-run image count
    #: cap — boundary limits enforced by the image upload endpoint.
    multimodal_max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    multimodal_max_images_per_run: int = Field(default=8, gt=0)
    #: Accepted image content types for upload.
    multimodal_allowed_content_types: tuple[str, ...] = (
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    )
    #: Per-document upload size cap (default 25 MiB) — a document lands in the
    #: user's persistent workspace where ``read_document`` parses it. Bigger
    #: than images: office docs / PDFs run larger.
    document_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    #: Accepted document content types for upload → workspace. The parse runs
    #: in the sandbox (read_document); the control-plane only lands the bytes.
    document_allowed_content_types: tuple[str, ...] = (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/markdown",
        "text/csv",
    )

    # ------------------------------------------------------------------ auth (C.1)
    # OIDC issuer used to validate the ``iss`` JWT claim and to derive
    # the JWKS endpoint when ``oidc_jwks_uri`` is not set. The default
    # points at the Keycloak service in ``infra/docker-compose.yml``.
    oidc_issuer: str = "http://keycloak:8080/realms/expert-work"

    #: Accepted JWT ``aud`` values. Anthropic / Keycloak service-account
    #: tokens typically carry the client id here.
    oidc_audience: list[str] = Field(default_factory=lambda: ["expert-work-api-internal"])

    #: Optional explicit JWKS URI. ``None`` → derived from ``oidc_issuer``
    #: + Keycloak's well-known ``/protocol/openid-connect/certs`` path.
    oidc_jwks_uri: str | None = None

    #: Per-process TTL for the JWKS cache (seconds). Keycloak default key
    #: rotation cadence is daily; 300s gives bounded propagation delay
    #: without hammering Keycloak.
    oidc_jwks_cache_ttl_s: int = Field(default=300, gt=0)

    #: Leeway when validating ``exp`` (seconds). Clock skew tolerance.
    oidc_jwt_leeway_s: int = Field(default=30, ge=0)

    # ------------------------------------------------------ Keycloak Admin (Stream R)
    #: Toggle the real Keycloak Admin REST client. ``False`` (default) wires a
    #: ``FakeKeycloakAdminClient`` so dev/CI never depend on a live Keycloak;
    #: integration / prod set it ``True`` to provision real accounts.
    keycloak_enabled: bool = False

    #: Keycloak base URL for the Admin API + service-account token grant.
    keycloak_base_url: str = "http://keycloak:8080"

    #: Realm the Admin client manages (member accounts live here).
    keycloak_realm: str = "expert-work"

    #: Confidential client whose service account holds ``manage-users`` and
    #: drives the Admin API (``expert-work-api-internal`` already has
    #: ``serviceAccountsEnabled``).
    keycloak_admin_client_id: str = "expert-work-api-internal"

    #: Vault name of the service-account client secret (Stream Q
    #: ``SqlEncryptedSecretStore``). The secret value never lives in settings.
    keycloak_admin_secret_name: str = "expert-work/platform/keycloak/admin-client-secret"  # noqa: S105 — vault key NAME, not a secret value

    #: Lifespan (seconds) of the Keycloak ``execute-actions-email`` set-password
    #: link sent on member invite. Default 24h.
    keycloak_email_action_lifespan_s: int = Field(default=86400, gt=0)

    #: Stream ACCT — email of the first platform ``system_admin`` to auto-grant
    #: on first login. When set, the user whose verified JWT email matches gets
    #: a platform-scope ``system_admin`` binding the first time they log in,
    #: **only while the system holds zero platform admins** (zero-admin gate).
    #: This replaces the manual ``python -m control_plane.bootstrap_admin`` step
    #: on a fresh deployment; the script stays as a break-glass fallback.
    #: ``None`` (default) disables the auto-grant entirely.
    bootstrap_admin_email: str | None = None

    #: Stream ACCT — one-time setup-wizard token. ``POST /v1/setup`` (the
    #: unauthenticated first-run endpoint that creates the first platform
    #: ``system_admin``) REQUIRES the request to carry a matching
    #: ``X-Setup-Token`` header. When unset, ``/v1/setup`` is refused outright
    #: — closing the deploy-window hijack where anyone reaching the instance
    #: before the operator could seize ``system_admin``. Clear the env after
    #: first run. ``GET /v1/setup/status`` needs no token.
    setup_token: str | None = None

    #: Stream ACCT — reserved tenant the first platform admin is homed to (the
    #: wizard's "[Platform]" tenant). Distinct from the dev nil tenant and the
    #: mTLS system-sentinel; business tenants are created separately afterwards.
    platform_tenant_id: UUID = UUID("11111111-1111-1111-1111-111111111111")

    #: Path-prefix exemption list. Health + metrics are always allowed
    #: through; ``/v1/webhooks`` is exempt because an external webhook
    #: caller has no expert_work principal — that endpoint authenticates with
    #: a per-trigger secret instead (Stream J.10 / Mini-ADR J-42);
    #: ``/v1/setup`` is the unauthenticated first-run wizard (gated by the
    #: setup token + the zero-admin invariant). Any other path requires a
    #: valid JWT in M0.
    auth_exempt_path_prefixes: list[str] = Field(
        default_factory=lambda: ["/healthz", "/metrics", "/v1/webhooks", "/v1/setup"],
    )

    # ------------------------------------------------------------------ mTLS (C.2)
    #: Toggle the mTLS auth branch. When ``False``, ``AuthMiddleware``
    #: only accepts JWTs. Useful for environments where the proxy hasn't
    #: been configured to forward XFCC yet.
    mtls_enabled: bool = True

    #: Header forwarded by the reverse proxy (nginx / Envoy / Istio).
    #: Lowercase per Starlette header normalisation.
    mtls_xfcc_header_name: str = "x-forwarded-client-cert"

    #: CN allowlist for service certificates. Empty list means **no
    #: service is allowed in** — explicit opt-in only.
    mtls_allowed_service_subjects: list[str] = Field(
        default_factory=lambda: ["orchestrator", "sandbox-supervisor"],
    )

    #: Sentinel tenant id assigned to mTLS-authenticated principals.
    #: Internal handlers (e.g. ``/v1/quota/*``) must read the *target*
    #: tenant from the request body — mTLS only proves the caller, not
    #: the tenant they are calling for.
    mtls_system_tenant_id: UUID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    #: Reject XFCC elements without a SPIFFE-style ``URI`` SAN. Off by
    #: default — Keycloak / dev certs typically don't set one.
    mtls_require_uri_san: bool = False

    # ------------------------------------------------------------------ quota (C.5)
    #: Redis DSN for the quota engine (subsystems/16 § 3.2). When unset,
    #: ``create_app`` falls back to the in-memory quota service so unit
    #: tests stay fast. Set to ``redis://redis:6379/0`` in dev /
    #: production.
    quota_redis_url: str | None = None

    #: Default per-tenant QPS limit applied when no ``tenant_quota`` row
    #: exists for the ``qps`` dimension. ``None`` means "unlimited"
    #: (M0 dev default — production sets a value through
    #: ``tenant_quota``).
    quota_default_qps_limit: int | None = None

    #: Burst capacity for the default ``qps`` bucket. Ignored when
    #: ``quota_default_qps_limit`` is None.
    quota_default_qps_burst: int = Field(default=120, gt=0)

    #: Reservation timeout (seconds). After this many seconds a
    #: reservation in ``RESERVED`` state is auto-released by the reaper
    #: (subsystems/16 § 5.4 — 30 minutes).
    quota_reservation_max_age_s: int = Field(default=30 * 60, gt=0)

    #: How often the reaper scans for expired reservations.
    quota_reaper_interval_s: int = Field(default=10 * 60, gt=0)

    #: Number of reservations to release per reaper cycle. Caps the
    #: blast radius of a misconfiguration that leaks reservations en
    #: masse.
    quota_reaper_batch_size: int = Field(default=100, gt=0)

    # ------------------------------------------------------ knowledge ingest durability (KB)
    #: Whether the knowledge ingestion recovery worker runs. It CAS-claims
    #: stuck documents (pending / lease-expired processing) and re-drives them
    #: from retained bytes, so a crash mid-ingest never strands a document.
    enable_knowledge_recovery_worker: bool = True

    #: How often the recovery worker scans for stuck documents.
    knowledge_recovery_interval_s: int = Field(default=30, gt=0)

    #: Max documents claimed per recovery cycle.
    knowledge_recovery_batch_size: int = Field(default=20, gt=0)

    #: Ingestion lease — a claimed document past this with no terminal state is
    #: treated as crashed and re-claimable. Generous: longer than any single
    #: document's ingest so the fast path is not pre-empted.
    knowledge_ingest_lease_s: int = Field(default=300, gt=0)

    #: Bounded ingest attempts before a document goes terminally failed.
    knowledge_ingest_max_attempts: int = Field(default=5, gt=0)

    # ------------------------------------------------------------------ memory DLQ (K.K7)
    #: Stream K.K7 — how often the memory writeback DLQ worker scans
    #: for retry-ready rows. Per-row backoff schedule is owned by the
    #: worker itself (1 min → 5 → 30 → 2 h → 6 h); this is the cycle
    #: cadence, kept short so a freshly-enqueued row is picked up
    #: promptly.
    memory_dlq_worker_interval_s: int = Field(default=30, gt=0)

    # ------------------------------------------------------------------ trigger scheduler (J.10)
    #: Stream J.10 — how often the trigger scheduler sweeps the
    #: ``agent_trigger`` table for due cron triggers. Cron granularity
    #: is one minute, so 60s is the natural cadence.
    trigger_scheduler_interval_s: int = Field(default=60, gt=0)

    #: Max triggers fired per scheduler sweep — caps a single cycle's work.
    trigger_scheduler_batch_size: int = Field(default=100, gt=0)

    #: Spec 1 PR4 Task 3 — the debug console's "fire now" synchronous endpoint
    #: polls the fired run to a terminal status, capped at this many seconds;
    #: past it the endpoint returns ``delivery="pending"``.
    trigger_fire_now_timeout_s: int = Field(default=60, gt=0)

    # --- Stream 9.4 (HA failover) — orphaned-run sweep ----------------------
    #: Master switch for the orphaned-run recovery sweep.
    enable_orphan_sweep: bool = Field(default=True)
    #: How often the sweep scans for runs whose ownership lease expired. Should
    #: be a few times the run lease TTL so a brief GC pause never trips it.
    orphan_sweep_interval_s: int = Field(default=15, gt=0)
    #: Max orphans handled per sweep cycle.
    orphan_sweep_batch_size: int = Field(default=20, gt=0)
    #: When ``True`` an orphan is reclaimed + resumed from its checkpoint (hot
    #: handoff). ``False`` marks it errored (conservative — human/client sees it).
    ha_auto_reclaim: bool = Field(default=True)
    #: A run reclaimed this many times is marked errored instead of respawned —
    #: stops a run that crashes its owner every time (OOM) from looping forever.
    orphan_max_reclaims: int = Field(default=3, gt=0)

    # --- Stream 9.5 (distributed run queue) ---------------------------------
    #: Master switch for the run-queue worker (drains ``status='queued'`` runs
    #: submitted via ``POST /runs mode=queue``). Every instance runs it; the
    #: claim CAS makes it exactly-once across the pool.
    enable_run_queue_worker: bool = Field(default=True)
    #: How often the worker scans for queued runs (short — queue latency).
    run_queue_interval_s: float = Field(default=2.0, gt=0)
    #: Max queued runs claimed + started per cycle.
    run_queue_batch_size: int = Field(default=10, gt=0)

    #: Stream 9.5 — master switch for the approval timeout sweep (auto-rejects
    #: pending approvals past ``timeout_at``). Every instance runs it; the
    #: ``mark_decided`` CAS makes the auto-reject exactly-once across the pool.
    enable_approval_timeout_sweep: bool = Field(default=True)
    #: How often the sweep scans for expired approvals (minutes-scale lag is
    #: fine next to the 24h approval timeout horizon).
    approval_timeout_sweep_interval_s: float = Field(default=300.0, gt=0)
    #: Max expired approvals auto-rejected per cycle.
    approval_timeout_sweep_batch_size: int = Field(default=100, gt=0)

    #: Conversation IA M4 — master switch for the transcript mirror sweep
    #: (mirrors checkpoint turns into ``thread_message`` for content search).
    #: Idempotent ON CONFLICT writes → safe on every instance.
    enable_transcript_mirror_sweep: bool = Field(default=True)
    #: Sweep cadence — content search sees a new message within a minute.
    transcript_mirror_sweep_interval_s: float = Field(default=60.0, gt=0)
    #: Max threads mirrored per cycle (also paces the initial backfill).
    transcript_mirror_sweep_batch_size: int = Field(default=200, gt=0)

    #: Stream J.10 (Mini-ADR J-26 (2)) — max cron triggers a tenant may
    #: register. Caps the scheduler's per-sweep work + a runaway client.
    max_cron_triggers_per_tenant: int = Field(default=100, gt=0)
    #: HX-9 (STREAM-HX § 13) — max outbound webhook endpoints a tenant may
    #: register. Caps the delivery worker's per-event fan-out + a runaway client.
    max_webhook_endpoints_per_tenant: int = Field(default=50, gt=0)

    # ------------------------------------------------------------------ curation worker (J.12)
    #: Stream J.12 — how often the curation worker scans the L7 trajectory
    #: ObjectStore for new candidates. Curation is not latency-sensitive;
    #: a few minutes is plenty.
    curation_worker_interval_s: int = Field(default=300, gt=0)

    #: Stream SE (SE-6d) — the Layer B skill-evolution worker. OFF by default:
    #: it distils + replay-verifies candidate skills into DRAFTs, which only
    #: matter once the SE-7 governance gate can promote them. Enable per-env
    #: once governance + the SE-9 benchmark are in place.
    enable_skill_evolution_worker: bool = Field(default=False)
    #: Evolution sweeps run real replays (LLM + graph) — keep the cadence slow.
    skill_evolution_worker_interval_s: int = Field(default=600, gt=0)

    #: SE-7d regression-rollback monitor (gated OFF by default). Periodically
    #: archives auto-promoted ACTIVE distilled skills that regressed. Shares the
    #: evolution worker's circuit breaker so rollbacks trip the auto-promote
    #: channel — only meaningful with ``enable_skill_evolution_worker`` on.
    enable_skill_rollback_monitor: bool = Field(default=False)
    #: Rollback sweeps are cheap (DB-only); hourly is plenty.
    skill_rollback_monitor_interval_s: int = Field(default=3600, gt=0)
    #: Rolling outcome window (days) per version for the regression test.
    skill_rollback_window_days: int = Field(default=7, gt=0)
    #: Stream HX-2 — FeedbackConsumerWorker (user 👎 → memory review flags).
    #: DB-only (no LLM); enabled by default so the learning loop's human
    #: signal is live out of the box.
    enable_feedback_consumer: bool = Field(default=True)
    #: 👎 volume is low; 10 minutes keeps the loop responsive without noise.
    feedback_consumer_interval_s: int = Field(default=600, gt=0)
    #: HX-9 (STREAM-HX § 13) — outbound webhook delivery worker. Drains the
    #: ``webhook_delivery`` queue (sign + POST). Enabled by default; the
    #: enqueue side (PR3b) is what actually populates the queue.
    enable_webhook_delivery: bool = Field(default=True)
    webhook_delivery_interval_s: int = Field(default=15, gt=0)
    #: Per-tenant in-flight delivery cap — a slow tenant endpoint cannot
    #: starve others (Mini-ADR HX-J4).
    webhook_delivery_per_tenant_concurrency: int = Field(default=4, gt=0)
    #: P1-S2.1d — eval platform drain worker. Claims queued ``eval_run`` rows
    #: and runs the suite through the capability-eval engine. Gated OFF by
    #: default: it depends on the ``tools/eval`` harness being on the path, so
    #: only ops/eval deployments that have it arm the loop.
    enable_eval_worker: bool = Field(default=False)
    eval_worker_interval_s: int = Field(default=300, gt=0)
    #: 11.4/11.5 — the model the live eval agent (adversarial / trace suites)
    #: runs under. It resolves the tenant's *platform-configured* credential
    #: (Y-2), so this provider must be one the tenant has a key for; a missing
    #: credential fails the build → job ERROR (never a fake fallback). Set to
    #: match your platform-configured provider before enqueuing those suites.
    eval_agent_provider: str = Field(default="anthropic")
    eval_agent_model: str = Field(default="claude-sonnet-4-6")

    #: Stream RT-5 — production quality monitor. Since PR-3b (§14) this is a
    #: **deploy-level hard-off override**, not the day-to-day switch: the workers
    #: always start, and the effective on/off is ``enable_quality_monitor AND
    #: platform_quality_config.enabled`` (the UI toggle). Default True (subsystem
    #: available); set False to force the whole subsystem off regardless of the
    #: DB config. The UI ``enabled`` defaults off, so a fresh install is still
    #: OFF until an operator opts in (judge tokens cost money).
    enable_quality_monitor: bool = Field(default=True)
    quality_monitor_interval_s: int = Field(default=300, gt=0)
    #: Per-run sampling probability (hash-bucket, deterministic). Primary cost
    #: knob; low by default. Low-volume agents produce a sparse series.
    quality_sampling_rate_pct: int = Field(default=5, ge=0, le=100)
    #: Per-tenant per-day judged-run ceiling (runaway guard, not the main knob).
    quality_daily_cap: int = Field(default=500, gt=0)
    #: Candidate rows scanned per drain batch. Raise on a high-run-rate platform
    #: so the sampler keeps up (it drains multiple batches per cycle regardless).
    quality_monitor_batch_size: int = Field(default=200, gt=0)
    #: Judge model + provider (platform-credentialed, Haiku-tier for cost).
    quality_judge_provider: str = Field(default="anthropic")
    quality_judge_model: str = Field(default="claude-haiku-4-5-20251001")
    #: RT-5 PR-2 (RT-ADR-24/25) — quality-drift detector. Started alongside the
    #: sampler (same ``enable_quality_monitor`` gate): it reads the quality
    #: series and raises a ``quality.drift`` webhook when an agent's recent mean
    #: drops below its baseline. Compares the last ``recent_window_h`` against the
    #: ``baseline_window_h`` before it; needs ``min_samples`` in each window.
    quality_drift_interval_s: int = Field(default=3600, gt=0)
    quality_drift_recent_window_h: int = Field(default=24, gt=0)
    quality_drift_baseline_window_h: int = Field(default=168, gt=0)
    quality_drift_min_samples: int = Field(default=10, gt=0)
    #: Relative drop ``(baseline - recent) / baseline`` that raises an alert.
    quality_drift_threshold: float = Field(default=0.15, gt=0, le=1)
    #: Per-(tenant, agent) suppression window after an alert (no repeat spam).
    quality_drift_cooldown_h: int = Field(default=24, gt=0)

    #: SE-13 pre-evolution domain research (gated OFF by default). On cold start
    #: of an agent's evolution, research the tenant KB (+ optionally public web)
    #: and persist a DRAFT prior for the skill generator. ``web_search`` is a
    #: separate, also-off toggle (cost + egress); priors are cached ``ttl_days``.
    enable_domain_research: bool = Field(default=False)
    domain_research_web_search_enabled: bool = Field(default=False)
    domain_research_ttl_days: int = Field(default=30, gt=0)

    #: Max trajectories examined per curation sweep — caps a cycle's work.
    curation_worker_batch_size: int = Field(default=200, gt=0)

    #: Stream J.12 (Mini-ADR J-43) — max curated eval-dataset rows a
    #: tenant may hold. Checked at create + promote; "current count"
    #: ceiling, the table is the authoritative count (like cron quota).
    max_eval_dataset_rows_per_tenant: int = Field(default=1000, gt=0)

    # --------------------------------------------------------- skill curator (Sprint #4)
    #: Capability Uplift Sprint #4 (Mini-ADR U-26) — how often the
    #: Curator sweeps every tenant + applies the state-machine
    #: transitions. Default 86400 = once per day. Tests dial this down
    #: to seconds to verify state transitions.
    skill_curator_interval_s: int = Field(default=86_400, gt=0)

    #: TTL on the in-process activity throttle (Mini-ADR U-27). Default
    #: 3600 = each skill produces at most one ``last_used_at`` UPDATE
    #: per hour per replica. Tighter than the state machine cares about
    #: but loose enough to cap write amplification under high fan-out.
    skill_activity_throttle_s: int = Field(default=3_600, gt=0)

    # --------------------------------------------------- dynamic workers (1.3 Orchestrator-Worker)
    #: Platform master switch for dynamic ephemeral worker spawning (the
    #: ``spawn_worker`` tool). A manifest's ``dynamic_workers.enabled`` can
    #: opt an agent out, but cannot turn the feature on when this is False.
    enable_dynamic_workers: bool = Field(default=True)
    #: Max workers running concurrently within one parent run. Industry
    #: standard is 3 (deer-flow / hermes / Claude Code 3-5); >10 sees
    #: cost grow ~linearly with diminishing returns.
    dynamic_worker_max_concurrent: int = Field(default=3, gt=0, le=16)
    #: Cumulative worker spawns allowed across one parent run (all turns).
    #: References bound by depth+concurrency only; expert_work is a billed
    #: multi-tenant backend so this is an explicit runaway safety net.
    dynamic_worker_max_per_run: int = Field(default=16, gt=0, le=256)
    #: Per-worker ReAct iteration cap (worker workflow.max_iterations is
    #: clamped to this). 8 was tight vs comparable frameworks' subagent budgets
    #: (deer-flow children 50-150, hermes-agent 45); 16 still starved real
    #: delegated tasks — a production research+build worker hit the cap and was
    #: force-wrapped with a truncated result (2026-07-18). 32 gives a worker room
    #: to research + synthesise, still at/under a typical parent budget
    #: (default 30) and the ``le=64`` runaway ceiling; the effective cap remains
    #: ``min(parent's max_iterations, this)``.
    dynamic_worker_max_iterations: int = Field(default=32, gt=0, le=64)
    #: Tool-name allowlist a spawned worker may inherit from its parent
    #: (intersected with the parent's tools). Empty = inherit the parent's
    #: tools verbatim (still a subset of what the parent itself had).
    dynamic_worker_allowed_toolsets: list[str] = Field(default_factory=list)

    #: Platform-default wall-clock floor (seconds) for a run's deadline,
    #: applied when a manifest leaves ``policies.run_deadline_s`` at 0. A
    #: generous safety net so an unconfigured run cannot run unbounded on
    #: wall-clock (steps can be minutes); an agent that genuinely needs
    #: longer raises ``run_deadline_s`` explicitly. 3600 = 1h; 0 disables
    #: the floor (opt every run back to manifest-only). Capped at 24h.
    default_run_deadline_s: int = Field(default=3600, ge=0, le=86400)

    # --------------------------------------------------- memory consolidator (Sprint #7)
    #: Capability Uplift Sprint #7 (Mini-ADR U-34) — MemoryConsolidator
    #: sweep cadence. Default 14400 = every 4 hours (4 sweeps / day).
    #: Tighter than SkillCurator's daily because fact accumulation
    #: happens on hours-to-days, not weeks.
    memory_consolidator_interval_s: int = Field(default=14_400, gt=0)

    #: Capability Uplift Sprint #7 (Mini-ADR U-39) — default aux model
    #: name used when an agent's manifest leaves
    #: ``policies.memory_consolidation.aux_model`` unset. Sonnet rather
    #: than Haiku because the anti-mislearn classification benefits from
    #: a stronger model; this is a cold path so the cost premium is
    #: bounded.
    memory_consolidator_default_aux_model: str = "claude-sonnet-4-6"

    #: Stream O — default provider for the consolidator's aux model when
    #: the manifest leaves ``policies.memory_consolidation.aux_model``
    #: unset. The :class:`LLMRouterAuxModelAdapter` uses this to build
    #: a single-provider :class:`LLMRouter` with the appropriate
    #: credential resolved via :class:`CredentialsResolver`.
    memory_consolidator_default_aux_provider: Provider = "anthropic"

    # ----------------------------------------------- credentials catalog (Stream O)
    #: Stream O (Mini-ADR O-1) — LLM providers the platform deployment
    #: opts in to. A provider not in this list is unavailable to all
    #: tenants regardless of agent manifest (publish-time gate, Mini-ADR
    #: O-4) and cannot be configured by tenants in ``tenant`` mode.
    #: Default is the empty list — every deployment must explicitly
    #: enable providers via env (e.g.
    #: ``EXPERT_WORK_SUPPORTED_PROVIDERS=anthropic,openai,qwen``) along
    #: with a matching ``platform_provider_credentials`` entry. The
    #: startup validator (Mini-ADR O-1) refuses to boot if the two
    #: don't match.
    supported_providers: list[Provider] = Field(default_factory=list)

    #: Stream O (Mini-ADR O-1) — platform-level secret_ref per supported
    #: provider. The startup validator rejects any non-empty difference
    #: between ``supported_providers`` and the keys of this dict, so a
    #: deployment that opts a provider in **must** supply its credential.
    platform_provider_credentials: dict[Provider, str] = Field(
        default_factory=dict,
        description="provider → secret:// URI; startup validates full coverage",
    )

    #: Stream O (Mini-ADR O-1) — external tools the platform deployment
    #: opts in to. Same opt-in + tenant-mode-whitelist semantics as
    #: ``supported_providers``. Default empty; operators opt in explicitly.
    supported_tools: list[Tool] = Field(default_factory=list)

    #: Stream O (Mini-ADR O-1) — platform-level secret_ref per supported
    #: tool.
    platform_tool_credentials: dict[Tool, str] = Field(default_factory=dict)

    # --- Stream O Mini-ADR O-10: legacy → effective catalog derivation ---
    # The embedder / reranker callers migrate to CredentialsResolver. A
    # deployment that has not opted into Stream O (empty
    # ``platform_provider_credentials``) but still sets the legacy
    # ``embedding_api_key_ref`` / ``rerank_api_key_ref`` keeps working:
    # these properties gap-fill the catalog from the legacy fields. Explicit
    # Stream O config always wins; legacy only fills providers not already
    # present. The resolver is built from the ``effective_*`` view, the
    # startup validator still checks only the explicit fields. (The legacy
    # ``tavily_api_key_ref`` gap-fill was removed in M2 — see web_search.)

    @property
    def effective_platform_provider_credentials(self) -> dict[Provider, str]:
        """Explicit ``platform_provider_credentials`` plus legacy
        embedding / rerank refs gap-filling absent providers."""
        merged = dict(self.platform_provider_credentials)
        if self.embedding_api_key_ref and self.embedding_provider not in merged:
            merged[self.embedding_provider] = self.embedding_api_key_ref
        if (
            self.rerank_api_key_ref
            and self.rerank_provider in PROVIDER_CATALOG
            and self.rerank_provider not in merged
        ):
            # ``rerank_provider in PROVIDER_CATALOG`` narrows str → Provider.
            merged[self.rerank_provider] = self.rerank_api_key_ref
        return merged

    @property
    def effective_supported_providers(self) -> list[Provider]:
        """Union of explicit ``supported_providers`` and any provider the
        legacy derivation added — order: explicit first, then derived."""
        derived = list(self.supported_providers)
        for provider in self.effective_platform_provider_credentials:
            if provider not in derived:
                derived.append(provider)
        return derived

    @property
    def effective_platform_tool_credentials(self) -> dict[Tool, str]:
        """The platform's per-tool secret refs. (The legacy Tavily gap-fill
        was removed in M2 — the builtin ``web_search`` backend is now
        keyless SearXNG; Tavily moved to the platform MCP catalog.)"""
        return dict(self.platform_tool_credentials)

    @property
    def effective_supported_tools(self) -> list[Tool]:
        """Union of explicit ``supported_tools``, any tool the legacy
        derivation added, and ``web_search`` when a SearXNG backend is
        configured (SearXNG needs no credential, so it cannot ride the
        ``platform_tool_credentials`` gap-fill — gate it on the URL)."""
        derived = list(self.supported_tools)
        for tool in self.effective_platform_tool_credentials:
            if tool not in derived:
                derived.append(tool)
        if self.web_search_searxng_base_url and "web_search" not in derived:
            derived.append("web_search")
        return derived

    # ------------------------------------------------------------------ tenant rate limit (C.6)
    #: Per-tenant request bucket capacity (tokens). Drained one token
    #: per authenticated request.
    tenant_rate_limit_capacity: int = Field(default=200, gt=0)

    #: Refill rate (tokens / second) for the per-tenant bucket.
    tenant_rate_limit_refill_per_sec: float = Field(default=50.0, gt=0)

    #: Toggle for the per-tenant rate-limit middleware. ``False`` lets
    #: dev / single-tenant load tests run without the limit kicking in.
    tenant_rate_limit_enabled: bool = True

    #: Sample interval for ``quota:rate_limit_denied`` audit emissions.
    #: ``1`` audits every denial; default samples 1 in 100 per
    #: subsystems/16 § 8 (caps log volume under sustained throttle
    #: storms).
    tenant_rate_limit_audit_sample_every: int = Field(default=100, ge=1)

    #: Dedicated, tighter per-tenant bucket for the MCP probe-bearing
    #: endpoints (register / instantiate / test / list-tools). Each of those
    #: triggers a server-side outbound connection to a tenant-chosen URL, so
    #: they get a far smaller bucket than ordinary CRUD to blunt
    #: outbound-amplification / scanning (security audit #6). Gated by the
    #: same ``tenant_rate_limit_enabled`` toggle.
    mcp_probe_rate_limit_capacity: int = Field(default=10, gt=0)

    #: Refill rate (tokens / second) for the MCP probe bucket.
    mcp_probe_rate_limit_refill_per_sec: float = Field(default=0.5, gt=0)

    # --------------------------------------------------------- cross-tenant governance (HX-8)
    #: Deployment-level switch for system_admin cross-tenant access
    #: (Stream HX-8, Mini-ADR HX-H4). ``True`` (default) keeps the
    #: Stream N behaviour: ``tenant_id="*"`` aggregates and explicit
    #: tenant switches are allowed (audited). ``False`` confines every
    #: system_admin to their home tenant — both the ``"*"`` aggregate
    #: and explicit switches return 403 ``CROSS_TENANT_DISABLED`` and
    #: emit a ``SYSTEM_CROSS_TENANT_BLOCKED`` audit row. For deployments
    #: whose compliance posture forbids platform-side reads across
    #: tenant boundaries.
    cross_tenant_query_enabled: bool = True

    #: Public redirect URI for the per-user MCP OAuth callback (Stream MCP-OAUTH).
    #: Must point at the **admin-ui callback page** (not the backend endpoint):
    #: ``https://app.example.com/settings/mcp-oauth/callback``. The OAuth provider
    #: redirects the browser there (a plain navigation that carries no auth
    #: header); that SPA page then calls ``GET /v1/mcp-oauth/callback?state&code``
    #: with the logged-in user's bearer token so the request is authenticated.
    #: The value must also be registered in each connector's OAuth app allowlist.
    #: ``None`` (default) makes the OAuth initiate endpoint return 503.
    mcp_oauth_redirect_uri: str | None = None

    #: Multi-client redirect allowlist (Stream MCP-OAUTH, mcp-oauth-multi-client).
    #: A client (web app / native desktop-CLI / embedded) may supply its OWN
    #: ``redirect_uri`` at ``initiate`` so the provider returns the browser to
    #: that client. Each entry is a permitted redirect **prefix**; a candidate
    #: must origin-match (scheme+host+port for web; scheme for custom schemes)
    #: AND path-prefix-match an entry, else ``422 MCP_OAUTH_REDIRECT_NOT_ALLOWED``.
    #: ``mcp_oauth_redirect_uri`` (when set) is implicitly allowed. The redirect
    #: is never fetched server-side — this guards open-redirect, not SSRF.
    mcp_oauth_redirect_allowlist: list[str] = Field(default_factory=list)

    #: Allow RFC 8252 native-app loopback redirects (``http://127.0.0.1:<port>``
    #: / ``localhost`` / ``[::1]``, any port). Loopback is localhost-only, never
    #: remotely reachable, so it is safe to accept for desktop / CLI clients.
    mcp_oauth_allow_loopback_redirect: bool = True

    #: Path to the MCP connector catalog env-seed template (Stream MCP-OAUTH
    #: OA-5), e.g. ``configs/mcp-catalog-seed.json``. On startup each entry whose
    #: ``${VAR}`` placeholders all resolve from the environment is created (idem-
    #: potently); entries with an unset placeholder are skipped. ``None``
    #: (default) seeds nothing.
    mcp_catalog_seed_file: str | None = None

    #: When the runtime has a Redis URL (``quota_redis_url``) and
    #: ``single_instance`` is ``False``, the gateway / tenant limiter
    #: use Redis (multi-replica safe). On the default single-instance
    #: dev setup the in-process limiter is used so tests don't need a
    #: Redis container.

    # ------------------------------------------------------------------ tenant config (C.7)
    #: ``TenantConfigService`` in-memory cache TTL (seconds). Bounded
    #: by STREAM-C-DESIGN § 2.8 at 60s — keeps the hot path off the
    #: database without making admin edits invisible for too long.
    tenant_config_cache_ttl_s: int = Field(default=60, gt=0)
    #: Stream RT-4 (RT-ADR-16) — TTL (seconds) for the kill-switch status
    #: caches (``AgentDisableService`` + the ``TenantStatusService`` the run
    #: gates consult). Much shorter than ``tenant_config_cache_ttl_s`` because
    #: this is an EMERGENCY stop: a 60s TTL would let a peer replica keep
    #: admitting a disabled agent's / suspended tenant's runs for up to a
    #: minute. 5s bounds cross-replica staleness while keeping the hot path
    #: off the DB (a single indexed PK lookup on miss).
    kill_switch_cache_ttl_s: int = Field(default=5, gt=0)

    def resolve_jwks_uri(self) -> str:
        """Return the explicit JWKS URI or derive it from the issuer."""
        if self.oidc_jwks_uri:
            return self.oidc_jwks_uri
        return self.oidc_issuer.rstrip("/") + "/protocol/openid-connect/certs"
