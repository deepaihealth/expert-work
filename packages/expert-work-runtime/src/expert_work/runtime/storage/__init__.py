"""Object storage abstraction (per ADR-0004).

S3-compatible Protocol with in-memory + aiobotocore implementations. The
factory ``make_object_store`` mirrors the checkpointer / store / stream
bridge pattern for consistent lifespan management.
"""

from expert_work.runtime.storage.base import LockMode as LockMode
from expert_work.runtime.storage.base import ObjectLockedError as ObjectLockedError
from expert_work.runtime.storage.base import (
    ObjectLockNotHonouredError as ObjectLockNotHonouredError,
)
from expert_work.runtime.storage.base import ObjectNotFoundError as ObjectNotFoundError
from expert_work.runtime.storage.base import ObjectStore as ObjectStore
from expert_work.runtime.storage.base import ObjectStoreError as ObjectStoreError
from expert_work.runtime.storage.factory import (
    ObjectStoreBackend as ObjectStoreBackend,
)
from expert_work.runtime.storage.factory import (
    S3CompatibleConfig as S3CompatibleConfig,
)
from expert_work.runtime.storage.factory import (
    make_object_store as make_object_store,
)
from expert_work.runtime.storage.memory import (
    InMemoryObjectStore as InMemoryObjectStore,
)
from expert_work.runtime.storage.s3_compatible import (
    S3CompatibleObjectStore as S3CompatibleObjectStore,
)

__all__ = [
    "InMemoryObjectStore",
    "LockMode",
    "ObjectLockNotHonouredError",
    "ObjectLockedError",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreBackend",
    "ObjectStoreError",
    "S3CompatibleConfig",
    "S3CompatibleObjectStore",
    "make_object_store",
]
