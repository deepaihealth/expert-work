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
    in on stdin via ``tar -xzf - -C /ws``. Hardened the same way as
    the archive job: no network, read-only rootfs, every capability
    dropped except the two GNU tar needs to restore ownership.

    **Why ``CHOWN`` + ``FOWNER`` are added back.** Workspace archives
    carry entries owned by the sandbox ``agent`` user (uid 10000), and
    the extracting process here is the image's root. GNU tar running as
    root defaults to ``--same-owner``, so it ``chown``s every entry —
    which needs ``CAP_CHOWN`` — and then ``chmod``s entries it no longer
    owns, which needs ``CAP_FOWNER``. With a bare ``--cap-drop ALL`` the
    extraction dies partway with ``Cannot change ownership to uid
    10000`` and exits 2, i.e. **restore never worked for any archive of
    non-root content**. Measured on real GNU tar (Debian bookworm), an
    archive holding a ``0700`` dir and a ``0600`` file:

    * ``--cap-drop ALL``                     → exit 2, "Cannot change ownership"
    * ``+ --cap-add CHOWN``                  → exit 2, "Cannot change mode"
    * ``+ --cap-add CHOWN --cap-add FOWNER`` → exit 0, owner/modes exact

    ``DAC_OVERRIDE`` is deliberately NOT added: GNU tar delays applying
    directory permissions until it is done writing their children, so
    root never has to descend into a ``0700`` dir it does not own. The
    third row above is that claim's evidence — the archive it restores
    contains exactly such a directory.

    Dropping ``--same-owner`` instead is not an option: the restored
    volume is mounted back into a sandbox running as uid 10000, so
    root-owned files would be unusable.
    """
    # Operator-driven runbook tool; docker is expected on PATH.
    create_argv = ["docker", "volume", "create", new_volume_name]
    subprocess.run(create_argv, check=True)  # noqa: S603
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "FOWNER",
        "--security-opt",
        "no-new-privileges",
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
