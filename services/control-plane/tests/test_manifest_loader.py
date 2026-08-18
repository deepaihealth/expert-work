"""Manifest loader tests — YAML + Pydantic lint pipeline (no template stage:
``{{ }}`` is stored verbatim).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.manifest import (
    ManifestLoader,
    ManifestSyntaxError,
    ManifestValidationError,
    load_manifest,
)

_MINIMAL_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


def test_load_minimal_yaml() -> None:
    spec = load_manifest(_MINIMAL_YAML)
    assert spec.metadata.name == "code-reviewer"
    assert spec.spec.model.provider == "anthropic"


def test_double_braces_in_system_prompt_survive_verbatim() -> None:
    """Jinja 动态 prompt 的 {{ }} 是 run 期语义:保存时必须原样入库,不能被当成
    manifest 变量求值(调试台重设计 PR0 Bug B —— 「保存时填空」整层已拆掉)。"""
    yaml_text = _MINIMAL_YAML.replace(
        'template: "you are a reviewer"',
        'template: "you are {{ persona }}"\n    jinja: true\n    variables: [{name: persona}]',
    )
    spec = load_manifest(yaml_text)
    assert spec.spec.system_prompt.template == "you are {{ persona }}"
    assert spec.spec.system_prompt.jinja is True
    assert [v.name for v in spec.spec.system_prompt.variables] == ["persona"]


def test_double_braces_survive_even_when_jinja_is_off() -> None:
    """jinja 关着时 {{ }} 也只是普通文本,同样原样入库(以前会 ManifestTemplateError)。"""
    yaml_text = _MINIMAL_YAML.replace('"you are a reviewer"', '"literal {{ not_a_var }}"')
    spec = load_manifest(yaml_text)
    assert spec.spec.system_prompt.template == "literal {{ not_a_var }}"


def test_load_from_path(tmp_path: Path) -> None:
    f = tmp_path / "manifest.yaml"
    f.write_text(_MINIMAL_YAML, encoding="utf-8")
    spec = load_manifest(f)
    assert spec.metadata.name == "code-reviewer"


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_size_cap_enforced() -> None:
    loader = ManifestLoader(max_size_bytes=512)
    huge = "x" * 1024
    with pytest.raises(ManifestSyntaxError) as exc_info:
        loader.load_from_string(huge)
    assert "size cap" in str(exc_info.value)


def test_broken_yaml_raises_syntax() -> None:
    with pytest.raises(ManifestSyntaxError):
        load_manifest("apiVersion: expert_work.io/v1\nkind: Agent\nthis: is: broken: yaml")


def test_non_mapping_root_raises_syntax() -> None:
    with pytest.raises(ManifestSyntaxError):
        load_manifest("- just-a-list")


def test_pydantic_validation_error_surfaces() -> None:
    """Missing required ``kind`` → ManifestValidationError with the
    underlying pydantic errors attached."""
    broken = _MINIMAL_YAML.replace("kind: Agent\n", "")
    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(broken)
    assert exc_info.value.errors  # non-empty list of pydantic errors
    assert any("kind" in str(err.get("loc", "")) for err in exc_info.value.errors)


def test_lint_wildcard_allowlist_rejected() -> None:
    broken = _MINIMAL_YAML.replace(
        'allowlist: ["api.anthropic.com"]',
        'allowlist: ["*"]',
    )
    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(broken)
    # The summary message is intentionally generic (CodeQL stack-trace
    # exposure). Detail lives on ``.errors`` as a curated whitelist.
    assert any("allowlist" in str(err["msg"]).lower() for err in exc_info.value.errors)


def test_lint_fallback_cycle_rejected() -> None:
    """Self-referential fallback chain trips lint rule #8."""
    cycle_fragment = (
        "name: claude-sonnet-4-5\n"
        "    fallback:\n"
        "      - { provider: anthropic, name: claude-sonnet-4-5 }"
    )
    broken = _MINIMAL_YAML.replace("name: claude-sonnet-4-5", cycle_fragment)
    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(broken)
    assert any("cycle" in str(err["msg"]).lower() for err in exc_info.value.errors)


def test_loader_rejects_non_positive_size_cap() -> None:
    with pytest.raises(ValueError):
        ManifestLoader(max_size_bytes=0)


def test_yaml_safe_load_blocks_arbitrary_objects() -> None:
    """``yaml.safe_load`` must refuse the Python-tagged construction
    vector used by CVE-2017-18342 / 2020-1747."""
    malicious = """\
!!python/object/apply:os.system
- "echo pwned"
"""
    with pytest.raises(ManifestSyntaxError):
        load_manifest(malicious)
