"""B-67 §七 —— 手抄守卫的纯函数:候选集、URL 抽取、编辑距离、判定、文案。"""

from __future__ import annotations

import json
import logging
import random
import string
import time
from collections.abc import Iterator

import pytest

from orchestrator.graph_builder import input_url_guard as guard
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


SIGNED = LOGO + "?Expires=1726394851&OSSAccessKeyId=LTAI5tAbCdEf"  # 尾串 ≥ 48 字符
SIGNED_CAND = (UrlCandidate(var_name="org_logo", url=SIGNED, link="org_logo.png"),)


def test_percent_encoding_change_within_threshold_is_a_hit() -> None:
    written = SIGNED.replace("cover-", "cover%2D")  # '-' → '%2D':距离 3
    hit = find_retyped_url(written, SIGNED_CAND)
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
    a = UrlCandidate(var_name="a", url="https://x/materials/pic-1.png", link="a.png")
    b = UrlCandidate(var_name="b", url="https://x/materials/pic-2.png", link="b.png")
    hit = find_retyped_url(
        "near('https://x/materials/pic-3.png'); open('https://x/materials/pic-2.png')", (a, b)
    )
    assert hit is not None and (hit.var_name, hit.distance) == ("b", 0)
    # 都不完全一致 → 最小距离归属(尾串 20 字符,允许 1 处)。
    hit = find_retyped_url("open('https://x/materials/pic-3.png')", (a, b))
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


NEAR = GuardHit(var_name="org_logo", link="org_logo.png", distance=1, written="https://x/bad.png")
EXACT = GuardHit(var_name="org_logo", link="org_logo.png", distance=0, written="https://x/ok.png")
#: 近似命中给模型留的出路(P25):确实是另一个地址时,让代码从它自己的来源取得。
OUT = "如果它确实是另一个地址"


def test_message_names_the_variable_the_link_and_the_distance() -> None:
    text = guard_message(NEAR, trusted=True)
    assert text.startswith("[blocked]")
    assert "org_logo" in text
    assert "$EXPERT_WORK_INPUTS_DIR/org_logo.png" in text
    assert "疑似抄错 1 处" in text
    assert "https://x/bad.png" in text  # 模型自己写的那串,不是输入值
    assert "$EXPERT_WORK_INPUTS" in text
    exact = guard_message(EXACT, trusted=True)
    assert "与输入一致" in exact


def test_exact_hit_message_keeps_the_wording() -> None:
    assert guard_message(EXACT, trusted=True) == (
        "[blocked] 代码里的地址 https://x/ok.png 是输入 org_logo 的手抄件（平台比对：与输入一致）。"  # noqa: RUF001
        "这个文件应在 $EXPERT_WORK_INPUTS_DIR/org_logo.png（不在则按清单里的原地址下载）；"  # noqa: RUF001
        "请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 org_logo 的原地址，不要手抄。"  # noqa: RUF001
    )
    assert guard_message(EXACT, trusted=False) == (
        "[blocked] 代码里有一处地址是输入 org_logo 的手抄件（平台比对：与输入一致）。"  # noqa: RUF001
        "这个文件应在 $EXPERT_WORK_INPUTS_DIR 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里"  # noqa: RUF001
        " org_logo 对应条目的 local_path（不在则按清单里的原地址下载）；"  # noqa: RUF001
        "请用代码从清单里读路径或原地址，不要手抄。"  # noqa: RUF001
    )
    assert OUT not in guard_message(EXACT, trusted=True)


def test_trusted_is_a_required_keyword() -> None:
    """M5:调用方必须说清楚这个变量信不信得过,没有默认值可以偷懒。"""
    with pytest.raises(TypeError):
        guard_message(EXACT)  # type: ignore[call-arg]


@pytest.mark.parametrize("trusted", [True, False])
def test_near_hit_message_leaves_an_out_for_a_different_address(trusted: bool) -> None:
    text = guard_message(NEAR, trusted=trusted)
    assert text.startswith("[blocked]")
    assert "像是输入 org_logo 的地址手抄出来的" in text
    assert "疑似抄错 1 处" in text
    assert OUT in text
    assert "从它自己的来源取得" in text
    assert ("https://x/bad.png" in text) is trusted
    assert ("$EXPERT_WORK_INPUTS_DIR/org_logo.png" in text) is trusted


# --- C1:代码里的正则 / sed 片段、畸形 URL 不能让守卫抛异常。


@pytest.mark.parametrize(
    "code",
    [
        'import re\nm = re.match(r"https://([^/]+)/(.*)", u)',
        "sed -E 's#https://[^/]+/##' urls.txt",
        "open('https://[oops/logo.png')",
        "u = 'https://例子／路径＠x/a.png'",  # noqa: RUF001 — 全角字符过 NFKC 校验会抛
        "u = 'http://[::1]:99999/x'",
    ],
)
def test_odd_literals_never_raise(code: str) -> None:
    cands = (
        *CAND,
        UrlCandidate(var_name="bad", url="https://[oops/logo.png", link="bad"),
    )
    find_retyped_url(code, cands)  # 不抛就行;命中与否不是这条测的


def test_split_keeps_the_tail_after_the_host_including_query_and_fragment() -> None:
    assert guard._split("HTTPS://Files.Example.com/a/b.png?x=1#f") == (
        "https://files.example.com",
        "/a/b.png?x=1#f",
    )
    assert guard._split("https://h?q=1") == ("https://h", "?q=1")
    assert guard._split("https://[oops/logo.png") == ("https://[oops", "/logo.png")


# --- P25:允许的编辑距离随候选尾串长度走,短尾串只拦完全一致。


@pytest.mark.parametrize(
    ("inputs", "code"),
    [
        ({"api_base": "https://api.partner.com/v1"}, 'get("https://api.partner.com/v2")'),
        ({"api_base": "https://api.partner.com/v1"}, 'BASE = "https://api.partner.com/v1/"'),
        ({"api_base": "https://api.partner.com/v1"}, 'get("https://api.partner.com/")'),
        ({"api_base": "https://api.partner.com/v1"}, 'get("https://api.partner.com/v1/items")'),
        (
            {
                "materials": json.dumps(
                    [
                        {"url": f"https://cdn.x.com/k/img_{i}.png", "description": f"图{i}"}
                        for i in range(3)
                    ]
                )
            },
            'download("https://cdn.x.com/k/img_7.png")',
        ),
    ],
)
def test_short_tails_only_block_exact_copies(inputs: dict[str, str], code: str) -> None:
    assert find_retyped_url(code, candidates_from_inputs(inputs)) is None


def test_short_tail_exact_copy_is_still_blocked() -> None:
    cands = candidates_from_inputs({"api_base": "https://api.partner.com/v1"})
    hit = find_retyped_url('get("https://api.partner.com/v1")', cands)
    assert hit is not None and hit.distance == 0
    # scheme / host 只差大小写也是原文照抄 —— 短尾串走不到近似分支,这条只能靠完全一致判出来。
    upper = find_retyped_url('get("HTTPS://API.partner.com/v1")', cands)
    assert upper is not None and upper.distance == 0


def test_a_twenty_to_thirty_one_char_tail_allows_exactly_one_edit() -> None:
    url = "https://h/abcdefghij-klmnopqrs.png"  # 尾串 24 字符 → 允许 1 处
    cands = (UrlCandidate(var_name="v", url=url, link="v.png"),)
    one = find_retyped_url(url.replace("klm", "kXm"), cands)
    assert one is not None and one.distance == 1
    assert find_retyped_url(url.replace("klm", "XYm"), cands) is None


def test_a_long_signed_url_tolerates_up_to_three_edits() -> None:
    three = SIGNED.replace("LTAI5t", "LTXYZt")
    hit = find_retyped_url(three, SIGNED_CAND)
    assert hit is not None and hit.distance == 3
    assert find_retyped_url(SIGNED.replace("LTAI5t", "WXYZ5t"), SIGNED_CAND) is None


# --- M4 / M6:完全一致按子串找;URL 字面量后面跟着 ASCII 标点也算完全一致。


@pytest.mark.parametrize(
    "url",
    [
        "https://x/方案（终版）.pptx",  # noqa: RUF001
        "https://x/培训，第一版.pdf",  # noqa: RUF001
        "https://x/a(1).pdf",
    ],
)
def test_exact_copy_with_characters_that_stop_the_literal_is_caught(url: str) -> None:
    cands = (UrlCandidate(var_name="doc", url=url, link="doc.pdf"),)
    hit = find_retyped_url(f"download('{url}')\nprint({url!r})", cands)
    assert hit == GuardHit(var_name="doc", link="doc.pdf", distance=0, written=url)


@pytest.mark.parametrize(
    "code", [f"curl {LOGO};", f"urls = [{LOGO}, x]", f"见 {LOGO}.", f"({LOGO})"]
)
def test_exact_copy_followed_by_ascii_punctuation_is_exact(code: str) -> None:
    hit = find_retyped_url(code, CAND)
    assert hit == GuardHit(var_name="org_logo", link="org_logo.png", distance=0, written=LOGO)


def test_near_slip_followed_by_ascii_punctuation_keeps_its_distance() -> None:
    """尾随的 ``;`` 不算进编辑距离:LOGO 尾串 30 字符只允许 1 处,多算一位就漏了。"""
    written = LOGO.replace("1726394851207", "17263948512077")
    hit = find_retyped_url(f"curl {written};", CAND)
    assert hit is not None and (hit.distance, hit.written) == (1, written)


def test_input_that_is_a_prefix_of_a_longer_literal_is_not_an_exact_copy() -> None:
    cands = (UrlCandidate(var_name="v", url="https://x/a", link="v"),)
    for code in ("open('https://x/a.png')", "open('https://x/ab')", "open('https://x/a/b')"):
        assert find_retyped_url(code, cands) is None, code


# --- I3:便宜的下界先挡,找到第一个命中就停,每次调用有比较预算。

_OSS = "https://tenant-bucket.oss-cn-hangzhou.aliyuncs.com"


def _oss_url(rng: random.Random, i: int) -> str:
    sig = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(28))
    return (
        f"{_OSS}/tenant-x/materials/2026/09/17/batch-abcdef0123456789/image-{i:03d}.png"
        f"?Expires=1726394851&OSSAccessKeyId=LTAI5tAbCdEfGhIjKlMnOpQr&Signature={sig}%3D"
    )


@pytest.fixture
def dp_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """把 ``levenshtein`` 换成计数包装:断言的是跑了几次 DP,不只是墙钟。"""
    calls: list[int] = []
    real = guard.levenshtein

    def counting(a: str, b: str, *, cap: int) -> int:
        calls.append(len(a))
        return real(a, b, cap=cap)

    monkeypatch.setattr(guard, "levenshtein", counting)
    yield calls


def test_same_host_non_input_literals_are_ruled_out_before_the_dp(dp_calls: list[int]) -> None:
    """64 个候选 x 200 个同 host 非输入地址(各自的签名串):多重集下界先挡掉。"""
    rng = random.Random(7)  # noqa: S311 — 固定种子造测试数据
    urls = [_oss_url(rng, i) for i in range(64)]
    cands = candidates_from_inputs(
        {"materials": json.dumps([{"url": u, "description": f"d{i}"} for i, u in enumerate(urls)])}
    )
    assert len(cands) == 64
    code = "\n".join(f"u{i} = '{_oss_url(rng, 200 + i)}'" for i in range(200))
    started = time.perf_counter()
    assert find_retyped_url(code, cands) is None
    elapsed = time.perf_counter() - started
    assert len(dp_calls) <= 10
    assert elapsed < 2.0, f"took {elapsed:.3f}s"


def test_shared_prefix_pairs_stop_at_the_first_hit(dp_calls: list[int]) -> None:
    """20 x 20、共享 ~1000 字符前缀:第一处命中就停,不把 400 对都跑一遍。"""
    base = f"{_OSS}/tenant-x/pic.png?x-oss-process=" + "image/resize,w_100/" * 50
    cands = tuple(UrlCandidate("v", base + f"quality,q_{i:02d}", "v") for i in range(20))
    code = "\n".join(f"'{base}quality,q_{i:02d}x'" for i in range(20))
    started = time.perf_counter()
    hit = find_retyped_url(code, cands)
    elapsed = time.perf_counter() - started
    assert hit is not None and hit.distance == 1
    assert len(dp_calls) <= 3
    assert elapsed < 2.0, f"took {elapsed:.3f}s"


def test_comparison_budget_fails_open_and_logs_counts_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(guard, "MAX_DP_CELLS", 0)
    written = SIGNED.replace("LTAI5t", "LTAI6t")
    with caplog.at_level(logging.WARNING, logger=guard.__name__):
        assert find_retyped_url(f"open('{written}')", SIGNED_CAND) is None
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("tools.input_url_guard_budget_exhausted") for line in lines)
    assert not any("://" in line or "LTAI" in line for line in lines)
    # 完全一致不走 DP,预算用完也照拦。
    assert find_retyped_url(f"open('{SIGNED}')", SIGNED_CAND) is not None


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
    长度就要 10+ 秒(见 report 里的旧实现实测),带宽裁剪要能整体在 2 秒内跑完 ——
    实测约 0.23 秒,界定在 2 秒是为了在 CI ``-n auto``(多个 worker 抢 CPU)下留够
    余量,不是算法本身需要这么久(fix round 2:1 秒的界在这种环境下偶发抖动)。"""
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
    assert elapsed < 2.0, f"took {elapsed:.3f}s"


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


def test_host_match_is_case_insensitive() -> None:
    """review fix round 2 #1:候选的 host 与代码里写法的 host 只是大小写不同,也要判定
    同 host —— 与 #4(scheme 大小写不敏感)是两件事:那条测的是 URL_RE 抽不抽得出来
    大写 scheme,这条测的是 ``_split`` 里 host 比较本身。"""
    cand = (
        UrlCandidate(
            var_name="org_logo",
            url=LOGO.replace("files.example.com", "Files.Example.COM"),
            link="org_logo.png",
        ),
    )
    hit = find_retyped_url(f"open('{LOGO}')", cand)
    assert hit == GuardHit(var_name="org_logo", link="org_logo.png", distance=0, written=LOGO)
