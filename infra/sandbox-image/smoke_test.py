"""Sandbox-image smoke test — host-side driver.

Boots the built sandbox image and drives the runner's line-delimited JSON
protocol (one ``{"code": ...}`` request → one response), sending
``smoke_payload.py`` as the code. Passes iff the runner reports
``exit_code == 0`` and the payload printed ``OK`` — i.e. the single full image
ships a working Python 3.12 + the office/data/media libraries + the
soffice/poppler/ffmpeg/node binaries, and the baked runner.py loads. Run under
runc in CI.

Usage:
    python smoke_test.py <image-tag>
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PAYLOAD = Path(__file__).with_name("smoke_payload.py")
# .resolve(): --security-opt seccomp= needs an absolute path — the workflow
# invokes this script as `python infra/sandbox-image/smoke_test.py <tag>`
# from the repo root, where plain __file__ is relative.
_SECCOMP_PROFILE = Path(__file__).resolve().with_name("seccomp-profile.json")


def main(image: str) -> int:
    code = _PAYLOAD.read_text(encoding="utf-8")
    request = (
        json.dumps({"code": code, "timeout_s": 30, "agent_root": "/mnt/workspace/agents/smoke"})
        + "\n"
    )
    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "run",
            "--rm",
            "-i",
            # B-60 T10 (fix round 1) — the runner now execs every request
            # inside its own user+mount namespace (`unshare -Urm`) that binds
            # an agent directory onto /workspace; mirror the pieces of the
            # local backend's own hardening
            # (packages/expert-work-runtime/.../runtime_provider.py) that
            # path needs, same as a real sandbox container gets:
            #   * a writable /mnt/workspace tmpfs — the NAS-mount stand-in
            #     the runner's namespace binds *from* (agent_root below
            #     points inside it).
            #   * a **read-only** 4k /workspace tmpfs — the bind target. The
            #     image has no writable /workspace of its own (W2 Task 9);
            #     a request that skipped the namespace now fails loudly
            #     (EROFS) instead of silently writing into the container.
            #   * the pinned seccomp profile — docker's default profile
            #     blocks `unshare`/`mount`.
            #   * `apparmor=unconfined` — docker-default AppArmor carries
            #     `deny mount,`, which blocks the namespace's own mount(2)
            #     calls too.
            "--tmpfs",
            "/mnt/workspace:rw,size=64m,mode=1777",
            "--tmpfs",
            "/workspace:ro,size=4k",
            "--security-opt",
            f"seccomp={_SECCOMP_PROFILE}",
            "--security-opt",
            "apparmor=unconfined",
            image,
        ],
        input=request,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        print(f"no runner output; stderr:\n{proc.stderr}", file=sys.stderr)
        return 1
    # First line is the runner's {"ready": true}; the last is the response.
    try:
        response = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        print(f"unparseable response {lines[-1]!r}: {exc}", file=sys.stderr)
        return 1
    if response.get("exit_code") != 0 or "OK" not in response.get("stdout", ""):
        print(
            "smoke FAILED\n"
            f"  exit_code={response.get('exit_code')}\n"
            f"  stdout={response.get('stdout')!r}\n"
            f"  stderr={response.get('stderr')!r}",
            file=sys.stderr,
        )
        return 1
    print(f"smoke OK\n{response['stdout']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python smoke_test.py <image-tag>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
