"""Legal state-machine transitions for ``tenant_member`` — Stream R (Mini-ADR R-6).

```
invited ──(first login, 2026-08-27; was W3 first-run)──► active ──(admin DELETE)──► suspended
   │                                  (single-direction this iteration)
   └──(admin DELETE on invited)──► revoked
```

Shared by the in-memory and SQL stores so both enforce the same legality.
"""

from __future__ import annotations

from expert_work.protocol import MemberStatus

# to-status → the set of statuses it may legally move from.
LEGAL_PREDECESSORS: dict[MemberStatus, frozenset[MemberStatus]] = {
    "active": frozenset({"invited"}),
    "revoked": frozenset({"invited"}),
    "suspended": frozenset({"active"}),
    # No transition targets ``invited`` — it is only ever the create() state.
    "invited": frozenset(),
}
