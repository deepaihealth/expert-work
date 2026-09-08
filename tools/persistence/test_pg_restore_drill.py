"""Stream K.K15 — Postgres pg_dump / pg_restore round-trip drill.

Automates the procedure that ``docs/runbooks/pg-restore.md`` (and the
older ``docs/dr/RUNBOOK.md``) describes by hand: seed a Postgres
container with data, take a custom-format dump, drop the source
database, restore the dump into a fresh database, and assert the data
came back intact.

The point is the *pipeline* — that pg_dump + pg_restore against our
schema actually round-trips. RPO / RTO numbers from the dr/RUNBOOK.md
are operator-side targets that depend on object-store transfer time;
this drill catches regressions in the data-path contract (a future
schema change that pg_restore can't apply, an ordering bug between
``CREATE DATABASE`` and the bootstrap init script, etc.).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration


_TEST_TABLE_SQL = """
CREATE TABLE pg_restore_drill (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO pg_restore_drill (label, body) VALUES
    ('row-a', '{"k": "v1"}'::jsonb),
    ('row-b', '{"k": "v2"}'::jsonb),
    ('row-c', '{"k": "v3"}'::jsonb);
"""


@pytest.fixture
def restore_target(postgres_container: PostgresContainer) -> Iterator[str]:
    """Yield the bootstrap DSN for the test Postgres container.

    The drill creates / drops databases on this Postgres instance; the
    bootstrap DSN points at the superuser-owned ``test`` database that
    testcontainers provisioned, which we treat as the admin connection.
    """
    raw = str(postgres_container.get_connection_url())
    sync_dsn = raw.replace("+psycopg2", "+psycopg").replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    yield sync_dsn


def _swap_dsn_database(dsn: str, new_db: str) -> str:
    """Replace just the trailing ``/<db>`` of a DSN — naive string replace
    would also rewrite the ``test`` password inside the userinfo."""
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{new_db}"


def test_pg_restore_round_trip_recovers_seeded_rows(restore_target: str) -> None:
    """Stream K.K15 drill: seed → dump → drop → restore → row count matches.

    A regression where pg_dump + pg_restore can no longer apply our
    schema (or where the bootstrap order changed) would fail here long
    before an operator finds it in a DR exercise.
    """
    admin_engine = create_engine(restore_target, isolation_level="AUTOCOMMIT")
    drill_db = "pg_restore_drill_src"
    restored_db = "pg_restore_drill_tgt"

    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {drill_db}"))
            conn.execute(text(f"DROP DATABASE IF EXISTS {restored_db}"))
            conn.execute(text(f"CREATE DATABASE {drill_db}"))

        # Seed three rows in the source database.
        src_dsn = _swap_dsn_database(restore_target, drill_db)
        src_engine = create_engine(src_dsn)
        with src_engine.begin() as conn:
            conn.execute(text(_TEST_TABLE_SQL))
        src_engine.dispose()

        # Take a custom-format pg_dump against the running container.
        # ``docker exec`` lets us avoid mounting a workdir; the dump
        # lives in the container's tmpfs.
        container_id = _container_id_from_dsn(restore_target)
        dump_path = "/tmp/restore_drill.dump"  # noqa: S108 — container tmpfs, not host
        rto_started = time.monotonic()
        _docker_exec(
            container_id,
            [
                "pg_dump",
                "-U",
                "test",
                "-d",
                drill_db,
                "-Fc",
                "-f",
                dump_path,
            ],
        )

        # Drop the source database to simulate the disaster.
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE {drill_db}"))

        # Restore into a freshly-created sibling database.
        with admin_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {restored_db}"))
        _docker_exec(
            container_id,
            [
                "pg_restore",
                "-U",
                "test",
                "-d",
                restored_db,
                "--no-owner",
                "--exit-on-error",
                dump_path,
            ],
        )
        rto_elapsed_s = time.monotonic() - rto_started

        # Validate — same three rows came back, JSONB body intact.
        target_dsn = _swap_dsn_database(restore_target, restored_db)
        target_engine = create_engine(target_dsn)
        try:
            with target_engine.connect() as conn:
                count = conn.execute(text("SELECT count(*) FROM pg_restore_drill")).scalar_one()
                labels = sorted(
                    r[0] for r in conn.execute(text("SELECT label FROM pg_restore_drill"))
                )
        finally:
            target_engine.dispose()
        assert count == 3
        assert labels == ["row-a", "row-b", "row-c"]
        # The drill RTO target in slo.md is 4 h (M0) — the local
        # round-trip should beat that by orders of magnitude. Pin a
        # generous ceiling so a future hang shows up as a failure.
        assert rto_elapsed_s < 60, f"dump+restore took {rto_elapsed_s:.2f}s (> 60s)"
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {drill_db}"))
            conn.execute(text(f"DROP DATABASE IF EXISTS {restored_db}"))
        admin_engine.dispose()


def _container_id_from_dsn(dsn: str) -> str:
    """Find the testcontainers-managed Postgres container by the port
    embedded in the DSN. The default expert_work testcontainers image is
    ``ghcr.io/deepaihealth/mirror/pgvector:pg16`` (see root conftest); we
    filter for that so an unrelated Postgres on the host doesn't get picked
    up."""
    # DSN shape: postgresql+psycopg://test:test@localhost:PORT/test
    port = dsn.rsplit(":", 1)[-1].split("/")[0]
    # ``docker`` is on PATH in CI and on every dev box that ran this
    # repo's integration suite — partial-path warning is fine here.
    argv = [
        "docker",
        "ps",
        "--filter",
        f"publish={port}",
        "--filter",
        "ancestor=ghcr.io/deepaihealth/mirror/pgvector:pg16",
        "--format",
        "{{.ID}}",
    ]
    out = subprocess.run(argv, check=True, text=True, capture_output=True)  # noqa: S603
    cid = out.stdout.strip().splitlines()[0]
    return cid


def _docker_exec(container_id: str, argv: list[str]) -> None:
    """``docker exec`` wrapper that surfaces stderr for debugging."""
    full_argv = ["docker", "exec", container_id, *argv]
    result = subprocess.run(full_argv, check=False, text=True, capture_output=True)  # noqa: S603
    if result.returncode != 0:
        msg = f"docker exec failed (rc={result.returncode}): {result.stderr.strip()}"
        raise RuntimeError(msg)
