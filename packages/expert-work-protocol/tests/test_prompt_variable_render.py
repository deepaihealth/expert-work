"""B-67 §五 —— ``PromptVariableSpec.render``:收窄开关,默认值不落库。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from expert_work.protocol.agent_spec import PromptVariableSpec, SystemPromptSpec


def test_render_defaults_to_auto_and_is_omitted_from_dumps() -> None:
    var = PromptVariableSpec(name="org_logo")
    assert var.render == "auto"
    assert "render" not in var.model_dump(mode="json")
    assert "render" not in var.model_dump()


def test_render_raw_round_trips() -> None:
    var = PromptVariableSpec(name="org_logo", render="raw")
    dumped = var.model_dump(mode="json")
    assert dumped["render"] == "raw"
    assert PromptVariableSpec.model_validate(dumped).render == "raw"


def test_render_rejects_other_values() -> None:
    with pytest.raises(ValidationError):
        PromptVariableSpec.model_validate({"name": "x", "render": "verbatim"})


def test_existing_manifests_dump_byte_identical() -> None:
    """存库走 ``model_dump(mode="json")``;默认值不物化,存量 manifest 的 sha 不变、回滚
    到旧版本(``extra="forbid"``)也读得进。"""
    spec = SystemPromptSpec(
        template="{{ x }}", jinja=True, variables=[PromptVariableSpec(name="x")]
    )
    assert spec.model_dump(mode="json")["variables"] == [
        {"name": "x", "trusted": True, "required": True, "description": None}
    ]
