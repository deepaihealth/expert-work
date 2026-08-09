"""Stream J.15-补强-2 — restore a user workspace from an ObjectStore archive.

Used by the recovery runbook (``docs/runbooks/volume-restore.md``): if a
user's docker volume is lost (host failure / accidental ``rm`` / disk
corruption) we pull the most recent J-36 archive or J-29 daily backup
from ObjectStore, recreate a docker named volume from the tar.gz, and
hand the new volume name back to the operator. **The operator** then
decides whether to swap ``user_workspace.volume_name`` to point at the
restored volume (this script does not auto-swap — same posture as K14
``restore_audit.py``).

Library + CLI surface:

* :func:`restore_volume_from_object` — single-key restore for the
  testcontainers drill (no docker dep, takes a writer callback).
* :func:`restore_latest_archive_to_volume` — production path: streams
  the latest archive for ``(tenant, user)`` into a fresh named volume
  via a throwaway ``docker run``.
* ``python -m tools.persistence.restore_volume --tenant ... --user ...``
  — the runbook's CLI entrypoint.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from expert_work.runtime.storage.base import ObjectStore, ObjectStoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VolumeRestoreReport:
    """Outcome of one restore invocation."""

    object_key: str
    new_volume_name: str
    size_bytes: int
    skipped_reason: str | None = None
    failed_keys: tuple[str, ...] = field(default_factory=tuple)


def _format_new_volume_name(volume_name: str, *, suffix: str) -> str:
    """Deterministic restored-volume name. Operator promotes after review."""
    return f"{volume_name}_restored_{suffix}"


async def _select_latest_archive_key(
    *,
    object_store: ObjectStore,
    tenant_id: UUID,
    user_id: UUID,
    archive_prefix: str,
    backup_prefix: str,
    date: str | None = None,
) -> str | None:
    """Find the latest archive key for ``(tenant, user)``.

    Preference order:

    1. J-36 archive at ``{archive_prefix}/{tenant}/{user}/<volume>.tar.gz``
       (the soft-delete physical copy — newest content the operator has).
    2. J-29 daily backup — when ``date`` is given the operator-targeted
       day, otherwise the lexicographically latest folder under the
       backup prefix.

    Returns ``None`` when no archive or backup is found.
    """
    archive_root = f"{archive_prefix}/{tenant_id}/{user_id}/"
    archive_keys = await object_store.list_prefix(archive_root)
    if archive_keys:
        return archive_keys[-1]

    backup_root = f"{backup_prefix}/{tenant_id}/{user_id}/"
    if date is not None:
        keys = await object_store.list_prefix(f"{backup_root}{date}/")
        if keys:
            return keys[-1]
        return None
    keys = await object_store.list_prefix(backup_root)
    return keys[-1] if keys else None


#: Drill writer — used by testcontainers tests to capture the tar.gz
#: bytes without invoking docker.
VolumeWriter = Callable[[str, bytes], Awaitable[None]]


async def restore_volume_from_object(
    *,
    object_store: ObjectStore,
    object_key: str,
    new_volume_name: str,
    writer: VolumeWriter,
) -> VolumeRestoreReport:
    """Pull ``object_key`` from ObjectStore and hand the bytes to ``writer``.

    Drill-friendly: the writer absorbs the tar.gz bytes (tests stash
    them in memory; production binds to the docker-volume hydrate
    helper :func:`_hydrate_volume_with_docker`).
    """
    try:
        blob = await object_store.get(object_key)
    except ObjectStoreError as exc:
        logger.exception("volume_restore.get_failed key=%s", object_key)
        msg = f"could not pull archive {object_key!r}: {exc}"
        raise RuntimeError(msg) from exc
    await writer(new_volume_name, blob)
    logger.info(
        "volume_restore.staged key=%s new_volume=%s size=%d",
        object_key,
        new_volume_name,
        len(blob),
    )
    return VolumeRestoreReport(
        object_key=object_key,
        new_volume_name=new_volume_name,
        size_bytes=len(blob),
    )


def _hydrate_volume_with_docker(*, new_volume_name: str, blob: bytes, image: str) -> None:
    """Write ``blob`` into a fresh docker named volume.

    Runs (1) ``docker volume create`` then (2) a throwaway ``docker
    run --rm`` that mounts the volume at ``/ws`` and pipes the tar.gz
    in on stdin via ``tar -xzf - -C /ws``.

    **Hardening is the archive job's own constant, not a copy of it.**
    ``_AUX_CONTAINER_HARDENING_ARGS`` (``sandbox_supervisor.docker_client``)
    is what ``archive_volume`` uses to pack these very tarballs; this
    function imports it and appends the two capabilities that unpacking
    needs on top of packing. It used to be a hand-copied duplicate, and
    the copy went stale the moment #1136 added ``DAC_OVERRIDE`` to the
    original — copy drift is the same mechanism that produced the bug
    this docstring documents, so the duplicate is gone.

    **Why ``CHOWN`` + ``FOWNER`` are needed on top.** Workspace archives
    carry entries owned by the sandbox ``agent`` user (uid 10000), and
    the extracting process here is the image's root. GNU tar running as
    root defaults to ``--same-owner``, so it ``chown``s every entry —
    which needs ``CAP_CHOWN`` — then ``chmod``s entries it no longer
    owns and re-creates hard links across them, both of which need
    ``CAP_FOWNER`` (the link half via ``fs.protected_hardlinks``).
    Measured on the real sandbox image (GNU tar 1.35), an archive with
    nested ``0700`` dirs, ``0600``/``0644`` files, a hard link and a
    symlink:

    * ``--cap-drop ALL``    → exit 2, every entry "Cannot change ownership"
    * ``+ CHOWN``           → exit 2, "Cannot change mode" + "Cannot hard link"
    * ``+ CHOWN + FOWNER``  → exit 0, owner/modes exact

    Note what the first row does **not** mean: the tree lands complete
    with correct modes, only every entry stays ``root:root``, and then
    tar exits 2. An operator chasing this reads the ``stat`` owner
    column, not a list of missing files.

    ``DAC_OVERRIDE`` (inherited from the shared constant) is what makes
    the tool **re-runnable**. On a fresh volume it is unnecessary — GNU
    tar delays applying directory ownership until it has written the
    children, so root stays the owner of dirs it just created. On a
    *second* pass over a half-restored volume those dirs are already
    ``10000:10000 0700``, root's fsuid lands in the "other" class, and
    tar's "temporarily OR in ``S_IRWXU``" workaround does not help
    because ``S_IRWXU`` is the *owner* triad. Without it pass 2 fails
    with ``Cannot open: Permission denied`` — **the same symptom as the
    capability bug above**, which is exactly the wrong thing to hand an
    operator who has just applied the capability fix. Re-running after a
    partial failure is the documented recovery step
    (``docs/runbooks/volume-restore.md``), so it has to work.

    Dropping ``--same-owner`` instead is not an option: the restored
    volume is mounted back into a sandbox running as uid 10000, so
    root-owned files would be unusable.
    """
    # Imported lazily for the same reason ``_main`` does it: this module is
    # a thin operator CLI and importing a service package at module scope
    # would drag its settings/deps into every consumer (including the test
    # module, which only wants this one function).
    from sandbox_supervisor.docker_client import _AUX_CONTAINER_HARDENING_ARGS

    # Operator-driven runbook tool; docker is expected on PATH.
    create_argv = ["docker", "volume", "create", new_volume_name]
    subprocess.run(create_argv, check=True)  # noqa: S603
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        *_AUX_CONTAINER_HARDENING_ARGS,
        # On top of packing: see the docstring. Order is irrelevant —
        # docker resolves --cap-drop/--cap-add as sets.
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "FOWNER",
        "--volume",
        f"{new_volume_name}:/ws",
        "--entrypoint",
        "sh",
        image,
        "-c",
        "tar -xzf - -C /ws",
    ]
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        Path(tmp.name).write_bytes(blob)
        with Path(tmp.name).open("rb") as handle:
            subprocess.run(argv, check=True, stdin=handle)  # noqa: S603


async def restore_latest_archive_to_volume(
    *,
    object_store: ObjectStore,
    tenant_id: UUID,
    user_id: UUID,
    archive_prefix: str,
    backup_prefix: str,
    image: str,
    date: str | None = None,
    suffix: str | None = None,
) -> VolumeRestoreReport:
    """Production-mode restore: find the latest tar.gz + hydrate a new docker volume.

    Returns a :class:`VolumeRestoreReport` containing the new docker
    volume name (operator promotes by updating
    ``user_workspace.volume_name`` — this script does not auto-swap).
    """
    key = await _select_latest_archive_key(
        object_store=object_store,
        tenant_id=tenant_id,
        user_id=user_id,
        archive_prefix=archive_prefix,
        backup_prefix=backup_prefix,
        date=date,
    )
    if key is None:
        msg = f"no archive or backup found for tenant={tenant_id} user={user_id}" + (
            f" date={date}" if date is not None else ""
        )
        raise RuntimeError(msg)
    volume_basename = key.rsplit("/", 1)[-1].removesuffix(".tar.gz")
    new_volume_name = _format_new_volume_name(
        volume_basename,
        suffix=suffix or "manual",
    )

    async def _writer(name: str, blob: bytes) -> None:
        # Drop straight into docker — _hydrate is a sync subprocess
        # helper so we run it in the default executor to keep the
        # event loop responsive.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _hydrate_volume_with_docker(new_volume_name=name, blob=blob, image=image),
        )

    return await restore_volume_from_object(
        object_store=object_store,
        object_key=key,
        new_volume_name=new_volume_name,
        writer=_writer,
    )


def _parse_argv(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore a user workspace volume from ObjectStore (Stream J.15-补强-2)."
    )
    parser.add_argument("--tenant", required=True, help="tenant UUID")
    parser.add_argument("--user", required=True, help="user UUID")
    parser.add_argument(
        "--date",
        default=None,
        help="optional YYYY-MM-DD for J-29 daily backup; default = latest available",
    )
    parser.add_argument(
        "--archive-prefix",
        default="volume-archive",
        help="ObjectStore prefix for J-36 archives (default: volume-archive)",
    )
    parser.add_argument(
        "--backup-prefix",
        default="volume-backups",
        help="ObjectStore prefix for J-29 daily backups (default: volume-backups)",
    )
    parser.add_argument(
        "--image",
        default="expert-work-sandbox:dev",
        help="container image used to hydrate the new volume (default: expert-work-sandbox:dev)",
    )
    parser.add_argument(
        "--suffix",
        default=None,
        help="suffix appended to the new volume name (default: 'manual')",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = _parse_argv(argv)
    # The CLI requires real S3 creds — we let the caller export
    # EXPERT_WORK_SANDBOX_OBJECT_STORE_* env vars and build settings the
    # same way the supervisor does. Keeping this stub thin so the
    # runbook + the library are the source of truth.
    from sandbox_supervisor.app import _build_object_store
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    settings = SandboxSupervisorSettings()
    async with _build_object_store(settings) as store:
        report = await restore_latest_archive_to_volume(
            object_store=store,
            tenant_id=UUID(args.tenant),
            user_id=UUID(args.user),
            archive_prefix=args.archive_prefix,
            backup_prefix=args.backup_prefix,
            image=args.image,
            date=args.date,
            suffix=args.suffix,
        )
    next_steps = (
        f"restored {report.object_key} -> docker volume {report.new_volume_name} "
        f"({report.size_bytes} bytes)\n"
        "Next steps: verify with `docker run --rm -v "
        f"{report.new_volume_name}:/ws image ls -la /ws`, then run the\n"
        "promotion SQL shown in docs/runbooks/volume-restore.md.\n"
    )
    sys.stdout.write(next_steps)
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    sys.exit(asyncio.run(_main(sys.argv[1:])))
