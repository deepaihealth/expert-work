# apps/admin-ui/Dockerfile — static SPA image for Kubernetes.
#
# Two stages: build the Vite production bundle with Node/pnpm, then serve it
# from a hardened, non-root nginx image. The built SPA calls `/v1` as a
# relative path (see vite.config.ts's dev-only `/v1` proxy) — in the cluster
# the Ingress path-routes `/` to this image and `/v1` to control-plane on the
# same domain, so this image never needs to reverse-proxy anything itself.
#
# admin-ui is not part of a pnpm workspace (no root pnpm-workspace.yaml
# references it — it ships its own pnpm-lock.yaml), so build with this
# directory as the context:
#   docker build -f apps/admin-ui/Dockerfile -t expert-work-admin-ui apps/admin-ui

# --platform=$BUILDPLATFORM: the build stage's output is pure static
# files (no native binaries reach the final image), so it always runs on
# the build machine's native arch — cross-building for amd64 from an
# Apple-Silicon host skips QEMU emulation for the whole pnpm/Vite step.
FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/node:22-alpine AS build
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@10.14.0 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

# Baked into the built bundle via Vite's import.meta.env substitution
# (src/auth/oidc.ts reads these three at runtime, from the static
# replacement Vite performs at build time — there's no server to read a
# real env var from once this becomes static files behind nginx). Empty
# defaults keep the image buildable with no OIDC config at all, in which
# case admin-ui falls back to its token-paste login path.
ARG VITE_OIDC_ISSUER=""
ARG VITE_OIDC_CLIENT_ID=""
ARG VITE_OIDC_AUDIENCE=""
# W2-PR3 — Langfuse UI origin for the debug console's per-turn deep link
# (config/env.ts readLangfuseBaseUrl → buildLangfuseTraceUrl). Empty =
# the link stays hidden, same fallback shape as the OIDC trio.
ARG VITE_LANGFUSE_BASE_URL=""
ENV VITE_OIDC_ISSUER=${VITE_OIDC_ISSUER} \
    VITE_OIDC_CLIENT_ID=${VITE_OIDC_CLIENT_ID} \
    VITE_OIDC_AUDIENCE=${VITE_OIDC_AUDIENCE} \
    VITE_LANGFUSE_BASE_URL=${VITE_LANGFUSE_BASE_URL}

RUN pnpm build

# 公开 API 文档站(spec 2026-08-11):独立小站,产物挂到 nginx /docs/。
RUN cd docs-site && pnpm install --frozen-lockfile && pnpm build

# nginx-unprivileged ships pre-hardened for non-root K8s pods (listens on
# 8080, master process already runs as an unprivileged uid) — avoids
# hand-rolling the chown/USER dance stock nginx:alpine needs for that.
FROM public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=build /app/docs-site/.vitepress/dist /usr/share/nginx/html/docs
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080
