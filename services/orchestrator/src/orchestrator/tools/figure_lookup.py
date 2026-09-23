"""``ask_image(path, unit)`` 的宿主侧解析 —— 文档路径 + 页号 → 渲染页 ref。B-64 Task 8。

为什么要有这一层:``read_page`` 渲出来的页,ref 约 200 字符(两段 16hex + 租户 /
用户 / run 三个 UUID)。让模型把它原样抄进 ``ask_image.image_ref`` 是在赌它不抄错,
而实证是会错(同一 Agent、同一模型 09-14 手抄 URL 三次错三次,tokenizer 块边界上
复制走样)。所以模型只填两样它自己刚填过的东西 —— 文档路径与页号 —— ref 由这里拼。

**不在宿主复刻沙箱片段的 ``_render_sha``**(那是片段里对文档字节 + dpi 求的哈希,
宿主再写一份就是两份算法,迟早漂移)。换成两步:

1. 在**本 agent 作用域**里找 ``.tool_results/*/figures/<doc-sha>/*/_u<unit>/`` 下
   形状合法的渲染页,取修改时间最新的那一张;
2. **新鲜度闸**:文档自己的修改时间晚于那张渲染 → 不采用,显式失败,让模型重渲。

闸的道理:渲染发生在文档最后一次修改**之后**,渲的就是当前内容。错了的代价是
NAS 的 mtime 语义异常时误报「改过了请重渲」—— 可见、可恢复,不会静默看错页。

**只用** :meth:`WorkspaceStore.list_dir`,而且只往列表里**确认是目录**的条目里走:
NAS 实现对不存在的目录抛内部异常、内存替身回空列表,两者对「不存在」的说法不同;
只走确认过的条目,两者的答案就一样(「SQL↔内存 store 谓词必同义」的同一条规矩)。
``list_dir`` 用 ``lstat``,符号链接不会被当成目录走进去。

形状判据是 :func:`~expert_work.persistence.is_rendered_figure_rel`,不在这里写第二份。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID

from expert_work.persistence import (
    RENDERED_FIGURE_DIR,
    RENDERED_FIGURE_UNIT_PREFIX,
    WORKSPACE_OVERFLOW_DIR,
    is_rendered_figure_rel,
)
from orchestrator.tools.read_page import (
    _DEFAULT_UNIT_LABEL,
    _UNIT_LABELS,
    document_sha,
    workspace_figure_ref,
)
from orchestrator.tools.workspace_paths import resolve_scope
from orchestrator.tools.workspace_scope import scoped_path, store_scope
from orchestrator.tools.workspace_store import WorkspaceDirEntry, WorkspaceStore


@dataclass(frozen=True)
class _Rendered:
    #: 作用域相对路径,形状已过 ``is_rendered_figure_rel``。
    rel: str
    mtime: datetime | None


@dataclass(frozen=True)
class _ScopeLister:
    """一个 (tenant, user, 作用域) 上的 ``list_dir``,按名字索引。"""

    store: WorkspaceStore
    tenant_id: UUID
    user_id: UUID
    scope: str

    async def entries(self, rel: str) -> dict[str, WorkspaceDirEntry]:
        listing = await self.store.list_dir(
            tenant_id=self.tenant_id, user_id=self.user_id, scope=self.scope, path=rel or "."
        )
        return {entry.name: entry for entry in listing.entries}

    async def subdirs(self, rel: str) -> list[str]:
        return [name for name, entry in (await self.entries(rel)).items() if entry.is_dir]

    async def has_dir(self, rel: str, name: str) -> bool:
        entry = (await self.entries(rel)).get(name)
        return entry is not None and entry.is_dir

    async def entry_at(self, parts: tuple[str, ...]) -> WorkspaceDirEntry | None:
        """逐段往下走,只进确认是目录的条目;任何一段不在就 ``None``。"""
        current = ""
        found: WorkspaceDirEntry | None = None
        for index, part in enumerate(parts):
            if index > 0 and (found is None or not found.is_dir):
                return None
            found = (await self.entries(current)).get(part)
            if found is None:
                return None
            current = f"{current}/{part}" if current else part
        return found


async def resolve_rendered_figure(
    store: WorkspaceStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    path: str,
    unit: int,
) -> str:
    """``path`` 第 ``unit`` 页(docx 为第 ``unit`` 处)最新一次渲染的工作区 ref。

    ``path`` 是已经过 ``file_ops._require_path`` 的值 —— 与 ``read_page`` 算
    ``<doc-sha>`` 用的同一个输入。解析不了抛 ``ValueError``;文档不在、没渲过、
    渲完文档又改过,都抛 ``FileNotFoundError``(文字说清下一步);工作区后端不报
    修改时间、闸没法判,抛 ``RuntimeError``。
    """
    doc_sha = document_sha(path, agent_key=agent_key)
    if doc_sha is None:
        msg = f"ask_image 的 path 解析不了:{path!r} —— 填调用 read_page 时用的那条文档路径。"
        raise ValueError(msg)
    ws, rel = resolve_scope(path, agent_key=agent_key, tool="read_page")
    scope = store_scope(ws, agent_key=agent_key)
    lister = _ScopeLister(store=store, tenant_id=tenant_id, user_id=user_id, scope=scope)
    label = _UNIT_LABELS.get(PurePosixPath(path).suffix.lower().lstrip("."), _DEFAULT_UNIT_LABEL)
    document = await lister.entry_at(PurePosixPath(rel).parts)
    if document is None or document.is_dir:
        msg = f"文档 {path} 不在工作区里了,看不了它的第 {unit} {label}。"
        raise FileNotFoundError(msg)
    candidates = await _rendered_candidates(lister, doc_sha=doc_sha, unit=unit)
    if not candidates:
        msg = (
            f"{path} 的第 {unit} {label}还没渲染 —— "
            f"请先调用 read_page(path={path!r}, units=[{unit}]),再调用 ask_image。"
        )
        raise FileNotFoundError(msg)
    dated = [(c.mtime, c) for c in candidates if c.mtime is not None]
    if document.mtime is None or len(dated) != len(candidates):
        msg = (
            "当前部署的工作区不报告文件修改时间,核对不了渲染页是不是最新的,"
            "path + unit 这种写法在这里用不了。"
        )
        raise RuntimeError(msg)
    newest_mtime, newest = max(dated, key=lambda pair: pair[0])
    if document.mtime > newest_mtime:
        msg = (
            f"{path} 在渲染之后改过,那张渲染页已经过期 —— "
            f"请先重新调用 read_page(path={path!r}, units=[{unit}]) 渲染第 {unit} {label},"
            "再调用 ask_image。"
        )
        raise FileNotFoundError(msg)
    return workspace_figure_ref(tenant_id, user_id, scoped_path(scope, newest.rel))


async def _rendered_candidates(lister: _ScopeLister, *, doc_sha: str, unit: int) -> list[_Rendered]:
    """``.tool_results/*/figures/<doc_sha>/*/_u<unit>/`` 下形状合法的全部渲染页。"""
    if not await lister.has_dir("", WORKSPACE_OVERFLOW_DIR):
        return []
    found: list[_Rendered] = []
    for run_id in await lister.subdirs(WORKSPACE_OVERFLOW_DIR):
        run_dir = f"{WORKSPACE_OVERFLOW_DIR}/{run_id}"
        if not await lister.has_dir(run_dir, RENDERED_FIGURE_DIR):
            continue
        figures_dir = f"{run_dir}/{RENDERED_FIGURE_DIR}"
        if not await lister.has_dir(figures_dir, doc_sha):
            continue
        found.extend(await _unit_pages(lister, f"{figures_dir}/{doc_sha}", unit=unit))
    return found


async def _unit_pages(lister: _ScopeLister, doc_dir: str, *, unit: int) -> list[_Rendered]:
    unit_name = f"{RENDERED_FIGURE_UNIT_PREFIX}{unit}"
    pages: list[_Rendered] = []
    for render_sha in await lister.subdirs(doc_dir):
        render_dir = f"{doc_dir}/{render_sha}"
        if not await lister.has_dir(render_dir, unit_name):
            continue
        unit_dir = f"{render_dir}/{unit_name}"
        for name, entry in (await lister.entries(unit_dir)).items():
            rel = f"{unit_dir}/{name}"
            if not entry.is_dir and is_rendered_figure_rel(rel):
                pages.append(_Rendered(rel=rel, mtime=entry.mtime))
    return pages
