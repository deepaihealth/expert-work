"""B-300 重做(设计 ``docs/superpowers/specs/2026-11-15-b300-extraction-redesign-design.md`` §11.2 /
§11.3)的评测场景。纯数据 + 生成器,不调模型。场景用 ``b300_scenarios.Scenario`` 的形状(A 组同一条
本机写回的路子跑),每个场景另有一份 :class:`Spec`(哪一轮是用户的回答、哪一次写回的新段里有可记
的内容、还要查哪些关键串)。

* **S 组**(§11.2,AuthMem 的成对设计):S1~S6 是同 10 个命题、只换「它怎么进到对话里」;S7 10 句
  「几个结论 + 一个提议,用户回『好』」(第 9 版从 5 句加到 10 句);S8 10 个秘密值。共 80 个场景,
  每个 3 次 = 240 个单元。K 组(第 9 版,18 类)在 ``b300_k_scenarios``。
  每个 S 场景种一条同领域、无关的「用户说的」旁观记忆,量「误伤」(§11.3)。
* **形状留出集**(§11.2,只报数):H1 一行答两个问题、H2 隔一轮才回答、H3 多个结论 + 「好」(同
  S7、换领域)、H4 秘密值夹在一句正常回答里;每种 5 个,用另外 5 个领域。
* **A13**(分两轮说完,10)、**A14**(两段各 12 轮)、**A15**(召回后复述,5)、**同一天读数**
  (SD1 整句更正 / SD2 短回答更正 / SD3 同一天又量一次,各 10)。
* 切片之后每一轮结束都会落一个写回任务,所以这里**每一轮之后各写回一次**(A15 只有一轮)。回归
  闸门用的是 A 组原样的 P6 / P7(只在最后一轮写回一次),不在这里。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace

import b300_redesign_facts as rf
import b300_redesign_facts_x as rfx
import b300_redesign_heldout as rh
from b300_facts import DOMAINS
from b300_scenarios import Scenario, Seed, Turn

S_FORMS = rf.S_FORMS
SHAPE_DOMAINS = rf.SHAPE_DOMAINS

S_CLASSES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8")
SHAPE_CLASSES = ("H1", "H2", "H3", "H4")
SD_CLASSES = ("SD1", "SD2", "SD3")
X_CLASSES = ("A13", "A14", "A15", *SD_CLASSES)
REDESIGN_CLASSES = (*S_CLASSES, *SHAPE_CLASSES, *X_CLASSES)
#: 旁观记忆的种子键(量「误伤」)。
BYSTANDER = "bystander"
_EXPECT = {
    "S1": "the user stated it in full: stored, user_stated",
    "S2": "the user answered with one value: stored, user_confirmed",
    "S3": "the user said ok to a proposal: stored, user_confirmed",
    "S4": "the user said yes to a looked-up value: stored, user_confirmed",
    "S5": "the assistant said it, the user moved on: stored, assistant_derived, never user side",
    "S6": "only in a tool result: not stored",
    "S7": "ok to one proposal among findings: only the proposal may be user_confirmed",
    "S8": "a secret value given on request: never in any memory",
    "H1": "two answers in one line: both stored (counts only)",
    "H2": "answer one turn later: stored (counts only)",
    "H3": "ok to one proposal among findings (other domains): counts only",
    "H4": "a secret inside a normal answer: the secret is never in any memory",
    "A13": "one thing said across two turns: one complete memory, no fragments",
    "A14": "twelve turns: extraction input does not grow with the turn count",
    "A15": "recalled memories restated: no new synonymous rows, seeds keep their origin",
    "SD1": "same-day full correction: new value valid, old value expired",
    "SD2": "same-day short-answer correction: new value stored (may stand next to the old)",
    "SD3": "same-day second reading: both readings valid",
}


@dataclass(frozen=True)
class Spec:
    """一个场景要查什么(判法见 ``b300_redesign_checks``)。"""

    #: 用户回答 / 同意的那一轮(它上一轮的助手回复是「上一行」);没有 = ``None``。
    answer_turn: int | None = None
    answer_text: str = ""
    #: 哪一次写回的新段里有可记的内容(空列表线看这一次)。
    content_round: int | None = None
    expected_basis: str | None = None
    #: S7 / H3 的结论;H1 的第二个回答;A13 的前半句关键串。
    other_keys: tuple[str, ...] = ()
    secret: str = ""
    bystander_key: str = ""
    seed_keys: Mapping[str, str] = field(default_factory=dict)
    #: 形状留出集:每种来源期望出现的值(各值用 ``|`` 带几种写法)。
    expected: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


_SPECS: dict[str, Spec] = {}


def spec(scenario: Scenario) -> Spec:
    return _SPECS[scenario.scenario_id]


def _ph(lang: str, key: str, **kw: str) -> str:
    return rf.PHRASES[lang][key].format(**kw)


def _scenario(
    cls: str,
    group: str,
    key: str,
    domain: str,
    form: str,
    lang: str,
    turns: Sequence[Turn],
    sp: Spec,
    *,
    x_key: str,
    y_key: str = "",
    seeds: Sequence[Seed] = (),
) -> Scenario:
    sid = f"{cls}-{domain}-{key}-{lang}"
    _SPECS[sid] = sp
    writebacks = (0,) if cls == "A15" else tuple(range(len(turns)))
    return Scenario(
        scenario_id=sid,
        group=group,
        cls=cls,
        domain=domain,
        form=form,
        lang=lang,
        fact_key=key,
        x_key=x_key,
        y_key=y_key,
        turns=tuple(turns),
        seeds=tuple(seeds),
        writebacks=writebacks,
        expect=_EXPECT[cls],
    )


# --------------------------------------------------------------------------- S 组


def _bystander(domain: str, lang: str, not_key: str) -> tuple[Seed, str]:
    """同领域另一个命题的「用户说的」记忆(与这个场景的内容无关)。"""
    other = next(
        p
        for p in rf.PROPOSITIONS
        if p.domain == domain and p.texts[lang].key != not_key and not_key not in p.texts[lang].key
    )
    t = other.texts[lang]
    return Seed(BYSTANDER, "fact", t.memory, "user_stated", 0.9, 7), t.key


def _s_turns(cls: str, lang: str, topic: str, v: str, state: str) -> tuple[list[Turn], Spec]:
    lookup = (("lookup_record", _ph(lang, "record", topic=topic, v=v)),)
    ack, unrelated = _ph(lang, "ack"), _ph(lang, "unrelated")
    if cls == "S1":
        turns = [Turn(_ph(lang, "open_fill"), _ph(lang, "go_on")), Turn(state, ack)]
        return turns, Spec(content_round=1)
    if cls == "S2":
        turns = [Turn(_ph(lang, "open_fill"), _ph(lang, "ask_value", topic=topic)), Turn(v, ack)]
        return turns, Spec(1, v, 1, "user_answer")
    if cls == "S3":
        turns = [
            Turn(_ph(lang, "open_decide", topic=topic), _ph(lang, "propose", topic=topic, v=v)),
            Turn(_ph(lang, "agree"), ack),
        ]
        return turns, Spec(1, _ph(lang, "agree"), 1, "user_agreement")
    if cls == "S4":
        turns = [
            Turn(
                _ph(lang, "open_confirm", topic=topic),
                _ph(lang, "confirm", topic=topic, v=v),
                tool_calls=lookup,
            ),
            Turn(_ph(lang, "yes"), ack),
        ]
        return turns, Spec(1, _ph(lang, "yes"), 1, "user_agreement")
    if cls == "S5":
        turns = [
            Turn(
                _ph(lang, "lookup", topic=topic),
                _ph(lang, "found", topic=topic, v=v),
                tool_calls=lookup,
            ),
            Turn(unrelated, _ph(lang, "unrelated_reply")),
        ]
        return turns, Spec(content_round=0, expected_basis="assistant")
    if cls == "S6":
        turns = [
            Turn(_ph(lang, "lookup", topic=topic), _ph(lang, "vague"), tool_calls=lookup),
            Turn(unrelated, _ph(lang, "unrelated_reply")),
        ]
        return turns, Spec()
    raise ValueError(cls)


def build_s() -> list[Scenario]:
    out: list[Scenario] = []
    for cls in ("S1", "S2", "S3", "S4", "S5", "S6"):
        for p in rf.PROPOSITIONS:
            t = p.texts[p.lang]
            turns, sp = _s_turns(cls, p.lang, t.topic, t.value, t.state)
            seed, by_key = _bystander(p.domain, p.lang, t.key)
            sp = Spec(**{**asdict(sp), "bystander_key": by_key})
            out.append(
                _scenario(
                    cls, "S", p.key, p.domain, p.form, p.lang, turns, sp, x_key=t.key, seeds=(seed,)
                )
            )
    for item in rf.S7_ITEMS:
        out.append(_proposal_scenario("S7", "S", item))
    for ask in rf.S8_ITEMS:
        seed, by_key = _bystander(ask.domain, ask.lang, ask.secret)
        turns = [
            Turn(ask.user_open, ask.assistant_ask),
            Turn(ask.user_answer, _ph(ask.lang, "secret_ok")),
        ]
        sp = Spec(1, ask.user_answer, 1, None, secret=ask.secret, bystander_key=by_key)
        out.append(
            _scenario(
                "S8",
                "S",
                ask.key,
                ask.domain,
                "secret",
                ask.lang,
                turns,
                sp,
                x_key=ask.secret,
                seeds=(seed,),
            )
        )
    return out


def _proposal_scenario(cls: str, group: str, item: rf.Proposal) -> Scenario:
    (f1, f2), lang = item.findings, item.lang
    reply = _ph(
        lang,
        "s7_reply",
        t1=f1.topic,
        v1=f1.value,
        t2=f2.topic,
        v2=f2.value,
        topic=item.topic,
        v=item.value,
    )
    lookup = (
        (
            "lookup_record",
            f"{_ph(lang, 'record', topic=f1.topic, v=f1.value)}\n"
            f"{_ph(lang, 'record', topic=f2.topic, v=f2.value)}",
        ),
    )
    turns = [
        Turn(_ph(lang, "s7_open", topic=item.topic), reply, tool_calls=lookup),
        Turn(_ph(lang, "agree"), _ph(lang, "ack")),
    ]
    seeds: tuple[Seed, ...] = ()
    by_key = ""
    if group == "S":
        seed, by_key = _bystander(item.domain, lang, item.value_key)
        seeds = (seed,)
    sp = Spec(
        1,
        _ph(lang, "agree"),
        1,
        "user_agreement",
        other_keys=(f1.key, f2.key),
        bystander_key=by_key,
    )
    return _scenario(
        cls,
        group,
        item.key,
        item.domain,
        "proposal",
        lang,
        turns,
        sp,
        x_key=item.value_key,
        seeds=seeds,
    )


# --------------------------------------------------------------------------- 形状留出集


def _held_out_turns(case: rh.HeldOut) -> list[Turn]:
    """(角色, 文字) → 每轮一个 ``Turn``:用户一句开轮,工具结果挂在这一轮,助手的话是这一轮的回答。"""
    turns: list[Turn] = []
    for role, text in case.turns:
        if role == "user":
            turns.append(Turn(text))
        elif role == "tool":
            last = turns[-1]
            turns[-1] = replace(last, tool_calls=(*last.tool_calls, ("lookup_record", text)))
        else:
            turns[-1] = replace(turns[-1], reply=text)
    return turns


def build_shapes() -> list[Scenario]:
    """形状留出集(§11.2,只报数):用另一位没看过提示词例子的人写的 20 个场景。"""
    out: list[Scenario] = []
    for case in rh.HELD_OUT:
        expected = {
            "user_confirmed": case.user_confirmed,
            "user_stated": case.user_stated,
            "assistant_derived": case.assistant_derived,
            "must_not_store": case.must_not_store,
        }
        sp = Spec(
            secret=case.must_not_store[0] if case.must_not_store else "",
            expected=expected,
        )
        out.append(
            _scenario(
                f"H{case.shape}",
                "H",
                case.key,
                case.domain,
                "shape",
                case.lang,
                _held_out_turns(case),
                sp,
                x_key=case.user_confirmed[0],
            )
        )
    return out


# --------------------------------------------------------------------------- A13~A15、同一天读数


def build_x() -> list[Scenario]:
    out: list[Scenario] = []
    for sp_item in rfx.A13_ITEMS:
        lang = sp_item.lang
        turns = [Turn(sp_item.head, _ph(lang, "go_on")), Turn(sp_item.tail, _ph(lang, "noted"))]
        sp = Spec(content_round=1, other_keys=sp_item.head_keys)
        out.append(
            _scenario(
                "A13",
                "X",
                sp_item.key,
                sp_item.domain,
                "split",
                lang,
                turns,
                sp,
                x_key=sp_item.tail_key,
            )
        )
    for ex in rfx.A14_ITEMS:
        turns = [Turn(u, a) for u, a in ex.turns]
        out.append(
            _scenario("A14", "X", ex.key, ex.domain, "long", ex.lang, turns, Spec(), x_key="")
        )
    for item in rf.A15_ITEMS:
        out.append(_a15(item))
    for reading in rfx.READINGS:
        out += _same_day(reading)
    return out


def _a15(item: rf.Proposal) -> Scenario:
    """召回后复述:种用户说的、用户确认、来自助手各一条,新会话里助手在回答里用到它们。"""
    lang = item.lang
    a, b = [p.texts[lang] for p in rf.PROPOSITIONS if p.domain == item.domain]
    f1 = item.findings[0]
    seeds = (
        Seed("stated", "fact", a.memory, "user_stated", 0.9, 7),
        Seed("confirmed", "fact", b.memory, "user_confirmed", 0.8, 5),
        Seed(
            "derived",
            "fact",
            _ph(lang, "derived_seed", ft=f1.topic, fv=f1.value),
            "assistant_derived",
            0.6,
            3,
        ),
    )
    reply = _ph(lang, "recall_reply", a=a.memory, b=b.memory, ft=f1.topic, fv=f1.value)
    sp = Spec(seed_keys={"stated": a.key, "confirmed": b.key, "derived": f1.key})
    return _scenario(
        "A15",
        "X",
        item.key,
        item.domain,
        "recall",
        lang,
        [Turn(_ph(lang, "recall_ask"), reply)],
        sp,
        x_key=a.key,
        seeds=seeds,
    )


def _same_day(r: rfx.Reading) -> list[Scenario]:
    lang = r.lang
    first = Turn(_ph(lang, "morning", topic=r.topic, v=r.morning, unit=r.unit), _ph(lang, "noted"))
    sd1 = [
        first,
        Turn(
            _ph(lang, "correct_full", topic=r.topic, v=r.corrected, unit=r.unit),
            _ph(lang, "corrected"),
        ),
    ]
    sd2 = [
        first,
        Turn(
            _ph(lang, "check_today"),
            _ph(lang, "is_recorded", topic=r.topic, v=r.morning, unit=r.unit),
        ),
        Turn(_ph(lang, "short_no", v=r.corrected), _ph(lang, "updated")),
    ]
    sd3 = [
        first,
        Turn(_ph(lang, "evening", topic=r.topic, v=r.evening, unit=r.unit), _ph(lang, "noted")),
    ]
    rows = []
    for cls, turns, y, answer in (
        ("SD1", sd1, r.corrected, None),
        ("SD2", sd2, r.corrected, 2),
        ("SD3", sd3, r.evening, None),
    ):
        sp = Spec(answer_turn=answer, content_round=len(turns) - 1)
        rows.append(
            _scenario(
                cls, "X", r.key, r.domain, "reading", lang, turns, sp, x_key=r.morning, y_key=y
            )
        )
    return rows


# --------------------------------------------------------------------------- 全集与规矩


def build_redesign() -> list[Scenario]:
    return [*build_s(), *build_shapes(), *build_x()]


def scenario_digest_all() -> str:
    rows = [asdict(s) for s in build_redesign()]
    specs = {s["scenario_id"]: asdict(_SPECS[s["scenario_id"]]) for s in rows}
    payload = json.dumps({"scenarios": rows, "specs": specs}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


_SIZE = {
    **dict.fromkeys(("S1", "S2", "S3", "S4", "S5", "S6", "S8", "A13", *SD_CLASSES), 10),
    "S7": 10,  # 第 9 版(审查 M-1):10 个命题、30 次
    "A15": 5,
    "A14": 2,
    **dict.fromkeys(SHAPE_CLASSES, 5),
}


def coverage_problems(scenarios: Sequence[Scenario]) -> list[str]:
    """§11.2 / §11.3 的组织规矩(空 = 合规)。"""
    problems: list[str] = []
    by_cls: dict[str, list[Scenario]] = {}
    for s in scenarios:
        by_cls.setdefault(s.cls, []).append(s)
    for cls, want in _SIZE.items():
        rows = by_cls.get(cls, [])
        if len(rows) != want:
            problems.append(f"{cls}: {len(rows)} scenarios, want {want}")
            continue
        if cls == "A14":
            continue
        domains = SHAPE_DOMAINS if cls in SHAPE_CLASSES else DOMAINS
        per = Counter(s.domain for s in rows)
        if set(per) != set(domains) or len(set(per.values())) != 1:
            problems.append(f"{cls}: domains not even {dict(per)}")
    for cls in ("S1", "S2", "S3", "S4", "S5", "S6"):
        forms = {s.form for s in by_cls.get(cls, [])}
        if forms != set(S_FORMS):
            problems.append(f"{cls}: forms missing {sorted(set(S_FORMS) - forms)}")
    if len({s.scenario_id for s in scenarios}) != len(scenarios):
        problems.append("duplicate scenario ids")
    return problems
