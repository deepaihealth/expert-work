"""Abstract :class:`PlatformRecognitionConfigStore` — B-209 §5.7.

Single-row singleton storing the platform's document-recognition selection
(「平台设置 → 模型服务 → 文档识别」): the scanned-page recognition model, its
fallback, the figure-description model and the per-document recognition page
cap. Non-secret. Tenant-less (platform-global, shared by every tenant), so —
like ``platform_judge_config`` — there is no RLS policy on the table.

An absent row means "not configured" → the built-in defaults apply.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformRecognitionConfigRecord:
    """The platform's document-recognition selection. A ``None`` pair = that
    model is switched off."""

    ocr_provider: str | None
    ocr_model: str | None
    fallback_provider: str | None
    fallback_model: str | None
    figure_provider: str | None
    figure_model: str | None
    max_pages: int
    updated_by: str | None = None


class PlatformRecognitionConfigStore(abc.ABC):
    """Persistence Protocol for the single-row platform recognition config."""

    @abc.abstractmethod
    async def get(self) -> PlatformRecognitionConfigRecord | None:
        """The singleton row, or ``None`` if not configured."""

    @abc.abstractmethod
    async def put(self, record: PlatformRecognitionConfigRecord) -> None:
        """Upsert the singleton row (last write wins)."""
