"""Sandbox-image smoke test — host-side driver.

Boots the built sandbox image and drives the runner's line-delimited JSON
protocol (one ``{"code": ...}`` request → one response), sending
``smoke_payload.py`` as the code. Passes iff the runner reports
``exit_code == 0`` and the payload printed ``OK`` — i.e. the single full image
ships a working Python 3.12 + the office/data/media libraries + the
soffice/poppler/ffmpeg/node binaries, and the baked runner.py loads. Run under
runc in CI.

B-236 — the same container then runs a short sequence of per-conversation view
checks on the real kernel (``_view_requests``): relative writes land in the
conversation's own directory, other conversations are absent from ``/agent``,
``uploads/`` reaches the agent root, a planted symlink at the agent root's
``uploads`` is skipped while one at the conversation's ``work`` fails closed,
the old view sees only its own ``threads/<id>/``, and the payload — running in a
nested user namespace — can neither ``umount`` the covers nor escape them with
its own ``unshare -m``.

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


_ROOT = "/mnt/workspace/agents/smoke"
_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"


def _view_requests() -> list[tuple[str, dict[str, object], object]]:
    """B-236 — the per-conversation view on a real kernel, in the same container.

    Each entry is ``(name, request, expectation)``; the expectation is either an
    exit code (int) or the exact last stdout line (str). Setup steps run unbound
    (the whole user root as ``/workspace``), the way a pre-B-236 exec could plant
    files under the agent root.
    """

    def unbound(code: str) -> dict[str, object]:
        return {"code": code, "timeout_s": 30}

    def conv(code: str, conv_id: str = _A) -> dict[str, object]:
        return {
            "code": code,
            "timeout_s": 30,
            "agent_root": _ROOT,
            "conv_id": conv_id,
            "thread_id": conv_id,
        }

    def legacy(code: str) -> dict[str, object]:
        return {"code": code, "timeout_s": 30, "agent_root": _ROOT, "thread_id": _A}

    seed = (
        "import os\n"
        f"os.makedirs('/workspace/agents/smoke/threads/{_B}/work', exist_ok=True)\n"
        f"open('/workspace/agents/smoke/threads/{_B}/work/secret.txt','w').write('B')\n"
        "os.makedirs('/workspace/agents/smoke/uploads', exist_ok=True)\n"
        "open('/workspace/agents/smoke/uploads/u.txt','w').write('up')\n"
        "print('seeded')"
    )
    probe = (
        "import os, json\n"
        "open('rel.txt','w').write('a')\n"
        "print(json.dumps([os.listdir('/agent/threads'), "
        f"os.path.exists('/agent/threads/{_B}'), open('uploads/u.txt').read()]))"
    )
    landed = (
        "import os\n"
        f"print(os.path.exists('/workspace/agents/smoke/threads/{_A}/work/rel.txt'), "
        "os.path.exists('/workspace/agents/smoke/rel.txt'))"
    )
    plant_uploads = (
        "import os, shutil\n"
        "shutil.rmtree('/workspace/agents/smoke/uploads')\n"
        "os.symlink('threads', '/workspace/agents/smoke/uploads')\n"
        "print('planted')"
    )
    via_uploads = f"import os\nprint(os.path.exists('/workspace/uploads/{_B}/work/secret.txt'))"
    plant_work = (
        "import os\n"
        f"w = '/workspace/agents/smoke/threads/{_A}/work'\n"
        "os.rename(w, w + '.old')\n"
        f"os.symlink('../{_B}/work', w)\n"
        "print('planted')"
    )
    restore = (
        "import os\n"
        f"w = '/workspace/agents/smoke/threads/{_A}/work'\n"
        "os.remove(w); os.rename(w + '.old', w)\n"
        "u = '/workspace/agents/smoke/uploads'\n"
        "os.remove(u); os.mkdir(u)\n"
        "print('restored')"
    )
    legacy_probe = (
        "import os, json\n"
        "print(json.dumps([os.listdir('threads'), os.listdir('/agent/threads'), "
        f"os.path.exists('threads/{_B}')]))"
    )
    locked = (
        "import os, subprocess\n"
        "def fails(cmd):\n"
        "    return subprocess.run(cmd, capture_output=True).returncode != 0\n"
        "print(fails(['umount', '/agent/threads']), fails(['umount', '/mnt/workspace']), "
        "fails(['unshare', '-m', 'sh', '-c', 'umount /mnt/workspace']), "
        "len(os.listdir('/agent/threads')))"
    )
    return [
        ("seed", unbound(seed), "seeded"),
        ("covers cannot be unmounted from the payload", conv(locked), "True True True 1"),
        ("conversation view", conv(probe), f'[["{_A}"], false, "up"]'),
        ("relative write lands in threads/<conv>/work", unbound(landed), "True False"),
        ("plant uploads -> threads", unbound(plant_uploads), "planted"),
        ("symlinked uploads is skipped", conv(via_uploads), "False"),
        ("plant work -> other conversation", unbound(plant_work), "planted"),
        ("symlinked work dir fails closed", conv("print('ran')"), 98),
        ("restore", unbound(restore), "restored"),
        ("legacy view hides other threads", legacy(legacy_probe), f'[["{_A}"], ["{_A}"], false]'),
    ]


def main(image: str) -> int:
    code = _PAYLOAD.read_text(encoding="utf-8")
    requests: list[tuple[str, dict[str, object], object]] = [
        ("payload", {"code": code, "timeout_s": 30, "agent_root": _ROOT}, "OK"),
        *_view_requests(),
    ]
    stdin = "".join(json.dumps(request) + "\n" for _, request, _ in requests)
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
        input=stdin,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    # First line is the runner's {"ready": true}; then one response per request.
    responses = lines[1:]
    if len(responses) != len(requests):
        print(
            f"expected {len(requests)} responses, got {len(responses)}; stderr:\n{proc.stderr}",
            file=sys.stderr,
        )
        return 1
    failed = 0
    for (name, _request, expected), raw in zip(requests, responses, strict=True):
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"{name}: unparseable response {raw!r}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if not _meets(response, expected):
            failed += 1
            print(
                f"smoke FAILED: {name}\n"
                f"  exit_code={response.get('exit_code')}\n"
                f"  stdout={response.get('stdout')!r}\n"
                f"  stderr={response.get('stderr')!r}",
                file=sys.stderr,
            )
        else:
            print(f"ok: {name}")
    if failed:
        return 1
    print("smoke OK")
    return 0


def _meets(response: dict[str, object], expected: object) -> bool:
    if isinstance(expected, int):
        return response.get("exit_code") == expected
    out = str(response.get("stdout", "")).strip().splitlines()
    if expected == "OK":
        return response.get("exit_code") == 0 and "OK" in out
    return response.get("exit_code") == 0 and bool(out) and out[-1] == expected


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python smoke_test.py <image-tag>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
