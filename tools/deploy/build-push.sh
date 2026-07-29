#!/usr/bin/env bash
# Build (and optionally push) expert-work images to Aliyun ACR
# (registry.cn-hangzhou.aliyuncs.com/expert-work namespace).
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
# Usage:
#   tools/deploy/build-push.sh [--images control-plane,admin-ui] [--tag <tag>] [--push|--no-push]
#
# Examples:
#   tools/deploy/build-push.sh --no-push                  # build only, both images
#   tools/deploy/build-push.sh --images admin-ui --push    # build + push admin-ui only
#   tools/deploy/build-push.sh --tag v1.2.3 --push          # explicit tag, both images

set -euo pipefail

readonly REGISTRY="registry.cn-hangzhou.aliyuncs.com"
readonly NAMESPACE="expert-work"
readonly DEFAULT_IMAGES="control-plane,admin-ui"

#: repo root — tools/deploy/build-push.sh → two parents up.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT

images="${DEFAULT_IMAGES}"
tag=""
push=0

usage() {
    cat >&2 <<EOF
Usage: $0 [--images control-plane,admin-ui] [--tag <tag>] [--push|--no-push]

Builds (and optionally pushes) expert-work images to Aliyun ACR.

Options:
  --images <list>   comma-separated subset of: control-plane,admin-ui (default: both)
  --tag <tag>       image tag (default: \$(git rev-parse --short HEAD))
  --push            push after building (requires 'docker login ${REGISTRY}')
  --no-push         build only, skip push (default)
EOF
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --images)
            images="$2"
            shift 2
            ;;
        --tag)
            tag="$2"
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
    local repo="${REGISTRY}/${NAMESPACE}/${name}"

    echo "==> Building ${repo}:${tag} (+ :latest)"
    docker build \
        -f "${dockerfile}" \
        -t "${repo}:${tag}" \
        -t "${repo}:latest" \
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
            build_image "admin-ui" "${REPO_ROOT}/apps/admin-ui/Dockerfile" "${REPO_ROOT}/apps/admin-ui"
            ;;
        *)
            echo "Unknown image: ${image} (expected: control-plane, admin-ui)" >&2
            exit 2
            ;;
    esac
done

echo
echo "==> Done. tag=${tag} push=${push}"
