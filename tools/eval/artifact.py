"""J.9 Artifact eval — Stream J.13a (M0 baseline) closeout.

Mini-ADR J-25 + STREAM-J-DESIGN § 10 end-to-end behaviour lock. Drives
:class:`InMemoryArtifactStore` + the MIME-aware download helper (§ 10.5)
against scripted cases:

* **save** scenarios — first save gets ``version=1``, re-save bumps,
  kind is sticky.
* **lifecycle** scenarios — soft-delete hides from list / get, re-save
  un-deletes, cross-user misses (404 hiding rule).
* **update / versions** scenarios — ``update_kind`` round-trips,
  ``list_versions`` returns descending versions and ``None`` on
  unknown / soft-deleted parents.
* **MIME** scenarios — text-like → inline, image → inline, HTML / SVG →
  forced attachment ((c) red-line), unknown → octet-stream + attachment.

Per Mini-ADR J-37 J.9 metric is deterministic ``pass_rate`` with
threshold ≥ 0.90 (§ 18.3); achievable = 1.00 on the all-deterministic
scripted cases.
"""

from __future__ import annotations

import sys as _sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from pathlib import Path as _Path
from typing import Any, Literal
from uuid import UUID, uuid4

import yaml

from control_plane.api._artifact_mime import infer_content_type
from expert_work.persistence import InMemoryArtifactStore

_EVAL_DIR = _Path(__file__).resolve().parent
if str(_EVAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_EVAL_DIR))

from _capability import (  # type: ignore[import-not-found]  # noqa: E402
    CapabilityCaseResult,
    CapabilityReport,
)

CAPABILITY = "J.9_artifact"
METRIC_TYPE = "pass-rate"
THRESHOLD = {"pass_rate": 0.90}

# Fixed identities — RLS is exercised by J.14, so the J.9 cases all
# share one ``(tenant, user)`` and one alternate cross-user identity.
_TENANT = UUID("00000000-0000-0000-0000-0000000000c9")
_USER = UUID("00000000-0000-0000-0000-0000000000d9")
_OTHER_USER = UUID("00000000-0000-0000-0000-0000000000e9")
#: B-50 —— 这套用例是单 agent 场景,固定一个 key 即可(签名要求必传)。
_AGENT_KEY = "eval-agent-00000000"


Scenario = Literal[
    "save_basic",
    "save_version_increment",
    "save_keeps_kind",
    "soft_delete_hides_from_list",
    "soft_delete_cross_user_misses",
    "soft_delete_idempotent",
    "resave_undeletes",
    "update_kind_round_trip",
    "update_kind_hides_soft_deleted",
    "list_versions_desc",
    "list_versions_unknown_returns_none",
    "mime_md_inline_text",
    "mime_html_forces_attachment",
    "mime_svg_forces_attachment",
    "mime_png_inline_image",
    "mime_unknown_octet_attachment",
]


@dataclass(frozen=True)
class ArtifactCase:
    """One scripted artifact behaviour case."""

    case_id: str
    scenario: Scenario
    args: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scenario runners — each returns a bool + optional note.
# ---------------------------------------------------------------------------


async def _seed_one(store: InMemoryArtifactStore, *, name: str, kind: str, path: str) -> None:
    await store.save_version(
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT_KEY,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        path_in_workspace=path,
        created_in_thread="t",
    )


async def _run_save_basic() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    version = await store.save_version(
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT_KEY,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    if version.version != 1:
        return False, f"expected version=1, got {version.version}"
    if version.size_bytes is not None:
        return False, "size_bytes should be lazy-NULL on first save"
    return True, ""


async def _run_save_version_increment() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="x", kind="code", path="x.py")
    v2 = await store.save_version(
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT_KEY,
        name="x",
        kind="code",
        path_in_workspace="x.py",
        created_in_thread="t",
    )
    if v2.version != 2:
        return False, f"expected v2=2, got {v2.version}"
    artifacts = await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY)
    if not artifacts or artifacts[0].latest_version != 2:
        return False, "latest_version not bumped"
    return True, ""


async def _run_save_keeps_kind() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="x", kind="document", path="x.md")
    await _seed_one(store, name="x", kind="code", path="x.md")
    artifacts = await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY)
    if artifacts[0].kind != "document":
        return False, f"kind changed to {artifacts[0].kind!r} on re-save"
    return True, ""


async def _run_soft_delete_hides_from_list() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="r.md", kind="document", path="r.md")
    hit = await store.soft_delete(
        tenant_id=_TENANT, user_id=_USER, name="r.md", now=datetime.now(UTC)
    )
    if not hit:
        return False, "soft_delete reported miss on a known active row"
    if await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY) != []:
        return False, "soft-deleted row leaked into default list"
    revealed = await store.list_for_user(
        tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY, include_deleted=True
    )
    if len(revealed) != 1 or revealed[0].deleted_at is None:
        return False, "include_deleted should reveal the soft-deleted row with deleted_at set"
    if (
        await store.get_latest_version(
            tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY, name="r.md"
        )
        is not None
    ):
        return False, "get_latest_version should hide soft-deleted"
    return True, ""


async def _run_soft_delete_cross_user_misses() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="r.md", kind="document", path="r.md")
    hit = await store.soft_delete(
        tenant_id=_TENANT,
        user_id=_OTHER_USER,
        name="r.md",
        now=datetime.now(UTC),
    )
    if hit:
        return False, "cross-user soft_delete should miss (404 hiding rule)"
    return True, ""


async def _run_soft_delete_idempotent() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="r.md", kind="document", path="r.md")
    now = datetime.now(UTC)
    first = await store.soft_delete(
        tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY, name="r.md", now=now
    )
    second = await store.soft_delete(
        tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY, name="r.md", now=now
    )
    if not first or second:
        return False, f"expected (True, False), got ({first}, {second})"
    return True, ""


async def _run_resave_undeletes() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="r.md", kind="document", path="v1.md")
    await store.soft_delete(
        tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY, name="r.md", now=datetime.now(UTC)
    )
    v2 = await store.save_version(
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT_KEY,
        name="r.md",
        kind="document",
        path_in_workspace="v2.md",
        created_in_thread="t",
    )
    if v2.version != 2:
        return False, f"re-save should bump version to 2, got {v2.version}"
    active = await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY)
    if not active or active[0].deleted_at is not None:
        return False, "re-save should have un-deleted the row"
    return True, ""


async def _run_update_kind_round_trip() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="r.md", kind="document", path="r.md")
    row = (await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY))[0]
    updated = await store.update_kind(
        tenant_id=_TENANT, user_id=_USER, artifact_id=row.id, kind="code"
    )
    if updated is None or updated.kind != "code":
        return False, f"update_kind round-trip failed: {updated!r}"
    return True, ""


async def _run_update_kind_hides_soft_deleted() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    await _seed_one(store, name="r.md", kind="document", path="r.md")
    row = (await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY))[0]
    await store.soft_delete(
        tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY, name="r.md", now=datetime.now(UTC)
    )
    if (
        await store.update_kind(tenant_id=_TENANT, user_id=_USER, artifact_id=row.id, kind="code")
        is not None
    ):
        return False, "update_kind should hide soft-deleted rows (None)"
    return True, ""


async def _run_list_versions_desc() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    for path in ("v1.md", "v2.md", "v3.md"):
        await _seed_one(store, name="r.md", kind="document", path=path)
    row = (await store.list_for_user(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT_KEY))[0]
    versions = await store.list_versions(tenant_id=_TENANT, user_id=_USER, artifact_id=row.id)
    if versions is None or [v.version for v in versions] != [3, 2, 1]:
        return False, f"expected versions [3,2,1], got {versions}"
    return True, ""


async def _run_list_versions_unknown_returns_none() -> tuple[bool, str]:
    store = InMemoryArtifactStore()
    rows = await store.list_versions(tenant_id=_TENANT, user_id=_USER, artifact_id=uuid4())
    if rows is not None:
        return False, "list_versions on unknown should return None"
    return True, ""


def _check_mime(path: str, *, expected_ct_prefix: str, expected_disp: str) -> tuple[bool, str]:
    inferred = infer_content_type(path=path)
    if not inferred.content_type.startswith(expected_ct_prefix):
        return False, (
            f"{path}: expected content_type startswith {expected_ct_prefix!r}, "
            f"got {inferred.content_type!r}"
        )
    if inferred.disposition != expected_disp:
        return False, (
            f"{path}: expected disposition={expected_disp!r}, got {inferred.disposition!r}"
        )
    return True, ""


async def _run_mime_md_inline_text() -> tuple[bool, str]:
    return _check_mime(
        "report.md",
        expected_ct_prefix="text/plain",
        expected_disp="inline",
    )


async def _run_mime_html_forces_attachment() -> tuple[bool, str]:
    return _check_mime(
        "page.html",
        expected_ct_prefix="text/html",
        expected_disp="attachment",
    )


async def _run_mime_svg_forces_attachment() -> tuple[bool, str]:
    return _check_mime(
        "logo.svg",
        expected_ct_prefix="image/svg+xml",
        expected_disp="attachment",
    )


async def _run_mime_png_inline_image() -> tuple[bool, str]:
    return _check_mime(
        "photo.png",
        expected_ct_prefix="image/png",
        expected_disp="inline",
    )


async def _run_mime_unknown_octet_attachment() -> tuple[bool, str]:
    return _check_mime(
        "dump.bin",
        expected_ct_prefix="application/octet-stream",
        expected_disp="attachment",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_SCENARIOS: dict[str, Any] = {
    "save_basic": _run_save_basic,
    "save_version_increment": _run_save_version_increment,
    "save_keeps_kind": _run_save_keeps_kind,
    "soft_delete_hides_from_list": _run_soft_delete_hides_from_list,
    "soft_delete_cross_user_misses": _run_soft_delete_cross_user_misses,
    "soft_delete_idempotent": _run_soft_delete_idempotent,
    "resave_undeletes": _run_resave_undeletes,
    "update_kind_round_trip": _run_update_kind_round_trip,
    "update_kind_hides_soft_deleted": _run_update_kind_hides_soft_deleted,
    "list_versions_desc": _run_list_versions_desc,
    "list_versions_unknown_returns_none": _run_list_versions_unknown_returns_none,
    "mime_md_inline_text": _run_mime_md_inline_text,
    "mime_html_forces_attachment": _run_mime_html_forces_attachment,
    "mime_svg_forces_attachment": _run_mime_svg_forces_attachment,
    "mime_png_inline_image": _run_mime_png_inline_image,
    "mime_unknown_octet_attachment": _run_mime_unknown_octet_attachment,
}


async def _run_case(case: ArtifactCase) -> CapabilityCaseResult:
    runner = _SCENARIOS.get(case.scenario)
    if runner is None:
        return CapabilityCaseResult(
            case_id=case.case_id,
            passed=False,
            notes=(f"unknown scenario {case.scenario!r}",),
        )
    passed, note = await runner()
    return CapabilityCaseResult(
        case_id=case.case_id,
        passed=passed,
        notes=(note,) if note else (),
    )


# ---------------------------------------------------------------------------
# Public surface — matches sibling eval modules (load_cases / evaluate_set).
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> tuple[ArtifactCase, ...]:
    """Parse the YAML dataset into :class:`ArtifactCase` tuples."""
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    raw_cases = payload.get("cases", [])
    out: list[ArtifactCase] = []
    for raw in raw_cases:
        out.append(
            ArtifactCase(
                case_id=str(raw["id"]),
                scenario=str(raw["scenario"]),  # type: ignore[arg-type]
                args=dict(raw.get("args", {})),
            )
        )
    return tuple(out)


async def evaluate_set(cases: Sequence[ArtifactCase]) -> CapabilityReport:
    """Run all cases sequentially; produce the capability report."""
    per_case: list[CapabilityCaseResult] = []
    for case in cases:
        per_case.append(await _run_case(case))
    sample_size = len(per_case)
    passed = sum(1 for r in per_case if r.passed)
    pass_rate = passed / sample_size if sample_size else 0.0
    threshold = THRESHOLD["pass_rate"]
    status = "PASS" if pass_rate >= threshold and sample_size > 0 else "FAIL"
    return CapabilityReport(
        capability=CAPABILITY,
        metric_type=METRIC_TYPE,
        sample_size=sample_size,
        threshold=dict(THRESHOLD),
        aggregate_score={"pass_rate": pass_rate},
        status=status,
        per_case=tuple(per_case),
    )


__all__ = [
    "CAPABILITY",
    "METRIC_TYPE",
    "THRESHOLD",
    "ArtifactCase",
    "evaluate_set",
    "load_cases",
]
