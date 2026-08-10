"""``ObjectStore`` Protocol — S3-compatible object storage abstraction.

Per [ADR-0004](../../../../../../docs/adr/0004-object-storage.md). All
call sites depend on this Protocol, not the S3 client directly, so the
in-memory implementation can stand in for unit tests and a different
S3-compatible backend (MinIO / Aliyun OSS / AWS S3 / Tencent COS) can be
swapped via the factory without code changes.

Multipart streaming lands via ``put_stream`` (波 3).

Stream D.1b extends ``put`` with optional S3 Object Lock parameters
(``retain_until`` + ``lock_mode``) used by the D.1c audit WORM backup
worker. See STREAM-D-DESIGN § 2.3 + Mini-ADR D-2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Literal, Protocol

# The two Object Lock retention modes S3 / MinIO expose. We deliberately
# do not surface ``None`` semantics other than "no lock" — see Mini-ADR
# D-2: limiting to ``governance`` / ``compliance`` keeps in-memory mock
# and S3 backend behaviorally aligned and prevents callers from drifting
# the bucket-level default by accident.
LockMode = Literal["governance", "compliance"]


class ObjectStoreError(Exception):
    """Base class for object-store failures."""


class ObjectNotFoundError(ObjectStoreError):
    """Raised by ``get`` / ``delete`` when the key does not exist."""


class ObjectLockedError(ObjectStoreError):
    """In-memory signal that a put hit a still-retained compliance lock.

    The S3 / MinIO contract is **version-level**: each put creates a
    new version, and the compliance lock pins that *version* against
    delete-before-expiry. Same-key re-puts are not rejected by S3 —
    they just produce new versions. The in-memory store models the
    simpler, key-level semantic ("this key is locked, you cannot
    overwrite") because it doesn't track versions; that gives unit
    tests a clean way to assert "the lock is active" without needing
    to plumb version ids.

    Practical use: the D.1c WORM-backup worker keys by audit row id
    (monotonic), and uses ``backup_acked`` in the DB — not put-side
    rejection — to avoid double writes on retry. Both backends meet
    the worker's needs because the worker doesn't re-put under the
    same retention.
    """


class ObjectStore(Protocol):
    """Tenant-namespaced object storage surface.

    The store does **not** enforce key-naming convention; callers must
    prefix with ``{tenant_id}/...`` per ADR-0004 § 2.3. Cross-tenant
    isolation lives at the access-control layer (Stream C).
    """

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        retain_until: datetime | None = None,
        lock_mode: LockMode | None = None,
    ) -> None:
        """Write ``data`` at ``key``; overwrites if the key already exists.

        Optional Object Lock parameters (STREAM-D-DESIGN § 2.3):

        * ``retain_until`` — UTC datetime the object stays
          retention-locked until. ``None`` means no retention is set.
        * ``lock_mode`` — ``"governance"`` (privileged roles may override)
          or ``"compliance"`` (immutable until ``retain_until``).

        Contract:

        * Both ``retain_until`` and ``lock_mode`` must be supplied
          together, or neither — passing one without the other raises
          :class:`ValueError`. They jointly express "lock this object
          until X" and have no useful single-argument form.
        * **WORM semantics are version-level on S3.** The lock pins
          the *version* this put produced against delete-before-
          expiry. Same-key re-puts are not rejected; they just create
          new versions. The in-memory backend models this as a key-
          level :class:`ObjectLockedError` on re-put (simpler — no
          versioning); the S3 backend lets re-puts succeed and relies
          on the per-version retention for the audit guarantee.
        * For ``lock_mode="governance"``, a privileged caller may
          delete the locked version with ``BypassGovernanceRetention``;
          ``compliance`` cannot be overridden by anyone.
        * The target bucket must already have Object Lock enabled when
          locks are used; bucket-level setup belongs to IaC, not the
          runtime API.
        """

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        """Store an object from an async byte stream (multipart on S3).

        波 3 (spec § 4.2):杀掉 ``put(bytes)`` 的整体驻留内存上限。契约:

        - 实现负责攒片——调用方的 chunk 尺寸任意(含空流,产出空对象)。
        - 常驻内存 ≤ 单分片(S3 实现默认 64 MiB)。
        - 覆盖语义与 ``put`` 一致;不支持 object-lock 参数(归档不需要 WORM)。
        - 失败时不得留下部分可见对象(S3 走 multipart,abort 兜尾)。
        """
        ...

    async def get(self, key: str) -> bytes:
        """Read the full object at ``key``.

        :raises ObjectNotFoundError: the key does not exist.
        """

    async def delete(self, key: str) -> None:
        """Remove the object at ``key``.

        Idempotent — deleting a non-existent key does NOT raise.
        """

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return every key beginning with ``prefix``, lexicographically sorted."""

    async def presigned_url(
        self,
        key: str,
        *,
        expires_in: int = 3600,
        method: Literal["GET", "PUT"] = "GET",
    ) -> str:
        """Generate a time-limited signed URL the client can fetch directly.

        Used by the Control Plane to hand off large upload/download flows to
        the client without proxying bytes through the API tier.
        """


def validate_lock_args(retain_until: datetime | None, lock_mode: LockMode | None) -> None:
    """Reject the partial-spec cases described in :class:`ObjectStore.put`.

    Pulled out as a standalone helper so every backend uses the same
    rule and unit tests can pin the message without instantiating
    a store.
    """
    if (retain_until is None) != (lock_mode is None):
        msg = (
            "retain_until and lock_mode must be specified together "
            f"(got retain_until={retain_until!r}, lock_mode={lock_mode!r})"
        )
        raise ValueError(msg)
