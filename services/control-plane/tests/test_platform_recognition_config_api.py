"""``/v1/platform/recognition-config`` —— B-209 §5.7 平台「文档识别」设置。

同 ``test_platform_judge_config_api.py`` 的搭法:完整 ``create_app``,种一个 system_admin,
JWT 带出主体。平台配了 qwen + glm 两家密钥。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.platform_recognition_config import PlatformRecognitionConfigService
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.platform_recognition_config import (
    InMemoryPlatformRecognitionConfigStore,
    PlatformRecognitionConfigRecord,
)
from expert_work.protocol import Role
from tests.auth_fixtures import make_test_jwt

_URL = "/v1/platform/recognition-config"
_VALID = {
    "ocr_provider": "qwen",
    "ocr_model": "qwen3.7-flash",
    "fallback_provider": "glm",
    "fallback_model": "glm-ocr",
    "figure_provider": "qwen",
    "figure_model": "qwen3.8-flash",
    "max_pages": 300,
}


def _headers(subject: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(subject))}"}


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.fixture
async def admin_client(
    settings: Settings, lifecycle: Lifecycle, jwt_verifier: JWTVerifier
) -> AsyncIterator[tuple[AsyncClient, UUID, Any]]:
    settings = settings.model_copy(
        update={
            "supported_providers": ["qwen", "glm"],
            "platform_provider_credentials": {"qwen": "secret://q", "glm": "secret://g"},
        }
    )
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=admin,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    app.state.invalidation_bus = _SpyBus()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control-plane.test"
    ) as client:
        yield client, admin, app


async def test_non_admin_forbidden(admin_client: tuple[AsyncClient, UUID, Any]) -> None:
    client, _, _ = admin_client
    assert (await client.get(_URL, headers=_headers(uuid4()))).status_code == 403
    assert (await client.put(_URL, headers=_headers(uuid4()), json=_VALID)).status_code == 403


async def test_unset_shows_the_designed_defaults(
    admin_client: tuple[AsyncClient, UUID, Any],
) -> None:
    client, admin, _ = admin_client
    resp = await client.get(_URL, headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["ocr"] == {"provider": "glm", "model": "glm-ocr"}
    assert data["fallback"] == {"provider": "qwen", "model": "qwen3.7-flash"}
    assert data["figure"] == {"provider": "qwen", "model": "qwen3.8-flash"}
    assert data["max_pages"] == 1000
    assert data["is_default"] is True
    ocr = {(o["provider"], o["model"]) for o in data["available_ocr"]}
    assert {("glm", "glm-ocr"), ("qwen", "qwen3.7-flash"), ("qwen", "qwen3.8-flash")} <= ocr
    assert ("qwen", "qwen3.8-max") not in ocr  # a vision model without recognition caps
    figure = {(o["provider"], o["model"]) for o in data["available_figure"]}
    assert ("qwen", "qwen3.8-flash") in figure
    assert ("glm", "glm-ocr") not in figure  # takes no prompt
    assert ("qwen", "text-embedding-v4") not in figure


async def test_put_saves_audits_and_broadcasts(
    admin_client: tuple[AsyncClient, UUID, Any],
) -> None:
    client, admin, app = admin_client
    resp = await client.put(_URL, headers=_headers(admin), json=_VALID)
    assert resp.status_code == 200, resp.text
    saved = resp.json()["data"]
    assert saved["ocr"] == {"provider": "qwen", "model": "qwen3.7-flash"}
    assert saved["max_pages"] == 300
    assert saved["is_default"] is False
    again = (await client.get(_URL, headers=_headers(admin))).json()["data"]
    assert again["fallback"] == {"provider": "glm", "model": "glm-ocr"}
    assert [event.kind for event in app.state.invalidation_bus.events] == ["platform_recognition"]
    rows = list(app.state.audit_logger._store._rows.values())
    assert any(row.action.value == "platform_recognition_config:updated" for row in rows)


async def test_a_cleared_pair_switches_that_model_off(
    admin_client: tuple[AsyncClient, UUID, Any],
) -> None:
    client, admin, app = admin_client
    body = {**_VALID, "fallback_provider": None, "fallback_model": None}
    assert (await client.put(_URL, headers=_headers(admin), json=body)).status_code == 200
    settings = await app.state.platform_recognition_config_service.recognition_settings()
    assert settings.fallback is None
    assert (settings.primary.provider, settings.primary.model) == ("qwen", "qwen3.7-flash")
    assert settings.max_pages == 300


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"ocr_model": None}, "INVALID_RECOGNITION_PAIR"),
        ({"ocr_provider": "qwen", "ocr_model": "qwen3.8-max"}, "INVALID_RECOGNITION_MODEL"),
        ({"fallback_provider": "qwen", "fallback_model": "nope"}, "INVALID_RECOGNITION_MODEL"),
        ({"figure_provider": "glm", "figure_model": "glm-ocr"}, "INVALID_RECOGNITION_MODEL"),
        ({"ocr_provider": "kimi", "ocr_model": "kimi-k3"}, "RECOGNITION_PROVIDER_KEY_MISSING"),
    ],
)
async def test_invalid_selections_are_rejected(
    admin_client: tuple[AsyncClient, UUID, Any], change: dict[str, Any], code: str
) -> None:
    client, admin, _ = admin_client
    resp = await client.put(_URL, headers=_headers(admin), json={**_VALID, **change})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == code


@pytest.mark.parametrize("max_pages", [0, 10_001])
async def test_page_cap_is_bounded(
    admin_client: tuple[AsyncClient, UUID, Any], max_pages: int
) -> None:
    client, admin, _ = admin_client
    resp = await client.put(_URL, headers=_headers(admin), json={**_VALID, "max_pages": max_pages})
    assert resp.status_code == 422


async def test_service_defaults_until_a_row_exists_then_the_row_wins() -> None:
    store = InMemoryPlatformRecognitionConfigStore()
    service = PlatformRecognitionConfigService(store=store, ttl_seconds=0)
    default = await service.recognition_settings()
    assert default.primary is not None and default.primary.model == "glm-ocr"
    assert default.fallback is not None and default.fallback.model == "qwen3.7-flash"
    assert default.max_pages == 1000
    await service.put(
        PlatformRecognitionConfigRecord(
            ocr_provider=None,
            ocr_model=None,
            fallback_provider=None,
            fallback_model=None,
            figure_provider=None,
            figure_model=None,
            max_pages=5,
        )
    )
    off = await service.recognition_settings()
    assert (off.primary, off.fallback, off.max_pages) == (None, None, 5)


async def test_the_layout_parsing_model_is_never_offered_as_a_chat_model(
    admin_client: tuple[AsyncClient, UUID, Any],
) -> None:
    # glm-ocr takes no prompt: not a judge, not an agent's model.
    client, admin, _ = admin_client
    judge = (await client.get("/v1/platform/judge-config", headers=_headers(admin))).json()
    assert {"provider": "glm", "model": "glm-ocr"} not in judge["data"]["available"]
    assert {"provider": "glm", "model": "glm-5.3"} in judge["data"]["available"]
    catalog = (await client.get("/v1/model-catalog", headers=_headers(admin))).json()
    glm = next(row for row in catalog["data"]["providers"] if row["provider"] == "glm")
    names = {model["name"] for model in glm["models"]}
    assert "glm-ocr" not in names
    assert "glm-5.3" in names


def test_only_chat_vision_models_describe_figures() -> None:
    """B-206 K-7c — the figure-describing picker uses ``ModelEntry.chat_model``:
    a vision-flagged embedding / rerank model is no figure describer."""
    from control_plane.api.platform_recognition_config import _describes_figures
    from expert_work.protocol import ModelEntry

    assert _describes_figures(ModelEntry(name="vl", vision=True)) is True
    assert _describes_figures(ModelEntry(name="vl-rerank", vision=True, rerank=True)) is False
    assert _describes_figures(ModelEntry(name="vl-embed", vision=True, embeddings=True)) is False
    assert _describes_figures(ModelEntry(name="text", vision=False)) is False
