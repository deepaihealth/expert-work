"""In-memory ``ArtifactShareLinkStore`` — B-326 (tests / no-DB deployments)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

from expert_work.persistence.artifact_share.base import (
    ArtifactShareLink,
    ArtifactShareLinkStore,
    new_link_id,
)


class InMemoryArtifactShareLinkStore(ArtifactShareLinkStore):
    """Dict-backed store with the same predicates as the SQL implementation.

    One difference, on purpose: it trusts the caller's ``now``. The SQL store judges and
    stamps with ``max(now, clock_timestamp())`` so lagging replicas agree on one link; here a
    single process has one clock, and tests rely on passing an earlier ``now`` to mint a
    link that is about to run out."""

    def __init__(self) -> None:
        self._rows: dict[str, ArtifactShareLink] = {}

    async def mint_or_reuse(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_key: str,
        artifact_id: UUID,
        artifact_version_id: UUID,
        version: int,
        thread_id: UUID,
        run_id: UUID | None,
        file_name: str,
        ttl: timedelta,
        now: datetime,
        max_downloads: int,
    ) -> ArtifactShareLink:
        newest = (
            await self.newest_for(
                tenant_id=tenant_id, user_id=user_id, keys=[(thread_id, artifact_version_id)]
            )
        ).get((thread_id, artifact_version_id))
        if newest is not None and newest.reusable(now, ttl=ttl, max_downloads=max_downloads):
            return newest
        link = ArtifactShareLink(
            id=new_link_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=agent_key,
            artifact_id=artifact_id,
            artifact_version_id=artifact_version_id,
            version=version,
            run_id=run_id,
            thread_id=thread_id,
            created_at=now,
            expires_at=now + ttl,
            file_name=file_name,
        )
        self._rows = {**self._rows, link.id: link}
        return link

    async def newest_for(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        keys: Sequence[tuple[UUID, UUID]],
    ) -> dict[tuple[UUID, UUID], ArtifactShareLink]:
        wanted = set(keys)
        out: dict[tuple[UUID, UUID], ArtifactShareLink] = {}
        for row in self._rows.values():
            if row.tenant_id != tenant_id or row.user_id != user_id:
                continue
            key = (row.thread_id, row.artifact_version_id)
            if key in wanted and (key not in out or row.created_at > out[key].created_at):
                out[key] = row
        return out

    async def get_public(self, *, link_id: str) -> ArtifactShareLink | None:
        return self._rows.get(link_id)

    async def claim_download(self, *, link_id: str, max_downloads: int) -> bool:
        row = self._rows.get(link_id)
        if row is None or row.download_count >= max_downloads:
            return False
        self._rows = {**self._rows, link_id: replace(row, download_count=row.download_count + 1)}
        return True

    async def refund_download(self, *, link_id: str) -> None:
        row = self._rows.get(link_id)
        if row is not None and row.download_count > 0:
            self._rows = {
                **self._rows,
                link_id: replace(row, download_count=row.download_count - 1),
            }

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        wanted = set(thread_ids)
        keep = {
            k: v
            for k, v in self._rows.items()
            if not (v.tenant_id == tenant_id and v.thread_id in wanted)
        }
        removed = len(self._rows) - len(keep)
        self._rows = keep
        return removed

    async def delete_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        return self._drop(lambda v: v.tenant_id == tenant_id and v.user_id == user_id)

    async def delete_for_agent(
        self, *, tenant_id: UUID, agent_key: str, keep_users: Collection[UUID] = ()
    ) -> int:
        keep = set(keep_users)
        return self._drop(
            lambda v: (
                v.tenant_id == tenant_id and v.agent_key == agent_key and v.user_id not in keep
            )
        )

    def _drop(self, doomed: Callable[[ArtifactShareLink], bool]) -> int:
        keep = {k: v for k, v in self._rows.items() if not doomed(v)}
        removed = len(self._rows) - len(keep)
        self._rows = keep
        return removed

    def put(self, link: ArtifactShareLink) -> None:
        """Test seam: store a row as given (e.g. one already expired)."""
        self._rows = {**self._rows, link.id: replace(link)}
