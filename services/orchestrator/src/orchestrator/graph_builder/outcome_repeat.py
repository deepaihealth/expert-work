"""B-302 PR 4 —— 结果重复检测,**影子模式**(spec §4.4;用户 D2:先影子 + 离线回放,看完误伤再定)。

防打转(``expert_work.runtime.middleware.loop_detection``)只认「连续 3 次参数完全相同」;参数
次次不同、结果却一样的打转它看不见。这里在 tools 节点每批工具调用之后给每条工具结果算一个
指纹,和本轮之前的比,按两种信号标出**提案里会提醒 / 会计打转**的调用:

* **S1 同参数同结果**(``same_args``):同一工具、规整后参数相同、结果指纹相同,本轮第 3 次
  起(不要求连续)—— 提案动作「计一次打转并提醒」,记成 ``trip``。
* **S2 换参数同结果**(``changed_args``):同一工具、结果指纹相同、出现过 ≥ 2 种参数,本轮
  第 3 次起 —— 第 3、4 次提案只提醒(``remind``),第 5 次起计打转(``trip``)。

两种信号**各自独立计数**:同一个调用可以同时命中 S1 与 S2(例如 A、B、A 之后再来一次 A),
计数器两个序列各 +1。读数时 ``same_args`` 与 ``changed_args`` 不能直接相加当「被标的调用数」;
按批的 ``trips_total`` 不受影响。

只看真派发过的调用(tools 节点筛):审批续跑里由平台作答的、手抄 URL 守卫拦下的、前置中间件
拦下的不算;B-268 拒绝的重复读取也不算 —— 它已经计进现有的打转次数,影子再算一遍会重复。

**影子模式只记、不动**:不给模型任何文字,不改工具结果、不碰 ``loop_trip_count``、不改路由;
内部任何异常都吞掉(只记异常类型和一个错误计数),run 照常。账本身不进 ``updates`` 帧
(``orchestrator.sse._INTERNAL_STATE_KEYS``)。记三样,全部只有工具名、次数、布尔(参数、结果、
路径、用户文字一律不记):

* 计数器 ``expert_work_outcome_repeat_shadow_total{signal, action, tool}``:每个被标的调用、每种
  信号一次;
* 计数器 ``expert_work_outcome_repeat_shadow_trips_total``:有 ``trip`` 的批次一次(真开的时候
  每步至多计一次打转,同 loop-detection 与 B-268);
* 轨迹 span ``expert_work.orchestrator.outcome_repeat_shadow``(有标记的批次一条)与一行 INFO 日志。

另有 ``expert_work_outcome_repeat_shadow_errors_total``:检测自己出错的批次。

指纹 = sha256(工具名 + 去掉易变字段后的结果字节)。先把 spotlight 的包装精确还原(去围栏与
▁,围栏里的 nonce 每次构建一换);超大结果只取头尾各 :data:`FINGERPRINT_EDGE_CHARS` 个字符。
易变字段只按与语言无关的机器格式去:UUID、ISO-8601 日期时间、``hh:mm:ss(.fff)``、≥ 16 位且
至少含一个 a-f 的十六进制串、10 / 13 位时间戳、耗时(「数字 + ms / sec」,单字母 ``s`` 只认带
小数的)、``/tmp/`` 下的路径,外加本次调用自己的 ``tool_call_id``(≥ 6 个字符时按整段去;超长
结果落盘引用 ``.tool_results/<run>/<call_id>-<tool>.txt`` 里带着它,不去掉的话同样的大输出永远
对不上)。不切词、不按任何语言处理。去完不足 :data:`MIN_RESULT_BYTES` 字节的结果不计。

两个方向都会错(§4.4):去不干净 → 漏认(例如 ``time`` 命令的 ``0m1.234s`` 这种写法不去,两次
耗时不同的同一输出认不出;整数秒的 ``12s`` 同样不去);去得太多(两个不同的哈希、两个真的不同的
10 位编号、有意义的耗时差)→ 误认。影子模式要量的正是后者。

账(``AgentState.outcome_repeats``)= ``{结果指纹: {参数指纹: 次数}}``,指纹都含工具名;只由
tools 节点整份写,每轮开始清空(``control_plane.api.runs`` 的两个图输入函数,同打转次数)。

已知的影子期代价,转正时再改:

* 账每批**整份**写回检查点,一轮 O(n²) 字节;转正时改成增量或加上限。
* 指标先于状态写入:一批在指标记完、检查点写入之前被打断(实例挂掉后复活重跑这一批),这一批
  的标记会再记一次 —— 影子计数可能偏多,不会偏少。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from langchain_core.messages import ToolMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.common.observability.metrics import expert_work_counter
from expert_work.common.spotlight import unspotlight
from expert_work.runtime.middleware.loop_detection import fingerprint_tool_calls
from orchestrator.tools.mcp import parse_mcp_tool_name

logger = logging.getLogger(__name__)

#: ``AgentState`` 的通道名。
OUTCOME_REPEATS_KEY: Final = "outcome_repeats"
#: 去掉易变字段后不足这么多字节的结果不计(§4.4)。
MIN_RESULT_BYTES: Final = 64
#: 本轮第几次起标(S1、S2 都是第 3 次);S2 第几次起提案计打转。
FLAG_FROM: Final = 3
S2_TRIP_FROM: Final = 5
#: 超大结果只取头尾各这么多字符算指纹(L6:6 MB 整段过正则约 0.3 秒,会卡住事件循环)。
FINGERPRINT_EDGE_CHARS: Final = 64_000
#: 本次调用的 ``tool_call_id`` 不足这么多字符时不从结果里去(太短,容易撞上正文,L9)。
MIN_CALL_ID_CHARS: Final = 6
#: 存进检查点的指纹长度(十六进制位)。一轮里几百条结果,64 位足够不撞。
_FINGERPRINT_HEX: Final = 16

Signal = Literal["same_args", "changed_args"]
Action = Literal["remind", "trip"]

#: 只认 ASCII:``\d`` 不吃全角数字,``\b`` 把汉字当边界(「编号1762934130」里的时间戳能去掉,
#: 「id1762934130」这种标识符里的数字不动)。顺序有讲究:UUID 先于十六进制串,日期时间先于时刻。
_VOLATILE: Final = tuple(
    re.compile(pattern, re.ASCII)
    for pattern in (
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
        r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b",
        # 至少含一个 a-f:纯十进制的长号码(订单号、雪花 ID)不是哈希(L1)。
        r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{16,}\b",
        r"\b(?:\d{13}|\d{10})\b",
        # 耗时:``ms`` / ``sec`` 带不带小数都算;单字母 ``s`` 只认带小数的(``7.5 s``、``0.32s``),
        # 「1990s」「100s of files」「version 3 s」不是耗时(L1)。
        r"\b\d+(?:\.\d+)?\s?(?:ms|sec)\b",
        r"\b\d+\.\d+\s?s\b",
        r"/tmp/[^\s'\"`<>()\[\]{},;]*",  # noqa: S108 — 匹配结果里的路径,不是建临时文件
    )
)

_shadow_total = expert_work_counter(
    "expert_work_outcome_repeat_shadow_total",
    "Tool calls the outcome-repeat check would have flagged, by signal (same_args / "
    "changed_args) and proposed action (remind / trip); shadow mode, nothing is enforced "
    "(B-302 PR 4).",
    ("signal", "action", "tool"),
)
_shadow_trips_total = expert_work_counter(
    "expert_work_outcome_repeat_shadow_trips_total",
    "Tool batches in which the outcome-repeat check would have counted a no-progress trip "
    "(at most one per batch); shadow mode, nothing is enforced (B-302 PR 4).",
)
_shadow_errors_total = expert_work_counter(
    "expert_work_outcome_repeat_shadow_errors_total",
    "Tool batches the outcome-repeat shadow check failed on; the run goes on unchanged "
    "(B-302 PR 4).",
)


@dataclass(frozen=True)
class OutcomeFlag:
    """一个被标的调用:哪种信号、提案动作、工具名、本轮第几次、几种参数。没有任何内容。"""

    signal: Signal
    action: Action
    tool: str
    occurrence: int
    distinct_args: int


def strip_volatile(text: str) -> str:
    """去掉易变字段(见模块说明)。"""
    for pattern in _VOLATILE:
        text = pattern.sub("", text)
    return text


def _result_text(message: ToolMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # dict 键 int / str 混用时 ``sort_keys`` 比不了(M3)。
        return str(content)


def result_fingerprint(tool: str, message: ToolMessage) -> str | None:
    """sha256(工具名 + 去易变字段后的结果字节);去完不足 :data:`MIN_RESULT_BYTES` → ``None``。"""
    text = _result_text(message)
    if len(text) > 2 * FINGERPRINT_EDGE_CHARS:
        text = text[:FINGERPRINT_EDGE_CHARS] + text[-FINGERPRINT_EDGE_CHARS:]
    # 工具结果默认被 spotlight 包过(行内空白前插 ▁、围栏带每次构建一换的 nonce):先精确还原,
    # 带空格的易变字段才去得掉,换了构建的同一结果才对得上(M2)。落盘引用在围栏外,原样留着。
    text = unspotlight(text)
    call_id = str(message.tool_call_id or "")
    if len(call_id) >= MIN_CALL_ID_CHARS:
        # 只按整段去:前后不是 ASCII 字母数字(落盘引用里是 ``/<call_id>-``)。
        text = re.sub(rf"(?<![0-9A-Za-z]){re.escape(call_id)}(?![0-9A-Za-z])", "", text)
    # ``surrogatepass``:孤立代理字符照样有字节,不抛(M3)。
    body = strip_volatile(text).encode("utf-8", "surrogatepass")
    if len(body) < MIN_RESULT_BYTES:
        return None
    digest = hashlib.sha256(tool.encode("utf-8") + b"\x00" + body).hexdigest()
    return digest[:_FINGERPRINT_HEX]


def _args_fingerprint(call: Mapping[str, Any]) -> str:
    """与 loop-detection 同一套参数规整(键排序、字符串去首尾空白并小写),含工具名。"""
    one = {"name": str(call.get("name", "")), "args": call.get("args") or {}}
    return fingerprint_tool_calls([one])[:_FINGERPRINT_HEX]


def _clean_ledger(previous: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """从检查点里的账取一份新的;坏值一律丢掉。"""
    out: dict[str, dict[str, int]] = {}
    for result_fp, by_args in previous.items():
        if not isinstance(result_fp, str) or not isinstance(by_args, Mapping):
            continue
        counts = {
            args_fp: n
            for args_fp, n in by_args.items()
            if isinstance(args_fp, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0
        }
        if counts:
            out[result_fp] = counts
    return out


def _flags_for(tool: str, by_args: Mapping[str, int], args_fp: str) -> list[OutcomeFlag]:
    flags: list[OutcomeFlag] = []
    same = by_args[args_fp]
    if same >= FLAG_FROM:
        flags.append(OutcomeFlag("same_args", "trip", tool, same, 1))
    total = sum(by_args.values())
    distinct = len(by_args)
    if total >= FLAG_FROM and distinct >= 2:
        action: Action = "trip" if total >= S2_TRIP_FROM else "remind"
        flags.append(OutcomeFlag("changed_args", action, tool, total, distinct))
    return flags


def next_outcomes(
    previous: Mapping[str, Any],
    calls: Sequence[tuple[Mapping[str, Any], ToolMessage]],
) -> tuple[dict[str, dict[str, int]], list[OutcomeFlag]]:
    """把一批真跑过的 ``(tool_call, ToolMessage)`` 按调用顺序套到账上;不改 ``previous``。"""
    ledger = _clean_ledger(previous)
    flags: list[OutcomeFlag] = []
    for call, message in calls:
        tool = str(call.get("name", ""))
        result_fp = result_fingerprint(tool, message)
        if result_fp is None:
            continue
        args_fp = _args_fingerprint(call)
        by_args = dict(ledger.get(result_fp, {}))
        by_args[args_fp] = by_args.get(args_fp, 0) + 1
        ledger[result_fp] = by_args
        flags.extend(_flags_for(tool, by_args, args_fp))
    return ledger, flags


def _metric_tool_label(name: str) -> str:
    """同 ``graph_builder.builder._metric_tool_label``:MCP 工具名收成 ``mcp:<server>``。"""
    parsed = parse_mcp_tool_name(name)
    return f"mcp:{parsed[0]}" if parsed is not None else name


def _record(flags: Sequence[OutcomeFlag]) -> None:
    """计数器 + 轨迹 span + 一行日志。只有工具名、次数、布尔。"""
    for flag in flags:
        _shadow_total.labels(
            signal=flag.signal, action=flag.action, tool=_metric_tool_label(flag.tool)
        ).inc()
    would_trip = any(flag.action == "trip" for flag in flags)
    if would_trip:
        _shadow_trips_total.inc()
    tools = sorted({flag.tool for flag in flags})
    same_args = sum(flag.signal == "same_args" for flag in flags)
    changed_args = sum(flag.signal == "changed_args" for flag in flags)
    max_occurrence = max(flag.occurrence for flag in flags)
    with expert_work_span(
        ExpertWorkComponent.ORCHESTRATOR,
        "outcome_repeat_shadow",
        attributes={
            "same_args": same_args,
            "changed_args": changed_args,
            "would_trip": would_trip,
            "tools": tools,
            "max_occurrence": max_occurrence,
        },
    ):
        pass
    logger.info(
        "outcome_repeat.shadow same_args=%d changed_args=%d would_trip=%s tools=%s "
        "max_occurrence=%d",
        same_args,
        changed_args,
        would_trip,
        tools,
        max_occurrence,
    )


def outcome_repeat_update(
    state: Mapping[str, Any],
    calls: Sequence[tuple[Mapping[str, Any], ToolMessage]],
) -> dict[str, Any]:
    """tools 节点的钩子:套账、记影子标记,返回要写进 state 的那一键(没有可计的结果 → ``{}``)。

    影子模式:返回值里永远只有 :data:`OUTCOME_REPEATS_KEY`。内部任何异常都吞掉、计数、只记异常
    类型,返回 ``{}`` —— 影子检测绝不能打断 run(M3)。
    """
    try:
        raw = state.get(OUTCOME_REPEATS_KEY)
        previous: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        ledger, flags = next_outcomes(previous, calls)
        if flags:
            _record(flags)
        if ledger == _clean_ledger(previous):
            return {}
        return {OUTCOME_REPEATS_KEY: ledger}
    except Exception as exc:
        _shadow_errors_total.inc()
        logger.warning("outcome_repeat.shadow_failed err=%s", type(exc).__name__)
        return {}
