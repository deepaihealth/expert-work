"""J.7a Skill 内容 admin 写入期 moderation — Mini-ADR J-23 § 15.6.

M0 实施轻量正则 deny-list + size cap. LLM-based moderation 升级推 M1-K
(需 LLM router + budget cap, 见 ITERATION-PLAN § M1-K). 目标: 拦截
最常见的 prompt injection 模式 + 防 admin 误塞超大 payload.

调用方: ``api/skills.py`` 在 POST/PUT skill + version + ZIP import 路径
传 ``prompt_fragment`` 入这里, 触犯返回 :class:`ModerationError`
(被路由 catch 并映射 400). Audit 行无需额外字段 (默认 SUCCESS / DENIED
状态机够用).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

#: Soft cap on ``prompt_fragment`` size. 256 KiB is generous for human-
#: authored skill text (typical ~5-20 KiB) while still catching an admin
#: pasting an entire codebase. The original 64 KiB rejected real curated
#: skills whose SKILL.md is a full playbook (live driver: ppt-master ≈ 77
#: KiB); progressive disclosure (lazy_load, RT-ADR-11) means the body is
#: skill_view-fetched on demand, not eagerly injected, so a large SKILL.md
#: does not bloat every turn's context. M1-K LLM moderation can lift this.
MAX_PROMPT_FRAGMENT_BYTES: Final[int] = 256 * 1024

#: B-84 item 7 —— **eager**(``lazy_load is False``)正文的上架上限,单位是
#: **字符**不是字节(提示词预算按字符看得见,UTF-8 字节数对写技能的人没有意义)。
#:
#: 为什么它是**条件**的、不能一刀切:
#:
#: * lazy 正文只在模型真的调 ``skill_view`` 时才加载,不进系统提示词。
#:   2026-09-20 测试环境实测 ``prompt_fragment`` 长度:最大 46,741 /
#:   p90 19,181 / 中位 7,527 字符 —— 8,000 一刀切会当场打死存量里
#:   一大半技能,而它们并没有在每轮付这笔钱。所以 lazy 路径继续走
#:   :data:`MAX_PROMPT_FRAGMENT_BYTES`(256 KiB),一个字节都不动。
#: * eager 正文是**每轮**整段进系统提示词的。256 KiB ≈ 64k token,
#:   一个 eager 技能就能把整个上下文窗口吃掉,而且每一次 prefill 重付一遍。
#:
#: 无需 grandfathering:2026-09-20 测试环境实测 57 个活跃技能里
#: **eager(lazy_load=False)= 0 个** —— 这条护栏落地时存量一个都不违反,
#: 它拦的是未来新塞进来的。
MAX_EAGER_PROMPT_FRAGMENT_CHARS: Final[int] = 8_000

#: Max tool names per skill version. > 32 tools in one skill is almost
#: certainly a mistake (or an attempted denial-of-service against the
#: agent's tool registry).
MAX_TOOL_NAMES_PER_VERSION: Final[int] = 32

#: Max required_models entries per skill version.
MAX_REQUIRED_MODELS_PER_VERSION: Final[int] = 16

#: Regex patterns the moderation rejects. Each pattern targets a known
#: prompt-injection idiom (case-insensitive). The list is intentionally
#: conservative: M1-K replaces this with LLM-graded moderation, but
#: catching the obvious cases at M0 prevents the worst exploits today.
_DENY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # ``ignore`` / ``disregard`` followed by an arbitrary phrase ending
    # in ``instructions?``. Non-greedy ``[\w\s]{0,40}`` tolerates filler
    # words ("all previous", "the prior", "every single") without
    # exploding the regex.
    re.compile(r"\bignore[\w\s]{0,40}?\binstructions?\b", re.IGNORECASE),
    re.compile(r"\bdisregard[\w\s]{0,40}?\binstructions?\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(everything|all|the\s+above)\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+a\s+different\b", re.IGNORECASE),
    re.compile(r"\bsystem\s*:\s*you\s+(must|will)\s+", re.IGNORECASE),
)


@dataclass(frozen=True)
class ModerationError(ValueError):
    """``prompt_fragment`` failed admin moderation.

    Carries the reason as a stable code so the API layer can produce
    a structured 400 response (caller picks the user-facing wording).
    """

    code: str
    detail: str

    def __post_init__(self) -> None:
        # ValueError.__init__ already ran via dataclass; re-args for ``str()``.
        super().__init__(self.detail)


def moderate_prompt_fragment(text: str, *, lazy_load: bool) -> None:
    """Apply M0 deny-list + size cap; raise :class:`ModerationError` on hit.

    Returns silently on a pass. Run this exactly once per admin write
    path (POST skill / POST version / ZIP import); double-running is
    safe but wastes CPU on large fragments.

    ``lazy_load`` is the ``lazy_load`` the version being written will
    actually carry — it selects which size cap applies (see
    :data:`MAX_EAGER_PROMPT_FRAGMENT_CHARS`). It is deliberately a
    **required** keyword: a default would let a newly-added admin write
    path silently skip the eager cap, and "忘了静默跑偏" is exactly the
    failure mode this guard exists to prevent.
    """
    if len(text.encode("utf-8")) > MAX_PROMPT_FRAGMENT_BYTES:
        raise ModerationError(
            code="prompt_fragment_too_large",
            detail=(
                f"prompt_fragment exceeds {MAX_PROMPT_FRAGMENT_BYTES} byte limit "
                f"(M0 admin moderation; M1-K LLM moderation can lift this)"
            ),
        )
    if not lazy_load and len(text) > MAX_EAGER_PROMPT_FRAGMENT_CHARS:
        raise ModerationError(
            code="eager_prompt_fragment_too_large",
            detail=(
                f"prompt_fragment is {len(text)} characters — "
                f"{len(text) - MAX_EAGER_PROMPT_FRAGMENT_CHARS} over the "
                f"{MAX_EAGER_PROMPT_FRAGMENT_CHARS}-character limit that applies to an "
                f"eager (lazy_load=false) skill, whose whole body is re-sent in the "
                f"system prompt on every turn. Two ways out: (1) make the skill lazy "
                f"(set `lazy: true` in its SKILL.md expert_work block) so the body "
                f"loads on demand via skill_view and only the "
                f"{MAX_PROMPT_FRAGMENT_BYTES}-byte limit applies, or (2) split it into "
                f"several smaller skills."
            ),
        )
    for pat in _DENY_PATTERNS:
        match = pat.search(text)
        if match is not None:
            raise ModerationError(
                code="prompt_injection_pattern",
                detail=(
                    f"prompt_fragment contains a known prompt-injection pattern: "
                    f"{match.group(0)!r}. Rephrase the skill instructions or escalate "
                    f"to an admin who can disable moderation."
                ),
            )


def moderate_tool_names(tool_names: Iterable[str]) -> None:
    """Cap the tool_names list size."""
    count = sum(1 for _ in tool_names)
    if count > MAX_TOOL_NAMES_PER_VERSION:
        raise ModerationError(
            code="too_many_tool_names",
            detail=(
                f"skill version declares {count} tool_names; M0 admin moderation caps "
                f"at {MAX_TOOL_NAMES_PER_VERSION} (split into multiple skills if "
                f"genuinely needed)"
            ),
        )


def moderate_required_models(required_models: Iterable[str]) -> None:
    """Cap the required_models list size."""
    count = sum(1 for _ in required_models)
    if count > MAX_REQUIRED_MODELS_PER_VERSION:
        raise ModerationError(
            code="too_many_required_models",
            detail=(
                f"skill version declares {count} required_models; M0 admin moderation "
                f"caps at {MAX_REQUIRED_MODELS_PER_VERSION}"
            ),
        )
