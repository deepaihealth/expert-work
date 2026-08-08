# Skill Runtime Capability — what skills actually run, and how we widen the lane

Status: Proposed (design-first; implementation split into follow-up PRs)
Date: 2026-06-21
Related: [skill-github-import.md](./skill-github-import.md),
[skill-marketplace-ia.md](./skill-marketplace-ia.md),
[skill-authoring-ia.md](./skill-authoring-ia.md), Stream HX-10 (sandbox isolation)

## 1. Why this doc

Once GitHub import (#726/#734) made it trivial to pull arbitrary public skills
(e.g. `skills.sh`), the obvious next question is: **does an imported skill
actually run in Expert Work?** Spot-checking two Vercel skills —
`vercel-labs/skills/find-skills` (a `npx skills` meta/installer) and
`vercel-labs/agent-browser` (Node + Playwright + Chromium) — both do **not**
run. That raises a fair concern: *if a large fraction of public skills are like
these, is the whole Skill module low-value?*

This doc answers that honestly: it pins down **what Expert Work can and cannot run
today**, corrects the sampling bias in "most skills are like those two", and
specifies the changes that widen the valuable lane without breaking the security
model.

## 2. How Expert Work runs a skill today (as-built)

- **Instructions.** A skill's `SKILL.md` body is injected into the agent system
  prompt — eagerly (`lazy_load=false`) or on demand via the `skill_view`
  tool. `<available-skills>` lists each skill + its file manifest.
  (`agent_factory._LoadedSkills`, `tools/skill_view.py`.)
- **Files are text, read on demand.** `skill_view(skill_name, path)` returns any
  bundled file (`reference/*.md`, `scripts/*.py`, …) as **text** to the model.
  Supporting files live in the DB (base64), gated by the U-21 drift + context
  re-scan. **They are never placed on a filesystem.**
- **Execution substrate.** Expert Work has `exec_python` and `bash` tools that run in a
  gVisor sandbox via the Sandbox Supervisor (`acquire → exec(code) → release`,
  per call). The sandbox images are:
  - `infra/sandbox-image` — **Python 3.12 alpine, pure stdlib**, pip removed.
  - `infra/sandbox-image-office` — Python 3.12 slim + pandas/numpy/Pillow/
    matplotlib + CJK fonts (manifest `image_variant: office`).
- **Hard runtime constraints (by design, HX-10 / Stream F):**
  - **`network=none`** at `docker run`; egress is credential-proxy-only.
  - **No runtime package install** — pip uninstalled; no npm; read-only rootfs.
  - **No Node.js, no browser/Chromium** in either image.
  - Sandbox is **acquired per tool call** (ephemeral tmpfs `/workspace`) unless
    the agent has a persistent user workspace volume (J.15).
  - Supervisor API today: `acquire / exec(code) / release / destroy /
    read_workspace_file / reap`. **There is no "write file into the sandbox"
    API** — `exec` only takes a code string.

### Consequence

A skill that says *"run `python scripts/x.py`"* cannot do so out of the box: the
script exists only as DB text reachable via `skill_view`, not as a file on
`/workspace`. The agent would have to `skill_view` the script → write it to
`/workspace` via `bash`/file ops → execute it. Possible, but fragile and
unstated — most skills assume their files are on disk.

## 3. Capability taxonomy — what runs

| Skill class | Example | Runs in Expert Work? |
| --- | --- | --- |
| **Instruction / knowledge** (SKILL.md + reference docs, no execution) | SOPs, API conventions, writing/brand rules, workflows | ✅ Fully. The primary design intent of Agent Skills. |
| **Python compute, stdlib** | data munging, text processing | ⚠️ Works via `exec_python`/`bash`, but scripts aren't on disk → friction (read→write→run). |
| **Python compute, needs libs** | Anthropic `pptx`/`docx`/`xlsx`/`pdf` (pandas/Pillow/…) | ⚠️ Libs only if in the `office` image; **and** the scripts shell out to system binaries (`soffice`/poppler) the image lacks (§5.4). |
| **Node / other-runtime** | `agent-browser`, anything `npx`/Node | ❌ No runtime; can't install (network=none, no npm). |
| **Network-dependent** | browser automation, scraping, arbitrary API calls | ❌ `network=none` + egress allowlist. |
| **Env-specific tooling** | `find-skills` (`npx skills` installer) | ❌ No local skills dir / npx substrate; its job (discover+install) is already Expert Work's platform import. |

### The sampling-bias correction

"Most skills are like the two I saw" is overstated. The two are **Vercel
CLI-ecosystem** skills (Node-flavored because Vercel's `skills` tooling is Node).
The **canonical Agent Skills catalog (Anthropic's own)** — `pptx`, `docx`,
`xlsx`, `pdf`, plus many pure-knowledge skills — is **Python + instructions**.
The Agent Skills spec frames skills as *procedural knowledge with optional
helper scripts*, not as runtime plugins. So Expert Work's lane (instructions + Python
compute) already covers the **bulk** of the canonical catalog — just not "any
`skills.sh` entry verbatim".

### Market runtime-needs survey (2026-06-21)

De-duplicated across the installed marketplaces (~290 unique skills), the runtime
distribution is lopsided:

- **>90% are pure-knowledge** (`*-patterns`, `*-best-practices`, SOPs) — zero
  runtime, work today via prompt injection.
- Only **~40 carry scripts/deps**. Of those: stdlib ~22%, pip-in-office-image
  (C1), **extra-pip ~30% (C2)**, **Node ~20% (D)**, browser ~8% (E), external
  network ~12% (F), **system binaries ~20% (G)**.

Three counter-intuitive findings drove the next decision:

1. **"Python lib present" ≠ "office skill runs."** The Anthropic flagship
   `docx`/`xlsx`/`pptx`/`pdf` skills *shell out to system binaries* the office
   image never installed: `soffice` (LibreOffice) for xlsx formula recalc, pptx
   thumbnails, docx accept-changes; poppler for pdf→image. So the office image —
   despite shipping `python-pptx`/`openpyxl`/`pdfplumber` — runs its own
   namesake catalog **half-broken**. The gap is **G (binaries)**, not packages.
2. **`image_variant: {minimal, office}` is a fixed menu** that cannot chase an
   open-ended market (every skill's arbitrary pip/binary). Widening it is a
   curated-vs-open-ended architecture choice (route ① vs ②, §5.4/§5.5).
3. **C2 (~30%) looks largest but is the cheapest** (extend a pinned requirements
   list). **F (network, ~12%) is addressable, not a ceiling** — the sandbox is
   not truly `network=none` (it sits behind a credential-proxy on an internal
   net), and the per-agent `NetworkSpec` already exists but is unwired; wiring a
   transparent, audited egress proxy lets F-bucket skills reach public APIs (see
   [sandbox-egress-per-agent.md](./sandbox-egress-per-agent.md)). **E (browser,
   ~8%) is the real hard part** — a live headless browser is heavy and its job is
   inherently external/stateful → MCP territory. (An earlier draft called F a
   hard ceiling; that was overstated.)

## 4. Architectural division: Skill vs MCP

Expert Work is **MCP-client** by direction. The clean split:

- **Skill = knowledge + in-sandbox computation.** Portable content, no external
  state, no network. Lives in the DB, surfaced via `skill_view` and (proposed)
  materialized into the sandbox for Python execution.
- **MCP = capabilities that need an external runtime, network, or stateful
  service.** Browser, DB, GitHub, SaaS APIs. Process-isolated, governed by
  credentials + audit, network-capable.

Under this split, **`agent-browser` as a skill is a category error.** Browser
automation belongs to a **browser MCP server** (consumed like the GitHub/
Postgres MCPs), not a Node skill shoved into a Python, network-less sandbox.
Vercel blends the two because Claude Code treats everything as a local skill in
a Node env; Expert Work need not copy that. "Expert Work can't run a browser skill" is not a
weakness — it's the boundary doing its job.

## 5. Changes (widen the lane without breaking the model)

### 5.1 Auto-materialize skill supporting files into the sandbox — HIGHEST ROI

> **Path update (2026-08-07, sandbox migration W2):** skill supporting files no
> longer land under `/workspace/skills/…`. Sandbox migration W2 moved them to
> `/opt/skills/<agent_key>/<skill_name>/…` — a per-agent namespace on the
> sandbox's local disk, separate from the now NAS-backed, per-(tenant, user)
> `/workspace`. Rationale: skills are the agent's capability definition, not
> user data — they shouldn't occupy NAS quota or show up in the user's
> workspace browser. See
> [2026-08-07-sandbox-migration-w2-design.md](../superpowers/specs/2026-08-07-sandbox-migration-w2-design.md)
> § 四. The design narrative below (written 2026-06-21, pre-migration)
> otherwise still describes the seeding mechanism correctly; only the mount
> point changed.

**Goal:** when a run executes `exec_python`/`bash`, the activated skills'
supporting files are present on `/opt/skills/<agent_key>/<skill_name>/…` so a
skill's `python scripts/x.py` works as authored.

**Design:**
- Add a Supervisor capability to **seed files at `acquire` time** (extend
  `acquire(...)` with an optional `seed_files: list[(path, bytes)]`, written into
  `/workspace` before the first `exec`). Files are small and already in memory at
  build time (`_LoadedSkills.resolved_versions[*].supporting_files`).
- The orchestrator passes the activated skills' supporting files (decoded,
  post drift/scan check — reuse the `skill_view` U-21 path so seeding can't
  bypass the scanner) under `skills/<name>/`. SKILL.md itself is also written so
  relative refs inside the skill resolve.
- Per-call acquire means seeding runs each call; that's fine (bytes are tiny).
  For persistent-workspace agents, seed idempotently (skip if content_hash
  matches what's already there).
- **Security:** seeded files pass the **same U-21 drift + context re-scan** as
  `skill_view`; a `[BLOCKED]` skill is not seeded. Still `network=none`,
  read-only rootfs except the `/workspace` tmpfs, non-root uid 10000. No new
  egress, no installer. Cap total seeded bytes (reuse the 5 MiB skill-package
  total) and entry count.
- **Scope:** Supervisor service + orchestrator. New protocol field on `acquire`;
  back-compat (empty list = today's behavior).

**Unlocks:** the entire Anthropic Python catalog (`pptx`/`docx`/`xlsx`/`pdf`)
runs as authored under the `office` image, plus any stdlib Python skill.

### 5.2 Import-time runtime-dependency detection + warning — LIGHT

**Goal:** don't let an operator import a skill that can't run and only find out
at runtime.

**Design:** during import parse (`parse_skill_zip` / `_ingest_*`), classify the
package and attach a non-blocking signal:
- `node` markers: `package.json`, `*.js`/`*.ts` as the primary scripts, `npx`/
  `node` in SKILL.md.
- `browser`/`network` markers: `playwright`, `puppeteer`, `chromium`, `fetch(`/
  `http` to arbitrary hosts in SKILL.md, `requirements` on networked libs.
- Surface a `runtime: { kind: "python" | "node" | "browser" | "unknown",
  runnable: bool, hint }` in the import response. The admin UI shows it:
  *"This skill needs Node/a browser — Expert Work runs Python-only sandboxes; consider
  a browser/Node MCP server instead."*
- **Non-blocking** — knowledge value may still exist (the instructions are
  readable even if scripts won't run). Just set expectations.

### 5.4 Office image completeness (route ①) — bake the system binaries the office skills call — HIGH ROI

**Problem.** §3 finding (1): the `office` image ships the Python office libs but
not the *system binaries those libs/scripts shell out to*, so its own namesake
catalog runs half-broken even after auto-mount (§5.1):

| Anthropic skill | Python lib (have) | Binary it shells to (missing) | Broken path |
| --- | --- | --- | --- |
| `xlsx` | openpyxl | `soffice --headless` | formula **recalc** |
| `pptx` | python-pptx | `soffice` (`scripts/thumbnail.py`) | **thumbnail** render |
| `docx` | python-docx, lxml | `soffice` | **accept tracked changes** |
| `pdf` | pypdf, pdfplumber | poppler (`pdf2image`/`pdftoppm`) | PDF→**image** convert |
| `slack-gif-creator` | Pillow, numpy | `ffmpeg` (imageio-ffmpeg) | **gif/video** encode |

**Decision: fatten the same `office` variant** — this is a bug-fix to the
variant's premise ("run the office catalog"), not a new capability tier, so **no
new manifest enum and zero plumbing change** (`image_variant: office` is already
wired end-to-end from OFFICE-1a). Pure Dockerfile + requirements + build/CI/docs.

**Add (curated, narrow):**
- **`soffice`** (LibreOffice headless, no-GUI components only: core + writer +
  calc + impress) — unlocks the docx/xlsx/pptx full paths. The single
  highest-value binary; one install fixes three flagship skills.
- **`poppler-utils`** — `pdftoppm`/`pdftocairo` for the pdf skill's image
  conversion (small).
- **`ffmpeg`** — gif/video encode for slack-gif-creator and similar (medium).
- **Extended pip (tight):** `pdf2image` (needs poppler), `markitdown` (the pptx
  SKILL.md read path), `imageio` + `imageio-ffmpeg`, `defusedxml` (office scripts
  import it directly; today only transitive). No kitchen-sink — every line stays
  part of the trusted, import-available surface (matches the existing
  requirements.txt discipline).

**Explicitly deferred (not in route ①):**
- **LaTeX / `pdflatex`** (math-olympiad, manim render) — adds ~1 GB for
  single-skill ROI. Defer until demand.
- **Node** (§5.3) — separate policy decision; revisit *after* ① ships.
- **C2 long tail / declared-deps resolution** (route ②, §5.5) — deferred; ①'s
  curated list covers the common C2; the long tail waits.
- **E (browser) / F (network)** — out of sandbox scope by the security model →
  MCP. ① does not touch them.

**Costs / tradeoffs (accepted):**
- **Image size:** slim (~300 MB) → fat (~1–1.3 GB, mostly LibreOffice). Bounded
  blast radius: `office` is **opt-in** (`image_variant: office`); the common case
  stays the alpine `minimal` image. Only office agents pay the larger
  cold-start/pull.
- **soffice attack surface:** LibreOffice has a macro-execution CVE history.
  Mitigated by the *existing* runtime envelope — gVisor/runsc, `network=none`,
  read-only rootfs, non-root uid 10000, tmpfs `/workspace` — plus running it
  `--headless --nolockcheck` against a throwaway profile. soffice processes
  untrusted office files but can neither call out nor persist.
- **Build weight:** office image is already **not** built by `make dev-up` (too
  heavy); it stays an explicit target.

**Build / ops:**
- Add `make build-sandbox-office` (host `docker build`, context `infra/`, tag
  `expert-work-sandbox-office:dev`). Keep it out of `dev-up`; document that office
  agents need it built once on the host (the daemon the supervisor drives).
- The office-image CI workflow already builds `:ci` — extend its smoke step to
  assert `soffice`/`ffmpeg`/`pdftoppm` are present **and** do one real
  conversion (e.g. `soffice --headless --convert-to pdf` on a generated file).

**Verify (live E2E — the real proof, per "CI green ≠ live"):** extend
`tools/eval/verify_live_skill_runtime.py` with a `--generate` phase on an
`image_variant: office` manifest that grants `exec_python` (+ lists it in
`policies.approval_required_tools`). The agent generates a real artifact whose
path **requires the new binary** — e.g. python-pptx writes `out.pptx`, then
`soffice --headless --convert-to pdf out.pptx` produces `out.pdf` — and the
script reads it back / asserts a valid file. Because `exec_python` is
`side_effect=irreversible` it is approval-gated, so the verify script needs an
**approval-polling loop** (poll `GET /v1/approvals` → `POST
/v1/sessions/{tid}/runs/{rid}/resume {"decision":"approve"}`), which the current
read-only probe deliberately avoids.

**Unlocks:** the Anthropic office flagship (`docx`/`xlsx`/`pptx`/`pdf`) goes from
*half-broken* to *fully runnable as authored*, plus gif/video encode — the
highest-value real skills, with no ADR break and no new plumbing.

### 5.3 Node sandbox variant — EVALUATION (not yet a commitment)

**Question:** add `infra/sandbox-image-node` (Node 20 + a pinned dep set) so
Node-script skills run?

**Assessment:**
- *Pro:* covers Node compute skills (no browser) that are otherwise dead.
- *Con / cost:* a third image to maintain + pin + scan; Node's dep model wants
  `npm install` which conflicts with the no-installer rule (so deps must be
  pre-baked, like the office image — only skills using the baked set run);
  doubles the "which image" surface in manifests.
- *Does not help the headline cases:* `agent-browser` still needs a browser +
  network, which a Node image alone doesn't provide and which the security model
  forbids → that stays MCP.
- **Recommendation: defer.** Do 5.1 + 5.2 first. Revisit a Node image only if,
  after auto-mount, real demand for **non-network Node compute** skills shows up.
  Browser/network Node skills are out of scope for sandboxing regardless → MCP.

## 6. Decision

1. Skill module value is **real but lane-bound**: instructions + Python compute
   (covers the bulk of the canonical catalog). Not a universal runner for any
   `skills.sh` entry.
2. **Ship 5.1 (auto-mount)** — the single biggest unlock; turns the Python
   catalog from "fiddly" to "works as authored". *(shipped: #736/#737/#742)*
3. **Ship 5.2 (import detection)** — set expectations at import time. *(shipped:
   #738/#740)*
4. **Ship 5.4 (office image completeness, route ①)** — bake `soffice`/poppler/
   ffmpeg + a tight extra-pip set so the office flagship runs whole, not half.
   Curated, opt-in, no plumbing/ADR break.
5. **Defer route ② (declared-deps resolution)** — its honest gain over ① is the
   C2 long tail + future-proofing (~+10% of runtime skills); the headline +20%
   is Node, which is a *policy* call (§5.3), not ②'s mechanism. Revisit when the
   long tail actually hurts.
6. **Defer 5.3 (Node image)** to a standalone decision *after* ① ships; **route
   browser/network to MCP**. E+F (~20%) are a hard ceiling no sandbox reaches.
7. Document this taxonomy (this file) as the canonical answer to "will skill X
   run?".

## 7. Open questions

- Persistent-workspace seeding idempotency: key on per-skill content_hash; evict
  stale `skills/<name>/` dirs when a version changes mid-run? (Likely yes.)
- Should SKILL.md body injection note to the model that files are now on disk at
  `/opt/skills/<agent_key>/<name>/` (so it prefers running over re-reading via
  `skill_view`)? (Probably a one-line hint in the `<available-skills>` summary.)
- Seeded-file size/count caps vs the existing package caps — reuse or separate?
