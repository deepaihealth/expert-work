"""B-286 —— 留存作业用和 control-plane **同一个**对象存储。

此前作业只认 ``EXPERT_WORK_RETENTION_OBJECT_STORE_*``(默认 ``memory``、要明文 AK/SK),而 CronJob
经 envFrom 拿到的是 control-plane 那套 ``EXPERT_WORK_OBJECT_STORE_*``:密钥是 ``secret://`` 引用,
control-plane 经它的 SecretStore(测试 / 生产是 ``sql_encrypted``:库里 AES-GCM 加密、KEK 在
``EXPERT_WORK_SECRET_ENCRYPTION_KEY``)解出来。作业从不解析,于是图片字节那一 pass 在测试 / 生产
从来没跑过。

现在作业读同一套 ``EXPERT_WORK_OBJECT_STORE_*`` / ``EXPERT_WORK_SECRET_STORE_*``,用同一份代码
解引用(:func:`expert_work.runtime.storage.config.resolve_object_store_config` +
:class:`expert_work.runtime.secret_store.sql_encrypted.SqlEncryptedSecretStore`,不另写加解密),
连同一个库(DSN 与 control-plane 相同,读得到 ``encrypted_secret``)。

``EXPERT_WORK_RETENTION_OBJECT_STORE_BACKEND`` **显式设了**的,照旧走作业自己那套(明文
``EXPERT_WORK_RETENTION_OBJECT_STORE_ACCESS_KEY`` / ``…_SECRET_KEY``),留给本地 / 单独部署。

解不出来(KEK 缺 / 不对、密钥行不在、库读不了、配置不全)**不让作业崩**:只记异常类型,依赖对象
存储的 pass 记成跳过并进 ``failed_passes``(进程退出码 3,告警会响),其余 pass 照跑。
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.runtime.secret_store import SecretStore, make_secret_store
from expert_work.runtime.secret_store.sql_encrypted import (
    SqlEncryptedSecretStore,
    build_kek_from_b64,
)
from expert_work.runtime.storage import (
    ObjectStore,
    ObjectStoreBackend,
    S3CompatibleConfig,
    make_object_store,
)
from expert_work.runtime.storage.config import resolve_object_store_config
from retention_cleanup_job.settings import RetentionCleanupSettings

logger = logging.getLogger(__name__)


class SharedStorageSettings(BaseSettings):
    """control-plane 的那几个键(``control_plane.settings.Settings`` 同名同默认值),CronJob 经
    envFrom 拿到的就是它们。其余 ``EXPERT_WORK_*`` 一律忽略。"""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_", case_sensitive=False, extra="ignore"
    )

    object_store_backend: Literal["memory", "s3-compatible"] = "memory"
    object_store_endpoint_url: str | None = None
    object_store_region: str = "us-east-1"
    object_store_bucket: str = "expert-work"
    object_store_addressing_style: Literal["path", "virtual", "auto"] = "path"
    object_store_access_key_ref: str | None = None
    object_store_secret_key_ref: str | None = None
    secret_store_backend: Literal["local_dev", "sql_encrypted", "aliyun_kms"] = "local_dev"  # noqa: S105 — backend selector, not a password
    secret_store_env_file: str | None = None
    secret_encryption_key: SecretStr | None = None


@dataclass(frozen=True)
class ObjectStorePlan:
    """这一轮用哪个对象存储。``backend == "s3-compatible"`` 而 ``config is None`` = 配了但用不了
    (``error`` 是异常类型 / 原因,不含任何密钥),依赖它的 pass 跳过并记失败。"""

    backend: ObjectStoreBackend
    source: Literal["shared", "retention_override"]
    config: S3CompatibleConfig | None = None
    error: str | None = None

    @property
    def unavailable(self) -> bool:
        return self.backend == "s3-compatible" and self.config is None


def build_secret_store(
    shared: SharedStorageSettings, session_factory: async_sessionmaker[AsyncSession]
) -> SecretStore:
    """与 control-plane 同一个 SecretStore 选择:``sql_encrypted`` 要 KEK;其余交给
    :func:`make_secret_store`(``aliyun_kms`` 在那里会报未实现)。"""
    if shared.secret_store_backend == "sql_encrypted":  # noqa: S105 — backend selector
        if shared.secret_encryption_key is None:
            msg = "secret_store_backend='sql_encrypted' requires EXPERT_WORK_SECRET_ENCRYPTION_KEY"
            raise RuntimeError(msg)
        kek = build_kek_from_b64(shared.secret_encryption_key.get_secret_value())
        return SqlEncryptedSecretStore(session_factory, kek=kek)
    return make_secret_store(shared.secret_store_backend, env_file=shared.secret_store_env_file)


async def plan_object_store(
    settings: RetentionCleanupSettings,
    shared: SharedStorageSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> ObjectStorePlan:
    """决定并解析这一轮的对象存储。永不抛:解不出来的回 ``unavailable`` 的 plan。"""
    if "object_store_backend" in settings.model_fields_set:
        return _retention_override(settings)
    if shared.object_store_backend != "s3-compatible":
        return ObjectStorePlan(backend="memory", source="shared")
    try:
        config = await resolve_object_store_config(
            backend=shared.object_store_backend,
            endpoint_url=shared.object_store_endpoint_url,
            region=shared.object_store_region,
            bucket=shared.object_store_bucket,
            access_key_ref=shared.object_store_access_key_ref,
            secret_key_ref=shared.object_store_secret_key_ref,
            secret_store=build_secret_store(shared, session_factory),
            addressing_style=shared.object_store_addressing_style,
        )
    except Exception as exc:
        return ObjectStorePlan(backend="s3-compatible", source="shared", error=type(exc).__name__)
    return ObjectStorePlan(backend="s3-compatible", source="shared", config=config)


def _retention_override(settings: RetentionCleanupSettings) -> ObjectStorePlan:
    """``EXPERT_WORK_RETENTION_OBJECT_STORE_*`` 显式配了:照旧用作业自己那套(明文密钥)。"""
    if settings.object_store_backend != "s3-compatible":
        return ObjectStorePlan(backend="memory", source="retention_override")
    if not (
        settings.object_store_endpoint_url
        and settings.object_store_access_key
        and settings.object_store_secret_key
    ):
        return ObjectStorePlan(
            backend="s3-compatible", source="retention_override", error="incomplete_config"
        )
    return ObjectStorePlan(
        backend="s3-compatible",
        source="retention_override",
        config=S3CompatibleConfig(
            endpoint_url=settings.object_store_endpoint_url,
            region=settings.object_store_region,
            bucket=settings.object_store_bucket,
            access_key=settings.object_store_access_key,
            secret_key=settings.object_store_secret_key,
            addressing_style=settings.object_store_addressing_style,
        ),
    )


async def enter_object_store(
    stack: AsyncExitStack, plan: ObjectStorePlan
) -> tuple[ObjectStore | None, ObjectStorePlan]:
    """按 plan 建对象存储(挂进 ``stack``,作业结束时关);建不起来同样回 ``unavailable``。"""
    if plan.config is None:
        return None, plan
    try:
        store = await stack.enter_async_context(make_object_store(plan.backend, plan.config))
    except Exception as exc:
        return None, replace(plan, config=None, error=type(exc).__name__)
    return store, plan


def log_plan(plan: ObjectStorePlan) -> None:
    """每轮一行:用的是哪个后端、从哪来、能不能用(不打密钥、不打引用)。"""
    if plan.unavailable:
        logger.error(
            "retention.object_store backend=%s source=%s available=false error=%s "
            "— image bytes are not deleted this run",
            plan.backend,
            plan.source,
            plan.error,
        )
        return
    logger.info(
        "retention.object_store backend=%s source=%s available=true bucket=%s",
        plan.backend,
        plan.source,
        plan.config.bucket if plan.config is not None else "-",
    )


__all__ = [
    "ObjectStorePlan",
    "SharedStorageSettings",
    "build_secret_store",
    "enter_object_store",
    "log_plan",
    "plan_object_store",
]
