# Expert Work credential-proxy image — Stream F.10.
#
# Multi-stage uv build. This is the pilot that establishes the "expert_work
# service containerization" pattern (Mini-ADR F-15); the other three
# services reuse it under ITERATION-PLAN I.1.
#
# Builder: the whole uv workspace must be present for uv to resolve it,
# but ``--package`` builds only credential-proxy + its workspace deps,
# installed as non-editable wheels into a self-contained ``.venv``.
# Runtime: carries only that ``.venv`` on a slim glibc Python base.
#
# Build from the repo root:
#   docker build -f services/credential-proxy/Dockerfile -t expert-work-credential-proxy .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/ services/
RUN uv sync --frozen --no-dev --no-editable --package expert-work-credential-proxy


FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Non-root service account — parity with the sandbox image hardening.
# (``proxy`` is a pre-existing Debian system user, hence ``expert_work``.)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin expert_work

COPY --from=builder --chown=expert_work:expert_work /app/.venv /app/.venv

USER expert_work
EXPOSE 8080
ENTRYPOINT ["python", "-m", "credential_proxy.main"]
