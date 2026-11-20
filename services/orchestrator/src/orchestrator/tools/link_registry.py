"""B-330 —— 对话的链接登记:模板变量收到过的链接,之后的轮次也能交给 MCP 工具。

设计见 ``docs/superpowers/specs/2026-11-20-b330-conversation-link-registry-design.md``。

* **记住**(:func:`register_turn`):每轮开头,本轮模板变量里出现的每个 ``http`` / ``https``
  链接(任何变量、任何深度、JSON 字符串也解析)记进对话检查点的 ``conversation_links`` 通道,
  每条一个编号 ``expert_work://link/<根对话 id 前 8 位>-<顺序号>``。按原文去重,只增不减。
* **告诉模型**(:func:`links_note`):平台段里列最近 10 条的编号与事实,**没有链接本身**。
* **发出前换回**(:func:`check_call` / :func:`rewrite_link_ids`):只对 MCP 工具;参数里任何位置
  整段等于本对话登记里的一个编号 → 换成原链接。不在登记里、拼在长串里、写进 ``http`` 工具
  → 拒绝。入参是「模型可写的引用 → 原值」对照表(§4.10,以后的方案 (a) 往里加上传路径)。
* **结果遮回**(:func:`mask_links`):MCP 结果与错误文字里出现登记过的链接(原样 / JSON 转义 /
  斜杠转义 / 百分号编码 / HTML ``&amp;``)→ 换回编号。纯子串替换。

**原链接只在检查点和真正发出的那一次 MCP 调用里**:日志、指标、审计、错误文字只有编号与计数。
遍历是**新写的**,不复用 ``inputs_doc._walk``(它跳过列表里的裸字符串 —— 正是事故的形状),
只复用 :func:`~orchestrator.tools.inputs_doc.http_url_in` 与
:func:`~orchestrator.tools.inputs_doc.parse_json_value` 两个判定。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from orchestrator.agent_timezone import resolve_agent_timezone
from orchestrator.tools.inputs_doc import http_url_in, parse_json_value
from orchestrator.tools.link_masking import mask_text

logger = logging.getLogger(__name__)

#: 编号的固定前缀;平台已有 ``expert_work://image/…`` 同一类引用。
LINK_ID_PREFIX = "expert_work://link/"
#: 每段对话最多登记几条;满了新来的不登记、不挤掉旧的(旧编号可能已写在历史里)。
MAX_LINKS = 200
#: 一轮最多新登记几条(变量本身最多 64 个键,见 control-plane ``api/runs.py``)。
MAX_NEW_PER_TURN = 64
#: 平台段只列最近这么多条。
NOTE_RECENT = 10
#: 遍历变量值的嵌套上限;超了跳过这个变量(与 ``inputs_doc.MAX_PARSE_DEPTH`` 同量级)。
MAX_DEPTH = 32

#: 完整编号:前缀 8 位十六进制 + 顺序号。
_ID_RE = re.compile(r"expert_work://link/([0-9a-f]{8})-([1-9][0-9]*)")
#: 一个值「整段是一个编号形态」:以前缀开头,中间没有空白和常见分隔符。
_WHOLE_CANDIDATE_RE = re.compile(r"expert_work://link/[^\s;,，；、]*")  # noqa: RUF001
_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,5})$")

_MCP_PREFIX = "mcp__"
_HTTP_TOOL = "http"

RefusalReason = Literal["unknown", "foreign", "embedded", "http", "empty"]

_registered_total = expert_work_counter(
    "expert_work_link_registered_total",
    "B-330 conversation link registry: links registered / dropped at turn start.",
    ("outcome",),
)
_use_total = expert_work_counter(
    "expert_work_link_use_total",
    "B-330 conversation link registry: rewrites, verbatim / near copies, masked results, "
    "ids in final replies.",
    ("event",),
)
_refused_total = expert_work_counter(
    "expert_work_link_refused_total",
    "B-330 conversation link registry: tool calls refused for a link id.",
    ("reason",),
)


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------


class _TooDeepError(Exception):
    """变量嵌套超过 :data:`MAX_DEPTH`。"""


def _walk(
    value: Any, path: tuple[str | int, ...], depth: int
) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """变量值里每个 http(s) 链接的位置,深度优先、按给定顺序。列表里的裸字符串也算。"""
    if depth > MAX_DEPTH:
        raise _TooDeepError
    if isinstance(value, str):
        parsed = parse_json_value(value)
        if parsed is not None:
            yield from _walk(parsed, path, depth + 1)
            return
        url = http_url_in(value)
        if url is not None and _has_host(url):
            yield path, url
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            # 调用方自己塞的 ``local_path`` 不算(``inputs_doc._walk`` 同一理由)。
            if key == "local_path":
                continue
            yield from _walk(item, (*path, str(key)), depth + 1)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _walk(item, (*path, index), depth + 1)


def _has_host(url: str) -> bool:
    """只登记有主机名的链接:``https://`` 这种空壳会把结果里所有链接的开头都遮成编号。"""
    try:
        return bool(urlparse(url).netloc)
    except ValueError:
        return False


def _where(path: tuple[str | int, ...]) -> str:
    """位置说明。键名是租户数据,不写(同 control-plane ``inputs_block``)。"""
    if not path:
        return "整个值"
    head = path[0]
    if isinstance(head, int):
        inner = "里的字段" if any(isinstance(p, str) for p in path[1:]) else ""
        return f"第 {head + 1} 项{inner}"
    return "某个字段"


def _ext(url: str) -> str | None:
    try:
        last = urlparse(url).path.rsplit("/", 1)[-1]
    except ValueError:
        return None
    match = _EXT_RE.search(last)
    return match.group(1).lower() if match else None


def link_id(conversation_id: UUID, seq: int) -> str:
    return f"{LINK_ID_PREFIX}{conversation_id.hex[:8]}-{seq}"


def _max_seq(links: Sequence[Mapping[str, Any]]) -> int:
    """已有登记里最大的顺序号。下一条用它 +1,不用条数 +1:清洗掉的坏条目会让条数变少,
    条数 +1 就会和一条还在的编号撞上。"""
    seqs = [
        int(match.group(2))
        for entry in links
        if (match := _ID_RE.fullmatch(str(entry.get("id", "")))) is not None
    ]
    return max(seqs, default=0)


@dataclass(frozen=True)
class RegisterOutcome:
    #: 整份新登记;``None`` = 没有变化(不写通道)。
    links: list[dict[str, Any]] | None
    added: int
    dropped_cap: int
    dropped_depth: int


def register_turn(
    existing: Sequence[Mapping[str, Any]],
    *,
    inputs: Mapping[str, Any],
    trusted_names: Collection[str],
    conversation_id: UUID,
    run_id: UUID | None,
    now: datetime,
    turn_documents: Sequence[str] = (),
    turn_image_count: int = 0,
) -> RegisterOutcome:
    """本轮 inputs 里的链接并进已有登记(纯函数)。

    去重键 = 链接原文;重复的保留第一次的记录。``trusted_names`` 之外的变量不记扩展名(P16)。
    """
    links = [dict(entry) for entry in existing if isinstance(entry, Mapping)]
    known = {str(entry.get("url")) for entry in links}
    added = dropped_cap = dropped_depth = 0
    next_seq = _max_seq(links) + 1
    files = [str(doc) for doc in turn_documents if isinstance(doc, str)]
    for name, value in inputs.items():
        variable = str(name)
        try:
            found = list(_walk(value, (), 0))
        except _TooDeepError:
            dropped_depth += 1
            continue
        for path, url in found:
            if url in known:
                continue
            if len(links) >= MAX_LINKS or added >= MAX_NEW_PER_TURN:
                dropped_cap += 1
                continue
            known.add(url)
            added += 1
            links.append(
                {
                    "id": link_id(conversation_id, next_seq),
                    "url": url,
                    "variable": variable,
                    "where": _where(path),
                    "ext": _ext(url) if variable in trusted_names else None,
                    "received_at": now.isoformat(),
                    "run_id": str(run_id) if run_id is not None else None,
                    "turn_files": files,
                    "turn_images": int(turn_image_count),
                }
            )
            next_seq += 1
    return RegisterOutcome(
        links=links if added else None,
        added=added,
        dropped_cap=dropped_cap,
        dropped_depth=dropped_depth,
    )


def count_registration(outcome: RegisterOutcome) -> None:
    if outcome.added:
        _registered_total.labels(outcome="added").inc(outcome.added)
    if outcome.dropped_cap:
        _registered_total.labels(outcome="dropped_cap").inc(outcome.dropped_cap)
    if outcome.dropped_depth:
        _registered_total.labels(outcome="dropped_depth").inc(outcome.dropped_depth)


def valid_links(raw: object) -> tuple[Mapping[str, Any], ...]:
    """检查点 / 子代种子里的登记,逐条清洗:``id`` 与 ``url`` 都是字符串的才算。"""
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(
        entry
        for entry in raw
        if isinstance(entry, Mapping)
        and isinstance(entry.get("id"), str)
        and isinstance(entry.get("url"), str)
    )


#: 委派子代拿父对话登记的 ``config["configurable"]`` 键。走 config 而不是状态通道:子代用持久化的
#: 检查点、按子线程 id 存,会话 / 用户清除只删根对话的线程 —— 种进状态的原链接会留在子线程里
#: 删不掉(审查 M3)。值不是标量,LangGraph 不会把它抄进检查点的 metadata。
CONVERSATION_LINKS_KEY = "conversation_links"


def conversation_links_of(
    state: Mapping[str, Any], configurable: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    """本次 run 看到的登记:根对话读自己检查点里的通道,委派子代读父传下来的 config。"""
    return valid_links(state.get("conversation_links")) or valid_links(
        configurable.get(CONVERSATION_LINKS_KEY)
    )


def link_table(links: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """「模型可写的引用 → 原值」对照表(§4.10)。"""
    return {str(entry["id"]): str(entry["url"]) for entry in valid_links(list(links))}


def ids_by_variable(links: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """变量名 → 它收到的链接编号(按登记顺序,每个变量最多最近 :data:`NOTE_RECENT` 条)。"""
    out: dict[str, list[str]] = {}
    for entry in valid_links(list(links)):
        out.setdefault(str(entry.get("variable", "")), []).append(str(entry["id"]))
    return {name: ids[-NOTE_RECENT:] for name, ids in out.items()}


# ---------------------------------------------------------------------------
# 平台段
# ---------------------------------------------------------------------------

_NOTE_HEAD = (
    "[本对话收到的链接]（平台自动生成；链接地址由平台保管，这里只有编号）\n"  # noqa: RUF001
    "MCP 工具的参数值整段是下面某个编号时，平台在发出前把它换成原链接；"  # noqa: RUF001
    "编号对客户没有意义，回复里写了客户也打不开。"  # noqa: RUF001
)


def _received(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return when.astimezone(resolve_agent_timezone()).strftime("%m-%d %H:%M") + " 收到"


def _line(entry: Mapping[str, Any]) -> str:
    facts = [f"{entry.get('variable', '')} {entry.get('where', '')}".strip()]
    ext = entry.get("ext")
    if isinstance(ext, str) and ext:
        facts.append(f".{ext}")
    received = _received(entry.get("received_at"))
    if received:
        facts.append(received)
    text = "，".join(facts)  # noqa: RUF001
    same_turn: list[str] = []
    files = entry.get("turn_files")
    if isinstance(files, list) and files:
        same_turn.append("文件 " + "、".join(str(f) for f in files))
    images = entry.get("turn_images")
    if isinstance(images, int) and images > 0:
        same_turn.append(f"图片 {images} 张")
    if same_turn:
        text += "；同一轮还收到" + "，".join(same_turn)  # noqa: RUF001
    return f"- {entry['id']}：{text}"  # noqa: RUF001


def recent_lines(links: Sequence[Mapping[str, Any]]) -> list[str]:
    return [_line(entry) for entry in valid_links(list(links))[-NOTE_RECENT:]]


def links_note(links: Sequence[Mapping[str, Any]], *, has_mcp: bool) -> str | None:
    """平台段里的那一小段;登记为空或没有 MCP 工具时是 ``None``。同一份登记逐字节相同。"""
    entries = valid_links(list(links))
    if not entries or not has_mcp:
        return None
    parts = [_NOTE_HEAD, *recent_lines(entries)]
    older = len(entries) - NOTE_RECENT
    if older > 0:
        parts.append(f"另有 {older} 条更早的链接，编号仍然有效。")  # noqa: RUF001
    if len(entries) >= MAX_LINKS:
        parts.append(f"登记已满 {MAX_LINKS} 条，之后收到的链接没有编号。")  # noqa: RUF001
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 检查与换回
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkRefusal:
    tool: str
    #: 最外层参数名。
    arg: str
    reason: RefusalReason
    #: 模型写的编号(或长串里的第一个编号);没有链接。
    link_id: str


class LinkIdError(ValueError):
    """派发前换回时才发现的编号错误(检查已在 tools 节点做过,这里是兜底)。消息不含链接。"""


def is_mcp_tool(name: str) -> bool:
    return name.startswith(_MCP_PREFIX)


def _candidate(text: str) -> str | None:
    stripped = text.strip()
    if _WHOLE_CANDIDATE_RE.fullmatch(stripped):
        return stripped
    return None


def _id_prefix(candidate: str) -> str:
    return candidate[len(LINK_ID_PREFIX) :].split("-", 1)[0]


def _classify_unknown(candidate: str, table: Mapping[str, str]) -> RefusalReason:
    if not table:
        return "empty"
    prefixes = {_id_prefix(key) for key in table}
    return "unknown" if _id_prefix(candidate) in prefixes else "foreign"


def _embedded_id(text: str) -> str:
    match = _ID_RE.search(text)
    return match.group(0) if match else LINK_ID_PREFIX


class _RefusedError(Exception):
    def __init__(self, reason: RefusalReason, link_id: str) -> None:
        super().__init__(reason)
        self.reason: RefusalReason = reason
        self.link_id = link_id


def _rewrite_value(
    value: Any, table: Mapping[str, str], used: list[str], *, json_layer: bool
) -> Any:
    if isinstance(value, str):
        if LINK_ID_PREFIX not in value:
            return value
        candidate = _candidate(value)
        if candidate is not None:
            url = table.get(candidate)
            if url is None:
                raise _RefusedError(_classify_unknown(candidate, table), candidate)
            used.append(candidate)
            return url
        if json_layer:
            parsed = parse_json_value(value)
            if parsed is not None:
                rewritten = _rewrite_value(parsed, table, used, json_layer=False)
                return json.dumps(rewritten, ensure_ascii=False)
        raise _RefusedError("embedded", _embedded_id(value))
    if isinstance(value, Mapping):
        # 字典的键也是字符串叶子:写在键里的编号同样换回或拒绝。
        return {
            _rewrite_value(key, table, used, json_layer=False): _rewrite_value(
                item, table, used, json_layer=json_layer
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_rewrite_value(item, table, used, json_layer=json_layer) for item in value]
    return value


def _contains_id(value: Any) -> str | None:
    if isinstance(value, str):
        return _embedded_id(value) if LINK_ID_PREFIX in value else None
    if isinstance(value, Mapping):
        values: Iterator[Any] = iter([*value.keys(), *value.values()])
    elif isinstance(value, list | tuple):
        values = iter(value)
    else:
        return None
    for item in values:
        found = _contains_id(item)
        if found is not None:
            return found
    return None


def _scan(
    tool: str,
    args: Mapping[str, Any],
    table: Mapping[str, str],
    skip: Collection[str],
) -> tuple[dict[str, Any], tuple[str, ...], LinkRefusal | None]:
    """(新参数, 换了哪几个编号, 拒绝)。不改入参(``arg_bindings`` 同一规矩)。"""
    if tool == _HTTP_TOOL:
        for arg, value in args.items():
            found = _contains_id({arg: value})
            if found is not None:
                return dict(args), (), LinkRefusal(tool, str(arg), "http", found)
        return dict(args), (), None
    if not is_mcp_tool(tool):
        return dict(args), (), None
    out: dict[str, Any] = {}
    used: list[str] = []
    for arg, value in args.items():
        if arg in skip:
            out[arg] = value
            continue
        try:
            new_arg = _rewrite_value(arg, table, used, json_layer=False)
            out[new_arg] = _rewrite_value(value, table, used, json_layer=True)
        except _RefusedError as refused:
            return dict(args), (), LinkRefusal(tool, str(arg), refused.reason, refused.link_id)
    return out, tuple(used), None


def check_call(
    tool: str,
    args: object,
    table: Mapping[str, str],
    *,
    skip: Collection[str] = (),
) -> LinkRefusal | None:
    """这一次调用会不会因编号被拒(只判,不换)。``args`` 来自模型,不是映射就不查。"""
    if not isinstance(args, Mapping):
        return None
    return _scan(tool, args, table, skip)[2]


def rewrite_link_ids(
    tool: str,
    args: Mapping[str, Any],
    table: Mapping[str, str],
    *,
    skip: Collection[str] = (),
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """派发前最后一步:编号换成原链接。返回 (新参数, 换了的编号)。出错抛 :class:`LinkIdError`。"""
    new_args, used, refusal = _scan(tool, args, table, skip)
    if refusal is not None:
        msg = f"link id refused ({refusal.reason}) in argument {refusal.arg}"
        raise LinkIdError(msg)
    if used:
        _use_total.labels(event="rewritten").inc(len(used))
    return new_args, used


def refusal_message(refusal: LinkRefusal, links: Sequence[Mapping[str, Any]]) -> str:
    """回给模型的那句话。只有参数名与编号,没有链接。"""
    arg, written = refusal.arg, refusal.link_id
    if not arg:
        # 检查本身出了异常(``_fallback_refusal``):说不出是哪个参数,也不硬写一个空参数名。
        return (
            "[blocked] 平台检查这条调用里的链接编号时出错，这条调用没有发出。"  # noqa: RUF001
            "请确认每个编号都整段单独写在一个值里，再重新调用。"  # noqa: RUF001
        )
    if refusal.reason == "http":
        return (
            f"[blocked] 参数 {arg} 里有链接编号 {written}。编号只在 MCP 工具里会被换成链接，"  # noqa: RUF001
            "http 工具收到的就是这串字。这条调用没有发出。"
        )
    if refusal.reason == "embedded":
        return (
            f"[blocked] 参数 {arg} 里的链接编号拼在了一段文字里。一个值只能是一个编号；"  # noqa: RUF001
            "要传多个链接，请写成数组、每项一个编号，参数只收一个字符串时一次只传一个。"  # noqa: RUF001
            "这条调用没有发出。"
        )
    if refusal.reason == "empty":
        return (
            f"[blocked] 参数 {arg} 里的 {written} 不会被换成链接：本对话没有收到过链接。"  # noqa: RUF001
            "这条调用没有发出。"
        )
    head = (
        f"[blocked] 参数 {arg} 里的 {written} 不是本对话收到的链接编号。"
        if refusal.reason == "foreign"
        else f"[blocked] 参数 {arg} 里的 {written}：本对话没有这个编号。"  # noqa: RUF001
    )
    return "\n".join([head + "这条调用没有发出。本对话最近收到的链接：", *recent_lines(links)])  # noqa: RUF001


#: 同一批里另一条调用的编号有错,这一条没有执行(这一步不挂审批,模型重发)。
NOT_RUN_CONTENT = (
    "[not run] 同一批里另一条调用的链接编号有错，这一批都没有执行。"  # noqa: RUF001
    "改好那一条后，需要的调用请重新发起。"  # noqa: RUF001
)


def count_refusal(refusal: LinkRefusal) -> None:
    _refused_total.labels(reason=refusal.reason).inc()
    logger.warning(
        "tools.link_id_refused tool=%s reason=%s id=%s arg=%s",
        refusal.tool,
        refusal.reason,
        refusal.link_id,
        refusal.arg,
    )


def _string_leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _string_leaves(item)


def note_written_urls(
    tool: str, args: Mapping[str, Any], links: Sequence[Mapping[str, Any]]
) -> None:
    """MCP 参数里模型自己写了登记过的链接:原文一致计 ``verbatim``,抄得接近计 ``near_copy``。
    只计数、不拦(§4.4,审查 M4)。永不抛。"""
    if not is_mcp_tool(tool) or not links or not isinstance(args, Mapping):
        return
    try:
        # 函数内导入:``graph_builder`` 包会导入 builder → tools,模块级导入会绕成环。
        from orchestrator.graph_builder.input_url_guard import UrlCandidate, find_retyped_url

        candidates = [
            UrlCandidate(var_name=str(e.get("variable", "")), url=str(e["url"]), link=str(e["id"]))
            for e in valid_links(list(links))
        ]
        for text in _string_leaves(args):
            if "://" not in text:
                continue
            hit = find_retyped_url(text, candidates)
            if hit is not None:
                _use_total.labels(event="verbatim" if hit.distance == 0 else "near_copy").inc()
    except Exception as exc:
        logger.warning("tools.link_written_check_skipped err=%s", type(exc).__name__)


def note_ids_in_reply(messages: Sequence[Any]) -> None:
    """最终回复(最后一条消息)里出现了编号:只计数,回复原样不改(§8 第 15 条)。"""
    if messages and LINK_ID_PREFIX in str(getattr(messages[-1], "content", "")):
        _use_total.labels(event="id_in_reply").inc()


# ---------------------------------------------------------------------------
# tools 节点用的批量入口
# ---------------------------------------------------------------------------


def _skip_args(bindings: Mapping[str, Any], tool: str) -> frozenset[str]:
    """平台绑定 / 固定值的参数不参与(那是平台的值,§4.4 第 3 条)。"""
    bound = bindings.get(tool)
    if bound is None:
        return frozenset()
    return frozenset({*getattr(bound, "args", {}), *getattr(bound, "fixed", {})})


def check_link_calls(
    calls: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
    bindings: Mapping[str, Any],
) -> dict[int, LinkRefusal]:
    """本批每个会因编号被拒的调用:``{下标: 拒绝}``。检查出异常时那一条按拒绝处理(§4.12)。"""
    table = link_table(links)
    out: dict[int, LinkRefusal] = {}
    for index, call in enumerate(calls):
        tool = str(call.get("name", ""))
        args = call.get("args") or {}
        try:
            refusal = check_call(tool, args, table, skip=_skip_args(bindings, tool))
        except Exception as exc:
            logger.warning("tools.link_check_failed tool=%s err=%s", tool, type(exc).__name__)
            refusal = _fallback_refusal(tool, args)
        if refusal is not None:
            out[index] = refusal
    return out


def _fallback_refusal(tool: str, args: object) -> LinkRefusal | None:
    """检查出异常时(§4.12):参数里看得出编号 → 按拒绝处理;连这一步也出错 → 也按拒绝。"""
    try:
        found = LINK_ID_PREFIX in json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        found = True
    return LinkRefusal(tool, "", "unknown", LINK_ID_PREFIX) if found else None


LinkRewrite = Callable[[str, Mapping[str, Any]], tuple[dict[str, Any], tuple[str, ...]]]


def make_link_rewrite(
    links: Sequence[Mapping[str, Any]], bindings: Mapping[str, Any]
) -> LinkRewrite | None:
    """派发前最后一步用的换回函数;登记为空时 ``None``(什么都不换,零行为变化)。"""
    entries = valid_links(list(links))
    table = link_table(entries)
    if not table:
        return None

    def _rewrite(tool: str, args: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        if not is_mcp_tool(tool) or not isinstance(args, Mapping):
            return args, ()  # type: ignore[return-value]  # 不是映射:原样交给后面的校验报错
        note_written_urls(tool, args, entries)
        return rewrite_link_ids(tool, args, table, skip=_skip_args(bindings, tool))

    return _rewrite


def with_rewritten_links(
    details: dict[str, Any] | None, ids: Sequence[str]
) -> dict[str, Any] | None:
    """审计 details 加 ``rewritten_links``(只有编号);没换就原样返回。"""
    if not ids:
        return details
    return {**(details or {}), "rewritten_links": list(ids)}


def masked_error_text(
    tool: str, exc: BaseException, links: Sequence[Mapping[str, Any]]
) -> str | None:
    """MCP 服务报错的文字里有登记过的链接 → 遮过的文字;否则 ``None``(照旧用原文)。"""
    if not is_mcp_tool(tool) or not links:
        return None
    text = str(exc)
    masked = mask_links(text, links)
    return masked if masked != text else None


def has_mcp_tools(specs: Iterable[Any]) -> bool:
    return any(is_mcp_tool(str(getattr(spec, "name", ""))) for spec in specs)


# ---------------------------------------------------------------------------
# 结果遮回
# ---------------------------------------------------------------------------


def mask_links(text: str, links: Sequence[Mapping[str, Any]]) -> str:
    """文字里登记过的链接(几种写法)换回编号;没有登记或没有命中时原样返回。

    做法见 :mod:`orchestrator.tools.link_masking`(主机名预筛 + 按链接生成写法 + 结尾边界)。
    """
    if not text or not links:
        return text
    pairs = [(str(e["id"]), str(e["url"])) for e in valid_links(list(links))]
    masked, count = mask_text(text, pairs)
    if count:
        _use_total.labels(event="result_masked").inc(count)
    return masked
