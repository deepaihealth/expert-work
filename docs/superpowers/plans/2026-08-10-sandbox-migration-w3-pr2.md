# 沙箱迁移波 3 PR-2(janitor + 收尾)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除收尾闭环:`ObjectStore.put_stream`(流式 multipart,杀 1.5GiB 内存上限)+ `WorkspaceJanitorWorker` 三阶段(归档软删用户 → 配额全量扫 → `_scratch` 清理)+ 运维页「已用/上限」+ runbook + SetDirQuota 调研结论。

**Architecture:** janitor 是 control-plane 内新后台 worker(30 分钟一轮,advisory lock 单飞,classid 8619)。归档 = 流式 tar.gz(线程内 `tarfile w|gz` → 有界队列 → async 喂 multipart,常驻内存 ≤ 单分片)传 OSS 确定性 key,先传后删 `mark_archived` 最后。阶段 2 复用 PR-1 的 `WorkspaceQuotaService.refresh`(docstring 已自称 janitor 入口)。90 天硬删**不做**——`RetentionCleanupJob._sweep_workspaces` 已拥有(`list_archived_expired` → OSS delete → `hard_delete`),janitor 只到 `mark_archived` 为止。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / aiobotocore / pytest;React + antd + i18next(admin-ui)。

**Spec:** `docs/superpowers/specs/2026-08-09-sandbox-migration-w3-design.md`(§ 四、§ 五、§ 六、§ 七、§ 八)。

**两条硬要求(PR-1 终审已知缺口的收敛,用户拍板记录)**:
1. janitor 清扫覆盖软删目录,含「行已 `archived` 但目录被软删用户后续上传复活」的**重新收割**(`write_file`/上传路径不查软删标记是 W2 既有设计)→ Task 5。
2. runbook 写明 PR-1→PR-2 窗口期「软删用户上传不入账」的行为 → Task 8。

## Global Constraints

- 归档 key **确定性** = `workspace-archives/{tenant_id}/{user_id}/{workspace_id}.tar.gz`(workspace_id = `user_workspace` 行 UUID);重试幂等覆盖,不产生重复档案(spec § 4.1)。
- 崩溃安全顺序:**先传后删,`mark_archived` 最后**;重入矩阵见 Task 5(spec § 4.1 + 硬要求①收割复活目录:目录在就重做归档覆盖上传,行已 archived 则不重 mark)。
- janitor:30 分钟一轮(`workspace_janitor_interval_s` 默认 1800);三阶段顺序固定 归档→全量扫→`_scratch`;advisory lock 单飞 **classid 8619**(注册表:workspace_lock=1 / mcp_oauth=2 / drift=8615 / consolidator=8616 / curator=8617 / tenant_resource=8618),拿不到锁静默跳过;**不建 DLQ**;每单目录/单用户失败 log + 继续。
- `_scratch/<sandbox_id>` 判据 = 目录 mtime 距今 > 24h,不查 DB。
- `put_stream` 常驻内存 ≤ 单分片;S3 分片默认 64 MiB(`_MULTIPART_PART_SIZE`,S3 min part 5 MiB);**实现内部攒片**,契约不要求调用方 chunk 尺寸;整流 < 一片 → 退化单次 `put_object`(顺带解决空流)。
- 老 `archive_volume(max_bytes=…)` 及 1.5GiB 语义**不动**(supervisor 冻结),云路径不引用;supervisor / compose 路径零行为变化。
- 90 天硬删零新代码:`RetentionCleanupJob`(`workspace_archive_retention_days=90`)已拥有;OSS 生命周期规则是控制台兜底,只进 runbook。
- 上限展示只用 `WorkspaceQuotaService.effective_limit`,**绝不读** `user_workspace.size_limit_bytes`(supervisor 冻结列,`workspace_quota.py:20-21` 明令)。
- worker `stop()` 用模块常量 `_STOP_TIMEOUT_S = 5.0`(照 `SandboxReapWorker`;**别用** `interval + 5` 公式——`sandbox_reap_worker.py:42` 注释点名批评过)。
- SQL 与 in-memory store 同一方法谓词/语义逐字节同义(仓库铁律;本 PR 不新增 store 方法,只消费)。
- 每条新断言变异自证 break→red→restore→green(重点杀:归档 key 确定性、重入矩阵 guard、`_scratch` cutoff 比较、S3 攒片切分)。
- i18n 双语(zh-CN + en),新键先 grep 确认不撞既有键;en.ts 键必须先在 `TranslationKeys` 接口声明再在值区实现。
- NFS 阻塞 IO(scandir/stat/rmtree/tar)一律 `asyncio.to_thread`。
- 本地验证命令:runtime `cd packages/expert-work-runtime && uv run pytest`;control-plane `cd services/control-plane && uv run pytest`;orchestrator `cd services/orchestrator && DOCKER_HOST= uv run pytest`;integration 测须 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`;前端 `cd apps/admin-ui && pnpm typecheck && pnpm test`;lint `uv run ruff check .`(全库含 tests)。

---

## File Structure(全景)

| 文件 | 动作 | 职责 |
|---|---|---|
| `packages/expert-work-runtime/src/expert_work/runtime/storage/base.py` | 改 | Protocol 加 `put_stream` |
| `packages/expert-work-runtime/src/expert_work/runtime/storage/memory.py` | 改 | 攒 bytes 委托 `put` |
| `packages/expert-work-runtime/src/expert_work/runtime/storage/s3_compatible.py` | 改 | multipart 实现 + 攒片 |
| `packages/expert-work-runtime/tests/test_object_store_put_stream.py` | 新 | memory + fake-client S3 单测 |
| `packages/expert-work-runtime/tests/test_minio_integration.py` | 改 | 真 MinIO put_stream 往返 |
| `services/control-plane/src/control_plane/workspace_archive.py` | 新 | 归档 key + 空档 + 流式 tar.gz |
| `services/control-plane/tests/test_workspace_archive.py` | 新 | 往返/分片/早退/错误传播 |
| `services/control-plane/src/control_plane/workspace_janitor.py` | 新 | worker 三阶段 + lock |
| `services/control-plane/tests/test_workspace_janitor.py` | 新 | tmp_path 假 NAS 树全覆盖 |
| `services/control-plane/tests/test_workspace_janitor_lock_integration.py` | 新 | 双实例单飞(真 PG) |
| `services/control-plane/src/control_plane/settings.py` | 改 | `workspace_janitor_interval_s` |
| `services/control-plane/src/control_plane/app.py` | 改 | lifespan 接线 + stop |
| `services/control-plane/src/control_plane/api/workspace.py` | 改 | 响应带 `limit_bytes` |
| `services/control-plane/tests/test_workspace_api.py` | 改 | limit 两分支 |
| `apps/admin-ui/src/api/sessions.ts` | 改 | `SessionWorkspace.limit_bytes?` |
| `apps/admin-ui/src/pages/user_profile/WorkspacePane.tsx` | 改 | 已用/上限一行 |
| `apps/admin-ui/src/i18n/locales/{en,zh-CN}.ts` | 改 | `user_profile.workspace_usage` |
| `apps/admin-ui/src/pages/__tests__/UserProfile.test.tsx` | 改 | usage 行断言 |
| `docs/runbooks/workspace-quota-and-archive.md` | 新 | 运维篇(含硬要求②) |
| `docs/research/2026-08-10-nas-setdirquota-feasibility.md` | 新 | 调研结论 |
| `docs/BACKLOG.md` | 改 | SetDirQuota 点杀项 |

分支:`git checkout -b sandbox-w3-pr2-janitor`(从 main)。

---

### Task 1: `ObjectStore.put_stream`(Protocol + memory + S3 multipart)

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/storage/base.py`(`put` 之后)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/storage/memory.py`
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/storage/s3_compatible.py`
- Test: `packages/expert-work-runtime/tests/test_object_store_put_stream.py`(新)
- Test: `packages/expert-work-runtime/tests/test_minio_integration.py`(追加)

**Interfaces:**
- Produces: `ObjectStore.put_stream(key: str, chunks: AsyncIterator[bytes], *, content_type: str | None = None) -> None`(Protocol 方法;Task 5 的 janitor 调它)。`S3CompatibleObjectStore.__init__` 新增 kwarg `multipart_part_size: int = _MULTIPART_PART_SIZE`(仅测试覆盖用;默认 64 MiB)。
- 契约:实现内部攒片,任意 chunk 尺寸合法;空流合法(产出空对象);覆盖语义同 `put`;memory 侧继承 `put` 的 object-lock 检查。

- [ ] **Step 1: 写失败测试(memory + fake S3)**。新文件 `packages/expert-work-runtime/tests/test_object_store_put_stream.py`:

```python
"""ObjectStore.put_stream 契约(spec 波3 § 4.2)——memory 直测 + S3 用 fake client 测攒片。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from expert_work.runtime.storage import InMemoryObjectStore, ObjectStoreError
from expert_work.runtime.storage.s3_compatible import S3CompatibleObjectStore


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


@pytest.mark.asyncio
async def test_memory_put_stream_round_trip() -> None:
    store = InMemoryObjectStore()
    await store.put_stream("a/b.tar.gz", _chunks(b"hello ", b"world"))
    assert await store.get("a/b.tar.gz") == b"hello world"


@pytest.mark.asyncio
async def test_memory_put_stream_empty_stream_creates_empty_object() -> None:
    store = InMemoryObjectStore()
    await store.put_stream("empty", _chunks())
    assert await store.get("empty") == b""


class _FakeExceptions:
    class ClientError(Exception):
        pass

    NoSuchKey = ClientError


class _FakeMultipartClient:
    """记录 multipart 调用序列;upload_part 失败可注入。"""

    def __init__(self, *, fail_part: int | None = None) -> None:
        self.exceptions = _FakeExceptions()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.parts: dict[int, bytes] = {}
        self.aborted = False
        self.completed = False
        self.single_put: bytes | None = None
        self._fail_part = fail_part

    async def put_object(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kw))
        self.single_put = kw["Body"]
        return {}

    async def create_multipart_upload(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("create", kw))
        return {"UploadId": "upl-1"}

    async def upload_part(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("upload_part", kw))
        if self._fail_part == kw["PartNumber"]:
            raise self.exceptions.ClientError("injected")
        self.parts[kw["PartNumber"]] = kw["Body"]
        return {"ETag": f'"etag-{kw["PartNumber"]}"'}

    async def complete_multipart_upload(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("complete", kw))
        self.completed = True
        return {}

    async def abort_multipart_upload(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("abort", kw))
        self.aborted = True
        return {}


@pytest.mark.asyncio
async def test_s3_small_stream_degrades_to_single_put() -> None:
    """整流 < 一片 → 不开 multipart,单次 put_object(顺带覆盖空流)。"""
    client = _FakeMultipartClient()
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=10)
    await store.put_stream("k", _chunks(b"tiny"), content_type="application/gzip")
    assert client.single_put == b"tiny"
    assert not any(name == "create" for name, _ in client.calls)
    put_kw = next(kw for name, kw in client.calls if name == "put_object")
    assert put_kw["ContentType"] == "application/gzip"


@pytest.mark.asyncio
async def test_s3_rebatches_chunks_into_exact_parts() -> None:
    """攒片:调用方 chunk 尺寸任意,落到 S3 的每片(除末片)恰好 part_size。"""
    client = _FakeMultipartClient()
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=4)
    await store.put_stream("k", _chunks(b"ab", b"cdefg", b"hij"))  # 10 bytes → 4+4+2
    assert client.parts == {1: b"abcd", 2: b"efgh", 3: b"ij"}
    assert client.completed
    complete_kw = next(kw for name, kw in client.calls if name == "complete")
    assert complete_kw["MultipartUpload"]["Parts"] == [
        {"ETag": '"etag-1"', "PartNumber": 1},
        {"ETag": '"etag-2"', "PartNumber": 2},
        {"ETag": '"etag-3"', "PartNumber": 3},
    ]


@pytest.mark.asyncio
async def test_s3_part_failure_aborts_and_raises() -> None:
    client = _FakeMultipartClient(fail_part=2)
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=4)
    with pytest.raises(ObjectStoreError):
        await store.put_stream("k", _chunks(b"abcdefghij"))
    assert client.aborted
    assert not client.completed


@pytest.mark.asyncio
async def test_s3_producer_error_aborts_and_propagates() -> None:
    """chunks 迭代器自己炸(打包线程错误传播路径)→ abort + 原样抛。"""

    async def _boom() -> AsyncIterator[bytes]:
        yield b"abcd"
        raise RuntimeError("tar failed")

    client = _FakeMultipartClient()
    store = S3CompatibleObjectStore(client=client, bucket="b", multipart_part_size=4)
    with pytest.raises(RuntimeError, match="tar failed"):
        await store.put_stream("k", _boom())
    assert client.aborted
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd packages/expert-work-runtime && uv run pytest tests/test_object_store_put_stream.py -v`
Expected: FAIL,`AttributeError: ... no attribute 'put_stream'`

- [ ] **Step 3: 实现**。三处:

`base.py`(`put` 方法之后,Protocol 体内;顶部 import 补 `AsyncIterator`,来自 `collections.abc`):

```python
    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        """Store an object from an async byte stream (multipart on S3).

        波 3 (spec § 4.2):杀掉 ``put(bytes)`` 的整体驻留内存上限。契约:

        - 实现负责攒片——调用方的 chunk 尺寸任意(含空流,产出空对象)。
        - 常驻内存 ≤ 单分片(S3 实现默认 64 MiB)。
        - 覆盖语义与 ``put`` 一致;不支持 object-lock 参数(归档不需要 WORM)。
        - 失败时不得留下部分可见对象(S3 走 multipart,abort 兜尾)。
        """
        ...
```

同时把模块 docstring `base.py:9-10` 的「Multipart upload deferred to M0 follow-up」段落改为一句「Multipart streaming lands via ``put_stream`` (波 3)」——欠账还清,注释别继续说谎。

`memory.py`(`put` 之后):

```python
    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        buf = bytearray()
        async for chunk in chunks:
            buf.extend(chunk)
        # 委托 put:锁检查 / 覆盖语义天然同契约。
        await self.put(key, bytes(buf), content_type=content_type)
```

`s3_compatible.py`:模块顶部加常量 + ctor 扩参 + 方法:

```python
#: S3 multipart 分片大小(spec § 4.2:常驻内存 ≤ 单分片;S3 硬下限 5 MiB,
#: 10k 片上限 → 64 MiB × 10k = 640 GiB 单对象天花板,远超工作区配额量级)。
_MULTIPART_PART_SIZE = 64 * 1024 * 1024
```

```python
    def __init__(self, client: Any, bucket: str, *, multipart_part_size: int = _MULTIPART_PART_SIZE) -> None:
        self._client = client
        self._bucket = bucket
        self._multipart_part_size = multipart_part_size
```

```python
    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
    ) -> None:
        part_size = self._multipart_part_size
        buf = bytearray()
        upload_id: str | None = None
        parts: list[dict[str, Any]] = []

        async def _flush_part(payload: bytes) -> None:
            nonlocal upload_id
            if upload_id is None:
                create_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
                if content_type is not None:
                    create_kwargs["ContentType"] = content_type
                created = await self._client.create_multipart_upload(**create_kwargs)
                upload_id = created["UploadId"]
            number = len(parts) + 1
            resp = await self._client.upload_part(
                Bucket=self._bucket, Key=key, PartNumber=number,
                UploadId=upload_id, Body=payload,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": number})

        try:
            async for chunk in chunks:
                buf.extend(chunk)
                while len(buf) >= part_size:
                    await _flush_part(bytes(buf[:part_size]))
                    del buf[:part_size]
            if upload_id is None:
                # 整流不足一片(含空流)——单次 put 更省一轮 multipart 往返。
                put_kwargs: dict[str, Any] = {
                    "Bucket": self._bucket, "Key": key, "Body": bytes(buf),
                }
                if content_type is not None:
                    put_kwargs["ContentType"] = content_type
                await self._client.put_object(**put_kwargs)
                return
            if buf:
                await _flush_part(bytes(buf))
            await self._client.complete_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except self._client.exceptions.ClientError as exc:
            await self._abort_quietly(key, upload_id)
            msg = f"put_stream failed for key {key!r}"
            raise ObjectStoreError(msg) from exc
        except BaseException:
            # 生产侧(tar 线程)异常原样上抛,但 multipart 残骸要清。
            await self._abort_quietly(key, upload_id)
            raise

    async def _abort_quietly(self, key: str, upload_id: str | None) -> None:
        if upload_id is None:
            return
        try:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id
            )
        except Exception:  # noqa: BLE001 - abort 尽力而为,别掩埋原始异常
            logger.warning("put_stream.abort_failed key=%s", key)
```

(`s3_compatible.py` 现无 logger 就加 `logger = logging.getLogger(__name__)` + `import logging`。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd packages/expert-work-runtime && uv run pytest tests/test_object_store_put_stream.py tests/test_in_memory_object_store.py tests/test_object_store_factory.py -v`
Expected: 全 PASS

- [ ] **Step 5: MinIO 集成测**。`tests/test_minio_integration.py` 追加两条(仿 `test_put_get_delete_round_trip` :79 风格,`store` fixture 复用;强制 multipart 那条照 `_ensure_bucket` 的 `getattr(store, "_client", None)` 逃生舱拿 client 重建小分片 store——**5 MiB 是 MinIO 真下限,分片别设更小**):

```python
async def test_put_stream_small_round_trip(store: ObjectStore) -> None:
    async def _chunks() -> AsyncIterator[bytes]:
        yield b"stream-"
        yield b"payload"

    await store.put_stream("stream/small.bin", _chunks())
    assert await store.get("stream/small.bin") == b"stream-payload"


async def test_put_stream_multipart_round_trip(store: ObjectStore) -> None:
    """> 2 片真 multipart:5 MiB 分片 × 11 MiB 载荷 → 2 full + 1 tail。"""
    client = getattr(store, "_client", None)
    assert client is not None
    small_parts = S3CompatibleObjectStore(
        client=client, bucket=store._bucket, multipart_part_size=5 * 1024 * 1024
    )
    payload = os.urandom(11 * 1024 * 1024)

    async def _chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(payload), 1024 * 1024):
            yield payload[i : i + 1024 * 1024]

    await small_parts.put_stream("stream/big.bin", _chunks(), content_type="application/gzip")
    assert await store.get("stream/big.bin") == payload
```

Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && cd packages/expert-work-runtime && uv run pytest tests/test_minio_integration.py -m integration -v`
Expected: 新旧全 PASS

- [ ] **Step 6: 变异自证**:①把 `_flush_part` 里 `bytes(buf[:part_size])` 改成 `bytes(buf)`(整桶上传)→ `test_s3_rebatches_chunks_into_exact_parts` 必 RED → 还原;②把 `except BaseException` 分支的 `_abort_quietly` 删掉 → `test_s3_producer_error_aborts_and_propagates` 必 RED → 还原。

- [ ] **Step 7: Commit**

```bash
git add packages/expert-work-runtime/src/expert_work/runtime/storage/ packages/expert-work-runtime/tests/
git commit -m "feat(storage): ObjectStore.put_stream 流式 multipart——实现内攒片+abort 兜尾,整流不足一片退化单次 put"
```

---

### Task 2: `workspace_archive.py`——归档 key + 空档 + 流式 tar.gz

**Files:**
- Create: `services/control-plane/src/control_plane/workspace_archive.py`
- Test: `services/control-plane/tests/test_workspace_archive.py`(新)

**Interfaces:**
- Produces(Task 5 消费):
  - `workspace_archive_key(tenant_id: UUID, user_id: UUID, workspace_id: UUID) -> str` = `f"workspace-archives/{tenant_id}/{user_id}/{workspace_id}.tar.gz"`
  - `empty_tar_gz_bytes() -> bytes`(合法空 tar.gz,几十字节)
  - `stream_directory_tar_gz(directory: Path, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]`(async 生成器;目录不存在 → 首次迭代抛 `FileNotFoundError`;打包线程异常原样传播;消费方早退不悬挂线程)
- Consumes: 无(纯新模块)。

- [ ] **Step 1: 写失败测试**:

```python
"""流式 tar.gz 打包(spec 波3 § 4.2)——tmp_path 树往返 + 早退不悬挂 + 错误传播。"""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
from pathlib import Path
from uuid import UUID

import pytest

from control_plane.workspace_archive import (
    empty_tar_gz_bytes,
    stream_directory_tar_gz,
    workspace_archive_key,
)


def test_archive_key_is_deterministic() -> None:
    t = UUID("00000000-0000-0000-0000-0000000000aa")
    u = UUID("00000000-0000-0000-0000-0000000000bb")
    w = UUID("00000000-0000-0000-0000-0000000000cc")
    key = workspace_archive_key(t, u, w)
    assert key == f"workspace-archives/{t}/{u}/{w}.tar.gz"
    assert key == workspace_archive_key(t, u, w)  # 无随机成分


def test_empty_tar_gz_is_valid_and_memberless() -> None:
    with tarfile.open(fileobj=io.BytesIO(empty_tar_gz_bytes()), mode="r:gz") as tar:
        assert tar.getmembers() == []


async def _collect(directory: Path, *, chunk_size: int = 64) -> bytes:
    out = bytearray()
    async for chunk in stream_directory_tar_gz(directory, chunk_size=chunk_size):
        out.extend(chunk)
    return bytes(out)


@pytest.mark.asyncio
async def test_round_trip_preserves_tree_and_symlink(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"alpha")
    (src / "sub" / "b.bin").write_bytes(b"\x00" * 5000)
    (src / "link").symlink_to("a.txt")

    payload = await _collect(src)  # chunk_size=64 强制多 chunk 路径

    dst = tmp_path / "dst"
    dst.mkdir()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        tar.extractall(dst, filter="data")
    assert (dst / "a.txt").read_bytes() == b"alpha"
    assert (dst / "sub" / "b.bin").read_bytes() == b"\x00" * 5000


@pytest.mark.asyncio
async def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await _collect(tmp_path / "nope")


@pytest.mark.asyncio
async def test_early_consumer_exit_does_not_leak_thread(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"x" * 512 * 1024)

    before = threading.active_count()
    gen = stream_directory_tar_gz(src, chunk_size=64)
    await gen.__anext__()  # 只取一块
    await gen.aclose()  # 早退 → 打包线程必须退出
    for _ in range(100):
        if threading.active_count() <= before:
            break
        await asyncio.sleep(0.05)
    assert threading.active_count() <= before
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_archive.py -v`
Expected: FAIL,`ModuleNotFoundError: control_plane.workspace_archive`

- [ ] **Step 3: 实现**。全文:

```python
"""工作区归档打包(沙箱迁移波 3, spec § 4.2)。

流式生成 tar.gz:``tarfile w|gz`` 在专用线程里写一个有界队列包装的
fileobj,async 侧逐块取出喂 ``ObjectStore.put_stream``——常驻内存
≤ ``chunk_size`` × 队列深度,无总量上限。老 ``archive_volume``
(supervisor,1.5GiB 内存 buffer)冻结不动,云路径用本模块。
"""

from __future__ import annotations

import io
import queue
import tarfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import asyncio

_QUEUE_DEPTH = 4
_DONE = object()


def workspace_archive_key(tenant_id: UUID, user_id: UUID, workspace_id: UUID) -> str:
    """确定性归档 key(spec § 4.1)——重试幂等覆盖,不产生重复档案。"""
    return f"workspace-archives/{tenant_id}/{user_id}/{workspace_id}.tar.gz"


def empty_tar_gz_bytes() -> bytes:
    """空 tar.gz(spec § 4.1:用户生前就没有目录 → 统一产出档案,恢复侧无需分叉)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


class _ConsumerGone(Exception):
    """消费方早退(abort event 置位)——打包线程借它无声退出。"""


class _QueueWriter(io.RawIOBase):
    def __init__(self, q: queue.Queue[object], abort: threading.Event, chunk_size: int) -> None:
        self._q = q
        self._abort = abort
        self._chunk_size = chunk_size
        self._buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b: bytes) -> int:  # type: ignore[override]
        self._buf.extend(b)
        while len(self._buf) >= self._chunk_size:
            self._emit(bytes(self._buf[: self._chunk_size]))
            del self._buf[: self._chunk_size]
        return len(b)

    def flush_tail(self) -> None:
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf.clear()

    def emit_raw(self, item: object) -> None:
        self._emit(item)

    def _emit(self, item: object) -> None:
        # 有界队列 + abort 轮询:消费方死了绝不能让线程卡死在 put 上。
        while True:
            if self._abort.is_set():
                raise _ConsumerGone
            try:
                self._q.put(item, timeout=0.1)
            except queue.Full:
                continue
            return


def _produce(directory: Path, writer: _QueueWriter) -> None:
    try:
        with tarfile.open(fileobj=writer, mode="w|gz") as tar:
            # arcname="." → 档案根即用户目录根;符号链接按链接存(与 du 口径一致)。
            tar.add(directory, arcname=".")
        writer.flush_tail()
        writer.emit_raw(_DONE)
    except _ConsumerGone:
        return
    except BaseException as exc:  # noqa: BLE001 - 异常本体就是要传给消费方的载荷
        try:
            writer.emit_raw(exc)
        except _ConsumerGone:
            return


async def stream_directory_tar_gz(
    directory: Path, *, chunk_size: int = 1024 * 1024
) -> AsyncIterator[bytes]:
    """把目录打成 tar.gz 字节流。目录不存在 → 首块就抛 FileNotFoundError。"""
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    q: queue.Queue[object] = queue.Queue(maxsize=_QUEUE_DEPTH)
    abort = threading.Event()
    writer = _QueueWriter(q, abort, chunk_size)
    thread = threading.Thread(
        target=_produce, args=(directory, writer), name="workspace-archive-tar", daemon=True
    )
    thread.start()
    try:
        while True:
            item = await asyncio.to_thread(q.get)
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item  # type: ignore[misc]
    finally:
        abort.set()
        await asyncio.to_thread(thread.join, 10.0)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_archive.py -v`
Expected: 全 PASS

- [ ] **Step 5: 变异自证**:①`workspace_archive_key` 里给 key 拼上 `uuid4()` 后缀 → `test_archive_key_is_deterministic` RED → 还原;②`finally` 里的 `abort.set()` 注释掉 → `test_early_consumer_exit_does_not_leak_thread` RED(线程数降不回去)→ 还原。

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/workspace_archive.py services/control-plane/tests/test_workspace_archive.py
git commit -m "feat(control-plane): 流式 tar.gz 打包 + 确定性归档 key——有界队列线程桥,消费早退不悬挂"
```

---

### Task 3: `WorkspaceJanitorWorker` 骨架 + advisory lock + `_scratch` 阶段

**Files:**
- Create: `services/control-plane/src/control_plane/workspace_janitor.py`
- Test: `services/control-plane/tests/test_workspace_janitor.py`(新)
- Test: `services/control-plane/tests/test_workspace_janitor_lock_integration.py`(新)

**Interfaces:**
- Produces:`WorkspaceJanitorWorker(*, user_workspaces: UserWorkspaceStore, quota_service: WorkspaceQuotaService, object_store: ObjectStore, workspace_root: str, session_factory: async_sessionmaker[AsyncSession] | None = None, interval_s: float = _INTERVAL_S)`;方法 `start() -> None` / `async stop() -> None` / `async run_once() -> JanitorRunStats` / `async _run_cycle(stats) -> None`(阶段桩,本 task 只有 `_sweep_scratch` 有实现,`_sweep_archives`/`_sweep_sizes` 空实现 `pass` 待 Task 4/5 填)。
- Produces:`@dataclass JanitorRunStats: archived: int = 0; reharvested: int = 0; refreshed: int = 0; scratch_removed: int = 0; skipped: bool = False`
- 常量:`_INTERVAL_S = 1800.0`、`_STOP_TIMEOUT_S = 5.0`、`_JANITOR_LOCK_CLASSID = 8619`、`_LOCK_TXN_TIMEOUT_MS = 60 * 60 * 1000`、`_SCRATCH_MAX_AGE_S = 24 * 3600.0`、`_SCRATCH_DIR = "_scratch"`。
- Consumes:`WorkspaceQuotaService`(`control_plane.workspace_quota`)、`UserWorkspaceStore`(`expert_work.persistence`)、`ObjectStore`(`expert_work.runtime.storage`)。

结构样板:循环/start/stop 照抄 `sandbox_reap_worker.py:78-108`(dataclass 可改普通 class,与 lock 持有并存更顺);锁包装照抄 `skill_curator.py:191-225`(比 drift 干净);`_LOCK_TXN_TIMEOUT_MS` 取 60 分钟**并写注释**:归档上传是长活(GiB 级 multipart),drift 的 5 分钟会把锁会话半路杀掉;60 分钟 = interval × 2,仍然有孤锁兜底。classid 注册表照 `_tenant_resource_lock.py:1-24` 的 docstring 模板补一行 8619。

- [ ] **Step 1: 写失败测试**(`tests/test_workspace_janitor.py`,本 task 部分):

```python
"""WorkspaceJanitorWorker —— tmp_path 假 NAS 树;in-memory 全套 store。

harness 造树布局(照 nas_workspace_store.workspace_user_root 口径):
    {root}/{tenant}/{user}/...      用户目录
    {root}/{tenant}/.deleted/{user} 软删标记(Task 5 用)
    {root}/_scratch/{sandbox_id}    临时沙箱目录
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from control_plane.workspace_janitor import (
    _SCRATCH_MAX_AGE_S,
    JanitorRunStats,
    WorkspaceJanitorWorker,
)
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence import InMemoryTenantQuotaStore
from expert_work.persistence.workspace.memory import InMemoryUserWorkspaceStore
from expert_work.runtime.storage import InMemoryObjectStore
from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory


def _build(tmp_path: Path) -> tuple[WorkspaceJanitorWorker, InMemoryUserWorkspaceStore, InMemoryObjectStore]:
    workspaces = InMemoryUserWorkspaceStore()
    quotas = InMemoryTenantQuotaStore()
    service = WorkspaceQuotaService(
        user_workspaces=workspaces, tenant_quotas=quotas, workspace_root=str(tmp_path)
    )
    store = InMemoryObjectStore()
    worker = WorkspaceJanitorWorker(
        user_workspaces=workspaces,
        quota_service=service,
        object_store=store,
        workspace_root=str(tmp_path),
    )
    return worker, workspaces, store


def _age(path: Path, *, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@pytest.mark.asyncio
async def test_scratch_stale_removed_fresh_kept(tmp_path: Path) -> None:
    stale = tmp_path / "_scratch" / str(uuid4())
    fresh = tmp_path / "_scratch" / str(uuid4())
    (stale / "junk").mkdir(parents=True)
    fresh.mkdir(parents=True)
    _age(stale, seconds=_SCRATCH_MAX_AGE_S + 60)
    _age(fresh, seconds=_SCRATCH_MAX_AGE_S - 3600)

    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.scratch_removed == 1
    assert not stale.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_scratch_missing_root_is_noop(tmp_path: Path) -> None:
    worker, _, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.scratch_removed == 0


@pytest.mark.asyncio
async def test_lock_loser_skips_cycle(tmp_path: Path) -> None:
    (tmp_path / "_scratch" / str(uuid4())).mkdir(parents=True)
    workspaces = InMemoryUserWorkspaceStore()
    service = WorkspaceQuotaService(
        user_workspaces=workspaces,
        tenant_quotas=InMemoryTenantQuotaStore(),
        workspace_root=str(tmp_path),
    )
    worker = WorkspaceJanitorWorker(
        user_workspaces=workspaces,
        quota_service=service,
        object_store=InMemoryObjectStore(),
        workspace_root=str(tmp_path),
        session_factory=FakeAdvisoryLockSessionFactory(granted=False),
    )
    stats = await worker.run_once()
    assert stats.skipped
    assert stats.scratch_removed == 0


@pytest.mark.asyncio
async def test_stop_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import control_plane.workspace_janitor as mod

    worker, _, _ = _build(tmp_path)

    async def _never_returns() -> JanitorRunStats:
        await asyncio.sleep(3600)
        raise AssertionError

    monkeypatch.setattr(worker, "run_once", _never_returns)
    monkeypatch.setattr(mod, "_STOP_TIMEOUT_S", 0.05, raising=False)
    worker.interval_s = 0.01
    worker.start()
    await asyncio.sleep(0.05)  # 让循环进入 run_once
    await asyncio.wait_for(worker.stop(), timeout=2)
```

先读 `tests/fake_advisory_lock.py` 确认 `FakeAdvisoryLockSessionFactory` 的构造签名(`granted=False` 按实际参数名改;若它只支持"永远给锁",给它加个拒锁参数或在测试里用现成的拒锁形态——`tests/test_tenant_resource_lock.py:45` 有用法样板)。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -v`
Expected: FAIL,`ModuleNotFoundError: control_plane.workspace_janitor`

- [ ] **Step 3: 实现骨架 + `_scratch` 阶段**。要点(结构照样板,此处给非样板部分):

```python
_INTERVAL_S = 1800.0  # spec § 五:30 分钟一轮
_STOP_TIMEOUT_S = 5.0  # 照 sandbox_reap_worker:别用 interval+5 公式
_JANITOR_LOCK_CLASSID = 8619  # 注册表见 _tenant_resource_lock.py docstring
#: drift 用 5 分钟;janitor 的归档上传是 GiB 级长活,5 分钟会把持锁会话
#: 半路杀掉(idle_in_transaction 判定的是锁会话,不是干活协程)。
#: 60 分钟 = interval × 2,孤锁仍有界。
_LOCK_TXN_TIMEOUT_MS = 60 * 60 * 1000
_SCRATCH_MAX_AGE_S = 24 * 3600.0  # spec § 五:临时沙箱寿命 ≤20min,72 倍余量
_SCRATCH_DIR = "_scratch"  # 与 orchestrator agent_sandbox._SCRATCH_DIR 同值(私名不跨包 import)
```

`run_once`:`session_factory is None` → 直跑 `_run_cycle`;有 → `SET LOCAL idle_in_transaction_session_timeout` + `pg_try_advisory_xact_lock(:cid, hashtext('workspace_janitor'))`,拿不到 → `rollback()` + `JanitorRunStats(skipped=True)`,拿到 → `try: _run_cycle finally: rollback()`(照 `skill_curator.py:191-225` 逐行形状)。

`_run_cycle(stats)`:

```python
        for phase in (self._sweep_archives, self._sweep_sizes, self._sweep_scratch):
            try:
                await phase(stats)
            except Exception:  # 单阶段炸不拖累后续阶段;下轮自然重试
                logger.exception("workspace_janitor.phase_failed phase=%s", phase.__name__)
```

`_sweep_scratch(stats)`:

```python
    async def _sweep_scratch(self, stats: JanitorRunStats) -> None:
        scratch_root = Path(self._workspace_root) / _SCRATCH_DIR

        def _stale_dirs() -> list[Path]:
            cutoff = time.time() - _SCRATCH_MAX_AGE_S
            out: list[Path] = []
            try:
                with os.scandir(scratch_root) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False) and entry.stat(
                                follow_symlinks=False
                            ).st_mtime < cutoff:
                                out.append(Path(entry.path))
                        except OSError:
                            continue
            except FileNotFoundError:
                return []
            return out

        for path in await asyncio.to_thread(_stale_dirs):
            try:
                await asyncio.to_thread(shutil.rmtree, path)
                stats.scratch_removed += 1
            except OSError:
                logger.warning("workspace_janitor.scratch_remove_failed path=%s", path)
```

`_sweep_archives` / `_sweep_sizes` 本 task 留 `pass`(Task 4/5 填)。`start/stop/_loop` 照 `sandbox_reap_worker.py:82-108`(循环里 `run_once` 包 `try/except Exception: logger.exception(...)`,永不外抛;启动不立刻扫)。logger 名 `logging.getLogger(__name__)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -v`
Expected: 全 PASS

- [ ] **Step 5: lock 双实例集成测**。新文件 `tests/test_workspace_janitor_lock_integration.py`,**逐结构照抄** `tests/test_worker_advisory_lock_integration.py`(`pytestmark = pytest.mark.integration`、`_async_dsn`、engine fixture 吃全局 `postgres_container`、`_slow_down` 实例属性遮蔽 `_run_cycle` 加 `await asyncio.sleep(0.3)`):两个 worker 共享同一批 in-memory store + tmp_path 根(放一个过期 `_scratch` 目录),`asyncio.gather(a.run_once(), b.run_once())` → 断言 `skipped` 的恰好 1 个、`scratch_removed` 总和恰好 1。

Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && cd services/control-plane && uv run pytest tests/test_workspace_janitor_lock_integration.py -m integration -v`
Expected: PASS

- [ ] **Step 6: 变异自证**:①`_stale_dirs` 里 `< cutoff` 改 `> cutoff` → `test_scratch_stale_removed_fresh_kept` RED → 还原;②`run_once` 拿不到锁的分支改成继续跑 `_run_cycle` → `test_lock_loser_skips_cycle` RED → 还原。

- [ ] **Step 7: Commit**

```bash
git add services/control-plane/src/control_plane/workspace_janitor.py services/control-plane/tests/test_workspace_janitor.py services/control-plane/tests/test_workspace_janitor_lock_integration.py
git commit -m "feat(control-plane): WorkspaceJanitorWorker 骨架——advisory lock 8619 单飞 + _scratch mtime>24h 清理"
```

---

### Task 4: 阶段 2——配额全量扫(文件系统为发现源)

**Files:**
- Modify: `services/control-plane/src/control_plane/workspace_janitor.py`(填 `_sweep_sizes`)
- Test: `services/control-plane/tests/test_workspace_janitor.py`(追加)

**Interfaces:**
- Consumes: `WorkspaceQuotaService.refresh(*, tenant_id, user_id)`(建行 + 软删早退 + du + `update_size`,无防抖——`workspace_quota.py:118-160`)。
- Produces: `stats.refreshed` 计数;私有 helper `_list_uuid_dirs(path: Path) -> list[tuple[UUID, Path]]`(Task 5 复用)。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_workspace_janitor.py`):

```python
@pytest.mark.asyncio
async def test_full_scan_discovers_from_filesystem_and_writes_sizes(tmp_path: Path) -> None:
    """行不存在也扫(FS 为发现源,refresh 建行);字节数 = du 真值。"""
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user_a)).mkdir(parents=True)
    (tmp_path / str(tenant) / str(user_a) / "f1").write_bytes(b"x" * 100)
    (tmp_path / str(tenant) / str(user_b) / "sub").mkdir(parents=True)
    (tmp_path / str(tenant) / str(user_b) / "sub" / "f2").write_bytes(b"y" * 250)

    worker, workspaces, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.refreshed == 2
    row_a = await workspaces.get(tenant_id=tenant, user_id=user_a)
    row_b = await workspaces.get(tenant_id=tenant, user_id=user_b)
    assert row_a is not None and row_a.size_bytes == 100
    assert row_b is not None and row_b.size_bytes == 250


@pytest.mark.asyncio
async def test_full_scan_skips_junk_and_special_dirs(tmp_path: Path) -> None:
    """非 UUID 目录、.deleted、_scratch 都不进扫描;坏目录不炸整轮。"""
    tenant = uuid4()
    user = uuid4()
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    (tmp_path / str(tenant) / ".deleted").mkdir()
    (tmp_path / str(tenant) / "not-a-uuid").mkdir()
    (tmp_path / "_scratch" / str(uuid4())).mkdir(parents=True)
    (tmp_path / "lost+found").mkdir()

    worker, workspaces, _ = _build(tmp_path)
    stats = await worker.run_once()
    assert stats.refreshed == 1
    assert await workspaces.get(tenant_id=tenant, user_id=user) is not None


@pytest.mark.asyncio
async def test_full_scan_one_user_failure_does_not_stop_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user_a)).mkdir(parents=True)
    (tmp_path / str(tenant) / str(user_b)).mkdir(parents=True)

    worker, workspaces, _ = _build(tmp_path)
    real_refresh = worker._quota_service.refresh
    calls: list[UUID] = []

    async def _flaky(*, tenant_id, user_id):  # 第一个用户炸,其余照常
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await real_refresh(tenant_id=tenant_id, user_id=user_id)

    monkeypatch.setattr(worker._quota_service, "refresh", _flaky)
    stats = await worker.run_once()
    assert stats.refreshed == 1
    assert len(calls) == 2
```

(`UUID` 已在文件顶部 import 区补上。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -k full_scan -v`
Expected: FAIL(`refreshed == 0`,`_sweep_sizes` 还是 `pass`)

- [ ] **Step 3: 实现**:

```python
def _list_uuid_dirs(path: Path) -> list[tuple[UUID, Path]]:
    """列出 path 下目录名可解析为 UUID 的子目录——布局约定
    (workspace_user_root)之外的东西(_scratch/.deleted/lost+found/垃圾)
    天然被 UUID 解析挡掉。"""
    out: list[tuple[UUID, Path]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    out.append((UUID(entry.name), Path(entry.path)))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    return sorted(out, key=lambda t: str(t[0]))
```

```python
    async def _sweep_sizes(self, stats: JanitorRunStats) -> None:
        root = Path(self._workspace_root)
        for tenant_id, tenant_dir in await asyncio.to_thread(_list_uuid_dirs, root):
            for user_id, _user_dir in await asyncio.to_thread(_list_uuid_dirs, tenant_dir):
                try:
                    await self._quota_service.refresh(tenant_id=tenant_id, user_id=user_id)
                    stats.refreshed += 1
                except Exception:
                    logger.exception(
                        "workspace_janitor.refresh_failed tenant=%s user=%s", tenant_id, user_id
                    )
```

(软删行:`refresh` 自己早退——阶段 1 先跑,同轮内软删目录已被归档删除,残余情况下轮收敛。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -v`
Expected: 全 PASS(含 Task 3 用例)

- [ ] **Step 5: 变异自证**:`_list_uuid_dirs` 里把 `except ValueError: continue` 改成 `raise` → `test_full_scan_skips_junk_and_special_dirs` RED → 还原。

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/workspace_janitor.py services/control-plane/tests/test_workspace_janitor.py
git commit -m "feat(control-plane): janitor 阶段2 配额全量扫——FS 为发现源,复用 WorkspaceQuotaService.refresh"
```

---

### Task 5: 阶段 1——归档清扫(重入矩阵 + 硬要求①复活收割)

**Files:**
- Modify: `services/control-plane/src/control_plane/workspace_janitor.py`(填 `_sweep_archives` + `_archive_one`)
- Test: `services/control-plane/tests/test_workspace_janitor.py`(追加)

**Interfaces:**
- Consumes: Task 1 `put_stream`、Task 2 `workspace_archive_key`/`empty_tar_gz_bytes`/`stream_directory_tar_gz`、store 方法 `resolve`/`soft_delete`/`mark_archived`(DTO 字段:`ws.id`/`ws.deleted_at`/`ws.archived_object_key`)、`workspace_deleted_marker`/`workspace_user_root`/`DELETED_DIR`(`orchestrator.tools.nas_workspace_store`)。
- 行为矩阵(**单一代码路径,矩阵是推论**):

| 行状态 | 目录 | OSS 对象 | 动作 |
|---|---|---|---|
| 未软删(标记刚落) | 在 | — | soft_delete → 上传 → rm -rf → mark(`stats.archived`)|
| 软删未 mark(传后崩/删后崩) | 在 | 任意 | 重新上传覆盖 → rm -rf → mark |
| 软删未 mark | 不在 | 在 | 直接 mark(上次删完没 mark)|
| 软删未 mark | 不在 | 不在 | 传空 tar.gz → mark(生前无目录)|
| **已 mark(硬要求①)** | **在(复活)** | 在 | **重新上传覆盖 → rm -rf,不重 mark(`stats.reharvested`)** |
| 已 mark | 不在 | 在 | 零操作(稳态墓碑)|

- [ ] **Step 1: 写失败测试**(追加;文件顶部补 import:`io`/`tarfile`/`datetime`,`workspace_deleted_marker` 等来自 `orchestrator.tools.nas_workspace_store`):

```python
def _mark_deleted(root: Path, tenant, user) -> None:
    from orchestrator.tools.nas_workspace_store import workspace_deleted_marker

    marker = workspace_deleted_marker(str(root), tenant, user)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _tar_names(payload: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


class _CountingObjectStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.stream_puts: list[str] = []
        self.puts: list[str] = []

    async def put_stream(self, key, chunks, *, content_type=None):
        self.stream_puts.append(key)
        await super().put_stream(key, chunks, content_type=content_type)

    async def put(self, key, data, **kw):
        self.puts.append(key)
        await super().put(key, data, **kw)


def _build_counting(tmp_path: Path):
    workspaces = InMemoryUserWorkspaceStore()
    service = WorkspaceQuotaService(
        user_workspaces=workspaces,
        tenant_quotas=InMemoryTenantQuotaStore(),
        workspace_root=str(tmp_path),
    )
    store = _CountingObjectStore()
    worker = WorkspaceJanitorWorker(
        user_workspaces=workspaces,
        quota_service=service,
        object_store=store,
        workspace_root=str(tmp_path),
    )
    return worker, workspaces, store


@pytest.mark.asyncio
async def test_archive_happy_path(tmp_path: Path) -> None:
    """标记+目录+无行 → 建行软删、OSS 出现确定性 key 档案、目录删、标记留。"""
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    (user_dir / "keep.txt").write_bytes(b"data")
    _mark_deleted(tmp_path, tenant, user)

    worker, workspaces, store = _build_counting(tmp_path)
    stats = await worker.run_once()

    assert stats.archived == 1
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.deleted_at is not None
    key = f"workspace-archives/{tenant}/{user}/{row.id}.tar.gz"
    assert row.archived_object_key == key
    names = _tar_names(await store.get(key))
    assert any(n.endswith("keep.txt") for n in names)  # 档案含用户文件(arcname="." 下成员名形如 ./keep.txt)
    assert not user_dir.exists()
    from orchestrator.tools.nas_workspace_store import workspace_deleted_marker

    assert workspace_deleted_marker(str(tmp_path), tenant, user).exists()  # 墓碑留


@pytest.mark.asyncio
async def test_archive_rerun_is_zero_op(tmp_path: Path) -> None:
    """稳态墓碑:已 mark + 目录不在 → 第二轮零上传零标记。"""
    tenant, user = uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    _mark_deleted(tmp_path, tenant, user)
    worker, _, store = _build_counting(tmp_path)
    await worker.run_once()
    uploads_before = len(store.stream_puts) + len(store.puts)

    stats = await worker.run_once()
    assert stats.archived == 0 and stats.reharvested == 0
    assert len(store.stream_puts) + len(store.puts) == uploads_before


@pytest.mark.asyncio
async def test_resurrected_dir_is_reharvested(tmp_path: Path) -> None:
    """硬要求①:已 mark 后目录复活 → 覆盖上传 + 再删目录,不重 mark。"""
    tenant, user = uuid4(), uuid4()
    user_dir = tmp_path / str(tenant) / str(user)
    user_dir.mkdir(parents=True)
    (user_dir / "original.txt").write_bytes(b"v1")
    _mark_deleted(tmp_path, tenant, user)
    worker, workspaces, store = _build_counting(tmp_path)
    await worker.run_once()
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.archived_object_key is not None

    # 复活:上传路径不查软删标记(W2 既有设计)——直接造目录
    user_dir.mkdir(parents=True)
    (user_dir / "stray.txt").write_bytes(b"post-purge")

    stats = await worker.run_once()
    assert stats.reharvested == 1 and stats.archived == 0
    assert not user_dir.exists()
    names = _tar_names(await store.get(row.archived_object_key))
    assert any(n.endswith("stray.txt") for n in names)  # 档案被复活内容覆盖(runbook 有言在先)
    row2 = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row2 is not None and row2.archived_object_key == row.archived_object_key


@pytest.mark.asyncio
async def test_reentry_dir_gone_object_present_marks_only(tmp_path: Path) -> None:
    """删后崩重入:目录不在 + 对象在 + 行软删未 mark → 只 mark,零上传。"""
    tenant, user = uuid4(), uuid4()
    _mark_deleted(tmp_path, tenant, user)
    worker, workspaces, store = _build_counting(tmp_path)
    row = await workspaces.resolve(tenant_id=tenant, user_id=user)
    from datetime import UTC, datetime

    await workspaces.soft_delete(workspace_id=row.id, now=datetime.now(UTC))
    key = f"workspace-archives/{tenant}/{user}/{row.id}.tar.gz"
    await store.put(key, b"pre-existing")
    store.puts.clear()

    stats = await worker.run_once()
    assert stats.archived == 1
    assert store.stream_puts == [] and store.puts == []
    row2 = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row2 is not None and row2.archived_object_key == key


@pytest.mark.asyncio
async def test_reentry_no_dir_no_object_uploads_empty_archive(tmp_path: Path) -> None:
    """生前无目录:统一产出空档案再 mark(恢复侧无需分叉)。"""
    tenant, user = uuid4(), uuid4()
    _mark_deleted(tmp_path, tenant, user)
    worker, workspaces, store = _build_counting(tmp_path)

    stats = await worker.run_once()
    assert stats.archived == 1
    row = await workspaces.get(tenant_id=tenant, user_id=user)
    assert row is not None and row.archived_object_key is not None
    assert _tar_names(await store.get(row.archived_object_key)) == set()


@pytest.mark.asyncio
async def test_archive_upload_failure_isolates_and_retries_next_round(tmp_path: Path) -> None:
    """A 用户上传炸 → B 照常归档;下轮 A 重试成功(幂等重入)。"""
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    for u in (user_a, user_b):
        d = tmp_path / str(tenant) / str(u)
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"z")
        _mark_deleted(tmp_path, tenant, u)

    worker, workspaces, store = _build_counting(tmp_path)
    real_put_stream = store.put_stream
    fail_once: list[str] = []

    async def _flaky(key, chunks, *, content_type=None):
        if str(user_a) in key and not fail_once:
            fail_once.append(key)
            raise ObjectStoreError("injected")
        await real_put_stream(key, chunks, content_type=content_type)

    store.put_stream = _flaky  # type: ignore[method-assign]
    stats = await worker.run_once()
    assert stats.archived == 1  # 只有 B
    row_a = await workspaces.get(tenant_id=tenant, user_id=user_a)
    assert row_a is not None and row_a.archived_object_key is None
    assert (tmp_path / str(tenant) / str(user_a)).exists()  # 先传后删:没传成就没删

    stats2 = await worker.run_once()
    assert stats2.archived == 1  # A 补上
```

(`ObjectStoreError` 从 `expert_work.runtime.storage` import;`test_archive_happy_path` 里 tar 成员名按实现跑一次后收敛为精确断言——`arcname="."` 下成员名形如 `./keep.txt`。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -k "archive or reharvest or reentry" -v`
Expected: FAIL(`_sweep_archives` 还是 `pass`,stats 全 0)

- [ ] **Step 3: 实现**:

```python
    async def _sweep_archives(self, stats: JanitorRunStats) -> None:
        root = Path(self._workspace_root)

        def _markers(tenant_dir: Path) -> list[UUID]:
            out: list[UUID] = []
            try:
                with os.scandir(tenant_dir / DELETED_DIR) as it:
                    for entry in it:
                        try:
                            out.append(UUID(entry.name))
                        except ValueError:
                            continue
            except FileNotFoundError:
                return []
            return sorted(out, key=str)

        for tenant_id, tenant_dir in await asyncio.to_thread(_list_uuid_dirs, root):
            for user_id in await asyncio.to_thread(_markers, tenant_dir):
                try:
                    await self._archive_one(tenant_id, user_id, stats)
                except Exception:
                    logger.exception(
                        "workspace_janitor.archive_failed tenant=%s user=%s", tenant_id, user_id
                    )

    async def _archive_one(self, tenant_id: UUID, user_id: UUID, stats: JanitorRunStats) -> None:
        """spec § 4.1 + 硬要求①。单一路径:目录在就(重)归档;矩阵是推论。

        崩溃安全顺序:先传后删,mark 最后。已 mark 行的目录复活
        (上传路径不查软删标记,W2 既有设计)→ 覆盖上传同 key 再删,
        不重 mark——覆盖语义 runbook 有言在先。
        """
        ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
        if ws.deleted_at is None:
            await self._user_workspaces.soft_delete(
                workspace_id=ws.id, now=datetime.now(UTC)
            )
        key = workspace_archive_key(tenant_id, user_id, ws.id)
        user_dir = workspace_user_root(self._workspace_root, tenant_id, user_id)

        if await asyncio.to_thread(user_dir.is_dir):
            await self._object_store.put_stream(
                key, stream_directory_tar_gz(user_dir), content_type="application/gzip"
            )
            await asyncio.to_thread(shutil.rmtree, user_dir)
            if ws.archived_object_key is None:
                await self._user_workspaces.mark_archived(
                    workspace_id=ws.id, archived_object_key=key
                )
                stats.archived += 1
            else:
                stats.reharvested += 1
                logger.info(
                    "workspace_janitor.reharvested tenant=%s user=%s", tenant_id, user_id
                )
            return

        if ws.archived_object_key is not None:
            return  # 稳态墓碑:标记留着挡 acquire,行已收口
        if key not in await self._object_store.list_prefix(key):
            # 生前无目录(或上传前崩且目录本来就空缺)→ 统一产出空档案
            await self._object_store.put(
                key, empty_tar_gz_bytes(), content_type="application/gzip"
            )
        await self._user_workspaces.mark_archived(workspace_id=ws.id, archived_object_key=key)
        stats.archived += 1
```

import 补:`from datetime import UTC, datetime`、`shutil`、`from orchestrator.tools.nas_workspace_store import DELETED_DIR, workspace_user_root`、Task 2 的三个名字。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -v`
Expected: 全 PASS(三 task 全部用例)

- [ ] **Step 5: 变异自证**(重入矩阵是本 PR 命门,逐条杀):①`ws.archived_object_key is None` guard 删掉(总是 mark)→ `test_resurrected_dir_is_reharvested` 的 `archived == 0` 断言 RED → 还原;②`list_prefix` 探测删掉(总是传空档)→ `test_reentry_dir_gone_object_present_marks_only` 的零上传断言 RED → 还原;③`rm -rf` 挪到 `put_stream` 之前 → `test_archive_upload_failure_isolates_and_retries_next_round` 的「没传成就没删」断言 RED → 还原。

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/workspace_janitor.py services/control-plane/tests/test_workspace_janitor.py
git commit -m "feat(control-plane): janitor 阶段1 归档清扫——先传后删 mark 最后,复活目录重新收割(硬要求①)"
```

---

### Task 6: app 接线 + settings

**Files:**
- Modify: `services/control-plane/src/control_plane/settings.py`(workspace 组附近,`:218` `workspace_nas_root` 之后)
- Modify: `services/control-plane/src/control_plane/app.py`

**Interfaces:**
- Consumes: `WorkspaceJanitorWorker`(Task 3-5)、lifespan 内既有名:`object_store`(`:1455-1470` 构建)、`resolved_workspace_quota` + `resolved_user_workspace_store`(`:788-798`)、`sql_stores.session_factory`(照 `:2093` 用法)、`resolved_settings`。
- Produces: `settings.workspace_janitor_interval_s: int = 1800`(env `EXPERT_WORK_WORKSPACE_JANITOR_INTERVAL_S`);`app.state.workspace_janitor_worker`。

- [ ] **Step 1: settings 加项**(`workspace_nas_root` 同组):

```python
    #: 沙箱迁移波 3 (spec § 五) —— WorkspaceJanitorWorker 扫描周期。
    #: 30 分钟:归档消费 + 配额全量扫兜底 + _scratch 清理共用一轮。
    workspace_janitor_interval_s: int = Field(default=1800, gt=0)
```

- [ ] **Step 2: app.py 接线**。四处:

1. import 区(`:236` 附近):`from control_plane.workspace_janitor import WorkspaceJanitorWorker`
2. lifespan 预声明区(`:1280-1290`):`workspace_janitor_worker: WorkspaceJanitorWorker | None = None`
3. object_store 构建之后(`:1470` 后;**必须在 lifespan 内、AsyncExitStack 存续期中**,worker 用的 S3 client 随 stack 关闭):

```python
                # 沙箱迁移波 3 (spec § 五):janitor 只在云工作区路径
                # (quota gate 已组装)时上岗——条件跟随实际组装物,
                # 照 SandboxReapWorker 的 isinstance 先例,不另开开关。
                if resolved_workspace_quota is not None and resolved_settings.workspace_nas_root:
                    workspace_janitor_worker = WorkspaceJanitorWorker(
                        user_workspaces=resolved_user_workspace_store,
                        quota_service=resolved_workspace_quota,
                        object_store=object_store,
                        workspace_root=resolved_settings.workspace_nas_root,
                        session_factory=sql_stores.session_factory if sql_stores else None,
                        interval_s=float(resolved_settings.workspace_janitor_interval_s),
                    )
                    workspace_janitor_worker.start()
                    _app.state.workspace_janitor_worker = workspace_janitor_worker
```

4. finally 停机区(`:2162-2169`,挨着 `sandbox_reap_worker`,**在 AsyncExitStack 收尾之前**——worker 停了 S3 client 才能安全关):

```python
        if workspace_janitor_worker is not None:
            await workspace_janitor_worker.stop()
```

先读 `:1440-1480` 与 `:2129-2169` 确认缩进层级与 stack 嵌套(object_store 是 `stack.enter_async_context` 进的,worker 的 start/stop 必须都在该 stack 生命周期内)。

- [ ] **Step 3: 验证**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_janitor.py tests/test_app_smoke.py -v 2>/dev/null || cd services/control-plane && uv run pytest tests/test_workspace_janitor.py -v`
(app 冒烟测试文件名先 `ls tests | grep -i "app\|lifespan"` 找,存在就一起跑。)
Expected: PASS;另跑 `uv run pytest` 全量确认没有 lifespan 回归。

- [ ] **Step 4: Commit**

```bash
git add services/control-plane/src/control_plane/settings.py services/control-plane/src/control_plane/app.py
git commit -m "feat(control-plane): janitor 接线 lifespan——随 quota gate 组装上岗,interval 旋钮默认 1800s"
```

---

### Task 7: 端点带 `limit_bytes` + 运维页「已用/上限」

**Files:**
- Modify: `services/control-plane/src/control_plane/api/workspace.py:90-153`(`get_workspace`)
- Test: `services/control-plane/tests/test_workspace_api.py`(追加)
- Modify: `apps/admin-ui/src/api/sessions.ts:106-109`(`SessionWorkspace`)
- Modify: `apps/admin-ui/src/pages/user_profile/WorkspacePane.tsx`
- Modify: `apps/admin-ui/src/i18n/locales/en.ts` + `zh-CN.ts`
- Test: `apps/admin-ui/src/pages/__tests__/UserProfile.test.tsx`(追加)

**Interfaces:**
- Produces: `GET /v1/workspace` 响应 `data.limit_bytes: int`(两个返回分支都带,含机器 principal 分支);来源 `app.state.workspace_quota_service.effective_limit(tenant_id=)`,service 缺席(本地 compose/测试)→ `DEFAULT_WORKSPACE_BYTES_PER_USER`。**绝不读 `workspace.size_limit_bytes`**。
- 前端:`SessionWorkspace.limit_bytes?: number`(optional,旧后端容错);i18n 键 `user_profile.workspace_usage`。

- [ ] **Step 1: 后端失败测试**(追加到 `test_workspace_api.py`,复用 `setup` fixture):

```python
@pytest.mark.asyncio
async def test_get_workspace_carries_default_limit_without_quota_service(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """quota service 未组装(本地/测试形态)→ 回落共享默认 10GiB。"""
    client, _, _ = setup
    resp = await client.get("/v1/workspace")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["limit_bytes"] == DEFAULT_WORKSPACE_BYTES_PER_USER


@pytest.mark.asyncio
async def test_get_workspace_limit_follows_tenant_quota(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
    tmp_path,
) -> None:
    """租户配了 WORKSPACE_BYTES_PER_USER → limit_bytes 即时反映(effective_limit,
    非 user_workspace.size_limit_bytes 列)。"""
    client, _, _ = setup
    app = client._transport.app  # type: ignore[attr-defined]
    quotas = InMemoryTenantQuotaStore()
    app.state.workspace_quota_service = WorkspaceQuotaService(
        user_workspaces=app.state.user_workspace_store,
        tenant_quotas=quotas,
        workspace_root=str(tmp_path),
    )
    await quotas.upsert(
        tenant_id=_TENANT,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER, limit_value=123_456_789
        ),
        updated_by="test",
    )
    resp = await client.get("/v1/workspace")
    assert resp.json()["data"]["limit_bytes"] == 123_456_789
```

import 区补:`DEFAULT_WORKSPACE_BYTES_PER_USER`/`QuotaDimension`/`TenantQuotaPatch`(`expert_work.protocol`)、`InMemoryTenantQuotaStore`(`expert_work.persistence`)、`WorkspaceQuotaService`。`TenantQuotaPatch` 构造签名先 grep `services/control-plane/tests/test_quota_in_memory.py` 里 PR-1 加的哨兵用例照抄。`setup` fixture 若没把 `user_workspace_store` 挂 `app.state` 就在测试里造一个挂上(照 `test_get_workspace_returns_meta_when_seeded` :120 的取用方式反推)。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_api.py -k limit -v`
Expected: FAIL,`KeyError: 'limit_bytes'`

- [ ] **Step 3: 后端实现**。`api/workspace.py` `get_workspace`:两个 return 前算一次:

```python
    quota_service = getattr(request.app.state, "workspace_quota_service", None)
    if quota_service is not None:
        limit_bytes = await quota_service.effective_limit(tenant_id=tenant_id)
    else:
        # 本地 compose / 测试形态没有组装 quota gate —— 上限即平台默认。
        limit_bytes = DEFAULT_WORKSPACE_BYTES_PER_USER
```

机器 principal 早退分支(`:124`)同样带 `"limit_bytes": limit_bytes`(该分支在 tenant 解析后;若在之前则用默认常量)。响应 data 加 `"limit_bytes": limit_bytes`。handler 现有参数里没有 `Request` 就补(照本文件其他 handler 取 `app.state` 的既有方式)。

- [ ] **Step 4: 后端测试通过**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: 变异自证**:`effective_limit` 调用改成读 `workspace.size_limit_bytes`(supervisor 冻结列)→ `test_get_workspace_limit_follows_tenant_quota` RED(行不存在/列值 10GiB ≠ 123_456_789)→ 还原。

- [ ] **Step 6: 前端**。四处:

1. `sessions.ts` `SessionWorkspace` 加 `limit_bytes?: number;`
2. i18n:先 `grep -n "workspace_usage" apps/admin-ui/src/i18n/locales/en.ts apps/admin-ui/src/i18n/locales/zh-CN.ts` 确认不撞;`en.ts` `TranslationKeys` 接口 `user_profile` 段(`:316-321` 附近)声明 `workspace_usage: string;`,值区(`:3231-3237`)加 `workspace_usage: "Used {{used}} / limit {{limit}} (approx., rescanned every 30 min)"`;`zh-CN.ts`(`:325-330`)加 `workspace_usage: "已用 {{used}} / 上限 {{limit}}(约,30 分钟粒度)"`。
3. `WorkspacePane.tsx`:装载处把整包 `SessionWorkspace` 的 `limit_bytes` 也取出(`:261` 附近 `const limitBytes = workspaceResp?.limit_bytes ?? null;`,变量名按当地);meta 行(`:288-291`)改为:

```tsx
{limitBytes != null
  ? `${t("user_profile.workspace_volume")}: ${meta.volume_name} · ${t("user_profile.workspace_usage", {
      used: formatBytes(meta.size_bytes),
      limit: formatBytes(limitBytes),
    })}`
  : /* 旧后端容错:保留原「大小」行 */ existingSizeLine}
```

(具体 JSX 以现文件为准——只动 meta 行内容,`data-testid="user-workspace-meta"` 保持。)
4. `UserProfile.test.tsx`:`getUserWorkspace` mock 返回值加 `limit_bytes: 10 * 1024 ** 3`;新断言:workspace tab 渲染后 `getByTestId("user-workspace-meta")` 文本含「已用」/`Used`(按测试文件既用语言)与 `10.0 GB`。

- [ ] **Step 7: 前端验证**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm test -- UserProfile`
Expected: 全 PASS(编辑器诊断 stale 不作数,以真 tsc+vitest 定论——仓库既有教训)

- [ ] **Step 8: Commit**

```bash
git add services/control-plane/src/control_plane/api/workspace.py services/control-plane/tests/test_workspace_api.py apps/admin-ui/src/api/sessions.ts apps/admin-ui/src/pages/user_profile/WorkspacePane.tsx apps/admin-ui/src/i18n/locales/ apps/admin-ui/src/pages/__tests__/UserProfile.test.tsx
git commit -m "feat: 用户运维页工作区已用/上限——端点携带 effective_limit,绝不读 size_limit_bytes 冻结列"
```

---

### Task 8: 文档三件(runbook 硬要求② + SetDirQuota 调研 + purge 一句)

**Files:**
- Create: `docs/runbooks/workspace-quota-and-archive.md`
- Create: `docs/research/2026-08-10-nas-setdirquota-feasibility.md`
- Modify: `docs/BACKLOG.md`(追加一项)
- Modify: purge 响应文档(位置 Step 3 找)

- [ ] **Step 1: runbook**。风格照 `docs/runbooks/sandbox-image-release.md`(常规操作 + 一次性配置混合型)。**全文骨架(按此扩写,硬要求②段落逐字保留语义)**:

```markdown
# Runbook — 工作区配额与归档(workspace-quota-and-archive)

> 沙箱迁移波 3 交付(spec `docs/superpowers/specs/2026-08-09-sandbox-migration-w3-design.md`)。
> 覆盖:每用户工作区配额调整、删用户后的归档/恢复、OSS 生命周期与 NAS 快照一次性配置。
> supervisor / compose 路径不在本篇范围(冻结,行为照旧)。

## 机制速览
- 配额上限:租户配额 `workspace_bytes_per_user` 维度;未配 = 平台默认 10 GiB。
  闸 A(领沙箱)`size_bytes >= limit`;闸 B(上传)`size_bytes + incoming > limit`。
  读、下载、删文件、列表永远放行(删文件是用户唯一自救路)。
- 记账三层:上传增量 → release 60s 防抖 du → janitor 30 分钟全量扫。页面「已用」为约值。
- 删除收尾:`user_purge` 写 `{tenant}/.deleted/{user}` 标记 → `WorkspaceJanitorWorker` 每轮:
  流式打包上传 OSS `workspace-archives/{tenant}/{user}/{workspace_id}.tar.gz`(确定性 key)
  → `rm -rf` NAS 目录 → 行 `mark_archived`。标记文件保留(墓碑,挡 acquire)。
- 90 天:`RetentionCleanupJob`(`workspace_archive_retention_days=90`)删档案对象 + 硬删行;
  OSS 生命周期规则是控制台兜底(见下)。误删自救窗口 = 90 天。

## 配额调整
- 管理界面:设置 → 租户配额 → 维度选 `workspace_bytes_per_user`,limit_value 单位字节。
- API:`PUT /v1/tenants/{tenant_id}/quotas`(照租户配额既有 CRUD;curl 示例给一条)。
- 生效:即时(闸每次读 store,无缓存)。

## 归档恢复(90 天窗口内)
1. 找 key:`SELECT id, archived_object_key FROM user_workspace WHERE tenant_id='…' AND user_id='…';`
2. 下载:`ossutil cp oss://<bucket>/workspace-archives/{tenant}/{user}/{workspace_id}.tar.gz ./a.tar.gz`
   (S3 兼容形态:`aws s3 cp --endpoint-url <endpoint> s3://<bucket>/… ./a.tar.gz`)
3. 解包回 NAS:`mkdir -p <nas_root>/{tenant}/{user} && tar -xzf a.tar.gz -C <nas_root>/{tenant}/{user}`
4. 清墓碑:`rm <nas_root>/{tenant}/.deleted/{user}`
5. 行复位:`UPDATE user_workspace SET deleted_at = NULL, archived_object_key = NULL WHERE id = '…';`
6. 下一轮 janitor 全量扫会把 size_bytes 扫正(30 分钟内)。
步骤 4/5 都完成前别让该用户领沙箱。

## 一次性配置
### OSS 生命周期规则(兜底删除)
控制台 → 目标 bucket → 数据管理 → 生命周期 → 新建规则:前缀 `workspace-archives/`,90 天后删除。
(应用层 RetentionCleanupJob 已做同样的事;此规则兜它挂掉的情况。)
### NAS 自动快照(取代云上每日全量备份)
NAS 控制台 → 快照 → 自动快照策略:每日一快照,保留 7 天,绑定 workspace 文件系统。
### 每日全量备份云上退役声明
云路径不跑 supervisor 的每日 volume 备份(该链路只在 compose/supervisor 形态存续)。
云上的恢复面 = NAS 快照(整树误操作)+ OSS 归档(单用户删除)。

## 已知窗口期与复活语义(硬要求②)
- **PR-1 已上线、PR-2 未上线的窗口期**:软删用户的上传**不入账**(增量记账对软删行
  早退)且无人清扫——字节滞留 NAS 直到 janitor 上线后第一轮收割。
- 上传路径与沙箱内 `write_file` **不查软删标记**(W2 既定设计):归档完成后目录仍可能
  被「复活」(purge 前已联入的会话/残存热沙箱)。janitor 每轮重新收割:**覆盖上传**同
  key 档案 → 再删目录。覆盖意味着旧档案内容被复活内容替换——恢复操作要赶在复活写入
  前,或先下载核对档案内容。
- 复活面收敛:acquire 有软删闸(新沙箱拿不到);purge 完成后主体已删,上传 API 通常
  不可达——实际复活窗口 ≈ purge 前已联入的会话存续期。

## 故障排查
- janitor 活着吗:日志 `workspace_janitor.*`;多副本 advisory lock(classid 8619)单飞,
  loser 静默跳过是常态,不是故障。
- 单用户归档反复失败:`workspace_janitor.archive_failed` 有堆栈;幂等重试,无 DLQ;
  连续多轮失败按堆栈修,手工补救走「归档恢复」逆操作。
- 配额显示与实际不符:30 分钟粒度 + 同名覆盖上传重复计数是已知偏差,全量扫兜正。
```

- [ ] **Step 2: SetDirQuota 调研文档**。`docs/research/2026-08-10-nas-setdirquota-feasibility.md`,回答 spec § 七的三问。**结论骨架(按此成文;标注[待核实]处执行时若有 WebSearch 能力先查阿里云 NAS 官方文档核掉,查不了就保留标注)**:

```markdown
# NAS SetDirQuota 可行性调研(2026-08-10,波 3 § 七;只出结论不接线)

## 背景与现状
- 挂载形态:静态 PV `workspace-nas`(`nasplugin.csi.alibabacloud.com`,
  `path=/workspaces`,NFSv3;`infra/k8s/base/control-plane/workspace-nas.yaml`)。
  control-plane 挂整树;沙箱 `pvName + subPath={tenant}/{user}`(`agent_sandbox.py`
  csi-volume-config)。**没有**走 CSI 动态供给的 `volumeAs: subpath +
  volumeCapacity: "true"` 那条路(那条才是「PVC storage 映射目录硬配额」,仅容量型,
  见 docs/research/2026-07-28-storage-selection.md)。
- 应用层现状:PR-1 双闸 + 三层记账 + janitor 全量扫已闭环,自救路 = 删文件。

## 问 1:我们的 CSI 挂载方式下可行吗?
SetDirQuota 是 NAS OpenAPI(对文件系统内指定目录设配额,支持 user/group 维度),
配额落在服务端目录上,与客户端怎么挂载(静态 PV、subPath)解耦 → 机制上可行。
[待核实] 支持范围:通用型 NAS 支持、极速型不支持——我们的实例类型需控制台/
`DescribeFileSystems` 确认;若为极速型则直接不可行。

## 问 2:500 配额目录/文件系统上限的含义?
配额目标 = `{tenant}/{user}` 目录,数量 = 活跃用户数。500 上限 → 只够 ~500 用户,
多租户 SaaS 规模下**不能**做全量二道闸。降级为「每租户一条」只能限租户总量,与
本波「每用户上限」语义不匹配(spec § 十已明确不做租户级聚合)。

## 问 3:要不要作为二道硬闸叠加?
**结论:不叠加,不接线。**
1. 500 上限挡死全量覆盖(问 2);
2. 应用层闸已闭环且有人话自救路径;SetDirQuota 超限表现为沙箱内 NFS 写 EDQUOT/EIO,
   工具报错文案不可控,体验劣化;
3. OpenAPI 带外状态又一套,易与租户配额页漂移。
**保留的点杀用法(入 BACKLOG)**:单个惯犯用户在 janitor 30 分钟窗口内狂写、绕过
增量记账时,可对其目录手工 SetDirQuota(500 上限内个案无压力),作运维手段不作产品面。

## 引用
- docs/research/2026-07-28-storage-selection.md(volumeAs/volumeCapacity 仅容量型 + 500 上限出处)
- docs/superpowers/specs/2026-08-03-sandbox-migration-design.md(「500 目录上限是硬伤」原判)
- infra/k8s/base/control-plane/workspace-nas.yaml(挂载形态)
```

- [ ] **Step 3: BACKLOG + purge 一句**。①`docs/BACKLOG.md` 追加:`- SetDirQuota 点杀:对单个绕记账窗口的惯犯用户手工设目录配额(调研结论 docs/research/2026-08-10-nas-setdirquota-feasibility.md;不做产品面)`(格式照文件现有条目);②`grep -rn "workspace_marked_deleted\|mark_deleted" services/control-plane/src/control_plane/api/*.py services/control-plane/src/control_plane/purge/user_purge.py docs/ | grep -v test` 找 purge 端点 docstring/响应文档位置,在 workspace 步骤描述处补一句:「工作区字节的归档与释放由 janitor 异步完成(30 分钟粒度);purge 返回时只保证标记落地。」(spec § 十一)。

- [ ] **Step 4: 验证 + Commit**

Run: `uv run ruff check docs 2>/dev/null; git diff --stat`(文档无 lint;确认只碰四个文件)

```bash
git add docs/runbooks/workspace-quota-and-archive.md docs/research/2026-08-10-nas-setdirquota-feasibility.md docs/BACKLOG.md <purge文档路径>
git commit -m "docs: 工作区配额与归档 runbook(含 PR-1→PR-2 窗口期声明)+ SetDirQuota 调研结论(不接线)"
```

---

### Task 9: 终检 + PR

- [ ] **Step 1: 全量本地验证**(逐条跑,全绿才继续;既知红名单:`test_eval_engine_live.py` 6 条 `ModuleNotFoundError: tools` 是本机 import-mode 差异、`test_rls_detect` 全量红单跑绿是隔离 flake——**对照 main 基线,只有分支不新增红才算过**):

```bash
cd packages/expert-work-runtime && uv run pytest
cd packages/expert-work-persistence && uv run pytest
cd services/control-plane && uv run pytest
cd services/orchestrator && DOCKER_HOST= uv run pytest
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
cd packages/expert-work-runtime && uv run pytest -m integration
cd services/control-plane && uv run pytest -m integration
cd apps/admin-ui && pnpm typecheck && pnpm test
uv run ruff check . && uv run ruff format --check .
```

mypy 跑 CI 同款范围(`.github/workflows` 里 grep mypy 的目标目录,照跑)。

- [ ] **Step 2: 推分支 + PR**

```bash
git push -u origin sandbox-w3-pr2-janitor
gh pr create --title "feat: 波 3 PR-2 janitor+收尾——put_stream 流式归档、三阶段清扫、已用/上限、runbook" --body "$(cat <<'EOF'
## Summary
- `ObjectStore.put_stream`:流式 multipart(实现内攒片,整流不足一片退化单次 put),杀掉 1.5GiB 内存上限(supervisor 老路径冻结不动)
- `WorkspaceJanitorWorker`(30min,advisory lock 8619 单飞):①归档软删用户(流式 tar.gz → OSS 确定性 key → rm → mark;**复活目录重新收割**)②配额全量扫兜底 ③`_scratch` mtime>24h 清理
- 用户运维页工作区 tab「已用/上限」(effective_limit,不读冻结列)
- runbook `workspace-quota-and-archive.md`(含 **PR-1→PR-2 窗口期不入账声明**)+ SetDirQuota 调研结论(不接线)

Spec: docs/superpowers/specs/2026-08-09-sandbox-migration-w3-design.md § 四/五/六/七
硬要求两条(PR-1 终审缺口收敛)均落地:复活收割 = Task 5 矩阵测试;窗口期声明 = runbook。

## Test plan
- [ ] runtime/persistence/control-plane/orchestrator 单测 + MinIO/PG integration 全绿
- [ ] janitor 重入矩阵 6 格 + 复活收割 + 双实例单飞逐条变异自证
- [ ] admin-ui typecheck + vitest
- [ ] 真栈验收(合并后,spec § 八):删用户 → OSS 档案出现/NAS 目录消失/重跑不重复;_scratch 老目录清、活目录无伤;运维页已用/上限
EOF
)"
```

---

## Self-Review(已执行)

- **Spec 覆盖**:§ 4.1 流程+重入矩阵 → Task 5;§ 4.2 put_stream/流式打包 → Task 1/2;§ 4.3 OSS 生命周期 → Task 8 runbook(RetentionCleanupJob 已有硬删,零新代码——比 spec 写作时的认知更进一步,已在 Global Constraints 声明);§ 五三阶段/锁/停机 → Task 3/4/5/6;§ 六.2 已用/上限 → Task 7(§ 六.1 上传 429 文案是 PR-1 已交付);§ 七 runbook/调研 → Task 8;§ 八测试策略逐条对号(契约档=Task 1、崩溃重入矩阵=Task 5、mtime 边界+双实例=Task 3、变异自证散在各 task);§ 十一 purge 文档一句 → Task 8。硬要求①=Task 5(矩阵第 5 行+专测),②=Task 8(runbook 专节)。
- **占位符扫描**:无 TBD/TODO;Task 6 的 app.py 行号区间与 Task 7 的 JSX 细节以「先读现场再落」表述,均给了精确锚点与完整代码意图,不属占位。
- **类型一致性**:`put_stream` 签名 Task 1 定义与 Task 5 调用一致;`JanitorRunStats` 字段五处使用同名;`workspace_archive_key` 参数序 (tenant, user, workspace) 与调用一致;`ws.id`(DTO 字段名,非 workspace_id)已核对 `note_written` 现行用法。
