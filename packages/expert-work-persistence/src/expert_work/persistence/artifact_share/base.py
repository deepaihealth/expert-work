"""Abstract ``ArtifactShareLinkStore`` — B-326 artifact share links.

A share link is a short random id that names one exact artifact version for a limited
time. An agent marks a file for delivery (``share_file``); the platform mints a row and
reports ``share_url`` / ``share_expires_at`` on that artifact in the run's artifact list
— the URL never enters model text. The id is the last-but-one URL segment and the only
credential: 16 URL-safe characters from :func:`secrets.token_urlsafe` (96 bits).

Implementations:

* :class:`expert_work.persistence.artifact_share.memory.InMemoryArtifactShareLinkStore`
* :class:`expert_work.persistence.artifact_share.sql.SqlArtifactShareLinkStore`

Scoping:

* :meth:`ArtifactShareLinkStore.mint_or_reuse` / :meth:`newest_for` work inside
  conversations of one ``(tenant_id, user_id)``, keyed by ``(conversation, artifact version
  id)`` — at most one reusable link per pair (:meth:`ArtifactShareLink.reusable`).
* :meth:`get_public` / :meth:`claim_download` / :meth:`refund_download` — the
  unauthenticated download endpoint only has the id. Cross-tenant by nature, so the
  caller MUST be inside an RLS bypass scope (``bypass_rls_session``); the SQL store refuses
  otherwise.

Rows are never deleted on access: an expired row stays until the retention job removes it
(a grace window after ``expires_at``) or its conversation is purged.
"""

from __future__ import annotations

import abc
import secrets
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

#: Bytes of randomness per link id; ``token_urlsafe(12)`` is exactly 16 characters.
_LINK_ID_BYTES = 12
LINK_ID_LENGTH = 16
#: Downloads one link serves unless the platform says otherwise
#: (``EXPERT_WORK_SHARE_LINK_MAX_DOWNLOADS``) — the one place the default lives.
DEFAULT_MAX_DOWNLOADS = 20
#: A link with less time left than this is not handed out again (it could expire before
#: the end user opens it); capped at a quarter of the lifetime for short lifetimes.
REUSE_MIN_REMAINING = timedelta(minutes=5)
#: A link with fewer downloads left than this is not handed out again (a forward or a
#: retry would use it up); capped at the per-link maximum.
REUSE_MIN_DOWNLOADS_LEFT = 2
#: Slack on "no longer to go than the current lifetime": concurrent refreshes read their
#: clocks at slightly different instants (and replicas' clocks drift), so a link minted a
#: moment "later" must still count as reusable, or each of them mints its own.
REUSE_CLOCK_TOLERANCE = timedelta(minutes=1)


def new_link_id() -> str:
    """A fresh 16-character URL-safe random link id."""
    return secrets.token_urlsafe(_LINK_ID_BYTES)


@dataclass(frozen=True)
class ArtifactShareLink:
    """One ``artifact_share_links`` row."""

    id: str
    tenant_id: UUID
    user_id: UUID
    agent_key: str
    artifact_id: UUID
    artifact_version_id: UUID
    version: int
    run_id: UUID | None
    thread_id: UUID
    created_at: datetime
    expires_at: datetime
    download_count: int = 0
    #: The artifact's name when the link was minted — only for the cosmetic last URL
    #: segment (``…/files/<id>/<name>``) that downstream senders name the file by.
    file_name: str = ""

    def is_expired(self, now: datetime) -> bool:
        """Expired at or after ``expires_at`` (the boundary instant is already expired)."""
        return now >= self.expires_at

    def reusable(self, now: datetime, *, ttl: timedelta, max_downloads: int) -> bool:
        """Worth handing out again under the current policy: at least
        ``min(2, max_downloads)`` downloads left, at least ``min(5 min, ttl / 4)`` to go,
        and no longer to go than ``ttl`` (+ :data:`REUSE_CLOCK_TOLERANCE`) — a lowered
        lifetime takes effect on the next read, not when the old link runs out."""
        remaining = self.expires_at - now
        margin = min(REUSE_MIN_REMAINING, ttl / 4)
        downloads_left = max_downloads - self.download_count
        return (
            downloads_left >= min(REUSE_MIN_DOWNLOADS_LEFT, max_downloads)
            and margin <= remaining <= ttl + REUSE_CLOCK_TOLERANCE
        )


class ArtifactShareLinkStore(abc.ABC):
    """Registry of public artifact share links."""

    @abc.abstractmethod
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
        """The newest link for ``(thread_id, artifact_version_id)`` when it is still
        :meth:`~ArtifactShareLink.reusable`, else a new one valid for ``ttl`` — atomic, so
        concurrent calls mint at most one. The SQL store judges and stamps with the later
        of ``now`` and the database clock read under the lock, so callers whose clocks
        lag (another replica, a read taken before waiting for the lock) agree."""

    @abc.abstractmethod
    async def newest_for(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        keys: Sequence[tuple[UUID, UUID]],
    ) -> dict[tuple[UUID, UUID], ArtifactShareLink]:
        """The newest link (any state) per ``(thread_id, artifact_version_id)`` key, in one
        query; keys without a link are absent."""

    @abc.abstractmethod
    async def get_public(self, *, link_id: str) -> ArtifactShareLink | None:
        """The row for ``link_id`` in any tenant, or ``None``.

        Caller MUST be inside an RLS bypass scope (the public download endpoint).
        """

    @abc.abstractmethod
    async def claim_download(self, *, link_id: str, max_downloads: int) -> bool:
        """Atomically count one download; ``False`` (nothing counted) once the link has
        already served ``max_downloads``. Caller MUST be inside an RLS bypass scope."""

    @abc.abstractmethod
    async def refund_download(self, *, link_id: str) -> None:
        """Give back one claimed download (the download did not succeed). Never below 0.
        Caller MUST be inside an RLS bypass scope."""

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """Delete every link minted in these conversations (conversation purge). Returns
        the number of rows removed."""

    @abc.abstractmethod
    async def delete_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Delete every link of this user (user purge). Returns the rows removed."""

    @abc.abstractmethod
    async def delete_for_agent(
        self, *, tenant_id: UUID, agent_key: str, keep_users: Collection[UUID] = ()
    ) -> int:
        """Delete every link of this agent except ``keep_users``' (agent purge — the same
        split as ``ArtifactStore.delete_all_for_agent``). Returns the rows removed."""
