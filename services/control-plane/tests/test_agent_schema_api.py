"""GET /v1/agents/schema — Stream S PR B (Mini-ADR S-1)."""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from control_plane.api.agent_schema import build_agent_schema_router
from control_plane.audit import build_default_audit_logger
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import Principal


def _client() -> TestClient:
    app = FastAPI()
    # ``console_only()`` also injects the audit logger (FastAPI resolves every
    # dependency up front, whether or not the deny branch that writes runs).
    app.state.audit_logger = build_default_audit_logger(InMemoryAuditLogStore())

    # External-API-v1 P2-b — the route carries ``console_only()``, whose
    # ``_principal`` dependency 401s when nothing resolved an identity. The
    # real app has ``AuthMiddleware`` upstream; this bare test app does not,
    # so stand in for it. An employee principal is what the console editor
    # that consumes this schema actually presents.
    @app.middleware("http")
    async def _stub_principal(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.principal = Principal(
            subject_id="editor@test",
            subject_type="user",
            tenant_id=uuid4(),
            roles=("viewer",),
        )
        return await call_next(request)

    app.include_router(build_agent_schema_router())
    return TestClient(app)


def test_schema_endpoint_returns_agentspec_json_schema() -> None:
    resp = _client().get("/v1/agents/schema")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    schema = body["data"]
    assert "apiVersion" in schema["properties"]
    assert "spec" in schema["properties"]
    assert "kind" in schema["properties"]


def test_schema_endpoint_exposes_output_schema_field() -> None:
    """RT-1 PR-3 — the Tier3 ``output_schema`` field flows into the manifest
    editor's JSON Schema automatically (Pydantic model_json_schema)."""
    schema = _client().get("/v1/agents/schema").json()["data"]
    body = schema["$defs"]["AgentSpecBody"]
    assert "output_schema" in body["properties"]
    output_schema_spec = schema["$defs"]["OutputSchemaSpec"]
    props = output_schema_spec["properties"]
    assert set(props) == {"name", "json_schema", "strict"}
    # The Field descriptions ARE the editor-facing copy — they must exist.
    assert props["json_schema"]["description"]
    assert "final" in body["properties"]["output_schema"]["description"].lower()
