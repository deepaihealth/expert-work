"""Stream CM-12 — mechanical tool-result prune gate.

The cheapest, least-lossy rung of the ``agent_node`` context cascade, run
*before* the CM-2 :class:`~orchestrator.context.working_window.WorkingWindow`.
When the estimated prompt is over threshold it collapses **old** tool results
(every :class:`~langchain_core.messages.ToolMessage` beyond the most-recent
``recent_tool_results_kept``) to a 1-line reference, while leaving every turn
and the assistant's reasoning intact. This is the graceful step between the
window's *drop-the-whole-turn* (which also loses the reasoning) and the L.L2
:class:`~orchestrator.context.compressor.ContextCompressor`'s *LLM summary*
(which costs a model call).

Design anchors (docs/design/tool-result-context-budget.md §Phase 2):

* **Token-gated** — no-op under ``context_window * threshold_pct`` (zero
  behaviour change for runs that are not overflowing). Shares the compressor's
  estimator basis (CM-C6).
* **Count-based recent protection** — the bloat shape is *many tool calls*
  (often within one turn), so the recent window is counted in ``ToolMessage``s,
  not turns: a turn-based window would not relieve a single turn that fired
  eight searches.
* **Pairing-safe by construction** — prune only **rewrites** ``ToolMessage``
  content, never removes a message, so no ``AIMessage.tool_calls`` ↔
  ``ToolMessage`` pair is ever split. No boundary logic needed.
* **Lossless when a copy is on disk** — a result is collapsed losslessly when
  either (a) it carries the ``<tool-result-overflow>`` footer (#859 externalized
  → keep just the footer; bonus: drops the untrusted spotlight-fenced preview),
  or (b) its ``artifact`` records a persisted-copy path (item 2 persist floor →
  render a footer reference). Only a small result below the persist floor (no
  on-disk copy) collapses to a lossy ``<tool-result-pruned>`` stub — still
  strictly less lossy than the whole-turn drop the window would otherwise apply.
* **Dedup (item 1)** — a tool result whose exact content recurs later is
  collapsed to a reference (latest copy kept), reclaiming the bulk of a repeated
  identical search/fetch even inside the recent window.
* **Skill reference (RT-2 PR-3 / RT-ADR-7)** — a SUCCESSFUL lazy-skill read
  (``ToolMessage(name="skill_view")``, artifact ``result`` ok/truncated)
  collapses to a one-line skill reference (name + source path + re-read hint)
  instead of the generic ladder: the skill row is durable in the skill store
  and re-readable via ``skill_view``, so the reference is the better recovery
  handle than even the workspace footer. Failure/blocked placeholders (drift /
  redacted / archived are security stops) keep the generic ladder — never
  rewritten into a re-read encouragement.
* **Prompt-view only** (CM-C4) — like the window, the gate shapes only the
  message list handed to *this* LLM call; ``agent_node`` returns just the new
  response tail and the ``add_messages`` reducer never deletes, so the
  checkpointed history is never rewritten — the next turn reloads it in full and
  prunes afresh.
* **Idempotent** — a content already reduced to a stub or to a footer-only
  reference is skipped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from expert_work.common.conversation_channel import is_hidden
from expert_work.runtime.tokens import TokenEstimator
from orchestrator.context.compressor import estimate_tokens
from orchestrator.context.skill_reference import skill_view_reference
from orchestrator.tools.overflow import (
    OVERFLOW_FOOTER_TAG_OPEN,
    TOOL_RESULT_PATH_ARTIFACT_KEY,
    render_overflow_footer,
)

logger = logging.getLogger(__name__)

#: Wrapper tags on the lossy stub left for a pruned non-externalized result.
#: Doubles as the idempotency marker (a content that already starts with this
#: has been pruned before).
_PRUNE_TAG_OPEN = "<tool-result-pruned>"
_PRUNE_TAG_CLOSE = "</tool-result-pruned>"


@dataclass(frozen=True)
class PruneResult:
    """Outcome of a prune pass — the (possibly rewritten) prompt view plus how
    many tool results were collapsed (``0`` ⇒ no-op)."""

    messages: list[BaseMessage]
    pruned_count: int
    #: B-126 —— 本次跨轮清理估算省下的 token(观测用;非跨轮路径为 0)。
    reclaimed_tokens: int = 0


def _is_already_pruned(content: str) -> bool:
    """True when ``content`` is already a prune stub or a footer-only reference.

    A *not-yet-pruned* externalized result has its preview body before the
    footer, so it does not start with the overflow tag — only a footer-only
    (already-pruned) message does. Keeps the pass idempotent.
    """
    stripped = content.lstrip()
    return stripped.startswith(_PRUNE_TAG_OPEN) or stripped.startswith(OVERFLOW_FOOTER_TAG_OPEN)


def _artifact_path(message: ToolMessage) -> str | None:
    """Workspace path of the full copy persisted at tool time (item 2), if any."""
    art = message.artifact
    if isinstance(art, dict):
        value = art.get(TOOL_RESULT_PATH_ARTIFACT_KEY)
        if isinstance(value, str) and value:
            return value
    return None


def _collapsed_content(message: ToolMessage, *, lossy_note: str) -> str | None:
    """1-line replacement for a ``ToolMessage`` — lossless if recoverable.

    Returns ``None`` when the message is non-string (multimodal) content or is
    already collapsed (idempotent skip), so the caller's ``pruned_count`` stays
    accurate. Recoverability ladder:

    0. **Skill reference** (RT-2 PR-3 / RT-ADR-7) — a successful ``skill_view``
       result collapses to a one-line skill reference (name + source path +
       re-read hint), overriding the footer/path rungs: the skill store is
       durable while the workspace copy is run-scoped, so ``skill_view``
       re-read is the better recovery handle. Blocked/error placeholders get
       no reference (see :mod:`orchestrator.context.skill_reference`).
    1. **In-context footer** (CM-5 / #859 externalized) — keep the trusted footer
       alone; the full output is on disk and re-readable via ``read_file``.
    2. **Artifact path** (item 2 persist floor) — render a footer reference to the
       persisted copy, even though there was no in-context footer.
    3. **Lossy stub** — only when no on-disk copy exists (small result below the
       persist floor): a short note with the tool name + char count + reason.
    """
    content = message.content
    if not isinstance(content, str) or _is_already_pruned(content):
        return None
    reference = skill_view_reference(message)
    if reference is not None:
        # Wrapped in the prune tags so ``_is_already_pruned`` keeps the pass
        # idempotent for skill stubs too.
        return f"{_PRUNE_TAG_OPEN}\n{reference}\n{_PRUNE_TAG_CLOSE}"
    footer_at = content.find(OVERFLOW_FOOTER_TAG_OPEN)
    if footer_at != -1:
        return content[footer_at:].lstrip("\n")
    path = _artifact_path(message)
    if path is not None:
        return render_overflow_footer(rel=path, total_chars=len(content)).lstrip("\n")
    name = message.name or "tool"
    return (
        f"{_PRUNE_TAG_OPEN}\n"
        f"[{name}] {len(content):,} chars elided ({lossy_note})\n"
        f"{_PRUNE_TAG_CLOSE}"
    )


def _rebuild(message: ToolMessage, new_content: str) -> ToolMessage:
    """A collapsed copy preserving the pairing identity.

    Keeps ``tool_call_id`` (REQUIRED for AIMessage pairing) + ``name`` + ``id``
    (so an accidental persistence path would replace-by-id under ``add_messages``
    rather than duplicate). ``artifact`` is dropped — it never reaches the LLM,
    and the next turn re-reads the original from the checkpoint anyway.
    """
    return ToolMessage(
        content=new_content,
        tool_call_id=message.tool_call_id,
        name=message.name,
        id=message.id,
    )


def prune_old_tool_results(
    messages: Sequence[BaseMessage], *, recent_tool_results_kept: int
) -> PruneResult:
    """Collapse old + duplicate tool results to 1-line references.

    Two collapses, in one pass:

    * **Dedup (item 1)** — a ``ToolMessage`` whose exact content recurs in a
      *later* ``ToolMessage`` is collapsed (the latest identical copy is kept;
      earlier ones become references). Overrides the recent-window protection,
      since an exact duplicate is redundant even when recent.
    * **Age** — a non-duplicate ``ToolMessage`` beyond the most-recent
      ``recent_tool_results_kept`` is collapsed.

    Token-unaware: callers gate on size via :meth:`ToolResultPruner.should_prune`.
    Returns a new list — never mutates the input.
    """
    msgs = list(messages)
    tool_idxs = [i for i, m in enumerate(msgs) if isinstance(m, ToolMessage)]
    # Last index each exact content appears at — anything earlier is a duplicate.
    last_occurrence: dict[str, int] = {}
    for i in tool_idxs:
        content = msgs[i].content
        if isinstance(content, str):
            last_occurrence[content] = i
    protected = set(tool_idxs[max(0, len(tool_idxs) - recent_tool_results_kept) :])
    out: list[BaseMessage] = []
    pruned = 0
    for i, message in enumerate(msgs):
        if not isinstance(message, ToolMessage):
            out.append(message)
            continue
        content = message.content
        is_duplicate = isinstance(content, str) and last_occurrence.get(content, i) > i
        if is_duplicate:
            new_content = _collapsed_content(
                message, lossy_note="duplicate of a later identical result"
            )
        elif i not in protected:
            new_content = _collapsed_content(message, lossy_note="older context, not preserved")
        else:
            new_content = None
        if new_content is not None and new_content != content:
            out.append(_rebuild(message, new_content))
            pruned += 1
        else:
            out.append(message)
    return PruneResult(messages=out, pruned_count=pruned)


_ARGS_HINT_LIMIT = 120


def current_turn_start(messages: Sequence[BaseMessage]) -> int:
    """B-126 —— 本轮起点 = 最后一条**真实**用户消息的下标;没有则 0。

    平台注入的隐藏 HumanMessage(本轮输入块 / 计划 / 工作区摘要 …)带
    ``HIDE_FROM_UI``,不算边界 —— 与 CM-2 ``trim_to_recent_turns`` 同一口径。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and not is_hidden(m):
            return i
    return 0


def _args_hint(args: Mapping[str, Any]) -> str:
    text = json.dumps(dict(args), ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= _ARGS_HINT_LIMIT else text[: _ARGS_HINT_LIMIT - 1] + "…"


def _lossless_reference(message: ToolMessage) -> str | None:
    """无损找回途径 —— 技能引用 / 外置 footer / 持久化路径;都没有则 ``None``。"""
    reference = skill_view_reference(message)
    if reference is not None:
        return reference
    content = message.content
    if isinstance(content, str):
        footer_at = content.find(OVERFLOW_FOOTER_TAG_OPEN)
        if footer_at != -1:
            return content[footer_at:].strip()
    path = _artifact_path(message)
    if path is not None and isinstance(content, str):
        return render_overflow_footer(rel=path, total_chars=len(content)).strip()
    return None


def _cross_turn_stub(
    message: ToolMessage, *, args: Mapping[str, Any] | None, reference: str
) -> str:
    content = message.content
    size = len(content) if isinstance(content, str) else 0
    head = f"[{message.name or 'tool'}]"
    if args:
        head += f" {_args_hint(args)}"
    return (
        f"{_PRUNE_TAG_OPEN}\n"
        f"{head} — {size:,} chars from an earlier turn, elided; recover below if needed.\n"
        f"{reference}\n"
        f"{_PRUNE_TAG_CLOSE}"
    )


def prune_prior_turns(
    messages: Sequence[BaseMessage],
    *,
    recent_tool_results_kept: int,
    min_context_tokens: int,
    min_reclaim_tokens: int,
    estimator: TokenEstimator | None = None,
) -> PruneResult:
    """B-126 —— 新一轮开头无损收起更早轮次的大块工具结果。

    决定只依赖 ``messages[:boundary + 1]``(旧轮次 + 本轮用户消息),所以同一轮内
    每次调用结果逐字相同、缓存前缀稳定(spec D3 / R2)。只收有无损找回途径的结果
    (D6),``status == "error"`` 与非字符串内容一律不动(R4)。去重只在旧轮次内部做
    (R3)。返回新列表,不改入参。
    """
    msgs = list(messages)
    boundary = current_turn_start(msgs)
    if boundary == 0:
        return PruneResult(messages=msgs, pruned_count=0)
    if estimate_tokens(msgs[: boundary + 1], estimator=estimator) < min_context_tokens:
        return PruneResult(messages=msgs, pruned_count=0)

    prior_tool_idxs = [i for i in range(boundary) if isinstance(msgs[i], ToolMessage)]
    protected = set(prior_tool_idxs[max(0, len(prior_tool_idxs) - recent_tool_results_kept) :])
    args_by_call: dict[str, Mapping[str, Any]] = {}
    for i in range(boundary):
        m = msgs[i]
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                call_id = tc.get("id")
                if call_id:
                    args_by_call[call_id] = tc.get("args") or {}
    last_occurrence: dict[str, int] = {}
    for i in prior_tool_idxs:
        c = msgs[i].content
        if isinstance(c, str):
            last_occurrence[c] = i

    replacements: dict[int, ToolMessage] = {}
    reclaimed = 0
    for i in prior_tool_idxs:
        m = msgs[i]
        if not isinstance(m, ToolMessage):
            continue
        content = m.content
        if not isinstance(content, str) or _is_already_pruned(content):
            continue
        if getattr(m, "status", "success") == "error":
            continue
        is_duplicate = last_occurrence.get(content, i) > i
        if i in protected and not is_duplicate:
            continue
        reference = _lossless_reference(m)
        if reference is None:
            continue
        stub = _cross_turn_stub(m, args=args_by_call.get(m.tool_call_id), reference=reference)
        new = _rebuild(m, stub)
        saved = estimate_tokens([m], estimator=estimator) - estimate_tokens(
            [new], estimator=estimator
        )
        if saved <= 0:
            continue
        replacements[i] = new
        reclaimed += saved

    if not replacements or reclaimed < min_reclaim_tokens:
        return PruneResult(messages=msgs, pruned_count=0)
    out = [replacements.get(i, m) for i, m in enumerate(msgs)]
    return PruneResult(messages=out, pruned_count=len(replacements), reclaimed_tokens=reclaimed)


@dataclass(frozen=True)
class ToolResultPruner:
    """Token-gated mechanical prune of old tool results (working memory).

    Build one per agent (the factory wires ``policies.tool_result_prune`` into
    this) and pass to
    :func:`~orchestrator.graph_builder.builder.build_react_graph` so
    ``agent_node`` can call :meth:`apply` at its entry, *before* the CM-2 window.
    """

    context_window: int
    threshold_pct: float = 0.7
    recent_tool_results_kept: int = 4
    #: Stream HX-1 — injected token estimator, shared with the window /
    #: compressor so all gates keep one estimation basis (CM-C6). ``None`` keeps
    #: the legacy ``chars // 4`` heuristic for network-free unit tests.
    estimator: TokenEstimator | None = None

    @property
    def threshold_tokens(self) -> int:
        """Token threshold at/above which a pass prunes. Same shape as the
        window / compressor thresholds so the gates share one basis."""
        return int(self.context_window * self.threshold_pct)

    def should_prune(self, messages: Sequence[BaseMessage]) -> bool:
        """Cheap preflight — ``True`` when the estimate meets/exceeds threshold."""
        return estimate_tokens(messages, estimator=self.estimator) >= self.threshold_tokens

    def apply(self, messages: Sequence[BaseMessage]) -> PruneResult:
        """Collapse old + duplicate tool results when over threshold, else no-op.

        Returns a :class:`PruneResult`; ``pruned_count == 0`` means the prompt was
        left untouched (under threshold, or nothing old/duplicate to collapse).
        """
        msgs = list(messages)
        if not self.should_prune(msgs):
            return PruneResult(messages=msgs, pruned_count=0)
        result = prune_old_tool_results(
            msgs, recent_tool_results_kept=self.recent_tool_results_kept
        )
        if result.pruned_count:
            logger.info(
                "tool_result_prune.pruned count=%d kept=%d",
                result.pruned_count,
                self.recent_tool_results_kept,
            )
        return result
