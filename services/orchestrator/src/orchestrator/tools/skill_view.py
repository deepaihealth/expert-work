"""``skill_view`` orchestrator tool — Capability Uplift Sprint #3
(Mini-ADRs U-17 + U-21).

When an agent is given access to one or more skills (via ``skills:`` in
its manifest), :class:`SkillViewTool` is registered as a single
``skill_view(skill_name, path)`` tool. The tool serves as the single
mental model for reading skill content:

- ``path == "SKILL.md"`` → re-pack the version's frontmatter +
  prompt_fragment into a Markdown document and return it
- ``path == "reference/foo.md"`` (or any other subdir) → look up the
  base64-encoded entry in ``supporting_files`` and return its decoded
  contents

Every read goes through the U-21 double check:

1. **Drift detection** — recompute ``content_hash`` over canonicalized
   ``(prompt_fragment, supporting_files)`` and compare against the
   stored value. Mismatch fires :func:`record_skill_drift` + a
   ``SKILL_DRIFT_DETECTED`` audit row (P0 — almost certainly SQL
   injection or internal actor) and returns a ``[BLOCKED]`` placeholder.

2. **Context-scope re-scan** — run ``scan_for_threats(content,
   scope="context")`` on the chosen content. A hit fires
   :func:`record_skill_redacted` + :func:`record_threat_pattern_hits`
   and returns a ``[BLOCKED]`` placeholder. This catches the case
   where a pattern set update adds new rules after the row was already
   imported.

Both placeholders preserve Oracle defense: the LLM sees that the
content was withheld but never sees the offending substring or the
recomputed hash.
"""

from __future__ import annotations

import binascii
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from expert_work.common.skill_activity import SkillActivityRecorder
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.common.uplift_metrics import (
    record_skill_drift,
    record_skill_redacted,
    record_skill_view,
    record_skill_view_archived_blocked,
    record_threat_pattern_hits,
)
from expert_work.protocol import Skill, SkillStatus, SkillVersion
from expert_work.protocol.skill import (
    SkillSupportingFile,
    compute_content_hash,
    supporting_files_to_jsonable,
)
from expert_work.protocol.skill_package import (
    ParsedSkillMd,
    serialize_skill_md,
)
from expert_work.runtime.skill_assets import (
    ObjectStore as SkillAssetObjectStore,
)
from expert_work.runtime.skill_assets import (
    SkillAssetIntegrityError,
    fetch_supporting_file,
)
from orchestrator.tools.registry import (
    ToolBlockedError,
    ToolContext,
    ToolResult,
    ToolSpec,
)

logger = logging.getLogger(__name__)

#: Mirrors ``MCPTool`` (Sprint #5). Skill content can grow but the LLM
#: doesn't need to see all of it on every read — middle-trim if over
#: this many chars so head + tail are both visible.
SKILL_VIEW_CONTENT_CAP: int = 20_000
_TRUNCATION_PREFIX = "...["
_TRUNCATION_SUFFIX = " chars truncated]..."


@dataclass(frozen=True)
class SkillResolution:
    """Capability Uplift Sprint #4 (Mini-ADR U-29) — richer resolver
    return so ``skill_view`` can dispatch on lifecycle state.

    ``skill is None`` → unknown for this tenant; ``skill_view`` returns
    NOT FOUND. ``skill.status``:

    * ``ACTIVE`` / ``STALE`` → ``version`` is the latest published row;
      ``skill_view`` reads + bumps activity. ``STALE`` auto-revives on
      bump.
    * ``ARCHIVED`` → ``version`` may still be populated for forensic
      inspection but ``skill_view`` returns ``[BLOCKED]`` rather than
      content; cold path expected to be near-zero in steady state.
    * ``DRAFT`` → treated identically to "unknown" for the agent's
      purposes (the agent never sees draft skills via skill_view).
    """

    skill: Skill | None
    version: SkillVersion | None


@runtime_checkable
class SkillResolver(Protocol):
    """Minimum surface :class:`SkillViewTool` needs.

    Production wiring: a thin shim over :class:`SkillStore.get_skill_by_name`
    + :class:`SkillStore.get_version_by_number`. Tests inject a
    :class:`RecordingSkillResolver` for determinism.
    """

    async def resolve(self, *, tenant_id: UUID, skill_name: str) -> SkillResolution:
        """Return the resolution for ``skill_name``.

        Always returns a :class:`SkillResolution` (never ``None``); a
        truly unknown skill manifests as ``SkillResolution(skill=None,
        version=None)``.
        """


@dataclass(frozen=True)
class RecordingSkillResolver:
    """In-memory :class:`SkillResolver` for tests.

    Test authors construct one of two mappings:

    * ``skills``: ``(tenant_id, skill_name) → Skill`` — drives the
      status dispatch path (active / stale / archived / draft).
    * ``versions``: ``(tenant_id, skill_name) → SkillVersion`` — the
      content side, fetched only when ``skill.status`` is in the
      readable set.
    """

    versions: Mapping[tuple[UUID, str], SkillVersion]
    skills: Mapping[tuple[UUID, str], Skill] = field(default_factory=dict)

    async def resolve(self, *, tenant_id: UUID, skill_name: str) -> SkillResolution:
        key = (tenant_id, skill_name)
        version = self.versions.get(key)
        skill = self.skills.get(key)
        if skill is None and version is not None:
            # Tests that only supply ``versions`` get a synthetic ACTIVE
            # skill so the dispatch path doesn't refuse to read.
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            skill = Skill(
                id=version.skill_id,
                tenant_id=tenant_id,
                name=skill_name,
                status=SkillStatus.ACTIVE,
                latest_version=version.version,
                description=version.description,
                category=version.category,
                created_at=now,
                updated_at=now,
            )
        return SkillResolution(skill=skill, version=version)


@dataclass(frozen=True)
class SkillViewTool:
    """The ``skill_view`` tool — Capability Uplift Sprint #3.

    Stateless across calls; the per-tenant scope comes from
    ``ctx.tenant_id`` which the orchestrator's ReAct loop populates from
    the run binding. ``allowed_skill_names`` is the manifest's
    ``skills:`` list (parsed by Stream J.7a); the tool refuses to load
    anything outside this set so a poisoned skill body can't pivot to
    reading other tenants' skills via this surface.
    """

    resolver: SkillResolver
    allowed_skill_names: frozenset[str]
    content_char_cap: int = SKILL_VIEW_CONTENT_CAP
    # Capability Uplift Sprint #4 — Mini-ADR U-27. ``None`` keeps the
    # tool runnable in tests without a Curator stack; production wires
    # a :class:`ThrottledActivityRecorder`.
    activity_recorder: SkillActivityRecorder | None = None
    # skill-asset-store — object store for externalized supporting files
    # (dual-read); ``None`` keeps inline-only behavior.
    skill_asset_store: SkillAssetObjectStore | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="skill_view",
            description=(
                "Read a file from one of the available skills. The skills you "
                "can reach are listed in the system prompt under "
                '<available-skills>. Start with `path="SKILL.md"`: it returns '
                "the skill's main body and, at the end, the list of its "
                "supporting files. Then pass one of those relative paths "
                "(e.g. `reference/error_codes.md`) to read it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": (
                            "Name of the skill (must be one of those listed in <available-skills>)."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            'File path within the skill — "SKILL.md" or a '
                            "relative supporting-file path."
                        ),
                    },
                },
                "required": ["skill_name", "path"],
            },
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = "skill_view requires a tenant binding"
            raise ToolBlockedError(msg)

        skill_name = self._require_str(args, "skill_name")
        path = self._require_str(args, "path")

        if skill_name not in self.allowed_skill_names:
            # Stay quiet about whether the skill exists for other tenants
            # — only signal that this agent can't reach it.
            return ToolResult(
                content=(f"[NOT AVAILABLE: skill {skill_name!r} is not in the agent's allowlist]"),
                meta={"skill_name": skill_name, "result": "not_allowed"},
            )

        resolution = await self.resolver.resolve(tenant_id=ctx.tenant_id, skill_name=skill_name)
        skill = resolution.skill
        version = resolution.version

        # Sprint #4 (Mini-ADR U-29) — dispatch on the parent skill's
        # lifecycle state before reading any content. ARCHIVED takes
        # priority over the version probe; DRAFT and missing both
        # surface as NOT FOUND for the agent.
        if skill is None or skill.status == SkillStatus.DRAFT:
            record_skill_view(result="not_found")
            return ToolResult(
                content=f"[NOT FOUND: skill {skill_name!r}]",
                meta={"skill_name": skill_name, "result": "not_found"},
            )
        if skill.status == SkillStatus.ARCHIVED:
            record_skill_view_archived_blocked()
            # Audit row is the orchestrator caller's job (it builds the
            # per-tool-call audit envelope downstream). We log a
            # structured marker so SecOps can correlate the metric
            # bump to the offending agent / skill pair.
            logger.warning(
                "skill_view.archived_blocked skill=%s path=%s",
                skill_name,
                path,
            )
            return ToolResult(
                content=(
                    f"[BLOCKED: skill {skill_name!r} is archived — contact "
                    f"a tenant admin to unarchive before reading]"
                ),
                meta={
                    "skill_name": skill_name,
                    "result": "archived",
                    "is_error": True,
                },
            )
        if version is None:
            record_skill_view(result="not_found")
            return ToolResult(
                content=f"[NOT FOUND: skill {skill_name!r}]",
                meta={"skill_name": skill_name, "result": "not_found"},
            )

        # Sprint #4 (Mini-ADR U-27) — mark this skill as "just used".
        # Fires for both ACTIVE and STALE; STALE rows auto-revive to
        # ACTIVE inside the recorder's SQL UPDATE. The recorder owns
        # throttling + error swallowing.
        if self.activity_recorder is not None:
            try:
                await self.activity_recorder.record(skill_id=skill.id, tenant_id=ctx.tenant_id)
            except Exception:  # noqa: S110 — best-effort hot path
                # ThrottledActivityRecorder swallows its own errors;
                # this guard is belt-and-braces for non-default recorders.
                pass

        # ── U-21 step 1: drift check ────────────────────────────────
        jsonable_files = supporting_files_to_jsonable(version.supporting_files)
        recomputed_hash = compute_content_hash(version.prompt_fragment, jsonable_files)
        if recomputed_hash != version.content_hash:
            record_skill_drift()
            # Audit is the caller's job (orchestrator builds a tool-call
            # audit envelope downstream). We log a structured marker so
            # SecOps can correlate; no user content goes into extra=
            # per [memory:codeql-log-injection-request-taint].
            logger.warning(
                "skill_view.drift_detected skill=%s path=%s",
                skill_name,
                path,
            )
            return ToolResult(
                content=(f"[BLOCKED: skill content drift detected for {skill_name!r}/{path}]"),
                meta={
                    "skill_name": skill_name,
                    "result": "drift",
                    "is_error": True,
                },
            )

        # ── Extract requested content ───────────────────────────────
        manifest = ""
        if path == "SKILL.md":
            content = _repack_skill_md(version)
            # B-84 —— 附属文件清单从系统提示词搬到这里。它过去是每个
            # ``<skill>`` 摘要的 ``files=``,每轮 prefill 都重付:对接方两个
            # Agent 分别列了 197 / 232 个文件名。可**选**技能靠 description,
            # 文件清单是**用**技能时才需要的 —— 挪到这一跳,用到才付费。
            # 没有它模型就发现不了附属文件:skill_view 只认精确 path,
            # 没有列目录的能力。
            manifest = _supporting_file_manifest(version)
        else:
            file_entry = version.supporting_files.get(path)
            if file_entry is None:
                record_skill_view(result="not_found")
                return ToolResult(
                    content=(f"[NOT FOUND: {path!r} in skill {skill_name!r}]"),
                    meta={"skill_name": skill_name, "result": "not_found"},
                )
            content = await _decode_supporting_file(file_entry, object_store=self.skill_asset_store)

        # ── U-21 step 2: context-scope re-scan ──────────────────────
        findings = scan_for_threats(content, scope="context")
        if findings:
            record_threat_pattern_hits(findings, scope="context")
            record_skill_redacted()
            logger.warning(
                "skill_view.context_match skill=%s path=%s findings=%d",
                skill_name,
                path,
                len(findings),
            )
            return ToolResult(
                content=("[BLOCKED: content matched threat pattern at runtime]"),
                meta={
                    "skill_name": skill_name,
                    "result": "redacted",
                    "is_error": True,
                },
            )

        # 清单拼在威胁扫描**之后**:这些字符串此前逐字出现在每一份系统提示词里、
        # 从不过扫描,把它们挪到扫描前面等于顺手改了失败形态(一个文件名撞上模式,
        # 整个技能就读不出来了)——那是另一件事,不该捆在一次瘦身里。
        # 拼在末尾还有一个好处:``_middle_trim`` 砍的是中段,清单跟着 tail 活下来。
        if manifest:
            content = f"{content}\n\n{manifest}"

        # ── Truncate to LLM-friendly size ───────────────────────────
        rendered, truncated = _middle_trim(content, self.content_char_cap)
        record_skill_view(result="truncated" if truncated else "ok")
        return ToolResult(
            content=rendered,
            meta={
                "skill_name": skill_name,
                # RT-2 PR-3 (RT-ADR-7) — source path for the reference stub
                # the context gates leave when this result is compressed.
                # Success-only by design: failure branches carry no path so a
                # blocked read can never be rewritten into a re-read hint.
                "path": path,
                "result": "truncated" if truncated else "ok",
                "truncated": truncated,
            },
        )

    @staticmethod
    def _require_str(args: Mapping[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value:
            msg = f"skill_view requires non-empty {key!r}"
            raise ToolBlockedError(msg)
        return value


def _supporting_file_manifest(version: SkillVersion) -> str:
    """技能附属文件清单,拼在 ``SKILL.md`` 正文末尾(B-84)。

    没有附属文件就返回空串 —— 一个只有正文的技能不该为一段空清单付字符。
    """
    names = sorted(version.supporting_files)
    if not names:
        return ""
    listed = "\n".join(f"- {name}" for name in names)
    return (
        "## Supporting files\n"
        "Load any of these with skill_view(skill_name, path), or run them from "
        "$EXPERT_WORK_SKILLS_DIR/<skill name>/ inside the sandbox.\n"
        f"{listed}"
    )


def _repack_skill_md(version: SkillVersion) -> str:
    """Reconstruct the canonical SKILL.md text from a SkillVersion row."""
    parsed = ParsedSkillMd(
        name=_skill_name_for_repack(version),
        description=version.description or _skill_name_for_repack(version),
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


def _skill_name_for_repack(version: SkillVersion) -> str:
    """SkillVersion only knows the skill_id, not the skill row's name.
    For SKILL.md re-pack we need the name — fall back to the version's
    description or a synthetic name. In practice the caller (orchestrator)
    can pass an enriched version with the name baked in via the
    ``allowed_skill_names`` lookup; for now this default keeps round-trip
    deterministic without a second store fetch."""
    # The skill_name is the lookup key the agent used. If the
    # `SkillResolver` is the production shim it will have stamped the
    # name into a side channel — but to keep the DTO minimal we use the
    # description as a stable string. The body is what matters; an
    # empty / synthetic name in SKILL.md re-pack is acceptable for an
    # internal-only tool consumption.
    return version.description.split("\n", 1)[0] or "skill"


async def _decode_supporting_file(entry: object, *, object_store: object | None) -> str:
    """Resolve one supporting-file entry to text. Decode + best-effort
    UTF-8 (binary files come back as a [BINARY: ...] marker so the LLM
    knows not to try parsing them as prose).

    Dual-read (skill-asset-store): typed entries go through
    :func:`fetch_supporting_file` — inline base64 or object-store fetch
    with digest verification; raw dict entries (legacy tests) keep the
    inline-only path."""
    import base64

    mime = getattr(entry, "mime", "") or ""
    size = getattr(entry, "size", 0)
    if isinstance(entry, SkillSupportingFile):
        try:
            raw = await fetch_supporting_file(entry, object_store=object_store)  # type: ignore[arg-type]
        except (ValueError, TypeError, binascii.Error):
            return f"[BINARY: corrupt content, {size} bytes, mime={mime!r}]"
        except SkillAssetIntegrityError:
            return f"[BLOCKED: stored asset failed integrity check, mime={mime!r}]"
        except Exception:
            return f"[NOT AVAILABLE: asset store unreachable, {size} bytes, mime={mime!r}]"
    else:
        if isinstance(entry, dict):
            raw_b64 = entry.get("content", "")
            mime = entry.get("mime", "") or ""
            size = entry.get("size", 0)
        elif hasattr(entry, "content"):
            raw_b64 = entry.content
        else:
            return "[BINARY: unknown entry format]"
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except (ValueError, TypeError):
            return f"[BINARY: corrupt content, {size} bytes, mime={mime!r}]"
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"[BINARY: {size} bytes, mime={mime!r}]"


def _middle_trim(text: str, cap: int) -> tuple[str, bool]:
    """Same middle-truncation pattern as ``MCPTool`` — keeps head + tail
    50% so the LLM sees both ends of a long file."""
    if len(text) <= cap:
        return text, False
    half = cap // 2
    dropped = len(text) - cap
    head = text[:half]
    tail = text[-half:]
    return (
        f"{head}\n{_TRUNCATION_PREFIX}{dropped}{_TRUNCATION_SUFFIX}\n{tail}",
        True,
    )
