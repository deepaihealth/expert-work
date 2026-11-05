"""B-112 —— 模型名写错要在保存时拦下,而不是等第一个 run 被厂商 404。

厂商回「模型不存在」是 4xx,按设计不切备用,所以备用链保护不了这种配置错误;
能拦它的只有保存 / 发布时的构建闸。这里验:

* 走遍 manifest 里每一个写模型名的字段(含各层 fallback),路径要准;
* 内置 7 家只认目录里的名字,azure / self-hosted 的名字是租户自己的,不查;
* 已下架(``deprecated``)的目录条目照样放行,老 agent 不因下架就建不出来;
* 厂商白名单闸门和名字校验走的是同一个遍历,routing / dynamic_workers 不再漏。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.api.agents import _collect_manifest_providers
from control_plane.api.tenant_config import _providers_referenced_by
from control_plane.manifest_model_names import iter_manifest_models, off_catalog_models_error
from expert_work.protocol import AgentSpec

_GOOD = {"provider": "qwen", "name": "qwen3.6-plus"}
_TYPO = "qwen3.6-pluss"


def _manifest(**spec_extra: Any) -> AgentSpec:
    spec: dict[str, Any] = {
        "tenant_config": {},
        "model": dict(_GOOD),
        "system_prompt": {"template": "hi"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    }
    spec.update(spec_extra)
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "a", "version": "1.0.0", "tenant": "t"},
            "spec": spec,
        }
    )


def _every_field() -> AgentSpec:
    """每个写模型名的字段都填上,而且各带一层 fallback。"""

    def with_fb(name: str) -> dict[str, Any]:
        return {
            "provider": "qwen",
            "name": name,
            "fallback": [{"provider": "glm", "name": "glm-5.3"}],
        }

    return _manifest(
        model={
            "provider": "qwen",
            "name": "qwen3.6-plus",
            "fallback": [
                {
                    "provider": "deepseek",
                    "name": "deepseek-v4-pro",
                    "fallback": [{"provider": "kimi", "name": "kimi-k3"}],
                }
            ],
        },
        routing={"rules": [{"when": "planning", "model": with_fb("qwen3.5-plus")}]},
        dynamic_workers={"model": {"provider": "doubao", "name": "doubao-seed-2-1-pro-260628"}},
        vision={
            "model": with_fb("qwen3-vl-plus"),
            "fallbacks": [{"provider": "glm", "name": "glm-5v-turbo"}],
        },
        policies={"memory_consolidation": {"aux_model": with_fb("qwen3.5-plus")}},
    )


def test_walker_visits_every_model_field_with_exact_paths() -> None:
    paths = [path for path, _ in iter_manifest_models(_every_field())]
    assert paths == [
        "spec.model",
        "spec.model.fallback[0]",
        "spec.model.fallback[0].fallback[0]",
        "spec.routing.rules[0].model",
        "spec.routing.rules[0].model.fallback[0]",
        "spec.dynamic_workers.model",
        "spec.vision.model",
        "spec.vision.model.fallback[0]",
        "spec.vision.fallbacks[0]",
        "spec.policies.memory_consolidation.aux_model",
        "spec.policies.memory_consolidation.aux_model.fallback[0]",
    ]


def test_all_catalog_names_pass() -> None:
    assert off_catalog_models_error(_every_field()) is None


def _typo_at(path: str) -> AgentSpec:
    """在 ``path`` 指的那个字段上写一个错的 qwen 模型名,其余全对。"""
    typo = {"provider": "qwen", "name": _TYPO}
    if path == "spec.model":
        return _manifest(model=typo)
    if path == "spec.model.fallback[0]":
        return _manifest(model={**_GOOD, "fallback": [typo]})
    if path == "spec.routing.rules[0].model":
        return _manifest(routing={"rules": [{"when": "planning", "model": typo}]})
    if path == "spec.routing.rules[0].model.fallback[0]":
        rule_model = {**_GOOD, "fallback": [typo]}
        return _manifest(routing={"rules": [{"when": "planning", "model": rule_model}]})
    if path == "spec.dynamic_workers.model":
        return _manifest(dynamic_workers={"model": typo})
    if path == "spec.vision.model":
        return _manifest(vision={"model": typo})
    if path == "spec.vision.fallbacks[0]":
        return _manifest(
            vision={"model": {"provider": "qwen", "name": "qwen3-vl-plus"}, "fallbacks": [typo]}
        )
    if path == "spec.policies.memory_consolidation.aux_model":
        return _manifest(policies={"memory_consolidation": {"aux_model": typo}})
    raise AssertionError(path)


@pytest.mark.parametrize(
    "path",
    [
        "spec.model",
        "spec.model.fallback[0]",
        "spec.routing.rules[0].model",
        "spec.routing.rules[0].model.fallback[0]",
        "spec.dynamic_workers.model",
        "spec.vision.model",
        "spec.vision.fallbacks[0]",
        "spec.policies.memory_consolidation.aux_model",
    ],
)
def test_off_catalog_name_is_reported_with_its_field_path(path: str) -> None:
    error = off_catalog_models_error(_typo_at(path))
    assert error is not None
    assert f"{path}.name" in error
    assert repr(_TYPO) in error
    assert "qwen" in error


def test_every_bad_name_is_listed_not_just_the_first() -> None:
    spec = _manifest(
        model={"provider": "qwen", "name": _TYPO},
        vision={"model": {"provider": "glm", "name": "glm-5v-turbbo"}},
    )
    error = off_catalog_models_error(spec)
    assert error is not None
    assert "spec.model.name" in error and repr(_TYPO) in error
    assert "spec.vision.model.name" in error and "'glm-5v-turbbo'" in error


def test_a_name_from_another_vendor_is_off_catalog() -> None:
    """目录按厂商分:名字在别家目录里也不算数,请求是发给这一家的。"""
    error = off_catalog_models_error(_manifest(model={"provider": "qwen", "name": "glm-5.3"}))
    assert error is not None and "'glm-5.3'" in error


@pytest.mark.parametrize(
    "model",
    [
        {
            "provider": "self-hosted",
            "name": "my-private-llm",
            "base_url": "http://llm.internal:8000",
        },
        {
            "provider": "azure",
            "name": "whatever-the-tenant-deployed",
            "base_url": "https://r.openai.azure.com",
            "azure_deployment": "prod",
            "azure_api_version": "2024-10-21",
        },
    ],
)
def test_tenant_named_providers_are_not_checked(model: dict[str, Any]) -> None:
    assert off_catalog_models_error(_manifest(model=model)) is None


@pytest.mark.parametrize(
    ("provider", "name"),
    [
        ("qwen", "qwen-max"),
        ("kimi", "kimi-k2.5"),
        ("doubao", "doubao-seed-2.0-pro"),
        ("doubao", "doubao-seed-2.0-lite"),
    ],
)
def test_deprecated_catalog_entries_still_pass(provider: str, name: str) -> None:
    """下架只是不再出现在下拉里;已经用着它的 agent 照样要能保存、能建。"""
    assert off_catalog_models_error(_manifest(model={"provider": provider, "name": name})) is None


def test_provider_whitelist_gate_sees_routing_and_worker_models() -> None:
    spec = _manifest(
        routing={"rules": [{"when": "planning", "model": {"provider": "glm", "name": "glm-5.3"}}]},
        dynamic_workers={"model": {"provider": "deepseek", "name": "deepseek-v4-pro"}},
    )
    assert _collect_manifest_providers(spec) == {"qwen", "glm", "deepseek"}


def test_credentials_panel_usage_sees_routing_and_worker_models() -> None:
    spec = _manifest(
        routing={"rules": [{"when": "planning", "model": {"provider": "glm", "name": "glm-5.3"}}]},
        dynamic_workers={"model": {"provider": "deepseek", "name": "deepseek-v4-pro"}},
    )
    record = SimpleNamespace(spec=spec)
    used = _providers_referenced_by(record, embedding_provider="qwen")  # type: ignore[arg-type]
    assert used == {"qwen", "glm", "deepseek"}
