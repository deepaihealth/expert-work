"""B-300 ④ —— 长期记忆用用户的语言写:判语言 + 指令里的「Output language」一节。

规则只有一条:记忆用用户的语言写。程序认得出来时把语言名写进指令,让这条规则更硬;认不出来时
指令仍是同一条规则,只是不点名(设计 §3);全是拉丁字母、又不是英语时,要求照用户自己的话的那种
语言写(11-14)。

**怎么判**:只看用户输入的文字;去掉代码块、行内代码、网址、路径后,数「有信号的字符」(各种
文字的字母,不含数字、标点、空白)。只有一种文字只对应一种语言时才判得准,所以只点名中日韩三种,外加英语(靠虚词判):

* 不足 20 个 → 不点名(只发了几个汉字的日语用户不被点名成 Chinese);
* 全是拉丁字母(评测 A 组,11-14:英文场景里有记忆被写成中文、葡语 / 西语一类)→ 英语独有的
  标记词(the、and、you、this、with、please、单独大写的 I……,不收与别的拉丁字母语言同形的)
  至少两个不同的(the、you、this、please、I 这类强标记一个就够)、占词数一成以上 → English;
  否则 → :data:`LATIN_SCRIPT`(指令要求照用户自己的话的那种语言写,不点名、不翻译);
* 夹着中日韩等别的文字、拉丁字母超过八成 → 不点名;
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

#: 全是拉丁字母、但认不准是哪种语言(不是英语)时的返回值。
LATIN_SCRIPT = "latin-script"
#: 英语标记词至少占拉丁词数的这么多、且至少有这么多个不同的才判 English;词数太少不判。
#: 复审(11-14)按评测 A 组的英文写回与一组荷、丹、挪、瑞、波、德、西、葡、法语句子量过再定。
_MIN_ENGLISH_SHARE = 0.10
_MIN_ENGLISH_DISTINCT = 2
_MIN_LATIN_WORDS = 4
#: 只收英语独有的词。与别的拉丁字母语言同形的不收:a、an、in、no、do、as、was、will、me、he、
#: i、so(西、葡、法、德、意、荷),for、at、to、of、or、but、my、we、by、if、can、then、here、
#: some、any、her、just(丹、挪、瑞、波、荷、法),is、had、been(荷兰语;复审 11-14)。另收
#: 几个常用的英语实词(please、today、tomorrow、month、new……),短句里常常只有它们。
_ENGLISH_WORDS = frozenset(
    "the and are were have has does did you your yours our they their them she him his it its "
    "this that these those with from about not what when where which who how than there could "
    "would should it's i'm i've i'll i'd don't doesn't didn't can't won't isn't that's let's "
    "please thanks thank today tomorrow yesterday tonight morning evening month new".split()
)
#: 句中单独大写的 I(英语的「我」)也算:意大利语的冠词 i 是小写。
_ENGLISH_PRONOUN = "I"
#: 强标记:在上面那几种语言里都不是词,出现一个就够(仍要占词数一成以上);其余标记词要两个不同的。
_STRONG_ENGLISH = frozenset(
    "the you your this that these those please it's i'm i've don't i".split()
)
#: 一个词:任意文字的字母连在一起(带重音的字母不把词切开),中间可夹一个撇号。
_LATIN_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")

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


def _is_english(prose: str) -> bool:
    # 弯撇号(U+2019)按直撇号算;大小写只用来认单独的 I。
    words = _LATIN_WORD.findall(prose.replace("\u2019", "'"))
    if len(words) < _MIN_LATIN_WORDS:
        return False
    hits = [
        word.casefold()
        for word in words
        if word == _ENGLISH_PRONOUN or word.casefold() in _ENGLISH_WORDS
    ]
    distinct = set(hits)
    enough_kinds = len(distinct) >= _MIN_ENGLISH_DISTINCT or bool(distinct & _STRONG_ENGLISH)
    return enough_kinds and len(hits) >= _MIN_ENGLISH_SHARE * len(words)


def detect_user_language(texts: Iterable[str]) -> str | None:
    """用户文字的语言名(``English`` / ``Chinese`` / ``Japanese`` / ``Korean``);全是拉丁字母但
    不是英语时 :data:`LATIN_SCRIPT`;认不准时 ``None``。

    ``texts`` 只能是用户输入的文字:附件、外部内容、助手的话都不该传进来。"""
    counts: dict[str, int] = {}
    prose: list[str] = []
    for text in texts:
        stripped = _strip_non_prose(text)
        prose.append(stripped)
        for ch in stripped:
            script = _script(ch)
            if script is not None:
                counts[script] = counts.get(script, 0) + 1
    total = sum(counts.values())
    if total < _MIN_SIGNAL_CHARS:
        return None
    if set(counts) == {"latin"}:
        return "English" if _is_english("\n".join(prose)) else LATIN_SCRIPT
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

_LATIN = (
    "## Output language\n"
    'Write the "content" of every memory in exactly the language of the user\'s own messages\n'
    "in the input — a language written in Latin letters, not Chinese, Japanese or Korean.\n"
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)

_SUMMARY_NAMED = (
    "## Output language\n"
    "Write the summary in {language}, in the same script the user writes in.\n"
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)

_SUMMARY_LATIN = (
    "## Output language\n"
    "Write the summary in exactly the language most of these memories are written in — a\n"
    "language written in Latin letters, not Chinese, Japanese or Korean.\n"
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)

_SUMMARY_UNNAMED = (
    "## Output language\n"
    "Write the summary in the language most of these memories are written in.\n"
    "Keep names, codes, units and quoted terms exactly as they appear. Do not translate."
)


def extraction_language_section(language: str | None) -> str:
    """两段提炼指令末尾的「Output language」一节(设计 §3 的三档原文)。"""
    if language == LATIN_SCRIPT:
        return _LATIN
    return _NAMED.format(language=language) if language else _UNNAMED


def summary_language_section(language: str | None) -> str:
    """整理汇总指令末尾的同一节;不点名时按这批记忆多数的语言写。"""
    if language == LATIN_SCRIPT:
        return _SUMMARY_LATIN
    return _SUMMARY_NAMED.format(language=language) if language else _SUMMARY_UNNAMED


__all__ = [
    "LATIN_SCRIPT",
    "detect_user_language",
    "extraction_language_section",
    "summary_language_section",
]
