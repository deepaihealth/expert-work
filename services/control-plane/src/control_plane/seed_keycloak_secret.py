"""Seed the Keycloak Admin service-account secret into the vault — Stream R W4.

When ``keycloak_enabled`` is on, the control plane provisions member accounts
through the Keycloak Admin API, authenticating with a ``client_credentials``
grant whose client secret is loaded lazily from the Stream Q encrypted vault
(``secret_store.get(keycloak_admin_secret_name)``). With the ``sql_encrypted``
backend nothing else writes that key, so a brand-new dev/dogfood stack has no
way to reach Keycloak until the secret is seeded.

This one-shot CLI breaks that bootstrap gap, mirroring
:mod:`control_plane.bootstrap_admin`: it builds the same
:class:`SqlEncryptedSecretStore` the app uses and ``put``\\ s the secret under
``settings.keycloak_admin_secret_name``. Run it once after the stack is up:

.. code:: sh

    docker compose exec control-plane \\
      python -m control_plane.seed_keycloak_secret --value dev-internal-secret-rotate-me

The value may also be supplied via ``EXPERT_WORK_KEYCLOAK_ADMIN_CLIENT_SECRET``.
``put`` writes a new current version each run, so re-running after a Keycloak
client-secret rotation is the supported update path (idempotent in effect).
See ``docs/runbooks/getting-started.md`` for the end-to-end local recipe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from control_plane.encrypted_secret_store import (
    SqlEncryptedSecretStore,
    build_kek_from_b64,
)
from control_plane.settings import Settings
from expert_work.persistence import (
    DatabaseConfig,
    build_rls_sessionmaker,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.secret_store import SecretStore

logger = logging.getLogger("expert_work.control_plane.seed_keycloak_secret")

_ENV_VAR = "EXPERT_WORK_KEYCLOAK_ADMIN_CLIENT_SECRET"


class SeedValueMissingError(ValueError):
    """Neither ``--value`` nor the environment variable supplied a secret."""


def resolve_secret_value(arg_value: str | None, env: dict[str, str] | None = None) -> str:
    """Resolve the secret from ``--value`` (preferred) or the env var.

    Empty strings are treated as absent so a blank ``--value=""`` does not seed
    an unusable secret. Raises :class:`SeedValueMissingError` when neither
    source provides a non-empty value.
    """
    source = os.environ if env is None else env
    value = arg_value or source.get(_ENV_VAR)
    if not value:
        raise SeedValueMissingError(f"supply the client secret via --value or {_ENV_VAR}")
    return value


def resolve_secret_name(arg_name: str | None, settings_name: str) -> str:
    """``--name`` wins; empty/absent falls back to the Keycloak default.

    PROD-5 generalization: the vault bootstrap needs three refs on a fresh
    stack (KC admin-client secret + the two OSS keys the ``secret://`` refs in
    configmap.yaml point at), and this CLI already assembles the store — one
    optional flag beats a second seeding path.
    """
    return arg_name or settings_name


async def seed_keycloak_admin_secret(store: SecretStore, *, name: str, value: str) -> None:
    """Write the Keycloak admin client secret to the vault under ``name``."""
    await store.put(name, value)
    logger.info("seeded keycloak admin client secret into the vault")


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    if settings.secret_store_backend != "sql_encrypted":  # noqa: S105 — backend name, not a secret
        print(
            "ERROR: seed_keycloak_secret only applies to the 'sql_encrypted' secret "
            "store backend. For the local_dev backend put the value in the dev-keys "
            "env file instead."
        )
        return 2
    if settings.secret_encryption_key is None:
        print("ERROR: EXPERT_WORK_SECRET_ENCRYPTION_KEY (base64 32-byte KEK) is required")
        return 2

    try:
        value = resolve_secret_value(args.value)
    except SeedValueMissingError as exc:
        print(f"ERROR: {exc}")
        return 2

    dsn = args.dsn or settings.db_dsn
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn, pgbouncer_mode=settings.db_pgbouncer_mode)
    )
    try:
        session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
        kek = build_kek_from_b64(settings.secret_encryption_key.get_secret_value())
        store = SqlEncryptedSecretStore(session_factory, kek=kek)
        name = resolve_secret_name(args.name, settings.keycloak_admin_secret_name)
        await seed_keycloak_admin_secret(store, name=name, value=value)
    finally:
        await engine.dispose()

    print(f"OK: seeded '{name}' into the vault")
    return 0


def main() -> None:
    """CLI entrypoint — ``python -m control_plane.seed_keycloak_secret``."""
    parser = argparse.ArgumentParser(
        prog="python -m control_plane.seed_keycloak_secret",
        description="Seed the Keycloak admin client secret into the encrypted vault.",
    )
    parser.add_argument(
        "--value",
        default=None,
        help=f"The client secret value (or set {_ENV_VAR}).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Override the DB DSN (default: Settings.db_dsn / EXPERT_WORK_DB_DSN).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "Vault ref name to seed (default: settings.keycloak_admin_secret_name). "
            "E.g. expert-work/platform/oss/access-key for the object-store refs."
        ),
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
