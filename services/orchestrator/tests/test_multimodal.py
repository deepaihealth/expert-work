"""Tests for the J.6 multimodal content-block helpers + resolver."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from expert_work.protocol.multimodal import ImageRef, parse_image_ref, parse_workspace_image_ref
from expert_work.runtime.storage import InMemoryObjectStore, ObjectNotFoundError
from orchestrator.multimodal import (
    _MAX_WORKSPACE_IMAGE_BYTES,
    IMAGE_REF_BLOCK_TYPE,
    CachingImageResolver,
    DispatchingImageResolver,
    ImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ObjectStoreImageResolver,
    ResolvedImage,
    image_ref_block,
    is_cacheable_image_ref,
    split_human_content,
)

_DATA = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def test_image_ref_block_shape() -> None:
    block = image_ref_block("expert_work://image/abc")
    assert block == {"type": IMAGE_REF_BLOCK_TYPE, "ref": "expert_work://image/abc"}


def test_split_human_content_plain_string() -> None:
    assert split_human_content("just text") == ("just text", [])


def test_split_human_content_text_blocks_only() -> None:
    content = [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]
    assert split_human_content(content) == ("hello world", [])


def test_split_human_content_collects_image_refs() -> None:
    content = [
        {"type": "text", "text": "look:"},
        image_ref_block("expert_work://image/one"),
        image_ref_block("expert_work://image/two"),
    ]
    text, refs = split_human_content(content)
    assert text == "look:"
    assert refs == ["expert_work://image/one", "expert_work://image/two"]


def test_split_human_content_accepts_bare_string_blocks() -> None:
    assert split_human_content(["a", "b"]) == ("ab", [])


def test_split_human_content_ignores_unknown_block_kinds() -> None:
    content = [123, {"type": "text", "text": "x"}, {"type": "mystery"}]
    assert split_human_content(content) == ("x", [])


def test_resolved_image_base64_round_trips() -> None:
    img = ResolvedImage(media_type="image/png", data=_DATA)
    assert base64.b64decode(img.base64_data) == _DATA


def test_resolved_image_data_uri() -> None:
    img = ResolvedImage(media_type="image/jpeg", data=_DATA)
    assert img.data_uri == f"data:image/jpeg;base64,{img.base64_data}"


@pytest.mark.asyncio
async def test_in_memory_resolver_resolves_known_ref() -> None:
    img = ResolvedImage(media_type="image/webp", data=_DATA)
    resolver = InMemoryImageResolver(images={"expert_work://image/x": img})
    assert await resolver.resolve("expert_work://image/x") is img


@pytest.mark.asyncio
async def test_in_memory_resolver_raises_on_missing_ref() -> None:
    resolver = InMemoryImageResolver()
    with pytest.raises(KeyError, match="no image for ref"):
        await resolver.resolve("expert_work://image/missing")


def test_in_memory_resolver_satisfies_protocol() -> None:
    assert isinstance(InMemoryImageResolver(), ImageResolver)


# ---------------------------------------------------------------------------
# ObjectStoreImageResolver (Stream J.6 / PR4)
# ---------------------------------------------------------------------------


def _image_ref(ext: str = ".png") -> ImageRef:
    return ImageRef(tenant_id=uuid4(), thread_id=uuid4(), image_id=uuid4(), ext=ext)


@pytest.mark.asyncio
async def test_object_store_resolver_resolves_image() -> None:
    ref = _image_ref(".png")
    store = InMemoryObjectStore()
    await store.put(ref.storage_key, _DATA, content_type="image/png")

    resolved = await ObjectStoreImageResolver(store=store).resolve(ref.to_uri())

    assert resolved.media_type == "image/png"
    assert resolved.data == _DATA


@pytest.mark.asyncio
async def test_object_store_resolver_derives_media_type_from_extension() -> None:
    ref = _image_ref(".jpg")
    store = InMemoryObjectStore()
    await store.put(ref.storage_key, _DATA)

    resolved = await ObjectStoreImageResolver(store=store).resolve(ref.to_uri())

    assert resolved.media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_object_store_resolver_missing_object_raises() -> None:
    resolver = ObjectStoreImageResolver(store=InMemoryObjectStore())
    with pytest.raises(ObjectNotFoundError):
        await resolver.resolve(_image_ref().to_uri())


@pytest.mark.asyncio
async def test_object_store_resolver_rejects_unsupported_extension() -> None:
    resolver = ObjectStoreImageResolver(store=InMemoryObjectStore())
    with pytest.raises(ValueError, match="unsupported image extension"):
        await resolver.resolve(_image_ref(".bmp").to_uri())


@pytest.mark.asyncio
async def test_object_store_resolver_rejects_malformed_ref() -> None:
    resolver = ObjectStoreImageResolver(store=InMemoryObjectStore())
    with pytest.raises(ValueError, match="image ref"):
        await resolver.resolve("not-a-expert-work-image-ref")


def test_object_store_resolver_satisfies_protocol() -> None:
    assert isinstance(ObjectStoreImageResolver(store=InMemoryObjectStore()), ImageResolver)


# ---------------------------------------------------------------------------
# NasWorkspaceImageResolver (B-64) — symlink safety + size cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nas_resolver_resolves_a_real_file(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    (user_dir / "page-01.jpg").write_bytes(b"\xff\xd8\xff\xe0real-bytes")

    resolved = await NasWorkspaceImageResolver(root=tmp_path).resolve(
        f"expert_work://workspace/{tenant}/{user}/page-01.jpg"
    )

    assert resolved.data == b"\xff\xd8\xff\xe0real-bytes"
    assert resolved.media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_nas_resolver_refuses_a_leaf_symlink_escaping_the_user_root(tmp_path: Path) -> None:
    """Critical finding 1 —— a malicious run plants a symlink inside its OWN
    subtree pointing at another tenant's file, then calls ``ask_image`` with
    its own (legitimate) tenant/user. The ref string itself is a clean
    relative path — ``parse_workspace_image_ref`` cannot see the escape; only
    touching the filesystem can.
    """
    tenant, user = uuid4(), uuid4()
    victim_tenant, victim_user = uuid4(), uuid4()
    victim_dir = tmp_path / str(victim_tenant) / str(victim_user)
    victim_dir.mkdir(parents=True)
    (victim_dir / "secret.png").write_bytes(b"victim-bytes")

    attacker_dir = tmp_path / str(tenant) / str(user)
    attacker_dir.mkdir(parents=True)
    (attacker_dir / "evil.png").symlink_to(
        Path("..") / ".." / str(victim_tenant) / str(victim_user) / "secret.png"
    )

    resolver = NasWorkspaceImageResolver(root=tmp_path)
    ref = f"expert_work://workspace/{tenant}/{user}/evil.png"

    with pytest.raises(ValueError, match="escapes the user root"):
        await resolver.resolve(ref)


@pytest.mark.asyncio
async def test_nas_resolver_refuses_an_intermediate_symlink_escaping_the_user_root(
    tmp_path: Path,
) -> None:
    """Same escape, but the symlink is a directory one path segment up from
    the leaf — exercises ``_walk_to_parent_fd`` (the dir_fd chain), not the
    leaf-open branch. A resolve()-then-reopen-by-string approach would have
    missed exactly this: only the *final* component was re-checked, not the
    intermediate ones.
    """
    tenant, user = uuid4(), uuid4()
    victim_tenant, victim_user = uuid4(), uuid4()
    victim_dir = tmp_path / str(victim_tenant) / str(victim_user)
    victim_dir.mkdir(parents=True)
    (victim_dir / "secret.png").write_bytes(b"victim-bytes")

    attacker_dir = tmp_path / str(tenant) / str(user)
    attacker_dir.mkdir(parents=True)
    (attacker_dir / "figures").symlink_to(victim_dir)

    resolver = NasWorkspaceImageResolver(root=tmp_path)
    ref = f"expert_work://workspace/{tenant}/{user}/figures/secret.png"

    with pytest.raises(ValueError, match="escapes the user root"):
        await resolver.resolve(ref)


@pytest.mark.asyncio
async def test_nas_resolver_refuses_a_file_over_the_size_cap(tmp_path: Path) -> None:
    """Important finding 3 —— stat happens before read, so an outsized file
    is refused instead of being loaded fully into the control-plane
    process's memory. ``os.truncate`` makes a sparse file: the reported size
    crosses the cap without actually writing that many bytes to disk.
    """
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    big = user_dir / "too-big.jpg"
    big.touch()
    os.truncate(big, _MAX_WORKSPACE_IMAGE_BYTES + 1)

    resolver = NasWorkspaceImageResolver(root=tmp_path)
    ref = f"expert_work://workspace/{tenant}/{user}/too-big.jpg"

    with pytest.raises(ValueError, match="exceeds the"):
        await resolver.resolve(ref)


@pytest.mark.asyncio
async def test_nas_resolver_bounds_the_read_even_when_fstat_undercounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Important finding 1 —— the fstat cap alone isn't enough: it's asked
    once, and the sandbox writes this tree directly while an agent controls
    its own files, so a writer growing the file *after* that fstat is a
    reachable race, not a hypothetical one. Simulated deterministically by
    making ``os.fstat`` under-report the size of a file that is genuinely
    over the cap on disk (a sparse file, so no real 64 MiB write) — proving
    the real backstop is the bounded ``handle.read(cap + 1)`` + length
    recheck, not the fstat pre-check.
    """
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    big = user_dir / "grew-after-stat.jpg"
    big.touch()
    os.truncate(big, _MAX_WORKSPACE_IMAGE_BYTES + 10)  # sparse -- genuinely over cap

    real_fstat = os.fstat

    def _fstat_that_undercounts(fd: int) -> SimpleNamespace:
        real = real_fstat(fd)
        return SimpleNamespace(st_mode=real.st_mode, st_size=1)  # lies: "tiny file"

    monkeypatch.setattr("os.fstat", _fstat_that_undercounts)

    resolver = NasWorkspaceImageResolver(root=tmp_path)
    ref = f"expert_work://workspace/{tenant}/{user}/grew-after-stat.jpg"

    with pytest.raises(ValueError, match="exceeds the"):
        await resolver.resolve(ref)


@pytest.mark.timeout(5)
@pytest.mark.asyncio
async def test_nas_resolver_refuses_a_fifo_without_hanging(tmp_path: Path) -> None:
    """Important finding 2 —— ``O_NOFOLLOW`` rejects symlinks, not FIFOs. An
    agent naming a FIFO ``page.jpg`` in its own workspace would otherwise
    have ``open``/``read`` block forever waiting for a writer that never
    comes, pinning a worker in the shared ``asyncio.to_thread`` pool.

    Why this test doesn't hang even if the fix regresses: the leaf open
    carries ``O_NONBLOCK`` (a no-op for a regular file, but it makes opening
    a writer-less FIFO return immediately instead of blocking), so the
    refusal is reachable without ever calling a blocking ``read()``. The
    ``@pytest.mark.timeout(5)`` is the second, independent safety net — if
    a future change dropped ``O_NONBLOCK``, this test would fail on a bounded
    timeout instead of hanging the whole suite.
    """
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    os.mkfifo(user_dir / "page.jpg")

    resolver = NasWorkspaceImageResolver(root=tmp_path)
    ref = f"expert_work://workspace/{tenant}/{user}/page.jpg"

    with pytest.raises(ValueError, match="not a regular file"):
        await resolver.resolve(ref)


# ---------------------------------------------------------------------------
# is_cacheable_image_ref (B-64) — which refs CachingImageResolver may keep
# ---------------------------------------------------------------------------


def test_is_cacheable_image_ref_true_for_upload_refs() -> None:
    assert is_cacheable_image_ref(_image_ref().to_uri()) is True


def test_is_cacheable_image_ref_true_under_write_once_prefix() -> None:
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/.tool_results/r1/figures/abc/page-03.jpg"
    assert is_cacheable_image_ref(ref) is True


def test_is_cacheable_image_ref_false_outside_write_once_prefix() -> None:
    """An ordinary, overwritable workspace file named through this scheme
    must never be cached — see the function's own docstring for why."""
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/chart.png"
    assert is_cacheable_image_ref(ref) is False


def test_is_cacheable_image_ref_true_under_write_once_prefix_for_an_agent_scoped_ref() -> None:
    """B-64 回修 C1 —— agent-bound run 的 ref 形如
    ``agents/<key>/.tool_results/...``,判据必须落在 ``agents/<key>/`` 之后,
    不然每一条绑了 agent 的渲染页都会被判成不可缓存(见函数自己的 C1 段落)。
    """
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/agents/pf-probe-33086dc0/.tool_results/r1/page.jpg"
    assert is_cacheable_image_ref(ref) is True


def test_is_cacheable_image_ref_false_outside_write_once_prefix_for_an_agent_scoped_ref() -> None:
    """同上,但落点仍在 ``agents/<key>/`` 下面、却不在 ``.tool_results/`` 里
    ——普通可覆写文件,agent 作用域不改变这条规则。"""
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/agents/pf-probe-33086dc0/chart.png"
    assert is_cacheable_image_ref(ref) is False


class _CountingResolver:
    """Inner resolver that counts fetches — to prove the cache short-circuits."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, ref: str) -> ResolvedImage:
        self.calls += 1
        return ResolvedImage(media_type="image/png", data=ref.encode())


def test_caching_resolver_satisfies_protocol() -> None:
    assert isinstance(CachingImageResolver(_CountingResolver()), ImageResolver)


@pytest.mark.asyncio
async def test_caching_resolver_memoizes_same_ref() -> None:
    inner = _CountingResolver()
    resolver = CachingImageResolver(inner)
    first = await resolver.resolve("expert_work://image/a.png")
    second = await resolver.resolve("expert_work://image/a.png")
    assert first is second  # cache hit returns the same resolved object
    assert inner.calls == 1  # the inner store was hit exactly once


@pytest.mark.asyncio
async def test_caching_resolver_lru_evicts_oldest() -> None:
    inner = _CountingResolver()
    resolver = CachingImageResolver(inner, max_size=2)
    await resolver.resolve("a")
    await resolver.resolve("b")
    await resolver.resolve("c")  # cache full (max 2) → evicts the oldest, "a"
    assert inner.calls == 3
    await resolver.resolve("a")  # "a" was evicted → re-fetched
    assert inner.calls == 4


@pytest.mark.asyncio
async def test_caching_resolver_never_stores_a_ref_the_predicate_rejects() -> None:
    inner = _CountingResolver()
    resolver = CachingImageResolver(inner, should_cache=lambda ref: False)
    await resolver.resolve("a")
    await resolver.resolve("a")
    assert inner.calls == 2  # never cached -> refetched every call


@pytest.mark.asyncio
async def test_caching_resolver_still_caches_a_ref_the_predicate_accepts() -> None:
    inner = _CountingResolver()
    resolver = CachingImageResolver(inner, should_cache=lambda ref: True)
    await resolver.resolve("a")
    await resolver.resolve("a")
    assert inner.calls == 1


# ---------------------------------------------------------------------------
# DispatchingImageResolver (B-64) — one resolver instance, two ref schemes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatching_resolver_routes_upload_ref_to_uploads_backend() -> None:
    ref = _image_ref(".png").to_uri()
    uploads = InMemoryImageResolver(images={ref: ResolvedImage(media_type="image/png", data=_DATA)})
    resolver = DispatchingImageResolver(uploads=uploads, workspace=InMemoryImageResolver({}))

    resolved = await resolver.resolve(ref)

    assert resolved.data == _DATA


@pytest.mark.asyncio
async def test_dispatching_resolver_routes_workspace_ref_to_workspace_backend(
    tmp_path: Path,
) -> None:
    tenant, user = uuid4(), uuid4()
    page_dir = tmp_path / str(tenant) / str(user) / "r1"
    page_dir.mkdir(parents=True)
    (page_dir / "page-01.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    ref = f"expert_work://workspace/{tenant}/{user}/r1/page-01.jpg"
    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver({}), workspace=NasWorkspaceImageResolver(root=tmp_path)
    )

    resolved = await resolver.resolve(ref)

    assert resolved.data == b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.mark.asyncio
async def test_dispatching_resolver_rejects_unknown_scheme() -> None:
    resolver = DispatchingImageResolver(uploads=InMemoryImageResolver({}))
    with pytest.raises(ValueError, match="unrecognized image ref scheme"):
        await resolver.resolve("s3://not-a-scheme-we-know/x.png")


@pytest.mark.asyncio
async def test_dispatching_resolver_rejects_workspace_ref_when_unconfigured() -> None:
    """``workspace=None``(默认)—— 这个部署没接 NAS。"""
    tenant, user = uuid4(), uuid4()
    resolver = DispatchingImageResolver(uploads=InMemoryImageResolver({}))
    ref = f"expert_work://workspace/{tenant}/{user}/r1/page-01.jpg"
    with pytest.raises(ValueError, match="not available"):
        await resolver.resolve(ref)


def test_dispatching_resolver_satisfies_protocol() -> None:
    assert isinstance(DispatchingImageResolver(uploads=InMemoryImageResolver({})), ImageResolver)


# ---------------------------------------------------------------------------
# parse_workspace_image_ref (B-64) — sibling of parse_image_ref, not a branch
# ---------------------------------------------------------------------------


def test_workspace_ref_rejects_parent_traversal() -> None:
    # Extension is valid (``.jpg``) and the first segment is not a reserved
    # tree, so the ONLY thing standing between this ref and a clean parse is
    # the ``..`` check — a mutation that deletes that check turns this ValueError
    # into a successful parse, not just a differently-worded error.
    t, u = uuid4(), uuid4()
    with pytest.raises(ValueError, match="free of '\\.\\.'"):
        parse_workspace_image_ref(
            f"expert_work://workspace/{t}/{u}/.tool_results/r1/figures/../../../etc/evil.jpg"
        )


def test_workspace_ref_accepts_an_agent_scoped_path() -> None:
    """B-64 回修 C1 —— ``agents/<key>/...`` 现在是合法形状(绑了 agent 的 run
    在用户根上的真实落点)。「这个 key 是不是调用方自己的」不在解析这层判 ——
    见 ``test_multimodal.py`` (orchestrator 侧) 里 ``vision.AskImageTool`` 的
    跨 agent 拒绝测试。"""
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/agents/pf-probe-33086dc0/.tool_results/r1/page.jpg"
    parsed = parse_workspace_image_ref(ref)
    assert parsed.agent_key == "pf-probe-33086dc0"
    assert parsed.rel == "agents/pf-probe-33086dc0/.tool_results/r1/page.jpg"


def test_workspace_ref_rejects_bare_agents_segment() -> None:
    # "agents" alone (nothing after it) can't name any real subtree.
    t, u = uuid4(), uuid4()
    with pytest.raises(ValueError, match="must be followed by an agent key"):
        parse_workspace_image_ref(f"expert_work://workspace/{t}/{u}/agents")


def test_workspace_ref_rejects_an_unsafe_agent_key() -> None:
    # "@" is outside require_safe_key's [A-Za-z0-9._-]+ charset.
    t, u = uuid4(), uuid4()
    with pytest.raises(ValueError, match="unsafe agent key"):
        parse_workspace_image_ref(f"expert_work://workspace/{t}/{u}/agents/weird@key/x.jpg")


def test_workspace_ref_still_rejects_shared_subtree() -> None:
    # shared/ stays reserved — it's a read-only bind, never a render target.
    t, u = uuid4(), uuid4()
    with pytest.raises(ValueError, match="reserved shared/ tree"):
        parse_workspace_image_ref(f"expert_work://workspace/{t}/{u}/shared/x.jpg")


def test_workspace_ref_without_agents_prefix_has_no_agent_key() -> None:
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/.tool_results/r1/page.jpg"
    assert parse_workspace_image_ref(ref).agent_key is None


def test_workspace_ref_roundtrips() -> None:
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/.tool_results/r1/figures/abc/page-03.jpg"
    parsed = parse_workspace_image_ref(ref)
    assert parsed.tenant_id == t
    assert parsed.user_id == u
    assert parsed.rel.endswith("page-03.jpg")


def test_workspace_ref_rejects_unsupported_extension() -> None:
    t, u = uuid4(), uuid4()
    with pytest.raises(ValueError, match="unsupported image extension"):
        parse_workspace_image_ref(f"expert_work://workspace/{t}/{u}/.tool_results/r1/page.pdf")


def test_workspace_ref_rejects_malformed_tenant_id() -> None:
    u = uuid4()
    with pytest.raises(ValueError, match="malformed tenant/user id"):
        parse_workspace_image_ref(f"expert_work://workspace/not-a-uuid/{u}/r1/page.jpg")


def test_parse_image_ref_still_refuses_workspace_scheme() -> None:
    """老边界一个字没松 —— 这是「加兄弟不加分支」的钉子。"""
    with pytest.raises(ValueError, match="must start with"):
        parse_image_ref("expert_work://workspace/t/u/x.jpg")
