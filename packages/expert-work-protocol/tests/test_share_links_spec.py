"""B-326 —— ``spec.share_links``:按智能体开启的「产物分享链接」。

不填 = 关(默认),而且不落库:存量配置的内容与指纹不变,回滚到没有这个字段的镜像时
(``extra="forbid"``)不填的配置照样能读。``ttl_minutes`` 限在 5..1440。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from expert_work.protocol import AgentSpec, ShareLinksSpec

_MINIMAL: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "share-demo", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "hi"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _doc(**spec_extra: Any) -> dict[str, Any]:
    doc = deepcopy(_MINIMAL)
    doc["spec"].update(spec_extra)
    return doc


def test_absent_block_means_off_and_is_not_written_out() -> None:
    spec = AgentSpec.model_validate(_doc())
    assert spec.spec.share_links is None
    for dumped in (spec.model_dump(), spec.model_dump(mode="json")):
        assert "share_links" not in dumped["spec"]


def test_block_defaults_off_with_two_hours() -> None:
    block = ShareLinksSpec()
    assert block.enabled is False
    assert block.ttl_minutes == 120


def test_enabled_block_round_trips() -> None:
    spec = AgentSpec.model_validate(_doc(share_links={"enabled": True, "ttl_minutes": 30}))
    assert spec.spec.share_links == ShareLinksSpec(enabled=True, ttl_minutes=30)
    dumped = spec.model_dump(mode="json")
    assert dumped["spec"]["share_links"] == {"enabled": True, "ttl_minutes": 30}
    assert AgentSpec.model_validate(dumped) == spec


@pytest.mark.parametrize("ttl", [5, 1440])
def test_ttl_bounds_inclusive(ttl: int) -> None:
    assert ShareLinksSpec(enabled=True, ttl_minutes=ttl).ttl_minutes == ttl


@pytest.mark.parametrize("ttl", [0, 4, 1441, -1])
def test_ttl_out_of_bounds_rejected(ttl: int) -> None:
    with pytest.raises(ValidationError):
        ShareLinksSpec(enabled=True, ttl_minutes=ttl)


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_doc(share_links={"enabled": True, "url": "x"}))


def test_block_is_in_the_json_schema() -> None:
    schema = AgentSpec.model_json_schema(by_alias=True)
    assert "share_links" in schema["$defs"]["AgentSpecBody"]["properties"]
