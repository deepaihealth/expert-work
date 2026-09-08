"""Stub-driven tests for ``tools/deploy/release.sh`` (B-29 ③ + X-14 P5).

The script is run for real (bash) against a throwaway "repo root": its
``tools/deploy/`` holds the script under test next to STUB ``build-push.sh``
/ ``smoke.sh`` / ``canary.py`` (release.sh calls those by path, not via
PATH), and ``infra/k8s/overlays/test/kustomization.yaml`` carries known
previous newTags. ``kubectl`` / ``kustomize`` / ``git`` are stub executables
on a PATH prefix that log their argv and fail on demand (``STUB_FAIL``).
No cluster, no docker, no real git — same unit/integration split as
``test_deploy.py`` vs ``test_deploy_integration.py``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_RELEASE_SH = Path(__file__).resolve().parent / "release.sh"

_ACR = "crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work"

PREV_TAG = "aaaa1111"
NEW_TAG = "bbbb2222"

# --------------------------------------------------------------------------- stubs

# Every stub appends "<tool> <argv>" to $STUB_CALLS so tests can assert what
# ran (and, for the failure cases, what did NOT run after the red stage).
_KUBECTL_STUB = r"""#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"${STUB_CALLS}"
fail() { echo "stub kubectl: simulated failure ($1)" >&2; exit 1; }
case " $* " in
    *" get deploy -o name "*)
        [[ "${STUB_FAIL:-}" == "get-deploy" ]] && fail get-deploy
        [[ "${STUB_EMPTY_DEPLOYS:-}" == "1" ]] && exit 0
        printf 'deployment.apps/control-plane\ndeployment.apps/admin-ui\n'
        ;;
    *" rollout status "*)
        [[ "${STUB_FAIL:-}" == "rollout" ]] && fail rollout
        ;;
    *" get secret canary-credentials --ignore-not-found -o name "*)
        [[ "${STUB_CANARY_UNSEEDED:-}" == "1" ]] || echo secret/canary-credentials
        ;;
    *"jsonpath={.data.api-key}"*) printf 'c2VjcmV0' ;;              # base64 "secret"
    *"jsonpath={.data.agent-code}"*) printf 'cmVsZWFzZS1jYW5hcnk=' ;;  # base64 "release-canary"
    *" get pods "*) printf 'True||control-plane-stub\n' ;;
    *" exec -i "*)
        cat >/dev/null
        [[ "${STUB_FAIL:-}" == "canary" ]] && fail canary
        ;;
esac
exit 0
"""

# ``edit set image NAME=NAME:TAG`` really rewrites the cwd's
# kustomization.yaml, so a release.sh that read the "previous" tag AFTER
# the edit would print new → new and fail the P5 assertions.
_KUSTOMIZE_STUB = r"""#!/usr/bin/env bash
printf 'kustomize %s\n' "$*" >>"${STUB_CALLS}"
[[ "${STUB_FAIL:-}" == "kustomize" ]] && { echo "stub kustomize: simulated failure" >&2; exit 1; }
if [[ "$1 $2 $3" == "edit set image" ]]; then
    name="${4%%=*}"
    new_tag="${4##*:}"
    awk -v name="${name}" -v tag="${new_tag}" '
        $1 == "-" && $2 == "name:" { hit = ($3 == name) }
        hit && $1 == "newTag:" { $0 = "  newTag: " tag; hit = 0 }
        { print }' kustomization.yaml >kustomization.yaml.tmp
    mv kustomization.yaml.tmp kustomization.yaml
fi
exit 0
"""

_GIT_STUB = r"""#!/usr/bin/env bash
printf 'git %s\n' "$*" >>"${STUB_CALLS}"
case " $* " in
    *" rev-parse --short HEAD "*) echo stubsha0 ;;
    *" diff --stat "*) echo " infra/k8s/overlays/test/kustomization.yaml | 6 +++---" ;;
esac
exit 0
"""

_BUILD_PUSH_STUB = r"""#!/usr/bin/env bash
printf 'build-push.sh %s\n' "$*" >>"${STUB_CALLS}"
[[ "${STUB_FAIL:-}" == "build-push" ]] && { echo "stub build-push: simulated failure" >&2; exit 1; }
exit 0
"""

_SMOKE_STUB = r"""#!/usr/bin/env bash
printf 'smoke.sh %s\n' "$*" >>"${STUB_CALLS}"
if [[ "${STUB_FAIL:-}" == "smoke" ]]; then
    echo "SMOKE FAIL"
    exit 1
fi
echo "SMOKE PASS"
exit 0
"""


def _kustomization(*, cp: str, ui: str, proxy: str) -> str:
    """A cut-down overlay: same ``images:`` entry shape as the real one,
    plus one entry without newTag and one with a quoted tag (both must not
    confuse the previous-tag lookup)."""
    return f"""apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
- ../../base

images:
- name: clickhouse/clickhouse-server
  newName: {_ACR}/clickhouse-server
  newTag: 24.12-alpine-r1
- name: {_ACR}/admin-ui
  newName: {_ACR}/admin-ui
  newTag: {ui}
- name: {_ACR}/control-plane
  newName: {_ACR}/control-plane
  newTag: {cp}
- name: {_ACR}/credential-proxy
  newName: {_ACR}/credential-proxy
  newTag: {proxy}
- name: grafana/grafana
  newName: {_ACR}/grafana
- name: quay.io/keycloak/keycloak
  newName: {_ACR}/keycloak
  newTag: "25.0"
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@dataclass
class Harness:
    root: Path
    bin_dir: Path
    calls_log: Path
    overlay: Path

    def write_overlay(self, *, cp: str, ui: str, proxy: str) -> None:
        self.overlay.write_text(_kustomization(cp=cp, ui=ui, proxy=proxy))

    def run(self, *extra_args: str, **stub_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
            "STUB_CALLS": str(self.calls_log),
            **stub_env,
        }
        script = self.root / "tools" / "deploy" / "release.sh"
        argv = ["bash", str(script), "test", "--tag", NEW_TAG, *extra_args]
        return subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> str:
        return self.calls_log.read_text() if self.calls_log.exists() else ""


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    deploy_dir = tmp_path / "tools" / "deploy"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "release.sh").write_text(_RELEASE_SH.read_text())
    _write_exec(deploy_dir / "build-push.sh", _BUILD_PUSH_STUB)
    _write_exec(deploy_dir / "smoke.sh", _SMOKE_STUB)
    (deploy_dir / "canary.py").write_text("# stub canary — streamed into the kubectl exec stub\n")

    overlay_dir = tmp_path / "infra" / "k8s" / "overlays" / "test"
    overlay_dir.mkdir(parents=True)
    (tmp_path / "home").mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "kubectl", _KUBECTL_STUB)
    _write_exec(bin_dir / "kustomize", _KUSTOMIZE_STUB)
    _write_exec(bin_dir / "git", _GIT_STUB)

    h = Harness(
        root=tmp_path,
        bin_dir=bin_dir,
        calls_log=tmp_path / "calls.log",
        overlay=overlay_dir / "kustomization.yaml",
    )
    h.write_overlay(cp=PREV_TAG, ui=f"{PREV_TAG}-test", proxy=PREV_TAG)
    return h


# --------------------------------------------------------------------------- B-29 ③ exit codes


def test_build_push_failure_names_stage_and_stops(harness: Harness) -> None:
    r = harness.run(STUB_FAIL="build-push")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 1 build-push" in r.stderr
    assert "kustomize edit" not in harness.calls()
    assert "kubectl apply" not in harness.calls()
    assert f"Release {NEW_TAG} done" not in r.stdout


def test_kustomize_failure_names_stage(harness: Harness) -> None:
    r = harness.run(STUB_FAIL="kustomize")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 2 overlay newTag" in r.stderr
    assert "kubectl apply" not in harness.calls()


def test_rollout_failure_names_stage_and_skips_smoke(harness: Harness) -> None:
    r = harness.run(STUB_FAIL="rollout")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 4 rollout" in r.stderr
    assert "smoke.sh" not in harness.calls()


def test_deploy_listing_failure_is_not_swallowed(harness: Harness) -> None:
    """B-29 ③ concrete hole: ``for d in $(kubectl get deploy -o name)`` —
    errexit ignores a failing substitution in a for-list, so the release
    waited for zero rollouts and ran smoke against the still-healthy OLD
    pods, then printed "Release done" with exit 0."""
    r = harness.run(STUB_FAIL="get-deploy")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 4 rollout" in r.stderr
    assert "rollout status" not in harness.calls()
    assert "smoke.sh" not in harness.calls()
    assert f"Release {NEW_TAG} done" not in r.stdout


def test_empty_deploy_listing_is_a_failure(harness: Harness) -> None:
    r = harness.run(STUB_EMPTY_DEPLOYS="1")
    assert r.returncode != 0
    assert "no Deployments" in r.stderr
    assert "smoke.sh" not in harness.calls()


def test_smoke_failure_names_stage_and_prints_rollback(harness: Harness) -> None:
    r = harness.run(STUB_FAIL="smoke")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 5 smoke" in r.stderr
    assert f"tools/deploy/rollback.sh test {PREV_TAG}" in r.stderr
    assert "exec -i" not in harness.calls(), "canary must not run after a red smoke"


def test_canary_failure_is_red_with_the_real_previous_tag(harness: Harness) -> None:
    r = harness.run(STUB_FAIL="canary")
    assert r.returncode != 0
    assert "CANARY FAILED" in r.stderr
    assert "RELEASE FAILED at stage 6 canary" in r.stderr
    assert f"tools/deploy/rollback.sh test {PREV_TAG}" in r.stderr
    assert "<上一版 tag>" not in r.stderr
    assert f"Release {NEW_TAG} done" not in r.stdout


def test_unseeded_canary_is_a_warning_not_a_failure(harness: Harness) -> None:
    """Deliberate: an environment without the canary Secret must still
    release (release.sh stage 6 header) — only a canary that RAN and failed
    is red."""
    r = harness.run(STUB_CANARY_UNSEEDED="1")
    assert r.returncode == 0, r.stderr
    assert "WARNING: canary skipped" in r.stderr
    assert "RELEASE FAILED" not in r.stderr
    assert "exec -i" not in harness.calls()
    assert f"Release {NEW_TAG} done" in r.stdout


# --------------------------------------------------------------------------- X-14 P5 previous tag


def test_success_prints_previous_tags_and_one_rollback_command(harness: Harness) -> None:
    r = harness.run()
    assert r.returncode == 0, r.stderr
    assert "RELEASE FAILED" not in r.stderr
    assert f"- control-plane: {PREV_TAG} → {NEW_TAG}" in r.stdout
    assert f"- admin-ui: {PREV_TAG}-test → {NEW_TAG}-test" in r.stdout
    assert f"- credential-proxy: {PREV_TAG} → {NEW_TAG}" in r.stdout
    assert f"上一版 tag:{PREV_TAG}" in r.stdout
    # Lockstep previous tags → exactly rollback.sh's real CLI, no --images.
    assert f"  tools/deploy/rollback.sh test {PREV_TAG}\n" in r.stdout
    # The stub kustomize really rewrote the overlay, so the previous tags
    # above can only have come from BEFORE the edit.
    overlay = harness.overlay.read_text()
    assert f"newTag: {NEW_TAG}\n" in overlay
    assert f"newTag: {NEW_TAG}-test\n" in overlay
    assert PREV_TAG not in overlay


def test_hand_pinned_proxy_gets_its_own_rollback_line(harness: Harness) -> None:
    """Pre-2026-09-07 overlays pinned credential-proxy by hand (rollback.sh
    header) — one rollback command per image then, each with its own tag."""
    harness.write_overlay(cp=PREV_TAG, ui=f"{PREV_TAG}-test", proxy="94f1a371")
    r = harness.run()
    assert r.returncode == 0, r.stderr
    assert f"tools/deploy/rollback.sh test {PREV_TAG} --images control-plane\n" in r.stdout
    assert f"tools/deploy/rollback.sh test {PREV_TAG} --images admin-ui\n" in r.stdout
    assert "tools/deploy/rollback.sh test 94f1a371 --images credential-proxy\n" in r.stdout
    assert f"  tools/deploy/rollback.sh test {PREV_TAG}\n" not in r.stdout


def test_images_subset_keeps_the_images_flag(harness: Harness) -> None:
    r = harness.run("--images", "control-plane")
    assert r.returncode == 0, r.stderr
    assert f"- control-plane: {PREV_TAG} → {NEW_TAG}" in r.stdout
    assert f"- admin-ui: {PREV_TAG}-test (unchanged" in r.stdout
    assert f"tools/deploy/rollback.sh test {PREV_TAG} --images control-plane\n" in r.stdout


def test_first_release_has_no_rollback_target(harness: Harness) -> None:
    placeholder = "PROD_PLACEHOLDER_TAG"
    harness.write_overlay(cp=placeholder, ui=f"{placeholder}-test", proxy=placeholder)
    r = harness.run()
    assert r.returncode == 0, r.stderr
    assert "上一版 tag:无(首次发布)" in r.stdout
    assert "rollback.sh test PROD_PLACEHOLDER" not in r.stdout
    assert "nothing to roll back to" in r.stdout
