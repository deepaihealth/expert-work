"""B-209 PR-2 —— the parse workers' memory ceilings must fire before the pod's.

Every parse worker (``EXPERT_WORK_KNOWLEDGE_PARSE_WORKERS``) carries an
address-space ceiling (``EXPERT_WORK_KNOWLEDGE_PARSE_WORKER_MAX_VM_MB``), the
one figure worker its own (``EXPERT_WORK_KNOWLEDGE_FIGURE_WORKER_MAX_VM_MB``).
They only protect the pod when the processes at their ceilings still fit in
the container's memory limit; otherwise the cgroup OOM-kills workers (or the
server) first and the ceilings never fire. The values live in different files
and overlays can patch any of them — so every overlay directory is rendered
(``kustomize build``, else ``kubectl kustomize``) and the rendered Deployment
and ConfigMap are checked together, container env overriding the ConfigMap.

Constants and where they come from (control-plane image f4a075a54,
``MALLOC_ARENA_MAX=2`` and the thread pins of the Deployment, linux/amd64,
one CPU; sampled every 5 ms with VmSize and VmRSS read from the same
``/proc/<pid>/status`` read — review rounds 5 and 6):

* :data:`MAIN_PROCESS_MIB` — the server process: 614 MiB RSS idle on the live
  test pod, plus 752 MiB with both of the server's ingestion budgets full at
  once (``control_plane/knowledge/parsed_size.py``): four Chinese text files
  just under the per-document cap of 2,500,000 parsed characters
  (``knowledge_document_max_chars``, which also caps the text the server
  chunks / embeds / stores at once) through the upload path, and four 47 MiB
  scanned PDFs through the recovery worker, holding the raw-bytes budget
  (``knowledge_ingest_raw_bytes_budget``, 100 MiB); one parse worker, no-op
  recognition, the fake embedder, real Postgres (2026-10-23, the parsed-size
  cap branch). The two budgets replace review round 6's N3: a document whose
  text is past the cap is refused in the parse worker, so the 110 MB-text Word
  file that took the server to 5.5 GB no longer reaches it. Those measurements
  apart: one document at the cap +463 MiB, four at once +503; eight 47 MiB
  scanned PDFs (four uploads + four recovery claims) +241 MiB.
* :data:`PARSE_WORKER_NEVER_RESIDENT_MIB` — the part of a parse worker's
  address space that is not resident when its RSS peaks: the smallest
  same-instant gap at the peak over the workloads measured — a 30-page text
  PDF (650 MiB), the 110 MB-text Word file parsed without figures (734 MiB),
  two 48 MP Word parses at once (699 and 705 MiB). A worker held to ``cap``
  MiB peaks at most ``cap - gap`` resident.
* :data:`FIGURE_WORKER_NEVER_RESIDENT_MIB` — the same for the figure worker
  (654 MiB; 59 MP JPEG / flate images on a text page and a 59 MP scanned
  page). The figure worker is counted at its own ceiling, not at a measured
  peak.

Shipped (2026-10-23: 4Gi, one parse worker at 2400 MiB, the figure worker at
1536 MiB): 1,366 + (2,400 - 650) + (1,536 - 654) = 3,998 MiB of 4,096 — 98 MiB
to spare. Raising either ingestion budget, the worker ceilings or the worker
count eats into that first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_K8S = _REPO_ROOT / "infra" / "k8s"
_CONTAINER = "control-plane"

MAIN_PROCESS_MIB = 614 + 752
PARSE_WORKER_NEVER_RESIDENT_MIB = 650
FIGURE_WORKER_NEVER_RESIDENT_MIB = 654

_WORKERS = "EXPERT_WORK_KNOWLEDGE_PARSE_WORKERS"
_CAP = "EXPERT_WORK_KNOWLEDGE_PARSE_WORKER_MAX_VM_MB"
_FIGURE_CAP = "EXPERT_WORK_KNOWLEDGE_FIGURE_WORKER_MAX_VM_MB"


def worst_case_mib(*, workers: int, cap_mib: int, figure_cap_mib: int) -> int:
    """The container's memory with every worker at its ceiling."""
    parse = cap_mib - PARSE_WORKER_NEVER_RESIDENT_MIB
    figure = figure_cap_mib - FIGURE_WORKER_NEVER_RESIDENT_MIB
    return MAIN_PROCESS_MIB + workers * parse + figure


def largest_cap_mib(*, limit_mib: int, workers: int, figure_cap_mib: int) -> int:
    """The largest parse-worker ceiling that still fits ``limit_mib``."""
    figure = figure_cap_mib - FIGURE_WORKER_NEVER_RESIDENT_MIB
    return (limit_mib - MAIN_PROCESS_MIB - figure) // workers + PARSE_WORKER_NEVER_RESIDENT_MIB


def _renderer() -> list[str] | None:
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    if shutil.which("kubectl"):
        return ["kubectl", "kustomize"]
    return None


def _render(overlay: Path) -> list[dict[str, Any]]:
    command = _renderer()
    if command is None:
        reason = "neither kustomize nor kubectl is installed — overlays cannot be rendered"
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail(f"{reason}; CI must render them, a skip there is a silent pass")
        pytest.skip(reason)
    rendered = subprocess.run(  # noqa: S603 — fixed argv list, no shell
        [*command, str(overlay)], capture_output=True, text=True, check=True, timeout=120
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _quantity_mib(value: str) -> int:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(int(value) / (1024 * 1024))  # plain bytes


def budget_of(docs: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """``(workers, cap, figure cap, limit)`` (MiB) of the rendered manifests."""
    (deployment,) = [
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == _CONTAINER
    ]
    (container,) = [
        c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == _CONTAINER
    ]
    maps = {d["metadata"]["name"]: d.get("data") or {} for d in docs if d["kind"] == "ConfigMap"}
    values: dict[str, str] = {}
    for source in container.get("envFrom") or []:
        name = (source.get("configMapRef") or {}).get("name")
        values.update({k: str(v) for k, v in maps.get(name, {}).items()})
    values.update({e["name"]: str(e["value"]) for e in container.get("env") or [] if "value" in e})
    fields = Settings.model_fields
    workers = int(values.get(_WORKERS, fields["knowledge_parse_workers"].default))
    cap = int(values.get(_CAP, fields["knowledge_parse_worker_max_vm_mb"].default))
    figure_cap = int(values.get(_FIGURE_CAP, fields["knowledge_figure_worker_max_vm_mb"].default))
    limit = _quantity_mib(container["resources"]["limits"]["memory"])
    return workers, cap, figure_cap, limit


def _problem(docs: list[dict[str, Any]]) -> str | None:
    workers, cap, figure_cap, limit = budget_of(docs)
    if cap <= PARSE_WORKER_NEVER_RESIDENT_MIB or figure_cap <= FIGURE_WORKER_NEVER_RESIDENT_MIB:
        return "no usable worker memory ceiling is configured"
    needed = worst_case_mib(workers=workers, cap_mib=cap, figure_cap_mib=figure_cap)
    if needed <= limit:
        return None
    largest = largest_cap_mib(limit_mib=limit, workers=workers, figure_cap_mib=figure_cap)
    return (
        f"{workers} parse workers at {cap} MiB + the figure worker at {figure_cap} MiB need up to "
        f"{needed} MiB with the server, but the container may use {limit} MiB "
        f"(the largest parse-worker ceiling that fits is {largest} MiB)"
    )


_OVERLAYS = sorted(path.parent for path in (_K8S / "overlays").glob("*/kustomization.yaml"))


@pytest.mark.parametrize("overlay", _OVERLAYS, ids=[path.name for path in _OVERLAYS])
def test_the_worker_ceilings_fire_before_the_pod_limit(overlay: Path) -> None:
    problem = _problem(_render(overlay))
    assert problem is None, f"{overlay.name}: {problem}"


#: What the constants above were measured with (see module doc).
_MEASURED_WITH = {"MALLOC_ARENA_MAX": "2", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}


@pytest.mark.parametrize("overlay", _OVERLAYS, ids=[path.name for path in _OVERLAYS])
def test_the_pod_runs_as_the_constants_were_measured(overlay: Path) -> None:
    (deployment,) = [
        d
        for d in _render(overlay)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == _CONTAINER
    ]
    (container,) = [
        c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == _CONTAINER
    ]
    env = {e["name"]: str(e.get("value")) for e in container.get("env") or []}
    assert {name: env.get(name) for name in _MEASURED_WITH} == _MEASURED_WITH


def _container_patch(body: str) -> str:
    return (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: control-plane\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - name: control-plane\n" + body
    )


def _env_patch(**values: int) -> str:
    listed = "".join(
        f'            - name: {name}\n              value: "{value}"\n'
        for name, value in values.items()
    )
    return _container_patch("          env:\n" + listed)


def _prod_with(tmp_path: Path, patches: dict[str, str]) -> Path:
    """A copy of the manifests whose prod overlay also applies ``patches``
    (file name → patch document)."""
    tree = tmp_path / "k8s"
    shutil.copytree(_K8S, tree)
    prod = tree / "overlays" / "prod"
    for name, body in patches.items():
        (prod / name).write_text(body)
    listed = "".join(f"- path: {name}\n" for name in patches)
    kustomization = prod / "kustomization.yaml"
    text = kustomization.read_text()
    assert text.count("patches:\n") == 1
    kustomization.write_text(text.replace("patches:\n", f"patches:\n{listed}"))
    return prod


def test_an_overlay_lowering_the_pod_limit_is_caught(tmp_path: Path) -> None:
    # Review round 5 (C, the reviewer's overlay): a patch setting the prod
    # memory limit to 2Gi must fail the check — shown on a ceiling that fits
    # the base limit, so only the patch can make it fail.
    fitting = {"cap-patch.yaml": _env_patch(**{_CAP: 1500})}
    assert _problem(_render(_prod_with(tmp_path / "fits", fitting))) is None
    limit = _container_patch(
        "          resources:\n            limits:\n              memory: 2Gi\n"
    )
    lowered = _render(_prod_with(tmp_path / "lowered", {**fitting, "memory-patch.yaml": limit}))
    assert budget_of(lowered)[3] == 2048
    assert _problem(lowered) is not None


def test_the_config_map_and_a_container_env_override_are_both_read(tmp_path: Path) -> None:
    config = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: control-plane-config\n"
        f'data:\n  {_WORKERS}: "3"\n  {_CAP}: "1234"\n  {_FIGURE_CAP}: "777"\n'
    )
    from_map = _render(_prod_with(tmp_path / "map", {"config-patch.yaml": config}))
    assert budget_of(from_map)[:3] == (3, 1234, 777)
    overridden = _prod_with(
        tmp_path / "env",
        {
            "config-patch.yaml": config,
            "env-patch.yaml": _env_patch(**{_WORKERS: 4, _CAP: 9999, _FIGURE_CAP: 888}),
        },
    )
    assert budget_of(_render(overridden))[:3] == (4, 9999, 888)


@pytest.mark.parametrize(
    ("limit_mib", "workers", "cap_mib", "figure_cap_mib", "fits"),
    [
        (4096, 2, 2400, 1536, False),  # two workers at 2400: 1652 MiB over
        (4096, 2, 1574, 1536, True),  # the largest ceiling two workers fit 4Gi at
        (4096, 2, 1575, 1536, False),
        (4096, 1, 2400, 1536, True),  # shipped (2026-10-23): one parse worker, 98 MiB spare
        (6144, 2, 2400, 1536, True),
        (2048, 2, 1800, 1536, False),
    ],
)
def test_the_budget_arithmetic(
    limit_mib: int, workers: int, cap_mib: int, figure_cap_mib: int, fits: bool
) -> None:
    needed = worst_case_mib(workers=workers, cap_mib=cap_mib, figure_cap_mib=figure_cap_mib)
    assert (needed <= limit_mib) is fits


def test_ci_fails_rather_than_skips_without_a_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 6 (N4): a skipped budget check in CI would pass silently.
    monkeypatch.setattr(shutil, "which", lambda _: None)

    def outcome() -> type[BaseException]:
        # Caught here: an escaping skip would mark this very test skipped.
        try:
            _render(_OVERLAYS[0])
        except (pytest.skip.Exception, pytest.fail.Exception) as exc:
            return type(exc)
        raise AssertionError("rendered without a renderer")

    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    assert outcome() is pytest.skip.Exception
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.setenv(name, "true")
        assert outcome() is pytest.fail.Exception
        monkeypatch.delenv(name)
