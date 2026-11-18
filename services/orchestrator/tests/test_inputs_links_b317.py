"""B-317 —— 宿主侧读本轮输入:预拉链接、``ask_image`` 的短路径、跨轮记下的真实路径。

L1 的来源是协调方的探针:预拉建的 ``inputs/<run>/org_logo.png -> ../cache/<摘要>.png`` 交给
宿主侧的图片读取(``NasWorkspaceImageResolver``)会被当成越界拒掉。修法不放宽读取,而是让
``ask_image`` 先把这一种链接换成它指向的缓存文件(``NasWorkspaceStore.resolve_input_link``)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from orchestrator.graph_builder.figure_block import _document_path
from orchestrator.multimodal import IMAGE_REF_BLOCK_TYPE, NasWorkspaceImageResolver
from orchestrator.tools.inputs_doc import input_link_cache_path
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import document_sha, workspace_figure_ref
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.vision import AskImageTool
from orchestrator.tools.workspace_paths import WorkspaceView, resolve_workspace_path

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_RUN = UUID("00000000-0000-4000-8000-000000000001")
_RUN2 = UUID("00000000-0000-4000-8000-000000000002")
_AGENT = "report-aaaaaaaa"
_DIGEST = "a" * 32
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _user_root(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER)


def _plant(root: Path, *, link: str, target: str, cache_name: str = f"{_DIGEST}.png") -> None:
    """agent 根下建 ``inputs/cache/<cache_name>`` 与一条链接(老视图:agent 根就是 /workspace)。"""
    base = _user_root(root) / "agents" / _AGENT
    cache = base / "inputs" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / cache_name).write_bytes(_PNG)
    path = base / link
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


def _ctx(*, inputs: bool = True) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT,
        run_id=_RUN,
        prompt_inputs={"org_logo": "https://x.invalid/l.png"} if inputs else None,
    )


@dataclass
class _VL:
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls.append(list(messages))
        return AIMessage(content="ok")


def _sent_ref(vl: _VL) -> str:
    content = vl.calls[-1][1].content
    assert isinstance(content, list)
    block = content[1]
    assert block["type"] == IMAGE_REF_BLOCK_TYPE
    return str(block["ref"])


def _tool(root: Path, vl: _VL) -> AskImageTool:
    return AskImageTool(
        vl_caller=vl,
        image_resolver=NasWorkspaceImageResolver(root=root),
        workspace_store=NasWorkspaceStore(root=str(root)),
    )


# --- L1:宿主侧图片读取拒绝预拉链接(现状),ask_image 经链接解析读到 ---------------------


@pytest.mark.anyio
async def test_the_image_resolver_refuses_a_prefetch_link_on_its_own(tmp_path: Path) -> None:
    """协调方探针原样:读取层本身不放宽 —— 链接照旧按越界拒。"""
    _plant(tmp_path, link=f"inputs/{_RUN}/org_logo.png", target=f"../cache/{_DIGEST}.png")
    ref = workspace_figure_ref(_TENANT, _USER, f"agents/{_AGENT}/inputs/{_RUN}/org_logo.png")
    with pytest.raises(ValueError, match="escapes the user root"):
        await NasWorkspaceImageResolver(root=tmp_path).resolve(ref)


@pytest.mark.anyio
@pytest.mark.parametrize("written", ["inputs/current/org_logo.png", f"inputs/{_RUN}/org_logo.png"])
async def test_ask_image_reads_this_turns_input_image_through_its_link(
    tmp_path: Path, written: str
) -> None:
    _plant(tmp_path, link=f"inputs/{_RUN}/org_logo.png", target=f"../cache/{_DIGEST}.png")
    vl = _VL()

    result = await _tool(tmp_path, vl).call({"path": written, "question": "?"}, ctx=_ctx())

    assert result.content == "ok"
    cache = f"agents/{_AGENT}/inputs/cache/{_DIGEST}.png"
    assert _sent_ref(vl) == workspace_figure_ref(_TENANT, _USER, cache)
    # P9 —— 元数据记真实路径(下一轮也指同一个文件),不记短路径。
    assert result.meta["resolved_from"] == {"path": f"inputs/{_RUN}/org_logo.png"}


@pytest.mark.anyio
async def test_ask_image_without_inputs_this_turn_says_so(tmp_path: Path) -> None:
    vl = _VL()
    with pytest.raises(ValueError, match="本轮没有输入变量"):
        await _tool(tmp_path, vl).call(
            {"path": "inputs/current/org_logo.png", "question": "?"}, ctx=_ctx(inputs=False)
        )
    assert vl.calls == []


# --- L2-L4:只认指回本 inputs/cache/ 的平台链接 --------------------------------------


@pytest.mark.parametrize(
    ("link", "target", "expected"),
    [
        (f"inputs/{_RUN}/x.png", f"../cache/{_DIGEST}.png", f"inputs/cache/{_DIGEST}.png"),
        (
            f"inputs/{_RUN}/m/0-a.png",
            f"../../cache/{_DIGEST}.png",
            f"inputs/cache/{_DIGEST}.png",
        ),
        (
            f"agents/k/threads/c/work/inputs/{_RUN}/x",
            f"../cache/{_DIGEST}",
            f"agents/k/threads/c/work/inputs/cache/{_DIGEST}",
        ),
    ],
)
def test_platform_links_resolve_to_their_cache_entry(link: str, target: str, expected: str) -> None:
    assert input_link_cache_path(link, target) == expected


@pytest.mark.parametrize(
    ("link", "target"),
    [
        (f"inputs/{_RUN}/x.png", "../../../etc/passwd"),
        (f"inputs/{_RUN}/x.png", f"../cache/sub/{_DIGEST}.png"),
        (f"inputs/{_RUN}/x.png", f"../other/{_DIGEST}.png"),
        (f"inputs/{_RUN}/x.png", "../cache/notahexname.png"),
        (f"inputs/{_RUN}/x.png", f"/workspace/inputs/cache/{_DIGEST}.png"),
        (f"inputs/{_RUN}/x.png", ""),
        # 链接不在 inputs/<规范 UUID>/ 下
        ("crops/x.png", f"../inputs/cache/{_DIGEST}.png"),
        ("inputs/notauuid/x.png", f"../cache/{_DIGEST}.png"),
        ("inputs/ABCDEF00-0000-4000-8000-000000000001/x.png", f"../cache/{_DIGEST}.png"),
        ("inputs/cache/x.png", f"../cache/{_DIGEST}.png"),
    ],
)
def test_other_links_are_not_followed(link: str, target: str) -> None:
    assert input_link_cache_path(link, target) is None


@pytest.mark.anyio
async def test_the_store_leaves_anything_else_untouched(tmp_path: Path) -> None:
    store = NasWorkspaceStore(root=str(tmp_path))
    _plant(tmp_path, link=f"inputs/{_RUN}/evil.png", target="../../../../../etc/hosts")
    _plant(
        tmp_path,
        link=f"inputs/{_RUN}/ok.png",
        target=f"../cache/{_DIGEST}.png",
    )
    (_user_root(tmp_path) / "agents" / _AGENT / f"inputs/{_RUN}/plain.png").write_bytes(_PNG)
    base = f"agents/{_AGENT}/inputs/{_RUN}"
    for name, expected in (
        ("evil.png", f"{base}/evil.png"),
        ("plain.png", f"{base}/plain.png"),
        ("missing.png", f"{base}/missing.png"),
        ("ok.png", f"agents/{_AGENT}/inputs/cache/{_DIGEST}.png"),
    ):
        got = await store.resolve_input_link(
            tenant_id=_TENANT, user_id=_USER, path=f"{base}/{name}"
        )
        assert got == expected, name


# --- L5:链接名没有扩展名,按缓存文件的扩展名判是不是图 -------------------------------


@pytest.mark.anyio
async def test_an_extensionless_input_link_to_an_image_is_looked_at_as_an_image(
    tmp_path: Path,
) -> None:
    _plant(tmp_path, link=f"inputs/{_RUN}/org_logo", target=f"../cache/{_DIGEST}.png")
    vl = _VL()

    result = await _tool(tmp_path, vl).call(
        {"path": "inputs/current/org_logo", "question": "?"}, ctx=_ctx()
    )

    assert result.content == "ok"
    cache = f"agents/{_AGENT}/inputs/cache/{_DIGEST}.png"
    assert _sent_ref(vl) == workspace_figure_ref(_TENANT, _USER, cache)


# --- L6:list_dir 报缓存文件的大小 ------------------------------------------------------


@pytest.mark.anyio
async def test_list_dir_reports_the_cached_files_size_for_input_links(tmp_path: Path) -> None:
    store = NasWorkspaceStore(root=str(tmp_path))
    _plant(tmp_path, link=f"inputs/{_RUN}/ok.png", target=f"../cache/{_DIGEST}.png")
    _plant(tmp_path, link=f"inputs/{_RUN}/odd.png", target="../elsewhere.png")
    listing = await store.list_dir(
        tenant_id=_TENANT, user_id=_USER, path=f"agents/{_AGENT}/inputs/{_RUN}"
    )
    sizes = {entry.name: entry.size for entry in listing.entries}
    assert sizes["ok.png"] == len(_PNG)
    assert sizes["odd.png"] == len("../elsewhere.png")  # 认不出的链接:仍是 lstat 大小


# --- P7(H1):跨轮记下的是真实路径,下一轮核对得上 -------------------------------------


@pytest.mark.parametrize("next_inputs_run", [_RUN2, None])
def test_a_document_read_by_short_path_is_found_again_next_turn(
    next_inputs_run: UUID | None,
) -> None:
    turn1 = WorkspaceView(agent_key=_AGENT, inputs_run=_RUN)
    turn2 = WorkspaceView(agent_key=_AGENT, inputs_run=next_inputs_run)
    recorded = resolve_workspace_path(
        "inputs/current/report.pdf", view=turn1, tool="read_page"
    ).path
    doc_sha = document_sha(recorded, view=turn1)

    assert _document_path(doc_sha, {doc_sha: recorded}, turn2) == f"inputs/{_RUN}/report.pdf"
    # 对照:当初若记的是短路径,下一轮要么核对不上,要么指向另一轮的同名文档。
    assert _document_path(doc_sha, {doc_sha: "inputs/current/report.pdf"}, turn2) is None
