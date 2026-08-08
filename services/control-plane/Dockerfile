# Expert Work control-plane image — Stream I.1.
#
# Reuses the multi-stage uv build pattern established by the
# credential-proxy pilot (Mini-ADR F-15 / I-1). The control-plane runs
# the orchestrator in-process as a library (STREAM-E-DESIGN § 2.6), so
# this single image carries the whole M0 API + execution surface.
#
# The Alembic config + migrations dir are copied in as well: the
# docker-compose ``migrate`` one-shot service reuses this image to run
# ``alembic upgrade head`` (Mini-ADR I-3) — they live outside the
# package ``src/`` tree, so the ``--no-editable`` wheel doesn't carry them.
#
# Build from the repo root:
#   docker build -f services/control-plane/Dockerfile -t expert-work-control-plane .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

# `kruise-agents` (the Agent Sandbox E2B patch — sandbox migration W1) is
# a git dependency: uv.lock pins a GitHub rev rather than a PyPI version,
# because the package has no PyPI release. `uv sync` shells out to the
# git binary to fetch it, and this base image ships without one, so
# without this the build dies at `uv sync` with "Git executable not
# found". It reproduces only in Docker — a dev machine always has git,
# which is why it survived until the first image build.
#
# Builder stage only: by the time stage 2 copies /app/.venv the wheel is
# already built, so the runtime image needs neither git nor network.
#
# Aliyun mirror for the same reason infra/sandbox-image/Dockerfile uses
# one: deb.debian.org 502s partway through from this network.
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=5 \
    && apt-get install -y -o Acquire::Retries=5 --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/ services/
RUN uv sync --frozen --no-dev --no-editable --package expert-work-control-plane


FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Non-root service account — parity with the sandbox / credential-proxy
# image hardening.
#
# 沙箱迁移波 2 BUG-1 —— 与沙箱镜像的 `agent` 用户同 uid(infra/sandbox-image/
# Dockerfile 的 `useradd -u 10000`)。两个进程读写同一棵 NAS 工作区树,uid 不同
# 时 POSIX 直接拒绝:agent 写的文件属主是 10000,本进程读不动,用户在前端列得出
# 却下载报 404。同 uid 之后跨 uid 这件事从根上不存在,工作区目录因此还能收紧到
# 0700(此前是 world-writable 的 0777)。两处 uid 由
# services/orchestrator/tests/test_workspace_shared_uid.py 双向钉住。
RUN useradd --uid 10000 --no-create-home --shell /usr/sbin/nologin expert_work

COPY --from=builder --chown=expert_work:expert_work /app/.venv /app/.venv

# Alembic assets for the compose ``migrate`` service. ``alembic.ini`` has
# ``script_location = %(here)s/migrations``, so this layout resolves to
# /app/alembic/migrations. The migrate service overrides ``entrypoint``
# to ``alembic -c /app/alembic/alembic.ini upgrade head``.
COPY --chown=expert_work:expert_work packages/expert-work-persistence/alembic.ini /app/alembic/alembic.ini
COPY --chown=expert_work:expert_work packages/expert-work-persistence/migrations /app/alembic/migrations

# Eval harness (``tools/eval``) for the optional eval worker — the M0
# baseline suite + the 11.4/11.5 live adversarial/trace suites. It lives
# outside the package ``src/`` tree (the ``--no-editable`` wheel doesn't
# carry it), so ship it onto PYTHONPATH; the worker's lazy
# ``from tools.eval...`` imports + dataset loads then resolve in-container.
COPY --chown=expert_work:expert_work tools/eval /app/tools/eval
ENV PYTHONPATH=/app

# MCP connector catalog seed template (Stream MCP-OAUTH OA-5). Lives outside
# the package ``src/`` tree, so the ``--no-editable`` wheel doesn't carry it.
# Ship it so ``mcp_catalog_seed_file=configs/mcp-catalog-seed.json`` (relative
# to WORKDIR /app) resolves at startup; unset → seeding is a no-op.
COPY --chown=expert_work:expert_work configs /app/configs

USER expert_work
EXPOSE 8000
ENTRYPOINT ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
