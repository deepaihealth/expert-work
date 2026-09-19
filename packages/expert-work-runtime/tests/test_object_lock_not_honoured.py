"""合规锁被后端静默吞掉时,``put`` 必须报错 —— 不能假装成功。

**这条钉的不是一个假想的后端。** 2026-09-19 在阿里云 OSS 的 S3 兼容层上实测
(``expert-work-test`` 桶,该桶没有开 Object Lock):

* ``put_object`` 带 ``ObjectLockMode=COMPLIANCE`` + ``ObjectLockRetainUntilDate``
  —— **返回成功**。真 S3 / MinIO 在桶没开 Object Lock 时报 ``InvalidRequest``。
* ``head_object`` 回读 —— ``ObjectLockMode=None`` / ``ObjectLockRetainUntilDate=None``。
* ``delete_object`` —— **删掉了**。

代价不是「少了一层保护」那么轻:D.1c 的审计 WORM 备份 worker 把「写进去了」当成
「已受保护」并置 ``backup_acked=true``,而那正是留存清理 job 删掉库里审计行的
**许可证**。静默的假锁 = 照着一张假许可证删审计数据。

所以 ``S3CompatibleObjectStore.put`` 在请求了锁之后会多走一趟 HEAD 回读确认。
这个文件用一个最小假客户端复现两种后端形状,并钉住两边的行为。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from expert_work.runtime.storage import (
    ObjectLockNotHonouredError,
    ObjectStoreError,
    S3CompatibleObjectStore,
)


class _FakeClientError(Exception):
    """站位 botocore 的 ``ClientError``(测试不依赖 botocore 的异常形状)。"""


class _FakeExceptions:
    ClientError = _FakeClientError


class _FakeS3Client:
    """最小 S3 假客户端。

    ``honour_lock`` 就是这个文件要区分的那一件事:

    * ``True``  —— S3 / MinIO 的行为:锁参数被存下来,HEAD 能读回。
    * ``False`` —— 阿里云 OSS 的行为:put 成功,但锁参数一个字节都不存。

    ``drop_retain_until`` 是第三种形态:mode 回来了、**到期时间没回来**。
    分出这一种是因为前两种撞不到 ``got_until is None`` 那半边判断 ——
    不支持锁的后端两个字段一起是空的,永远先撞在 mode 上,于是那半边
    在「不可能失败的条件下」被验证(变异自证实锤:把它删掉,前两条照样绿)。
    """

    def __init__(self, *, honour_lock: bool, drop_retain_until: bool = False) -> None:
        self._honour_lock = honour_lock
        self._drop_retain_until = drop_retain_until
        self.exceptions = _FakeExceptions()
        self.puts: list[dict[str, Any]] = []
        self.heads: list[str] = []
        self._objects: dict[str, dict[str, Any]] = {}

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        stored: dict[str, Any] = {}
        if self._honour_lock:
            if "ObjectLockMode" in kwargs:
                stored["ObjectLockMode"] = kwargs["ObjectLockMode"]
            if "ObjectLockRetainUntilDate" in kwargs and not self._drop_retain_until:
                stored["ObjectLockRetainUntilDate"] = kwargs["ObjectLockRetainUntilDate"]
        self._objects[kwargs["Key"]] = stored
        return {}

    async def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.heads.append(Key)
        if Key not in self._objects:
            raise _FakeClientError("404")
        return dict(self._objects[Key])


def _store(client: _FakeS3Client) -> S3CompatibleObjectStore:
    return S3CompatibleObjectStore(client, "bucket")


_RETAIN = datetime.now(tz=UTC) + timedelta(days=90)


@pytest.mark.asyncio
async def test_lock_silently_dropped_is_an_error() -> None:
    """OSS 形态:put 成功但锁没落地 → 必须抛,而且是**可分辨的类型**。"""
    client = _FakeS3Client(honour_lock=False)
    with pytest.raises(ObjectLockNotHonouredError) as excinfo:
        await _store(client).put(
            "audit/1.json", b"{}", retain_until=_RETAIN, lock_mode="compliance"
        )
    # 报错里要能看出「读回来的是什么」,否则排查时只知道失败不知道为什么。
    assert "ObjectLockMode=None" in str(excinfo.value)
    assert "NOT" in str(excinfo.value)
    # 宽 except 仍然接得住 —— 调用方不需要为这条新写 except 分支。
    assert isinstance(excinfo.value, ObjectStoreError)
    # 确实发了锁参数(否则这条测试测的是「我们压根没请求锁」,恒真)。
    assert client.puts[0]["ObjectLockMode"] == "COMPLIANCE"
    assert client.puts[0]["ObjectLockRetainUntilDate"] == _RETAIN


@pytest.mark.asyncio
async def test_lock_honoured_passes() -> None:
    """S3 / MinIO 形态:锁真的落了 → 不抛,且确实回读验证过。"""
    client = _FakeS3Client(honour_lock=True)
    await _store(client).put("audit/1.json", b"{}", retain_until=_RETAIN, lock_mode="compliance")
    assert client.heads == ["audit/1.json"]


@pytest.mark.asyncio
async def test_unlocked_put_does_not_pay_for_a_head() -> None:
    """普通 put 一趟 HEAD 都不多走 —— 代价只落在真要锁的那条路上。

    没有这条,「加一次回读」很容易变成「每次 put 都多一次往返」,而
    ``put`` 是上传/产物路径上最热的调用之一。
    """
    client = _FakeS3Client(honour_lock=False)
    await _store(client).put("plain.json", b"{}")
    assert client.heads == []


@pytest.mark.asyncio
async def test_mode_without_retain_until_is_also_an_error() -> None:
    """只回 ``ObjectLockMode`` 而不回到期时间,同样不算真锁住。

    一个有模式却没有到期时间的对象,「保护到什么时候」是未定义的 —— 而调用方
    要的保证恰恰是「到某个时刻之前不可删」。这一条单独存在是因为前两条都撞在
    mode 上,验不到 ``got_until`` 那半边(见 ``_FakeS3Client`` 的说明)。
    """
    client = _FakeS3Client(honour_lock=True, drop_retain_until=True)
    with pytest.raises(ObjectLockNotHonouredError) as excinfo:
        await _store(client).put(
            "audit/2.json", b"{}", retain_until=_RETAIN, lock_mode="compliance"
        )
    assert "ObjectLockRetainUntilDate=None" in str(excinfo.value)
