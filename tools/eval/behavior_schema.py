"""B-140 行为回归评测 —— 数据模型:用例、一次运行的记录、判定结果。

见 ``docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md``。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DATASET_DIR = Path(__file__).parent / "datasets" / "behavior"
CASES_DIR = DATASET_DIR / "cases"
FIXTURES_DIR = DATASET_DIR / "fixtures"
AGENTS_DIR = DATASET_DIR / "agents"

AgentCode = Literal["eval-general", "eval-ahp", "eval-compress"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- 判据(规格 §6)-------------------------------------------------------


class Completed(_Strict):
    type: Literal["completed"]


class ExitReason(_Strict):
    type: Literal["exit_reason"]
    value: str


class FinalTextContains(_Strict):
    type: Literal["final_text_contains"]
    all: list[str] = Field(min_length=1)


class FinalTextNotContains(_Strict):
    type: Literal["final_text_not_contains"]
    any: list[str] = Field(min_length=1)


def _regex(value: str) -> str:
    # 加载时就报错,别等智能体跑完才在判分时炸掉整批。
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regex {value!r}: {exc}") from exc
    return value


class FinalTextRegex(_Strict):
    type: Literal["final_text_regex"]
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        return _regex(value)


class ArtifactExists(_Strict):
    type: Literal["artifact_exists"]
    name: str = Field(min_length=1)  # fnmatch 通配


_NO_TEXT_SUFFIXES = (".pdf", ".xlsx")


def _text_readable(name: str) -> str:
    # 本版取不出 pdf / xlsx 的文本:放进来两边都 0/3,对照永远「持平」,会藏住退步。
    if name.lower().endswith(_NO_TEXT_SUFFIXES):
        raise ValueError(
            f"no text extractor for {name!r}; check its existence with artifact_exists"
        )
    return name


class ArtifactContains(_Strict):
    type: Literal["artifact_contains"]
    name: str = Field(min_length=1)
    all: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _readable(cls, value: str) -> str:
        return _text_readable(value)


class ArtifactNotContains(_Strict):
    type: Literal["artifact_not_contains"]
    name: str = Field(min_length=1)
    any: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _readable(cls, value: str) -> str:
        return _text_readable(value)


class ArtifactUnchangedLines(_Strict):
    type: Literal["artifact_unchanged_lines"]
    name: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    except_lines: list[int] = Field(default_factory=list)


class WorkspaceFileContains(_Strict):
    type: Literal["workspace_file_contains"]
    path: str = Field(min_length=1)
    all: list[str] = Field(min_length=1)


class WorkspaceFileNotContains(_Strict):
    type: Literal["workspace_file_not_contains"]
    path: str = Field(min_length=1)
    any: list[str] = Field(min_length=1)


class WorkspaceFileUnchangedLines(_Strict):
    type: Literal["workspace_file_unchanged_lines"]
    path: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    except_lines: list[int] = Field(default_factory=list)


class WorkspaceFileLineCount(_Strict):
    """工作区文件里匹配 ``pattern``(逐行 ``re.search``)的行数恰好是 ``count``。

    B-141:抓「重做一步」(同一行出现两次)和「漏一步」(该有的行没有)。
    """

    type: Literal["workspace_file_line_count"]
    path: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    count: int = Field(ge=0)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        return _regex(value)


class ToolUsed(_Strict):
    type: Literal["tool_used"]
    tool: str = Field(min_length=1)
    turn: int | None = Field(default=None, ge=1)  # None = 全部轮次


class ToolNotUsed(_Strict):
    type: Literal["tool_not_used"]
    tool: str = Field(min_length=1)
    turn: int | None = Field(default=None, ge=1)  # None = 全部轮次


class ToolCountMax(_Strict):
    type: Literal["tool_count_max"]
    max: int = Field(ge=0)
    tool: str | None = None  # None = 全部工具
    turn: int | None = Field(default=None, ge=1)  # None = 全部轮次


class ToolNotUsedOn(_Strict):
    type: Literal["tool_not_used_on"]
    tool: str = Field(min_length=1)
    path: str = Field(min_length=1)
    turn: int | None = Field(default=None, ge=1)  # None = 全部轮次


class TurnTokensMax(_Strict):
    type: Literal["turn_tokens_max"]
    turn: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)


Check = Annotated[
    Completed
    | ExitReason
    | FinalTextContains
    | FinalTextNotContains
    | FinalTextRegex
    | ArtifactExists
    | ArtifactContains
    | ArtifactNotContains
    | ArtifactUnchangedLines
    | WorkspaceFileContains
    | WorkspaceFileNotContains
    | WorkspaceFileUnchangedLines
    | WorkspaceFileLineCount
    | ToolUsed
    | ToolNotUsed
    | ToolCountMax
    | ToolNotUsedOn
    | TurnTokensMax,
    Field(discriminator="type"),
]


# --- 用例 -------------------------------------------------------------------


class Turn(_Strict):
    prompt: str = Field(min_length=1)


class Case(_Strict):
    id: str = Field(pattern=r"^[ghc][0-9]{2}-[a-z0-9-]+$")
    agent: AgentCode
    shape: str = Field(min_length=1)  # 出处:ROADMAP 编号
    fixtures: list[str] = Field(default_factory=list)
    turns: list[Turn] = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)
    #: B-141 —— 用例测的是压缩之后的行为:整次运行一次 ``compaction`` 都没有就判「不可判」
    #: (不算过也不算不过)—— 在不可能失败的条件下验证等于没验证。
    requires_compaction: bool = False


def load_case(path: Path) -> Case:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    case = Case.model_validate(raw)
    if case.id != path.stem:
        raise ValueError(f"{path.name}: id {case.id!r} must equal the file stem {path.stem!r}")
    return case


def load_cases(cases_dir: Path = CASES_DIR, only: Iterable[str] | None = None) -> list[Case]:
    """按 id 排序加载全部用例;``only`` 里有不存在的 id 直接报错。"""
    stray = sorted(
        p.name for p in cases_dir.iterdir() if p.suffix != ".yaml" and not p.name.startswith(".")
    )
    if stray:
        raise ValueError(f"{cases_dir}: only *.yaml case files are loaded; found {stray}")
    cases = [load_case(p) for p in sorted(cases_dir.glob("*.yaml"))]
    if only is None:
        return cases
    wanted = set(only)
    unknown = sorted(wanted - {c.id for c in cases})
    if unknown:
        raise ValueError(f"unknown case ids: {unknown}")
    return [c for c in cases if c.id in wanted]


def _counts(path: Path, root: Path) -> bool:
    """只算入库的内容:跳过 ``__pycache__`` 与点文件(测试导入生成脚本会留下 pyc)。"""
    parts = path.relative_to(root).parts
    return not any(part == "__pycache__" or part.startswith(".") for part in parts)


def case_set_hash(cases_dir: Path = CASES_DIR, fixtures_dir: Path = FIXTURES_DIR) -> str:
    """用例与 fixtures 的内容哈希(12 位)。对照时两边不同就拒绝比较。"""
    digest = hashlib.sha256()
    for root in (cases_dir, fixtures_dir):
        for p in sorted(q for q in root.rglob("*") if q.is_file() and _counts(q, root)):
            digest.update(str(p.relative_to(root.parent)).encode())
            digest.update(b"\0")
            digest.update(p.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:12]


# --- 一次运行的记录 ---------------------------------------------------------


class ToolCall(_Strict):
    turn: int
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(_Strict):
    index: int
    run_id: str | None = None
    status: str | None = None
    completed: bool | None = None
    exit_reason: str | None = None
    final_text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_errors: int = 0
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    input_tokens: int | None = None  # None = 结束帧没有用量(缺席 ≠ 0)
    output_tokens: int | None = None
    wall_s: float = 0.0
    compactions: int = 0  # 本轮 ``compaction`` 帧个数(帧对外可见,没有帧 = 没压缩)


class RunRecord(_Strict):
    case_id: str
    rep: int
    user_id: str
    session_id: str | None = None
    turns: list[TurnRecord] = Field(default_factory=list)


# --- 结果 -------------------------------------------------------------------


class CheckVerdict(_Strict):
    type: str
    passed: bool
    detail: str = ""


class CaseResult(_Strict):
    kind: Literal["case"] = "case"
    case_id: str
    rep: int
    passed: bool | None  # None = infra_error 或不可判,不计入过 / 不过
    verdicts: list[CheckVerdict] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    turn_metrics: list[dict[str, int | None]] = Field(default_factory=list)
    infra_error: str | None = None
    #: 不可判的原因(``requires_compaction`` 而一次压缩都没发生);此时 ``passed`` 为 None,
    #: ``verdicts`` 照常记下来留作排查。
    indeterminate: str | None = None
    session_id: str | None = None


class ResultHeader(_Strict):
    kind: Literal["header"] = "header"
    label: str
    started_at: str
    base_url: str
    note: str = ""
    agent_map: dict[str, str] = Field(default_factory=dict)
    case_set_hash: str
    repeats: int
    case_ids: list[str]


def append_line(path: Path, item: ResultHeader | CaseResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(item.model_dump_json() + "\n")


def read_results(path: Path) -> tuple[ResultHeader, list[CaseResult]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not rows or rows[0].get("kind") != "header":
        raise ValueError(f"{path}: first line must be the header")
    return ResultHeader.model_validate(rows[0]), [CaseResult.model_validate(r) for r in rows[1:]]
