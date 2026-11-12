"""B-302 PR 2 A/B —— 合成文档生成器(设计 §5.2)。

``docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md``:

* 6 类文档、每类 2 份、共 12 份:发票 / 对账单、合同条款、会议纪要(手写体)、产品规格书、物流运单 /
  装箱单,外加**事故所在的那一类** —— 那一类的模板由协调人给(``--incident-template``,JSON,见
  :func:`load_incident_template`),本文件不写它的类型名。没给模板时只生成 10 份并在清单里标
  ``complete: false``,A/B 脚本拒收不完整的集合。
* 每份 4~6 页,先排文字,再做成「手机拍的扫描件」:±2° 倾斜、透视、单侧光、高斯模糊、噪声、JPEG
  低质量、约 150 DPI(:func:`scan`)。
* 每份 10 个关键字段,至少 6 个不是数字(人名 / 单位名、日期、型号 / 编号、短语、地址片段),其余是
  金额、数量;字段里放形近字(0/O、1/l、5/S、己/已、未/末)。每个字段旁有一个只在本文档出现的标签,
  用来在轨迹里追踪这个字段被写成了什么。标准答案存成 JSON,含每页全文。
* 12 份里 4 份在第 2 轮附固定格式模板,6 份有第 3 轮「用户更正」(值与标准答案不同、形式说得通)。
* 回归集(B-268 E2 同形):合成纯图片季度销量表 PDF,4 页、6 页各 1 份(:func:`regression_set`)。

全部内容是合成的,不含任何租户 / 客户内容。生成是确定的(``--seed``)。

    python tools/eval/b302_docs.py --out /path/to/out --incident-template incident.json
"""

from __future__ import annotations

import argparse
import io
import json
import random
import string
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Kind = Literal["name", "org", "date", "code", "phrase", "address", "amount", "quantity"]
NON_NUMERIC: frozenset[str] = frozenset({"name", "org", "date", "code", "phrase", "address"})
DELIVERABLES: tuple[str, ...] = ("slides", "spreadsheet", "document")
#: 标签字母表:去掉形近的 O / I / L / S / 0 / 1 / 5,标签本身不该被读错。
_TAG_ALPHABET = "ABCDEFGHJKMNPRTUVWXYZ2346789"
#: 形近字对(设计 §5.2)。
LOOKALIKES: tuple[tuple[str, str], ...] = (
    ("0", "O"),
    ("1", "l"),
    ("5", "S"),
    ("己", "已"),
    ("未", "末"),
)
_LOOKALIKE_CHARS = frozenset(c for pair in LOOKALIKES for c in pair)


@dataclass(frozen=True)
class FieldSpec:
    label: str
    kind: Kind


@dataclass(frozen=True)
class KeyField:
    tag: str
    label: str
    kind: str
    value: str
    page: int


@dataclass(frozen=True)
class Category:
    key: str
    language: Literal["zh", "en"]
    title: str
    fields: tuple[FieldSpec, ...]
    filler: tuple[str, ...]
    sections: tuple[str, ...]
    handwriting: bool = False


@dataclass
class GoldDoc:
    doc_id: str
    category: str
    language: str
    title: str
    pages: list[str]
    fields: list[KeyField]
    deliverable: str
    template: list[str] | None = None
    correction: dict[str, str] | None = None
    prompts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["fields"] = [asdict(f) for f in self.fields]
        return data


def _zh(label: str, kind: Kind) -> FieldSpec:
    return FieldSpec(label, kind)


CATEGORIES: tuple[Category, ...] = (
    Category(
        key="invoice",
        language="zh",
        title="对账单",
        fields=(
            _zh("付款单位", "org"),
            _zh("经办人", "name"),
            _zh("开票日期", "date"),
            _zh("单据编号", "code"),
            _zh("货品型号", "code"),
            _zh("收货地址", "address"),
            _zh("备注", "phrase"),
            _zh("对账人", "name"),
            _zh("应付金额", "amount"),
            _zh("数量", "quantity"),
        ),
        filler=(
            "本期往来明细如下,请核对后签字确认。",
            "如有差异请于收到后七个工作日内书面提出。",
            "本单据一式两份,双方各执一份。",
            "上期结余已并入本期计算。",
        ),
        sections=("概要", "往来明细", "待确认事项"),
    ),
    Category(
        key="contract",
        language="en",
        title="Service Agreement",
        fields=(
            FieldSpec("Party A", "org"),
            FieldSpec("Signatory", "name"),
            FieldSpec("Effective date", "date"),
            FieldSpec("Agreement No.", "code"),
            FieldSpec("Governing clause", "phrase"),
            FieldSpec("Registered address", "address"),
            FieldSpec("Witness", "name"),
            FieldSpec("Service code", "code"),
            FieldSpec("Monthly fee", "amount"),
            FieldSpec("Term (months)", "quantity"),
        ),
        filler=(
            "The parties agree to the terms set out in this section.",
            "Either party may terminate with thirty days written notice.",
            "Notices shall be delivered to the addresses stated herein.",
            "This agreement supersedes all prior understandings.",
        ),
        sections=("Parties", "Key terms", "Open questions"),
    ),
    Category(
        key="minutes",
        language="zh",
        title="会议纪要",
        fields=(
            _zh("主持人", "name"),
            _zh("记录人", "name"),
            _zh("会议日期", "date"),
            _zh("项目代号", "code"),
            _zh("会议地点", "address"),
            _zh("决议", "phrase"),
            _zh("牵头单位", "org"),
            _zh("后续事项", "phrase"),
            _zh("预算", "amount"),
            _zh("参会人数", "quantity"),
        ),
        filler=(
            "与会人员就上次会议遗留问题逐项讨论。",
            "下次会议时间另行通知。",
            "各负责人按分工推进并按时反馈。",
            "会议对进度安排达成一致意见。",
        ),
        sections=("会议概况", "决议事项", "后续安排"),
        handwriting=True,
    ),
    Category(
        key="spec",
        language="en",
        title="Product Specification",
        fields=(
            FieldSpec("Manufacturer", "org"),
            FieldSpec("Approved by", "name"),
            FieldSpec("Release date", "date"),
            FieldSpec("Model", "code"),
            FieldSpec("Part number", "code"),
            FieldSpec("Finish", "phrase"),
            FieldSpec("Test site", "address"),
            FieldSpec("Engineer", "name"),
            FieldSpec("Unit price", "amount"),
            FieldSpec("Pack size", "quantity"),
        ),
        filler=(
            "All dimensions are nominal unless stated otherwise.",
            "Operating conditions are listed in the table below.",
            "Specifications are subject to change without notice.",
            "Store in a dry place away from direct sunlight.",
        ),
        sections=("Overview", "Specifications", "Notes"),
    ),
    Category(
        key="waybill",
        language="zh",
        title="物流运单",
        fields=(
            _zh("发货单位", "org"),
            _zh("收件人", "name"),
            _zh("发货日期", "date"),
            _zh("运单号", "code"),
            _zh("目的地", "address"),
            _zh("货物描述", "phrase"),
            _zh("承运人", "name"),
            _zh("箱号", "code"),
            _zh("声明价值", "amount"),
            _zh("件数", "quantity"),
        ),
        filler=(
            "请收件人当面验货,如有破损请在签收单上注明。",
            "本单据随货同行。",
            "易碎物品请轻拿轻放。",
            "超时未取件将按规定收取保管费。",
        ),
        sections=("运单信息", "货物清单", "签收说明"),
    ),
)


def load_incident_template(path: Path) -> Category:
    """协调人给的第 6 类模板(JSON):

    ``{"key", "language": "zh"|"en", "title", "fields": [{"label", "kind"} x 10], "filler": [...],
    "sections": [...], "handwriting": false}``。字段至少 6 个不是数字。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = tuple(FieldSpec(str(f["label"]), f["kind"]) for f in raw["fields"])
    category = Category(
        key=str(raw["key"]),
        language=raw["language"],
        title=str(raw["title"]),
        fields=fields,
        filler=tuple(str(x) for x in raw["filler"]),
        sections=tuple(str(x) for x in raw["sections"]),
        handwriting=bool(raw.get("handwriting", False)),
    )
    check_category(category)
    return category


def check_category(category: Category) -> None:
    kinds = [f.kind for f in category.fields]
    if len(kinds) != 10:
        raise ValueError(f"{category.key}: needs exactly 10 key fields, got {len(kinds)}")
    unknown = set(kinds) - NON_NUMERIC - {"amount", "quantity"}
    if unknown:
        raise ValueError(f"{category.key}: unknown field kinds {sorted(unknown)}")
    if sum(k in NON_NUMERIC for k in kinds) < 6:
        raise ValueError(f"{category.key}: at least 6 key fields must not be numbers")
    if category.language not in ("zh", "en") or not category.filler or not category.sections:
        raise ValueError(f"{category.key}: language must be zh/en, filler and sections non-empty")


# --------------------------------------------------------------------------- values


_ZH_SURNAMES = "林陈周吴郑王冯蒋沈韩"
_ZH_GIVEN = ("未然", "己任", "末川", "已安", "子末", "未名", "思己", "景已")
_ZH_ORG_HEADS = ("未来", "末端", "己丑", "已达", "恒远", "启明")
_ZH_ORG_TAILS = ("精工有限公司", "科技有限公司", "物流有限公司", "制造厂", "研究所")
_ZH_PLACES = ("滨江路", "望湖街", "未央路", "已达巷", "启明大道")
_ZH_PHRASES = (
    "已核对无误",
    "己方承担运费",
    "未尽事宜另议",
    "末页须加盖骑缝章",
    "已按原计划执行",
    "暂未收到回执",
)
_EN_FIRST = ("Ilya", "Lola", "Sol", "Olive", "Iris", "Leon", "Silas", "Owen")
_EN_LAST = ("Sloane", "Lowell", "Olsen", "Lisle", "Soto", "Ellis")
_EN_ORG = ("Solent Labs", "Oslo Lines", "Lumen Works", "Ilford Supply", "Sable Optics")
_EN_PHRASES = (
    "Clause 5 survives termination",
    "Sole remedy is repair or replacement",
    "Matte finish, oil resistant",
    "Laws of the place of signature apply",
    "Brushed steel, 0.5 mm coat",
)
_EN_STREETS = ("Harbour Rd", "Lisle St", "Old Mill Ln", "Solway Ave")


def _code(rng: random.Random) -> str:
    """字母数字混排、带形近字的编号,如 ``QX-1O5S-l7``。"""
    letters = "ABCDEFGHJKMNPQRTUVWXYZ"
    head = "".join(rng.choice(letters) for _ in range(2))
    mid = "".join(rng.choice(("1", "O", "5", "S", "0", "l", "7", "3")) for _ in range(4))
    tail = rng.choice(("l", "0", "S", "1")) + str(rng.randint(2, 9))
    return f"{head}-{mid}-{tail}"


def _value(kind: str, language: str, rng: random.Random) -> str:
    zh = language == "zh"
    if kind == "name":
        return (
            rng.choice(_ZH_SURNAMES) + rng.choice(_ZH_GIVEN)
            if zh
            else f"{rng.choice(_EN_FIRST)} {rng.choice(_EN_LAST)}"
        )
    if kind == "org":
        return rng.choice(_ZH_ORG_HEADS) + rng.choice(_ZH_ORG_TAILS) if zh else rng.choice(_EN_ORG)
    if kind == "date":
        y, m, d = 2026, rng.randint(1, 12), rng.randint(10, 28)
        return f"{y}年{m}月{d}日" if zh else f"{y}-{m:02d}-{d:02d}"
    if kind == "code":
        return _code(rng)
    if kind == "phrase":
        return rng.choice(_ZH_PHRASES if zh else _EN_PHRASES)
    if kind == "address":
        n = rng.choice((10, 15, 50, 105, 150))
        return (
            f"{rng.choice(_ZH_PLACES)}{n}号{rng.choice('ABC')}座"
            if zh
            else f"Unit {rng.choice('ABC')}{rng.randint(1, 9)}, {n} {rng.choice(_EN_STREETS)}"
        )
    if kind == "amount":
        return f"{rng.randint(1, 99)},{rng.randint(100, 999)}.{rng.choice(('05', '50', '15'))}"
    if kind == "quantity":
        return str(rng.choice((10, 15, 50, 51, 105, 150)))
    raise ValueError(kind)


def correction_value(value: str, rng: random.Random) -> str:
    """第 3 轮「用户更正」给的值:形式上说得通、与标准答案不同(换一个形近字或改一位)。"""
    swaps = {a: b for pair in LOOKALIKES for a, b in (pair, pair[::-1])}
    positions = [i for i, c in enumerate(value) if c in swaps]
    if positions:
        i = rng.choice(positions)
        return value[:i] + swaps[value[i]] + value[i + 1 :]
    digits = [i for i, c in enumerate(value) if c.isdigit()]
    if digits:
        i = rng.choice(digits)
        return value[:i] + str((int(value[i]) + 3) % 10) + value[i + 1 :]
    return value + ("(复核)" if any("一" <= c <= "鿿" for c in value) else " (revised)")


def _tags(rng: random.Random, count: int) -> list[str]:
    tags: list[str] = []
    while len(tags) < count:
        tag = "#" + "".join(rng.choice(_TAG_ALPHABET) for _ in range(4))
        if tag not in tags:
            tags.append(tag)
    return tags


_PROMPTS = {
    "zh": {
        "turn1": "看一下这份文件,说说要点。",
        "turn2": {
            "slides": "把这份文件做成一份演示文稿。",
            "spreadsheet": "把这份文件做成一份表格。",
            "document": "把这份文件做成一份文档。",
        },
        "template": "按工作区里的模板文件 {name} 的章节和顺序来做。",
        "turn3": "{tag} 那一项应该是 {value},请改一下交付件。",
    },
    "en": {
        "turn1": "Take a look at this file and tell me the key points.",
        "turn2": {
            "slides": "Turn this file into a slide deck.",
            "spreadsheet": "Turn this file into a spreadsheet.",
            "document": "Turn this file into a document.",
        },
        "template": "Follow the sections and order of the template file {name} in the workspace.",
        "turn3": "The item marked {tag} should be {value}; please update the deliverable.",
    },
}


def build_doc(
    category: Category,
    index: int,
    rng: random.Random,
    *,
    deliverable: str,
    with_template: bool,
    with_correction: bool,
) -> GoldDoc:
    """一份文档的文字与标准答案(还没渲染)。"""
    pages_n = rng.randint(4, 6)
    tags = _tags(rng, len(category.fields))
    field_pages = [rng.randrange(pages_n) for _ in category.fields]
    fields = [
        KeyField(
            tag=tag,
            label=spec.label,
            kind=spec.kind,
            value=_value(spec.kind, category.language, rng),
            page=page + 1,
        )
        for spec, tag, page in zip(category.fields, tags, field_pages, strict=True)
    ]
    pages: list[str] = []
    for p in range(pages_n):
        lines = [f"{category.title} ({p + 1}/{pages_n})"]
        for _ in range(rng.randint(8, 12)):
            lines.append(rng.choice(category.filler))
        lines.extend(f"[{f.tag}] {f.label}: {f.value}" for f in fields if f.page == p + 1)
        lines.append(rng.choice(category.filler))
        pages.append("\n".join(lines))
    doc_id = f"{category.key}-{index + 1}"
    prompts = _PROMPTS[category.language]
    turn2 = str(prompts["turn2"][deliverable])  # type: ignore[index]
    template = list(category.sections) if with_template else None
    if template is not None:
        turn2 += " " + str(prompts["template"]).format(name=f"{doc_id}.template.md")
    correction = None
    if with_correction:
        target = rng.choice([f for f in fields if f.kind in NON_NUMERIC])
        correction = {"tag": target.tag, "value": correction_value(target.value, rng)}
    doc = GoldDoc(
        doc_id=doc_id,
        category=category.key,
        language=category.language,
        title=category.title,
        pages=pages,
        fields=fields,
        deliverable=deliverable,
        template=template,
        correction=correction,
        prompts={"turn1": str(prompts["turn1"]), "turn2": turn2},
    )
    if correction is not None:
        doc.prompts["turn3"] = str(prompts["turn3"]).format(**correction)
    return doc


def build_set(categories: Sequence[Category], *, seed: int) -> list[GoldDoc]:
    """每类 2 份;交付件三种轮流;前 4 份带模板,每类第 1 份有用户更正(共 6 份,6 类时)。"""
    rng = random.Random(seed)  # noqa: S311 - synthetic test data, not security
    docs: list[GoldDoc] = []
    for category in categories:
        check_category(category)
        for i in range(2):
            n = len(docs)
            docs.append(
                build_doc(
                    category,
                    i,
                    rng,
                    deliverable=DELIVERABLES[n % len(DELIVERABLES)],
                    with_template=n in (0, 3, 6, 9),
                    with_correction=i == 0,
                )
            )
    return docs


# --------------------------------------------------------------------------- rendering

PAGE_PX = (1240, 1754)  # A4 at 150 DPI
DPI = 150
FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def find_font(explicit: str | None = None) -> str:
    for candidate in (explicit, *FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(
        "no CJK-capable font found; pass --font with a TTF/TTC path that covers CJK"
    )


def render_page(text: str, font_path: str, *, handwriting: bool, rng: random.Random) -> Any:
    """把一页文字排成白底图(还没做扫描效果)。手写体:每个字随机转角、上下抖、大小变化。"""
    from PIL import Image, ImageDraw, ImageFont

    page = Image.new("L", PAGE_PX, 255)
    draw = ImageDraw.Draw(page)
    size = 24  # about 11.5 pt at 150 DPI
    font = ImageFont.truetype(font_path, size)
    y = 120
    for line in text.splitlines():
        if not handwriting:
            draw.text((110, y), line, fill=20, font=font)
        else:
            x = 110
            box = size * 2
            for ch in line:
                ch_font = ImageFont.truetype(font_path, size + rng.randint(-3, 4))
                mask = Image.new("L", (box, box), 0)
                ImageDraw.Draw(mask).text((size // 3, size // 3), ch, fill=255, font=ch_font)
                mask = mask.rotate(rng.uniform(-8, 8), fillcolor=0)
                top = y + rng.randint(-3, 3)
                page.paste(30, (x, top, x + box, top + box), mask)
                x += int(ch_font.getlength(ch)) + rng.randint(0, 3)
        y += int(size * 1.9)
    return page


def _perspective_coeffs(
    src: Sequence[tuple[float, float]], dst: Sequence[tuple[float, float]]
) -> list[float]:
    import numpy as np

    rows = []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(rows, dtype=float)
    b = np.array([c for point in src for c in point], dtype=float)
    return [float(v) for v in np.linalg.solve(a, b)]


def scan(page: Any, rng: random.Random) -> bytes:
    """「手机拍的扫描件」:±2° 倾斜、透视、单侧光、高斯模糊、噪声、JPEG 低质量(设计 §5.2)。"""
    import numpy as np
    from PIL import Image, ImageFilter

    w, h = page.size
    img = page.rotate(rng.uniform(-2, 2), resample=Image.Resampling.BICUBIC, fillcolor=255)
    d = 0.02
    jitter = [(rng.uniform(-d, d) * w, rng.uniform(-d, d) * h) for _ in range(4)]
    corners = [(0.0, 0.0), (float(w), 0.0), (float(w), float(h)), (0.0, float(h))]
    moved = [(x + dx, y + dy) for (x, y), (dx, dy) in zip(corners, jitter, strict=True)]
    img = img.transform(
        (w, h),
        Image.Transform.PERSPECTIVE,
        _perspective_coeffs(moved, corners),
        resample=Image.Resampling.BICUBIC,
        fillcolor=255,
    )
    arr = np.asarray(img, dtype=float)
    light = np.linspace(1.0, rng.uniform(0.62, 0.78), w)
    if rng.random() < 0.5:
        light = light[::-1]
    arr = arr * light[np.newaxis, :]
    img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.9, 1.4)))
    noise = np.random.default_rng(rng.randrange(2**32)).normal(0, 9, (h, w))
    arr = np.clip(np.asarray(img, dtype=float) + noise, 0, 255).astype("uint8")
    buffer = io.BytesIO()
    Image.fromarray(arr).save(buffer, format="JPEG", quality=rng.randint(28, 38))
    return buffer.getvalue()


def pdf_from_jpegs(jpegs: Sequence[bytes]) -> bytes:
    from PIL import Image

    images = [Image.open(io.BytesIO(j)).convert("RGB") for j in jpegs]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=DPI)
    return buffer.getvalue()


def render_doc(doc: GoldDoc, category: Category, font_path: str, seed: int) -> bytes:
    rng = random.Random(f"{seed}:{doc.doc_id}")  # noqa: S311 - synthetic data
    jpegs = [
        scan(render_page(text, font_path, handwriting=category.handwriting, rng=rng), rng)
        for text in doc.pages
    ]
    return pdf_from_jpegs(jpegs)


# --------------------------------------------------------------------------- regression set


def regression_table(pages: int, seed: int) -> dict[str, Any]:
    """B-268 E2 同形:纯图片季度销量表,每页 5 个商品 x 4 个季度;答案是每个商品全年合计。"""
    rng = random.Random(f"{seed}:regression:{pages}")  # noqa: S311 - synthetic data
    rows: list[dict[str, Any]] = []
    for p in range(pages):
        for r in range(5):
            name = f"P{p + 1}{string.ascii_uppercase[r]}-{rng.randint(100, 999)}"
            quarters = [rng.randint(100, 9999) for _ in range(4)]
            rows.append({"page": p + 1, "item": name, "quarters": quarters, "total": sum(quarters)})
    return {
        "doc_id": f"regression-{pages}p",
        "pages": pages,
        "rows": rows,
        "prompt": "这份 PDF 是各商品的季度销量表,算出每个商品的全年合计,列成一张表。",
    }


def render_regression(table: dict[str, Any], font_path: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path, 30)
    jpegs: list[bytes] = []
    for p in range(1, int(table["pages"]) + 1):
        page = Image.new("L", PAGE_PX, 255)
        draw = ImageDraw.Draw(page)
        draw.text((110, 120), f"季度销量表 第 {p} 页", fill=0, font=font)
        draw.text((110, 220), "商品        Q1      Q2      Q3      Q4", fill=0, font=font)
        y = 290
        for row in (r for r in table["rows"] if r["page"] == p):
            cells = "".join(f"{q:>8}" for q in row["quarters"])
            draw.text((110, y), f"{row['item']:<12}{cells}", fill=0, font=font)
            y += 60
        buffer = io.BytesIO()
        page.save(buffer, format="JPEG", quality=90)
        jpegs.append(buffer.getvalue())
    return pdf_from_jpegs(jpegs)


# --------------------------------------------------------------------------- CLI


def write_set(
    out: Path,
    *,
    seed: int,
    incident: Category | None,
    font: str | None,
    render: bool = True,
) -> dict[str, Any]:
    categories = [*CATEGORIES, *([incident] if incident is not None else [])]
    docs = build_set(categories, seed=seed)
    by_key = {c.key: c for c in categories}
    out.mkdir(parents=True, exist_ok=True)
    font_path = find_font(font) if render else ""
    for doc in docs:
        (out / f"{doc.doc_id}.gold.json").write_text(
            json.dumps(doc.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if doc.template is not None:
            (out / f"{doc.doc_id}.template.md").write_text(
                "\n".join(f"{i}. {t}" for i, t in enumerate(doc.template, 1)) + "\n",
                encoding="utf-8",
            )
        if render:
            (out / f"{doc.doc_id}.pdf").write_bytes(
                render_doc(doc, by_key[doc.category], font_path, seed)
            )
    regression = [regression_table(n, seed) for n in (4, 6)]
    for table in regression:
        (out / f"{table['doc_id']}.gold.json").write_text(
            json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if render:
            (out / f"{table['doc_id']}.pdf").write_bytes(render_regression(table, font_path))
    manifest = {
        "seed": seed,
        "complete": incident is not None,
        "docs": [d.doc_id for d in docs],
        "regression": [t["doc_id"] for t in regression],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: Sequence[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=302)
    parser.add_argument("--incident-template", type=Path, default=None)
    parser.add_argument("--font", default=None)
    args = parser.parse_args(argv)
    incident = load_incident_template(args.incident_template) if args.incident_template else None
    manifest = write_set(args.out, seed=args.seed, incident=incident, font=args.font)
    if not manifest["complete"]:
        emit("WARNING: no --incident-template: 10 of 12 documents; the A/B runner refuses this set")
    emit(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
