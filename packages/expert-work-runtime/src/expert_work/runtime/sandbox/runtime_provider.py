"""Hardened ``docker run`` argv construction for sandbox containers.

Stream F.3 — STREAM-F-DESIGN § 2.3 / Mini-ADR F-3 / F-5.

The Sandbox Supervisor (F.1) launches one container per ``exec_python``
call. *How* it is launched — the OCI runtime and the hardening flags —
is owned here, so a single place enforces the Mini-ADR F-5 checklist and
the dev (``runc``) vs prod (``runsc`` / gVisor) split is one config knob
rather than branching scattered across the supervisor.

subsystem 14 § 5.5: gVisor is Linux-only, so dev (incl. macOS) runs
``runc`` — it verifies sandbox *behaviour*, not isolation *strength*;
the gVisor isolation gates run on a Linux CI runner under ``runsc``.

The provider only *builds* the argv — it never calls Docker. That keeps
it pure and unit-testable (test matrix #43) and leaves process execution
to the supervisor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from expert_work.persistence import SANDBOX_AGENTS_ROOT, SANDBOX_SKILLS_ROOT

#: OCI runtimes the sandbox supports. ``runc`` is Docker's default
#: (dev / macOS); ``runsc`` is gVisor (Linux prod).
SandboxOciRuntime = Literal["runc", "runsc"]

#: Docker network the sandbox attaches to. Egress from it is restricted
#: to the credential-proxy by an iptables allowlist (Mini-ADR F-2); the
#: network itself is created by Stream F.5.
DEFAULT_EGRESS_NETWORK = "expert-work-sandbox-egress"

#: W2 Task 9 baked the sandbox image's non-root user at ``useradd -u
#: 10000`` (subsystem 14 § 5.6) but dropped the image-level ``USER``
#: directive itself — the container must start as root so ACS's envd can
#: fork/exec its NAS-mount storage helper as the container's own identity
#: (spec § 二之二). The local docker backend has no such constraint and
#: still uses the container as a real isolation boundary (ACS's is the
#: microVM instead), so it restores the pre-Task-9 non-root posture itself
#: via ``--user`` below — the same uid/gid the image's ``agent`` user has.
SANDBOX_AGENT_UID = 10000
SANDBOX_AGENT_GID = 10000

#: Matches the image's ``useradd -u 10000 -m`` home directory (``ENV
#: HOME=/home/agent``, W2 Task 9) — the local backend's tmpfs mount target
#: for it, mirroring the image's own path.
SANDBOX_AGENT_HOME = "/home/agent"


@dataclass(frozen=True)
class SandboxResourceLimits:
    """Per-container resource caps. Defaults match STREAM-F-DESIGN § 2.3."""

    cpus: float = 1.0
    memory_mb: int = 512
    pids_limit: int = 128
    workspace_size_mb: int = 64
    #: Scratch ``/tmp`` tmpfs size. The rootfs is read-only, so without a
    #: writable ``/tmp`` any tool that needs scratch space there fails — most
    #: notably ``soffice`` (LibreOffice headless), which puts its named pipe /
    #: socket under ``/tmp`` and dies with "no valid pipe path found" otherwise
    #: (route ① office image). Ephemeral, destroyed with the container.
    tmp_size_mb: int = 256


#: Default caps — a module-level singleton so it can be an argument
#: default without tripping flake8-bugbear B008 (it is frozen / immutable).
DEFAULT_RESOURCE_LIMITS = SandboxResourceLimits()


@dataclass(frozen=True)
class SandboxRuntimeProvider:
    """Builds the hardened ``docker run`` argv for one sandbox container.

    ``oci_runtime`` selects the runtime: ``runsc`` appends
    ``--runtime runsc``; ``runc`` is Docker's default and adds no flag.
    """

    oci_runtime: SandboxOciRuntime
    egress_network: str = DEFAULT_EGRESS_NETWORK
    #: Stream HX-10 — host-visible path to a pinned seccomp profile JSON.
    #: ``None`` emits no ``--security-opt seccomp`` flag (the container then
    #: rides the host Docker daemon's built-in default profile — fine for
    #: dev, but version-drifting). A path pins our own profile
    #: (``infra/sandbox-image/seccomp-profile.json``) so the syscall floor is
    #: decided by our repo, not the host's Docker version. The provider only
    #: forwards the path; existence / JSON validity is validated fail-closed
    #: at supervisor startup (it stays pure / Docker-free).
    seccomp_profile_path: str | None = None
    #: Stream HX-10-F1 — static ``(hostname, ip)`` pairs emitted as
    #: ``--add-host`` flags. gVisor's netstack does not implement Docker's
    #: embedded DNS (127.0.0.11 is the sentry's own loopback —
    #: google/gvisor#7469), so under ``runsc`` the sandbox cannot resolve
    #: sibling containers by name. ``/etc/hosts`` entries are written by
    #: dockerd *before* the sandbox starts (a gofer-backed file read, which
    #: gVisor handles natively), so a fixed-IP mapping for the
    #: credential-proxy works under both runtimes. Empty = no flags (dev /
    #: runc, where embedded DNS works). A tuple of pairs keeps the frozen
    #: dataclass hashable; ordering is preserved into the argv.
    extra_hosts: tuple[tuple[str, str], ...] = ()

    def docker_run_argv(
        self,
        *,
        image: str,
        container_name: str,
        limits: SandboxResourceLimits = DEFAULT_RESOURCE_LIMITS,
        workspace_volume: str | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> list[str]:
        """Return the full ``docker run`` argv for the sandbox.

        The argv carries the Mini-ADR F-5 runtime hardening: read-only
        rootfs, a writable ``/workspace`` mount + an ephemeral scratch
        ``/tmp`` tmpfs, all capabilities dropped, ``no-new-privileges``,
        and PID / memory / CPU caps.
        ``--interactive`` keeps stdin open for the runner's line-JSON
        protocol; the image is the final argument.

        ``workspace_volume`` selects the ``/workspace`` backing: ``None``
        → an ephemeral tmpfs (destroyed with the container); a volume
        name → a docker named volume that persists across containers
        (Stream J.15 — the per-user persistent workspace).

        ``env`` emits ``-e KEY=VALUE`` flags (sandbox-egress §3.3 injects
        ``HTTPS_PROXY``/``HTTP_PROXY``/``NO_PROXY`` here when egress is on).
        """
        argv = [
            "docker",
            "run",
            "--name",
            container_name,
            "--interactive",
            "--read-only",
            # W2 Task 6 — the image (Task 9) starts as root with cwd
            # undeclared (see the SANDBOX_AGENT_UID/GID comment above); the
            # local backend restores the container-level non-root + cwd
            # posture itself, byte-identical to the pre-Task-9 behaviour.
            "--user",
            f"{SANDBOX_AGENT_UID}:{SANDBOX_AGENT_GID}",
            "--workdir",
            "/workspace",
            *self._workspace_mount(limits, workspace_volume),
            # Scratch /tmp — always an ephemeral tmpfs. The rootfs is read-only,
            # and many tools (soffice/poppler, tempfile-heavy libs) need a
            # writable /tmp; mode=1777 so the non-root agent user can write.
            "--tmpfs",
            f"/tmp:rw,size={limits.tmp_size_mb}m,mode=1777",  # noqa: S108 — docker tmpfs mount spec, not a temp-file path
            # Skills / pip-user-site / HOME — W2 Task 9 moved these off the
            # image (the run root must stay bare for ACS's NAS-mount
            # symlink, so nothing can be pre-chowned into it at build time
            # any more), so the local backend supplies all three as
            # sandbox-local, agent-owned tmpfs at run time, one-to-one with
            # the image's own /opt/skills + /opt/agents + /home/agent
            # directories. ``uid=``/``gid=`` on the tmpfs mount option sets
            # ownership directly (no ``mode=1777`` — unlike /workspace and
            # /tmp, only the single non-root --user above ever touches
            # these, so a world-writable mode isn't needed).
            "--tmpfs",
            f"{SANDBOX_SKILLS_ROOT}:rw,size=64m,uid={SANDBOX_AGENT_UID},gid={SANDBOX_AGENT_GID}",
            "--tmpfs",
            f"{SANDBOX_AGENTS_ROOT}:rw,size=512m,uid={SANDBOX_AGENT_UID},gid={SANDBOX_AGENT_GID}",
            "--tmpfs",
            f"{SANDBOX_AGENT_HOME}:rw,size=64m,uid={SANDBOX_AGENT_UID},gid={SANDBOX_AGENT_GID}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *self._seccomp_opt(),
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            f"{limits.memory_mb}m",
            "--cpus",
            str(limits.cpus),
            "--network",
            self.egress_network,
        ]
        for key, value in env:
            argv += ["--env", f"{key}={value}"]
        for hostname, ip in self.extra_hosts:
            argv += ["--add-host", f"{hostname}:{ip}"]
        if self.oci_runtime == "runsc":
            argv += ["--runtime", "runsc"]
        argv.append(image)
        return argv

    def _seccomp_opt(self) -> list[str]:
        """The ``--security-opt seccomp=`` flag, or empty when unset.

        ``None`` → no flag (host Docker default profile). A path → pin our
        own profile. Applies under both runc and runsc: gVisor still honours
        seccomp on the host-side sentry process, so the two layers stack.
        """
        if self.seccomp_profile_path is None:
            return []
        return ["--security-opt", f"seccomp={self.seccomp_profile_path}"]

    @staticmethod
    def _workspace_mount(limits: SandboxResourceLimits, workspace_volume: str | None) -> list[str]:
        """The ``/workspace`` mount flags — tmpfs or a persistent volume."""
        if workspace_volume is None:
            # Ephemeral tmpfs. mode=1777: the tmpfs root mounts root-owned,
            # so without it the image's non-root ``agent`` user cannot
            # create files (F.8 gate #1).
            return [
                "--tmpfs",
                f"/workspace:rw,size={limits.workspace_size_mb}m,mode=1777",
            ]
        # Stream J.15 — a per-user docker named volume. This comment
        # previously claimed a fresh volume inherits the image's
        # ``/workspace`` ownership (``agent:agent``); that relied on the
        # image baking a ``WORKDIR /workspace`` + chown, which W2 Task 9
        # removed (the run root must stay bare for ACS's NAS-mount
        # symlink). Empirically reconfirmed post-Task-9 (``docker run
        # --user 10000:10000 -v freshvol:/workspace <image> touch
        # /workspace/x`` → ``Permission denied``): a never-before-mounted
        # volume now lands root:root, so the non-root --user above cannot
        # write into it until something chowns it once. No such bootstrap
        # exists yet — flagged as a W2 Task 6 follow-up (would need a new
        # docker_client aux op + a supervisor call site, both beyond this
        # task's two authorized touch points), not fixed here.
        return ["--volume", f"{workspace_volume}:/workspace"]


def make_sandbox_runtime_provider(
    oci_runtime: str,
    *,
    egress_network: str = DEFAULT_EGRESS_NETWORK,
    seccomp_profile_path: str | None = None,
    extra_hosts: dict[str, str] | None = None,
) -> SandboxRuntimeProvider:
    """Build a :class:`SandboxRuntimeProvider`, validating ``oci_runtime``.

    ``oci_runtime`` is typed ``str`` (not :data:`SandboxOciRuntime`)
    because it arrives from ``environments/{env}.yaml`` — an arbitrary
    runtime string. An unrecognised value raises :class:`ValueError`,
    mirroring :func:`~expert_work.runtime.secret_store.make_secret_store`.

    ``seccomp_profile_path`` is forwarded verbatim — the caller
    (supervisor startup) is responsible for the fail-closed existence /
    JSON-validity check, keeping this factory pure. ``extra_hosts``
    (HX-10-F1) maps hostname → fixed IP; insertion order is preserved
    into the argv.
    """
    valid: tuple[str, ...] = get_args(SandboxOciRuntime)
    if oci_runtime not in valid:
        msg = f"unknown sandbox OCI runtime: {oci_runtime!r} (expected one of {valid})"
        raise ValueError(msg)
    return SandboxRuntimeProvider(
        oci_runtime=oci_runtime,  # type: ignore[arg-type]  # validated above
        egress_network=egress_network,
        seccomp_profile_path=seccomp_profile_path,
        extra_hosts=tuple((extra_hosts or {}).items()),
    )
