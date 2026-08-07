"""Docker engine access — the supervisor's only OS-process surface.

A :class:`DockerClient` Protocol so the :class:`SandboxSupervisor` logic
is unit-testable with a recording fake (test matrix #40); the real
:class:`CliDockerClient` shells out to the ``docker`` CLI.

The C-model (STREAM-F-DESIGN): ``launch`` starts ``docker run -i`` and
*keeps the subprocess alive*, handing back a :class:`RunnerLink` over its
stdio — the supervisor talks to the in-sandbox runner through that pipe.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from typing import Protocol

from expert_work.persistence import SANDBOX_SKILLS_ROOT
from sandbox_supervisor.runner_link import PipeRunnerLink, RunnerLink

logger = logging.getLogger(__name__)


def _build_seed_tar(files: list[tuple[str, bytes]]) -> bytes:
    """Pack ``(relpath, bytes)`` pairs into an uncompressed tar for extraction
    inside the sandbox container (see :meth:`CliDockerClient.seed_workspace`).

    Members are mode 0o755 (world-readable + executable) — belt-and-braces now
    that extraction runs as the container's own non-root ``agent`` user (see
    ``seed_workspace``'s docstring for why it is no longer a root-vs-agent
    ownership workaround). Paths are relative (e.g. ``pptx/SKILL.md``);
    extracting with ``-C {SANDBOX_SKILLS_ROOT}`` (``/opt/skills``, W2 Task 6)
    lands them there, creating parent dirs.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in files:
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


#: ``asyncio`` StreamReader buffer for a runner's stdout. Generous so a
#: large (runner-capped, ~1 MiB) response line never overflows the
#: default 64 KiB limit.
_READ_LIMIT = 4 * 1024 * 1024

#: ``docker run`` argv prefix used to find leftover sandboxes on boot.
_ORPHAN_NAME_PREFIX = "expert-work-sb-"

#: Hardening flags shared by every throwaway aux container that touches a
#: named volume (read/list/write/delete/measure/archive) — network-isolated,
#: read-only rootfs, all capabilities dropped, no privilege escalation.
#: These are *not* the sandbox launch (that argv comes from the F.3
#: ``SandboxRuntimeProvider``, W2 Task 6's ``--user``/tmpfs additions live
#: there, not here) — each of these six is a one-shot ``--rm`` container
#: that mounts a volume at ``/ws`` and runs a single coreutils/python
#: command, so it stays root (the image's ``docker run`` default since W2
#: Task 9 dropped ``USER agent``) rather than the non-root sandbox identity;
#: forcing ``--user`` here would risk it being unable to read/write a
#: volume whose top-level ownership it doesn't control.
_AUX_CONTAINER_HARDENING_ARGS = [
    "--network",
    "none",
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
]


class DockerError(RuntimeError):
    """A ``docker`` CLI invocation failed."""


class DockerClient(Protocol):
    """The Docker operations the supervisor needs — nothing more."""

    async def launch(self, argv: list[str]) -> RunnerLink:
        """Start a ``docker run -i`` container; return a live link to it."""

    async def remove(self, container_name: str) -> None:
        """Force-remove a container (``docker rm --force``)."""

    async def ping(self) -> bool:
        """Return whether the Docker daemon is reachable."""

    async def sweep_orphans(self) -> int:
        """Remove leftover ``expert-work-sb-*`` containers; return the count."""

    async def read_volume_file(
        self, *, volume: str, path: str, image: str, max_bytes: int
    ) -> bytes:
        """Read a file from a named volume; return up to ``max_bytes + 1`` bytes."""

    async def list_volume_files(
        self, *, volume: str, image: str, max_entries: int
    ) -> list[tuple[int, str]]:
        """List regular files under ``/workspace``; return ``(size, relpath)`` pairs."""

    async def write_volume_file(self, *, volume: str, path: str, data: bytes, image: str) -> None:
        """Write ``data`` to ``path`` in a named volume (document upload)."""

    async def delete_volume_file(self, *, volume: str, path: str, image: str) -> None:
        """Delete ``path`` from a named volume (workspace cleanup)."""

    async def measure_volume_size(self, *, volume: str, image: str) -> int:
        """Return the total ``/workspace`` size in bytes for a named volume.

        Stream J.15-补强-1 — backs :class:`QuotaEnforcer.refresh_size`.
        Runs a throwaway ``--rm`` container that mounts ``volume`` read-
        only and runs ``du -sb``. Returns the apparent total bytes. Raises
        :class:`DockerError` when the measure can't run.
        """

    async def archive_volume(self, *, volume: str, image: str, max_bytes: int) -> bytes:
        """Stream a tar.gz snapshot of a named volume into memory.

        Stream J.15-补强-2 — backs the J.15 lifecycle archive + daily
        backup pipelines. Spawns a throwaway ``--rm`` container that
        mounts ``volume`` read-only and runs ``tar -czf - .`` from
        ``/ws``. ``max_bytes`` caps the buffer so a runaway / malicious
        volume can't OOM the supervisor; over-cap archives raise
        :class:`DockerError`. Raises on any non-zero exit.
        """

    async def remove_volume(self, *, volume: str) -> None:
        """Force-remove a named volume (``docker volume rm --force``).

        Stream J.15-补强-2 — called after a successful archive to free
        the disk. Idempotent — removing a missing volume is logged but
        does not raise.
        """

    async def seed_workspace(self, container_name: str, *, files: list[tuple[str, bytes]]) -> None:
        """Copy ``files`` into a running container's ``SANDBOX_SKILLS_ROOT``
        (``/opt/skills``, W2 Task 6 — skill-runtime §5.1 — materialize an
        agent's activated skill files so bundled scripts run as authored).

        The target is a per-container tmpfs that a side container cannot
        mount, so this writes INTO the live container (``docker exec`` + an
        in-container ``tar`` extraction — see
        :meth:`CliDockerClient.seed_workspace` for why ``docker cp`` cannot be
        used here). Covers all acquire paths (cold/pooled/reused) uniformly.
        No-op for an empty list. Raises :class:`DockerError` on failure.
        """

    async def update_limits(
        self, container_name: str, *, cpus: float, memory_mb: int, pids_limit: int
    ) -> None:
        """Re-pair a live container's resource limits (``docker update``).

        Stream HX-6 (Mini-ADR HX-F3) — a claimed pool container was
        launched with the default limits; this applies the acquire
        request's values. Raises :class:`DockerError` on failure — the
        caller destroys the container and cold-starts instead
        (fail-closed: limits are a security surface).
        """

    async def image_exists(self, image: str) -> bool:
        """Whether ``image`` is present locally (``docker image inspect``).

        Stream HX-6 (Mini-ADR HX-F4) — the startup prefetch probe.
        """

    async def pull_image(self, image: str) -> None:
        """Pull ``image`` from its registry (``docker pull``).

        Stream HX-6 (Mini-ADR HX-F4) — backs the startup image prefetch.
        Raises :class:`DockerError` on failure; the prefetch task logs
        and moves on (fail-open: ``docker run`` pulls on demand anyway).
        """


class CliDockerClient:
    """:class:`DockerClient` backed by the ``docker`` CLI via asyncio subprocess."""

    async def launch(self, argv: list[str]) -> RunnerLink:
        # `docker run -i` (no -d): the subprocess stays alive for the
        # container's lifetime and its stdio *is* the container's stdio.
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_READ_LIMIT,
        )
        return PipeRunnerLink(process)

    async def remove(self, container_name: str) -> None:
        _, stderr, code = await self._exec(["docker", "rm", "--force", container_name])
        if code != 0:
            # A missing container is fine — destroy is idempotent.
            logger.warning(
                "docker_client.remove_failed container=%s reason=%s",
                container_name,
                stderr.strip(),
            )

    async def ping(self) -> bool:
        try:
            _, _, code = await self._exec(["docker", "version", "--format", "{{.Server.Version}}"])
        except OSError:
            return False
        return code == 0

    async def sweep_orphans(self) -> int:
        stdout, _, code = await self._exec(
            ["docker", "ps", "--all", "--quiet", "--filter", f"name={_ORPHAN_NAME_PREFIX}"]
        )
        if code != 0:
            return 0
        ids = [line for line in stdout.split() if line]
        for container_id in ids:
            await self.remove(container_id)
        if ids:
            logger.info("docker_client.swept_orphans count=%d", len(ids))
        return len(ids)

    async def read_volume_file(
        self, *, volume: str, path: str, image: str, max_bytes: int
    ) -> bytes:
        """Read ``path`` from a docker named volume (Stream J.9).

        Runs a throwaway ``--rm`` container — read-only rootfs, no
        network, all capabilities dropped — that mounts ``volume`` at
        ``/ws`` read-only and ``head``s the file. ``head -c`` bounds the
        output (and the supervisor's buffer) at ``max_bytes + 1`` bytes,
        so the caller can detect an over-cap file. Raises
        :class:`DockerError` when the file cannot be read.

        ``--entrypoint head`` overrides the sandbox image's runner
        entrypoint — without it the runner would treat ``head ...`` as
        its own args and just print its readiness line.
        """
        argv = [
            "docker",
            "run",
            "--rm",
            *_AUX_CONTAINER_HARDENING_ARGS,
            "--volume",
            f"{volume}:/ws:ro",
            "--entrypoint",
            "head",
            image,
            "-c",
            str(max_bytes + 1),
            f"/ws/{path}",
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=max_bytes + 1024,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            msg = f"workspace file read failed for {path!r}: {detail}"
            raise DockerError(msg)
        return stdout

    async def list_volume_files(
        self, *, volume: str, image: str, max_entries: int
    ) -> list[tuple[int, str]]:
        """List regular files under ``/ws`` in a docker named volume (browse).

        Runs a throwaway ``--rm`` container — read-only rootfs, no network,
        all capabilities dropped — that mounts ``volume`` read-only and walks
        it with Python (portable across the image's interpreters; the runner
        always ships python3), emitting ``<size>\\t<relpath>`` per regular
        file, capped at ``max_entries``. Symlinks are skipped so a dangling /
        escaping link can't perturb the listing. Returns the parsed pairs;
        raises :class:`DockerError` on a non-zero exit.

        ``--entrypoint python3`` overrides the sandbox image's runner so the
        walk script runs instead of the readiness line.
        """
        script = (
            "import os,sys\n"
            f"root='/ws'; n=0; lim={int(max_entries)}\n"
            "for dp,ds,fs in os.walk(root):\n"
            "    for f in sorted(fs):\n"
            "        p=os.path.join(dp,f)\n"
            "        if os.path.islink(p):\n"
            "            continue\n"
            "        try:\n"
            "            s=os.path.getsize(p)\n"
            "        except OSError:\n"
            "            continue\n"
            "        sys.stdout.write(str(s)+chr(9)+os.path.relpath(p,root)+chr(10))\n"
            "        n+=1\n"
            "        if n>=lim:\n"
            "            sys.exit(0)\n"
        )
        argv = [
            "docker",
            "run",
            "--rm",
            *_AUX_CONTAINER_HARDENING_ARGS,
            "--volume",
            f"{volume}:/ws:ro",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=int(max_entries) * 4096 + 4096,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            msg = f"workspace listing failed: {detail}"
            raise DockerError(msg)
        out: list[tuple[int, str]] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            size_str, _, rel = line.partition("\t")
            if not rel:
                continue
            try:
                size = int(size_str)
            except ValueError:
                continue
            out.append((size, rel))
        return out

    async def write_volume_file(self, *, volume: str, path: str, data: bytes, image: str) -> None:
        """Write ``data`` to ``path`` in a docker named volume (document upload).

        Runs a throwaway ``--rm`` container — read-only rootfs, no network,
        all capabilities dropped — that mounts ``volume`` read-WRITE at
        ``/ws`` and pipes the bytes from stdin into ``/ws/<path>`` (creating
        parent dirs). The mounted volume stays writable even under a
        ``--read-only`` rootfs, so the container can touch nothing but the
        workspace. ``path`` is the supervisor-validated relative path (no
        ``..`` / leading ``/``). Raises :class:`DockerError` on non-zero exit.

        ``--entrypoint sh`` overrides the sandbox image's runner so the
        ``mkdir``/``cat`` script reaches busybox / coreutils.
        """
        argv = [
            "docker",
            "run",
            "--rm",
            "-i",
            *_AUX_CONTAINER_HARDENING_ARGS,
            "--volume",
            f"{volume}:/ws",
            "--entrypoint",
            "sh",
            image,
            "-c",
            'p="/ws/$0"; mkdir -p "$(dirname "$p")" && cat > "$p"',
            path,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(input=data)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            msg = f"workspace file write failed for {path!r}: {detail}"
            raise DockerError(msg)

    async def delete_volume_file(self, *, volume: str, path: str, image: str) -> None:
        """Delete ``path`` from a docker named volume (workspace cleanup).

        Mirror of :meth:`write_volume_file`: a throwaway ``--rm`` container —
        read-only rootfs, no network, all capabilities dropped — mounts
        ``volume`` read-WRITE at ``/ws`` and ``rm -f`` the single file. ``path``
        is the supervisor-validated relative path (no ``..`` / leading ``/``),
        passed as ``$0`` so it is never re-parsed by the shell. ``rm -f`` makes
        a missing file a no-op (idempotent delete). Raises :class:`DockerError`
        on a non-zero exit (e.g. the path is a directory).
        """
        argv = [
            "docker",
            "run",
            "--rm",
            *_AUX_CONTAINER_HARDENING_ARGS,
            "--volume",
            f"{volume}:/ws",
            "--entrypoint",
            "sh",
            image,
            "-c",
            'rm -f "/ws/$0"',
            path,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            msg = f"workspace file delete failed for {path!r}: {detail}"
            raise DockerError(msg)

    async def measure_volume_size(self, *, volume: str, image: str) -> int:
        """Measure a named volume's total size in bytes (Stream J.15-补强-1).

        Spawns a throwaway ``--rm`` container — read-only rootfs, no
        network, all capabilities dropped — that mounts ``volume`` at
        ``/ws`` read-only and runs ``du -sb /ws`` (apparent bytes).
        Returns the parsed integer. Raises :class:`DockerError` when
        the measure fails (volume missing, du parse error, etc.).

        ``--entrypoint sh`` overrides the sandbox image's runner so the
        ``du -sb`` argv reaches busybox / coreutils.
        """
        argv = [
            "docker",
            "run",
            "--rm",
            *_AUX_CONTAINER_HARDENING_ARGS,
            "--volume",
            f"{volume}:/ws:ro",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "du -sb /ws | cut -f1",
        ]
        stdout, stderr, code = await self._exec(argv)
        if code != 0:
            detail = stderr.strip() or "non-zero exit"
            msg = f"volume size measure failed for {volume!r}: {detail}"
            raise DockerError(msg)
        try:
            return int(stdout.strip())
        except ValueError as exc:
            msg = f"could not parse du output for {volume!r}: {stdout!r}"
            raise DockerError(msg) from exc

    async def archive_volume(self, *, volume: str, image: str, max_bytes: int) -> bytes:
        """Stream a tar.gz of ``volume`` to stdout via a throwaway container.

        Stream J.15-补强-2. The container mounts ``volume`` read-only at
        ``/ws`` and emits ``tar -czf - .`` on stdout. We capture up to
        ``max_bytes + 1`` bytes — one over the cap so callers can detect
        an over-cap volume and raise. Hardening mirrors
        :meth:`read_volume_file`: no network, read-only rootfs, all caps
        dropped.
        """
        argv = [
            "docker",
            "run",
            "--rm",
            *_AUX_CONTAINER_HARDENING_ARGS,
            "--volume",
            f"{volume}:/ws:ro",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "cd /ws && tar -czf - .",
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=max_bytes + 1024,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            msg = f"volume archive failed for {volume!r}: {detail}"
            raise DockerError(msg)
        if len(stdout) > max_bytes:
            msg = (
                f"volume archive for {volume!r} exceeds the in-memory cap "
                f"({len(stdout)} > {max_bytes} bytes); upgrade to multipart "
                "ObjectStore.put (推 M1) before enabling larger workspaces"
            )
            raise DockerError(msg)
        return stdout

    async def remove_volume(self, *, volume: str) -> None:
        """Force-remove a named volume; idempotent.

        Stream J.15-补强-2 — called after successful archive to free disk.
        """
        _, stderr, code = await self._exec(["docker", "volume", "rm", "--force", volume])
        if code != 0:
            # A missing volume is fine — archive may have raced with a
            # manual cleanup; the row's archived_object_key already
            # records the only durable copy.
            logger.warning(
                "docker_client.volume_remove_failed volume=%s reason=%s",
                volume,
                stderr.strip(),
            )

    async def update_limits(
        self, container_name: str, *, cpus: float, memory_mb: int, pids_limit: int
    ) -> None:
        """Apply per-acquire limits to a claimed pool container (HX-6).

        ``--memory-swap`` is set to 2x memory to mirror ``docker run``'s
        default (swap = 2x memory when only ``--memory`` is given) —
        ``docker update`` rejects a memory value above the container's
        current swap ceiling, so both must move together.
        """
        argv = [
            "docker",
            "update",
            "--cpus",
            str(cpus),
            "--memory",
            f"{memory_mb}m",
            "--memory-swap",
            f"{memory_mb * 2}m",
            "--pids-limit",
            str(pids_limit),
            container_name,
        ]
        _, stderr, code = await self._exec(argv)
        if code != 0:
            detail = stderr.strip() or "non-zero exit"
            msg = f"docker update failed for {container_name!r}: {detail}"
            raise DockerError(msg)

    async def seed_workspace(self, container_name: str, *, files: list[tuple[str, bytes]]) -> None:
        """Stream a tar of ``files`` into ``{container}:{SANDBOX_SKILLS_ROOT}``
        (``/opt/skills``, W2 Task 6 — sandbox-local, per-agent-namespaced by
        the ``<agent_key>/…`` prefix the *caller* already baked into each
        ``relpath``; this layer just lands them).

        Uses ``docker exec <container> tar -xf - -C {SANDBOX_SKILLS_ROOT}``
        (stdin pipe), **not** ``docker cp - <container>:{SANDBOX_SKILLS_ROOT}``.
        The daemon's copy-into-container path (``docker cp``) unconditionally
        rejects the operation with "container rootfs is marked read-only" for
        any container started with ``--read-only`` (``runtime_provider.py``'s
        hardening — Mini-ADR F-5) — it checks the container's
        ``HostConfig.ReadonlyRootfs`` flag globally, without noticing that the
        actual destination is a separate, writable tmpfs mount, not the
        read-only rootfs. Verified reproducible against this repo's sandbox
        image (originally against the ephemeral ``/workspace`` tmpfs, before
        the W2 Task 6 landing-root move — the daemon-side check is
        target-agnostic, so the same failure mode applies here unchanged):
        ``docker cp`` fails 100% of the time on a ``--read-only`` container
        even when the target directory is a writable mount; ``docker exec`` +
        an in-container ``tar`` extraction does not go through that
        daemon-side check at all (a new process inside the container's own
        mount namespace, which already sees the target as writable) and lands
        the files correctly. This was silently broken for every acquire with
        non-empty ``seed_files`` — the failure degrades gracefully (caller
        logs + continues, skill-runtime §5.1), so no run ever hard-failed;
        skill files simply never materialized.

        A side benefit: extraction runs as uid 10000 (the image's ``agent``
        user) because the sandbox's ``docker run`` now carries an explicit
        ``--user 10000:10000`` (W2 Task 6, ``runtime_provider.py``'s
        ``SANDBOX_AGENT_UID``/``SANDBOX_AGENT_GID`` — the image itself no
        longer has a non-root default user to fall back on; W2 Task 9 dropped
        ``USER agent`` so ACS's envd can fork/exec its NAS-mount helper as the
        container's own root identity), so seeded files land ``agent``-owned
        instead of root-owned. :func:`_build_seed_tar`'s 0o755 mode is no
        longer load-bearing for that but is kept as belt-and-braces.
        """
        if not files:
            return
        tar_bytes = _build_seed_tar(files)
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            container_name,
            "tar",
            "-xf",
            "-",
            "-C",
            SANDBOX_SKILLS_ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=tar_bytes)
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip() or "non-zero exit"
            msg = f"workspace seed failed for {container_name!r}: {detail}"
            raise DockerError(msg)

    async def image_exists(self, image: str) -> bool:
        """Probe the local image cache — non-zero exit means missing."""
        _, _, code = await self._exec(["docker", "image", "inspect", image])
        return code == 0

    async def pull_image(self, image: str) -> None:
        """Pull ``image``; raise :class:`DockerError` on failure (HX-6)."""
        _, stderr, code = await self._exec(["docker", "pull", image])
        if code != 0:
            detail = stderr.strip() or "non-zero exit"
            msg = f"docker pull failed for {image!r}: {detail}"
            raise DockerError(msg)

    @staticmethod
    async def _exec(argv: list[str]) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            proc.returncode if proc.returncode is not None else -1,
        )
