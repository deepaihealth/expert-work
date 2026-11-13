"""B-300 ④ —— 记忆用用户的语言写:程序判语言 + 两档指令文字(设计 §3)。"""

from __future__ import annotations

import pytest

from expert_work.common.memory_language import (
    detect_user_language,
    extraction_language_section,
    summary_language_section,
)

#: 每段都超过 20 个有信号的字符(不足 20 的单独测)。
_ZH = "我下周三要去上海出差请帮我把会议改到周四下午三点以后再开"
_JA = "来週の水曜日に東京へ出張するので、会議を木曜日の午後に変更してください"
_KO = "다음 주 수요일에 서울로 출장을 가니까 회의를 목요일 오후로 바꿔 주세요"
_EN = "Please move the meeting to Thursday afternoon because I travel on Wednesday"


def test_fewer_than_twenty_signal_characters_is_not_named() -> None:
    assert detect_user_language(["上海出差"]) is None
    # 门槛就是 20:19 个不点名,20 个点名。
    assert detect_user_language(["字" * 19]) is None
    assert detect_user_language(["字" * 20]) == "Chinese"
    # 数字、标点、空白不是信号字符:凑不够 20 个。
    assert detect_user_language(["改到 2026-11-20 15:30 !!! 好的"]) is None


def test_pure_han_is_chinese() -> None:
    assert detect_user_language([_ZH]) == "Chinese"


def test_han_with_some_latin_is_still_chinese() -> None:
    assert detect_user_language([_ZH + " 用 Python 写一个 report"]) == "Chinese"


def test_latin_above_eighty_percent_is_not_named() -> None:
    mostly_latin = "这个 " + "configuration deployment pipeline repository " * 3
    assert detect_user_language([mostly_latin]) is None


def test_pure_latin_is_not_named() -> None:
    assert detect_user_language([_EN]) is None


def test_kana_text_is_japanese() -> None:
    assert detect_user_language([_JA]) == "Japanese"
    assert detect_user_language(["ありがとうございます、またよろしくおねがいします"]) == "Japanese"


def test_han_with_a_little_kana_is_not_named() -> None:
    # 汉字里只夹少量假名(不到三成):几种文字混着,不点名。
    assert detect_user_language([_ZH + "の"]) is None


def test_hangul_is_korean() -> None:
    assert detect_user_language([_KO]) == "Korean"


def test_mixed_scripts_are_not_named() -> None:
    assert detect_user_language([_ZH, _KO]) is None
    assert detect_user_language(["Привет, перенеси встречу на четверг после обеда"]) is None


def test_code_urls_and_paths_do_not_count() -> None:
    code_heavy = (
        "看一下 `def handler(request): return response_object_value` 和 "
        "https://example.com/some/really/long/english/path 还有 /var/log/application/output.log\n"
        "```python\nfor element in collection_of_things: process(element)\n```"
    )
    # 去掉代码与网址、路径后只剩几个汉字:不足 20 个,不点名。
    assert detect_user_language([code_heavy]) is None
    # 汉字够数时,代码里的大量拉丁字母不会把它推过八成。
    assert detect_user_language([_ZH + " `" + "x" * 200 + "`"]) == "Chinese"


def test_counts_over_all_texts() -> None:
    assert detect_user_language(["上海出差", "会议改到周四下午三点以后再开吧谢谢"]) == "Chinese"


@pytest.mark.parametrize("language", ["Chinese", "Japanese", "Korean"])
def test_named_section_names_the_language(language: str) -> None:
    text = extraction_language_section(language)
    assert text.startswith("## Output language\n")
    assert f'Write the "content" of every memory in {language}, in the same script' in text
    assert "Do not translate." in text


def test_unnamed_section_follows_the_user() -> None:
    text = extraction_language_section(None)
    assert text.startswith("## Output language\n")
    assert "in the language the user writes in" in text
    assert "If no user text\nis shown, use the language of the conversation." in text


def test_summary_sections() -> None:
    assert "Write the summary in Chinese" in summary_language_section("Chinese")
    assert (
        "Write the summary in the language most of these memories are written in."
        in summary_language_section(None)
    )
