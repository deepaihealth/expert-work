"""B-286 —— 留存作业的 ``SharedStorageSettings`` 照抄了 control-plane ``Settings`` 里对象存储 /
SecretStore 的那几个键(作业不 import control-plane)。CronJob 经 envFrom 拿到的是同一份
``EXPERT_WORK_*``:两边的名字、默认值、类型或环境变量前缀一旦分叉,作业就会悄悄解析出另一个
对象存储(或什么都不解析)。这里把两边钉在一起。"""

from __future__ import annotations

from control_plane.settings import Settings
from retention_cleanup_job.object_store import SharedStorageSettings


def test_shared_storage_fields_match_the_control_plane_settings() -> None:
    shared = SharedStorageSettings.model_fields
    control = Settings.model_fields

    missing = sorted(name for name in shared if name not in control)
    assert missing == []
    for name, info in shared.items():
        mine, theirs = info, control[name]
        assert (name, mine.annotation) == (name, theirs.annotation)
        assert (name, mine.default) == (name, theirs.default)
        assert (name, mine.alias, mine.validation_alias) == (
            name,
            theirs.alias,
            theirs.validation_alias,
        )


def test_shared_storage_reads_the_same_environment_variables() -> None:
    for key in ("env_prefix", "case_sensitive"):
        assert SharedStorageSettings.model_config.get(key) == Settings.model_config.get(key)


def test_shared_storage_covers_every_object_store_and_secret_store_key() -> None:
    """A new ``object_store_*`` / ``secret_store_*`` knob on the control plane must be copied to
    the job too (or consciously left out here)."""
    prefixes = ("object_store_", "secret_store_", "secret_encryption_")
    control = {name for name in Settings.model_fields if name.startswith(prefixes)}

    assert control - set(SharedStorageSettings.model_fields) == set()
