"""B-328 PR B —— ``read_document`` 按单条预算 R 返回、全文存进工作区;``read_file`` 每页也不超过 R。

R = T / 5(下限 4,000):T=6 万 → 1.2 万;20 万窗口默认配置(T=14 万)→ 2.8 万;100 万窗口
默认配置(T=20 万)→ 4 万。「护栏」用例回退时本来就绿,守的是默认配置下不该变的行为。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage

from orchestrator import ToolContext
from orchestrator.context.compressor import estimate_tokens
from orchestrator.context.result_budget import result_budget_tokens
from orchestrator.graph_builder.builder import _invoke_tool
from orchestrator.tools.file_ops import ReadFileTool
from orchestrator.tools.overflow import OVERFLOW_FOOTER_TAG_OPEN
from orchestrator.tools.read_document import ReadDocumentTool
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome
from orchestrator.tools.workspace_store import RecordingWorkspaceStore

NONCE = "n0nce"
R_60K = result_budget_tokens(60_000)  # 12,000
R_200K_WINDOW = result_budget_tokens(int(200_000 * 0.7))  # 28,000
R_DEFAULT = result_budget_tokens(200_000)  # 40,000


class RatioEstimator:
    """平台估算器的形状:中文 1 字 ≈ 0.88 token,其余 4 字 1 token(不联网)。"""

    def count(self, text: str) -> int:
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        return max(1, int(cjk * 0.88 + (len(text) - cjk) / 4))


EST = RatioEstimator()


@dataclass
class _Writer:
    writes: dict[str, str] = field(default_factory=dict)
    fail: bool = False

    async def write(self, *, rel: str, content: str) -> None:
        if self.fail:
            raise OSError("sandbox unreachable")
        self.writes[rel] = content

    async def read(self, rel: str) -> str | None:
        return self.writes.get(rel)


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())


def _document_tool(text: str, budget: int | None) -> ReadDocumentTool:
    client = RecordingSandboxRuntime()
    env = {"ok": True, "content": text, "format": "txt", "chars": len(text), "truncated": False}
    client.outcome = SandboxOutcome(stdout=json.dumps(env), stderr="", exit_code=0, timed_out=False)
    return ReadDocumentTool(client=client, result_budget_tokens=budget, estimator=EST)


async def _read_document(
    text: str, budget: int | None, *, writer: _Writer | None, path: str = "doc.txt"
) -> ToolMessage:
    message, *_ = await _invoke_tool(
        _document_tool(text, budget),
        {"path": path},
        "c-rd",
        _ctx(),
        overflow_writer=writer,
        spotlight_nonce=NONCE,
    )
    return message


def _tokens(message: ToolMessage) -> int:
    return estimate_tokens([message], estimator=EST)


@pytest.mark.asyncio
async def test_document_is_returned_within_the_budget_with_the_full_text_saved() -> None:
    """T=6 万 + 5 万字中文:整条结果 ≤ 1.2 万,写手收到全文,footer 给路径,记下真实路径。"""
    text = "文" * 50_000
    writer = _Writer()
    message = await _read_document(text, R_60K, writer=writer)
    assert _tokens(message) <= R_60K
    assert list(writer.writes.values()) == [text]
    (rel,) = writer.writes
    content = str(message.content)
    assert rel.startswith(".tool_results/") and rel.endswith("c-rd-read_document.txt")
    assert f"saved to {rel} in your workspace" in content
    assert content.rfind(OVERFLOW_FOOTER_TAG_OPEN) > content.rfind(f"«/UNTRUSTED nonce={NONCE}»")
    assert isinstance(message.artifact, dict)
    assert message.artifact["source_path"] == "doc.txt"
    assert message.artifact["truncated"] is True


@pytest.mark.asyncio
async def test_document_write_failure_leaves_no_footer_and_points_at_exec_python() -> None:
    message = await _read_document("文" * 50_000, R_60K, writer=_Writer(fail=True))
    content = str(message.content)
    assert OVERFLOW_FOOTER_TAG_OPEN not in content
    assert "otherwise extract the part you need with exec_python or bash" in content
    assert _tokens(message) <= R_60K


@pytest.mark.asyncio
async def test_english_document_on_a_200k_window_default_is_cut_and_saved() -> None:
    """20 万窗口默认配置(R=2.8 万)+ 15 万字英文:截了、有 footer。"""
    text = "word " * 30_000
    writer = _Writer()
    message = await _read_document(text, R_200K_WINDOW, writer=writer)
    assert OVERFLOW_FOOTER_TAG_OPEN in str(message.content)
    assert list(writer.writes.values()) == [text]
    assert _tokens(message) <= R_200K_WINDOW


@pytest.mark.asyncio
async def test_big_chinese_document_on_the_default_config_is_cut_to_40k() -> None:
    message = await _read_document("文" * 150_000, R_DEFAULT, writer=_Writer())
    assert _tokens(message) <= R_DEFAULT


@pytest.mark.asyncio
async def test_guard_small_english_document_on_the_default_config_is_unchanged() -> None:
    """护栏:100 万窗口默认配置 + 5 万字英文 —— 正文与今天逐字相同,不落盘。"""
    text = "word " * 10_000
    writer = _Writer()
    budgeted = await _read_document(text, R_DEFAULT, writer=writer)
    today = await _read_document(text, None, writer=_Writer())
    assert budgeted.content == today.content
    assert writer.writes == {}


@pytest.mark.asyncio
async def test_document_records_the_real_path_for_a_short_input_path() -> None:
    run = uuid4()
    ctx = ToolContext(
        tenant_id=uuid4(), run_id=run, user_id=uuid4(), agent_key="a1", prompt_inputs={"x": "1"}
    )
    result = await _document_tool("短文", R_60K).call({"path": "inputs/current/a.txt"}, ctx=ctx)
    assert result.meta["source_path"] == f"inputs/{run}/a.txt"


def _text_file(lines: int, per_line: int) -> bytes:
    return ("".join("文" * per_line + "\n" for _ in range(lines))).encode("utf-8")


@pytest.mark.asyncio
async def test_read_file_page_stays_within_the_budget() -> None:
    """T=6 万 + 5 万字中文文本:单页 ≤ 1.2 万。"""
    store = RecordingWorkspaceStore(workspace_file=_text_file(500, 100))
    tool = ReadFileTool(store=store, token_cap=R_60K, estimator=EST)
    message, *_ = await _invoke_tool(tool, {"path": "a.txt"}, "c-rf", _ctx(), spotlight_nonce=NONCE)
    assert _tokens(message) <= R_60K
    assert "call read_file again with offset=" in str(message.content)


@pytest.mark.asyncio
async def test_guard_read_file_page_on_the_default_config_is_still_20k_chars() -> None:
    """护栏:100 万窗口默认配置 + 5 万字中文 —— 每页仍按 2 万字。"""
    data = _text_file(500, 100)
    capped = await ReadFileTool(
        store=RecordingWorkspaceStore(workspace_file=data), token_cap=R_DEFAULT, estimator=EST
    ).call({"path": "a.txt"}, ctx=_ctx())
    today = await ReadFileTool(store=RecordingWorkspaceStore(workspace_file=data)).call(
        {"path": "a.txt"}, ctx=_ctx()
    )
    assert capped.content == today.content
    assert 19_000 < len(capped.content) <= 20_000
