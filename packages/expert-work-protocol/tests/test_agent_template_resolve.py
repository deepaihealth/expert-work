"""Tier-① security-floor resolution tests — Stream Agent-Templates (M1).

The floor is the security-critical guarantee: an inheriting tenant agent can
never run with weaker defenses / safety policies than its platform template,
regardless of how the tenant edited the manifest. These tests pin that contract.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from expert_work.protocol import (
    FIELD_TIERS,
    AgentSpec,
    FieldTier,
    enforce_security_floor,
    parse_extends_ref,
    resolve_extends,
)
from expert_work.protocol.agent_spec import AgentSpecBody

_BASE_DOC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are a support agent"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec(**spec_over: Any) -> AgentSpec:
    doc = deepcopy(_BASE_DOC)
    doc["spec"].update(spec_over)
    return AgentSpec.model_validate(doc)


# ---------------------------------------------------------------------------
# Drift guard (design §6) — every AgentSpecBody field must be tiered.
# ---------------------------------------------------------------------------


def test_every_field_classified_exactly_once() -> None:
    fields = set(AgentSpecBody.model_fields) - {"extends"}
    assert set(FIELD_TIERS) == fields, "FIELD_TIERS drifted from AgentSpecBody"


def test_security_floor_fields_are_defenses_and_policies() -> None:
    floor = {f for f, t in FIELD_TIERS.items() if t is FieldTier.SECURITY_FLOOR}
    assert floor == {"defenses", "policies"}


# ---------------------------------------------------------------------------
# DefenseSpec floor — tenant may tighten, never weaken.
# ---------------------------------------------------------------------------


def test_tenant_cannot_weaken_a_base_defense() -> None:
    base = _spec(defenses={"output_screen": "block", "prompt_injection": "spotlight"})
    # Tenant tries to turn both OFF in their inheriting manifest.
    instance = _spec(defenses={"output_screen": "off", "prompt_injection": "off"})
    resolved = enforce_security_floor(base, instance)
    assert resolved.spec.defenses.output_screen == "block"
    assert resolved.spec.defenses.prompt_injection == "spotlight"


def test_tenant_may_tighten_above_base() -> None:
    base = _spec(defenses={"output_judge": "off", "action_screen": "off"})
    instance = _spec(defenses={"output_judge": "block", "action_screen": "block"})
    resolved = enforce_security_floor(base, instance)
    assert resolved.spec.defenses.output_judge == "block"
    assert resolved.spec.defenses.action_screen == "block"


def test_action_screen_ordinal_block_beats_approval_beats_off() -> None:
    base = _spec(defenses={"action_screen": "approval"})
    # Tenant weakens approval -> off : clamped back up to approval.
    weaker = enforce_security_floor(base, _spec(defenses={"action_screen": "off"}))
    assert weaker.spec.defenses.action_screen == "approval"
    # Tenant tightens approval -> block : kept.
    stronger = enforce_security_floor(base, _spec(defenses={"action_screen": "block"}))
    assert stronger.spec.defenses.action_screen == "block"


def test_on_error_closed_is_stricter_than_open() -> None:
    base = _spec(defenses={"output_judge_on_error": "closed"})
    instance = _spec(defenses={"output_judge_on_error": "open"})
    resolved = enforce_security_floor(base, instance)
    assert resolved.spec.defenses.output_judge_on_error == "closed"


# ---------------------------------------------------------------------------
# PolicySpec floor — safety/pii base-authoritative; approval list union.
# ---------------------------------------------------------------------------


def test_safety_base_key_wins_tenant_may_add() -> None:
    base = _spec(policies={"safety": {"block_self_harm": True}})
    instance = _spec(policies={"safety": {"block_self_harm": False, "tenant_extra": "x"}})
    resolved = enforce_security_floor(base, instance)
    # Base key authoritative (tenant cannot flip it off); tenant addition kept.
    assert resolved.spec.policies.safety["block_self_harm"] is True
    assert resolved.spec.policies.safety["tenant_extra"] == "x"


def test_pii_base_key_authoritative() -> None:
    base = _spec(policies={"pii": {"redact_ssn": True}})
    instance = _spec(policies={"pii": {"redact_ssn": False}})
    resolved = enforce_security_floor(base, instance)
    assert resolved.spec.policies.pii["redact_ssn"] is True


def test_approval_required_tools_union() -> None:
    base = _spec(policies={"approval_required_tools": ["wire_transfer"]})
    instance = _spec(policies={"approval_required_tools": ["delete_account"]})
    resolved = enforce_security_floor(base, instance)
    assert set(resolved.spec.policies.approval_required_tools) == {
        "wire_transfer",
        "delete_account",
    }


def test_tenant_tunable_policy_fields_untouched() -> None:
    base = _spec(policies={"run_deadline_s": 10})
    instance = _spec(policies={"run_deadline_s": 99, "safety": {}})
    resolved = enforce_security_floor(base, instance)
    # run_deadline_s is tenant-tunable (not a floor field) -> instance value kept.
    assert resolved.spec.policies.run_deadline_s == 99


# ---------------------------------------------------------------------------
# Tier ② / ③ untouched; resolve_extends clears the link.
# ---------------------------------------------------------------------------


def test_tier2_and_tier3_fields_are_instance_authoritative() -> None:
    base = _spec(skills=["base-skill"], system_prompt={"template": "base prompt"})
    instance = _spec(skills=["tenant-skill"], system_prompt={"template": "tenant prompt"})
    resolved = enforce_security_floor(base, instance)
    assert resolved.spec.skills == ["tenant-skill"]
    assert resolved.spec.system_prompt.template == "tenant prompt"


def test_resolve_extends_clears_extends_link() -> None:
    base = _spec()
    instance = _spec(extends="support-bot@1.0.0")
    resolved = resolve_extends(base, instance)
    assert resolved.spec.extends is None


def test_resolve_extends_applies_floor() -> None:
    base = _spec(defenses={"output_screen": "block"})
    instance = _spec(extends="support-bot@1.0.0", defenses={"output_screen": "off"})
    resolved = resolve_extends(base, instance)
    assert resolved.spec.defenses.output_screen == "block"
    assert resolved.spec.extends is None


def test_input_specs_not_mutated() -> None:
    base = _spec(defenses={"output_screen": "block"})
    instance = _spec(defenses={"output_screen": "off"})
    enforce_security_floor(base, instance)
    # Immutability: the original instance is unchanged (new object returned).
    assert instance.spec.defenses.output_screen == "off"


# ---------------------------------------------------------------------------
# parse_extends_ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("support-bot@1.0.0", ("support-bot", "1.0.0")),
        ("support-bot@latest", ("support-bot", "latest")),
    ],
)
def test_parse_extends_ref_ok(ref: str, expected: tuple[str, str]) -> None:
    assert parse_extends_ref(ref) == expected


@pytest.mark.parametrize("bad", ["support-bot", "@1.0.0", "support-bot@", ""])
def test_parse_extends_ref_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid extends ref"):
        parse_extends_ref(bad)


def test_output_schema_is_capability_tier() -> None:
    """RT-1 PR-3 (RT-ADR-4) — the structured final-reply contract is tier ②."""
    assert FIELD_TIERS["output_schema"] is FieldTier.CAPABILITY


def test_workflow_execution_mode_survives_resolve() -> None:
    """B-35 —— spec §6 假设钉住:``workflow`` 整块是 CAPABILITY tier,
    新子键 ``execution_mode`` 随块走,模板解析不得丢它。"""
    base = _spec()
    instance = _spec(
        workflow={"type": "plan_execute", "execution_mode": "plan_first"},
    )
    resolved = resolve_extends(base, instance)
    assert resolved.spec.workflow.execution_mode == "plan_first"
    assert resolved.spec.workflow.type == "plan_execute"
