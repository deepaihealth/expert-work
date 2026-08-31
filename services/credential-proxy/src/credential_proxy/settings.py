"""``CredentialProxySettings`` — env-driven knobs for the F.5 service."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CredentialProxySettings(BaseSettings):
    """Resolved runtime settings; cheap to construct in tests."""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_CRED_PROXY_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "credential_proxy"
    log_level: str = "INFO"

    # --------------------------------------------------------------- listen
    host: str = "0.0.0.0"  # noqa: S104 - internal service, bound inside the cluster
    port: int = Field(default=8080, gt=0, le=65535)

    # --------------------------------------------------------------- admin
    #: B-31 ② — bearer token the ``/admin`` write surface requires. Unset
    #: **closes** those routes (503) rather than opening them: the admin API
    #: registers allowlist rows, and an allowlist row is what lets ``/forward``
    #: hand a tenant secret to a caller-chosen upstream. Anything that can
    #: reach this pod could otherwise mint itself one.
    admin_token: str | None = None

    # ------------------------------------------------------------------ db
    db_dsn: str = "postgresql+asyncpg://expert_work:expert_work_dev@localhost:5432/expert_work_dev"
    db_echo: bool = False

    # -------------------------------------------------------- secret store
    #: SecretStore backend (ADR-0007). ``local_dev`` for dev / test;
    #: ``aliyun_kms`` once the deploy-time backend is wired.
    secret_store_backend: str = "local_dev"  # noqa: S105 — a backend name, not a secret
    secret_store_env_file: str | None = None

    # ----------------------------------------------------------- secret cache
    #: In-process LRU over resolved secrets — keeps SecretStore read QPS
    #: down (subsystems/11 § 9 M0: flat 60s TTL).
    cache_max_size: int = Field(default=10_000, gt=0)
    cache_ttl_s: float = Field(default=60.0, gt=0, le=3600)

    # -------------------------------------------------------------- upstream
    upstream_timeout_s: float = Field(default=60.0, gt=0, le=600)

    # ---------------------------------------------------------- egress proxy
    #: Transparent CONNECT egress proxy (sandbox-egress design §3.1). On by
    #: default — egress is allowed + audited, not walled (the operator-set
    #: "audit over blocking" posture). A sandbox reaches it only when the
    #: supervisor injects ``HTTPS_PROXY`` + a valid per-sandbox token.
    egress_enabled: bool = True
    egress_port: int = Field(default=8081, gt=0, le=65535)
    #: HMAC secret shared with the supervisor (which mints the per-sandbox
    #: egress token). Dev default; set a real value in deploy.
    egress_token_secret: str = "dev-egress-token-secret-rotate-me"  # noqa: S105 — dev default
    egress_connect_timeout_s: float = Field(default=10.0, gt=0, le=120)
