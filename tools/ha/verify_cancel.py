"""Live cross-replica cancel verification — 多副本 CAS 守卫(洞 A/B 修复)的执行面。

Proves the cross-replica cancel chain end to end against a **real** two-instance
dev stack (blue ``:8000`` + green, sharing one Postgres). CI can't: the chain's
last link — the owner's heartbeat CAS failing and setting ``abort_event`` — only
exists between two live processes.

What it does:

1. Start a long-running agent run on **blue** (stream mode, kept open so the
   run keeps executing); wait until it is durably ``running`` and blue owns
   the lease.
2. Send the cancel from **green** — the non-owner replica. Green's
   ``RunManager.cancel`` misses its in-process registry and falls through to
   ``RunStore.request_cancel`` (guarded CAS → ``interrupted``).
3. Assert, in order:
   - the durable row flips to ``interrupted`` with ``error='user_cancel'``
     immediately (green's CAS won);
   - **blue actually stops**: its SSE stream delivers an ``end`` frame with
     ``interrupted`` within the heartbeat-detection bound
     (``lease_ttl/3`` ≈ 10s + margin) — the ``_heartbeat_loop`` →
     ``abort_event`` link, previously untested anywhere;
   - blue's lease heartbeat stops advancing (the owner loop exited);
   - the run is never resurrected (row still ``interrupted`` after a grace
     re-check — the set_status CAS guard holding in production shape).

Usage (bring the two-colour dev stack up first)::

    export EXPERT_WORK_API_TOKEN=<a dev-login bearer token>
    uv run python tools/ha/verify_cancel.py
    uv run python tools/ha/verify_cancel.py --agent my-agent@1.0.0

Exit code is non-zero when any link of the chain failed — a manual release
gate, same shape as ``verify_failover.py`` / ``verify_queue.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from typing import Any

import httpx

#: A prompt long enough that the run is still executing when green cancels it.
_LONG_PROMPT = (
    "Think step by step and write a thorough, detailed technical explanation "
    "(at least 1500 words) of how modern container orchestrators schedule "
    "workloads: bin-packing, affinity, preemption, autoscaling. Be exhaustive."
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set — export it before running (see module docstring)")
    return value


def _unwrap(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("success") is False:
        err = data.get("error") or {}
        raise SystemExit(f"API error: {err.get('code')}: {err.get('message')}")
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def _psql(pg_container: str, sql: str) -> str:
    db_user = os.environ.get("EXPERT_WORK_DB_USER", "expert_work")
    db_name = os.environ.get("EXPERT_WORK_DB_NAME", "expert_work_dev")
    proc = subprocess.run(
        ["docker", "exec", pg_container, "psql", "-U", db_user, "-d", db_name, "-tAc", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


async def _pick_agent(client: httpx.AsyncClient, override: str | None) -> tuple[str, str]:
    resp = await client.get("/v1/agents", params={"status": "active", "limit": 200})
    resp.raise_for_status()
    items = _unwrap(resp.json()).get("items", [])
    if not items:
        raise SystemExit("no active agents found on this stack")
    if override is not None:
        name, _, version = override.partition("@")
        for rec in items:
            if rec.get("name") == name and (not version or rec.get("version") == version):
                return rec["name"], rec["version"]
        raise SystemExit(f"agent {override!r} not found among active agents")
    rec = items[0]
    return rec["name"], rec["version"]


async def _create_session(client: httpx.AsyncClient, name: str, version: str) -> str:
    resp = await client.post("/v1/sessions", json={"agent_name": name, "agent_version": version})
    resp.raise_for_status()
    return str(_unwrap(resp.json())["thread_id"])


async def _start_run_capture(base_url: str, token: str, thread_id: str, lines: list[str]) -> None:
    """Open the run SSE stream on blue and append every line to ``lines``.

    The stream must stay open for the whole test (stream mode +
    ``on_disconnect=cancel`` means closing it would cancel the run ourselves
    and invalidate the cross-replica proof). It ends naturally when blue
    aborts and publishes the terminal ``end`` frame.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with (
            httpx.AsyncClient(base_url=base_url, headers=headers, timeout=None) as client,
            client.stream(
                "POST", f"/v1/sessions/{thread_id}/runs", json={"input": _LONG_PROMPT}
            ) as resp,
        ):
            async for line in resp.aiter_lines():
                lines.append(line)
    except Exception:
        # Transport teardown after the end frame (or during shutdown) is fine —
        # the assertions read ``lines``, not this task's outcome.
        pass


def _run_row(pg_container: str, thread_id: str) -> dict[str, str] | None:
    out = _psql(
        pg_container,
        "SELECT id || '|' || status || '|' || coalesce(claimed_by, '') || '|' "
        "|| coalesce(error, '') || '|' || coalesce(heartbeat_at::text, '') "
        f"FROM agent_run WHERE thread_id = '{thread_id}' ORDER BY created_at DESC LIMIT 1",
    )
    if not out:
        return None
    run_id, status, claimed_by, error, heartbeat_at = out.split("|", 4)
    return {
        "run_id": run_id,
        "status": status,
        "claimed_by": claimed_by,
        "error": error,
        "heartbeat_at": heartbeat_at,
    }


async def _await_running(pg_container: str, thread_id: str, timeout_s: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _run_row(pg_container, thread_id)
        if row and row["status"] == "running" and row["claimed_by"] and row["heartbeat_at"]:
            return row
        await asyncio.sleep(0.5)
    raise SystemExit("timed out waiting for the run to reach running+claimed on blue")


def _cancel_from_green(
    green_container: str, token: str, thread_id: str, run_id: str
) -> dict[str, Any]:
    """POST the cancel endpoint from **inside green** (no host port in dev).

    Green is the non-owner: its ``RunManager.cancel`` misses and the request
    falls through to the ``request_cancel`` DB CAS — exactly the production
    cross-replica path. The token rides an env var into the container, never
    argv (``docker exec`` argv is visible in ``ps``).
    """
    code = (
        "import json, os, urllib.request\n"
        f"req = urllib.request.Request('http://127.0.0.1:8000/v1/sessions/{thread_id}"
        f"/runs/{run_id}:cancel', method='POST', data=b'{{}}')\n"
        "req.add_header('Authorization', 'Bearer ' + os.environ['VC_TOKEN'])\n"
        "req.add_header('Content-Type', 'application/json')\n"
        "resp = urllib.request.urlopen(req, timeout=10)\n"
        "print(json.dumps({'status': resp.status, 'body': resp.read().decode()[:500]}))\n"
    )
    proc = subprocess.run(
        ["docker", "exec", "-e", f"VC_TOKEN={token}", green_container, "python", "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, Any] = json.loads(proc.stdout.strip())
    return result


def _end_frame_status(lines: list[str]) -> str | None:
    """The ``end`` frame's ``status`` from captured SSE lines, if it arrived."""
    for i, line in enumerate(lines):
        if line.strip() == "event: end":
            for follow in lines[i + 1 : i + 4]:
                if follow.startswith("data: "):
                    try:
                        payload = json.loads(follow[len("data: ") :])
                    except json.JSONDecodeError:
                        return None
                    return str(payload.get("status"))
    return None


async def _amain(args: argparse.Namespace) -> int:
    token = _require_env("EXPERT_WORK_API_TOKEN")  # never logged
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=args.blue_url, headers=headers, timeout=30.0) as blue:
        name, version = await _pick_agent(blue, args.agent)
        print(f"agent: {name}@{version}")
        thread_id = await _create_session(blue, name, version)
        print(f"session: {thread_id}")

    lines: list[str] = []
    run_task = asyncio.create_task(_start_run_capture(args.blue_url, token, thread_id, lines))
    running = await _await_running(args.pg_container, thread_id, timeout_s=60.0)
    run_id = running["run_id"]
    blue_owner = running["claimed_by"]
    hb_before = running["heartbeat_at"]
    print(f"run {run_id} RUNNING on blue (owner={blue_owner[:24]}…)")

    print(f"cancelling from {args.green_container} (non-owner) …")
    t_cancel = time.monotonic()
    cancel_resp = _cancel_from_green(args.green_container, token, thread_id, run_id)
    print(f"green cancel response: HTTP {cancel_resp['status']}")

    # 1. 行立即翻 interrupted + user_cancel(green 的 request_cancel CAS 赢了)。
    row = _run_row(args.pg_container, thread_id)
    row_ok = bool(row and row["status"] == "interrupted" and row["error"] == "user_cancel")
    row_status = row["status"] if row else "?"
    row_error = row["error"] if row else "?"
    print(f"durable row after cancel: {row_status} error={row_error}")

    # 2. blue 真停:end 帧(interrupted)必须在心跳检测上界内到达。
    end_status: str | None = None
    stop_latency: float | None = None
    deadline = time.monotonic() + args.stop_timeout
    while time.monotonic() < deadline:
        end_status = _end_frame_status(lines)
        if end_status is not None:
            stop_latency = time.monotonic() - t_cancel
            break
        await asyncio.sleep(0.5)
    print(f"blue end frame: status={end_status} latency={stop_latency and round(stop_latency, 1)}s")

    # 3. 心跳停(属主循环退出)。
    await asyncio.sleep(args.heartbeat_grace)
    row_late = _run_row(args.pg_container, thread_id)
    hb_frozen = bool(
        row_late and row_late["heartbeat_at"] == (row["heartbeat_at"] if row else hb_before)
    )

    # 4. 不复活(set_status CAS 守卫)。
    not_resurrected = bool(row_late and row_late["status"] == "interrupted")
    run_task.cancel()

    checks = {
        "row interrupted + user_cancel (CAS won)": row_ok,
        f"blue stopped ≤ {args.stop_timeout:.0f}s (end frame interrupted)": end_status
        == "interrupted",
        "owner heartbeat frozen (loop exited)": hb_frozen,
        "run never resurrected (guarded set_status)": not_resurrected,
    }
    print("\n--- cross-replica cancel verdict ---")
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    all_ok = all(checks.values())
    print("\nRESULT:", "PASS — cross-replica cancel chain verified." if all_ok else "FAIL.")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live cross-replica cancel verification (多副本 CAS 守卫执行面)."
    )
    parser.add_argument(
        "--blue-url", default="http://localhost:8000", help="blue control-plane URL"
    )
    parser.add_argument("--agent", default=None, help="target agent as name@version (else auto)")
    parser.add_argument(
        "--green-container",
        default="expert-work-control-plane-green",
        help="green container (non-owner; cancel is POSTed from inside it)",
    )
    parser.add_argument("--pg-container", default="expert-work-postgres", help="Postgres container")
    parser.add_argument(
        "--stop-timeout",
        type=float,
        default=45.0,
        help="max seconds for blue to stop after the cancel (lease_ttl/3 + margin)",
    )
    parser.add_argument(
        "--heartbeat-grace",
        type=float,
        default=12.0,
        help="seconds to wait before asserting the owner heartbeat froze",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
