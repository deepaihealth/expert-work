#!/usr/bin/env bash
# Build (and optionally push) expert-work images to Aliyun ACR
# (crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work namespace).
#
# Two images:
#   control-plane  services/control-plane/Dockerfile, context = repo root
#                  (the uv workspace build needs packages/ + services/).
#   admin-ui       apps/admin-ui/Dockerfile, context = apps/admin-ui
#   credential-proxy  services/credential-proxy/Dockerfile, context = repo root
#                  (2026-09-07 加入:此前不在这里,test/prod overlay 手工钉死
#                  94f1a371,B-31 ② #1399 合了 30 多小时没进过任何环境)
#                  (not part of the pnpm workspace — see that Dockerfile's
#                  own header comment).
#
# Each image gets two tags — <sha> and latest — matching the CI double-tag
# convention for the sandbox image (.github/workflows/sandbox-image.yml).
#
# admin-ui OIDC build args (--oidc-issuer / --oidc-client-id /
# --oidc-audience, or the matching VITE_OIDC_* env vars — plus
# --langfuse-base-url / VITE_LANGFUSE_BASE_URL for the debug-console
# Langfuse deep link, same bake-at-build mechanics): apps/admin-ui/
# Dockerfile bakes these into the static bundle via Vite's build-time
# import.meta.env substitution — there's no server-side env read once
# this becomes nginx-served static files. That means an admin-ui image
# built WITH these is ENVIRONMENT-SPECIFIC (only that Keycloak realm's
# users can OIDC-login through it) — tag it with an environment suffix
# (e.g. `<sha>-test`), not the bare sha, so it can't be mistaken for the
# generic image. Built WITHOUT them (the default), the image is generic
# and falls back to token-paste login (no OIDC configured).
#
# Mirrors are picked per build (tools/deploy/pick_mirror.py, run in the
# builder base image so it sees the build's network path): the Aliyun default
# is kept while it serves >= 1 MiB/s — normal builds unchanged, layer cache
# intact — and only a slow default makes the build probe the other mirrors
# and take the fastest. Setting either variable below skips its probe.
#
# PyPI mirror (EXPERT_WORK_PYPI_MIRROR): control-plane and credential-proxy
# get `--build-arg PYPI_MIRROR=<url>`, defaulting to the Aliyun mirror
# https://mirrors.aliyun.com/pypi/ — 2026-10-16 files.pythonhosted.org ran at
# ~65 KB/s from this network and cost the release close to an hour. The
# Dockerfiles rewrite the file URLs in their copy of uv.lock to that prefix
# (the committed uv.lock stays on pypi.org; hashes are still checked against
# it) — see the comment above `uv sync` in services/control-plane/Dockerfile.
# Set it to the empty string (`EXPERT_WORK_PYPI_MIRROR= tools/deploy/...`) to
# download from pypi.org instead, e.g. when the mirror lags behind a version
# bump and 404s. admin-ui has no Python packages and never gets the arg.
#
# Debian mirror (EXPERT_WORK_APT_MIRROR): control-plane's builder stage
# apt-installs git from this host (default mirrors.aliyun.com). Set it to
# another mirror host, e.g. `EXPERT_WORK_APT_MIRROR=mirrors.ustc.edu.cn`,
# when that mirror is slow — 2026-10-23 every mirrors.aliyun.com download
# (Debian and PyPI alike) ran at ~30 KB/s from this network; pair it with
# EXPERT_WORK_PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/ in that case.
#
# Usage:
#   tools/deploy/build-push.sh [--images control-plane,admin-ui,credential-proxy] [--tag <tag>] [--push|--no-push] \
#       [--oidc-issuer <url>] [--oidc-client-id <id>] [--oidc-audience <aud>] \
#       [--langfuse-base-url <url>] [--app-version <sha>] [--app-env test|prod] [--build-time <iso8601>]
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
readonly DEFAULT_IMAGES="control-plane,admin-ui,credential-proxy"

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
# "About" dialog (admin-ui config/env.ts readBuildInfo): version = the image
# tag (git short sha), env = release.sh's env_name, build time. Baked like the
# OIDC trio; release.sh passes all three, a bare build-push leaves them empty
# and the dialog says "local dev".
app_version="${VITE_APP_VERSION:-}"
app_env="${VITE_APP_ENV:-}"
build_time="${VITE_BUILD_TIME:-}"
# PyPI mirror for the Python images — see the header comment. `-` not `:-`:
# unset means "probe" (Aliyun kept while fast), set-but-empty means pypi.org.
DEFAULT_PYPI_MIRROR="https://mirrors.aliyun.com/pypi/"
pypi_mirror="${EXPERT_WORK_PYPI_MIRROR-}"
# Debian mirror host for control-plane's builder stage — see the header.
DEFAULT_APT_MIRROR="mirrors.aliyun.com"
apt_mirror="${EXPERT_WORK_APT_MIRROR:-}"
# The probe runs in the builder stage's base image (already pulled for the
# build; it carries the CA bundle the host Python may lack). Keep it equal
# to the `AS builder` FROM of services/control-plane/Dockerfile.
MIRROR_PROBE_IMAGE="ghcr.io/astral-sh/uv:python3.12-bookworm-slim"

usage() {
    cat >&2 <<EOF
Usage: $0 [--images control-plane,admin-ui,credential-proxy] [--tag <tag>] [--push|--no-push] \\
    [--oidc-issuer <url>] [--oidc-client-id <id>] [--oidc-audience <aud>] \\
    [--langfuse-base-url <url>] [--app-version <sha>] [--app-env test|prod] [--build-time <iso8601>]

Builds (and optionally pushes) expert-work images to Aliyun ACR.

Options:
  --images <list>       comma-separated subset of: control-plane,admin-ui,credential-proxy (default: all three)
  --tag <tag>           image tag (default: \$(git rev-parse --short HEAD))
  --push                push after building (requires 'docker login ${REGISTRY}')
  --no-push             build only, skip push (default)
  --oidc-issuer <url>   admin-ui build arg VITE_OIDC_ISSUER (default: \$VITE_OIDC_ISSUER, else unset)
  --oidc-client-id <id> admin-ui build arg VITE_OIDC_CLIENT_ID (default: \$VITE_OIDC_CLIENT_ID, else unset)
  --oidc-audience <aud> admin-ui build arg VITE_OIDC_AUDIENCE (default: \$VITE_OIDC_AUDIENCE, else unset)
  --langfuse-base-url <url> admin-ui build arg VITE_LANGFUSE_BASE_URL (default: \$VITE_LANGFUSE_BASE_URL, else unset)
  --app-version <sha>   admin-ui build arg VITE_APP_VERSION — shown in the About dialog
  --app-env <env>       admin-ui build arg VITE_APP_ENV (test|prod) — shown in the About dialog
  --build-time <iso>    admin-ui build arg VITE_BUILD_TIME — shown in the About dialog

Any --oidc-* / --langfuse-* value set makes the built admin-ui image
environment-specific — see the admin-ui OIDC paragraph in this file's
header comment.

Environment:
  EXPERT_WORK_PYPI_MIRROR  PyPI mirror for control-plane / credential-proxy
                        (unset: probe, Aliyun kept while fast; set to empty for pypi.org)
  EXPERT_WORK_APT_MIRROR   Debian mirror host for control-plane's builder stage
                        (unset: probe, mirrors.aliyun.com kept while fast)
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
        --app-version)
            [[ $# -ge 2 && -n "$2" ]] || usage
            app_version="$2"
            shift 2
            ;;
        --app-env)
            [[ $# -ge 2 && -n "$2" ]] || usage
            app_env="$2"
            shift 2
            ;;
        --build-time)
            [[ $# -ge 2 && -n "$2" ]] || usage
            build_time="$2"
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
        # --platform on push too: with the containerd image store a tag
        # can hold BOTH arch variants, and a bare push picks the HOST
        # arch (arm64 on Apple Silicon) — the cluster then dies with
        # "exec format error" (live lesson, W2-PR3 mirror sweep).
        echo "==> Pushing ${repo}:${tag}"
        docker push --platform linux/amd64 "${repo}:${tag}"
        echo "==> Pushing ${repo}:latest"
        docker push --platform linux/amd64 "${repo}:latest"
    fi
}

if [[ "${push}" -eq 1 ]]; then
    check_login "${REGISTRY}"
fi

IFS=',' read -ra selected <<<"${images}"

# Prints the mirror pick_mirror.py chooses; fails when the probe cannot run.
pick_mirror() {
    docker run --rm --platform linux/amd64 --entrypoint python3 \
        -v "${REPO_ROOT}/tools/deploy/pick_mirror.py:/pick_mirror.py:ro" \
        -v "${REPO_ROOT}/uv.lock:/uv.lock:ro" \
        "${MIRROR_PROBE_IMAGE}" /pick_mirror.py "$1" --lock /uv.lock
}
case ",${images}," in
    *,control-plane,* | *,credential-proxy,*)
        if [[ -z "${EXPERT_WORK_PYPI_MIRROR+x}" ]]; then
            pypi_mirror="$(pick_mirror pypi)" || {
                echo "WARN PyPI mirror probe failed; using ${DEFAULT_PYPI_MIRROR}" >&2
                pypi_mirror="${DEFAULT_PYPI_MIRROR}"
            }
        fi
        ;;
esac
case ",${images}," in
    *,control-plane,*)
        if [[ -z "${apt_mirror}" ]]; then
            apt_mirror="$(pick_mirror apt)" || {
                echo "WARN Debian mirror probe failed; using ${DEFAULT_APT_MIRROR}" >&2
                apt_mirror="${DEFAULT_APT_MIRROR}"
            }
        fi
        ;;
esac

for image in "${selected[@]}"; do
    case "${image}" in
        control-plane)
            build_image "control-plane" "${REPO_ROOT}/services/control-plane/Dockerfile" "${REPO_ROOT}" \
                --build-arg "PYPI_MIRROR=${pypi_mirror}" --build-arg "APT_MIRROR=${apt_mirror}"
            ;;
        admin-ui)
            admin_ui_build_args=()
            [[ -n "${oidc_issuer}" ]] && admin_ui_build_args+=(--build-arg "VITE_OIDC_ISSUER=${oidc_issuer}")
            [[ -n "${oidc_client_id}" ]] && admin_ui_build_args+=(--build-arg "VITE_OIDC_CLIENT_ID=${oidc_client_id}")
            [[ -n "${oidc_audience}" ]] && admin_ui_build_args+=(--build-arg "VITE_OIDC_AUDIENCE=${oidc_audience}")
            [[ -n "${langfuse_base_url}" ]] && admin_ui_build_args+=(--build-arg "VITE_LANGFUSE_BASE_URL=${langfuse_base_url}")
            [[ -n "${app_version}" ]] && admin_ui_build_args+=(--build-arg "VITE_APP_VERSION=${app_version}")
            [[ -n "${app_env}" ]] && admin_ui_build_args+=(--build-arg "VITE_APP_ENV=${app_env}")
            [[ -n "${build_time}" ]] && admin_ui_build_args+=(--build-arg "VITE_BUILD_TIME=${build_time}")
            if [[ ${#admin_ui_build_args[@]} -gt 0 ]]; then
                build_image "admin-ui" "${REPO_ROOT}/apps/admin-ui/Dockerfile" "${REPO_ROOT}/apps/admin-ui" \
                    "${admin_ui_build_args[@]}"
            else
                build_image "admin-ui" "${REPO_ROOT}/apps/admin-ui/Dockerfile" "${REPO_ROOT}/apps/admin-ui"
            fi
            ;;
        credential-proxy)
            # Same workspace build as control-plane (uv --package), context = repo root.
            build_image "credential-proxy" "${REPO_ROOT}/services/credential-proxy/Dockerfile" "${REPO_ROOT}" \
                --build-arg "PYPI_MIRROR=${pypi_mirror}"
            ;;
        *)
            echo "Unknown image: ${image} (expected: control-plane, admin-ui, credential-proxy)" >&2
            exit 2
            ;;
    esac
done

echo
echo "==> Done. tag=${tag} push=${push}"
