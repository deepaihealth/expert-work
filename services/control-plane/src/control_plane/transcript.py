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

from datetime import datetime
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from expert_work.common.conversation_channel import visible_turns
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.persistence import MessageTurn


def _parse_stamp_created_at(ak: dict[str, Any]) -> datetime | None:
    """``expert_work_created_at`` → ``datetime``,缺失/损坏一律退化成 ``None``。

    上线前写入的老消息、或手工构造的 ``additional_kwargs`` 都不能让整个
    会话的读取炸掉。
    """
    raw = ak.get(STAMP_CREATED_AT)
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _parse_stamp_run_id(ak: dict[str, Any]) -> UUID | None:
    """``expert_work_run_id`` → ``UUID``,缺失/损坏一律退化成 ``None``。"""
    raw = ak.get(STAMP_RUN_ID)
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def extract_turns(raw_messages: list[Any], *, include_hidden: bool = True) -> list[MessageTurn]:
    """把检查点 ``messages`` 通道的原始消息抽成用户/助手文本轮次。

    从 :func:`read_turns` 拆出的纯函数(P2)。拆的目的是让「对外消息列表」
    与「会话 message_count」共用同一个定义 —— 镜像表那摊语义债的根因正是
    两套定义各写各的然后漂了。任何一侧改口径,另一侧自动跟随。

    轮次抽取 + ``channel`` 判定本体在
    :func:`expert_work.common.conversation_channel.visible_turns` —— 对话条目
    (``conversation_derive``)要给出同一个 ``channel``,而 orchestrator 不能
    import control-plane,所以那条规则住在 common。本函数只负责把可见轮次映射
    成 :class:`MessageTurn` 并补上写入侧盖的时间戳 / run 归属(它们是
    control-plane 的 ``datetime`` / ``UUID`` 形态,不进 common 的纯结构层)。
    """
    out: list[MessageTurn] = []
    for turn in visible_turns(raw_messages, include_hidden=include_hidden):
        ak = getattr(raw_messages[turn.seq], "additional_kwargs", None) or {}
        out.append(
            MessageTurn(
                seq=turn.seq,
                role=turn.role,
                content=turn.text,
                channel=turn.channel,
                created_at=_parse_stamp_created_at(ak),
                run_id=_parse_stamp_run_id(ak),
            )
        )
    return out


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
    return extract_turns(raw, include_hidden=include_hidden)


__all__ = ["extract_turns", "read_turns"]
