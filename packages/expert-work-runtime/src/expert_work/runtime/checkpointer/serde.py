"""Checkpoint serializer with an explicit msgpack allowlist — B-161.

langgraph-checkpoint's ``JsonPlusSerializer`` stores every pydantic model,
dataclass and enum it meets as ``(module, class name, fields)`` and, on load,
imports that module and calls that class. With no allowlist configured it
does so for *any* type and logs "Deserializing unregistered type ... This
will be blocked in a future version" — and once that future default (or
``LANGGRAPH_STRICT_MSGPACK=true``) lands, an unregistered type comes back
as a bare ``dict``: a conversation whose checkpoint holds a plan would load
with ``state["plan"]`` no longer a :class:`Plan`.

So every saver we build gets a serializer that names our types up front.
Passing ``allowed_msgpack_modules`` explicitly puts the serializer in strict
mode: only langgraph's own safe set (messages, datetime, UUID, ``Send`` …)
plus the pairs below are rebuilt. This changes the *load* policy only — the
bytes written are the same as before (the encoder takes no allowlist), so
an older image reads what this one writes and vice versa.

The pairs are strings, not imports: ``ClassifiedToolError`` lives in the
orchestrator, which depends on this package, not the other way round.
``services/orchestrator/tests/test_checkpoint_serde_allowlist.py`` keeps the
list honest — every entry must import, and every type reachable from
``AgentState``'s annotations must be listed.
"""

from __future__ import annotations

from typing import Final

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

#: Every custom type that can sit inside checkpointed graph state, as
#: ``(module, class name)``. Adding a typed ``AgentState`` field whose type
#: (or a nested pydantic / dataclass / enum type) is ours means adding it here.
CHECKPOINT_MSGPACK_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    # AgentState.pending_approval
    ("expert_work.protocol.approval", "ApprovalRequest"),
    # AgentState.recalled_memories
    ("expert_work.protocol.memory_item", "MemoryItem"),
    # AgentState.plan (PlanStep is dumped nested inside Plan, listed so a
    # bare PlanStep anywhere in state also survives)
    ("expert_work.protocol.plan", "Plan"),
    ("expert_work.protocol.plan", "PlanStep"),
    # AgentState.reflections
    ("expert_work.protocol.reflection", "Reflection"),
    # AgentState.subagent_invocations (the StrEnum status is encoded as its
    # own entry inside the model dump)
    ("expert_work.protocol.subagent", "SubAgentInvocation"),
    ("expert_work.protocol.subagent", "SubagentStatus"),
    # AgentState.tool_failures / unresolved_failures
    ("orchestrator.tools.error_classifier", "ClassifiedToolError"),
)


def make_checkpoint_serde() -> JsonPlusSerializer:
    """Return the serializer every checkpointer we construct must use."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_MSGPACK_ALLOWLIST)
