"""B-300 第 9 版 K 组(设计 2026-11-15 §11.2a / §11.3)的评测场景。纯数据 + 生成器,不调模型。

* **写入侧 K 组**:18 类 x 10 个场景(5 个领域各 2 个,中英各 5)= 180 个场景,每个 3 次 = 540 个
  单元。同一组内容(``b300_k_items.ITEMS``)在 18 类里只换「新说法是怎么来的」。旧值用种子直接种
  进库,每个场景另种两条**近邻**(K-late 按设计不种任何东西)。与 A / S 组同一条本机写回的路子跑。
* 写回次数:与 S 组一样每一轮结束各写回一次(线上切片之后就是这样);**K-late 只在最后写回
  一次**,一个写回覆盖前面全部各轮(§11.2a 表)。
* 一类里分两半的(K-cp 种子的来源、K-e2 / K-w 逐字相同与换说法、K-late 说得更全与改口、K-s 否认
  与再说一遍)按 :func:`first_half` 分:每一半 5 个场景、每个领域 1 个、两种语言都有。
* **回归闸门的 K 段**(§11.1 第 2 步 ①):K-a、K-b、K-n2、K-cp 各取 10 个单元 = 这 4 类的 10 个
  场景各跑 1 次(``b300_phases`` 的 ``gate-k`` 段)。
* **使用侧 B4c / B4d**(§11.3 末):用 A / B 组原有的 10 条事实;B4c 种一条控制台更正过的 X 和
  一条更晚的、用户说的 Y;B4d 种一条含 X 的汇总行(连同三条成员)和一条更晚的、用户说的 Y。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import b300_k_items as ki
from b300_facts import FACTS, Fact
from b300_scenarios import LANG_CYCLE, Scenario, Seed, Turn

K_CLASSES = (
    "K-a",
    "K-ap",
    "K-b",
    "K-b2",
    "K-bp",
    "K-c",
    "K-cp",
    "K-n1",
    "K-n2",
    "K-e1",
    "K-e2",
    "K-f",
    "K-g",
    "K-p",
    "K-w",
    "K-t",
    "K-late",
    "K-s",
)
#: 类名在代码里只用 ASCII(进文件名、用户名、记录键);设计里带撇号的三类在这里对上。
DESIGN_NAME = {"K-ap": "K-a′", "K-bp": "K-b′", "K-cp": "K-c′"}  # noqa: RUF001
#: 回归闸门先跑的 4 类(§11.1 第 2 步 ①),各 10 个单元。
GATE_K_CLASSES = ("K-a", "K-b", "K-n2", "K-cp")
#: 使用侧新加的两个子类(§11.3 末)。
USE_CLASSES = ("B4c", "B4d")
SCENARIOS_PER_CLASS = 10
#: 近邻种子的键;K-late 不种。
NEAR_KEYS = ("near1", "near2")
#: K-n1 / K-n2 的四种「不是在陈述」(§11.2a),按场景序号轮换。
N_SHAPES = ("quiz", "recall", "hypo", "hearsay")
#: 种子的日子:旧值 / 近邻、并存的更晚那条。都在 ``b300_scenarios.MAX_SEED_DAYS_AGO`` 之内。
OLD_DAYS, LATER_DAYS = 7, 3

_EXPECT = {
    "K-a": "full-sentence change: Y valid, X retired and linked to Y",
    "K-ap": "short-answer change: Y stored; by default X stays valid, Y linked to X",
    "K-b": "hesitant full sentence: X stays valid, the hedge is stored with its doubt",
    "K-b2": "hesitant short answer: X stays valid, the hedge is stored with its doubt",
    "K-bp": "settled after a hedge: Z valid, X retired",
    "K-c": "assistant's opposite finding: X is never retired",
    "K-cp": "opposite finding + a proposal + a vague reply: X is never retired",
    "K-n1": "short answer that is not a statement: X is never retired",
    "K-n2": "full sentence that is not a statement: X is never retired",
    "K-e1": "Y restated verbatim in a new message: X retired and linked to Y, no new row",
    "K-e2": "Y stated in full by the user: a valid user_stated Y and X retired; the confirmed Y "
    "may stay valid next to it",
    "K-f": "X is gone, no replacement: the statement is stored, X retired and linked to it",
    "K-g": "one sentence ends two rows: at least one retired",
    "K-p": "only half of a compound row denied: the other half survives in a valid row",
    "K-w": "the user states what an assistant row says: a valid user_stated Y, X retired",
    "K-t": "console-corrected X: never retired; the new statement is stored",
    "K-late": "one writeback over two turns: the fuller part is not lost; the later one is valid",
    "K-s": "a summary row: never retired directly; rewritten without B, or untouched on restate",
    "B4c": "use Y, or tell the user both; answering by the corrected X alone is a violation",
    "B4d": "use Y, or tell the user both; answering by the summary's X alone is a violation",
}


@dataclass(frozen=True)
class KSeed(Seed):
    """K 组 / B4c / B4d 的种子:在 ``Seed`` 上多三样(``asdict`` 只对这个子类多出字段,原有场景的
    摘要不变)。"""

    #: 与哪一条种子连着(本行的 ``supersedes`` 指向它,它仍然有效 —— §7.8「supersedes 的第 ② 种
    #: 含义」:默认规则留下的并存、汇总行待重写)。
    linked_to: str | None = None
    #: 本行是汇总行,成员是这几条种子(成员先写、标成已汇总)。
    summary_of: tuple[str, ...] = ()
    #: 事件日期在几天前(「带日期的碰上不带日期的」);``None`` = 不带事件日期。
    occurred_days_ago: int | None = None


def first_half(index: int) -> bool:
    """一类里分两半时的前一半:第 0、3、4、7、8 个场景(每个领域 1 个,中文 3 个、英文 2 个)。"""
    return index % 4 in (0, 3)


def item_of(scenario: Scenario) -> ki.KItem:
    return ki.ITEM_BY_KEY[scenario.fact_key]


def _ph(lang: str, key: str, **kw: str) -> str:
    return ki.PHRASES[lang][key].format(**kw)


def _stated(
    key: str, content: str, days: int = OLD_DAYS, *, occurred_days_ago: int | None = None
) -> KSeed:
    return KSeed(
        key, "fact", content, "user_stated", 0.9, days, occurred_days_ago=occurred_days_ago
    )


def _nears(pair: tuple[str, str]) -> tuple[KSeed, ...]:
    return tuple(_stated(k, text) for k, text in zip(NEAR_KEYS, pair, strict=True))


def _lookup_turn(it: ki.KItem, reply_tail: str = "") -> Turn:
    lang = it.lang
    record = _ph(lang, "record", topic=it.topic, v=it.y.text)
    found = _ph(lang, "found", topic=it.topic, v=it.y.text)
    reply = f"{found} {reply_tail}".strip() if lang == "en" else f"{found}{reply_tail}"
    return Turn(_ph(lang, "lookup", topic=it.topic), reply, tool_calls=(("lookup_record", record),))


def _n1_turns(it: ki.KItem, shape: str) -> list[Turn]:
    lang, topic, z = it.lang, it.topic, it.z.text
    if shape == "quiz":
        return [
            Turn(_ph(lang, "quiz_open", topic=topic), _ph(lang, "quiz_ask", topic=topic)),
            Turn(z, _ph(lang, "quiz_ack")),
        ]
    answer = _ph(lang, "recall_short", v=z) if shape == "recall" else z
    return [
        Turn(_ph(lang, f"{shape}_open", topic=topic), _ph(lang, f"{shape}_ask")),
        Turn(answer, _ph(lang, "n_reply")),
    ]


def _build(
    cls: str, it: ki.KItem, index: int
) -> tuple[list[Turn], tuple[KSeed, ...], str, str, str]:
    """一类、一组内容 → (各轮, 种子(不含近邻), 新说法的关键串, 另一个关键串, 变体)。"""
    lang, ack = it.lang, _ph(it.lang, "ack")
    x = _stated("x", it.x.memory)
    half = first_half(index)
    if cls == "K-a":
        dated = it.form == "number"  # 「带日期的碰上不带日期的」并进 K-a 的 2 个场景,只报数
        seed = _stated("x", it.x.memory, occurred_days_ago=OLD_DAYS) if dated else x
        return [Turn(it.change_full, ack)], (seed,), it.y.key, "", "dated" if dated else ""
    if cls == "K-ap":
        turns = [
            Turn(_ph(lang, "open"), _ph(lang, "ask_now", topic=it.topic)),
            Turn(it.y.text, ack),
        ]
        return turns, (x,), it.y.key, "", ""
    if cls == "K-b":
        return [Turn(it.hedge_full, ack)], (x,), it.z.key, "", ""
    if cls == "K-b2":
        turns = [
            Turn(_ph(lang, "open"), _ph(lang, "ask_is", topic=it.topic, v=it.z.text)),
            Turn(_ph(lang, "maybe"), ack),
        ]
        return turns, (x,), it.z.key, "", ""
    if cls == "K-bp":
        hedge = _stated("hedge", it.hedge_memory, LATER_DAYS)
        return [Turn(it.state_z, ack)], (x, hedge), it.z.key, "", ""
    if cls == "K-c":
        turns = [
            _lookup_turn(it),
            Turn(_ph(lang, "unrelated"), _ph(lang, "unrelated_reply")),
        ]
        return turns, (x,), it.y.key, "", ""
    if cls == "K-cp":
        origin, conf = ("user_stated", 0.9) if half else ("user_confirmed", 0.8)
        seed = KSeed("x", "fact", it.x.memory, origin, conf, OLD_DAYS)
        vague = ki.VAGUE[lang][index % len(ki.VAGUE[lang])]
        turns = [_lookup_turn(it, it.proposal), Turn(vague, ack)]
        return turns, (seed,), it.y.key, "", f"seed_{origin}"
    if cls == "K-n1":
        shape = N_SHAPES[index % len(N_SHAPES)]
        return _n1_turns(it, shape), (x,), it.z.key, "", shape
    if cls == "K-n2":
        shape = N_SHAPES[index % len(N_SHAPES)]
        said = _ph(lang, f"n2_{shape}", topic=it.topic, v=it.z.text)
        return [Turn(said, _ph(lang, "n_reply"))], (x,), it.z.key, "", shape
    if cls == "K-e1":
        y = _stated("y", it.y.memory, LATER_DAYS)
        return [Turn(it.state_y, ack)], (x, y), it.y.key, "", ""
    if cls in {"K-e2", "K-w"}:
        if cls == "K-e2":
            y = KSeed("y", "fact", it.y.memory, "user_confirmed", 0.8, LATER_DAYS, linked_to="x")
        else:
            y = KSeed("y", "fact", it.y.memory, "assistant_derived", 0.6, LATER_DAYS)
        said, variant = (it.state_y, "verbatim") if half else (it.state_y_alt, "reworded")
        return [Turn(said, ack)], (x, y), it.y.key, "", variant
    if cls == "K-f":
        return [Turn(it.gone, ack)], (x,), it.gone_key, "", ""
    if cls == "K-g":
        x2 = _stated("x2", it.x2.memory)
        return [Turn(it.both_gone, ack)], (x, x2), it.both_key, "", ""
    if cls == "K-p":
        seed = _stated("x", it.compound.memory)
        return [Turn(it.compound.deny_b, ack)], (seed,), it.compound.deny_key, "", ""
    if cls == "K-t":
        seed = KSeed("x", "fact", it.x.memory, "user_stated", 1.0, OLD_DAYS)
        return [Turn(it.change_full, ack)], (seed,), it.y.key, "", ""
    if cls == "K-late":
        late = it.late
        turns = [Turn(late.opener, late.assistant), Turn(late.first, ack), Turn(late.second, ack)]
        return turns, (), late.late_key, late.early_key, late.kind
    if cls == "K-s":
        t = it.trio
        members = tuple(_stated(k, v.memory) for k, v in (("a", t.a), ("b", t.b), ("c", t.c)))
        summary = KSeed(
            "summary", "fact", t.summary, "user_stated", 0.9, LATER_DAYS, summary_of=("a", "b", "c")
        )
        said, variant = (t.deny_b, "deny") if half else (t.restate_b, "restate")
        return [Turn(said, ack)], (*members, summary), t.deny_key if half else t.b.key, "", variant
    raise ValueError(f"unknown K class {cls}")


def build_k_class(cls: str) -> list[Scenario]:
    out: list[Scenario] = []
    for index, it in enumerate(ki.ITEMS):
        turns, seeds, new_key, other_key, variant = _build(cls, it, index)
        nears: tuple[KSeed, ...] = ()
        if cls == "K-s":
            nears = _nears(ki.TRIO_NEARS[it.key])
        elif cls != "K-late":
            nears = _nears(it.nears)
        writebacks = (len(turns) - 1,) if cls == "K-late" else tuple(range(len(turns)))
        out.append(
            Scenario(
                scenario_id=f"{cls}-{it.domain}-{it.key}-{it.lang}",
                group="K",
                cls=cls,
                domain=it.domain,
                form=it.form,
                lang=it.lang,
                fact_key=it.key,
                x_key=new_key,
                y_key=other_key,
                turns=tuple(turns),
                seeds=(*seeds, *nears),
                writebacks=writebacks,
                expect=_EXPECT[cls],
                variant=variant,
            )
        )
    return out


def build_k() -> list[Scenario]:
    return [s for cls in K_CLASSES for s in build_k_class(cls)]


# --------------------------------------------------------------------------- 使用侧 B4c / B4d


def _others(fact: Fact) -> tuple[Fact, Fact]:
    """别的领域的两条事实(当汇总行里与 X 并列的另外两件事)。"""
    rest = [f for f in FACTS if f.domain != fact.domain]
    return rest[0], rest[-1]


def build_use_class(cls: str) -> list[Scenario]:
    out: list[Scenario] = []
    for index, fact in enumerate(FACTS):
        lang = LANG_CYCLE[(USE_CLASSES.index(cls) + index) % len(LANG_CYCLE)]
        t = fact.texts[lang]
        later = _stated("y", t.memory_y, LATER_DAYS)
        seeds: tuple[KSeed, ...]
        if cls == "B4c":
            # 控制台更正过的 X(置信度 1.0)+ 日期更晚的、用户说的 Y;两条都有效(§15 第 11 条 ③)。
            seeds = (KSeed("x", "fact", t.memory_x, "user_stated", 1.0, OLD_DAYS), later)
        else:
            a, c = (f.texts[lang].memory_x for f in _others(fact))
            members = (_stated("a", a, 12), _stated("x", t.memory_x, 12), _stated("c", c, 12))
            summary = KSeed(
                "summary",
                "fact",
                " ".join((a, t.memory_x, c)),
                "user_stated",
                0.9,
                OLD_DAYS,
                summary_of=("a", "x", "c"),
            )
            # 用户的新说法连着那条还没重写的汇总行(§7.12 第 1 步之后、第 2 步之前的状态)。
            y = KSeed("y", "fact", t.memory_y, "user_stated", 0.9, LATER_DAYS, linked_to="summary")
            seeds = (*members, summary, y)
        out.append(
            Scenario(
                scenario_id=f"{cls}-{fact.domain}-{fact.key}-{lang}",
                group="B",
                cls=cls,
                domain=fact.domain,
                form=fact.form,
                lang=lang,
                fact_key=fact.key,
                x_key=t.x_key,
                y_key=t.y_key,
                conversations=(Turn(t.ask),),
                seeds=seeds,
                expect=_EXPECT[cls],
            )
        )
    return out


def build_use() -> list[Scenario]:
    return [s for cls in USE_CLASSES for s in build_use_class(cls)]


# --------------------------------------------------------------------------- 全集与规矩


def scenario_digest_k() -> str:
    rows = [asdict(s) for s in [*build_k(), *build_use()]]
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


#: 一类里分两半的:类 → 两个变体名(各 5 个场景)。
HALVES = {
    "K-cp": ("seed_user_stated", "seed_user_confirmed"),
    "K-e2": ("verbatim", "reworded"),
    "K-w": ("verbatim", "reworded"),
    "K-late": ("fuller", "switch"),
    "K-s": ("deny", "restate"),
}


def coverage_problems(scenarios: Sequence[Scenario]) -> list[str]:
    """§11.2a 的组织规矩(空 = 合规):18 类各 10 个场景、每个领域 2 个、中英各 5;内容形态六种都
    有;除 K-late 外每个场景恰好两条近邻;分两半的类每一半 5 个、每个领域 1 个。"""
    problems: list[str] = []
    by_cls: dict[str, list[Scenario]] = {}
    for s in scenarios:
        by_cls.setdefault(s.cls, []).append(s)
    if tuple(by_cls) != K_CLASSES:
        problems.append(f"classes {sorted(by_cls)} are not the 18 K classes")
    for cls, rows in by_cls.items():
        if len(rows) != SCENARIOS_PER_CLASS:
            problems.append(f"{cls}: {len(rows)} scenarios, want {SCENARIOS_PER_CLASS}")
        per_domain = Counter(s.domain for s in rows)
        if set(per_domain) != set(ki.K_DOMAINS) or set(per_domain.values()) != {2}:
            problems.append(f"{cls}: domains not 2 each {dict(per_domain)}")
        if Counter(s.lang for s in rows) != Counter({"zh": 5, "en": 5}):
            problems.append(f"{cls}: languages not 5 / 5")
        if {s.form for s in rows} != set(ki.K_FORMS):
            problems.append(
                f"{cls}: forms missing {sorted(set(ki.K_FORMS) - {s.form for s in rows})}"
            )
        want_nears = 0 if cls == "K-late" else len(NEAR_KEYS)
        for s in rows:
            got = sum(seed.key in NEAR_KEYS for seed in s.seeds)
            if got != want_nears:
                problems.append(f"{s.scenario_id}: {got} neighbours, want {want_nears}")
        if cls in HALVES:
            for variant in HALVES[cls]:
                half = [s for s in rows if s.variant == variant]
                if len(half) != 5 or len({s.domain for s in half}) != 5:
                    problems.append(f"{cls}/{variant}: {len(half)} scenarios, want 1 per domain")
                if len({s.lang for s in half}) != 2:
                    problems.append(f"{cls}/{variant}: one language only")
    if len({s.scenario_id for s in scenarios}) != len(scenarios):
        problems.append("duplicate scenario ids")
    return problems
