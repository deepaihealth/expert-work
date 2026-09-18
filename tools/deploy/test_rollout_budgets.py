"""The four timeouts that decide whether a healthy release looks failed (B-56).

Each of them lives in a different file and each carries a comment saying the
others must be checked when it changes — which is exactly the arrangement that
lets them drift. The relationship, smallest to largest:

    startup probe budget  <  drain cap  <  terminationGracePeriodSeconds  <  rollout timeout

* startup budget too small -> the kubelet restarts a new pod that was merely
  slow to bind, `rollout status` gives up, and release.sh reports a good
  release as failed (the B-56 incident: 5 restarts, healthy ~13 min in).
* drain cap >= grace period -> the kubelet kills a draining pod mid-handoff
  (B-80).
* grace period >= rollout timeout -> a worst-case drain outlasts the wait, and
  every release with a long-running conversation reports failed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from control_plane.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_SH = _REPO_ROOT / "tools" / "deploy" / "release.sh"
_CONTROL_PLANE_DEPLOYMENT = (
    _REPO_ROOT / "infra" / "k8s" / "base" / "control-plane" / "deployment.yaml"
)


def _control_plane_container() -> dict[str, Any]:
    doc = yaml.safe_load(_CONTROL_PLANE_DEPLOYMENT.read_text())
    spec = doc["spec"]["template"]["spec"]
    containers = [c for c in spec["containers"] if c["name"] == "control-plane"]
    assert containers, "control-plane container disappeared from the Deployment"
    return {**containers[0], "_pod_spec": spec}


def _rollout_timeout_s() -> int:
    """The ``${EXPERT_WORK_ROLLOUT_TIMEOUT:-600s}`` default from release.sh."""
    m = re.search(
        r'ROLLOUT_TIMEOUT="\$\{EXPERT_WORK_ROLLOUT_TIMEOUT:-(\d+)s\}"',
        _RELEASE_SH.read_text(),
    )
    assert m, "release.sh no longer defines ROLLOUT_TIMEOUT in the expected shape"
    return int(m.group(1))


def _startup_budget_s() -> int:
    probe = _control_plane_container()["startupProbe"]
    return int(probe["periodSeconds"]) * int(probe["failureThreshold"])


def test_startup_probe_budget_survives_a_cold_node() -> None:
    """60s (the pre-B-56 value) was not enough on a cold ACS serverless node:
    the probe failures were all `connection refused` — the app had not bound
    yet — not crashes."""
    assert _startup_budget_s() >= 120


def test_the_four_timeouts_are_ordered() -> None:
    grace = _control_plane_container()["_pod_spec"]["terminationGracePeriodSeconds"]
    drain = Settings.model_fields["run_drain_timeout_s"].default
    assert _startup_budget_s() < drain < grace < _rollout_timeout_s()
