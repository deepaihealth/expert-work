"""从 _run_event_stream 拆出的 seq 工具函数(PR-A.3 为守 ≤400 行)。"""

from __future__ import annotations

import logging

from expert_work.runtime.stream_bridge import StreamEvent

logger = logging.getLogger(__name__)


def _merge_ranges(seqs: set[int]) -> list[tuple[int, int]]:
    """把一组 seq 合并成连续闭区间 —— 一个洞段只发一帧 ``gap``。"""
    merged: list[tuple[int, int]] = []
    for seq in sorted(seqs):
        if merged and seq == merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], seq)
        else:
            merged.append((seq, seq))
    return merged


def _seq_of(entry: StreamEvent) -> int | None:
    """从帧 id 里解析落库 ``seq``;``None`` 表示这帧不参与接合。

    ``entry.id is None`` 是一次性帧 —— 不可回放、不占号(今天只有 ``token``,
    见 :meth:`StreamBridge.publish_ephemeral`)。
    id 形状不认识时同样返回 ``None``:放行总比把它当成某个号去参与去重安全。
    """
    if entry.id is None:
        return None
    try:
        return int(entry.id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        logger.warning("live_stream.unparsable_frame_id id=%s", entry.id)
        return None
