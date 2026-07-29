"""Backfill: move inline skill supporting-file bytes into the object store.

skill-asset-store — new imports externalize supporting-file bytes to the
durable object store; rows imported before that (or while the store was
unconfigured) still carry base64 ``content`` inline in the
``skill_version.supporting_files`` JSONB. Dual-read means they keep working
untouched — this backfill is OPTIONAL housekeeping that shrinks fat rows and
unifies the storage story.

Per row: every inline entry's bytes are uploaded under the content-addressed
key (idempotent — identical bytes are the same object), the manifest entry is
rewritten to the external shape, and ``content_hash`` is recomputed over the
new persist shape IN THE SAME UPDATE (the hash covers the manifest, so entry
rewrites must swap both atomically or drift detection would fire).

Idempotent + resumable: already-external entries are skipped; a re-run after
an interruption picks up where it left off. Run inside the control-plane
container (compose network + dev-keys mounted):

.. code:: sh

    docker compose exec control-plane-blue \\
        python -m control_plane.backfill_skill_assets            # dry-run
    docker compose exec control-plane-blue \\
        python -m control_plane.backfill_skill_assets --apply

Requires ``EXPERT_WORK_OBJECT_STORE_BACKEND=s3-compatible`` — refusing the
in-memory backend on purpose: objects there die with the process, which
would corrupt every backfilled row.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select, update

from control_plane.runtime import resolve_object_store_config
from control_plane.settings import Settings
from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence import (
    DatabaseConfig,
    build_rls_sessionmaker,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models.skill import SkillVersionRow
from expert_work.protocol.skill import (
    SkillSupportingFile,
    compute_content_hash,
    supporting_files_to_jsonable,
)
from expert_work.runtime.secret_store import make_secret_store
from expert_work.runtime.skill_assets import externalize_supporting_files
from expert_work.runtime.storage import make_object_store

logger = logging.getLogger("expert_work.control_plane.backfill_skill_assets")


def _needs_backfill(supporting_files: dict[str, dict[str, object]]) -> bool:
    return any(
        entry.get("content") and not entry.get("storage_key") for entry in supporting_files.values()
    )


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    if settings.object_store_backend != "s3-compatible":
        print(
            "ERROR: backfill requires EXPERT_WORK_OBJECT_STORE_BACKEND=s3-compatible "
            "(the in-memory backend loses objects on restart)"
        )
        return 2

    if settings.secret_store_backend != "local_dev":  # noqa: S105 — backend name
        print(
            "ERROR: backfill currently supports the local_dev secret store only "
            "(object-store creds are resolved through it)"
        )
        return 2
    secret_store = make_secret_store("local_dev", env_file=settings.secret_store_env_file)
    store_config = await resolve_object_store_config(
        backend=settings.object_store_backend,
        endpoint_url=settings.object_store_endpoint_url,
        region=settings.object_store_region,
        bucket=settings.object_store_bucket,
        access_key_ref=settings.object_store_access_key_ref,
        secret_key_ref=settings.object_store_secret_key_ref,
        secret_store=secret_store,
        addressing_style=settings.object_store_addressing_style,
    )

    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=args.dsn or settings.db_dsn, pgbouncer_mode=settings.db_pgbouncer_mode)
    )
    migrated = skipped = 0
    try:
        # skill_version rows are RLS-guarded (platform rows are tenant-less) —
        # a management sweep must read them all, so run under the bypass.
        session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
        async with (
            make_object_store(settings.object_store_backend, store_config) as object_store,
            bypass_rls_session(),
        ):
            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(
                                SkillVersionRow.id,
                                SkillVersionRow.prompt_fragment,
                                SkillVersionRow.supporting_files,
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
            for version_id, prompt_fragment, raw_files in rows:
                files = dict(raw_files or {})
                if not files or not _needs_backfill(files):
                    skipped += 1
                    continue
                typed = {path: SkillSupportingFile(**meta) for path, meta in files.items()}
                external = await externalize_supporting_files(typed, object_store=object_store)
                new_jsonable = supporting_files_to_jsonable(external)
                new_hash = compute_content_hash(prompt_fragment, new_jsonable)
                if args.apply:
                    async with session_factory() as session, session.begin():
                        await session.execute(
                            update(SkillVersionRow)
                            .where(SkillVersionRow.id == version_id)
                            .values(supporting_files=new_jsonable, content_hash=new_hash)
                        )
                migrated += 1
                logger.info(
                    "backfill %s version=%s files=%d",
                    "APPLIED" if args.apply else "DRY-RUN",
                    version_id,
                    len(files),
                )
    finally:
        await engine.dispose()

    mode = "applied" if args.apply else "dry-run (pass --apply to write)"
    print(f"OK: {migrated} version(s) externalized, {skipped} already clean — {mode}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--dsn", default=None, help="override the DB DSN")
    raise SystemExit(asyncio.run(_amain(parser.parse_args())))


if __name__ == "__main__":
    main()
