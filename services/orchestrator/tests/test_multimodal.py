"""Tests for the J.6 multimodal content-block helpers + resolver."""

from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

import pytest

from expert_work.protocol.multimodal import ImageRef, parse_image_ref, parse_workspace_image_ref
from expert_work.runtime.storage import InMemoryObjectStore, ObjectNotFoundError
from orchestrator.multimodal import (
    IMAGE_REF_BLOCK_TYPE,
    CachingImageResolver,
    DispatchingImageResolver,
    ImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ObjectStoreImageResolver,
    ResolvedImage,
    image_ref_block,
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


def test_workspace_ref_rejects_another_agents_subtree() -> None:
    # Valid UUIDs + valid extension + no ``..`` — the reserved-tree check is
    # the only thing that can reject this ref.
    t, u = uuid4(), uuid4()
    with pytest.raises(ValueError, match="reserved agents/ tree"):
        parse_workspace_image_ref(f"expert_work://workspace/{t}/{u}/agents/other/x.jpg")


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
