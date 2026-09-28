"""Content JSON: schema, validation and the inventory of texts that must appear (spec §4)."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpr.catalog import KINDS

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
URL_RE = re.compile(r"^https?://\S+$")


@dataclass(frozen=True)
class ContentError:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class F:
    spec: Any
    required: bool = True


def opt(spec: Any) -> F:
    return F(spec, required=False)


@dataclass(frozen=True)
class Enum:
    values: tuple[str, ...]


@dataclass(frozen=True)
class ListOf:
    item: Any
    min_items: int = 0


@dataclass(frozen=True)
class Obj:
    fields: dict[str, F]
    any_of: tuple[str, ...] = ()


TEXT = "text"
NUMTEXT = "numtext"
NUM = "num"
DATE = "date"
URL = "url"
PATH = "path"
HHMM = "hhmm"
IDENT = "ident"
BLOCK = "block"

_KV = Obj({"label": F(TEXT), "value": F(TEXT)})
_BEDWAKE = Obj({"bed": F(HHMM), "wake": F(HHMM)})

KIND_SCHEMAS: dict[str, Obj] = {
    "summary": Obj({"items": F(ListOf(Obj({"label": F(TEXT), "text": F(TEXT)}), 1))}),
    "profile": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "name": F(TEXT),
                            "value": F(NUMTEXT),
                            "unit": opt(TEXT),
                            "ref_low": opt(NUM),
                            "ref_high": opt(NUM),
                            "ref_text": opt(TEXT),
                            "position": opt(Enum(("within", "above", "below", "none"))),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "issues": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "title": F(TEXT),
                            "evidence": opt(TEXT),
                            "level": opt(Enum(("focus", "watch", "info"))),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "trend": Obj(
        {
            "metric": F(TEXT),
            "unit": opt(TEXT),
            "points": F(ListOf(Obj({"date": F(TEXT), "value": F(NUM)}), 2)),
            "target_low": opt(NUM),
            "target_high": opt(NUM),
            "ref_text": opt(TEXT),
        }
    ),
    "goals": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "name": F(TEXT),
                            "current": opt(NUMTEXT),
                            "target": F(NUMTEXT),
                            "unit": opt(TEXT),
                            "due": opt(TEXT),
                            "note": opt(TEXT),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "phases": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "label": F(TEXT),
                            "focus": F(ListOf(Obj({"area": F(TEXT), "text": F(TEXT)}), 1)),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "nutrition": Obj(
        {
            "energy_kcal": opt(NUMTEXT),
            "macros": opt(
                ListOf(Obj({"name": F(TEXT), "grams": opt(NUMTEXT), "percent": opt(NUMTEXT)}), 1)
            ),
            "meals": opt(
                ListOf(Obj({"name": F(TEXT), "percent": opt(NUMTEXT), "note": opt(TEXT)}), 1)
            ),
        },
        any_of=("energy_kcal", "macros", "meals"),
    ),
    "meal_plan": Obj(
        {
            "templates": F(
                ListOf(
                    Obj(
                        {
                            "name": opt(TEXT),
                            "applies_to": opt(TEXT),
                            "meals": F(
                                ListOf(
                                    Obj(
                                        {
                                            "name": F(TEXT),
                                            "time": opt(TEXT),
                                            "foods": F(
                                                ListOf(
                                                    Obj({"name": F(TEXT), "amount": opt(TEXT)}),
                                                    1,
                                                )
                                            ),
                                            "kcal": opt(NUMTEXT),
                                        }
                                    ),
                                    1,
                                )
                            ),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "diet_rules": Obj(
        {
            "recommend": opt(ListOf(TEXT, 1)),
            "limit": opt(ListOf(TEXT, 1)),
            "avoid": opt(ListOf(TEXT, 1)),
            "swaps": opt(ListOf(Obj({"from": F(TEXT), "to": F(TEXT)}), 1)),
        },
        any_of=("recommend", "limit", "avoid", "swaps"),
    ),
    "exercise": Obj(
        {
            "fitt": opt(
                Obj(
                    {
                        "frequency": opt(TEXT),
                        "intensity": opt(TEXT),
                        "time": opt(TEXT),
                        "type": opt(TEXT),
                    },
                    any_of=("frequency", "intensity", "time", "type"),
                )
            ),
            "schedule": opt(ListOf(Obj({"day": F(TEXT), "items": F(ListOf(TEXT, 1))}), 1)),
            "progression": opt(TEXT),
            "cautions": opt(ListOf(TEXT, 1)),
        },
        any_of=("fitt", "schedule", "progression", "cautions"),
    ),
    "sleep": Obj(
        {"current": opt(_BEDWAKE), "target": opt(_BEDWAKE), "tips": opt(ListOf(TEXT, 1))},
        any_of=("current", "target", "tips"),
    ),
    "stress": Obj(
        {
            "status": opt(TEXT),
            "methods": opt(
                ListOf(Obj({"name": F(TEXT), "how": opt(TEXT), "frequency": opt(TEXT)}), 1)
            ),
        },
        any_of=("status", "methods"),
    ),
    "habits": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "name": F(TEXT),
                            "current": opt(TEXT),
                            "target": opt(TEXT),
                            "how": opt(TEXT),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "material": Obj(
        {"name": F(TEXT), "description": F(TEXT), "url": F(URL), "media_path": opt(PATH)}
    ),
    "monitoring": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "item": F(TEXT),
                            "frequency": opt(TEXT),
                            "timing": opt(TEXT),
                            "alert": opt(TEXT),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "referral": Obj({"text": F(TEXT)}),
    "shopping": Obj({"groups": F(ListOf(Obj({"name": F(TEXT), "items": F(ListOf(TEXT, 1))}), 1))}),
    "follow_up": Obj({"items": F(ListOf(_KV, 1))}),
    "paragraph": Obj({"text": F(TEXT)}),
    "bullets": Obj({"items": F(ListOf(TEXT, 1))}),
    "table": Obj({"columns": F(ListOf(TEXT, 1)), "rows": F(ListOf(ListOf(TEXT, 1), 1))}),
    "kv": Obj({"items": F(ListOf(_KV, 1))}),
    "image": Obj({"path": F(PATH), "caption": opt(TEXT)}),
    "callout": Obj({"level": opt(Enum(("info", "warn"))), "title": opt(TEXT), "text": F(TEXT)}),
}

SECTION = Obj({"id": F(IDENT), "title": F(TEXT), "blocks": F(ListOf(BLOCK, 1))})

TOP = Obj(
    {
        "schema_version": F(Enum(("1",))),
        "title": F(TEXT),
        "subtitle": opt(TEXT),
        "period": opt(Obj({"label": F(TEXT), "weeks": opt(NUM)})),
        "generated_at": F(DATE),
        "data_basis": opt(TEXT),
        "client": F(
            Obj(
                {
                    "name": F(TEXT),
                    "facts": opt(ListOf(Obj({"label": F(TEXT), "value": F(TEXT)}))),
                }
            )
        ),
        "manager": opt(Obj({"name": F(TEXT), "title": opt(TEXT)})),
        "brand": opt(
            Obj(
                {
                    "org_name": opt(TEXT),
                    "logo_path": opt(PATH),
                    "footer_signature": opt(TEXT),
                    "disclaimer": opt(TEXT),
                }
            )
        ),
        "sections": F(ListOf(SECTION, 1)),
    }
)

_SCALAR_MSG = {
    TEXT: "应为非空文字",
    NUMTEXT: "应为数字或非空文字",
    NUM: "应为数字",
    DATE: "应为 YYYY-MM-DD 日期",
    URL: "应为 http(s) 链接",
    PATH: "应为非空文件路径",
    HHMM: "应为 HH:MM 时间",
    IDENT: "应为小写字母开头、只含小写字母/数字/-/_ 的短标识（≤32）",  # noqa: RUF001
}


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _scalar_ok(value: Any, spec: str) -> bool:
    if spec == TEXT or spec == PATH:
        return _nonempty_str(value)
    if spec == NUMTEXT:
        return _is_num(value) or _nonempty_str(value)
    if spec == NUM:
        return _is_num(value)
    if spec == DATE:
        return isinstance(value, str) and bool(DATE_RE.match(value))
    if spec == URL:
        return isinstance(value, str) and bool(URL_RE.match(value))
    if spec == HHMM:
        return isinstance(value, str) and bool(HHMM_RE.match(value))
    if spec == IDENT:
        return isinstance(value, str) and bool(ID_RE.match(value))
    raise AssertionError(f"unknown scalar spec {spec!r}")


def _check(value: Any, spec: Any, path: str, errs: list[ContentError]) -> None:
    if isinstance(spec, Obj):
        if not isinstance(value, dict):
            errs.append(ContentError(path, "应为对象"))
            return
        for key in value:
            if key not in spec.fields:
                errs.append(ContentError(_join(path, key), "不支持的字段"))
        for key, field in spec.fields.items():
            if key in value:
                _check(value[key], field.spec, _join(path, key), errs)
            elif field.required:
                errs.append(ContentError(_join(path, key), "缺少必填字段"))
        if spec.any_of and not any(k in value for k in spec.any_of):
            errs.append(
                ContentError(path, "至少需要以下字段之一：" + "、".join(spec.any_of))  # noqa: RUF001
            )
        return
    if isinstance(spec, ListOf):
        if not isinstance(value, list):
            errs.append(ContentError(path, "应为数组"))
            return
        if len(value) < spec.min_items:
            errs.append(ContentError(path, f"至少需要 {spec.min_items} 项"))
        for i, item in enumerate(value):
            _check(item, spec.item, f"{path}[{i}]", errs)
        return
    if isinstance(spec, Enum):
        if value not in spec.values:
            errs.append(ContentError(path, "取值应为：" + " / ".join(spec.values)))  # noqa: RUF001
        return
    if spec == BLOCK:
        _check_block(value, path, errs)
        return
    if not _scalar_ok(value, spec):
        errs.append(ContentError(path, _SCALAR_MSG[spec]))


def _check_block(block: Any, path: str, errs: list[ContentError]) -> None:
    if not isinstance(block, dict):
        errs.append(ContentError(path, "应为对象"))
        return
    kind = block.get("kind")
    if kind not in KINDS:
        errs.append(ContentError(_join(path, "kind"), "未知积木类型：" + str(kind)))  # noqa: RUF001
        return
    if "id" in block and not _scalar_ok(block["id"], IDENT):
        errs.append(ContentError(_join(path, "id"), _SCALAR_MSG[IDENT]))
    body = {k: v for k, v in block.items() if k not in ("kind", "id")}
    _check(body, KIND_SCHEMAS[kind], path, errs)
    if kind == "table" and isinstance(block.get("columns"), list):
        width = len(block["columns"])
        for i, row in enumerate(block.get("rows") or []):
            if isinstance(row, list) and len(row) != width:
                errs.append(
                    ContentError(
                        f"{path}.rows[{i}]",
                        f"应有 {width} 列，实际 {len(row)} 列",  # noqa: RUF001
                    )
                )


def _check_ids(content: dict, errs: list[ContentError]) -> None:
    seen: set[str] = set()
    for si, sec in enumerate(content.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        sid = sec.get("id")
        if isinstance(sid, str) and sid in seen:
            errs.append(ContentError(f"sections[{si}].id", f"章节 id 重复：{sid}"))  # noqa: RUF001
        if isinstance(sid, str):
            seen.add(sid)
        bseen: set[str] = set()
        for bi, blk in enumerate(sec.get("blocks") or []):
            bid = blk.get("id") if isinstance(blk, dict) else None
            if isinstance(bid, str) and bid in bseen:
                errs.append(
                    ContentError(
                        f"sections[{si}].blocks[{bi}].id",
                        f"积木 id 重复：{bid}",  # noqa: RUF001
                    )
                )
            if isinstance(bid, str):
                bseen.add(bid)


def validate_content(content: Any) -> list[ContentError]:
    errs: list[ContentError] = []
    _check(content, TOP, "", errs)
    if isinstance(content, dict):
        _check_ids(content, errs)
    return errs


def load_content(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"内容文件读取失败或不是合法 JSON：{exc}") from exc  # noqa: RUF001
    if not isinstance(data, dict):
        raise ValueError("内容文件的顶层必须是 JSON 对象")
    return data


_NON_TEXT_KEYS = frozenset(
    {"schema_version", "kind", "id", "url", "media_path", "logo_path", "path", "position", "level"}
)


def _phase_areas_once(block: dict) -> dict:
    """A phases block's ``area`` names are grouping labels: the table variant shows each one once
    as a column header, so each distinct area is required once per block (texts all count)."""
    areas = list(dict.fromkeys(f["area"] for it in block["items"] for f in it["focus"]))
    items = [{**it, "focus": [{"text": f["text"]} for f in it["focus"]]} for it in block["items"]]
    return {**block, "items": items, "areas": areas}


def required_texts(content: dict) -> list[str]:
    """Every string the reader must find in the deliverable, once per occurrence (numbers and
    chart points excluded)."""
    out: list[str] = []

    def walk(value: Any, in_points: bool) -> None:
        if isinstance(value, dict) and value.get("kind") == "phases":
            value = _phase_areas_once(value)
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in _NON_TEXT_KEYS:
                    walk(item, in_points or key == "points")
        elif isinstance(value, list):
            for item in value:
                walk(item, in_points)
        elif isinstance(value, str) and not in_points and value.strip():
            out.append(value)

    walk(content, False)
    return out


_PATH_KEYS = frozenset({"path", "logo_path", "media_path"})
_C0_BUT_TAB_LF = re.compile(r"[\x00-\x08\x0b-\x1f]")


def normalize_text(s: str) -> str:
    """Presentation-level whitespace only (spec §4.4): CRLF / CR and the line / paragraph
    separators U+2028 / U+2029 become LF, every other C0 control except LF and TAB becomes a
    space. python-pptx would store C0 as literal ``_x000D_`` escapes and weasyprint garbles the
    text layer, so both writers see this form."""
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    return _C0_BUT_TAB_LF.sub(" ", s)


def normalize_content(content: dict) -> dict:
    """A new content dict with every display string normalised; file paths are left as given
    (they name files, they are not text the reader sees)."""

    def walk(value: Any, key: str | None) -> Any:
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, str) and key not in _PATH_KEYS:
            return normalize_text(value)
        return value

    return walk(content, None)


def iter_blocks(content: dict) -> Iterator[tuple[int, str, int, dict]]:
    for si, sec in enumerate(content["sections"]):
        for bi, blk in enumerate(sec["blocks"], start=1):
            yield si, sec["id"], bi, blk
