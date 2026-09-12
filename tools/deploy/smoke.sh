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
        PROD_DOMAIN="$(grep -E '^PROD_DOMAIN=' "${PARAMS_FILE}" | tail -1 | cut -d= -f2- || true)"
        PROD_LANGFUSE_DOMAIN="$(grep -E '^PROD_LANGFUSE_DOMAIN=' "${PARAMS_FILE}" | tail -1 | cut -d= -f2- || true)"
        PROD_DOMAIN="${PROD_DOMAIN%\"}"; PROD_DOMAIN="${PROD_DOMAIN#\"}"
        PROD_LANGFUSE_DOMAIN="${PROD_LANGFUSE_DOMAIN%\"}"; PROD_LANGFUSE_DOMAIN="${PROD_LANGFUSE_DOMAIN#\"}"
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

# 多副本首发一致性(PROD-1 #1312 / PROD-12 #1315)。三个硬约束:
#   1. spec.replicas >= 2 —— 有人把副本调回 1(或把删掉的单副本 patch 加
#      回来)时 smoke 变红,而不是静默退化;
#   2. ready == spec —— 两个副本都真的在服;
#   3. EXPERT_WORK_REPLICA_COUNT == spec.replicas —— RPM 除法的分母跟真实
#      副本数漂移时,总出量会超厂商额度(env 偏小)或每副本被饿(偏大)。
# 真正的跨副本 /events attach(非属主副本轮询 run_event 兜底)需要带凭据
# 建 run,超出本脚本"只读、无凭据"的边界 —— 归发布后的验收探针;这里的
# per-replica 探活证明每个副本都能独立服 HTTP,是 attach 落到任一副本都
# 不 404 的 smoke 层前提。
echo "== replicas =="
spec_replicas="$(kubectl -n expert-work get deploy control-plane -o jsonpath='{.spec.replicas}')"
ready_replicas="$(kubectl -n expert-work get deploy control-plane -o jsonpath='{.status.readyReplicas}')"
if [[ "${spec_replicas}" =~ ^[0-9]+$ ]] && ((spec_replicas >= 2)); then
    echo "OK   control-plane spec.replicas >= 2 (${spec_replicas})"
else
    echo "FAIL control-plane spec.replicas: got '${spec_replicas}', want >= 2"
    fail=1
fi
check "control-plane ready replicas" "${ready_replicas}" "${spec_replicas}"
env_replicas="$(kubectl -n expert-work exec "${POD}" -- printenv EXPERT_WORK_REPLICA_COUNT \
    || echo MISSING)"
check "EXPERT_WORK_REPLICA_COUNT matches spec.replicas" "${env_replicas}" "${spec_replicas}"

# credential-proxy 必须与 control-plane 同 tag。2026-09-07 勘误:此前它不在
# release.sh 里,test/prod overlay 手工钉死 94f1a371(2026-08-04 构建),
# B-31 ② 的 /admin 闸(#1399)合了 30 多小时没进过任何环境 —— 集群里 GET
# /admin/allowlist 无 token 返 405 而不是 503。两边 tag 不等 = 有人又把它
# 从发布路径上摘了,红在这里而不是等下次侦察。
echo "== images =="
cp_tag="$(kubectl -n expert-work get deploy control-plane \
    -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://')"
proxy_tag="$(kubectl -n expert-work get deploy credential-proxy \
    -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://')"
check "credential-proxy tag matches control-plane" "${proxy_tag}" "${cp_tag}"

# B-57 —— 沙箱镜像的 tag 手工钉在 infra/k8s/sandbox/sandboxset.yaml,**不在
# release.sh 的 apply 范围里**(SandboxSet 在 default namespace,不在 kustomize
# overlay 内)。它有一条文档化的手工发布路径
# (docs/runbooks/sandbox-image-release.md),但**没有任何东西在「镜像源码变了
# 而钉子没动」时说一句话** —— 于是那条手工步骤静默烂掉:2026-09-12 实测钉子停
# 在 63a3109f(2026-08-09),34 天里 infra/sandbox-image/ 改过 4 次,两次 pypdf
# 升级和 #1402(基础镜像改从 ECR Public 拉)从没进过集群。
#
# 这里**只 WARN 不 FAIL**,是有意的:smoke 非零会让 release.sh 走 EXIT trap
# 建议回滚,那等于拿一个好的 control-plane 发版去赔一个无关的镜像滞后 ——
# B-56 记的正是这种「叫你回滚一个健康发布」的坏信号。镜像滞后要人去走 runbook,
# 不是要人回滚。
echo "== sandbox image pin (B-57, warn-only) =="
sandboxset="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/infra/k8s/sandbox/sandboxset.yaml"
pinned_tag="$(sed -n 's|.*expert-work/sandbox:\([A-Za-z0-9._-]*\).*|\1|p' "${sandboxset}" | head -1)"
if [[ -z "${pinned_tag}" ]]; then
    echo "WARN sandbox pin: 在 ${sandboxset} 里没解析出 tag(格式变了?)"
elif ! git -C "$(dirname "${sandboxset}")" rev-parse --verify --quiet "${pinned_tag}^{commit}" >/dev/null; then
    # 钉的 tag 不是本仓库的 commit(手动构建 / 已被 GC),无从比对提交数。
    echo "WARN sandbox pin: ${pinned_tag} 不是本仓库的 commit,无法判断是否滞后"
else
    behind="$(git -C "$(dirname "${sandboxset}")" rev-list --count \
        "${pinned_tag}..HEAD" -- ../../sandbox-image 2>/dev/null || echo "?")"
    if [[ "${behind}" == "0" ]]; then
        echo "OK   sandbox image pin is current (${pinned_tag})"
    else
        echo "WARN sandbox image pin ${pinned_tag} 落后 ${behind} 个提交 —— infra/sandbox-image/ 改过但钉子没动。"
        echo "     受影响的提交:"
        git -C "$(dirname "${sandboxset}")" log --oneline --no-decorate \
            "${pinned_tag}..HEAD" -- ../../sandbox-image 2>/dev/null | sed 's/^/       /'
        echo "     发布这些改动要走 docs/runbooks/sandbox-image-release.md(独立手动路径,"
        echo "     release.sh 不碰它);不发也可以,但要知道集群跑的不是 main 上的镜像。"
    fi
fi

# Same Ready+not-Terminating filter as the POD pick above — probing a
# Terminating pod's IP is a phantom failure, not a finding.
POD_ROWS="$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}|{.metadata.deletionTimestamp}|{.metadata.name}|{.status.podIP}{"\n"}{end}' \
    | awk -F'|' '$1 == "True" && $2 == "" {print $3"|"$4}')"
for row in ${POD_ROWS}; do
    replica_name="${row%%|*}"
    replica_ip="${row##*|}"
    got="$(kubectl -n expert-work exec "${POD}" -- python -c "
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen('http://${replica_ip}:8000/healthz/ready', timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print(type(e).__name__)
")"
    check "replica ${replica_name} /healthz/ready" "${got}" "200"
done

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
