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

#: ``workspace_janitor`` — single-flight janitor cycle, key
#: ``"workspace_janitor"``. Also named in
#: ``docs/runbooks/workspace-quota-and-archive.md``.
WORKSPACE_JANITOR_LOCK_CLASSID: Final[int] = 8619

#: ``supersede`` — per-thread regenerate / edit-resend lock, key = thread id.
SUPERSEDE_LOCK_CLASSID: Final[int] = 8620

#: ``trigger_delivery`` — per-thread delivery close-the-window lock, key =
#: thread id. Held ``8619`` until the registry landed, overlapping the janitor;
#: moved to its own value here.
TRIGGER_DELIVERY_LOCK_CLASSID: Final[int] = 8621

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
        "workspace_janitor": WORKSPACE_JANITOR_LOCK_CLASSID,
        "supersede": SUPERSEDE_LOCK_CLASSID,
        "trigger_delivery": TRIGGER_DELIVERY_LOCK_CLASSID,
    }
)
