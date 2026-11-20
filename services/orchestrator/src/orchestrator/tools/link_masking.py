"""B-330 —— 结果遮回:文字里出现的登记链接换回编号(纯函数,不碰计数)。

不拼大正则(审查 M2:200 条链接的大选择式编译要 3 秒、峰值上百 MB,还常驻在 ``re`` 的全局
缓存里)。做法是三步,全是 ``str`` 操作:

1. **按主机名预筛**:主机名在原样、JSON 转义、``\\/``、``quote(safe="")`` 几种写法里都原样
   保留,文字里没有这台主机的链接直接跳过;
2. **只给命中的链接生成写法**(按链接缓存,有上限),``str.find`` 找出现位置;
3. **查结尾边界**(审查 M1):出现位置后面紧跟的还是链接的一部分(字母数字、``-_~/?#%=&+``,
   ``.`` 后接字母数字,``\\/`` / ``\\u0026``)就不算 —— ``…/a.pdf`` 不会把 ``…/a.pdf.bak`` 的
   开头遮掉;句末的 ``.`` ``)`` ``,`` 不挡。重叠的命中取长的。

百分号编码的十六进制位大小写不敏感:文字和写法都先归一成小写十六进制(长度不变,位置对得上)。

**已知认不出的写法**(spec §4.6 与对外文档里写明):JSON 套两层转义、``ensure_ascii`` 的非 ASCII
双层、HTML 数字实体 ``&#38;``、服务端只回显半截链接。
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Sequence
from functools import lru_cache
from urllib.parse import quote, unquote, urlparse

#: 百分号编码(任意大小写),归一成小写用。固定的小正则,导入时编译一次。
_PERCENT_ANY_RE = re.compile(r"%[0-9A-Fa-f]{2}")
#: 紧跟在一段链接后面、说明链接还没完的字符。
_CONTINUES = frozenset(string.ascii_letters + string.digits + "-_~/?#%=&+")
#: 完全百分号编码的写法(``quote(safe="")``)里,紧跟其后还算链接一部分的字符:这种写法里
#: 不会有原样的 ``& # = ? / +``,所以它们出现就是外层的分隔符(``?src=<编码链接>&page=2``)。
_ENCODED_CONTINUES = frozenset(string.ascii_letters + string.digits + "-_~%")
#: ``encodeURIComponent`` 的写法多保留 ``!'()*``;``quote(url)`` 的写法多保留 ``/``。
_COMPONENT_CONTINUES = _ENCODED_CONTINUES | frozenset("!'()*")
_QUOTE_CONTINUES = _ENCODED_CONTINUES | frozenset("/")
#: ``encodeURIComponent`` 不转义的几个字符(``quote`` 默认会转义)。
_URI_COMPONENT_SAFE = "!'()*~"
#: 每条链接的写法 / 预筛串按链接缓存;上限远大于每对话的登记上限(200)。
_CACHE_SIZE = 512


def _lower_percent(text: str) -> str:
    return _PERCENT_ANY_RE.sub(lambda m: m.group(0).lower(), text) if "%" in text else text


def _escapes(text: str) -> set[str]:
    """JSON 转义(``ensure_ascii`` 真假)、``&`` 写成 ``\\u0026``(Go ``encoding/json``)或
    ``&amp;``(HTML)、``/`` 写成 ``\\/`` —— 可以叠加。"""
    out = {
        text,
        json.dumps(text, ensure_ascii=True)[1:-1],
        json.dumps(text, ensure_ascii=False)[1:-1],
    }
    out |= {form.replace("&", "\\u0026") for form in out} | {
        form.replace("&", "&amp;") for form in out
    }
    out |= {form.replace("/", "\\/") for form in out}
    return out


@lru_cache(maxsize=_CACHE_SIZE)
def link_forms(url: str) -> tuple[tuple[str, frozenset[str] | None], ...]:
    """一条链接在工具结果里可能的写法(已归一成小写百分号),长的在前。

    每项是 ``(写法, 结尾边界字符集)``:原样与 JSON 转义这类写法的边界字符集是 ``None``(用
    :func:`_continues` 的规则);三种百分号编码(``quote`` 的两种 ``safe`` 与
    ``encodeURIComponent``)各有自己的字符集。同一串字出现在几种写法里时,按原样那种算
    (它的边界最严,宁可不遮也不遮坏别的链接)。
    """
    # 同一串字可能是几种写法(没有 ``!'()*`` 的链接,``safe=""`` 与 ``encodeURIComponent`` 的写法
    # 完全一样):取各自边界字符集的**交集**(最窄的),与写入顺序无关;其中有原样规则(``None``)
    # 的就用原样规则。
    kinds: dict[str, frozenset[str] | None] = {}

    def add(form: str, charset: frozenset[str] | None) -> None:
        if form not in kinds:
            kinds[form] = charset
        elif kinds[form] is None or charset is None:
            kinds[form] = None
        else:
            kinds[form] = kinds[form] & charset  # type: ignore[operator]

    for base in {url, unquote(url)}:
        add(quote(base, safe=""), _ENCODED_CONTINUES)
        add(quote(base, safe=_URI_COMPONENT_SAFE), _COMPONENT_CONTINUES)
        add(quote(base), _QUOTE_CONTINUES)
        for form in _escapes(base):
            add(form, None)
    normalized = {_lower_percent(form): kind for form, kind in kinds.items() if form}
    return tuple(sorted(normalized.items(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=_CACHE_SIZE)
def _host_probes(url: str) -> tuple[str, ...]:
    """预筛用的主机名写法;取不到主机名就是空(这样的链接本来就不登记)。"""
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        return ()
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    if not host:
        return ()
    probes = {host, json.dumps(host)[1:-1], quote(host, safe="")}
    return tuple(_lower_percent(probe) for probe in probes if probe)


def _continues(text: str, end: int, charset: frozenset[str] | None = None) -> bool:
    """``text[end:]`` 是不是还在接着写同一条链接。``charset`` 给了就只按它判(百分号编码的写法)。"""
    if end >= len(text):
        return False
    char = text[end]
    if char == ".":
        following = text[end + 1 : end + 2]
        return bool(following) and following in _CONTINUES and following.isalnum()
    if charset is not None:
        return char in charset
    if char in _CONTINUES:
        return True
    if char == "\\":
        return text.startswith(("\\/", "\\u0026"), end)
    return False


def _spans(normalized: str, pairs: Sequence[tuple[str, str]]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for link, url in pairs:
        if not any(probe in normalized for probe in _host_probes(url)):
            continue
        for form, charset in link_forms(url):
            start = normalized.find(form)
            while start != -1:
                end = start + len(form)
                if not _continues(normalized, end, charset):
                    found.append((start, end, link))
                start = normalized.find(form, start + 1)
    # 重叠的取长的;同样长取靠前的。
    found.sort(key=lambda span: (span[0] - span[1], span[0]))
    chosen: list[tuple[int, int, str]] = []
    for span in found:
        if all(span[1] <= other[0] or span[0] >= other[1] for other in chosen):
            chosen.append(span)
    return sorted(chosen)


def mask_text(text: str, pairs: Sequence[tuple[str, str]]) -> tuple[str, int]:
    """``pairs`` = ``(编号, 原链接)``。返回 (遮过的文字, 换了几处)。没有命中时原样返回。"""
    if not text or not pairs:
        return text, 0
    spans = _spans(_lower_percent(text), pairs)
    if not spans:
        return text, 0
    parts: list[str] = []
    cursor = 0
    for start, end, link in spans:
        parts.append(text[cursor:start])
        parts.append(link)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), len(spans)
