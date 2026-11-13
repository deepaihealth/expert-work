"""B-300 ④ —— 长期记忆用用户的语言写:判语言 + 指令里的「Output language」一节。

规则只有一条:记忆用用户的语言写。程序认得出来时把语言名写进指令,让这条规则更硬;认不出来时
指令仍是同一条规则,只是不点名(设计 §3)。

**怎么判**:只看用户输入的文字;去掉代码块、行内代码、网址、路径后,数「有信号的字符」(各种
文字的字母,不含数字、标点、空白)。只有一种文字只对应一种语言时才判得准,所以只点名三种:

* 不足 20 个 → 不点名(只发了几个汉字的日语用户不被点名成 Chinese);
* 拉丁字母超过八成 → 不点名;
* 其余信号字符全是汉字 → Chinese;
* 全是假名,或假名 + 汉字且假名占三成以上 → Japanese(汉字里只夹少量假名算「混着」);
* 全是谚文 → Korean;
* 别的(西里尔、阿拉伯等一种文字对应多种语言的,或几种文字混着的)→ 不点名。

不引入语言识别库,不调模型,不加配置项。住在 common:编排侧(提炼)与控制面(整理汇总)
用同一个函数。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: 少于这么多个有信号的字符就不点名。
_MIN_SIGNAL_CHARS = 20
#: 拉丁字母占有信号字符的比例超过它就不点名(中日韩用户夹英文单词很常见)。
_MAX_LATIN_SHARE = 0.8
#: 假名 + 汉字时,假名至少占这么多才算日语。
_MIN_KANA_SHARE = 0.3

_FENCED_CODE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
# B-300 review M2 —— 网址与路径只认 ASCII:中日文不用空格分词,``\S*/\S*`` 会把「血压 130/85」
# 所在的整句当路径吃掉。网址前面不要求词边界(紧跟在汉字后面也要去掉);路径只在行首、空白或
# 括号之后开始,所以夹在汉字中间的斜杠不算。
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[\x21-\x7e]+")
_PATH = re.compile(r"(?<![^\s(\[])(?:[A-Za-z0-9_.~-]*[/\\])+[A-Za-z0-9_.~-]+")


def _script(ch: str) -> str | None:
    """一个有信号的字符属于哪种文字;不是字母(数字、标点、空白、符号)返回 ``None``。"""
    if not ch.isalpha():
        return None
    code = ord(ch)
    if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9D:
        return "kana"
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return "hangul"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
        return "han"
    if 0x20000 <= code <= 0x2FA1F:
        return "han"
    if code < 0x0250 or 0x1E00 <= code <= 0x1EFF or 0xFF21 <= code <= 0xFF5A:
        return "latin"
    return "other"


def _strip_non_prose(text: str) -> str:
    text = _FENCED_CODE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _URL.sub(" ", text)
    return _PATH.sub(" ", text)


def detect_user_language(texts: Iterable[str]) -> str | None:
    """用户文字的语言名(``Chinese`` / ``Japanese`` / ``Korean``),认不准时 ``None``。

    ``texts`` 只能是用户输入的文字:附件、外部内容、助手的话都不该传进来。"""
    counts: dict[str, int] = {}
    for text in texts:
        for ch in _strip_non_prose(text):
            script = _script(ch)
            if script is not None:
                counts[script] = counts.get(script, 0) + 1
    total = sum(counts.values())
    if total < _MIN_SIGNAL_CHARS:
        return None
    if counts.get("latin", 0) > _MAX_LATIN_SHARE * total:
        return None
    others = {name for name, n in counts.items() if n and name != "latin"}
    if others == {"han"}:
        return "Chinese"
    if others == {"hangul"}:
        return "Korean"
    if others == {"kana"}:
        return "Japanese"
    if others == {"kana", "han"} and counts["kana"] >= _MIN_KANA_SHARE * (
        counts["kana"] + counts["han"]
    ):
        return "Japanese"
    return None


_NAMED = (
    "## Output language\n"
    'Write the "content" of every memory in {language}, in the same script the user writes in.\n'
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)

_UNNAMED = (
    "## Output language\n"
    'Write the "content" of every memory in the language the user writes in. If no user text\n'
    "is shown, use the language of the conversation. Keep names, codes, units and quoted terms\n"
    "exactly as they appear. Do not translate."
)

_SUMMARY_NAMED = (
    "## Output language\n"
    "Write the summary in {language}, in the same script the user writes in.\n"
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)

_SUMMARY_UNNAMED = (
    "## Output language\n"
    "Write the summary in the language most of these memories are written in.\n"
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)


def extraction_language_section(language: str | None) -> str:
    """两段提炼指令末尾的「Output language」一节(设计 §3 的两档原文)。"""
    return _NAMED.format(language=language) if language else _UNNAMED


def summary_language_section(language: str | None) -> str:
    """整理汇总指令末尾的同一节;不点名时按这批记忆多数的语言写。"""
    return _SUMMARY_NAMED.format(language=language) if language else _SUMMARY_UNNAMED


__all__ = [
    "detect_user_language",
    "extraction_language_section",
    "summary_language_section",
]
