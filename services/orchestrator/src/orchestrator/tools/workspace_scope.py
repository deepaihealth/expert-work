"""宿主侧只读文件操作的作用域 —— B-84 PR-2b 的唯一翻译层。

``list_dir`` / ``read_file`` / ``search_files`` 的实现从沙箱 exec 挪到 control-plane
自己挂着的 NAS 之后,沙箱里那套「每次 exec 一个私有 ``/workspace``」(B-60,
:mod:`orchestrator.tools.exec_view`)在宿主侧没有对应物:宿主看到的是整棵用户树
``{root}/{tenant_id}/{user_id}/``。本模块把沙箱视图翻成用户根下的**前缀**,是这层
翻译的唯一真源。

三档,逐条对着 :data:`~orchestrator.tools.exec_view.EXEC_VIEW_SCRIPT` 抄的,不是照
感觉写的:

===============================  ===============================================
沙箱里的视图                      宿主侧前缀
===============================  ===============================================
绑了 agent:``/workspace``         ``agents/{agent_key}/``
  = bind(``{NAS_MOUNT}/agents/{agent_key}``)
绑了 agent:``/workspace/shared``  ``shared/``
  = 只读 bind(``{NAS_MOUNT}/shared``)
没绑 agent:``/workspace``         (空 —— 就是整个用户根)
  = bind(``{NAS_MOUNT}``)
===============================  ===============================================

**``agent_key`` 不是 agent 名。** ``sanitize_agent_key()``
(``expert_work.protocol.agent_key``)的产物是「净化后的名字 + 原始名字 sha256 的前
8 位」—— 叫 ``pf-probe`` 的 agent 在 NAS 上的目录是 ``pf-probe-33086dc0``。任何地方
手工拼 ``agents/<agent 名>`` 都会指向一个不存在的目录,而**失败形态是「目录是空的」
不是报错**,测试里非常容易看着像通过。

**路径怎么走不归这里管。** 本模块只回答「作用域的起点在哪」和「这个相对路径合不
合法」;真正防穿越的是
:meth:`~orchestrator.tools.nas_workspace_store.NasWorkspaceStore._open_parent_dir_fd`
的 ``openat`` 链(``O_NOFOLLOW``,一段一段走,不重走字符串路径)。两件事分开是有意
的:再写一套路径解析就是再写一遍逃逸面。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from expert_work.persistence import WORKSPACE_AGENTS_DIR, WORKSPACE_SHARED_DIR
from orchestrator.tools.sandbox import SandboxSupervisorError, WorkspacePathEscapeError
from orchestrator.tools.workspace_paths import require_safe_key

#: 整个用户根 —— 没绑 agent 时 ``/workspace`` 就是它。也是每个方法的默认值:
#: 浏览端点 / 产物下载 / 留存清扫本来就是这一档,它们一个字都不用改。
SCOPE_USER_ROOT = "user_root"

#: ``shared/`` 只读区(B-50 §7.1 的归属不明 legacy)。值就是目录名本身,不写第二份
#: 字面量 —— 作用域名与它指向的目录必须永远同名。
SCOPE_SHARED = WORKSPACE_SHARED_DIR

#: ``agent:<agent_key>`` 的前缀。
SCOPE_AGENT_PREFIX = "agent:"

#: 布局里的保留段。绑了 agent 的调用一律不许拿它们寻址 —— 与
#: ``file_ops._require_path`` 的同款规矩逐字同义(那里是工具层的闸,这里是 store 层
#: 的第二道)。不拒的话 ``agents/<别人的 key>/x`` 在**没绑 agent** 的作用域里就是一次
#: 合法的跨 agent 读,而绑了 agent 时它会静悄悄指向 ``agents/<自己>/agents/...``
#: 这种不存在的路径 —— 两种都该当场报错,不该让模型对着一个「文件不存在」瞎猜。
_RESERVED_HEADS = frozenset({WORKSPACE_AGENTS_DIR, WORKSPACE_SHARED_DIR})


def agent_scope(agent_key: str) -> str:
    """``agent_key`` 的作用域字符串。**要传 key,不是 agent 名** —— 见模块 docstring。"""
    return f"{SCOPE_AGENT_PREFIX}{agent_key}"


def normalize_workspace_path(path: str) -> tuple[str, tuple[str, ...]]:
    """The single source of truth for "what does this workspace path mean".

    Returns ``(relpath, parts)`` where ``parts`` is what the ``dir_fd`` walk
    steps through and ``relpath`` is ``"/".join(parts)`` — the canonical
    spelling every *guard* must compare against.

    Wave 2 final review (Critical 2) — before this existed, the guards in
    :meth:`NasWorkspaceStore.write_file` / :meth:`NasWorkspaceStore.
    delete_file` compared the **raw** input string while the actual
    filesystem walk used ``PurePosixPath(cleaned).parts``, which silently
    drops ``.`` segments. The two therefore answered differently for the
    same input: ``"./uploads/a.txt"`` did not look reserved to the guard,
    but landed on exactly ``uploads/a.txt`` on disk (measured, not
    reasoned — the file really was deleted). Normalising in one place and
    letting both the guard and the walk read *that* result is what makes
    the two structurally incapable of disagreeing; re-implementing the
    normalisation next to each guard would recreate the bug.

    ``PurePosixPath`` collapses ``.`` segments and duplicate slashes but
    never ``..``, so the ``..`` rejection below still sees every climb
    attempt. A URL-encoded traversal (``%2e%2e%2f``) is not decoded — it is
    just an odd filename, and stays one.

    Empty ``parts`` (``"."``, ``"./"``, ``".//"``) raises rather than
    falling through: the walk's ``parts[-1]`` would otherwise throw a bare
    ``IndexError`` straight past this store's error boundary, and
    ``/v1/workspace/file`` — which only catches
    :class:`SandboxSupervisorError` — would answer 500 where the supervisor
    backend answers 404 (the "错误类型统一" half of the parity contract in
    ``NasWorkspaceStore``'s module docstring).

    A NUL byte is rejected here for exactly the same reason (wave 2 final
    re-review, New 1). CPython refuses to pass an embedded NUL to any
    syscall and raises a bare :class:`ValueError` from deep inside
    :func:`os.open` — not an :class:`OSError`, so none of the ``except
    OSError`` wrappers downstream catch it, and ``GET /v1/workspace/file
    ?path=a%00b`` answered 500 where the supervisor backend answers 400.
    Same class as the empty-``parts`` case above, same fix, same place: the
    normaliser is where "is this string a workspace path at all" is decided.

    B-84 —— 从 ``nas_workspace_store`` 原样搬到这里(一行没改),因为
    :func:`scoped_path` 与三个 store 实现都要用它,而 ``nas_workspace_store``
    反过来要 import ``workspace_store``。搬家不是再写一份。
    """
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or "\0" in cleaned:
        raise WorkspacePathEscapeError(
            f"workspace path must be relative and free of '..': {path!r}"
        )
    parts = PurePosixPath(cleaned).parts
    if not parts or ".." in parts:
        raise WorkspacePathEscapeError(
            f"workspace path must be relative and free of '..': {path!r}"
        )
    return "/".join(parts), parts


def scope_parts(scope: str) -> tuple[str, ...]:
    """作用域 → 用户根下的前缀分段。未知作用域一律拒。

    ``agent_key`` 来自 ``config["configurable"]``,一路从 HTTP 载荷传下来,**不可
    信**:带 ``/`` 或 ``..`` 的值能把整个作用域撬出去(``{root}/agents/..`` 就是
    ``{root}``),所以过 :func:`~orchestrator.tools.workspace_paths.require_safe_key`
    的同一道闸 —— 不是这里再写一条正则。
    """
    if scope == SCOPE_USER_ROOT:
        return ()
    if scope == SCOPE_SHARED:
        return (WORKSPACE_SHARED_DIR,)
    if scope.startswith(SCOPE_AGENT_PREFIX):
        key = scope[len(SCOPE_AGENT_PREFIX) :]
        try:
            require_safe_key(key)
        except ValueError as exc:
            raise WorkspacePathEscapeError(f"workspace scope is not safe: {scope!r}") from exc
        return (WORKSPACE_AGENTS_DIR, key)
    raise SandboxSupervisorError(f"unknown workspace scope: {scope!r}")


def scoped_path(scope: str, path: str) -> str:
    """作用域内的相对路径 → **用户根**下的相对路径(文件用)。

    先把调用方给的 ``path`` 自己归一化一遍再拼前缀,顺序是有意的:先拼后归一化的
    话,``/etc/passwd`` 拼成 ``agents/k//etc/passwd``,``PurePosixPath`` 会把双斜杠
    吃掉、变成一个作用域内的合法相对路径 —— 一条绝对路径于是被静默改写成了别的东
    西,而不是被拒绝。
    """
    prefix = scope_parts(scope)
    _relpath, parts = normalize_workspace_path(path)
    _reject_reserved_head(prefix, parts, path)
    return "/".join((*prefix, *parts))


def scoped_dir(scope: str, path: str) -> str:
    """同 :func:`scoped_path`,但 ``path`` 可以是「作用域根自己」(目录用)。

    ``"."`` / ``"./"`` / 空串都表示作用域根,返回值因此可能是空串(用户根作用域下
    的根)。``list_dir`` 的 ``path`` 默认就是 ``"."``,而 :func:`normalize_workspace_path`
    对它是拒绝的(文件路径不能是 ``.``)—— 这两件事必须分开,不能让目录去迁就文件的
    判据。
    """
    prefix = scope_parts(scope)
    cleaned = path.strip()
    if cleaned.startswith("/") or "\0" in cleaned:
        raise WorkspacePathEscapeError(
            f"workspace path must be relative and free of '..': {path!r}"
        )
    if not cleaned or not PurePosixPath(cleaned).parts:
        return "/".join(prefix)
    return scoped_path(scope, cleaned)


def _reject_reserved_head(prefix: tuple[str, ...], parts: tuple[str, ...], path: str) -> None:
    """绑了 agent 时,不许拿 ``agents/`` / ``shared/`` 这两个布局保留段寻址。

    只在 agent 作用域下生效,与 ``file_ops._require_path`` 逐字同义:没绑 agent 时
    ``/workspace`` 本来就是整个用户根,``agents/<key>/x`` 在那一档是一条普通的合法
    路径(今天走沙箱也一样能读),这里不能顺手把它收紧成另一种语义。
    """
    if prefix[:1] != (WORKSPACE_AGENTS_DIR,):
        return
    if parts[0] in _RESERVED_HEADS:
        raise WorkspacePathEscapeError(
            f"workspace path must be relative to your own workspace; "
            f"{parts[0]!r} is a reserved layout segment: {path!r}"
        )
