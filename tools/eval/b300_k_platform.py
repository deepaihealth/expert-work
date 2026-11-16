"""B-300 第 9 版评测对**代码侧新接口**的全部假设 —— 集中在这一个文件(设计 2026-11-15 §12.5 K-E)。

代码侧(分支 ``feat/b300-write-retire-split``)与评测并行做,写这份脚本时它的新接口还没合。下面每一条
都按设计 §7 写的形状实现;接口合进来之后对不上的,只改这里(和 ``b300_inpod.py`` 里标了
「接口假设」的那一处 —— 那个文件要整份喂进 pod,不能 import 这里)。:data:`ASSUMPTIONS` 是给人看的
清单,报告的 ``disclosures`` 里原样带上。

平台包只在函数里按需 import(``b300_k_checks`` / ``b300_k_scenarios`` 是纯函数,不 import 这里)。
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

ASSUMPTIONS: tuple[str, ...] = (
    "A1 存储 supersede(*, tenant_id, user_id, new_item, old_ids=(), identical_ids=(), "
    "flag_summary_ids=(), link_id=None)(§7.8;已对着代码核实)。评测用「签名里有 old_ids」判断被测"
    "代码是不是第 9 版(capabilities['retire_split']);pod 里种「已被取代」的种子、给汇总行打标"
    "也走它。",
    "A2 supersede 返回 SupersedeResult(item / inserted / retired / refused / flagged / "
    "collision_replaced),不再返回 None;种子取 result.item.id。老签名(old_id、返回单行或 None)"
    "只为在第 9 版之前的提交上跑基线保留。",
    "A3 记忆行的字段不变(§7.8「不需要迁移」):superseded_by / supersedes / invalid_at / "
    "consolidated_from / consolidated_into / status / review_flagged_at / occurred_on / "
    "confidence / origin。读库断言全靠这几列。",
    "A4 supersedes 的含义(§7.8;8b2fc1f48 核实):普通行上,被指的行仍有效 = 只是连着(并存、"
    "汇总行待重写),被指的行已作废 = 替掉了它;**汇总行自己的 supersedes = 打标的依据那一行**"
    "(用户的说法),不表示汇总行替掉了谁,重写出的新汇总行这一列为空。评测只读新写的普通行上的"
    "这一列,从不把汇总行的 supersedes 当成「它替掉了谁」。历史行 = 落库时已作废(invalid_at 有值)、"
    "superseded_by 指向一条有效行。",
    "A5 控制台更正过的 = confidence >= 1.0(effective_origin;§6.3 没改)。种子直接写 1.0。",
    "A6 汇总行与成员用 write_consolidated(tenant_id, user_id, content, embedding, source_ids, "
    "source_hashes) 种(第 9 版没改它);汇总行 status='consolidated'、带 consolidated_from,成员带 "
    "consolidated_into。",
    "A7 重写标记 = 汇总行的 review_flagged_at;由 supersede(flag_summary_ids=...) 打(只有用户一方的"
    "新行打得上;清单里每条符合条件的汇总行都打,flagged 可以有多条)。重写之后旧汇总行作废、"
    "superseded_by 指向新汇总行(或用户最晚的那条新说法 / 撞上的那条),新汇总行的 "
    "consolidated_from 与旧行相同、成员的 consolidated_into 改指新汇总行。",
    "A8 当场重写的入口(§7.12 第 2 步):control_plane.memory_consolidator.SummaryRewriter("
    "memory_store, aux_model, embedder, audit_logger, default_aux_model_name).rewrite_for_user("
    "tenant_id, user_id) -> list[str](各条的结果:rewritten / emptied / unchanged / collided / "
    "stale / corrected_member / blocked / abandoned / failed)。它从不向外抛:模型调用出错记成 "
    "failed。评测给它的审计是空桩(只有新句子被写入扫描拦下时才用到)。",
    "A9 去重合并的材料是一条 JSON(candidates[].index / content / source / existing[].id / "
    'content,短编号 e0、e1……);回复是 {"ops": [{index, op, target_id, replaces, restates}]}'
    "(已核实)。评测只读这两张清单与 op / target_id,按候选的 index 对回材料。",
    "A10 去重合并指令的开头是 'You compare newly extracted memories'(已核实),"
    "b300_live.classify_call 认得出;旧的 'You reconcile newly extracted memories' 也还认。",
    "A11 来源的显示文字由 orchestrator.context.memory_render.ORIGIN_LABELS(有效来源 → 文字)给;"
    "评测用它把材料里候选的 source 倒回来源。",
    "A12 写回入口:orchestrator.graph_builder.memory.flush_messages_with_outcome(..., "
    "segment=SegmentStart(message_index=...));K-late 靠「起点 0、只在最后写回一次」让一个写回"
    "覆盖两轮。候选要有 line_at 才退得掉旧行:消息的发送时间由 _build_human_message 盖,turn 由"
    "提炼模型给 —— 模型没给 turn 时什么都退不掉(retire_refused_unknown_time)。",
    "A13 不退的原因只从库里的结果和去重合并的回复推(模型列了、那一行却仍有效);"
    "retire_refused_* 计数是进程级的 Prometheus 计数,评测并发跑单元时分不到每个单元,不读。",
    "A14 写回入口把内部的错吞掉、只回 failed,错由 logging 记在 "
    "'orchestrator.graph_builder.memory' 这个 logger 上、带 exc_info;去重合并回复解析失败的异常名"
    "是 ReconcileReplyError(已核实)。评测挂一个只记异常名的 handler 来数它。",
)

#: A8:当场重写的入口(模块, 类, 方法)。
REWRITE_ENTRY = (
    "control_plane.memory_consolidator",
    "SummaryRewriter",
    "rewrite_for_user",
)
#: A1:新 ``supersede`` 的清单参数名。
SUPERSEDE_LIST_PARAM = "old_ids"
#: A9:回复里的两张清单。
LIST_FIELDS = ("replaces", "restates")


#: A14:写回入口记错的 logger,与去重合并回复解析失败的异常名。
WRITEBACK_LOGGER = "orchestrator.graph_builder.memory"
RECONCILE_REPLY_ERROR = "ReconcileReplyError"
_ERROR_SINK: ContextVar[list[str] | None] = ContextVar("b300_writeback_errors", default=None)


class _ErrorNames(logging.Handler):
    """只记异常的类名(连同它的起因),记进当前这个单元的清单;不记任何正文。"""

    def emit(self, record: logging.LogRecord) -> None:
        sink = _ERROR_SINK.get()
        exc = record.exc_info[1] if record.exc_info else None
        while sink is not None and exc is not None:
            sink.append(type(exc).__name__)
            exc = exc.__cause__ or exc.__context__


_HANDLER = _ErrorNames()


@contextlib.contextmanager
def writeback_errors() -> Iterator[list[str]]:
    """这一次写回里被吞掉的错的异常名(A14)。并发跑的单元各记各的(``ContextVar``)。"""
    logger = logging.getLogger(WRITEBACK_LOGGER)
    if _HANDLER not in logger.handlers:
        logger.addHandler(_HANDLER)
    names: list[str] = []
    token = _ERROR_SINK.set(names)
    try:
        yield names
    finally:
        _ERROR_SINK.reset(token)


def capabilities(store: Any) -> dict[str, bool]:
    """被测代码是不是第 9 版(A1),有没有当场重写的入口(A8)。"""
    params = inspect.signature(store.supersede).parameters
    return {
        "retire_split": SUPERSEDE_LIST_PARAM in params,
        "summary_rewrite": _rewrite_method() is not None,
    }


def _rewrite_method() -> Any:
    module_name, class_name, method = REWRITE_ENTRY
    try:
        module = __import__(module_name, fromlist=[class_name])
    except ImportError:
        return None
    return getattr(getattr(module, class_name, None), method, None)


def origin_labels() -> dict[str, str]:
    """A11:显示文字 → 有效来源(读不到就空,检查那边按「不知道来源」处理)。"""
    try:
        from orchestrator.context.memory_render import ORIGIN_LABELS
    except ImportError:
        return {}
    return {str(label): str(origin) for origin, label in ORIGIN_LABELS.items()}


# --------------------------------------------------------------------------- 种子(本机内存库)


def _item(
    seed: Any, *, tenant_id: UUID, user_id: UUID, vector: Sequence[float], now: datetime
) -> Any:
    from expert_work.protocol import MemoryItem

    created = now - timedelta(days=int(seed.days_ago))
    fields: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "kind": seed.kind,
        "content": seed.content,
        "embedding": tuple(float(v) for v in vector),
        "confidence": float(seed.confidence),  # A5:1.0 = 控制台更正过的
        "importance": 0.7,
        "created_at": created,
        "last_used_at": created,
        "valid_at": created,
    }
    if seed.origin is not None:
        fields["origin"] = seed.origin
    occurred = getattr(seed, "occurred_days_ago", None)
    if occurred is not None:
        fields["occurred_on"] = (now - timedelta(days=int(occurred))).date()
    return MemoryItem(**fields)


async def seed_store(
    store: Any, embedder: Any, seeds: Sequence[Any], *, tenant_id: UUID, user_id: UUID
) -> dict[str, str]:
    """K 组的种子(``b300_k_scenarios.KSeed``)→ 库:先写普通行(带各自的日子、来源、置信度、事件
    日期),再写汇总行(A6),最后写「连着另一条」的行(A4:``supersedes`` 指向一条仍有效的行)。
    返回 种子键 → 记忆 id。并存的两条就是两条普通行,不需要特别处理。"""
    from expert_work.persistence.memory import hash_content

    if not seeds:
        return {}
    now = datetime.now(UTC)
    vectors = await embedder.embed([s.content for s in seeds], tenant_id=tenant_id)
    vector_of = {s.key: v for s, v in zip(seeds, vectors, strict=True)}
    items = {
        s.key: _item(s, tenant_id=tenant_id, user_id=user_id, vector=vector_of[s.key], now=now)
        for s in seeds
    }
    summaries = [s for s in seeds if getattr(s, "summary_of", ())]
    linked = [s for s in seeds if getattr(s, "linked_to", None)]
    late = {s.key for s in (*summaries, *linked)}
    await store.write([items[s.key] for s in seeds if s.key not in late])
    ids = {s.key: str(items[s.key].id) for s in seeds if s.key not in late}
    for s in summaries:
        members = [items[k] for k in s.summary_of]
        row = await store.write_consolidated(
            tenant_id=tenant_id,
            user_id=user_id,
            content=s.content,
            embedding=vector_of[s.key],
            source_ids=[m.id for m in members],
            source_hashes={m.id: hash_content(m.content) for m in members},
        )
        ids[s.key] = str(row.id)
    for s in linked:
        row = items[s.key].model_copy(update={"supersedes": UUID(ids[s.linked_to])})
        await store.write([row])
        ids[s.key] = str(row.id)
    return ids


def dump_row(m: Any) -> dict[str, Any]:
    """读库断言要的列(A3),在 ``b300_live._dump`` 之外多出来的那几样。"""

    def text(value: Any) -> Any:
        return None if value is None else str(value)

    return {
        "supersedes": text(getattr(m, "supersedes", None)),
        "invalid_at": text(getattr(m, "invalid_at", None)),
        "status": text(getattr(m, "status", None)),
        "consolidated_into": text(getattr(m, "consolidated_into", None)),
        "consolidated_from": [str(x) for x in getattr(m, "consolidated_from", ()) or ()],
        "review_flagged_at": text(getattr(m, "review_flagged_at", None)),
    }


# --------------------------------------------------------------------------- 当场重写(A8)


class _Stub:
    """构造重写器时要给的审计(只有新句子被写入扫描拦下时才用到):任何方法都是空的异步函数。"""

    def __getattr__(self, _name: str) -> Any:
        async def nothing(*_args: Any, **_kwargs: Any) -> None:
            return None

        return nothing


def _outcome(value: Any) -> str:
    return str(getattr(value, "outcome", value))


async def summary_state(
    store: Any, *, tenant_id: UUID, user_id: UUID, summary_id: str | None
) -> dict[str, bool]:
    """写回落完库、重写还没开始时,种下的那条汇总行还有效吗、打了重写标记吗(A7)。"""
    rows = await store.list_for_user(tenant_id=tenant_id, user_id=user_id, limit=500)
    row = next((m for m in rows if str(m.id) == str(summary_id)), None)
    live = (
        row is not None
        and row.deleted_at is None
        and row.invalid_at is None
        and row.superseded_by is None
    )
    return {
        "summary_live": live,
        "summary_flagged": row is not None and getattr(row, "review_flagged_at", None) is not None,
    }


async def rewrite_flagged(
    store: Any,
    embedder: Any,
    aux_model: Any,
    *,
    tenant_id: UUID,
    user_id: UUID,
    model: str,
    summary_id: str | None = None,
) -> dict[str, Any]:
    """写回任务收尾时的那一步(§7.12 第 2 步):对这个用户打了标的汇总行各调一次重写。入口不在
    (A8 对不上、或代码还没合)→ ``ran`` 为假,不调任何模型。入口在而调用出错 → 照抛。``before``
    = 重写之前那条汇总行的状态(「直接作废」「打了标」两条线读它)。"""
    before = await summary_state(store, tenant_id=tenant_id, user_id=user_id, summary_id=summary_id)
    if _rewrite_method() is None:
        return {"ran": False, "outcomes": [], "before": before}
    from control_plane.memory_consolidator import SummaryRewriter, make_consolidator_embedder

    rewriter = SummaryRewriter(
        memory_store=store,
        aux_model=aux_model,
        embedder=make_consolidator_embedder(embedder),
        audit_logger=_Stub(),  # type: ignore[arg-type]
        default_aux_model_name=model,
    )
    method = getattr(rewriter, REWRITE_ENTRY[2])
    result = await method(tenant_id=tenant_id, user_id=user_id)
    rows = result if isinstance(result, list | tuple) else ([] if result is None else [result])
    return {"ran": True, "outcomes": [_outcome(r) for r in rows], "before": before}


class GuardedAux:
    """整理器要的辅助模型(``ConsolidatorAuxModel`` 的调用形状)→ 评测的记账传输:每次调用照常
    先预留后发送,记下 token。``transport`` 是 ``b300_live.ChatTransport``。"""

    def __init__(self, transport: Any, item: str, phase: str) -> None:
        self._transport, self._item, self._phase = transport, item, phase
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *, prompt: str, output_schema: Any = None, **_: Any) -> Any:
        from control_plane.memory_consolidator import ConsolidatorLLMReply

        text, usage = await self._transport.complete(
            [{"role": "user", "content": prompt}],
            item=f"{self._item}|rewrite",
            phase=self._phase,
            json_mode=output_schema is not None,
        )
        tokens = (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        self.calls.append(
            {
                "kind": "rewrite",
                "reply": text,
                "input_tokens": tokens[0],
                "output_tokens": tokens[1],
            }
        )
        parsed = _json(text) if output_schema is not None else None
        return ConsolidatorLLMReply(
            text=text,
            model=self._transport.model,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            parsed=parsed if isinstance(parsed, dict) else None,
        )


# --------------------------------------------------------------------------- 去重合并的回复(A9)


def _decisions(node: Any) -> list[Mapping[str, Any]]:
    """回复 JSON 里所有「带 op 或带清单」的对象(不假设它们挂在哪个键下面)。"""
    found: list[Mapping[str, Any]] = []
    if isinstance(node, Mapping):
        if "op" in node or any(k in node for k in LIST_FIELDS):
            found.append(node)
        for value in node.values():
            found += _decisions(value)
    elif isinstance(node, list):
        for value in node:
            found += _decisions(value)
    return found


def _json(text: str) -> Any:
    try:
        return json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (ValueError, TypeError):
        return None


def parse_reconcile(material: str, reply: str) -> list[dict[str, Any]]:
    """一次去重合并调用 → 每条候选一项:``content`` / ``source``(显示文字)/ ``op`` / ``target``
    (重复的那一条的正文)/ ``replaces`` / ``restates``(被列的已有记忆的正文)。纯函数;材料或回复
    读不懂 = 空。编号不认识的(模型编的)丢掉。"""
    data, answer = _json(material), _json(reply)
    candidates = data.get("candidates") if isinstance(data, Mapping) else None
    if not isinstance(candidates, list):
        return []
    by_index = {c.get("index"): c for c in candidates if isinstance(c, Mapping)}
    decisions = {d.get("index", d.get("id")): d for d in _decisions(answer)}
    out: list[dict[str, Any]] = []
    for index, cand in by_index.items():
        existing = {
            str(e.get("id")): str(e.get("content", ""))
            for e in cand.get("existing") or []
            if isinstance(e, Mapping)
        }
        decision = decisions.get(index) or decisions.get(str(index)) or {}
        lists = {}
        for name in LIST_FIELDS:
            raw = decision.get(name)
            listed = raw if isinstance(raw, list) else []
            lists[name] = [existing[i] for i in listed if isinstance(i, str) and i in existing]
        out.append(
            {
                "content": str(cand.get("content", "")),
                "source": str(cand.get("source", "")),
                "op": str(decision.get("op", "")),
                "target": existing.get(str(decision.get("target_id"))),
                **lists,
            }
        )
    return out
