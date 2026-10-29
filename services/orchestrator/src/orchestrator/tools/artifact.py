"""``save_artifact`` / ``list_artifacts`` tools — Stream J.9.

Lets an agent explicitly register the named files it produces in the
persistent workspace (Mini-ADR J-11 — explicit registration, never an
auto-scan of the workspace). ``save_artifact`` records one revision in
the :class:`ArtifactStore`; ``list_artifacts`` reads the user's
artifacts back. Both are tenant- *and* user-scoped — an artifact
belongs to a ``(tenant, user)`` pair, so a run with no user binding
cannot use them.

``save_artifact`` **stats the file before registering it**, then (B-235)
**copies its bytes at that moment** into a per-version snapshot outside the
sandbox's reach (:mod:`orchestrator.tools.artifact_snapshots`) and records the
copy's key, size and sha256 on the version row. Before B-235 the row only
pointed at the workspace path: two saves of the same name pointed at the same
file, so a later save silently replaced every earlier version's content. No
copy, no row — a registration whose content cannot be served later is worse
than an error the model can act on. Content download is a control-plane
endpoint (STREAM-J-DESIGN § 10).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, get_args
from uuid import UUID, uuid4

from expert_work.persistence import ArtifactStore
from expert_work.protocol import ArtifactKind
from orchestrator.tools.artifact_snapshots import (
    MAX_SNAPSHOT_BYTES,
    ArtifactSnapshotStore,
    SnapshotDigest,
)
from orchestrator.tools.file_ops import FileOpError, build_artifact_locate_wrapper, parse_envelope
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.tools.sandbox import (
    SandboxRuntime,
    SandboxSupervisorError,
    WorkspaceFileChangedError,
    WorkspaceFileTooLargeError,
    WorkspacePathEscapeError,
    run_in_sandbox,
)
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW
from orchestrator.tools.workspace_paths import AGENTS_DIR, SHARED_PREFIX, agent_view_alias

logger = logging.getLogger(__name__)

#: The artifact kinds the manifest's ``ArtifactKind`` literal allows.
_ARTIFACT_KINDS: tuple[str, ...] = get_args(ArtifactKind)
_DEFAULT_KIND: ArtifactKind = "other"
#: Thread label for the ``created_in_thread`` column when a run has no id.
_FALLBACK_THREAD_ID = "save-artifact"
#: ``shared/`` 目录名 —— 由前缀反推,不写第二份字面量(前缀与目录必须永远同名)。
_SHARED_DIR_NAME = SHARED_PREFIX.rstrip(":")

#: 产物清单契约 —— per-run 产物记录器的 ``config["configurable"]`` 键。
#: ``sse.run_agent`` 每 run 注入一个同步 append 回调(镜像
#: ``WORKER_EVENT_SINK_KEY`` 的注入/读取惯例);``save_artifact`` 每次成功
#: 登记喂一条清单项。键定义在 tools 层与 sink keys 同理由:builder 与
#: sse 都要 import,而 tools 不 import 它们。
ARTIFACT_RECORDER_KEY = "artifact_manifest_recorder"
#: B-226 —— builder 据此判断本 agent 有没有登记交付物这项能力(收尾指令 / 步数预警
#: 只在有时才提登记)。
SAVE_ARTIFACT_TOOL_NAME = "save_artifact"


class ArtifactRecorder:
    """产物清单契约 —— 本 run 的产物登记累积器(``ARTIFACT_RECORDER_KEY`` 注入的那个对象)。

    name 去重、后登记覆盖(与前端产物 chips 的 newest-wins 同义)。调用它 = 登记一条。
    子代拿到的是 :meth:`scoped` 分出来的视图:写进同一份清单(整棵委派树的登记都在
    end 帧里),``count`` 数的是**自己这棵子树**登记过的名字(自己的 + 自己委派出去的子代
    的)—— B-226 ① 收尾指令里的「本 run 登记了几个产物」读它:父的收尾算上子代交付的,
    子代撞上限时不会把父或兄弟登记的算成自己的。
    """

    def __init__(
        self,
        entries: dict[str, dict[str, Any]] | None = None,
        parent: ArtifactRecorder | None = None,
    ) -> None:
        self._entries: dict[str, dict[str, Any]] = {} if entries is None else entries
        self._parent = parent
        self._subtree: set[str] = set()

    def __call__(self, entry: dict[str, Any]) -> None:
        name = str(entry.get("name"))
        self._entries[name] = entry
        node: ArtifactRecorder | None = self
        while node is not None:
            node._subtree.add(name)
            node = node._parent

    def scoped(self) -> ArtifactRecorder:
        """给委派出去的子代:同一份清单, 计数只含它自己的子树(也计进本视图)。"""
        return ArtifactRecorder(self._entries, parent=self)

    @property
    def count(self) -> int:
        return len(self._subtree)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._entries.values())


def _require_user_scope(ctx: ToolContext, tool: str) -> tuple[UUID, UUID]:
    """Return ``(tenant_id, user_id)`` or raise — artifacts are per-user."""
    if ctx.tenant_id is None or ctx.user_id is None:
        msg = f"{tool} requires a tenant + user binding"
        raise ToolBlockedError(msg)
    return ctx.tenant_id, ctx.user_id


def _require_str(args: Mapping[str, Any], key: str, tool: str) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        msg = f"{tool} requires a non-empty {key!r} string"
        raise ValueError(msg)
    return raw.strip()


def _validate_path(path: str, *, agent_key: str = "") -> str:
    """Normalise ``path`` to an agent-relative workspace path, or reject it.

    Folds the two absolute spellings models produce from the tool
    descriptions — ``/workspace/x`` and ``/workspace/agents/<own key>/x`` —
    to ``x`` (same rule as ``file_ops._require_path``); any other absolute
    path, a ``..`` segment, or an ``agents/`` / ``shared/`` first segment
    (someone else's tree, or the read-only shared area) is rejected. The
    result is later resolved against the user's workspace volume by the
    download endpoint, so nothing unnormalised may reach the store.
    """
    cleaned = path.strip()
    if agent_key:
        own = f"{agent_view_alias(agent_key)}/"
        if cleaned.startswith(own):
            cleaned = cleaned[len(own) :]
    if cleaned.startswith(f"{EXEC_VIEW}/"):
        cleaned = cleaned[len(EXEC_VIEW) + 1 :]
    parts = PurePosixPath(cleaned).parts
    if not cleaned or cleaned.startswith("/") or ".." in parts:
        msg = f"artifact path must be a relative workspace path without '..': {path!r}"
        raise ValueError(msg)
    if agent_key and parts and parts[0] in (AGENTS_DIR, _SHARED_DIR_NAME):
        msg = f"artifact path must not address the reserved {parts[0]}/ tree: {path!r}"
        raise ValueError(msg)
    return cleaned


def _coerce_kind(raw: object) -> ArtifactKind:
    if raw is None:
        return _DEFAULT_KIND
    if not isinstance(raw, str) or raw not in _ARTIFACT_KINDS:
        msg = f"save_artifact 'kind' must be one of {sorted(_ARTIFACT_KINDS)}"
        raise ValueError(msg)
    return raw  # type: ignore[return-value]  # membership-checked above


def _artifact_path(agent_key: str, path: str) -> str:
    """产物在工作区里的相对路径 —— 含 agent 前缀。

    这一列是下载端点真去读的**物理**路径(control-plane 拿它 join 用户根),
    所以它必须与 ``write_file`` 的落盘位置同一个口径:PR3 把文件工具的根改成
    ``agents/<agent_key>/`` 的同一个 PR 里,这里也跟着加前缀。早一步加(PR2)
    就是登记一个没有文件的路径 = 每次下载 404;晚一步就是反过来。

    形状**不含** ``artifacts/`` 段:``save_artifact`` 不搬字节,它只写一行登记,
    没有任何东西往那个目录写(spec §四 2026-09-12 勘误)。分类语义由
    ``artifact.kind`` 列承担。

    ``agent_key`` 为空(迁移期未升级的调用方 / 临时沙箱)时保持扁平路径,否则
    会拼出 ``agents//x`` 这种既非旧位置也非新位置的第三形状 —— 搬迁脚本
    (PR5 Task 11)两边都认不出来。
    """
    if not agent_key:
        return path
    return f"{AGENTS_DIR}/{agent_key}/{path}"


@dataclass
class SaveArtifactTool:
    """Registers a workspace file as a named artifact — ``save_artifact``."""

    store: ArtifactStore
    #: 沙箱执行通道 —— 登记前 stat 那个文件要用。**必填,没有默认值**:做成
    #: 可选开关的话,漏传的调用点会静默退回「不校验」,而那正是这条修复要堵的
    #: 洞(平台缺口改默认,不要设计成让人去配)。
    client: SandboxRuntime
    #: B-235 —— 每次登记都复制一份那一刻的字节。同样**必填**:没有它就退回「只记路径」,
    #: 那正是同名登记互相覆盖的洞。
    snapshots: ArtifactSnapshotStore

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=SAVE_ARTIFACT_TOOL_NAME,
            description=(
                "Register a file you produced in the workspace as a named "
                "artifact so the user can retrieve it later. Re-saving an "
                "existing name records a new version."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Logical artifact name, e.g. 'report.md'.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path of the file in the workspace "
                            "(optional; defaults to 'name')."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(_ARTIFACT_KINDS),
                        "description": "Content category (optional; defaults to 'other').",
                    },
                },
                "required": ["name"],
            },
            # Stream L.L6 — registers a new artifact version; concurrent
            # ``save_artifact`` calls on the same ``name`` would race
            # the version counter. ``path_args=("name",)`` lets the
            # scheduler parallelise saves to *different* names (the
            # common case) while serialising saves to the same name.
            path_args=("name",),
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        tenant_id, user_id = _require_user_scope(ctx, "save_artifact")
        name = _require_str(args, "name", "save_artifact")
        raw_path = args.get("path")
        path = raw_path if isinstance(raw_path, str) and raw_path.strip() else name
        rel = _validate_path(path, agent_key=ctx.agent_key)
        path_in_workspace = _artifact_path(ctx.agent_key, rel)
        kind = _coerce_kind(args.get("kind"))
        thread_id = str(ctx.run_id) if ctx.run_id is not None else _FALLBACK_THREAD_ID

        # 登记之前先确认文件真的在登记的那个位置。没有这一步,agent 喊一声
        # save_artifact 就能造出一条「列表里看得见、下载 404」的产物,而 run
        # 照样报 success —— 测试环境 2026-09-14 实测:exec_python 写了
        # ``/workspace/x.pptx``,save_artifact 登记 ``agents/<key>/x.pptx``,两边都
        # 「成功」,说的却不是同一个文件。B-60 之后 ``/workspace`` 就是 agent 自己的
        # 视图,这条 stat 只确认文件真的在那儿 —— 不在就是没写,不再去别处找。
        await self._locate_file(rel, ctx=ctx)

        # B-235 —— 先复制,再登记。副本以这里生成的版本行 id 命名,同一个 id 原样交给
        # ``save_version``:副本与版本行在这一次调用里配对,两段会话并发登记同名时,
        # 版本号由库里的原子 upsert 分配,但谁的副本归谁的行不会串。
        version_id = uuid4()
        snapshot_key = str(version_id)
        digest = await self._capture(
            tenant_id=tenant_id,
            user_id=user_id,
            rel=rel,
            path_in_workspace=path_in_workspace,
            snapshot_key=snapshot_key,
        )
        # B-50 PR3 —— ``path_in_workspace`` 带 agent 前缀,与 ``write_file`` 的
        # 落盘位置同一个口径(PR2 时有意暂缓,见 ``_artifact_path``)。
        try:
            version = await self.store.save_version(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_key=ctx.agent_key,
                name=name,
                kind=kind,
                path_in_workspace=path_in_workspace,
                created_in_thread=thread_id,
                version_id=version_id,
                snapshot_key=snapshot_key,
                size_bytes=digest.size_bytes,
                sha256=digest.sha256,
            )
        except BaseException:
            # 行没落下,副本就没人认领 —— 立刻删;删不掉的由留存作业的孤儿清理兜底。
            with contextlib.suppress(Exception):
                await self.snapshots.delete(
                    tenant_id=tenant_id, user_id=user_id, snapshot_key=snapshot_key
                )
            raise
        # 产物清单契约 —— 登记成功即计入本 run 的清单;created_at 优先取
        # 版本行的落库时间(server_default 场景可能缺席,退回登记时刻)。
        if ctx.artifact_recorder is not None:
            created = version.created_at or datetime.now(UTC)
            ctx.artifact_recorder(
                {
                    "name": name,
                    "kind": kind,
                    "version": version.version,
                    "created_at": created.isoformat(),
                }
            )
        return ToolResult(
            content=(
                f"Saved artifact {name!r} (kind={kind}) as version {version.version}. "
                "The user can now download it directly from the conversation; tell them "
                "it is ready and refer to it by name — do not fabricate a download link "
                "or URL (the interface renders the download for them)."
            ),
            meta={"artifact": name, "version": version.version, "kind": kind},
        )

    async def _capture(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        rel: str,
        path_in_workspace: str,
        snapshot_key: str,
    ) -> SnapshotDigest:
        """复制那一刻的字节;复制不成就是工具错误,什么都不登记。"""
        try:
            return await self.snapshots.capture(
                tenant_id=tenant_id,
                user_id=user_id,
                path=path_in_workspace,
                snapshot_key=snapshot_key,
            )
        except WorkspacePathEscapeError as exc:
            msg = f"save_artifact denied: {rel!r} escapes the workspace"
            raise ToolBlockedError(msg) from exc
        except WorkspaceFileTooLargeError as exc:
            limit_mib = MAX_SNAPSHOT_BYTES // (1024 * 1024)
            msg = (
                f"save_artifact: {rel!r} is larger than the {limit_mib} MiB artifact limit — "
                "nothing was registered. Make the file smaller (compress it, or split it into "
                "several artifacts) and save again."
            )
            raise FileOpError(msg) from exc
        except WorkspaceFileChangedError as exc:
            msg = (
                f"save_artifact: {rel!r} changed while it was being saved — nothing was "
                "registered. Make sure nothing is still writing the file, then save again."
            )
            raise FileOpError(msg) from exc
        except (SandboxSupervisorError, OSError) as exc:
            logger.warning("save_artifact.snapshot_failed rel=%s", rel, exc_info=True)
            msg = (
                f"save_artifact: could not keep a copy of {rel!r} — nothing was registered. "
                "Check that the file exists, then save again."
            )
            raise FileOpError(msg) from exc

    async def _locate_file(self, rel: str, *, ctx: ToolContext) -> None:
        """Confirm ``rel`` exists where the registration row will point, or raise.

        B-60 —— only stats inside the exec view (the agent's own directory once
        bound). Not found there means the file was never written — a tool error
        the model can act on: *you did not write that file*, which is
        recoverable, unlike handing the user a dead artifact. The #1551 "claim
        from the user root" branch is gone with the hole it papered over: exec
        code cannot reach a separate user root any more (spec §4.8).
        """
        outcome = await run_in_sandbox(
            self.client,
            code=build_artifact_locate_wrapper(rel),
            timeout_s=None,
            ctx=ctx,
            tool_label="save_artifact",
            fallback_thread_id="save_artifact",
        )
        env = parse_envelope(outcome, tool="save_artifact")
        if env.get("ok"):
            return
        error = env.get("error", "unknown")
        if error == "path_escapes_workspace":
            msg = f"save_artifact denied: {rel!r} escapes the workspace"
            raise ToolBlockedError(msg)
        if error == "not_found":
            msg = (
                f"save_artifact: no file at {rel!r} in your workspace — nothing was "
                "registered. Write the file first (and check the path you passed), "
                "then call save_artifact again."
            )
            raise FileOpError(msg)
        if error == "not_a_file":
            msg = f"save_artifact: {rel!r} is a directory, not a file — nothing was registered."
            raise FileOpError(msg)
        detail = env.get("detail", "")
        msg = f"save_artifact: could not stat {rel!r} ({error}{': ' + detail if detail else ''})"
        raise FileOpError(msg)


@dataclass
class ListArtifactsTool:
    """Lists the user's saved artifacts — ``list_artifacts``."""

    store: ArtifactStore

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_artifacts",
            description=(
                "List the named artifacts you have saved, with each one's kind and latest "
                "version. Scoped to you: artifacts saved by other agents working for the "
                "same user are not listed."
            ),
            parameters={"type": "object", "properties": {}},
            # Stream L.L6 — pure read.
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args  # list_artifacts takes no arguments
        tenant_id, user_id = _require_user_scope(ctx, "list_artifacts")
        # B-50 —— 只列本 agent 的。``agent_key`` 为空串(没绑 agent 的合成执行
        # 路径)时传 ``None`` 不过滤 —— 那条路径连 user_id 都没有,不存在跨 agent
        # 泄露;拼一个空串去过滤反而只会捞到归属不明的历史行。
        artifacts = await self.store.list_for_user(
            tenant_id=tenant_id, user_id=user_id, agent_key=ctx.agent_key or None
        )
        if not artifacts:
            return ToolResult(content="(no artifacts saved yet)", meta={"n_artifacts": 0})
        lines = [f"- {a.name} ({a.kind}, v{a.latest_version})" for a in artifacts]
        return ToolResult(content="\n".join(lines), meta={"n_artifacts": len(artifacts)})
