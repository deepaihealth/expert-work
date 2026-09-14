"""Tests for the fail-closed seccomp profile validation — Stream HX-10."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_supervisor.seccomp import SeccompProfileError, validate_seccomp_profile
from sandbox_supervisor.settings import SandboxSupervisorSettings

#: The pinned profile shipped in the repo — its content contract is asserted
#: below so a regression (e.g. someone adding io_uring to the allowlist) is
#: caught by the unit suite, not in production.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PINNED_PROFILE = _REPO_ROOT / "infra" / "sandbox-image" / "seccomp-profile.json"


# ---------- fail-closed validation ----------


def test_none_fails_closed_since_b60() -> None:
    # B-60:每次 exec 都要在自己的 user ns 里 mount;宿主 Docker 默认 profile 把 unshare /
    # mount 都关在 CAP_SYS_ADMIN 后面(本机实测 EPERM),不配钉住的 profile 沙箱等于废的。
    with pytest.raises(SeccompProfileError, match="B-60"):
        validate_seccomp_profile(None)


def test_valid_profile_passes(tmp_path: Path) -> None:
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"defaultAction": "SCMP_ACT_ERRNO"}))
    validate_seccomp_profile(str(profile))


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SeccompProfileError, match="not found"):
        validate_seccomp_profile(str(tmp_path / "absent.json"))


def test_invalid_json_fails_closed(tmp_path: Path) -> None:
    profile = tmp_path / "bad.json"
    profile.write_text("{not json")
    with pytest.raises(SeccompProfileError, match="not valid JSON"):
        validate_seccomp_profile(str(profile))


def test_missing_default_action_fails_closed(tmp_path: Path) -> None:
    profile = tmp_path / "incomplete.json"
    profile.write_text(json.dumps({"syscalls": []}))
    with pytest.raises(SeccompProfileError, match="defaultAction"):
        validate_seccomp_profile(str(profile))


# ---------- pinned profile content contract ----------


def _load_pinned() -> dict:
    return json.loads(_PINNED_PROFILE.read_text(encoding="utf-8"))


def test_pinned_profile_is_valid() -> None:
    validate_seccomp_profile(str(_PINNED_PROFILE))


def test_pinned_profile_denies_by_default() -> None:
    assert _load_pinned()["defaultAction"] == "SCMP_ACT_ERRNO"


def _unconditional_allow(profile: dict) -> set[str]:
    """syscalls allowed with no capability gate (i.e. allowed under cap-drop ALL)."""
    names: set[str] = set()
    for grp in profile["syscalls"]:
        caps = grp.get("includes", {}).get("caps")
        if grp["action"] == "SCMP_ACT_ALLOW" and not caps:
            names.update(grp["names"])
    return names


def _cap_gated_allow(profile: dict) -> set[str]:
    names: set[str] = set()
    for grp in profile["syscalls"]:
        caps = grp.get("includes", {}).get("caps")
        if grp["action"] == "SCMP_ACT_ALLOW" and caps:
            names.update(grp["names"])
    return names


@pytest.mark.parametrize(
    "syscall",
    [
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
        "userfaultfd",
        "keyctl",
        "add_key",
        "request_key",
    ],
)
def test_high_risk_syscalls_not_in_allowlist(syscall: str) -> None:
    # These are escape-prone and never appear in the allowlist → the
    # SCMP_ACT_ERRNO default denies them. Pinning the profile keeps that true
    # regardless of the host Docker version (old hosts still allow io_uring).
    assert syscall not in _unconditional_allow(_load_pinned())


@pytest.mark.parametrize("syscall", ["bpf", "perf_event_open", "setns"])
def test_privileged_syscalls_only_cap_gated(syscall: str) -> None:
    # Allowed only behind a CAP_* the sandbox drops (cap-drop ALL) → denied.
    profile = _load_pinned()
    assert syscall not in _unconditional_allow(profile)
    assert syscall in _cap_gated_allow(profile)


def test_clone3_keeps_enosys_fallback() -> None:
    # clone3 must return ENOSYS(38) without CAP_SYS_ADMIN so glibc falls back
    # to clone — NOT EPERM, which would crash modern glibc/Python. This is the
    # upstream default behaviour and must be preserved verbatim.
    clone3 = [
        grp
        for grp in _load_pinned()["syscalls"]
        if "clone3" in grp["names"] and grp["action"] == "SCMP_ACT_ERRNO"
    ]
    assert clone3, "clone3 ENOSYS entry missing"
    assert clone3[0]["errnoRet"] == 38


def test_pinned_profile_allows_core_runtime_syscalls() -> None:
    # Smoke check the allowlist did not get truncated — the runner needs these.
    allow = _unconditional_allow(_load_pinned())
    for needed in ("read", "write", "openat", "mmap", "futex", "execve", "clone"):
        assert needed in allow, f"core syscall {needed} missing from allowlist"


def _ungated_rules_for(profile: dict, name: str) -> list[dict]:
    return [
        grp
        for grp in profile["syscalls"]
        if name in grp["names"]
        and grp["action"] == "SCMP_ACT_ALLOW"
        and not grp.get("includes", {}).get("caps")
    ]


def test_unshare_is_allowed_only_for_user_and_mount_namespaces() -> None:
    # B-60 —— 只放 CLONE_NEWUSER|CLONE_NEWNS(mask 掉这两位后其余必须为 0);pid/net/ipc/
    # uts/cgroup namespace 仍然拒。
    rules = _ungated_rules_for(_load_pinned(), "unshare")
    assert len(rules) == 1
    assert rules[0]["names"] == ["unshare"]
    assert rules[0]["args"] == [
        {"index": 0, "value": 4026400767, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}
    ]
    assert 4026400767 == 0xFFFFFFFF & ~(0x00020000 | 0x10000000)  # ~(NEWNS | NEWUSER)


def test_mount_and_the_new_mount_api_are_allowed_without_caps() -> None:
    # util-linux 2.41 走 open_tree/move_mount/fsopen…;默认 ERRNO 回 EPERM 不是 ENOSYS,
    # libmount 不回落经典 mount(2)(本机实测 "permission denied")。
    allow = _unconditional_allow(_load_pinned())
    for name in (
        "mount",
        "open_tree",
        "move_mount",
        "fsopen",
        "fsconfig",
        "fsmount",
        "fspick",
        "mount_setattr",
    ):
        assert name in allow, name


def test_umount_setns_pivot_root_stay_cap_gated() -> None:
    allow = _unconditional_allow(_load_pinned())
    for name in ("umount2", "setns", "pivot_root"):
        assert name not in allow, name


# ---------------------------------------------------------------------------
# Stream HX-10-F1 — EXPERT_WORK_SANDBOX_EXTRA_HOSTS parsing (same fail-closed
# misconfig discipline as the seccomp path, hence this module).
# ---------------------------------------------------------------------------


def test_extra_hosts_empty_parses_to_no_entries() -> None:
    settings = SandboxSupervisorSettings(extra_hosts="")
    assert settings.parsed_extra_hosts == {}


def test_extra_hosts_parses_single_and_multiple_entries() -> None:
    single = SandboxSupervisorSettings(extra_hosts="credential-proxy.internal=172.30.0.10")
    assert single.parsed_extra_hosts == {"credential-proxy.internal": "172.30.0.10"}

    multi = SandboxSupervisorSettings(
        extra_hosts=" credential-proxy.internal = 172.30.0.10 , collector.internal=172.30.0.11 ,"
    )
    assert multi.parsed_extra_hosts == {
        "credential-proxy.internal": "172.30.0.10",
        "collector.internal": "172.30.0.11",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "no-equals-sign",
        "=172.30.0.10",
        "credential-proxy.internal=",
    ],
)
def test_extra_hosts_malformed_entry_raises(raw: str) -> None:
    settings = SandboxSupervisorSettings(extra_hosts=raw)
    with pytest.raises(ValueError, match="malformed EXPERT_WORK_SANDBOX_EXTRA_HOSTS"):
        _ = settings.parsed_extra_hosts
