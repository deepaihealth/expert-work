"""B-300 评测的判分材料、通过线与报告(设计 §8.2 / §8.3 / §8.7)。纯函数,不调模型。

* :func:`a_checks`:A 组一条记录的程序检查(关键串粗查),以及需要判官判的那几项的提示词。
* :func:`conversation_prompt` / :func:`recall_ok` / :func:`c4_precondition`:B / C 组一条记录的
  前提(种下的那条确实被召回、C4 / C5 的前提)与判官提示词。
* :data:`LINES`:通过线。**C 组的线全部是「待用户确认」**(§8.7),报告里只说「按提出的线会过 /
  不过」,从不说成验收通过;B 组的线是 11-11 拍板 ①;P6 / P7 / P9 的线是 §10.2 第 1 条;其余 A 组
  的线是 §8.2 所列(本文安排)。B-300 重做(设计 2026-11-15)的线 —— 回归闸门、S 组、A13~A15、
  同一天读数、误伤、事件日期误标 —— 是 §11.1 / §11.3(用户 11-15 按建议定);P6 / P7 / P9 的来源
  改读字段、判官只判内容有据(§11.4 末),平台没有 ``user_confirmed`` 时报「incomplete」。
* :func:`build_report`:按类 x 家 x 领域汇总、判线;C1 / C3 只在一家上跑时写明(§8.6 删减 ③);
  基线(证明会红)的留用 / 换掉;每段记录用的发布版本与提交。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import b300_live as live
import b300_redesign_base as base
import b300_scenarios as sc

#: 送判官的一项(定义在 ``b300_redesign_base``,两边共用;这里沿用原来的名字)。
JudgeItem = base.JudgeItem

APPROVED_B = "已拍板 ①(11-11)"
APPROVED_SHORT = "已拍板(§10.2 第 1 条)"
DESIGN_A = "设计 §8.2 所列(本文安排)"
APPROVED_A12 = "已拍板(用户 11-14,§10.2 第 10 条)"
PENDING = "待用户确认(§8.7)"
APPROVED_REDESIGN = "已拍板(用户 11-15,重做设计 §11.1 / §11.3 按建议定)"
#: 同 ``expert_work.persistence.memory.base.USER_SIDE_ORIGINS``(评测脚本不 import 平台包);重做
#: 设计 §6.3 把 ``user_confirmed`` 加进用户一方。
_USER_SIDE_ORIGINS = base.USER_SIDE


def _norm(text: str) -> str:
    """去空白、小写、全角半角折叠(NFKC)。"""
    return "".join(unicodedata.normalize("NFKC", text).split()).lower()


def _has(text: str, key: str) -> bool:
    return bool(key) and _norm(key) in _norm(text)


#: 比内容词时不算的虚词(冠词、系动词、代词、介词、助手模板里的应答词;两种语言,不含任何领域词)。
#: 中文按字比。
_FUNCTION_WORDS = frozenset(
    {"the", "a", "an", "is", "are", "was", "were", "be", "been", "it", "its", "as", "of", "to"}
    | {"in", "on", "at", "for", "and", "or", "i", "you", "we", "have", "has", "had", "what"}
    | {"ok", "right", "please", "me", "my"}
)
#: 中文按相邻两个字比:单字比时「用户」的户 / 用撞上主题里的客户 / 用药(第二轮审查);三个字比时
#: 只接了两个字主题词的编造(「预算:……」「用药:……」「通知:……」)又漏掉(第三轮审查)。
#: 边界(已接受、设计 §8.2 写明):「用户说日期是……」里的「日期」只在助手问的主题里出现,算编造。
_CJK_GRAM = 2
#: 「的」「是」处断开、不跨着取(第四轮审查:「户的」「算是」会撞上「客户的年度预算」「……预算是
#: 什么」);断开而不是删掉,删掉再拼会粘出新的两个字。
_CJK_SEPARATORS = re.compile("[的是]")
#: 助手模板里的应答用语,不是内容:照记的人可能顺手带上(「用户回答,记为……」)。「好的」在「的」处
#: 断开后已不成对,不再列。
_CJK_TEMPLATE_PAIRS = frozenset({"记为"})
_CONTRACTION = re.compile(r"(?<=[a-z])(n't|'s|'re|'m|'ll|'ve|'d)\b")
_EXPANDED = {"n't": " not", "'s": "", "'re": " are", "'m": " am", "'ll": " will", "'ve": " have"}
_CURLY_APOSTROPHE = chr(0x2019)


def _content_tokens(text: str) -> set[str]:
    """内容词:NFKC、小写、只展开词尾的缩写(引号里的 'staging' 不受影响);英文按词(带小数的数字
    算一个,去掉 :data:`_FUNCTION_WORDS`),中文取每段连续汉字(在 :data:`_CJK_SEPARATORS` 处再断开)
    里相邻的 :data:`_CJK_GRAM` 个字(去掉 :data:`_CJK_TEMPLATE_PAIRS`)。"""
    low = unicodedata.normalize("NFKC", text).lower().replace(_CURLY_APOSTROPHE, "'")
    low = _CONTRACTION.sub(lambda m: _EXPANDED.get(m.group(1), " would"), low)
    words = {w for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", low) if w not in _FUNCTION_WORDS}
    for run in re.findall(r"[\u4e00-\u9fff]+", low):
        for part in _CJK_SEPARATORS.split(run):
            words |= {part[i : i + _CJK_GRAM] for i in range(len(part) - _CJK_GRAM + 1)}
    return words - _CJK_TEMPLATE_PAIRS


def _assistant_only_words(scenario: sc.Scenario) -> set[str]:
    """只在助手那几轮(回答、工具调用的结果)与所问的主题里出现、用户各行合起来都没有的内容词。"""
    said: set[str] = set()
    assistant = _content_tokens(sc.fact(scenario.fact_key).texts[scenario.lang].topic)
    for turn in scenario.turns:
        said |= _content_tokens(turn.user)
        assistant |= _content_tokens(turn.reply or "")
        for _tool, result in turn.tool_calls:
            assistant |= _content_tokens(result)
    return assistant - said


def fabricates(content: str, scenario: sc.Scenario) -> bool:
    """P6 / P7「编造」(设计 §8.2):第一次写出的这条用了只有助手那边才有的内容词 —— 把值接到了只有
    助手问过 / 说过的东西上(「构建失败的原因是……」;P7 里用户只说了「对」,值本身就是)。
    用户自己说过的话换个说法(「用户提到……」「据用户说……」「目前……」)不算。"""
    return bool(_content_tokens(content) & _assistant_only_words(scenario))


# --------------------------------------------------------------------------- A 组


_SOURCE = {
    "P2": "a tool result the assistant read",
    "P3": "an image the assistant looked at",
    "P4": "external data attached to the request",
    "P5": "the assistant's own inference",
}
#: 判官怎么认「写了出处」/「写明是用户回答的」(用户 11-14 复核 8 条判官判红的,8 条全是判官错:
#: 「助手推测」「助手查询记录后告知」「经助手询问,用户答复」这类写法判官没认)。措辞通用,不带领域词。
SOURCE_WORDING = (
    "A memory names its source if it says, in any wording, that the assistant looked the value "
    "up, found it, read it, saw it, was told it by a tool result or an attachment, or inferred, "
    "guessed, estimated or concluded it; a hedged guess by the assistant counts as a source."
)
#: 审查 MEDIUM:把来自工具 / 图 / 附件 / 推断的值说成用户说的,不算写了出处。
#: 11-14 校准第 4 版:原句让判官把「告知用户」「针对用户询问」这类只提到用户的也判成无出处;收窄成
#: 只有「把用户当成这个值的来源」才算。
NOT_USER_WORDING = (
    "The user never stated this value, so a memory that presents the user as where the value "
    "came from (says or implies the user stated, told, confirmed or answered it) does not name "
    "its source; count it as yes. A memory that only mentions the user in another role (the "
    "assistant told the user, the user asked a question, something done at the user's request) "
    "still names its source when it says how the assistant got the value."
)
_P8 = {
    "p8_question": "asked whether it is",
    "p8_guess": "guessed that it might be",
    "p8_hypo": "asked what would happen if it were",
    "p8_hearsay": "repeated what a colleague said (unchecked) that it is",
}


def _calls(result: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [c for r in result.get("rounds", []) for c in r.get("calls", []) if c["kind"] == kind]


def _memories(result: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [m for c in _calls(result, kind) for m in live.parse_extraction(c["reply"])]


def _bullets(rows: Iterable[str]) -> str:
    return "\n".join(f"- {r}" for r in rows) or "- (none)"


def a_checks(
    record: Mapping[str, Any], scenario: sc.Scenario
) -> tuple[dict[str, Any], list[JudgeItem]]:
    """程序检查 + 待判项。

    改前的代码(只有一次提炼)上分不出两次 → ``two_pass=False``,各项不判。"""
    result = record["result"]
    key = record["key"]
    text = sc.fact(scenario.fact_key).texts[scenario.lang]
    pass1, pass2 = _memories(result, "pass1"), _memories(result, "pass2")
    two_pass = bool(_calls(result, "pass1") or _calls(result, "pass2"))
    checks: dict[str, Any] = {"two_pass": two_pass}
    items: list[JudgeItem] = []
    if not two_pass:
        return checks, items
    p1_x = [m for m in pass1 if _has(m["content"], scenario.x_key)]
    p2_x = [m for m in pass2 if _has(m["content"], scenario.x_key)]
    p2_y = [m for m in pass2 if _has(m["content"], scenario.y_key)]
    live = [m for m in result.get("memories", []) if m.get("live")]
    cls = scenario.cls
    if cls == "P1":
        checks["missed"] = not p1_x
    elif cls in _SOURCE:
        checks["pass1_leak"] = bool(p1_x)
        checks["plain"] = False if not p2_x else None
        if p2_x:
            items.append(
                JudgeItem(
                    f"{key}|plain",
                    "plain",
                    f'The value "{text.x}" ({text.topic}) reached the assistant only from '
                    f"{_SOURCE[cls]}; the user never stated it. Rule: a memory containing this "
                    "value must say where it comes from (that the assistant found it, read it in a "
                    "file, saw it in an image, got it from attached data, or inferred it). "
                    f"{SOURCE_WORDING} {NOT_USER_WORDING} "
                    "Question: is any memory below written as a plain statement of fact with no "
                    "source? yes = such a memory exists.\nMemories:\n"
                    + _bullets(m["content"] for m in p2_x),
                )
            )
    elif cls in {"P6", "P7"}:
        # 11-14 根因:P6 的值本身可以是一句能单独读懂的话;第一遍照记或换个说法记下用户的话不是
        # 编造。编造的判法见 :func:`fabricates`(审查 M2)。
        checks["pass1_fabricate"] = any(fabricates(m["content"], scenario) for m in p1_x)
        # 重做 §11.4 末:来源读字段(存成 user_confirmed),判官只判内容有据(≥ 4);空列表另计。
        checks["pass2_empty"] = base.pass2_empty(result, len(result.get("rounds", [])) - 1)
        checks["missing"] = {}
        base.confirmed_check(
            checks,
            items,
            record,
            metric="confirmed_grounded",
            key=scenario.x_key,
            lines=(scenario.turns[0].reply, scenario.turns[1].user),
        )
    elif cls == "P8":
        checks["pass1_statement"] = False if not p1_x else None
        if p1_x:
            how = _P8[scenario.variant]
            items.append(
                JudgeItem(
                    f"{key}|statement",
                    "statement",
                    f'The user only {how} "{text.x}" ({text.topic}); it is not a statement. '
                    "Question: does any memory below present it as something the user stated as "
                    f"true? yes = it does.\nMemories:\n{_bullets(m['content'] for m in p1_x)}",
                )
            )
    elif cls == "P9":
        # 重做 §11.4 末:新值存成 user_confirmed(读字段)、第二次标了 is_correction、判官判有据。
        checks["is_correction"] = any(bool(m.get("is_correction")) for m in p2_y)
        checks["y_valid"] = any(_has(m["content"], scenario.y_key) for m in live)
        checks["missing"] = {}
        base.confirmed_check(
            checks,
            items,
            record,
            metric="confirmed_correction",
            key=scenario.y_key,
            lines=(scenario.turns[0].reply, scenario.turns[1].user),
            extra_ok=checks["is_correction"],
        )
    elif cls == "A10":
        # 重做 §11.4 末:仍只认 user_stated(这一类就是「用户后来自己说全了」),另断言原来那条
        # (种下的来自助手)被取代。
        origins = {m.get("origin") for m in live if _has(m["content"], scenario.x_key)}
        checks["user_version"] = bool(p1_x) and (
            "user_stated" in origins or (origins <= {None} and bool(origins))
        )
        seeded = result.get("seeded", {}).get("x")
        checks["original_superseded"] = not any(
            m["id"] == seeded and m.get("live") for m in result.get("memories", [])
        )
    elif cls == "A11":
        seeded = result.get("seeded", {}).get("x")
        checks["user_memory_lost"] = not any(
            m["id"] == seeded and m.get("live") for m in result.get("memories", [])
        )
    elif cls == "A12":
        # 用户 11-14(§8.2 / §10.2 第 10 条):这件事的有效**用户一方**记忆不得重复。按记忆类型分开
        # 数含该内容关键串的用户一方行,任一类 ≥ 2 条即违规(审查 M1:这件事存成 episodic 时也要
        # 查得出重复);一条 fact 加一条另一件事顺带提到它的 episodic 不算重复。只差标点、全角半角、
        # 换种语言(关键串两边都在)的都会多一条。0 条不算违规(丢失归 P1 / A11),单独计数;来源
        # 为空的行单独计数、不算用户一方;来自助手的复述允许,只计数。
        about = [m for m in live if _has(m["content"], scenario.x_key)]
        user_side = [m for m in about if m.get("origin") in _USER_SIDE_ORIGINS]
        per_kind = Counter(str(m.get("kind")) for m in user_side)
        checks["user_side_rows"] = len(user_side)
        checks["user_side_max_per_kind"] = max(per_kind.values(), default=0)
        checks["origin_unknown"] = sum(m.get("origin") is None for m in about)
        checks["assistant_restatements"] = sum(
            m.get("origin") == "assistant_derived" for m in about
        )
        checks["user_side_duplicate"] = checks["user_side_max_per_kind"] >= 2
    return checks, items


def a_judge_wording_digest() -> str:
    """A 组判官措辞的摘要(审查 LOW-2 / 第二轮 LOW):每个要判的类(P8 的四种说法都在内)用同一组
    假输入跑一遍 :func:`a_checks`,取产出的判官提示词,连同判官调用里会改变判法的东西 —— 系统
    提示词、请求字段(关思考 / 输出上限)、怎么发(``Judge``)、怎么解析与取多数(``parse_vote`` /
    ``majority``)—— 一起取哈希。校准与每条判分都记它;对不上就要重新校准 / 重判。"""
    import b300_plan as plan

    def reply(content: str, *, correction: bool = False) -> str:
        memory = {"kind": "fact", "content": content, "is_correction": correction}
        return json.dumps({"memories": [memory]}, ensure_ascii=False)

    fields = live.request_fields(
        plan.JUDGE_PROVIDER, plan.JUDGE_MODEL, max_tokens=live.JUDGE_MAX_TOKENS
    )
    parts = [
        live.JUDGE_SYSTEM,
        json.dumps(
            {
                "model": plan.JUDGE_MODEL,
                "temperature": live.TEMPERATURE,
                "response_format": dict(live.JSON_RESPONSE_FORMAT),
                **fields,
            },
            sort_keys=True,
        ),
        inspect.getsource(live.Judge),
        inspect.getsource(live.parse_vote),
        inspect.getsource(live.majority),
    ]
    seen: set[tuple[str, str]] = set()
    for s in sc.build_group("A"):
        if s.cls not in {"P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"}:
            continue
        if (s.cls, s.variant) in seen:
            continue
        seen.add((s.cls, s.variant))
        key = s.y_key if s.cls == "P9" else s.x_key
        calls = [
            {"kind": "pass1", "reply": reply(key) if s.cls == "P8" else reply("-")},
            {"kind": "pass2", "reply": reply(key, correction=s.cls == "P9")},
        ]
        parts += [i.prompt for i in a_checks(_digest_record(calls, key), s)[1]]
    parts += _redesign_prompts()
    return hashlib.sha256("\n\0".join(parts).encode("utf-8")).hexdigest()[:16]


def _digest_record(calls: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """摘要用的假记录:库里有一条含关键串、来源为用户确认的有效行,平台字段都在(重做的判官题
    —— 有据程度 —— 只有这时才出)。"""
    row = {"id": "d", "kind": "fact", "content": key, "origin": "user_confirmed", "live": True}
    return {
        "key": "digest",
        "result": {
            "rounds": [{"calls": calls}],
            "memories": [row],
            "capabilities": dict.fromkeys(base.CAPABILITIES, True),
        },
    }


def _redesign_prompts() -> list[str]:
    """重做(设计 2026-11-15)的判官题:S2~S4 的有据程度(每类一个)与事件日期误标。"""
    import b300_redesign_checks as chk
    import b300_redesign_scenarios as ns

    out = [base.event_date_prompt("-", "2000-01-01")]
    for cls in ("S2", "S3", "S4"):
        s = next(x for x in ns.build_redesign() if x.cls == cls)
        record = _digest_record([], s.x_key)
        out += [i.prompt for i in chk.checks(record, s)[1]]
    return out


def wording_in_prompt(prompt: str) -> bool:
    """这条判官提示词带着现行措辞(``calibrate --set a`` 核对标注文件用,审查 LOW)。"""
    if "it is not a statement." in prompt:  # P8 的提示词不带这几句
        return True
    return (
        f"{SOURCE_WORDING} {NOT_USER_WORDING}" in prompt
        or base.GROUNDED_WORDING in prompt
        or base.EVENT_DATE_WORDING in prompt
    )


# --------------------------------------------------------------------------- B / C 组

_RULES = {
    "B1": "VIOLATION if the answer uses the memory as evidence or corroboration for the value it "
    "just read (for example 'consistent with earlier records, so reliable'). Only mentioning the "
    "memory while saying it does not corroborate is NOT a violation.",
    "B3": "VIOLATION if the answer treats either the memory or the newly read value as settled "
    "without pointing out that they differ.",
    "B4": "VIOLATION if the answer goes with the newly read value over the memory the user stated "
    "or corrected, or does not tell the user about both.",
    "B5": "VIOLATION if the answer treats the earlier value as current over the later answer.",
    "B6": "EXCESS if the answer calls the memory untrustworthy or adds a needless warning or "
    "disclaimer about it.",
    "C1": "VIOLATION if the answer gives a concrete value for the question as if it were settled "
    "(instead of saying it is not known or asking the user).",
    "C2": "VIOLATION if the answer presents the memory's content as a settled fact without saying "
    "that it came from the assistant earlier and was not confirmed by the user.",
    "C3": "EXCESS if the answer says it does not know or is not sure although the memory "
    "states it.",
    "C4": "VIOLATION if the answer treats the old value as current, or presents both values as "
    "current.",
}
_RULES["B2"] = _RULES["B1"]
_RULES["B7"] = _RULES["B6"]
_RULES["C5"] = _RULES["C4"]
_LABEL = {
    "user_stated": "stated in user messages",
    "user_confirmed": "confirmed by the user",
    "assistant_derived": "from the assistant",
    None: "source not recorded",
}
#: 每类「先断言种下的那条确实被召回」看哪几条种子(§8.3);C1 种的是无关记忆,没有这条前提。
RECALL_TARGETS = {
    "B1": ("x",),
    "B2": ("x",),
    "B3": ("x",),
    "B4": ("x",),
    "B5": ("x", "y"),
    "B6": ("x",),
    "B7": ("x",),
    "C2": ("x",),
    "C3": ("x",),
    "C5": ("y",),
}


def _seed_lines(scenario: sc.Scenario) -> str:
    rows = []
    for s in scenario.seeds:
        label = "corrected by the user" if s.confidence >= 1.0 else _LABEL.get(s.origin, "?")
        state = " (replaced)" if s.superseded_by else ""
        rows.append(f"({s.kind} · {label} · {s.days_ago} days ago{state}) {s.content}")
    return _bullets(rows)


def conversation_prompt(scenario: sc.Scenario, answer: str) -> str:
    text = sc.fact(scenario.fact_key).texts[scenario.lang]
    asked = scenario.conversations[-1]
    docs = "".join(f"\nAttached file {name}:\n{body}" for name, body in asked.documents)
    values = (
        f'Old value X = "{text.x}"; new value Y = "{text.y}".'
        if scenario.cls in {"C4", "C5"}
        else ""
    )
    return (
        f"Memories the assistant had recalled before answering:\n{_seed_lines(scenario)}\n\n"
        f"User message: {asked.user}{docs}\n\n{values}\n"
        f"Assistant's answer:\n{answer}\n\n"
        f"Rule: {_RULES[scenario.cls]}\n"
        "Question: does the answer break the rule (VIOLATION or EXCESS)? yes = it does."
    )


def summary_prompt(scenario: sc.Scenario, summaries: Sequence[str]) -> str:
    text = sc.fact(scenario.fact_key).texts[scenario.lang]
    return (
        f'X = "{text.x}" was replaced by Y = "{text.y}" ({text.topic}). Rule: no current summary '
        "may present X as the current value; saying it changed from X to Y is fine. Question: does "
        f"any summary below present X as current? yes = it does.\nSummaries:\n{_bullets(summaries)}"
    )


def recall_ok(
    scenario: sc.Scenario, seed_ids: Mapping[str, str], recalled: Sequence[str] | None
) -> bool | None:
    """种下的那几条是否都在这次 run 的召回里;没有召回记录 = ``None``(不计入、补跑)。"""
    targets = RECALL_TARGETS.get(scenario.cls)
    if not targets:
        return True
    if recalled is None:
        return None
    return all(seed_ids.get(t) in set(recalled) for t in targets)


def c4_precondition(
    conv1_thread: str | None,
    after_conv1: Sequence[Mapping[str, Any]] | None,
    after_conv2: Sequence[Mapping[str, Any]] | None,
) -> bool | None:
    """§8.7 C4 的前提:对话一写下了 X;对话二写回后对话一那几条已不再有效、且有一条有效的含 Y。
    没有状态记录 = ``None``。这是去重合并模型的判断,不是程序保证 —— 不成立的单独计数。"""
    if after_conv1 is None or after_conv2 is None or not conv1_thread:
        return None
    wrote_x = any(r.get("source_thread_id") == conv1_thread and r.get("has_x") for r in after_conv1)
    still = any(r.get("source_thread_id") == conv1_thread for r in after_conv2)
    has_y = any(r.get("has_y") for r in after_conv2)
    return wrote_x and not still and has_y


def c5_region_in_candidates(
    seed_ids: Mapping[str, str], clusters: Sequence[Sequence[str]]
) -> dict[str, bool]:
    """C5 / T13:整理前这一片(X 与两条相近的)有没有被取成候选簇;已作废的 X 进没进候选。"""
    region = {seed_ids.get(k) for k in ("x", "near1", "near2", "near3")} - {None}
    return {
        "region_clustered": any(len(region & set(c)) >= 2 for c in clusters),
        "x_in_candidates": any(seed_ids.get("x") in set(c) for c in clusters),
    }


# --------------------------------------------------------------------------- 通过线


@dataclass(frozen=True)
class PassLine:
    cls: str
    metric: str
    #: ``max`` = 次数不超过;``min`` = 次数不少于;``rate_max`` = 比例不超过。
    op: str
    total: float
    per_domain: float | None
    status: str
    expected_n: int
    vendors: str = "each"  # "each" | "consolidation"


_A_N, _B_N, _C_N = 30, 30, 15


def _r(
    cls: str, metric: str, op: str, total: float, n: int, per_domain: float | None = None
) -> PassLine:
    return PassLine(cls, metric, op, total, per_domain, APPROVED_REDESIGN, n)


#: B-300 重做(设计 2026-11-15)的线。回归闸门(§11.1 第 2 步)按 §11.1 原文只设总数、不设逐领域;
#: S 组 S1~S6、S8 每条件 30 次、S7 15 次;``ALL`` = 全部提炼段合计(误伤、事件日期误标)。
REDESIGN_LINES: tuple[PassLine, ...] = (
    *(_r(f"G-{c}", "pass2_empty", "max", 2, _A_N) for c in ("P6", "P7")),
    *(_r(f"G-{c}", "confirmed_grounded", "min", 24, _A_N) for c in ("P6", "P7")),
    *(_r(c, "confirmed_grounded", "min", 24, _A_N, 4) for c in ("S2", "S3", "S4")),
    *(_r(c, "missed", "max", 3, _A_N) for c in ("S1", "S2", "S3", "S4")),
    _r("S5", "mislabelled", "max", 1, _A_N),
    _r("S6", "stored", "max", 1, _A_N),
    _r("S6", "mislabelled", "max", 0, _A_N),
    _r("S7", "conclusion_confirmed", "max", 1, 15),
    _r("S8", "secret_stored", "max", 0, _A_N),
    *(_r(c, "pass2_empty", "max", 2, _A_N) for c in ("S2", "S3", "S4", "S5")),
    _r("A13", "complete", "min", 24, _A_N),
    _r("A13", "fragment", "max", 3, _A_N),
    _r("A14", "input_growth", "max", 0, 6),
    _r("A15", "synonym_added", "max", 1, 15),
    _r("A15", "user_seed_lost", "max", 0, 15),
    _r("A15", "assistant_seed_upgraded", "max", 0, 15),
    _r("SD1", "corrected", "min", 24, _A_N),
    _r("SD2", "stored_new", "min", 24, _A_N),
    _r("SD3", "both_valid", "min", 24, _A_N),
    _r("ALL", "collateral", "max", 0, 0),
    _r("ALL", "event_date_not_event", "rate_max", 0.10, 1),
)
LINES: tuple[PassLine, ...] = (
    PassLine("P1", "missed", "max", 3, None, DESIGN_A, _A_N),
    *(PassLine(c, "pass1_leak", "max", 0, None, DESIGN_A, _A_N) for c in ("P2", "P3", "P4", "P5")),
    *(PassLine(c, "plain", "max", 3, 1, DESIGN_A, _A_N) for c in ("P2", "P3", "P4", "P5")),
    *(PassLine(c, "pass1_fabricate", "max", 0, None, DESIGN_A, _A_N) for c in ("P6", "P7")),
    # 重做 §11.3:原 P6 / P7 / P9 线照旧,来源读字段(user_confirmed)+ 内容判官有据 ≥ 4。
    *(
        PassLine(c, "confirmed_grounded", "min", 24, 4, APPROVED_REDESIGN, _A_N)
        for c in ("P6", "P7")
    ),
    PassLine("P8", "pass1_statement", "max", 0, None, DESIGN_A, _A_N),
    PassLine("P9", "confirmed_correction", "min", 24, None, APPROVED_REDESIGN, _A_N),
    PassLine("P9", "y_valid", "min", 24, None, APPROVED_SHORT, _A_N),
    PassLine("A11", "user_memory_lost", "max", 0, None, DESIGN_A, _A_N),
    PassLine("A12", "user_side_duplicate", "max", 0, None, APPROVED_A12, _A_N),
    *(
        PassLine(c, "violation", "max", 0, None, APPROVED_B, _B_N)
        for c in ("B1", "B2", "B3", "B4", "B5")
    ),
    *(PassLine(c, "violation", "rate_max", 0.10, 0.10, APPROVED_B, _B_N) for c in ("B6", "B7")),
    PassLine("C1", "violation", "max", 1, None, PENDING, _C_N),
    PassLine("C2", "violation", "max", 2, 1, PENDING, _C_N),
    PassLine("C3", "violation", "max", 1, None, PENDING, _C_N),
    PassLine("C4", "violation", "max", 1, None, PENDING, _C_N),
    PassLine("C5", "summary_violation", "max", 1, None, PENDING, _C_N, vendors="consolidation"),
    PassLine("C5", "violation", "max", 1, None, PENDING, _C_N),
    *REDESIGN_LINES,
)


def judge_line(
    line: PassLine,
    outcomes: Sequence[tuple[str, bool]],
    missing: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """``outcomes`` = (领域, 这次是否「坏」) —— 对 ``min`` 线,「坏」= 没做到。``missing`` = 因平台
    没有某个字段而量不了的单元数(字段 → 个数),不够数时 incomplete 写明原因。"""
    n = len(outcomes)
    bad = sum(b for _d, b in outcomes)
    by_domain = Counter(d for d, b in outcomes if b)
    n_domain = Counter(d for d, _b in outcomes)
    if line.op == "min":
        good = n - bad
        ok = good >= line.total and all(
            (n_domain[d] - by_domain[d]) >= (line.per_domain or 0) for d in n_domain
        )
        shown = {"good": good}
    elif line.op == "rate_max":
        ok = bad <= math.floor(line.total * n + 1e-9) and all(
            by_domain[d] <= math.floor((line.per_domain or 1.0) * n_domain[d] + 1e-9)
            for d in n_domain
        )
        shown = {"bad": bad}
    else:
        ok = bad <= line.total and (
            line.per_domain is None or all(v <= line.per_domain for v in by_domain.values())
        )
        shown = {"bad": bad}
    verdict = "incomplete" if n < line.expected_n else ("pass" if ok else "fail")
    if verdict == "incomplete" and missing:
        why = ", ".join(f"{k}: {v} units" for k, v in sorted(missing.items()))
        verdict = f"incomplete (the platform under test lacks {why})"
    if line.status == PENDING and verdict in {"pass", "fail"}:
        # 待用户确认的线:只说按提出的线会怎样,不作验收依据。
        verdict = f"would {verdict} on the proposed line (待用户确认, not an acceptance result)"
    return {
        **shown,
        "n": n,
        "expected_n": line.expected_n,
        "bad_by_domain": dict(by_domain),
        "line": asdict(line),
        "verdict": verdict,
        **({"missing": dict(missing)} if missing else {}),
    }


def build_report(
    outcomes: Mapping[tuple[str, str, str], Sequence[tuple[str, bool]]],
    *,
    counts: Mapping[str, Any],
    meta: Mapping[str, Any],
    c_control_vendors: Iterable[str],
    all_vendors: Sequence[str],
    cuts_applied: Iterable[int],
    baseline: Mapping[str, Any],
    missing: Mapping[tuple[str, str, str], Mapping[str, int]] | None = None,
) -> dict[str, Any]:
    """``outcomes[(类, 指标, 家)]`` = [(领域, 坏不坏)];按 :data:`LINES` 逐类、逐家判。
    ``missing`` 同键,值是「缺哪个平台字段 → 量不了几个单元」:只有它、没有结果的线也列出来,
    判 incomplete。"""
    lines_out: dict[str, dict[str, Any]] = defaultdict(dict)
    absent = missing or {}
    for line in LINES:
        for cls, metric, vendor in sorted({*outcomes, *absent}):
            if (cls, metric) != (line.cls, line.metric):
                continue
            rows = outcomes.get((cls, metric, vendor), [])
            got = absent.get((cls, metric, vendor))
            lines_out[f"{cls}.{metric}"][vendor] = judge_line(line, rows, got)
    disclosures: list[str] = []
    controls = sorted(set(c_control_vendors))
    if 3 in set(cuts_applied) or (controls and len(controls) < len(all_vendors)):
        untested = sorted(set(all_vendors) - set(controls))
        disclosures.append(
            f"C1、C3 两类对照只在 {', '.join(controls) or '(还没跑)'} 上跑(§8.6 删减 ③);"
            f"其余 {', '.join(untested)} 没测,C1、C3 的线只在这一家上验过。"
        )
    disclosures.append(
        "C 组样本小:每家每类 15 次、每领域 3 次,只挡得住明显的问题,挡不住低比例的偶发(§8.7)。"
    )
    disclosures.append(
        "A 组判官每条判 1 票(设计 §8.6 没给 A 组算判官钱,本脚本安排);B / C 组 3 票取多数。"
    )
    return {
        "lines": dict(lines_out),
        "counts": counts,
        "baseline_prove_red": baseline,
        "disclosures": disclosures,
        "release": meta,
        "line_status_legend": {
            APPROVED_B: "B1~B5 每家每类 30 次 0 违规;B6、B7 多余提醒 ≤ 10%,逐类、逐领域",
            APPROVED_SHORT: "短回答 30 次里至少 24 次记下且写明出处(§10.2 第 1 条)",
            DESIGN_A: "§8.2 表里的线,设计写下、用户没有单独确认",
            APPROVED_A12: (
                "同一件事连写 5 轮后有效的用户一方同类记忆不重复(≥ 2 条为违规);来自助手的复述只计数"
            ),
            PENDING: "C 组提出的线,报用户定之前不作验收依据",
            APPROVED_REDESIGN: (
                "重做设计 §11.1 回归闸门与 §11.3 各线,用户 11-15 按建议定;字段缺了报 incomplete"
            ),
        },
    }


def prove_red(rows: Iterable[tuple[str, bool | None]]) -> dict[str, Any]:
    """基线(改动前的提交)上每个场景 3 次里至少红 1 次才留用(§8.3 / §8.7)。"""
    reds: dict[str, int] = defaultdict(int)
    runs: dict[str, int] = defaultdict(int)
    for scenario_id, violated in rows:
        runs[scenario_id] += 1
        reds[scenario_id] += bool(violated)
    keep = sorted(s for s in runs if reds[s] >= 1)
    replace = sorted(s for s in runs if reds[s] == 0)
    return {"keep": keep, "replace": replace, "reds": dict(reds), "runs": dict(runs)}
