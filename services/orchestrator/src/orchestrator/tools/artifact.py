"""``save_artifact`` / ``list_artifacts`` tools — Stream J.9.

Lets an agent explicitly register the named files it produces in the
persistent workspace (Mini-ADR J-11 — explicit registration, never an
auto-scan of the workspace). ``save_artifact`` records one revision in
the :class:`ArtifactStore`; ``list_artifacts`` reads the user's
artifacts back. Both are tenant- *and* user-scoped — an artifact
belongs to a ``(tenant, user)`` pair, so a run with no user binding
cannot use them.

The file *content* stays in the J.15 workspace volume; these tools
only touch metadata. Content download is a control-plane endpoint
(STREAM-J-DESIGN § 10).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, get_args
from uuid import UUID

from expert_work.persistence import ArtifactStore
from expert_work.protocol import ArtifactKind
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

#: The artifact kinds the manifest's ``ArtifactKind`` literal allows.
_ARTIFACT_KINDS: tuple[str, ...] = get_args(ArtifactKind)
_DEFAULT_KIND: ArtifactKind = "other"
#: Thread label for the ``created_in_thread`` column when a run has no id.
_FALLBACK_THREAD_ID = "save-artifact"

#: 产物清单契约 —— per-run 产物记录器的 ``config["configurable"]`` 键。
#: ``sse.run_agent`` 每 run 注入一个同步 append 回调(镜像
#: ``WORKER_EVENT_SINK_KEY`` 的注入/读取惯例);``save_artifact`` 每次成功
#: 登记喂一条清单项。键定义在 tools 层与 sink keys 同理由:builder 与
#: sse 都要 import,而 tools 不 import 它们。
ARTIFACT_RECORDER_KEY = "artifact_manifest_recorder"


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


def _validate_path(path: str) -> str:
    """Reject a non-relative or ``..``-bearing workspace path.

    The path is later resolved against the user's workspace volume
    (PR3 content download), so an absolute path or a ``..`` segment
    must never reach the store.
    """
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        msg = f"artifact path must be a relative workspace path without '..': {path!r}"
        raise ValueError(msg)
    return cleaned


def _coerce_kind(raw: object) -> ArtifactKind:
    if raw is None:
        return _DEFAULT_KIND
    if not isinstance(raw, str) or raw not in _ARTIFACT_KINDS:
        msg = f"save_artifact 'kind' must be one of {sorted(_ARTIFACT_KINDS)}"
        raise ValueError(msg)
    return raw  # type: ignore[return-value]  # membership-checked above


@dataclass
class SaveArtifactTool:
    """Registers a workspace file as a named artifact — ``save_artifact``."""

    store: ArtifactStore

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="save_artifact",
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
        path_in_workspace = _validate_path(path)
        kind = _coerce_kind(args.get("kind"))
        thread_id = str(ctx.run_id) if ctx.run_id is not None else _FALLBACK_THREAD_ID

        version = await self.store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name=name,
            kind=kind,
            path_in_workspace=path_in_workspace,
            created_in_thread=thread_id,
        )
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


@dataclass
class ListArtifactsTool:
    """Lists the user's saved artifacts — ``list_artifacts``."""

    store: ArtifactStore

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_artifacts",
            description=(
                "List the named artifacts you have saved, with each one's kind and latest version."
            ),
            parameters={"type": "object", "properties": {}},
            # Stream L.L6 — pure read.
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args  # list_artifacts takes no arguments
        tenant_id, user_id = _require_user_scope(ctx, "list_artifacts")
        artifacts = await self.store.list_for_user(tenant_id=tenant_id, user_id=user_id)
        if not artifacts:
            return ToolResult(content="(no artifacts saved yet)", meta={"n_artifacts": 0})
        lines = [f"- {a.name} ({a.kind}, v{a.latest_version})" for a in artifacts]
        return ToolResult(content="\n".join(lines), meta={"n_artifacts": len(artifacts)})
