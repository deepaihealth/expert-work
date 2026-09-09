"""B-11 — the two ``ResourceType`` Literals must be the SAME set.

``expert_work.protocol.audit.ResourceType`` and ``control_plane.audit.ResourceType``
are hand-mirrored (every entry carries a "[memory:audit-literal-drift] — both
must stay in sync" comment), and they still drifted: ``"artifact"`` (Stream
J.9-step3) and ``"approval"`` (Stream J.8) were only ever added on the protocol
side, so ``emit(resource_type="artifact")`` (``api/artifacts.py`` twice,
``api/external_artifacts.py``) and ``emit(resource_type="approval")``
(``api/runs.py``) were type errors that never turned red — CI's mypy job does
not scan ``services/control-plane`` (``ci.yml`` "mypy strict" lists
``packages`` + five other services).

Per-value membership checks (the previous shape of this test, and
``test_audit_mcp_server_types.py``'s) only guard the one value they name.
Set EQUALITY guards every value, in both directions, so the next one-sided
addition fails here instead of surfacing as a silent type hole.
"""

from __future__ import annotations

from typing import get_args

from control_plane.audit import ResourceType as CpResourceType
from expert_work.protocol.audit import ResourceType as ProtoResourceType


def test_resource_type_literals_are_the_same_set_on_both_sides() -> None:
    cp = set(get_args(CpResourceType))
    proto = set(get_args(ProtoResourceType))
    assert cp == proto, (
        f"protocol-only (missing from control_plane.audit): {sorted(proto - cp)}; "
        f"control-plane-only (missing from expert_work.protocol.audit): {sorted(cp - proto)}"
    )
    # Not vacuous: the values this fix was about are really in there.
    assert {"artifact", "approval"} <= cp
