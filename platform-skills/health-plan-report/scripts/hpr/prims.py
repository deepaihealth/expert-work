"""Layout primitives: the small vocabulary both writers (PPT, PDF) know how to draw."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tag:
    text: str
    tone: str  # within | out | alert | info | neutral


@dataclass(frozen=True)
class RangeBar:
    low: float
    high: float
    value: float


@dataclass(frozen=True)
class Card:
    title: str = ""
    value: str = ""
    unit: str = ""
    lines: tuple[str, ...] = ()
    tag: Tag | None = None
    bar: RangeBar | None = None
    badge: str = ""


@dataclass(frozen=True)
class CardGrid:
    cards: tuple[Card, ...]
    cols: int


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    highlight_col: int | None = None


@dataclass(frozen=True)
class KeyValue:
    pairs: tuple[tuple[str, str], ...]
    cols: int = 2


@dataclass(frozen=True)
class Bullets:
    items: tuple[str, ...]
    style: str = "dots"  # dots | numbers | checks


@dataclass(frozen=True)
class Paragraph:
    text: str
    boxed: bool = False


@dataclass(frozen=True)
class Callout:
    text: str
    title: str = ""
    tone: str = "info"  # info | warn | alert


@dataclass(frozen=True)
class Chart:
    kind: str  # line | bar | donut
    categories: tuple[str, ...]
    values: tuple[float, ...]
    unit: str = ""
    low: float | None = None
    high: float | None = None
    legend: tuple[str, ...] = ()  # donut: one text line per slice (drawn as text, not chart legend)
    center: str = ""  # donut: centre label


@dataclass(frozen=True)
class Step:
    label: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Timeline:
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Column:
    title: str
    items: tuple[str, ...]
    tone: str = "neutral"


@dataclass(frozen=True)
class Columns:
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Media:
    name: str
    description: str
    url: str
    media_path: str | None = None


@dataclass(frozen=True)
class Image:
    path: str
    caption: str = ""


@dataclass(frozen=True)
class TimeBars:
    rows: tuple[tuple[str, str, str], ...]  # (label, bed HH:MM, wake HH:MM)


@dataclass(frozen=True)
class SubHeading:
    text: str


Prim = (
    CardGrid
    | Table
    | KeyValue
    | Bullets
    | Paragraph
    | Callout
    | Chart
    | Timeline
    | Columns
    | Media
    | Image
    | TimeBars
    | SubHeading
)
