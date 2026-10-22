"""Single-row platform document-recognition config store — B-209."""

from expert_work.persistence.platform_recognition_config.base import (
    PlatformRecognitionConfigRecord,
    PlatformRecognitionConfigStore,
)
from expert_work.persistence.platform_recognition_config.memory import (
    InMemoryPlatformRecognitionConfigStore,
)
from expert_work.persistence.platform_recognition_config.sql import (
    SqlPlatformRecognitionConfigStore,
)

__all__ = [
    "InMemoryPlatformRecognitionConfigStore",
    "PlatformRecognitionConfigRecord",
    "PlatformRecognitionConfigStore",
    "SqlPlatformRecognitionConfigStore",
]
