"""CI guard for the checked-in canonical agent manifest — Stream P (P-17).

``manifests/canonical-agent/v1.0.0.yaml`` is the agent the M0→M1 Gate E2E
SOP registers and runs through Phases 1-6. This test loads it through the
real :class:`ManifestLoader` so a schema change that would make the manifest
un-registerable fails CI here instead of mid-E2E, and asserts the capability
surface each phase depends on is actually declared.
"""

from __future__ import annotations

from pathlib import Path

from control_plane.manifest.loader import ManifestLoader

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANONICAL = _REPO_ROOT / "manifests" / "canonical-agent" / "v1.0.0.yaml"


def test_canonical_manifest_loads_and_validates() -> None:
    spec = ManifestLoader().load_from_path(_CANONICAL)
    assert spec.metadata.name == "canonical-agent"
    assert spec.metadata.version == "1.0.0"
    assert spec.kind == "Agent"


def test_canonical_manifest_declares_phase_capabilities() -> None:
    spec = ManifestLoader().load_from_path(_CANONICAL)

    # Phase 5 — multimodal Path A: vision-capable main model, no vision block.
    assert spec.spec.model.supports_vision is True
    assert spec.spec.vision is None

    # Phase 2 — long-term memory recall + write-back.
    assert spec.spec.memory is not None
    assert spec.spec.memory.long_term is not None
    assert spec.spec.memory.long_term.write_back is True

    # Phase 3 — persistent workspace mounted at /workspace.
    assert spec.spec.sandbox.filesystem.persistent_workspace is True

    # Phase 4 — human-approval gate on an exact tool name (not a regex).
    assert spec.spec.policies.approval_required_tools == ["http"]


def test_canonical_manifest_relies_on_platform_key() -> None:
    """No model in the fallback tree pins ``api_key_ref`` — the canonical agent
    resolves its provider key from the platform-configured credential (Stream Q,
    Mini-ADR Q-8), so the whole E2E can be driven by pasting a key in the web UI.
    The per-agent override path (an explicit ``api_key_ref``) is covered by the
    orchestrator's ``build_llm_router`` tests."""
    spec = ManifestLoader().load_from_path(_CANONICAL)
    stack = [spec.spec.model]
    while stack:
        model = stack.pop()
        assert model.api_key_ref is None
        stack.extend(model.fallback)


def test_canonical_manifest_names_catalog_models_only() -> None:
    """B-112 —— the platform's own reference agent must pass the save-time
    model-name check it is registered through."""
    from control_plane.manifest_model_names import off_catalog_models_error

    spec = ManifestLoader().load_from_path(_CANONICAL)
    assert off_catalog_models_error(spec) is None
