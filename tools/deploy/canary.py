"""Release canary — one real agent run as the release gate (X-14 P1).

``smoke.sh`` only probes HTTP: during the e2b incident all nine checks were
green while every sandbox tool was dead. This script closes that gap by
driving ONE real run through the execution surface and treating its outcome
as the release verdict:

1. ``POST /v1/agents/{agent_code}/runs`` (external plane, ``mode: "stream"``)
   with a fixed instruction: ``exec_python`` computes a deterministic value,
   ``write_file`` writes it into the workspace, ``save_artifact`` registers
   the file.
2. Consume the SSE stream until the ``end`` frame; assert
   ``status == "success"`` and a non-empty ``artifacts`` snapshot
   (``orchestrator/sse.py::end_frame_data``).
3. Download the artifact via ``GET .../artifacts/download`` and assert the
   bytes contain the expected deterministic line.

Any red step = the release is NOT good (``rollback.sh`` guidance is printed
by the caller, ``release.sh`` stage 6).

Inputs are environment variables only — the API key must never appear in
argv (visible in ``ps`` / the k8s exec audit trail) or in any log line:

    EXPERT_WORK_CANARY_API_KEY      required; ``aforge_pat_*`` bearer (never logged)
    EXPERT_WORK_CANARY_AGENT_CODE   default ``release-canary``
    EXPERT_WORK_CANARY_USER_ID      default ``canary:release``
    EXPERT_WORK_CANARY_BASE_URL     default ``http://localhost:8000`` (in-pod)
    EXPERT_WORK_CANARY_DEADLINE_S   default 300 (whole-run wall clock bound)

Dev machines usually can't reach the cluster directly (``smoke.sh`` header),
so ``release.sh`` ships this file into a Running+Ready control-plane pod over
stdin and runs it with the pod's venv python. Seeding of the credentials +
canary agent: ``python -m control_plane.seed_canary`` (runbook §1.6).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

DEFAULT_AGENT_CODE = "release-canary"
DEFAULT_USER_ID = "canary:release"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_DEADLINE_S = 300.0

#: The artifact name the prompt instructs the agent to register.
CANARY_ARTIFACT_NAME = "canary-check.txt"

#: Deterministic payload: ``exec_python`` computes the product so the exact
#: digits come from the sandbox, not from the LLM's arithmetic.
_FACTOR_A = 6683
_FACTOR_B = 9109


def expected_line() -> str:
    """The exact line the canary artifact must contain."""
    return f"CANARY_OK {_FACTOR_A * _FACTOR_B}"


#: Fixed instruction. Tool-by-tool, no room for interpretation — the file
#: content is whatever ``exec_python`` printed, so a green canary proves the
#: whole exec → write → register → download chain, not the model's math.
CANARY_PROMPT = (
    "This is an automated release canary check. Perform exactly these three "
    "steps in order, then stop:\n"
    "1. Call exec_python with exactly this code: "
    f'print("CANARY_OK " + str({_FACTOR_A} * {_FACTOR_B}))\n'
    f"2. Call write_file to create the file {CANARY_ARTIFACT_NAME} in the workspace; "
    "its content must be exactly the single line exec_python printed.\n"
    f'3. Call save_artifact with name "{CANARY_ARTIFACT_NAME}".\n'
    "Do not call any other tool. Do not add commentary inside the file."
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set — export it before running (see module docstring)")
    return value


def parse_end_frame(lines: list[str]) -> dict[str, Any] | None:
    """The ``end`` frame's ``data`` payload from captured SSE lines, if any.

    Wire shape (``orchestrator/sse.py::format_sse``): optional ``id:`` line,
    then ``event: end``, then ``data: {...}``. Same scan as
    ``tools/ha/verify_cancel.py::_end_frame_status``, but returning the whole
    payload — the canary needs ``artifacts`` as well as ``status``. Returns
    ``None`` when no parseable end frame arrived (either way the canary is
    red, so the two cases don't need distinguishing).
    """
    for i, line in enumerate(lines):
        if line.strip() == "event: end":
            for follow in lines[i + 1 : i + 4]:
                if follow.startswith("data: "):
                    try:
                        payload = json.loads(follow[len("data: ") :])
                    except json.JSONDecodeError:
                        return None
                    return payload if isinstance(payload, dict) else None
    return None


def pick_artifact(
    artifacts: list[Any], preferred_name: str = CANARY_ARTIFACT_NAME
) -> dict[str, Any] | None:
    """Choose which end-frame artifact entry to download.

    Prefer the name the prompt dictated; fall back to the first well-formed
    entry so a slightly creative agent still gets its artifact verified.
    """
    dicts = [a for a in artifacts if isinstance(a, dict) and a.get("name")]
    for entry in dicts:
        if entry.get("name") == preferred_name:
            return entry
    return dicts[0] if dicts else None


def content_ok(data: bytes) -> bool:
    """Downloaded artifact bytes must contain the deterministic line."""
    return expected_line().encode("utf-8") in data


def download_params(entry: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    """Query params for ``GET .../artifacts/download``.

    ``version`` rides along when the snapshot carries one — the endpoint
    treats it as a validation gate (409 on mismatch), which is exactly what
    a canary wants: never silently verify different bytes than the run
    registered.
    """
    params: dict[str, Any] = {"user_id": user_id, "name": entry["name"]}
    version = entry.get("version")
    if isinstance(version, int):
        params["version"] = version
    return params


async def _amain() -> int:
    api_key = _require_env("EXPERT_WORK_CANARY_API_KEY")  # never logged
    agent_code = os.environ.get("EXPERT_WORK_CANARY_AGENT_CODE") or DEFAULT_AGENT_CODE
    user_id = os.environ.get("EXPERT_WORK_CANARY_USER_ID") or DEFAULT_USER_ID
    base_url = os.environ.get("EXPERT_WORK_CANARY_BASE_URL") or DEFAULT_BASE_URL
    deadline_s = float(os.environ.get("EXPERT_WORK_CANARY_DEADLINE_S") or DEFAULT_DEADLINE_S)

    print(f"canary: agent={agent_code} user={user_id} base={base_url} deadline={deadline_s:.0f}s")
    headers = {"Authorization": f"Bearer {api_key}"}
    started = time.monotonic()

    end_payload: dict[str, Any] | None = None
    download_status: int | None = None
    body: bytes = b""
    timed_out = False
    try:
        async with asyncio.timeout(deadline_s):
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                # ``read=None``: the stream can be quiet while the model works
                # (SSE heartbeats bound this at ~15s, but don't depend on it);
                # the overall wall clock is the ``asyncio.timeout`` above.
                timeout=httpx.Timeout(30.0, read=None),
            ) as client:
                lines: list[str] = []
                async with client.stream(
                    "POST",
                    f"/v1/agents/{agent_code}/runs",
                    json={"user_id": user_id, "input": CANARY_PROMPT, "mode": "stream"},
                ) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread())[:300].decode("utf-8", errors="replace")
                        print(f"FAIL run request: HTTP {resp.status_code} {detail}")
                        return 1
                    async for line in resp.aiter_lines():
                        lines.append(line)
                end_payload = parse_end_frame(lines)

                entry = None
                if end_payload is not None:
                    arts = end_payload.get("artifacts")
                    if isinstance(arts, list):
                        entry = pick_artifact(arts)
                if entry is not None:
                    dl = await client.get(
                        f"/v1/agents/{agent_code}/artifacts/download",
                        params=download_params(entry, user_id=user_id),
                    )
                    download_status = dl.status_code
                    body = dl.content
    except TimeoutError:
        timed_out = True
    except httpx.HTTPError as exc:
        # Transport-level failure — the message never carries the key.
        print(f"FAIL transport: {type(exc).__name__}")
        return 1

    elapsed = time.monotonic() - started
    run_id = end_payload.get("run_id") if end_payload else None
    status = end_payload.get("status") if end_payload else None
    artifacts = (end_payload.get("artifacts") if end_payload else None) or []
    names = [a.get("name") for a in artifacts if isinstance(a, dict)]
    print(f"end frame: run_id={run_id} status={status} artifacts={names} ({elapsed:.1f}s)")

    checks = {
        f"end frame arrived ≤ {deadline_s:.0f}s": end_payload is not None and not timed_out,
        "end.status == success": status == "success",
        "end.artifacts non-empty": len(names) > 0,
        "artifact download HTTP 200": download_status == 200,
        f"artifact contains {expected_line()!r}": content_ok(body),
    }
    print("\n--- release canary verdict ---")
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    all_ok = all(checks.values())
    print(
        "\nRESULT:",
        "PASS — real run + artifact chain verified."
        if all_ok
        else "FAIL — the release is NOT good.",
    )
    return 0 if all_ok else 1


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
