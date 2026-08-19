"""Langfuse trace normalizer — Batch 4b Task 1 (spec §2.3).

The debug console's "precise" trace view wants a stable, human-labelled
DTO instead of the raw Langfuse observation tree. ``normalize_trace`` is a
pure function — no IO, no network, no upstream dependency — so it can be
unit-tested against lightweight stubs and reused unchanged once the
control-plane wires an actual Langfuse client in a later task.

Normalization does three things to the raw ``observations`` list:

1. **Classify** each observation into a ``kind``/``label`` pair a human can
   read (GENERATION → "LLM 调用", ``*.tool_call`` → "工具调用", …).
2. **Merge** the internal ``*.orchestrator.llm_call`` SPAN wrapper into its
   single GENERATION child — that wrapper is an orchestrator implementation
   detail, not something a human debugging a run needs to see as a
   separate row.
3. **Elide** the ``*.http_request`` root — it is the FastAPI entry span,
   not part of the agent's own work — re-parenting its children so the
   remaining tree stays connected.

Real Langfuse SDK objects mix snake_case and camelCase attribute names
across SDK versions (``prompt_tokens`` vs ``promptTokens``); token count
extraction defends against both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langfuse.api import NotFoundError

from expert_work.common.observability import LLM_SPAN_PURPOSES

__all__ = ["TraceSpan", "fetch_and_normalize", "fetch_span_raw", "normalize_trace"]

_NAME_PREFIX = "expert_work."
_TRUNCATION_SUFFIX = "…(已截断)"
_MSG_CAP = 8192
_TEXT_CAP = 16384


@dataclass(frozen=True)
class TraceSpan:
    """One normalized row in the debug console's trace timeline."""

    id: str
    parent_id: str | None
    kind: str  # "session" | "llm" | "tool" | "span"
    label: str
    detail: str | None
    start_ms: int
    latency_ms: int
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    level: str
    status_message: str | None
    #: LLM-call intent for the console's visual marker: "" for non-LLM spans
    #: and unwrapped generations, else "main"/"memory"/"planner"/…
    purpose: str
    #: Which functional stage this span belongs to, for the console's colour
    #: grouping and the pre-first-token breakdown bar. "entry" = the entry
    #: chain (recall / ingest / context gates); None = everything else.
    #: Deliberately NOT folded into ``purpose``: that field is locked to
    #: ``LLM_SPAN_PURPOSES`` by a parity test and means "LLM call intent",
    #: so a non-LLM span carrying purpose="recall" would mislead.
    group: str | None = None


@dataclass(frozen=True)
class _ParsedObs:
    """Per-observation working state before merge/elision + re-parenting."""

    id: str
    parent_id: str | None
    obs_type: str
    name: str
    kind: str
    label: str
    detail: str | None
    start_ms: int
    latency_ms: int
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    level: str
    status_message: str | None
    purpose: str
    group: str | None


def normalize_trace(trace: object) -> dict[str, object]:
    """Normalize a Langfuse ``TraceWithFullDetails`` into a stable DTO.

    Returns ``{"status": "ok", "trace": {...}, "spans": [...]}`` where each
    span dict uses camelCase keys (``parentId``/``startMs``/``latencyMs``/
    ``inputTokens``/``outputTokens``/``costUsd``).
    """
    t: Any = trace
    raw_observations: list[Any] = list(t.observations or [])

    trace_name = str(t.name)
    # 兜底单位存疑(终审 Minor 3)——这版 Langfuse 实测回的是 ms,这里却按秒
    # x1000;只在 observations 为空 / 全无 end_time(下面的 `end-start` 覆盖
    # 不到)时才会真的走到这条兜底,今天没有已知触发场景。
    trace_latency_ms = round((t.latency or 0) * 1000)
    trace_total_cost_usd = t.total_cost or None

    if not raw_observations:
        return {
            "status": "ok",
            "trace": {
                "name": trace_name,
                "latencyMs": trace_latency_ms,
                "totalCostUsd": trace_total_cost_usd,
                "spanCount": 0,
            },
            "spans": [],
        }

    start_times = [o.start_time for o in raw_observations if o.start_time is not None]
    trace_start = min(start_times) if start_times else None

    end_times = [
        getattr(o, "end_time", None)
        for o in raw_observations
        if getattr(o, "end_time", None) is not None
    ]
    if trace_start is not None and end_times:
        trace_latency_ms = max(0, round((max(end_times) - trace_start).total_seconds() * 1000))

    parsed_by_id: dict[str, _ParsedObs] = {}
    children_by_parent: dict[str | None, list[str]] = {}
    for o in raw_observations:
        parsed = _parse_observation(o, trace_start=trace_start)
        parsed_by_id[parsed.id] = parsed
        children_by_parent.setdefault(parsed.parent_id, []).append(parsed.id)

    omitted, latency_override, label_override, purpose_override = _resolve_omissions(
        parsed_by_id, children_by_parent
    )

    def resolve_parent(raw_parent_id: str | None) -> str | None:
        current = raw_parent_id
        visited: set[str] = set()
        # T1-reviewer follow-up: real Langfuse data flows through here now —
        # guard against a corrupted upstream parent cycle wedging the event loop.
        while current is not None and current in omitted:
            if current in visited:
                return None
            visited.add(current)
            current = omitted[current]
        return current

    spans = [
        TraceSpan(
            id=parsed.id,
            parent_id=resolve_parent(parsed.parent_id),
            kind=parsed.kind,
            label=label_override.get(parsed.id, parsed.label),
            detail=parsed.detail,
            start_ms=parsed.start_ms,
            latency_ms=latency_override.get(parsed.id, parsed.latency_ms),
            model=parsed.model,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            cost_usd=parsed.cost_usd,
            input=parsed.input,
            output=parsed.output,
            level=parsed.level,
            status_message=parsed.status_message,
            purpose=purpose_override.get(parsed.id, parsed.purpose),
            group=parsed.group,
        )
        for parsed in sorted(parsed_by_id.values(), key=lambda p: (p.start_ms, p.id))
        if parsed.id not in omitted
    ]

    return {
        "status": "ok",
        "trace": {
            "name": trace_name,
            "latencyMs": trace_latency_ms,
            "totalCostUsd": trace_total_cost_usd,
            "spanCount": len(spans),
        },
        "spans": [_span_as_dict(s) for s in spans],
    }


def fetch_and_normalize(client: Any, trace_id: str) -> dict[str, object]:
    """Fetch one trace from Langfuse and normalize it — Batch 4b Task 2.

    Unlike :func:`normalize_trace` this DOES touch the network (via the
    injected read-only Langfuse SDK client) — the try/except below is a
    deliberate fail-soft degrade boundary, not a swallowed error: every
    branch returns an explicit ``status`` so the debug console can render
    "tracing off" vs "not ingested yet" vs a real trace, and a Langfuse
    outage never turns into a 500 for the caller.
    """
    if client is None:
        return {"status": "unavailable"}
    try:
        trace = client.api.trace.get(trace_id)
    except NotFoundError:
        # Genuinely unknown, or Langfuse's async ingestion pipeline just
        # hasn't landed it yet — distinct from tracing being disabled.
        return {"status": "not_ready"}
    except Exception:
        return {"status": "unavailable"}
    # Trace exists but Langfuse's aggregation hasn't finished yet (latency is
    # populated once the trace closes out) — tell the caller to retry rather
    # than rendering a bogus zero-latency row.
    if getattr(trace, "latency", None) is None:
        return {"status": "not_ready"}
    try:
        normalized = normalize_trace(trace)
    except Exception:
        # Belt-and-suspenders: no code path from a successful trace.get()
        # should ever reach an uncaught exception (硬约束「降级永不 500」).
        return {"status": "unavailable"}
    # Langfuse ingestion is NOT atomic under load: a multi-span run's child
    # observations can land in Langfuse BEFORE their session-root parent does
    # (the trace root closes → ``latency`` populated → passes the None-latency
    # guard above, yet the observation set is still partial). Two shapes hit
    # the waterfall as a bare time-axis with no bars:
    #   * zero renderable spans, or
    #   * spans present but NONE is a root / some reference a parent that
    #     hasn't been ingested (``parentId`` dangles outside the set) — the
    #     frontend's preorder tree walk starts from the roots, so an
    #     orphaned-only set renders nothing.
    # Either way the trace isn't fully ingested — degrade to ``not_ready`` so
    # the console shows the refresh card (and auto-polls) until it settles,
    # instead of a confusing empty ruler.
    spans = normalized.get("spans")
    if not isinstance(spans, list) or not _is_renderable_tree(spans):
        return {"status": "not_ready"}
    return normalized


def fetch_span_raw(client: Any, trace_id: str, span_id: str, field: str) -> str | None:
    """未截断、未清洗的单 span input/output 全文(raw 层)——Task 4 "查看原文".

    Unlike :func:`_render_io` (used by :func:`normalize_trace`) this applies
    NO ``_cap_text`` truncation and NO cleaning — the debug console's "查看
    原文" affordance re-fetches this single field on demand when a facade
    message was truncated at ``_MSG_CAP``. best-effort: any failure (no
    client, bad field, network error, unknown span) degrades to ``None``,
    never an exception — the caller turns that into a 404.
    """
    if client is None or field not in ("input", "output"):
        return None
    # The fetch AND the post-fetch serialization are both best-effort: a
    # non-JSON-serializable span field (input/output are typed ``Any``) must
    # degrade to None → 404, never a 500 (硬约束「降级永不 500」). Mirrors the
    # defensive ``json.dumps`` guard in :func:`_render_io`.
    try:
        trace = client.api.trace.get(trace_id)
        for o in getattr(trace, "observations", None) or []:
            if str(getattr(o, "id", "")) != span_id:
                continue
            value = getattr(o, field, None)
            if value is None:
                return None
            if _is_message_list(value):
                return "\n\n".join(
                    f"[{_extract_role(m)}]\n{_extract_content(m.get('content'))}" for m in value
                )
            return (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            )
    except Exception:
        return None
    return None


def _is_renderable_tree(spans: list[Any]) -> bool:
    """A normalized span set renders iff it is non-empty, has at least one root
    (``parentId is None``), and every span's ``parentId`` resolves within the
    set (no dangling parent from a not-yet-ingested observation)."""
    if not spans:
        return False
    ids = {s.get("id") for s in spans}
    has_root = any(s.get("parentId") is None for s in spans)
    fully_connected = all(s.get("parentId") is None or s.get("parentId") in ids for s in spans)
    return has_root and fully_connected


def _parse_observation(o: Any, *, trace_start: Any) -> _ParsedObs:
    obs_id = str(o.id)
    obs_type = str(o.type)
    name = str(o.name)
    parent_id = _clean_str(getattr(o, "parent_observation_id", None))
    if o.start_time is None or trace_start is None:
        start_ms = 0
    else:
        start_ms = round((o.start_time - trace_start).total_seconds() * 1000)
    end_time = getattr(o, "end_time", None)
    if o.start_time is not None and end_time is not None:
        latency_ms = max(0, round((end_time - o.start_time).total_seconds() * 1000))
    else:
        latency_ms = round((o.latency or 0) * 1000)
    kind, label, group = _classify(obs_type, name)
    detail = _tool_detail(o) if kind == "tool" else None
    return _ParsedObs(
        id=obs_id,
        parent_id=parent_id,
        obs_type=obs_type,
        name=name,
        kind=kind,
        label=label,
        detail=detail,
        start_ms=start_ms,
        latency_ms=latency_ms,
        model=_model(o),
        input_tokens=_token_count(o, "prompt_tokens", "promptTokens"),
        output_tokens=_token_count(o, "completion_tokens", "completionTokens"),
        cost_usd=_cost_usd(o),
        input=_render_io(getattr(o, "input", None)),
        output=_render_io(getattr(o, "output", None)),
        level=_level(o),
        status_message=_clean_str(getattr(o, "status_message", None)),
        purpose="",
        group=group,
    )


#: Human labels for each purpose-wrapped LLM span, keyed by the full span name.
#: The machine ``purpose`` is the single source of truth in common's
#: :data:`LLM_SPAN_PURPOSES`; the UI label is this module's presentation
#: concern. Keys are kept in parity with ``LLM_SPAN_PURPOSES`` by the facade
#: tests, so a purpose added in common without a label here fails CI rather than
#: silently rendering the raw span name. Every orchestrator LLM call is a bare
#: ``llm_call`` GENERATION in Langfuse (the router never names them
#: per-purpose), so the wrapper span name is the only surviving intent signal.
_LLM_LABELS: dict[str, str] = {
    "expert_work.orchestrator.llm_call": "LLM 调用",
    "expert_work.memory.extract": "记忆抽取",
    "expert_work.memory.verify": "记忆校验",
    "expert_work.memory.reconcile": "记忆整合",
    "expert_work.orchestrator.planner": "规划",
    "expert_work.orchestrator.reflect": "反思",
    "expert_work.orchestrator.compress": "上下文压缩",
    "expert_work.orchestrator.judge": "输出评审",
    "expert_work.orchestrator.judge_action": "行动评审",
    "expert_work.orchestrator.vision": "视觉理解",
    "expert_work.orchestrator.rerank": "文档重排",
}


#: Human labels for the non-LLM entry-chain spans. Sibling of
#: :data:`_LLM_LABELS`; keys kept in parity with common's ``TRACED_SPANS`` by
#: the facade tests, so a span renamed in the orchestrator without a label
#: here fails CI rather than silently rendering its raw English name.
_SPAN_LABELS: dict[str, str] = {
    "expert_work.memory.recall": "记忆召回",
    "expert_work.memory.resolve_mode": "读取召回配置",
    "expert_work.memory.embed": "向量化",
    "expert_work.memory.retrieve": "向量检索",
    "expert_work.memory.rerank": "记忆重排",
    "expert_work.memory.bump_access": "回写访问计数",
    "expert_work.orchestrator.workspace_ingest": "工作区摄取",
    "expert_work.orchestrator.context_gates": "上下文门",
}


def _llm_wrapper_purpose(name: str) -> tuple[str, str] | None:
    """Return ``(label, purpose)`` if ``name`` is a known LLM wrapper span,
    else ``None``. ``purpose`` comes from common's single-source
    ``LLM_SPAN_PURPOSES``; ``label`` is this module's UI text (keys kept in
    parity by the facade tests)."""
    purpose = LLM_SPAN_PURPOSES.get(name)
    if purpose is None:
        return None
    return _LLM_LABELS.get(name, name), purpose


def _resolve_omissions(
    parsed_by_id: dict[str, _ParsedObs],
    children_by_parent: dict[str | None, list[str]],
) -> tuple[dict[str, str | None], dict[str, int], dict[str, str], dict[str, str]]:
    """Find nodes to drop from the output tree + their reparent target.

    Two independent omission rules (spec §2.3 步骤 3-4):

    * A known LLM wrapper SPAN (``LLM_SPAN_PURPOSES`` — the main
      ``orchestrator.llm_call`` plus each auxiliary ``memory.extract`` /
      ``orchestrator.planner`` / … purpose span) with exactly one GENERATION
      child is a wrapper — omit the SPAN, let the GENERATION inherit the SPAN's
      (more complete) latency, and stamp the wrapper's human label + purpose
      onto the GENERATION so an auxiliary call is no longer an anonymous
      "LLM 调用".
    * The ROOT ``*.http_request`` observation (the FastAPI entry span, i.e.
      ``parent_id is None``) is omitted; its children re-parent past it. A
      non-root span whose name merely contains ``.http_request`` is kept.

    Returns ``(omitted, latency_override, label_override, purpose_override)``
    where ``omitted`` maps an omitted node's id to the raw parent id its
    children should redirect through (chained by the caller until a surviving
    ancestor or ``None``); the override maps carry the merged GENERATION's new
    label / purpose keyed by child id.
    """
    omitted: dict[str, str | None] = {}
    latency_override: dict[str, int] = {}
    label_override: dict[str, str] = {}
    purpose_override: dict[str, str] = {}

    for parsed in parsed_by_id.values():
        if parsed.obs_type != "SPAN":
            continue
        purpose = _llm_wrapper_purpose(parsed.name)
        if purpose is None:
            continue
        child_ids = children_by_parent.get(parsed.id, [])
        if len(child_ids) != 1:
            continue
        child = parsed_by_id[child_ids[0]]
        if child.obs_type != "GENERATION":
            continue
        omitted[parsed.id] = parsed.parent_id
        latency_override[child.id] = parsed.latency_ms
        label_override[child.id] = purpose[0]
        purpose_override[child.id] = purpose[1]

    for parsed in parsed_by_id.values():
        # Root-only: elide the FastAPI entry span (parent_id is None), NOT any
        # legitimately-named non-root span that happens to contain the substring.
        if parsed.parent_id is None and ".http_request" in parsed.name:
            omitted[parsed.id] = parsed.parent_id

    return omitted, latency_override, label_override, purpose_override


def _classify(obs_type: str, name: str) -> tuple[str, str, str | None]:
    """Map an observation's raw ``type``/``name`` to (kind, label, group)."""
    if obs_type == "GENERATION":
        return "llm", "LLM 调用", None
    if ".tool_call" in name:
        return "tool", "工具调用", None
    if ".session.run" in name:
        return "session", "会话运行", None
    entry_label = _SPAN_LABELS.get(name)
    if entry_label is not None:
        return "span", entry_label, "entry"
    return "span", _clean_label(name), None


def _clean_label(name: str) -> str:
    if name.startswith(_NAME_PREFIX):
        return name[len(_NAME_PREFIX) :]
    return name


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _level(o: Any) -> str:
    raw = getattr(o, "level", None)
    if raw is None:
        return "default"
    # ObservationLevel enum → "DEFAULT"/"WARNING"/"ERROR";也兼容裸字符串
    text = getattr(raw, "value", None) or str(raw)
    return text.rsplit(".", 1)[-1].lower()


def _model(o: Any) -> str | None:
    model = getattr(o, "model", None)
    return str(model) if model else None


def _tool_detail(o: Any) -> str | None:
    metadata = getattr(o, "metadata", None)
    if isinstance(metadata, dict):
        # Langfuse stores a span's OTel attributes under metadata["attributes"];
        # the tool_call span sets attributes={"tool": <name>} (builder.py), so
        # the tool name lives at metadata["attributes"]["tool"].
        attrs = metadata.get("attributes")
        if isinstance(attrs, dict) and attrs.get("tool"):
            return str(attrs["tool"])
        # Defensive fallbacks for older / flattened shapes.
        value = metadata.get("tool_name") or metadata.get("toolName")
        if value:
            return str(value)
    direct = getattr(o, "tool_name", None) or getattr(o, "toolName", None)
    if direct:
        return str(direct)
    return None


def _token_count(o: Any, snake_name: str, camel_name: str) -> int | None:
    # Langfuse SDK versions mix snake_case + camelCase attribute names —
    # try both, then fall back to the (0-or-missing → None) convention.
    raw = getattr(o, snake_name, None) or getattr(o, camel_name, None)
    if raw is None:
        return None
    count = int(raw)
    return count if count > 0 else None


def _cost_usd(o: Any) -> float | None:
    raw = getattr(o, "calculated_total_cost", None)
    if not raw or raw <= 0:
        return None
    return float(raw)


def _extract_role(m: dict[str, Any]) -> str:
    return str(m.get("type") or m.get("role") or "message")


def _extract_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return json.dumps(content, ensure_ascii=False)


def _extract_tool_calls(m: dict[str, Any]) -> list[str] | None:
    raw = m.get("tool_calls")
    if not raw:
        ak = m.get("additional_kwargs")
        raw = ak.get("tool_calls") if isinstance(ak, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    names: list[str] = []
    for c in raw:
        if isinstance(c, dict):
            name = c.get("name") or (c.get("function") or {}).get("name")
            if name:
                names.append(str(name))
    return names or None


def _cap_text(text: str, cap: int) -> tuple[str, bool, int]:
    full = len(text)
    if full > cap:
        return text[:cap] + _TRUNCATION_SUFFIX, True, full
    return text, False, full


def _is_message_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(m, dict) and "content" in m for m in value)
    )


def _render_io(value: Any) -> dict[str, Any] | None:
    """结构化渲染 observation 的 input/output(spec §A1)。"""
    if value is None:
        return None
    if _is_message_list(value):
        messages: list[dict[str, Any]] = []
        for m in value:
            capped, truncated, full = _cap_text(_extract_content(m.get("content")), _MSG_CAP)
            messages.append(
                {
                    "role": _extract_role(m),
                    "content": capped,
                    "truncated": truncated,
                    "fullChars": full,
                    "toolCalls": _extract_tool_calls(m),
                }
            )
        return {"kind": "messages", "messages": messages}
    if isinstance(value, str):
        text_full = value
    else:
        try:
            text_full = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text_full = str(value)
    capped, truncated, full = _cap_text(text_full, _TEXT_CAP)
    return {"kind": "text", "text": capped, "truncated": truncated, "fullChars": full}


def _span_as_dict(span: TraceSpan) -> dict[str, object]:
    return {
        "id": span.id,
        "parentId": span.parent_id,
        "kind": span.kind,
        "label": span.label,
        "detail": span.detail,
        "startMs": span.start_ms,
        "latencyMs": span.latency_ms,
        "model": span.model,
        "inputTokens": span.input_tokens,
        "outputTokens": span.output_tokens,
        "costUsd": span.cost_usd,
        "input": span.input,
        "output": span.output,
        "level": span.level,
        "statusMessage": span.status_message,
        "purpose": span.purpose,
        "group": span.group,
    }
