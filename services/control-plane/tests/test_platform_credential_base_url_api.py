"""``/v1/platform/credentials`` — the request address on a provider credential
(B-308 §4.2 / §4.5 / §7.1 groups 3, 5, 6, 7, 11).

The vendor is a recorder registered as the address-checking client: what it
receives is exactly what the save-time connectivity check sent. DNS is stubbed.
No test here reaches a network.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import httpcore
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from control_plane import credential_check
from control_plane.api import platform_config
from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.credential_check import CheckPlan
from control_plane.settings import Settings
from expert_work.common import url_validation
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.url_validation import PublicEndpointError
from expert_work.protocol import Role
from expert_work.runtime.secret_store import SecretNotFoundError
from orchestrator.llm.providers import _http
from tests.auth_fixtures import make_test_jwt

_URL = "https://proxy.example.com/v1"
_PUBLIC_IP = "93.184.216.34"
_SLOT = "expert-work/platform/llm/openai"
_OTHER_REF = "secret://someone/elses-key"
_OPENAI = "/v1/platform/credentials/providers/openai"
_CHAT_REPLY = {
    "id": "c1",
    "model": "m",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
}


class _Vendor:
    """What the connectivity check talks to."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.respond: Callable[[httpx.Request], Any] = lambda _r: httpx.Response(
            200, json=_CHAT_REPLY
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        result = self.respond(request)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[no-any-return]

    @property
    def urls(self) -> list[str]:
        return [str(call.url) for call in self.calls]


@pytest.fixture
async def vendor() -> AsyncIterator[_Vendor]:
    recorder = _Vendor()
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handle))
    _http.register_checked_client(client)
    try:
        yield recorder
    finally:
        _http.unregister_checked_client(client)
        await client.aclose()


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str] | OSError]:
    """Every name resolves to one public address unless a test says otherwise."""
    answers: dict[str, list[str] | OSError] = {}

    async def _fake(host: str, port: int) -> list[tuple[Any, ...]]:
        answer = answers.get(host, [_PUBLIC_IP])
        if isinstance(answer, OSError):
            raise answer
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
            for address in answer
        ]

    monkeypatch.setattr(url_validation, "_getaddrinfo", _fake)
    return answers


class _App:
    """The app under test with an admin session."""

    def __init__(self, app: Any, client: AsyncClient, admin: UUID) -> None:
        self.app = app
        self.client = client
        #: Slot names written to / deleted from the SecretStore, in order.
        self.puts: list[str] = []
        self.deletes: list[str] = []
        store = app.state.secret_store
        real_put, real_delete = store.put, store.delete

        async def _put(name: str, value: str) -> None:
            self.puts.append(name)
            await real_put(name, value)

        async def _delete(name: str) -> None:
            self.deletes.append(name)
            await real_delete(name)

        store.put, store.delete = _put, _delete
        self._headers = {
            "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(admin))}"
        }

    async def put(self, path: str, **body: Any) -> httpx.Response:
        return await self.client.put(path, json=body, headers=self._headers)

    async def get(self, path: str = "/v1/platform/credentials") -> httpx.Response:
        return await self.client.get(path, headers=self._headers)

    async def key(self, provider: str = "openai", key_id: str = "default") -> dict[str, Any]:
        providers = (await self.get()).json()["data"]["providers"]
        row = next(p for p in providers if p["provider"] == provider)
        found: dict[str, Any] = next(k for k in row["keys"] if k["key_id"] == key_id)
        return found

    async def ref(self, provider: str = "openai", key_id: str = "default") -> str:
        return str((await self.key(provider, key_id))["secret_ref"])

    async def secret_at(self, ref_or_slot: str) -> str | None:
        slot = ref_or_slot.removeprefix("secret://")
        try:
            return await self.app.state.secret_store.get(slot)  # type: ignore[no-any-return]
        except SecretNotFoundError:
            return None

    async def stored_secret(self) -> str | None:
        """The key the ``openai`` row points at right now (``None`` = no row)."""
        providers = (await self.get()).json()["data"]["providers"]
        keys = next(p for p in providers if p["provider"] == "openai")["keys"]
        return await self.secret_at(keys[0]["secret_ref"]) if keys else None

    async def tenant_override(self, tenant: UUID, provider: str = "openai") -> dict[str, Any]:
        data = (await self.get(f"/v1/platform/credentials/tenants/{tenant}")).json()["data"]
        row = next(p for p in data["providers"] if p["provider"] == provider)
        found: dict[str, Any] = row["override"]
        return found

    async def tenant(self) -> UUID:
        tenant_id = uuid4()
        await self.app.state.tenant_config_repo.create(
            tenant_id=tenant_id, display_name="T", actor_id="seed"
        )
        return tenant_id

    def audit(self, action: str, resource_id: str) -> dict[str, Any]:
        rows = self.app.state.audit_logger._store._rows.values()
        entries = [e for e in rows if e.action.value == action and e.resource_id == resource_id]
        assert entries, f"no audit row for {action} / {resource_id}"
        return dict(entries[-1].details)

    def all_audit_text(self) -> str:
        return " ".join(str(e.details) for e in self.app.state.audit_logger._store._rows.values())


@pytest.fixture
async def api(
    settings: Settings, lifecycle: Lifecycle, jwt_verifier: JWTVerifier
) -> AsyncIterator[_App]:
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        yield _App(app, client, admin)


def _ref_of(slot_or_ref: str) -> str:
    return slot_or_ref if slot_or_ref.startswith("secret://") else f"secret://{slot_or_ref}"


def _code(response: httpx.Response) -> str:
    assert response.status_code == 422, response.text
    return str(response.json()["detail"]["code"])


async def _saved_custom(api: _App, url: str = _URL, value: str = "sk-mine") -> None:
    response = await api.put(_OPENAI, value=value, base_url=url)
    assert response.status_code == 200, response.text


# --- 5. not sent = keep, sent empty = clear ----------------------------------------


@pytest.mark.usefixtures("dns")
async def test_write_without_the_field_keeps_the_address(api: _App, vendor: _Vendor) -> None:
    await _saved_custom(api)
    vendor.calls.clear()

    # What the console sends to toggle "enabled": the row's ref, nothing about the address.
    response = await api.put(_OPENAI, secret_ref=await api.ref(), enabled=False, priority=7)

    assert response.status_code == 200, response.text
    key = await api.key()
    assert key["base_url"] == _URL
    assert (key["enabled"], key["priority"]) == (False, 7)
    assert vendor.calls == []  # only enabled / priority changed: no check


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize("empty", [None, "", "   "])
async def test_null_or_empty_clears_the_address(
    api: _App, vendor: _Vendor, empty: str | None
) -> None:
    await _saved_custom(api)
    vendor.calls.clear()

    response = await api.put(_OPENAI, secret_ref=await api.ref(), base_url=empty)

    assert response.status_code == 200, response.text
    assert (await api.key())["base_url"] is None
    assert vendor.calls == []  # back to official: no check, and no key needed
    assert (
        api.audit("platform_credential:provider_upsert", "openai#default")["connectivity_check"]
        == "not_run"
    )


# --- 6. a new address needs a new key -------------------------------------------------

_RELAY_SLOT = "ops/relay-key"
_RELAY_REF = f"secret://{_RELAY_SLOT}"


class _Row:
    """One provider credential under test: the platform ``openai`` key, or a
    tenant's ``openai`` override."""

    def __init__(self, api: _App, path: str, tenant: UUID | None) -> None:
        self.api, self.path, self.tenant = api, path, tenant

    async def read(self) -> dict[str, Any]:
        if self.tenant is None:
            return await self.api.key()
        return await self.api.tenant_override(self.tenant)

    @property
    def audit_key(self) -> tuple[str, str]:
        if self.tenant is None:
            return ("platform_credential:provider_upsert", "openai#default")
        return ("platform_credential:tenant_provider_upsert", "openai")


async def _row(api: _App, scope: str) -> _Row:
    if scope == "platform":
        return _Row(api, _OPENAI, None)
    tenant = await api.tenant()
    return _Row(api, f"/v1/platform/credentials/tenants/{tenant}/providers/openai", tenant)


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize("scope", ["platform", "tenant"])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, _URL),  # official -> custom
        (_URL, "https://other.example.com/v1"),  # another host
        (_URL, "https://proxy.example.com:8443/v1"),  # only the port
        (_URL, "https://proxy.example.com/team-b/v1"),  # only the path
    ],
)
async def test_changed_address_without_a_new_key_is_refused(
    api: _App, vendor: _Vendor, scope: str, before: str | None, after: str
) -> None:
    row = await _row(api, scope)
    saved = await api.put(row.path, value="sk-old", base_url=before)
    assert saved.status_code == 200, saved.text
    stored = await row.read()
    vendor.calls.clear()
    puts = list(api.puts)

    response = await api.put(row.path, secret_ref=stored["secret_ref"], base_url=after)

    assert _code(response) == "BASE_URL_NEEDS_NEW_KEY"
    assert vendor.calls == []  # refused before any request went anywhere
    assert api.puts == puts
    assert await row.read() == stored
    assert await api.secret_at(stored["secret_ref"]) == "sk-old"
    audit = api.audit(*row.audit_key)
    assert (audit["refused"], audit["saved"]) == ("BASE_URL_NEEDS_NEW_KEY", False)


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize("scope", ["platform", "tenant"])
async def test_changed_address_with_a_pasted_key_is_saved(
    api: _App, vendor: _Vendor, scope: str
) -> None:
    row = await _row(api, scope)
    assert (await api.put(row.path, value="sk-old", base_url=_URL)).status_code == 200
    vendor.calls.clear()

    response = await api.put(row.path, value="sk-new", base_url="https://other.example.com/v1")

    assert response.status_code == 200, response.text
    assert [call.headers["authorization"] for call in vendor.calls] == ["Bearer sk-new"]
    stored = await row.read()
    assert stored["base_url"] == "https://other.example.com/v1"
    assert await api.secret_at(stored["secret_ref"]) == "sk-new"


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize("scope", ["platform", "tenant"])
async def test_changed_address_with_another_ref_is_saved(
    api: _App, vendor: _Vendor, scope: str
) -> None:
    row = await _row(api, scope)
    assert (await api.put(row.path, value="sk-old")).status_code == 200
    await api.app.state.secret_store.put(_RELAY_SLOT, "sk-relay")
    vendor.calls.clear()

    response = await api.put(row.path, secret_ref=_RELAY_REF, base_url=_URL)

    assert response.status_code == 200, response.text
    assert [call.headers["authorization"] for call in vendor.calls] == ["Bearer sk-relay"]
    assert ((await row.read())["secret_ref"], (await row.read())["base_url"]) == (_RELAY_REF, _URL)


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize(
    "same",
    [
        _URL,
        "https://PROXY.example.com/v1",
        "https://proxy.example.com./v1",
        "https://proxy.example.com:443/v1/",
    ],
)
async def test_unchanged_address_needs_no_new_key_and_is_checked_again(
    api: _App, vendor: _Vendor, same: str
) -> None:
    await _saved_custom(api, value="sk-stored")
    vendor.calls.clear()

    response = await api.put(_OPENAI, secret_ref=await api.ref(), base_url=same)

    assert response.status_code == 200, response.text
    assert [call.headers["authorization"] for call in vendor.calls] == ["Bearer sk-stored"]
    assert (await api.key())["base_url"] == _URL


@pytest.mark.usefixtures("dns")
async def test_new_rows_bring_their_own_key_by_ref(api: _App, vendor: _Vendor) -> None:
    """A new row is always written with a key — nothing was "forgotten". Pairing
    a vendor key's ref with a relay on purpose is outside what this guards
    (§0.1: admins are trusted)."""
    await api.app.state.secret_store.put(_RELAY_SLOT, "sk-relay")
    tenant = await api.tenant()

    new_key = await api.put(f"{_OPENAI}/keys/relay", secret_ref=_RELAY_REF, base_url=_URL)
    override = await api.put(
        f"/v1/platform/credentials/tenants/{tenant}/providers/openai",
        secret_ref=_RELAY_REF,
        base_url=_URL,
    )

    assert new_key.status_code == 200, new_key.text
    assert override.status_code == 200, override.text
    assert [call.headers["authorization"] for call in vendor.calls] == ["Bearer sk-relay"] * 2


@pytest.mark.usefixtures("dns")
async def test_ref_only_write_at_the_official_address_is_saved(api: _App, vendor: _Vendor) -> None:
    response = await api.put(_OPENAI, secret_ref=_OTHER_REF)
    assert response.status_code == 200, response.text
    assert vendor.calls == []


@pytest.mark.usefixtures("dns")
async def test_another_ref_on_a_custom_address_row_is_checked(api: _App, vendor: _Vendor) -> None:
    await _saved_custom(api)
    await api.app.state.secret_store.put(_RELAY_SLOT, "sk-relay")
    vendor.calls.clear()

    response = await api.put(_OPENAI, secret_ref=_RELAY_REF)  # address not sent: kept

    assert response.status_code == 200, response.text
    assert [call.headers["authorization"] for call in vendor.calls] == ["Bearer sk-relay"]
    assert ((await api.key())["secret_ref"], (await api.key())["base_url"]) == (_RELAY_REF, _URL)


@pytest.mark.usefixtures("dns", "vendor")
@pytest.mark.parametrize("scope", ["platform", "tenant"])
async def test_the_key_is_stored_before_the_row(
    api: _App, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """A new address becomes visible only once its key is in place: a reader
    can never pair the new address with the old key (§4.2 write order)."""
    row = await _row(api, scope)
    assert (await api.put(row.path, value="sk-old")).status_code == 200
    slot = (await row.read())["secret_ref"]
    store = api.app.state.platform_secret_store
    method = "upsert_provider" if row.tenant is None else "upsert_tenant_provider"
    real = getattr(store, method)
    seen: list[str | None] = []

    async def _upsert(**kwargs: Any) -> Any:
        seen.append(await api.secret_at(slot))
        return await real(**kwargs)

    monkeypatch.setattr(store, method, _upsert)

    response = await api.put(row.path, value="sk-new", base_url=_URL)

    assert response.status_code == 200, response.text
    assert seen == ["sk-new"]


# --- 7. the connectivity check --------------------------------------------------------


@pytest.mark.usefixtures("dns")
async def test_passing_check_saves_and_records(
    api: _App, vendor: _Vendor, settings: Settings
) -> None:
    await _saved_custom(api, value="sk-new")

    assert vendor.urls == ["https://proxy.example.com/v1/chat/completions"]
    assert vendor.calls[0].headers["authorization"] == "Bearer sk-new"
    key = await api.key()
    assert (key["base_url"], key["base_url_unverified"]) == (_URL, False)
    assert await api.stored_secret() == "sk-new"
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert audit["connectivity_check"] == "passed"
    assert audit["base_url_host"] == "proxy.example.com"
    # The check's tokens are billed to the platform's own tenant as overhead.
    rows = await api.app.state.token_usage_store.list_for_tenant(
        tenant_id=settings.platform_tenant_id
    )
    assert [
        (r.agent_name, r.usage_kind, r.provider, r.input_tokens, r.output_tokens) for r in rows
    ] == [("credential_check", "platform_overhead", "openai", 11, 2)]


@pytest.mark.usefixtures("dns")
async def test_tenant_override_check_is_billed_to_that_tenant(api: _App, vendor: _Vendor) -> None:
    tenant = await api.tenant()
    response = await api.put(
        f"/v1/platform/credentials/tenants/{tenant}/providers/openai",
        value="sk-tenant",
        base_url=_URL,
    )
    assert response.status_code == 200, response.text
    rows = await api.app.state.token_usage_store.list_for_tenant(tenant_id=tenant)
    assert [(r.agent_name, r.usage_kind) for r in rows] == [
        ("credential_check", "platform_overhead")
    ]
    audit = api.audit("platform_credential:tenant_provider_upsert", "openai")
    assert audit["connectivity_check"] == "passed"


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code, json={"error": {"message": "nope"}})


async def _slow(_request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(5)
    return httpx.Response(200, json=_CHAT_REPLY)


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize(
    ("respond", "reason", "status"),
    [
        (_status(401), "auth_rejected", 401),
        (_status(403), "auth_rejected", 403),
        (_status(500), "vendor_error", 500),
        (_status(502), "vendor_error", 502),
        (_status(404), "vendor_error", 404),
        (_status(429), "vendor_error", 429),
        (lambda _r: httpx.ConnectError("connection refused"), "unreachable", None),
        (_slow, "timeout", None),
    ],
)
async def test_failed_check_refuses_the_save_and_leaves_everything_as_it_was(
    api: _App,
    vendor: _Vendor,
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], Any],
    reason: str,
    status: int | None,
) -> None:
    if respond is _slow:
        # Only the slow vendor needs a short cap; under a loaded machine an
        # ordinary answer can take longer than a fraction of a second.
        monkeypatch.setattr(credential_check, "CHECK_TIMEOUT_S", 0.2)
    assert (await api.put(_OPENAI, value="sk-old")).status_code == 200
    vendor.respond = respond

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL)

    assert _code(response) == "BASE_URL_CHECK_FAILED"
    detail = response.json()["detail"]
    assert (detail["reason"], detail["status"]) == (reason, status)
    # The check used the pasted key — and the stored one is still the old one.
    assert vendor.calls[0].headers["authorization"] == "Bearer sk-new"
    assert await api.stored_secret() == "sk-old"
    key = await api.key()
    assert (key["base_url"], key["base_url_unverified"]) == (None, False)
    assert "sk-new" not in response.text
    assert "/v1/chat" not in response.text
    # The call was made with a real key, so the refused save is on record too.
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert audit["connectivity_check"] == f"failed:{reason}"
    assert audit["connectivity_status"] == status
    assert audit["saved"] is False
    assert audit["base_url_host"] == "proxy.example.com"


@pytest.mark.usefixtures("dns")
async def test_answer_cut_by_the_output_cap_still_passes(api: _App, vendor: _Vendor) -> None:
    truncated = {
        **_CHAT_REPLY,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
        ],
    }
    vendor.respond = lambda _r: httpx.Response(200, json=truncated)
    await _saved_custom(api)
    assert (await api.key())["base_url_unverified"] is False


@pytest.mark.usefixtures("dns")
async def test_save_anyway_stores_the_row_and_marks_it_unverified(
    api: _App, vendor: _Vendor
) -> None:
    vendor.respond = _status(503)

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)

    assert response.status_code == 200, response.text
    key = await api.key()
    assert (key["base_url"], key["base_url_unverified"]) == (_URL, True)
    assert await api.stored_secret() == "sk-new"
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert audit["connectivity_check"] == "forced:vendor_error"
    assert audit["connectivity_status"] == 503


@pytest.mark.usefixtures("dns")
async def test_unverified_mark_follows_the_rules(api: _App, vendor: _Vendor) -> None:
    vendor.respond = _status(503)
    assert (
        await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)
    ).status_code == 200

    # Only enabled / priority: address and mark are written back as they were.
    assert (await api.put(_OPENAI, secret_ref=await api.ref(), enabled=False)).status_code == 200
    key = await api.key()
    assert (key["base_url"], key["base_url_unverified"], key["enabled"]) == (_URL, True, False)

    # The next passing check clears it.
    vendor.respond = lambda _r: httpx.Response(200, json=_CHAT_REPLY)
    assert (await api.put(_OPENAI, secret_ref=await api.ref(), base_url=_URL)).status_code == 200
    assert (await api.key())["base_url_unverified"] is False

    # Forced again, then cleared back to official: no custom address, no mark.
    vendor.respond = _status(503)
    assert (
        await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)
    ).status_code == 200
    assert (await api.key())["base_url_unverified"] is True
    assert (await api.put(_OPENAI, secret_ref=await api.ref(), base_url=None)).status_code == 200
    key = await api.key()
    assert (key["base_url"], key["base_url_unverified"]) == (None, False)


@pytest.mark.usefixtures("dns")
async def test_replacing_only_the_key_on_a_custom_address_row_runs_the_check(
    api: _App, vendor: _Vendor
) -> None:
    await _saved_custom(api, value="sk-first")
    vendor.calls.clear()

    response = await api.put(_OPENAI, value="sk-second")  # no address in the request

    assert response.status_code == 200, response.text
    assert [call.headers["authorization"] for call in vendor.calls] == ["Bearer sk-second"]
    assert (await api.key())["base_url"] == _URL


async def test_address_refused_by_the_connection_layer_cannot_be_forced(
    api: _App, vendor: _Vendor, dns: dict[str, list[str] | OSError]
) -> None:
    """The name passes the save-time lookup and then answers with an internal
    address to the check's own connection: refused, "save anyway" or not."""

    def _refused(_request: httpx.Request) -> Exception:
        blocked = PublicEndpointError("not public", reason="blocked")
        lower = httpcore.ConnectError("model service address is not available")
        lower.__cause__ = blocked
        error = httpx.ConnectError("model service address is not available")
        error.__cause__ = lower
        return error

    vendor.respond = _refused
    assert (await api.put(_OPENAI, value="sk-old")).status_code == 200

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)

    assert _code(response) == "BASE_URL_BLOCKED_HOST"
    assert await api.stored_secret() == "sk-old"
    assert (await api.key())["base_url"] is None


async def test_not_allowed_address_cannot_be_forced_and_sends_nothing(
    api: _App, vendor: _Vendor, dns: dict[str, list[str] | OSError]
) -> None:
    dns["proxy.example.com"] = [_PUBLIC_IP, "10.0.0.5"]

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)

    assert _code(response) == "BASE_URL_BLOCKED_HOST"
    assert vendor.calls == []
    assert await api.stored_secret() is None


@pytest.mark.usefixtures("dns")
async def test_embeddings_only_provider_is_checked_on_the_embeddings_interface(
    api: _App, vendor: _Vendor, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async def _plan(_request: Any, _provider: str) -> list[CheckPlan]:
        return [CheckPlan(interface="embeddings", model="text-embedding-v4")]

    monkeypatch.setattr(platform_config, "_check_plan", _plan)
    vendor.respond = lambda _r: httpx.Response(
        200,
        json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {"prompt_tokens": 3}},
    )

    response = await api.put(
        "/v1/platform/credentials/providers/qwen",
        value="sk-qwen",
        base_url="https://proxy.example.com/compatible-mode/v1",
    )

    assert response.status_code == 200, response.text
    assert vendor.urls == ["https://proxy.example.com/compatible-mode/v1/embeddings"]
    rows = await api.app.state.token_usage_store.list_for_tenant(
        tenant_id=settings.platform_tenant_id
    )
    assert [(r.agent_name, r.usage_kind, r.model, r.input_tokens) for r in rows] == [
        ("credential_check", "platform_overhead", "text-embedding-v4", 3)
    ]


@pytest.mark.usefixtures("dns")
async def test_rerank_only_provider_is_checked_on_the_rerank_interface(
    api: _App, vendor: _Vendor, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _plan(_request: Any, _provider: str) -> list[CheckPlan]:
        return [CheckPlan(interface="rerank", model="rerank")]

    monkeypatch.setattr(platform_config, "_check_plan", _plan)
    vendor.respond = lambda _r: httpx.Response(
        200, json={"results": [{"index": 0, "relevance_score": 0.9}]}
    )

    response = await api.put(
        "/v1/platform/credentials/providers/glm",
        value="sk-glm",
        base_url="https://proxy.example.com/api/paas/v4",
    )

    assert response.status_code == 200, response.text
    assert vendor.urls == ["https://proxy.example.com/api/paas/v4/rerank"]


# --- 3. the address itself (through the API) -------------------------------------------


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize(
    ("provider", "address", "code"),
    [
        ("openai", "http://proxy.example.com/v1", "BASE_URL_INVALID"),
        ("openai", "https://u:p@proxy.example.com/v1", "BASE_URL_INVALID"),
        ("openai", "https://proxy.example.com/v1?a=1", "BASE_URL_INVALID"),
        ("openai", "https://proxy.example.com/v1#x", "BASE_URL_INVALID"),
        ("openai", "https://proxy.example.com/v1/chat/completions", "BASE_URL_FORMAT"),
        ("anthropic", "https://proxy.example.com", "BASE_URL_FORMAT"),
        ("deepseek", "https://proxy.example.com", "BASE_URL_FORMAT"),
        ("qwen", "https://proxy.example.com/v1", "BASE_URL_FORMAT"),
        ("azure", "https://proxy.example.com/v1", "BASE_URL_NOT_SUPPORTED"),
        ("self-hosted", "https://proxy.example.com/v1", "BASE_URL_NOT_SUPPORTED"),
        ("openai", "https://127.0.0.1/v1", "BASE_URL_BLOCKED_HOST"),
        ("openai", "https://100.100.100.200/v1", "BASE_URL_BLOCKED_HOST"),
        ("openai", "https://169.254.169.254/v1", "BASE_URL_BLOCKED_HOST"),
        ("openai", "https://[::ffff:10.0.0.1]/v1", "BASE_URL_BLOCKED_HOST"),
        ("openai", "https://localhost/v1", "BASE_URL_BLOCKED_HOST"),
        ("openai", "https://api/v1", "BASE_URL_BLOCKED_HOST"),
        ("openai", "https://db.svc.cluster.local/v1", "BASE_URL_BLOCKED_HOST"),
    ],
)
async def test_bad_address_is_refused_before_anything_happens(
    api: _App, vendor: _Vendor, provider: str, address: str, code: str
) -> None:
    path = f"/v1/platform/credentials/providers/{provider}"
    # The key is pasted and "save anyway" is on: neither gets a bad address through.
    response = await api.put(path, value="sk-new", base_url=address, force_save=True)

    assert _code(response) == code
    assert vendor.calls == []
    assert api.puts == []  # nothing reached the SecretStore
    assert "sk-new" not in response.text


async def test_unresolvable_name_is_refused(
    api: _App, vendor: _Vendor, dns: dict[str, list[str] | OSError]
) -> None:
    dns["proxy.example.com"] = OSError("no such host")
    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)
    assert _code(response) == "BASE_URL_UNRESOLVABLE"
    assert vendor.calls == []


@pytest.mark.usefixtures("dns")
async def test_format_error_tells_the_provider_specific_form(api: _App) -> None:
    response = await api.put(
        "/v1/platform/credentials/providers/qwen",
        value="k",
        base_url="https://proxy.example.com/v1",
    )
    detail = response.json()["detail"]
    assert detail["expected_suffix"] == "/compatible-mode/v1"
    assert detail["example"] == "https://your-host/compatible-mode/v1"
    assert "proxy.example.com" not in detail["message"]


@pytest.mark.usefixtures("dns", "vendor")
@pytest.mark.parametrize("scope", ["platform", "tenant"])
@pytest.mark.parametrize("address", [_URL, None])
async def test_tool_credentials_refuse_the_field(
    api: _App, scope: str, address: str | None
) -> None:
    path = "/v1/platform/credentials/tools/web_search"
    if scope == "tenant":
        path = f"/v1/platform/credentials/tenants/{await api.tenant()}/tools/web_search"
    refused = await api.put(path, secret_ref="secret://tools/search", base_url=address)
    assert _code(refused) == "BASE_URL_NOT_SUPPORTED"
    # Without the field a tool write works as before.
    assert (await api.put(path, secret_ref="secret://tools/search")).status_code == 200


# --- read view and audit ------------------------------------------------------------------


@pytest.mark.usefixtures("dns", "vendor")
async def test_read_view_carries_the_address_and_the_official_root(api: _App) -> None:
    secret_path = "https://proxy.example.com/tok-9f2c/v1"
    await _saved_custom(api, url=secret_path)

    providers = {p["provider"]: p for p in (await api.get()).json()["data"]["providers"]}
    assert providers["openai"]["keys"][0]["base_url"] == secret_path
    assert providers["openai"]["keys"][0]["base_url_unverified"] is False
    assert providers["openai"]["official_base_url"] == "https://api.openai.com/v1"
    assert providers["qwen"]["official_base_url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    assert providers["azure"]["official_base_url"] is None
    assert providers["self-hosted"]["official_base_url"] is None
    # The audit trail has the host and a fingerprint of the path — never the path.
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert audit["base_url_host"] == "proxy.example.com"
    assert len(audit["base_url_path_fp"]) == 12
    assert "tok-9f2c" not in api.all_audit_text()


# --- 11. an address change reaches built agents ----------------------------------------


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invalidate_all(self) -> None:
        self.calls.append("all")

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        self.calls.append(f"tenant:{tenant_id}")


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish_soon(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.usefixtures("dns", "vendor")
async def test_changing_the_address_invalidates_built_agents_and_broadcasts(
    api: _App,
) -> None:
    await _saved_custom(api)
    runtime, bus = _Runtime(), _Bus()
    api.app.state.agent_runtime = runtime
    api.app.state.invalidation_bus = bus

    response = await api.put(
        _OPENAI, value="sk-team-b", base_url="https://proxy.example.com/team-b/v1"
    )

    assert response.status_code == 200, response.text
    assert runtime.calls == ["all"]
    assert [(event.kind, event.tenant_id) for event in bus.events] == [("platform_secrets", None)]


# --- C. the tenant override path: checks before the SecretStore --------------------------------


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize("scope", ["platform", "tenant"])
async def test_refused_save_never_touches_the_secret_store(
    api: _App, vendor: _Vendor, scope: str
) -> None:
    path = _OPENAI
    if scope == "tenant":
        path = f"/v1/platform/credentials/tenants/{await api.tenant()}/providers/openai"
    assert (await api.put(path, value="sk-old")).status_code == 200
    puts = list(api.puts)
    vendor.respond = _status(401)

    failed = await api.put(path, value="sk-new", base_url=_URL)
    current = (await api.key())["secret_ref"] if scope == "platform" else puts[-1]
    needs_key = await api.put(path, secret_ref=_ref_of(current), base_url=_URL)

    assert _code(failed) == "BASE_URL_CHECK_FAILED"
    assert _code(needs_key) == "BASE_URL_NEEDS_NEW_KEY"
    assert api.puts == puts  # neither refusal wrote a key
    assert api.deletes == []
    assert await api.secret_at(puts[-1]) == "sk-old"


# --- refusals at step 1 are on record ------------------------------------------------------------


@pytest.mark.usefixtures("vendor")
async def test_refusals_before_the_check_are_audited_with_host_and_code(
    api: _App, dns: dict[str, list[str] | OSError]
) -> None:
    assert (await api.put(_OPENAI, value="sk-mine")).status_code == 200

    needs_key = await api.put(
        _OPENAI, secret_ref=await api.ref(), base_url="https://proxy.example.com/tok-1/v1"
    )
    assert _code(needs_key) == "BASE_URL_NEEDS_NEW_KEY"
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert (audit["refused"], audit["saved"]) == ("BASE_URL_NEEDS_NEW_KEY", False)
    assert audit["base_url_host"] == "proxy.example.com"

    dns["inside.example.com"] = ["10.0.0.5"]
    blocked = await api.put(
        _OPENAI, value="sk-mine", base_url="https://inside.example.com/tok-2/v1"
    )
    assert _code(blocked) == "BASE_URL_BLOCKED_HOST"
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert (audit["refused"], audit["saved"]) == ("BASE_URL_BLOCKED_HOST", False)
    assert audit["base_url_host"] == "inside.example.com"
    assert "tok-" not in api.all_audit_text()


@pytest.mark.usefixtures("dns")
async def test_connection_layer_refusal_is_audited(api: _App, vendor: _Vendor) -> None:
    def _refused(_request: httpx.Request) -> Exception:
        error = httpx.ConnectError("model service address is not available")
        error.__cause__ = PublicEndpointError("not public", reason="blocked")
        return error

    vendor.respond = _refused
    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)

    assert _code(response) == "BASE_URL_BLOCKED_HOST"
    audit = api.audit("platform_credential:provider_upsert", "openai#default")
    assert (audit["refused"], audit["saved"]) == ("BASE_URL_BLOCKED_HOST", False)
    assert api.puts == []


# --- answers that are not an answer ---------------------------------------------------


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize(
    "respond",
    [
        lambda _r: httpx.Response(302, headers={"location": "http://169.254.169.254/"}),
        lambda _r: httpx.Response(200, text="<html>a login page</html>"),
        lambda _r: httpx.Response(200, json={"weird": 1}),
        lambda _r: httpx.Response(200, json=[1, 2, 3]),
        lambda _r: ValueError("something unexpected inside the client"),
    ],
)
async def test_odd_vendor_answer_fails_the_check_instead_of_passing_or_crashing(
    api: _App, vendor: _Vendor, respond: Callable[[httpx.Request], Any]
) -> None:
    vendor.respond = respond

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL)

    assert _code(response) == "BASE_URL_CHECK_FAILED"
    assert response.json()["detail"]["reason"] == "vendor_error"
    assert api.puts == []
    assert len(vendor.calls) == 1  # the redirect was not followed


@pytest.mark.usefixtures("dns")
async def test_address_the_http_client_cannot_use_is_invalid(api: _App, vendor: _Vendor) -> None:
    vendor.respond = lambda _r: httpx.InvalidURL("Invalid non-printable ASCII character in URL")

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)

    assert _code(response) == "BASE_URL_INVALID"
    assert api.puts == []


@pytest.mark.usefixtures("dns")
@pytest.mark.parametrize(
    ("provider", "address", "plan", "body"),
    [
        (
            "qwen",
            "https://proxy.example.com/compatible-mode/v1",
            CheckPlan(interface="embeddings", model="text-embedding-v4"),
            {"data": []},
        ),
        (
            "qwen",
            "https://proxy.example.com/compatible-mode/v1",
            CheckPlan(interface="rerank", model="qwen3-vl-rerank"),
            {"output": {}},
        ),
        (
            "glm",
            "https://proxy.example.com/api/paas/v4",
            CheckPlan(interface="rerank", model="rerank"),
            {"results": []},
        ),
    ],
)
async def test_empty_embeddings_or_rerank_answer_fails_the_check(
    api: _App,
    vendor: _Vendor,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    address: str,
    plan: CheckPlan,
    body: dict[str, Any],
) -> None:
    async def _plan(_request: Any, _provider: str) -> list[CheckPlan]:
        return [plan]

    monkeypatch.setattr(platform_config, "_check_plan", _plan)
    vendor.respond = lambda _r: httpx.Response(200, json=body)

    response = await api.put(
        f"/v1/platform/credentials/providers/{provider}", value="sk-x", base_url=address
    )

    assert _code(response) == "BASE_URL_CHECK_FAILED"
    assert response.json()["detail"]["reason"] == "vendor_error"


@pytest.mark.usefixtures("dns")
async def test_provider_used_for_embeddings_and_rerank_is_checked_on_both(
    api: _App, vendor: _Vendor, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The rerank address is derived differently from the embeddings one, so a
    passing embeddings call says nothing about it."""

    async def _plan(_request: Any, _provider: str) -> list[CheckPlan]:
        return [
            CheckPlan(interface="embeddings", model="text-embedding-v4"),
            CheckPlan(interface="rerank", model="qwen3-vl-rerank"),
        ]

    def _answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {"prompt_tokens": 3}},
            )
        results = [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]
        return httpx.Response(
            200, json={"output": {"results": results}, "usage": {"total_tokens": 7}}
        )

    monkeypatch.setattr(platform_config, "_check_plan", _plan)
    vendor.respond = _answer

    response = await api.put(
        "/v1/platform/credentials/providers/qwen",
        value="sk-qwen",
        base_url="https://proxy.example.com/x/compatible-mode/v1",
    )

    assert response.status_code == 200, response.text
    assert vendor.urls == [
        "https://proxy.example.com/x/compatible-mode/v1/embeddings",
        "https://proxy.example.com/x/api/v1/services/rerank/text-rerank/text-rerank",
    ]
    rows = await api.app.state.token_usage_store.list_for_tenant(
        tenant_id=settings.platform_tenant_id
    )
    assert sorted((r.model, r.input_tokens, r.agent_name) for r in rows) == [
        ("qwen3-vl-rerank", 7, "credential_check"),
        ("text-embedding-v4", 3, "credential_check"),
    ]

    # The second interface failing fails the save, whatever the first said.
    vendor.calls.clear()
    vendor.respond = lambda r: (
        _answer(r) if r.url.path.endswith("/embeddings") else httpx.Response(404, json={})
    )
    failed = await api.put(
        "/v1/platform/credentials/providers/qwen",
        value="sk-qwen",
        base_url="https://proxy.example.com/y/compatible-mode/v1",
    )
    assert _code(failed) == "BASE_URL_CHECK_FAILED"
    assert failed.json()["detail"]["status"] == 404
    assert len(vendor.calls) == 2


# --- the official root typed in ---------------------------------------------------------


@pytest.mark.usefixtures("dns")
async def test_official_root_as_the_address_is_stored_as_no_address(
    api: _App, vendor: _Vendor
) -> None:
    assert (await api.put(_OPENAI, value="sk-mine")).status_code == 200

    response = await api.put(
        _OPENAI, secret_ref=await api.ref(), base_url="https://API.openai.com/v1/"
    )

    assert response.status_code == 200, response.text
    assert (await api.key())["base_url"] is None
    assert vendor.calls == []


def test_the_check_is_capped_at_fifteen_seconds() -> None:
    # Decision 8: at most 15 seconds. A longer cap holds the save (and the row) that long.
    assert credential_check.CHECK_TIMEOUT_S == 15.0


# --- which interface failed ----------------------------------------------------------------


@pytest.mark.usefixtures("dns")
async def test_failed_check_says_which_interface_did_not_answer(
    api: _App, vendor: _Vendor, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _plan(_request: Any, _provider: str) -> list[CheckPlan]:
        return [
            CheckPlan(interface="embeddings", model="text-embedding-v4"),
            CheckPlan(interface="rerank", model="qwen3-vl-rerank"),
        ]

    monkeypatch.setattr(platform_config, "_check_plan", _plan)
    vendor.respond = lambda r: (
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
        if r.url.path.endswith("/embeddings")
        else httpx.Response(404, json={})
    )
    path = "/v1/platform/credentials/providers/qwen"
    address = "https://proxy.example.com/x/compatible-mode/v1"

    failed = await api.put(path, value="sk-qwen", base_url=address)

    assert _code(failed) == "BASE_URL_CHECK_FAILED"
    assert failed.json()["detail"]["interface"] == "rerank"
    audit = api.audit("platform_credential:provider_upsert", "qwen#default")
    assert audit["connectivity_interface"] == "rerank"

    forced = await api.put(path, value="sk-qwen", base_url=address, force_save=True)
    assert forced.status_code == 200, forced.text
    audit = api.audit("platform_credential:provider_upsert", "qwen#default")
    assert (audit["connectivity_check"], audit["connectivity_interface"]) == (
        "forced:vendor_error",
        "rerank",
    )

    vendor.respond = lambda _r: httpx.Response(401, json={})
    first_fails = await api.put(path, value="sk-qwen", base_url=address)
    assert first_fails.json()["detail"]["interface"] == "embedding"


@pytest.mark.usefixtures("dns")
async def test_failed_chat_check_names_the_chat_interface(api: _App, vendor: _Vendor) -> None:
    vendor.respond = _status(500)
    response = await api.put(_OPENAI, value="sk-new", base_url=_URL)
    assert response.json()["detail"]["interface"] == "chat"


# --- hand errors that used to be a 500 ----------------------------------------------------


@pytest.mark.usefixtures("dns")
async def test_a_ref_that_does_not_exist_is_a_clear_refusal(api: _App, vendor: _Vendor) -> None:
    response = await api.put(_OPENAI, secret_ref="secret://ops/no-such-key", base_url=_URL)

    assert _code(response) == "SECRET_NOT_FOUND"
    assert vendor.calls == []
    assert "no-such-key" not in response.json()["detail"]["message"]


@pytest.mark.usefixtures("dns")
async def test_a_provider_with_no_model_to_check_is_a_clear_refusal(
    api: _App, vendor: _Vendor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credential_check, "models_for_provider", lambda _provider: [])

    response = await api.put(_OPENAI, value="sk-new", base_url=_URL, force_save=True)

    assert _code(response) == "BASE_URL_CHECK_UNAVAILABLE"
    assert vendor.calls == []
    assert api.puts == []
