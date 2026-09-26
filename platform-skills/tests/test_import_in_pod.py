"""Tests for `platform-skills/import_in_pod.py` (Task 8 + fix rounds 1-2).

Exercises `run_import` — the whole create / version-bump / no-op / dry-run /
partial-batch-failure decision logic — against in-memory stores
(`InMemorySkillStore`, `InMemoryAuditLogStore`) via the platform's own
`_ingest_platform_skill_payload` pipeline. No database, no cluster.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType

import pytest

from control_plane.api._skill_zip import build_skill_zip
from control_plane.audit import TenantConfigPiiResolver, build_default_audit_logger
from expert_work.persistence import InMemoryAuditLogStore, InMemorySkillStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var

PLATFORM_SKILLS = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, PLATFORM_SKILLS / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec — `ImportDeps` is a `@dataclass` under
    # `from __future__ import annotations`; dataclasses resolves its
    # annotations via ``sys.modules[cls.__module__]``, which is unset (and
    # crashes) until the module is registered.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


build = _load("ps_build_for_import_in_pod_tests", "build.py")
import_in_pod = _load("ps_import_in_pod", "import_in_pod.py")


class _RecordingObjectStore:
    """Records every `put` — used to prove dry-run makes none (finding 1)
    and, separately, to give a real import somewhere to put bytes so its
    resulting `content_hash` can be compared against the dry-run preview's."""

    def __init__(self) -> None:
        self.puts: list[str] = []
        self._data: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self.puts.append(key)
        self._data[key] = data

    async def get(self, key: str) -> bytes:
        try:
            return self._data[key]
        except KeyError:
            raise import_in_pod.SkillAssetUnavailableError(key) from None


def _counting_publish() -> tuple[Callable[[], Awaitable[bool]], dict[str, int]]:
    calls = {"n": 0}

    async def publish() -> bool:
        calls["n"] += 1
        return True

    return publish, calls


def _make_deps(
    publish: Callable[[], Awaitable[bool]], *, object_store: object | None = None
) -> import_in_pod.ImportDeps:
    return import_in_pod.ImportDeps(
        store=InMemorySkillStore(),
        audit=build_default_audit_logger(
            store=InMemoryAuditLogStore(), pii_fields_resolver=TenantConfigPiiResolver()
        ),
        object_store=object_store,
        publish=publish,
    )


def _bytes_map(built_packages: dict[str, Path]) -> dict[str, bytes]:
    return {name: path.read_bytes() for name, path in built_packages.items()}


def _rebuild_docx_with_body_tweak(tmp_path: Path) -> bytes:
    """Copy the real docx skill + its shared deps, append a body sentence,
    rebuild it alone — same source ``build.py`` uses, so the resulting
    ``.skill`` is byte-for-byte what a real re-release would produce."""
    root = tmp_path / "ps"
    shutil.copytree(PLATFORM_SKILLS / "shared", root / "shared")
    shutil.copytree(PLATFORM_SKILLS / "docx", root / "docx")
    skill_md = root / "docx" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8")
        + "\n\n本段落仅用于 import_in_pod 测试, 验证正文变化会改变 content_hash.\n",
        encoding="utf-8",
    )
    [pkg] = build.build_all(root, tmp_path / "out", only={"docx"})
    return pkg.read_bytes()


def _second_synthetic_package(tmp_path: Path) -> bytes:
    """A second, independent skill (`docx2`, a renamed copy of the real
    docx skill). Needed so "publish once per BATCH, not per package" is
    actually exercised (fix round 1, finding 6): with only one real skill
    in this worktree, per-package-publish and per-batch-publish give the
    same count and a mutation from one to the other survives the suite."""
    root = tmp_path / "ps2"
    shutil.copytree(PLATFORM_SKILLS / "shared", root / "shared")
    shutil.copytree(PLATFORM_SKILLS / "docx", root / "docx2")
    skill_md = root / "docx2" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace("name: docx", "name: docx2", 1),
        encoding="utf-8",
    )
    [pkg] = build.build_all(root, tmp_path / "out2", only={"docx2"})
    return pkg.read_bytes()


def _bad_name_package() -> bytes:
    """A structurally valid `.skill` whose declared name fails
    `_ingest_platform_skill_payload`'s own format gate (`platform_skills.py`
    :363) — used for the finding 2/3 partial-failure and parity tests."""
    return build_skill_zip(
        name="Bad Name",
        description="仅用于测试的非法技能名",
        category=None,
        required_models=(),
        prompt_fragment="测试正文, 仅用于触发名称校验失败.",
        tool_names=(),
    )


async def test_first_import_creates_every_package_and_publishes_once(
    built_packages: dict[str, Path], tmp_path: Path
) -> None:
    packages = _bytes_map(built_packages)
    packages["docx2"] = _second_synthetic_package(tmp_path)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)

    results, published = await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    assert len(results) == len(packages)
    for result in results:
        assert result["status"] == 201
        assert result["created"] is True
        assert result["version"] == 1
        assert result["file"] in packages
        assert result["name"] == result["file"]  # frontmatter name matches this fixture's file stem
        assert isinstance(result["runtime"], dict)
        assert isinstance(result["content_hash"], str)
        assert result["content_hash"]
    assert calls["n"] == 1  # one publish per BATCH, not per package (finding 6)
    assert published is True


async def test_reimport_unchanged_is_idempotent_and_does_not_republish(
    built_packages: dict[str, Path],
) -> None:
    packages = _bytes_map(built_packages)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)
    await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    results, published = await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    assert calls["n"] == 1  # unchanged since the first import above — no new publish
    assert published is None  # nothing created this call — no publish attempted
    for result in results:
        assert result["status"] == 200
        assert result["created"] is False


async def test_changed_package_gets_a_new_version_others_stay_unchanged(
    built_packages: dict[str, Path], tmp_path: Path
) -> None:
    packages = _bytes_map(built_packages)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)
    await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    modified = dict(packages)
    modified["docx"] = _rebuild_docx_with_body_tweak(tmp_path)

    results, published = await import_in_pod.run_import(modified, dry_run=False, deps=deps)

    by_file = {r["file"]: r for r in results}
    assert by_file["docx"]["status"] == 201
    assert by_file["docx"]["created"] is True
    assert by_file["docx"]["version"] == 2
    for file_name, result in by_file.items():
        if file_name != "docx":
            assert result["status"] == 200
            assert result["created"] is False
    assert calls["n"] == 2  # first import's publish + one more for the docx version bump
    assert published is True


async def test_dry_run_writes_nothing_and_does_not_publish(
    built_packages: dict[str, Path],
) -> None:
    packages = _bytes_map(built_packages)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)

    results, published = await import_in_pod.run_import(packages, dry_run=True, deps=deps)

    assert calls["n"] == 0
    assert published is None
    for result in results:
        assert result["status"] == "dry-run"
        assert result["created"] is False
        assert result["would_create_version"] == 1  # fresh store — nothing imported yet
        assert isinstance(result["runtime"], dict)
        assert isinstance(result["content_hash"], str)
        assert result["content_hash"]

    async with import_in_pod.bypass_rls_session():
        for name in packages:
            existing = await deps.store.get_platform_skill_by_name(name=name)
            assert existing is None


async def test_dry_run_never_uploads_and_hash_matches_a_real_import(
    built_packages: dict[str, Path],
) -> None:
    """Finding 1: dry-run must call the same `externalize_supporting_files`
    (so its hash matches reality) but never actually PUT to the object
    store. Uses the SAME recording store for both a dry-run and a real
    import of the same bytes, so "0 puts" and "identical content_hash" are
    checked against each other, not against a hardcoded expectation."""
    packages = _bytes_map(built_packages)
    dry_store = _RecordingObjectStore()
    dry_deps = _make_deps(_counting_publish()[0], object_store=dry_store)

    dry_results, dry_published = await import_in_pod.run_import(
        packages, dry_run=True, deps=dry_deps
    )

    assert dry_store.puts == []  # zero PUTs during dry-run
    assert dry_published is None

    real_store = _RecordingObjectStore()
    real_deps = _make_deps(_counting_publish()[0], object_store=real_store)
    real_results, _ = await import_in_pod.run_import(packages, dry_run=False, deps=real_deps)

    assert len(real_store.puts) > 0  # the real import DOES upload (sanity check on the fixture)

    dry_by_file = {r["file"]: r for r in dry_results}
    real_by_file = {r["file"]: r for r in real_results}
    for file_name in packages:
        assert dry_by_file[file_name]["content_hash"] == real_by_file[file_name]["content_hash"]


async def test_bad_name_rejected_by_both_real_and_dry_run_parity() -> None:
    """Finding 3: if either side's name-format rule changes independently,
    this goes red — both must reject (or both accept) the same package."""
    bad = {"whatever_filename": _bad_name_package()}

    with pytest.raises(Exception, match="fails validation"):
        await import_in_pod.run_import(bad, dry_run=False, deps=_make_deps(_counting_publish()[0]))

    with pytest.raises(Exception, match="fails validation"):
        await import_in_pod.run_import(bad, dry_run=True, deps=_make_deps(_counting_publish()[0]))


async def test_partial_batch_failure_still_publishes_once_and_keeps_good_import(
    built_packages: dict[str, Path],
) -> None:
    """Finding 2: a later package failing must not lose the earlier
    package's persisted import or its invalidation publish, and the whole
    run must still raise (non-zero exit under `python3 -`)."""
    packages = _bytes_map(built_packages)
    batch = {**packages, "zzz_bad": _bad_name_package()}
    publish, calls = _counting_publish()
    deps = _make_deps(publish)

    with pytest.raises(Exception, match="fails validation"):
        await import_in_pod.run_import(batch, dry_run=False, deps=deps)

    assert calls["n"] == 1  # the good package(s) still triggered exactly one publish
    async with import_in_pod.bypass_rls_session():
        for name in packages:
            existing = await deps.store.get_platform_skill_by_name(name=name)
            assert existing is not None
            assert existing.latest_version == 1


async def test_publish_failure_is_surfaced_not_silently_reported_as_published(
    built_packages: dict[str, Path],
) -> None:
    """Finding 8(c): `run_import` must faithfully report whatever
    `deps.publish()` says — a caller (here, `_pod_entrypoint`) decides how
    to render "skipped"/"failed", but `run_import` itself must not paper
    over a `False` as if nothing happened."""

    async def failing_publish() -> bool:
        return False

    deps = _make_deps(failing_publish)

    results, published = await import_in_pod.run_import(
        _bytes_map(built_packages), dry_run=False, deps=deps
    )

    assert published is False
    assert all(r["status"] == 201 for r in results)  # the imports themselves still succeeded


async def test_run_import_runs_under_the_platform_tenant_scope(
    built_packages: dict[str, Path],
) -> None:
    """Finding 7: audit inserts must run under the platform tenant scope
    (current_tenant_id_var=UUID(int=0), bypass_rls_var=False), not a
    bypass, so they survive once RLS enforcement is live."""
    seen: dict[str, object] = {}

    async def publish() -> bool:
        # Captured from inside `run_import`'s `finally` — still inside its
        # `_platform_tenant_scope()` context at this point.
        seen["tenant_id"] = current_tenant_id_var.get()
        seen["bypass"] = bypass_rls_var.get()
        return True

    deps = _make_deps(publish)
    await import_in_pod.run_import(_bytes_map(built_packages), dry_run=False, deps=deps)

    assert seen["tenant_id"] == import_in_pod.PRINCIPAL.tenant_id
    assert seen["bypass"] is False
    assert current_tenant_id_var.get() is None  # reset after run_import returns
    assert bypass_rls_var.get() is False


async def test_output_name_is_the_parsed_skill_name_not_the_file_stem(
    built_packages: dict[str, Path],
) -> None:
    """Finding 8(a): a misnamed input file must not mislabel the skill."""
    docx_bytes = built_packages["docx"].read_bytes()
    packages = {"totally-different-filename": docx_bytes}
    deps = _make_deps(_counting_publish()[0])

    results, _ = await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    [result] = results
    assert result["file"] == "totally-different-filename"
    assert result["name"] == "docx"


class _FakeRedisClient:
    """Stand-in for the real `redis.asyncio` client — only `publish` is
    exercised by `_PublishRecordingRedisClient`/`InvalidationBus`."""

    def __init__(self, receivers: int) -> None:
        self._receivers = receivers
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return self._receivers


async def test_publish_recording_client_records_receiver_count_and_delegates() -> None:
    """Finding N3: the proxy must forward the call and the real return
    value unchanged (so `InvalidationBus.publish()` keeps working exactly
    as before), while separately recording the receiver count for the
    caller to inspect afterward."""
    fake = _FakeRedisClient(receivers=3)
    recorder = import_in_pod._PublishRecordingRedisClient(fake)

    result = await recorder.publish("expert_work:invalidation", '{"kind": "platform_skill"}')

    assert result == 3
    assert recorder.last_publish_receivers == 3
    assert fake.published == [("expert_work:invalidation", '{"kind": "platform_skill"}')]


def test_publish_recording_client_delegates_other_attributes() -> None:
    class _WithExtra:
        async def publish(self, channel: str, message: str) -> int:
            return 0

        def pubsub(self) -> str:
            return "a-pubsub-object"

    recorder = import_in_pod._PublishRecordingRedisClient(_WithExtra())

    assert recorder.pubsub() == "a-pubsub-object"


def test_publish_outcome_true_only_when_receivers_positive() -> None:
    """Finding N3: 0 receivers (published to nobody) and None (never
    recorded — e.g. InvalidationBus swallowed an exception before
    assigning it) must both count as "not confirmed", the same as a
    genuine failure."""
    assert import_in_pod._publish_outcome(3) is True
    assert import_in_pod._publish_outcome(1) is True
    assert import_in_pod._publish_outcome(0) is False
    assert import_in_pod._publish_outcome(None) is False


def test_bundle_program_compiles_and_embeds_packages_and_asyncio_run(
    built_packages: dict[str, Path],
) -> None:
    text = import_in_pod._make_bundle([str(path) for path in built_packages.values()], dry_run=True)

    compile(text, "<import_in_pod bundle>", "exec")
    assert "asyncio.run(" in text
    for name in built_packages:
        assert f'"{name}"' in text
