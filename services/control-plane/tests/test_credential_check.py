"""B-308 §4.5.3 — which interface and which model the save-time connectivity
check calls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from control_plane import credential_check
from control_plane.credential_check import CheckPlan, NoCheckModelError, plan_check
from control_plane.platform_provider_uses import PlatformProviderUse
from expert_work.protocol import AgentSpec, AgentSpecRecord, AgentSpecStatus, ModelRateCardRecord
from expert_work.protocol.model_catalog import MODEL_CATALOG, models_for_provider
from orchestrator.llm.endpoints import OFFICIAL_BASE_URLS

_NOW = datetime.now(UTC)


def _agent(model: dict[str, Any], name: str = "a") -> AgentSpecRecord:
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": model,
                "system_prompt": {"template": "x"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )
    return AgentSpecRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        name=name,
        version="1.0.0",
        spec=spec,
        spec_sha256="a" * 64,
        status=AgentSpecStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
        created_by="t",
    )


def _price(provider: str, model: str, input_micros: int, output_micros: int) -> ModelRateCardRecord:
    return ModelRateCardRecord(
        id=uuid4(),
        provider=provider,
        model=model,
        input_per_mtok_micros=input_micros,
        output_per_mtok_micros=output_micros,
        cache_creation_per_mtok_micros=0,
        cache_read_per_mtok_micros=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _use(feature: str, model: str, enabled: bool = True) -> PlatformProviderUse:
    return PlatformProviderUse(feature=feature, provider="p", model=model, enabled=enabled)


def _chat_models(provider: str) -> list[str]:
    return [
        entry.name
        for entry in models_for_provider(provider)
        if not entry.deprecated
        and not entry.embeddings
        and not entry.rerank
        and entry.ocr_caps is None
    ]


def _plans(provider: str, **overrides: Any) -> list[CheckPlan]:
    arguments: dict[str, Any] = {
        "specs": [],
        "used_by_agents": 0,
        "platform_uses": [],
        "prices": [],
        **overrides,
    }
    return plan_check(provider, **arguments)


def _plan(provider: str, **overrides: Any) -> CheckPlan:
    [plan] = _plans(provider, **overrides)
    return plan


def test_the_model_agents_use_most_comes_first() -> None:
    first, second = _chat_models("glm")[:2]
    specs = [
        _agent({"provider": "glm", "name": second}, "a"),
        _agent({"provider": "glm", "name": second}, "b"),
        _agent({"provider": "glm", "name": first}, "c"),
        _agent({"provider": "openai", "name": "gpt-4o"}, "d"),
    ]
    plan = _plan(
        "glm",
        specs=specs,
        used_by_agents=3,
        platform_uses=[_use("quality_judge", first)],
        prices=[_price("glm", first, 1, 1)],
    )
    assert plan == CheckPlan(interface="chat", model=second)


def test_fallback_models_count_as_in_use() -> None:
    primary = {
        "provider": "openai",
        "name": "gpt-4o",
        "fallback": [{"provider": "glm", "name": _chat_models("glm")[0]}],
    }
    plan = _plan("glm", specs=[_agent(primary)], used_by_agents=1)
    assert plan == CheckPlan(interface="chat", model=_chat_models("glm")[0])


def test_an_off_catalog_model_in_use_is_taken_as_is() -> None:
    # A deployment endpoint id: the catalog's cheapest model may not exist there.
    specs = [_agent({"provider": "doubao", "name": "ep-20260101-abcde"})]
    plan = _plan("doubao", specs=specs, used_by_agents=1)
    assert plan == CheckPlan(interface="chat", model="ep-20260101-abcde")


def test_then_the_chat_model_a_platform_feature_is_configured_with() -> None:
    model = _chat_models("glm")[1]
    plan = _plan(
        "glm",
        platform_uses=[_use("embedding", "embedding-3"), _use("memory_consolidation", model)],
        prices=[_price("glm", _chat_models("glm")[0], 1, 1)],
    )
    assert plan == CheckPlan(interface="chat", model=model)


def test_a_disabled_platform_feature_does_not_count() -> None:
    cheapest = _chat_models("glm")[2]
    plan = _plan(
        "glm",
        platform_uses=[_use("quality_judge", _chat_models("glm")[1], enabled=False)],
        prices=[_price("glm", cheapest, 1, 1), _price("glm", _chat_models("glm")[0], 5, 5)],
    )
    assert plan == CheckPlan(interface="chat", model=cheapest)


def test_then_the_cheapest_catalog_chat_model_by_input_plus_output_price() -> None:
    models = _chat_models("glm")
    prices = [
        _price("glm", models[0], 9, 1),  # 10
        _price("glm", models[1], 2, 5),  # 7  ← cheapest
        _price("glm", models[2], 1, 9),  # 10
        _price("glm", "embedding-3", 0, 0),  # not a chat model: never chosen
        _price("openai", "gpt-4o", 0, 0),  # another provider's row
    ]
    assert _plan("glm", prices=prices) == CheckPlan(interface="chat", model=models[1])


def test_without_any_price_the_first_catalog_chat_model() -> None:
    assert _plan("glm") == CheckPlan(interface="chat", model=_chat_models("glm")[0])


def test_embedding_rerank_and_recognition_only_models_are_never_the_chat_model() -> None:
    specs = [_agent({"provider": "glm", "name": "glm-ocr"})]
    plan = _plan(
        "glm",
        specs=specs,
        used_by_agents=1,
        platform_uses=[_use("quality_judge", "embedding-3")],
        prices=[_price("glm", "rerank", 0, 0), _price("glm", "glm-ocr", 0, 0)],
    )
    assert plan.interface == "chat"
    assert plan.model in _chat_models("glm")


@pytest.mark.parametrize("provider", sorted(OFFICIAL_BASE_URLS))
def test_every_provider_that_takes_an_address_has_a_model_to_check_with(provider: str) -> None:
    assert _plan(provider).interface == "chat"


def test_embeddings_only_provider_checks_the_embeddings_interface() -> None:
    plan = _plan("qwen", platform_uses=[_use("embedding", "text-embedding-v4")])
    assert plan == CheckPlan(interface="embeddings", model="text-embedding-v4")


def test_rerank_only_provider_checks_the_rerank_interface() -> None:
    plan = _plan("glm", platform_uses=[_use("rerank", "rerank")])
    assert plan == CheckPlan(interface="rerank", model="rerank")


def test_embeddings_and_rerank_only_provider_checks_both_interfaces() -> None:
    plans = _plans(
        "qwen",
        platform_uses=[_use("rerank", "qwen3-vl-rerank"), _use("embedding", "text-embedding-v4")],
    )
    assert plans == [
        CheckPlan(interface="embeddings", model="text-embedding-v4"),
        CheckPlan(interface="rerank", model="qwen3-vl-rerank"),
    ]


def test_a_provider_agents_reference_is_checked_on_chat_even_if_the_platform_embeds_with_it() -> (
    None
):
    plan = _plan(
        "qwen",
        specs=[_agent({"provider": "qwen", "name": _chat_models("qwen")[0]})],
        used_by_agents=1,
        platform_uses=[_use("embedding", "text-embedding-v4")],
    )
    assert plan.interface == "chat"


def test_a_platform_chat_use_keeps_the_check_on_chat() -> None:
    plan = _plan(
        "qwen",
        platform_uses=[
            _use("embedding", "text-embedding-v4"),
            _use("memory_consolidation", _chat_models("qwen")[0]),
        ],
    )
    assert plan == CheckPlan(interface="chat", model=_chat_models("qwen")[0])


def test_a_deprecated_model_is_not_chosen_even_when_it_is_the_cheapest() -> None:
    deprecated = next(e.name for e in MODEL_CATALOG["openai"] if e.deprecated)
    current = _chat_models("openai")[0]
    plan = _plan(
        "openai", prices=[_price("openai", deprecated, 0, 0), _price("openai", current, 9, 9)]
    )
    assert plan == CheckPlan(interface="chat", model=current)


def test_a_provider_without_any_chat_model_raises_a_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write endpoint turns this into a 422 instead of a 500."""
    monkeypatch.setattr(credential_check, "models_for_provider", lambda _provider: [])
    with pytest.raises(NoCheckModelError):
        _plans("openai")
