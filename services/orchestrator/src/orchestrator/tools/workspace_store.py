"""``WorkspaceStore`` — user persistent-workspace file operations.

Split out of :mod:`orchestrator.tools.sandbox` (sandbox migration wave 1,
Task 4). ``SandboxRuntime`` used to carry two unrelated jobs: sandbox
*lifecycle* (acquire / exec / release / destroy / reap) and per-user
*workspace file* access (read / list / write / delete / mark-deleted). The
five workspace methods never touch a live sandbox — they proxy straight to
the supervisor's workspace-file HTTP API — so bundling them onto the
sandbox-lifecycle Protocol was the wrong shape: a future workspace
implementation (wave 2's NAS-mounted control-plane read path) needs none of
the sandbox machinery.

This module is the first cut of that split: same HTTP wire behaviour, moved
under its own :class:`WorkspaceStore` Protocol. URL / request body / header /
error handling are carried over unchanged from ``HTTPSupervisorRuntime``; the
only change is the method names losing their redundant ``workspace_``
prefix (``read_workspace_file`` → :meth:`WorkspaceStore.read_file`, etc.) —
now that they live on a type called ``WorkspaceStore``, repeating the word
in every method name added nothing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from orchestrator.llm.providers._http import client_for
from orchestrator.tools.sandbox import (
    SandboxSupervisorError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    _traced_headers,
)
from orchestrator.tools.workspace_scope import (
    SCOPE_USER_ROOT,
    scope_parts,
    scoped_dir,
    scoped_path,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0

#: ``list_dir`` 的默认条目上限 —— 与 ``NasWorkspaceStore._MAX_LIST_ENTRIES`` 同值。
#: 工具层自己传更小的那个(``file_ops._MAX_LIST_ENTRIES``), 模型看到的截断行因此
#: 与 B-84 之前逐字相同。
_DEFAULT_MAX_DIR_ENTRIES = 2000

#: ``search_files`` 的默认命中上限。
_DEFAULT_SEARCH_RESULTS = 50


@dataclass(frozen=True)
class WorkspaceFileEntry:
    """One file in a user's persistent workspace volume (browse listing)."""

    path: str
    size: int
    #: B-84 —— 最后修改时间, 带 tzinfo 的 UTC ``datetime``。它正是决定"复用还是
    #: 重建"的那个字段: 模型要能分辨"这是我上一轮写的"和"很久以前的"。
    #:
    #: ``None`` 的唯一合法来源是 :class:`SupervisorWorkspaceStore` —— 它的 HTTP
    #: 列表里没有这一项(supervisor 侧 ``list_volume_files`` 只回 ``(size,
    #: relpath)``), 而那个后端在集群上已经退役, 只剩本地 dev / CI。宁可如实报
    #: ``None`` 也不编一个假时间: 一个看起来像真的、实际是编的时间戳比没有更坏。
    #: :class:`~orchestrator.tools.nas_workspace_store.NasWorkspaceStore`
    #: (生产唯一在跑的实现)永远给真值。
    mtime: datetime | None = None


@dataclass(frozen=True)
class WorkspaceDirEntry:
    """One entry of **one** directory —— ``list_dir`` 的行(B-84)。

    与 :class:`WorkspaceFileEntry` 是两件事, 别合并:后者是「整棵子树里的一个文
    件」(递归、过滤保留前缀、给浏览与产物下载用), 这个是「这一个目录里的一项」
    (含子目录、不过滤、给模型的 ``list_dir`` 用)。今天沙箱里就是两套语义, 合并
    等于把其中一套悄悄改掉。

    ``size`` 对目录是 ``None`` —— 与沙箱片段的 ``entry.stat().st_size if
    entry.is_file() else None`` 逐字同义, 模型看到的渲染因此一个字不变。
    """

    name: str
    is_dir: bool
    size: int | None
    mtime: datetime | None = None


@dataclass(frozen=True)
class WorkspaceDirListing:
    """``list_dir`` 的返回 —— 条目 + 是否被 ``max_entries`` 截断。"""

    entries: tuple[WorkspaceDirEntry, ...]
    truncated: bool


@dataclass(frozen=True)
class WorkspaceSearchResult:
    """``search_files`` 的返回 —— 命中 + 是否被 ``max_results`` 截断。

    截断标记是单独一个字段, 不是「``len(entries) == max_results``」这种推断:
    恰好命中 N 条和命中了更多被截到 N 条, 对模型是两个不同的事实, 推断法把它们
    混成一个。照 ``list_dir`` 既有的 ``truncated`` 约定, 不发明第二种。
    """

    entries: tuple[WorkspaceFileEntry, ...]
    truncated: bool


def _entry_from_wire(raw: Mapping[str, Any]) -> WorkspaceFileEntry:
    """Parse one listing row from the supervisor's JSON body.

    ``mtime`` is read **when present** (epoch seconds) and left ``None``
    otherwise: today's supervisor does not send it (see
    :attr:`WorkspaceFileEntry.mtime`), and the day it starts, this store needs
    no second change. A non-numeric value degrades to ``None`` rather than
    raising — a listing is a browse surface, not a place to fail a whole run
    over one odd row.
    """
    raw_mtime = raw.get("mtime")
    mtime = (
        datetime.fromtimestamp(float(raw_mtime), tz=UTC)
        if isinstance(raw_mtime, int | float)
        else None
    )
    return WorkspaceFileEntry(path=str(raw["path"]), size=int(raw["size"]), mtime=mtime)


def scope_view(entries: Sequence[WorkspaceFileEntry], scope: str) -> list[WorkspaceFileEntry]:
    """把一份**用户根相对**的清单裁到一个作用域里, 并剥掉前缀。

    给那些只拿得到"整棵树的扁平清单"的后端用(HTTP 代理与内存替身)。NAS 侧不走
    这里 —— 它直接从作用域根开始遍历, 那才是本 PR 要的那条路径。两边的答案必须
    一致, 所以前缀由同一个 :func:`~orchestrator.tools.workspace_scope.scope_parts`
    算出来, 不是各拼各的。
    """
    prefix = scope_parts(scope)
    if not prefix:
        return list(entries)
    base = "/".join(prefix) + "/"
    return [
        WorkspaceFileEntry(path=e.path[len(base) :], size=e.size, mtime=e.mtime)
        for e in entries
        if e.path.startswith(base)
    ]


def dir_listing_from_files(
    entries: Sequence[WorkspaceFileEntry], *, rel: str, max_entries: int
) -> WorkspaceDirListing:
    """把一份扁平文件清单折成「一个目录的列表」。

    **已知与 NAS 侧的偏差, 两条, 都是原料决定的而不是选择:** 扁平清单里没有空目
    录(没有文件的目录不会出现在任何一行里), 而且喂进来的清单本身已经被后端按保
    留前缀过滤过 —— 所以 ``uploads/`` 在这两个后端的 ``list_dir`` 里看不见, NAS
    侧看得见。两者都只影响本地 dev / CI(supervisor 后端在集群上已退役, 内存替身
    只在测试里), 生产跑的是 NAS 实现。记在这里而不是假装没有。
    """
    base = f"{rel}/" if rel else ""
    found: dict[str, WorkspaceDirEntry] = {}
    for entry in entries:
        if base and not entry.path.startswith(base):
            continue
        head, sep, _rest = entry.path[len(base) :].partition("/")
        if not head:
            continue
        if sep:
            found.setdefault(head, WorkspaceDirEntry(name=head, is_dir=True, size=None))
        else:
            found[head] = WorkspaceDirEntry(
                name=head, is_dir=False, size=entry.size, mtime=entry.mtime
            )
    names = sorted(found)
    return WorkspaceDirListing(
        entries=tuple(found[name] for name in names[:max_entries]),
        truncated=len(names) > max_entries,
    )


def name_matches(entry: WorkspaceFileEntry, name_glob: str | None) -> bool:
    """``*.py`` 匹配文件名, ``style/*.py`` 匹配作用域相对路径 —— 与 NAS 侧同一条规矩。"""
    if name_glob is None:
        return True
    return fnmatch(entry.path.rsplit("/", 1)[-1], name_glob) or fnmatch(entry.path, name_glob)


def require_search_terms(name_glob: str | None, content: str | None) -> None:
    if name_glob is None and content is None:
        msg = "search_files needs at least one of 'name_glob' / 'content'"
        raise SandboxSupervisorError(msg)


def search_result(hits: Sequence[WorkspaceFileEntry], *, max_results: int) -> WorkspaceSearchResult:
    ordered = sorted(hits, key=lambda entry: entry.path)
    return WorkspaceSearchResult(
        entries=tuple(ordered[:max_results]), truncated=len(ordered) > max_results
    )


@runtime_checkable
class WorkspaceStore(Protocol):
    """A user's persistent workspace volume — file read / list / write / delete."""

    async def read_file(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        path: str,
        scope: str = SCOPE_USER_ROOT,
        max_bytes: int | None = None,
    ) -> bytes:
        """Read a file from a user's persistent workspace volume (J.9 artifact download).

        ``scope`` (B-84) 把 ``path`` 锚在用户根下的某个前缀上 —— 三档见
        :mod:`orchestrator.tools.workspace_scope`。默认是整个用户根, 与 B-84 之前
        逐字相同, 所以产物下载 / 浏览端点那批既有调用方一个字都不用改。

        ``max_bytes`` 收紧本次读的上限(不放宽:后端自己的下载闸仍然封顶)。
        ``read_file`` 工具的 10 MiB 语义因此不用靠"先读进 64 MiB 再拒绝"来实现。
        """

    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID, scope: str = SCOPE_USER_ROOT
    ) -> list[WorkspaceFileEntry]:
        """List the files in a user's persistent workspace volume (browse).

        递归, 且过滤保留前缀(``uploads/`` / ``skills/`` / ``inputs/`` /
        ``.tool_results/``)—— 这是"产物"视图。要"这一个目录里有什么"用
        :meth:`list_dir`, 两者不是同一件事。``path`` 回的是**作用域相对**路径。
        """

    async def list_dir(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        path: str = ".",
        max_entries: int = _DEFAULT_MAX_DIR_ENTRIES,
    ) -> WorkspaceDirListing:
        """List **one** directory, ``ls`` 语义(B-84)—— backs the ``list_dir`` tool.

        含子目录, **不**过滤保留前缀:``uploads/`` 是 B-67 落本轮输入的地方, 模型
        必须看得见。``path`` 是作用域相对的, ``"."`` 是作用域根。
        """

    async def search_files(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        name_glob: str | None = None,
        content: str | None = None,
        max_results: int = _DEFAULT_SEARCH_RESULTS,
    ) -> WorkspaceSearchResult:
        """Find files under one scope by name and/or content (B-84).

        两个都给 = 文件名匹配**且**内容包含。一个都不给是调用方的 bug, 抛
        :class:`SandboxSupervisorError` —— 默认成"列出全部"会让一次写错的搜索悄悄
        变成一次全量列表。内容搜只看文本文件, 二进制跳过(不试着解码)。
        """

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` in a user's persistent workspace volume.

        Backs the document-upload path: a user uploads a file, the
        control-plane proxies here, and the bytes land in the durable
        workspace so a later run's ``read_document`` can read them. Only
        the supervisor can write a per-user docker volume."""

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        """Delete one file from a user's persistent workspace volume.

        Backs the playground workspace cleanup. Only the supervisor can
        mutate a per-user docker volume; the control-plane proxies here."""

    async def delete_tree(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        """Recursively delete one directory under a user's persistent workspace.

        ``rm -rf`` semantics — a missing target is a no-op; reserved prefixes
        are refused like :meth:`delete_file`. 留存链 B-27:会话 purge 同步删
        ``threads/<thread_id>/``(该会话的 MEMORY.md / PLAN.md / TODO.md 投影)。"""

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """Soft-delete a user's whole persistent workspace (Phase 3a purge_user).

        The supervisor destroys any warm session, drops the user's
        sandbox_instance rows, and marks the volume deleted (Mini-ADR J-36 —
        the reaper archives it, then the 90-day sweep hard-deletes). Idempotent.
        Only the supervisor can mutate a per-user docker volume; the
        control-plane proxies here as part of the cascade purge."""


@dataclass
class SupervisorWorkspaceStore:
    """Production :class:`WorkspaceStore` — calls the supervisor's HTTP API.

    A non-2xx response raises :class:`SandboxSupervisorError`. Field shape
    mirrors :class:`orchestrator.tools.sandbox.HTTPSupervisorRuntime`.
    """

    base_url: str
    timeout_s: float = _DEFAULT_TIMEOUT_S
    #: Test seam — inject ``httpx.MockTransport`` / ``ASGITransport`` to
    #: exercise the wire layer (e.g. the A.8 traceparent round-trip).
    #: Production leaves it ``None`` (real network transport).
    transport: httpx.AsyncBaseTransport | None = None
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``httpx.AsyncClient(...)`` behaviour (tests / eval
    #: CLI / not-yet-wired production paths). When injected it must NOT be
    #: closed here: it is owned by the control-plane lifespan and outlives
    #: every individual call.
    http: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def _make_client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with client_for(
            self.http, timeout=self.timeout_s, transport=self.transport
        ) as client:
            yield client

    async def read_file(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        path: str,
        scope: str = SCOPE_USER_ROOT,
        max_bytes: int | None = None,
    ) -> bytes:
        # B-84 —— 作用域在这个后端上就是路径前缀:它的 HTTP API 只认用户根相对
        # 路径, 而 NAS 上的布局(``agents/<key>/…``)两边是同一套, 所以"换个起点"
        # 与"加个前缀"指的是同一个文件。``scoped_path`` 先归一化再拼, 顺序见它自己
        # 的 docstring。
        rel = scoped_path(scope, path)
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/file"
        async with self._make_client() as client:
            try:
                response = await client.get(
                    url,
                    params={"path": rel},
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                f"sandbox supervisor workspace read failed: {response.status_code} {response.text}"
            )
            if response.status_code == 413:
                # supervisor 侧的 WorkspaceFileTooLargeError 映射成 413(见
                # sandbox_supervisor/app.py 的 exception handler)—— 在这里
                # 还原成同名窄类型,和 NasWorkspaceStore 的行为保持一致,
                # 下载端点才能把「太大」与「不存在」分开回 413。
                raise WorkspaceFileTooLargeError(msg)
            if response.status_code == 404:
                # B-84 —— 同一条理由的第二处:"不存在"是模型自己纠得过来的失败,
                # 其它状态码不是。合并成一句之后 ``file_ops`` 只能拿文本去猜。
                raise WorkspaceFileNotFoundError(msg)
            raise SandboxSupervisorError(msg)
        data = response.content
        if max_bytes is not None and len(data) > max_bytes:
            # 这个后端没有"读之前先 stat"的手段(HTTP 一次就把字节带回来了),
            # 只能读完再判。闸仍然生效, 只是省不掉那次传输。
            msg = f"workspace file {path!r} exceeds the {max_bytes}-byte read cap"
            raise WorkspaceFileTooLargeError(msg)
        return data

    async def list_dir(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        path: str = ".",
        max_entries: int = _DEFAULT_MAX_DIR_ENTRIES,
    ) -> WorkspaceDirListing:
        # 这个后端只有"整棵树的扁平清单"这一种原料 —— 两条已知偏差见
        # :func:`dir_listing_from_files`。
        rel = scoped_dir(scope, path)
        prefix = "/".join(scope_parts(scope))
        under = f"{prefix}/{rel}" if prefix and rel else (prefix or rel)
        entries = await self.list_files(tenant_id=tenant_id, user_id=user_id)
        return dir_listing_from_files(entries, rel=under, max_entries=max_entries)

    async def search_files(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        name_glob: str | None = None,
        content: str | None = None,
        max_results: int = _DEFAULT_SEARCH_RESULTS,
    ) -> WorkspaceSearchResult:
        require_search_terms(name_glob, content)
        candidates = [
            entry
            for entry in await self.list_files(tenant_id=tenant_id, user_id=user_id, scope=scope)
            if name_matches(entry, name_glob)
        ]
        if content is None:
            return search_result(candidates, max_results=max_results)
        hits: list[WorkspaceFileEntry] = []
        for entry in candidates:
            # 名字先筛再读 —— 内容搜在这个后端上是一次 HTTP 往返一个文件, 不先按
            # 名字收窄的话一次搜索就是一整棵树的下载。
            data = await self.read_file(
                tenant_id=tenant_id, user_id=user_id, path=entry.path, scope=scope
            )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # 二进制跳过, 不试着解码 —— 与 NAS 侧同一条规矩。
            if content in text:
                hits.append(entry)
        return search_result(hits, max_results=max_results)

    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID, scope: str = SCOPE_USER_ROOT
    ) -> list[WorkspaceFileEntry]:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/files"
        async with self._make_client() as client:
            try:
                response = await client.get(
                    url,
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                f"sandbox supervisor workspace list failed: {response.status_code} {response.text}"
            )
            raise SandboxSupervisorError(msg)
        body = response.json()
        return scope_view([_entry_from_wire(f) for f in body.get("files", [])], scope)

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/file"
        async with self._make_client() as client:
            try:
                response = await client.put(
                    url,
                    params={"path": path},
                    content=data,
                    headers={**_traced_headers(), "content-type": "application/octet-stream"},
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                f"sandbox supervisor workspace write failed: {response.status_code} {response.text}"
            )
            raise SandboxSupervisorError(msg)

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        url = f"{self.base_url}/v1/workspaces/{tenant_id}/{user_id}/file"
        async with self._make_client() as client:
            try:
                response = await client.request(
                    "DELETE",
                    url,
                    params={"path": path},
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = (
                "sandbox supervisor workspace delete failed: "
                f"{response.status_code} {response.text}"
            )
            raise SandboxSupervisorError(msg)

    async def delete_tree(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        # The supervisor HTTP API has no recursive delete, and that backend is
        # retired on the cluster (local dev / CI only). Raise rather than
        # no-op: the session purge hook records the failure in its audit
        # details, and the retention job's daily orphan scan is the NAS-side
        # backstop either way.
        msg = f"sandbox supervisor workspace backend has no recursive delete: {path!r}"
        raise SandboxSupervisorError(msg)

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        await self._post(
            f"/v1/workspaces/{tenant_id}/{user_id}:delete",
            json=None,
            expect_body=False,
        )

    async def _post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None,
        expect_body: bool = True,
    ) -> Mapping[str, Any]:
        async with self._make_client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}{path}",
                    json=json,
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({path}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = f"sandbox supervisor {path} failed: {response.status_code} {response.text}"
            raise SandboxSupervisorError(msg)
        if not expect_body:
            return {}
        data = response.json()
        if not isinstance(data, Mapping):
            msg = f"sandbox supervisor {path} returned a non-object body"
            raise SandboxSupervisorError(msg)
        return data


@dataclass
class RecordingWorkspaceStore:
    """In-memory :class:`WorkspaceStore` for dev / tests.

    Records the read / list / write / delete / mark-deleted calls and
    returns the pre-set fixtures. Set the matching ``*_error`` field to
    drive an error path.
    """

    workspace_file: bytes = b""
    #: B-84 —— 按**用户根相对**路径给内容, 让替身也能回答"你问的是哪个文件"。
    #: 没配到的路径回落 :attr:`workspace_file`(既有行为, 不动)。作用域相关的测试
    #: 必须能分出"读到了自己的文件"和"读到了随便什么文件", 而单一 ``workspace_file``
    #: 分不出来。
    workspace_file_contents: dict[str, bytes] = field(default_factory=dict)
    workspace_file_error: Exception | None = None
    workspace_files: list[WorkspaceFileEntry] = field(default_factory=list)
    workspace_list_error: Exception | None = None
    workspace_reads: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    workspace_writes: list[tuple[UUID, UUID, str, bytes]] = field(default_factory=list)
    workspace_write_error: Exception | None = None
    workspace_deletes: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    workspace_delete_error: Exception | None = None
    #: 留存链 B-27 — the ``(tenant_id, user_id, path)`` of each delete_tree call.
    workspace_tree_deletes: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    workspace_tree_delete_error: Exception | None = None
    #: Phase 3a — the ``(tenant_id, user_id)`` of each mark_deleted call.
    workspace_deletions: list[tuple[UUID, UUID]] = field(default_factory=list)
    workspace_deletion_error: Exception | None = None

    async def read_file(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        path: str,
        scope: str = SCOPE_USER_ROOT,
        max_bytes: int | None = None,
    ) -> bytes:
        # 作用域解析先跑, 再记 —— 一条越界路径必须在被记下之前就炸。替身要是只
        # 记不校验, 用它写的测试就会以为"越界读被拒了", 而拒它的其实是另一个实现。
        rel = scoped_path(scope, path)
        self.workspace_reads.append((tenant_id, user_id, rel))
        if self.workspace_file_error is not None:
            raise self.workspace_file_error
        data = self.workspace_file_contents.get(rel, self.workspace_file)
        if max_bytes is not None and len(data) > max_bytes:
            msg = f"workspace file {path!r} exceeds the {max_bytes}-byte read cap"
            raise WorkspaceFileTooLargeError(msg)
        return data

    async def list_files(
        self, *, tenant_id: UUID, user_id: UUID, scope: str = SCOPE_USER_ROOT
    ) -> list[WorkspaceFileEntry]:
        self.workspace_reads.append((tenant_id, user_id, ""))
        if self.workspace_list_error is not None:
            raise self.workspace_list_error
        return scope_view(self.workspace_files, scope)

    async def list_dir(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        path: str = ".",
        max_entries: int = _DEFAULT_MAX_DIR_ENTRIES,
    ) -> WorkspaceDirListing:
        if self.workspace_list_error is not None:
            raise self.workspace_list_error
        rel = scoped_dir(scope, path)
        prefix = "/".join(scope_parts(scope))
        under = f"{prefix}/{rel}" if prefix and rel else (prefix or rel)
        self.workspace_reads.append((tenant_id, user_id, under))
        return dir_listing_from_files(self.workspace_files, rel=under, max_entries=max_entries)

    async def search_files(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        scope: str = SCOPE_USER_ROOT,
        name_glob: str | None = None,
        content: str | None = None,
        max_results: int = _DEFAULT_SEARCH_RESULTS,
    ) -> WorkspaceSearchResult:
        require_search_terms(name_glob, content)
        if self.workspace_list_error is not None:
            raise self.workspace_list_error
        prefix = "/".join(scope_parts(scope))
        hits: list[WorkspaceFileEntry] = []
        for entry in scope_view(self.workspace_files, scope):
            if not name_matches(entry, name_glob):
                continue
            if content is not None:
                full = f"{prefix}/{entry.path}" if prefix else entry.path
                try:
                    text = self.workspace_file_contents.get(full, self.workspace_file).decode(
                        "utf-8"
                    )
                except UnicodeDecodeError:
                    continue  # 二进制跳过 —— 与另外两个实现同一条规矩。
                if content not in text:
                    continue
            hits.append(entry)
        return search_result(hits, max_results=max_results)

    async def write_file(self, *, tenant_id: UUID, user_id: UUID, path: str, data: bytes) -> None:
        if self.workspace_write_error is not None:
            raise self.workspace_write_error
        self.workspace_writes.append((tenant_id, user_id, path, data))

    async def delete_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        if self.workspace_delete_error is not None:
            raise self.workspace_delete_error
        self.workspace_deletes.append((tenant_id, user_id, path))

    async def delete_tree(self, *, tenant_id: UUID, user_id: UUID, path: str) -> None:
        if self.workspace_tree_delete_error is not None:
            raise self.workspace_tree_delete_error
        self.workspace_tree_deletes.append((tenant_id, user_id, path))

    async def mark_deleted(self, *, tenant_id: UUID, user_id: UUID) -> None:
        if self.workspace_deletion_error is not None:
            raise self.workspace_deletion_error
        self.workspace_deletions.append((tenant_id, user_id))
