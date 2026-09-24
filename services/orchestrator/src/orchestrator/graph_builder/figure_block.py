"""B-64 —— Path A 的渲染页段:把已看过的文档页挂到提示词尾部。

**只进这一次的提示词视图,从不落检查点** —— 与 ``builder._workspace_block_tail``
同一口径(CM-C4)。检查点里只有 ``state["viewed_figures"]`` 的 ref 字符串与
``state["figure_documents"]`` 的路径表。

这是我们比两份参考实现省掉的一整层:hermes / openclaw 把 base64 塞进消息,所以
「退役一张图」要重写多 MB 的消息,还得管检查点里的大块
(``drop_stale_api_content`` / ``_strip_images_from_tool_msg``)。我们存的是 ref
(``image_ref_block`` 返回 ``{"type": "image_ref", "ref": uri}``),字节由适配器在
**调用时**解析 —— 退役只是重建时少挂一个 ref。

回修第 3 轮把这一段从 ``builder`` 拆出来:``agent_node`` 要在**压缩判定之前**
造好它(把它的估算作为预留交给压缩器),在压缩之后才挂上去 —— 造与挂分成两步,
各自一个函数(:func:`figure_block_message` / :func:`with_figure_block`)。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage

from expert_work.common.conversation_channel import FIGURE_BLOCK_MARK, HIDE_FROM_UI
from orchestrator.multimodal import image_ref_block, parse_rendered_figure_ref
from orchestrator.tools.figure_lookup import FigureFreshness, rendered_page_freshness
from orchestrator.tools.read_page import document_sha
from orchestrator.tools.workspace_store import WorkspaceStore

logger = logging.getLogger(__name__)

#: 带像素进提示词的**槽**数上限。超出的退役成可见占位文字。
#:
#: 两份参考实现独立撞上同一个数 —— hermes ``_MAX_KEEP_TOOL_IMAGES = 3``、
#: openclaw ``keepLastAssistants: 3``。这是引用,不是本仓实测:我们没有数据说明
#: 3 对我们的模型/页型是否合适。
#:
#: 一个「槽」是一个 ``(<doc-sha>, _u<n>)``,不是一条 ref —— 同一页的新旧两版只占
#: 一个槽(见 :func:`_figure_slots`)。
FIGURE_KEEP_RECENT = 3

_FIGURE_BLOCK_HEADING = "# 你取来的文档页(这一轮可见)\n"


@dataclass(frozen=True)
class _Figure:
    """``viewed_figures`` 里一条通过了校验的 ref,拆出排槽与占位要用的几段。"""

    ref: str
    #: 文档路径派生的哈希 —— 同一条路径换了内容,这一段不变。
    doc_sha: str
    #: 模型调 ``read_page`` 时传的 ``units`` 编号。``(doc_sha, unit)`` 是一个槽。
    unit: int
    #: 内容 + dpi 的哈希。同槽不同值 = 文档在两次读之间被改过。
    render_sha: str
    #: 真实 PDF 页号,取自文件名 ``page-NN.jpg``。**不从** ``_u<n>`` 取:
    #: 对 docx 那是图的编号,不是页号。
    page: int
    #: 这张渲染页的**用户根相对**路径(``agents/<key>/`` 前缀在内)—— 核对新鲜度时要 stat 它。
    rel: str

    @property
    def slot(self) -> tuple[str, int]:
        return (self.doc_sha, self.unit)


def _own_figure(
    ref: object, *, tenant_id: UUID | None, user_id: UUID | None, agent_key: str
) -> _Figure | None:
    """``ref`` 是不是**本次 run 自己**渲出来的一页;是就拆开,不是就 ``None``。

    三项比对缺一不可 —— 照 :mod:`orchestrator.tools.vision` 的写法。今天唯一写
    ``viewed_figures`` 的是 ``read_page``(MCP 工具的结果不带 ``state_updates``);
    但这个通道在 ``TOOL_ALLOWED_STATE_KEYS`` 里对工具开放,而下游
    ``NasWorkspaceImageResolver`` 的 tenant/user **取自 ref 自身**、不跟调用方比对,
    所以这里仍按不可信输入校验:不拦,就是拿别的租户 / 用户 / agent 的文件当自己的
    看。三个身份都来自调用方传入的**本次 run 的** config。

    另外要求 ref 是 ``read_page`` 的**产出形状**
    (:func:`~expert_work.persistence.is_rendered_figure_rel`):排槽与页号都从
    这个形状里读,形状不对就没有可信的槽和页号。三项都对得上、但形状不对的 ref
    (比如模型自己写进工作区、尾巴凑成 ``_u3/page-3.jpg`` 的图)不是这个通道该装
    的东西 —— 它是模型自己的文件,不是平台渲染的页。
    """
    if not isinstance(ref, str) or tenant_id is None or user_id is None:
        return None
    # 解析、剥作用域前缀、形状判据、页号 —— 都在 ``parse_rendered_figure_ref`` 一处。
    figure = parse_rendered_figure_ref(ref)
    if figure is None:
        return None
    parsed = figure.workspace
    if (
        parsed.tenant_id != tenant_id
        or parsed.user_id != user_id
        # 空串 ↔ None 是两套代码分别表达「没绑 agent」的写法,折成同一个值再比。
        or parsed.agent_key != (agent_key or None)
    ):
        return None
    return _Figure(
        ref=ref,
        doc_sha=figure.doc_sha,
        unit=figure.unit,
        render_sha=figure.render_sha,
        page=figure.page,
        rel=parsed.rel,
    )


def _figure_slots(figures: Sequence[_Figure]) -> list[tuple[_Figure, list[_Figure]]]:
    """按槽归并:每槽 ``(当前版本, 更早的其他内容版本)``,槽按当前版本的位置排。

    「当前版本」= 这个槽在列表里**最靠后**的那一条。``viewed_figures`` 按最近一次
    看到排序(``state._merge_viewed_figures``),所以它就是最近一次读到的那一版。
    注意这不等于「磁盘上此刻的文档」:读完之后文档又被改了而没有重读,这里不知道。

    同槽、同 ``render_sha`` 的更早条目(跨 run 重读一份没改过的文档:run_id 段不同,
    内容哈希相同)**不单独列出**:它画的就是当前版本那张图,并没有什么丢了。
    """
    last_index = {f.slot: i for i, f in enumerate(figures)}
    slots: list[tuple[_Figure, list[_Figure]]] = []
    for slot, idx in sorted(last_index.items(), key=lambda item: item[1]):
        current = figures[idx]
        older: dict[str, _Figure] = {}
        for f in figures[:idx]:
            if f.slot == slot and f.render_sha != current.render_sha:
                older[f.render_sha] = f
        slots.append((current, list(older.values())))
    return slots


def _unit_ranges(units: Sequence[int]) -> str:
    """``[1, 2, 3, 7, 9, 10]`` → ``"1-3、7、9-10"``(先去重排序)。"""
    ordered = sorted(set(units))
    runs: list[tuple[int, int]] = []
    for unit in ordered:
        if runs and unit == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], unit)
        else:
            runs.append((unit, unit))
    return "、".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _document_path(doc_sha: str, documents: Mapping[str, object], agent_key: str) -> str | None:
    """``<doc-sha>`` 对应的、模型能再传给 ``read_page`` 的路径;对不上就 ``None``。

    ``figure_documents`` 是工具可写的通道,所以不直接采信:用 ``read_page`` 自己那对
    函数把路径**正向**算一遍哈希,与键相同才用。对不上(或没记)就不给路径,只说
    「用你当初传的那个路径」—— 宁可少说,不说错。
    """
    path = documents.get(doc_sha)
    if not isinstance(path, str) or document_sha(path, agent_key=agent_key) != doc_sha:
        return None
    return path


def _quote_path(path: str) -> str:
    """把路径渲成一个带引号的字面量,放进提示词文字里。

    ``json.dumps`` 挡住了引号与换行;回修第 4 轮 —— 再把 U+2028 / U+2029(行分隔符 /
    段分隔符,``json.dumps(ensure_ascii=False)`` 原样放行)也转义掉:模型可能把它们
    当换行读,而文件名来自用户上传、第三方附件或模型自建,不可信。
    """
    return (
        json.dumps(path, ensure_ascii=False)
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _placeholder_lines(
    slots: Sequence[tuple[_Figure, list[_Figure]]],
    live_from: int,
    documents: Mapping[str, object],
    agent_key: str,
) -> list[str]:
    """退役与旧版本的可见文字 —— **按文档折叠**,每份文档一行、路径只写一次。

    有界于什么(回修第 3 轮):行数 = 有退役或旧版本的**文档数**;每行里的编号
    压成区间,区间个数 ≤ 这份文档里被看过的**不同编号数**,也 ≤ ⌈这份文档编号总数 / 2⌉
    (两个相邻区间之间至少隔一个没看过的编号)。同一编号重看、改版多少次都只占一次。
    **文档数本身没有上界** —— 一条会话读过多少份不同的文档,这里就有多少份的行;
    它随「读过的文档」增长,不再随「看过的页次」增长。
    """
    retired: dict[str, list[int]] = {}
    old_versions: dict[str, list[int]] = {}
    for i, (current, older) in enumerate(slots):
        if i < live_from:
            retired.setdefault(current.doc_sha, []).append(current.unit)
        if older:
            old_versions.setdefault(current.doc_sha, []).append(current.unit)
    lines: list[str] = []
    for doc_sha in dict.fromkeys([*retired, *old_versions]):
        path = _document_path(doc_sha, documents, agent_key)
        # 回修第 4 轮 —— 路径每份文档只写一次:两件事并进同一行。
        label = f"文档 {_quote_path(path)}" if path is not None else "一份文档(路径没记下来)"
        path_hint = "这个路径" if path is not None else "你当初调用 read_page 时传的那个路径"
        parts: list[str] = []
        if doc_sha in retired:
            parts.append(
                f"编号 {_unit_ranges(retired[doc_sha])} 的图已退出上下文"
                f"(需要重看就调用 read_page,path 填{path_hint},units 填要看的编号)"
            )
        if doc_sha in old_versions:
            parts.append(
                f"编号 {_unit_ranges(old_versions[doc_sha])} 有旧版本(文档之后被改过),"
                "旧版本不再展示,以新版本为准"
            )
        lines.append(f"[{label}:" + ";".join(parts) + "。]")
    return lines


def _bracket_path(path: str) -> str:
    """「路径」—— 与 ``read_page`` 回执同一种括法;行分隔符照 :func:`_quote_path` 转义。"""
    return "「" + path.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + "」"


#: 窗口里某一页**这一轮不附图**的原因 → 给模型看的那半句(``{unit}`` 是编号区间)。
_WITHHELD_REASONS: dict[FigureFreshness, str] = {
    "stale": "在你看过之后被改过或替换了,编号 {units} 的渲染页已过期",
    "missing": "已不在工作区里,编号 {units} 的渲染页核对不了是不是它现在的内容",
    "unknown": "核对不了在渲染之后有没有被改过,编号 {units} 的渲染页",
}


def _withheld_lines(
    withheld: Sequence[tuple[_Figure, FigureFreshness]],
    documents: Mapping[str, object],
    agent_key: str,
) -> list[str]:
    """终审 #1 —— 窗口里没附图的页,按 (文档, 原因) 折成一行,写明路径与编号怎么拿回来。"""
    grouped: dict[tuple[str, FigureFreshness], list[int]] = {}
    for figure, freshness in withheld:
        grouped.setdefault((figure.doc_sha, freshness), []).append(figure.unit)
    lines: list[str] = []
    for (doc_sha, freshness), units in grouped.items():
        path = _document_path(doc_sha, documents, agent_key)
        label = f"文档{_bracket_path(path)}" if path is not None else "一份文档(路径没记下来)"
        path_hint = (
            _bracket_path(path) if path is not None else "你当初调用 read_page 时传的那个路径"
        )
        reason = _WITHHELD_REASONS.get(freshness, _WITHHELD_REASONS["unknown"])
        ranges = _unit_ranges(units)
        lines.append(
            f"[{label}{reason.format(units=ranges)},这一轮不附图 —— 需要的话重新调用 read_page,"
            f"path 填{path_hint},units 填 {ranges}。]"
        )
    return lines


def _window(
    viewed: Sequence[object], *, tenant_id: UUID | None, user_id: UUID | None, agent_key: str
) -> tuple[list[tuple[_Figure, list[_Figure]]], int]:
    """通过三项比对的 ref 按槽归并,外加窗口起点 —— 建块与核对新鲜度看的是同一个窗口。"""
    figures: list[_Figure] = []
    for ref in viewed:
        fig = _own_figure(ref, tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)
        if fig is not None:
            figures.append(fig)
    if len(figures) != len(viewed):
        logger.warning(
            "figure_block.refs_rejected rejected=%d total=%d",
            len(viewed) - len(figures),
            len(viewed),
        )
    slots = _figure_slots(figures)
    return slots, max(len(slots) - FIGURE_KEEP_RECENT, 0)


async def figure_freshness(
    store: WorkspaceStore,
    *,
    viewed: Sequence[object],
    documents: Mapping[str, object],
    tenant_id: UUID | None,
    user_id: UUID | None,
    agent_key: str,
) -> dict[str, FigureFreshness]:
    """终审 #1 —— 窗口里每一张(至多 :data:`FIGURE_KEEP_RECENT` 张)的新鲜度,按 ref 索引。

    判据是 ``figure_lookup`` 那一条(:func:`~orchestrator.tools.figure_lookup.render_is_stale`),
    不在这里写第二份。路径没记下来(旧会话)时核对不了 → ``"unknown"``。
    """
    if tenant_id is None or user_id is None:
        return {}
    slots, live_from = _window(viewed, tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)
    result: dict[str, FigureFreshness] = {}
    for current, _older in slots[live_from:]:
        path = _document_path(current.doc_sha, documents, agent_key)
        if path is None:
            result[current.ref] = "unknown"
            continue
        result[current.ref] = await rendered_page_freshness(
            store,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=agent_key,
            path=path,
            render_rel=current.rel,
        )
    return result


def is_figure_block(msg: BaseMessage) -> bool:
    """这条消息是不是平台注入的渲染页段。"""
    return isinstance(msg, HumanMessage) and bool(
        (msg.additional_kwargs or {}).get(FIGURE_BLOCK_MARK)
    )


def figure_block_message(
    *,
    viewed: Sequence[object],
    documents: Mapping[str, object],
    supports_vision: bool,
    tenant_id: UUID | None,
    user_id: UUID | None,
    agent_key: str,
    freshness: Mapping[str, FigureFreshness] | None = None,
) -> HumanMessage | None:
    """造这一轮的渲染页段;没有可挂的就 ``None``。

    **新鲜度(终审 #1)。** ``freshness`` 是 :func:`figure_freshness` 对窗口里每张的
    核对结果。给了它,窗口里不是 ``"fresh"`` 的页**一律不附图**,换成可见文字(文档
    被改过 / 不在了 / 核对不了 + 怎么拿回来)—— 同名上传覆盖了原文档时,不能拿旧文档
    的页顶着当前路径的名字给模型看。``None`` = 调用方没有工作区存储、没法核对(单测;
    没接 NAS 的部署,那里工作区 ref 本来就解析不了),行为与之前相同。

    **退役是替换不是删除。** 超窗的页换成可见文字,模型看得见这里原来有图、也看得见
    怎么拿回来(路径 + 编号再调 ``read_page``:同一 run 里重读拿到逐字相同的 ref,
    ``_merge_viewed_figures`` 把它挪到队尾,它就回到窗口里)。同一页的旧版本同理,
    可见地标成旧版本,不静默丢。

    **不挂像素的 ref 有两类,待遇不同:** 超窗的、旧版本的 —— 是模型自己读过的
    东西,留可见文字;没通过 :func:`_own_figure` 的 —— 不是本次 run 自己的渲染页,
    **连文字都不留**(那串 ref 不是平台为这次 run 渲的),只打一条 warning。

    ``supports_vision`` 为假时不造块:主模型看不了图。那条路由要么走 ``ask_image``
    (Path B,字节从不进主上下文),要么两样都没有(``read_page`` 在 ``"none"`` 档
    不渲染,见 ``tools.read_page``)—— 两种情况挂了它都看不见,只会白烧 token。

    适配器(``split_human_content``)把一条消息里的全部文字拼在前、全部图片排在
    后,所以不能在每张图前面插一句说明 —— 文字段末尾按次序列出后面几张是哪几页。
    """
    if not supports_vision or not viewed:
        return None
    slots, live_from = _window(viewed, tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)
    if not slots:
        return None
    lines = _placeholder_lines(slots, live_from, documents, agent_key)
    # 回修第 4 轮 —— 窗口里的几张按文档归到一起(文档之间按首次出现排,文档内保持
    # 原次序),路径每份文档只写一次;图片块按同一次序挂,文字与图一一对应。
    in_window = [current for current, _ in slots[live_from:]]
    if freshness is not None:
        withheld = [(f, freshness.get(f.ref, "unknown")) for f in in_window]
        withheld = [(f, state) for f, state in withheld if state != "fresh"]
        lines.extend(_withheld_lines(withheld, documents, agent_key))
        in_window = [f for f in in_window if freshness.get(f.ref) == "fresh"]
    doc_order = list(dict.fromkeys(f.doc_sha for f in in_window))
    live = sorted(in_window, key=lambda f: doc_order.index(f.doc_sha))
    groups: list[str] = []
    for doc_sha in doc_order:
        path = _document_path(doc_sha, documents, agent_key)
        pages = "、".join(f"第 {f.page} 页" for f in live if f.doc_sha == doc_sha)
        groups.append(f"{_quote_path(path)} {pages}" if path is not None else pages)
    if live:
        lines.append(f"下面依次附上 {len(live)} 张:" + ";".join(groups) + "。")
    blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _FIGURE_BLOCK_HEADING + "\n".join(lines)}
    ]
    blocks.extend(image_ref_block(f.ref) for f in live)
    # ``HIDE_FROM_UI`` = 不进任何面向用户/第三方的视图, 也不开新一段;
    # ``FIGURE_BLOCK_MARK`` 是更窄的一层, 让 :func:`with_figure_block` 的剔除只认这一段。
    return HumanMessage(
        content=blocks,
        additional_kwargs={HIDE_FROM_UI: True, FIGURE_BLOCK_MARK: True},
    )


def with_figure_block(
    messages: Sequence[BaseMessage], block: HumanMessage | None
) -> list[BaseMessage]:
    """剔掉提示词视图里更早的渲染页段,再把 ``block``(若有)挂到尾部。返回新列表。

    **挂尾部**,与工作区快照段同一个位置口径:尾部是这一轮唯一不可能夹在
    ``tool_call`` ↔ ``tool_result`` 之间的位置。
    """
    kept = [m for m in messages if not is_figure_block(m)]
    return kept if block is None else [*kept, block]


def figure_block_tail(
    messages: Sequence[BaseMessage],
    *,
    viewed: Sequence[object],
    documents: Mapping[str, object] | None = None,
    supports_vision: bool,
    tenant_id: UUID | None,
    user_id: UUID | None,
    agent_key: str,
    freshness: Mapping[str, FigureFreshness] | None = None,
) -> list[BaseMessage]:
    """:func:`figure_block_message` + :func:`with_figure_block` 一步做完(测试用这个入口)。"""
    block = figure_block_message(
        viewed=viewed,
        documents=documents or {},
        supports_vision=supports_vision,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=agent_key,
        freshness=freshness,
    )
    return with_figure_block(messages, block)
