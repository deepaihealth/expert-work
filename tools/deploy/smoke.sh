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
    echo "Usage: $0 <env>   (env: test | prod)" >&2
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
        # Domains come from the params file — same rationale as release.sh:
        # git stays free of prod real values.
        KUBECONFIG_PATH="${HOME}/.kube/expert-work-prod.yaml"
        PARAMS_FILE="${HOME}/.kube/expert-work-prod-params.env"
        if [[ ! -f "${KUBECONFIG_PATH}" || ! -f "${PARAMS_FILE}" ]]; then
            echo "prod prerequisites missing — see docs/runbooks/production-release.md:" >&2
            echo "  ${KUBECONFIG_PATH}" >&2
            echo "  ${PARAMS_FILE}  (PROD_DOMAIN= / PROD_LANGFUSE_DOMAIN=)" >&2
            exit 1
        fi
        # Extract only the two keys instead of sourcing (same rationale as
        # release.sh — a stray assignment must not repoint anything).
        PROD_DOMAIN="$(grep -E '^PROD_DOMAIN=' "${PARAMS_FILE}" | tail -1 | cut -d= -f2-)"
        PROD_LANGFUSE_DOMAIN="$(grep -E '^PROD_LANGFUSE_DOMAIN=' "${PARAMS_FILE}" | tail -1 | cut -d= -f2-)"
        if [[ -z "${PROD_DOMAIN}" || -z "${PROD_LANGFUSE_DOMAIN}" ]]; then
            echo "PROD_DOMAIN / PROD_LANGFUSE_DOMAIN not set in ${PARAMS_FILE}." >&2
            exit 1
        fi
        PUBLIC_BASE="https://${PROD_DOMAIN}"
        LANGFUSE_BASE="https://${PROD_LANGFUSE_DOMAIN}"
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

# ``release.sh`` calls this the instant the last rollout reports done, and at
# that instant the cluster is legitimately still converging: the old pods are
# Terminating and Prometheus has not re-scraped the new ones yet. Neither is a
# failure, but both used to be reported as one — the 2026-08-13 and 2026-08-14
# releases each needed three runs before a green one, and "run it again" is not
# a smoke test, it is a coin flip.
#
# So the two transient checks get a bounded settle. Bounded is the point: a pod
# stuck Terminating for two minutes IS a real failure and must still be
# reported, so the deadline expires into the normal FAIL path rather than
# looping forever.
settle() {
    local name="$1" want="$2" budget_s="$3"
    shift 3
    local got deadline=$((SECONDS + budget_s))
    while :; do
        got="$("$@")"
        [[ "${got}" == "${want}" ]] && break
        ((SECONDS >= deadline)) && break
        sleep 3
    done
    check "${name}" "${got}" "${want}"
}

pods_not_ready() {
    local out
    out="$(kubectl -n expert-work get pods --no-headers \
        | awk '$3 != "Running" && $3 != "Completed" {print $1"("$3")"}' | paste -sd, - || true)"
    echo "${out:-none}"
}

echo "== pods =="
settle "all pods Running/Completed" "none" 120 pods_not_ready

# Pick a pod that can actually be exec'd into. ``items[0]`` alone picks
# whatever comes first, which right after a rollout is routinely the OLD pod
# mid-Terminating or an already-Succeeded one — ``kubectl exec`` then fails
# with "cannot exec into a container in a completed pod" and the smoke reports
# a phantom failure. Seen twice on the 2026-08-13 release; the third run passed
# only because the terminating pod had finally gone away.
#
# ``--field-selector status.phase=Running`` drops Succeeded/Failed but NOT a
# Terminating pod (it keeps phase=Running while its deletionTimestamp ticks
# down), so also require Ready=True and an empty deletionTimestamp.
#
# Both extra conditions are filtered in awk rather than in the jsonpath:
# kubectl's jsonpath has no negation — `items[?(!@.metadata.deletionTimestamp)]`
# fails outright with "unrecognized character in action: U+0021 '!'". Printing
# the fields with a `|` separator and testing them in awk sidesteps that, and
# the separator matters: an absent deletionTimestamp prints as empty, which
# whitespace-splitting awk would silently collapse into the wrong column.
POD="$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}|{.metadata.deletionTimestamp}|{.metadata.name}{"\n"}{end}' \
    | awk -F'|' '$1 == "True" && $2 == "" {print $3; exit}')"
if [[ -z "${POD}" ]]; then
    echo "FAIL no Running+Ready control-plane pod to probe from" >&2
    exit 1
fi

echo "== http (via ${POD}) =="
# One python invocation, one line per probe: "<name> <status-or-error>".
run_probes() { kubectl -n expert-work exec "${POD}" -- python -c "
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
"; }
probes="$(run_probes)"

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

all_up() {
    local t="$1"
    [[ "${t}" == */* && "${t%%/*}" == "${t##*/}" && "${t%%/*}" != "0" ]]
}

# Prometheus finds the new pods through Kubernetes SD and needs a scrape
# interval before they report up, so right after a rollout this legitimately
# reads 2/3. Re-probe until it settles. Re-running the whole blob rather than
# just this one target keeps it to a single ``kubectl exec`` per attempt; the
# probes are read-only, so repeating them costs nothing but a round trip.
targets="$(get prom_targets)"
prom_deadline=$((SECONDS + 90))
while ! all_up "${targets}" && ((SECONDS < prom_deadline)); do
    sleep 5
    probes="$(run_probes)"
    targets="$(get prom_targets)"
done
if all_up "${targets}"; then
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
