"""2.2 — runtime validation of tool_call args against the tool's JSON Schema."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from orchestrator import ToolContext, ToolResult, ToolSpec
from orchestrator.graph_builder.builder import _invoke_tool, _validate_tool_args

_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["url"],
    "additionalProperties": False,
}


@dataclass
class _SchemaTool:
    parameters: Mapping[str, Any] = field(default_factory=lambda: _SCHEMA)
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="fetch", description="fetch a url", parameters=self.parameters)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(args)
        return ToolResult(content="ok")


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id="u")


# --- _validate_tool_args (pure) --------------------------------------------


def test_valid_args_pass() -> None:
    assert _validate_tool_args(_SchemaTool(), {"url": "https://x", "n": 1}) is None


def test_missing_required_field_rejected() -> None:
    msg = _validate_tool_args(_SchemaTool(), {"n": 1})
    assert msg is not None and "required" in msg


def test_missing_required_field_is_named() -> None:
    """B-84 — a bare ``$ (required)`` leaves the model guessing WHICH key.

    Sixty days of test-environment traffic rejected ``update_plan`` 50 times
    on exactly this message. The property name comes from the schema, not
    from the args, so naming it keeps the never-echo-the-value rule intact.
    """
    msg = _validate_tool_args(_SchemaTool(), {"n": 1})
    assert msg is not None
    assert "missing url" in msg


def test_missing_required_names_every_absent_key() -> None:
    msg = _validate_tool_args(_SchemaTool(), {})
    assert msg is not None and "missing url" in msg


def test_non_required_keyword_keeps_the_bare_path_form() -> None:
    """Only ``required`` gains the suffix — other keywords are unchanged."""
    msg = _validate_tool_args(_SchemaTool(), {"url": 123})
    assert msg is not None and "missing" not in msg


def test_wrong_type_rejected_without_echoing_value() -> None:
    msg = _validate_tool_args(_SchemaTool(), {"url": 123})
    assert msg is not None and "type" in msg
    assert "123" not in msg  # never echo the offending value


def test_no_schema_skips_validation() -> None:
    assert _validate_tool_args(_SchemaTool(parameters={}), {"anything": True}) is None


def test_malformed_schema_does_not_block() -> None:
    # A tool whose own schema is broken must not crash dispatch.
    assert _validate_tool_args(_SchemaTool(parameters={"type": "nonsense"}), {"x": 1}) is None


# --- _invoke_tool wiring ----------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_tool_rejects_bad_args_without_calling() -> None:
    tool = _SchemaTool()
    msg, _state, _refund, classified = await _invoke_tool(tool, {"n": 1}, "tc-1", _ctx())
    assert msg.status == "error"
    assert "[invalid args]" in str(msg.content)
    assert not tool.calls  # the tool was never invoked
    assert classified is not None and classified.error_class == "invalid_arguments"
    assert classified.retryable is False


@pytest.mark.asyncio
async def test_invoke_tool_dispatches_valid_args() -> None:
    tool = _SchemaTool()
    msg, _state, _refund, classified = await _invoke_tool(
        tool, {"url": "https://x"}, "tc-2", _ctx()
    )
    assert msg.status != "error"
    assert tool.calls == [{"url": "https://x"}]
    assert classified is None
