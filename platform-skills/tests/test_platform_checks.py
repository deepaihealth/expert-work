"""Layer-1 platform consistency checks across the four office skills.

Runs against the built .skill ZIPs (via ``built_packages`` / ``unpacked_skills``
from conftest.py) using the real control-plane / threat-scan / sandbox-contract
code — no docker, no network. See Task 7 of
.superpowers/sdd/2026-09-26-platform-office-skills/.
"""

import re

import pytest
import yaml

from control_plane.api._skill_moderation import (
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_zip import parse_skill_zip
from expert_work.common.threat_patterns import scan_for_threats
from orchestrator.tools import sandbox_image_contract as contract

# Not imported from conftest: with --import-mode=importlib a test module cannot
# reliably ``import conftest``. Keep in sync with conftest.EXPECTED_SKILLS.
EXPECTED_SKILLS = ("docx", "pptx", "xlsx", "pdf")

# Ruling R2: the JS-route ban targets actionable JS instructions, not the
# required env-block sentence "没有 npm：不要走任何 JavaScript 路线。" (whose own  # noqa: RUF003
# text would otherwise trip a bare ``\bnpm\b``). It must also not flag an
# unrelated Python identifier like an XML "node" loop variable (bare
# ``\bnode\s`` did) — so the node branch requires a trailing ``.js`` argument.
_JS_ROUTE = re.compile(r"npm install|npx\s|require\(|pptxgenjs|docx-js|\bnode\s+\S+\.js", re.I)
_SCRIPT_REF = re.compile(r"\$EXPERT_WORK_SKILLS_DIR/([a-z]+)/scripts/([A-Za-z0-9_]+\.py)")
_ENV_BLOCK = re.compile(r"^## 环境\n(.*?)(?=^## )", re.S | re.M)
_LIBS_LINE = re.compile(r"已预装、直接用、不要装：(.*?)；命令行 (.*?)；")  # noqa: RUF001


def _frontmatter(md: str) -> dict:
    _, fm, _ = md.split("---\n", 2)
    return yaml.safe_load(fm)


def test_exactly_the_four_office_skills_exist(built_packages):
    assert sorted(built_packages) == sorted(EXPECTED_SKILLS)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_platform_zip_parse_accepts_package(built_packages, name):
    payload = parse_skill_zip(built_packages[name].read_bytes(), asset_tier=False)
    assert payload.name == name
    assert payload.lazy_load is True
    assert payload.license == "深护智康自研，仅限本平台使用"  # noqa: RUF001
    # same three moderation calls, same order, as _ingest_platform_skill_payload
    moderate_prompt_fragment(payload.prompt_fragment, lazy_load=payload.lazy_load)
    moderate_tool_names(payload.tool_names)
    moderate_required_models(payload.required_models)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_threat_scans_pass_body_and_every_text_file(unpacked_skills, name):
    for f in sorted((unpacked_skills / name).rglob("*")):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            assert not scan_for_threats(text, scope="strict"), f"strict scan hit: {f}"
            assert not scan_for_threats(text, scope="context"), (
                f"context scan hit (would be dropped at seed): {f}"
            )


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_frontmatter(unpacked_skills, name):
    fm = _frontmatter((unpacked_skills / name / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == name
    assert 0 < len(fm["description"]) <= 200
    assert fm["license"] == "深护智康自研，仅限本平台使用"  # noqa: RUF001
    assert fm["expert_work"] == {"lazy": True, "category": "通用"}


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_body_length_and_no_js_route(unpacked_skills, name):
    root = unpacked_skills / name
    body = (root / "SKILL.md").read_text(encoding="utf-8").split("---\n", 2)[2]
    assert len(body) <= 6000, f"{name} body {len(body)} chars > 6000"
    for f in [root / "SKILL.md", *sorted((root / "scripts").glob("*.py"))]:
        assert not _JS_ROUTE.search(f.read_text(encoding="utf-8")), f"JS route in {f}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_referenced_script_exists_and_every_script_is_referenced(unpacked_skills, name):
    root = unpacked_skills / name
    body = (root / "SKILL.md").read_text(encoding="utf-8")
    refs = _SCRIPT_REF.findall(body)
    assert all(skill == name for skill, _ in refs), (
        f"{name} references another skill's scripts: {refs}"
    )
    referenced = {script for _, script in refs}
    present = {p.name for p in (root / "scripts").glob("*.py")}
    missing = referenced - present
    assert not missing, f"{name}: referenced but not packaged: {missing}"
    helpers = {"_cli.py", "_office.py"}  # imported by other scripts, never called directly
    dead = present - referenced - helpers
    assert not dead, f"{name}: packaged but never mentioned in SKILL.md: {dead}"


def test_env_block_identical_across_skills_modulo_name(unpacked_skills):
    blocks = {}
    for name in EXPECTED_SKILLS:
        m = _ENV_BLOCK.search((unpacked_skills / name / "SKILL.md").read_text(encoding="utf-8"))
        assert m, f"{name}: no '## 环境' section"
        blocks[name] = m.group(1).replace(f"/{name}/", "/<skill>/")
    assert len(set(blocks.values())) == 1, blocks


def test_env_block_claims_match_sandbox_contract(unpacked_skills):
    body = (unpacked_skills / "docx" / "SKILL.md").read_text(encoding="utf-8")
    m = _LIBS_LINE.search(body)
    assert m, "env line format changed"
    libs = {s.strip().lower() for s in m.group(1).split("、")}
    preinstalled = {p.lower() for p in contract.SANDBOX_PREINSTALLED_PYTHON}
    assert libs <= preinstalled, f"claimed but not preinstalled: {libs - preinstalled}"
    bins = {b.command for b in contract.SANDBOX_PREINSTALLED_BINARIES}
    claimed_bins = {w for w in ("soffice", "pdftoppm") if w in m.group(2)}
    assert claimed_bins <= bins
