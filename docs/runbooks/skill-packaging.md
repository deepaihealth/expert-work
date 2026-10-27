# Skill Packaging — Capability Uplift Sprint #3

> Pairs with `docs/streams/STREAM-UPLIFT-DESIGN.md` § 4.
>
> Authoritative source for: SKILL.md format, ZIP import / Admin UI
> mutation flow, threat-scan reject triage (U-21), drift response
> (U-21), obfuscation defense (U-22), Chinese-pattern tuning (U-23),
> and high-risk publish gate (U-24).

The skill subsystem lives across three packages:

| Component | Path |
|-----------|------|
| SKILL.md parser / serializer | `packages/expert-work-protocol/src/expert_work/protocol/skill_package.py` |
| SkillVersion model + content_hash + high_risk | `packages/expert-work-persistence/src/expert_work/persistence/models/skill.py` |
| ZIP import + Admin UI mutation API | `services/control-plane/src/control_plane/api/skills.py` |
| skill_view tool (runtime drift / redact) | `services/orchestrator/src/orchestrator/tools/skill_view.py` |
| Threat patterns (incl. cn_*, obfuscation) | `packages/expert-work-common/src/expert_work/common/threat_patterns.py` |

Two audit columns carry the forensics:

| Column | Where |
|--------|-------|
| `action` | `skill:prompt_injection_blocked`, `skill:drift_detected`, `skill:high_risk_activation_blocked`, `skill:high_risk_activated`, `skill_supporting_file:uploaded`, `skill_supporting_file:removed` |
| `details.findings[i].pattern_id` | Pattern that matched (NOT in the HTTP response — Oracle defense, same as Sprint #1) |
| `details.findings[i].excerpt` | ≤ 200-char window around the match |
| `details.findings[i].variant` | Obfuscation variant (`original` / `nfkc` / `collapsed` / `base64`) — U-22 |
| `details.reject_reason` | One of 11 allowlisted enums for ZIP rejects (§ 3) |
| `details.skill_id` / `details.skill_version_id` | Tenant-scoped target row |

## § 1 SKILL.md frontmatter schema

Every skill is one `SKILL.md` file plus a free-form supporting-file
tree. The frontmatter is YAML between two `---` lines; the body is
Markdown that becomes the agent-visible system prompt fragment.

Standard fields (also read by other Claude clients):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | str | yes | Snake_case slug; must match the skill row's `name` column |
| `description` | str | yes | One-line natural-language summary — agent uses this to decide if the skill is relevant |
| `license` | str | no | SPDX identifier (e.g. `MIT`, `Apache-2.0`); free text accepted |

`Expert Work:` namespace extensions (expert-work-only — other clients ignore):

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `expert_work.version` | str | required | Semver of the skill, NOT the SkillVersion DB row id |
| `expert_work.category` | str | `general` | Free-form tag — surfaced in Admin UI grouping |
| `expert_work.required_models` | list[str] | `[]` | Capability hint (e.g. `["vision"]`) — agent build fails if no model matches |
| `expert_work.tool_names` | list[str] | `[]` | Tools this skill expects; feeds U-24 high-risk classification |
| `expert_work.authored_by` | str | empty | Free-form attribution; for M1-K J.7b-1 agent-authored skills will be `agent:<name>` |
| `expert_work.lazy` | bool | `false` | Progressive disclosure — see § 5 |

Example:

```markdown
---
name: incident_triage
description: Walk an on-call engineer through a P0 incident — paging, mitigation, postmortem.
license: MIT
Expert Work:
  version: "1.2.0"
  category: oncall
  required_models: []
  tool_names: ["http", "slack_post"]
  authored_by: oncall-rotation@Expert Work
  lazy: false
---

# Incident triage

When the agent suspects a P0 incident, walk the user through ...
```

## § 2 Subdirectory convention

The supporting-file tree under the skill ZIP is **free-form** — there
is no enforced subdirectory layout. This matches the Claude Code
standard: the agent discovers paths by reading `SKILL.md` itself, which
points at any helper files it wants to expose. The packager (§ 6
Admin UI Import ZIP / file tree) only validates path safety (§ 3),
not naming.

Common conventions you'll see in the wild, all optional:

- `reference/` or `references/` — fact-style documents the agent reads
  on demand (`skill_view path="reference/sql_dialects.md"`).
- `scripts/` — runnable helpers the agent calls via `exec_python` or
  shells out to. The presence of `scripts/*.py` plus a `expert_work.tool_names`
  entry that includes `exec_python` is the U-24 high-risk trigger
  (§ 11).
- `templates/` — boilerplate fragments the agent stitches together
  (e.g. PR template, postmortem template).
- `prompts/` — secondary prompt snippets when `expert_work.lazy: true` and
  the agent only loads them on demand (§ 5).

When debugging "agent can't find file X" — first `skill_view path=""`
(returns SKILL.md body) and confirm the body actually references X.
If SKILL.md doesn't mention the path, the agent has no way to
discover it.

## § 3 Path validation failure

ZIP import (and Admin UI single-file mutations) reject any path that
hits one of 11 allowlisted reasons. Reasons are **enum values, not
user paths** — Oracle defense, same logic as Sprint #1: an attacker
who can read the reject reason should not learn the validator's
internal limits beyond the enum.

| `reason` enum | Meaning |
|---------------|---------|
| `missing_skill_md` | ZIP has no `SKILL.md` at the root |
| `invalid_frontmatter` | YAML frontmatter is missing, malformed, missing required field, or wrong type |
| `path_traversal` | Path contains `..`, normalized path escapes root, or absolute-style segment |
| `symlink` | ZIP entry is a symlink (no symlinks ever — even pointing inside the tree) |
| `absolute_path` | Path starts with `/` or a Windows drive letter |
| `invalid_chars` | Path contains NUL, control chars, or non-portable filename chars |
| `depth_exceeded` | More than 6 directories above a file |
| `extension_not_allowed` | Extension not in the allowlist (`ALLOWED_EXTENSIONS` in `control_plane/api/_skill_zip.py`): text — md txt yaml yml json py js cjs mjs ts sh ps1 toml html css xsd xml csv tsv ini cfg rst dot example; binary — png jpg svg ttf otf pptx docx xlsx pdf. Text files are threat-scanned, binary ones are not |
| `file_too_large` | Single file > 1 MiB — 8 MiB for a **platform** skill import on a deployment with the skill object store; tenant imports always keep 1 MiB |
| `total_too_large` | Sum of all file bytes > 8 MiB (64 MiB for a platform import with the object store) |
| `too_many_entries` | More than 1,024 files (16,384 for a platform import with the object store) |
| `legacy_format` | ZIP uses M0 `skill.yaml` + `prompt.md` + `tools.txt` layout (warning only — see § 4) |

To inspect the actual internal detail (what path / what byte size hit
the cap), pull the audit row:

```sh
curl -s "/v1/audit?action=skill:prompt_injection_blocked&from=now-1h" \
  | jq '.entries[] | {ts: .occurred_at, tenant: .tenant_id, details: .details}'
```

The audit `details` carry the user-supplied path + the limit, scoped
to tenant admin only. **Never** echo the path to the HTTP response or
mention the byte cap to the uploader in a generic error message —
attackers can use either as an oracle to binary-search the validator.

## § 4 Backward compatibility

The M0 (pre-Sprint-#3) skill format was three files: `skill.yaml`
(metadata), `prompt.md` (body), `tools.txt` (tool list). The Sprint #3
importer **still accepts these** — when it sees them at the ZIP root
and no `SKILL.md`, it transparently maps:

- `skill.yaml.name` / `description` / `license` → frontmatter standard fields
- `skill.yaml.version` → `expert_work.version`
- `skill.yaml.category` → `expert_work.category`
- `prompt.md` body → Markdown body of the new `SKILL.md`
- `tools.txt` lines → `expert_work.tool_names`

The audit log records a structured warning
(`reject_reason: legacy_format` — present in the metric but the import
proceeds) and the Admin UI shows a yellow banner: "Legacy skill format
imported; re-export to SKILL.md format to silence this warning."

The legacy importer is scheduled for removal in M1; tenants should
re-export by hitting the Admin UI **Export ZIP** button, which round-
trips through the SKILL.md serializer. After re-export the warning
goes away.

## § 5 Progressive disclosure (`expert_work.lazy`)

The default is `expert_work.lazy: false`: the entire SKILL.md body is
injected into the agent's system prompt at build time. This is the
fast path — no extra tool calls, the agent has the skill in context
from turn 1.

Setting `expert_work.lazy: true` flips the model: at build time only the
frontmatter `name` + `description` are injected. To read the body or
any supporting file, the agent must call `skill_view(skill="X",
path="")`. This is the right choice for skills whose body is large
(burns context) or whose use is rare (most turns don't need it).

**Debugging "agent can't find a lazy skill"**: the agent is supposed
to call `skill_view`, but if it doesn't, two common causes:

1. The skill description is too vague — the agent didn't realize the
   skill is relevant. Fix by rewriting the description with a concrete
   trigger ("Use when the user asks about X, Y, or Z").
2. The lazy skill is being shadowed by an eager skill with overlapping
   description. Check `expert_work_uplift_skill_view_rate:5m{result="ok"}`
   for the lazy skill — if it's zero across the whole tenant, the
   agent is never trying.

**Cache impact**: Sprint #8 prompt-cache anchor (per-session memory
snapshot) is unaffected — the `skill_view` response lands in message
history, not the system prompt, so the cache anchor on the system
block stays warm across turns. Lazy skills do NOT churn the cache.

## § 6 Admin UI mutation operations

The Admin UI (apps/admin-ui) exposes one panel per skill_version with
five actions, every one of which creates a **new** SkillVersion row
(immutability per Mini-ADR U-3 — old rows are never edited in place):

| Action | What it does |
|--------|--------------|
| **View** | Read-only file tree + Markdown / code preview |
| **Edit file** | In-place editor for a single file; save creates new SkillVersion + re-runs strict scan + re-hashes |
| **Upload file** | Drop a new file into the tree; same scan + hash + new version |
| **Rename / Move** | Pure path mutation; still creates new version (content_hash changes because path is in the hash input per U-21) |
| **Delete file** | Removes one supporting file; new version |
| **Import ZIP** | Replaces the entire supporting tree from a fresh ZIP — strict scan runs on every text file (§ 7) |
| **Export ZIP** | Round-trips the current version back to ZIP for offline editing |

The DRAFT → ACTIVE state transition is a separate API call (PATCH
status=active) and goes through the U-24 high-risk gate (§ 11). All
other mutations preserve the current status.

## § 7 Threat scan reject triage (U-21)

`SKILL_PROMPT_INJECTION_BLOCKED` fires when the write-time strict
scan matches a threat pattern in either the SKILL.md body OR any
text supporting file. The full finding list is in the audit row's
`details.findings`.

1. Locate the audit row:

   ```sh
   curl -s "/v1/audit?action=skill:prompt_injection_blocked&from=now-1h" \
     | jq '.entries[] | {tenant: .tenant_id, skill: .details.skill_id, findings: .details.findings}'
   ```

2. Read every `findings[i].pattern_id` + `findings[i].excerpt` +
   `findings[i].file_path` + `findings[i].variant` (U-22). Decide
   per finding:
   - **Genuinely suspicious** (role-override / sys-prompt extraction /
     `[INST]`-style injection): explain to the tenant which file to
     rewrite. **Never** quote `pattern_id` to the tenant — Oracle
     defense, same as Sprint #1.
   - **Legitimate match on a benign phrase** (e.g. code review skill
     containing "pretend you are a reviewer"): open a `security`-
     labelled PR that narrows the pattern (anchor on more attack-
     specific vocabulary) and add the legitimate excerpt to the
     anti-FP test set.

3. Per-tenant pattern rollback is **not** in scope today — pattern
   updates go through the global git flow per
   `threat-scanner-tuning.md` § 1. If a tenant is currently blocked
   on a benign pattern and needs to ship before the PR lands, the
   only escape is to re-author the skill content to avoid the
   matching substring (or wait for the pattern PR).

4. Watch `Expert Work:uplift:skill_blocked_rate:5m{phase="zip_import"}` for
   24 h after any pattern PR lands; spike with no corresponding deploy
   = real attack signal, spike right after deploy = noisy pattern.

## § 8 Drift response (U-21)

`SKILL_DRIFT_DETECTED` is a **P0**. The runtime drift check inside
`skill_view` recomputes
`sha256(prompt_fragment_canonical + sorted_supporting_files)` and
compares to the stored `skill_version.content_hash`. A mismatch means
the row was mutated past **both** the write-time strict scan AND the
hash-update path of the legitimate writer (`SkillStore.update_content()`
always updates content_hash atomically). That leaves three causes,
all bad:

1. **SQL injection** — someone ran `UPDATE skill_version SET
   prompt_fragment = '...' WHERE id = ...` bypassing the API.
2. **Internal actor** — engineer with DB access edited the row directly,
   either intending to test something or maliciously.
3. **Restored-from-backup row** whose content was re-rolled but
   `content_hash` not re-computed (rare; happens if the restore script
   skips the rehash step).

Immediate steps:

1. **Lock the row** at the API layer:
   ```sh
   psql -c "UPDATE skill_version SET status='draft' WHERE id='<vid>'"
   ```
   DRAFT skills are not injected into agent system prompts and not
   read by `skill_view` (returns `[BLOCKED:drift_tampered]` anyway).

2. **Snapshot for forensics** before doing anything else:
   ```sh
   psql -c "COPY (SELECT id, prompt_fragment, supporting_files, content_hash, updated_at FROM skill_version WHERE id='<vid>') TO '/tmp/skill_drift_<vid>.csv' CSV HEADER"
   ```

3. **Force re-import from trusted source** (Admin UI Import ZIP) using
   the last known-good ZIP from the tenant's offline store. The new
   import will scan + hash + version, leaving the tampered row in the
   audit trail.

4. **Page SecOps** — the audit row alone is not enough; we need a
   `psql` history pull + access-log review for the affected tenant
   to find the actor.

5. **Never** edit `skill_version.prompt_fragment` / `supporting_files`
   / `content_hash` directly via SQL even to "fix" the drift — that
   makes the drift undetectable and destroys the forensic trail.
   Always go through `SkillStore` / Admin UI mutation flow.

If the calendar shows a backup restore in the last 24 h covering the
affected row's tenant, drift is probably benign — run the rehash
script (analogous to memory rehash in `threat-scanner-tuning.md` §
8.1; the skill version is on the M1 follow-up list) and close the
incident.

## § 9 Obfuscation false positive (U-22)

U-22 added pre-processing variants (NFKC normalization, whitespace
collapse, base64 decode) so the threat scanner can catch
attacks that bypass the literal regex (e.g. `i g n o r e   p r e v i o u s`
or base64-encoded role-override). The trade-off: variants generate
more match opportunities, so false-positive rate is structurally higher.

When a tenant reports a benign skill blocked and the audit row's
`findings[i].variant` is anything other than `original`:

1. Confirm which variant fired:

   ```promql
   sum by (variant) (rate(Expert Work:uplift:threat_scan_variant_rate:1h[1h]))
   ```

   A high `nfkc` rate often means a CJK-heavy tenant (NFKC folds full-
   width to half-width, which surfaces patterns hidden by font width);
   a high `base64` rate often means a tenant whose skill embeds
   legitimately-base64-encoded payload examples.

2. Per-tenant variant disable is **not** wired today (M1 follow-up).
   The current escape hatches are:
   - Rewrite the skill content to avoid the variant-recovered substring
     (e.g. replace base64 example with a stub).
   - Narrow the offending pattern (per § 7) so the variant projection
     no longer matches.

3. **Trade-off to document if M1 adds the per-tenant flag**:
   disabling `base64` variant opens base64-encoded injection bypass;
   disabling `nfkc` opens homoglyph + full-width bypass. Each disable
   shrinks the attack surface visible to the scanner.

Track this section's open follow-up in
`STREAM-UPLIFT-DESIGN.md § 4.3.10 Mini-ADR U-22` — when the per-tenant
flag lands, link it from here.

## § 10 Chinese pattern tuning (U-23)

U-23 added ~12 `cn_*` patterns covering Chinese-language injection:
direct injection, system-prompt extraction, role hijack, restriction
removal, counterfactual framing, authority impersonation. Suspect a
`cn_*` false positive when:

- A Chinese-speaking tenant reports their skill blocked.
- The audit `findings[i].pattern_id` starts with `cn_`.
- The matched excerpt reads as a legitimate Chinese phrasing (role-
  play / persona setup / quotation of attack examples in a security-
  training skill).

The test corpus lives at:

```
packages/expert-work-common/tests/test_threat_patterns_chinese.py
```

50 attack cases (must all stay blocked) + 50 legitimate cases (must
all stay allowed). To PR a tuning fix:

1. Reproduce the FP locally:

   ```python
   from expert_work.common.threat_patterns import scan_for_threats
   scan_for_threats("<the matched substring>", scope="strict")
   ```

2. Narrow the pattern. Prefer adding a negative lookahead (e.g.
   require the attack vocabulary AND a directive verb) over loosening
   the character class.

3. Add the legitimate excerpt to the 50-case legitimate corpus —
   this locks the fix and prevents regression.

4. Run the full Sprint #3 test matrix:

   ```sh
   uv run pytest packages/expert-work-common/tests/test_threat_patterns_chinese.py \
                 packages/expert-work-common/tests/test_threat_patterns_obfuscation.py \
                 packages/expert-work-common/tests/test_threat_patterns.py
   ```

5. PR label `security`, SecOps reviewer, 24 h SLA per
   `threat-scanner-tuning.md` § 1.

Avoid widening the legitimate corpus to mask a too-broad pattern —
shrink the pattern first; the corpus is the safety net, not the fix.

## § 11 High-risk publish approval (U-24)

The U-24 gate: when a SkillVersion's `expert_work.tool_names` intersects
`HIGH_RISK_TOOLS = {exec_python, http, shell, sql}` OR the supporting
tree contains executable scripts under `scripts/`, the row's `high_risk`
column is set `true` at write time. Status transitions DRAFT → ACTIVE
on a `high_risk: true` row require an actor with `role=tenant_admin`
(or higher); non-admin attempts get HTTP 403 + audit
`skill:high_risk_activation_blocked`.

**What tenant_admin sees in the Admin UI**:

- A lock-icon `🔒 High-risk` badge on the skill card (component
  `apps/admin-ui/src/components/HighRiskBadge.tsx`).
- The Activate button is rendered greyed out for non-admin viewers
  with a tooltip "High-risk skill — requires tenant admin to
  activate."
- For tenant_admin: the Activate button is enabled but the click
  surfaces a confirmation dialog summarizing the high-risk reasons
  (which tool names, which scripts) before submitting.

**Granting approval**: the tenant_admin clicks Activate → confirms →
the PATCH lands → audit `skill:high_risk_activated` is written with
`details.high_risk_reasons` so the decision is traceable. The skill
is now ACTIVE and reachable by agents per the standard binding flow.

**Audit-trail decisions**: pull the high-risk activation history per
skill:

```sh
curl -s "/v1/audit?resource_type=skill_version&resource_id=<vid>" \
  | jq '.entries[] | select(.action | startswith("skill:high_risk")) | {ts: .occurred_at, actor: .actor_id, action: .action, reasons: .details.high_risk_reasons}'
```

**M0 reality**: this is transparent today because all skill writes in
M0 are admin-driven — every active skill is authored by a tenant_admin
who can approve their own publish. The gate exists for **M1-K J.7b-1**
where agents will be able to author their own skills; at that point
`audited_by != tenant_admin` and the gate becomes the actual control
point. The metric `Expert Work:uplift:skill_high_risk_event_rate:1h{event="activation_blocked"}`
will be ~0 in M0 and grow once agent self-authored skills ship. The
`ExpertWorkUpliftSkillHighRiskActivationSurge` alert threshold (> 30 / hr)
is calibrated for the M1 traffic baseline; if M0 traffic ever trips
it, that itself is a P2 — something is calling the API as a non-admin
service account when it shouldn't be.
