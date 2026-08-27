#!/usr/bin/env bash
# One-command release to an expert-work cluster environment (W2 spec §3.3).
#
#   tools/deploy/release.sh test [--tag <sha>] [--images control-plane,admin-ui] [--dry-run]
#
# Steps (in order):
#   1. build + push both images via build-push.sh
#      (control-plane :<tag>; admin-ui :<tag>-test with the env's OIDC +
#      Langfuse build args baked in — see build-push.sh header for why
#      the admin-ui image is environment-specific)
#   2. pin the overlay's `images:` newTags to the fresh tags
#      (kustomize edit; the ACS image cache resolves tags WITHOUT
#      consulting the registry, so every release must mint fresh tags —
#      never re-push an existing one)
#   3. delete the immutable migrate Job (apply would reject the image
#      change otherwise), then `kubectl apply -k` the overlay
#   4. wait for every Deployment rollout
#   5. run smoke.sh
#
# The overlay newTag edit is left UNCOMMITTED on purpose — commit it as
# the `chore(deploy): ... newTag ...` record PR after the release checks
# out (repo convention, see e.g. #1074/#1075).
#
# prod prerequisites live OUTSIDE git (kubeconfig + params file) —
# see docs/runbooks/production-release.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly SCRIPT_DIR REPO_ROOT

usage() {
    cat >&2 <<EOF
Usage: $0 <env> [--tag <sha>] [--images control-plane,admin-ui] [--dry-run] [--yes]

  env         target environment: test | prod
  --tag       image tag basis (default: current git short HEAD)
  --images    subset to build/deploy (default: both)
  --dry-run   print every step without executing anything
  --yes       skip the interactive prod confirmation (CI / scripted use)

prod prerequisites (docs/runbooks/production-release.md):
  ~/.kube/expert-work-prod.yaml          cluster kubeconfig
  ~/.kube/expert-work-prod-params.env    PROD_DOMAIN= / PROD_LANGFUSE_DOMAIN=
  overlay free of PROD_PLACEHOLDER_*     (checked before anything builds)
EOF
    exit 2
}

[[ $# -ge 1 ]] || usage
env_name="$1"
shift

tag=""
images="control-plane,admin-ui"
dry_run=0
assume_yes=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            [[ $# -ge 2 && -n "$2" ]] || usage
            tag="$2"
            shift 2
            ;;
        --images)
            [[ $# -ge 2 && -n "$2" ]] || usage
            images="$2"
            shift 2
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        --yes)
            assume_yes=1
            shift
            ;;
        -h | --help) usage ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

# ---------------------------------------------------------------- env params
case "${env_name}" in
    test)
        KUBECONFIG_PATH="${HOME}/.kube/expert-work-test.yaml"
        OVERLAY="${REPO_ROOT}/infra/k8s/overlays/test"
        OIDC_ISSUER="https://expert-work-test.deepaihealth.com/kc/realms/expert-work"
        OIDC_CLIENT_ID="expert-work-admin-ui"
        OIDC_AUDIENCE="expert-work-api-internal"
        LANGFUSE_BASE_URL="https://langfuse-test.deepaihealth.com"
        ADMIN_UI_TAG_SUFFIX="-test"
        ;;
    prod)
        # PROD-5(2026-08-24)— prod path. Domains are NOT hardcoded here:
        # they live in ~/.kube/expert-work-prod-params.env (same home as the
        # test cluster's params file), so git stays free of prod real values
        # and this script is final before the domain even exists.
        KUBECONFIG_PATH="${HOME}/.kube/expert-work-prod.yaml"
        OVERLAY="${REPO_ROOT}/infra/k8s/overlays/prod"
        PARAMS_FILE="${HOME}/.kube/expert-work-prod-params.env"
        if [[ ! -f "${KUBECONFIG_PATH}" || ! -f "${PARAMS_FILE}" ]]; then
            echo "prod prerequisites missing (kubeconfig and/or params file):" >&2
            echo "  ${KUBECONFIG_PATH}" >&2
            echo "  ${PARAMS_FILE}  (PROD_DOMAIN= / PROD_LANGFUSE_DOMAIN=)" >&2
            echo "see docs/runbooks/production-release.md (开荒清单)." >&2
            exit 1
        fi
        # Extract only the two keys instead of sourcing — a stray
        # OVERLAY=/KUBECONFIG_PATH= line in the params file must not be
        # able to silently repoint the release (review M-9). `|| true`
        # keeps the missing-key case on the friendly error below instead
        # of dying in set -e (review NEW-3); quotes are stripped so a
        # dotenv-style quoted value doesn't get baked into the image URL
        # (review NEW-4).
        PROD_DOMAIN="$(grep -E '^PROD_DOMAIN=' "${PARAMS_FILE}" | tail -1 | cut -d= -f2- || true)"
        PROD_LANGFUSE_DOMAIN="$(grep -E '^PROD_LANGFUSE_DOMAIN=' "${PARAMS_FILE}" | tail -1 | cut -d= -f2- || true)"
        PROD_DOMAIN="${PROD_DOMAIN%\"}"; PROD_DOMAIN="${PROD_DOMAIN#\"}"
        PROD_LANGFUSE_DOMAIN="${PROD_LANGFUSE_DOMAIN%\"}"; PROD_LANGFUSE_DOMAIN="${PROD_LANGFUSE_DOMAIN#\"}"
        if [[ -z "${PROD_DOMAIN}" || -z "${PROD_LANGFUSE_DOMAIN}" ]]; then
            echo "PROD_DOMAIN / PROD_LANGFUSE_DOMAIN not set in ${PARAMS_FILE}." >&2
            exit 1
        fi
        OIDC_ISSUER="https://${PROD_DOMAIN}/kc/realms/expert-work"
        OIDC_CLIENT_ID="expert-work-admin-ui"
        OIDC_AUDIENCE="expert-work-api-internal"
        LANGFUSE_BASE_URL="https://${PROD_LANGFUSE_DOMAIN}"
        ADMIN_UI_TAG_SUFFIX="-prod"
        ;;
    *)
        echo "Unknown env: ${env_name}" >&2
        usage
        ;;
esac
readonly KUBECONFIG_PATH OVERLAY

# ------------------------------------------------------- prod-only guards
if [[ "${env_name}" == "prod" ]]; then
    # Placeholder scan BEFORE the (10-minute) image builds. Scan the
    # RENDERED manifests, not the raw directory — raw text would hit
    # comments and the secrets.env.example template, refusing forever
    # (and its "fill these" output would push real secrets toward git).
    # PROD_PLACEHOLDER_TAG is exempt: step 2 (kustomize edit) replaces
    # it on the first release.
    # Two steps so a kustomize failure trips set -e instead of the
    # `|| true` silently blanking the guard (review NEW-7).
    rendered="$(kustomize build "${OVERLAY}")"
    leftover="$(printf '%s\n' "${rendered}" | grep -n "PROD_PLACEHOLDER" | grep -v "PROD_PLACEHOLDER_TAG" || true)"
    if [[ -n "${leftover}" ]]; then
        echo "prod overlay still renders PROD_PLACEHOLDER_* values — fill them first:" >&2
        echo "${leftover}" >&2
        exit 1
    fi
    if [[ "${dry_run}" -eq 0 && "${assume_yes}" -eq 0 ]]; then
        if [[ ! -t 0 ]]; then
            echo "non-interactive stdin: pass --yes to release to prod." >&2
            exit 1
        fi
        echo "About to release to PRODUCTION (${PROD_DOMAIN})."
        read -r -p "Type 'prod' to continue: " reply
        if [[ "${reply}" != "prod" ]]; then
            echo "aborted." >&2
            exit 1
        fi
    fi
fi

if [[ -z "${tag}" ]]; then
    tag="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
fi
readonly tag
admin_ui_tag="${tag}${ADMIN_UI_TAG_SUFFIX}"
readonly admin_ui_tag

if ! git -C "${REPO_ROOT}" diff --quiet HEAD -- ':!infra/k8s/overlays'; then
    echo "WARNING: working tree has uncommitted changes outside the overlay —" >&2
    echo "the image tag '${tag}' names a commit that does not match what you build." >&2
fi

run() {
    if [[ "${dry_run}" -eq 1 ]]; then
        echo "DRY-RUN> $*"
    else
        echo "==> $*"
        "$@"
    fi
}

readonly ACR="crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work"

# ------------------------------------------------------------- 1. build+push
if [[ ",${images}," == *",admin-ui,"* ]]; then
    # admin-ui gets its env-specific tag in a SEPARATE build-push call so
    # control-plane keeps the bare sha tag.
    if [[ ",${images}," == *",control-plane,"* ]]; then
        run "${SCRIPT_DIR}/build-push.sh" --images control-plane --tag "${tag}" --push
    fi
    run "${SCRIPT_DIR}/build-push.sh" --images admin-ui --tag "${admin_ui_tag}" --push \
        --oidc-issuer "${OIDC_ISSUER}" \
        --oidc-client-id "${OIDC_CLIENT_ID}" \
        --oidc-audience "${OIDC_AUDIENCE}" \
        --langfuse-base-url "${LANGFUSE_BASE_URL}"
else
    run "${SCRIPT_DIR}/build-push.sh" --images "${images}" --tag "${tag}" --push
fi

# ------------------------------------------------------- 2. overlay newTags
# kustomize edit operates on the cwd's kustomization.yaml.
set_new_tag() {
    local image="$1" new_tag="$2"
    if [[ "${dry_run}" -eq 1 ]]; then
        echo "DRY-RUN> kustomize edit set image ${image}=${image}:${new_tag} (in ${OVERLAY})"
    else
        echo "==> newTag ${image}:${new_tag}"
        (cd "${OVERLAY}" && kustomize edit set image "${image}=${image}:${new_tag}")
    fi
}
if [[ ",${images}," == *",control-plane,"* ]]; then
    set_new_tag "${ACR}/control-plane" "${tag}"
fi
if [[ ",${images}," == *",admin-ui,"* ]]; then
    set_new_tag "${ACR}/admin-ui" "${admin_ui_tag}"
fi

# --------------------------------------------------------- 3. migrate+apply
export KUBECONFIG="${KUBECONFIG_PATH}"
run kubectl -n expert-work delete job migrate --ignore-not-found
run kubectl apply -k "${OVERLAY}"

# ------------------------------------------------------------- 4. rollouts
if [[ "${dry_run}" -eq 0 ]]; then
    kubectl -n expert-work wait --for=condition=complete job/migrate --timeout=300s
    for d in $(kubectl -n expert-work get deploy -o name); do
        kubectl -n expert-work rollout status "${d}" --timeout=300s
    done
else
    echo "DRY-RUN> wait job/migrate + rollout status (all deployments)"
fi

# ---------------------------------------------------------------- 5. smoke
run "${SCRIPT_DIR}/smoke.sh" "${env_name}"

# --------------------------------------------------------------- 6. canary
# X-14 P1 — 发布合格判据:一条真实 Agent run(exec_python + write_file +
# save_artifact + 产物下载),end 帧 status=success 才算发布成功。smoke 只探
# HTTP(e2b 事故:九项全绿时沙箱工具可以全挂),canary 补上执行面。凭据来自
# 集群 Secret canary-credentials(python -m control_plane.seed_canary 预置,
# 见 docs/runbooks/production-release.md §1.6);未 seed 时打 WARNING 跳过而
# 不失败 —— 未预置金丝雀的环境发布不能被打断。rollback.sh 不跑 canary
# (救火路径只复用 smoke,不能被真 run 拖住)。
echo
echo "== canary (X-14 P1) =="
if [[ "${dry_run}" -eq 1 ]]; then
    echo "DRY-RUN> read Secret canary-credentials (api-key/agent-code); skip+WARN if absent"
    echo "DRY-RUN> pick a Running+Ready control-plane pod; stream canary.py + key over stdin"
    echo "DRY-RUN> kubectl exec: python canary.py — end.status=success + artifact download gate"
elif ! kubectl -n expert-work get secret canary-credentials > /dev/null 2>&1; then
    echo "WARNING: canary skipped — Secret canary-credentials not found in this cluster." >&2
    echo "  seed it first (docs/runbooks/production-release.md §1.6.7):" >&2
    echo "    kubectl -n expert-work exec -it <control-plane-pod> -- \\" >&2
    echo "      python -m control_plane.seed_canary --tenant-id <tenant uuid>" >&2
else
    # 凭据只进变量与 stdin —— 绝不进 argv(节点 ps / exec 审计可见)、绝不
    # echo。agent-code 非机密,走 argv 便于排障。
    canary_key="$(kubectl -n expert-work get secret canary-credentials \
        -o jsonpath='{.data.api-key}' | base64 -d)"
    canary_agent="$(kubectl -n expert-work get secret canary-credentials \
        -o jsonpath='{.data.agent-code}' | base64 -d || true)"
    if [[ -z "${canary_key}" ]]; then
        echo "canary-credentials Secret exists but key 'api-key' is empty — re-seed it:" >&2
        echo "  python -m control_plane.seed_canary --rotate-key (runbook §1.6.7)" >&2
        exit 1
    fi
    # Same Running+Ready+not-Terminating pod filter as smoke.sh (see the
    # comment there for why items[0] alone picks Terminating corpses).
    CANARY_POD="$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
        --field-selector=status.phase=Running \
        -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}|{.metadata.deletionTimestamp}|{.metadata.name}{"\n"}{end}' \
        | awk -F'|' '$1 == "True" && $2 == "" {print $3; exit}')"
    if [[ -z "${CANARY_POD}" ]]; then
        echo "no Running+Ready control-plane pod to run the canary from." >&2
        exit 1
    fi
    echo "==> canary run via ${CANARY_POD} (agent=${canary_agent:-release-canary})"
    # canary.py 与 key 一起走 stdin(第一行 key,其余是脚本):单次 exec、
    # 无临时文件、无 tar 依赖;in-pod 的 python 即 /app/.venv(httpx 可用)。
    if ! { printf '%s\n' "${canary_key}"; cat "${SCRIPT_DIR}/canary.py"; } \
        | kubectl -n expert-work exec -i "${CANARY_POD}" -- \
            env "EXPERT_WORK_CANARY_AGENT_CODE=${canary_agent:-release-canary}" \
            python -c 'import os, sys
os.environ["EXPERT_WORK_CANARY_API_KEY"] = sys.stdin.readline().rstrip("\n")
exec(compile(sys.stdin.read(), "canary.py", "exec"))'; then
        echo "CANARY FAILED — the release is NOT good. Roll back:" >&2
        echo "  tools/deploy/rollback.sh ${env_name} <上一版 tag>" >&2
        echo "  (上一版 tag 在上一个 chore(deploy) 记录 PR 里 —— 记录 PR 按约定写明它)" >&2
        exit 1
    fi
fi

echo
echo "Release ${tag} done. Overlay newTag edits are uncommitted —"
echo "commit them as the chore(deploy) record PR:"
git -C "${REPO_ROOT}" --no-pager diff --stat -- "${OVERLAY}" || true
