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

FROM node:22-alpine AS build
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@10.14.0 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# nginx-unprivileged ships pre-hardened for non-root K8s pods (listens on
# 8080, master process already runs as an unprivileged uid) — avoids
# hand-rolling the chown/USER dance stock nginx:alpine needs for that.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080
