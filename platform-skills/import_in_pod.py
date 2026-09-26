"""Import built `.skill` packages through the platform's own ingest pipeline.

Two-step usage:

    python platform-skills/build.py
    python platform-skills/import_in_pod.py bundle [--dry-run] dist/*.skill \\
        | kubectl -n expert-work exec -i <control-plane-pod> -- python3 -

``bundle`` runs on the developer's machine: it only reads local `.skill`
files and prints a self-contained Python program to stdout — it never
touches the cluster. The printed program embeds this module's own source
(so the pod's ``python3 -`` process needs nothing from this repository)
plus the packages (base64) and, when piped into a pod, actually performs
the import there: same ``_ingest_platform_skill_payload`` pipeline the
platform's own ZIP-upload endpoint uses (parse → name check → moderation
→ Mini-ADR U-21 strict threat scan → idempotent create/version → audit),
same skill-asset-store externalization, then one cross-replica
``platform_skill`` invalidation over the platform's own Redis bus if
anything was actually created — so every replica's built-agent cache
picks up the change without a restart.

``run_import`` / ``ImportDeps`` are the testable core: pass in-memory
stores and a counting ``publish`` to exercise the whole decision logic
(create / version-bump / no-op / dry-run) without a real database.
"""

from __future__ import annotations

import argparse

# Unused *in this file's own code* — the bundle's embedded tail (see
# `_make_bundle`) appends an `asyncio.run(...)` call that relies on this
# module-level import rather than re-importing it a second time.
import asyncio  # noqa: F401
import base64
import contextlib
import json
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from control_plane.api._skill_moderation import (
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_zip import SkillZipPayload, parse_skill_zip
from control_plane.api.platform_skills import _ingest_platform_skill_payload
from control_plane.audit import TenantConfigPiiResolver, build_default_audit_logger
from control_plane.invalidation_bus import InvalidationBus, InvalidationEvent, NoopInvalidationBus
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.persistence import SkillStore
from expert_work.protocol import Principal
from expert_work.protocol.skill import compute_content_hash, supporting_files_to_jsonable
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.skill_assets import ObjectStore as SkillAssetObjectStore
from expert_work.runtime.skill_assets import externalize_supporting_files

#: Audit/actor identity for this batch tool — a platform system_admin
#: service principal. ``UUID(int=0)`` for the "no specific tenant" audit
#: rows is the existing convention (see ``skill_curator.py``'s
#: ``_PLATFORM_TENANT_ID``).
PRINCIPAL = Principal(
    subject_id="platform-skills-import",
    subject_type="service",
    tenant_id=UUID(int=0),
    is_system_admin=True,
    allowed_tenants="*",
)
SOURCE = "platform_skills_repo"


@dataclass
class ImportDeps:
    """Everything ``run_import`` needs, injectable for tests (in-memory
    store/audit, ``object_store=None``, a counting ``publish``)."""

    store: SkillStore
    audit: AuditLogger
    object_store: SkillAssetObjectStore | None
    publish: Callable[[], Awaitable[None]]


async def _run_pipeline_gates(
    blob: bytes, *, object_store: SkillAssetObjectStore | None
) -> tuple[SkillZipPayload, bytes]:
    """Parse + moderate + strict-scan one package and compute its content
    hash — the exact same functions, in the exact same order, that
    ``_ingest_platform_skill_payload`` runs before it ever touches the
    database. Used directly by the dry-run preview (which must never call
    the write step) so the previewed hash can never drift from what a
    real import would compute.
    """
    payload = parse_skill_zip(blob, asset_tier=object_store is not None)
    moderate_prompt_fragment(payload.prompt_fragment, lazy_load=payload.lazy_load)
    moderate_tool_names(payload.tool_names)
    moderate_required_models(payload.required_models)
    findings = scan_for_threats(payload.prompt_fragment, scope="strict")
    if findings:
        categories = sorted({f.category for f in findings})
        msg = (
            f"skill {payload.name!r} tripped the Mini-ADR U-21 threat scanner "
            f"({len(findings)} finding(s): {', '.join(categories)})"
        )
        raise RuntimeError(msg)
    supporting_files = await externalize_supporting_files(
        payload.supporting_files, object_store=object_store
    )
    files_jsonable = supporting_files_to_jsonable(supporting_files)
    content_hash = compute_content_hash(payload.prompt_fragment, files_jsonable)
    return payload, content_hash


async def _would_create_version(
    payload: SkillZipPayload, content_hash: bytes, *, store: SkillStore
) -> int | str:
    """Preview the version a real import would create — the same
    ``store.get_platform_skill_by_name`` / ``get_platform_version_by_number``
    idempotency check ``_ingest_platform_skill_payload`` runs, read-only."""
    async with bypass_rls_session():
        existing = await store.get_platform_skill_by_name(name=payload.name)
        if existing is not None and existing.latest_version > 0:
            latest = await store.get_platform_version_by_number(
                skill_id=existing.id, version=existing.latest_version
            )
            if latest is not None and latest.content_hash == content_hash:
                return "unchanged"
            return existing.latest_version + 1
    return 1


async def _read_content_hash_hex(store: SkillStore, *, skill_id: UUID, version_number: int) -> str:
    """Read back the ``content_hash`` a real import just persisted (or found
    unchanged). Reading it back — rather than recomputing it a second time
    ourselves — keeps ``_ingest_platform_skill_payload`` the single source
    of truth for real imports, including its own audit trail on a
    moderation/scan rejection (never pre-empted by a local check here)."""
    async with bypass_rls_session():
        version = await store.get_platform_version_by_number(
            skill_id=skill_id, version=version_number
        )
    return version.content_hash.hex() if version is not None else ""


async def run_import(
    packages: dict[str, bytes], *, dry_run: bool, deps: ImportDeps
) -> list[dict[str, object]]:
    """Import every package through the platform's own ingest pipeline.

    ``dry_run=True`` only previews (parse + moderate + strict-scan + hash,
    never the write step) — nothing is written, ``deps.publish`` is never
    called. Otherwise each package goes through
    ``_ingest_platform_skill_payload`` unchanged; if ANY package came back
    newly-created (HTTP 201), ``deps.publish()`` fires exactly once after
    the whole batch — one cross-replica invalidation per import run, not
    one per package.
    """
    results: list[dict[str, object]] = []
    any_created = False

    for name, blob in packages.items():
        if dry_run:
            payload, content_hash = await _run_pipeline_gates(blob, object_store=deps.object_store)
            would_create_version = await _would_create_version(
                payload, content_hash, store=deps.store
            )
            results.append(
                {
                    "name": name,
                    "status": "dry-run",
                    "created": False,
                    "would_create_version": would_create_version,
                    "content_hash": content_hash.hex(),
                }
            )
            continue

        content, status = await _ingest_platform_skill_payload(
            blob=blob,
            store=deps.store,
            audit=deps.audit,
            principal=PRINCIPAL,
            source=SOURCE,
            object_store=deps.object_store,
        )
        if status == 201:
            any_created = True
        skill: dict[str, object] = content["skill"]  # type: ignore[assignment]
        version: dict[str, object] = content["version"]  # type: ignore[assignment]
        version_number = int(version["version"])  # type: ignore[arg-type]
        content_hash_hex = await _read_content_hash_hex(
            deps.store, skill_id=UUID(str(skill["id"])), version_number=version_number
        )
        results.append(
            {
                "name": name,
                "status": status,
                "created": bool(content["created"]),
                "version": version_number,
                "content_hash": content_hash_hex,
            }
        )

    if not dry_run and any_created:
        await deps.publish()
    return results


# ---------------------------------------------------------------------------
# Pod-side wiring — only reachable inside a running control-plane pod (needs
# a live DB / Redis via env). Never exercised by the unit tests, which build
# ``ImportDeps`` from in-memory stores directly.
# ---------------------------------------------------------------------------


async def _pod_entrypoint(packages: dict[str, bytes], *, dry_run: bool) -> None:
    """Wire the platform's own store / audit / object-store / invalidation
    stack from live env vars (``Settings()`` reads ``EXPERT_WORK_*``, the
    same env the control-plane process itself boots from) and run the
    import. Prints one JSON line per package, plus a final
    ``{"invalidation": "published"}`` line when anything was created.
    Closes the DB engine and (if opened) the Redis connection before
    returning, success or failure.
    """
    from control_plane.app import _build_secret_store, _build_sql_stores
    from control_plane.runtime import resolve_object_store_config
    from control_plane.settings import Settings
    from expert_work.runtime.storage import make_object_store

    settings = Settings()
    sql_stores = _build_sql_stores(settings)
    try:
        secret_store = _build_secret_store(settings, sql_stores)
        audit = build_default_audit_logger(
            store=sql_stores.audit_log, pii_fields_resolver=TenantConfigPiiResolver()
        )

        async with contextlib.AsyncExitStack() as stack:
            skill_object_store: SkillAssetObjectStore | None = None
            if settings.object_store_backend == "s3-compatible":
                config = await resolve_object_store_config(
                    backend=settings.object_store_backend,
                    endpoint_url=settings.object_store_endpoint_url,
                    region=settings.object_store_region,
                    bucket=settings.object_store_bucket,
                    access_key_ref=settings.object_store_access_key_ref,
                    secret_key_ref=settings.object_store_secret_key_ref,
                    secret_store=secret_store,
                    addressing_style=settings.object_store_addressing_style,
                )
                skill_object_store = await stack.enter_async_context(
                    make_object_store(settings.object_store_backend, config)
                )

            bus: InvalidationBus | NoopInvalidationBus
            if settings.quota_redis_url:
                import redis.asyncio as redis_async

                bus_redis = redis_async.from_url(
                    settings.quota_redis_url, encoding="utf-8", decode_responses=True
                )
                stack.push_async_callback(bus_redis.aclose)
                bus = InvalidationBus(redis_client=bus_redis, origin=socket.gethostname())
            else:
                bus = NoopInvalidationBus()

            async def _publish() -> None:
                await bus.publish(InvalidationEvent(kind="platform_skill"))

            deps = ImportDeps(
                store=sql_stores.skill,
                audit=audit,
                object_store=skill_object_store,
                publish=_publish,
            )
            results = await run_import(packages, dry_run=dry_run, deps=deps)
    finally:
        await sql_stores.engine.dispose()

    for result in results:
        print(json.dumps(result, sort_keys=True))
    if not dry_run and any(r["status"] == 201 for r in results):
        print(json.dumps({"invalidation": "published"}))


# ---------------------------------------------------------------------------
# `bundle` — local generation only; touches nothing but disk.
# ---------------------------------------------------------------------------


def _read_packages(paths: list[str]) -> dict[str, bytes]:
    packages: dict[str, bytes] = {}
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            print(f"import_in_pod: no such file: {path}", file=sys.stderr)
            raise SystemExit(2)
        packages[path.stem] = path.read_bytes()
    return packages


def _make_bundle(pkg_paths: list[str], *, dry_run: bool) -> str:
    """Read local `.skill` files, cut this module's own source at the
    ``BUNDLE-EMBED-CUT`` marker comment (everything below it — this CLI —
    is meaningless once piped into a pod, since `python3 -` has no argv),
    and append a tail that embeds the packages and calls
    ``_pod_entrypoint`` directly. The marker text below is built by
    concatenation so this line doesn't itself become a second (earlier,
    wrong) match for ``str.index``.
    """
    packages = _read_packages(pkg_paths)
    source = Path(__file__).read_text(encoding="utf-8")
    marker = "# ---- BUNDLE" + "-EMBED-CUT ----"
    cut = source.index(marker)
    embedded_source = source[:cut]
    encoded = {name: base64.b64encode(blob).decode("ascii") for name, blob in packages.items()}
    tail = (
        "\n\n"
        "# ---- generated by `import_in_pod.py bundle` — do not edit by hand ----\n"
        f"_PACKAGES_B64 = {json.dumps(encoded)}\n"
        "_PACKAGES = {name: base64.b64decode(b64) for name, b64 in _PACKAGES_B64.items()}\n"
        f"asyncio.run(_pod_entrypoint(_PACKAGES, dry_run={dry_run!r}))\n"
    )
    return embedded_source + tail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="import_in_pod.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    bundle = subparsers.add_parser(
        "bundle", help="print a self-contained pod-import program to stdout"
    )
    bundle.add_argument("--dry-run", action="store_true")
    bundle.add_argument("packages", nargs="+", metavar="PKG.skill")
    args = parser.parse_args(argv)

    print(_make_bundle(args.packages, dry_run=args.dry_run))
    return 0


# ---- BUNDLE-EMBED-CUT ----
if __name__ == "__main__":
    raise SystemExit(main())
