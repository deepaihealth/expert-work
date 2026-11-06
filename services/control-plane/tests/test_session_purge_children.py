"""B-280 —— 删除单个对话(``POST /v1/sessions/{id}:purge``)把按对话存的子数据一起删掉。

反馈、质量评分、评测候选样本、记忆写回失败队列、上传登记、图片(连对象存储里的字节)。
每类都种两行:要删的这条对话一行,同一个智能体、同一个用户的另一条对话一行 —— 只有前者
没了。某一类删不掉:对话本身留着、返回 503,重跑把剩下的删完。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import FeedbackRecord
from expert_work.protocol import (
    AuditQuery,
    AuditResult,
    CurationCandidateRecord,
    QualityScoreRecord,
)
from expert_work.runtime.storage import InMemoryObjectStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_USER = UUID("00000000-0000-4000-8000-0000000000c1")
_AGENT = "code-reviewer"
_AGENT_YAML = f"""\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: {_AGENT}
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {{}}
  model:
    provider: anthropic
    name: claude-sonnet-4-6
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: {{ cpu: "1.0", memory: "1Gi" }}
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.object_store = InMemoryObjectStore()
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=headers
    ) as c:
        resp = await c.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        assert resp.status_code in (200, 201), resp.text
        yield c


def _app(client: AsyncClient) -> FastAPI:
    return client._transport.app  # type: ignore[attr-defined,no-any-return]


async def _create(client: AsyncClient) -> UUID:
    resp = await client.post("/v1/sessions", json={"agent_name": _AGENT, "agent_version": "1.0.0"})
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["data"]["thread_id"])


@dataclass(frozen=True)
class _Seeded:
    """What one conversation got: the ids to look each row up by."""

    run_id: UUID
    candidate_id: UUID
    upload_id: UUID
    image_id: UUID
    object_key: str


async def _seed(app: FastAPI, thread_id: UUID) -> _Seeded:
    state = app.state
    run_id, candidate_id, upload_id, image_id = uuid4(), uuid4(), uuid4(), uuid4()
    object_key = f"{_TENANT}/uploads/{thread_id}/{image_id}.png"
    await state.feedback_store.insert(
        FeedbackRecord(tenant_id=_TENANT, thread_id=thread_id, rating="down", actor_id="a")
    )
    await state.quality_score_store.insert(
        QualityScoreRecord(
            tenant_id=_TENANT,
            agent_name=_AGENT,
            agent_version="1.0.0",
            run_id=run_id,
            thread_id=thread_id,
            overall=2,
            dimensions={},
            rationale="r",
            judge_model="m",
        )
    )
    await state.curation_candidate_store.upsert(
        CurationCandidateRecord(
            id=candidate_id,
            tenant_id=_TENANT,
            agent_name=_AGENT,
            thread_id=thread_id,
            user_id=_USER,
            trajectory_key=f"k-{uuid4().hex}",
            outcome="failed",
            signal="failed_outcome",
            detected_at=datetime.now(UTC),
        )
    )
    await state.memory_writeback_dlq.enqueue(
        tenant_id=_TENANT,
        user_id=_USER,
        source_thread_id=str(thread_id),
        extracted=[("fact", "c")],
        error="e",
    )
    await state.user_upload_store.insert(
        upload_id=upload_id,
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=thread_id,
        kind="document",
        ref="uploads/report.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="report.pdf",
    )
    await state.object_store.put(object_key, b"png", content_type="image/png")
    await state.image_upload_store.insert(
        image_id=image_id,
        tenant_id=_TENANT,
        thread_id=thread_id,
        user_id=_USER,
        object_key=object_key,
        size_bytes=3,
        mime_type="image/png",
        sha256="x",
    )
    return _Seeded(run_id, candidate_id, upload_id, image_id, object_key)


async def _remaining(app: FastAPI, thread_id: UUID, seeded: _Seeded) -> dict[str, int]:
    """How many rows of each kind this conversation still has (0 / 1)."""
    s = app.state
    ids = [thread_id]
    blob = 1 if seeded.object_key in await s.object_store.list_prefix(seeded.object_key) else 0
    return {
        "feedback": await s.feedback_store.count_for_threads(tenant_id=_TENANT, thread_ids=ids),
        "quality_scores": int(
            await s.quality_score_store.exists(tenant_id=_TENANT, run_id=seeded.run_id)
        ),
        "curation_candidates": int(
            await s.curation_candidate_store.get(
                candidate_id=seeded.candidate_id, tenant_id=_TENANT
            )
            is not None
        ),
        "memory_writeback_dlq": await s.memory_writeback_dlq.count_for_threads(
            tenant_id=_TENANT, thread_ids=ids
        ),
        "user_uploads": await s.user_upload_store.count_for_threads(
            tenant_id=_TENANT, thread_ids=ids
        ),
        "image_uploads": len(
            await s.image_upload_store.list_for_threads(tenant_id=_TENANT, thread_ids=ids)
        ),
        "image_blob": blob,
    }


_ALL_ONE = {
    "feedback": 1,
    "quality_scores": 1,
    "curation_candidates": 1,
    "memory_writeback_dlq": 1,
    "user_uploads": 1,
    "image_uploads": 1,
    "image_blob": 1,
}
_ALL_ZERO = dict.fromkeys(_ALL_ONE, 0)


@pytest.mark.asyncio
async def test_purge_deletes_each_thread_keyed_kind_and_spares_a_sibling(
    client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    app = _app(client)
    doomed, sibling = await _create(client), await _create(client)
    mine = await _seed(app, doomed)
    theirs = await _seed(app, sibling)

    resp = await client.post(f"/v1/sessions/{doomed}:purge")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    for kind in (
        "feedback",
        "quality_scores",
        "curation_candidates",
        "memory_writeback_dlq",
        "user_uploads",
        "image_uploads",
        "image_blobs_removed",
    ):
        assert data[kind] == 1, kind
    assert data["image_blobs_failed"] == 0

    assert await _remaining(app, doomed, mine) == _ALL_ZERO
    assert await _remaining(app, sibling, theirs) == _ALL_ONE
    assert (await client.get(f"/v1/sessions/{doomed}")).status_code == 404
    assert (await client.get(f"/v1/sessions/{sibling}")).status_code == 200

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    details = next(
        r.details
        for r in page.entries
        if r.resource_id == str(doomed) and r.details.get("op") == "purge"
    )
    assert details["feedback"] == 1
    assert details["image_uploads"] == 1


#: (kind, app.state store attribute, method that fails)
_FAILING = [
    ("feedback", "feedback_store", "delete_for_threads"),
    ("quality_scores", "quality_score_store", "delete_for_threads"),
    ("curation_candidates", "curation_candidate_store", "delete_for_threads"),
    ("memory_writeback_dlq", "memory_writeback_dlq", "delete_for_threads"),
    ("user_uploads", "user_upload_store", "delete_for_threads"),
    ("image_uploads", "image_upload_store", "list_for_threads"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "attr", "method"), _FAILING)
async def test_a_kind_that_cannot_go_keeps_the_conversation_and_a_retry_finishes(
    client: AsyncClient,
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    attr: str,
    method: str,
) -> None:
    """One kind fails: the others still go (each step is independent), but the
    conversation row stays — it is how a retry finds the conversation — and the
    request says so (503). The retry then removes what is left."""
    app = _app(client)
    doomed = await _create(client)
    mine = await _seed(app, doomed)
    store = getattr(app.state, attr)

    async def _boom(**_kw: object) -> int:
        raise ConnectionError("postgres://user:secret@db/x unreachable")

    monkeypatch.setattr(store, method, _boom)
    failed = await client.post(f"/v1/sessions/{doomed}:purge")
    assert failed.status_code == 503, failed.text
    assert "secret" not in failed.text
    assert (await client.get(f"/v1/sessions/{doomed}")).status_code == 200
    monkeypatch.undo()  # the store works again (``_remaining`` reads through it too)
    left = await _remaining(app, doomed, mine)
    expected = dict(_ALL_ZERO)
    expected[kind] = 1
    if kind == "image_uploads":
        expected["image_blob"] = 1
    assert left == expected

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    entry = next(
        r for r in page.entries if r.resource_id == str(doomed) and r.details.get("op") == "purge"
    )
    assert entry.result == AuditResult.ERROR
    assert entry.details[f"{kind}_delete_failed"] is True
    assert entry.details["meta_removed"] is False
    assert "secret" not in str(entry.details)

    retried = await client.post(f"/v1/sessions/{doomed}:purge")
    assert retried.status_code == 200, retried.text
    assert await _remaining(app, doomed, mine) == _ALL_ZERO
    assert (await client.get(f"/v1/sessions/{doomed}")).status_code == 404


@pytest.mark.asyncio
async def test_image_bytes_that_will_not_go_keep_their_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No job looks for image bytes without a row, so a row whose bytes could not
    be deleted stays (with the conversation) until a retry deletes both."""
    app = _app(client)
    doomed = await _create(client)
    mine = await _seed(app, doomed)

    async def _boom(key: str) -> None:
        raise TimeoutError("oss")

    monkeypatch.setattr(app.state.object_store, "delete", _boom)
    failed = await client.post(f"/v1/sessions/{doomed}:purge")
    assert failed.status_code == 503, failed.text
    left = await _remaining(app, doomed, mine)
    assert (left["image_uploads"], left["image_blob"]) == (1, 1)
    assert (await client.get(f"/v1/sessions/{doomed}")).status_code == 200

    monkeypatch.undo()
    retried = await client.post(f"/v1/sessions/{doomed}:purge")
    assert retried.status_code == 200, retried.text
    data = retried.json()["data"]
    assert (data["image_uploads"], data["image_blobs_removed"]) == (1, 1)
    assert await _remaining(app, doomed, mine) == _ALL_ZERO
