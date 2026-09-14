"""Fail-closed validation of the pinned seccomp profile — Stream HX-10.

The :class:`SandboxRuntimeProvider` forwards the profile path verbatim and
stays Docker-free / pure (test matrix #43). The *fail-closed* contract lives
here and runs once at supervisor startup: a configured profile path that is
missing or not valid JSON is a security misconfiguration — not a transient
fault — so the supervisor must refuse to start rather than silently fall back
to the host default profile (HX-K2: security-config events are the exception
to the fail-open axiom).
"""

from __future__ import annotations

import json
from pathlib import Path


class SeccompProfileError(RuntimeError):
    """The configured seccomp profile is missing or not valid JSON."""


def validate_seccomp_profile(path: str | None) -> None:
    """Validate the pinned seccomp profile, fail-closed.

    ``None`` is refused since B-60: every exec bind-mounts its own view inside an
    unprivileged user namespace, and the host Docker default profile keeps
    ``unshare``/``mount`` behind CAP_SYS_ADMIN (which the sandbox drops) — so a
    supervisor without the pinned profile launches sandboxes whose every exec
    fails closed on its first mount. A non-``None`` path must point at an
    existing, readable file whose contents parse as JSON with a ``defaultAction``.
    """
    if path is None:
        msg = (
            "EXPERT_WORK_SANDBOX_SECCOMP_PROFILE_PATH is required (B-60): the host Docker "
            "default seccomp profile denies unshare/mount without CAP_SYS_ADMIN, so every "
            "exec would fail closed on its first bind mount. Point it at "
            "infra/sandbox-image/seccomp-profile.json readable inside the supervisor "
            "container (compose mounts it read-only at /etc/expert-work/seccomp-profile.json)."
        )
        raise SeccompProfileError(msg)
    profile = Path(path)
    if not profile.is_file():
        msg = f"seccomp profile not found: {path!r}"
        raise SeccompProfileError(msg)
    try:
        parsed = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"seccomp profile is not valid JSON: {path!r} ({exc})"
        raise SeccompProfileError(msg) from exc
    if not isinstance(parsed, dict) or "defaultAction" not in parsed:
        msg = f"seccomp profile missing 'defaultAction': {path!r}"
        raise SeccompProfileError(msg)
