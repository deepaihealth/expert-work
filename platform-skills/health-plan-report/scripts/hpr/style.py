"""Style layers: merge high→low over platform defaults, normalise plain-language values,
lock brand keys, keep text readable, and account for every input (spec §5)."""

from __future__ import annotations

import colorsys
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpr.catalog import VARIANT_SYNONYMS, VARIANTS

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")

#: key -> (kind, default). kind: "color" | "background" | "bool" | tuple(enum values)
OPTIONS: dict[str, tuple[Any, Any]] = {
    "color.primary": ("color", "#0B4F5C"),
    "color.accent": ("color", "#E8A33D"),
    "color.background": ("background", "#FFFFFF"),
    "type.scale": (("compact", "standard", "large"), "standard"),
    "layout.density": (("compact", "standard", "airy"), "standard"),
    "brand.logo_position": (POSITIONS, "top-left"),
    "brand.org_position": (POSITIONS, "top-left"),
    "footer.align": (("left", "center"), "left"),
    "footer.page_number": ("bool", True),
    "cover.variant": (("band", "split", "minimal"), "band"),
    "toc": (("auto", "on", "off"), "auto"),
    "section.icons": ("bool", True),
    "output.formats": (("pptx", "pdf", "both"), "pptx"),
}

BRAND_LOCKED = frozenset(
    {"brand.org_name", "brand.logo_path", "brand.footer_signature", "brand.disclaimer"}
)
NOT_APPLIED = ("adjusted", "out_of_range", "brand_locked")
INK = "#1D2B30"

_POS_SYN = {
    "左上": "top-left",
    "左上角": "top-left",
    "右上": "top-right",
    "右上角": "top-right",
    "左下": "bottom-left",
    "左下角": "bottom-left",
    "右下": "bottom-right",
    "右下角": "bottom-right",
    "居中": "center",
    "中间": "center",
    "正中": "center",
    "中央": "center",
}
ENUM_SYN: dict[str, dict[str, str]] = {
    "type.scale": {
        "紧凑": "compact",
        "小": "compact",
        "小号": "compact",
        "标准": "standard",
        "默认": "standard",
        "正常": "standard",
        "大": "large",
        "大号": "large",
        "大字": "large",
        "老年": "large",
        "适合老年人": "large",
    },
    "layout.density": {
        "紧凑": "compact",
        "紧密": "compact",
        "标准": "standard",
        "默认": "standard",
        "宽松": "airy",
        "舒展": "airy",
        "留白多": "airy",
    },
    "brand.logo_position": _POS_SYN,
    "brand.org_position": _POS_SYN,
    "footer.align": {
        "左": "left",
        "靠左": "left",
        "左对齐": "left",
        "居中": "center",
        "中间": "center",
    },
    "cover.variant": {
        "色块": "band",
        "整版": "band",
        "整版色块": "band",
        "分栏": "split",
        "左右": "split",
        "左右分栏": "split",
        "极简": "minimal",
        "简洁": "minimal",
        "白底": "minimal",
    },
    "toc": {
        "自动": "auto",
        "要": "on",
        "开": "on",
        "显示": "on",
        "加目录": "on",
        "不要": "off",
        "关": "off",
        "隐藏": "off",
        "不要目录": "off",
    },
    "output.formats": {
        "ppt": "pptx",
        "幻灯片": "pptx",
        "演示文稿": "pptx",
        "都要": "both",
        "两个都要": "both",
        "两种": "both",
    },
}
ORDERED: dict[str, tuple[str, ...]] = {
    "type.scale": ("compact", "standard", "large"),
    "layout.density": ("compact", "standard", "airy"),
}
UP_WORDS = frozenset(
    {
        "大一点",
        "更大",
        "调大",
        "大些",
        "放大",
        "再大点",
        "宽松一点",
        "更宽松",
        "松一点",
        "up",
        "larger",
        "bigger",
    }
)
DOWN_WORDS = frozenset(
    {
        "小一点",
        "更小",
        "调小",
        "小些",
        "缩小",
        "再小点",
        "紧凑一点",
        "更紧凑",
        "紧一点",
        "down",
        "smaller",
    }
)
_BOOL_SYN = {
    "开": True,
    "打开": True,
    "显示": True,
    "要": True,
    "是": True,
    "on": True,
    "true": True,
    "yes": True,
    "关": False,
    "关闭": False,
    "隐藏": False,
    "不要": False,
    "否": False,
    "off": False,
    "false": False,
    "no": False,
}

COLOR_NAMES: dict[str, str] = {
    "深青": "#0B4F5C",
    "青色": "#0E7C86",
    "青绿": "#11807A",
    "蓝": "#1F5BD8",
    "蓝色": "#1F5BD8",
    "深蓝": "#1E3A8A",
    "藏青": "#1C2640",
    "海军蓝": "#1C2640",
    "天蓝": "#3B82C4",
    "浅蓝": "#DCEBFA",
    "绿": "#2E7D5B",
    "绿色": "#2E7D5B",
    "墨绿": "#1F4D3A",
    "深绿": "#1E5B45",
    "浅绿": "#E3F2E9",
    "紫": "#5B3F9E",
    "紫色": "#5B3F9E",
    "深紫": "#3F2A73",
    "浅紫": "#ECE6F7",
    "红": "#B42318",
    "红色": "#B42318",
    "酒红": "#7A1F2B",
    "橙": "#D97706",
    "橙色": "#D97706",
    "暖橙": "#E8A33D",
    "金": "#C9A45C",
    "金色": "#C9A45C",
    "暖金": "#D2BD8F",
    "灰": "#6B7280",
    "灰色": "#6B7280",
    "深灰": "#374151",
    "浅灰": "#F3F4F6",
    "米白": "#FAF7F0",
    "米色": "#F5EFE3",
    "白": "#FFFFFF",
    "白色": "#FFFFFF",
    "黑": "#111111",
    "黑色": "#111111",
    "粉": "#F4D6DC",
    "粉色": "#F4D6DC",
    "浅粉": "#FBEAEE",
    "棕": "#7C4A2D",
    "棕色": "#7C4A2D",
    "咖啡色": "#6F4E37",
}
_BG_KEYWORDS = {
    "white": "#FFFFFF",
    "白": "#FFFFFF",
    "白色": "#FFFFFF",
    "纯白": "#FFFFFF",
    "light-gray": "#F5F7F8",
    "浅灰": "#F5F7F8",
    "浅灰色": "#F5F7F8",
}
_TINT_WORDS = frozenset({"tint", "浅色调", "主色浅调", "主色浅色"})
_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HEX3 = re.compile(r"^#[0-9A-Fa-f]{3}$")
_RGB = re.compile(r"^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$", re.I)
_VARIANT_KEY = re.compile(r"^blocks\.([a-z_]+)\.variant$")
_SECTION_KEY = re.compile(r"^sections\.([a-z][a-z0-9_-]{0,31})(?:\.([a-z0-9_-]{1,32}))?\.variant$")


@dataclass(frozen=True)
class Layer:
    name: str
    values: dict[str, Any]


@dataclass
class Resolution:
    style: dict[str, Any]
    report: list[dict[str, Any]]


# ---------- colour maths ----------


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(c))) for c in (r, g, b)))


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex(*(x * (1 - t) + y * t for x, y in zip(ra, rb, strict=True)))


def luminance(hex_: str) -> float:
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def parse_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if _HEX6.match(v):
        return v.upper()
    if _HEX3.match(v):
        return ("#" + "".join(ch * 2 for ch in v[1:])).upper()
    m = _RGB.match(v)
    if m:
        parts = [int(x) for x in m.groups()]
        return _hex(*parts) if all(p <= 255 for p in parts) else None
    if v in COLOR_NAMES:
        return COLOR_NAMES[v]
    if v.endswith("色") and v[:-1] in COLOR_NAMES:
        return COLOR_NAMES[v[:-1]]
    return None


# ---------- layers ----------


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, full))
        else:
            out[full] = value
    return out


def load_layer(path: Path) -> Layer:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"样式文件 {path.name} 读取失败或不是合法 JSON：{exc}") from exc  # noqa: RUF001
    if not isinstance(data, dict):
        raise ValueError(f"样式文件 {path.name} 的顶层必须是 JSON 对象")
    return Layer(path.name, flatten(data))


# ---------- normalisation ----------


def _norm_enum(key: str, allowed: tuple[str, ...], raw: Any, below: Any) -> tuple[Any, str]:
    """Return (value, error_note). value None = rejected."""
    if not isinstance(raw, str):
        return None, "应为文字"
    v = raw.strip()
    if key in ORDERED and (v in UP_WORDS or v in DOWN_WORDS):
        order = ORDERED[key]
        i = order.index(below) + (1 if v in UP_WORDS else -1)
        if 0 <= i < len(order):
            return order[i], ""
        return None, "已经是最" + ("大" if v in UP_WORDS else "小") + "一档"
    if v.lower() in allowed:
        return v.lower(), ""
    syn = ENUM_SYN.get(key, {})
    if v in syn:
        return syn[v], ""
    if key == "output.formats" and v.lower() in ("ppt", "pdf"):
        return {"ppt": "pptx", "pdf": "pdf"}[v.lower()], ""
    return None, "可选值：" + " / ".join(allowed)  # noqa: RUF001


def _norm_bool(raw: Any) -> tuple[Any, str]:
    if isinstance(raw, bool):
        return raw, ""
    if isinstance(raw, str) and raw.strip().lower() in _BOOL_SYN:
        return _BOOL_SYN[raw.strip().lower()], ""
    return None, "应为 开 / 关"


def _norm_variant(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip()
    return VARIANT_SYNONYMS.get(v, v.lower())


def _section_kinds(content: dict | None, sid: str, bid: str | None) -> list[str] | None:
    if content is None:
        return None
    for sec in content.get("sections", []):
        if sec.get("id") != sid:
            continue
        blocks = sec.get("blocks", [])
        if bid is None:
            return [b.get("kind") for b in blocks]
        for i, blk in enumerate(blocks, start=1):
            if blk.get("id") == bid or str(i) == bid:
                return [blk.get("kind")]
        return []
    return []


def _normalise(
    key: str, raw: Any, current: dict[str, Any], content: dict | None
) -> tuple[Any, str]:
    if key in OPTIONS:
        kind, _default = OPTIONS[key]
        if kind == "color":
            c = parse_color(raw)
            return (c, "") if c else (None, "无法识别的颜色（可写 #RRGGBB 或常用中文色名）")  # noqa: RUF001
        if kind == "background":
            if isinstance(raw, str) and raw.strip() in _TINT_WORDS:
                return "tint", ""
            if isinstance(raw, str) and raw.strip().lower() in _BG_KEYWORDS:
                return _BG_KEYWORDS[raw.strip().lower()], ""
            c = parse_color(raw)
            return (c, "") if c else (None, "无法识别的背景色")
        if kind == "bool":
            return _norm_bool(raw)
        return _norm_enum(key, kind, raw, current[key])
    m = _VARIANT_KEY.match(key)
    if m:
        kind_name = m.group(1)
        v = _norm_variant(raw)
        if kind_name not in VARIANTS:
            return None, "未知积木类型"
        if v not in VARIANTS[kind_name]:
            return None, "可选版式：" + " / ".join(VARIANTS[kind_name])  # noqa: RUF001
        return v, ""
    m = _SECTION_KEY.match(key)
    if m:
        v = _norm_variant(raw)
        kinds = _section_kinds(content, m.group(1), m.group(2))
        if kinds is None:
            return v, ""
        if not kinds:
            return None, "内容里找不到这个章节或积木"
        if not any(v in VARIANTS.get(k, ()) for k in kinds):
            return None, "该处积木不支持这个版式"
        return v, ""
    return None, "未知可调项"


def _fix_background(bg: str, primary: str) -> tuple[str, str]:
    if bg == "tint":
        return mix(primary, "#FFFFFF", 0.94), ""
    if luminance(bg) >= 0.80:
        return bg, ""
    fixed = bg
    for step in range(1, 21):
        fixed = mix(bg, "#FFFFFF", step * 0.05)
        if luminance(fixed) >= 0.85:
            break
    return fixed, f"背景只支持浅色，已调浅为 {fixed}"  # noqa: RUF001


def darken_to(hex_: str, bg: str, ratio: float = 4.5) -> str:
    """Nearest shade of ``hex_`` with the same hue and HSL saturation (lower lightness only)
    whose contrast with ``bg`` is >= ratio. Every RGB channel of hls_to_rgb is non-decreasing in
    L, so contrast against a light background is monotone in L and bisection is valid; L = 0 is
    black, which passes on the light backgrounds _fix_background guarantees (contrast >= 17)."""
    h, light, s = colorsys.rgb_to_hls(*(c / 255 for c in _rgb(hex_)))

    def at(lv: float) -> str:
        return _hex(*(c * 255 for c in colorsys.hls_to_rgb(h, lv, s)))

    lo, hi = 0.0, light  # invariant: at(lo) passes, at(hi) fails
    for _ in range(20):
        mid = (lo + hi) / 2
        if contrast(at(mid), bg) >= ratio:
            lo = mid
        else:
            hi = mid
    return at(lo)  # the hex that was actually tested, so rounding cannot drop below ratio


def _fix_primary(primary: str, bg: str) -> tuple[str, str]:
    if contrast(primary, bg) >= 4.5:
        return primary, ""
    fixed = darken_to(primary, bg)
    return fixed, f"与背景对比不足，已加深为 {fixed}"  # noqa: RUF001


def resolve(layers: list[Layer], content: dict | None = None) -> Resolution:
    style: dict[str, Any] = {k: default for k, (_kind, default) in OPTIONS.items()}
    report: list[dict[str, Any]] = []
    final_entry: dict[str, dict[str, Any]] = {}
    for layer in reversed(layers):  # lowest first; higher layers overwrite
        for key, raw in layer.values.items():
            entry = {
                "key": key,
                "layer": layer.name,
                "input": raw,
                "value": None,
                "status": "applied",
                "note": "",
            }
            report.append(entry)
            if key in BRAND_LOCKED:
                entry.update(
                    status="brand_locked",
                    note="机构品牌项只取内容里的 brand，不可由样式覆盖",  # noqa: RUF001
                )
                continue
            value, note = _normalise(key, raw, style, content)
            if value is None:
                entry.update(status="out_of_range", note=note)
                continue
            entry["value"] = value
            style[key] = value
            if key in final_entry:
                final_entry[key]["status"] = "overridden"
                final_entry[key]["note"] = f"被更高一层「{layer.name}」覆盖"
            final_entry[key] = entry
    bg, bg_note = _fix_background(style["color.background"], style["color.primary"])
    style["color.background"] = bg
    if "color.background" in final_entry:
        if bg_note:
            final_entry["color.background"].update(status="adjusted", value=bg, note=bg_note)
        else:
            final_entry["color.background"]["value"] = bg
    primary, p_note = _fix_primary(style["color.primary"], bg)
    style["color.primary"] = primary
    if p_note and "color.primary" in final_entry:
        final_entry["color.primary"].update(status="adjusted", value=primary, note=p_note)
    return Resolution(style, report)


def variant_for(
    style: dict[str, Any], kind: str, section_id: str, block_id: str | None, index: int
) -> str:
    allowed = VARIANTS[kind]
    for key in (
        f"sections.{section_id}.{block_id}.variant" if block_id else None,
        f"sections.{section_id}.{index}.variant",
        f"sections.{section_id}.variant",
        f"blocks.{kind}.variant",
    ):
        if key and style.get(key) in allowed:
            return style[key]
    return allowed[0]
