#!/usr/bin/env bash
# Release canary on its own (B-207 runbook §7): one real agent run + artifact
# download, exactly as tools/deploy/release.sh stage 5 runs it — same Secret,
# same pod filter, key over stdin only (never argv, never echoed).
#
#   tools/deploy/pg_upgrade/run_canary.sh test|prod
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-}" in
    test) export KUBECONFIG="${HOME}/.kube/expert-work-test.yaml" ;;
    prod) export KUBECONFIG="${HOME}/.kube/expert-work-prod.yaml" ;;
    *) echo "Usage: $0 test|prod" >&2; exit 2 ;;
esac

canary_key="$(kubectl -n expert-work get secret canary-credentials -o jsonpath='{.data.api-key}' | base64 -d)"
canary_agent="$(kubectl -n expert-work get secret canary-credentials -o jsonpath='{.data.agent-code}' | base64 -d || true)"
[[ -n "${canary_key}" ]] || { echo "canary-credentials api-key is empty" >&2; exit 1; }
pod="$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}|{.metadata.deletionTimestamp}|{.metadata.name}{"\n"}{end}' \
    | awk -F'|' '$1 == "True" && $2 == "" {print $3; exit}')"
[[ -n "${pod}" ]] || { echo "no Running+Ready control-plane pod" >&2; exit 1; }
echo "==> canary run via ${pod} (agent=${canary_agent:-release-canary})"

canary_src="$(base64 <"${SCRIPT_DIR}/../canary.py" | tr -d '\n')"
log="$(mktemp)"
trap 'rm -f "${log}"' EXIT
set +e
# stdin must stay open after the key line (release.sh: an EOF drops the
# pod's output), so keep feeding blank lines until kubectl exits.
{ printf '%s\n' "${canary_key}"; while sleep 1; do printf '\n' || exit 0; done; } 2>/dev/null \
    | kubectl -n expert-work exec -i "${pod}" -- \
        env "EXPERT_WORK_CANARY_AGENT_CODE=${canary_agent:-release-canary}" \
        python -c "import base64, os, sys
os.environ['EXPERT_WORK_CANARY_API_KEY'] = sys.stdin.readline().rstrip('\n')
exec(compile(base64.b64decode('${canary_src}').decode(), 'canary.py', 'exec'))" 2>&1 \
    | tee "${log}"
rc="${PIPESTATUS[1]}"
set -e
if [[ "${rc}" -ne 0 ]] || ! grep -q '^RESULT: PASS' "${log}"; then
    echo "CANARY FAILED" >&2
    exit 1
fi
