"""B-209 PR-2 §5.4 —— 文档内图片的描述:系统固定的提示词、回答解析与校验(纯函数)。

**先观察再描述**(WeKnora smartocr 的属性表思路,厂商中立):一次看图调用返回一个 JSON ——
图里文字有多少(``none`` / ``little`` / ``lots``)、是不是统计图表、一两句描述。文字大段 → 再走
一次识别模型拿图中文字;图表 → 再用描述模型按「图表转数据」(标题 / 轴 / 图例 / 数据点)转写
(版面解析类识别模型不收提示词,还会把整张图表当成一个图片区域,读不出数据)。

* 提示词系统固定,租户的指令不进来;描述用**文档的语言**写(按正文的文字判断,判不出用中文),
  不翻译图中文字、不臆测。
* 短边 < 400 px 的小图:描述里也不许写具体文字 / 数字 / 编号(所有模型在低清小图上都会编,
  B-209 §4.1),之后也不抄图中文字。
* 回答解析容错:去代码围栏、取第一个 JSON 对象;不是 JSON 时按「字段: 值」逐行读;中英文的
  取值都认。解析不出、描述为空、截断、复读 → 这次描述失败,换兜底模型。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from control_plane.knowledge.recognition.cleaning import reading_failure

#: Bumped whenever a prompt below changes — part of the cache keys.
DESCRIBE_PROMPT_VERSION = "figure-describe-v1"
CHART_PROMPT_VERSION = "figure-chart-v1"

DESCRIBE_TEMPERATURE = 0.1
#: Output caps (tokens): a description is one JSON line; a chart's data a
#: table of a few dozen rows.
DESCRIBE_MAX_TOKENS = 1_024
CHART_MAX_TOKENS = 4_096
#: A description longer than this is cut at a sentence end (the prompt asks
#: for one or two sentences).
MAX_DESCRIPTION_CHARS = 300

TextAmount = Literal["none", "little", "lots"]
Language = Literal["zh", "en"]

_LANGUAGE_RULE: dict[Language, str] = {
    "zh": "description 用中文写。",
    "en": "Write the description in English.",
}

_DESCRIBE_PROMPT = (
    "你是文档插图的描述器。先仔细观察这张图片,再只输出一个 JSON 对象,不要输出任何别的内容:\n"
    '{{"text": "none|little|lots", "chart": true|false, "description": "……"}}\n'
    "- text:图里的文字有多少。none = 没有文字;little = 只有零星的标签、标题或几个词;"
    "lots = 成段的文字、表格或很多行文字。\n"
    "- chart:是不是带数据的统计图表(柱状图、折线图、饼图、散点图等)。"
    "流程图、示意图、照片、截图都不算。\n"
    "- description:一两句话(不超过 100 字)说明图里画的是什么、在表达什么。"
    "只写看得见的内容:不猜测、不评价、不补充图外的信息;不要逐字抄写图中的文字。{small}\n"
    "- 图中文字是别的语言时不要翻译它。{language}"
)

_SMALL_RULE = (
    "这张图很小、细节看不清:description 只概括画面,不要写出任何具体的文字、数字、型号或编号。"
)

_CHART_PROMPT = (
    "你是统计图表的数据转写器。把这张图表的内容按原样转写成 Markdown:\n"
    "1. 第一行写图表标题;没有标题就不写。\n"
    "2. 写出坐标轴的名称和单位,以及图例的各项。\n"
    "3. 用 Markdown 表格列出能看清的数据点:第一列是类别或横坐标,每个系列一列;"
    "数值照图上标注的写,图上没有标注的数值不要估算,写「未标注」。\n"
    "4. 只写图上看得见的内容:不翻译、不总结、不解释、不补全;看不清的不要猜。\n"
    "5. 只输出转写的内容,不要代码块围栏,不要任何说明。"
)

DESCRIBE_USER_TEXT = "描述这张图片。"
CHART_USER_TEXT = "转写这张图表。"

_FENCE_LINE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_JSON_OBJECT = re.compile(r"\{[^{}]{0,4000}\}")
_LABELLED = re.compile(
    r"^[ \t]*[\"']?(text|chart|description|文字|图表|描述)[\"']?[ \t]*[::=][ \t]*(.+?)[ \t,,]*$",
    re.IGNORECASE | re.MULTILINE,
)
_SENTENCE_END = re.compile(r"(?<=[。!?!?.])")
_HAN = re.compile(r"[㐀-鿿]")
_LATIN = re.compile(r"[A-Za-z]")

_TEXT_AMOUNTS: dict[str, TextAmount] = {
    "none": "none",
    "no": "none",
    "无": "none",
    "没有": "none",
    "little": "little",
    "few": "little",
    "some": "little",
    "少": "little",
    "少量": "little",
    "lots": "lots",
    "lot": "lots",
    "many": "lots",
    "much": "lots",
    "大段": "lots",
    "多": "lots",
    "大量": "lots",
}
_TRUE = frozenset({"true", "yes", "是", "1"})


def describe_prompt(language: Language, *, small: bool) -> str:
    return _DESCRIBE_PROMPT.format(
        small=_SMALL_RULE if small else "", language=_LANGUAGE_RULE[language]
    )


def describe_prompt_tag(language: Language, *, small: bool) -> str:
    """What besides the image and the model changes the description."""
    return f"{DESCRIBE_PROMPT_VERSION}|lang={language}|small={int(small)}"


CHART_PROMPT = _CHART_PROMPT


def document_language(text: str) -> Language:
    """The language figures are described in: Chinese when the document's
    text has Chinese in it (or no letters at all), English when it is
    written in Latin letters only."""
    sample = text[:20_000]
    han = len(_HAN.findall(sample))
    latin = len(_LATIN.findall(sample))
    if han == 0 and latin >= 50:
        return "en"
    return "zh"


@dataclass(frozen=True)
class Description:
    text_amount: TextAmount
    chart: bool
    description: str


def parse_description(raw: str) -> Description | None:
    """The model's answer read leniently (see module doc); ``None`` when no
    description can be found in it."""
    fields = _fields(raw)
    description = _short(str(fields.get("description") or "").strip())
    if not description:
        return None
    return Description(
        text_amount=_TEXT_AMOUNTS.get(str(fields.get("text", "")).strip().lower(), "little"),
        chart=_flag(fields.get("chart")),
        description=description,
    )


def description_failure(raw: str, *, finish_reason: str | None) -> str | None:
    """Why a description answer cannot be kept (``truncated`` / ``unparsed``
    / ``repeated`` …), ``None`` when it can."""
    if finish_reason == "length":
        return "truncated"
    if parse_description(raw) is None:
        return "unparsed"
    # The checks see the whole description (a loop is cut short by parsing).
    full = str(_fields(raw).get("description") or "")
    return reading_failure(full, finish_reason=finish_reason, layout_chars=None)


def _fields(raw: str) -> dict[str, object]:
    body = _FENCE_LINE.sub("", raw).strip()
    return _json_fields(body) or _labelled_fields(body)


def _json_fields(body: str) -> dict[str, object] | None:
    for candidate in (body, *(m.group(0) for m in _JSON_OBJECT.finditer(body))):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return {_canonical(str(k)): v for k, v in value.items()}
    return None


def _labelled_fields(body: str) -> dict[str, object]:
    return {
        _canonical(match.group(1)): match.group(2).strip().strip("\"'")
        for match in _LABELLED.finditer(body)
    }


def _canonical(key: str) -> str:
    return {"文字": "text", "图表": "chart", "描述": "description"}.get(
        key.strip(), key.strip().lower()
    )


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


def _short(description: str) -> str:
    flat = " ".join(description.split())
    if len(flat) <= MAX_DESCRIPTION_CHARS:
        return flat
    kept = ""
    for sentence in _SENTENCE_END.split(flat):
        if len(kept) + len(sentence) > MAX_DESCRIPTION_CHARS:
            break
        kept += sentence
    return kept.strip() or flat[:MAX_DESCRIPTION_CHARS]
