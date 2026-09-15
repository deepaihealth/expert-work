"""B-61 Task 2 —— 预拉脚本的判定逻辑(不起沙箱,直接 import)。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.tools.prefetch_script import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    content_type_ok,
    pick_suffix,
    script_source,
    target_name,
)


@pytest.mark.parametrize(
    "content_type",
    [
        "image/jpeg",
        "image/png; charset=binary",
        "video/mp4",
        "audio/mpeg",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
    ],
)
def test_media_types_are_prefetched(content_type: str) -> None:
    assert content_type_ok(content_type) is True


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "text/html; charset=utf-8", "application/json", "text/plain", ""],
)
def test_pages_and_text_are_not_prefetched(content_type: str) -> None:
    assert content_type_ok(content_type) is False


def test_suffix_prefers_the_content_type_over_the_url() -> None:
    assert pick_suffix("image/jpeg", "https://x/y") == ".jpg"
    assert pick_suffix("video/mp4", "https://x/y.bin") == ".mp4"


def test_suffix_falls_back_to_the_url_extension() -> None:
    assert pick_suffix("application/octet-stream", "https://x/y.webp") == ".webp"


def test_suffix_is_empty_when_neither_says_anything() -> None:
    assert pick_suffix("application/octet-stream", "https://x/y") == ""


def test_target_name_encodes_the_site_so_two_urls_never_collide() -> None:
    assert target_name("org_logo", [], ".jpg") == "org_logo.jpg"
    assert target_name("materials", [0, "url"], ".mp4") == "materials.0.url.mp4"


def test_target_name_rejects_traversal_in_the_variable_name() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        target_name("../../etc/passwd", [], ".jpg")


def test_limits_are_the_spec_numbers() -> None:
    assert MAX_FILE_BYTES == 32 * 1024 * 1024
    assert MAX_TOTAL_BYTES == 128 * 1024 * 1024


def test_script_source_is_this_module_and_imports_only_stdlib() -> None:
    source = script_source()
    assert "def content_type_ok" in source
    tree = ast.parse(source)
    imported = {
        node.module.split(".")[0]
        if isinstance(node, ast.ImportFrom) and node.module
        else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names or [ast.alias(name="")])
    }
    forbidden = {"orchestrator", "expert_work", "control_plane", "httpx", "pydantic"}
    hit = imported & forbidden
    assert not hit, f"脚本要在沙箱里跑,不能依赖仓库/三方包: {hit}"


def test_script_source_matches_the_file_on_disk() -> None:
    path = Path(__file__).parents[1] / "src/orchestrator/tools/prefetch_script.py"
    assert script_source() == path.read_text(encoding="utf-8")


def test_site_walk_matches_the_host_side_implementation() -> None:
    from orchestrator.tools.inputs_doc import iter_url_sites
    from orchestrator.tools.prefetch_script import _sites

    doc = {
        "variables": {
            "a": {"value": "https://x/1.jpg", "trusted": True},
            "b": {"value": [{"url": "https://x/2.mp4"}, {"url": "not-a-url"}], "trusted": True},
            "c": {
                "value": {
                    "url": "https://ok/a.mp4",
                    "local_path": "https://attacker/b.mp4",
                },
                "trusted": True,
            },
        }
    }
    host = [(s.var_name, list(s.path), s.url) for s in iter_url_sites(doc)]
    sandbox = [
        (name, path, url)
        for name, entry in doc["variables"].items()
        for path, url in _sites(entry["value"], [])
    ]
    assert host == sandbox
    # 显式钉住攻击场景本身:c 只应该命中 url 那条,local_path 绝不能被当成待预拉的地址。
    assert ("c", ["url"], "https://ok/a.mp4") in sandbox
    assert not any(name == "c" and url == "https://attacker/b.mp4" for name, _, url in sandbox)
