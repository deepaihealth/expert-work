"""CI guard: vendored observability rule files must stay byte-identical.

``infra/k8s/base/observability/rules/`` is a **hand-maintained vendored
copy** of ``tools/observability/rules/`` — see that directory's own
``README.md``: "W2-PR3 vendored copies of tools/observability/rules/ — keep
in sync by hand." Nothing enforces the copy automatically: a fix applied to
the source and forgotten on the vendored side silently ships a stale
alert/recording rule to the k8s-deployed Prometheus while
``tools/observability/rules/`` (and any local ``promtool test`` run against
it) has already moved on. This already recurred once (whole-branch review
round-1 HIGH finding) — pin it here so the next drift fails CI instead of
waiting for the next manual review to catch it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_DIR = _REPO_ROOT / "tools" / "observability" / "rules"
_VENDORED_DIR = _REPO_ROOT / "infra" / "k8s" / "base" / "observability" / "rules"

#: Every file the vendored README claims to mirror.
_VENDORED_FILENAMES = ("alerts.yml", "burn_rate.yml", "sli.yml", "uplift.yml")


@pytest.mark.parametrize("filename", _VENDORED_FILENAMES)
def test_vendored_observability_rule_matches_source(filename: str) -> None:
    source = _SOURCE_DIR / filename
    vendored = _VENDORED_DIR / filename
    assert source.read_bytes() == vendored.read_bytes(), (
        f"{vendored.relative_to(_REPO_ROOT)} has drifted from "
        f"{source.relative_to(_REPO_ROOT)} — infra/k8s/base/observability/rules/ "
        "is a hand-maintained vendored copy (see its README.md); re-copy the "
        "source file over the vendored one to fix."
    )
