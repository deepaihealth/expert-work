"""B-67 §七 —— 手抄守卫的纯函数:候选集、URL 抽取、编辑距离、判定、文案。"""

from __future__ import annotations

from orchestrator.graph_builder.input_url_guard import (
    MAX_EDIT_DISTANCE,
    URL_RE,
    GuardHit,
    UrlCandidate,
    candidates_from_inputs,
    find_retyped_url,
    guard_message,
    levenshtein,
)

LOGO = "https://files.example.com/brand/cover-1726394851207.png"
CAND = (UrlCandidate(var_name="org_logo", url=LOGO, link="org_logo.png"),)


def test_exact_copy_is_a_hit_with_distance_zero() -> None:
    """抄对了也拦:这次对不代表下次对;拦一次模型这一轮就改道。"""
    hit = find_retyped_url(f"urllib.request.urlretrieve('{LOGO}', 'l.png')", CAND)
    assert hit == GuardHit(var_name="org_logo", link="org_logo.png", distance=0, written=LOGO)


def test_one_character_slip_is_a_hit() -> None:
    """事故形态:``1726394851207`` 多写一位(编辑距离 1)。"""
    written = LOGO.replace("1726394851207", "17263948512077")
    hit = find_retyped_url(f'requests.get("{written}")', CAND)
    assert hit is not None
    assert (hit.var_name, hit.distance, hit.written) == ("org_logo", 1, written)


def test_percent_encoding_change_within_threshold_is_a_hit() -> None:
    written = LOGO.replace("cover-", "cover%2D")  # '-' → '%2D':距离 3
    hit = find_retyped_url(written, CAND)
    assert hit is not None and hit.distance == 3


def test_four_edits_away_is_not_a_hit() -> None:
    written = LOGO.replace("1726", "9999")
    assert levenshtein(written, LOGO, cap=MAX_EDIT_DISTANCE) == MAX_EDIT_DISTANCE + 1
    assert find_retyped_url(written, CAND) is None


def test_other_host_is_never_compared() -> None:
    assert find_retyped_url(LOGO.replace("files.example.com", "cdn.example.com"), CAND) is None


def test_query_string_is_part_of_the_compared_tail() -> None:
    """裁定 4:OSS 签名 URL 的长串常在 query 里。"""
    signed = LOGO + "?Expires=1726394851&Signature=abcDEF"
    cand = (UrlCandidate(var_name="org_logo", url=signed, link="org_logo.png"),)
    slipped = signed.replace("Signature=abcDEF", "Signature=abcDEG")
    hit = find_retyped_url(slipped, cand)
    assert hit is not None and hit.distance == 1


def test_code_that_reads_the_manifest_has_no_literal_and_passes() -> None:
    code = (
        "import json, os\n"
        "d = json.load(open(os.environ['EXPERT_WORK_INPUTS']))\n"
        "url = d['variables']['org_logo']['value']\n"
        "p = os.environ['EXPERT_WORK_INPUTS_DIR'] + '/org_logo.png'\n"
    )
    assert find_retyped_url(code, CAND) is None


def test_exact_wins_over_near_when_two_inputs_are_siblings() -> None:
    a = UrlCandidate(var_name="a", url="https://x/pic-1.png", link="a.png")
    b = UrlCandidate(var_name="b", url="https://x/pic-2.png", link="b.png")
    hit = find_retyped_url("open('https://x/pic-2.png')", (a, b))
    assert hit is not None and (hit.var_name, hit.distance) == ("b", 0)
    # 都不完全一致 → 最小距离归属。
    hit = find_retyped_url("open('https://x/pic-3.png')", (a, b))
    assert hit is not None and hit.distance == 1 and hit.var_name == "a"


def test_url_literal_extraction_stops_at_quotes_and_brackets() -> None:
    code = "x = ['https://h/a.png', \"https://h/b.png\"]; y = (https://h/c.png)\nz=`https://h/d`"
    assert URL_RE.findall(code) == [
        "https://h/a.png",
        "https://h/b.png",
        "https://h/c.png",
        "https://h/d",
    ]


def test_levenshtein_is_capped() -> None:
    assert levenshtein("abc", "abd", cap=3) == 1
    assert levenshtein("", "abc", cap=3) == 3
    assert levenshtein("a" * 100, "b" * 100, cap=3) == 4
    assert levenshtein("a" * 100, "a" * 90, cap=3) == 4  # 长度差先短路


def test_candidates_come_from_the_same_walker_as_the_manifest() -> None:
    inputs = {
        "materials": '[{"url": "https://x/a.mp4", "description": "示范"}]',
        "org_logo": "https://x/l.png",
        "note": "hi",
        "images": ["https://x/bare.png"],  # 裸列表项不是 site,也不是候选
    }
    out = candidates_from_inputs(inputs)
    assert [(c.var_name, c.url, c.link) for c in out] == [
        ("materials", "https://x/a.mp4", "materials/0-示范.mp4"),
        ("org_logo", "https://x/l.png", "org_logo.png"),
    ]
    assert candidates_from_inputs({}) == ()


def test_message_names_the_variable_the_link_and_the_distance() -> None:
    text = guard_message(
        GuardHit(var_name="org_logo", link="org_logo.png", distance=1, written="https://x/bad.png")
    )
    assert text.startswith("[blocked]")
    assert "org_logo" in text
    assert "$EXPERT_WORK_INPUTS_DIR/org_logo.png" in text
    assert "疑似抄错 1 处" in text
    assert "https://x/bad.png" in text  # 模型自己写的那串,不是输入值
    assert "$EXPERT_WORK_INPUTS" in text
    exact = guard_message(GuardHit(var_name="v", link="v.png", distance=0, written="u"))
    assert "与输入一致" in exact
