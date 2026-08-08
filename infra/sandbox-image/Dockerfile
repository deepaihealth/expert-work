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
#
# Sandbox migration W2 Task 9 (2026-08-07) — a cluster A/B probe against the
# real ACS NAS mount (csi-volume-config) pinned two hard blockers baked into
# this image; full writeup in
# docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md § 二之二.
#   * The container must start as root (no image-level `USER`, see bottom of
#     this file). envd mounts the NAS share by fork/exec-ing a storage
#     helper *as the container's own identity*, not as a subprocess it can
#     re-exec under another user — non-root (uid 10000, this image's
#     previous posture) got `operation not permitted`; the same PV mounted
#     fine once the container ran as root. This is not a security
#     downgrade: the sandbox's actual isolation boundary on ACS is the
#     microVM, not the container user — the official code-interpreter image
#     ships root for the same reason. Exec still drops privileges:
#     `commands.run(user="agent")` (W1 Task 7/8, already wired) runs agent
#     code as uid 10000 regardless of the container's own identity. Local
#     docker keeps a real container-level boundary, so it needs to stay
#     non-root there — via `docker run --user 10000:10000`, added in Task 6,
#     not yet as of this commit, so local sandboxes built from this image
#     run as root in the interim (dev/CI only; harmless, and ACS is
#     unaffected either way).
#   * The run root must not pre-exist in the image. The platform mounts the
#     NAS share by creating a *symlink* at the mountPath when the sandbox
#     starts, not by mounting over an existing directory — a directory
#     already sitting there (from an explicit `mkdir`, or the
#     directory-creating side effect of declaring it as the image's working
#     directory) blocks the symlink from ever landing. So this image no
#     longer creates or declares a working directory at that path; HOME /
#     MPLCONFIGDIR move to /home/agent (this image's other pre-existing
#     writable-user path, created by `useradd -m` below) to stay out of the
#     way too. The run root itself stays a bare mount point with nothing
#     baked under it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # HOME/MPLCONFIGDIR point at /home/agent, not the run root (W2 Task 9,
    # see the header note above `FROM`) — the run root must stay empty for
    # the platform's NAS-mount symlink. /home/agent is `useradd -m`-created
    # + agent-owned below. Local docker's --read-only rootfs still needs a
    # writable mount over it before this is actually writable there — same
    # gap as the run root's `USER`/`--workdir`, closed by Task 6.
    HOME=/home/agent \
    MPLCONFIGDIR=/home/agent/.mplconfig

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
# on /usr/local. Defaulting pip to --user lands packages in $HOME/.local and
# the user-site is on sys.path automatically, so a bare `pip install X` just
# works for the agent. Set AFTER the build-time requirements install above so
# those stay global.
#
# W2 Task 9: $HOME/.local now lands under /home/agent, sandbox-local disk —
# HOME moved off the durable NAS-backed run root (see the header note above
# `FROM`), so installs made this way persist only within one sandbox's execs,
# not across a sandbox rebuild (still shadowing the image's baked package
# versions if an agent installs a different one, same as before — deliberate
# PIP_USER semantics, not a bug). Durable, per-agent, concurrency-safe
# installs are Task 5/6's `PYTHONUSERBASE=/opt/agents/<agent_key>` wiring
# (spec § 二之三), injected at exec time; this bare-HOME fallback keeps
# working either way.
ENV PIP_USER=1

# Non-root user, uid 10000 (subsystem 14 § 5.6). `-m` creates /home/agent,
# which HOME now points straight at (see ENV above). `nologin` shell — the
# container is never meant to be exec'd into interactively. Exec still runs
# as this user (`commands.run(user="agent")` / local docker `--user
# 10000:10000`) even though the container itself now starts as root — see
# the W2 Task 9 header note.
#
# The explicit `chown -R` is load-bearing, confirmed by a real build+run: no
# `USER` directive exists anywhere in this file any more (root startup), so
# every `RUN` — including the `mpl_cjk_rc.py` step above, which imports
# matplotlib and makes it eagerly create its config dir — executes as root.
# That step already ran with HOME=/home/agent, so /home/agent exists
# root-owned by the time this line runs; `useradd -m` only takes ownership
# of a home dir it creates itself, and silently no-ops on one that already
# exists ("warning: the home directory ... already exists"). Without this
# chown, /home/agent stays root-owned and the agent user can't write to its
# own HOME at all (matplotlib import fails outright, `pip install --user`
# fails) — this mirrors why the old `/workspace` setup used `chown -R`
# too, for the same underlying reason.
RUN useradd -u 10000 -m -s /usr/sbin/nologin agent \
    && chown -R agent:agent /home/agent

# The run root gets nothing baked under it — W2 Task 9, see the header note
# above `FROM`: the platform builds a NAS-mount symlink there, and a
# pre-existing directory (from `mkdir` or a working-directory declaration,
# both of which create the directory as a side effect) blocks that.
#
# /opt/skills and /opt/agents, by contrast, must exist and be agent-owned:
# skill files and `PYTHONUSERBASE` pip installs both land there as the
# agent user at runtime, and /opt itself is root-owned — an agent-uid write
# under an unset subdirectory fails. (Consumed by Task 5's skill-seed
# rework and the per-agent PYTHONUSERBASE wiring; this task only lays the
# directories down.)
RUN mkdir -p /opt/skills /opt/agents && chown agent:agent /opt/skills /opt/agents

COPY runner.py /opt/runner.py

# Egress urllib shim (sandbox-egress §3.5): placed in the GLOBAL site-packages
# so Python auto-imports it at startup — including under the runner's
# `python -E -P -c` (still runs `site`). No-op unless the supervisor
# injects EXPERT_WORK_EGRESS_PROXY_AUTH (egress on). Makes stdlib urllib send the
# proxy token on CONNECT like requests/httpx already do.
COPY sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py

# No image-level `USER` here any more — the container starts as root. See
# the W2 Task 9 header note above `FROM` for why (envd's NAS-mount helper
# needs to fork/exec as the container's own identity) and why it's safe
# (isolation boundary is the ACS microVM, not the container user). Agent
# code execution still drops privileges at the exec layer, not here.

# exec form — no shell process, no shell-injection surface.
ENTRYPOINT ["python", "/opt/runner.py"]
