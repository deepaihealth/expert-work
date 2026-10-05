# B-140 行为回归评测 PR-1(框架 + 3 个样板用例)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `tools/eval/` 下做出能在测试环境真跑合成任务、并对比两次结果的评测框架,附 3 个样板用例(g02 / g08 / h01)。

**Architecture:** 本机 CLI 走对外平面(`/v1/agents/{code}/...`)对测试环境发起真实 run;流里收工具调用与结束帧,取回产物 / 工作区文件,用纯函数判据判分,结果逐行写 JSONL;另一个 CLI 读两份 JSONL,按「稳定翻转」规则出 Markdown 对照表。模块平铺在 `tools/eval/`,文件名统一 `behavior_` 前缀。

**Tech Stack:** Python 3.12、httpx(AsyncClient + MockTransport 测试)、pydantic v2、PyYAML、标准库 zipfile / xml / difflib;pytest。

**Spec:** `docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md`

## Global Constraints

- 纯合成:用例、虚构客户、fixtures 全部编造;不读生产、不读对接方租户;仓库里不出现任何真实客户内容。
- 只跑测试环境或本机:runner 拒绝主机名既不是 `localhost` / `127.0.0.1`、也不含 `-test.` 的地址。
- API key 只经环境变量 `EXPERT_WORK_API_TOKEN` 传入;不进命令行参数、不落文件、不打印。
- 只走对外平面 `/v1/agents/{code}/...`(API key 打控制台平面会被 `console_only()` 403)。
- 不加任何新依赖(docx / pptx 用标准库读 zip;本机没有 python-docx)。
- 模块平铺在 `tools/eval/`,文件名 `behavior_*.py`,测试 `test_behavior_*.py`;数据在 `tools/eval/datasets/behavior/{agents,cases,fixtures}`。
- 用例文本用简体中文;称呼一律「员工 / 客户」,不出现「教练」。
- 稳定退步 = 改动前过的次数 ≥ 2/3 且改动后 ≤ 1/3(整数比较:`3*passed >= 2*n` / `3*passed <= n`);稳定进步反之;`infra_error` 不计入 n。
- 指标(tokens_in / tokens_out / tool_calls / wall_s)比中位数,变化 ≥ 20% 标出。
- ruff 规则含 E / S / RUF,行宽 100:每个任务提交前跑 `ruff format` + `ruff check`,格式化后仍超长的字符串手工折行;`noqa` 只留真正触发的(E402 / S314)。
- 运行测试一律:`UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest <path> -q`(在 worktree 根目录执行;`--no-sync` 必带)。

## Review Focus

1. SSE 里出现 `:` 开头的心跳注释行、一个事件的 `data:` 分成多行 —— 解析器必须忽略注释、把多行 data 用 `\n` 拼起来(Task 4 测试 `test_parser_handles_comments_and_multiline_data`)。
2. 流在 `end` 帧之前断开(B-155 那类)—— 必须记为 `infra_error` 并重试,而不是判成任务不过(Task 4 `test_run_turn_without_end_raises_stream_incomplete`、Task 5 `test_run_one_retries_transport_errors_then_records_infra`)。
3. 结束帧没有 `usage_by_model` —— token 记为 None(缺席 ≠ 0),`turn_tokens_max` 判不过并写明原因,指标里不出现半截求和(Task 3 `test_turn_tokens_max_without_usage_fails`、Task 5 `test_metrics_skip_tokens_when_any_turn_lacks_usage`)。
4. 产物通配符匹配到多个 —— 取最后一轮里最后登记的那个(Task 5 `test_find_artifact_prefers_latest`)。
5. 用例文件名与 id 不一致、`--only` 写了不存在的 id —— 加载时直接报错,不静默跳过(Task 1 `test_load_case_rejects_stem_mismatch`、`test_load_cases_only_rejects_unknown`)。

---

### Task 1: 数据模型、用例加载、结果读写

**Files:**
- Create: `tools/eval/behavior_schema.py`
- Test: `tools/eval/test_behavior_schema.py`

**Interfaces:**
- Produces: `Case`, `Turn`, `Check`(判别联合,16 个判据类)、`ToolCall`, `TurnRecord`, `RunRecord`, `CheckVerdict`, `CaseResult`, `ResultHeader`;`load_case(path) -> Case`;`load_cases(cases_dir=CASES_DIR, only=None) -> list[Case]`;`case_set_hash(cases_dir=CASES_DIR, fixtures_dir=FIXTURES_DIR) -> str`;`append_line(path, item) -> None`;`read_results(path) -> tuple[ResultHeader, list[CaseResult]]`;常量 `DATASET_DIR` / `CASES_DIR` / `FIXTURES_DIR` / `AGENTS_DIR`。

- [ ] **Step 1: 写失败的测试**

```python
"""B-140 behavior_schema 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from behavior_schema import (
    CaseResult,
    CheckVerdict,
    ResultHeader,
    ToolUsed,
    WorkspaceFileContains,
    append_line,
    case_set_hash,
    load_case,
    load_cases,
    read_results,
)

_CASE = """\
id: g99-sample
agent: eval-general
shape: B-137
fixtures: [a.md]
turns:
  - prompt: 改一下
checks:
  - {type: completed}
  - {type: tool_used, tool: edit_file}
  - {type: workspace_file_contains, path: a.md, all: [新]}
"""


def _write(dir_: Path, name: str, text: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_text(text, encoding="utf-8")
    return p


def test_load_case_parses_discriminated_checks(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "g99-sample.yaml", _CASE))
    assert case.agent == "eval-general"
    assert isinstance(case.checks[1], ToolUsed)
    assert isinstance(case.checks[2], WorkspaceFileContains)
    assert case.checks[2].path == "a.md"


def test_load_case_rejects_stem_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="file stem"):
        load_case(_write(tmp_path, "g99-other.yaml", _CASE))


def test_load_case_rejects_unknown_check_type(tmp_path: Path) -> None:
    bad = _CASE.replace("{type: completed}", "{type: vibes_ok}")
    with pytest.raises(ValidationError):
        load_case(_write(tmp_path, "g99-sample.yaml", bad))


def test_load_cases_only_filters_and_rejects_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "g99-sample.yaml", _CASE)
    _write(tmp_path, "g98-other.yaml", _CASE.replace("g99-sample", "g98-other"))
    assert [c.id for c in load_cases(tmp_path)] == ["g98-other", "g99-sample"]
    assert [c.id for c in load_cases(tmp_path, only=["g99-sample"])] == ["g99-sample"]
    with pytest.raises(ValueError, match="unknown case ids"):
        load_cases(tmp_path, only=["g01-nope"])


def test_load_cases_only_rejects_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "g99-sample.yaml", _CASE)
    with pytest.raises(ValueError, match="g00-missing"):
        load_cases(tmp_path, only=["g00-missing"])


def test_case_set_hash_changes_with_fixture_bytes(tmp_path: Path) -> None:
    cases, fixtures = tmp_path / "cases", tmp_path / "fixtures"
    _write(cases, "g99-sample.yaml", _CASE)
    _write(fixtures, "a.md", "旧\n")
    before = case_set_hash(cases, fixtures)
    _write(fixtures, "a.md", "新\n")
    assert case_set_hash(cases, fixtures) != before
    assert len(before) == 12


def test_results_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    header = ResultHeader(
        label="base", started_at="2026-10-05T00:00:00+00:00", base_url="http://localhost",
        case_set_hash="abc123abc123", repeats=3, case_ids=["g99-sample"],
    )
    result = CaseResult(
        case_id="g99-sample", rep=1, passed=False,
        verdicts=[CheckVerdict(type="completed", passed=False, detail="turns not completed: [1]")],
        metrics={"tool_calls": 2.0},
    )
    append_line(out, header)
    append_line(out, result)
    got_header, got_results = read_results(out)
    assert got_header == header
    assert got_results == [result]


def test_read_results_requires_header(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    append_line(out, CaseResult(case_id="g99-sample", rep=1, passed=True))
    with pytest.raises(ValueError, match="header"):
        read_results(out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_schema.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'behavior_schema'`

- [ ] **Step 3: 实现 `tools/eval/behavior_schema.py`**

```python
"""B-140 行为回归评测 —— 数据模型:用例、一次运行的记录、判定结果。

见 ``docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DATASET_DIR = Path(__file__).parent / "datasets" / "behavior"
CASES_DIR = DATASET_DIR / "cases"
FIXTURES_DIR = DATASET_DIR / "fixtures"
AGENTS_DIR = DATASET_DIR / "agents"

AgentCode = Literal["eval-general", "eval-ahp"]


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


class FinalTextRegex(_Strict):
    type: Literal["final_text_regex"]
    pattern: str = Field(min_length=1)


class ArtifactExists(_Strict):
    type: Literal["artifact_exists"]
    name: str = Field(min_length=1)  # fnmatch 通配


class ArtifactContains(_Strict):
    type: Literal["artifact_contains"]
    name: str = Field(min_length=1)
    all: list[str] = Field(min_length=1)


class ArtifactNotContains(_Strict):
    type: Literal["artifact_not_contains"]
    name: str = Field(min_length=1)
    any: list[str] = Field(min_length=1)


class ArtifactUnchangedLines(_Strict):
    type: Literal["artifact_unchanged_lines"]
    name: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    except_lines: list[int] = Field(default_factory=list)


class WorkspaceFileContains(_Strict):
    type: Literal["workspace_file_contains"]
    path: str = Field(min_length=1)
    all: list[str] = Field(min_length=1)


class WorkspaceFileUnchangedLines(_Strict):
    type: Literal["workspace_file_unchanged_lines"]
    path: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    except_lines: list[int] = Field(default_factory=list)


class ToolUsed(_Strict):
    type: Literal["tool_used"]
    tool: str = Field(min_length=1)


class ToolNotUsed(_Strict):
    type: Literal["tool_not_used"]
    tool: str = Field(min_length=1)


class ToolCountMax(_Strict):
    type: Literal["tool_count_max"]
    max: int = Field(ge=0)
    tool: str | None = None  # None = 全部工具


class ToolNotUsedOn(_Strict):
    type: Literal["tool_not_used_on"]
    tool: str = Field(min_length=1)
    path: str = Field(min_length=1)


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
    | WorkspaceFileUnchangedLines
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
    id: str = Field(pattern=r"^[gh][0-9]{2}-[a-z0-9-]+$")
    agent: AgentCode
    shape: str = Field(min_length=1)  # 出处:ROADMAP 编号
    fixtures: list[str] = Field(default_factory=list)
    turns: list[Turn] = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)


def load_case(path: Path) -> Case:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    case = Case.model_validate(raw)
    if case.id != path.stem:
        raise ValueError(f"{path.name}: id {case.id!r} must equal the file stem {path.stem!r}")
    return case


def load_cases(cases_dir: Path = CASES_DIR, only: Iterable[str] | None = None) -> list[Case]:
    """按 id 排序加载全部用例;``only`` 里有不存在的 id 直接报错。"""
    cases = [load_case(p) for p in sorted(cases_dir.glob("*.yaml"))]
    if only is None:
        return cases
    wanted = set(only)
    unknown = sorted(wanted - {c.id for c in cases})
    if unknown:
        raise ValueError(f"unknown case ids: {unknown}")
    return [c for c in cases if c.id in wanted]


def case_set_hash(cases_dir: Path = CASES_DIR, fixtures_dir: Path = FIXTURES_DIR) -> str:
    """用例与 fixtures 的内容哈希(12 位)。对照时两边不同就拒绝比较。"""
    digest = hashlib.sha256()
    for root in (cases_dir, fixtures_dir):
        for p in sorted(q for q in root.rglob("*") if q.is_file()):
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
    passed: bool | None  # None = infra_error,不计入过 / 不过
    verdicts: list[CheckVerdict] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    infra_error: str | None = None
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
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or rows[0].get("kind") != "header":
        raise ValueError(f"{path}: first line must be the header")
    return ResultHeader.model_validate(rows[0]), [CaseResult.model_validate(r) for r in rows[1:]]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_schema.py -q`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add tools/eval/behavior_schema.py tools/eval/test_behavior_schema.py
git commit -m "feat(eval): B-140 behavior eval data model and case loader"
```

---

### Task 2: 从产物取文本

**Files:**
- Create: `tools/eval/behavior_extract.py`
- Test: `tools/eval/test_behavior_extract.py`

**Interfaces:**
- Produces: `document_text(name: str, data: bytes) -> str`;异常 `UnsupportedDocumentError(ValueError)`。docx 每段一行;pptx 按幻灯片编号顺序、每段一行;`.md/.txt/.csv/.json/.py/.html` 按 UTF-8 解码;其余(含 `.pdf` / `.xlsx`)抛 `UnsupportedDocumentError`。

- [ ] **Step 1: 写失败的测试**

```python
"""B-140 behavior_extract 单元测试。"""

from __future__ import annotations

import io
import zipfile

import pytest

from behavior_extract import UnsupportedDocumentError, document_text

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_docx_paragraphs_become_lines() -> None:
    xml = (
        f"<w:document {_W}><w:body>"
        "<w:p><w:r><w:t>王小雨</w:t></w:r><w:r><w:t>的方案</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>饮食建议</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    assert document_text("方案.docx", _zip({"word/document.xml": xml})) == "王小雨的方案\n饮食建议"


def test_pptx_slides_in_numeric_order() -> None:
    def slide(text: str) -> str:
        return f"<p:sld {_A} xmlns:p='x'><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>"

    data = _zip(
        {"ppt/slides/slide10.xml": slide("第十页"), "ppt/slides/slide2.xml": slide("第二页")}
    )
    assert document_text("deck.PPTX", data) == "第二页\n第十页"


def test_plain_text_decoded() -> None:
    assert document_text("report.md", "第一行\n".encode()) == "第一行\n"


def test_pdf_is_unsupported() -> None:
    with pytest.raises(UnsupportedDocumentError):
        document_text("a.pdf", b"%PDF-1.7")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_extract.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'behavior_extract'`

- [ ] **Step 3: 实现 `tools/eval/behavior_extract.py`**

```python
"""B-140 —— 从产物字节里取文本给判据用。

docx / pptx 是 zip 包,直接用标准库读里面的 XML(本机与 CI 都没有 python-docx,
不为评测加依赖)。pdf / xlsx 本版不支持,判据会判不过并写明原因。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_TEXT_SUFFIXES = (".md", ".txt", ".csv", ".json", ".py", ".html")
_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml")


class UnsupportedDocumentError(ValueError):
    """判据要读的产物类型本版取不出文本。"""


def _paragraphs(xml: bytes, para: str, text: str) -> list[str]:
    # 输入是我们自己的评测智能体在测试环境生成的产物,不是外部上传。
    root = ET.fromstring(xml)  # noqa: S314
    return ["".join(t.text or "" for t in p.iter(text)) for p in root.iter(para)]


def document_text(name: str, data: bytes) -> str:
    lower = name.lower()
    if lower.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "\n".join(_paragraphs(zf.read("word/document.xml"), f"{_W}p", f"{_W}t"))
    if lower.endswith(".pptx"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            numbered = [
                (int(match.group(1)), member)
                for member in zf.namelist()
                if (match := _SLIDE_RE.fullmatch(member))
            ]
            lines: list[str] = []
            for _, member in sorted(numbered):
                lines.extend(_paragraphs(zf.read(member), f"{_A}p", f"{_A}t"))
            return "\n".join(lines)
    if lower.endswith(_TEXT_SUFFIXES):
        return data.decode("utf-8", errors="replace")
    raise UnsupportedDocumentError(f"no text extractor for {name!r}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_extract.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add tools/eval/behavior_extract.py tools/eval/test_behavior_extract.py
git commit -m "feat(eval): B-140 extract text from docx/pptx artifacts without new deps"
```

---

### Task 3: 判据

**Files:**
- Create: `tools/eval/behavior_checks.py`
- Test: `tools/eval/test_behavior_checks.py`

**Interfaces:**
- Consumes: Task 1 全部判据类、`Case`、`RunRecord`、`ToolCall`、`TurnRecord`、`CheckVerdict`;Task 2 `document_text`、`UnsupportedDocumentError`。
- Produces: `FileKey = tuple[Literal["artifact", "workspace"], str]`;`FetchedFile = tuple[str, bytes]`(实际文件名,内容);`required_files(case) -> list[FileKey]`;`evaluate(case, record, files: Mapping[FileKey, FetchedFile | None], fixtures_dir: Path) -> list[CheckVerdict]`;`changed_lines(before: str, after: str) -> set[int]`;`path_matches(candidate: str, target: str) -> bool`。

- [ ] **Step 1: 写失败的测试**

```python
"""B-140 behavior_checks 单元测试:每种判据一条「好记录过」、一条「坏记录不过」。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from behavior_checks import changed_lines, evaluate, path_matches, required_files
from behavior_schema import Case, RunRecord, ToolCall, TurnRecord


def _case(checks: list[dict[str, Any]]) -> Case:
    return Case.model_validate(
        {"id": "g99-x", "agent": "eval-general", "shape": "test", "turns": [{"prompt": "p"}], "checks": checks}
    )


def _turn(index: int = 1, **kw: Any) -> TurnRecord:
    base: dict[str, Any] = {
        "index": index, "status": "success", "completed": True, "exit_reason": "text_response",
        "final_text": "A=104.5, B=209.0", "input_tokens": 1000, "output_tokens": 50,
    }
    base.update(kw)
    return TurnRecord.model_validate(base)


def _record(*turns: TurnRecord) -> RunRecord:
    return RunRecord(case_id="g99-x", rep=1, user_id="u", turns=list(turns))


def _one(check: dict[str, Any], record: RunRecord, files: dict | None = None, fixtures: Path | None = None) -> tuple[bool, str]:
    [v] = evaluate(_case([check]), record, files or {}, fixtures or Path("."))
    return v.passed, v.detail


def test_completed_requires_every_turn() -> None:
    assert _one({"type": "completed"}, _record(_turn(1), _turn(2)))[0]
    ok, detail = _one({"type": "completed"}, _record(_turn(1), _turn(2, completed=False)))
    assert not ok and "[2]" in detail
    assert not _one({"type": "completed"}, _record(_turn(1, exit_reason="max_steps")))[0]


def test_exit_reason_reads_last_turn() -> None:
    assert _one({"type": "exit_reason", "value": "text_response"}, _record(_turn()))[0]
    assert not _one({"type": "exit_reason", "value": "text_response"}, _record(_turn(exit_reason="no_progress")))[0]


def test_final_text_checks() -> None:
    rec = _record(_turn(final_text="结果:A=104.5"))
    assert _one({"type": "final_text_contains", "all": ["A=104.5"]}, rec)[0]
    assert not _one({"type": "final_text_contains", "all": ["B=209.0"]}, rec)[0]
    assert _one({"type": "final_text_not_contains", "any": ["教练"]}, rec)[0]
    assert not _one({"type": "final_text_not_contains", "any": ["A="]}, rec)[0]
    assert _one({"type": "final_text_regex", "pattern": r"A=\d+\.\d"}, rec)[0]
    assert not _one({"type": "final_text_regex", "pattern": r"C=\d"}, rec)[0]


def test_artifact_exists_glob_across_turns() -> None:
    rec = _record(_turn(1), _turn(2, artifacts=[{"name": "王小雨-健康方案.docx", "version": 1}]))
    assert _one({"type": "artifact_exists", "name": "*.docx"}, rec)[0]
    assert not _one({"type": "artifact_exists", "name": "*.pptx"}, rec)[0]


def test_artifact_and_workspace_contains() -> None:
    files = {
        ("artifact", "*.md"): ("plan.md", "饮食\n运动\n".encode()),
        ("workspace", "report.md"): ("report.md", "数据截至 2026 年 9 月\n".encode()),
    }
    rec = _record(_turn())
    assert _one({"type": "artifact_contains", "name": "*.md", "all": ["饮食", "运动"]}, rec, files)[0]
    ok, detail = _one({"type": "artifact_contains", "name": "*.md", "all": ["睡眠"]}, rec, files)
    assert not ok and "睡眠" in detail
    assert _one({"type": "artifact_not_contains", "name": "*.md", "any": ["教练"]}, rec, files)[0]
    assert not _one({"type": "artifact_not_contains", "name": "*.md", "any": ["饮食"]}, rec, files)[0]
    assert _one({"type": "workspace_file_contains", "path": "report.md", "all": ["2026 年 9 月"]}, rec, files)[0]


def test_missing_or_unreadable_file_fails_with_reason() -> None:
    rec = _record(_turn())
    ok, detail = _one({"type": "artifact_contains", "name": "*.docx", "all": ["x"]}, rec, {("artifact", "*.docx"): None})
    assert not ok and "not found" in detail
    ok, detail = _one(
        {"type": "artifact_contains", "name": "*.docx", "all": ["x"]}, rec, {("artifact", "*.docx"): ("a.docx", b"not a zip")}
    )
    assert not ok and "unreadable" in detail


def test_unchanged_lines(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("一\n二\n三\n四\n", encoding="utf-8")
    rec = _record(_turn())
    check = {"type": "workspace_file_unchanged_lines", "path": "f.md", "fixture": "f.md", "except_lines": [2]}
    good = {("workspace", "f.md"): ("f.md", "一\n贰\n三\n四\n".encode())}
    bad = {("workspace", "f.md"): ("f.md", "一\n贰\n叁\n四\n".encode())}
    assert _one(check, rec, good, tmp_path)[0]
    ok, detail = _one(check, rec, bad, tmp_path)
    assert not ok and "[3]" in detail


def test_changed_lines_counts_insertions_and_deletions() -> None:
    assert changed_lines("a\nb\nc\n", "a\nb\nc\n") == set()
    assert changed_lines("a\nb\nc\n", "a\nX\nc\n") == {2}
    assert changed_lines("a\nb\nc\n", "a\nb\nNEW\nc\n") == {2}
    assert changed_lines("a\nb\nc\n", "a\nc\n") == {2}
    assert changed_lines("a\n", "NEW\na\n") == {1}


def test_tool_checks() -> None:
    rec = _record(
        _turn(1, tool_calls=[ToolCall(turn=1, name="bash", args={"command": "cp a b"})]),
        _turn(2, tool_calls=[ToolCall(turn=2, name="edit_file", args={"path": "/workspace/report.md"})]),
    )
    assert _one({"type": "tool_used", "tool": "edit_file"}, rec)[0]
    assert not _one({"type": "tool_used", "tool": "write_file"}, rec)[0]
    assert _one({"type": "tool_not_used", "tool": "write_file"}, rec)[0]
    assert not _one({"type": "tool_not_used", "tool": "bash"}, rec)[0]
    assert _one({"type": "tool_count_max", "max": 2}, rec)[0]
    assert not _one({"type": "tool_count_max", "max": 1}, rec)[0]
    assert _one({"type": "tool_count_max", "max": 1, "tool": "bash"}, rec)[0]
    assert _one({"type": "tool_not_used_on", "tool": "write_file", "path": "report.md"}, rec)[0]
    ok, detail = _one({"type": "tool_not_used_on", "tool": "edit_file", "path": "report.md"}, rec)
    assert not ok and "[2]" in detail


def test_path_matches_normalizes_workspace_prefixes() -> None:
    assert path_matches("/workspace/report.md", "report.md")
    assert path_matches("./uploads/a.md", "uploads/a.md")
    assert path_matches("/workspace/agent-key/report.md", "report.md")
    assert not path_matches("myreport.md", "report.md")


def test_turn_tokens_max() -> None:
    rec = _record(_turn(1, input_tokens=900), _turn(2, input_tokens=5000))
    assert _one({"type": "turn_tokens_max", "turn": 1, "max_input_tokens": 1000}, rec)[0]
    ok, detail = _one({"type": "turn_tokens_max", "turn": 2, "max_input_tokens": 1000}, rec)
    assert not ok and "5000" in detail
    assert not _one({"type": "turn_tokens_max", "turn": 3, "max_input_tokens": 1000}, rec)[0]


def test_turn_tokens_max_without_usage_fails() -> None:
    ok, detail = _one({"type": "turn_tokens_max", "turn": 1, "max_input_tokens": 10}, _record(_turn(input_tokens=None)))
    assert not ok and "no usage" in detail


def test_required_files_dedupes_in_order() -> None:
    case = _case([
        {"type": "artifact_contains", "name": "*.docx", "all": ["a"]},
        {"type": "artifact_not_contains", "name": "*.docx", "any": ["b"]},
        {"type": "workspace_file_contains", "path": "r.md", "all": ["c"]},
        {"type": "completed"},
    ])
    assert required_files(case) == [("artifact", "*.docx"), ("workspace", "r.md")]


@pytest.mark.parametrize("check_type", [
    "completed", "exit_reason", "final_text_contains", "final_text_not_contains", "final_text_regex",
    "artifact_exists", "artifact_contains", "artifact_not_contains", "artifact_unchanged_lines",
    "workspace_file_contains", "workspace_file_unchanged_lines", "tool_used", "tool_not_used",
    "tool_count_max", "tool_not_used_on", "turn_tokens_max",
])
def test_every_check_type_is_documented_in_spec(check_type: str) -> None:
    spec = Path(__file__).resolve().parents[2] / "docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md"
    assert f"`{check_type}`" in spec.read_text(encoding="utf-8")
```

> 最后一条参数化测试确认每个判据名都在规格里出现过(规格表把同类判据写在同一格,每个名字各自带反引号),防止代码里多出规格没写的判据类型。

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_checks.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'behavior_checks'`

- [ ] **Step 3: 实现 `tools/eval/behavior_checks.py`**

```python
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
            if not (t.status == "success" and t.completed is True and t.exit_reason == "text_response")
        ]
        return _verdict(check, bool(turns) and not bad, f"turns not completed: {bad}" if turns else "no turns")
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
        return _verdict(check, re.search(check.pattern, final) is not None, f"no match for {check.pattern!r}")
    if isinstance(check, ArtifactExists):
        names = [str(a["name"]) for t in turns for a in t.artifacts if isinstance(a, dict) and a.get("name")]
        return _verdict(check, any(fnmatch.fnmatchcase(n, check.name) for n in names), f"artifacts={names}")
    if isinstance(check, ArtifactContains | WorkspaceFileContains):
        key: FileKey = ("artifact", check.name) if isinstance(check, ArtifactContains) else ("workspace", check.path)
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
            ("artifact", check.name) if isinstance(check, ArtifactUnchangedLines) else ("workspace", check.path)
        )
        text, problem = _text(files, key)
        if text is None:
            return _verdict(check, False, problem)
        before = (fixtures_dir / check.fixture).read_text(encoding="utf-8")
        extra = sorted(changed_lines(before, text) - set(check.except_lines))
        return _verdict(check, not extra, f"unexpected changes at fixture lines {extra[:20]}")
    if isinstance(check, ToolUsed):
        return _verdict(
            check, any(c.name == check.tool for c in calls), f"tools used: {sorted({c.name for c in calls})}"
        )
    if isinstance(check, ToolNotUsed):
        n = sum(c.name == check.tool for c in calls)
        return _verdict(check, n == 0, f"{check.tool} called {n} times")
    if isinstance(check, ToolCountMax):
        n = sum(1 for c in calls if check.tool is None or c.name == check.tool)
        return _verdict(check, n <= check.max, f"{n} calls > {check.max}")
    if isinstance(check, ToolNotUsedOn):
        hits = [
            c.turn for c in calls if c.name == check.tool and path_matches(str(c.args.get("path", "")), check.path)
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_checks.py -q`
Expected: 29 passed(13 条普通 + 16 条参数化)

- [ ] **Step 5: 变异自证(判据能红)**

先提交,再逐条变异、确认对应测试变红、用反向替换还原(**不用 `git checkout` 还原**):

```bash
git add tools/eval/behavior_checks.py tools/eval/test_behavior_checks.py
git commit -m "feat(eval): B-140 behavior checks (pure functions)"
S=/private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad
F=tools/eval/behavior_checks.py
python3 $S/mut.py $F 't.completed is True and t.exit_reason == "text_response"' 't.completed is True'
git diff --stat   # 必须显示 1 file changed
UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_checks.py -q -k completed   # 期望 1 failed
python3 $S/mut.py $F 't.completed is True' 't.completed is True and t.exit_reason == "text_response"'
git status --porcelain   # 期望为空
```

再对 `changed_lines` 的插入分支做一次:把 `out.add(max(i1, 1))` 换成 `pass`,`-k changed_lines` 期望红,还原后 `git status --porcelain` 为空。

---

### Task 4: 对外平面客户端

**Files:**
- Create: `tools/eval/behavior_client.py`
- Test: `tools/eval/test_behavior_client.py`

**Interfaces:**
- Consumes: Task 1 `ToolCall`、`TurnRecord`。
- Produces: `class ExternalClient(http: httpx.AsyncClient)`,方法:
  - `upload(agent: str, user_id: str, filename: str, data: bytes, session_id: str | None) -> tuple[str, str]`(upload_id, session_id)
  - `run_turn(agent: str, user_id: str, session_id: str | None, prompt: str, upload_ids: list[str], index: int) -> tuple[TurnRecord, str | None]`(记录, 会话 id)
  - `download_artifact(agent: str, user_id: str, entry: dict[str, Any]) -> bytes | None`(404 → None)
  - `read_workspace_file(agent: str, user_id: str, path: str) -> bytes | None`(404 → None)
  - `archive(agent: str, user_id: str, session_id: str) -> None`
- 异常 `StreamIncompleteError(RuntimeError)`:流在 `end` 帧前结束。
- 辅助:`SseParser.feed(line) -> tuple[str, str] | None`、`SseParser.flush()`;`TurnBuilder(index)`、`.on_frame(event, raw)`、`.result(wall_s) -> TurnRecord`、`.session_id`。

- [ ] **Step 1: 写失败的测试**

```python
"""B-140 behavior_client 单元测试(httpx.MockTransport,不连真栈)。"""

from __future__ import annotations

import json

from collections.abc import Callable

import httpx
import pytest

from behavior_client import ExternalClient, SseParser, StreamIncompleteError


def _sse(*frames: tuple[str, object]) -> str:
    out = [": keepalive", ""]
    for event, data in frames:
        out += [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False)}", ""]
    return "\n".join(out) + "\n"


_FRAMES = [
    ("metadata", {"run_id": "r1", "thread_id": "s1"}),
    ("updates", {"agent": {"messages": [{"type": "ai", "content": "", "tool_calls": [
        {"name": "edit_file", "args": {"path": "report.md"}, "id": "c1"}]}]}}),
    ("updates", {"tools": {"messages": [{"type": "tool", "name": "edit_file", "status": "error", "content": "x"}]}}),
    ("updates", {"agent": {"messages": [{"type": "ai", "content": [{"type": "text", "text": "已改好"}], "tool_calls": []}]}}),
    ("token", {"t": "已"}),
    ("end", {"status": "success", "run_id": "r1", "completed": True, "exit_reason": "text_response",
             "artifacts": [{"name": "report.md", "version": 2}],
             "usage_by_model": [{"provider": "glm", "model": "glm-5.3", "input_tokens": 1000, "output_tokens": 40},
                                {"provider": "glm", "model": "glm-5.3-flash", "input_tokens": 200, "output_tokens": 10}]}),
]


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ExternalClient:
    return ExternalClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t"))


def test_parser_handles_comments_and_multiline_data() -> None:
    p = SseParser()
    got = [p.feed(line) for line in [": ping", "event: end", 'data: {"a":', 'data: 1}', ""]]
    assert got[-1] == ("end", '{"a":\n1}')
    assert json.loads(got[-1][1]) == {"a": 1}
    assert [g for g in got[:-1] if g is not None] == []


@pytest.mark.asyncio
async def test_run_turn_collects_tools_text_end_and_usage() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=_sse(*_FRAMES), headers={"content-type": "text/event-stream"})

    rec, session_id = await _client(handler).run_turn("eval-general", "u1", None, "改三处", ["upl_1"], 1)
    assert seen["path"] == "/v1/agents/eval-general/runs"
    assert seen["body"] == {"user_id": "u1", "input": "改三处", "mode": "stream", "files": [{"upload_id": "upl_1"}]}
    assert session_id == "s1"
    assert rec.run_id == "r1" and rec.status == "success" and rec.completed is True
    assert [c.name for c in rec.tool_calls] == ["edit_file"] and rec.tool_calls[0].args == {"path": "report.md"}
    assert rec.tool_errors == 1
    assert rec.final_text == "已改好"
    assert rec.artifacts == [{"name": "report.md", "version": 2}]
    assert (rec.input_tokens, rec.output_tokens) == (1200, 50)


@pytest.mark.asyncio
async def test_run_turn_continues_session_and_keeps_usage_absent() -> None:
    frames = [f for f in _FRAMES if f[0] != "end"] + [("end", {"status": "success", "run_id": "r1"})]

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["session_id"] == "s0"
        return httpx.Response(200, text=_sse(*frames))

    rec, _ = await _client(handler).run_turn("eval-general", "u1", "s0", "再改", [], 2)
    assert rec.index == 2
    assert rec.input_tokens is None and rec.output_tokens is None
    assert rec.completed is None


@pytest.mark.asyncio
async def test_run_turn_without_end_raises_stream_incomplete() -> None:
    frames = [f for f in _FRAMES if f[0] != "end"]
    client = _client(lambda request: httpx.Response(200, text=_sse(*frames)))
    with pytest.raises(StreamIncompleteError):
        await client.run_turn("eval-general", "u1", None, "p", [], 1)


@pytest.mark.asyncio
async def test_upload_sends_multipart_with_user_and_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="replace")
        assert request.url.path == "/v1/agents/eval-general/uploads"
        assert 'name="user_id"' in body and "u1" in body
        assert "text/markdown" in body and "report.md" in body
        return httpx.Response(201, json={"success": True, "data": {"upload_id": "upl_9", "session_id": "s9"}})

    assert await _client(handler).upload("eval-general", "u1", "report.md", "内容".encode(), None) == ("upl_9", "s9")


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_type() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        await _client(lambda r: httpx.Response(500)).upload("eval-general", "u1", "a.py", b"x", None)


@pytest.mark.asyncio
async def test_downloads_return_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts/download"):
            assert request.url.params["name"] == "a.docx" and request.url.params["version"] == "3"
            return httpx.Response(200, content=b"DOCX")
        return httpx.Response(404, json={"success": False})

    client = _client(handler)
    assert await client.download_artifact("eval-ahp", "u1", {"name": "a.docx", "version": 3}) == b"DOCX"
    assert await client.read_workspace_file("eval-ahp", "u1", "report.md") is None


@pytest.mark.asyncio
async def test_archive_passes_user_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/agents/eval-general/sessions/s1"
        assert request.url.params["user_id"] == "u1"
        return httpx.Response(200, json={"success": True})

    await _client(handler).archive("eval-general", "u1", "s1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_client.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'behavior_client'`

- [ ] **Step 3: 实现 `tools/eval/behavior_client.py`**

```python
"""B-140 对外平面客户端:上传开局文件、流式跑一轮、取产物 / 工作区文件、归档会话。

只走 ``/v1/agents/{code}/...`` —— API key 打控制台平面(``/v1/sessions/...``)会被
``console_only()`` 直接 403。帧的形态见 ``docs/api/streaming-events.md``。
"""

from __future__ import annotations

import json
import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from behavior_schema import ToolCall, TurnRecord

# 与 ``control_plane.api.uploads._DOC_EXT_BY_CONTENT_TYPE`` 一致(.py 不在其中)。
_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_AI_TYPES = {"ai", "AIMessage", "AIMessageChunk"}
_TOOL_TYPES = {"tool", "ToolMessage"}


class StreamIncompleteError(RuntimeError):
    """流在 ``end`` 帧之前结束 —— 记为 infra_error,不算任务不过。"""


def content_type_for(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in _CONTENT_TYPES:
        raise ValueError(f"upload type not allowed for {filename!r} (allowed: {sorted(_CONTENT_TYPES)})")
    return _CONTENT_TYPES[suffix]


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class SseParser:
    """逐行喂 SSE;空行结束一个事件。忽略 ``:`` 注释,多行 ``data:`` 用换行拼接。"""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str, str] | None:
        if line == "":
            return self.flush()
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def flush(self) -> tuple[str, str] | None:
        if not self._data:
            self._event = ""
            return None
        frame = (self._event or "message", "\n".join(self._data))
        self._event, self._data = "", []
        return frame


class TurnBuilder:
    """把一轮的帧累积成 :class:`TurnRecord`。"""

    def __init__(self, index: int) -> None:
        self.index = index
        self.session_id: str | None = None
        self._run_id: str | None = None
        self._end: dict[str, Any] | None = None
        self._final_text = ""
        self._calls: list[ToolCall] = []
        self._tool_errors = 0

    def on_frame(self, event: str, raw: str) -> None:
        if event not in {"metadata", "updates", "end"}:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if event == "metadata":
            self._run_id = data.get("run_id") or self._run_id
            self.session_id = data.get("thread_id") or self.session_id
        elif event == "updates":
            self._on_updates(data)
        else:
            self._end = data

    def _on_updates(self, data: dict[str, Any]) -> None:
        for node in data.values():
            if not isinstance(node, dict):
                continue
            for msg in node.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype in _AI_TYPES:
                    calls = [c for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
                    for call in calls:
                        args = call.get("args")
                        self._calls.append(
                            ToolCall(turn=self.index, name=str(call.get("name", "")), args=args if isinstance(args, dict) else {})
                        )
                    text = message_text(msg.get("content"))
                    if text.strip() and not calls:
                        self._final_text = text
                elif mtype in _TOOL_TYPES and msg.get("status") == "error":
                    self._tool_errors += 1

    def result(self, wall_s: float) -> TurnRecord:
        if self._end is None:
            raise StreamIncompleteError(f"turn {self.index}: stream ended without an end frame (run {self._run_id})")
        end = self._end
        usage = end.get("usage_by_model")
        tokens_in: int | None = None
        tokens_out: int | None = None
        if isinstance(usage, list):  # 缺席 = 没有记录,不是 0
            tokens_in = sum(int(u.get("input_tokens") or 0) for u in usage if isinstance(u, dict))
            tokens_out = sum(int(u.get("output_tokens") or 0) for u in usage if isinstance(u, dict))
        artifacts = end.get("artifacts")
        return TurnRecord(
            index=self.index,
            run_id=str(end.get("run_id") or self._run_id or "") or None,
            status=end.get("status"),
            completed=end.get("completed"),
            exit_reason=end.get("exit_reason"),
            final_text=self._final_text,
            tool_calls=self._calls,
            tool_errors=self._tool_errors,
            artifacts=[a for a in artifacts if isinstance(a, dict)] if isinstance(artifacts, list) else [],
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            wall_s=round(wall_s, 1),
        )


class ExternalClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def upload(
        self, agent: str, user_id: str, filename: str, data: bytes, session_id: str | None
    ) -> tuple[str, str]:
        form = {"user_id": user_id}
        if session_id is not None:
            form["session_id"] = session_id
        resp = await self._http.post(
            f"/v1/agents/{agent}/uploads",
            data=form,
            files={"file": (filename, data, content_type_for(filename))},
        )
        resp.raise_for_status()
        body = resp.json()["data"]
        return str(body["upload_id"]), str(body["session_id"])

    async def run_turn(
        self,
        agent: str,
        user_id: str,
        session_id: str | None,
        prompt: str,
        upload_ids: list[str],
        index: int,
    ) -> tuple[TurnRecord, str | None]:
        payload: dict[str, Any] = {"user_id": user_id, "input": prompt, "mode": "stream"}
        if session_id is not None:
            payload["session_id"] = session_id
        if upload_ids:
            payload["files"] = [{"upload_id": u} for u in upload_ids]
        builder = TurnBuilder(index)
        parser = SseParser()
        started = time.monotonic()
        async with self._http.stream("POST", f"/v1/agents/{agent}/runs", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                frame = parser.feed(line)
                if frame is not None:
                    builder.on_frame(*frame)
        tail = parser.flush()
        if tail is not None:
            builder.on_frame(*tail)
        return builder.result(time.monotonic() - started), builder.session_id

    async def download_artifact(self, agent: str, user_id: str, entry: dict[str, Any]) -> bytes | None:
        params: dict[str, Any] = {"user_id": user_id, "name": entry["name"]}
        if entry.get("version") is not None:
            params["version"] = entry["version"]
        resp = await self._http.get(f"/v1/agents/{agent}/artifacts/download", params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def read_workspace_file(self, agent: str, user_id: str, path: str) -> bytes | None:
        resp = await self._http.get(f"/v1/agents/{agent}/workspace/file", params={"user_id": user_id, "path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def archive(self, agent: str, user_id: str, session_id: str) -> None:
        resp = await self._http.delete(f"/v1/agents/{agent}/sessions/{session_id}", params={"user_id": user_id})
        resp.raise_for_status()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_client.py -q`
Expected: 8 passed。若 `pytest.mark.asyncio` 报未知标记,看根 `pyproject.toml` 的 `asyncio_mode`;仓库已用 pytest-asyncio(`tools/eval/test_verify_live*.py` 有先例),照它们的写法改。

- [ ] **Step 5: 提交**

```bash
git add tools/eval/behavior_client.py tools/eval/test_behavior_client.py
git commit -m "feat(eval): B-140 external-plane client (upload, stream a turn, fetch, archive)"
```

---

### Task 5: 跑整套的 runner

**Files:**
- Create: `tools/eval/behavior_runner.py`
- Test: `tools/eval/test_behavior_runner.py`

**Interfaces:**
- Consumes: Task 1 `Case` / `CaseResult` / `ResultHeader` / `RunRecord` / `TurnRecord` / `append_line` / `case_set_hash` / `load_cases` / `FIXTURES_DIR`;Task 3 `FileKey` / `FetchedFile` / `evaluate` / `required_files`;Task 4 `ExternalClient` / `StreamIncompleteError`。
- Produces: `check_base_url(url: str) -> None`(不合规 `SystemExit`);`find_artifact(turns: list[TurnRecord], pattern: str) -> dict[str, Any] | None`;`metrics_of(record: RunRecord) -> dict[str, float]`;`async run_case_once(client, case, *, rep, label, agent_map, fixtures_dir) -> tuple[RunRecord, dict[FileKey, FetchedFile | None]]`;`async run_one(client, case, rep, *, label, agent_map, fixtures_dir) -> CaseResult`;`async run_suite(client, cases, *, header, out_path, repeats, concurrency, agent_map, fixtures_dir=FIXTURES_DIR) -> list[CaseResult]`;`main() -> int`。

- [ ] **Step 1: 写失败的测试**

```python
"""B-140 behavior_runner 单元测试(假客户端,不连真栈)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import behavior_runner as runner
from behavior_client import StreamIncompleteError
from behavior_schema import Case, ResultHeader, RunRecord, TurnRecord, read_results


def _case(**kw: Any) -> Case:
    raw: dict[str, Any] = {
        "id": "g99-x", "agent": "eval-general", "shape": "test", "fixtures": ["a.md"],
        "turns": [{"prompt": "一"}, {"prompt": "二"}],
        "checks": [{"type": "completed"}, {"type": "artifact_contains", "name": "*.md", "all": ["好"]}],
    }
    raw.update(kw)
    return Case.model_validate(raw)


def _turn(index: int, **kw: Any) -> TurnRecord:
    base: dict[str, Any] = {"index": index, "status": "success", "completed": True, "exit_reason": "text_response",
                            "input_tokens": 100, "output_tokens": 10, "wall_s": 1.0}
    base.update(kw)
    return TurnRecord.model_validate(base)


class FakeClient:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.calls: list[tuple[str, Any]] = []

    async def upload(self, agent: str, user_id: str, filename: str, data: bytes, session_id: str | None) -> tuple[str, str]:
        self.calls.append(("upload", (agent, filename, session_id)))
        return "upl_1", "s1"

    async def run_turn(self, agent: str, user_id: str, session_id: str | None, prompt: str, upload_ids: list[str], index: int) -> tuple[TurnRecord, str | None]:
        self.calls.append(("run_turn", (agent, session_id, prompt, list(upload_ids), index)))
        if self.failures:
            raise self.failures.pop(0)
        arts = [{"name": "out.md", "version": 1}] if index == 2 else []
        return _turn(index, artifacts=arts), "s1"

    async def download_artifact(self, agent: str, user_id: str, entry: dict[str, Any]) -> bytes | None:
        self.calls.append(("download", entry["name"]))
        return "好".encode()

    async def read_workspace_file(self, agent: str, user_id: str, path: str) -> bytes | None:
        return None

    async def archive(self, agent: str, user_id: str, session_id: str) -> None:
        self.calls.append(("archive", session_id))


def _fixtures(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text("原文\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_run_case_once_wires_uploads_session_and_archive(tmp_path: Path) -> None:
    client = FakeClient()
    record, files = await runner.run_case_once(
        client, _case(), rep=1, label="base", agent_map={"eval-general": "eval-general-b"}, fixtures_dir=_fixtures(tmp_path)
    )
    runs = [c[1] for c in client.calls if c[0] == "run_turn"]
    assert runs[0] == ("eval-general-b", "s1", "一", ["upl_1"], 1)
    assert runs[1] == ("eval-general-b", "s1", "二", [], 2)
    assert record.session_id == "s1" and record.user_id.startswith("b140-base-g99-x-1-")
    assert files == {("artifact", "*.md"): ("out.md", "好".encode())}
    assert client.calls[-1] == ("archive", "s1")


@pytest.mark.asyncio
async def test_run_one_scores_a_passing_case(tmp_path: Path) -> None:
    res = await runner.run_one(FakeClient(), _case(), 1, label="base", agent_map={}, fixtures_dir=_fixtures(tmp_path))
    assert res.passed is True and [v.passed for v in res.verdicts] == [True, True]
    assert res.metrics == {"tool_calls": 0.0, "wall_s": 2.0, "tokens_in": 200.0, "tokens_out": 20.0}


@pytest.mark.asyncio
async def test_run_one_retries_transport_errors_then_records_infra(tmp_path: Path) -> None:
    fx = _fixtures(tmp_path)
    flaky = FakeClient([httpx.ConnectError("boom"), StreamIncompleteError("cut")])
    assert (await runner.run_one(flaky, _case(), 1, label="b", agent_map={}, fixtures_dir=fx)).passed is True
    dead = FakeClient([httpx.ConnectError("boom")] * 3)
    res = await runner.run_one(dead, _case(), 1, label="b", agent_map={}, fixtures_dir=fx)
    assert res.passed is None and "attempt 3" in (res.infra_error or "")


@pytest.mark.asyncio
async def test_run_one_does_not_retry_4xx(tmp_path: Path) -> None:
    req = httpx.Request("POST", "http://t/v1/agents/eval-general/runs")
    err = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(422, request=req))
    client = FakeClient([err, err, err])
    res = await runner.run_one(client, _case(), 1, label="b", agent_map={}, fixtures_dir=_fixtures(tmp_path))
    assert res.passed is None and "HTTP 422" in (res.infra_error or "")
    assert sum(1 for c in client.calls if c[0] == "run_turn") == 1


@pytest.mark.asyncio
async def test_run_suite_writes_header_and_one_line_per_rep(tmp_path: Path) -> None:
    out = tmp_path / "out" / "r.jsonl"
    header = ResultHeader(label="b", started_at="t", base_url="http://localhost", case_set_hash="h" * 12,
                          repeats=3, case_ids=["g99-x"])
    results = await runner.run_suite(FakeClient(), [_case()], header=header, out_path=out, repeats=3,
                                     concurrency=2, agent_map={}, fixtures_dir=_fixtures(tmp_path))
    got_header, got = read_results(out)
    assert got_header == header
    assert sorted(r.rep for r in got) == [1, 2, 3] and len(results) == 3


def test_check_base_url() -> None:
    runner.check_base_url("https://expert-work-test.deepaihealth.com")
    runner.check_base_url("http://localhost:8000")
    with pytest.raises(SystemExit):
        runner.check_base_url("https://expert-work.deepaihealth.com")


def test_find_artifact_prefers_latest() -> None:
    turns = [_turn(1, artifacts=[{"name": "a.docx", "version": 1}]),
             _turn(2, artifacts=[{"name": "b.docx", "version": 1}, {"name": "c.docx", "version": 1}])]
    assert runner.find_artifact(turns, "*.docx") == {"name": "c.docx", "version": 1}
    assert runner.find_artifact(turns, "*.pptx") is None


def test_metrics_skip_tokens_when_any_turn_lacks_usage() -> None:
    rec = RunRecord(case_id="g99-x", rep=1, user_id="u", turns=[_turn(1), _turn(2, input_tokens=None, output_tokens=None)])
    assert runner.metrics_of(rec) == {"tool_calls": 0.0, "wall_s": 2.0}


def test_main_refuses_missing_token_and_existing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXPERT_WORK_API_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="EXPERT_WORK_API_TOKEN"):
        runner.main(["--label", "x", "--base-url", "http://localhost"])
    monkeypatch.setenv("EXPERT_WORK_API_TOKEN", "test-token")
    out = tmp_path / "x.jsonl"
    out.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="exists"):
        runner.main(["--label", "x", "--base-url", "http://localhost", "--out", str(out)])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_runner.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'behavior_runner'`

- [ ] **Step 3: 实现 `tools/eval/behavior_runner.py`**

```python
"""B-140 跑整套:每个用例的每次重复,在测试环境真跑,结果逐行写 JSONL。

用法(key 只经环境变量,不进命令行):
  EXPERT_WORK_API_TOKEN=… uv run --no-sync python tools/eval/behavior_runner.py --label base \\
      [--only g02-edit-three-places,g08-group-mean] [--repeats 3] [--concurrency 4] \\
      [--agent-map eval-general=eval-general-broken] [--note "test 5091938c"]

只允许测试环境或本机(规格 §8)。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # 从仓库根目录以脚本方式运行
    sys.path.insert(0, str(_HERE))

from behavior_checks import FetchedFile, FileKey, evaluate, required_files  # noqa: E402
from behavior_client import ExternalClient, StreamIncompleteError  # noqa: E402
from behavior_schema import (  # noqa: E402
    FIXTURES_DIR,
    Case,
    CaseResult,
    ResultHeader,
    RunRecord,
    TurnRecord,
    append_line,
    case_set_hash,
    load_cases,
)

DEFAULT_BASE_URL = "https://expert-work-test.deepaihealth.com"
_ATTEMPTS = 3


class Client(Protocol):
    async def upload(
        self, agent: str, user_id: str, filename: str, data: bytes, session_id: str | None
    ) -> tuple[str, str]: ...

    async def run_turn(
        self, agent: str, user_id: str, session_id: str | None, prompt: str, upload_ids: list[str], index: int
    ) -> tuple[TurnRecord, str | None]: ...

    async def download_artifact(self, agent: str, user_id: str, entry: dict[str, Any]) -> bytes | None: ...

    async def read_workspace_file(self, agent: str, user_id: str, path: str) -> bytes | None: ...

    async def archive(self, agent: str, user_id: str, session_id: str) -> None: ...


def check_base_url(url: str) -> None:
    host = urlparse(url).hostname or ""
    if not (host in {"localhost", "127.0.0.1"} or "-test." in host):
        raise SystemExit(f"refusing to run against {host!r}: B-140 runs only on the test env or localhost")


def find_artifact(turns: list[TurnRecord], pattern: str) -> dict[str, Any] | None:
    """最后一轮里最后登记的、名字匹配通配符的产物。"""
    for turn in reversed(turns):
        for entry in reversed(turn.artifacts):
            if entry.get("name") and fnmatch.fnmatchcase(str(entry["name"]), pattern):
                return entry
    return None


def metrics_of(record: RunRecord) -> dict[str, float]:
    out = {
        "tool_calls": float(sum(len(t.tool_calls) for t in record.turns)),
        "wall_s": round(sum(t.wall_s for t in record.turns), 1),
    }
    # 任何一轮没有用量就不出 token 指标 —— 半截求和比没有更误导。
    if record.turns and all(t.input_tokens is not None for t in record.turns):
        out["tokens_in"] = float(sum(t.input_tokens or 0 for t in record.turns))
    if record.turns and all(t.output_tokens is not None for t in record.turns):
        out["tokens_out"] = float(sum(t.output_tokens or 0 for t in record.turns))
    return out


async def run_case_once(
    client: Client,
    case: Case,
    *,
    rep: int,
    label: str,
    agent_map: dict[str, str],
    fixtures_dir: Path,
) -> tuple[RunRecord, dict[FileKey, FetchedFile | None]]:
    agent = agent_map.get(case.agent, case.agent)
    # 每次一个新 user_id:工作区按「用户 + 智能体」划分,复用会把上一次的 PLAN.md 带进来。
    user_id = f"b140-{label}-{case.id}-{rep}-{uuid.uuid4().hex[:6]}"
    session_id: str | None = None
    try:
        upload_ids: list[str] = []
        for name in case.fixtures:
            upload_id, session_id = await client.upload(
                agent, user_id, name, (fixtures_dir / name).read_bytes(), session_id
            )
            upload_ids.append(upload_id)
        turns: list[TurnRecord] = []
        for index, turn in enumerate(case.turns, start=1):
            record, sid = await client.run_turn(
                agent, user_id, session_id, turn.prompt, upload_ids if index == 1 else [], index
            )
            session_id = session_id or sid
            turns.append(record)
        files: dict[FileKey, FetchedFile | None] = {}
        for kind, name in required_files(case):
            if kind == "artifact":
                entry = find_artifact(turns, name)
                data = await client.download_artifact(agent, user_id, entry) if entry else None
                files[(kind, name)] = (str(entry["name"]), data) if entry and data is not None else None
            else:
                data = await client.read_workspace_file(agent, user_id, name)
                files[(kind, name)] = (name, data) if data is not None else None
        return RunRecord(case_id=case.id, rep=rep, user_id=user_id, session_id=session_id, turns=turns), files
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                await client.archive(agent, user_id, session_id)


async def run_one(
    client: Client,
    case: Case,
    rep: int,
    *,
    label: str,
    agent_map: dict[str, str],
    fixtures_dir: Path,
) -> CaseResult:
    last_error = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            record, files = await run_case_once(
                client, case, rep=rep, label=label, agent_map=agent_map, fixtures_dir=fixtures_dir
            )
        except (httpx.TransportError, StreamIncompleteError) as exc:
            last_error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
            continue
        except httpx.HTTPStatusError as exc:
            last_error = f"attempt {attempt}: HTTP {exc.response.status_code} {exc.request.url.path}"
            if exc.response.status_code < 500:
                break  # 4xx 是用例或配置错,重试没用
            continue
        verdicts = evaluate(case, record, files, fixtures_dir)
        return CaseResult(
            case_id=case.id,
            rep=rep,
            passed=all(v.passed for v in verdicts),
            verdicts=verdicts,
            metrics=metrics_of(record),
            session_id=record.session_id,
        )
    return CaseResult(case_id=case.id, rep=rep, passed=None, infra_error=last_error)


async def run_suite(
    client: Client,
    cases: list[Case],
    *,
    header: ResultHeader,
    out_path: Path,
    repeats: int,
    concurrency: int,
    agent_map: dict[str, str],
    fixtures_dir: Path = FIXTURES_DIR,
) -> list[CaseResult]:
    append_line(out_path, header)
    gate = asyncio.Semaphore(concurrency)
    results: list[CaseResult] = []

    async def job(case: Case, rep: int) -> None:
        async with gate:
            res = await run_one(client, case, rep, label=header.label, agent_map=agent_map, fixtures_dir=fixtures_dir)
        append_line(out_path, res)  # 单线程事件循环,两次 await 之间的同步写不会交错
        results.append(res)
        mark = "INFRA" if res.passed is None else ("PASS" if res.passed else "FAIL")
        failed = [v.type for v in res.verdicts if not v.passed]
        print(f"{mark:5} {case.id} #{rep} {failed or ''} {res.infra_error or ''}".rstrip(), flush=True)

    await asyncio.gather(*(job(c, r) for c in cases for r in range(1, repeats + 1)))
    return results


def _agent_map(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        src, sep, dst = item.partition("=")
        if not sep or not src or not dst:
            raise SystemExit(f"--agent-map wants FROM=TO, got {item!r}")
        out[src] = dst
    return out


async def _amain(args: argparse.Namespace, token: str, out: Path) -> int:
    cases = load_cases(only=args.only.split(",") if args.only else None)
    agent_map = _agent_map(args.agent_map)
    header = ResultHeader(
        label=args.label,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        base_url=args.base_url,
        note=args.note,
        agent_map=agent_map,
        case_set_hash=case_set_hash(),
        repeats=args.repeats,
        case_ids=[c.id for c in cases],
    )
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, read=600.0),
    ) as http:
        results = await run_suite(
            ExternalClient(http), cases, header=header, out_path=out,
            repeats=args.repeats, concurrency=args.concurrency, agent_map=agent_map,
        )
    passed = sum(r.passed is True for r in results)
    infra = sum(r.passed is None for r in results)
    print(f"done: {passed}/{len(results)} passed, {infra} infra errors -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="本次运行的名字,如 base / cand-5091938c")
    parser.add_argument("--base-url", default=os.environ.get("EXPERT_WORK_API_URL", DEFAULT_BASE_URL))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", default="", help="逗号分隔的用例 id")
    parser.add_argument("--agent-map", action="append", default=[], help="FROM=TO,换用另一个评测智能体")
    parser.add_argument("--note", default="", help="写进结果头,如测试环境镜像 tag")
    parser.add_argument("--out", default="", help="默认 eval-out/b140/<label>.jsonl")
    args = parser.parse_args(argv)
    check_base_url(args.base_url)
    token = os.environ.get("EXPERT_WORK_API_TOKEN")
    if not token:
        raise SystemExit("set EXPERT_WORK_API_TOKEN (read it from the eval-credentials Secret into the env)")
    out = Path(args.out or f"eval-out/b140/{args.label}.jsonl")
    if out.exists():
        raise SystemExit(f"{out} exists; pick a new --label or --out")
    return asyncio.run(_amain(args, token, out))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_runner.py -q`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add tools/eval/behavior_runner.py tools/eval/test_behavior_runner.py
git commit -m "feat(eval): B-140 suite runner (retries infra errors, JSONL results, test-env only)"
```

---

### Task 6: 对照报告

**Files:**
- Create: `tools/eval/behavior_compare.py`
- Test: `tools/eval/test_behavior_compare.py`

**Interfaces:**
- Consumes: Task 1 `CaseResult` / `ResultHeader` / `read_results`。
- Produces: `CaseSummary`(dataclass: `case_id, n, passed, infra, medians`);`summarize(results) -> dict[str, CaseSummary]`;`classify(base, cand) -> Verdict`(`"regressed" | "improved" | "same" | "flaky" | "insufficient" | "missing"`);`metric_flags(base, cand) -> list[str]`;`Row`(dataclass: `case_id, base, cand, verdict, flags`);`compare(base_header, base_results, cand_header, cand_results, *, allow_different_cases=False) -> list[Row]`;`render_markdown(base_header, cand_header, rows, cand_results) -> str`;`main(argv=None) -> int`(有稳定退步 1、用例集不同 2、否则 0)。

- [ ] **Step 1: 写失败的测试**

```python
"""B-140 behavior_compare 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

import behavior_compare as cmp
from behavior_schema import CaseResult, CheckVerdict, ResultHeader, append_line


def _results(case_id: str, outcomes: list[bool | None], tokens: float = 1000.0) -> list[CaseResult]:
    out = []
    for rep, ok in enumerate(outcomes, start=1):
        verdicts = [] if ok is None else [CheckVerdict(type="tool_used", passed=ok, detail="" if ok else "tools used: []")]
        out.append(CaseResult(case_id=case_id, rep=rep, passed=ok, verdicts=verdicts,
                              metrics={} if ok is None else {"tokens_in": tokens, "tool_calls": 3.0},
                              infra_error="cut" if ok is None else None))
    return out


def _header(label: str, h: str = "a" * 12) -> ResultHeader:
    return ResultHeader(label=label, started_at="t", base_url="http://localhost", case_set_hash=h, repeats=3, case_ids=[])


def test_summarize_excludes_infra_and_takes_medians() -> None:
    s = cmp.summarize(_results("g01", [True, None, False], tokens=900.0))["g01"]
    assert (s.n, s.passed, s.infra) == (2, 1, 1)
    assert s.medians == {"tokens_in": 900.0, "tool_calls": 3.0}


@pytest.mark.parametrize(("base", "cand", "verdict"), [
    ([True] * 3, [False] * 3, "regressed"),
    ([True, True, False], [True, False, False], "regressed"),
    ([True] * 3, [True, True, False], "flaky"),
    ([False] * 3, [True] * 3, "improved"),
    ([True, True, False], [False, True, True], "same"),
    ([True, None, None], [True] * 3, "insufficient"),
])
def test_classify(base: list[bool | None], cand: list[bool | None], verdict: str) -> None:
    b = cmp.summarize(_results("g01", base))["g01"]
    c = cmp.summarize(_results("g01", cand))["g01"]
    assert cmp.classify(b, c) == verdict


def test_classify_missing() -> None:
    b = cmp.summarize(_results("g01", [True] * 3))["g01"]
    assert cmp.classify(b, None) == "missing"


def test_metric_flags_threshold() -> None:
    b = cmp.summarize(_results("g01", [True] * 3, tokens=1000.0))["g01"]
    assert cmp.metric_flags(b, cmp.summarize(_results("g01", [True] * 3, tokens=1350.0))["g01"]) == ["tokens_in +35%"]
    assert cmp.metric_flags(b, cmp.summarize(_results("g01", [True] * 3, tokens=1100.0))["g01"]) == []


def test_compare_rejects_different_case_sets() -> None:
    with pytest.raises(ValueError, match="case sets differ"):
        cmp.compare(_header("a"), [], _header("b", "b" * 12), [])
    assert cmp.compare(_header("a"), [], _header("b", "b" * 12), [], allow_different_cases=True) == []


def test_main_exit_codes_and_report(tmp_path: Path) -> None:
    def write(name: str, header: ResultHeader, results: list[CaseResult]) -> Path:
        p = tmp_path / name
        append_line(p, header)
        for r in results:
            append_line(p, r)
        return p

    base = write("base.jsonl", _header("base"), _results("g01", [True] * 3) + _results("g02", [True] * 3))
    same = write("same.jsonl", _header("same"), _results("g01", [True] * 3) + _results("g02", [True] * 3))
    worse = write("worse.jsonl", _header("worse"), _results("g01", [True] * 3) + _results("g02", [False] * 3))
    other = write("other.jsonl", _header("other", "c" * 12), [])
    report = tmp_path / "r.md"
    assert cmp.main([str(base), str(same)]) == 0
    assert cmp.main([str(base), str(worse), "--out", str(report)]) == 1
    text = report.read_text(encoding="utf-8")
    assert "| g02 | 3/3 | 0/3 | ❌ 稳定退步 |" in text
    assert "g02 第 1 次:不过: tool_used(tools used: [])" in text
    assert cmp.main([str(base), str(other)]) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_compare.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'behavior_compare'`

- [ ] **Step 3: 实现 `tools/eval/behavior_compare.py`**

```python
"""B-140 对照:两份结果 → Markdown 对照表。

  uv run --no-sync python tools/eval/behavior_compare.py eval-out/b140/base.jsonl eval-out/b140/cand.jsonl \\
      [--out report.md] [--allow-different-cases]

退出码:有稳定退步 1;用例集不同(且没加 --allow-different-cases)2;否则 0。
规则见规格 §6:改动前 ≥2/3 过、改动后 ≤1/3 过才算稳定退步;infra_error 不计入。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from behavior_schema import CaseResult, ResultHeader, read_results  # noqa: E402

Verdict = Literal["regressed", "improved", "same", "flaky", "insufficient", "missing"]
METRICS = ("tokens_in", "tokens_out", "tool_calls", "wall_s")
METRIC_THRESHOLD = 0.20
_LABEL: dict[Verdict, str] = {
    "regressed": "❌ 稳定退步",
    "improved": "✅ 稳定进步",
    "same": "持平",
    "flaky": "抖动",
    "insufficient": "样本不足",
    "missing": "缺失",
}


@dataclass(frozen=True)
class CaseSummary:
    case_id: str
    n: int  # 不含 infra_error
    passed: int
    infra: int
    medians: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Row:
    case_id: str
    base: CaseSummary | None
    cand: CaseSummary | None
    verdict: Verdict
    flags: list[str]


def summarize(results: list[CaseResult]) -> dict[str, CaseSummary]:
    by_case: dict[str, list[CaseResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)
    out: dict[str, CaseSummary] = {}
    for case_id, rows in by_case.items():
        scored = [r for r in rows if r.passed is not None]
        medians: dict[str, float] = {}
        for metric in METRICS:
            values = [r.metrics[metric] for r in scored if metric in r.metrics]
            if values:
                medians[metric] = float(statistics.median(values))
        out[case_id] = CaseSummary(
            case_id=case_id,
            n=len(scored),
            passed=sum(r.passed is True for r in scored),
            infra=len(rows) - len(scored),
            medians=medians,
        )
    return out


def classify(base: CaseSummary | None, cand: CaseSummary | None) -> Verdict:
    if base is None or cand is None:
        return "missing"
    if base.n < 2 or cand.n < 2:
        return "insufficient"
    base_good, base_bad = 3 * base.passed >= 2 * base.n, 3 * base.passed <= base.n
    cand_good, cand_bad = 3 * cand.passed >= 2 * cand.n, 3 * cand.passed <= cand.n
    if base_good and cand_bad:
        return "regressed"
    if base_bad and cand_good:
        return "improved"
    if base.passed * cand.n == cand.passed * base.n:
        return "same"
    return "flaky"


def metric_flags(base: CaseSummary, cand: CaseSummary) -> list[str]:
    flags = []
    for metric in METRICS:
        if metric in base.medians and metric in cand.medians and base.medians[metric] > 0:
            delta = (cand.medians[metric] - base.medians[metric]) / base.medians[metric]
            if abs(delta) >= METRIC_THRESHOLD:
                flags.append(f"{metric} {delta:+.0%}")
    return flags


def compare(
    base_header: ResultHeader,
    base_results: list[CaseResult],
    cand_header: ResultHeader,
    cand_results: list[CaseResult],
    *,
    allow_different_cases: bool = False,
) -> list[Row]:
    if base_header.case_set_hash != cand_header.case_set_hash and not allow_different_cases:
        raise ValueError(
            f"case sets differ ({base_header.case_set_hash} vs {cand_header.case_set_hash}); "
            "pass --allow-different-cases to compare anyway"
        )
    base, cand = summarize(base_results), summarize(cand_results)
    rows = []
    for case_id in sorted(set(base) | set(cand)):
        b, c = base.get(case_id), cand.get(case_id)
        rows.append(Row(case_id, b, c, classify(b, c), metric_flags(b, c) if b and c else []))
    return rows


def _cell(s: CaseSummary | None) -> str:
    if s is None:
        return "—"
    return f"{s.passed}/{s.n}" + (f"(另 {s.infra} 次环境错误)" if s.infra else "")


def render_markdown(
    base_header: ResultHeader, cand_header: ResultHeader, rows: list[Row], cand_results: list[CaseResult]
) -> str:
    lines = [
        "# B-140 行为回归对照",
        "",
        "| | 改动前 | 改动后 |",
        "|---|---|---|",
        f"| 标签 | {base_header.label} | {cand_header.label} |",
        f"| 时间 | {base_header.started_at} | {cand_header.started_at} |",
        f"| 备注 | {base_header.note} | {cand_header.note} |",
        f"| 智能体替换 | {base_header.agent_map or '—'} | {cand_header.agent_map or '—'} |",
        f"| 用例集 | {base_header.case_set_hash} | {cand_header.case_set_hash} |",
        f"| 每个用例次数 | {base_header.repeats} | {cand_header.repeats} |",
        "",
    ]
    counts = Counter(r.verdict for r in rows)
    lines.append("结论:" + "、".join(f"{_LABEL[v]} {counts[v]}" for v in _LABEL if counts[v]))
    lines += ["", "| 用例 | 改动前 | 改动后 | 结论 | 指标变化(中位数,≥20%) |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.case_id} | {_cell(r.base)} | {_cell(r.cand)} | {_LABEL[r.verdict]} | {'、'.join(r.flags)} |")
    detail = [r for r in rows if r.verdict in ("regressed", "flaky")]
    if detail:
        lines += ["", "## 改动后没过的判据", ""]
        for r in detail:
            for res in sorted((x for x in cand_results if x.case_id == r.case_id), key=lambda x: x.rep):
                if res.passed is None:
                    state = f"环境错误 {res.infra_error or ''}"
                elif res.passed:
                    state = "过"
                else:
                    state = "不过: " + "; ".join(f"{v.type}({v.detail})" for v in res.verdicts if not v.passed)
                lines.append(f"- {r.case_id} 第 {res.rep} 次:{state}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base")
    parser.add_argument("cand")
    parser.add_argument("--out", default="")
    parser.add_argument("--allow-different-cases", action="store_true")
    args = parser.parse_args(argv)
    base_header, base_results = read_results(Path(args.base))
    cand_header, cand_results = read_results(Path(args.cand))
    try:
        rows = compare(
            base_header, base_results, cand_header, cand_results, allow_different_cases=args.allow_different_cases
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    report = render_markdown(base_header, cand_header, rows, cand_results)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 1 if any(r.verdict == "regressed" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_compare.py -q`
Expected: 11 passed

- [ ] **Step 5: 变异自证 + 提交**

```bash
git add tools/eval/behavior_compare.py tools/eval/test_behavior_compare.py
git commit -m "feat(eval): B-140 compare two result files, stable flips only"
S=/private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad
F=tools/eval/behavior_compare.py
python3 $S/mut.py $F "if base.n < 2 or cand.n < 2:" "if False:"
git diff --stat   # 1 file changed
UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_compare.py -q -k classify   # 期望 insufficient 那条红
python3 $S/mut.py $F "if False:" "if base.n < 2 or cand.n < 2:"
git status --porcelain   # 期望为空
```

---

### Task 7: 评测智能体、fixtures、3 个样板用例、文档

**Files:**
- Create: `tools/eval/datasets/behavior/agents/eval-general.yaml`
- Create: `tools/eval/datasets/behavior/agents/eval-ahp.yaml`
- Create: `tools/eval/datasets/behavior/fixtures/make_fixtures.py`
- Create(生成):`tools/eval/datasets/behavior/fixtures/report-300-lines.md`、`tools/eval/datasets/behavior/fixtures/sales.csv`
- Create: `tools/eval/datasets/behavior/cases/g02-edit-three-places.yaml`、`g08-group-mean.yaml`、`h01-word-plan.yaml`
- Test: `tools/eval/test_behavior_dataset.py`
- Modify: `tools/eval/README.md`(加「B-140 行为回归评测」一节)
- Modify: `docs/superpowers/ROADMAP.md`(B-140 行)、`docs/superpowers/plans/2026-10-02-harness-quality-program.md`(总表 B-140 行 + 进度记录)

**Interfaces:**
- Consumes: Task 1 `load_cases` / `CASES_DIR` / `FIXTURES_DIR` / `AGENTS_DIR`;Task 3 `required_files`。

- [ ] **Step 1: 写失败的数据集测试**

```python
"""B-140 数据集自检:用例都能加载、fixtures 与智能体都在、fixtures 与生成脚本一致。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import yaml

from behavior_schema import AGENTS_DIR, CASES_DIR, FIXTURES_DIR, load_cases


def _make_fixtures_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES_DIR / "make_fixtures.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_cases_load_and_reference_existing_files() -> None:
    cases = load_cases(CASES_DIR)
    assert {c.id for c in cases} >= {"g02-edit-three-places", "g08-group-mean", "h01-word-plan"}
    for case in cases:
        for name in case.fixtures:
            assert (FIXTURES_DIR / name).is_file(), (case.id, name)
        for check in case.checks:
            fixture = getattr(check, "fixture", None)
            if fixture:
                assert (FIXTURES_DIR / fixture).is_file(), (case.id, fixture)


def test_agent_manifests_match_codes() -> None:
    for code in ("eval-general", "eval-ahp"):
        manifest = yaml.safe_load((AGENTS_DIR / f"{code}.yaml").read_text(encoding="utf-8"))
        assert manifest["metadata"]["name"] == code
        assert manifest["spec"]["sandbox"]["filesystem"]["persistent_workspace"] is True
        assert manifest["spec"]["policies"]["token_budget"] > 0


def test_committed_fixtures_match_generator() -> None:
    built = _make_fixtures_module().build()
    for name, text in built.items():
        assert (FIXTURES_DIR / name).read_text(encoding="utf-8") == text, name


def test_g02_fixture_has_the_three_outdated_lines_where_the_case_says() -> None:
    lines = (FIXTURES_DIR / "report-300-lines.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 300
    assert "2026 年 6 月" in lines[11] and "987 人" in lines[139] and "张工" in lines[287]


def test_no_coach_wording_in_cases() -> None:
    for path in Path(CASES_DIR).glob("*.yaml"):
        assert "教练" not in path.read_text(encoding="utf-8").replace("any: [教练]", ""), path.name
```

- [ ] **Step 2: 跑测试确认失败**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_dataset.py -q`
Expected: FAIL(目录与文件不存在)

- [ ] **Step 3: 写 fixtures 生成脚本并生成**

`tools/eval/datasets/behavior/fixtures/make_fixtures.py`:

```python
"""生成 B-140 开局文件(全部合成数据)。改了这里要重跑,并把生成的文件一起提交:

  uv run --no-sync python tools/eval/datasets/behavior/fixtures/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent

# g02:三处过时内容在第 12 / 140 / 288 行(用例的 except_lines 与之对应)。
_SPECIAL = {
    12: "012. 数据截至 2026 年 6 月,以下各项按当月巡检记录整理。",
    140: "140. 本季度注册用户数为 987 人,较上季度持平。",
    288: "288. 本报告负责人:张工,如有疑问请联系负责人。",
}


def _report() -> str:
    lines = ["# 设备巡检报告(合成数据)"]
    for i in range(2, 301):
        lines.append(_SPECIAL.get(i, f"{i:03d}. 第 {i} 项设备巡检结果正常,无需处理。"))
    return "\n".join(lines) + "\n"


def _sales() -> str:
    # g08:A 组均值 104.5,B 组 209.0,C 组 35.5。
    rows = ["region,amount"]
    rows += [f"A,{v}" for v in range(100, 110)]
    rows += [f"B,{v}" for v in range(200, 220, 2)]
    rows += [f"C,{v}" for v in range(31, 41)]
    return "\n".join(rows) + "\n"


def build() -> dict[str, str]:
    return {"report-300-lines.md": _report(), "sales.csv": _sales()}


if __name__ == "__main__":
    for name, text in build().items():
        (HERE / name).write_text(text, encoding="utf-8")
        print(f"wrote {name}")
```

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync python tools/eval/datasets/behavior/fixtures/make_fixtures.py`
Expected: `wrote report-300-lines.md`、`wrote sales.csv`

- [ ] **Step 4: 写两个评测智能体 manifest**

`tools/eval/datasets/behavior/agents/eval-general.yaml`:

```yaml
# B-140 行为回归评测 —— 通用智能体。只绑平台能力,不接任何业务系统。
# 由用户在测试环境评测租户 b140-eval 的控制台导入(规格 §8)。
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: eval-general
  version: "1.0.0"
  tenant: b140-eval
spec:
  description: B-140 行为回归评测用的通用智能体(合成任务专用)。
  tenant_config:
    isolation_level: dedicated_sandbox
  model:
    provider: glm
    name: glm-5.3
    temperature: 0.2
    max_tokens: 8000
  system_prompt:
    template: |
      你是一个通用的办公助手,在工作区里读写文件、运行脚本,完成用户交代的任务。
      做完后用一两句话说明结果;做不到的事情如实说明原因,不要编造。
  tools:
    - { type: builtin, name: read_file }
    - { type: builtin, name: write_file }
    - { type: builtin, name: edit_file }
    - { type: builtin, name: search_files }
    - { type: builtin, name: exec_python }
    - { type: builtin, name: bash }
    - { type: builtin, name: save_artifact }
  policies:
    token_budget: 400000
  sandbox:
    runtime: gvisor
    resources:
      cpu: "0.5"
      memory: 512Mi
    network:
      egress: none
    filesystem:
      readonly_root: true
      writable:
        - /tmp
      persistent_workspace: true
```

`tools/eval/datasets/behavior/agents/eval-ahp.yaml`:

```yaml
# B-140 行为回归评测 —— 模仿 ai-health-plan 形态的智能体(出健康方案文档、多轮修改)。
# 只绑平台技能,不接对接方的业务 MCP;客户资料由用例在提示词里给出(全部虚构)。
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: eval-ahp
  version: "1.0.0"
  tenant: b140-eval
spec:
  description: B-140 行为回归评测用的健康方案智能体(合成任务专用)。
  tenant_config:
    isolation_level: dedicated_sandbox
  model:
    provider: glm
    name: glm-5.3
    temperature: 0.2
    max_tokens: 8000
  system_prompt:
    template: |
      你是健康管理方案助手。使用你的人是健康管理机构的员工,方案的对象是员工服务的客户。
      根据员工给出的客户资料出方案文档时,按 health-plan-report 技能的步骤来做,成品用 save_artifact 交付。
      资料里没有的信息不要编造,在文档里标出缺项;做不到的事情如实说明。
  skills: [health-plan-report, docx, pptx, pdf]
  tools:
    - { type: builtin, name: read_file }
    - { type: builtin, name: write_file }
    - { type: builtin, name: edit_file }
    - { type: builtin, name: read_document }
    - { type: builtin, name: exec_python }
    - { type: builtin, name: bash }
    - { type: builtin, name: save_artifact }
    - { type: builtin, name: list_artifacts }
  policies:
    token_budget: 800000
  sandbox:
    runtime: gvisor
    resources:
      cpu: "1"
      memory: 1Gi
    network:
      egress: none
    filesystem:
      readonly_root: true
      writable:
        - /tmp
      persistent_workspace: true
```

- [ ] **Step 5: 写 3 个样板用例**

`tools/eval/datasets/behavior/cases/g02-edit-three-places.yaml`:

```yaml
id: g02-edit-three-places
agent: eval-general
shape: B-137
fixtures: [report-300-lines.md]
turns:
  - prompt: |
      工作区的 uploads/report-300-lines.md 是一份巡检报告。请先用 bash 执行
      `cp uploads/report-300-lines.md report.md`,然后只修改 report.md 里过时的三处,其余内容一个字都不要动:
      1. 「数据截至 2026 年 6 月」改成「数据截至 2026 年 9 月」;
      2. 「注册用户数为 987 人」改成「注册用户数为 1,234 人」;
      3. 「负责人:张工」改成「负责人:李工」。
checks:
  - {type: completed}
  - {type: workspace_file_contains, path: report.md, all: ["数据截至 2026 年 9 月", "注册用户数为 1,234 人", "负责人:李工"]}
  - {type: workspace_file_unchanged_lines, path: report.md, fixture: report-300-lines.md, except_lines: [12, 140, 288]}
  - {type: tool_used, tool: edit_file}
  - {type: tool_not_used_on, tool: write_file, path: report.md}
```

`tools/eval/datasets/behavior/cases/g08-group-mean.yaml`:

```yaml
id: g08-group-mean
agent: eval-general
shape: 沙箱 exec_python 基本链路
fixtures: [sales.csv]
turns:
  - prompt: |
      工作区的 uploads/sales.csv 有 region 与 amount 两列。用 exec_python 按 region 分组计算 amount 的平均值。
      回复的最后一行只写结果,格式为 `A=…, B=…, C=…`,保留一位小数。
checks:
  - {type: completed}
  - {type: tool_used, tool: exec_python}
  - {type: final_text_contains, all: ["A=104.5", "B=209.0", "C=35.5"]}
```

`tools/eval/datasets/behavior/cases/h01-word-plan.yaml`:

```yaml
id: h01-word-plan
agent: eval-ahp
shape: ai-health-plan 主流程
turns:
  - prompt: |
      请为下面这位客户出一份 Word 版健康管理方案,用 save_artifact 交付,文件名用「王小雨-健康方案.docx」。
      客户资料(虚构):王小雨,女,42 岁,身高 162 cm,体重 68 kg,空腹血糖 6.4 mmol/L,
      平时久坐、晚睡;诉求是三个月内减重 5 kg、把空腹血糖降到 6.1 以下。
      方案至少包括饮食、运动、作息三部分。
checks:
  - {type: completed}
  - {type: artifact_exists, name: "*.docx"}
  - {type: artifact_contains, name: "*.docx", all: ["王小雨", "饮食", "运动", "作息"]}
  - {type: artifact_not_contains, name: "*.docx", any: [教练]}
```

- [ ] **Step 6: 跑数据集测试与全部 behavior 测试**

Run: `UV_PROJECT_ENVIRONMENT=/Users/mac/src/github/jone_qian/expert-work/.venv uv run --no-sync pytest tools/eval/test_behavior_*.py -q`
Expected: 全部通过(Task 1–6 的 70 条 + 本任务 5 条)

再跑一次 lint:

Run: `/Users/mac/src/github/jone_qian/expert-work/.venv/bin/ruff check tools/eval/behavior_*.py tools/eval/test_behavior_*.py tools/eval/datasets/behavior/fixtures/make_fixtures.py && /Users/mac/src/github/jone_qian/expert-work/.venv/bin/ruff format --check tools/eval/behavior_*.py tools/eval/test_behavior_*.py tools/eval/datasets/behavior/fixtures/make_fixtures.py`
Expected: 无报错(有格式差异就 `ruff format` 后重跑测试)

- [ ] **Step 7: 文档**

`tools/eval/README.md` 末尾追加:

````markdown
## B-140 行为回归评测(在测试环境真跑)

设计:`docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md`。用例在
`datasets/behavior/cases/`(全部合成),两个评测智能体在 `datasets/behavior/agents/`。

一次性准备(平台管理员,测试环境):建租户 `b140-eval` → 建服务账号与 `write` 档 API key →
存进集群 Secret `eval-credentials`(键 `api-key`)→ 控制台导入两个评测智能体 manifest。

跑一遍(key 只进环境变量):

```sh
export KUBECONFIG=~/.kube/expert-work-test.yaml
EXPERT_WORK_API_TOKEN="$(kubectl -n expert-work get secret eval-credentials -o jsonpath='{.data.api-key}' | base64 -d)" \
  uv run --no-sync python tools/eval/behavior_runner.py --label base-$(date +%m%d) --note "test <镜像 tag>"
```

对照两次:

```sh
uv run --no-sync python tools/eval/behavior_compare.py eval-out/b140/<改动前>.jsonl eval-out/b140/<改动后>.jsonl --out report.md
```

退出码 1 = 有稳定退步(改动前 ≥2/3 过、改动后 ≤1/3 过)。只换提示词 / 工具 / 模型时,不用发版:建一个改过的评测智能体副本(另一个 code),`--agent-map eval-ahp=eval-ahp-b`。
````

ROADMAP B-140 行把「🔄 设计稿 10-05 已交」后面补「;PR-1 框架 + 3 个样板用例 #<PR 号>」;专项计划总表 B-140 行「时间 / 下一步」改为「PR-1(框架 + g02 / g08 / h01)提交;等评测租户准备好后在测试环境跑样板用例,再做 PR-2 其余 21 个用例 + 首次基线 + 自证」,进度记录加一行 2026-10-05。

- [ ] **Step 8: 提交**

```bash
git add tools/eval/datasets/behavior tools/eval/test_behavior_dataset.py tools/eval/README.md docs/superpowers/ROADMAP.md docs/superpowers/plans/2026-10-02-harness-quality-program.md
git commit -m "feat(eval): B-140 eval agents, fixtures and three sample cases"
```

---

### Task 8: 测试环境跑样板用例(依赖评测租户准备好)

**前置**:用户已完成 README「一次性准备」四步。没完成就跳过本任务,在 PR 正文写明「真跑待评测租户」。

- [ ] **Step 1: 单次冒烟**

```sh
export KUBECONFIG=~/.kube/expert-work-test.yaml
EXPERT_WORK_API_TOKEN="$(kubectl -n expert-work get secret eval-credentials -o jsonpath='{.data.api-key}' | base64 -d)" \
  uv run --no-sync python tools/eval/behavior_runner.py --label smoke-1 --repeats 1 --note "test <当前镜像 tag>"
```

Expected:3 行 `PASS`/`FAIL`、0 行 `INFRA`;`eval-out/b140/smoke-1.jsonl` 有 1 行头 + 3 行结果。`FAIL` 要逐条看 `verdicts` 判断是用例写错还是模型真没做到:用例写错(路径、文件名、判据过严)就修用例重跑;模型没做到就如实记录。

- [ ] **Step 2: 判据能红(端到端)**

在评测租户控制台建 `eval-general-broken`(复制 `eval-general.yaml`,`metadata.name` 改名、`tools` 删掉 `edit_file`),然后:

```sh
EXPERT_WORK_API_TOKEN=… uv run --no-sync python tools/eval/behavior_runner.py --label smoke-broken --repeats 1 \
  --only g02-edit-three-places --agent-map eval-general=eval-general-broken
```

Expected:g02 判 `FAIL`,失败判据里有 `tool_used`。跑完在控制台删掉 `eval-general-broken`。

- [ ] **Step 3: 结果写进 PR 正文与 ROADMAP B-140 行**(通过数、单次耗时、token 中位数),提交并推送。
