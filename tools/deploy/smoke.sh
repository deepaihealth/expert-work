#!/usr/bin/env bash
# Post-release smoke for an expert-work environment — the manual checks
# PR-2/PR-3 bring-up ran, frozen into one command (W2 spec §3.3).
#
#   tools/deploy/smoke.sh test
#
# Read-only and idempotent. In-cluster HTTP goes through a control-plane
# pod's python (dev machines often can't curl the cluster/public
# endpoints directly — the PR-2 recipe). Exits non-zero on the first
# failed check.
set -euo pipefail

usage() {
    echo "Usage: $0 <env>   (env: test)" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage
case "$1" in
    test)
        KUBECONFIG_PATH="${HOME}/.kube/expert-work-test.yaml"
        PUBLIC_BASE="https://expert-work-test.deepaihealth.com"
        LANGFUSE_BASE="https://langfuse-test.deepaihealth.com"
        ;;
    prod)
        echo "prod is not provisioned yet (W4) — refusing." >&2
        exit 1
        ;;
    *) usage ;;
esac
export KUBECONFIG="${KUBECONFIG_PATH}"

fail=0
check() {
    local name="$1" got="$2" want="$3"
    if [[ "${got}" == "${want}" ]]; then
        echo "OK   ${name} (${got})"
    else
        echo "FAIL ${name}: got '${got}', want '${want}'"
        fail=1
    fi
}

echo "== pods =="
not_ready="$(kubectl -n expert-work get pods --no-headers \
    | awk '$3 != "Running" && $3 != "Completed" {print $1"("$3")"}' | paste -sd, - || true)"
check "all pods Running/Completed" "${not_ready:-none}" "none"

POD="$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
    -o jsonpath='{.items[0].metadata.name}')"

echo "== http (via ${POD}) =="
# One python invocation, one line per probe: "<name> <status-or-error>".
probes="$(kubectl -n expert-work exec "${POD}" -- python -c "
import json, urllib.request, urllib.error

def status(url):
    try:
        return str(urllib.request.urlopen(url, timeout=10).status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:
        return type(e).__name__

print('healthz', status('http://localhost:8000/healthz/ready'))
print('v1_auth', status('${PUBLIC_BASE}/v1/models'))
print('public_home', status('${PUBLIC_BASE}/'))
print('docs_site', status('${PUBLIC_BASE}/docs/'))
print('keycloak', status('${PUBLIC_BASE}/kc/realms/expert-work/.well-known/openid-configuration'))
print('langfuse_pub', status('${LANGFUSE_BASE}/api/public/health'))
print('langfuse_int', status('http://langfuse-web:3000/api/public/health'))
print('grafana', status('http://grafana:3000/api/health'))
try:
    t = json.load(urllib.request.urlopen('http://prometheus:9090/api/v1/targets', timeout=10))
    up = sum(1 for x in t['data']['activeTargets'] if x['health'] == 'up')
    total = len(t['data']['activeTargets'])
    print('prom_targets', f'{up}/{total}')
except Exception as e:
    print('prom_targets', type(e).__name__)
")"

get() { echo "${probes}" | awk -v k="$1" '$1==k{print $2}'; }

check "control-plane /healthz/ready" "$(get healthz)" "200"
# 401 = the auth layer is alive and rejecting anonymous traffic — the
# expected steady state for a bare /v1 endpoint.
check "public /v1 auth gate" "$(get v1_auth)" "401"
check "public admin-ui" "$(get public_home)" "200"
check "public docs site" "$(get docs_site)" "200"
check "keycloak oidc discovery" "$(get keycloak)" "200"
check "langfuse public" "$(get langfuse_pub)" "200"
check "langfuse in-cluster" "$(get langfuse_int)" "200"
check "grafana" "$(get grafana)" "200"

targets="$(get prom_targets)"
if [[ "${targets}" == */* && "${targets%%/*}" == "${targets##*/}" && "${targets%%/*}" != "0" ]]; then
    echo "OK   prometheus targets all up (${targets})"
else
    echo "FAIL prometheus targets: ${targets} (want all up, non-zero)"
    fail=1
fi

echo
if [[ "${fail}" -eq 0 ]]; then
    echo "SMOKE PASS"
else
    echo "SMOKE FAIL"
    exit 1
fi
