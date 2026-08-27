"""Seed the release-canary credentials + agent — X-14 P1.

``release.sh`` stage 6 runs one real agent run as the release gate
(``tools/deploy/canary.py``). That run needs two things a fresh environment
does not have: an API key with ``write`` scope (the external run plane,
``POST /v1/agents/{code}/runs``) and a registered canary agent that declares
the ``exec_python`` / ``write_file`` / ``save_artifact`` builtins. Service
accounts can only be minted through console-only endpoints, so — mirroring
:mod:`control_plane.bootstrap_admin` — this one-shot CLI writes them at the
store layer, gated by infra-level DB access instead of HTTP.

Run it once per environment, inside a control-plane pod (the venv carries
this module, the pod env carries the DSN + settings):

.. code:: sh

    kubectl -n expert-work exec -it <control-plane-pod> -- \\
      python -m control_plane.seed_canary --tenant-id <tenant uuid>

It is idempotent:

* service account ``release-canary`` — created, or reused when present;
* API key (``write`` scope) — minted only when the account has no active
  key (pass ``--rotate-key`` to mint another after losing the plaintext);
  the plaintext is printed **once** and never stored by this CLI — copy it
  into the ``canary-credentials`` k8s Secret with the printed command;
* agent ``release-canary@1.1.0`` — registered from the embedded manifest,
  or left untouched when the ``(name, version)`` already exists.

The model provider/name default to the canonical agent's choice; override
with ``--model-provider`` / ``--model-name`` to match a provider the target
environment actually has a platform key for (runbook §1.6).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from control_plane.api.agents import _spec_sha256
from control_plane.auth.api_key_verifier import mint_api_key
from control_plane.manifest.loader import ManifestLoader
from control_plane.settings import Settings
from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence import (
    DatabaseConfig,
    SqlTenantConfigStore,
    TenantConfigStore,
    build_rls_sessionmaker,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.agent_spec import (
    AgentSpecStore,
    DuplicateAgentSpecError,
    SqlAgentSpecStore,
)
from expert_work.persistence.auth import (
    ApiKeyStore,
    DuplicateServiceAccountError,
    ServiceAccountStore,
    SqlApiKeyStore,
    SqlServiceAccountStore,
)
from expert_work.protocol import AgentSpec, ApiKeyScope

logger = logging.getLogger("expert_work.control_plane.seed_canary")

_SEED_ACTOR = "seed-canary"

SERVICE_ACCOUNT_NAME = "release-canary"
DEFAULT_AGENT_CODE = "release-canary"
CANARY_AGENT_VERSION = "1.1.0"
DEFAULT_MODEL_PROVIDER = "anthropic"
DEFAULT_MODEL_NAME = "claude-sonnet-4-5"
#: One fallback so a transient primary-provider 429/outage doesn't red the
#: release gate: the canary's assertions all ride on deterministic sandbox
#: output, so WHICH model answered never weakens them (2026-08-27 拍板).
#: A masked primary failure still shows up in the Langfuse trace.
DEFAULT_FALLBACK_PROVIDER = "deepseek"
DEFAULT_FALLBACK_NAME = "deepseek-v4-pro"

#: Embedded rather than a ``manifests/`` file: the pod image ships
#: ``services/`` but not ``manifests/``, and this CLI runs in-pod. Kept
#: loadable by ``tests/test_seed_canary.py`` through the real
#: :class:`ManifestLoader` (same CI-guard pattern as
#: ``test_canonical_manifest.py``). Deliberately minimal: the three sandbox
#: builtins the canary exercises and nothing else — in particular NO
#: ``approval_required_tools`` (an approval interrupt would park the run as
#: ``paused`` and red every release).
_CANARY_MANIFEST_TEMPLATE = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: {agent_code}
  version: "{version}"
  tenant: canary
spec:
  description: >-
    Release canary agent (X-14 P1). release.sh stage 6 drives one real run
    through exec_python + write_file + save_artifact and downloads the
    artifact; the end frame must report success for the release to count.
  display_name: Release Canary
  tenant_config: {{}}
  model:
    provider: {model_provider}
    name: {model_name}
    temperature: 0.0
    max_tokens: 4096
    fallback:
      - provider: {fallback_provider}
        name: {fallback_name}
        temperature: 0.0
        max_tokens: 4096
  system_prompt:
    template: |
      You are an automated release canary agent. Follow the user's numbered
      steps exactly, calling only the tools they name, in the given order.
      Do not ask questions and do not add extra steps.
  tools:
    - {{ type: builtin, name: exec_python }}
    - {{ type: builtin, name: write_file }}
    - {{ type: builtin, name: save_artifact }}
  sandbox:
    runtime: gvisor
    resources:
      cpu: "1.0"
      memory: 1Gi
    network:
      egress: proxy
    filesystem:
      readonly_root: true
      writable:
        - /tmp
  observability:
    log_level: info
"""


class SeedCanaryError(Exception):
    """A precondition failed (unknown tenant, unresolvable duplicate)."""


def render_canary_manifest(
    *,
    agent_code: str = DEFAULT_AGENT_CODE,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    model_name: str = DEFAULT_MODEL_NAME,
    fallback_provider: str = DEFAULT_FALLBACK_PROVIDER,
    fallback_name: str = DEFAULT_FALLBACK_NAME,
) -> str:
    """The canary manifest YAML with the environment's models filled in."""
    return _CANARY_MANIFEST_TEMPLATE.format(
        agent_code=agent_code,
        version=CANARY_AGENT_VERSION,
        model_provider=model_provider,
        model_name=model_name,
        fallback_provider=fallback_provider,
        fallback_name=fallback_name,
    )


def load_canary_spec(
    *,
    agent_code: str = DEFAULT_AGENT_CODE,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    model_name: str = DEFAULT_MODEL_NAME,
    fallback_provider: str = DEFAULT_FALLBACK_PROVIDER,
    fallback_name: str = DEFAULT_FALLBACK_NAME,
) -> AgentSpec:
    """Validate the rendered manifest through the real loader.

    An invalid ``--model-provider`` fails here (pydantic ``Literal``) with
    the loader's field-level error rather than a late run-time surprise.
    """
    yaml_text = render_canary_manifest(
        agent_code=agent_code,
        model_provider=model_provider,
        model_name=model_name,
        fallback_provider=fallback_provider,
        fallback_name=fallback_name,
    )
    return ManifestLoader().load_from_string(yaml_text)


@dataclass(frozen=True)
class SeedCanaryResult:
    """Outcome of one seeding pass — every field is idempotency-aware."""

    service_account_id: UUID
    service_account_created: bool
    #: The freshly minted plaintext bearer, or ``None`` when an active key
    #: already existed (plaintext is unrecoverable here — ``--rotate-key``).
    minted_key_plaintext: str | None
    agent_created: bool
    agent_code: str


async def seed_canary(
    *,
    tenants: TenantConfigStore,
    accounts: ServiceAccountStore,
    keys: ApiKeyStore,
    specs: AgentSpecStore,
    tenant_id: UUID,
    agent_code: str = DEFAULT_AGENT_CODE,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    model_name: str = DEFAULT_MODEL_NAME,
    fallback_provider: str = DEFAULT_FALLBACK_PROVIDER,
    fallback_name: str = DEFAULT_FALLBACK_NAME,
    rotate_key: bool = False,
) -> SeedCanaryResult:
    """Create-or-reuse the canary service account, key and agent.

    The caller supplies the stores so this is unit-testable against the
    in-memory implementations; the CLI wires the SQL stores + RLS bypass
    (same shape as :func:`control_plane.bootstrap_admin.bootstrap_system_admin`).
    """
    spec = load_canary_spec(
        agent_code=agent_code,
        model_provider=model_provider,
        model_name=model_name,
        fallback_provider=fallback_provider,
        fallback_name=fallback_name,
    )
    async with bypass_rls_session():
        if await tenants.get(tenant_id=tenant_id) is None:
            raise SeedCanaryError(
                f"tenant {tenant_id} not found — create the tenant first (runbook §1.6.5)"
            )

        sa_created = False
        try:
            sa = await accounts.create(
                tenant_id=tenant_id,
                name=SERVICE_ACCOUNT_NAME,
                description="Release canary (X-14 P1) — used by release.sh stage 6 only.",
                created_by=_SEED_ACTOR,
            )
            sa_created = True
        except DuplicateServiceAccountError:
            existing_sa = next(
                (
                    s
                    for s in await accounts.list_by_tenant(tenant_id=tenant_id, limit=500)
                    if s.name == SERVICE_ACCOUNT_NAME
                ),
                None,
            )
            if existing_sa is None:
                raise SeedCanaryError(
                    f"service account {SERVICE_ACCOUNT_NAME!r} exists but could not be "
                    "listed back — check tenant_id"
                ) from None
            sa = existing_sa

        minted: str | None = None
        active_keys = [
            k
            for k in await keys.list_by_service_account(
                tenant_id=tenant_id, service_account_id=sa.id
            )
            if k.is_active
        ]
        if not active_keys or rotate_key:
            generated = mint_api_key(tenant_id=tenant_id)
            await keys.create(
                tenant_id=tenant_id,
                service_account_id=sa.id,
                prefix=generated.prefix,
                secret_hash=generated.secret_hash,
                scopes=[ApiKeyScope.WRITE],
                expires_at=None,
                created_by=_SEED_ACTOR,
            )
            minted = generated.plaintext

        agent_created = False
        try:
            await specs.create(
                tenant_id=tenant_id,
                spec=spec,
                spec_sha256=_spec_sha256(spec.model_dump(by_alias=True, mode="json")),
                created_by=_SEED_ACTOR,
            )
            agent_created = True
        except DuplicateAgentSpecError:
            logger.info("canary agent already registered: %s@%s", agent_code, CANARY_AGENT_VERSION)

    return SeedCanaryResult(
        service_account_id=sa.id,
        service_account_created=sa_created,
        minted_key_plaintext=minted,
        agent_created=agent_created,
        agent_code=agent_code,
    )


def _print_report(result: SeedCanaryResult) -> None:
    print(
        f"service account {SERVICE_ACCOUNT_NAME!r}: "
        f"{'created' if result.service_account_created else 'reused'} "
        f"(id={result.service_account_id})"
    )
    print(
        f"agent {result.agent_code}@{CANARY_AGENT_VERSION}: "
        f"{'registered' if result.agent_created else 'already registered (left untouched)'}"
    )
    if result.minted_key_plaintext is None:
        print(
            "API key: an active key already exists — nothing minted. Its plaintext "
            "is not recoverable here; pass --rotate-key to mint a new one."
        )
        return
    print()
    print("API key (shown ONCE — never logged, never stored by this CLI):")
    print(f"  {result.minted_key_plaintext}")
    print()
    print("Put it into the canary Secret (run where the cluster kubeconfig lives):")
    print("  kubectl -n expert-work delete secret canary-credentials --ignore-not-found")
    print("  kubectl -n expert-work create secret generic canary-credentials \\")
    print("    --from-literal=api-key='<paste the key printed above>' \\")
    print(f"    --from-literal=agent-code='{result.agent_code}'")


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    dsn = args.dsn or settings.db_dsn

    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn, pgbouncer_mode=settings.db_pgbouncer_mode)
    )
    try:
        session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
        result = await seed_canary(
            tenants=SqlTenantConfigStore(session_factory),
            accounts=SqlServiceAccountStore(session_factory),
            keys=SqlApiKeyStore(session_factory),
            specs=SqlAgentSpecStore(session_factory),
            tenant_id=args.tenant_id,
            agent_code=args.agent_code,
            model_provider=args.model_provider,
            model_name=args.model_name,
            fallback_provider=args.fallback_provider,
            fallback_name=args.fallback_name,
            rotate_key=args.rotate_key,
        )
    except SeedCanaryError as exc:
        print(f"ERROR: {exc}")
        return 2
    finally:
        await engine.dispose()

    _print_report(result)
    return 0


def main() -> None:
    """CLI entrypoint — ``python -m control_plane.seed_canary``."""
    parser = argparse.ArgumentParser(
        prog="python -m control_plane.seed_canary",
        description="Seed the release-canary service account, API key and agent (X-14 P1).",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        required=True,
        help="Tenant UUID the canary lives under (runbook §1.6.5 的租户).",
    )
    parser.add_argument(
        "--agent-code",
        default=DEFAULT_AGENT_CODE,
        help=f"Canary agent name / external agent_code (default: {DEFAULT_AGENT_CODE}).",
    )
    parser.add_argument(
        "--model-provider",
        default=DEFAULT_MODEL_PROVIDER,
        help=(
            "Manifest model.provider — pick one the environment has a platform "
            f"key for (default: {DEFAULT_MODEL_PROVIDER})."
        ),
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL_NAME,
        help=f"Manifest model.name (default: {DEFAULT_MODEL_NAME}).",
    )
    parser.add_argument(
        "--fallback-provider",
        default=DEFAULT_FALLBACK_PROVIDER,
        help=(
            "Fallback model.provider — a DIFFERENT provider the environment "
            "also has a platform key for, so a primary 429/outage doesn't red "
            f"the release gate (default: {DEFAULT_FALLBACK_PROVIDER})."
        ),
    )
    parser.add_argument(
        "--fallback-name",
        default=DEFAULT_FALLBACK_NAME,
        help=f"Fallback model.name (default: {DEFAULT_FALLBACK_NAME}).",
    )
    parser.add_argument(
        "--rotate-key",
        action="store_true",
        help="Mint a new API key even when an active one exists (lost plaintext).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Override the DB DSN (default: Settings.db_dsn / EXPERT_WORK_DB_DSN).",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
