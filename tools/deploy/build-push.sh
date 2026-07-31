#!/usr/bin/env bash
# Build (and optionally push) expert-work images to Aliyun ACR
# (crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work namespace).
#
# Two images:
#   control-plane  services/control-plane/Dockerfile, context = repo root
#                  (the uv workspace build needs packages/ + services/).
#   admin-ui       apps/admin-ui/Dockerfile, context = apps/admin-ui
#                  (not part of the pnpm workspace — see that Dockerfile's
#                  own header comment).
#
# Each image gets two tags — <sha> and latest — matching the CI double-tag
# convention for the sandbox image (.github/workflows/sandbox-image.yml).
#
# admin-ui OIDC build args (--oidc-issuer / --oidc-client-id /
# --oidc-audience, or the matching VITE_OIDC_* env vars): apps/admin-ui/
# Dockerfile bakes these into the static bundle via Vite's build-time
# import.meta.env substitution — there's no server-side env read once
# this becomes nginx-served static files. That means an admin-ui image
# built WITH these is ENVIRONMENT-SPECIFIC (only that Keycloak realm's
# users can OIDC-login through it) — tag it with an environment suffix
# (e.g. `<sha>-test`), not the bare sha, so it can't be mistaken for the
# generic image. Built WITHOUT them (the default), the image is generic
# and falls back to token-paste login (no OIDC configured).
#
# Usage:
#   tools/deploy/build-push.sh [--images control-plane,admin-ui] [--tag <tag>] [--push|--no-push] \
#       [--oidc-issuer <url>] [--oidc-client-id <id>] [--oidc-audience <aud>]
#
# Examples:
#   tools/deploy/build-push.sh --no-push                  # build only, both images
#   tools/deploy/build-push.sh --images admin-ui --push    # build + push admin-ui only
#   tools/deploy/build-push.sh --tag v1.2.3 --push          # explicit tag, both images
#   tools/deploy/build-push.sh --images admin-ui --tag abc123-test --push \
#       --oidc-issuer https://expert-work-test.deepaihealth.com/kc/realms/expert-work \
#       --oidc-client-id expert-work-admin-ui                # environment-specific admin-ui image

set -euo pipefail

readonly REGISTRY="crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com"
readonly NAMESPACE="expert-work"
readonly DEFAULT_IMAGES="control-plane,admin-ui"

#: repo root — tools/deploy/build-push.sh → two parents up.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT

images="${DEFAULT_IMAGES}"
tag=""
push=0
# admin-ui OIDC build args — default from env (VITE_OIDC_* passthrough),
# overridable per-invocation via the matching --oidc-* flag below. Empty
# (the default either way) means "build the generic image" — see the
# admin-ui OIDC paragraph in this file's header comment.
oidc_issuer="${VITE_OIDC_ISSUER:-}"
oidc_client_id="${VITE_OIDC_CLIENT_ID:-}"
oidc_audience="${VITE_OIDC_AUDIENCE:-}"
# W2-PR3 — Langfuse UI origin for the debug console deep link; baked at
# build time like the OIDC trio (env-specific image when set).
langfuse_base_url="${VITE_LANGFUSE_BASE_URL:-}"

usage() {
    cat >&2 <<EOF
Usage: $0 [--images control-plane,admin-ui] [--tag <tag>] [--push|--no-push] \\
    [--oidc-issuer <url>] [--oidc-client-id <id>] [--oidc-audience <aud>]

Builds (and optionally pushes) expert-work images to Aliyun ACR.

Options:
  --images <list>       comma-separated subset of: control-plane,admin-ui (default: both)
  --tag <tag>           image tag (default: \$(git rev-parse --short HEAD))
  --push                push after building (requires 'docker login ${REGISTRY}')
  --no-push             build only, skip push (default)
  --oidc-issuer <url>   admin-ui build arg VITE_OIDC_ISSUER (default: \$VITE_OIDC_ISSUER, else unset)
  --oidc-client-id <id> admin-ui build arg VITE_OIDC_CLIENT_ID (default: \$VITE_OIDC_CLIENT_ID, else unset)
  --oidc-audience <aud> admin-ui build arg VITE_OIDC_AUDIENCE (default: \$VITE_OIDC_AUDIENCE, else unset)
  --langfuse-base-url <url> admin-ui build arg VITE_LANGFUSE_BASE_URL (default: \$VITE_LANGFUSE_BASE_URL, else unset)

Any --oidc-* / --langfuse-* value set makes the built admin-ui image
environment-specific — see the admin-ui OIDC paragraph in this file's
header comment.
EOF
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --images)
            [[ $# -ge 2 && -n "$2" ]] || usage
            images="$2"
            shift 2
            ;;
        --tag)
            [[ $# -ge 2 && -n "$2" ]] || usage
            tag="$2"
            shift 2
            ;;
        --oidc-issuer)
            [[ $# -ge 2 && -n "$2" ]] || usage
            oidc_issuer="$2"
            shift 2
            ;;
        --oidc-client-id)
            [[ $# -ge 2 && -n "$2" ]] || usage
            oidc_client_id="$2"
            shift 2
            ;;
        --oidc-audience)
            [[ $# -ge 2 && -n "$2" ]] || usage
            oidc_audience="$2"
            shift 2
            ;;
        --langfuse-base-url)
            [[ $# -ge 2 && -n "$2" ]] || usage
            langfuse_base_url="$2"
            shift 2
            ;;
        --push)
            push=1
            shift
            ;;
        --no-push)
            push=0
            shift
            ;;
        -h | --help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [[ -z "${tag}" ]]; then
    tag="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
fi
readonly tag

# Refuses to push without a registry session — a build that succeeds but
# fails to push halfway through leaves a dangling :latest on the remote.
check_login() {
    local registry="$1"
    local config="${DOCKER_CONFIG:-${HOME}/.docker}/config.json"
    if [[ -f "${config}" ]] && grep -q "\"${registry}\"" "${config}"; then
        return 0
    fi
    echo "Not logged in to ${registry}." >&2
    echo "Run: docker login ${registry}" >&2
    exit 1
}

build_image() {
    local name="$1" dockerfile="$2" context="$3"
    shift 3
    # Remaining args ("$@", may be empty — e.g. --build-arg pairs) go
    # between the tags and the context.
    local repo="${REGISTRY}/${NAMESPACE}/${name}"

    echo "==> Building ${repo}:${tag} (+ :latest)"
    # --platform is pinned: the ACS cluster nodes are linux/amd64, and a
    # default build on an Apple-Silicon dev machine produces arm64 images
    # the cluster rejects with "no match for platform in manifest".
    docker build \
        --platform linux/amd64 \
        -f "${dockerfile}" \
        -t "${repo}:${tag}" \
        -t "${repo}:latest" \
        "$@" \
        "${context}"

    if [[ "${push}" -eq 1 ]]; then
        echo "==> Pushing ${repo}:${tag}"
        docker push "${repo}:${tag}"
        echo "==> Pushing ${repo}:latest"
        docker push "${repo}:latest"
    fi
}

if [[ "${push}" -eq 1 ]]; then
    check_login "${REGISTRY}"
fi

IFS=',' read -ra selected <<<"${images}"
for image in "${selected[@]}"; do
    case "${image}" in
        control-plane)
            build_image "control-plane" "${REPO_ROOT}/services/control-plane/Dockerfile" "${REPO_ROOT}"
            ;;
        admin-ui)
            admin_ui_build_args=()
            [[ -n "${oidc_issuer}" ]] && admin_ui_build_args+=(--build-arg "VITE_OIDC_ISSUER=${oidc_issuer}")
            [[ -n "${oidc_client_id}" ]] && admin_ui_build_args+=(--build-arg "VITE_OIDC_CLIENT_ID=${oidc_client_id}")
            [[ -n "${oidc_audience}" ]] && admin_ui_build_args+=(--build-arg "VITE_OIDC_AUDIENCE=${oidc_audience}")
            [[ -n "${langfuse_base_url}" ]] && admin_ui_build_args+=(--build-arg "VITE_LANGFUSE_BASE_URL=${langfuse_base_url}")
            if [[ ${#admin_ui_build_args[@]} -gt 0 ]]; then
                build_image "admin-ui" "${REPO_ROOT}/apps/admin-ui/Dockerfile" "${REPO_ROOT}/apps/admin-ui" \
                    "${admin_ui_build_args[@]}"
            else
                build_image "admin-ui" "${REPO_ROOT}/apps/admin-ui/Dockerfile" "${REPO_ROOT}/apps/admin-ui"
            fi
            ;;
        *)
            echo "Unknown image: ${image} (expected: control-plane, admin-ui)" >&2
            exit 2
            ;;
    esac
done

echo
echo "==> Done. tag=${tag} push=${push}"
