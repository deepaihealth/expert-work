"""Import built `.skill` packages through the platform's own ingest pipeline.

Two-step usage:

    uv run --no-sync python platform-skills/build.py
    uv run --no-sync python platform-skills/import_in_pod.py bundle [--dry-run] dist/*.skill \\
        | kubectl -n expert-work exec -i <control-plane-pod> -- python3 -

``bundle`` runs on the developer's machine (with this repo's platform
packages on the path — it imports ``control_plane``/``expert_work`` itself,
a bare interpreter fails with ``ModuleNotFoundError``): it only reads local
`.skill` files and prints a self-contained Python program to stdout — it
never connects to the cluster. The printed program embeds this module's own
source (so the *pod's* ``python3 -`` process needs nothing from this
repository, only what the control-plane image already has installed) plus
the packages (base64) and, when piped into a pod, actually performs the
import there: same ``_ingest_platform_skill_payload`` pipeline the
platform's own ZIP-upload endpoint uses (parse → name check → moderation →
Mini-ADR U-21 strict threat scan → idempotent create/version → audit), then
one cross-replica ``platform_skill`` invalidation over the platform's own
Redis bus if anything was actually created — so every replica's built-agent
cache picks up the change without a restart. Dry-run (``--dry-run``) runs
the same parse/name-check/moderation/scan/hash gates but never the write
step or the publish, and never uploads to the skill-asset object store
either (a no-upload stand-in produces the same content-addressed shape).

``run_import`` / ``ImportDeps`` are the testable core: pass in-memory
stores and a ``publish`` returning whether a real bus publish succeeded, to
exercise the whole decision logic (create / version-bump / no-op / dry-run
/ partial-batch-failure) without a real database.
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
import re
import socket
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from control_plane.api._skill_moderation import (
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_runtime import classify_skill_runtime
from control_plane.api._skill_zip import SkillZipPayload, parse_skill_zip
from control_plane.api.platform_skills import _ingest_platform_skill_payload
from control_plane.audit import TenantConfigPiiResolver, build_default_audit_logger
from control_plane.invalidation_bus import InvalidationBus, InvalidationEvent, NoopInvalidationBus
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.persistence import SkillStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import Principal
from expert_work.protocol.skill import compute_content_hash, supporting_files_to_jsonable
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.skill_assets import ObjectStore as SkillAssetObjectStore
from expert_work.runtime.skill_assets import (
    SkillAssetUnavailableError,
    externalize_supporting_files,
)

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

#: Mirrors `_ingest_platform_skill_payload`'s own name-format gate
#: (`platform_skills.py:363`) — inline there, not a shared helper, so it is
#: replicated here rather than imported. The dry-run preview runs this too
#: (fix round 1, finding 3) so a malformed name is rejected the same way in
#: preview and reality.
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass
class ImportDeps:
    """Everything ``run_import`` needs, injectable for tests (in-memory
    store/audit, ``object_store=None``, a ``publish`` returning whether a
    real bus publish actually succeeded)."""

    store: SkillStore
    audit: AuditLogger
    object_store: SkillAssetObjectStore | None
    publish: Callable[[], Awaitable[bool]]


class _NoUploadObjectStore:
    """Structural stand-in for ``SkillAssetObjectStore`` used only by the
    dry-run preview (fix round 1, finding 1): ``put`` never touches the
    network. ``externalize_supporting_files`` still takes the "external"
    branch — the same content-addressed ``storage_key``/``sha256`` shape a
    real durable store would produce, computed from the raw bytes' own
    sha256 (``skill_assets.py:94-107``), not from anything the store itself
    does — so the previewed ``content_hash`` is identical to what a real
    import against a real durable store would compute, without ever
    writing to it.
    """

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        return None

    async def get(self, key: str) -> bytes:
        raise SkillAssetUnavailableError(key)


@contextlib.contextmanager
def _platform_tenant_scope() -> Iterator[None]:
    """Run under the platform pseudo-tenant (``PRINCIPAL.tenant_id``, the
    nil UUID) — not a bypass. Mirrors ``skill_curator.py``'s
    ``_tenant_scope(_PLATFORM_TENANT_ID)`` (fix round 1, finding 7):
    ``_ingest_platform_skill_payload``'s own ``audit_emit`` calls run
    *outside* its internal ``bypass_rls_session()`` (``platform_skills.py``
    :467-501), so without a matching tenant context those audit-log inserts
    would be denied once RLS enforcement is live — a bypass context does
    not help an INSERT the way it helps a SELECT.
    """
    tenant = current_tenant_id_var.set(PRINCIPAL.tenant_id)
    bypass = bypass_rls_var.set(False)
    try:
        yield
    finally:
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


async def _run_pipeline_gates(
    blob: bytes, *, object_store: SkillAssetObjectStore | None
) -> tuple[SkillZipPayload, bytes, dict[str, object]]:
    """Parse + name-check + moderate + strict-scan one package and compute
    its content hash — the exact same functions, in the exact same order,
    that ``_ingest_platform_skill_payload`` runs before it ever touches the
    database. Used directly by the dry-run preview (which must never call
    the write step) so the previewed hash and rejection behavior can never
    drift from what a real import would do. ``object_store`` decides the
    *tier* (asset vs inline size caps) but is never itself uploaded to —
    see ``_NoUploadObjectStore``.
    """
    payload = parse_skill_zip(blob, asset_tier=object_store is not None)
    if not _SKILL_NAME_RE.fullmatch(payload.name):
        msg = f"skill name {payload.name!r} fails validation (see platform_skills.py:363)"
        raise ValueError(msg)
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
    runtime = classify_skill_runtime(payload).as_dict()
    stand_in = _NoUploadObjectStore() if object_store is not None else None
    supporting_files = await externalize_supporting_files(
        payload.supporting_files, object_store=stand_in
    )
    files_jsonable = supporting_files_to_jsonable(supporting_files)
    content_hash = compute_content_hash(payload.prompt_fragment, files_jsonable)
    return payload, content_hash, runtime


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
    packages: dict[str, bytes],
    *,
    dry_run: bool,
    deps: ImportDeps,
    on_result: Callable[[dict[str, object]], None] | None = None,
) -> tuple[list[dict[str, object]], bool | None]:
    """Import every package through the platform's own ingest pipeline.

    ``dry_run=True`` only previews (parse + name-check + moderate +
    strict-scan + hash, never the write step) — nothing is written,
    ``deps.publish`` is never called. Otherwise each package goes through
    ``_ingest_platform_skill_payload`` unchanged.

    Each result is appended to the returned list *and* handed to
    ``on_result`` (if given) as soon as it is produced — so a caller that
    prints from ``on_result`` sees every already-completed package even if
    a later one raises (fix round 1, finding 2).

    If ANY package came back newly-created (HTTP 201), ``deps.publish()``
    is awaited exactly once — in a ``finally``, so it still fires even when
    a later package raises — one cross-replica invalidation per import run,
    not one per package. The whole batch runs under the platform tenant
    scope (fix round 1, finding 7).

    Returns ``(results, published)``: ``published`` is ``None`` when no
    publish was attempted (dry-run, or nothing created), otherwise exactly
    what ``deps.publish()`` returned — ``True`` for a confirmed real
    publish, ``False`` for "skipped or failed" (the caller decides how to
    report that; see ``_pod_entrypoint``).
    """
    results: list[dict[str, object]] = []
    any_created = False
    published: bool | None = None

    with _platform_tenant_scope():
        try:
            for name, blob in packages.items():
                result: dict[str, object]
                if dry_run:
                    payload, content_hash, runtime = await _run_pipeline_gates(
                        blob, object_store=deps.object_store
                    )
                    would_create_version = await _would_create_version(
                        payload, content_hash, store=deps.store
                    )
                    result = {
                        "file": name,
                        "name": payload.name,
                        "status": "dry-run",
                        "created": False,
                        "would_create_version": would_create_version,
                        "content_hash": content_hash.hex(),
                        "runtime": runtime,
                    }
                else:
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
                        deps.store,
                        skill_id=UUID(str(skill["id"])),
                        version_number=version_number,
                    )
                    result = {
                        "file": name,
                        "name": skill["name"],
                        "status": status,
                        "created": bool(content["created"]),
                        "version": version_number,
                        "content_hash": content_hash_hex,
                        "runtime": content["runtime"],
                    }

                results.append(result)
                if on_result is not None:
                    on_result(result)
        finally:
            if not dry_run and any_created:
                published = await deps.publish()

    return results, published


# ---------------------------------------------------------------------------
# Pod-side wiring — only reachable inside a running control-plane pod (needs
# a live DB / Redis via env). Never exercised by the unit tests, which build
# ``ImportDeps`` from in-memory stores directly. ``_PublishRecordingRedisClient``
# and ``_publish_outcome`` are pure enough to unit-test on their own, though.
# ---------------------------------------------------------------------------


class _PublishRecordingRedisClient:
    """Thin proxy around a real (async) redis client: records ``PUBLISH``'s
    own return value (the receiver/subscriber count — ``InvalidationBus``
    calls it and discards it, ``invalidation_bus.py``'s ``publish()``) so
    the caller can tell "confirmed delivered to at least one replica" from
    "sent into the void" without duplicating ``InvalidationBus``'s own
    channel/payload construction (fix round 2, N3 — replaces round 1's
    PING-before-publish probe). Every other attribute delegates straight
    through to the wrapped client.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.last_publish_receivers: int | None = None

    async def publish(self, channel: str, message: str) -> int:
        receivers = await self._client.publish(channel, message)
        self.last_publish_receivers = receivers
        return receivers

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _publish_outcome(receivers: int | None) -> bool:
    """``True`` only when ``PUBLISH`` reached at least one receiver.
    ``None`` (never recorded — ``InvalidationBus.publish()`` swallowed an
    exception before assigning it) and ``0`` (no replica currently
    subscribed) both count as "not confirmed", the same way: an operator
    cannot tell them apart from ``receivers`` alone, and both mean no
    replica is guaranteed to have picked up the change.
    """
    return bool(receivers)


async def _pod_entrypoint(packages: dict[str, bytes], *, dry_run: bool) -> None:
    """Wire the platform's own store / audit / object-store / invalidation
    stack from live env vars (``Settings()`` reads ``EXPERT_WORK_*``, the
    same env the control-plane process itself boots from) and run the
    import. Prints each package's result line as soon as it is produced
    (``flush=True``), and prints the ``{"invalidation": ...}`` line from
    inside the publish step itself (fix round 2, N1) — both survive a later
    package raising, since ``run_import`` calls the publish step from its
    own ``finally``. An uncaught exception here (e.g. a rejected package)
    still exits non-zero, same as any other Python script; on a *clean* run
    where nothing confirms the invalidation went out, this function raises
    ``SystemExit(1)`` itself. Closes the DB engine and (if opened) the
    Redis connection before returning, success or failure.
    """
    from control_plane.app import _build_secret_store, _build_sql_stores
    from control_plane.runtime import resolve_object_store_config
    from control_plane.settings import Settings
    from expert_work.runtime.storage import make_object_store

    def _emit(result: dict[str, object]) -> None:
        print(json.dumps(result, sort_keys=True), flush=True)

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

            bus: InvalidationBus | NoopInvalidationBus = NoopInvalidationBus()
            recorder: _PublishRecordingRedisClient | None = None
            bus_is_noop = True
            if settings.quota_redis_url:
                import redis.asyncio as redis_async

                bus_redis = redis_async.from_url(
                    settings.quota_redis_url, encoding="utf-8", decode_responses=True
                )
                stack.push_async_callback(bus_redis.aclose)
                recorder = _PublishRecordingRedisClient(bus_redis)
                bus = InvalidationBus(redis_client=recorder, origin=socket.gethostname())
                bus_is_noop = False

            def _report_invalidation(*, confirmed: bool, **extra: object) -> None:
                # Printed from inside the publish step itself (not after
                # `run_import` returns) so it still shows up on a
                # partial-batch failure — `run_import` calls `deps.publish`
                # from its own `finally`, which runs even when a later
                # package raises (fix round 2, N1).
                body: dict[str, object] = {
                    "invalidation": "published" if confirmed else "skipped",
                    **extra,
                }
                print(json.dumps(body, sort_keys=True), flush=True)

            async def _publish() -> bool:
                if bus_is_noop or recorder is None:
                    _report_invalidation(
                        confirmed=False,
                        reason="no invalidation bus configured (EXPERT_WORK_QUOTA_REDIS_URL unset)",
                    )
                    return False
                # `InvalidationBus.publish()` never raises by design (a live
                # app degrades to a WARNING + counter because its *local*
                # invalidation, which already ran, is the real safety net —
                # see invalidation_bus.py's module docstring); this batch
                # script has no local cache to fall back on, so success is
                # judged by the PUBLISH receiver count `recorder` captured,
                # not by the absence of an exception.
                await bus.publish(InvalidationEvent(kind="platform_skill"))
                receivers = recorder.last_publish_receivers
                if _publish_outcome(receivers):
                    _report_invalidation(confirmed=True, receivers=receivers)
                    return True
                _report_invalidation(
                    confirmed=False,
                    reason=(
                        f"redis PUBLISH reached 0 receivers (recorded={receivers!r} — no replica "
                        "subscribed, or the publish itself failed; see control-plane logs)"
                    ),
                )
                return False

            deps = ImportDeps(
                store=sql_stores.skill,
                audit=audit,
                object_store=skill_object_store,
                publish=_publish,
            )
            _, published = await run_import(packages, dry_run=dry_run, deps=deps, on_result=_emit)
    finally:
        await sql_stores.engine.dispose()

    # `_publish` (above) already printed the "published"/"skipped" line the
    # moment it ran — including on a partial-batch failure, since it's
    # called from `run_import`'s `finally`. If a package failure is what
    # ended the run, that exception already guarantees a non-zero exit and
    # has already propagated past this point (this function would not still
    # be executing). Reaching here means `run_import` returned normally, so
    # this is only about the exit code for "everything imported, but the
    # invalidation itself wasn't confirmed".
    if not dry_run and published is False:
        raise SystemExit(1)


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
