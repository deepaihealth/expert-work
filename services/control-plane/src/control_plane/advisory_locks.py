"""Postgres advisory-lock ``classid`` registry — one place, one number each.

Every two-arg ``pg_advisory_xact_lock(classid, hashtext(key))`` /
``pg_try_advisory_xact_lock`` call site in the control plane takes its
``classid`` from here.

Before this module each lock carried its own integer literal plus a
hand-maintained "values already taken" comment. Two of those comments both
claimed to have taken a fresh ``8619`` — ``trigger_delivery`` and
``workspace_janitor``. Nothing broke, because the two locks happen to hash
different key strings into that shared class, but a comment is not evidence:
the next module picking "the next free number" off one of those lists would
have collided for real. The registry replaces the lists with a single set of
values a test can actually check.

Adding a lock:

* Append a constant with the next unused value and a one-line note naming the
  owner and the key string it hashes.
* Add it to :data:`CLASSID_BY_OWNER`. ``tests/test_advisory_lock_registry.py``
  fails both on a duplicate value and on a constant missing from that mapping,
  so a new lock cannot quietly skip the duplicate check.
* Never reuse a retired value — a rolling deploy runs old and new pods at once,
  and two different locks sharing a class during that window is exactly the
  collision this registry exists to prevent.

Renumbering a live lock:

Changing an already-deployed lock's ``classid`` is **not** free housekeeping. A
rolling deploy runs old and new pods together for a minute or two, and during
that window old pods lock on the old class while new pods lock on the new one —
the two do not exclude each other at all. So when a collision has to be broken
by moving one of the two locks, move the one whose **split-brain is survivable**,
not the one whose name reads better in the docs.

That is why ``workspace_janitor`` moved and ``trigger_delivery`` stayed on
``8619``, even though the janitor took the number first and a runbook already
named it:

* ``trigger_delivery`` without exclusion delivers a result twice, or lets two
  ``aupdate_state`` calls write sibling versions of the same parent checkpoint —
  a lost update. User-visible, and it corrupts conversation state.
* ``workspace_janitor`` without exclusion just runs a second copy of an
  idempotent sweep (reaping an already-gone dir / an already-swept size is a
  no-op); the cost is one cycle's wasted archive upload, not wrong data. The
  janitor already tolerates exactly this: its lock txn is killed at 12 h and a
  peer replica then starts a concurrent cycle, which
  ``docs/runbooks/workspace-quota-and-archive.md`` documents as a known,
  non-corrupting state.

If **neither** side of a future collision is survivable, do not pick one — take
both classes at once in one release (acquire old *and* new), then drop the old
one in the next release, so no window exists where the two are unlocked.

Scope: the two-arg ``(int4, int4)`` key space only. Postgres keeps the
single-arg ``pg_advisory_xact_lock(bigint)`` space structurally separate and it
carries no ``classid``; its only user is the event-log thread lock
(``expert_work.runtime.event_log.db``), which therefore needs no entry here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = [
    "CLASSID_BY_OWNER",
    "MCP_OAUTH_REFRESH_LOCK_CLASSID",
    "MEMORY_CONSOLIDATOR_LOCK_CLASSID",
    "QUALITY_DRIFT_LOCK_CLASSID",
    "SKILL_CURATOR_LOCK_CLASSID",
    "SUPERSEDE_LOCK_CLASSID",
    "TENANT_RESOURCE_LOCK_CLASSID",
    "TRIGGER_DELIVERY_LOCK_CLASSID",
    "WORKSPACE_JANITOR_LOCK_CLASSID",
    "WORKSPACE_LOCK_CLASSID",
]

#: ``workspace_lock.PgWorkspaceLock`` — per-workspace write lock, key
#: ``"{tenant_id}:{user_id}"``.
WORKSPACE_LOCK_CLASSID: Final[int] = 1

#: ``mcp_oauth_refresh_lock`` — per-(tenant, user) OAuth token refresh, key
#: ``"{tenant_id}:{user_id}"`` (``user_id`` here is the OAuth subject string).
MCP_OAUTH_REFRESH_LOCK_CLASSID: Final[int] = 2

#: ``quality_drift_worker`` — single-flight drift cycle, key ``"quality_drift"``.
QUALITY_DRIFT_LOCK_CLASSID: Final[int] = 8615

#: ``memory_consolidator`` — single-flight sweep, key ``"memory_consolidator"``.
MEMORY_CONSOLIDATOR_LOCK_CLASSID: Final[int] = 8616

#: ``skill_curator`` — single-flight sweep, key ``"skill_curator"``.
SKILL_CURATOR_LOCK_CLASSID: Final[int] = 8617

#: ``_tenant_resource_lock`` — per-tenant quota check-then-insert critical
#: section, key ``"{tenant_id}:{resource_kind}"``.
TENANT_RESOURCE_LOCK_CLASSID: Final[int] = 8618

#: ``trigger_delivery`` — per-thread delivery close-the-window lock, key =
#: thread id. Shared ``8619`` with the janitor until the registry landed; kept
#: the value because it is the one that must not lose exclusion for even a
#: deploy window (see "Renumbering a live lock" above).
TRIGGER_DELIVERY_LOCK_CLASSID: Final[int] = 8619

#: ``supersede`` — per-thread regenerate / edit-resend lock, key = thread id.
SUPERSEDE_LOCK_CLASSID: Final[int] = 8620

#: ``workspace_janitor`` — single-flight janitor cycle, key
#: ``"workspace_janitor"``. Moved off the shared ``8619`` when the registry
#: landed. Also named in ``docs/runbooks/workspace-quota-and-archive.md``.
WORKSPACE_JANITOR_LOCK_CLASSID: Final[int] = 8621

#: Every registered classid, keyed by the owning module. The self-audit test
#: asserts the values are unique *and* that every ``*_CLASSID`` constant above
#: appears here.
CLASSID_BY_OWNER: Final[Mapping[str, int]] = MappingProxyType(
    {
        "workspace_lock": WORKSPACE_LOCK_CLASSID,
        "mcp_oauth_refresh_lock": MCP_OAUTH_REFRESH_LOCK_CLASSID,
        "quality_drift_worker": QUALITY_DRIFT_LOCK_CLASSID,
        "memory_consolidator": MEMORY_CONSOLIDATOR_LOCK_CLASSID,
        "skill_curator": SKILL_CURATOR_LOCK_CLASSID,
        "_tenant_resource_lock": TENANT_RESOURCE_LOCK_CLASSID,
        "trigger_delivery": TRIGGER_DELIVERY_LOCK_CLASSID,
        "supersede": SUPERSEDE_LOCK_CLASSID,
        "workspace_janitor": WORKSPACE_JANITOR_LOCK_CLASSID,
    }
)
