"""入口链延迟取数脚本 —— 一期 Task 4。

跑 N 轮固定 prompt,每轮从 trace facade 拉 span 树,按 ``group == "entry"``
的 span 加首个 llm_call 聚合出各段耗时,输出 median / p95。

不是 benchmark 框架,是个取数脚本。二期量 P1.1/P1.2/P1.3/P3 复用它。

``tools/bench`` 不是包(见 conftest.py),所以按脚本路径跑,不是 ``-m``::

    export EXPERT_WORK_API_URL=http://localhost:8000
    export EXPERT_WORK_API_TOKEN=<a dev-login bearer token>
    uv run python tools/bench/entry_latency.py \\
        --agent my-agent@1.0.0 --prompt-file tools/bench/prompts/fixed.txt --runs 10 \\
        --out tools/bench/baselines/2026-07-27-before.yaml

Needs a running control-plane (+ its full stack — the agent actually
executes) and a bearer token for a tenant that has ``my-agent@1.0.0``
registered and bound to real model credentials. See ``tools/eval/verify_live.py``
for the same env-var / auth convention this script follows.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

#: Internal-only key carrying each run's "time until the first LLM call
#: starts" through the same ``aggregate()`` pass as the real span segments —
#: popped back out into the YAML's top-level ``first_output`` section before
#: writing. Can never collide with a real span label (those are all
#: Chinese entry-chain names from trace_facade.py's ``_SPAN_LABELS``).
FIRST_OUTPUT_KEY = "__first_output_ms__"


@dataclass(frozen=True)
class Segment:
    """One entry-chain stage's aggregated latency across runs."""

    median: float
    p95: float
    n: int


def aggregate(runs: list[dict[str, float]]) -> dict[str, Segment]:
    """Fold per-run segment timings into median / p95 per segment.

    A segment absent from a run is **skipped**, not counted as zero — an
    optional stage (rerank only exists when a reranker is configured) would
    otherwise drag its own median toward zero and make two differently
    configured agents incomparable. ``Segment.n`` records how many runs
    actually had the stage.
    """
    names = {name for run in runs for name in run}
    out: dict[str, Segment] = {}
    for name in names:
        values = sorted(run[name] for run in runs if name in run)
        if not values:
            continue
        out[name] = Segment(
            median=statistics.median(values),
            p95=values[min(len(values) - 1, int(len(values) * 0.95))],
            n=len(values),
        )
    return out


def extract_run_metrics(trace: dict[str, Any]) -> dict[str, float]:
    """Pull one run's per-segment latencies out of a trace-facade response.

    ``trace`` is the JSON body of ``GET /v1/sessions/{tid}/runs/{rid}/trace``
    (``fetch_and_normalize`` in ``control_plane/api/trace_facade.py``) once its
    ``status`` is ``"ok"``. Two things come out of the span list:

    * **Segments** — every span with ``group == "entry"`` (the 8 入口链 spans,
      Task 1/2), keyed by its (already Chinese) ``label``.
    * ``FIRST_OUTPUT_KEY`` — the earliest ``startMs`` among ``kind == "llm"``
      spans, as a proxy for "entry chain finished, generation started". This
      script only reads the trace facade (not the ``first_output_seconds``
      Prometheus histogram Task 3 added), so it is an approximation, not the
      real first-token timestamp — flagged for live-stack verification.

    Malformed / missing fields degrade silently (span skipped) rather than
    raising — one bad span in a trace should not sink an entire run's data.
    """
    spans = trace.get("spans")
    if not isinstance(spans, list):
        return {}

    metrics: dict[str, float] = {}
    llm_starts: list[float] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        if span.get("group") == "entry":
            label = span.get("label")
            latency_ms = span.get("latencyMs")
            if isinstance(label, str) and isinstance(latency_ms, int | float):
                metrics[label] = float(latency_ms)
        if span.get("kind") == "llm":
            start_ms = span.get("startMs")
            if isinstance(start_ms, int | float):
                llm_starts.append(float(start_ms))

    if llm_starts:
        metrics[FIRST_OUTPUT_KEY] = min(llm_starts)
    return metrics


def _segment_to_dict(segment: Segment) -> dict[str, float | int]:
    return {"median": segment.median, "p95": segment.p95, "n": segment.n}


def _git_commit() -> str:
    """Short commit sha for the baseline's ``meta`` — same pattern as
    ``tools/eval/run_longmem.py``'s ``_git_commit``."""
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell, dev tool
            [git, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def _host_fingerprint() -> str:
    """Machine identity for the baseline's ``meta`` — ``platform``-derived,
    e.g. ``darwin-arm64``. Use ``--note`` for context a machine can't infer
    (e.g. "local dev compose")."""
    return f"{platform.system().lower()}-{platform.machine()}"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set — export it before running (see module docstring)")
    return value


def _unwrap(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the ``{success, data, error}`` envelope used by ``POST /v1/sessions``
    (``tools/eval/verify_live.py``'s ``_unwrap``, same convention)."""
    if data.get("success") is False:
        err = data.get("error") or {}
        raise SystemExit(f"API error: {err.get('code')}: {err.get('message')}")
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


async def _create_session(client: httpx.AsyncClient, name: str, version: str) -> str:
    resp = await client.post("/v1/sessions", json={"agent_name": name, "agent_version": version})
    resp.raise_for_status()
    return str(_unwrap(resp.json())["thread_id"])


async def _run_once(client: httpx.AsyncClient, thread_id: str, prompt: str) -> str:
    """POST a run, drain its SSE stream to completion, return the run id.

    Mirrors ``tools/eval/verify_live.py``'s ``_run_once`` — this script
    doesn't need the reply content, only that the graph has finished, which
    the SSE stream closing signals. ``X-Expert-Work-Run-Id`` is set on the
    response headers before the body starts streaming (``runs.py`` sets it
    alongside the ``StreamingResponse``).
    """
    async with client.stream(
        "POST", f"/v1/sessions/{thread_id}/runs", json={"input": prompt}
    ) as resp:
        resp.raise_for_status()
        run_id = resp.headers.get("X-Expert-Work-Run-Id")
        async for _line in resp.aiter_lines():
            pass  # drain to completion; content itself isn't needed here
    if not run_id:
        raise RuntimeError("run response carried no X-Expert-Work-Run-Id header")
    return run_id


async def _fetch_trace(
    client: httpx.AsyncClient, thread_id: str, run_id: str, *, timeout_s: float
) -> dict[str, Any]:
    """GET the run's normalized trace, retrying while Langfuse ingestion is
    still async-flushing (``status: "not_ready"`` — see ``fetch_and_normalize``'s
    docstring in ``control_plane/api/trace_facade.py``)."""
    deadline = time.monotonic() + timeout_s
    while True:
        resp = await client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/trace")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        if data.get("status") == "ok":
            return data
        if time.monotonic() >= deadline:
            raise RuntimeError(f"trace never became ready: status={data.get('status')}")
        await asyncio.sleep(1.0)


async def _amain(args: argparse.Namespace) -> int:
    base_url = args.base_url or _require_env("EXPERT_WORK_API_URL")
    token = _require_env("EXPERT_WORK_API_TOKEN")  # never logged
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        raise SystemExit(f"{args.prompt_file} is empty")
    agent_name, sep, agent_version = args.agent.partition("@")
    if not sep:
        raise SystemExit("--agent must be name@version")

    headers = {"Authorization": f"Bearer {token}"}
    per_run: list[dict[str, float]] = []
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=180.0) as client:
        thread_id = await _create_session(client, agent_name, agent_version)
        print(f"session: {thread_id}", file=sys.stderr)
        for i in range(args.runs):
            print(f"run {i + 1}/{args.runs} ...", file=sys.stderr)
            try:
                run_id = await _run_once(client, thread_id, prompt)
                trace = await _fetch_trace(
                    client, thread_id, run_id, timeout_s=args.trace_timeout_s
                )
                per_run.append(extract_run_metrics(trace))
            except (httpx.HTTPError, RuntimeError) as exc:
                print(f"  run {i + 1} failed, skipping: {exc}", file=sys.stderr)
                per_run.append({})

    aggregated = aggregate(per_run)
    first_output = aggregated.pop(FIRST_OUTPUT_KEY, None)

    meta: dict[str, Any] = {
        "commit": _git_commit(),
        "host": _host_fingerprint(),
        "agent": args.agent,
        "runs": args.runs,
    }
    if args.note:
        meta["note"] = args.note

    result: dict[str, Any] = {
        "segments": {name: _segment_to_dict(seg) for name, seg in sorted(aggregated.items())},
        "meta": meta,
    }
    if first_output is not None:
        result["first_output"] = {"median": first_output.median, "p95": first_output.p95}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(result, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(f"wrote {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Entry-chain latency bench script (Task 4).")
    parser.add_argument(
        "--base-url", default=None, help="control-plane URL (or $EXPERT_WORK_API_URL)"
    )
    parser.add_argument("--agent", required=True, help="target agent as name@version")
    parser.add_argument(
        "--prompt-file", required=True, help="path to the fixed prompt text for every round"
    )
    parser.add_argument("--runs", type=int, default=10, help="number of rounds (default 10)")
    parser.add_argument("--out", required=True, help="output baseline YAML path")
    parser.add_argument(
        "--trace-timeout-s",
        type=float,
        default=30.0,
        help="max seconds to wait per run for Langfuse trace ingestion (default 30)",
    )
    parser.add_argument("--note", default=None, help="free-text note stored under meta.note")
    args = parser.parse_args(argv)
    if args.runs <= 0:
        raise SystemExit("--runs must be positive")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
