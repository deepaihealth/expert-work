"""A.8 — ``HTTPSupervisorRuntime`` injects W3C trace context on the wire.

The inject half of the cross-service propagation contract (subsystems/20
§ 5.8). The matching extract half lives in the sandbox-supervisor suite
(``test_trace_middleware.py``); the two meet at the ``traceparent`` wire
format, which expert-work-common's propagation tests validate independently.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from expert_work.common.observability import (
    TRACEPARENT_HEADER,
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    init_tracing,
)
from orchestrator.tools.sandbox import HTTPSupervisorRuntime
from orchestrator.tools.workspace_store import SupervisorWorkspaceStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tracing_setup() -> Iterator[None]:
    provider = init_tracing(
        service_name="sandbox-trace-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


def _capturing_client(seen: dict[str, httpx.Headers]) -> HTTPSupervisorRuntime:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        return httpx.Response(200, json={"sandbox_id": str(uuid4())})

    return HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )


async def test_post_injects_traceparent_matching_active_span(tracing_setup: None) -> None:
    seen: dict[str, httpx.Headers] = {}
    client = _capturing_client(seen)

    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "tool_call"):
        active_trace_id = current_trace_id_hex()
        await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert active_trace_id is not None
    traceparent = seen["headers"].get(TRACEPARENT_HEADER)
    assert traceparent is not None
    # traceparent = "00-<32-hex trace_id>-<16-hex span_id>-<flags>"
    assert traceparent.split("-")[1] == active_trace_id


async def test_workspace_get_injects_traceparent(tracing_setup: None) -> None:
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        return httpx.Response(200, content=b"file-bytes")

    store = SupervisorWorkspaceStore(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "tool_call"):
        active_trace_id = current_trace_id_hex()
        await store.read_file(tenant_id=uuid4(), user_id=uuid4(), path="a.txt")

    assert active_trace_id is not None
    assert seen["headers"].get(TRACEPARENT_HEADER, "").split("-")[1] == active_trace_id


async def test_workspace_put_sends_bytes_and_path(tracing_setup: None) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.params.get("path")
        seen["content"] = request.content
        seen["headers"] = request.headers
        return httpx.Response(204)

    store = SupervisorWorkspaceStore(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "tool_call"):
        active_trace_id = current_trace_id_hex()
        await store.write_file(
            tenant_id=uuid4(), user_id=uuid4(), path="uploads/x.pdf", data=b"PDF"
        )

    assert seen["method"] == "PUT"
    assert seen["path"] == "uploads/x.pdf"
    assert seen["content"] == b"PDF"
    headers = seen["headers"]
    assert isinstance(headers, httpx.Headers)
    assert active_trace_id is not None
    assert headers.get(TRACEPARENT_HEADER, "").split("-")[1] == active_trace_id


async def test_workspace_put_raises_on_error_response(tracing_setup: None) -> None:
    from orchestrator.tools.sandbox import SandboxSupervisorError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="boom")

    store = SupervisorWorkspaceStore(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SandboxSupervisorError):
        await store.write_file(
            tenant_id=uuid4(), user_id=uuid4(), path="uploads/x.pdf", data=b"PDF"
        )


async def test_exec_aligns_timeout_and_passes_it(tracing_setup: None) -> None:
    # The exec read timeout is derived from the per-call deadline so a long
    # command (e.g. ``pip install`` granted 300s) isn't cut short by the
    # client; here we assert the deadline reaches the supervisor + a result
    # round-trips + the trace context is still injected (the exec path no
    # longer rides the shared ``_post`` helper).
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["headers"] = request.headers
        seen["body"] = _json.loads(request.content)
        return httpx.Response(
            200, json={"stdout": "ok", "stderr": "", "exit_code": 0, "timed_out": False}
        )

    client = HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "tool_call"):
        active_trace_id = current_trace_id_hex()
        outcome = await client.exec(sandbox_id=uuid4(), code="pip install fpdf2", timeout_s=300)

    assert outcome.stdout == "ok"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["timeout_s"] == 300
    headers = seen["headers"]
    assert isinstance(headers, httpx.Headers)
    assert active_trace_id is not None
    assert headers.get(TRACEPARENT_HEADER, "").split("-")[1] == active_trace_id


async def test_exec_injects_agent_key_env_when_set() -> None:
    """sandbox migration wave 2 (spec 决策 10) — ``agent_key`` non-empty →
    the POST body carries ``envs.PYTHONUSERBASE``, the same value the cloud
    backend (``AgentSandboxClient``) sends via ``commands.run(envs=...)``
    (contract-tested in ``test_sandbox_runtime_contract.py``)."""
    import json as _json

    from expert_work.persistence import SANDBOX_AGENTS_ROOT

    body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(_json.loads(request.content))
        return httpx.Response(
            200, json={"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}
        )

    client = HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    await client.exec(sandbox_id=uuid4(), code="pass", timeout_s=5, agent_key="my-agent")

    assert body["envs"] == {"PYTHONUSERBASE": f"{SANDBOX_AGENTS_ROOT}/my-agent"}


async def test_exec_omits_envs_when_agent_key_unset() -> None:
    import json as _json

    body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(_json.loads(request.content))
        return httpx.Response(
            200, json={"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False}
        )

    client = HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    await client.exec(sandbox_id=uuid4(), code="pass", timeout_s=5)

    assert "envs" not in body  # back-compat: no key when there's nothing to inject


async def test_workspace_list_parses_files_and_traces(tracing_setup: None) -> None:
    from orchestrator.tools.workspace_store import WorkspaceFileEntry

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = request.headers
        return httpx.Response(200, json={"files": [{"path": "report.pdf", "size": 2048}]})

    store = SupervisorWorkspaceStore(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "tool_call"):
        active_trace_id = current_trace_id_hex()
        entries = await store.list_files(tenant_id=uuid4(), user_id=uuid4())

    assert entries == [WorkspaceFileEntry(path="report.pdf", size=2048)]
    assert seen["method"] == "GET"
    assert str(seen["path"]).endswith("/files")
    headers = seen["headers"]
    assert isinstance(headers, httpx.Headers)
    assert active_trace_id is not None
    assert headers.get(TRACEPARENT_HEADER, "").split("-")[1] == active_trace_id


async def test_no_active_span_omits_traceparent(tracing_setup: None) -> None:
    # No surrounding span (OTel active but no span) → nothing to propagate;
    # external egress and uninstrumented call sites stay header-free.
    seen: dict[str, httpx.Headers] = {}
    client = _capturing_client(seen)

    await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert TRACEPARENT_HEADER not in seen["headers"]


async def test_acquire_serializes_seed_files_base64() -> None:
    # skill-runtime §5.1 — seed_files land in the acquire POST body as
    # {path, content_b64} entries (the supervisor decodes + docker-cp's them).
    import base64
    import json

    body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"sandbox_id": str(uuid4())})

    client = HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    await client.acquire(
        tenant_id=uuid4(),
        thread_id="t",
        seed_files=(("skills/pptx/SKILL.md", b"hello"),),
    )

    assert body["seed_files"] == [
        {"path": "skills/pptx/SKILL.md", "content_b64": base64.b64encode(b"hello").decode()}
    ]


async def test_acquire_omits_seed_files_when_empty() -> None:
    import json

    body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"sandbox_id": str(uuid4())})

    client = HTTPSupervisorRuntime(
        base_url="http://supervisor", transport=httpx.MockTransport(handler)
    )
    await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert "seed_files" not in body  # back-compat: no key when empty
