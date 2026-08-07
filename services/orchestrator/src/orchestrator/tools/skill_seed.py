"""Build the seed-file set for sandbox skill materialization.

skill-runtime §5.1 — an agent's activated skills are materialized at
``{SANDBOX_SKILLS_ROOT}/<agent_key>/<name>/…`` so bundled scripts run as
authored (the canonical Agent Skills model: skill = a directory on the VM
filesystem). Sandbox migration wave 2 (spec § 四) moved this off the user's
NAS-backed ``/workspace`` onto sandbox-local disk, namespaced per agent
(``agent_key``) so two agents sharing one warm sandbox never overwrite each
other's skill files — see :func:`sanitize_agent_key`.

This runs ONCE at build time (``agent_factory.build_agent``) over the already-
resolved ``SkillVersion`` rows; the result is bound onto the sandbox tools and
sent to the supervisor on each ``acquire`` (the supervisor re-validates path +
caps at its trust boundary). Reuses the same U-21 checks as ``skill_view`` so
seeded bytes can't bypass the scanner:

* **drift** — a row whose recomputed ``content_hash`` doesn't match is skipped
  whole (tampered past the import-time scan).
* **context-scope threat scan** — each text supporting file is re-scanned; a hit
  drops that file. Binary files can't encode a prompt, so they're seeded as-is.

SKILL.md itself is always seeded (it is the ``prompt_fragment`` already injected
into the system prompt) so a skill's relative refs resolve on disk.
"""

from __future__ import annotations

import binascii
import logging
import re
from dataclasses import dataclass
from uuid import UUID

from expert_work.common.threat_patterns import scan_for_threats
from expert_work.protocol import AuditAction, AuditResult, SkillVersion
from expert_work.protocol.audit import AuditEntry
from expert_work.protocol.skill import compute_content_hash, supporting_files_to_jsonable
from expert_work.protocol.skill_package import ParsedSkillMd, serialize_skill_md
from expert_work.runtime.skill_assets import (
    ObjectStore,
    SkillAssetIntegrityError,
    fetch_supporting_files_settled,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedDrop:
    """One file (or whole skill) excluded from the sandbox seed set.

    ``path`` is ``None`` for a whole-skill drop (drift makes every file
    untrustworthy); otherwise it is the supporting file's relpath. ``reason`` is
    one of ``drift`` / ``bad_base64`` / ``injection`` — the security-relevant
    drops that earn a durable audit row (the cap-truncation drop stays a log
    line: it is a capacity limit, not a tamper/injection signal)."""

    skill_name: str
    reason: str
    path: str | None = None


@dataclass(frozen=True)
class SkillSeedResult:
    """Outcome of :func:`build_skill_seed_files`: the seed set plus the dropped
    files, so the caller can write an audit row per drop (audit-over-blocking —
    a silently-dropped file must still be traceable)."""

    files: tuple[tuple[str, bytes], ...]
    drops: tuple[SeedDrop, ...]


#: A dropped file maps to the closest existing skill audit action. ``drift``,
#: ``bad_base64`` and ``asset_integrity`` are all content-integrity failures
#: (stored bytes not as expected) → ``SKILL_DRIFT_DETECTED``; an ``injection``
#: hit → the dedicated ``SKILL_PROMPT_INJECTION_BLOCKED``.
#: ``asset_unavailable`` (object-store outage) is an infra failure, not a
#: security signal — it stays a log line and gets NO audit row (absent here).
_DROP_ACTION: dict[str, AuditAction] = {
    "drift": AuditAction.SKILL_DRIFT_DETECTED,
    "bad_base64": AuditAction.SKILL_DRIFT_DETECTED,
    "asset_integrity": AuditAction.SKILL_DRIFT_DETECTED,
    "injection": AuditAction.SKILL_PROMPT_INJECTION_BLOCKED,
}


def seed_drop_audit_entries(tenant_id: UUID, drops: tuple[SeedDrop, ...]) -> list[AuditEntry]:
    """Map seed drops to ``audit_log`` rows. Build-time (no run yet) so the
    actor is ``system``. ``details`` carries skill name + path only — no file
    content (which could be the very injection payload that got it dropped).
    Drops whose reason has no action mapping (infra, e.g. ``asset_unavailable``)
    are skipped — logged by the seeder, not audited."""
    entries: list[AuditEntry] = []
    for drop in drops:
        if drop.reason not in _DROP_ACTION:
            continue
        details: dict[str, object] = {"skill": drop.skill_name, "stage": "skill_seed"}
        if drop.path is not None:
            details["path"] = drop.path
        entries.append(
            AuditEntry(
                tenant_id=tenant_id,
                actor_type="system",
                actor_id="skill_seed",
                action=_DROP_ACTION[drop.reason],
                resource_type="skill",
                resource_id=drop.skill_name,
                result=AuditResult.DENIED,
                reason=f"seed_dropped:{drop.reason}",
                details=details,
            )
        )
    return entries


def _skill_md_with_name(name: str, version: SkillVersion) -> str:
    """Serialize the version's SKILL.md with the REAL skill name in frontmatter.

    ``skill_view._repack_skill_md`` falls back to ``description`` for the name
    (the SkillVersion DTO doesn't carry the skill row's name) — fine for the
    internal skill_view text read, but a file seeded to disk should have a
    faithful ``name:``. Here we know the real name (the activated-skill key).
    """
    parsed = ParsedSkillMd(
        name=name,
        description=version.description or name,
        license=None,
        expert_work_version=version.version,
        expert_work_category=version.category,
        expert_work_required_models=version.required_models,
        expert_work_tool_names=version.tool_names,
        expert_work_authored_by=version.authored_by,
        expert_work_lazy=version.lazy_load,
        body=version.prompt_fragment,
    )
    return serialize_skill_md(parsed)


# Aligned with the import caps in ``_skill_zip`` (asset-store tier): a skill
# that was allowed in must also materialize. The sandbox workspace tmpfs is
# the real bound.
_MAX_SEED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_SEED_FILES = 16384

#: Characters a sandbox path segment tolerates without escaping/confusing the
#: seed anchor. ``spec.metadata.name`` is author-authored YAML text (arbitrary
#: Unicode, ``/`` allowed in principle), but it becomes a directory name under
#: :data:`~expert_work.persistence.SANDBOX_SKILLS_ROOT` — anything outside
#: this set collapses to ``-``.
_AGENT_KEY_DISALLOWED = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize_agent_key(name: str) -> str:
    """Turn an agent's manifest name into a safe sandbox directory segment.

    Used as the per-agent namespace prefix for both the skill-seed anchor
    (below) and ``PYTHONUSERBASE`` (spec 决策 10,
    ``orchestrator.tools.sandbox.agent_key_envs``) — the same key must reach
    both so a skill's declared path and its own ``pip install --user`` target
    agree on which agent they belong to. An all-disallowed (or empty) name
    falls back to ``"agent"`` rather than producing an empty path segment.
    """
    return _AGENT_KEY_DISALLOWED.sub("-", name) or "agent"


async def build_skill_seed_files(
    resolved_versions: dict[str, SkillVersion],
    activated_skill_names: list[str],
    *,
    agent_key: str,
    object_store: ObjectStore | None = None,
) -> SkillSeedResult:
    """Return the ``(relpath, raw_bytes)`` seed set (anchored under
    ``<agent_key>/<name>/``) plus the dropped files. Drift-skipped +
    threat-filtered + capped; each security-relevant drop is recorded in
    ``.drops`` so the caller can audit it.

    ``agent_key`` (:func:`sanitize_agent_key` of ``spec.metadata.name``) is
    the per-agent namespace: the caller writes these relpaths under
    ``SANDBOX_SKILLS_ROOT`` on the sandbox, and two agents sharing one warm
    sandbox each seed their own subtree instead of clobbering one another
    (spec § 四 — no cleanup needed, namespacing alone gives concurrency
    safety).

    Async since skill-asset-store: externalized supporting files are fetched
    from ``object_store`` (digest-verified); a missing store or a corrupt
    object drops that file with an audited reason rather than failing the
    whole agent build.
    """
    out: list[tuple[str, bytes]] = []
    drops: list[SeedDrop] = []
    total = 0
    for name in activated_skill_names:
        version = resolved_versions.get(name)
        if version is None:
            continue
        # U-21 drift: a tampered row → skip the whole skill (mirrors skill_view).
        jsonable = supporting_files_to_jsonable(version.supporting_files)
        if compute_content_hash(version.prompt_fragment, jsonable) != version.content_hash:
            logger.warning("skill_seed.drift_skipped skill=%s", name)
            drops.append(SeedDrop(skill_name=name, reason="drift"))
            continue

        candidates: list[tuple[str, bytes]] = [
            (
                f"{agent_key}/{name}/SKILL.md",
                _skill_md_with_name(name, version).encode("utf-8"),
            )
        ]
        # Fetch this skill's assets concurrently (bounded), isolating per-file
        # failures so one corrupt / missing object drops just that file instead
        # of a serial await-per-file loop (that was the N+1). The isinstance
        # chain below mirrors the original except order exactly.
        settled = await fetch_supporting_files_settled(
            version.supporting_files, object_store=object_store
        )
        for relpath in sorted(version.supporting_files):
            raw_or_err = settled[relpath]
            if isinstance(raw_or_err, ValueError | binascii.Error):
                logger.warning("skill_seed.bad_base64 skill=%s path=%s", name, relpath)
                drops.append(SeedDrop(skill_name=name, reason="bad_base64", path=relpath))
                continue
            if isinstance(raw_or_err, SkillAssetIntegrityError):
                logger.warning("skill_seed.asset_integrity skill=%s path=%s", name, relpath)
                drops.append(SeedDrop(skill_name=name, reason="asset_integrity", path=relpath))
                continue
            if isinstance(raw_or_err, BaseException):
                # Object-store outage / missing object: the sandbox is still
                # useful without this one asset; drop + audit, never fail the
                # whole build on a storage hiccup.
                logger.warning(
                    "skill_seed.asset_unavailable skill=%s path=%s err=%s",
                    name,
                    relpath,
                    raw_or_err,
                )
                drops.append(SeedDrop(skill_name=name, reason="asset_unavailable", path=relpath))
                continue
            raw = raw_or_err
            # Re-scan text files (context scope); binary can't carry a prompt.
            try:
                text: str | None = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and scan_for_threats(text, scope="context"):
                logger.warning("skill_seed.blocked skill=%s path=%s", name, relpath)
                drops.append(SeedDrop(skill_name=name, reason="injection", path=relpath))
                continue
            candidates.append((f"{agent_key}/{name}/{relpath}", raw))

        for path, data in candidates:
            if len(out) >= _MAX_SEED_FILES or total + len(data) > _MAX_SEED_TOTAL_BYTES:
                # Capacity limit, not a tamper/injection signal → log only (the
                # "no silent caps" rule names what was dropped). No audit row.
                logger.warning(
                    "skill_seed.truncated reason=cap files=%d total_bytes=%d", len(out), total
                )
                return SkillSeedResult(files=tuple(out), drops=tuple(drops))
            out.append((path, data))
            total += len(data)
    return SkillSeedResult(files=tuple(out), drops=tuple(drops))
