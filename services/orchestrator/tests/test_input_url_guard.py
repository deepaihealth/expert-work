"""B-67 §七 —— 手抄守卫的纯函数:候选集、URL 抽取、编辑距离、判定、文案。"""

from __future__ import annotations

import time

from orchestrator.graph_builder.input_url_guard import (
    MAX_COMPARE_CHARS,
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


def test_exact_branch_catches_urls_longer_than_the_near_match_bound() -> None:
    """review fix round 1 #1:近似分支的长度闸挡住的串,完全一致分支不受它限制。"""
    long_tail = "a" * (MAX_COMPARE_CHARS + 10)
    long_url = f"https://z/{long_tail}"
    cand = (UrlCandidate(var_name="big", url=long_url, link="big.bin"),)
    hit = find_retyped_url(f"open('{long_url}')", cand)
    assert hit == GuardHit(var_name="big", link="big.bin", distance=0, written=long_url)


LONG_TAIL = "b" * 2100
LONG_URL = "https://y/" + LONG_TAIL
LONG_CAND = (UrlCandidate(var_name="v", url=LONG_URL, link="v.bin"),)


def test_one_character_slip_on_a_long_tail_is_still_a_hit() -> None:
    """P19:签名 URL 常见的长度(> 旧上限 2048),一处改动仍要抓到。"""
    written = "https://y/" + LONG_TAIL[:-1] + "c"
    hit = find_retyped_url(f"open('{written}')", LONG_CAND)
    assert hit is not None and hit.distance == 1


def test_four_edits_on_a_long_tail_is_not_a_hit() -> None:
    mutated = "c" * 4 + LONG_TAIL[4:]
    written = "https://y/" + mutated
    assert find_retyped_url(written, LONG_CAND) is None


def _salt(i: int) -> str:
    """每个候选独有的 4 字符前缀,字母互不相同 —— 保证跨候选比较在几行内就能算出
    编辑距离 > cap 提前退出;真正对得上的那一对(同下标)只在尾部差 1~2 个字符,
    要跑满整段带宽,是这条尾串量级下真正的最坏情形。"""
    return chr(ord("A") + i) * 4


def test_near_match_perf_on_long_tails_is_fast() -> None:
    """P19:20 写法 x 20 候选、~8000 字符的尾串。每个写法只跟同下标的候选「差一点」
    (要跑满整段带宽),跟其余 19 个候选在前几个字符就能判定 > cap(现实里 20 个兄弟
    输入各有自己的签名串,不会跟别的候选撞出一段相同前缀)。全矩阵版本单次比较这个
    长度就要 10+ 秒(见 report 里的旧实现实测),带宽裁剪必须整体在 1 秒内跑完。"""
    base = "d" * 7990
    candidates = tuple(
        UrlCandidate(var_name=f"v{i}", url=f"https://p/{_salt(i)}{base}{i:04d}", link=f"v{i}.bin")
        for i in range(20)
    )
    code = "\n".join(
        f"open('https://p/{_salt(i)}{base}{i:03d}x')"  # 尾部一处改动:'0005' → '005x'
        for i in range(20)
    )
    started = time.perf_counter()
    find_retyped_url(code, candidates)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"took {elapsed:.3f}s"


def test_url_literal_extraction_stops_at_chinese_punctuation() -> None:
    """review fix round 1 #3:裸 URL 后面常跟中文句读,不是 URL 的一部分。"""
    code = "见 https://h/a.png，然后再看 https://h/b.png。完"  # noqa: RUF001 — 测的就是全角标点
    assert URL_RE.findall(code) == ["https://h/a.png", "https://h/b.png"]


def test_url_literal_extraction_keeps_cjk_in_the_path() -> None:
    """中文文件名是真实输入,不能被当成「跟在 URL 后面的文字」剔除。"""
    assert URL_RE.findall("open('https://h/图片.png')") == ["https://h/图片.png"]


def test_scheme_match_is_case_insensitive() -> None:
    """review fix round 1 #4:``HTTPS://`` 一样要抽出来,host 比较本就不分大小写。"""
    written = LOGO.replace("https://", "HTTPS://")
    assert URL_RE.findall(f"open('{written}')") == [written]
    hit = find_retyped_url(f"open('{written}')", CAND)
    assert hit == GuardHit(var_name="org_logo", link="org_logo.png", distance=0, written=written)
