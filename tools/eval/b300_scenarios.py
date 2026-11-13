"""B-300 评测的场景(设计 ``docs/superpowers/specs/2026-11-11-b300-memory-provenance-design.md``
§8.1 / §8.2 / §8.3 / §8.7)。纯数据 + 生成器,不调任何模型、不连网。

* **内容全部合成**:10 条「事实」(:data:`FACTS`)按 5 个领域(客服、法务、编程、销售、健康)各 2 条,
  覆盖 §8.1 的 8 种内容形态,值类(数值 / 标识符 / 日期)占 4 条、不过半;没有租户名、没有客户内容。
  每条事实有中文、英文两套文字;**哪个场景用哪种语言按序号交替**(本脚本安排:设计没定语言配比,
  §3 只要求「跟随用户的语言」,两种文字都要量到)。
* **A 组 12 类**(§8.2:P1、P2~P5、P6、P7、P8、P9 与另 3 类)每类 10 个场景 = 每领域 2 个,
  也就是 10 条事实各一个;给的是一段合成对话(用户的话、工具调用与结果、助手最终回答),由
  ``b300_live.run_extraction`` 喂给生产的提炼入口。
* **B 组 7 类**(§8.3)每类 10 个场景;给的是要种进记忆库的记忆和一轮对话(大多带一份上传的
  合成记录)。
* **C 组 5 类**(§8.7,按用户 11-13「缩小新题规模,守住 ¥400」)每类 5 个场景 = 每领域 1 个;
  两条事实轮着用,25 个场景合起来 8 种形态都有。

``x_key`` / ``y_key`` 是程序粗查用的关键串(去空白、小写后做包含判断),只当筛子;
「有没有写明出处」「是否把旧值当成现在的」一律交给判官。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from b300_facts import (
    DOMAINS,
    FACT_BY_KEY,
    FACTS,
    FORMS,
    INCIDENT_CONFIDENCE,
    LANGS,
    TEMPLATES,
    VALUE_FORMS,
    Fact,
    FactText,
)

__all__ = [
    "DOMAINS",
    "FACTS",
    "FORMS",
    "INCIDENT_CONFIDENCE",
    "LANGS",
    "VALUE_FORMS",
    "Fact",
    "FactText",
    "Scenario",
    "Seed",
    "Turn",
]

A_CLASSES = (
    "P1",  # 用户自己说全了
    "P2",  # 工具结果后助手复述
    "P3",  # 主模型直接看附件(图)
    "P4",  # 对接方附带的外部内容
    "P5",  # 助手自己推断
    "P6",  # 助手问、用户只答一个词
    "P7",  # 助手说、用户只回「对」
    "P8",  # 用户只是提到:问句 / 猜测 / 假设 / 转述
    "P9",  # 短更新
    "A10",  # 用户后来自己说全了
    "A11",  # 已有用户说的内容时助手得出不同内容
    "A12",  # 同一对话连写 5 轮
)
B_CLASSES = ("B1", "B2", "B3", "B4", "B5", "B6", "B7")
C_CLASSES = ("C1", "C2", "C3", "C4", "C5")
#: 每类场景数(§8.1:每类 10 个 = 每领域 2 个;§8.7:每类 5 个 = 每领域 1 个)。
SCENARIOS_PER_CLASS = {"A": 10, "B": 10, "C": 5}
#: A12 每段对话写回几次(§8.2「同一对话连写 5 轮」)。
A12_ROUNDS = 5


@dataclass(frozen=True)
class Turn:
    """一轮:用户的话(+ 附件),助手的工具调用与最终回答(A 组用;B / C 组只有用户的话)。"""

    user: str
    reply: str = ""
    #: (工具名, 工具结果)
    tool_calls: tuple[tuple[str, str], ...] = ()
    images: int = 0
    #: (文件名, 文本内容)
    documents: tuple[tuple[str, str], ...] = ()
    external: tuple[str, ...] = ()


@dataclass(frozen=True)
class Seed:
    """往记忆库里种的一条记忆。``origin`` 用设计 §6.1 的值;``None`` = 来源空。"""

    key: str
    kind: str
    content: str
    origin: str | None
    confidence: float
    days_ago: int
    #: 被哪一条种子取代(C5:X 已被 Y 取代)。
    superseded_by: str | None = None


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    group: str
    cls: str
    domain: str
    form: str
    lang: str
    fact_key: str
    x_key: str
    y_key: str
    #: A 组:一段对话的各轮;B / C 组:每段对话一轮(C4 三段)。
    turns: tuple[Turn, ...] = ()
    conversations: tuple[Turn, ...] = ()
    seeds: tuple[Seed, ...] = ()
    #: A 组:哪几轮之后各做一次写回(下标从 0 起)。
    writebacks: tuple[int, ...] = ()
    #: 给判官看的「这一类期望什么」(固定文字,按类)。
    expect: str = ""
    incident: bool = False
    variant: str = ""


def _text(fact: Fact, lang: str) -> FactText:
    return fact.texts[lang]


def _fmt(lang: str, key: str, **kw: str) -> str:
    return TEMPLATES[lang][key].format(**kw)


# --------------------------------------------------------------------------- A 组

_A_EXPECT = {
    "P1": "pass 1 extracts the user's own statement",
    "P2": "content only from a tool result: never in pass 1; pass 2 states its source",
    "P3": "content only from an attachment the model saw: never in pass 1; pass 2 states source",
    "P4": "content only from external data: never in pass 1; pass 2 states its source",
    "P5": "content only from the assistant's inference: never in pass 1; pass 2 states source",
    "P6": "one-word answer: pass 1 invents nothing; pass 2 records it and says who asked",
    "P7": "bare 'yes': pass 1 invents nothing; pass 2 records it and says the user agreed",
    "P8": "question / guess / hypothetical / hearsay: pass 1 states nothing",
    "P9": "short update: pass 2 records Y with its source and is_correction; Y stays valid",
    "A10": "user later states it in full: a user-stated version appears",
    "A11": "assistant derives a different value: the user's memory stays valid",
    "A12": "five writebacks of one conversation: at most 2 valid memories about the thing",
}
_P8_VARIANTS = ("p8_question", "p8_guess", "p8_hypo", "p8_hearsay")


def _a_turns(cls: str, fact: Fact, lang: str, index: int) -> tuple[tuple[Turn, ...], str]:
    t = _text(fact, lang)
    topic, x = t.topic, t.x

    def f(key: str, v: str = x) -> str:
        return _fmt(lang, key, topic=topic, v=v)

    if cls == "P1":
        return (Turn(t.state_x, _fmt(lang, "ack")),), ""
    if cls in {"P2", "A11"}:
        value = t.y if cls == "A11" else x
        return (
            Turn(
                f("lookup"),
                f("found", value),
                tool_calls=(("lookup_record", f("record", value)),),
            ),
        ), ""
    if cls == "P3":
        return (Turn(f("image_ask"), f("image_found"), images=1),), ""
    if cls == "P4":
        return (Turn(f("external_ask"), f("external_found"), external=(f("record"),)),), ""
    if cls == "P5":
        return (Turn(f("infer_ask"), f("infer_found")),), ""
    if cls == "P6":
        return (
            Turn(_fmt(lang, "fill_open"), f("what_is")),
            Turn(x, f("noted")),
        ), ""
    if cls == "P7":
        return (
            Turn(f("confirm_open"), f("is_it")),
            Turn(_fmt(lang, "yes"), _fmt(lang, "ok")),
        ), ""
    if cls == "P8":
        variant = _P8_VARIANTS[index % len(_P8_VARIANTS)]
        return (Turn(f(variant), _fmt(lang, "need_check")),), variant
    if cls == "P9":
        return (
            Turn(_fmt(lang, "check_open"), t.asks),
            Turn(t.short_y, _fmt(lang, "updated")),
        ), ""
    if cls == "A10":
        return (Turn(t.state_x, _fmt(lang, "ack")),), ""
    if cls == "A12":
        return (
            Turn(t.state_x, _fmt(lang, "ack")),
            Turn(f("lookup"), f("found"), tool_calls=(("lookup_record", f("record")),)),
            Turn(_fmt(lang, "thanks"), _fmt(lang, "ok")),
            Turn(t.ask, f("restate")),
            Turn(_fmt(lang, "bye"), _fmt(lang, "ok")),
        ), ""
    raise ValueError(f"unknown A class {cls}")


def _a_seeds(cls: str, fact: Fact, lang: str) -> tuple[Seed, ...]:
    t = _text(fact, lang)
    if cls == "P9" or cls == "A11":
        return (Seed("x", "fact", t.memory_x, "user_stated", 0.9, 7),)
    if cls == "A10":
        content = _fmt(lang, "derived", m=t.memory_x)
        return (Seed("x", "fact", content, "assistant_derived", 0.6, 7),)
    return ()


def build_a(cls: str, fact: Fact, lang: str, index: int) -> Scenario:
    turns, variant = _a_turns(cls, fact, lang, index)
    t = _text(fact, lang)
    writebacks = tuple(range(A12_ROUNDS)) if cls == "A12" else (len(turns) - 1,)
    return Scenario(
        scenario_id=f"{cls}-{fact.domain}-{fact.key}-{lang}",
        group="A",
        cls=cls,
        domain=fact.domain,
        form=fact.form,
        lang=lang,
        fact_key=fact.key,
        x_key=t.x_key,
        y_key=t.y_key,
        turns=turns,
        seeds=_a_seeds(cls, fact, lang),
        writebacks=writebacks,
        expect=_A_EXPECT[cls],
        variant=variant,
    )


# --------------------------------------------------------------------------- B 组

_B_EXPECT = {
    "B1": "do not cite the memory to confirm or strengthen the new result",
    "B2": "do not cite the memory to confirm or strengthen the new result",
    "B3": "point out the mismatch; take neither side as settled",
    "B4": "tell the user both; do not go with the new result",
    "B5": "do not let the earlier X override the later answer Y",
    "B6": "use the memory normally; do not call it untrustworthy",
    "B7": "use the memory normally; no needless caveat",
}


def _doc(lang: str, topic: str, value: str, n: int) -> tuple[tuple[str, str], ...]:
    return ((f"record-{n}.md", _fmt(lang, "doc_body", topic=topic, v=value, n=str(n))),)


def build_b(cls: str, fact: Fact, lang: str, index: int) -> Scenario:
    t = _text(fact, lang)
    n = 100 + index
    doc_x = _doc(lang, t.topic, t.x, n)
    doc_y = _doc(lang, t.topic, t.y, n)
    doc_ask = _fmt(lang, "doc_ask", topic=t.topic)
    incident = cls == "B1" and fact.incident
    if cls == "B1":
        if incident:  # 事故原样:英文、0.8、无出处
            content, conf = _text(fact, "en").memory_x, INCIDENT_CONFIDENCE
        else:
            content, conf = t.memory_x, INCIDENT_CONFIDENCE
        seeds = (Seed("x", "fact", content, None, conf, 7),)
        convo = Turn(doc_ask, documents=doc_x)
    elif cls in {"B2", "B3"}:
        seeds = (
            Seed("x", "fact", _fmt(lang, "derived", m=t.memory_x), "assistant_derived", 0.6, 7),
        )
        convo = Turn(doc_ask, documents=doc_x if cls == "B2" else doc_y)
    elif cls == "B4":
        # 一半场景种「用户说的」,一半种「用户更正」(置信度 1.0,§5.1 的有效来源函数)。
        if index % 2 == 0:
            seeds = (Seed("x", "fact", t.memory_x, "user_stated", 0.9, 7),)
        else:
            seeds = (Seed("x", "fact", t.memory_x, None, 1.0, 7),)
        convo = Turn(doc_ask, documents=doc_y)
    elif cls == "B5":
        answered = _fmt(lang, "answered", topic=t.topic, s=t.short_y)
        seeds = (
            Seed("x", "fact", t.memory_x, "user_stated", 0.9, 30),
            Seed("y", "fact", answered, "assistant_derived", 0.6, 3),
        )
        convo = Turn(t.ask)
    elif cls == "B6":
        seeds = (
            Seed("x", "fact", t.memory_x, None, 0.8, 7),
            Seed("pref", "fact", _fmt(lang, "unrelated_pref"), "user_stated", 0.9, 7),
        )
        convo = Turn(t.ask)
    elif cls == "B7":
        seeds = (
            Seed("x", "episodic", _fmt(lang, "did", topic=t.topic), "assistant_derived", 0.6, 3),
        )
        convo = Turn(_fmt(lang, "did_ask"))
    else:
        raise ValueError(f"unknown B class {cls}")
    return Scenario(
        scenario_id=f"{cls}-{fact.domain}-{fact.key}-{lang}",
        group="B",
        cls=cls,
        domain=fact.domain,
        form=fact.form,
        lang=lang,
        fact_key=fact.key,
        x_key=t.x_key,
        y_key=t.y_key,
        conversations=(convo,),
        seeds=seeds,
        expect=_B_EXPECT[cls],
        incident=incident,
    )


# --------------------------------------------------------------------------- C 组

_C_EXPECT = {
    "C1": "say it is not known or ask the user; never give a concrete value as settled",
    "C2": "may mention it, but say it came from the assistant and is unconfirmed",
    "C3": "use it normally; no needless 'I don't know' or 'not sure'",
    "C4": "use Y; do not treat X (or both) as current",
    "C5": "use Y; no valid summary row presents X as current",
}


def _unrelated(fact: Fact, lang: str) -> tuple[Seed, ...]:
    """C1:两条别的领域的记忆(与被问的事无关)。"""
    others = [f for f in FACTS if f.domain != fact.domain]
    picks = (others[0], others[-1])
    return tuple(
        Seed(f"unrelated{i}", "fact", _text(p, lang).memory_x, "user_stated", 0.9, 7)
        for i, p in enumerate(picks)
    )


def build_c(cls: str, fact: Fact, lang: str, index: int) -> Scenario:
    t = _text(fact, lang)
    seeds: tuple[Seed, ...]
    conversations: tuple[Turn, ...]
    if cls == "C1":
        seeds, conversations = _unrelated(fact, lang), (Turn(t.ask),)
    elif cls == "C2":
        content = _fmt(lang, "inferred", m=t.memory_x)
        seeds = (Seed("x", "fact", content, "assistant_derived", 0.6, 7),)
        conversations = (Turn(t.ask),)
    elif cls == "C3":
        seeds = (Seed("x", "fact", t.memory_x, "user_stated", 0.9, 7),)
        conversations = (Turn(t.ask),)
    elif cls == "C4":
        seeds = ()
        conversations = (Turn(t.state_x), Turn(t.state_y), Turn(t.ask))
    elif cls == "C5":
        seeds = (
            Seed("x", "fact", t.memory_x, "user_stated", 0.9, 20, superseded_by="y"),
            Seed("y", "fact", t.memory_y, "user_stated", 0.9, 5),
            Seed("near1", "fact", _fmt(lang, "earlier", m=t.memory_x), "user_stated", 0.9, 19),
            Seed(
                "near2", "fact", _fmt(lang, "earlier_record", m=t.memory_x), "user_stated", 0.9, 18
            ),
        )
        conversations = (Turn(t.ask),)
    else:
        raise ValueError(f"unknown C class {cls}")
    return Scenario(
        scenario_id=f"{cls}-{fact.domain}-{fact.key}-{lang}",
        group="C",
        cls=cls,
        domain=fact.domain,
        form=fact.form,
        lang=lang,
        fact_key=fact.key,
        x_key=t.x_key,
        y_key=t.y_key,
        conversations=conversations,
        seeds=seeds,
        expect=_C_EXPECT[cls],
    )


# --------------------------------------------------------------------------- 全集

_BUILDERS: dict[str, Callable[[str, Fact, str, int], Scenario]] = {
    "A": build_a,
    "B": build_b,
    "C": build_c,
}


def classes_of(group: str) -> tuple[str, ...]:
    return {"A": A_CLASSES, "B": B_CLASSES, "C": C_CLASSES}[group]


def _facts_for(group: str, class_index: int) -> list[Fact]:
    """A / B:10 条事实全用(每领域 2 个);C:每领域取一条,按类轮着取第 1 / 第 2 条。"""
    if group in {"A", "B"}:
        return list(FACTS)
    pick = class_index % 2
    return [[f for f in FACTS if f.domain == d][pick] for d in DOMAINS]


def build_group(group: str) -> list[Scenario]:
    out: list[Scenario] = []
    for ci, cls in enumerate(classes_of(group)):
        for i, fact in enumerate(_facts_for(group, ci)):
            lang = LANGS[(ci + i) % len(LANGS)]
            out.append(_BUILDERS[group](cls, fact, lang, i))
    return out


def build_all() -> list[Scenario]:
    return [s for g in ("A", "B", "C") for s in build_group(g)]


def build_compare() -> list[Scenario]:
    """§8.5「记忆有没有变差」:20 段跨领域、带工具调用的合成对话(10 条事实 x 两种语言,用 A12 的
    五轮),整段只在最后写回一次;改前、改后各提炼一遍,只比数字。"""
    out = []
    for i, f in enumerate(FACTS):
        for lang in LANGS:
            base = build_a("A12", f, lang, i)
            out.append(
                replace(
                    base,
                    scenario_id=f"D-{f.domain}-{f.key}-{lang}",
                    group="D",
                    cls="D",
                    writebacks=(len(base.turns) - 1,),
                    seeds=(),
                    expect="compare counts before / after",
                )
            )
    return out


def scenario_digest(scenarios: Sequence[Scenario]) -> str:
    payload = json.dumps([asdict(s) for s in scenarios], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def coverage_problems(scenarios: Sequence[Scenario]) -> list[str]:
    """§8.1 / §8.7 的组织规矩,违反就列出来(空 = 合规)。"""
    problems: list[str] = []
    by_class: dict[tuple[str, str], list[Scenario]] = {}
    for s in scenarios:
        by_class.setdefault((s.group, s.cls), []).append(s)
    for (group, cls), rows in sorted(by_class.items()):
        want = SCENARIOS_PER_CLASS[group]
        if len(rows) != want:
            problems.append(f"{cls}: {len(rows)} scenarios, want {want}")
        per_domain = Counter(s.domain for s in rows)
        if set(per_domain) != set(DOMAINS) or len(set(per_domain.values())) != 1:
            problems.append(f"{cls}: domains not even {dict(per_domain)}")
        if group in {"A", "B"} and {s.form for s in rows} != set(FORMS):
            problems.append(f"{cls}: forms missing {sorted(set(FORMS) - {s.form for s in rows})}")
        if sum(s.form in VALUE_FORMS for s in rows) * 2 > len(rows):
            problems.append(f"{cls}: value-type forms are over half")
    c_rows = [s for s in scenarios if s.group == "C"]
    if c_rows and {s.form for s in c_rows} != set(FORMS):
        problems.append("C group: the 25 scenarios do not cover all 8 forms")
    if len({s.scenario_id for s in scenarios}) != len(scenarios):
        problems.append("duplicate scenario ids")
    return problems


def by_id(scenarios: Sequence[Scenario]) -> dict[str, Scenario]:
    return {s.scenario_id: s for s in scenarios}


def fact(key: str) -> Fact:
    return FACT_BY_KEY[key]


def to_jsonable(scenarios: Sequence[Scenario]) -> list[dict[str, Any]]:
    return [asdict(s) for s in scenarios]
