"""Block kinds and their selectable layout variants (first variant = default)."""

from __future__ import annotations

VARIANTS: dict[str, tuple[str, ...]] = {
    "summary": ("columns", "list"),
    "profile": ("cards", "table"),
    "issues": ("cards", "list"),
    "trend": ("line", "bar"),
    "goals": ("cards", "table"),
    "phases": ("timeline", "columns", "table"),
    "nutrition": ("donut", "table"),
    "meal_plan": ("timeline", "table", "cards"),
    "diet_rules": ("columns", "list"),
    "exercise": ("fitt", "cards"),
    "sleep": ("timebar", "list"),
    "stress": ("cards", "list"),
    "habits": ("cards", "table"),
    "material": ("card",),
    "monitoring": ("table", "cards"),
    "referral": ("box",),
    "shopping": ("columns", "list"),
    "follow_up": ("cards", "table"),
    "paragraph": ("plain", "boxed"),
    "bullets": ("dots", "numbers", "checks"),
    "table": ("table",),
    "kv": ("two-column", "one-column"),
    "image": ("fit",),
    "callout": ("box",),
}

KINDS: tuple[str, ...] = tuple(VARIANTS)

#: 用户 / Agent 常用说法 → 版式名。只收录不会跨积木歧义的词。
VARIANT_SYNONYMS: dict[str, str] = {
    "卡片": "cards",
    "按天卡片": "cards",
    "表格": "table",
    "列表": "list",
    "清单": "list",
    "时间轴": "timeline",
    "时间线": "timeline",
    "分栏": "columns",
    "多栏": "columns",
    "环形图": "donut",
    "饼图": "donut",
    "折线": "line",
    "折线图": "line",
    "柱状": "bar",
    "柱状图": "bar",
    "四格": "fitt",
    "作息条": "timebar",
    "圆点": "dots",
    "编号": "numbers",
    "打勾": "checks",
    "双栏": "two-column",
    "单栏": "one-column",
    "带底色": "boxed",
    "无底色": "plain",
}
