"""Image content blocks and resolution for multimodal input — Stream J.6.

A run message carries an uploaded image as an ``image_ref`` content
block — ``{"type": "image_ref", "ref": "expert_work://image/..."}`` — instead
of inline base64, which would bloat every checkpoint snapshot. The
provider adapters resolve the reference to bytes through an
:class:`ImageResolver` only at the moment they build the wire payload.

This module is a leaf — block helpers + the resolver interface, no
orchestrator-internal imports — so the ``llm`` adapter layer can import
it without an ``llm ↔ tools`` cycle.

See ``docs/streams/STREAM-J-DESIGN.md`` § 13.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import os
import stat
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from expert_work.persistence import is_rendered_figure_rel
from expert_work.protocol.multimodal import (
    IMAGE_REF_PREFIX,
    WORKSPACE_REF_PREFIX,
    parse_image_ref,
    parse_workspace_image_ref,
)
from expert_work.runtime.storage.base import ObjectStore

#: ``content`` block discriminator for an uploaded-image reference.
IMAGE_REF_BLOCK_TYPE: Final = "image_ref"

#: Image media type per file extension — the J.6 supported set. The
#: upload endpoint sets the extension from the validated content type,
#: so every reference resolves to exactly one of these.
_MEDIA_TYPE_BY_EXT: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: B-64 —— per-file read cap for :class:`NasWorkspaceImageResolver`. Mirrors
#: ``orchestrator.tools.nas_workspace_store._MAX_READ_BYTES`` /
#: ``sandbox_supervisor.supervisor._MAX_ARTIFACT_BYTES`` (same 64 MiB value,
#: re-declared rather than imported — those two already independently
#: re-declare the same constant for the same reason: the three modules have
#: no runtime dependency on each other, and a contract test is what would
#: catch a drift, not a shared import). This guards against an outsized file
#: (a mis-tagged video, a multi-GB upload) being read fully into the
#: control-plane process's memory before a size check ever runs — not
#: against a normal rendered page, which is tens to a few hundred KB at the
#: spec's 100 dpi default (§7.1).
_MAX_WORKSPACE_IMAGE_BYTES: Final = 64 * 1024 * 1024


def image_ref_block(uri: str) -> dict[str, str]:
    """Build the ``image_ref`` content block for a ``expert_work://image/...`` URI."""
    return {"type": IMAGE_REF_BLOCK_TYPE, "ref": uri}


def split_human_content(content: str | Sequence[object]) -> tuple[str, list[str]]:
    """Split a ``HumanMessage.content`` into ``(text, image-ref URIs)``.

    ``content`` is either a plain string or a LangChain block list. Text
    blocks are concatenated; ``image_ref`` blocks contribute their URI.
    """
    if isinstance(content, str):
        return content, []
    text_parts: list[str] = []
    image_refs: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, Mapping):
            if block.get("type") == IMAGE_REF_BLOCK_TYPE:
                ref = block.get("ref")
                if isinstance(ref, str):
                    image_refs.append(ref)
            else:
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
    return "".join(text_parts), image_refs


@dataclass(frozen=True)
class ResolvedImage:
    """Image bytes + media type, resolved from an ``image_ref``."""

    media_type: str
    data: bytes = field(repr=False)

    @property
    def base64_data(self) -> str:
        """Base64-encoded image payload (ASCII)."""
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_uri(self) -> str:
        """``data:`` URI form — the OpenAI ``image_url`` wire shape."""
        return f"data:{self.media_type};base64,{self.base64_data}"


@runtime_checkable
class ImageResolver(Protocol):
    """Resolves a ``expert_work://image/...`` reference to image bytes."""

    async def resolve(self, ref: str) -> ResolvedImage:
        """Fetch the referenced image.

        Raises an error if the reference is malformed or the object is
        missing — the exact type is implementation-specific.
        """


@dataclass(frozen=True)
class InMemoryImageResolver:
    """:class:`ImageResolver` over a fixed ``ref -> ResolvedImage`` map — tests."""

    images: Mapping[str, ResolvedImage] = field(default_factory=dict)

    async def resolve(self, ref: str) -> ResolvedImage:
        try:
            return self.images[ref]
        except KeyError as exc:
            msg = f"no image for ref {ref!r}"
            raise KeyError(msg) from exc


@dataclass(frozen=True)
class ObjectStoreImageResolver:
    """:class:`ImageResolver` backed by an :class:`ObjectStore` — Stream J.6.

    The media type is derived from the reference's file extension:
    ``ObjectStore.get`` returns only bytes, and the upload endpoint sets
    the extension from the validated content type.
    """

    store: ObjectStore

    async def resolve(self, ref: str) -> ResolvedImage:
        image_ref = parse_image_ref(ref)
        media_type = _MEDIA_TYPE_BY_EXT.get(image_ref.ext.lower())
        if media_type is None:
            msg = f"unsupported image extension {image_ref.ext!r} in ref {ref!r}"
            raise ValueError(msg)
        data = await self.store.get(image_ref.storage_key)
        return ResolvedImage(media_type=media_type, data=data)


@dataclass(frozen=True)
class NasWorkspaceImageResolver:
    """:class:`ImageResolver` over the NAS-mounted workspace volume —— B-64。

    control-plane 已经挂着 ``/mnt/workspaces``,所以渲染页的字节直接从盘上读,
    不用走沙箱 ``exec`` 的 stdout(一页 base64 约 83 KB,没必要塞进管道)。

    **TOCTOU —— 同 ``orchestrator.tools.nas_workspace_store`` 模块头注释的
    判断,威胁模型逐字适用。** 这棵 NAS 树是沙箱直接写、同一个 tenant/user
    下的 agent 自己也能落文件的树:一个恶意 run 可以在**自己的**子树里种一条
    symlink(比如 ``evil.jpg -> ../../<other-tenant>/<user>/x.png``),再拿
    自己的 tenant/user 调 ``ask_image``——``parse_workspace_image_ref`` 看不
    出问题(它验的是**这条 ref 字符串**本身:相对路径、没有 ``..``、不碰保留
    段;symlink 指向别处不改变字符串的形状),只有真去访问文件系统那一刻才能
    拦。所以这里不走"``Path.resolve()`` 校验一次、再拿字符串路径重新
    open"的写法——检查和真正打开之间的窗口里,任何中间分量都可能被并发种
    进来的 symlink 换掉,不只是叶子,那样的重检查关不上这条洞。照搬
    ``nas_workspace_store.py`` 的 openat/``O_NOFOLLOW`` dir_fd 链:从可信前缀
    ``{root}/{tenant_id}/{user_id}``(两个 id 已经是
    :func:`~expert_work.protocol.multimodal.parse_workspace_image_ref` 校验
    过的 UUID,不是攻击者能塞进来的路径文本)开始,``rel`` 的每一段、包括
    叶子,都用 ``O_NOFOLLOW`` openat——真的是 symlink,open 直接报错,不会
    跟着走,拿到的目录 fd 也钉在打开时的 inode 上,后续任何改名/换链接都
    动不了已经握着的 fd。

    没有照抄 ``retention_cleanup_job/workspace_files.py`` 更轻的
    ``resolve()`` + ``is_relative_to()`` + 叶子 ``lstat()`` 写法,是因为两者
    的威胁模型不同:那条路径删的是**登记过**的产物路径,由平台自己的清理
    job 低频触发,该模块自己的注释也承认"删除是不可逆的,多一次 lstat 便
    宜"——多出的检查-到-访问窗口是可接受的残余风险。这里是**模型每次调用
    ``ask_image`` 都会触发**的读,ref 字符串**完全由上游 agent/模型给
    定**,正是 ``nas_workspace_store.py`` 自己 ``read_file`` 要防的那个场景
    ——用它同款、真正消除竞态的做法,而不是只收窄窗口的那一种。

    Stat-before-read(:data:`_MAX_WORKSPACE_IMAGE_BYTES`)+ 全部 IO 走
    :func:`asyncio.to_thread`:NFS 上的阻塞系统调用不能占用 control-plane 的
    事件循环,理由与 ``NasWorkspaceStore`` 完全一致。fstat 同一次结果还兼两件
    事:非 ``S_ISREG``(FIFO / socket / device)直接拒绝——不这样做的话,一个
    agent 在自己工作区里放一个没有 writer 的 FIFO 就能把 open/read 永远卡住,
    钉死共享 ``asyncio.to_thread`` 线程池里的一个 worker;读文件走
    ``handle.read(cap + 1)`` 而不是无界 ``handle.read()``——沙箱直写这棵树,
    agent 自己控制自己的文件,fstat 之后再把文件写大这条竞态是刻意可达的,
    不只是意外。
    """

    root: Path

    async def resolve(self, ref: str) -> ResolvedImage:
        parsed = parse_workspace_image_ref(ref)
        media_type = _MEDIA_TYPE_BY_EXT[parsed.ext]
        data = await asyncio.to_thread(
            _read_workspace_leaf, self.root, parsed.tenant_id, parsed.user_id, parsed.rel, ref
        )
        return ResolvedImage(data=data, media_type=media_type)


def _read_workspace_leaf(root: Path, tenant_id: UUID, user_id: UUID, rel: str, ref: str) -> bytes:
    """Synchronous body of :meth:`NasWorkspaceImageResolver.resolve`.

    Runs inside :func:`asyncio.to_thread` — see that method's docstring for
    the symlink-safety rationale (mirrors ``nas_workspace_store.py``'s
    ``read_file``, minus the create/mkdir/delete branches this resolver never
    needs).
    """
    # tenant_id/user_id 已经是校验过的 UUID,不是攻击者路径文本 —— 同
    # nas_workspace_store.py 对这一段前缀的信任论证,按普通路径字符串直接
    # open 是安全的;只有 rel 的每一段需要 dir_fd 链。
    user_root = (root / str(tenant_id) / str(user_id)).resolve()
    try:
        dfd = os.open(user_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise FileNotFoundError(f"workspace image ref not found: {ref!r}") from exc
    parts = PurePosixPath(rel).parts
    dfd = _walk_to_parent_fd(dfd, parts[:-1], ref)
    try:
        try:
            # O_NONBLOCK —— 一个 FIFO 用阻塞模式 open 会卡在这一句本身(等一个
            # 永远不会出现的 writer),连下面的 S_ISREG 检查都走不到。这个标志
            # 对普通文件是空操作(POSIX),合法路径的行为不变;只有非常规文件
            # 才会感觉到差别,而那正是下面要拒绝的对象。
            leaf_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError as exc:
            raise _nofollow_open_error(exc, dfd, parts[-1], ref) from exc
    finally:
        os.close(dfd)
    with os.fdopen(leaf_fd, "rb") as handle:
        # Stat before reading so an over-cap file never gets fully loaded
        # into memory — same reasoning as nas_workspace_store.read_file.
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            # FIFO / socket / device (a symlink already can't reach here —
            # O_NOFOLLOW rejected it above). Opening *or reading* a FIFO with
            # no writer blocks indefinitely — that would pin a worker in the
            # shared asyncio.to_thread pool forever; refused here, before any
            # read is attempted, using the same fstat this function already
            # takes for the size cap.
            msg = f"workspace image ref is not a regular file: {ref!r}"
            raise ValueError(msg)
        if st.st_size > _MAX_WORKSPACE_IMAGE_BYTES:
            msg = (
                f"workspace image ref exceeds the {_MAX_WORKSPACE_IMAGE_BYTES}-byte "
                f"read cap: {ref!r}"
            )
            raise ValueError(msg)
        # Bounded read, not handle.read(): a writer that grows the file
        # *after* the fstat above (the sandbox writes this tree directly,
        # and an agent controls its own files — this race is reachable on
        # purpose) would otherwise still be fully buffered into memory.
        # Reading one byte past the cap is enough to detect the overrun
        # without ever holding more than cap+1 bytes.
        data = handle.read(_MAX_WORKSPACE_IMAGE_BYTES + 1)
        if len(data) > _MAX_WORKSPACE_IMAGE_BYTES:
            msg = (
                f"workspace image ref exceeds the {_MAX_WORKSPACE_IMAGE_BYTES}-byte "
                f"read cap: {ref!r}"
            )
            raise ValueError(msg)
        return data


def _walk_to_parent_fd(dfd: int, components: tuple[str, ...], ref: str) -> int:
    """Step through ``components`` one ``O_NOFOLLOW`` openat at a time, closing
    each fd behind us — mirrors ``nas_workspace_store._walk_dir_fd``.

    Returns the final parent directory fd (the caller owns it). On a raised
    exception, every fd this function opened — including the one passed in
    — has already been closed; there is nothing left for the caller to clean
    up.
    """
    for component in components:
        try:
            nfd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
        except OSError as exc:
            raise _nofollow_open_error(exc, dfd, component, ref, close_dfd=True) from exc
        os.close(dfd)
        dfd = nfd
    return dfd


def _nofollow_open_error(
    exc: OSError, dfd: int, name: str, ref: str, *, close_dfd: bool = False
) -> Exception:
    """Translate an ``OSError`` from an ``O_NOFOLLOW`` open into the right
    refusal — a symlink at ``name`` is a **safe refusal** (``ValueError``),
    not "file doesn't exist" (``FileNotFoundError``).

    ``O_NOFOLLOW``'s errno for "it's a symlink" is not portable (Linux:
    ``ELOOP``; macOS's ``O_DIRECTORY | O_NOFOLLOW``: ``ENOTDIR``, which is
    also the errno for an ordinary typo — a plain file where a directory was
    expected), so this asks ``lstat`` directly rather than trusting one
    errno value across platforms — same reasoning, same fix, as
    ``nas_workspace_store._is_symlink_at``: production runs Linux, but a
    check that only holds on Linux verifies nothing on a macOS dev box or a
    macOS CI runner.
    """
    escaped = exc.errno == errno.ELOOP or _is_symlink_at(dfd, name)
    if close_dfd:
        os.close(dfd)
    if escaped:
        return ValueError(f"workspace image ref escapes the user root: {ref!r}")
    return FileNotFoundError(f"workspace image ref not found: {ref!r}")


def _is_symlink_at(dfd: int, name: str) -> bool:
    """``name`` under ``dfd`` — is it a symlink? Asked once, only on the error path."""
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dfd).st_mode)
    except OSError:
        return False


@dataclass(frozen=True)
class DispatchingImageResolver:
    """:class:`ImageResolver` that routes by ref scheme —— B-64。

    有且只有**一个** ``ImageResolver`` 实例贯穿全程:
    ``control_plane.runtime.make_image_resolver`` 造出它,两个 provider
    适配器(``openai.py`` / ``anthropic.py``)与 ``AskImageTool`` 共享同一个
    引用。给 B-64 加第二种 ref scheme(工作区渲出来的文档页)不能要求每个
    消费方都另外接一根线——那样漏接一处就是那一条路径永远拿不到字节;正确
    做法是把这**一个**实例本身改宽,让它认得两种 scheme。``ImageResolver`` 是
    Protocol,所以这是"加一个实现",不是"改接口"。

    ``workspace`` 为 ``None`` 时(部署没接 NAS,``settings.workspace_nas_root``
    未配)工作区 ref 报一个说明性的 ``ValueError``,而不是落进 ``uploads`` 那支
    然后被它自己的 scheme 校验拒掉——错误信息应该说「工作区没接」,不是「不是
    合法的上传 ref」。
    """

    uploads: ImageResolver
    workspace: ImageResolver | None = None

    async def resolve(self, ref: str) -> ResolvedImage:
        if ref.startswith(IMAGE_REF_PREFIX):
            return await self.uploads.resolve(ref)
        if ref.startswith(WORKSPACE_REF_PREFIX):
            if self.workspace is None:
                msg = f"workspace image refs are not available in this deployment: {ref!r}"
                raise ValueError(msg)
            return await self.workspace.resolve(ref)
        msg = f"unrecognized image ref scheme: {ref!r}"
        raise ValueError(msg)


def _cache_every_ref(ref: str) -> bool:
    """Default :attr:`CachingImageResolver.should_cache` — the pre-B-64 behaviour."""
    return True


def is_cacheable_image_ref(ref: str) -> bool:
    """B-64 —— which refs :class:`CachingImageResolver` may remember.

    An upload ref (``expert_work://image/...``) is genuinely content-addressed:
    ``image_id`` is randomly generated per upload, so the same id can only ever
    name the same bytes — caching it has no "goes stale" case. A workspace ref
    is not uniformly like that: :func:`~expert_work.protocol.multimodal.parse_workspace_image_ref`
    only validates the *shape* of ``rel`` (relative, no ``..``, not a reserved
    tree) — it accepts any relative path, not only the
    ``.tool_results/<run_id>/figures/<doc-sha>/<render-sha>/_u<unit>/page-NN.jpg``
    convention the rendering pipeline actually writes. Nothing stops a workspace
    ref from naming an ordinary, overwritable user file instead (``chart.png`` at
    the workspace root); caching *that* would mean a later overwrite silently
    keeps serving the old bytes, process-wide, for the rest of this resolver's
    lifetime — no TTL, no invalidation path. So only a workspace ref matching
    that rendered-page **shape**
    (:func:`~expert_work.persistence.is_rendered_figure_rel`) is cacheable;
    every other workspace ref resolves fresh on every call.

    B-64 回修 C1 —— 判据要落在**作用域前缀之后**。绑了 agent 的 run 里,
    ``parsed.rel`` 形如 ``agents/<agent_key>/.tool_results/...``——``rel`` 整串
    直接比 ``WORKSPACE_OVERFLOW_DIR`` 前缀,``agents/`` 段会让每一条比较全部落
    空,于是绑了 agent 的渲染页永远判成"不可缓存",每次都要重新走一次 NAS 读。
    这不是正确性 bug(未缓存只是慢,不是错),但完全背离了这个函数存在的理由。

    B-64 回修第 2 轮 New-4 —— 剥前缀不能按**字节长度**砍。``parsed.rel`` 不
    保证被归一化过:``parse_workspace_image_ref`` 只是拿
    ``PurePosixPath(rel).parts`` 去解 ``agent_key``,从没把归一化结果写回
    ``rel`` 字段(下面这行原来的注释"``rel`` 恒以这个前缀开头"是错的)。一个
    形如 ``agents/./k1/.tool_results/...`` 或 ``agents//k1/...`` 的 ``rel``
    (``agent_key`` 依然正确解成 ``k1``)按 ``len("agents/k1/")`` 这样的字节数
    去切,切到的不是真正的前缀边界。**没有安全影响**(切错之后 ``tail`` 对
    不上 ``WORKSPACE_OVERFLOW_DIR`` 前缀,只会让判据回落到"不可缓存"这一
    支,不会反向产出错误的 ``True``),纯粹是缓存命中率的问题。改用
    ``PurePosixPath`` 重新分段,按**分段数**砍前两段,不受这类写法影响。

    B-64 回修第 3 轮 Minor-4 —— 上一轮只把 ``agent_key is not None`` 那一支
    改成了归一化的分段切法,``agent_key is None`` 那一支仍然直接用原始
    ``parsed.rel`` 字符串。同样没有安全影响(只会让判据偏保守地回落到"不可
    缓存"),但两支各写一套算法本身就是新的不一致,且不受这条 docstring 的
    "改用 PurePosixPath 重新分段"这句话覆盖。统一成一个表达式:未绑 agent
    时跳过的段数是 0,绑了 agent 时是 2(``agents/<agent_key>``),分段数量
    照样不受 ``.``/``//`` 这类写法影响。

    B-64 回修第 4 轮 New-I1 —— 这段 docstring 原来把渲染落点称作 **write-once**
    并以此论证它可缓存,而那个前提当时并不成立:产出目录只按 ``(run_id, 文档
    路径)`` 算,同一条路径换了内容会拿到逐字相同的 ``rel``,渲染又是每次原地
    重写,于是"文档被覆盖 → 再渲一次 → 模型读到的仍是缓存里旧文档那一页"是
    端到端跑得通的。现在 ``read_page`` 把**文档内容的哈希**也拼进了产出路径
    (``<render-sha>`` 那一段,见 :mod:`orchestrator.tools.read_page` 模块
    docstring 第 6 条),所以这里能给出的事实陈述是:这条 ``rel`` 是从"渲染输入
    (源文档字节 + dpi)+ 页号"派生的,**绝大多数情况下同一条 rel 就是同一份源
    文档同一页的渲染结果**;源文档内容一变,``rel`` 跟着变,缓存条目自然失效而
    不是被悄悄覆盖。还要注意这**不等于**那个文件从此不会被重写 —— 内容没变时
    重复调用会原地重渲一遍(soffice 每次产出的 pdf 未必逐字节相同),但重渲出来
    的仍是同一份源文档的同一页,所以缓存服务的旧字节与磁盘上的新字节画的是同一
    个东西,这才是可缓存成立的真正理由。

    B-64 回修第 5 轮 I-1 —— 判据从"整棵 ``.tool_results/`` 的**目录前缀**通行证"
    收窄成"认 ``read_page`` 自己那段**路径形状**"。上一轮把前者记成"今天没有触发
    面",那是**枚举不全**:除了 ``read_page`` 和溢出缓存,还有第三个写入者 ——
    **模型自己**。``.tool_results`` 不在任何写保护集合里
    (``WORKSPACE_RESERVED_PREFIXES`` 只管浏览面隐藏,``_WRITE_TOOLS`` 只挡
    ``shared:``),所以 ``write_file`` 对 ``.tool_results/evil.jpg`` 是放行的;
    模型再把那条 ref 交给 ``ask_image``,租户/用户/agent_key 三项校验全都对得上
    (文件就是它自己写的),于是"写 A → 读到 A → 覆盖成 B → **仍然读到 A**"
    端到端成立,与 New-I1 逐字同病。形状判据是单一真源,和 ``read_page`` 拼路径
    用的是同一组常量(:data:`~expert_work.persistence.RENDERED_FIGURE_DIR` 那一组)。

    **代价是预期的**:``ask_image`` 读模型自己写的图片,现在一律不进缓存、每次
    重新读盘。这是**对的** —— 那种文件随时可被覆盖,缓存它就等于发旧字节。慢一点
    是这条路正确的样子。

    **上面那句为什么不是"只会"(回修第 5 轮 M-2 / M-3)—— 两个已知的破绽:**

    * **TOCTOU**:哈希取在转换**之前**,``soffice``/``pdftoppm`` 在**之后**才去读
      同一个文件。``/workspace`` 是 NAS 上的共享目录,同一 ``(tenant, user,
      agent)`` 的另一条并发 run、或同一个沙箱里的 ``bash``/``exec_python``,
      都能在这个窗口里把文件换掉 —— 那样算出的 ``<render-sha>`` 属于旧字节,渲
      出来的页却是新字节。窗口 = 哈希读完到 soffice 打开文件之间。这条路上没有
      任何锁(``read_page`` 不持 workspace 锁),所以这是个真窗口,不是理论值。
    * **哈希碰撞**:``<render-sha>`` 是 sha256 取前 16 位 hex = 64 位。对**意外**
      碰撞而言这远够用;但文件内容是**模型自己写的**,对抗场景下生日界是
      ``~2^32`` 而不是 ``2^64`` —— 一个刻意构造的攻击者能在可行算力内造出撞同
      一段哈希的两份内容。这不是今天的威胁模型(能写文件的模型已经在自己的工作
      区里了),但"只会"这个词担不起它,所以这里说的是"绝大多数情况下"。
    """
    if not ref.startswith(WORKSPACE_REF_PREFIX):
        return True
    parsed = parse_workspace_image_ref(ref)
    skip = 2 if parsed.agent_key is not None else 0
    tail = "/".join(PurePosixPath(parsed.rel).parts[skip:])
    return is_rendered_figure_rel(tail)


@dataclass
class CachingImageResolver:
    """Wraps an :class:`ImageResolver` with a bounded LRU cache.

    A resolved image is only remembered when ``should_cache(ref)`` says so
    (default :func:`_cache_every_ref` — cache everything, the pre-B-64
    behaviour: every upload ref really is content-addressed + immutable, so a
    resolved image never goes stale, and a ref identifies exactly one image,
    so a ``ref -> ResolvedImage`` cache can only ever return that ref's own
    bytes — it never exposes anything a direct ``resolve(ref)`` would not).
    B-64 added a second ref scheme that is *not* uniformly immutable —
    :func:`is_cacheable_image_ref` is the predicate ``make_image_resolver``
    passes in production to exclude the refs that could go stale; see its
    docstring. A ref that fails the predicate is resolved fresh on every
    call, exactly like an ordinary cache miss that is never stored.

    Without caching, every LLM turn of a run re-fetches every image in the
    whole history from the object store — ``_human_content`` re-walks all
    messages on each ``complete`` call, and this resolver outlives individual
    turns, so the cache spans them.

    Failures are not cached (a raised lookup re-runs next time). ``max_size``
    bounds retained images so the cache cannot grow without limit.
    """

    inner: ImageResolver
    max_size: int = 32
    should_cache: Callable[[str], bool] = _cache_every_ref
    _cache: OrderedDict[str, ResolvedImage] = field(
        default_factory=OrderedDict, init=False, repr=False
    )

    async def resolve(self, ref: str) -> ResolvedImage:
        cached = self._cache.get(ref)
        if cached is not None:
            self._cache.move_to_end(ref)
            return cached
        resolved = await self.inner.resolve(ref)
        if self.should_cache(ref):
            self._cache[ref] = resolved
            self._cache.move_to_end(ref)
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)
        return resolved
