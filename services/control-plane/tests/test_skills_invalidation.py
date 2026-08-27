"""PR-E3b — skill-surface build-cache invalidation wiring (HTTP + spy bus).

Skill content is baked into ``BuiltAgent`` at build time, so every write that
changes what the build-time skill resolver returns must (a) evict the local
built-agent cache and (b) broadcast on the invalidation bus so peer replicas
do the same. These tests drive the real app over HTTP with the runtime + bus
swapped for spies (template: ``test_tenant_config_endpoints.py``) and assert:

* platform-skill writes (``/v1/platform/skills``) → ``invalidate_all()`` +
  exactly one ``platform_skill`` event (tenant-less rows are the all-tenant
  resolver fallback, so the only safe granularity is everything);
* tenant-skill writes (``/v1/skills``) → ``invalidate_tenant(tid)`` + exactly
  one ``agent_build`` event carrying the tenant id;
* promote-approval (``/v1/skill-evolution``) → same tenant-scoped pair
  (approval flips visibility ``agent_private`` → ``tenant``); reject → none;
* batch endpoints invalidate exactly ONCE per batch (no per-item storm);
* no-op paths (idempotent re-import, pinned-only patch) do NOT invalidate.

The cross-replica ``platform_skill`` handler lands with PR-E3b-A; nothing here
depends on it — the spies only observe the publish side.
"""

from __future__ import annotations

import base64
import io
import zipfile
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.api import _skill_github
from control_plane.api._skill_zip import build_skill_zip
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_ADMIN = uuid4()  # UUID subject — promote approve/reject resolves the decider


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _SpyRuntime:
    def __init__(self) -> None:
        self.tenant_calls: list[UUID] = []
        self.all_calls: int = 0

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        self.tenant_calls.append(tenant_id)

    def invalidate_all(self) -> None:
        self.all_calls += 1

    def reset(self) -> None:
        self.tenant_calls.clear()
        self.all_calls = 0


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[InvalidationEvent] = []

    async def publish(self, event: InvalidationEvent) -> None:
        self.events.append(event)

    def reset(self) -> None:
        self.events.clear()


class _Ctx:
    def __init__(
        self, client: AsyncClient, app: FastAPI, runtime: _SpyRuntime, bus: _SpyBus
    ) -> None:
        self.client = client
        self.app = app
        self.runtime = runtime
        self.bus = bus

    def reset(self) -> None:
        self.runtime.reset()
        self.bus.reset()

    def assert_platform_invalidated_once(self) -> None:
        assert self.runtime.all_calls == 1
        assert self.runtime.tenant_calls == []
        assert len(self.bus.events) == 1
        event = self.bus.events[0]
        assert event.kind == "platform_skill"
        assert event.tenant_id is None

    def assert_tenant_invalidated_once(self, tenant_id: UUID) -> None:
        assert self.runtime.tenant_calls == [tenant_id]
        assert self.runtime.all_calls == 0
        assert len(self.bus.events) == 1
        event = self.bus.events[0]
        assert event.kind == "agent_build"
        assert event.tenant_id == str(tenant_id)

    def assert_nothing_invalidated(self) -> None:
        assert self.runtime.tenant_calls == []
        assert self.runtime.all_calls == 0
        assert self.bus.events == []


def _make_app() -> tuple[FastAPI, _SpyRuntime, _SpyBus]:
    app = create_app(
        settings=_settings(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    runtime = _SpyRuntime()
    bus = _SpyBus()
    app.state.agent_runtime = runtime
    app.state.invalidation_bus = bus
    return app, runtime, bus


@pytest.fixture
async def pctx() -> AsyncIterator[_Ctx]:
    """system_admin client over ``/v1/platform/skills`` with spy runtime+bus."""
    app, runtime, bus = _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        client.headers.update(await grant_system_admin(client))
        yield _Ctx(client, app, runtime, bus)


@pytest.fixture
async def tctx() -> AsyncIterator[_Ctx]:
    """Dev-tenant admin client over ``/v1/skills`` with spy runtime+bus."""
    app, runtime, bus = _make_app()
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject=str(_ADMIN))}"}
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=headers
    ) as client:
        yield _Ctx(client, app, runtime, bus)


def _build_zip(*, name: str = "zipped", prompt: str = "be helpful") -> bytes:
    """Legacy-layout ``.skill`` ZIP — mirrors ``test_skills_api._build_zip``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill.yaml", yaml.safe_dump({"name": name, "description": "d"}))
        archive.writestr("prompt.md", prompt)
        archive.writestr("tools.txt", "web_search")
    return buf.getvalue()


def _github_archive(files: dict[str, str]) -> bytes:
    """GitHub-style archive: everything nested under ``<repo>-<ref>/``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(f"skills-HEAD/{path}", content)
    return buf.getvalue()


def _skill_md(name: str) -> str:
    blob = build_skill_zip(
        name=name,
        description=f"{name} skill",
        category=None,
        required_models=(),
        prompt_fragment="be helpful",
        tool_names=(),
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read("SKILL.md").decode()


def _file_body(text: str = "benign reference notes") -> dict[str, object]:
    raw = text.encode()
    return {
        "content": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
        "mime": "text/markdown",
    }


# ---------------------------------------------------------------------------
# Platform skills — every write endpoint drops ALL built agents + broadcasts
# ---------------------------------------------------------------------------


async def _seed_platform_skill(ctx: _Ctx, *, name: str, versions: int = 0) -> str:
    created = await ctx.client.post("/v1/platform/skills", json={"name": name})
    assert created.status_code == 201, created.text
    skill_id: str = created.json()["id"]
    for i in range(versions):
        v = await ctx.client.post(
            f"/v1/platform/skills/{skill_id}/versions",
            json={"prompt_fragment": f"prompt v{i + 1}"},
        )
        assert v.status_code == 201, v.text
    return skill_id


@pytest.mark.asyncio
async def test_create_platform_skill_invalidates(pctx: _Ctx) -> None:
    resp = await pctx.client.post("/v1/platform/skills", json={"name": "foo"})
    assert resp.status_code == 201, resp.text
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_add_platform_version_invalidates(pctx: _Ctx) -> None:
    skill_id = await _seed_platform_skill(pctx, name="foo")
    pctx.reset()
    resp = await pctx.client.post(
        f"/v1/platform/skills/{skill_id}/versions", json={"prompt_fragment": "be helpful"}
    )
    assert resp.status_code == 201, resp.text
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_import_platform_skill_invalidates_once_not_on_idempotent_rerun(
    pctx: _Ctx,
) -> None:
    blob = _build_zip(name="zipped")
    first = await pctx.client.post(
        "/v1/platform/skills/import",
        files={"file": ("zipped.skill", blob, "application/zip")},
    )
    assert first.status_code == 201, first.text
    pctx.assert_platform_invalidated_once()

    # Idempotent re-import (identical content_hash) writes nothing → no evict.
    pctx.reset()
    again = await pctx.client.post(
        "/v1/platform/skills/import",
        files={"file": ("zipped.skill", blob, "application/zip")},
    )
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    pctx.assert_nothing_invalidated()


@pytest.mark.asyncio
async def test_import_from_github_invalidates(
    pctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _github_archive({"skills/find-skills/SKILL.md": _skill_md("find-skills")})

    async def _fake_download(src: object, *, client: object = None) -> bytes:
        return archive

    monkeypatch.setattr(_skill_github, "download_github_archive", _fake_download)
    resp = await pctx.client.post(
        "/v1/platform/skills/import-from-github",
        json={"source": "vercel-labs/skills", "skill": "find-skills"},
    )
    assert resp.status_code == 201, resp.text
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_batch_github_import_invalidates_exactly_once(
    pctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _github_archive(
        {
            "skills/find-skills/SKILL.md": _skill_md("find-skills"),
            "skills/other/SKILL.md": _skill_md("other"),
        }
    )

    async def _fake_download(src: object, *, client: object = None) -> bytes:
        return archive

    monkeypatch.setattr(_skill_github, "download_github_archive", _fake_download)
    resp = await pctx.client.post(
        "/v1/platform/skills/import-from-github/batch",
        json={"source": "vercel-labs/skills", "skills": ["skills/find-skills", "skills/other"]},
    )
    assert resp.status_code == 200, resp.text
    statuses = {r["skill"]: r["status"] for r in resp.json()["results"]}
    assert statuses == {"skills/find-skills": "created", "skills/other": "created"}
    # Two skills ingested — still exactly ONE invalidation for the batch.
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_batch_github_import_all_failed_does_not_invalidate(
    pctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _github_archive({"skills/other/SKILL.md": _skill_md("other")})

    async def _fake_download(src: object, *, client: object = None) -> bytes:
        return archive

    monkeypatch.setattr(_skill_github, "download_github_archive", _fake_download)
    resp = await pctx.client.post(
        "/v1/platform/skills/import-from-github/batch",
        json={"source": "vercel-labs/skills", "skills": ["skills/no-such"]},
    )
    assert resp.status_code == 200, resp.text
    assert [r["status"] for r in resp.json()["results"]] == ["failed"]
    pctx.assert_nothing_invalidated()


@pytest.mark.asyncio
async def test_patch_platform_skill_invalidates(pctx: _Ctx) -> None:
    skill_id = await _seed_platform_skill(pctx, name="foo", versions=1)
    pctx.reset()
    resp = await pctx.client.patch(
        f"/v1/platform/skills/{skill_id}",
        json={"status": "active", "pinned": True, "category": "数据"},
    )
    assert resp.status_code == 200, resp.text
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_batch_update_platform_skills_invalidates_exactly_once(pctx: _Ctx) -> None:
    id_a = await _seed_platform_skill(pctx, name="foo-a", versions=1)
    id_b = await _seed_platform_skill(pctx, name="foo-b", versions=1)
    pctx.reset()
    resp = await pctx.client.post(
        "/v1/platform/skills/batch", json={"ids": [id_a, id_b], "set_status": "active"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    # Two rows patched — still exactly ONE invalidation for the batch.
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_put_platform_supporting_file_invalidates(pctx: _Ctx) -> None:
    skill_id = await _seed_platform_skill(pctx, name="foo", versions=1)
    pctx.reset()
    resp = await pctx.client.put(
        f"/v1/platform/skills/{skill_id}/versions/1/supporting-files/references/notes.md",
        json=_file_body(),
    )
    assert resp.status_code == 201, resp.text
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_delete_platform_supporting_file_invalidates(pctx: _Ctx) -> None:
    skill_id = await _seed_platform_skill(pctx, name="foo", versions=1)
    put = await pctx.client.put(
        f"/v1/platform/skills/{skill_id}/versions/1/supporting-files/references/notes.md",
        json=_file_body(),
    )
    assert put.status_code == 201, put.text
    new_version = put.json()["version"]
    pctx.reset()
    resp = await pctx.client.delete(
        f"/v1/platform/skills/{skill_id}/versions/{new_version}"
        "/supporting-files/references/notes.md"
    )
    assert resp.status_code == 200, resp.text
    pctx.assert_platform_invalidated_once()


@pytest.mark.asyncio
async def test_put_platform_prompt_invalidates(pctx: _Ctx) -> None:
    """The canonical "edited SKILL.md, agents kept the old one" bug."""
    skill_id = await _seed_platform_skill(pctx, name="foo", versions=1)
    pctx.reset()
    resp = await pctx.client.put(
        f"/v1/platform/skills/{skill_id}/versions/1/prompt",
        json={"prompt_fragment": "updated prompt body"},
    )
    assert resp.status_code == 201, resp.text
    pctx.assert_platform_invalidated_once()


# ---------------------------------------------------------------------------
# Tenant skills — every content write drops the tenant's builds + broadcasts
# ---------------------------------------------------------------------------


async def _seed_tenant_skill(ctx: _Ctx, *, name: str, versions: int = 0) -> str:
    created = await ctx.client.post("/v1/skills", json={"name": name})
    assert created.status_code == 201, created.text
    skill_id: str = created.json()["id"]
    for i in range(versions):
        v = await ctx.client.post(
            f"/v1/skills/{skill_id}/versions", json={"prompt_fragment": f"prompt v{i + 1}"}
        )
        assert v.status_code == 201, v.text
    return skill_id


@pytest.mark.asyncio
async def test_create_skill_invalidates(tctx: _Ctx) -> None:
    resp = await tctx.client.post("/v1/skills", json={"name": "foo"})
    assert resp.status_code == 201, resp.text
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_add_version_invalidates(tctx: _Ctx) -> None:
    skill_id = await _seed_tenant_skill(tctx, name="foo")
    tctx.reset()
    resp = await tctx.client.post(
        f"/v1/skills/{skill_id}/versions", json={"prompt_fragment": "be helpful"}
    )
    assert resp.status_code == 201, resp.text
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_put_supporting_file_invalidates(tctx: _Ctx) -> None:
    skill_id = await _seed_tenant_skill(tctx, name="foo", versions=1)
    tctx.reset()
    resp = await tctx.client.put(
        f"/v1/skills/{skill_id}/versions/1/supporting-files/references/notes.md",
        json=_file_body(),
    )
    assert resp.status_code == 201, resp.text
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_delete_supporting_file_invalidates(tctx: _Ctx) -> None:
    skill_id = await _seed_tenant_skill(tctx, name="foo", versions=1)
    put = await tctx.client.put(
        f"/v1/skills/{skill_id}/versions/1/supporting-files/references/notes.md",
        json=_file_body(),
    )
    assert put.status_code == 201, put.text
    new_version = put.json()["version"]
    tctx.reset()
    resp = await tctx.client.delete(
        f"/v1/skills/{skill_id}/versions/{new_version}/supporting-files/references/notes.md"
    )
    assert resp.status_code == 200, resp.text
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_put_prompt_invalidates(tctx: _Ctx) -> None:
    """The canonical "edited SKILL.md, agents kept the old one" bug."""
    skill_id = await _seed_tenant_skill(tctx, name="foo", versions=1)
    tctx.reset()
    resp = await tctx.client.put(
        f"/v1/skills/{skill_id}/versions/1/prompt",
        json={"prompt_fragment": "updated prompt body"},
    )
    assert resp.status_code == 201, resp.text
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_patch_status_invalidates_and_broadcasts(tctx: _Ctx) -> None:
    skill_id = await _seed_tenant_skill(tctx, name="foo", versions=1)
    tctx.reset()
    resp = await tctx.client.patch(f"/v1/skills/{skill_id}", json={"status": "active"})
    assert resp.status_code == 200, resp.text
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_patch_pinned_only_does_not_invalidate(tctx: _Ctx) -> None:
    """``pinned`` is Curator/UI-only (never read by the build resolver) —
    a pinned-only patch must NOT drop the tenant's builds."""
    skill_id = await _seed_tenant_skill(tctx, name="foo")
    tctx.reset()
    resp = await tctx.client.patch(f"/v1/skills/{skill_id}", json={"pinned": True})
    assert resp.status_code == 200, resp.text
    tctx.assert_nothing_invalidated()


@pytest.mark.asyncio
async def test_import_skill_invalidates_once_not_on_idempotent_rerun(tctx: _Ctx) -> None:
    blob = _build_zip(name="zipped")
    first = await tctx.client.post(
        "/v1/skills/import", files={"file": ("zipped.skill", blob, "application/zip")}
    )
    assert first.status_code == 201, first.text
    tctx.assert_tenant_invalidated_once(_TENANT)

    # Idempotent re-import (identical content_hash) writes nothing → no evict.
    tctx.reset()
    again = await tctx.client.post(
        "/v1/skills/import", files={"file": ("zipped.skill", blob, "application/zip")}
    )
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    tctx.assert_nothing_invalidated()


# ---------------------------------------------------------------------------
# Promote approval — visibility flip changes the resolution set
# ---------------------------------------------------------------------------


async def _open_promote_request(ctx: _Ctx, *, name: str) -> str:
    """Seed an agent_private skill (v1) + open a promote request; return rid."""
    store = ctx.app.state.skill_store
    skill_id = uuid4()
    await store.create_skill(
        skill_id=skill_id,
        tenant_id=_TENANT,
        name=name,
        visibility="agent_private",
        created_by_user_id=_ADMIN,
        created_by_agent_name="researcher",
    )
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill_id,
        tenant_id=_TENANT,
        prompt_fragment="do the thing",
        authored_by="agent",
        evolution_origin="in_session",
    )
    r = await ctx.client.post(
        f"/v1/skill-evolution/skills/{skill_id}/promote-requests",
        json={"skill_version": 1, "reason": "tenant-wide useful"},
    )
    assert r.status_code == 201, r.text
    rid: str = r.json()["id"]
    return rid


@pytest.mark.asyncio
async def test_approve_promote_invalidates(tctx: _Ctx) -> None:
    rid = await _open_promote_request(tctx, name="evolved-skill")
    tctx.reset()
    resp = await tctx.client.post(f"/v1/skill-evolution/promote-requests/{rid}/approve", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    tctx.assert_tenant_invalidated_once(_TENANT)


@pytest.mark.asyncio
async def test_reject_promote_does_not_invalidate(tctx: _Ctx) -> None:
    rid = await _open_promote_request(tctx, name="evolved-skill")
    tctx.reset()
    resp = await tctx.client.post(f"/v1/skill-evolution/promote-requests/{rid}/reject", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    tctx.assert_nothing_invalidated()
