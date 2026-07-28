"""Unit tests for the HX-9 webhook delivery worker (STREAM-HX § 13)."""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from control_plane.webhook_delivery_worker import WebhookDeliveryWorker
from expert_work.persistence import (
    InMemoryApprovalStore,
    InMemoryArtifactStore,
    InMemorySkillStore,
    InMemoryThreadMetaStore,
    InMemoryWebhookDeliveryStore,
    InMemoryWebhookEndpointStore,
)
from expert_work.protocol import (
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookEndpointRecord,
)
from expert_work.runtime.runs import InMemoryRunStore, RunInfo, RunStatus
from expert_work.runtime.runs.schemas import DisconnectMode

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
_SECRET = "topsecret-value"


class _FakeSecretStore:
    """Minimal in-memory SecretStore for the worker test."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    async def get(self, name: str, *, version: str | None = None) -> str:
        if name not in self._d:
            from expert_work.runtime.secret_store import SecretNotFoundError

            raise SecretNotFoundError(name)
        return self._d[name]

    async def put(self, name: str, value: str) -> None:
        self._d[name] = value

    async def list_versions(self, name: str) -> list[str]:
        return ["1"]


class _RecordingPost:
    """A pluggable ``http_post`` that records calls and returns a fixed status
    (or raises, to simulate a transport failure)."""

    def __init__(self, status: int | None = 200, *, raise_exc: bool = False) -> None:
        self.status = status
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.calls.append((url, body, headers))
        if self.raise_exc:
            raise ConnectionError("boom")
        assert self.status is not None
        return self.status


async def _seed(
    *,
    endpoint_enabled: bool = True,
    status: WebhookDeliveryStatus = WebhookDeliveryStatus.PENDING,
    attempt: int = 0,
    payload_format: str = "generic",
    deliveries: InMemoryWebhookDeliveryStore | None = None,
) -> tuple[
    InMemoryWebhookEndpointStore,
    InMemoryWebhookDeliveryStore,
    _FakeSecretStore,
    WebhookEndpointRecord,
    WebhookDeliveryRecord,
]:
    endpoints = InMemoryWebhookEndpointStore()
    deliveries = deliveries if deliveries is not None else InMemoryWebhookDeliveryStore()
    secrets_store = _FakeSecretStore()
    tenant = uuid4()
    endpoint_id = uuid4()
    ref = f"webhook-endpoint/{endpoint_id}"
    await secrets_store.put(ref, _SECRET)
    endpoint = WebhookEndpointRecord(
        id=endpoint_id,
        tenant_id=tenant,
        name="ops",
        url="https://hooks.example.com/ingest",
        event_types=("run.completed",),
        agent_name=None,
        secret_ref=ref,
        enabled=endpoint_enabled,
        source="api",
        payload_format=payload_format,  # type: ignore[arg-type]
        created_at=_NOW,
        updated_at=_NOW,
    )
    await endpoints.create(endpoint)
    delivery = WebhookDeliveryRecord(
        id=uuid4(),
        tenant_id=tenant,
        endpoint_id=endpoint_id,
        event_id="run:abc",
        event_type="run.completed",
        run_id=uuid4(),
        payload={"run_id": "abc", "status": "success"},
        status=status,
        attempt=attempt,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await deliveries.create(delivery)
    return endpoints, deliveries, secrets_store, endpoint, delivery


def _worker(endpoints, deliveries, secrets_store, post, **kwargs) -> WebhookDeliveryWorker:
    # Delivery-only tests pass empty source stores; the enqueue scan over
    # them is a no-op (the deliveries under test are seeded directly).
    return WebhookDeliveryWorker(
        delivery_store=deliveries,
        endpoint_store=endpoints,
        secret_store=secrets_store,
        run_store=InMemoryRunStore(),
        approval_store=InMemoryApprovalStore(),
        artifact_store=InMemoryArtifactStore(),
        thread_meta_store=InMemoryThreadMetaStore(),
        http_post=post,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_2xx_marks_delivered_and_signs_body() -> None:
    endpoints, deliveries, secrets_store, _, delivery = await _seed()
    post = _RecordingPost(status=200)
    result = await _worker(endpoints, deliveries, secrets_store, post).run_once()
    assert result == (1, 0, 0)

    row = await deliveries.get(delivery_id=delivery.id, tenant_id=delivery.tenant_id)
    assert row is not None and row.status is WebhookDeliveryStatus.DELIVERED
    assert row.response_status == 200

    # The signature header is a correct HMAC-SHA256 of the exact body sent.
    url, body, headers = post.calls[0]
    assert url == "https://hooks.example.com/ingest"
    expected = "sha256=" + hmac.new(_SECRET.encode(), body, sha256).hexdigest()
    assert headers["X-Expert-Work-Signature-256"] == expected
    assert headers["X-Expert-Work-Event"] == "run.completed"


@pytest.mark.asyncio
async def test_5xx_schedules_retry_with_backoff() -> None:
    endpoints, deliveries, secrets_store, _, delivery = await _seed()
    post = _RecordingPost(status=503)
    result = await _worker(endpoints, deliveries, secrets_store, post).run_once()
    assert result == (0, 1, 0)

    row = await deliveries.get(delivery_id=delivery.id, tenant_id=delivery.tenant_id)
    assert row is not None
    assert row.status is WebhookDeliveryStatus.RETRYING
    assert row.next_retry_at is not None  # backoff scheduled


@pytest.mark.asyncio
async def test_4xx_dead_letters_without_retry() -> None:
    endpoints, deliveries, secrets_store, _, delivery = await _seed()
    post = _RecordingPost(status=400)
    result = await _worker(endpoints, deliveries, secrets_store, post).run_once()
    assert result == (0, 0, 1)

    row = await deliveries.get(delivery_id=delivery.id, tenant_id=delivery.tenant_id)
    assert row is not None and row.status is WebhookDeliveryStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_last_attempt_dead_letters() -> None:
    # attempt=4 with max_attempts=5 → next_attempt=5 → spent → dead.
    endpoints, deliveries, secrets_store, _, delivery = await _seed(attempt=4)
    post = _RecordingPost(status=503)
    result = await _worker(endpoints, deliveries, secrets_store, post, max_attempts=5).run_once()
    assert result == (0, 0, 1)
    row = await deliveries.get(delivery_id=delivery.id, tenant_id=delivery.tenant_id)
    assert row is not None and row.status is WebhookDeliveryStatus.DEAD_LETTER


@pytest.mark.asyncio
async def test_transport_error_is_retryable() -> None:
    endpoints, deliveries, secrets_store, _, _ = await _seed()
    post = _RecordingPost(raise_exc=True)
    result = await _worker(endpoints, deliveries, secrets_store, post).run_once()
    assert result == (0, 1, 0)


@pytest.mark.asyncio
async def test_disabled_endpoint_dead_letters() -> None:
    endpoints, deliveries, secrets_store, _, _ = await _seed(endpoint_enabled=False)
    post = _RecordingPost(status=200)
    result = await _worker(endpoints, deliveries, secrets_store, post).run_once()
    assert result == (0, 0, 1)
    assert post.calls == []  # never POSTed to a disabled endpoint


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold() -> None:
    endpoints, deliveries, secrets_store, endpoint, _ = await _seed()
    post = _RecordingPost(status=503)
    worker = _worker(
        endpoints, deliveries, secrets_store, post, max_attempts=100, breaker_threshold=3
    )
    # Enqueue several deliveries to the same endpoint; after 3 failures the
    # breaker opens and further deliveries this run are skipped (no POST).
    for i in range(6):
        await deliveries.create(
            WebhookDeliveryRecord(
                id=uuid4(),
                tenant_id=endpoint.tenant_id,
                endpoint_id=endpoint.id,
                event_id=f"run:{i}",
                event_type="run.completed",
                payload={},
                status=WebhookDeliveryStatus.PENDING,
                attempt=0,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    await worker.run_once()
    # Breaker threshold 3 → at most 3 endpoints attempted before it trips;
    # the remaining are skipped, so far fewer than 7 POSTs happen.
    assert len(post.calls) <= 3


class _CallSpyDeliveryStore(InMemoryWebhookDeliveryStore):
    """Wraps the real in-memory store, recording which claim path the worker
    used — the CAS ``claim_ready`` must be used, not the read-only
    ``list_ready`` (W1-PR1, multi-replica readiness: a fleet of worker
    replicas sharing ``list_ready`` would each pick up the same row and
    double-POST it)."""

    def __init__(self) -> None:
        super().__init__()
        self.claim_ready_calls = 0
        self.list_ready_calls = 0
        self.last_claimed_statuses: list[WebhookDeliveryStatus] = []

    async def claim_ready(
        self, *, before: datetime, limit: int = 1000
    ) -> list[WebhookDeliveryRecord]:
        self.claim_ready_calls += 1
        claimed = await super().claim_ready(before=before, limit=limit)
        self.last_claimed_statuses = [d.status for d in claimed]
        return claimed

    async def list_ready(
        self, *, before: datetime, limit: int = 1000
    ) -> list[WebhookDeliveryRecord]:
        self.list_ready_calls += 1
        return await super().list_ready(before=before, limit=limit)


class _WeirdError(BaseException):
    """A raise type outside ``Exception``'s hierarchy — simulates a failure
    that escapes ``_deliver_one``'s own ``except Exception`` handling around
    the POST call (unlike a modeled transport failure, which is caught and
    turned into a normal retry)."""


@pytest.mark.asyncio
async def test_breaker_skip_releases_row_to_retrying_not_stuck_delivering() -> None:
    """Review Critical (deploy-w1-replica-fixes) — the breaker-open skip
    branch of ``_deliver_one`` never calls ``_finish``, so under the CAS
    claim (claim_ready writes DELIVERING before delivery is attempted) a
    skipped row would be stranded DELIVERING until the 300s stale-claim
    window, instead of being retried on the very next 15s sweep like
    before the CAS change. It must be released back to RETRYING (attempt
    untouched) so the next claim_ready can pick it up immediately."""
    endpoints, deliveries, secrets_store, endpoint, first = await _seed()
    post = _RecordingPost(status=503)
    worker = _worker(
        endpoints, deliveries, secrets_store, post, max_attempts=100, breaker_threshold=3
    )
    # 5 more deliveries on the same endpoint (6 total) — the first 3 trip
    # the breaker (3 consecutive 503s), the last 3 are skipped by it.
    rows = [first]
    for i in range(5):
        row = WebhookDeliveryRecord(
            id=uuid4(),
            tenant_id=endpoint.tenant_id,
            endpoint_id=endpoint.id,
            event_id=f"run:{i}",
            event_type="run.completed",
            payload={},
            status=WebhookDeliveryStatus.PENDING,
            attempt=0,
            created_at=_NOW,
            updated_at=_NOW,
        )
        await deliveries.create(row)
        rows.append(row)

    await worker.run_once()
    assert len(post.calls) == 3  # breaker trips after 3 failed attempts

    refreshed = [await deliveries.get(delivery_id=r.id, tenant_id=r.tenant_id) for r in rows]
    assert all(r is not None for r in refreshed)
    attempted, skipped = refreshed[:3], refreshed[3:]

    # The 3 attempted rows failed normally (retry with backoff) — untouched
    # by this fix, included here to pin down the boundary.
    for r in attempted:
        assert r.status is WebhookDeliveryStatus.RETRYING
        assert r.attempt == 1

    # The 3 breaker-skipped rows must be released, not stuck DELIVERING.
    now = datetime.now(UTC)
    for r in skipped:
        assert r.status is WebhookDeliveryStatus.RETRYING
        assert r.status is not WebhookDeliveryStatus.DELIVERING
        assert r.attempt == 0  # a release is not a delivery attempt
        assert r.next_retry_at is not None
        assert r.next_retry_at <= now

    # Second sweep: the released rows are immediately re-claimable (no 15s
    # wait needed) — still skipped by the still-open breaker, but claimed
    # (not orphaned), and no new POSTs happen.
    await worker.run_once()
    assert len(post.calls) == 3  # no growth — the skipped rows never POST

    refreshed_again = [
        await deliveries.get(delivery_id=r.id, tenant_id=r.tenant_id) for r in skipped
    ]
    for r in refreshed_again:
        assert r is not None
        assert r.status is WebhookDeliveryStatus.RETRYING
        assert r.attempt == 0


@pytest.mark.asyncio
async def test_unhandled_exception_releases_row_to_retrying_not_stuck_delivering() -> None:
    """Review Critical (deploy-w1-replica-fixes) — when _deliver_one raises
    past its own exception handling (a bug, not a modeled transport
    failure), run_once's ``gather(..., return_exceptions=True)`` branch
    used to leave the row's status untouched — under the CAS claim that
    means stuck DELIVERING until the 300s stale-claim window. It must be
    released back to RETRYING instead."""
    endpoints, deliveries, secrets_store, _, delivery = await _seed()

    async def _boom(url: str, body: bytes, headers: dict[str, str]) -> int:
        raise _WeirdError("unexpected, not a modeled transport failure")

    result = await _worker(endpoints, deliveries, secrets_store, _boom).run_once()
    assert result == (0, 0, 0)  # not counted delivered/retry/dead this cycle

    row = await deliveries.get(delivery_id=delivery.id, tenant_id=delivery.tenant_id)
    assert row is not None
    assert row.status is WebhookDeliveryStatus.RETRYING
    assert row.status is not WebhookDeliveryStatus.DELIVERING
    assert row.attempt == 0  # a release is not a delivery attempt
    assert row.next_retry_at is not None
    assert row.next_retry_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_run_once_claims_via_cas_not_list_ready() -> None:
    """W1-PR1 — ``run_once`` must claim its batch (CAS) rather than merely
    list it: the row is written ``delivering`` before delivery is attempted,
    and the read-only ``list_ready`` is never called from the sweep."""
    spy = _CallSpyDeliveryStore()
    endpoints, deliveries, secrets_store, _, delivery = await _seed(deliveries=spy)
    post = _RecordingPost(status=200)
    result = await _worker(endpoints, deliveries, secrets_store, post).run_once()
    assert result == (1, 0, 0)

    assert spy.claim_ready_calls == 1
    assert spy.list_ready_calls == 0
    # The row passed through ``delivering`` on its way to the terminal state.
    assert spy.last_claimed_statuses == [WebhookDeliveryStatus.DELIVERING]

    row = await deliveries.get(delivery_id=delivery.id, tenant_id=delivery.tenant_id)
    assert row is not None and row.status is WebhookDeliveryStatus.DELIVERED


# --------------------------------------------------------------- enqueue


async def _enqueue_fixture(*, endpoint_agent: str | None, thread_agent: str | None):
    """Seed a terminal run + its thread + one endpoint; return the worker
    and stores so a test can call ``enqueue_once`` and inspect the queue."""
    endpoints = InMemoryWebhookEndpointStore()
    deliveries = InMemoryWebhookDeliveryStore()
    runs = InMemoryRunStore()
    threads = InMemoryThreadMetaStore()
    secrets_store = _FakeSecretStore()
    await secrets_store.put("webhook-endpoint/x", _SECRET)
    tenant = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    await threads.create(
        thread_id=thread_id,
        tenant_id=tenant,
        created_by="u1",
        agent_name=thread_agent,
    )
    await runs.create(
        RunInfo(
            run_id=run_id,
            tenant_id=tenant,
            thread_id=thread_id,
            user_id=None,
            status=RunStatus.SUCCESS,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=False,
            error=None,
            created_at=_NOW,
            updated_at=_NOW,
            finished_at=_NOW,
        )
    )
    await endpoints.create(
        WebhookEndpointRecord(
            id=uuid4(),
            tenant_id=tenant,
            name="ops",
            url="https://hooks.example.com/x",
            event_types=("run.completed",),
            agent_name=endpoint_agent,
            secret_ref="webhook-endpoint/x",
            enabled=True,
            source="api",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    worker = WebhookDeliveryWorker(
        delivery_store=deliveries,
        endpoint_store=endpoints,
        secret_store=secrets_store,
        run_store=runs,
        approval_store=InMemoryApprovalStore(),
        artifact_store=InMemoryArtifactStore(),
        thread_meta_store=threads,
        http_post=_RecordingPost(status=200),
    )
    return worker, deliveries, tenant, run_id


@pytest.mark.asyncio
async def test_enqueue_run_terminal_fans_out_and_is_idempotent() -> None:
    worker, deliveries, _tenant, run_id = await _enqueue_fixture(
        endpoint_agent=None, thread_agent="reporter"
    )
    assert await worker.enqueue_once() == 1
    rows = [r async for r in _aiter(deliveries)]
    assert len(rows) == 1
    assert rows[0].event_id == f"run:{run_id}"
    assert rows[0].event_type == "run.completed"
    # Re-scanning the same terminal run does not double-enqueue.
    assert await worker.enqueue_once() == 0


@pytest.mark.asyncio
async def test_enqueue_respects_agent_scope() -> None:
    # Endpoint scoped to "other"; the run's thread is agent "reporter" → no match.
    worker, _deliveries, _tenant, _run_id = await _enqueue_fixture(
        endpoint_agent="other", thread_agent="reporter"
    )
    assert await worker.enqueue_once() == 0


@pytest.mark.asyncio
async def test_enqueue_then_deliver_end_to_end() -> None:
    worker, _deliveries, _tenant, _run_id = await _enqueue_fixture(
        endpoint_agent="reporter", thread_agent="reporter"
    )
    assert await worker.enqueue_once() == 1
    delivered, retried, dead = await worker.run_once()
    assert (delivered, retried, dead) == (1, 0, 0)


async def _aiter(store: InMemoryWebhookDeliveryStore):
    """Yield every delivery row in the in-memory store (test helper)."""
    for row in store._rows.values():
        yield row


# ---------------------------------------------------------------------------
# SE-16 PR-8 — skill_promote.requested fan-out
# ---------------------------------------------------------------------------


async def _promote_fixture() -> tuple[WebhookDeliveryWorker, InMemoryWebhookDeliveryStore, UUID]:
    endpoints = InMemoryWebhookEndpointStore()
    deliveries = InMemoryWebhookDeliveryStore()
    secrets_store = _FakeSecretStore()
    tenant = uuid4()
    skills = InMemorySkillStore()
    skill = await skills.create_skill(
        skill_id=uuid4(),
        tenant_id=tenant,
        name="evolved-reporting",
        visibility="agent_private",
        created_by_agent_name="reporter",
    )
    await skills.add_version(
        version_id=uuid4(),
        skill_id=skill.id,
        tenant_id=tenant,
        prompt_fragment="how-to",
        authored_by="agent",
    )
    request = await skills.request_skill_promote(
        request_id=uuid4(),
        tenant_id=tenant,
        skill_id=skill.id,
        skill_version=1,
        requested_by_agent_name="reporter",
        reason="replay passed, needs review",
    )
    await endpoints.create(
        WebhookEndpointRecord(
            id=uuid4(),
            tenant_id=tenant,
            name="ops",
            url="https://hooks.example.com/x",
            event_types=("skill_promote.requested",),
            agent_name=None,
            secret_ref="webhook-endpoint/x",
            enabled=True,
            source="api",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    worker = WebhookDeliveryWorker(
        delivery_store=deliveries,
        endpoint_store=endpoints,
        secret_store=secrets_store,
        run_store=InMemoryRunStore(),
        approval_store=InMemoryApprovalStore(),
        artifact_store=InMemoryArtifactStore(),
        thread_meta_store=InMemoryThreadMetaStore(),
        skill_store=skills,
        http_post=_RecordingPost(status=200),
    )
    return worker, deliveries, request.id


@pytest.mark.asyncio
async def test_enqueue_pending_promote_request_fans_out_and_is_idempotent() -> None:
    worker, deliveries, request_id = await _promote_fixture()
    assert await worker.enqueue_once() == 1
    rows = [r async for r in _aiter(deliveries)]
    assert len(rows) == 1
    assert rows[0].event_type == "skill_promote.requested"
    assert rows[0].event_id == f"skill_promote:{request_id}"
    assert rows[0].payload["skill_version"] == 1
    assert rows[0].payload["requested_by_agent_name"] == "reporter"
    # Re-scanning the same pending request does not double-enqueue.
    assert await worker.enqueue_once() == 0


@pytest.mark.asyncio
async def test_no_skill_store_disables_promote_scan() -> None:
    worker, _deliveries, _request_id = await _promote_fixture()
    worker._skills = None  # legacy assembly shape
    assert await worker.enqueue_once() == 0


@pytest.mark.asyncio
async def test_feishu_endpoint_gets_bot_message() -> None:
    """Channel formats — a feishu endpoint receives the platform's text-bot
    schema (not the expert_work envelope), and the HMAC signs the actual body."""
    import hashlib
    import hmac as hmac_mod

    endpoints, deliveries, secrets_store, _, _ = await _seed(payload_format="feishu")
    post = _RecordingPost(status=200)
    worker = _worker(endpoints, deliveries, secrets_store, post)

    await worker.run_once()

    (_, body, headers) = post.calls[0]
    parsed = json.loads(body)
    assert parsed["msg_type"] == "text"
    text = parsed["content"]["text"]
    assert text.startswith("【Expert Work】运行完成")
    assert "run_id: abc" in text
    expected = "sha256=" + hmac_mod.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert headers["X-Expert-Work-Signature-256"] == expected


@pytest.mark.asyncio
async def test_dingtalk_and_wecom_share_msgtype_shape() -> None:
    for fmt in ("dingtalk", "wecom"):
        endpoints, deliveries, secrets_store, _, _ = await _seed(payload_format=fmt)
        post = _RecordingPost(status=200)
        worker = _worker(endpoints, deliveries, secrets_store, post)

        await worker.run_once()

        parsed = json.loads(post.calls[0][1])
        assert parsed["msgtype"] == "text", fmt
        assert "【Expert Work】" in parsed["text"]["content"], fmt


def test_render_channel_body_unknown_format_falls_back_to_envelope() -> None:
    from control_plane.webhook_delivery_worker import render_channel_body

    row = WebhookDeliveryRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        endpoint_id=uuid4(),
        event_id="run:x",
        event_type="run.completed",
        payload={"a": 1},
        status=WebhookDeliveryStatus.PENDING,
        attempt=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    envelope = b'{"canonical":true}'
    assert render_channel_body("someday-slack", row, envelope) == envelope
    assert render_channel_body("generic", row, envelope) == envelope


def test_im_text_collapses_newlines_in_values() -> None:
    """RT-6 — a multi-line value can't inject fake `key: value` card lines."""
    from control_plane.webhook_delivery_worker import _im_text

    row = WebhookDeliveryRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        endpoint_id=uuid4(),
        event_id="approval:x",
        event_type="approval.requested",
        payload={"action_summary": "delete rows\nbinding: forged-digest"},
        status=WebhookDeliveryStatus.PENDING,
        attempt=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    text = _im_text(row)
    # The injected newline is flattened → no standalone spoofed "binding:" line.
    action_lines = [ln for ln in text.splitlines() if ln.startswith("action_summary:")]
    assert action_lines == ["action_summary: delete rows binding: forged-digest"]
    assert not any(ln.strip().startswith("binding:") for ln in text.splitlines())
