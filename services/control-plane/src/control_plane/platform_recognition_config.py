"""``PlatformRecognitionConfigService`` —— B-209 §5.7「平台设置 → 模型服务 → 文档识别」。

平台级、所有租户共用(设计 ⑦):扫描页识别模型、兜底识别模型、图片描述模型(PR-2)、单份
文档识别页数上限。数据库里有行就以行为准(某一对为空 = 关掉它);没有行用默认值:
glm-ocr / qwen3.7-flash / qwen3.8-flash / 1,000 页(设计 ⑧ ⑪)。

同 :class:`PlatformJudgeConfigService`:读出的结果按 TTL 缓存,写接口调 :meth:`invalidate`
立即生效,其它副本靠失效总线(``platform_recognition``)。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from control_plane.knowledge.recognition.recognizer import ModelChoice, RecognitionSettings
from expert_work.persistence.platform_recognition_config import (
    PlatformRecognitionConfigRecord,
    PlatformRecognitionConfigStore,
)

#: B-209 §0 ⑧ ⑪ / §5.7 —— used until an admin saves the section.
DEFAULT_RECOGNITION_CONFIG = PlatformRecognitionConfigRecord(
    ocr_provider="glm",
    ocr_model="glm-ocr",
    fallback_provider="qwen",
    fallback_model="qwen3.7-flash",
    figure_provider="qwen",
    figure_model="qwen3.8-flash",
    max_pages=1000,
)


def _choice(provider: str | None, model: str | None) -> ModelChoice | None:
    return ModelChoice(provider, model) if provider and model else None


class PlatformRecognitionConfigService:
    """DB-wins / built-in-default document-recognition config, TTL-cached."""

    def __init__(
        self,
        *,
        store: PlatformRecognitionConfigStore,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._record = DEFAULT_RECOGNITION_CONFIG
        self._is_default = True
        self._loaded = False
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def effective(self) -> tuple[PlatformRecognitionConfigRecord, bool]:
        """The effective config and whether it is the built-in default."""
        await self._maybe_refresh()
        return self._record, self._is_default

    async def recognition_settings(self) -> RecognitionSettings:
        record, _ = await self.effective()
        return RecognitionSettings(
            primary=_choice(record.ocr_provider, record.ocr_model),
            fallback=_choice(record.fallback_provider, record.fallback_model),
            max_pages=record.max_pages,
            figure=_choice(record.figure_provider, record.figure_model),
        )

    async def put(self, record: PlatformRecognitionConfigRecord) -> None:
        await self._store.put(record)
        self.invalidate()

    def invalidate(self) -> None:
        self._expires_at = 0.0

    async def _maybe_refresh(self) -> None:
        if self._loaded and self._clock() < self._expires_at:
            return
        async with self._lock:
            if self._loaded and self._clock() < self._expires_at:
                return
            row = await self._store.get()
            self._record = row if row is not None else DEFAULT_RECOGNITION_CONFIG
            self._is_default = row is None
            self._loaded = True
            self._expires_at = self._clock() + self._ttl_seconds
