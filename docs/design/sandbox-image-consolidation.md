# Sandbox: single image + auto persistent workspace

Two operator-driven simplifications to the sandbox runtime:

1. **Collapse the two image variants (`minimal` + `office`) into one image**
   (`expert-work-sandbox`) that carries the full toolchain — Python + data/office/media
   libraries + the system binaries (`soffice`/`poppler`/`ffmpeg`) **+ Node.js**.
   Removes the `image_variant` field and all variant-selection machinery.
2. **Persistent workspace becomes automatic for any user-scoped run** — driven by
   `ctx.user_id`, not a manifest opt-in. A per-user persistent agent's files are
   guaranteed to survive idle-reclaim; no manual configuration.

Both follow the standing principle: capability over speculative minimalism, and
**no manual config where the system can decide** (same spirit as audit-over-
blocking and egress-on-by-default).

## 1. Why one image (the `minimal`/`office` split was wrong)

The split assumed `minimal` (alpine, pure stdlib, pip removed) is the cheap,
useful default and `office` an opt-in heavy tier. In practice:

- **`minimal` is too thin to be useful.** No `requests`/`httpx`, no `pandas`, no
  `numpy` — only the stdlib. Almost every real skill/agent needs at least an HTTP
  client, so agents get pushed to `office` anyway. The "cheap default" is a
  default nobody can actually do work in.
- **The split's real costs are smaller than first assumed.** Docker images are
  layered: a fat image is **one copy on host disk**, not N×size per container.
  RAM is **pay-per-use** — a container that never invokes `soffice` never pages
  it in; the only overhead is a larger read-only rootfs sitting idle. Cold-start
  of the bigger image is mitigated by the warm pool and (for user sessions) by
  session reuse.
- **The one genuine residual cost is the CVE scan surface** (LibreOffice/ffmpeg/
  Node carry a perpetual HIGH count). This is **already accepted** for the office
  image: its Trivy gate is CRITICAL-only (HX-K5) because gVisor — not the image's
  user-space libraries — is the security boundary. The weekly full scan still
  tracks HIGH.
- **`image_variant` is manual config** the operator should not have to set, and
  auto-inferring it (scan skills for required binaries, derive a `runtime_tier`)
  adds uncertainty (false-negatives fail at runtime) for a benefit that
  evaporates once there is only one image.

Net: one full image is simpler, more capable, and its costs are dominated by a
CVE-gate posture we already run. Adding **Node** to it is near-zero marginal — the
`bash` tool already executes arbitrary binaries (`/bin/sh -c`), so `node` scripts
run as-authored with no new runner or tool; it unblocks pure-JS skills/CLIs.
(Browser automation — Playwright/Puppeteer + a headless Chromium — stays out of
scope; that belongs in an MCP server, per the MCP-client-only direction.)

### 1.1 The single image

`infra/sandbox-image/` becomes the one and only image. It already holds the
shared, security-sensitive assets (`runner.py`, `sitecustomize.py` egress shim,
`seccomp-profile.json`, smoke test); the office image only added a Debian base +
libraries on top and `COPY`d those same assets from here. So:

- **Base** `python:3.12-slim` (was alpine for minimal): the data/office wheels are
  manylinux/glibc — alpine/musl would compile from source. The full image must be
  Debian-based regardless.
- **System binaries**: `libreoffice-{writer,calc,impress}-nogui`, `poppler-utils`,
  `ffmpeg`, CJK fonts + `zh_CN.UTF-8` locale (unchanged from office).
- **Node**: `nodejs` + `npm` from the Debian apt line (no NodeSource repo/key —
  keeps the install surface minimal; the Debian-packaged Node is sufficient to run
  skill-bundled `.js`). Version caveat documented; bump via NodeSource later only
  if a skill needs a newer runtime.
- **Python libs**: the office `requirements.txt` (pandas/openpyxl/python-docx/
  python-pptx/pypdf/pdfplumber/Pillow/matplotlib/pdf2image/imageio[-ffmpeg]/
  defusedxml). These also become the de-facto "standard" libs every agent gets.
- **Hardening unchanged**: non-root uid 10000, pip removed post-install, no shell
  entrypoint, runtime flags (`--read-only`, `cap-drop ALL`, gVisor, tmpfs
  `/workspace`, `/tmp`) applied at `docker run` by F.3 — image stays runtime-
  agnostic (runc dev / runsc prod).
- **Build context** is now `infra/sandbox-image/` itself (all `COPY`s local) — the
  office image's `infra/`-parent-context trick is no longer needed.
- `infra/sandbox-image-office/` is **deleted** (its `requirements.txt` +
  `mpl_cjk_rc.py` move into `infra/sandbox-image/`; its smoke checks merge into the
  single smoke test).

### 1.2 What gets deleted (variant machinery)

- **Protocol**: `SandboxSpec.image_variant` is **kept as a deprecated,
  accepted-but-ignored field** (NOT deleted). `SandboxSpec` is
  `ConfigDict(extra="forbid")`, and dogfooded agents already stored a manifest
  with `image_variant: office`; removing the field would 422 on load. So the
  field stays (description marked deprecated), and nothing reads it — the
  supervisor always serves the one image. It can be dropped in a later
  breaking cleanup once no stored agent carries it.
- **Supervisor**: `settings.sandbox_image_office`, the variant→image resolver
  (`supervisor.py` office branch), the dual pool targets
  (`pool_size_minimal`/`pool_size_office` → a single `pool_size`), `pool.py`'s
  per-variant tuple, `schemas.AcquireRequest.image_variant`, `app.py` wiring.
- **Orchestrator**: `image_variant` threading through `agent_factory`,
  `tools/assembly.py`, `tools/sandbox.py` (`run_in_sandbox`, the client Protocols),
  `tools/bash.py`, `tools/file_ops.py`, `graph_builder/workspace_ingest.py`, and the
  recording fakes.
- **CI**: `.github/workflows/sandbox-image-office.yml` deleted;
  `sandbox-image.yml` becomes the single full-image build (multi-arch amd64+arm64
  at the time — arm64 was dropped 2026-09-20, see the workflow's `platforms` comment;
  CRITICAL-only Trivy gate since it now carries LibreOffice/ffmpeg/Node);
  `sandbox-image-cve-weekly.yml` reduced to the one image.
- **Makefile**: `build-sandbox-office` removed; `build-sandbox` builds the one
  image; `rebuild-sandbox` simplified.
- **Config**: `infra/docker-compose.yml`, `infra/README.md`,
  `infra/.env.example` (`EXPERT_WORK_SANDBOX_SANDBOX_IMAGE_OFFICE` dropped — committed via
  git add, harness can't edit it directly).

### 1.3 Migration / compatibility

- An existing manifest carrying `image_variant: office|minimal` must not 422.
  Because `SandboxSpec` is `extra="forbid"`, the field is **retained** (deprecated,
  ignored) rather than deleted — a stored agent loads unchanged; the value is
  simply never read. No data migration (`image_variant` lived only in the
  manifest, never a DB column).
- Supervisor `AcquireRequest.image_variant` is likewise kept but ignored (or
  dropped if its schema is not `extra="forbid"`); an old caller sending it causes
  no error. The supervisor always resolves to the single `sandbox_image`.

## 2. Persistent workspace, automatic by `user_id`

### 2.1 The current gap

`tools/sandbox.py:run_in_sandbox` gates the durable workspace on the manifest
flag:

```python
user_id = ctx.user_id if persistent_workspace else None
```

So a per-user persistent conversation whose manifest didn't set
`persistent_workspace=True` runs with `user_id=None` → ephemeral tmpfs `/workspace`
→ **files lost on idle-reclaim**. The supervisor already mounts the per-user named
volume whenever `user_id` is present; the only thing blocking durability is this
orchestrator-side opt-in gate.

### 2.2 The change — drive it off `ctx.user_id`

- `run_in_sandbox`: `user_id = ctx.user_id` (drop the `persistent_workspace`
  gate). Any run with a user binding mounts that user's named volume → files
  survive reclaim, restored on the next acquire (reaper `docker rm -f`s the
  *container*, never the named volume).
- The build-time workspace machinery (`workspace_writer_factory`,
  `workspace_ingest_node`) is wired whenever a supervisor client is present, not
  gated on the flag; at runtime it engages only when `ctx.user_id` is set (no-ops
  otherwise), so ephemeral/system runs are unaffected.
- The manifest `persistent_workspace` field is **demoted to an optional
  force-OFF override** (default = auto/follow `user_id`): a deliberately stateless
  utility agent that runs under a user but wants no persistence can opt out. Most
  agents set nothing and get durability automatically.

### 2.3 Durability semantics (document for agent authors)

| path | backing | survives idle-reclaim? |
| --- | --- | --- |
| `/workspace` (user-scoped run) | per-user **named volume** (J.15) | **yes — restored** |
| `/workspace` (no user / forced-off) | tmpfs | no |
| `/tmp` | tmpfs (always, 256 MiB) | **no — scratch, always wiped** |

Durable state MUST live under `/workspace`. `/tmp` is per-acquire scratch.

## 3. Scope / phases

- **PR A — single image.** Rewrite `infra/sandbox-image/Dockerfile` to the full
  toolchain + Node; move office assets in; delete `sandbox-image-office/`; delete
  the `image_variant` field + all wiring; one pool target; one CI workflow
  (multi-arch, CRITICAL gate); Makefile/compose/README/.env updates; merge smoke
  tests. Verify: supervisor + orchestrator unit suites; image build + smoke +
  Trivy in CI.
- **PR B — auto persistent workspace.** Flip `run_in_sandbox` to `ctx.user_id`;
  ungate the build-time workspace wiring; demote the manifest flag to optional
  force-off; update tests asserting "flag off → tmpfs". Verify: orchestrator
  suite; a test proving `user_id` present → acquire carries it without the flag.
- **Live proof** (per "CI green ≠ live"): one image runs a Python skill (soffice
  path) AND a `node` one-liner; a user-scoped run writes `/workspace/foo`, idle-
  reclaim, re-acquire, file still there.

## 4. Non-goals

- Browser automation (Chromium/Playwright) — MCP, not this image.
- A newer Node than Debian ships — bump only on demand.
- Any per-agent or per-skill capability declaration — there is one image; nothing
  to select.
