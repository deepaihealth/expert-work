"""Unit tests for :class:`SandboxRuntimeProvider` — Stream F.3 (test matrix #43).

Covers the Mini-ADR F-5 hardening flags landing in the ``docker run`` argv
and the dev (``runc``) vs prod (``runsc``) runtime split.
"""

from __future__ import annotations

import pytest

from expert_work.runtime.sandbox import (
    DEFAULT_EGRESS_NETWORK,
    SandboxResourceLimits,
    SandboxRuntimeProvider,
    make_sandbox_runtime_provider,
)


def _flag_value(argv: list[str], flag: str) -> str:
    """Return the token immediately after ``flag`` in ``argv``."""
    return argv[argv.index(flag) + 1]


def _runc_provider() -> SandboxRuntimeProvider:
    return SandboxRuntimeProvider(oci_runtime="runc")


def _runsc_provider() -> SandboxRuntimeProvider:
    return SandboxRuntimeProvider(oci_runtime="runsc")


# ---------- hardening flags ----------


def test_argv_carries_all_hardening_flags() -> None:
    argv = _runc_provider().docker_run_argv(image="expert-work-sandbox:dev", container_name="sb-1")

    assert "--read-only" in argv
    assert _flag_value(argv, "--cap-drop") == "ALL"
    assert _flag_value(argv, "--security-opt") == "no-new-privileges"
    assert _flag_value(argv, "--pids-limit") == "128"
    assert _flag_value(argv, "--memory") == "512m"
    assert _flag_value(argv, "--cpus") == "1.0"
    assert _flag_value(argv, "--network") == DEFAULT_EGRESS_NETWORK
    assert _flag_value(argv, "--tmpfs") == "/workspace:rw,size=64m,mode=1777"


# ---------- W2 Task 6 — local-docker non-root/cwd posture + skill/pip/home tmpfs ----------


def test_argv_runs_as_the_non_root_agent_user() -> None:
    # The image (W2 Task 9) starts as root — no image-level USER — so the
    # ACS platform can fork/exec its NAS-mount helper as the container's own
    # identity. The local docker backend still uses the container itself as
    # the isolation boundary (ACS's is the microVM instead), so it restores
    # the pre-Task-9 non-root posture explicitly via --user.
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert _flag_value(argv, "--user") == "10000:10000"


def test_argv_sets_workdir_to_workspace() -> None:
    # Mirrors the --user restoration: the image no longer declares a
    # WORKDIR (that would pre-create /workspace and block ACS's NAS-mount
    # symlink), so the local backend sets cwd itself.
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert _flag_value(argv, "--workdir") == "/workspace"


def test_argv_mounts_skills_agents_home_as_owned_tmpfs() -> None:
    # W2 Task 9 moved /opt/skills, /opt/agents and HOME off the image (the
    # run root must stay bare); the local backend supplies all three as
    # sandbox-local, agent-owned tmpfs at run time so the non-root --user
    # above can write into them (uid=/gid= sets ownership directly — no
    # mode=1777 needed since only the agent uid ever touches them).
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    tmpfs = {}
    for i, tok in enumerate(argv):
        if tok == "--tmpfs":
            target, _, spec = argv[i + 1].partition(":")
            tmpfs[target] = spec
    assert tmpfs["/opt/skills"] == "rw,size=64m,uid=10000,gid=10000"
    assert tmpfs["/opt/agents"] == "rw,size=512m,uid=10000,gid=10000"
    assert tmpfs["/home/agent"] == "rw,size=64m,uid=10000,gid=10000"


def test_argv_user_workdir_and_extra_tmpfs_present_under_persistent_workspace_too() -> None:
    # The --user/--workdir/skills/agents/home additions are unconditional —
    # they don't depend on whether /workspace itself is an ephemeral tmpfs
    # or a J.15 persistent named volume.
    argv = _runc_provider().docker_run_argv(
        image="img", container_name="sb-1", workspace_volume="expert-work-ws-abc"
    )
    assert _flag_value(argv, "--user") == "10000:10000"
    assert _flag_value(argv, "--workdir") == "/workspace"
    tmpfs_targets = [argv[i + 1].split(":")[0] for i, t in enumerate(argv) if t == "--tmpfs"]
    assert "/opt/skills" in tmpfs_targets
    assert "/opt/agents" in tmpfs_targets
    assert "/home/agent" in tmpfs_targets


def test_argv_keeps_stdin_open_for_runner_protocol() -> None:
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert "--interactive" in argv


def test_argv_structure_name_then_image_last() -> None:
    argv = _runc_provider().docker_run_argv(image="expert-work-sandbox:dev", container_name="sb-7")
    assert argv[:2] == ["docker", "run"]
    assert _flag_value(argv, "--name") == "sb-7"
    assert argv[-1] == "expert-work-sandbox:dev"


# ---------- runc vs runsc split ----------


def test_runc_omits_runtime_flag() -> None:
    # runc is Docker's default — no --runtime flag is emitted.
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert "--runtime" not in argv


def test_runsc_appends_gvisor_runtime() -> None:
    argv = _runsc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert _flag_value(argv, "--runtime") == "runsc"


# ---------- custom limits ----------


def test_custom_limits_reflected_in_argv() -> None:
    limits = SandboxResourceLimits(cpus=2.5, memory_mb=1024, pids_limit=64, workspace_size_mb=128)
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1", limits=limits)
    assert _flag_value(argv, "--cpus") == "2.5"
    assert _flag_value(argv, "--memory") == "1024m"
    assert _flag_value(argv, "--pids-limit") == "64"
    assert _flag_value(argv, "--tmpfs") == "/workspace:rw,size=128m,mode=1777"


def test_custom_egress_network_reflected() -> None:
    provider = SandboxRuntimeProvider(oci_runtime="runc", egress_network="custom-net")
    argv = provider.docker_run_argv(image="img", container_name="sb-1")
    assert _flag_value(argv, "--network") == "custom-net"


# ---------- workspace mount: ephemeral tmpfs vs persistent volume (J.15) ----------


def test_default_workspace_is_ephemeral_tmpfs() -> None:
    # No workspace_volume → the pre-J.15 ephemeral tmpfs.
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert _flag_value(argv, "--tmpfs") == "/workspace:rw,size=64m,mode=1777"
    assert "--volume" not in argv


def test_persistent_workspace_mounts_named_volume() -> None:
    # Stream J.15 — a workspace_volume mounts a docker named volume for
    # /workspace (no /workspace tmpfs), but the scratch /tmp tmpfs stays —
    # along with the W2 Task 6 skills/agents/home tmpfs, which are
    # unconditional (independent of the /workspace backing).
    argv = _runc_provider().docker_run_argv(
        image="img", container_name="sb-1", workspace_volume="expert-work-ws-abc"
    )
    assert _flag_value(argv, "--volume") == "expert-work-ws-abc:/workspace"
    # /workspace is a volume, not a tmpfs.
    tmpfs_targets = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
    assert tmpfs_targets == [
        "/tmp:rw,size=256m,mode=1777",  # noqa: S108 — mount spec literal
        "/opt/skills:rw,size=64m,uid=10000,gid=10000",
        "/opt/agents:rw,size=512m,uid=10000,gid=10000",
        "/home/agent:rw,size=64m,uid=10000,gid=10000",
    ]


def test_argv_always_mounts_scratch_tmp() -> None:
    # Read-only rootfs needs a writable /tmp (soffice named pipe etc.); always
    # an ephemeral tmpfs, both for tmpfs- and volume-backed /workspace.
    for vol in (None, "expert-work-ws-abc"):
        argv = _runc_provider().docker_run_argv(
            image="img", container_name="sb-1", workspace_volume=vol
        )
        tmpfs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
        assert "/tmp:rw,size=256m,mode=1777" in tmpfs  # noqa: S108 — mount spec literal


# ---------- factory ----------


def test_factory_builds_provider_for_valid_runtime() -> None:
    assert make_sandbox_runtime_provider("runc").oci_runtime == "runc"
    assert make_sandbox_runtime_provider("runsc").oci_runtime == "runsc"


def test_factory_rejects_unknown_runtime() -> None:
    with pytest.raises(ValueError, match="unknown sandbox OCI runtime"):
        make_sandbox_runtime_provider("firecracker")


def test_factory_forwards_seccomp_profile_path() -> None:
    provider = make_sandbox_runtime_provider("runc", seccomp_profile_path="/etc/seccomp.json")
    assert provider.seccomp_profile_path == "/etc/seccomp.json"


# ---------- Stream HX-10 — seccomp pinned profile ----------


def test_no_seccomp_opt_when_unset() -> None:
    # Default None → no --security-opt seccomp (host Docker default profile).
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert "seccomp" not in " ".join(argv)


def test_seccomp_opt_emitted_when_path_set() -> None:
    provider = SandboxRuntimeProvider(oci_runtime="runc", seccomp_profile_path="/etc/sb.json")
    argv = provider.docker_run_argv(image="img", container_name="sb-1")
    # --security-opt appears twice now (no-new-privileges + seccomp); the
    # seccomp value carries the pinned profile path.
    opts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--security-opt"]
    assert "no-new-privileges" in opts
    assert "seccomp=/etc/sb.json" in opts


def test_seccomp_opt_under_runsc_too() -> None:
    # gVisor still honours seccomp on the host sentry — the two layers stack.
    provider = SandboxRuntimeProvider(oci_runtime="runsc", seccomp_profile_path="/etc/sb.json")
    argv = provider.docker_run_argv(image="img", container_name="sb-1")
    assert "seccomp=/etc/sb.json" in argv
    assert _flag_value(argv, "--runtime") == "runsc"


# ---------- Stream HX-10 — misconfig assertions (SANDBOXESCAPEBENCH 100%-escape classes) ----------


def test_argv_never_mounts_docker_socket() -> None:
    # Exposed docker.sock is the #1 100%-escape misconfig — it must never
    # reach a sandbox container under any workspace shape.
    for vol in (None, "expert-work-ws-abc"):
        argv = _runc_provider().docker_run_argv(
            image="img", container_name="sb-1", workspace_volume=vol
        )
        assert "/var/run/docker.sock" not in " ".join(argv)
        assert "docker.sock" not in " ".join(argv)


def test_argv_never_privileged_or_cap_add() -> None:
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    assert "--privileged" not in argv
    assert "--cap-add" not in argv


def test_argv_never_host_path_bind_mount() -> None:
    # The only --volume we ever emit is a docker *named* volume (J.15);
    # a host-path bind mount (source starting with '/') is forbidden.
    argv = _runc_provider().docker_run_argv(
        image="img", container_name="sb-1", workspace_volume="expert-work-ws-abc"
    )
    volumes = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--volume"]
    for spec in volumes:
        assert not spec.startswith("/"), f"host-path bind mount: {spec}"


def test_argv_keeps_core_hardening_under_all_shapes() -> None:
    for vol in (None, "expert-work-ws-abc"):
        argv = _runc_provider().docker_run_argv(
            image="img", container_name="sb-1", workspace_volume=vol
        )
        assert _flag_value(argv, "--cap-drop") == "ALL"
        assert "--read-only" in argv
        assert "no-new-privileges" in [
            argv[i + 1] for i, tok in enumerate(argv) if tok == "--security-opt"
        ]


# ---------------------------------------------------------------------------
# Stream HX-10-F1 — extra_hosts (--add-host) for gVisor proxy addressing
# ---------------------------------------------------------------------------


def test_no_extra_hosts_emits_no_add_host_flag() -> None:
    provider = SandboxRuntimeProvider(oci_runtime="runc")
    argv = provider.docker_run_argv(image="img", container_name="c")
    assert "--add-host" not in argv


def test_extra_hosts_emit_add_host_pairs_in_order() -> None:
    provider = SandboxRuntimeProvider(
        oci_runtime="runsc",
        extra_hosts=(
            ("credential-proxy.internal", "172.30.0.10"),
            ("collector.internal", "172.30.0.11"),
        ),
    )
    argv = provider.docker_run_argv(image="img", container_name="c")
    first = argv.index("--add-host")
    assert argv[first : first + 2] == ["--add-host", "credential-proxy.internal:172.30.0.10"]
    second = argv.index("--add-host", first + 2)
    assert argv[second : second + 2] == ["--add-host", "collector.internal:172.30.0.11"]
    # /etc/hosts entries must be in place regardless of runtime flag order;
    # the runsc runtime flag still lands after them.
    assert argv.index("--runtime") > second


def test_factory_forwards_extra_hosts() -> None:
    provider = make_sandbox_runtime_provider(
        "runsc",
        extra_hosts={"credential-proxy.internal": "172.30.0.10"},
    )
    assert provider.extra_hosts == (("credential-proxy.internal", "172.30.0.10"),)
    # Default stays empty — runc dev path emits no flags (regression).
    assert make_sandbox_runtime_provider("runc").extra_hosts == ()
