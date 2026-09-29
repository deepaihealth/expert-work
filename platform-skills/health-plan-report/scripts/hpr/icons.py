"""Section icons as polylines on a 24×24 grid (drawn as PPT freeforms and PDF SVG)."""  # noqa: RUF002

from __future__ import annotations

import math

Polyline = list[tuple[float, float]]


def _circle(cx: float, cy: float, r: float, n: int = 16) -> Polyline:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n + 1)
    ]


ICONS: dict[str, list[Polyline]] = {
    "pulse": [[(2, 13), (7, 13), (9, 7), (13, 18), (15, 11), (22, 11)]],
    "chart": [[(4, 4), (4, 20), (21, 20)], [(7, 15), (11, 11), (14, 13), (20, 6)]],
    "bowl": [
        [(3, 11), (21, 11), (19, 17), (15, 20), (9, 20), (5, 17), (3, 11)],
        [(9, 7), (10, 4)],
        [(14, 7), (15, 4)],
    ],
    "run": [
        [(12, 3), (15, 3), (15, 6), (12, 6), (12, 3)],
        [(9, 10), (14, 8), (16, 12), (19, 13)],
        [(14, 8), (12, 14), (15, 17), (15, 21)],
        [(12, 14), (8, 17), (5, 17)],
    ],
    "moon": [
        [
            (15, 3),
            (10, 5),
            (7, 9),
            (7, 15),
            (10, 19),
            (15, 21),
            (19, 19),
            (14, 18),
            (11, 15),
            (11, 9),
            (14, 5),
            (15, 3),
        ]
    ],
    "leaf": [
        [(5, 19), (5, 11), (10, 6), (19, 5), (18, 14), (13, 19), (5, 19)],
        [(5, 19), (14, 10)],
    ],
    "repeat": [
        [(4, 10), (4, 7), (18, 7)],
        [(15, 4), (18, 7), (15, 10)],
        [(20, 14), (20, 17), (6, 17)],
        [(9, 14), (6, 17), (9, 20)],
    ],
    "clipboard": [
        [(6, 5), (18, 5), (18, 21), (6, 21), (6, 5)],
        [(9, 3), (15, 3), (15, 7), (9, 7), (9, 3)],
        [(9, 12), (15, 12)],
        [(9, 16), (15, 16)],
    ],
    "alert": [[(12, 3), (22, 20), (2, 20), (12, 3)], [(12, 9), (12, 14)], [(12, 17), (12, 17.6)]],
    "bag": [
        [(5, 8), (19, 8), (18, 21), (6, 21), (5, 8)],
        [(9, 8), (9, 6), (12, 3), (15, 6), (15, 8)],
    ],
    "calendar": [
        [(4, 6), (20, 6), (20, 21), (4, 21), (4, 6)],
        [(4, 10), (20, 10)],
        [(8, 3), (8, 7)],
        [(16, 3), (16, 7)],
    ],
    "target": [_circle(12, 12, 9), _circle(12, 12, 5), [(12, 11.5), (12, 12.5)]],
    "flag": [[(5, 21), (5, 4)], [(5, 4), (17, 4), (14, 8), (17, 12), (5, 12)]],
    "search": [_circle(10, 10, 6), [(14.5, 14.5), (21, 21)]],
    "star": [
        [
            (12, 3),
            (14.6, 9),
            (21, 9.3),
            (16, 13.3),
            (17.8, 20),
            (12, 16.2),
            (6.2, 20),
            (8, 13.3),
            (3, 9.3),
            (9.4, 9),
            (12, 3),
        ]
    ],
    "play": [[(6, 4), (19, 12), (6, 20), (6, 4)]],
    "dot": [_circle(12, 12, 4)],
}

_KIND_ICON = {
    "summary": "star",
    "profile": "pulse",
    "issues": "search",
    "trend": "chart",
    "goals": "target",
    "phases": "flag",
    "nutrition": "bowl",
    "meal_plan": "bowl",
    "diet_rules": "bowl",
    "exercise": "run",
    "sleep": "moon",
    "stress": "leaf",
    "habits": "repeat",
    "material": "play",
    "monitoring": "clipboard",
    "referral": "alert",
    "shopping": "bag",
    "follow_up": "calendar",
}


def icon_for_section(section: dict) -> str:
    for blk in section["blocks"]:
        if blk["kind"] in _KIND_ICON:
            return _KIND_ICON[blk["kind"]]
    return "dot"
