"""B-267 —— ``knowledge.probe`` is stored only when turned off.

Saving materialises defaults (``model_dump``): without the omit, every agent
with a knowledge block saved after the release would carry ``probe: true`` —
a key an older image does not know — and every such manifest's
``compute_spec_sha256`` would change, breaking the ``run`` ↔ revision join.
"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from typing import Any

from expert_work.protocol import AgentSpec, KnowledgeSpec
from expert_work.protocol.agent_spec import SPEC_DIGEST_CONTEXT

_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "kb-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "x"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
        "knowledge": {"knowledge_base_refs": ["kb"]},
    },
}


def _digest(spec: AgentSpec) -> str:
    payload = spec.model_dump(by_alias=True, mode="json", context={SPEC_DIGEST_CONTEXT: True})
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_the_default_is_not_stored() -> None:
    assert KnowledgeSpec(knowledge_base_refs=["kb"]).model_dump() == {"knowledge_base_refs": ["kb"]}
    stored = AgentSpec.model_validate(_MANIFEST).model_dump(by_alias=True, mode="json")
    assert stored["spec"]["knowledge"] == {"knowledge_base_refs": ["kb"]}


def test_an_opt_out_is_stored() -> None:
    doc = deepcopy(_MANIFEST)
    doc["spec"]["knowledge"]["probe"] = False
    stored = AgentSpec.model_validate(doc).model_dump(by_alias=True, mode="json")
    assert stored["spec"]["knowledge"] == {"knowledge_base_refs": ["kb"], "probe": False}


def test_the_digest_of_an_existing_manifest_is_unchanged() -> None:
    """An explicit ``probe: true`` hashes like a manifest from before B-267."""
    doc = deepcopy(_MANIFEST)
    doc["spec"]["knowledge"]["probe"] = True
    assert _digest(AgentSpec.model_validate(doc)) == _digest(AgentSpec.model_validate(_MANIFEST))


def test_the_json_schema_still_lists_probe() -> None:
    schema = KnowledgeSpec.model_json_schema(mode="serialization")
    assert "probe" in schema["properties"]
