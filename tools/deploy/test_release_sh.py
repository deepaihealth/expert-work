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
import yaml

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
    *" get job migrate "*)
        # B-228 — the migrate Job's true conditions. STUB_MIGRATE=Failed for a
        # broken / lock-timed-out migration; default: it completed.
        [[ "${STUB_FAIL:-}" == "get-job" ]] && fail get-job
        echo "${STUB_MIGRATE:-Complete}"
        ;;
    *" logs job/migrate "*) echo "ERROR: canceling statement due to lock timeout" ;;
    *" get pods -l job-name=migrate "*) echo "migrate-abcde   0/1   Error   0" ;;
    *" get deploy/"*)
        # The --images guard reads each untouched image off the cluster;
        # STUB_LIVE_<NAME> overrides, default = what the overlay pins.
        name="$*"; name="${name#*get deploy/}"; name="${name%% *}"
        case "${name}" in
            control-plane) tag="${STUB_LIVE_CONTROL_PLANE:-aaaa1111}" ;;
            admin-ui) tag="${STUB_LIVE_ADMIN_UI:-aaaa1111-test}" ;;
            credential-proxy) tag="${STUB_LIVE_CREDENTIAL_PROXY:-aaaa1111}" ;;
        esac
        printf 'registry.example/expert-work/%s:%s' "${name}" "${tag}"
        ;;
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
    *"jsonpath={.data.api-key}"*) printf 'Y2FuYXJ5LWtleS03ZjNh' ;;  # base64 "canary-key-7f3a"
    *"jsonpath={.data.agent-code}"*) printf 'cmVsZWFzZS1jYW5hcnk=' ;;  # base64 "release-canary"
    *" get deployment.apps/"*" -o wide "*)
        printf 'NAME            READY   UP-TO-DATE   AVAILABLE\n'
        printf 'control-plane   2/3     1            2\n'
        ;;
    *" get pods -o wide "*)
        # One old pod still serving, one new pod restarting — the shape
        # B-56 is about (slow, not broken).
        printf 'NAME                           READY   STATUS    RESTARTS\n'
        printf 'control-plane-old11111-aaaaa   1/1     Running   0\n'
        printf 'control-plane-new22222-bbbbb   0/1     Running   4\n'
        printf 'unrelated-pod-zzzzz            1/1     Running   0\n'
        ;;
    *" get events "*)
        printf 'TYPE      REASON      OBJECT                         MESSAGE\n'
        printf 'Warning   Unhealthy   control-plane-new22222-bbbbb   %s\n' \
            'Startup probe failed: connection refused'
        printf 'Warning   Unhealthy   unrelated-pod-zzzzz            %s\n' \
            'noise that must not be shown'
        ;;
    *" get pods "*) printf 'True||control-plane-stub\n' ;;
    *" exec -i "*)
        # The canary's stdin: the key on the first line, then the line release.sh keeps
        # sending so the stream stays open while the canary runs (an EOF there makes the
        # cluster drop the rest of the canary's output). Record what arrived.
        IFS= read -r key
        printf '%s' "${key}" >"${STUB_CANARY_KEY_FILE:-/dev/null}"
        if IFS= read -r -t 5 next; then
            if [[ -z "${next}" ]]; then echo open; else echo script-on-stdin; fi
        else
            echo closed
        fi >"${STUB_CANARY_STDIN_FILE:-/dev/null}"
        if [[ "${STUB_FAIL:-}" == "canary" ]]; then
            echo "RESULT: FAIL (wrong result) — the release is NOT good."
            exit 1
        fi
        if [[ "${STUB_FAIL:-}" == "canary-no-sandbox" ]]; then
            echo "could not get a sandbox:"
            echo "  exec_python: [tool error] SandboxSupervisorError: sandbox create failed: boom"
            echo "RESULT: FAIL (no sandbox) — the release is NOT good."
            exit 1
        fi
        [[ "${STUB_CANARY_SILENT:-}" == "1" ]] && exit 0
        echo "RESULT: PASS — real run + artifact chain verified."
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
    assert "exec -i" not in harness.calls(), "canary must not run after a timed-out rollout"


def test_slow_rollout_prints_the_evidence_not_just_a_rollback_command(
    harness: Harness,
) -> None:
    """B-56 — the one time this fired for real, the rollout was slow and the
    release was good: old pods kept serving, the new pod settled ~13min in.
    The script printed only "roll back with:", and following it would have
    rolled back a healthy release. Now it must print what it saw."""
    r = harness.run(STUB_FAIL="rollout")
    assert r.returncode != 0
    assert "rollout did not finish within 600s" in r.stderr
    # The evidence: deployment line, both pods of that deployment, its events.
    assert "control-plane-old11111-aaaaa" in r.stderr
    assert "control-plane-new22222-bbbbb" in r.stderr
    assert "Startup probe failed: connection refused" in r.stderr
    # ...and only that deployment's — a name-prefix filter, not the whole cluster.
    assert "unrelated-pod-zzzzz" not in r.stderr
    assert "noise that must not be shown" not in r.stderr
    # The verdict is the operator's, and both branches are spelled out.
    assert "does NOT mean the release is bad" in r.stderr
    assert "still coming up" in r.stderr
    assert "CrashLoopBackOff" in r.stderr
    assert "the release is unverified until they run" in r.stderr
    assert f"tools/deploy/rollback.sh test {PREV_TAG}" in r.stderr


def test_slow_rollout_suppresses_the_generic_roll_back_advice(harness: Harness) -> None:
    """Otherwise the EXIT trap contradicts the tailored block two lines
    later: "read the evidence and decide" followed by "roll back with:"."""
    r = harness.run(STUB_FAIL="rollout")
    assert "The cluster may already run the new images" not in r.stderr
    # Exactly one rollback block, the one inside the tailored advice.
    assert r.stderr.count(f"tools/deploy/rollback.sh test {PREV_TAG}") == 1


def test_every_deployment_is_probed_before_giving_up(harness: Harness) -> None:
    """``set -e`` used to kill the loop on the first timed-out rollout, so a
    release where two deployments were slow reported only the first one."""
    r = harness.run(STUB_FAIL="rollout")
    assert r.returncode != 0
    assert harness.calls().count("rollout status") == 2
    assert "deployment.apps/control-plane: rollout did not finish" in r.stderr
    assert "deployment.apps/admin-ui: rollout did not finish" in r.stderr


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


def test_canary_without_a_verdict_is_red(harness: Harness) -> None:
    """kubectl exec can return 0 with the canary's output cut off (10-16 on the test
    cluster: after stdin closed, the pod's stdout was dropped while the exit code still
    came back). A release must not pass on a canary nobody saw judge."""
    r = harness.run(STUB_CANARY_SILENT="1")
    assert r.returncode != 0
    assert "no 'RESULT: PASS' verdict" in r.stderr
    assert "CANARY FAILED" in r.stderr
    assert f"Release {NEW_TAG} done" not in r.stdout


def test_canary_key_goes_over_stdin_which_stays_open(harness: Harness, tmp_path: Path) -> None:
    key_file, stdin_file = tmp_path / "key", tmp_path / "stdin"
    r = harness.run(STUB_CANARY_KEY_FILE=str(key_file), STUB_CANARY_STDIN_FILE=str(stdin_file))
    assert r.returncode == 0, r.stderr
    assert "RESULT: PASS" in r.stdout
    assert key_file.read_text() == "canary-key-7f3a"  # base64-decoded api-key from the stub Secret
    # Never in argv: the calls log records every argv the stubs saw.
    assert "canary-key-7f3a" not in harness.calls()
    # After the key, stdin carries keep-alive blank lines, not the script, and no EOF.
    assert stdin_file.read_text().strip() == "open"


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


def test_canary_without_a_sandbox_says_so_and_keeps_the_rollback_advice(
    harness: Harness,
) -> None:
    """B-87 ② — "could not get a sandbox" is told apart from "ran and got it
    wrong", but it can still be this release (the e2b incident was), so the
    rollback command stays."""
    r = harness.run(STUB_FAIL="canary-no-sandbox")
    assert r.returncode != 0
    assert "RESULT: FAIL (no sandbox)" in r.stdout
    assert "The canary could not get a sandbox" in r.stderr
    assert "kubectl -n default get sandboxset expert-work-sandbox" in r.stderr
    assert f"tools/deploy/rollback.sh test {PREV_TAG}" in r.stderr


def test_wrong_result_canary_does_not_blame_the_sandbox(harness: Harness) -> None:
    r = harness.run(STUB_FAIL="canary")
    assert "CANARY FAILED" in r.stderr
    assert "could not get a sandbox" not in r.stderr


# --------------------------------------------------------------------------- B-228 migrate first


def _call_index(calls: list[str], needle: str) -> int:
    hits = [i for i, line in enumerate(calls) if needle in line]
    assert hits, f"no call containing {needle!r} in:\n" + "\n".join(calls)
    return hits[0]


def test_migration_completes_before_the_deployments_are_applied(harness: Harness) -> None:
    """B-228 — the Job used to go out in the same ``apply -k`` as the
    Deployments, so new pods could start before the columns they read existed."""
    r = harness.run()
    assert r.returncode == 0, r.stderr
    calls = harness.calls().splitlines()
    overlay = str(harness.overlay.parent)
    migrate_only = _call_index(
        calls, f"kubectl apply -k {overlay} -l app.kubernetes.io/name=migrate"
    )
    waited = _call_index(calls, "kubectl -n expert-work get job migrate")
    full = [i for i, line in enumerate(calls) if line == f"kubectl apply -k {overlay}"]
    assert len(full) == 1, calls
    rollout = _call_index(calls, "rollout status")
    assert _call_index(calls, "delete job migrate") < migrate_only < waited < full[0] < rollout


def test_dry_run_prints_the_migrate_first_order_and_touches_nothing(harness: Harness) -> None:
    r = harness.run("--dry-run")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    overlay = str(harness.overlay.parent)
    migrate = out.index(f"DRY-RUN> kubectl apply -k {overlay} -l app.kubernetes.io/name=migrate")
    wait = out.index("DRY-RUN> wait until job/migrate is Complete")
    full = out.index(f"DRY-RUN> kubectl apply -k {overlay}\n")
    assert migrate < wait < full
    assert "kubectl" not in harness.calls()


def test_failed_migration_stops_before_any_deployment_is_applied(harness: Harness) -> None:
    r = harness.run(STUB_MIGRATE="FailureTarget Failed")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 3 migrate" in r.stderr
    calls = harness.calls().splitlines()
    overlay = str(harness.overlay.parent)
    assert f"kubectl apply -k {overlay}" not in calls, "Deployments must not be applied"
    assert "rollout status" not in harness.calls()
    assert "smoke.sh" not in harness.calls()
    # What it saw, and that nothing needs rolling back.
    assert "canceling statement due to lock timeout" in r.stderr
    assert "migrate-abcde" in r.stderr
    assert "the Deployments were NOT applied" in r.stderr
    assert "rollback.sh" not in r.stderr


def test_migrate_transport_failure_also_says_nothing_was_applied(harness: Harness) -> None:
    """A kubectl failure while polling dies in set -e, still in stage 3
    migrate — the EXIT trap must not print rollback commands for a cluster
    whose Deployments were never touched."""
    r = harness.run(STUB_FAIL="get-job")
    assert r.returncode != 0
    assert "RELEASE FAILED at stage 3 migrate" in r.stderr
    assert "nothing to roll back" in r.stderr
    assert "rollback.sh" not in r.stderr
    assert f"kubectl apply -k {harness.overlay.parent}" not in harness.calls().splitlines()


def test_migrate_job_selector_matches_only_the_job() -> None:
    """``apply -l app.kubernetes.io/name=migrate`` must pick the Job and
    nothing else out of the rendered overlay — and the Job must carry the
    lock timeout the 11-04 releases set by hand."""
    base = Path(__file__).resolve().parents[2] / "infra" / "k8s" / "base"
    labelled = []
    for path in sorted(base.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            labels = (doc.get("metadata") or {}).get("labels") or {}
            if labels.get("app.kubernetes.io/name") == "migrate":
                labelled.append((doc["kind"], doc["metadata"]["name"]))
    assert labelled == [("Job", "migrate")]
    job = yaml.safe_load((base / "control-plane" / "migrate-job.yaml").read_text(encoding="utf-8"))
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "PGOPTIONS", "value": "-c lock_timeout=5s"} in env


# --------------------------------------------------------------------------- --images guard


def test_subset_release_refuses_when_the_cluster_is_ahead_of_the_overlay(
    harness: Harness,
) -> None:
    """2026-09-30: admin-ui was live on a newer tag than the overlay (record PR
    unmerged); a ``--images control-plane,credential-proxy`` release applied
    the whole overlay and silently rolled admin-ui back, smoke green."""
    r = harness.run(
        "--images", "control-plane,credential-proxy", STUB_LIVE_ADMIN_UI="cccc3333-test"
    )
    assert r.returncode != 0
    assert "admin-ui: cluster runs cccc3333-test, overlay pins aaaa1111-test" in r.stderr
    assert "Nothing was built or applied" in r.stderr
    assert "build-push.sh" not in harness.calls()
    assert "kubectl apply" not in harness.calls()


def test_subset_release_proceeds_when_the_overlay_matches(harness: Harness) -> None:
    r = harness.run("--images", "control-plane,credential-proxy")
    assert r.returncode == 0, r.stderr
    assert "get deploy/admin-ui" in harness.calls()
    # Only the image left out is checked.
    assert "get deploy/control-plane" not in harness.calls()


def test_full_release_skips_the_subset_guard(harness: Harness) -> None:
    r = harness.run(STUB_LIVE_ADMIN_UI="cccc3333-test")
    assert r.returncode == 0, r.stderr
    assert "get deploy/" not in harness.calls()


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
