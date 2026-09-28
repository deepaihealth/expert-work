"""Map content blocks to layout primitives. Pure presentation: every text is the caller's,
the only words added here are fixed chrome labels (column headers, status tags)."""

from __future__ import annotations

from typing import Any

from hpr.catalog import VARIANTS
from hpr.prims import (
    Bullets,
    Callout,
    Card,
    CardGrid,
    Chart,
    Column,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    Prim,
    RangeBar,
    Step,
    SubHeading,
    Table,
    Tag,
    TimeBars,
    Timeline,
)
from hpr.style import variant_for

DASH = "—"
_POSITION_TAG = {
    "within": Tag("在参考范围内", "within"),
    "above": Tag("高于参考范围", "out"),
    "below": Tag("低于参考范围", "out"),
}
_LEVEL_TAG = {"focus": Tag("重点关注", "out"), "watch": Tag("留意", "info")}


class RenderError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def fmt(v: Any) -> str:
    """Numbers keep the caller's precision (26.0 stays "26.0"); strings pass through."""
    return str(v)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _join(*parts: str, sep: str = " ") -> str:
    return sep.join(p for p in parts if p)


def _table(columns: list[str], rows: list[list[str]], highlight: str | None = None) -> Table:
    """Drop columns that are empty ("") in every row, then show empty cells as a dash. Builders
    pass "" for a missing field, so a caller's own "—" is text and keeps its column."""
    keep = [i for i in range(len(columns)) if i == 0 or any(r[i] != "" for r in rows)]
    cols = tuple(columns[i] for i in keep)
    body = tuple(tuple(r[i] or DASH for i in keep) for r in rows)
    hl = cols.index(highlight) if highlight in cols else None
    return Table(cols, body, hl)


def _grid(cards: list[Card], max_cols: int) -> CardGrid:
    return CardGrid(tuple(cards), cols=max(1, min(max_cols, len(cards))))


def _ref_text(item: dict) -> str:
    if item.get("ref_text"):
        return item["ref_text"]
    lo, hi = item.get("ref_low"), item.get("ref_high")
    if lo is not None and hi is not None:
        return f"参考 {fmt(lo)}–{fmt(hi)}"  # noqa: RUF001
    if lo is not None:
        return f"参考 ≥ {fmt(lo)}"
    if hi is not None:
        return f"参考 ≤ {fmt(hi)}"
    return ""


def _profile(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        rows = [
            [
                it["name"],
                _join(fmt(it["value"]), it.get("unit", "")),
                _ref_text(it),
                _POSITION_TAG[it["position"]].text if it.get("position") in _POSITION_TAG else "",
            ]
            for it in items
        ]
        return [_table(["指标", "当前值", "参考范围", "对照"], rows, highlight="对照")]
    cards = []
    for it in items:
        lo, hi, val = it.get("ref_low"), it.get("ref_high"), it["value"]
        bar = RangeBar(lo, hi, val) if lo is not None and hi is not None and _num(val) else None
        ref = _ref_text(it)
        cards.append(
            Card(
                title=it["name"],
                value=fmt(val),
                unit=it.get("unit", ""),
                lines=(ref,) if ref else (),
                tag=_POSITION_TAG.get(it.get("position", "")),
                bar=bar,
            )
        )
    return [_grid(cards, 4)]


def _issues(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "list":
        sep = "："  # noqa: RUF001
        joined = tuple(_join(i["title"], i.get("evidence", ""), sep=sep) for i in items)
        return [Bullets(joined, "numbers")]
    cards = [
        Card(
            badge=f"{n:02d}",
            title=i["title"],
            lines=(i["evidence"],) if i.get("evidence") else (),
            tag=_LEVEL_TAG.get(i.get("level", "")),
        )
        for n, i in enumerate(items, start=1)
    ]
    return [_grid(cards, 3)]


def _trend(b: dict, v: str) -> list[Prim]:
    unit = b.get("unit", "")
    head = SubHeading(b["metric"] + (f"（{unit}）" if unit else ""))  # noqa: RUF001
    cats = tuple(p["date"] for p in b["points"])
    vals = tuple(float(p["value"]) for p in b["points"])
    lo, hi = b.get("target_low"), b.get("target_high")
    out: list[Prim] = [head]
    if v == "bar":
        out.append(Chart("bar", cats, vals, unit))
        if lo is not None or hi is not None:
            rng = "–".join(fmt(x) for x in (lo, hi) if x is not None)  # noqa: RUF001
            out.append(Paragraph(_join(f"目标区间 {rng}", unit)))
    else:
        out.append(Chart("line", cats, vals, unit, low=lo, high=hi))
    if b.get("ref_text"):
        out.append(Paragraph(b["ref_text"]))
    return out


def _goals(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        rows = [
            [
                i["name"],
                fmt(i["current"]) if "current" in i else "",
                fmt(i["target"]),
                i.get("unit", ""),
                i.get("due", ""),
                i.get("note", ""),
            ]
            for i in items
        ]
        return [_table(["目标", "当前", "目标值", "单位", "期限", "说明"], rows)]
    cards = []
    for i in items:
        value = f"{fmt(i['current'])} → {fmt(i['target'])}" if "current" in i else fmt(i["target"])
        lines = tuple(x for x in (i.get("due", ""), i.get("note", "")) if x)
        cards.append(Card(title=i["name"], value=value, unit=i.get("unit", ""), lines=lines))
    return [_grid(cards, 3)]


def _phases(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        areas: list[str] = []
        for it in items:
            for f in it["focus"]:
                if f["area"] not in areas:
                    areas.append(f["area"])
        rows = [
            [
                it["label"],
                *("\n".join(f["text"] for f in it["focus"] if f["area"] == a) for a in areas),
            ]
            for it in items
        ]
        return [_table(["阶段", *areas], rows)]
    if v == "columns":
        return [
            Columns(
                tuple(
                    Column(
                        it["label"],
                        tuple(f"{f['area']}：{f['text']}" for f in it["focus"]),  # noqa: RUF001
                    )
                    for it in items
                )
            )
        ]
    return [
        Timeline(
            tuple(
                Step(
                    it["label"],
                    tuple(f"{f['area']}：{f['text']}" for f in it["focus"]),  # noqa: RUF001
                )
                for it in items
            )
        )
    ]


def _nutrition(b: dict, v: str, path: str) -> list[Prim]:
    out: list[Prim] = []
    macros = b.get("macros") or []
    energy = fmt(b["energy_kcal"]) + " kcal" if "energy_kcal" in b else ""
    if v == "donut" and macros:
        bad = [m for m in macros if not _num(m.get("percent"))]
        if bad:
            raise RenderError(
                path,
                "环形图需要每个营养素的 percent 都是数字；请改用表格版式（table）",  # noqa: RUF001
            )
        legend = tuple(
            _join(
                m["name"],
                f"{fmt(m['grams'])} g" if "grams" in m else "",
                f"{fmt(m['percent'])}%",
                sep="　",
            )
            for m in macros
        )
        out.append(
            Chart(
                "donut",
                tuple(m["name"] for m in macros),
                tuple(float(m["percent"]) for m in macros),
                legend=legend,
                center=energy,
            )
        )
    else:
        if energy:
            out.append(_grid([Card(title="每日总热量", value=energy)], 1))
        if macros:
            out.append(
                _table(
                    ["营养素", "克数", "占比"],
                    [
                        [
                            m["name"],
                            fmt(m["grams"]) if "grams" in m else "",
                            f"{fmt(m['percent'])}%" if "percent" in m else "",
                        ]
                        for m in macros
                    ],
                )
            )
    if b.get("meals"):
        out.append(
            _table(
                ["餐次", "占比", "说明"],
                [
                    [
                        m["name"],
                        f"{fmt(m['percent'])}%" if "percent" in m else "",
                        m.get("note", ""),
                    ]
                    for m in b["meals"]
                ],
            )
        )
    return out


def _foods(meal: dict) -> tuple[str, ...]:
    return tuple(_join(f["name"], f.get("amount", "")) for f in meal["foods"])


def _meal_plan(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = []
    for tpl in b["templates"]:
        applies = f"（{tpl['applies_to']}）" if tpl.get("applies_to") else ""  # noqa: RUF001
        title = _join(tpl.get("name", ""), applies, sep="")
        if title:
            out.append(SubHeading(title))
        meals = tpl["meals"]
        if v == "table":
            out.append(
                _table(
                    ["餐次", "时间", "食物与用量", "热量"],
                    [
                        [
                            m["name"],
                            m.get("time", ""),
                            "、".join(_foods(m)),
                            f"{fmt(m['kcal'])} kcal" if "kcal" in m else "",
                        ]
                        for m in meals
                    ],
                )
            )
        elif v == "cards":
            out.append(
                _grid(
                    [
                        Card(
                            title=_join(m["name"], m.get("time", "")),
                            value=f"{fmt(m['kcal'])}" if "kcal" in m else "",
                            unit="kcal" if "kcal" in m else "",
                            lines=_foods(m),
                        )
                        for m in meals
                    ],
                    3,
                )
            )
        else:
            out.append(
                Timeline(
                    tuple(
                        Step(
                            _join(m["name"], m.get("time", "")),
                            _foods(m) + ((f"{fmt(m['kcal'])} kcal",) if "kcal" in m else ()),
                        )
                        for m in meals
                    )
                )
            )
    return out


def _diet_rules(b: dict, v: str) -> list[Prim]:
    groups = [
        ("推荐", b.get("recommend"), "within"),
        ("限制", b.get("limit"), "out"),
        ("避免", b.get("avoid"), "alert"),
    ]
    groups = [(t, items, tone) for t, items, tone in groups if items]
    out: list[Prim] = []
    if v == "list":
        for t, items, _tone in groups:
            out += [SubHeading(t), Bullets(tuple(items))]
    elif groups:
        out.append(Columns(tuple(Column(t, tuple(items), tone) for t, items, tone in groups)))
    if b.get("swaps"):
        out.append(Table(("替换前", "替换为"), tuple((s["from"], s["to"]) for s in b["swaps"])))
    return out


_FITT = (("frequency", "频率"), ("intensity", "强度"), ("time", "时长"), ("type", "类型"))


def _exercise(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = []
    fitt = b.get("fitt") or {}
    cards = [Card(title=label, lines=(fitt[k],)) for k, label in _FITT if k in fitt]
    if cards:
        out.append(_grid(cards, 4))
    if b.get("schedule"):
        if v == "cards":
            out.append(
                _grid([Card(title=d["day"], lines=tuple(d["items"])) for d in b["schedule"]], 3)
            )
        else:
            out.append(
                Table(
                    ("日期", "安排"),
                    tuple((d["day"], "、".join(d["items"])) for d in b["schedule"]),
                )
            )
    if b.get("progression"):
        out.append(Paragraph(b["progression"], boxed=True))
    if b.get("cautions"):
        out.append(Callout("\n".join(b["cautions"]), title="运动注意", tone="warn"))
    return out


def _sleep(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = []
    rows = [
        (label, b[k]["bed"], b[k]["wake"])
        for k, label in (("current", "当前"), ("target", "目标"))
        if k in b
    ]
    if rows:
        if v == "list":
            dash = " – "  # noqa: RUF001
            pairs = tuple((f"{label}作息", f"{bed}{dash}{wake}") for label, bed, wake in rows)
            out.append(KeyValue(pairs, cols=2))
        else:
            out.append(TimeBars(tuple(rows)))
    if b.get("tips"):
        out.append(Bullets(tuple(b["tips"])))
    return out


def _stress(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = [Paragraph(b["status"])] if b.get("status") else []
    methods = b.get("methods") or []
    if methods and v == "list":
        out.append(
            Bullets(
                tuple(
                    _join(
                        m["name"],
                        _join(m.get("how", ""), m.get("frequency", ""), sep="，"),  # noqa: RUF001
                        sep="：",  # noqa: RUF001
                    )
                    for m in methods
                )
            )
        )
    elif methods:
        out.append(
            _grid(
                [
                    Card(
                        title=m["name"],
                        lines=tuple(x for x in (m.get("how", ""), m.get("frequency", "")) if x),
                    )
                    for m in methods
                ],
                3,
            )
        )
    return out


def _habits(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        return [
            _table(
                ["习惯", "现状", "目标", "方法"],
                [
                    [
                        i["name"],
                        i.get("current", ""),
                        i.get("target", ""),
                        i.get("how", ""),
                    ]
                    for i in items
                ],
            )
        ]
    cards = []
    for i in items:
        value = " → ".join(x for x in (i.get("current", ""), i.get("target", "")) if x)
        cards.append(Card(title=i["name"], lines=tuple(x for x in (value, i.get("how", "")) if x)))
    return [_grid(cards, 3)]


def _monitoring(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "cards":
        return [
            _grid(
                [
                    Card(
                        title=i["item"],
                        lines=tuple(x for x in (i.get("frequency", ""), i.get("timing", "")) if x),
                        tag=Tag(i["alert"], "out") if i.get("alert") else None,
                    )
                    for i in items
                ],
                3,
            )
        ]
    return [
        _table(
            ["项目", "频次", "时间点", "提醒阈值"],
            [
                [
                    i["item"],
                    i.get("frequency", ""),
                    i.get("timing", ""),
                    i.get("alert", ""),
                ]
                for i in items
            ],
            highlight="提醒阈值",
        )
    ]


def _shopping(b: dict, v: str) -> list[Prim]:
    if v == "list":
        out: list[Prim] = []
        for g in b["groups"]:
            out += [SubHeading(g["name"]), Bullets(tuple(g["items"]))]
        return out
    return [Columns(tuple(Column(g["name"], tuple(g["items"])) for g in b["groups"]))]


def _follow_up(b: dict, v: str) -> list[Prim]:
    pairs = tuple((i["label"], i["value"]) for i in b["items"])
    if v == "table":
        return [KeyValue(pairs, cols=1)]
    return [_grid([Card(title=k, lines=(val,)) for k, val in pairs], 4)]


def _summary(b: dict, v: str) -> list[Prim]:
    if v == "list":
        return [KeyValue(tuple((i["label"], i["text"]) for i in b["items"]), cols=1)]
    return [_grid([Card(title=i["label"], lines=(i["text"],)) for i in b["items"]], 3)]


def block_to_prims(block: dict, variant: str, path: str) -> list[Prim]:
    kind = block["kind"]
    if variant not in VARIANTS[kind]:
        raise RenderError(path, f"积木 {kind} 不支持版式 {variant}")
    if kind == "summary":
        return _summary(block, variant)
    if kind == "profile":
        return _profile(block, variant)
    if kind == "issues":
        return _issues(block, variant)
    if kind == "trend":
        return _trend(block, variant)
    if kind == "goals":
        return _goals(block, variant)
    if kind == "phases":
        return _phases(block, variant)
    if kind == "nutrition":
        return _nutrition(block, variant, path)
    if kind == "meal_plan":
        return _meal_plan(block, variant)
    if kind == "diet_rules":
        return _diet_rules(block, variant)
    if kind == "exercise":
        return _exercise(block, variant)
    if kind == "sleep":
        return _sleep(block, variant)
    if kind == "stress":
        return _stress(block, variant)
    if kind == "habits":
        return _habits(block, variant)
    if kind == "material":
        return [Media(block["name"], block["description"], block["url"], block.get("media_path"))]
    if kind == "monitoring":
        return _monitoring(block, variant)
    if kind == "referral":
        return [Callout(block["text"], tone="alert")]
    if kind == "shopping":
        return _shopping(block, variant)
    if kind == "follow_up":
        return _follow_up(block, variant)
    if kind == "paragraph":
        return [Paragraph(block["text"], boxed=variant == "boxed")]
    if kind == "bullets":
        return [Bullets(tuple(block["items"]), variant)]
    if kind == "table":
        return [Table(tuple(block["columns"]), tuple(tuple(r) for r in block["rows"]))]
    if kind == "kv":
        return [
            KeyValue(
                tuple((i["label"], i["value"]) for i in block["items"]),
                cols=2 if variant == "two-column" else 1,
            )
        ]
    if kind == "image":
        return [Image(block["path"], block.get("caption", ""))]
    if kind == "callout":
        return [
            Callout(
                block["text"],
                block.get("title", ""),
                "warn" if block.get("level") == "warn" else "info",
            )
        ]
    raise RenderError(path, f"未实现的积木类型 {kind}")


def section_prims(content: dict, style: dict) -> list[tuple[dict, list[tuple[Prim, str]]]]:
    out: list[tuple[dict, list[tuple[Prim, str]]]] = []
    for si, sec in enumerate(content["sections"]):
        items: list[tuple[Prim, str]] = []
        for bi, blk in enumerate(sec["blocks"], start=1):
            path = f"sections[{si}].blocks[{bi - 1}]"
            variant = variant_for(style, blk["kind"], sec["id"], blk.get("id"), bi)
            items += [(p, path) for p in block_to_prims(blk, variant, path)]
        out.append((sec, items))
    return out
