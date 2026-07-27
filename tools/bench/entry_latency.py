"""入口链延迟取数脚本 —— 一期 Task 4。

跑 N 轮固定 prompt,每轮从 trace facade 拉 span 树,按 ``group == "entry"``
的 span 加首个 llm_call 聚合出各段耗时,输出 median / p95。基线 YAML
每轮跑完就增量落盘一次(不是只在全部跑完后写一次)——跑 N 轮是真实 LLM
调用,有成本,某一轮的网络故障不应该让之前几轮的数据白跑一场。

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
import hashlib
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

#: Internal-only key carrying each run's "time until the first LLM call
#: starts" through the same ``aggregate()`` pass as the real span segments —
#: popped back out into the YAML's top-level ``first_llm_start`` section
#: before writing. Can never collide with a real span label (those are all
#: Chinese entry-chain names from trace_facade.py's ``_SPAN_LABELS``).
#:
#: Deliberately NOT named ``first_output`` — Task 3 already owns that name
#: for a different, non-overlapping measurement (the
#: ``expert_work_first_output_seconds`` Prometheus histogram: first token
#: frame / first ``agent`` updates chunk). This proxy measures the earliest
#: LLM *span start*, which sits a model prefill earlier than a real first
#: token. Same name, different clock — see ``tools/bench/README.md``.
FIRST_LLM_START_KEY = "__first_llm_start_ms__"


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
    * ``FIRST_LLM_START_KEY`` — the earliest ``startMs`` among **all**
      ``kind == "llm"`` spans, as a proxy for "entry chain finished,
      generation started". This is the earliest LLM call of *any* purpose —
      an agent with an auxiliary LLM call inside the entry chain itself
      (e.g. a query-rewrite step inside ``memory.recall``) will report that
      call's start, not the main generation call's. See
      ``tools/bench/README.md`` for why that's fine for before/after
      comparisons but not for cross-agent ones. This script only reads the
      trace facade (not the ``first_output_seconds`` Prometheus histogram
      Task 3 added), so it is **not** that metric either way — it's an
      earlier point on the clock (a model prefill separates the two).

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
        metrics[FIRST_LLM_START_KEY] = min(llm_starts)
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


def _prompt_fingerprint(prompt: str) -> str:
    """Short content hash for the baseline's ``meta`` — same shape as
    ``_git_commit()``'s short sha. Catches "same ``--prompt-file`` path, but
    someone edited its content between the before/after run" as well as a
    different path entirely — both invalidate a before/after comparison,
    and neither is visible from the path string alone.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


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
        # httpx.Headers.get()'s stub returns Any — the `if not run_id: raise`
        # below narrows at runtime but not for mypy, so annotate explicitly
        # (mypy strict: "Returning Any from function declared to return str").
        run_id: str | None = resp.headers.get("X-Expert-Work-Run-Id")
        async for _line in resp.aiter_lines():
            pass  # drain to completion; content itself isn't needed here
    if not run_id:
        raise RuntimeError("run response carried no X-Expert-Work-Run-Id header")
    # A run whose graph CRASHED still streams to a clean SSE close and still
    # leaves a trace behind — HTTP-level success is NOT run success. Without
    # this check a failed run's half-built trace (e.g. an entry chain that
    # died at the LLM call) is aggregated into the baseline as if it were a
    # real measurement — a silently poisoned median. Ask the authoritative
    # run record instead of scanning SSE frames for error events.
    status_resp = await client.get(f"/v1/sessions/{thread_id}/runs/{run_id}")
    status_resp.raise_for_status()
    payload = status_resp.json()
    run_status = str((payload.get("data") or payload).get("status", "")).lower()
    # RunStatus's sole terminal-success value is "success" (runs/schemas.py).
    if run_status != "success":
        raise RuntimeError(f"run {run_id} finished with status {run_status!r}")
    return run_id


async def _fetch_trace(
    client: httpx.AsyncClient, thread_id: str, run_id: str, *, timeout_s: float
) -> dict[str, Any]:
    """GET the run's normalized trace, retrying while Langfuse ingestion is
    still async-flushing (``status: "not_ready"`` — see ``fetch_and_normalize``'s
    docstring in ``control_plane/api/trace_facade.py``).

    ``resp.json()`` raises ``json.JSONDecodeError`` (a ``ValueError``
    subclass) when a 2xx response body isn't valid JSON (e.g. a gateway
    mangled it) — deliberately NOT caught here; the caller (``run_rounds``)
    catches it per-round so one bad response doesn't take earlier rounds'
    real data down with it.
    """
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


async def run_rounds(
    client: httpx.AsyncClient,
    thread_id: str,
    prompt: str,
    runs: int,
    *,
    trace_timeout_s: float,
    on_round_done: Callable[[list[dict[str, float]], int], None] | None = None,
) -> tuple[list[dict[str, float]], int]:
    """Run ``runs`` rounds against an already-created session.

    Returns ``(per_run_metrics, failed_runs)``. A single round's failure —
    an HTTP error, a run response missing its run id, trace ingestion never
    settling, **or a 2xx trace response whose body isn't valid JSON**
    (``resp.json()`` raises ``json.JSONDecodeError``, a ``ValueError``
    subclass — this used to escape the catch here and crash the whole batch,
    losing every earlier round's already-collected real data; code review
    caught it) — is recorded as an empty ``{}`` entry plus a failed-round
    increment, not a batch-aborting exception.

    ``on_round_done`` (if given) fires after every round, success or
    failure, with the metrics collected so far and the running failure
    count — the caller uses this to persist partial results to disk after
    each round instead of only once at the very end, so a crash mid-batch
    (from something this function doesn't catch) doesn't erase real,
    already-paid-for LLM calls.
    """
    per_run: list[dict[str, float]] = []
    failed_runs = 0
    for i in range(runs):
        print(f"run {i + 1}/{runs} ...", file=sys.stderr)
        try:
            run_id = await _run_once(client, thread_id, prompt)
            trace = await _fetch_trace(client, thread_id, run_id, timeout_s=trace_timeout_s)
            per_run.append(extract_run_metrics(trace))
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            print(f"  run {i + 1} failed, skipping: {exc}", file=sys.stderr)
            per_run.append({})
            failed_runs += 1
        if on_round_done is not None:
            on_round_done(per_run, failed_runs)
    return per_run, failed_runs


def _write_result(
    out_path: Path,
    per_run: list[dict[str, float]],
    meta_base: dict[str, Any],
    *,
    failed_runs: int,
) -> None:
    """Aggregate ``per_run`` and write the baseline YAML to ``out_path``.

    Called after every round (incremental persistence, see ``run_rounds``)
    as well as once more at the end — each call fully overwrites the file
    with the current cumulative state, so it's safe to call repeatedly.
    ``meta_base`` carries the fields that don't change per round (commit /
    host / agent / runs / prompt fingerprint / note); ``successful_runs``
    and ``failed_runs`` are computed fresh every call since they're the
    only thing that changes as rounds complete.
    """
    aggregated = aggregate(per_run)
    first_llm_start = aggregated.pop(FIRST_LLM_START_KEY, None)

    meta: dict[str, Any] = dict(meta_base)
    meta["successful_runs"] = len(per_run) - failed_runs
    meta["failed_runs"] = failed_runs

    result: dict[str, Any] = {
        "segments": {name: _segment_to_dict(seg) for name, seg in sorted(aggregated.items())},
        "meta": meta,
    }
    if first_llm_start is not None:
        result["first_llm_start"] = {
            "median": first_llm_start.median,
            "p95": first_llm_start.p95,
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(result, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _exit_status(successful_runs: int, total_runs: int) -> tuple[int, str | None]:
    """Decide the process exit code + an optional stdout warning from the
    final success/total tally.

    All rounds failing (e.g. a typo'd ``--agent`` version — every round
    404s, ``per_run`` is all empty dicts, ``aggregate()`` legitimately
    returns ``{}``) must NOT look like a clean run: the file would read
    identically to "this agent genuinely has no entry-chain spans" without
    ``meta.successful_runs`` — so this returns a non-zero exit code and a
    warning in that case. A low-but-nonzero success rate still exits 0
    (partial real data is still useful — the whole point of per-round fault
    tolerance) but is called out anyway so it isn't missed.
    """
    if total_runs <= 0:
        return 0, None
    if successful_runs == 0:
        return 1, f"all {total_runs} runs failed — no measurements were collected"
    if successful_runs / total_runs < 0.5:
        return (
            0,
            f"only {successful_runs}/{total_runs} runs succeeded — "
            "these numbers rest on a small sample",
        )
    return 0, None


async def _amain(args: argparse.Namespace) -> int:
    base_url = args.base_url or _require_env("EXPERT_WORK_API_URL")
    token = _require_env("EXPERT_WORK_API_TOKEN")  # never logged
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        raise SystemExit(f"{args.prompt_file} is empty")
    agent_name, sep, agent_version = args.agent.partition("@")
    if not sep:
        raise SystemExit("--agent must be name@version")

    meta_base: dict[str, Any] = {
        "commit": _git_commit(),
        "host": _host_fingerprint(),
        "agent": args.agent,
        "runs": args.runs,
        "prompt_file": args.prompt_file,
        "prompt_sha256": _prompt_fingerprint(prompt),
    }
    if args.note:
        meta_base["note"] = args.note

    out_path = Path(args.out)

    def _persist(per_run: list[dict[str, float]], failed_runs: int) -> None:
        _write_result(out_path, per_run, meta_base, failed_runs=failed_runs)

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=180.0) as client:
        thread_id = await _create_session(client, agent_name, agent_version)
        print(f"session: {thread_id}", file=sys.stderr)
        _per_run, failed_runs = await run_rounds(
            client,
            thread_id,
            prompt,
            args.runs,
            trace_timeout_s=args.trace_timeout_s,
            on_round_done=_persist,
        )

    successful_runs = args.runs - failed_runs
    print(f"wrote {out_path}")
    exit_code, warning = _exit_status(successful_runs, args.runs)
    if warning is not None:
        print(f"WARNING: {warning}")
    return exit_code


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
