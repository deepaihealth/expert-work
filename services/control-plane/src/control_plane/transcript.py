"""Shared transcript extraction — checkpoint blob → user/assistant turns.

One extraction path for the two consumers of a thread's conversation
history, so their notion of "a transcript turn" can't drift:

- ``GET /v1/sessions/{thread_id}/messages`` (Playground history + the
  conversation-detail transcript panel);
- :class:`control_plane.transcript_mirror_sweep.TranscriptMirrorSweep`
  (the ``thread_message`` mirror feeding content search — IA M4).

The ``messages`` channel uses the ``add_messages`` append reducer, so the
latest checkpoint carries the full history in one ``aget_tuple`` and a
message's index (``seq``) is stable across reads — the mirror's idempotency
key. Only human/ai turns with non-empty text survive; tool/system messages
stay in the per-run event stream by design.

Assistant turns also carry a structural output ``channel`` — "final" iff the
turn is the last visible one in its user-delimited segment AND has no
``tool_calls``; otherwise "commentary". See
docs/superpowers/specs/2026-07-30-conversation-output-channels-design.md.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from control_plane.api._session_title import message_text
from expert_work.persistence import MessageTurn


async def read_turns(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: UUID,
    *,
    include_hidden: bool = True,
) -> list[MessageTurn]:
    """Read a thread's user/assistant text turns off its durable checkpoint.

    Raises on checkpointer failure — callers pick their own degradation
    (the endpoint returns an empty transcript; the sweep skips the thread
    and retries next cycle).

    ``include_hidden`` (default ``True``) keeps the extraction *faithful*.
    RT-2 PR-4 (RT-ADR-9) marks orchestrator-authored scaffolding persisted
    into the checkpoint — e.g. the CM-1 ``<recovery-advisory>`` ``HumanMessage``
    — with ``expert_work_hide_from_ui``. That scaffolding must stay in the durable
    record, the search/audit mirror (``TranscriptMirrorSweep``) and the
    cross-tenant audit drill-in, so faithful is the *safe default*: a new
    persistence/audit caller that forgets the flag can never silently drop
    content from the audited record. Only the UI bubble view opts out
    (``include_hidden=False``) so scaffolding doesn't render as a turn — the
    raw record still carries it and the model always sees it in-prompt. This
    mirrors deer-flow, which reads the checkpoint faithfully and applies the
    ``hide_from_ui`` visibility filter only at its UI-serving router.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
    tup = await checkpointer.aget_tuple(config)
    if tup is None:
        return []
    raw = (tup.checkpoint.get("channel_values") or {}).get("messages", [])
    # Each row records whether IT opens a new segment, decided purely from its
    # own kwargs — never from list position. That keeps "channel" independent
    # of ``include_hidden`` (a hidden feedback row filtered out of the UI view
    # must not silently move a segment boundary relative to the faithful audit
    # view, which would report a different set of "final" rows for the same
    # thread) and lets a scheduled-delivery ``AIMessage`` (trigger_delivery.py
    # ``inject_delivery``, tagged ``expert_work_scheduled_delivery``) open its
    # OWN segment instead of being appended onto — and stealing "final" from —
    # the user's real answer.
    collected: list[tuple[int, str, str, bool, bool]] = []
    for seq, m in enumerate(raw):
        mtype = getattr(m, "type", None)
        if mtype not in ("human", "ai"):
            continue
        ak = getattr(m, "additional_kwargs", None) or {}
        hidden = bool(ak.get("expert_work_hide_from_ui"))
        if not include_hidden and hidden:
            continue
        text = message_text(getattr(m, "content", ""))
        if not text.strip():
            continue
        has_tool_calls = mtype == "ai" and bool(getattr(m, "tool_calls", None))
        starts_segment = (mtype == "human" and not hidden) or (
            mtype == "ai" and bool(ak.get("expert_work_scheduled_delivery"))
        )
        collected.append((seq, mtype, text, has_tool_calls, starts_segment))
    out: list[MessageTurn] = []
    for i, (seq, mtype, text, has_tool_calls, _starts_segment) in enumerate(collected):
        if mtype == "human":
            out.append(MessageTurn(seq=seq, role="user", content=text))
            continue
        # Channel is structural (spec): an assistant turn is "final" iff it is
        # the last visible turn of its user-delimited segment AND carries no
        # tool_calls; every other assistant turn is "commentary".
        nxt = collected[i + 1] if i + 1 < len(collected) else None
        last_in_segment = nxt is None or nxt[4]
        channel = "final" if last_in_segment and not has_tool_calls else "commentary"
        out.append(MessageTurn(seq=seq, role="assistant", content=text, channel=channel))
    return out


__all__ = ["read_turns"]
