"""B-300 ④ —— 记忆用用户的语言写:程序判语言 + 两档指令文字(设计 §3)。"""

from __future__ import annotations

import pytest

from expert_work.common.memory_language import (
    LATIN_SCRIPT,
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


# 评测 A 组(11-14):英文场景里约一成半的记忆被写成中文,另有一些写成葡语 / 西语一类 —— 只说
# 「拉丁字母的语言」不够,能认准英语就点名,认不准的拉丁字母语言要求「照用户的话的那种语言写」。
_ES = (
    "Por favor, recuerda que el número de mi pedido de "
    "devolución es RX-2291 y que la entrega fue en la oficina"
)
_PT = (
    "Por favor, lembre que o número do meu pedido de "
    "devolução é RX-2291 e que a entrega foi no escritório"
)
_FR = (
    "Merci de noter que le numéro de ma commande de "
    "retour est RX-2291 et que la livraison était au bureau"
)
_DE = (
    "Bitte merke dir, dass die Nummer meiner Rücksendung RX-2291 ist und die Lieferung im Büro war"
)
_IT = "Per favore ricorda che il numero del mio reso è RX-2291 e che la consegna era in ufficio"
_NL = (
    "Onthoud alsjeblieft dat het nummer van mijn retour "
    "RX-2291 is en dat de levering op kantoor was"
)


def test_english_is_named() -> None:
    assert detect_user_language([_EN]) == "English"
    assert detect_user_language(["my fasting glucose this morning was high", "it is 7.2"]) == (
        "English"
    )


@pytest.mark.parametrize(
    "text", [_ES, _PT, _FR, _DE, _IT, _NL], ids=["es", "pt", "fr", "de", "it", "nl"]
)
def test_other_latin_languages_are_latin_script_not_english(text: str) -> None:
    assert detect_user_language([text]) == LATIN_SCRIPT


def test_code_heavy_english_is_judged_by_its_prose() -> None:
    code = "```python\n# 处理用户的请求并返回结果\nfor item in items: handle(item)\n```\n"
    assert detect_user_language(
        [code + "The build fails when I run the tests, can you fix it?"]
    ) == ("English")
    # 代码去掉后剩下的文字不够 20 个字母:不点名。
    assert detect_user_language([code + "fix it"]) is None


def test_english_with_a_few_cjk_characters_is_not_named() -> None:
    """夹了几个汉字(人名、地名)的英文:几种文字混着,照旧不点名(不冒险说成 English)。"""
    assert (
        detect_user_language(["My colleague 王伟 will visit the Shanghai office next week"]) is None
    )


def test_mixed_chinese_with_english_words_stays_chinese() -> None:
    assert detect_user_language([_ZH + " please use the new template"]) == "Chinese"


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


def test_english_section_names_english() -> None:
    assert 'Write the "content" of every memory in English, in the same script' in (
        extraction_language_section("English")
    )


def test_latin_script_section_points_at_the_users_own_messages() -> None:
    text = extraction_language_section(LATIN_SCRIPT)
    assert text.startswith("## Output language\n")
    assert (
        'Write the "content" of every memory in exactly the language of the user\'s own '
        "messages in the input — a language written in Latin letters, not Chinese, Japanese "
        "or Korean." in " ".join(text.split())
    )
    assert "Do not translate." in text
    summary = " ".join(summary_language_section(LATIN_SCRIPT).split())
    assert (
        "Write the summary in exactly the language these memories are written in — a language "
        "written in Latin letters, not Chinese, Japanese or Korean." in summary
    )


def test_summary_sections() -> None:
    assert "Write the summary in Chinese" in summary_language_section("Chinese")
    assert (
        "Write the summary in the language most of these memories are written in."
        in summary_language_section(None)
    )


def test_a_slash_inside_cjk_text_is_not_a_path() -> None:
    """B-300 review M2 —— 中日文不用空格分词:斜杠(血压 130/85)不能把整句当路径吃掉。"""
    assert (
        detect_user_language(["我的血压一般是130/85,医生说要每天早晚各量一次并记录下来"])
        == "Chinese"
    )
    # 假名不到三成(汉字 + 少量假名):按规则不点名 —— 但绝不能因为斜杠吃掉了前一句而被点名成 Chinese。
    assert (
        detect_user_language(
            [
                "毎日の記録/東京都港区で働いています。",
                "東京都港区勤務、営業部長、週末休日、自宅埼玉県川口市在住",
            ]
        )
        is None
    )


def test_url_right_after_cjk_is_still_stripped() -> None:
    """网址紧跟在汉字后面(前面没有空格)也要去掉,不让网址里的拉丁字母把占比推过八成。"""
    url = "https://example.com/" + "abcdefghij" * 20
    assert (
        detect_user_language(["请帮我看看这个链接" + url + "里面写的东西我觉得很有意思"])
        == "Chinese"
    )
