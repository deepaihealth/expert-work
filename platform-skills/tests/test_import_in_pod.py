"""Tests for `platform-skills/import_in_pod.py` (Task 8).

Exercises `run_import` — the whole create / version-bump / no-op / dry-run
decision logic — against in-memory stores (`InMemorySkillStore`,
`InMemoryAuditLogStore`) via the platform's own
`_ingest_platform_skill_payload` pipeline. No database, no cluster.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType

from control_plane.audit import TenantConfigPiiResolver, build_default_audit_logger
from expert_work.persistence import InMemoryAuditLogStore, InMemorySkillStore

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


def _counting_publish() -> tuple[Callable[[], Awaitable[None]], dict[str, int]]:
    calls = {"n": 0}

    async def publish() -> None:
        calls["n"] += 1

    return publish, calls


def _make_deps(publish: Callable[[], Awaitable[None]]) -> import_in_pod.ImportDeps:
    return import_in_pod.ImportDeps(
        store=InMemorySkillStore(),
        audit=build_default_audit_logger(
            store=InMemoryAuditLogStore(), pii_fields_resolver=TenantConfigPiiResolver()
        ),
        object_store=None,
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


async def test_first_import_creates_every_package_and_publishes_once(
    built_packages: dict[str, Path],
) -> None:
    packages = _bytes_map(built_packages)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)

    results = await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    assert len(results) == len(packages)
    for result in results:
        assert result["status"] == 201
        assert result["created"] is True
        assert result["version"] == 1
        assert isinstance(result["content_hash"], str)
        assert result["content_hash"]
    assert calls["n"] == 1


async def test_reimport_unchanged_is_idempotent_and_does_not_republish(
    built_packages: dict[str, Path],
) -> None:
    packages = _bytes_map(built_packages)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)
    await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    results = await import_in_pod.run_import(packages, dry_run=False, deps=deps)

    assert calls["n"] == 1  # unchanged since the first import above — no new publish
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

    results = await import_in_pod.run_import(modified, dry_run=False, deps=deps)

    by_name = {r["name"]: r for r in results}
    assert by_name["docx"]["status"] == 201
    assert by_name["docx"]["created"] is True
    assert by_name["docx"]["version"] == 2
    for name, result in by_name.items():
        if name != "docx":
            assert result["status"] == 200
            assert result["created"] is False
    assert calls["n"] == 2  # first import's publish + one more for the docx version bump


async def test_dry_run_writes_nothing_and_does_not_publish(
    built_packages: dict[str, Path],
) -> None:
    packages = _bytes_map(built_packages)
    publish, calls = _counting_publish()
    deps = _make_deps(publish)

    results = await import_in_pod.run_import(packages, dry_run=True, deps=deps)

    assert calls["n"] == 0
    for result in results:
        assert result["status"] == "dry-run"
        assert result["created"] is False
        assert result["would_create_version"] == 1  # fresh store — nothing imported yet
        assert isinstance(result["content_hash"], str)
        assert result["content_hash"]

    async with import_in_pod.bypass_rls_session():
        for name in packages:
            existing = await deps.store.get_platform_skill_by_name(name=name)
            assert existing is None


def test_bundle_program_compiles_and_embeds_packages_and_asyncio_run(
    built_packages: dict[str, Path],
) -> None:
    text = import_in_pod._make_bundle([str(path) for path in built_packages.values()], dry_run=True)

    compile(text, "<import_in_pod bundle>", "exec")
    assert "asyncio.run(" in text
    for name in built_packages:
        assert f'"{name}"' in text
