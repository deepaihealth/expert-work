"""B-267 — a fixed-result backend for the builtin ``web_search`` tool.

Evaluations that compare two versions need the web to answer the same way on
both runs (the B-267 baseline lost its web search mid-run: 183 of 198 searches
came back empty). This backend answers from a JSON file the platform holds,
never from the network.

- Only where the platform configures a fixture directory
  (``ToolEnv.web_search_fixtures_dir`` ← ``EXPERT_WORK_WEB_SEARCH_FIXTURES_DIR``,
  test environment only). A manifest picks a file with ``config.fixture: <name>``
  on its ``web_search`` entry; ``<name>.json`` must be a plain file name inside
  that directory. Without the directory, or without the file, the build fails —
  it never silently falls back to real search. Tenants cannot write fixtures.
- The tool is the real :class:`~orchestrator.tools.web_search.WebSearchTool` over
  this client: same name, description and result rendering, so the model cannot
  tell. Which entry answered rides in ``ToolResult.meta`` (trace / event stream,
  never the model) and in one log line per query, for scoring.

File format::

    {"entries": [
        {"id": "dew-point",
         "match": [["露点"], ["dew", "point"]],
         "results": [{"title": "...", "url": "...", "content": "..."}]}
    ]}

Matching is deterministic and tolerant of wording: the query and every keyword
are normalised (NFKC, case-folded, everything but letters and digits dropped);
a ``match`` list hits when all its keywords occur in the query; an entry scores
the length of its longest hitting list; the highest score wins, ties go to file
order. No hit → no results (the model reads ``(no results)``, exactly as an
empty real search) and ``fixture_miss`` in the meta and the log.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

#: A plain file name: no separators, no leading dot, no extension.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)
_SUFFIX = ".json"


class WebSearchFixtureError(ValueError):
    """The fixture a manifest picked cannot be used (the build fails)."""


def normalize(text: str) -> str:
    """``text`` as the matcher compares it: NFKC, case-folded, letters and
    digits only (full-width forms, spaces and punctuation do not matter)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    content: str


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    match: list[list[str]] = Field(min_length=1)
    results: list[_Result]

    @field_validator("match")
    @classmethod
    def _keywords_present(cls, value: list[list[str]]) -> list[list[str]]:
        for keywords in value:
            if not keywords or not all(normalize(k) for k in keywords):
                msg = "every match list needs keywords with letters or digits"
                raise ValueError(msg)
        return value


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[_Entry]

    @field_validator("entries")
    @classmethod
    def _unique_ids(cls, value: list[_Entry]) -> list[_Entry]:
        ids = [entry.id for entry in value]
        if len(set(ids)) != len(ids):
            msg = "entry ids must be unique"
            raise ValueError(msg)
        return value


@dataclass(frozen=True)
class FixtureEntry:
    """One entry: its id, its keyword lists (normalised) and its results."""

    id: str
    keyword_lists: tuple[tuple[str, ...], ...]
    results: tuple[Mapping[str, str], ...]

    def score(self, normalized_query: str) -> int:
        """Keywords in the longest list that fully occurs in the query (0: none)."""
        return max(
            (
                len(keywords)
                for keywords in self.keyword_lists
                if all(k in normalized_query for k in keywords)
            ),
            default=0,
        )


def match_entry(entries: Sequence[FixtureEntry], query: str) -> FixtureEntry | None:
    """The entry answering ``query``: highest score, first in file order on a tie."""
    normalized = normalize(query)
    best: FixtureEntry | None = None
    best_score = 0
    for entry in entries:
        score = entry.score(normalized)
        if score > best_score:
            best, best_score = entry, score
    return best


@dataclass(frozen=True)
class FixtureWebSearchClient:
    """A :class:`~orchestrator.tools.web_search.TavilyClient` answering from a
    fixture file. ``search`` adds a ``fixture`` mapping to the body, which
    ``WebSearchTool`` copies into the result's meta (never into its text)."""

    name: str
    entries: tuple[FixtureEntry, ...]

    async def search(
        self, *, query: str, max_results: int, tenant_id: UUID | None = None
    ) -> Mapping[str, Any]:
        del tenant_id  # fixtures are platform data, the same for every tenant
        entry = match_entry(self.entries, query)
        logger.info(
            "web_search.fixture fixture=%s entry=%s miss=%s query=%s",
            self.name,
            entry.id if entry is not None else "-",
            "false" if entry is not None else "true",
            json.dumps(query, ensure_ascii=False),
        )
        results = [dict(r) for r in entry.results[:max_results]] if entry is not None else []
        return {
            "results": results,
            "fixture": {
                "fixture": self.name,
                "fixture_entry": entry.id if entry is not None else None,
                "fixture_miss": entry is None,
            },
        }


def load_web_search_fixture(fixtures_dir: str | None, name: object) -> FixtureWebSearchClient:
    """The client for fixture ``name`` in ``fixtures_dir``.

    :raises WebSearchFixtureError: no directory configured, ``name`` is not a
        plain file name, the file is missing (or resolves outside the
        directory) or does not parse as a fixture.
    """
    if fixtures_dir is None:
        msg = "web_search 'fixture' is not available in this deployment (no fixtures configured)"
        raise WebSearchFixtureError(msg)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        msg = (
            "web_search fixture name must be 1-64 letters, digits, '_' or '-', "
            "starting with a letter or digit"
        )
        raise WebSearchFixtureError(msg)
    root = Path(fixtures_dir).resolve()
    path = (root / f"{name}{_SUFFIX}").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        msg = f"web_search fixture {name!r} not found"
        raise WebSearchFixtureError(msg)
    return FixtureWebSearchClient(name=name, entries=_parse(name, path))


def _parse(name: str, path: Path) -> tuple[FixtureEntry, ...]:
    try:
        parsed = _File.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "file"
        msg = f"web_search fixture {name!r} is invalid: {where}: {first['msg']}"
        raise WebSearchFixtureError(msg) from exc
    return tuple(
        FixtureEntry(
            id=entry.id,
            keyword_lists=tuple(tuple(normalize(k) for k in kws) for kws in entry.match),
            results=tuple(result.model_dump() for result in entry.results),
        )
        for entry in parsed.entries
    )
