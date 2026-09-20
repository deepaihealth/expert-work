"""J.7a Skill 静态启用 eval — Stream J.13a (M0 baseline) closeout.

Mini-ADR J-23 § 15 build-time + admin moderation 全套行为锁。Drives
:class:`InMemorySkillStore` + ``build_agent`` + admin moderation /
ZIP parser against scripted cases:

* **resolve** scenarios — bare-name → active version, pinned ``name@N``
  → exact row, multi-skill ordering preserved.
* **error** scenarios — not_found / version_not_found / not_active /
  required_models mismatch / tool conflict.
* **moderation** scenarios — regex deny-list + size cap.
* **zip** scenarios — round-trip import/export + zip-slip reject.

Per Mini-ADR J-37 J.7a metric is deterministic ``pass_rate`` (no LLM
judge). Threshold ≥ 0.80 (§ 18.3); achievable = 1.00 on all-deterministic
scripted cases.
"""

from __future__ import annotations

import io
import sys as _sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import Path as _Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import yaml

from expert_work.persistence import InMemorySkillStore
from expert_work.protocol import AgentSpec, SkillStatus
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore

_EVAL_DIR = _Path(__file__).resolve().parent
if str(_EVAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_EVAL_DIR))

from _capability import (  # type: ignore[import-not-found]  # noqa: E402
    CapabilityCaseResult,
    CapabilityReport,
)

CAPABILITY = "J.7_skill"
METRIC_TYPE = "pass-rate"
THRESHOLD = {"pass_rate": 0.80}


# ---------------------------------------------------------------------------
# Per-case schema
# ---------------------------------------------------------------------------


Scenario = Literal[
    "resolve_bare",
    "resolve_pinned",
    "multi_skill_order",
    "not_found",
    "version_not_found",
    "not_active",
    "required_models_mismatch",
    "tool_conflict",
    "moderation_injection",
    "moderation_oversize",
    "zip_round_trip",
    "zip_slip",
]


@dataclass(frozen=True)
class ScriptedSkillVersion:
    """Sketched ``SkillVersion`` for a case's seed store."""

    name: str
    version: int
    prompt_fragment: str = "be helpful"
    tool_names: tuple[str, ...] = ()
    required_models: tuple[str, ...] = ()
    status: SkillStatus = SkillStatus.ACTIVE
    # These cases assert the skill body appears in the assembled prompt
    # (``expected_prompt_contains``), i.e. the EAGER injection path. RT-ADR-11
    # made lazy the default, so pin eager here to keep exercising that path.
    lazy_load: bool = False


@dataclass(frozen=True)
class SkillCase:
    case_id: str
    scenario: Scenario
    seed: tuple[ScriptedSkillVersion, ...] = ()
    skills: tuple[str, ...] = ()
    agent_model_name: str = "claude-sonnet-4-6"
    # Resolve / multi_skill_order:
    expected_prompt_contains: tuple[str, ...] = ()
    # RT-ADR-12 — strings that must NOT appear in the assembled system prompt.
    # A lazy skill's body is withheld (only its ``<available-skills>`` summary
    # is present), so a lazy case asserts the body text is absent here.
    expected_prompt_absent: tuple[str, ...] = ()
    expected_order: tuple[str, ...] = ()
    # Moderation:
    moderation_text: str = ""
    # ZIP:
    zip_payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-case runners
# ---------------------------------------------------------------------------


_ANTHROPIC_KEY_NAME = "anthropic-test"


async def _platform_resolver(provider: str) -> list[str]:
    # Stream Y-2 — agent builds resolve the LLM key via the platform resolver
    # (manifest ``api_key_ref`` is ignored). Stream Y-MK — returns the ordered
    # key list (one key here). These cases are all anthropic.
    del provider
    return [f"secret://{_ANTHROPIC_KEY_NAME}"]


def _spec_with_skills(skills: tuple[str, ...], model_name: str) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "test", "version": "1.0.0", "tenant": "test-tenant"},
            "spec": {
                "tenant_config": {},
                "model": {
                    "provider": "anthropic",
                    "name": model_name,
                    "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
                },
                "system_prompt": {"template": "you are an agent"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
                "skills": list(skills),
            },
        }
    )


async def _seed_store(case: SkillCase, tenant_id: UUID) -> InMemorySkillStore:
    store = InMemorySkillStore()
    for spec_version in case.seed:
        skill = await store.get_skill_by_name(tenant_id=tenant_id, name=spec_version.name)
        if skill is None:
            skill = await store.create_skill(
                skill_id=uuid4(), tenant_id=tenant_id, name=spec_version.name
            )
        await store.add_version(
            version_id=uuid4(),
            skill_id=skill.id,
            tenant_id=tenant_id,
            prompt_fragment=spec_version.prompt_fragment,
            tool_names=spec_version.tool_names,
            required_models=spec_version.required_models,
            lazy_load=spec_version.lazy_load,
        )
        if spec_version.status != SkillStatus.DRAFT:
            await store.set_status(
                skill_id=skill.id, tenant_id=tenant_id, status=spec_version.status
            )
    return store


def _make_resolver(store: InMemorySkillStore, tenant_id: UUID) -> Any:
    from orchestrator.agent_factory import _SkillLookupResult

    async def resolver(_tenant: Any, name: str, version: int | None) -> _SkillLookupResult:
        skill = await store.get_skill_by_name(tenant_id=tenant_id, name=name)
        if skill is None:
            return _SkillLookupResult.not_found()
        if version is None:
            if skill.status != SkillStatus.ACTIVE or skill.latest_version == 0:
                return _SkillLookupResult.not_active()
            row = await store.get_version_by_number(
                skill_id=skill.id, tenant_id=tenant_id, version=skill.latest_version
            )
            return _SkillLookupResult.ok(row) if row is not None else _SkillLookupResult.not_found()
        row = await store.get_version_by_number(
            skill_id=skill.id, tenant_id=tenant_id, version=version
        )
        if row is None:
            return _SkillLookupResult.version_not_found()
        return _SkillLookupResult.ok(row)

    return resolver


async def _run_resolve_case(case: SkillCase) -> CapabilityCaseResult:
    """Build agent with skill_resolver; verify ``<skill>`` wrap + ordering."""
    from orchestrator.agent_factory import build_agent

    notes: list[str] = []
    tenant_id = uuid4()
    store = await _seed_store(case, tenant_id)
    resolver = _make_resolver(store, tenant_id)
    spec = _spec_with_skills(case.skills, case.agent_model_name)
    secret_store = LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"})
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            spec,
            secret_store=secret_store,
            checkpointer=cp,
            skill_resolver=resolver,
            tenant_id=tenant_id,
            provider_key_resolver=_platform_resolver,
        )
    for needle in case.expected_prompt_contains:
        if needle not in built.system_prompt:
            notes.append(f"system_prompt missing {needle!r}")
    for needle in case.expected_prompt_absent:
        if needle in built.system_prompt:
            notes.append(f"system_prompt must not contain {needle!r} (lazy body leaked)")
    if case.expected_order:
        positions = [built.system_prompt.find(needle) for needle in case.expected_order]
        if any(p < 0 for p in positions):
            notes.append(f"system_prompt missing one of expected_order: {case.expected_order}")
        elif positions != sorted(positions):
            notes.append(f"system_prompt order wrong: got positions {positions}")
    return CapabilityCaseResult(case_id=case.case_id, passed=not notes, notes=tuple(notes))


async def _run_error_case(
    case: SkillCase, expected_exc: type[BaseException]
) -> CapabilityCaseResult:
    from orchestrator.agent_factory import build_agent

    notes: list[str] = []
    tenant_id = uuid4()
    store = await _seed_store(case, tenant_id)
    resolver = _make_resolver(store, tenant_id)
    spec = _spec_with_skills(case.skills, case.agent_model_name)
    secret_store = LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"})
    try:
        async with make_checkpointer("memory") as cp:
            await build_agent(
                spec,
                secret_store=secret_store,
                checkpointer=cp,
                skill_resolver=resolver,
                tenant_id=tenant_id,
                provider_key_resolver=_platform_resolver,
            )
        notes.append(f"expected {expected_exc.__name__} but build_agent succeeded")
    except expected_exc:
        pass  # success — expected exception raised
    except Exception as exc:
        notes.append(f"expected {expected_exc.__name__} but got {type(exc).__name__}: {exc}")
    return CapabilityCaseResult(case_id=case.case_id, passed=not notes, notes=tuple(notes))


def _run_moderation_case(case: SkillCase, expected_pattern: str) -> CapabilityCaseResult:
    from control_plane.api._skill_moderation import (
        ModerationError,
        moderate_prompt_fragment,
    )

    notes: list[str] = []
    # Dataset sentinel for the oversize case — keeps the YAML small but
    # produces a 65 KiB + 1 payload that beats the 64 KiB cap.
    text = case.moderation_text
    if text == "@OVERSIZE":
        text = "x" * (64 * 1024 + 1)
    try:
        # B-84 —— 这个 case 验的是 deny-list + 字节上限,与 eager 字符上限无关;
        # 用 lazy(平台默认)跑,免得 eager 上限抢在 deny-list 前面报错。
        moderate_prompt_fragment(text, lazy_load=True)
        notes.append(
            f"expected ModerationError matching {expected_pattern!r} "
            f"but moderate_prompt_fragment returned cleanly"
        )
    except ModerationError as exc:
        if expected_pattern not in exc.code:
            notes.append(
                f"ModerationError raised but code={exc.code!r} != expected {expected_pattern!r}"
            )
    return CapabilityCaseResult(case_id=case.case_id, passed=not notes, notes=tuple(notes))


def _run_zip_round_trip_case(case: SkillCase) -> CapabilityCaseResult:
    from control_plane.api._skill_zip import build_skill_zip, parse_skill_zip

    notes: list[str] = []
    payload = case.zip_payload
    blob = build_skill_zip(
        name=payload["name"],
        description=payload.get("description", ""),
        category=payload.get("category"),
        required_models=tuple(payload.get("required_models", ())),
        prompt_fragment=payload["prompt_fragment"],
        tool_names=tuple(payload.get("tool_names", ())),
    )
    parsed = parse_skill_zip(blob)
    if parsed.name != payload["name"]:
        notes.append(f"name mismatch: {parsed.name!r} vs {payload['name']!r}")
    if parsed.prompt_fragment != payload["prompt_fragment"]:
        notes.append("prompt_fragment lost in round-trip")
    if parsed.tool_names != tuple(payload.get("tool_names", ())):
        notes.append("tool_names lost in round-trip")
    return CapabilityCaseResult(case_id=case.case_id, passed=not notes, notes=tuple(notes))


def _run_zip_slip_case(case: SkillCase) -> CapabilityCaseResult:
    """Build a malicious ZIP with a ``../`` entry; verify parse_skill_zip rejects."""
    from control_plane.api._skill_zip import SkillZipError, parse_skill_zip

    notes: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as archive:
        archive.writestr("../../etc/passwd", b"root:x:0:0")
    try:
        parse_skill_zip(buf.getvalue())
        notes.append("expected SkillZipError on ../../ entry but parse returned cleanly")
    except SkillZipError:
        pass  # success — zip-slip rejected as expected
    return CapabilityCaseResult(case_id=case.case_id, passed=not notes, notes=tuple(notes))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _run_case(case: SkillCase) -> CapabilityCaseResult:
    from orchestrator.errors import (
        SkillConflictError,
        SkillModelMismatchError,
        SkillNotActiveError,
        SkillNotFoundError,
        SkillVersionNotFoundError,
    )

    if case.scenario in {"resolve_bare", "resolve_pinned", "multi_skill_order"}:
        return await _run_resolve_case(case)
    if case.scenario == "not_found":
        return await _run_error_case(case, SkillNotFoundError)
    if case.scenario == "version_not_found":
        return await _run_error_case(case, SkillVersionNotFoundError)
    if case.scenario == "not_active":
        return await _run_error_case(case, SkillNotActiveError)
    if case.scenario == "required_models_mismatch":
        return await _run_error_case(case, SkillModelMismatchError)
    if case.scenario == "tool_conflict":
        return await _run_error_case(case, SkillConflictError)
    if case.scenario == "moderation_injection":
        return _run_moderation_case(case, "prompt_injection_pattern")
    if case.scenario == "moderation_oversize":
        return _run_moderation_case(case, "prompt_fragment_too_large")
    if case.scenario == "zip_round_trip":
        return _run_zip_round_trip_case(case)
    if case.scenario == "zip_slip":
        return _run_zip_slip_case(case)
    return CapabilityCaseResult(
        case_id=case.case_id,
        passed=False,
        notes=(f"unknown scenario {case.scenario!r}",),
    )


# ---------------------------------------------------------------------------
# Evaluator + loader
# ---------------------------------------------------------------------------


async def evaluate_set(cases: Sequence[SkillCase]) -> CapabilityReport:
    per_case: list[CapabilityCaseResult] = []
    for case in cases:
        per_case.append(await _run_case(case))
    sample = len(per_case)
    pass_rate = sum(1 for r in per_case if r.passed) / sample if sample else 0.0
    status = "PASS" if pass_rate >= THRESHOLD["pass_rate"] else "FAIL"
    return CapabilityReport(
        capability=CAPABILITY,
        metric_type=METRIC_TYPE,
        sample_size=sample,
        threshold=THRESHOLD,
        aggregate_score={"pass_rate": pass_rate},
        status=cast(Any, status),
        per_case=tuple(per_case),
    )


def load_cases(path: Path) -> list[SkillCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [_parse_case(entry) for entry in raw.get("cases", [])]


def _parse_case(entry: dict[str, Any]) -> SkillCase:
    seed = tuple(
        ScriptedSkillVersion(
            name=str(s["name"]),
            version=int(s.get("version", 1)),
            prompt_fragment=str(s.get("prompt_fragment", "be helpful")),
            tool_names=tuple(s.get("tool_names", ())),
            required_models=tuple(s.get("required_models", ())),
            status=SkillStatus(s.get("status", "active")),
        )
        for s in entry.get("seed", [])
    )
    return SkillCase(
        case_id=str(entry["id"]),
        scenario=cast(Any, entry["scenario"]),
        seed=seed,
        skills=tuple(entry.get("skills", ())),
        agent_model_name=str(entry.get("agent_model_name", "claude-sonnet-4-6")),
        expected_prompt_contains=tuple(entry.get("expected_prompt_contains", ())),
        expected_order=tuple(entry.get("expected_order", ())),
        moderation_text=str(entry.get("moderation_text", "")),
        zip_payload=dict(entry.get("zip_payload", {})),
    )


__all__ = [
    "CAPABILITY",
    "METRIC_TYPE",
    "THRESHOLD",
    "SkillCase",
    "evaluate_set",
    "load_cases",
]
