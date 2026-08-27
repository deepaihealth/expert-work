"""Tests for ``control_plane.seed_canary`` — X-14 P1.

Two halves, mirroring the module:

* the embedded canary manifest is a CI-guarded artefact (same pattern as
  ``test_canonical_manifest.py``): it must stay loadable by the real
  :class:`ManifestLoader` and must keep declaring exactly the capability
  surface ``tools/deploy/canary.py`` exercises;
* :func:`seed_canary` is exercised against the in-memory stores for the
  create / reuse / rotate idempotency contract.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.manifest.errors import ManifestValidationError
from control_plane.seed_canary import (
    CANARY_AGENT_VERSION,
    DEFAULT_AGENT_CODE,
    SERVICE_ACCOUNT_NAME,
    SeedCanaryError,
    load_canary_spec,
    seed_canary,
)
from expert_work.persistence import InMemoryTenantConfigStore
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.persistence.auth import InMemoryApiKeyStore, InMemoryServiceAccountStore
from expert_work.protocol import ApiKeyScope
from expert_work.protocol.agent_spec import BuiltinToolSpec

# --------------------------------------------------------------------------- manifest guard


def test_canary_manifest_loads_and_validates() -> None:
    spec = load_canary_spec()
    assert spec.metadata.name == DEFAULT_AGENT_CODE
    assert spec.metadata.version == CANARY_AGENT_VERSION
    assert spec.kind == "Agent"


def test_canary_manifest_declares_exactly_the_canary_builtins() -> None:
    """``canary.py`` drives exec_python → write_file → save_artifact; the
    manifest must declare all three and nothing else (no web_search / http —
    the canary must not depend on external egress)."""
    spec = load_canary_spec()
    names = {t.name for t in spec.spec.tools if isinstance(t, BuiltinToolSpec)}
    assert names == {"exec_python", "write_file", "save_artifact"}
    assert len(spec.spec.tools) == 3


def test_canary_manifest_never_pauses() -> None:
    """An approval interrupt would park the run as ``paused`` and red every
    release — the canary agent must not gate any tool on human approval."""
    spec = load_canary_spec()
    assert spec.spec.policies.approval_required_tools == []


def test_canary_manifest_relies_on_platform_key() -> None:
    """Same contract as the canonical agent: no pinned ``api_key_ref`` — the
    provider key comes from the platform credential."""
    spec = load_canary_spec()
    assert spec.spec.model.api_key_ref is None
    assert spec.spec.model.fallback == []


def test_canary_manifest_model_override() -> None:
    spec = load_canary_spec(model_provider="glm", model_name="glm-4.7")
    assert spec.spec.model.provider == "glm"
    assert spec.spec.model.name == "glm-4.7"


def test_canary_manifest_rejects_unknown_provider() -> None:
    with pytest.raises(ManifestValidationError):
        load_canary_spec(model_provider="not-a-provider")


# --------------------------------------------------------------------------- seed idempotency


def _stores() -> tuple[
    InMemoryTenantConfigStore,
    InMemoryServiceAccountStore,
    InMemoryApiKeyStore,
    InMemoryAgentSpecStore,
]:
    return (
        InMemoryTenantConfigStore(),
        InMemoryServiceAccountStore(),
        InMemoryApiKeyStore(),
        InMemoryAgentSpecStore(),
    )


async def test_seed_canary_unknown_tenant_fails_fast() -> None:
    tenants, accounts, keys, specs = _stores()
    with pytest.raises(SeedCanaryError, match=r"tenant .* not found"):
        await seed_canary(
            tenants=tenants, accounts=accounts, keys=keys, specs=specs, tenant_id=uuid4()
        )


async def test_seed_canary_first_run_creates_everything() -> None:
    tenants, accounts, keys, specs = _stores()
    tenant_id = uuid4()
    await tenants.create(tenant_id=tenant_id, display_name="t", actor_id="test")

    result = await seed_canary(
        tenants=tenants, accounts=accounts, keys=keys, specs=specs, tenant_id=tenant_id
    )

    assert result.service_account_created is True
    assert result.agent_created is True
    assert result.minted_key_plaintext is not None
    assert result.minted_key_plaintext.startswith("aforge_pat_")

    sa_rows = await accounts.list_by_tenant(tenant_id=tenant_id)
    assert [s.name for s in sa_rows] == [SERVICE_ACCOUNT_NAME]
    key_rows = await keys.list_by_service_account(
        tenant_id=tenant_id, service_account_id=result.service_account_id
    )
    assert len(key_rows) == 1
    assert key_rows[0].scopes == (ApiKeyScope.WRITE,)
    record = await specs.get(
        tenant_id=tenant_id, name=DEFAULT_AGENT_CODE, version=CANARY_AGENT_VERSION
    )
    assert record is not None


async def test_seed_canary_rerun_is_idempotent() -> None:
    tenants, accounts, keys, specs = _stores()
    tenant_id = uuid4()
    await tenants.create(tenant_id=tenant_id, display_name="t", actor_id="test")

    first = await seed_canary(
        tenants=tenants, accounts=accounts, keys=keys, specs=specs, tenant_id=tenant_id
    )
    second = await seed_canary(
        tenants=tenants, accounts=accounts, keys=keys, specs=specs, tenant_id=tenant_id
    )

    assert second.service_account_created is False
    assert second.service_account_id == first.service_account_id
    assert second.agent_created is False
    # An active key exists → nothing minted, plaintext not re-derivable.
    assert second.minted_key_plaintext is None
    key_rows = await keys.list_by_service_account(
        tenant_id=tenant_id, service_account_id=first.service_account_id
    )
    assert len(key_rows) == 1


async def test_seed_canary_rotate_key_mints_another() -> None:
    tenants, accounts, keys, specs = _stores()
    tenant_id = uuid4()
    await tenants.create(tenant_id=tenant_id, display_name="t", actor_id="test")

    first = await seed_canary(
        tenants=tenants, accounts=accounts, keys=keys, specs=specs, tenant_id=tenant_id
    )
    rotated = await seed_canary(
        tenants=tenants,
        accounts=accounts,
        keys=keys,
        specs=specs,
        tenant_id=tenant_id,
        rotate_key=True,
    )

    assert rotated.minted_key_plaintext is not None
    assert rotated.minted_key_plaintext != first.minted_key_plaintext
    key_rows = await keys.list_by_service_account(
        tenant_id=tenant_id, service_account_id=first.service_account_id
    )
    assert len(key_rows) == 2
