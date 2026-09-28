"""Package-level checks for the health-plan-report skill: frontmatter, body length, that every
script/reference/style key/kind/variant named in the docs really exists, the boundary rule (no
tenant/customer/employee/agent names), and the shared platform moderation/threat/text-only gates.
See Task 9 of .superpowers/sdd/2026-09-28-health-plan-report-skill/.
"""

import inspect
import re
import zipfile
from pathlib import Path

import pytest
import yaml
from hpr import pdf_html, ppt_draw
from hpr.catalog import VARIANTS
from hpr.style import OPTIONS

from control_plane.api._skill_moderation import (
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_zip import parse_skill_zip
from expert_work.common.threat_patterns import scan_for_threats

NAME = "health-plan-report"
REF = re.compile(r"\$EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/([A-Za-z0-9_]+\.py)")
KEY = re.compile(r"`((?:color|type|layout|brand|footer|cover|section|output)\.[a-z_.]+|toc)`")
_TOC_THRESHOLD = re.compile(r'len\(content\["sections"\]\)\s*>=\s*(\d+)')


def _toc_threshold(fn) -> int:
    """The literal section-count threshold `fn`'s source uses for `toc: auto`."""
    m = _TOC_THRESHOLD.search(inspect.getsource(fn))
    assert m, f"未在 {fn.__qualname__} 里找到 TOC 章节数阈值表达式"
    return int(m.group(1))


@pytest.fixture(scope="module")
def pkg(built_packages) -> Path:
    return built_packages[NAME]


@pytest.fixture(scope="module")
def root(unpacked_skills) -> Path:
    return unpacked_skills / NAME


def _body(root: Path) -> tuple[dict, str]:
    _, fm, body = (root / "SKILL.md").read_text(encoding="utf-8").split("---\n", 2)
    return yaml.safe_load(fm), body


def test_platform_parse_and_moderation(pkg):
    payload = parse_skill_zip(pkg.read_bytes(), asset_tier=False)
    assert payload.name == NAME
    assert payload.lazy_load is True
    moderate_prompt_fragment(payload.prompt_fragment, lazy_load=payload.lazy_load)
    moderate_tool_names(payload.tool_names)
    moderate_required_models(payload.required_models)


def test_frontmatter(root):
    fm, _ = _body(root)
    assert fm["name"] == NAME
    assert 0 < len(fm["description"]) <= 200
    assert fm["license"] == "深护智康自研，仅限本平台使用"  # noqa: RUF001
    assert fm["expert_work"] == {"lazy": True, "category": "健康"}


def test_body_length(root):
    _, body = _body(root)
    assert len(body) <= 6000, len(body)


def test_scripts_referenced_both_ways(root):
    _, body = _body(root)
    referenced = set(REF.findall(body))
    present = {p.name for p in (root / "scripts").glob("*.py")} - {"_cli.py"}
    assert referenced == present


def test_reference_docs_linked_and_present(root):
    _, body = _body(root)
    for name in ("content-schema.md", "style-options.md", "migration.md"):
        assert f"reference/{name}" in body
        assert (root / "reference" / name).is_file()


def test_every_style_key_in_docs_exists(root):
    text = (root / "SKILL.md").read_text(encoding="utf-8") + (
        root / "reference" / "style-options.md"
    ).read_text(encoding="utf-8")
    for key in set(KEY.findall(text)):
        assert (
            key in OPTIONS
            or key.startswith("blocks.")
            or key.startswith("sections.")
            or key.startswith("brand.")
        ), key
    for key in OPTIONS:
        assert f"`{key}`" in text, f"style-options.md 漏写 {key}"


def test_toc_auto_thresholds_documented(root):
    """PDF (`build_html`) and PPTX (`_show_toc`) use different section-count thresholds for
    `toc: auto`; style-options.md must state each format's real number, not one shared number."""
    pdf_n = _toc_threshold(pdf_html.build_html)
    pptx_n = _toc_threshold(ppt_draw._show_toc)
    text = (root / "reference" / "style-options.md").read_text(encoding="utf-8")
    assert f"PDF 章节数 ≥ {pdf_n}" in text
    assert f"PPTX 章节数 ≥ {pptx_n}" in text


def test_every_kind_and_variant_documented(root):
    text = (root / "reference" / "content-schema.md").read_text(encoding="utf-8")
    for kind, variants in VARIANTS.items():
        assert f"`{kind}`" in text, kind
        for v in variants:
            assert f"`{v}`" in text, (kind, v)


def test_check_section_uses_read_page_then_ask_image(root):
    _, body = _body(root)
    m = re.search(r"^## 检查成品\n(.*?)(?=^## )", body, re.S | re.M)
    assert m
    sec = m.group(1)
    assert 0 <= sec.find("read_page(") < sec.find("ask_image(")
    for call in re.findall(r"ask_image\(([^)]*)\)", sec):
        assert ".png" not in call.lower()


def test_no_tenant_or_agent_names_anywhere(root):
    banned = ("ai-health-plan", "ahp", "张女士", "苹果夹子")
    for f in root.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            for word in banned:
                assert word not in text, (f, word)
            lines = [ln for ln in text.splitlines() if "深护" in ln]
            assert all(ln.startswith("license:") for ln in lines), (f, lines)


def test_threat_scans(root):
    for f in sorted(root.rglob("*")):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            assert not scan_for_threats(text, scope="strict"), f
            assert not scan_for_threats(text, scope="context"), f


def test_package_is_text_only(pkg):
    with zipfile.ZipFile(pkg) as z:
        for name in z.namelist():
            z.read(name).decode("utf-8")
