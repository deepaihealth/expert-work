"""Agent template inheritance resolution — Stream Agent-Templates (M1).

A tenant per-user agent may declare ``spec.extends = "<template>@<version>"`` to
inherit from a platform-curated base template (see
``docs/design/platform-agent-templates.md``). The fields of ``AgentSpecBody``
split into three tiers with different ownership:

- **① security floor** (``defenses`` + the ``policies`` safety sub-fields) — the
  platform sets a floor the tenant can only *tighten*, never weaken or drop. This
  is enforced at **build time** (not just in the UI) so a tenant editing the
  manifest directly cannot escape it.
- **② capability** (``tools`` / ``skills`` / ``subagents`` / ``workflow`` / ...) —
  platform default, tenant may add or remove.
- **③ tenant-owned** (``system_prompt`` / ``model`` / ``knowledge`` / ...) —
  tenant replaces freely.

**M1 scope.** The instance is stored as a *complete* ``AgentSpec`` (a copy of the
base materialized at instantiation, with ``extends`` recorded), so tier ② / ③
values come from the instance itself — the tenant edits them directly. The only
thing pulled from the live base at build time is the **tier ① floor**, re-asserted
via :func:`enforce_security_floor`. Auto-propagation of tier ① / ② base changes to
``@latest`` instances is M2 (the instance is pinned in M1).

This module is pure (depends only on protocol types) so the security-critical
floor logic is unit-testable in isolation. The async wrapper that loads the base
by ``extends`` and injects this into the build pipeline lives in the control-plane.
"""

from __future__ import annotations

from enum import StrEnum

from expert_work.protocol.agent_spec import AgentSpec, DefenseSpec, PolicySpec

__all__ = [
    "FIELD_TIERS",
    "FieldTier",
    "enforce_security_floor",
    "parse_extends_ref",
    "resolve_extends",
]


class FieldTier(StrEnum):
    """Ownership tier of an ``AgentSpecBody`` field (single source of truth).

    The drift-guard test asserts every ``AgentSpecBody`` field (bar ``extends``
    itself) is classified here, so a newly added manifest field cannot silently
    escape tiering (see ``docs/design/platform-agent-templates.md`` §6)."""

    SECURITY_FLOOR = "security_floor"  # ① platform floor — tenant may only tighten
    CAPABILITY = "capability"  # ② platform default — tenant may add / remove
    TENANT_OWNED = "tenant_owned"  # ③ tenant replaces freely


# Single source of truth: every AgentSpecBody field → its tier. ``extends`` is the
# inheritance link itself (cleared in the resolved spec), so it is not tiered.
FIELD_TIERS: dict[str, FieldTier] = {
    # ① security floor — enforced via enforce_security_floor (M1).
    "defenses": FieldTier.SECURITY_FLOOR,
    "policies": FieldTier.SECURITY_FLOOR,  # only safety/pii/approval_required_tools
    # ② capability — platform default + tenant delta (M2 propagation; M1 instance
    # carries the materialized set the tenant edited directly).
    "tools": FieldTier.CAPABILITY,
    "skills": FieldTier.CAPABILITY,
    # SE-16 (SE-A42) — the evolution auto-attach opt-in rides with ``skills``.
    "auto_attach_evolved_skills": FieldTier.CAPABILITY,
    "subagents": FieldTier.CAPABILITY,
    "dynamic_workers": FieldTier.CAPABILITY,
    "workflow": FieldTier.CAPABILITY,
    "reflection": FieldTier.CAPABILITY,
    "routing": FieldTier.CAPABILITY,
    # RT-1 PR-3 (RT-ADR-4) — the structured final-reply contract is a platform
    # capability default the tenant may add / remove.
    "output_schema": FieldTier.CAPABILITY,
    # ③ tenant-owned — tenant replaces.
    "system_prompt": FieldTier.TENANT_OWNED,
    "model": FieldTier.TENANT_OWNED,
    "memory": FieldTier.TENANT_OWNED,
    "knowledge": FieldTier.TENANT_OWNED,
    "vision": FieldTier.TENANT_OWNED,
    "cache": FieldTier.TENANT_OWNED,
    "description": FieldTier.TENANT_OWNED,
    "display_name": FieldTier.TENANT_OWNED,
    "triggers": FieldTier.TENANT_OWNED,
    "tenant_config": FieldTier.TENANT_OWNED,
    "dynamic_context": FieldTier.TENANT_OWNED,
    "stream_deadline_s": FieldTier.TENANT_OWNED,
    "idle_timeout_s": FieldTier.TENANT_OWNED,
    "code": FieldTier.TENANT_OWNED,
    "hooks": FieldTier.TENANT_OWNED,
    "observability": FieldTier.TENANT_OWNED,
    # sandbox is tenant-owned for resource/network needs in M1. Network egress has
    # its own proxy + audit; flooring sandbox.network is a future hardening item.
    "sandbox": FieldTier.TENANT_OWNED,
}

# Per-switch strictness ordering for DefenseSpec (higher index = stricter). The
# floor merge picks the stricter of (base, instance) for each switch, so a tenant
# can tighten but never weaken an inherited defense.
_DEFENSE_ORDER: dict[str, list[str]] = {
    "prompt_injection": ["off", "spotlight"],
    "output_screen": ["off", "block"],
    "output_judge": ["off", "block"],
    "output_judge_on_error": ["open", "closed"],
    "action_screen": ["off", "approval", "block"],
    "action_screen_on_error": ["open", "closed"],
    "output_dlp": ["off", "redact"],
}


def parse_extends_ref(ref: str) -> tuple[str, str]:
    """Split ``"name@version"`` into ``(name, version)``.

    ``version`` may be the literal ``"latest"`` (the @latest track is resolved by
    the caller against the template store). Raises ``ValueError`` on a malformed
    ref (missing ``@`` or empty side)."""
    name, sep, version = ref.partition("@")
    if not sep or not name or not version:
        raise ValueError(f"invalid extends ref {ref!r}: expected 'name@version'")
    return name, version


def _stricter(field: str, base_value: str, instance_value: str) -> str:
    order = _DEFENSE_ORDER[field]
    # An unknown value (shouldn't happen — Literal-typed) defers to the base floor.
    base_rank = order.index(base_value) if base_value in order else len(order)
    inst_rank = order.index(instance_value) if instance_value in order else -1
    return instance_value if inst_rank > base_rank else base_value


def _floor_defenses(base: DefenseSpec, instance: DefenseSpec) -> DefenseSpec:
    """Per-switch max-strict merge: tenant may tighten, never weaken the base."""
    merged = {
        field: _stricter(field, getattr(base, field), getattr(instance, field))
        for field in _DEFENSE_ORDER
    }
    return instance.model_copy(update=merged)


def _floor_policies(base: PolicySpec, instance: PolicySpec) -> PolicySpec:
    """Floor the security sub-fields of PolicySpec, leaving tenant-tunable ones
    (rate_limit / context_compression / deadlines / ...) as the instance has them.

    For the opaque ``safety`` / ``pii`` dicts strictness is undefinable generically,
    so the floor is **base-key-authoritative**: a base-set key always wins (tenant
    cannot weaken or drop it) and the tenant may only *add* new keys.
    ``approval_required_tools`` floors as a union (more gated tools = stricter)."""
    floored_safety = {**instance.safety, **base.safety}
    floored_pii = {**instance.pii, **base.pii}
    floored_approval = sorted({*base.approval_required_tools, *instance.approval_required_tools})
    return instance.model_copy(
        update={
            "safety": floored_safety,
            "pii": floored_pii,
            "approval_required_tools": floored_approval,
        }
    )


def enforce_security_floor(base: AgentSpec, instance: AgentSpec) -> AgentSpec:
    """Return ``instance`` with its tier-① security fields clamped to the floor
    defined by ``base`` (max-strict). Tier ② / ③ fields are left untouched — the
    instance is authoritative for them in M1.

    This is the build-time guarantee that an inheriting tenant agent can never run
    with weaker defenses than its platform template prescribes, regardless of how
    the tenant edited the manifest."""
    body = instance.spec
    floored = body.model_copy(
        update={
            "defenses": _floor_defenses(base.spec.defenses, body.defenses),
            "policies": _floor_policies(base.spec.policies, body.policies),
        }
    )
    return instance.model_copy(update={"spec": floored})


def resolve_extends(base: AgentSpec, instance: AgentSpec) -> AgentSpec:
    """Resolve an inheriting instance against its base template (M1 semantics):
    enforce the tier-① security floor and clear ``extends`` so the result is a
    plain, build-ready ``AgentSpec`` (the downstream build is unchanged).

    M2 will additionally pull tier-② capability deltas and un-overridden tier-③
    fields from the live base for ``@latest`` propagation."""
    floored = enforce_security_floor(base, instance)
    cleared = floored.spec.model_copy(update={"extends": None})
    return floored.model_copy(update={"spec": cleared})
