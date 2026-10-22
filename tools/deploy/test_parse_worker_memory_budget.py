"""B-209 PR-2 —— the parse workers' memory ceiling must fire before the pod's.

Every parse worker (``EXPERT_WORK_KNOWLEDGE_PARSE_WORKERS``, plus the one
figure worker) carries an address-space ceiling
(``EXPERT_WORK_KNOWLEDGE_PARSE_WORKER_MAX_VM_MB``). It only protects the pod
when the processes at that ceiling still fit in the container's memory limit;
otherwise the cgroup OOM-kills workers (or the server) first and the ceiling
never fires. Both values live in different files — this checks them together,
for every overlay as rendered (base config map + the overlay's patch).

Constants and where they come from (control-plane image f4a075a54,
``MALLOC_ARENA_MAX=2``, linux/amd64, two documents parsed at once):

* :data:`MAIN_PROCESS_MIB` — the uvicorn server process at idle on the live
  test pod (614 MiB RSS, measured 2026-10-22).
* :data:`WORKER_NEVER_RESIDENT_MIB` — the part of a parse worker's address
  space that never becomes resident: the smallest gap between peak address
  space and peak RSS across every measured parse (text PDF 1522 vs 507 MiB;
  48 MP photo in Word 2368 vs 1272; 58 MP 2581 vs 1484; 110 MB of text 2533
  vs 1434). A worker held to ``cap`` MiB of address space therefore peaks at
  most ``cap - WORKER_NEVER_RESIDENT_MIB`` resident.
* :data:`FIGURE_WORKER_PEAK_MIB` — the figure worker's peak (410 MiB, figures
  of a PDF with a 48 MP image). It never decodes a picture at full size: it
  renders clips and pages at most 2,000 px on the long edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_K8S = _REPO_ROOT / "infra" / "k8s"
_CONFIG_MAP = "control-plane-config"

MAIN_PROCESS_MIB = 614
WORKER_NEVER_RESIDENT_MIB = 1015
FIGURE_WORKER_PEAK_MIB = 410


def worst_case_mib(*, workers: int, cap_mib: int) -> int:
    """The container's memory with every parse worker at its ceiling."""
    worker = cap_mib - WORKER_NEVER_RESIDENT_MIB
    return MAIN_PROCESS_MIB + workers * worker + min(FIGURE_WORKER_PEAK_MIB, worker)


def _quantity_mib(value: str) -> int:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(int(value) / (1024 * 1024))  # plain bytes


def _config(overlay: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for path in (
        _K8S / "base" / "configmap.yaml",
        _K8S / "overlays" / overlay / "configmap-patch.yaml",
    ):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == _CONFIG_MAP:
                data.update({key: str(value) for key, value in (doc.get("data") or {}).items()})
    return data


def _limit_mib() -> int:
    doc: dict[str, Any] = yaml.safe_load(
        (_K8S / "base" / "control-plane" / "deployment.yaml").read_text()
    )
    (container,) = [
        c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "control-plane"
    ]
    return _quantity_mib(container["resources"]["limits"]["memory"])


@pytest.mark.parametrize("overlay", ["test", "prod"])
def test_the_worker_ceiling_fires_before_the_pod_limit(overlay: str) -> None:
    config = _config(overlay)
    default_workers = Settings.model_fields["knowledge_parse_workers"].default
    workers = int(config.get("EXPERT_WORK_KNOWLEDGE_PARSE_WORKERS", default_workers))
    cap = int(config.get("EXPERT_WORK_KNOWLEDGE_PARSE_WORKER_MAX_VM_MB", "0"))
    assert cap > WORKER_NEVER_RESIDENT_MIB, "no usable parse-worker memory ceiling is configured"
    limit = _limit_mib()
    needed = worst_case_mib(workers=workers, cap_mib=cap)
    assert needed <= limit, (
        f"{overlay}: {workers} parse workers + 1 figure worker at a {cap} MiB ceiling need up to "
        f"{needed} MiB with the server, but the container may use {limit} MiB"
    )


@pytest.mark.parametrize(
    ("limit_mib", "workers", "cap_mib", "fits"),
    [
        (4096, 2, 2400, True),  # shipped: about 300 MiB to spare
        (4096, 2, 2550, True),  # option 1 as measured: 2 MiB to spare
        (4096, 2, 2560, False),  # 18 MiB over
        (2048, 2, 1800, False),  # option 2
        (2048, 1, 1800, True),  # option 2b
    ],
)
def test_the_budget_arithmetic(limit_mib: int, workers: int, cap_mib: int, fits: bool) -> None:
    assert (worst_case_mib(workers=workers, cap_mib=cap_mib) <= limit_mib) is fits
