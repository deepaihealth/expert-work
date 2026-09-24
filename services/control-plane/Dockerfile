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
# uv 默认并发下载 50 个包。从这个网络出去,几十条连接同时拉
# files.pythonhosted.org 会集体撞上默认 30 秒超时 —— 2026-08-25 一次发布
# 连挂四次,全停在这一行,报的都是 `operation timed out`。
#
# 判据是「单连接好、并发坏」:同一时刻在容器里单独拉那个报错的 wheel
# (certifi)3.1 秒就完整下来了,所以不是连不上、不是 DNS、也不是墙。
# 换 PyPI 镜像源治不了这个 —— uv.lock 把每个 wheel 的完整 URL 和哈希都
# 钉死了,`--frozen` 直接照那个 URL 拉,index 配置在下载阶段根本不参与。
ENV UV_CONCURRENT_DOWNLOADS=8 \
    UV_HTTP_TIMEOUT=120
RUN uv sync --frozen --no-dev --no-editable --package expert-work-control-plane

# B-106 —— 分词表构建期打进镜像。tiktoken 第一次 get_encoding 时同步去
# openaipublic.blob.core.windows.net 下 BPE 文件(o200k_base 3.6MB),从
# 测试集群实测 56~63s;run 里第一次估算 token 正好卡在事件循环上,存活探针
# 把 pod 杀掉,每个新进程重来一遍,永远下不完。
#
# 用镜像里那一份 tiktoken 下载,TIKTOKEN_CACHE_DIR 与 runtime 阶段同一路径:
# 缓存文件名是 URL 的 sha1,命中时再按内容 sha256 校验(tiktoken 0.14.0
# load.py:35-58 read_file_cached),所以只要同版本、同路径就走得到命中分支。
# 两份都要:o200k_base 给 run 的 token 估算器,cl100k_base 给知识库分块
# (control_plane/knowledge/chunking.py 在模块导入时加载)。
#
# 下载失败必须让构建失败 —— 设了 TIKTOKEN_CACHE_DIR 时 tiktoken 写缓存失败
# 也会抛;最后再数一遍文件,宁可构建红也不打出一个没有分词表的镜像。
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache
RUN /app/.venv/bin/python -c "import tiktoken; [tiktoken.get_encoding(n) for n in ('o200k_base', 'cl100k_base')]" \
    && test "$(ls /app/tiktoken-cache | wc -l)" -eq 2


FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

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

# B-106 —— 构建期下好的两份分词表(见 builder 阶段)。只读即可:缓存命中
# 不写文件。
COPY --from=builder --chown=expert_work:expert_work /app/tiktoken-cache /app/tiktoken-cache
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache

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

# One-off persistence ops scripts (``tools/persistence``). These are run by
# hand from inside a control-plane pod because they need BOTH halves of the
# state at once: the DB (``Settings().db_dsn``) and the NAS workspace mount —
# and this pod is the only place that has both. Same reason ``tools/eval``
# ships above: outside the package ``src/`` tree, so the ``--no-editable``
# wheel doesn't carry it.
#
# Not shipping it makes the runbooks that invoke it
# (``docs/runbooks/workspace-agent-scoping-migration.md``, step 1/2) fail at
# ``python3 -m tools.persistence...`` with ModuleNotFoundError — at release
# time, in prod, which is exactly when nobody wants to be debugging a
# ``kubectl cp`` workaround.
COPY --chown=expert_work:expert_work tools/persistence /app/tools/persistence
ENV PYTHONPATH=/app

# MCP connector catalog seed template (Stream MCP-OAUTH OA-5). Lives outside
# the package ``src/`` tree, so the ``--no-editable`` wheel doesn't carry it.
# Ship it so ``mcp_catalog_seed_file=configs/mcp-catalog-seed.json`` (relative
# to WORKDIR /app) resolves at startup; unset → seeding is a no-op.
COPY --chown=expert_work:expert_work configs /app/configs

USER expert_work
EXPOSE 8000
ENTRYPOINT ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
