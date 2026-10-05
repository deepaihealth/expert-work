"""B-140 判据 —— 纯函数:输入用例、一次运行的记录、取回的文件,输出逐条判定。

判据只认行为的结果(结束方式、产物、文件内容、工具调用形状),不认措辞。
"""

from __future__ import annotations

import difflib
import fnmatch
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

from behavior_extract import UnsupportedDocumentError, document_text
from behavior_schema import (
    ArtifactContains,
    ArtifactExists,
    ArtifactNotContains,
    ArtifactUnchangedLines,
    Case,
    Check,
    CheckVerdict,
    Completed,
    ExitReason,
    FinalTextContains,
    FinalTextNotContains,
    FinalTextRegex,
    RunRecord,
    ToolCall,
    ToolCountMax,
    ToolNotUsed,
    ToolNotUsedOn,
    ToolUsed,
    TurnTokensMax,
    WorkspaceFileContains,
    WorkspaceFileUnchangedLines,
)

FileKey = tuple[Literal["artifact", "workspace"], str]
FetchedFile = tuple[str, bytes]  # (实际文件名, 内容)

_UNREADABLE = (UnsupportedDocumentError, zipfile.BadZipFile, KeyError, ET.ParseError)


def required_files(case: Case) -> list[FileKey]:
    """判据要读的文件,按首次出现的顺序去重。"""
    keys: list[FileKey] = []
    for check in case.checks:
        key: FileKey
        if isinstance(check, ArtifactContains | ArtifactNotContains | ArtifactUnchangedLines):
            key = ("artifact", check.name)
        elif isinstance(check, WorkspaceFileContains | WorkspaceFileUnchangedLines):
            key = ("workspace", check.path)
        else:
            continue
        if key not in keys:
            keys.append(key)
    return keys


def normalize_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    for prefix in ("/workspace/", "workspace/", "./"):
        if p.startswith(prefix):
            p = p[len(prefix) :]
    return p.lstrip("/")


def path_matches(candidate: str, target: str) -> bool:
    c, t = normalize_path(candidate), normalize_path(target)
    return c == t or c.endswith("/" + t)


def changed_lines(before: str, after: str) -> set[int]:
    """开局文件里被改动的行号(1 起)。插入算在插入点前一行(开头插入算第 1 行)。"""
    a, b = before.splitlines(), after.splitlines()
    out: set[int] = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 < i2:
            out.update(range(i1 + 1, i2 + 1))
        else:
            out.add(max(i1, 1))
    return out


def evaluate(
    case: Case,
    record: RunRecord,
    files: Mapping[FileKey, FetchedFile | None],
    fixtures_dir: Path,
) -> list[CheckVerdict]:
    return [_one(check, record, files, fixtures_dir) for check in case.checks]


def _verdict(check: Check, ok: bool, detail: str) -> CheckVerdict:
    return CheckVerdict(type=check.type, passed=ok, detail="" if ok else detail)


def _text(files: Mapping[FileKey, FetchedFile | None], key: FileKey) -> tuple[str | None, str]:
    got = files.get(key)
    if got is None:
        return None, f"{key[0]} {key[1]!r} not found"
    name, data = got
    try:
        return document_text(name, data), ""
    except _UNREADABLE as exc:
        return None, f"{name!r} unreadable: {exc}"


def _calls(record: RunRecord) -> list[ToolCall]:
    return [call for turn in record.turns for call in turn.tool_calls]


# 一个判据一个分支,与规格 §6 的表逐行对照。
def _one(
    check: Check,
    record: RunRecord,
    files: Mapping[FileKey, FetchedFile | None],
    fixtures_dir: Path,
) -> CheckVerdict:
    turns = record.turns
    final = turns[-1].final_text if turns else ""
    calls = _calls(record)

    if isinstance(check, Completed):
        bad = [
            t.index
            for t in turns
            if not (
                t.status == "success" and t.completed is True and t.exit_reason == "text_response"
            )
        ]
        return _verdict(
            check, bool(turns) and not bad, f"turns not completed: {bad}" if turns else "no turns"
        )
    if isinstance(check, ExitReason):
        got = turns[-1].exit_reason if turns else None
        return _verdict(check, got == check.value, f"exit_reason={got!r}")
    if isinstance(check, FinalTextContains):
        missing = [s for s in check.all if s not in final]
        return _verdict(check, not missing, f"missing {missing}")
    if isinstance(check, FinalTextNotContains):
        found = [s for s in check.any if s in final]
        return _verdict(check, not found, f"found {found}")
    if isinstance(check, FinalTextRegex):
        return _verdict(
            check, re.search(check.pattern, final) is not None, f"no match for {check.pattern!r}"
        )
    if isinstance(check, ArtifactExists):
        names = [
            str(a["name"])
            for t in turns
            for a in t.artifacts
            if isinstance(a, dict) and a.get("name")
        ]
        return _verdict(
            check, any(fnmatch.fnmatchcase(n, check.name) for n in names), f"artifacts={names}"
        )
    if isinstance(check, ArtifactContains | WorkspaceFileContains):
        key: FileKey = (
            ("artifact", check.name)
            if isinstance(check, ArtifactContains)
            else ("workspace", check.path)
        )
        text, problem = _text(files, key)
        if text is None:
            return _verdict(check, False, problem)
        missing = [s for s in check.all if s not in text]
        return _verdict(check, not missing, f"missing {missing}")
    if isinstance(check, ArtifactNotContains):
        text, problem = _text(files, ("artifact", check.name))
        if text is None:
            return _verdict(check, False, problem)
        found = [s for s in check.any if s in text]
        return _verdict(check, not found, f"found {found}")
    if isinstance(check, ArtifactUnchangedLines | WorkspaceFileUnchangedLines):
        key = (
            ("artifact", check.name)
            if isinstance(check, ArtifactUnchangedLines)
            else ("workspace", check.path)
        )
        text, problem = _text(files, key)
        if text is None:
            return _verdict(check, False, problem)
        before = (fixtures_dir / check.fixture).read_text(encoding="utf-8")
        extra = sorted(changed_lines(before, text) - set(check.except_lines))
        return _verdict(check, not extra, f"unexpected changes at fixture lines {extra[:20]}")
    if isinstance(check, ToolUsed):
        return _verdict(
            check,
            any(c.name == check.tool for c in calls),
            f"tools used: {sorted({c.name for c in calls})}",
        )
    if isinstance(check, ToolNotUsed):
        n = sum(c.name == check.tool for c in calls)
        return _verdict(check, n == 0, f"{check.tool} called {n} times")
    if isinstance(check, ToolCountMax):
        n = sum(1 for c in calls if check.tool is None or c.name == check.tool)
        return _verdict(check, n <= check.max, f"{n} calls > {check.max}")
    if isinstance(check, ToolNotUsedOn):
        hits = [
            c.turn
            for c in calls
            if c.name == check.tool and path_matches(str(c.args.get("path", "")), check.path)
        ]
        return _verdict(check, not hits, f"{check.tool} on {check.path!r} in turns {hits}")
    if isinstance(check, TurnTokensMax):
        if check.turn > len(turns):
            return _verdict(check, False, f"only {len(turns)} turns")
        got = turns[check.turn - 1].input_tokens
        if got is None:
            return _verdict(check, False, "no usage recorded")
        return _verdict(check, got <= check.max_input_tokens, f"input_tokens={got}")
    raise AssertionError(f"unhandled check {check!r}")
