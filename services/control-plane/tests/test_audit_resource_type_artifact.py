"""B-11 — ``"artifact"`` must be in BOTH ``ResourceType`` Literals.

The protocol-side Literal (``expert_work.protocol.audit``) has carried
``"artifact"`` since Stream J.9-step3; the control-plane copy
(``control_plane.audit``) never got it, so the three ``emit(...,
resource_type="artifact")`` call sites (``api/artifacts.py`` ×2,
``api/external_artifacts.py``) were type errors that never turned red —
CI's mypy job does not scan ``services/control-plane`` (``ci.yml``
"mypy strict" step lists ``packages`` + five other services). This pytest
guard is therefore the only CI-visible check for the drift; the mypy run
over those three files is the local proof (see the fix's PR).
"""

from __future__ import annotations

from typing import get_args

from control_plane.audit import ResourceType as CpResourceType
from expert_work.protocol.audit import ResourceType as ProtoResourceType


def test_resource_type_literal_includes_artifact_on_both_sides() -> None:
    assert "artifact" in get_args(CpResourceType)
    assert "artifact" in get_args(ProtoResourceType)
