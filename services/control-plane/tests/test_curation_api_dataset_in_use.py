"""delete_eval_dataset 把 ``EvalDatasetInUseError`` 映射成 409 — PR3 Task 8.

正常路径(先回退 promoted candidate 再删)不会触发该分支;它是 DB 级兜底
(迁移 0135 的 FK RESTRICT)在写路径绕过 app 闸时的语义化出口。SQL 侧真
FK 行为见 ``packages/expert-work-persistence/tests/test_curation_candidate_fk.py``;
这里用一个 delete 必炸的 in-memory 子类驱动端点的 catch 分支本身。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.curation import (
    EvalDatasetInUseError,
    InMemoryCurationCandidateStore,
    InMemoryEvalDatasetStore,
)
from expert_work.protocol import EvalDatasetRecord
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_BASE = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


class _InUseEvalDatasetStore(InMemoryEvalDatasetStore):
    """Simulates the SQL store hitting the 0135 RESTRICT FK on delete."""

    async def delete(self, *, dataset_id: UUID, tenant_id: UUID) -> bool:
        raise EvalDatasetInUseError(dataset_id=dataset_id)


@pytest.fixture
async def client() -> AsyncIterator[tuple[AsyncClient, _InUseEvalDatasetStore]]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    datasets = _InUseEvalDatasetStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(),
        enable_scheduler=False,
        curation_candidate_repo=InMemoryCurationCandidateStore(),
        eval_dataset_repo=datasets,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as http:
        yield http, datasets


@pytest.mark.asyncio
async def test_delete_eval_dataset_maps_in_use_to_409(
    client: tuple[AsyncClient, _InUseEvalDatasetStore],
) -> None:
    http, datasets = client
    # Seed a real row so the endpoint reaches the delete call regardless of
    # any pre-delete bookkeeping (Task 7's revert runs before the delete).
    record = EvalDatasetRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        agent_name="reporter",
        name="in-use-set",
        input={"prompt": "hi"},
        expected={"answer": "ok"},
        source="golden",
        created_at=_BASE,
        updated_at=_BASE,
    )
    await datasets.create(record)

    resp = await http.delete(f"/v1/eval-datasets/{record.id}")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EVAL_DATASET_IN_USE"
    # The row is untouched — the endpoint surfaced the conflict, nothing else.
    assert await datasets.get(dataset_id=record.id, tenant_id=_TENANT) is not None
