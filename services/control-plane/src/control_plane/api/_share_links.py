"""B-326 —— 产物清单里的分享链接:读的时候按**当前**配置给出能用的链接。

``share_file`` 把文件标记为交给用户时,在这一轮 run 的产物清单那一项上记下
``share_url`` / ``share_expires_at``,run 行里还存着内部的 ``_share``(标记的是哪个产物的哪一
版、哪个智能体)。之后每一处给出清单的地方 —— ``/items`` 的 ``runs[].artifacts``、``GET
/v1/agents/{code}/runs``、每一条 ``end`` 帧(直播、审批续跑、事件续传、幂等重放、排队收尾,
控制台同租户的事件流)—— 都经过这里:

* 智能体现在没开分享链接(或已不存在、已停用)、平台没配公开地址、调用方没有用户:两个分享
  字段去掉,**不铸新链接**;
* 标记的那一版已经不在(删了 / 过了保留期 / 产物软删了)、不是这个智能体的、或不是从这段对话
  自己的目录登记的:字段去掉;
* 否则给这段对话里这一版**还能用**的链接(没过期、剩余时间够、次数没用完、不长于当前有效期,
  见 ``ArtifactShareLink.reusable``);没有就按当前有效期再铸一条。来源是 ``_share`` 记下的
  版本,不是某条链接行 —— 链接行过期被清理掉也不影响续链;
* 一页一次查询:版本一次(``get_live_versions``)、链接一次(``newest_for``);只有需要新铸的
  才各走一次带锁的写;
* ``_share`` 永不出去;没带分享字段的项原样;
* 查库出错:不报错,两个分享字段去掉(存着的链接可能已过期,不往外发),只记错误类型。

控制台跨租户审计读不出链接(:func:`strip_share_fields`,同租户的控制台本来就能下载)。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Request

from control_plane.api._run_event_stream import ArtifactView
from expert_work.persistence.artifact_share import ArtifactShareLink
from expert_work.protocol import AgentSpecStatus, ShareLinksSpec
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools.artifact import SHARE_FIELDS, SHARE_META_KEY, public_entries
from orchestrator.tools.share_file import ShareLinkService
from orchestrator.tools.workspace_paths import AGENTS_DIR, CONVERSATION_WORK_DIR, THREADS_DIR

logger = logging.getLogger("expert_work.control_plane.share_links")

_Entries = list[dict[str, Any]]


def _marked(entry: object) -> bool:
    """A marked entry: it carries the delivery fields, or only the internal record (a
    continuation's mark from before the pause, whose link is re-derived here)."""
    return isinstance(entry, Mapping) and ("share_url" in entry or SHARE_META_KEY in entry)


def _without_share(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in SHARE_FIELDS and k != SHARE_META_KEY}


def strip_share_fields(artifacts: Sequence[Any] | None) -> _Entries | None:
    """The list with the share fields and the internal record removed from every entry."""
    if artifacts is None:
        return None
    return [_without_share(e) if isinstance(e, Mapping) else e for e in artifacts]


async def share_policy(
    request: Request, *, tenant_id: UUID, agent_name: str
) -> ShareLinksSpec | None:
    """The agent's **current** share-link block (its active version), or ``None`` when it is
    off, absent, the agent has no active version, or the agent is disabled."""
    state = request.app.state
    repo = getattr(state, "agent_spec_repo", None)
    if repo is None:
        return None
    disable = getattr(state, "agent_disable_service", None)
    if disable is not None and await disable.is_disabled(tenant_id, agent_name):
        return None
    active = await repo.list_by_tenant(
        tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, name=agent_name, limit=1
    )
    if not active:
        return None
    block: ShareLinksSpec | None = active[0].spec.spec.share_links
    return block if block is not None and block.enabled else None


@dataclass(frozen=True)
class RunArtifacts:
    """One run's stored artifact list and where it was produced."""

    run_id: UUID | None
    thread_id: UUID
    artifacts: Sequence[Any] | None


@dataclass(frozen=True)
class _Mark:
    artifact_id: UUID
    version_id: UUID
    agent_key: str


def _mark_of(entry: Mapping[str, Any]) -> _Mark | None:
    raw = entry.get(SHARE_META_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        return _Mark(
            artifact_id=UUID(str(raw["artifact_id"])),
            version_id=UUID(str(raw["version_id"])),
            agent_key=str(raw["agent_key"]),
        )
    except (KeyError, ValueError):
        return None


async def refresh_share_links_for_runs(
    request: Request,
    runs: Sequence[RunArtifacts],
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    agent_name: str | None,
) -> list[_Entries | None]:
    """Each run's artifact list as integrators read it (see module docstring), in order."""
    if not any(r.artifacts and any(_marked(e) for e in r.artifacts) for r in runs):
        return [
            public_entries(list(r.artifacts)) if r.artifacts is not None else None for r in runs
        ]
    service: ShareLinkService | None = getattr(request.app.state, "share_link_service", None)
    if service is None or user_id is None or agent_name is None:
        # No platform links, no user, or a conversation not bound to an agent.
        return [strip_share_fields(r.artifacts) for r in runs]
    try:
        policy = await share_policy(request, tenant_id=tenant_id, agent_name=agent_name)
        if policy is None:
            return [strip_share_fields(r.artifacts) for r in runs]
        return await _refresh(
            service,
            runs,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=sanitize_agent_key(agent_name),
            ttl=timedelta(minutes=policy.ttl_minutes),
        )
    except Exception as exc:
        # Type only: a driver error message can carry the full DSN. Fail closed: the
        # stored link may have expired, or belong to a mark that is no longer valid.
        logger.warning("share_links.refresh_failed error=%s", type(exc).__name__)
        return [strip_share_fields(r.artifacts) for r in runs]


async def _refresh(
    service: ShareLinkService,
    runs: Sequence[RunArtifacts],
    *,
    tenant_id: UUID,
    user_id: UUID,
    agent_key: str,
    ttl: timedelta,
) -> list[_Entries | None]:
    marks = {
        id(e): m
        for r in runs
        for e in r.artifacts or ()
        if _marked(e) and (m := _mark_of(e)) is not None and m.agent_key == agent_key
    }
    live = await service.artifacts.get_live_versions(
        tenant_id=tenant_id, user_id=user_id, version_ids=[m.version_id for m in marks.values()]
    )

    def still_marked(entry: Mapping[str, Any], mark: _Mark, thread_id: UUID) -> bool:
        found = live.get(mark.version_id)
        if found is None:
            return False
        artifact, version = found
        # Saved from this conversation's own directory, as ``share_file`` required.
        here = f"{AGENTS_DIR}/{agent_key}/{THREADS_DIR}/{thread_id}/{CONVERSATION_WORK_DIR}/"
        # (The agent is pinned by the path below: an artifact's versions live under its
        # own agent's directory.)
        return (
            artifact.id == mark.artifact_id
            and artifact.name == entry.get("name")
            and version.version == entry.get("version")
            and version.path_in_workspace.startswith(here)
        )

    wanted: dict[tuple[UUID, UUID], tuple[RunArtifacts, Mapping[str, Any], _Mark]] = {}
    for r in runs:
        for e in r.artifacts or ():
            mark = marks.get(id(e))
            if mark is not None and still_marked(e, mark, r.thread_id):
                wanted.setdefault((r.thread_id, mark.version_id), (r, e, mark))
    newest = await service.links.newest_for(tenant_id=tenant_id, user_id=user_id, keys=list(wanted))
    now = datetime.now(UTC)
    links: dict[tuple[UUID, UUID], ArtifactShareLink] = {}
    for key, (r, e, mark) in wanted.items():
        link = newest.get(key)
        if link is None or not link.reusable(now, ttl=ttl, max_downloads=service.max_downloads):
            link = await service.links.mint_or_reuse(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_key=agent_key,
                artifact_id=mark.artifact_id,
                artifact_version_id=mark.version_id,
                version=int(e["version"]),
                thread_id=r.thread_id,
                run_id=r.run_id,
                file_name=str(e["name"]),
                ttl=ttl,
                now=now,
                max_downloads=service.max_downloads,
            )
        links[key] = link

    def view(r: RunArtifacts) -> _Entries | None:
        if r.artifacts is None:
            return None
        out: _Entries = []
        for e in r.artifacts:
            if not _marked(e):
                out.append(e)
                continue
            mark = marks.get(id(e))
            link = links.get((r.thread_id, mark.version_id)) if mark is not None else None
            fields = (
                {
                    "share_url": service.url_for(link.id, link.file_name),
                    "share_expires_at": link.expires_at.isoformat(),
                }
                if link is not None and mark is not None and still_marked(e, mark, r.thread_id)
                else {}
            )
            out.append({**_without_share(e), **fields})
        return out

    return [view(r) for r in runs]


async def refresh_share_links(
    artifacts: Sequence[Any] | None,
    request: Request,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    thread_id: UUID,
    agent_name: str | None,
    run_id: UUID | None = None,
) -> _Entries | None:
    """One run's list — :func:`refresh_share_links_for_runs` for a single run."""
    [out] = await refresh_share_links_for_runs(
        request,
        [RunArtifacts(run_id=run_id, thread_id=thread_id, artifacts=artifacts)],
        tenant_id=tenant_id,
        user_id=user_id,
        agent_name=agent_name,
    )
    return out


def share_view(
    request: Request,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    thread_id: UUID,
    agent_name: str | None,
    run_id: UUID | None = None,
) -> ArtifactView:
    """An ``end``-frame artifact view that refreshes share links (external event paths)."""

    async def view(artifacts: _Entries | None) -> _Entries | None:
        return await refresh_share_links(
            artifacts,
            request,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            agent_name=agent_name,
            run_id=run_id,
        )

    return view


async def thread_share_view(
    request: Request, *, tenant_id: UUID, thread_id: UUID, run_id: UUID | None = None
) -> ArtifactView | None:
    """:func:`share_view` for a conversation, its agent and user read from the thread
    record; ``None`` when there is no record. A failed lookup fails closed
    (:func:`strip_view`) rather than failing the caller: the approval resume calls this
    after the decision is already applied."""
    try:
        meta = await request.app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)
    except Exception as exc:  # fail closed, like every other share-link error
        logger.warning("share_links.thread_lookup_failed error=%s", type(exc).__name__)
        return strip_view
    if meta is None:
        return None
    return share_view(
        request,
        tenant_id=tenant_id,
        user_id=meta.user_id,
        thread_id=thread_id,
        agent_name=meta.agent_name,
        run_id=run_id,
    )


async def strip_view(artifacts: _Entries | None) -> _Entries | None:
    """The ``end``-frame artifact view for the console's cross-tenant audit reads."""
    return strip_share_fields(artifacts)
