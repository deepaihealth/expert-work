"""``share_file`` tool — B-326 artifact share links.

An agent that registered a file (``save_artifact``) can deliver it to the END USER as a
link that opens without any login or API key. The link never goes through the model:

* :class:`ShareFileTool` marks a file for delivery. It resolves the artifact within this
  agent's ``agent_key``, this user **and this conversation** (only versions registered from
  this conversation's own directory; the latest of those unless ``version`` is given — see
  :meth:`ShareFileTool._resolve`), mints (or reuses) a row in
  :class:`~expert_work.persistence.artifact_share.ArtifactShareLinkStore` pinned to that
  exact version, and records ``share_url`` / ``share_expires_at`` on that artifact in the
  run's artifact list (:class:`~orchestrator.tools.artifact.ArtifactRecorder`) — the SSE
  ``end`` frame and the run record the integrator reads. The run record also keeps which
  version was marked (:data:`~orchestrator.tools.artifact.SHARE_META_KEY`, never sent
  out): reading the list later re-checks that version and renews the link from it, so a
  link row is never the only record of a mark. The tool result the model sees
  is plain confirmation text: no URL, no link id. So the credential never enters the
  conversation record, memory, traces or any later prompt.
* Integrators send ``share_url`` with their file-sending path; the URL ends with the file
  name (:func:`safe_file_name`) for senders that name downloads by URL path.

Opt-in per agent (``spec.share_links.enabled``); the tool is not registered otherwise,
never for dynamic workers, and a delegated child only marks when its root agent opted in.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

from expert_work.persistence import ArtifactStore
from expert_work.persistence.artifact_share import DEFAULT_MAX_DOWNLOADS, ArtifactShareLinkStore
from expert_work.protocol import ArtifactVersion
from orchestrator.tools.artifact import SHARE_META_KEY
from orchestrator.tools.file_ops import FileOpError
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.tools.workspace_paths import AGENTS_DIR, conversation_work_prefix

logger = logging.getLogger(__name__)

SHARE_FILE_TOOL_NAME = "share_file"

#: Path of the unauthenticated download endpoint (control-plane
#: ``api/public_files.py``); ``/v1`` so the existing ingress routes it.
PUBLIC_FILES_PATH = "/v1/public/files"

#: Characters a public base URL may not contain. Integrators pass ``share_url`` on
#: verbatim (into chat messages, JSON payloads, file-sending APIs), so a quote,
#: backslash, angle bracket or whitespace would break naive senders.
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\"'\\<>`]")


#: Longest cosmetic file-name segment, extension included.
SAFE_NAME_MAX_CHARS = 80
#: An extension worth keeping: a short run of letters / digits after the last dot.
_EXTENSION_MAX_CHARS = 10
_UNSAFE_RUN = re.compile(r"_+")


def _sanitize(text: str) -> str:
    """Unicode letters / digits (CJK included), ``.``, ``_``, ``-``; anything else → ``_``,
    runs of ``_`` collapsed — a charset naive downloaders keep as a file name."""
    mapped = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    return _UNSAFE_RUN.sub("_", mapped)


def safe_file_name(name: str) -> str:
    """The cosmetic last URL segment for ``name`` (unencoded): the base name, sanitized,
    capped at :data:`SAFE_NAME_MAX_CHARS` with its extension kept; ``file`` + the
    extension when nothing usable is left."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or not ext.isalnum() or len(ext) > _EXTENSION_MAX_CHARS:
        stem, ext = base, ""
    suffix = f".{ext}" if ext else ""
    clean = _sanitize(stem).strip("._-")
    clean = clean[: SAFE_NAME_MAX_CHARS - len(suffix)].rstrip("._-")
    return f"{clean or 'file'}{suffix}"


def normalize_public_base_url(raw: str) -> str:
    """Validate the platform's public base URL and drop a trailing ``/``.

    :raises ValueError: not an absolute http(s) URL, or contains characters that
        integrators' senders would mangle (see :data:`_UNSAFE_URL_CHARS`).
    """
    value = raw.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        msg = f"public base URL must be an absolute http(s) URL, got {raw!r}"
        raise ValueError(msg)
    if parts.query or parts.fragment or "?" in value or "#" in value:
        msg = f"public base URL must not carry a query or fragment: {raw!r}"
        raise ValueError(msg)
    if _UNSAFE_URL_CHARS.search(value):
        msg = f"public base URL contains characters not allowed in a link: {raw!r}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class ShareLinkService:
    """What both halves need: the link store, the artifact store, the public base URL."""

    links: ArtifactShareLinkStore
    artifacts: ArtifactStore
    #: Already normalized (:func:`normalize_public_base_url`), no trailing ``/``.
    public_base_url: str
    #: Downloads one link serves (``EXPERT_WORK_SHARE_LINK_MAX_DOWNLOADS``); a link that
    #: has served them is not handed out again.
    max_downloads: int = DEFAULT_MAX_DOWNLOADS

    def url_for(self, link_id: str, file_name: str = "") -> str:
        """``<base>/v1/public/files/<id>/<safe name>``. The last segment is cosmetic —
        downstream senders that name a download by its URL path get a real file name;
        the endpoint ignores it (the id is the only credential)."""
        name = quote(safe_file_name(file_name), safe="")
        return f"{self.public_base_url}{PUBLIC_FILES_PATH}/{link_id}/{name}"


def share_turn_run_id(ctx: ToolContext) -> UUID | None:
    """The run id a share link is recorded under — the turn's first run.

    An approval resume runs under a new run id but carries the turn's root
    (``turn_root_run_id``); a delegated child carries its parent's
    (``inputs_run_id``). Audit only: a link belongs to its conversation, not its run.
    """
    return ctx.turn_root_run_id or ctx.inputs_run_id or ctx.run_id


def _requested_version(args: Mapping[str, Any]) -> int | None:
    raw = args.get("version")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        msg = "share_file 'version' must be a positive integer (omit it for the latest)"
        raise ValueError(msg)
    return raw


@dataclass
class ShareFileTool:
    """Creates a time-limited public link to one registered artifact — ``share_file``."""

    service: ShareLinkService
    ttl_minutes: int

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=SHARE_FILE_TOOL_NAME,
            description=(
                "Mark a file you registered with save_artifact (in this conversation) for "
                "delivery to the user: it is sent to them together with your reply, as a "
                "download link they can open without logging in for a limited time. You never "
                "see or write the link — in your reply, just say the file has been sent. Do not "
                "put URLs or file links in your reply, tool arguments or files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Artifact name as registered with save_artifact.",
                    },
                    "version": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Version to share (optional; defaults to the latest).",
                    },
                },
                "required": ["name"],
            },
        )

    async def _resolve(
        self,
        name: str,
        requested: int | None,
        *,
        tenant_id: UUID,
        user_id: UUID,
        ctx: ToolContext,
    ) -> ArtifactVersion:
        """The version to share: saved in **this conversation**, latest unless ``requested``.

        Artifacts are keyed per (user, agent), not per conversation (B-236), and an
        integrator often runs many end customers under one platform user — so "the
        latest ``report.pdf``" of the user may belong to another customer's
        conversation. A version counts as this conversation's only when its bytes were
        registered from this conversation's own directory
        (``agents/<key>/threads/<conv>/work/…``); the file tools cannot address another
        conversation's directory, so the path is a reliable marker. Versions from
        another conversation, from the agent root, or from a conversation started
        before per-conversation directories are never shared.
        """
        view = ctx.workspace_view
        if not view.per_conversation:
            msg = (
                "share_file: this conversation has no workspace directory of its own "
                "(it was started before per-conversation workspaces), so none of its files "
                "can be shared. Nothing was shared."
            )
            raise FileOpError(msg)
        prefix = f"{AGENTS_DIR}/{ctx.agent_key}/{conversation_work_prefix(view)}"
        artifacts = self.service.artifacts
        latest = await artifacts.get_latest_version(
            tenant_id=tenant_id, user_id=user_id, agent_key=ctx.agent_key, name=name
        )
        if latest is None:
            msg = (
                f"share_file: no file named {name!r} was saved in this conversation — "
                "nothing was shared. Save the file here with save_artifact first (check "
                "the name with list_artifacts)."
            )
            raise FileOpError(msg)
        if requested is None:
            newest_here = await artifacts.get_latest_version_under(
                tenant_id=tenant_id,
                user_id=user_id,
                artifact_id=latest.artifact_id,
                path_prefix=prefix,
            )
            if newest_here is None:
                msg = (
                    f"share_file: no file named {name!r} was saved in this conversation — "
                    "nothing was shared. Save the file here with save_artifact first (check "
                    "the name with list_artifacts)."
                )
                raise FileOpError(msg)
            return newest_here
        pinned = await artifacts.get_version_by_id(
            tenant_id=tenant_id, user_id=user_id, artifact_id=latest.artifact_id, version=requested
        )
        if pinned is None or not pinned.path_in_workspace.startswith(prefix):
            msg = (
                f"share_file: version {requested} of {name!r} was not saved in this "
                "conversation — nothing was shared. Omit 'version' to share the latest "
                "one saved here."
            )
            raise FileOpError(msg)
        return pinned

    def _ttl_minutes(self, ctx: ToolContext) -> int:
        """The link lifetime: a delegated child mints with its root agent's (the agent whose
        run reports the link), a root with its own."""
        if ctx.child_run and ctx.share_links_ttl_minutes is not None:
            return ctx.share_links_ttl_minutes
        return self.ttl_minutes

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None or ctx.user_id is None or not ctx.agent_key:
            msg = "share_file requires a tenant, user and agent binding"
            raise ToolBlockedError(msg)
        if ctx.child_run and ctx.share_links_ttl_minutes is None:
            # The opt-in is per agent: a delegated child marks files for delivery only
            # when the root agent (whose run reports them) has share links on.
            msg = (
                "share_file is not available in this delegated task: the agent that "
                "delegated it does not deliver share links. Report the file name instead."
            )
            raise ToolBlockedError(msg)
        thread_id = ctx.workspace_thread_id or ctx.thread_id
        if thread_id is None:
            msg = "share_file requires a conversation"
            raise ToolBlockedError(msg)
        recorder = ctx.artifact_recorder
        if recorder is None:
            # The link reaches the user only through this run's artifact list; without
            # one, a minted link would be a credential nobody delivers.
            msg = (
                "share_file cannot deliver files from here: this run reports no artifact "
                "list. Nothing was shared."
            )
            raise ToolBlockedError(msg)
        raw_name = args.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            msg = "share_file requires a non-empty 'name' string"
            raise ValueError(msg)
        name = raw_name.strip()
        requested = _requested_version(args)
        chosen = await self._resolve(
            name, requested, tenant_id=ctx.tenant_id, user_id=ctx.user_id, ctx=ctx
        )
        if chosen.snapshot_key is None:
            # The link must serve exactly this version's bytes. A version registered
            # before per-version copies existed only points at a workspace path that a
            # later write can change, so it cannot be pinned.
            msg = (
                f"share_file: version {chosen.version} of {name!r} was registered before "
                "per-version copies were kept. Save the file again with save_artifact, then "
                "share the new version."
            )
            raise FileOpError(msg)
        link = await self.service.links.mint_or_reuse(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            agent_key=ctx.agent_key,
            artifact_id=chosen.artifact_id,
            artifact_version_id=chosen.id,
            version=chosen.version,
            thread_id=thread_id,
            run_id=share_turn_run_id(ctx),
            file_name=name,
            ttl=timedelta(minutes=self._ttl_minutes(ctx)),
            now=datetime.now(UTC),
            max_downloads=self.service.max_downloads,
        )
        logger.info(
            "share_file.marked artifact_version=%s version=%s expires_at=%s",
            chosen.id,
            chosen.version,
            link.expires_at.isoformat(),
        )
        artifact = await self.service.artifacts.get_by_id(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, artifact_id=chosen.artifact_id
        )
        created = chosen.created_at or datetime.now(UTC)
        # The run's artifact list (end frame + run record): the same entry shape as
        # ``save_artifact`` writes, plus the two delivery fields and the internal
        # record of what was marked (stripped from everything integrators read).
        recorder(
            {
                "name": name,
                "kind": artifact.kind if artifact is not None else "other",
                "version": chosen.version,
                "created_at": created.isoformat(),
                "share_url": self.service.url_for(link.id, link.file_name),
                "share_expires_at": link.expires_at.isoformat(),
                SHARE_META_KEY: {
                    "artifact_id": str(chosen.artifact_id),
                    "version_id": str(chosen.id),
                    "agent_key": ctx.agent_key,
                },
            }
        )
        return ToolResult(
            content=(
                f"Marked {name!r} (version {chosen.version}) for delivery: it will be sent to "
                "the user together with this reply. Do not write a link or URL in your reply — "
                "just say the file has been sent."
            ),
            meta={"artifact": name, "version": chosen.version},
        )
