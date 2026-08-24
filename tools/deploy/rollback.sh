#!/usr/bin/env bash
# Roll an expert-work environment back to a previously released tag
# (W2 spec §3.3). Image tags live in ACR forever, so rollback is a
# seconds-level image switch — no rebuild.
#
#   tools/deploy/rollback.sh <env> <tag> [--images control-plane,admin-ui] [--dry-run]
#
# <tag> is the CONTROL-PLANE tag of the release to return to (the bare
# main sha); admin-ui is switched to <tag><env-suffix> in lockstep, the
# pairing every release mints (release.sh). Uses `kubectl set image`
# directly against the live Deployments for speed — after the fire is
# out, record the state by fixing the overlay newTags + the usual
# chore(deploy) PR (this script prints the reminder).
#
# The migrate Job is NOT re-run: schema migrations are expected to be
# backward-compatible one release back (append-only convention). If the
# bad release shipped a destructive migration, rollback needs a human —
# not this script.
set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: $0 <env> <tag> [--images control-plane,admin-ui] [--dry-run]

  env         target environment: test | prod
  tag         control-plane tag to roll back to (bare main sha);
              admin-ui switches to <tag><env-suffix> in lockstep
  --images    subset to roll back (default: both)
  --dry-run   print every step without executing anything
EOF
    exit 2
}

[[ $# -ge 2 ]] || usage
env_name="$1"
tag="$2"
shift 2

images="control-plane,admin-ui"
dry_run=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --images)
            [[ $# -ge 2 && -n "$2" ]] || usage
            images="$2"
            shift 2
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        -h | --help) usage ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

case "${env_name}" in
    test)
        KUBECONFIG_PATH="${HOME}/.kube/expert-work-test.yaml"
        ADMIN_UI_TAG_SUFFIX="-test"
        ;;
    prod)
        # Deliberately NO confirmation gate here (release.sh has one):
        # rollback is the firefighting path — seconds matter.
        KUBECONFIG_PATH="${HOME}/.kube/expert-work-prod.yaml"
        ADMIN_UI_TAG_SUFFIX="-prod"
        if [[ ! -f "${KUBECONFIG_PATH}" ]]; then
            echo "missing ${KUBECONFIG_PATH} — see docs/runbooks/production-release.md." >&2
            exit 1
        fi
        ;;
    *)
        echo "Unknown env: ${env_name}" >&2
        usage
        ;;
esac
export KUBECONFIG="${KUBECONFIG_PATH}"

readonly ACR="crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work"

run() {
    if [[ "${dry_run}" -eq 1 ]]; then
        echo "DRY-RUN> $*"
    else
        echo "==> $*"
        "$@"
    fi
}

if [[ ",${images}," == *",control-plane,"* ]]; then
    run kubectl -n expert-work set image deploy/control-plane \
        "control-plane=${ACR}/control-plane:${tag}"
fi
if [[ ",${images}," == *",admin-ui,"* ]]; then
    run kubectl -n expert-work set image deploy/admin-ui \
        "admin-ui=${ACR}/admin-ui:${tag}${ADMIN_UI_TAG_SUFFIX}"
fi

if [[ "${dry_run}" -eq 0 ]]; then
    IFS=',' read -ra selected <<<"${images}"
    for d in "${selected[@]}"; do
        kubectl -n expert-work rollout status "deploy/${d}" --timeout=300s
    done
else
    echo "DRY-RUN> rollout status (${images})"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# smoke failure must not read as rollback failure — by this point the
# image switch has already been applied (review M-11).
run "${SCRIPT_DIR}/smoke.sh" "${env_name}" \
    || echo "WARNING: smoke failed/skipped — the rollback image switch itself is already applied." >&2

echo
echo "Rolled back to ${tag}. Now make the record match reality:"
echo "  1. fix infra/k8s/overlays/${env_name}/kustomization.yaml newTags to ${tag} / ${tag}${ADMIN_UI_TAG_SUFFIX}"
echo "  2. commit as the chore(deploy) record PR"
