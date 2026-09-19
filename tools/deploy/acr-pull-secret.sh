#!/usr/bin/env bash
# Create/refresh the `acr-pull` image-pull Secret in both namespaces that need it.
#
#   tools/deploy/acr-pull-secret.sh [--dry-run]
#
# Prompts for the ACR credentials (username = the Aliyun account's full name,
# password = ACR personal edition "访问凭证 → 固定密码", see
# docs/runbooks/workstation-setup.md §2) and writes one Secret per namespace:
#
#   expert-work   platform Deployments pull through the namespace's default
#                 ServiceAccount, which carries imagePullSecrets: acr-pull.
#   default       the SandboxSet lives there (infra/k8s/sandbox/sandboxset.yaml
#                 header explains why) and a Secret cannot be referenced across
#                 namespaces, so it needs its own copy.
#
# WHY THIS IS A SCRIPT AND NOT `kubectl create secret docker-registry`
# --------------------------------------------------------------------
# That command takes exactly one --docker-server, and we need TWO hosts in one
# Secret. A dockerconfigjson is a map keyed BY REGISTRY HOST: the kubelet looks
# up the host of the image reference it is about to pull and uses only that
# entry. There is no wildcard and no fallback — a host with no entry is an
# anonymous pull, which against a private ACR fails as
# `insufficient_scope: authorization failed` (and, worse, the SandboxSet then
# just sits at availableReplicas 0 rather than surfacing an error).
#
# Two hosts, same registry, same credentials:
#
#   crpi-<id>.cn-hangzhou.personal.cr.aliyuncs.com       public internet
#   crpi-<id>-vpc.cn-hangzhou.personal.cr.aliyuncs.com   VPC-internal
#
# B-55 moved the sandbox image reference to the -vpc host (measured: the same
# 619MB image pulls in 112s over the public endpoint and 66s over the VPC one,
# repeatedly). Everything else still references the public host. Both must
# therefore work at the same time, from one Secret — and the public entry is
# also what makes rolling the sandbox image reference back to the public host a
# one-line manifest edit rather than a credential incident.
#
# Credentials never reach argv (`ps` is world-readable on a shared workstation)
# and never reach a file on disk: the password is read with echo off, passed to
# python through the environment, and the rendered dockerconfigjson is piped
# straight into kubectl on stdin.
set -euo pipefail

readonly ACR_ID="crpi-sgadimluo7wm655m"
readonly ACR_REGION="cn-hangzhou"
readonly PUBLIC_HOST="${ACR_ID}.${ACR_REGION}.personal.cr.aliyuncs.com"
readonly VPC_HOST="${ACR_ID}-vpc.${ACR_REGION}.personal.cr.aliyuncs.com"
# Exported here rather than prefixed onto the `python3` call below: these are
# `readonly`, and a `VAR=x cmd` prefix is an assignment — bash rejects it with
# "readonly variable", `render` then emits nothing, and kubectl fails on empty
# JSON. (Found by running this script, not by reading it.)
export ACR_ID ACR_REGION PUBLIC_HOST VPC_HOST
readonly NAMESPACES=(expert-work default)

dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then
    dry_run=1
    shift
fi
if [[ $# -gt 0 ]]; then
    echo "unexpected argument: $1" >&2
    echo "usage: tools/deploy/acr-pull-secret.sh [--dry-run]" >&2
    exit 2
fi

if [[ -z "${KUBECONFIG:-}" ]]; then
    echo "KUBECONFIG is not set — refusing to guess which cluster you mean." >&2
    echo "  export KUBECONFIG=~/.kube/expert-work-test.yaml   # or -prod.yaml" >&2
    exit 2
fi
echo "cluster: $(kubectl config current-context)"
echo "hosts:   ${PUBLIC_HOST}"
echo "         ${VPC_HOST}"

# `read -p` is a bashism zsh spells differently, and this file is run by hand
# from whichever shell the operator has. printf + bare `read -r` works in both.
#
# The `-t 0` split is not politeness: `stty` needs a terminal and exits
# non-zero without one, which under `set -e` kills the script. Guarding on it
# keeps the password hidden where there IS a terminal (the real invocation)
# while leaving the script runnable with both values piped in — which is the
# only way its write path can be exercised before it is pointed at production.
if [[ -t 0 ]]; then
    printf '阿里云账号全名: '
    read -r ACR_USERNAME
    printf 'ACR 固定密码 (输入不回显): '
    stty -echo
    read -r ACR_PASSWORD
    stty echo
    printf '\n'
else
    read -r ACR_USERNAME
    read -r ACR_PASSWORD
fi
if [[ -z "${ACR_USERNAME}" || -z "${ACR_PASSWORD}" ]]; then
    echo "username and password are both required" >&2
    exit 2
fi
export ACR_USERNAME ACR_PASSWORD

render() {
    python3 - <<'PY'
import base64
import json
import os

user = os.environ["ACR_USERNAME"]
password = os.environ["ACR_PASSWORD"]
auth = base64.b64encode(f"{user}:{password}".encode()).decode()
# username/password alongside `auth` is redundant but is what `kubectl create
# secret docker-registry` emits; keeping the same shape means a Secret made by
# this script and one made by that command are byte-comparable.
entry = {"username": user, "password": password, "auth": auth}
hosts = [os.environ["PUBLIC_HOST"], os.environ["VPC_HOST"]]
print(json.dumps({"auths": {host: entry for host in hosts}}))
PY
}

for ns in "${NAMESPACES[@]}"; do
    if [[ "${dry_run}" == "1" ]]; then
        echo "DRY-RUN would write Secret acr-pull in namespace ${ns} with hosts:"
        render | python3 -c 'import json,sys; print("   ", *sorted(json.load(sys.stdin)["auths"]))'
        continue
    fi
    # --dry-run=client -o yaml | kubectl apply, rather than `create`, so that
    # re-running this on a cluster that already has the Secret refreshes it
    # instead of failing with AlreadyExists. `apply` also keeps it idempotent
    # when only one of the two namespaces was provisioned.
    render | kubectl -n "${ns}" create secret generic acr-pull \
        --type=kubernetes.io/dockerconfigjson \
        --from-file=.dockerconfigjson=/dev/stdin \
        --dry-run=client -o yaml \
        | kubectl apply -f - >/dev/null
    printf 'ok  %s/acr-pull  (%s)\n' "${ns}" "$(
        kubectl -n "${ns}" get secret acr-pull \
            -o jsonpath='{.data.\.dockerconfigjson}' \
            | base64 -d \
            | python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin)["auths"])))'
    )"
done

unset ACR_USERNAME ACR_PASSWORD

cat <<'NOTE'

The expert-work namespace's default ServiceAccount must reference it (one-time):

  kubectl -n expert-work patch serviceaccount default \
    -p '{"imagePullSecrets":[{"name":"acr-pull"}]}'
NOTE
