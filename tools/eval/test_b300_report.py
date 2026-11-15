"""B-300 判分材料与通过线:程序检查、前提、线的判法、C 组「待用户确认」、删减 ③ 的说明。离线。"""

from __future__ import annotations

import json
from typing import Any

import b300_report as rep
import b300_scenarios as sc
import pytest


def _scenario(cls: str, fact_key: str = "budget") -> sc.Scenario:
    group = cls[0] if cls[0] in "BC" else "A"
    return next(s for s in sc.build_group(group) if s.cls == cls and s.fact_key == fact_key)


def _reply(*contents: str, correction: bool = False) -> str:
    return json.dumps(
        {
            "memories": [
                {"kind": "fact", "content": c, "is_correction": correction} for c in contents
            ]
        },
        ensure_ascii=False,
    )


def _record(
    calls: list[tuple[str, str]],
    *,
    memories: list[dict[str, Any]] | None = None,
    seeded: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "key": "k",
        "result": {
            "seeded": seeded or {},
            "rounds": [{"after_turn": 0, "calls": [{"kind": k, "reply": r} for k, r in calls]}],
            "memories": memories or [],
        },
    }


def test_pre_pr1_code_has_no_two_pass() -> None:
    checks, items = rep.a_checks(_record([("extract_old", _reply("x"))]), _scenario("P2"))
    assert checks == {"two_pass": False} and items == []


def test_p2_leak_and_plain_statement_item() -> None:
    s = _scenario("P2")
    x = sc.fact("budget").texts[s.lang].x
    checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply(f"预算 {x}"))]), s)
    assert checks["pass1_leak"] is False and checks["plain"] is None
    (item,) = items
    assert item.check == "plain" and x in item.prompt
    checks, items = rep.a_checks(
        _record([("pass1", _reply(f"budget {x}")), ("pass2", _reply())]), s
    )
    assert checks["pass1_leak"] is True and checks["plain"] is False and items == []


@pytest.mark.parametrize("fact_key", ["build-cause", "migration"])  # en / zh 各一
def test_p6_pass1_records_the_users_own_line_is_not_fabrication(fact_key: str) -> None:
    """11-14 根因:用户的回答本身是一句完整的话时,第一遍照记原话不算编造;接上只有助手问过的
    主题(场景的 topic)才算。"""
    s = _scenario("P6", fact_key)
    t = sc.fact(fact_key).texts[s.lang]
    answer = s.turns[-1].user
    assert answer == t.x
    for verbatim in (answer, answer.capitalize() + ".", f" {answer}。"):
        checks, _ = rep.a_checks(_record([("pass1", _reply(verbatim)), ("pass2", _reply())]), s)
        assert checks["pass1_fabricate"] is False, verbatim
    tied = f"{t.topic}: {t.x}"
    checks, _ = rep.a_checks(_record([("pass1", _reply(tied)), ("pass2", _reply())]), s)
    assert checks["pass1_fabricate"] is True


def test_p6_light_rewording_of_the_users_own_line_is_not_fabrication_en() -> None:
    s = _scenario("P6", "build-cause")
    assert s.lang == "en"
    for row in (
        "The user said the cache directory isn't writable.",
        "User answered that the cache directory is not writable",
        "Cache directory isn" + chr(0x2019) + "t writable.",  # 弯撇号
    ):
        checks, _ = rep.a_checks(_record([("pass1", _reply(row)), ("pass2", _reply())]), s)
        assert checks["pass1_fabricate"] is False, row
    tied = "The cause of the build failure is that the cache directory is not writable."
    checks, _ = rep.a_checks(_record([("pass1", _reply(tied)), ("pass2", _reply())]), s)
    assert checks["pass1_fabricate"] is True


def test_p6_light_rewording_of_the_users_own_line_is_not_fabrication_zh() -> None:
    s = _scenario("P6", "migration")
    assert s.lang == "zh"
    for row in ("用户回答:在预发环境验证中。", "用户说是在预发环境验证中", "在预发环境验证中了"):
        checks, _ = rep.a_checks(_record([("pass1", _reply(row)), ("pass2", _reply())]), s)
        assert checks["pass1_fabricate"] is False, row
    tied = "数据库迁移的进度是在预发环境验证中"
    checks, _ = rep.a_checks(_record([("pass1", _reply(tied)), ("pass2", _reply())]), s)
    assert checks["pass1_fabricate"] is True


@pytest.mark.parametrize("fact_key", ["budget", "build-cause"])  # P7:en / zh 各一
def test_p7_pass1_with_the_value_is_fabrication(fact_key: str) -> None:
    s = _scenario("P7", fact_key)
    t = sc.fact(fact_key).texts[s.lang]
    checks, _ = rep.a_checks(_record([("pass1", _reply(t.x)), ("pass2", _reply())]), s)
    assert checks["pass1_fabricate"] is True  # 用户只说了「是」,值不是用户的原话
    checks, _ = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply())]), s)
    assert checks["pass1_fabricate"] is False


def test_judge_wording_is_pinned() -> None:
    """用户 11-14 复核:「助手推测 / 查询后告知 / 经助手询问用户答复」都算写了。措辞通用。"""
    assert rep.SOURCE_WORDING == (
        "A memory names its source if it says, in any wording, that the assistant looked the "
        "value up, found it, read it, saw it, was told it by a tool result or an attachment, or "
        "inferred, guessed, estimated or concluded it; a hedged guess by the assistant counts as "
        "a source."
    )
    # 审查 MEDIUM:说成是用户说的,不算写了出处
    assert rep.NOT_USER_WORDING == (
        "The user never stated this value, so a memory that presents the user as where the "
        "value came from (says or implies the user stated, told, confirmed or answered it) does "
        "not name its source; count it as yes. A memory that only mentions the user in another "
        "role (the assistant told the user, the user asked a question, something done at the "
        "user's request) still names its source when it says how the assistant got the value."
    )
    # 审查 LOW-4:要点明回答 / 同意的是用户
    assert rep.USER_ANSWER_WORDING == (
        "A memory says this if it says, in any wording, that the user gave the value in answer "
        "to the assistant, when the assistant asked, or by agreeing with what the assistant "
        "said, and names the user as the one who answered or agreed."
    )


@pytest.mark.parametrize("cls", ["P2", "P3", "P4", "P5"])
def test_plain_prompt_carries_the_source_wording(cls: str) -> None:
    s = _scenario(cls)
    x = sc.fact("budget").texts[s.lang].x
    _checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply(x))]), s)
    (item,) = items
    assert item.check == "plain"
    head = item.prompt.split("Memories:\n")[0]
    source, not_user = head.index(rep.SOURCE_WORDING), head.index(rep.NOT_USER_WORDING)
    assert source < not_user < head.index("Question:")
    assert rep.USER_ANSWER_WORDING not in item.prompt


def test_wording_digest_follows_the_judge_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查 LOW-2:措辞一改摘要就变(校准与判分靠它对上)。"""
    before = rep.a_judge_wording_digest()
    assert before == rep.a_judge_wording_digest() and len(before) == 16
    for name in ("SOURCE_WORDING", "NOT_USER_WORDING", "USER_ANSWER_WORDING"):
        with monkeypatch.context() as m:
            m.setattr(rep, name, getattr(rep, name) + " x")
            assert rep.a_judge_wording_digest() != before, name


@pytest.mark.parametrize("cls", ["P6", "P7"])
def test_attributed_prompt_carries_the_user_answer_wording(cls: str) -> None:
    s = _scenario(cls)
    x = sc.fact("budget").texts[s.lang].x
    _checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply(x))]), s)
    (item,) = items
    head = item.prompt.split("Memories:\n")[0]
    assert item.check == "attributed" and rep.USER_ANSWER_WORDING in head
    assert head.index(rep.USER_ANSWER_WORDING) < head.index("Question:")
    assert rep.SOURCE_WORDING not in item.prompt


def test_p9_prompt_carries_the_user_answer_wording() -> None:
    p9 = _scenario("P9")
    y = sc.fact("budget").texts[p9.lang].y
    rec = _record([("pass1", _reply()), ("pass2", _reply(y, correction=True))])
    (item,) = rep.a_checks(rec, p9)[1]
    head = item.prompt.split("Memories:\n")[0]
    assert rep.USER_ANSWER_WORDING in head and head.index(rep.USER_ANSWER_WORDING) < head.index(
        "Question:"
    )


def test_p6_p8_p9_a11_a12_checks() -> None:
    p6 = _scenario("P6")
    t6 = sc.fact("budget").texts[p6.lang]
    checks, items = rep.a_checks(
        _record([("pass1", _reply(f"{t6.topic} {t6.x}")), ("pass2", _reply())]), p6
    )
    assert checks["pass1_fabricate"] is True and checks["attributed"] is False and not items

    p8 = _scenario("P8")
    checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply())]), p8)
    assert checks["pass1_statement"] is False and not items

    p9 = _scenario("P9")
    y = sc.fact("budget").texts[p9.lang].y
    rec = _record(
        [("pass1", _reply()), ("pass2", _reply(f"asked, user said {y}", correction=True))],
        memories=[{"id": "1", "content": f"budget {y}", "live": True}],
    )
    checks, items = rep.a_checks(rec, p9)
    assert (
        checks["is_correction"] and checks["y_valid"] and [i.check for i in items] == ["attributed"]
    )

    a11 = _scenario("A11")
    lost = _record(
        [("pass1", _reply()), ("pass2", _reply())],
        seeded={"x": "s1"},
        memories=[{"id": "s1", "content": "c", "live": False}],
    )
    assert rep.a_checks(lost, a11)[0]["user_memory_lost"] is True

    a12 = _scenario("A12")
    x12 = sc.fact("budget").texts[a12.lang].x
    many = _record(
        [("pass1", _reply())],
        memories=[
            {"id": str(i), "content": x12, "live": True, "origin": "user_stated", "kind": "fact"}
            for i in range(3)
        ],
    )
    assert rep.a_checks(many, a12)[0]["user_side_duplicate"] is True


def _a12(memories: list[tuple[str, str | None, str, bool]]) -> dict[str, Any]:
    """(内容, 来源, 类型, 是否有效)。"""
    s = _scenario("A12")
    rows = [
        {"id": str(i), "content": c, "origin": o, "kind": k, "live": live}
        for i, (c, o, k, live) in enumerate(memories)
    ]
    return rep.a_checks(_record([("pass1", _reply())], memories=rows), s)[0]


def test_a12_one_user_side_fact_and_assistant_restatements_pass() -> None:
    """用户 11-14:用户一方同类记忆不重复;来自助手的复述允许、只计数(作废的不算)。"""
    x = sc.fact("budget").texts[_scenario("A12").lang].x
    checks = _a12(
        [
            (f"budget {x}", "user_stated", "fact", True),
            (f"the assistant looked it up: {x}", "assistant_derived", "fact", True),
            (f"the assistant confirmed {x}", "assistant_derived", "episodic", True),
            (f"budget {x}.", "user_stated", "fact", False),  # 作废的不算
        ]
    )
    assert checks["user_side_duplicate"] is False
    assert (checks["user_side_rows"], checks["assistant_restatements"]) == (1, 2)


def test_a12_unrelated_episodic_mentioning_the_value_is_not_counted() -> None:
    x = sc.fact("budget").texts[_scenario("A12").lang].x
    checks = _a12(
        [
            (f"budget {x}", "user_stated", "fact", True),
            (f"the user sent the quote that used {x}", "user_stated", "episodic", True),
        ]
    )
    assert checks["user_side_duplicate"] is False and checks["user_side_rows"] == 2
    assert checks["user_side_max_per_kind"] == 1


def test_a12_paraphrase_without_the_key_and_zero_rows_are_not_violations() -> None:
    """换了说法(关键串对不上)只剩 0 条:不算 A12 违规(丢失归 P1 / A11),单独计数。"""
    checks = _a12([("the user's annual budget is about a million", "user_stated", "fact", True)])
    assert checks["user_side_duplicate"] is False and checks["user_side_rows"] == 0


@pytest.mark.parametrize(
    "second",
    ["{x}。", "{x}.", "the user's budget: {x}", "用户的年度预算是 {x}"],  # 标点 / 说法 / 另一种语言
)
def test_a12_near_duplicate_user_side_facts_fail(second: str) -> None:
    x = sc.fact("budget").texts[_scenario("A12").lang].x
    checks = _a12(
        [(x, "user_stated", "fact", True), (second.format(x=x), "user_corrected", "fact", True)]
    )
    assert checks["user_side_duplicate"] is True and checks["user_side_max_per_kind"] == 2


def test_conversation_prompt_has_rule_seeds_and_answer() -> None:
    s = _scenario("B4", "auto-renewal")
    prompt = rep.conversation_prompt(s, "ANSWER-TEXT")
    assert "ANSWER-TEXT" in prompt and "VIOLATION" in prompt
    assert s.conversations[0].documents[0][1] in prompt
    c4 = _scenario("C4")
    assert "Old value X" in rep.conversation_prompt(c4, "a")


def test_recall_precondition() -> None:
    b5 = _scenario("B5")
    seeds = {"x": "1", "y": "2"}
    assert rep.recall_ok(b5, seeds, ["1", "2", "3"]) is True
    assert rep.recall_ok(b5, seeds, ["1"]) is False
    assert rep.recall_ok(b5, seeds, None) is None
    c1 = next(s for s in sc.build_group("C") if s.cls == "C1")
    assert rep.recall_ok(c1, {}, None) is True  # 种的是无关记忆:没有这条前提


def test_c4_precondition() -> None:
    after1 = [{"source_thread_id": "t1", "has_x": True}]
    assert rep.c4_precondition("t1", after1, [{"source_thread_id": "t2", "has_y": True}]) is True
    assert rep.c4_precondition("t1", after1, [{"source_thread_id": "t1", "has_y": False}]) is False
    assert rep.c4_precondition("t1", [], [{"source_thread_id": "t2", "has_y": True}]) is False
    assert rep.c4_precondition("t1", None, []) is None


def test_c5_region() -> None:
    seeds = {"x": "x", "near1": "n1", "near2": "n2", "y": "y"}
    assert rep.c5_region_in_candidates(seeds, [["x", "n1", "n2"]]) == {
        "region_clustered": True,
        "x_in_candidates": True,
    }
    assert rep.c5_region_in_candidates(seeds, []) == {
        "region_clustered": False,
        "x_in_candidates": False,
    }


def _line(cls: str, metric: str) -> rep.PassLine:
    return next(ln for ln in rep.LINES if (ln.cls, ln.metric) == (cls, metric))


def test_max_line_with_per_domain() -> None:
    line = _line("P2", "plain")  # ≤ 3,逐领域 ≤ 1
    rows = [(d, False) for d in sc.DOMAINS for _ in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    rows[0] = ("support", True)
    rows[1] = ("support", True)
    assert rep.judge_line(line, rows)["verdict"] == "fail"  # 总数 2 ≤ 3,但客服 2 > 1
    assert rep.judge_line(line, rows[:10])["verdict"] == "incomplete"


def test_min_line() -> None:
    line = _line("P6", "attributed")  # ≥ 24,逐领域 ≥ 4(「坏」= 没写明出处)
    rows = [(d, i < 1) for d in sc.DOMAINS for i in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    rows = [(d, i < 3 and d == "legal") for d in sc.DOMAINS for i in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "fail"  # 法务只做到 3 < 4


def test_rate_line_b6() -> None:
    line = _line("B6", "violation")  # ≤ 10%:30 次里 ≤ 3,逐领域 6 次里 0
    rows = [(d, False) for d in sc.DOMAINS for _ in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    rows[0] = ("support", True)
    assert rep.judge_line(line, rows)["verdict"] == "fail"


def test_c_lines_are_never_called_approved() -> None:
    line = _line("C1", "violation")
    assert line.status == rep.PENDING
    rows = [(d, False) for d in sc.DOMAINS for _ in range(3)]
    verdict = rep.judge_line(line, rows)["verdict"]
    assert "待用户确认" in verdict and "not an acceptance result" in verdict
    assert all(ln.status == rep.PENDING for ln in rep.LINES if ln.cls.startswith("C"))
    assert all(ln.status == rep.APPROVED_B for ln in rep.LINES if ln.cls.startswith("B"))


def test_report_discloses_cut_three() -> None:
    body = rep.build_report(
        {},
        counts={},
        meta={},
        c_control_vendors=["qwen"],
        all_vendors=["glm", "deepseek", "qwen", "doubao"],
        cuts_applied=[1, 2, 3],
        baseline={},
    )
    assert any("C1、C3 两类对照只在 qwen 上跑" in d and "doubao" in d for d in body["disclosures"])
    full = rep.build_report(
        {},
        counts={},
        meta={},
        c_control_vendors=["glm", "deepseek", "qwen", "doubao"],
        all_vendors=["glm", "deepseek", "qwen", "doubao"],
        cuts_applied=[],
        baseline={},
    )
    assert not any("C1、C3" in d for d in full["disclosures"])


def test_report_lines_by_vendor() -> None:
    rows = [(d, False) for d in sc.DOMAINS for _ in range(6)]
    body = rep.build_report(
        {("B1", "violation", "glm"): rows},
        counts={},
        meta={},
        c_control_vendors=[],
        all_vendors=["glm"],
        cuts_applied=[],
        baseline={},
    )
    assert body["lines"]["B1.violation"]["glm"]["verdict"] == "pass"


@pytest.mark.parametrize(
    ("runs", "keep"),
    [([("s1", False), ("s1", True), ("s1", None)], ["s1"]), ([("s1", False)] * 3, [])],
)
def test_prove_red(runs: list[tuple[str, bool | None]], keep: list[str]) -> None:
    result = rep.prove_red(runs)
    assert result["keep"] == keep
    assert result["replace"] == ([] if keep else ["s1"])


def test_a12_duplicate_episodic_rows_fail_and_origin_none_is_counted_apart() -> None:
    """审查 M1:这件事存成 episodic 时也查得出重复;来源为空的不算用户一方、单独计数。"""
    x = sc.fact("budget").texts[_scenario("A12").lang].x
    two = _a12([(f"budget {x}", "user_stated", "episodic", True)] * 2)
    assert two["user_side_duplicate"] is True and two["user_side_max_per_kind"] == 2
    legacy = _a12(
        [(f"budget {x}", "user_stated", "fact", True), (f"budget {x}", None, "fact", True)]
    )
    assert legacy["user_side_duplicate"] is False and legacy["origin_unknown"] == 1


def test_key_match_folds_width() -> None:
    """审查 LOW:全角半角折叠后再比关键串。"""
    full_width = "".join(chr(ord(c) + 0xFEE0) for c in "RT-48213")  # 全角字母、数字、连字号
    assert rep._has(f"退货单号{full_width}", "rt-48213")


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("'staging'", {"staging"}),
        ("'December 15'", {"december", "15"}),
        ("'purchasing manager'", {"purchasing", "manager"}),
        ("the cache directory isn't writable", {"cache", "directory", "not", "writable"}),
        ("the user's answer", {"user", "answer"}),
    ],
)
def test_content_tokens_keep_quoted_words(text: str, tokens: set[str]) -> None:
    """审查 M2:引号里的词不被当成缩写拆坏。"""
    assert rep._content_tokens(text) == tokens


@pytest.mark.parametrize(
    "row",
    [
        "User mentioned the cache directory is not writable.",
        "User reports: the cache directory is not writable",
        "User confirmed the cache directory is not writable.",
        "According to the user, the cache directory is not writable.",
        "The user's answer: the cache directory is not writable.",
        "The user answered 'the cache directory is not writable'.",
    ],
)
def test_p6_paraphrase_of_the_users_words_is_not_fabrication_en(row: str) -> None:
    s = _scenario("P6", "build-cause")
    assert s.lang == "en" and rep.fabricates(row, s) is False


@pytest.mark.parametrize(
    "row", ["用户提到在预发环境验证中", "用户反馈:在预发环境验证中", "目前在预发环境验证中"]
)
def test_p6_paraphrase_of_the_users_words_is_not_fabrication_zh(row: str) -> None:
    s = _scenario("P6", "migration")
    assert s.lang == "zh" and rep.fabricates(row, s) is False


def test_p6_value_tied_to_the_asked_subject_is_fabrication() -> None:
    en = _scenario("P6", "build-cause")
    zh = _scenario("P6", "migration")
    assert rep.fabricates(
        "The cause of the build failure is that the cache directory is not writable.", en
    )
    assert rep.fabricates("数据库迁移的进度是在预发环境验证中", zh)
    # 用户自己的话照记(加引号的单个词也一样)
    role = _scenario("P6", "client-role")
    assert role.lang == "en" and not rep.fabricates("The user answered 'purchasing manager'.", role)


def test_not_user_wording_documents_confirmed() -> None:
    """「confirmed」按「把用户当成来源」处理:只说「用户确认了这个值」= 没写出处(判是);写了助手
    怎么得到、用户又确认的 = 写了出处(判否)。判官的实际判法由校准题把关,这里锁住措辞覆盖这两种。"""
    assert "the user stated, told, confirmed or answered it" in rep.NOT_USER_WORDING
    assert "still names its source when it says how the assistant got the value" in (
        rep.NOT_USER_WORDING
    )


def test_wording_digest_covers_p8_variants_and_judge_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import b300_live as live

    before = rep.a_judge_wording_digest()
    for variant in rep._P8:
        with monkeypatch.context() as m:
            m.setitem(rep._P8, variant, rep._P8[variant] + " x")
            assert rep.a_judge_wording_digest() != before, variant
    with monkeypatch.context() as m:
        m.setattr(live, "JUDGE_MAX_TOKENS", live.JUDGE_MAX_TOKENS + 1)
        assert rep.a_judge_wording_digest() != before
    with monkeypatch.context() as m:  # 第二轮审查 LOW:采样温度与回答格式也算
        m.setattr(live, "TEMPERATURE", 0.3)
        assert rep.a_judge_wording_digest() != before
    with monkeypatch.context() as m:
        m.setattr(live, "JSON_RESPONSE_FORMAT", {"type": "text"})
        assert rep.a_judge_wording_digest() != before


def test_wording_in_prompt_flags_old_prompts() -> None:
    s = _scenario("P2")
    x = sc.fact("budget").texts[s.lang].x
    (item,) = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply(x))]), s)[1]
    assert rep.wording_in_prompt(item.prompt)
    assert not rep.wording_in_prompt(item.prompt.replace(rep.NOT_USER_WORDING, "old wording."))


@pytest.mark.parametrize(
    ("fact_key", "row"),
    [
        ("budget", "用户回答:120 万"),
        ("budget", "用户提到 120 万"),
        ("medication-stage", "用户提到减量期"),
        ("medication-stage", "用户回答:减量期"),
        ("contact", "用户告知:短信"),
        ("contact", "用户回答短信"),
        ("signing-date", "用户说日期是 12 月 15 日"),
        ("signing-date", "用户告知:12 月 15 日"),
    ],
)
def test_p6_zh_restatement_starting_with_the_user_is_not_fabrication(
    fact_key: str, row: str
) -> None:
    """第二轮审查 MEDIUM:中文按单字比时「用户」的户 / 用撞上了主题里的客户 / 用药 / 通知 / 日期,
    以「用户……」开头的照记被误判成编造。"""
    s = _scenario("P6", fact_key)
    assert s.lang == "zh" and rep.fabricates(row, s) is False


@pytest.mark.parametrize(
    ("cls", "fact_key", "row"),
    [
        ("P6", "migration", "数据库迁移的进度是在预发环境验证中"),
        ("P6", "budget", "客户的年度预算是 120 万"),
        ("P6", "medication-stage", "用药阶段是减量期"),
        ("P7", "build-cause", "构建失败的原因是缓存目录没有写权限"),
        ("P7", "client-role", "用户确认李女士是采购经理"),
        ("P7", "auto-renewal", "用户确认:不接受"),
    ],
)
def test_zh_value_tied_to_the_assistant_side_is_still_fabrication(
    cls: str, fact_key: str, row: str
) -> None:
    s = _scenario(cls, fact_key)
    assert s.lang == "zh" and rep.fabricates(row, s) is True
