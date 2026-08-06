"""Live verification for per-agent sandbox egress (sandbox-egress §3).

Closes the "code green ≠ it actually reaches the internet" gap for the egress
proxy (PRs #745/#746/#747/#748). Against a running dev stack it:

* **Phase 1 — egress allowed + audited.** Drives a real agent whose sandbox is
  egress-enabled to ``urllib.request.urlopen("https://1.1.1.1")`` (a public IP
  literal — DNS-independent, so a local fake-ip/WARP resolver can't mask it) from
  inside the sandbox (tunnelled through the audited egress proxy). Asserts the
  request succeeds AND a ``sandbox_egress_audit`` row with ``verdict=allowed``
  for that host shows up via ``GET /v1/sandbox-egress-audit``.

* **Phase 2 — SSRF blocked + audited (negative control).** Same agent tries
  ``https://169.254.169.254/`` (the cloud metadata IP). Asserts the request
  FAILS (the proxy refuses it) AND a ``verdict=blocked_ssrf`` row is recorded.

This is the half CI can't do (no Docker stack / model / real network in CI).
``exec_python`` is approval-gated, so the script approves each paused run via
the resume endpoint (the SSE run stream carries no approval event — it ends, and
the run is polled by id to find the pause).

Keyless: the model key is resolved server-side; this script only sends prompts.
The API token (a **system_admin** dev-login bearer) is read from
``EXPERT_WORK_API_TOKEN`` and never logged.

Prereqs — bring the dev stack up with the egress code built::

    cd infra
    # rebuild credential-proxy (egress listener), sandbox-supervisor (token
    # inject), control-plane (audit endpoint):
    docker compose --profile full --profile auth build \
        credential-proxy sandbox-supervisor control-plane-blue
    make dev-up

Then::

    export EXPERT_WORK_API_URL=http://localhost:8000   # control-plane (8080 is Keycloak)
    export EXPERT_WORK_API_TOKEN=<system_admin dev bearer token>
    # EDIT manifests/egress-test/v1.0.0.yaml model block to your provider first.
    uv run python tools/eval/verify_live_egress.py

Exit code is non-zero when any phase fails — so it can gate a manual release
check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set — export it before running (see module docstring)")
    return value


def _unwrap(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the ``{success, data, error}`` envelope (sessions/agents routes)."""
    if data.get("success") is False:
        err = data.get("error") or {}
        raise SystemExit(f"API error: {err.get('code')}: {err.get('message')}")
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def _iter_messages(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        msgs = obj.get("messages")
        if isinstance(msgs, list):
            found.extend(m for m in msgs if isinstance(m, dict))
        for v in obj.values():
            found.extend(_iter_messages(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_iter_messages(v))
    return found


def _message_type(msg: dict[str, Any]) -> str:
    return str(msg.get("type") or msg.get("role") or "")


def _content_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


# ── agent + run plumbing (shared shape with verify_live_skill_runtime) ────────


async def register_agent(
    client: httpx.AsyncClient, *, manifest_path: str
) -> tuple[str, str] | None:
    """Register the agent from a manifest YAML; return ``(name, version)``.
    Idempotent: a 409 (already registered) is treated as success."""
    import yaml

    text = Path(manifest_path).read_text()
    meta = (yaml.safe_load(text) or {}).get("metadata", {})
    name, version = meta.get("name"), str(meta.get("version"))
    print(f"[setup] register agent {name}@{version} from {manifest_path}")
    resp = await client.post("/v1/agents", json={"manifest_yaml": text})
    if resp.status_code == 409:
        print("  already registered — reusing")
        return name, version
    if resp.status_code not in (200, 201):
        print(f"  FAIL — register HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    print("  registered")
    return name, version


async def _create_session(client: httpx.AsyncClient, name: str, version: str) -> str:
    resp = await client.post("/v1/sessions", json={"agent_name": name, "agent_version": version})
    resp.raise_for_status()
    return str(_unwrap(resp.json())["thread_id"])


class _RunTrace:
    def __init__(self) -> None:
        self.tool_msgs: list[tuple[str, str]] = []
        self.assistant_text: str = ""
        self.errors: list[str] = []
        self.events: dict[str, int] = {}


async def _consume_sse(resp: httpx.Response, tr: _RunTrace) -> None:
    event = ""
    async for line in resp.aiter_lines():
        if line.startswith("event: "):
            event = line[len("event: ") :]
            tr.events[event] = tr.events.get(event, 0) + 1
        elif line.startswith("data: "):
            raw = line[len("data: ") :]
            if event in ("error", "run_error"):
                tr.errors.append(raw[:300])
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for msg in _iter_messages(payload):
                mtype = _message_type(msg)
                if mtype in ("tool", "ToolMessage"):
                    tr.tool_msgs.append((str(msg.get("name") or "?"), _content_text(msg)))
                elif mtype in ("ai", "AIMessage", "AIMessageChunk", "assistant"):
                    text = _content_text(msg)
                    if text.strip():
                        tr.assistant_text = text


async def _run_collect(
    client: httpx.AsyncClient, thread_id: str, prompt: str
) -> tuple[str | None, _RunTrace]:
    tr = _RunTrace()
    async with client.stream(
        "POST", f"/v1/sessions/{thread_id}/runs", json={"input": prompt}
    ) as resp:
        resp.raise_for_status()
        run_id = resp.headers.get("X-Expert-Work-Run-Id")
        await _consume_sse(resp, tr)
    return run_id, tr


async def _wait_paused(
    client: httpx.AsyncClient, thread_id: str, run_id: str, *, attempts: int = 40
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for _ in range(attempts):
        resp = await client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
        if resp.status_code == 200:
            detail = resp.json()
            if detail.get("status") in ("paused", "completed", "succeeded", "failed", "cancelled"):
                return detail
        await asyncio.sleep(2)
    return detail


async def _resume_collect(client: httpx.AsyncClient, thread_id: str, run_id: str) -> _RunTrace:
    tr = _RunTrace()
    async with client.stream(
        "POST",
        f"/v1/sessions/{thread_id}/runs/{run_id}/resume",
        json={"decision": "approve"},
    ) as resp:
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            await resp.aread()
            tr.errors.append("resume returned JSON (idempotent replay?) instead of a stream")
            return tr
        await _consume_sse(resp, tr)
    return tr


async def _gated_exec(
    client: httpx.AsyncClient, *, name: str, version: str, prompt: str, label: str
) -> _RunTrace | None:
    """Run a prompt that triggers approval-gated exec_python, approve it, and
    return the continuation trace. ``None`` if it never paused (misconfig)."""
    thread_id = await _create_session(client, name, version)
    run_id, _tr = await _run_collect(client, thread_id, prompt)
    if not run_id:
        print(f"  FAIL [{label}] — no X-Expert-Work-Run-Id header (cannot track approval).")
        return None
    detail = await _wait_paused(client, thread_id, run_id)
    if detail.get("status") != "paused" or not detail.get("pending_approval"):
        status = detail.get("status")
        print(f"  FAIL [{label}] — run did not pause for approval (status={status!r}).")
        print("  NOTE: exec_python must be in policies.approval_required_tools.")
        return None
    return await _resume_collect(client, thread_id, run_id)


# ── audit read-back ───────────────────────────────────────────────────────────


async def _find_audit_row(
    client: httpx.AsyncClient, *, host: str, verdict: str, attempts: int = 12
) -> dict[str, Any] | None:
    """Poll GET /v1/sandbox-egress-audit for a row matching host + verdict.
    The proxy writes the row best-effort after the connection, so we poll."""
    for _ in range(attempts):
        resp = await client.get(
            "/v1/sandbox-egress-audit",
            params={"target_host": host, "verdict": verdict, "limit": 20},
        )
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if items:
                return items[0]
        await asyncio.sleep(1)
    return None


# ── phases ────────────────────────────────────────────────────────────────────

# A public IP *literal*, not a hostname: the probe must not depend on whatever
# the proxy host's resolver returns. Some local setups (Cloudflare WARP, a
# Clash/mihomo "fake-ip" DNS, corporate split-DNS) map every public hostname to
# a synthetic reserved range (e.g. 198.18.0.0/15) — which the SSRF guard then
# *correctly* refuses, masking the allowed path. 1.1.1.1 (Cloudflare) serves a
# TLS cert with the IP in its SANs, so HTTPS verifies, and the egress proxy still
# resolves+pins it (a literal resolves to itself) and connects out via NAT.
_ALLOWED_HOST = "1.1.1.1"
# Two deliberate tricks here, both forced by live findings:
#
# 1. The target is base64-encoded. The probe is run by an LLM via exec_python;
#    given a bare ``https://1.1.1.1`` the model relays it but the IP literal is
#    what we need on the wire (no DNS → a local fake-ip resolver can't remap it
#    into a reserved range the SSRF guard then refuses). Opaque base64 keeps the
#    model from touching it. ``...L2Nkbi1jZ2kvdHJhY2U=`` == the URL below.
# 2. ``https://1.1.1.1`` 301-redirects to ``https://one.one.one.one`` — and
#    following that redirect goes back through DNS (fake-ip → blocked). So we hit
#    ``/cdn-cgi/trace`` (a 200, no redirect) AND disable redirect-following: any
#    HTTP response received over the tunnel proves egress reached Cloudflare,
#    redirect or not. The CONNECT target stays the literal ``1.1.1.1``.
_ALLOWED_CODE = """\
import base64, urllib.request, urllib.error
url = base64.b64decode("aHR0cHM6Ly8xLjEuMS4xL2Nkbi1jZ2kvdHJhY2U=").decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(_NoRedirect)
try:
    req = urllib.request.Request(url, headers={"User-Agent": "expert-work-egress-verify"})
    with opener.open(req, timeout=25) as r:
        body = r.read(200)
        print("EGRESS_RESULT", {"ok": True, "status": r.status, "bytes": len(body)})
except urllib.error.HTTPError as e:
    # A 3xx we refused to follow still means the CONNECT + TLS + HTTP round-trip
    # reached Cloudflare over the audited tunnel — egress worked.
    print("EGRESS_RESULT", {"ok": True, "status": e.code, "note": "non-2xx but reached"})
except Exception as e:
    print("EGRESS_RESULT", {"ok": False, "err": type(e).__name__ + ": " + str(e)[:200]})
"""

#: Plain-HTTP — PR-C #4: stdlib by default doesn't send Proxy-Authorization
#: for plain-HTTP proxied requests (only for CONNECT tunnels). Before the fix,
#: this always got 407. After sitecustomize patches ProxyHandler.proxy_open too.
_PLAIN_HTTP_CODE = _ALLOWED_CODE.replace(
    "aHR0cHM6Ly8xLjEuMS4xL2Nkbi1jZ2kvdHJhY2U=",
    "aHR0cDovLzEuMS4xLjEvY2RuLWNnaS90cmFjZQ==",
)


_SSRF_HOST = "169.254.169.254"
# base64 for the same reason as the allowed probe — keep the model from rewriting
# the literal. ``aHR0cHM6Ly8xNjkuMjU0LjE2OS4yNTQv`` == "https://169.254.169.254/".
_SSRF_CODE = """\
import base64, urllib.request
url = base64.b64decode("aHR0cHM6Ly8xNjkuMjU0LjE2OS4yNTQv").decode()
try:
    with urllib.request.urlopen(url, timeout=10) as r:
        print("SSRF_RESULT", {"reached": True, "status": r.status})
except Exception as e:
    print("SSRF_RESULT", {"reached": False, "err": type(e).__name__ + ": " + str(e)[:200]})
"""


def _exec_prompt(code: str) -> str:
    return (
        "Use the exec_python tool to run EXACTLY this Python code, verbatim "
        "(do not modify it), then report the line it prints:\n"
        f"```python\n{code}```"
    )


def _strip_ws(s: str) -> str:
    """Drop all whitespace and the spotlight space marker (U+2581).

    Tool output is wrapped in the spotlight/UNTRUSTED fence (PI defense), which
    rewrites spaces to ``▁``. A naive ``"'ok': True" in text`` then misses the
    real ``'ok':▁True``. Comparing with all spacing removed dodges the encoding
    without the verifier needing to know the fence format."""
    return "".join(ch for ch in s if not ch.isspace() and ch != "▁")


def _tool_text_has(tr: _RunTrace, *needles: str) -> bool:
    return any(all(_strip_ws(n) in _strip_ws(text) for n in needles) for _t, text in tr.tool_msgs)


async def phase_allowed(client: httpx.AsyncClient, *, name: str, version: str) -> bool:
    print(f"\n[phase 1] egress allowed — sandbox → https://{_ALLOWED_HOST} via the audited proxy")
    tr = await _gated_exec(
        client, name=name, version=version, prompt=_exec_prompt(_ALLOWED_CODE), label="allowed"
    )
    if tr is None:
        return False
    if not _tool_text_has(tr, "EGRESS_RESULT", "'ok': True"):
        print("  FAIL — the outbound request did not succeed from the sandbox.")
        for tool, text in tr.tool_msgs:
            print(f"    [{tool}] {text[:240].replace(chr(10), ' ')}")
        return False
    print("  request succeeded; checking the audit trail…")
    row = await _find_audit_row(client, host=_ALLOWED_HOST, verdict="allowed")
    if row is None:
        print("  FAIL — no sandbox_egress_audit row (verdict=allowed) for the host.")
        return False
    print(
        f"  PASS — egress reached {_ALLOWED_HOST}; audit row id={row.get('id')} "
        f"bytes_down={row.get('bytes_down')} agent={row.get('agent_name')}."
    )
    return True


async def phase_plain_http(client: httpx.AsyncClient, *, name: str, version: str) -> bool:
    print(f"\n[phase 3] plain-HTTP egress — sandbox → http://{_ALLOWED_HOST} via the audited proxy")
    tr = await _gated_exec(
        client,
        name=name,
        version=version,
        prompt=_exec_prompt(_PLAIN_HTTP_CODE),
        label="plain-http",
    )
    if tr is None:
        return False
    if not _tool_text_has(tr, "EGRESS_RESULT", "'ok': True"):
        print(
            "  FAIL — the plain-HTTP request did not succeed from the sandbox "
            "(PR-C 前的已知形态:stdlib 不发 Proxy-Authorization → 407)."
        )
        return False
    print("  request succeeded; checking the audit trail…")
    row = await _find_audit_row(client, host=_ALLOWED_HOST, verdict="allowed")
    if row is None:
        print("  FAIL — no sandbox_egress_audit row (verdict=allowed) for the host.")
        return False
    print(
        f"  PASS — egress reached {_ALLOWED_HOST}; audit row id={row.get('id')} "
        f"bytes_down={row.get('bytes_down')} agent={row.get('agent_name')}."
    )
    return True


async def phase_ssrf(client: httpx.AsyncClient, *, name: str, version: str) -> bool:
    print(f"\n[phase 2] SSRF blocked — sandbox → https://{_SSRF_HOST} (metadata IP) refused")
    tr = await _gated_exec(
        client, name=name, version=version, prompt=_exec_prompt(_SSRF_CODE), label="ssrf"
    )
    if tr is None:
        return False
    if not _tool_text_has(tr, "SSRF_RESULT", "'reached': False"):
        print("  FAIL — the metadata IP was NOT blocked (SSRF control regressed!).")
        for tool, text in tr.tool_msgs:
            print(f"    [{tool}] {text[:240].replace(chr(10), ' ')}")
        return False
    print("  request was refused; checking the audit trail…")
    row = await _find_audit_row(client, host=_SSRF_HOST, verdict="blocked_ssrf")
    if row is None:
        print("  FAIL — no sandbox_egress_audit row (verdict=blocked_ssrf) for the metadata IP.")
        return False
    print(f"  PASS — metadata IP refused + audited (blocked_ssrf, id={row.get('id')}).")
    return True


_DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[2] / "manifests" / "egress-test" / "v1.0.0.yaml"
)


async def _amain(args: argparse.Namespace) -> int:
    base_url = args.base_url or _require_env("EXPERT_WORK_API_URL")
    token = _require_env("EXPERT_WORK_API_TOKEN")  # never logged
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=180.0) as client:
        registered = await register_agent(client, manifest_path=args.manifest)
        if registered is None:
            print("\nRESULT: FAIL — could not register the egress test agent.")
            return 1
        name, version = registered

        ok = await phase_allowed(client, name=name, version=version)
        if not args.allowed_only:
            ok = await phase_plain_http(client, name=name, version=version) and ok
            ok = await phase_ssrf(client, name=name, version=version) and ok

    print(f"\nRESULT: {'PASS — egress + audit verified live.' if ok else 'FAIL — see above.'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live per-agent egress + audit verification.")
    parser.add_argument(
        "--base-url", default=None, help="control-plane URL (or $EXPERT_WORK_API_URL)"
    )
    parser.add_argument(
        "--manifest",
        default=str(_DEFAULT_MANIFEST),
        help="agent manifest YAML (default: manifests/egress-test/v1.0.0.yaml)",
    )
    parser.add_argument(
        "--allowed-only",
        action="store_true",
        help="run only phase 1 (skip the SSRF negative control)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
