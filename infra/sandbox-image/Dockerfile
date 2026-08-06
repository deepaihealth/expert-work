# Expert Work sandbox image — the single full toolchain for the `exec_python` / `bash`
# / `file_ops` tools: Python 3.12 + data/office/media libraries + the system
# binaries skills shell out to (LibreOffice headless / poppler / ffmpeg) + Node.js
# + CJK fonts & locale. (sandbox-image-consolidation: one image, no variants.)
#
# Why slim, not alpine: the data/office wheels (pandas/numpy/Pillow/matplotlib)
# ship manylinux/glibc wheels — on alpine's musl pip would compile from source.
# The full image must be Debian-based regardless, so there is no lighter base.
#
# Hardening per STREAM-F-DESIGN Mini-ADR F-5 / subsystem 14 § 5.6 — image-level
# only; the *runtime* flags (--read-only rootfs, --cap-drop=ALL,
# --security-opt=no-new-privileges, --pids-limit, --memory, --cpus, --network,
# tmpfs on /workspace + /tmp) are applied at `docker run` by the F.3
# SandboxRuntimeProvider, so one image works under both runc (dev) and
# runsc/gVisor (prod).
#
# Stream HX-10: the pinned seccomp profile lives beside this Dockerfile as
# `seccomp-profile.json` — NOT baked in (docker-out-of-docker means the host
# daemon launches the sandbox), passed to `docker run` via `--security-opt
# seccomp=`. Prod pre-places it + sets EXPERT_WORK_SANDBOX_SECCOMP_PROFILE_PATH; dev
# leaves it unset (host default).
#
# Sandbox migration W1 Task 1 (2026-08-03) — ACS Agent Sandbox custom-image
# requirements, checked against help.aliyun.com/zh/cs/user-guide/
# create-an-agent-sandbox + .../connect-to-agent-sandbox-using-the-e2b-sdk
# (full sourced checklist: .superpowers/sdd/2026-08-03-sandbox-migration-w1/
# task-1-report.md). Two HARD requirements, both already satisfied by this
# Debian-slim base and asserted (not installed — see the RUN check below):
#   * bash present with the executable at /bin/bash.
#   * basic coreutils present (cp/mv/mkdir, doc says "等" — non-exhaustive).
# Informational (no Dockerfile action, noted so this doesn't look like an
# oversight): agent-runtime/envd is injected as a Kubernetes native sidecar
# via the SandboxSet CR (`spec.runtimes`), a pod-level concern owned by
# Task 2 — this image does not bake in envd. A custom image also loses
# E2B's `run_code()` convenience method (only `commands.run` / envd
# shell-exec remains) — matches this migration's planned exec channel
# (spec § 6.1), so no impact; flagged for Task 8.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Read-only rootfs at runtime: point per-user cache/config dirs at the tmpfs
    # F.3 mounts on /workspace, so matplotlib & friends can write.
    HOME=/workspace \
    MPLCONFIGDIR=/workspace/.mplconfig

# Agent Sandbox hard requirement (see header note above): bash at /bin/bash
# + basic coreutils. Both are Debian `Essential: yes` packages already
# present in this base layer, so this is a build-time assertion, not an
# install — it exists to fail the build loudly if a future base-image swap
# ever drops them, rather than failing later at ACS deploy time.
RUN test -x /bin/bash \
    && command -v cp >/dev/null \
    && command -v mv >/dev/null \
    && command -v mkdir >/dev/null \
    && echo "agent-sandbox image requirement OK: bash + cp/mv/mkdir present"

# System deps:
#   * CJK fonts + fontconfig (matplotlib/Pillow Chinese rendering) + zh_CN.UTF-8
#     locale; `fc-cache -f` bakes the font cache into the read-only rootfs.
#   * The binaries office skills shell out to — the Python lib alone is not
#     enough: xlsx recalc / pptx thumbnail / docx accept-changes call `soffice`
#     (LibreOffice headless), pdf→image calls poppler, gif/video calls ffmpeg.
#     LibreOffice no-GUI components only (writer/calc/impress + core).
#   * Node.js + npm (Debian apt — no NodeSource repo/key, keeps the install
#     surface minimal) so skill-bundled `.js` runs via the `bash` tool. Browser
#     automation (Chromium/Playwright) is intentionally NOT here — that belongs
#     in an MCP server, not the sandbox image.
#   * Pango / cairo / gdk-pixbuf — weasyprint's native rendering backend (the
#     Python wheel alone can't rasterise HTML+CSS → PDF). Pairs with the baked
#     noto-cjk fonts so generated PDF reports render Chinese.
# No compiler toolchain — every Python dep still installs from a wheel.
#
# Mirror + retries (W1 Task 1, 2026-08-03): this layer fetches ~700 packages
# / ~350MB. From this network, `deb.debian.org` returns 502s partway through
# and throughput collapses — three independent build attempts all died around
# the ~17min mark with 20-48 unfetchable packages, and apt's default
# `Acquire::Retries=0` makes any single one of those fatal for the whole
# layer. Same network path that made a 52MB Docker Hub pull take hours (see
# the admin-ui image escape hatch in docs/). Fix is the mirror, not smaller
# batches: mainland Debian mirrors serve this at a usable rate. `Retries=5`
# stays as cheap insurance on top.
#
# The sed handles both source formats: Debian 12+ base images ship deb822
# (`/etc/apt/sources.list.d/debian.sources`), older ones a flat
# `/etc/apt/sources.list`. Neither file existing would mean the base image
# changed shape — the `-f` guards keep that from silently leaving the
# official mirror in place.
#
# Kept as ONE `RUN`: `rm -rf /var/lib/apt/lists/*` only shrinks the image if
# it runs in the same layer that populated it. Splitting the install into
# several `RUN`s strands those lists in the lower layers, where a later
# delete cannot reclaim them.
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    else \
        echo "no apt sources file found — base image layout changed" >&2; exit 1; \
    fi \
    && apt-get update -o Acquire::Retries=5 \
    && apt-get install -y -o Acquire::Retries=5 --no-install-recommends \
        fonts-noto-cjk \
        fontconfig \
        locales \
        libreoffice-writer-nogui \
        libreoffice-calc-nogui \
        libreoffice-impress-nogui \
        poppler-utils \
        ffmpeg \
        nodejs \
        npm \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 \
    && sed -i 's/^# *\(zh_CN.UTF-8 UTF-8\)/\1/' /etc/locale.gen \
    && locale-gen \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Set the CJK locale only AFTER locale-gen created it — declaring it earlier
# makes intervening build steps warn "cannot set LC_ALL". So str/bytes
# round-trips and library output default to UTF-8.
ENV LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8

# Python libraries (pins in requirements.txt) — these are the de-facto standard
# libs every agent gets (requests-via-stdlib-urllib + pandas/openpyxl/docx/pptx/
# pdf/Pillow/matplotlib/...).
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Bake CJK-capable matplotlib defaults into the (read-only at runtime) rootfs so
# agent charts render Chinese without per-call font config.
COPY mpl_cjk_rc.py /tmp/mpl_cjk_rc.py
RUN python /tmp/mpl_cjk_rc.py && rm /tmp/mpl_cjk_rc.py

# pip is kept (NOT uninstalled): the baked requirements cannot cover every
# library an agent might need, so agents install the rest on demand via the
# `bash` / `exec_python` tools. Runtime installs are bounded — outbound traffic
# to PyPI only flows through the per-agent egress credential proxy
# (sandbox-egress §3.5) and is audited. The earlier posture (Mini-ADR F-2)
# removed pip as attack surface; the egress gate now bounds that risk while
# restoring on-demand installs.
#
# PIP_USER=1 — the runtime rootfs is read-only, so a *global* install would fail
# on /usr/local. Defaulting pip to --user lands packages in $HOME/.local
# (HOME=/workspace, a writable tmpfs at runtime) and the user-site is on
# sys.path automatically, so a bare `pip install X` just works for the agent.
# Set AFTER the build-time requirements install above so those stay global.
ENV PIP_USER=1

# Non-root user, uid 10000 (subsystem 14 § 5.6). `-m` creates the home dir; we
# then repoint HOME at /workspace (writable tmpfs at runtime). `nologin` shell —
# the container is never meant to be exec'd into interactively.
RUN useradd -u 10000 -m -s /usr/sbin/nologin agent

# /workspace is the run root; a writable tmpfs is mounted over it at runtime.
# Created + owned here so WORKDIR resolves and the matplotlib config dir is
# writable even before (and without) the mount.
RUN mkdir -p /workspace/.mplconfig && chown -R agent:agent /workspace
WORKDIR /workspace

COPY runner.py /opt/runner.py

# Egress urllib shim (sandbox-egress §3.5): placed in the GLOBAL site-packages
# so Python auto-imports it at startup — including under the runner's
# `python -E -P -c` (still runs `site`). No-op unless the supervisor
# injects EXPERT_WORK_EGRESS_PROXY_AUTH (egress on). Makes stdlib urllib send the
# proxy token on CONNECT like requests/httpx already do.
COPY sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py

USER agent

# exec form — no shell process, no shell-injection surface.
ENTRYPOINT ["python", "/opt/runner.py"]
