"""B-302 PR 3 —— 每个文件的「连续失败次数」与「本轮成功改动」两本账(spec §4.3.3 末段、§4.3.4)。

**写的一侧是 tools 节点**(``graph_builder.builder``):每批工具调用之后按调用顺序把成败套到
上一批的账上;**读的一侧是** ``edit_file``(从 :class:`~orchestrator.tools.registry.ToolContext`
拿),做法同 B-268 的 ``figure_reads``。两本账都只属于一轮:每轮开始时清空(与打转次数同一处,
``control_plane.api.runs`` 的两个图输入函数)。

键是写的那个文件 —— 与 run 级欠账 ``("file", path)`` 同一个标识(``mutation_classifier``
取的是原始入参 ``path`` 去首尾空白),所以 ``edit_file`` 与 ``write_file`` / ``read_file``
对同一条路径记在同一格。

连续失败的规则(§4.3.4):

* ``edit_file`` 的 ``no_match`` / ``ambiguous`` / ``stale`` 失败 +1;其它 ``edit_file`` 失败不动;
* 同一文件 ``edit_file`` 或 ``write_file`` 成功 → 清零;
* 同一文件 ``read_file`` 成功只记「失败之后已读过」、不清零(还没失败过时读不算)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator.tools.edit_file_diagnostics import COUNTED_EDIT_FAILURES

#: ``AgentState`` 的两个通道名。
EDIT_FILE_STREAKS_KEY = "edit_file_streaks"
EDIT_FILE_HISTORY_KEY = "edit_file_history"

#: 失败 ``edit_file`` 的 ``ToolMessage.artifact`` 里记失败种类的键(artifact 不发给模型)。
EDIT_FAILURE_ARTIFACT_KEY = "expert_work_edit_file_failure"

#: 本轮成功改动:每处 ``old`` / ``new`` 只留前这么多码点,每个文件只留最近这么多处(§4.3.3)。
HISTORY_CHARS = 2000
HISTORY_PER_FILE = 20


def file_key(path: object) -> str | None:
    """文件账的键:原始 ``path`` 入参去首尾空白(与欠账的 ``("file", path)`` 同一个标识)。"""
    if not isinstance(path, str):
        return None
    key = path.strip()
    return key or None


@dataclass(frozen=True)
class EditStreak:
    """一个文件当前的连续失败次数,以及失败之后有没有 ``read_file`` 过它。"""

    failures: int = 0
    read: bool = False


def streak_for(streaks: Mapping[str, Any], path: object) -> EditStreak:
    """从检查点里的账取一个文件的记录;坏值一律当没有。"""
    key = file_key(path)
    raw = streaks.get(key) if key is not None else None
    if not isinstance(raw, Mapping):
        return EditStreak()
    failures = raw.get("failures")
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        return EditStreak()
    return EditStreak(failures=failures, read=raw.get("read") is True)


def history_for(history: Mapping[str, Any], path: object) -> list[dict[str, str]]:
    """一个文件本轮成功改动的 ``{old, new}`` 列表;坏值一律丢掉。"""
    key = file_key(path)
    raw = history.get(key) if key is not None else None
    if not isinstance(raw, list):
        return []
    return [
        {"old": item["old"], "new": item["new"]}
        for item in raw
        if isinstance(item, Mapping)
        and isinstance(item.get("old"), str)
        and isinstance(item.get("new"), str)
    ]


@dataclass(frozen=True)
class FileToolEvent:
    """一批里一次真跑过的调用,tools 节点按调用顺序交过来。"""

    tool: str
    args: Mapping[str, Any]
    ok: bool
    #: 失败的 ``edit_file`` 的种类(``no_match`` …);其余为 ``None``。
    failure_kind: str | None = None
    #: 成功的 ``edit_file`` 里按「已改过」算的那几处(1 起),它们没有改动任何东西。
    already_applied: tuple[int, ...] = ()


def next_streaks(
    previous: Mapping[str, Any], events: Sequence[FileToolEvent]
) -> dict[str, dict[str, Any]]:
    """把一批调用套到连续失败账上,返回新的账(不改 ``previous``)。"""
    out: dict[str, dict[str, Any]] = {}
    for kept in previous:
        streak = streak_for(previous, kept)
        if streak.failures:
            out[kept] = {"failures": streak.failures, "read": streak.read}
    for event in events:
        key = file_key(event.args.get("path"))
        if key is None:
            continue
        current = out.get(key)
        if event.tool in ("edit_file", "write_file") and event.ok:
            out.pop(key, None)
        elif event.tool == "edit_file" and event.failure_kind in COUNTED_EDIT_FAILURES:
            failures = current["failures"] if current else 0
            read = current["read"] if current else False
            out[key] = {"failures": failures + 1, "read": read}
        elif event.tool == "read_file" and event.ok and current:
            out[key] = {**current, "read": True}
    return out


def _edit_pairs(args: Mapping[str, Any]) -> list[tuple[str, str]]:
    raw = args.get("edits")
    items = raw if isinstance(raw, list) else [args]
    return [
        (item["old_string"], item["new_string"])
        for item in items
        if isinstance(item, Mapping)
        and isinstance(item.get("old_string"), str)
        and isinstance(item.get("new_string"), str)
    ]


def next_history(
    previous: Mapping[str, Any], events: Sequence[FileToolEvent]
) -> dict[str, list[dict[str, str]]]:
    """把一批里成功的 ``edit_file`` 记进本轮改动账,返回新的账(不改 ``previous``)。

    「已改过」的那几处不记 —— 它们这一次没有替换任何东西。
    """
    out: dict[str, list[dict[str, str]]] = {
        key: history_for(previous, key) for key in previous if history_for(previous, key)
    }
    for event in events:
        if event.tool != "edit_file" or not event.ok:
            continue
        key = file_key(event.args.get("path"))
        if key is None:
            continue
        added = [
            {"old": old[:HISTORY_CHARS], "new": new[:HISTORY_CHARS]}
            for k, (old, new) in enumerate(_edit_pairs(event.args), start=1)
            if k not in event.already_applied and old != new
        ]
        if added:
            out[key] = [*out.get(key, []), *added][-HISTORY_PER_FILE:]
    return out
