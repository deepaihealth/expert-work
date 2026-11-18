"""B-319 — every API that hands out an agent manifest hands it out as a valid
manifest: the shape ``GET /v1/agents/schema`` describes (root key
``apiVersion``, no extra keys) and ``POST /v1/agents`` accepts.

The console dumps ``record.spec`` straight into the YAML view and validates the
YAML against that schema before switching back to the form; people also copy
that YAML from one environment to create the agent in another. A response
that spelled the root key ``api_version`` broke both: the YAML → form switch
failed on every agent with no edit at all.

The shared frontend fixture
``apps/admin-ui/src/api/__fixtures__/agent_manifest_contract.json`` is a real
``GET /v1/agents/{name}/{version}`` response plus the schema's root contract;
the console's own test switches YAML → form on it, and the last case here keeps
it in step with what this API actually sends.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from control_plane.app import create_app
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "apps/admin-ui/src/api/__fixtures__/agent_manifest_contract.json"
)

_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-6
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""

_BASE = "/v1/agents/code-reviewer/1.0.0"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=DEFAULT_DEV_TENANT_ID)}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control-plane.test", headers=headers
    ) as c:
        yield c


async def _schema(client: AsyncClient) -> dict[str, Any]:
    resp = await client.get("/v1/agents/schema")
    assert resp.status_code == 200, resp.text
    schema: dict[str, Any] = resp.json()["data"]
    return schema


def _errors(doc: Any, schema: dict[str, Any]) -> list[str]:
    return sorted(
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in Draft202012Validator(schema).iter_errors(doc)
    )


async def _create(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post("/v1/agents", json={"manifest_yaml": _YAML})
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()["data"]
    return body


async def _save_draft(client: AsyncClient) -> None:
    sha = (await client.get(_BASE)).json()["data"]["record"]["spec_sha256"]
    resp = await client.put(
        f"{_BASE}/draft",
        json={"manifest_yaml": _YAML.replace("you are a reviewer", "you are a careful reviewer")},
        headers={"If-Match": sha},
    )
    assert resp.status_code == 200, resp.text


async def test_every_manifest_the_api_hands_out_validates_against_the_schema(
    client: AsyncClient,
) -> None:
    schema = await _schema(client)
    created = await _create(client)
    await _save_draft(client)
    detail = (await client.get(_BASE)).json()["data"]["record"]
    listed = (await client.get("/v1/agents")).json()["data"]["items"]
    revision = (await client.get(f"{_BASE}/revisions/1")).json()["data"]["record"]

    handed_out = {
        "POST /v1/agents record.spec": created["record"]["spec"],
        "GET detail record.spec": detail["spec"],
        "GET detail record.draft.spec": detail["draft"]["spec"],
        "GET list items[0].spec": listed[0]["spec"],
        "GET revisions/1 record.spec": revision["spec"],
    }
    problems = {where: _errors(doc, schema) for where, doc in handed_out.items()}
    assert problems == {where: [] for where in handed_out}


async def test_copied_yaml_creates_the_same_agent_under_a_new_name(client: AsyncClient) -> None:
    """The cross-environment copy: YAML dumped from one agent's detail is a
    valid manifest and creates an agent identical apart from its name."""
    schema = await _schema(client)
    await _create(client)
    original = (await client.get(_BASE)).json()["data"]["record"]["spec"]

    copied = yaml.safe_load(yaml.safe_dump(original, allow_unicode=True))
    assert _errors(copied, schema) == []
    copied["metadata"]["name"] = "code-reviewer-copy"
    resp = await client.post(
        "/v1/agents", json={"manifest_yaml": yaml.safe_dump(copied, allow_unicode=True)}
    )
    assert resp.status_code == 201, resp.text

    created = (await client.get("/v1/agents/code-reviewer-copy/1.0.0")).json()["data"]["record"]
    assert created["spec"] == {
        **original,
        "metadata": {**original["metadata"], "name": "code-reviewer-copy"},
    }


async def test_frontend_fixture_matches_the_live_wire_shape(client: AsyncClient) -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    schema = await _schema(client)
    root = fixture["schema_root"]
    assert sorted(schema["required"]) == sorted(root["required"])
    assert schema["additionalProperties"] == root["additionalProperties"]
    assert sorted(schema["properties"]) == sorted(root["properties"])

    await _create(client)
    await _save_draft(client)
    live = (await client.get(_BASE)).json()["data"]["record"]
    expected = fixture["detail"]["record"]
    assert set(live) >= set(expected), set(expected) - set(live)
    assert sorted(live["spec"]) == sorted(expected["spec"])
    assert sorted(live["draft"]["spec"]) == sorted(expected["draft"]["spec"])
    # The fixture's manifests are ones this backend accepts as they stand.
    assert _errors(expected["spec"], schema) == []
    assert _errors(expected["draft"]["spec"], schema) == []
