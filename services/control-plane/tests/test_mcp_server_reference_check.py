"""Unit tests for the MCP-server manifest reference check (Stream V-C)."""

from __future__ import annotations

from control_plane.api.mcp_servers import (
    manifest_references_server,
    manifest_uses_implicit_all,
)


def _spec(tools: list[dict]) -> dict:
    return {"apiVersion": "v1", "kind": "Agent", "spec": {"tools": tools}}


def test_no_reference_when_no_mcp_tool() -> None:
    spec = _spec([{"type": "builtin", "name": "web_search"}])
    assert manifest_references_server(spec, "github") is False


def test_no_reference_when_servers_field_absent() -> None:
    # Pre-V-E manifests have no `servers` key on the mcp tool → no reference.
    spec = _spec([{"type": "mcp", "allow_tools": []}])
    assert manifest_references_server(spec, "github") is False


def test_reference_when_server_in_servers_list() -> None:
    spec = _spec([{"type": "mcp", "servers": ["github", "linear"], "allow_tools": []}])
    assert manifest_references_server(spec, "github") is True
    assert manifest_references_server(spec, "linear") is True
    assert manifest_references_server(spec, "postgres") is False


def test_no_reference_when_servers_list_empty() -> None:
    # Explicit `servers: []` is the documented "every available server"
    # wildcard (D3) — a dynamic follow, NOT a hard reference.
    spec = _spec([{"type": "mcp", "servers": [], "allow_tools": []}])
    assert manifest_references_server(spec, "github") is False


def test_handles_missing_or_malformed_tools() -> None:
    assert manifest_references_server({}, "github") is False
    assert manifest_references_server({"spec": {}}, "github") is False
    assert manifest_references_server({"spec": {"tools": "nope"}}, "github") is False


# ---------------------------------------------------------------------------
# manifest_uses_implicit_all — D3 "servers empty/absent = follow everything"
# ---------------------------------------------------------------------------


def test_implicit_all_when_servers_list_empty() -> None:
    spec = _spec([{"type": "mcp", "servers": [], "allow_tools": []}])
    assert manifest_uses_implicit_all(spec) is True


def test_implicit_all_when_servers_field_absent() -> None:
    # Pre-V-E manifests have no `servers` key — same wildcard semantics.
    spec = _spec([{"type": "mcp", "allow_tools": []}])
    assert manifest_uses_implicit_all(spec) is True


def test_not_implicit_all_when_servers_explicit() -> None:
    spec = _spec([{"type": "mcp", "servers": ["github"], "allow_tools": []}])
    assert manifest_uses_implicit_all(spec) is False


def test_not_implicit_all_when_no_mcp_tool() -> None:
    assert manifest_uses_implicit_all(_spec([{"type": "builtin", "name": "web_search"}])) is False
    assert manifest_uses_implicit_all(_spec([])) is False


def test_implicit_all_handles_missing_or_malformed_shapes() -> None:
    assert manifest_uses_implicit_all({}) is False
    assert manifest_uses_implicit_all({"spec": {}}) is False
    assert manifest_uses_implicit_all({"spec": {"tools": "nope"}}) is False
