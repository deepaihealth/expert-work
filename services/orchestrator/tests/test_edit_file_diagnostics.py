"""B-302 PR 3 —— ``edit_file`` 失败时说清楚(spec §4.3,测试编号对应 §4.3.7)。

事故:20 次 ``edit_file`` 里 7 次 ``no_match``,都在同一个脚本上;一次调用里的几处改动
因为第一处没对上整批作废,模型再凭记忆重发。这里钉住:

* 失败时整张清单都试一遍(只看不写),说出哪几处本来对得上、哪几处没法判断(T3-1/2);
* 最像的 3 行、按原行(含缩进)算的列号、按码点计、片段不切断字形(T3-3/4/5);
* 「已经改过了」的 6 个条件,只对调用前的原文判,外加最后一次复核(T3-6/7/7b);
* 本轮自己改掉的旧文字(T3-8)、同一文件连续失败(T3-9);
* 分类按错误种类,不扫引用进来的文件内容(T3-10);错误文字上限 1,500(T3-11);
* 平台自己写的文字里没有 "not found"(T3-12)。

所有文件内容都是合成的,与任何租户无关;覆盖中文、西里尔字母、带重音的拉丁字母、
组合重音与多码点 emoji。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.tools import (
    EditFileTool,
    FileOpError,
    RecordingSandboxRuntime,
    SandboxOutcome,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from orchestrator.tools.error_classifier import classify_tool_error
from orchestrator.tools.file_ops import _raise_for_error, build_edit_wrapper, build_edits_wrapper

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_snippet(code: str) -> dict[str, Any]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})  # noqa: S102 — snippet is built from a fixed template
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def _edits(*pairs: tuple[str, str], replace_all: bool = False) -> list[dict[str, Any]]:
    return [{"old": o, "new": n, "replace_all": replace_all} for o, n in pairs]


def _render(env: Mapping[str, Any]) -> str:
    """The text the model reads for a failed ``edit_file`` envelope."""
    with pytest.raises(FileOpError) as excinfo:
        _raise_for_error(dict(env), tool="edit_file")
    return str(excinfo.value)


def _snips(detail: str) -> list[tuple[str, str]]:
    return re.findall(r"yours «(.*?)» / file «(.*?)»", detail)


@dataclass
class _LocalSandbox(RecordingSandboxRuntime):
    """Runs the edit snippet for real against ``root`` (the ``ws`` param is swapped)."""

    root: Path = field(default_factory=Path)

    async def exec(self, **kwargs: Any) -> SandboxOutcome:
        await super().exec(**kwargs)
        first, rest = str(kwargs["code"]).split("\n", 1)
        params = json.loads(ast.literal_eval(first.removeprefix("_PARAMS = ")))
        params["ws"] = str(self.root)
        local = f"_PARAMS = {json.dumps(params)!r}\n{rest}"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(local, {})  # noqa: S102 — snippet is built from a fixed template
        return SandboxOutcome(stdout=buf.getvalue(), stderr="", exit_code=0, timed_out=False)


def _ctx(**extra: Any) -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4(), **extra)


# The spec §4.3.1 example, made concrete: 7 edits, edit 3 has one wrong character, edit 5
# edits the text edit 3 would have produced.
_SEVEN_FILE = (
    "第一行 = 1\n第二行 = 2\n标题 = 季度报告草稿\n第四行 = 4\n第五行 = 5\n第六行 = 6\n第七行 = 7\n"
)
_SEVEN_EDITS = _edits(
    ("第一行 = 1", "第一行 = 10"),
    ("第二行 = 2", "第二行 = 20"),
    ("标题 = 季度报吿草稿", "标题 = 季度报告定稿"),  # 吿 ≠ 告: one wrong character
    ("第四行 = 4", "第四行 = 40"),
    ("标题 = 季度报告定稿", "标题 = 季度报告终稿"),  # depends on edit 3's new text
    ("第六行 = 6", "第六行 = 60"),
    ("第七行 = 7", "第七行 = 70"),
)


# ---------------------------------------------------------------------------
# T3-1 / T3-2 —— the whole list is tried (dry run), nothing is written
# ---------------------------------------------------------------------------


def test_t3_1_failed_batch_reports_which_edits_would_have_applied(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text(_SEVEN_FILE, encoding="utf-8")
    env = _run_snippet(build_edits_wrapper("f.txt", _SEVEN_EDITS, ws=str(tmp_path)))
    assert env["ok"] is False
    msg = _render(env)
    assert "edit 3 of 7 did not match" in msg
    assert "NOTHING was written" in msg
    assert "Edits 1, 2, 4, 6, 7 would have applied" in msg
    assert "Resend them together with a corrected edit 3" in msg
    # all-or-nothing is kept: the four edits that matched were not written either.
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == _SEVEN_FILE


def test_t3_2_an_edit_that_depends_on_a_skipped_edit_is_not_reported_as_no_match(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.txt").write_text(_SEVEN_FILE, encoding="utf-8")
    env = _run_snippet(build_edits_wrapper("f.txt", _SEVEN_EDITS, ws=str(tmp_path)))
    msg = _render(env)
    assert "edit 5 could not be checked because it depends on edit 3" in msg
    assert "edit 5 of 7 did not match" not in msg
    assert "Edit 5 also did not match" not in msg
    assert "5" not in re.search(r"Edits ([0-9, ]+) would have applied", msg).group(1).split(", ")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# T3-3 / T3-4 / T3-5 —— the 3 most similar lines, column, snippets
# ---------------------------------------------------------------------------


#: CYRILLIC SMALL LETTER ES — looks like Latin "c" (built with chr: lint flags the literal).
_CYR_ES = chr(0x0441)


@pytest.mark.parametrize(
    ("file_line", "old_line", "yours_char", "file_char", "column"),
    [
        # CJK: 吿 typed for 告. Before the difference: 8 spaces + 'label = "季度报' (12).
        ('        label = "季度报告草稿版本"', 'label = "季度报吿草稿版本"', "吿", "告", 21),
        # Cyrillic U+0441 typed for Latin 'c', after a 2-byte 'ï' (bytes != code points).
        ('        word = "naïve cat ok"', f'word = "naïve {_CYR_ES}at ok"', _CYR_ES, "c", 23),
        # Latin with an accent: 'é' typed for 'e', after two CJK characters.
        ('        word = "日本 resume ok"', 'word = "日本 résume ok"', "é", "e", 21),
    ],
)
def test_t3_3_column_is_on_the_original_line_in_code_points(
    tmp_path: Path, file_line: str, old_line: str, yours_char: str, file_char: str, column: int
) -> None:
    body = f"first = 1\n{file_line}\nlast = 2\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    env = _run_snippet(build_edit_wrapper("f.py", old_line, "x = 0", ws=str(tmp_path)))
    assert env["error"] == "no_match"
    detail = env["detail"]
    assert f"near line 2, column {column}:" in detail
    assert "counting Unicode code points" in detail
    (yours, mine), *_ = _snips(detail)
    assert yours_char in yours
    assert file_char in mine


_FAMILY = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"  # 7 code points


@pytest.mark.parametrize(
    ("file_line", "old_line", "grapheme"),
    [
        # ZWJ emoji: the left edge of the ±8 window falls inside it.
        (f"x{_FAMILY}ab=1", f"x{_FAMILY}ab=2", _FAMILY),
        # letter + combining acute: the right edge falls between them.
        ("v=Q123456ézz", "v=R123456ézz", "é"),
    ],
)
def test_t3_4_snippets_never_split_a_grapheme(
    tmp_path: Path, file_line: str, old_line: str, grapheme: str
) -> None:
    (tmp_path / "f.txt").write_text(f"{file_line}\n", encoding="utf-8")
    env = _run_snippet(build_edit_wrapper("f.txt", old_line, "zz", ws=str(tmp_path)))
    (yours, mine), *_ = _snips(env["detail"])
    for snippet in (yours, mine):
        assert grapheme in snippet
        assert not snippet.startswith(("‍", "́"))
        assert not snippet.endswith("‍")


def test_t3_5_similar_lines_are_for_the_old_line_that_is_wrong_not_the_first(
    tmp_path: Path,
) -> None:
    body = "def build():\n    step_one()\n    step_two(alpha)\n    step_three()\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    old = "def build():\n    step_one()\n    step_two(alpah)\n"
    env = _run_snippet(build_edit_wrapper("f.py", old, "pass", ws=str(tmp_path)))
    detail = env["detail"]
    assert "line 3 of 'old_string'" in detail
    assert detail.index("near line 3,") < detail.index("yours")
    assert "near line 1" not in detail


def test_up_to_three_similar_lines_are_given(tmp_path: Path) -> None:
    body = "".join(f"value_{k} = compute({k})\n" for k in range(1, 7))
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    env = _run_snippet(build_edit_wrapper("f.py", "value_4 = compute(44)", "x", ws=str(tmp_path)))
    assert len(_snips(env["detail"])) == 3
    assert "near line 4," in env["detail"]


def test_similarity_on_a_large_file_stays_fast(tmp_path: Path) -> None:
    """Spec §4.3.8 / §10 #9 —— over 5,000 lines the scan pre-filters with ``quick_ratio``."""
    body = "".join(f"row_{k:05d} = transform(input_{k}, factor={k % 97})\n" for k in range(6000))
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    started = time.perf_counter()
    env = _run_snippet(
        build_edit_wrapper(
            "big.py", "row_03000 = transfrom(input_3000, factor=90)", "x", ws=str(tmp_path)
        )
    )
    elapsed = time.perf_counter() - started
    assert env["error"] == "no_match"
    assert "near line 3001," in env["detail"]
    print(f"[b302-pr3] 6,000-line no_match diagnostics took {elapsed:.3f}s")
    assert elapsed < 5.0


# ---------------------------------------------------------------------------
# T3-6 / T3-7 / T3-7b —— "already applied"
# ---------------------------------------------------------------------------

_APPLIED_FILE = "header line one\n    result = combine(alpha, beta)  # revised\nfooter line two\n"
_APPLIED_OLD = "    result = combine(alpha)"
_APPLIED_NEW = "    result = combine(alpha, beta)  # revised"


async def test_t3_6_already_applied_counts_as_success_and_writes_nothing_new(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.py").write_text(_APPLIED_FILE, encoding="utf-8")
    env = _run_snippet(build_edit_wrapper("f.py", _APPLIED_OLD, _APPLIED_NEW, ws=str(tmp_path)))
    assert env["ok"] is True
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == _APPLIED_FILE
    result = await EditFileTool(client=_LocalSandbox(root=tmp_path)).call(
        {"path": "f.py", "old_string": _APPLIED_OLD, "new_string": _APPLIED_NEW}, ctx=_ctx()
    )
    assert "edit 1: its new text is already in the file at line 2" in result.content
    assert "counted as applied; do not send it again" in result.content
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == _APPLIED_FILE


#: Each case breaks exactly ONE of the conditions; the others all hold. The id names it.
_NOT_APPLIED_CASES: list[tuple[str, str, list[dict[str, Any]]]] = [
    # 1a — new text 7 code points.
    ("seven_code_points", "a\nq = r+1\nb\n", _edits(("q = r+2", "q = r+1"))),
    # 1b — new text is the middle of a line, not a whole line.
    (
        "mid_line_fragment",
        "    value = compute(first, second) + offset\n",
        _edits(("compute(first)", "compute(first, second)")),
    ),
    # 2 — new text occurs twice.
    (
        "occurs_twice",
        f"{_APPLIED_NEW}\nmiddle\n{_APPLIED_NEW}\n",
        _edits((_APPLIED_OLD, _APPLIED_NEW)),
    ),
    # 4 — old text contains the new text.
    ("old_contains_new", _APPLIED_FILE, _edits((_APPLIED_NEW + " extra", _APPLIED_NEW))),
    # 5 — replace_all.
    ("replace_all", _APPLIED_FILE, _edits((_APPLIED_OLD, _APPLIED_NEW), replace_all=True)),
    # 6 — the line the new text covers is not among the 3 lines most like the old line.
    (
        "not_among_similar",
        "item = load(path_b)\nitem = load(path_c)\nitem = load(path_d)\n"
        "completely different statement here\n",
        _edits(("item = load(path_a)", "completely different statement here")),
    ),
    # judged on the ORIGINAL text — the new text appears only after edit 1 of this call.
    (
        "only_after_an_earlier_edit",
        "start\nplaceholder text here\nend\n",
        _edits(
            ("placeholder text here", "result = combine(alpha, beta)  # revised"),
            ("result = combine(alpha)", "result = combine(alpha, beta)  # revised"),
        ),
    ),
    # final re-check — in the original, but edit 1 of this call deletes that place.
    (
        "earlier_edit_removes_it",
        "keep\nresult = combine(alpha, beta)  # revised\nend\n",
        _edits(
            ("result = combine(alpha, beta)  # revised\n", ""),
            ("result = combine(alpha)", "result = combine(alpha, beta)  # revised"),
        ),
    ),
]


@pytest.mark.parametrize(
    ("body", "edits"),
    [pytest.param(body, edits, id=name) for name, body, edits in _NOT_APPLIED_CASES],
)
def test_t3_7_not_already_applied_unless_every_condition_holds(
    tmp_path: Path, body: str, edits: list[dict[str, Any]]
) -> None:
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    env = _run_snippet(build_edits_wrapper("f.py", edits, ws=str(tmp_path)))
    assert env["ok"] is False
    assert env["error"] == "no_match"
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == body


def test_t3_7_the_recheck_says_why(tmp_path: Path) -> None:
    _name, body, edits = _NOT_APPLIED_CASES[-1]
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    env = _run_snippet(build_edits_wrapper("f.py", edits, ws=str(tmp_path)))
    assert "its new text was in the file, but an earlier edit in this call changes that place" in (
        _render(env)
    )


def test_t3_7b_any_line_the_new_text_covers_may_be_the_similar_one(tmp_path: Path) -> None:
    body = "setup()\n# section: retry policy\nretries = configure(limit=5, backoff=2)\nrun()\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    new = "# section: retry policy\nretries = configure(limit=5, backoff=2)"
    env = _run_snippet(
        build_edit_wrapper("f.py", "retries = configure(limit=3)", new, ws=str(tmp_path))
    )
    assert env["ok"] is True
    assert env["already_applied"] == [{"index": 1, "line": 3}]
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == body


# ---------------------------------------------------------------------------
# T3-8 —— the run's own earlier edit replaced this text
# ---------------------------------------------------------------------------


async def test_t3_8_old_text_already_replaced_by_an_earlier_edit_this_run(
    tmp_path: Path,
) -> None:
    body = "intro\nalpha = 1\nbeta = 20\ngamma = 3\noutro\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    # Earlier this run: "alpha = 1\nbeta = 10\ngamma = 3" → "alpha = 1\nbeta = 20\ngamma = 3".
    history = {"f.py": [{"old": "alpha = 1\nbeta = 10\ngamma = 3", "new": "alpha = 1\nbeta = 20"}]}
    tool = EditFileTool(client=_LocalSandbox(root=tmp_path))
    with pytest.raises(FileOpError) as excinfo:
        # Re-sent from memory: only ONE of the replaced lines — no whole-block hash would match.
        await tool.call(
            {"path": "f.py", "old_string": "beta = 10", "new_string": "beta = 30"},
            ctx=_ctx(edit_file_history=history),
        )
    msg = str(excinfo.value)
    assert "this text was already replaced by your own edit earlier in this run" in msg
    assert "that place now reads: «beta = 20» (line 3)" in msg


# ---------------------------------------------------------------------------
# T3-9 —— consecutive failures on one file (through the real graph)
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        return self.responses[min(idx, len(self.responses) - 1)]


@dataclass
class _NamedOk:
    """A stand-in ``read_file`` / ``write_file``: the tools node only reads name, args, status."""

    name: str
    read_only: bool

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="stand-in",
            is_read_only=self.read_only,
            side_effect="read_only" if self.read_only else "reversible",
            path_args=("path",),
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        return ToolResult(content=f"ok {args.get('path')}")


def _bad_edit(path: str = "f.py") -> dict[str, Any]:
    return {
        "name": "edit_file",
        "args": {"path": path, "old_string": "missing line here", "new_string": "x"},
        "id": str(uuid4()),
        "type": "tool_call",
    }


def _named(name: str, path: str = "f.py") -> dict[str, Any]:
    args: dict[str, Any] = {"path": path}
    if name == "write_file":
        args["content"] = "x"
    return {"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}


async def _run_batches(tmp_path: Path, *batches: list[dict[str, Any]]) -> list[str]:
    for name in ("f.py", "g.py"):
        (tmp_path / name).write_text("present line\n", encoding="utf-8")
    state = await _run_graph(tmp_path, *batches)
    return [
        str(m.content)
        for m in state["messages"]
        if isinstance(m, ToolMessage) and m.name == "edit_file"
    ]


async def _run_graph(tmp_path: Path, *batches: list[dict[str, Any]]) -> dict[str, Any]:
    """The real ReAct graph: real ``edit_file`` on ``tmp_path``, stand-in read / write."""
    registry = ToolRegistry()
    registry.register(EditFileTool(client=_LocalSandbox(root=tmp_path)))
    registry.register(_NamedOk(name="read_file", read_only=True))
    registry.register(_NamedOk(name="write_file", read_only=False))
    llm = _ScriptedLLM(
        responses=[AIMessage(content="", tool_calls=b) for b in batches]
        + [AIMessage(content="done")]
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "run_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "user_id": str(uuid4()),
            }
        }
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
            config=cfg,
        )
    return dict(state)


def _body(content: str) -> str:
    """The error text after ``[tool error] <Type>: ``."""
    return content.split(": ", 1)[1]


async def test_t3_9_third_failure_in_a_row_asks_for_read_file(tmp_path: Path) -> None:
    out = await _run_batches(tmp_path, [_bad_edit()], [_bad_edit()], [_bad_edit()])
    assert "This is failure" not in out[0]
    assert "This is failure" not in out[1]
    assert _body(out[2]).startswith("This is failure 3 of edit_file on this file in a row.")
    assert "call read_file on this path and copy 'old_string' from what it returns" in out[2]


async def test_t3_9_after_reading_it_only_a_rewrite_is_asked_for(tmp_path: Path) -> None:
    out = await _run_batches(
        tmp_path, [_bad_edit()], [_bad_edit()], [_named("read_file")], [_bad_edit()]
    )
    third = _body(out[2])
    assert third.startswith("This is failure 3 of edit_file on this file in a row, even after")
    assert "Rewrite the whole file with write_file instead of editing it." in third
    assert "call read_file on this path and copy" not in third
    assert "Call read_file" not in third  # the old guidance would say it again


async def test_t3_9_a_successful_write_file_resets_the_count(tmp_path: Path) -> None:
    out = await _run_batches(
        tmp_path,
        [_bad_edit()],
        [_bad_edit()],
        [_named("write_file")],
        [_bad_edit()],
        [_bad_edit()],
    )
    assert all("This is failure" not in text for text in out)


async def test_t3_9_failures_on_another_file_do_not_add_up(tmp_path: Path) -> None:
    out = await _run_batches(tmp_path, [_bad_edit("f.py")], [_bad_edit("g.py")], [_bad_edit()])
    assert all("This is failure" not in text for text in out)


# ---------------------------------------------------------------------------
# T3-10 —— classified by kind, never by quoted file content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    ["timeout", "connection", "unavailable", " 500", "not found", "invalid", "must be"],
)
def test_t3_10_quoted_file_content_does_not_change_the_class(tmp_path: Path, needle: str) -> None:
    from orchestrator.graph_builder import builder as builder_mod

    line = f'k = "{needle}"'
    middle = len('k = "') + len(needle) // 2
    typo = line[:middle] + "#" + line[middle + 1 :]
    (tmp_path / "f.py").write_text(f"{line}\nother = 1\n", encoding="utf-8")
    env = _run_snippet(build_edit_wrapper("f.py", typo, "x = 1", ws=str(tmp_path)))
    with pytest.raises(FileOpError) as excinfo:
        _raise_for_error(env, tool="edit_file")
    error = excinfo.value
    # precondition: the file's own words really are quoted in the error text.
    assert needle in str(error).lower()
    spec = EditFileTool(client=RecordingSandboxRuntime()).spec
    classified = classify_tool_error(tool_name="edit_file", error=error, spec=spec)
    assert classified.error_class == "unknown"
    # … and the failure stays in the run's ledger (a transient one would be dropped).
    call = {"name": "edit_file", "args": {"path": "f.py"}, "id": "c1", "type": "tool_call"}
    message = ToolMessage(content=str(error), tool_call_id="c1", status="error", name="edit_file")
    failure = builder_mod._classify_tool_failure(call, message, classified)
    ledger_failure = builder_mod._ledger_failure(failure, classified)
    key = builder_mod._ledger_key(call, message, failure)
    assert builder_mod._apply_failure_ledger([], [(key, ledger_failure)]) != []


def test_other_edit_file_failures_keep_their_class() -> None:
    """Spec §4.3.5 —— only no_match / ambiguous / stale change; not_found etc. as before."""
    with pytest.raises(FileOpError) as excinfo:
        _raise_for_error({"ok": False, "error": "not_found"}, tool="edit_file")
    spec = EditFileTool(client=RecordingSandboxRuntime()).spec
    classified = classify_tool_error(tool_name="edit_file", error=excinfo.value, spec=spec)
    assert classified.error_class == "resource_not_found"


# ---------------------------------------------------------------------------
# T3-11 —— 1,500-character cap for edit_file only, guidance tail intact
# ---------------------------------------------------------------------------

_GUIDANCE_TAIL = "rewrite the enclosing function or the whole file with write_file instead."


@pytest.mark.parametrize("prior_failures", [0, 99])
async def test_t3_11_worst_case_edit_file_error_fits_and_keeps_its_guidance(
    prior_failures: int,
) -> None:
    """Worst case: 50 edits split over failed / unchecked / applied, 3 similar lines with
    stretched snippets and huge line / column numbers, an own-edit note. Without a streak
    the old guidance closes the text; with one, the streak sentence (first) replaces it."""
    from orchestrator.graph_builder.builder import _format_error

    wide = "\U0001f468\U0001f3fb" + "́" * 7  # stretches a snippet edge to its limit
    snippet = (wide + "x" * 8) * 2
    near = "; ".join(
        f"near line 9999999, column 99999999: yours «{snippet}» / file «{snippet}»"
        for _ in range(3)
    )
    detail = (
        f"line 999 of 'old_string' differs; closest: {near} (columns counting Unicode code points)"
    )
    total = 50
    env = {
        "ok": False,
        "error": "no_match",
        "detail": detail,
        "own_edit": (
            "this text was already replaced by your own edit earlier in this run; that place "
            f"now reads: «{'界' * 60}» (line 9999999)."
        ),
        "index": 1,
        "total": total,
        "report": {
            "applied": list(range(2, total, 2)),
            "failed": [[1, "no_match"], *([k, "ambiguous"] for k in range(3, total, 4))],
            "unchecked": [[k, 1] for k in range(5, total + 1, 4)],
        },
    }
    client = RecordingSandboxRuntime()
    client.outcome = SandboxOutcome(stdout=json.dumps(env), stderr="", exit_code=0, timed_out=False)
    streaks = {"f.py": {"failures": prior_failures, "read": False}}
    with pytest.raises(FileOpError) as excinfo:
        await EditFileTool(client=client).call(
            {"path": "f.py", "edits": [{"old_string": "a", "new_string": "b"}] * total},
            ctx=_ctx(edit_file_streaks=streaks),
        )
    raw = str(excinfo.value)
    rendered = _format_error(excinfo.value)
    print(f"[b302-pr3] worst-case edit_file error ({prior_failures}): {len(raw)} chars")
    assert len(raw) <= 1500
    assert "[truncated]" not in rendered
    if prior_failures:
        assert raw.startswith(
            "This is failure 100 of edit_file on this file in a row. Do not send another edit "
            "built from memory: call read_file on this path and copy 'old_string' from what it "
            "returns, or rewrite the whole file with write_file."
        )
    else:
        assert rendered.endswith(_GUIDANCE_TAIL)


def test_t3_11_other_tools_keep_the_500_character_cap() -> None:
    from orchestrator.graph_builder.builder import _format_error

    rendered = _format_error(FileOpError("write_file failed: io_error (" + "x" * 900 + ")"))
    assert rendered.endswith("...[truncated]")
    assert len(rendered) == len("[tool error] FileOpError: ") + 500 + len("...[truncated]")


# ---------------------------------------------------------------------------
# T3-12 —— no "not found" in the platform's own new wording
# ---------------------------------------------------------------------------


def test_t3_12_platform_wording_has_no_not_found_snippet_paths(tmp_path: Path) -> None:
    """Guard (green on old code): every text the snippet path writes, with neutral file content."""
    texts: list[str] = []
    (tmp_path / "f.txt").write_text(_SEVEN_FILE, encoding="utf-8")
    texts.append(
        _render(_run_snippet(build_edits_wrapper("f.txt", _SEVEN_EDITS, ws=str(tmp_path))))
    )
    for _name, body, edits in _NOT_APPLIED_CASES:
        (tmp_path / "g.py").write_text(body, encoding="utf-8")
        texts.append(_render(_run_snippet(build_edits_wrapper("g.py", edits, ws=str(tmp_path)))))
    (tmp_path / "h.py").write_text("one = 1\none = 1\n", encoding="utf-8")
    texts.append(
        _render(_run_snippet(build_edit_wrapper("h.py", "one = 1", "two", ws=str(tmp_path))))
    )
    texts.append(_render({"ok": False, "error": "stale"}))
    for text in texts:
        assert "not found" not in text.lower(), text


async def test_t3_12_platform_wording_has_no_not_found_streak_and_success(
    tmp_path: Path,
) -> None:
    texts: list[str] = []
    (tmp_path / "f.py").write_text(_APPLIED_FILE, encoding="utf-8")
    tool = EditFileTool(client=_LocalSandbox(root=tmp_path))
    ok = await tool.call(
        {"path": "f.py", "old_string": _APPLIED_OLD, "new_string": _APPLIED_NEW}, ctx=_ctx()
    )
    texts.append(ok.content)
    history = {"f.py": [{"old": "footer line one", "new": "footer line two"}]}
    for read in (False, True):
        with pytest.raises(FileOpError) as excinfo:
            await tool.call(
                {"path": "f.py", "old_string": "footer line one", "new_string": "z"},
                ctx=_ctx(
                    edit_file_streaks={"f.py": {"failures": 5, "read": read}},
                    edit_file_history=history,
                ),
            )
        texts.append(str(excinfo.value))
    assert "This is failure 6" in texts[1]
    assert "already replaced by your own edit" in texts[1]
    for text in texts:
        assert "not found" not in text.lower(), text


# ---------------------------------------------------------------------------
# Review fixes (B-302 PR 3 review, 11-11)
# ---------------------------------------------------------------------------


def test_m2_old_text_changed_by_an_earlier_edit_of_the_same_call(tmp_path: Path) -> None:
    """Edit 2's old_string IS in the file; edit 1 of the same call changed that place.

    Saying "every line is in the file, but not together in this order" would be false.
    """
    body = "def f(a):\n    return a + 1\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    edits = _edits(
        ("def f(a):", "def f(a, b):"),
        ("def f(a):\n    return a + 1", "def f(a):\n    return a + 2"),
    )
    env = _run_snippet(build_edits_wrapper("f.py", edits, ws=str(tmp_path)))
    msg = _render(env)
    assert (
        "its old text is in the file, but an earlier edit in this call (edit 1) "
        "changes that place" in msg
    )
    assert "not together in this order" not in msg
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == body


def test_low1_a_middle_line_must_be_a_whole_file_line(tmp_path: Path) -> None:
    """A middle line that is only part of a file line (trailing comment dropped) is the
    line that differs, and gets similar lines."""
    body = "def run():\n    step_one()\n    step_two()  # keep in sync\n    step_three()\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    old = "def run():\n    step_one()\n    step_two()\n    step_three()"
    env = _run_snippet(build_edit_wrapper("f.py", old, "pass", ws=str(tmp_path)))
    detail = env["detail"]
    assert "line 3 of 'old_string' differs" in detail
    assert "near line 3, column 15:" in detail
    assert "not together in this order" not in detail


def test_low1_first_and_last_lines_may_still_be_partial(tmp_path: Path) -> None:
    """old_string may start / end inside a line: the first line (a fragment here) is not
    the one that differs; the mis-copied last line is."""
    body = "value = alpha(1) + beta(2)\nmiddle = 3\ntotal = gamma(4) + delta(5)\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    old = "beta(2)\nmiddle = 3\ntotal = gama(4)"
    env = _run_snippet(build_edit_wrapper("f.py", old, "B\nM\nT", ws=str(tmp_path)))
    assert "line 3 of 'old_string' differs" in env["detail"]
    assert "near line 3," in env["detail"]


def test_low2_a_common_line_still_in_the_text_is_not_a_dependency(tmp_path: Path) -> None:
    body = "if a:\n    x = 1\nelse:\n    x = 2\nif b:\n    y = 1\nelse:\n    y = 2\n"
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    edits = _edits(
        ("if a:\n    x = 11\nelse:", "if a:\n    x = 10\nelse:"),  # mis-copied
        ("if b:\n    y = 11\nelse:", "if b:\n    y = 10\nelse:"),  # mis-copied too
    )
    msg = _render(_run_snippet(build_edits_wrapper("f.py", edits, ws=str(tmp_path))))
    assert "Edit 2 also did not match (no_match)" in msg
    assert "could not be checked" not in msg


def test_low3_similarity_on_a_huge_random_file_stays_bounded(tmp_path: Path) -> None:
    """Long random lines defeat the cheap upper bounds; the per-line cap and the total
    budget keep the scan bounded (the reviewer's 4,900 x 2,000 case took 156 s before)."""
    import random

    rng = random.Random(302)  # noqa: S311 — synthetic test content, not security
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-*/=(){}<>"
    lines = ["".join(rng.choices(alphabet, k=2000)) for _ in range(1500)]
    (tmp_path / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    probe = lines[700][:1000] + "#" + lines[700][1001:]
    started = time.perf_counter()
    env = _run_snippet(build_edit_wrapper("big.txt", probe, "x", ws=str(tmp_path)))
    elapsed = time.perf_counter() - started
    print(f"[b302-pr3] 1,500 x 2,000 random no_match diagnostics took {elapsed:.3f}s")
    assert env["error"] == "no_match"
    assert "near line 701, column 1001:" in env["detail"]
    assert elapsed < 5.0


def test_m1_accepted_risk_a_near_identical_sibling_line_counts_as_applied(
    tmp_path: Path,
) -> None:
    """Pins condition 6 as the spec has it: "any of the top-3 most similar lines".

    ACCEPTED RISK (user decision 11-11): two near-identical sibling lines; the model meant
    to edit line 1 but mis-copied one character of its old_string, and its new_string
    happens to equal line 2. Line 2 is among the 3 lines most like the mis-copied line, so
    the edit counts as "already applied" — a false success, and line 1 stays unchanged.
    The user chose to keep "top-3" over "the single most similar line". Changing this test
    means changing that decision: do it deliberately, together with the spec (§4.3.3).
    """
    body = 'role = "reader"  # primary\nrole = "writer"  # primary\n'
    (tmp_path / "f.py").write_text(body, encoding="utf-8")
    env = _run_snippet(
        build_edit_wrapper(
            "f.py", 'role = "raeder"  # primary', 'role = "writer"  # primary', ws=str(tmp_path)
        )
    )
    assert env["ok"] is True
    assert env["already_applied"] == [{"index": 1, "line": 2}]
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == body


async def test_low8a_already_applied_comes_from_the_artifact_and_skips_history(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.py").write_text(_APPLIED_FILE, encoding="utf-8")
    call = {
        "name": "edit_file",
        "args": {
            "path": "f.py",
            "edits": [
                {"old_string": "header line one", "new_string": "header line 1"},
                {"old_string": _APPLIED_OLD, "new_string": _APPLIED_NEW},
            ],
        },
        "id": "c1",
        "type": "tool_call",
    }
    state = await _run_graph(tmp_path, [call])
    message = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert message.status != "error"
    assert message.artifact["already_applied"] == [{"index": 2, "line": 2}]
    assert state["edit_file_history"] == {
        "f.py": [{"old": "header line one", "new": "header line 1"}]
    }


async def test_low8b_history_and_counter_reach_the_tool_through_the_graph(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.py").write_text("intro\nalpha = 1\nbeta = 10\noutro\n", encoding="utf-8")
    first = {
        "name": "edit_file",
        "args": {"path": "f.py", "old_string": "alpha = 1\nbeta = 10", "new_string": "beta = 20"},
        "id": "c1",
        "type": "tool_call",
    }
    again = {
        "name": "edit_file",
        "args": {"path": "f.py", "old_string": "beta = 10", "new_string": "beta = 30"},
        "id": "c2",
        "type": "tool_call",
    }
    state = await _run_graph(tmp_path, [first], [again])
    second = [m for m in state["messages"] if isinstance(m, ToolMessage)][1]
    assert "this text was already replaced by your own edit earlier in this run" in str(
        second.content
    )
    assert "that place now reads: «beta = 20» (line 2)" in str(second.content)
    assert state["edit_file_streaks"] == {"f.py": {"failures": 1, "read": False}}


async def test_low8c_stale_is_a_counted_kind_classified_failure(tmp_path: Path) -> None:
    from orchestrator.tools.edit_file_tracking import FileToolEvent, next_streaks, streak_for
    from orchestrator.tools.file_ops import EditFileMismatchError

    (tmp_path / "f.py").write_text("unchanged = 1  # connection timeout\n", encoding="utf-8")
    tool = EditFileTool(client=_LocalSandbox(root=tmp_path))
    streaks = {"f.py": {"failures": 2, "read": False}}
    args = {
        "path": "f.py",
        "old_string": "unchanged = 1",
        "new_string": "unchanged = 2",
        "expected_hash": "0" * 64,
    }
    with pytest.raises(EditFileMismatchError) as excinfo:
        await tool.call(args, ctx=_ctx(edit_file_streaks=streaks))
    error = excinfo.value
    assert error.kind == "stale"
    assert str(error).startswith("This is failure 3 of edit_file on this file in a row.")
    classified = classify_tool_error(tool_name="edit_file", error=error, spec=tool.spec)
    assert classified.error_class == "unknown"
    event = FileToolEvent("edit_file", args, ok=False, failure_kind=error.kind)
    assert streak_for(next_streaks(streaks, [event]), "f.py").failures == 3
