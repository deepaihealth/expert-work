"""工作区归档打包(沙箱迁移波 3, spec § 4.2)。

流式生成 tar.gz:``tarfile w|gz`` 在专用线程里写一个有界队列包装的
fileobj,async 侧逐块取出喂 ``ObjectStore.put_stream``——常驻内存
≤ ``chunk_size`` x 队列深度,无总量上限。老 ``archive_volume``
(supervisor,1.5GiB 内存 buffer)冻结不动,云路径用本模块。
"""

from __future__ import annotations

import asyncio
import io
import queue
import tarfile
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

_QUEUE_DEPTH = 4
_DONE = object()
_ABORTED = object()  # 消费方轮询 get 时撞见 abort——早于任何 item 到达
_POLL_INTERVAL = 0.1


def workspace_archive_key(tenant_id: UUID, user_id: UUID, workspace_id: UUID) -> str:
    """确定性归档 key(spec § 4.1)——重试幂等覆盖,不产生重复档案。"""
    return f"workspace-archives/{tenant_id}/{user_id}/{workspace_id}.tar.gz"


def empty_tar_gz_bytes() -> bytes:
    """空 tar.gz(spec § 4.1:用户生前就没有目录 → 统一产出档案,恢复侧无需分叉)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


class _ConsumerGoneError(Exception):
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
                raise _ConsumerGoneError
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
    except _ConsumerGoneError:
        return
    except BaseException as exc:
        try:
            writer.emit_raw(exc)
        except _ConsumerGoneError:
            return


def _poll_get(q: queue.Queue[object], abort: threading.Event) -> object:
    """有超时地等下一条 item;abort 置位后 <=_POLL_INTERVAL 就交回控制权。

    consume 侧和 ``_QueueWriter._emit`` 用同一套轮询手法,原因也相同:这个
    调用是提交给专用单线程 executor 的一个任务,如果直接裸 ``q.get()``(无
    超时)且队列一直空,worker 会被永久占住——消费方取消/早退时,``finally``
    要把 ``thread.join`` 也提交到同一个 worker 收尾,若 worker 被卡死在这
    里,join 永远排不上号,清理本身就死锁了。
    """
    while True:
        if abort.is_set():
            return _ABORTED
        try:
            return q.get(timeout=_POLL_INTERVAL)
        except queue.Empty:
            continue


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
    loop = asyncio.get_running_loop()
    # 专用单线程 executor,而非 loop 共享的默认 executor(``asyncio.to_thread``
    # 走的就是那个):默认 executor 的 worker 线程没有空闲超时,用过一次就
    # 常驻进程,会被 threading.active_count() 计成"消费方早退后的泄漏线程"。
    # 这里显式拥有 + 显式 shutdown,保证生成器退出时不留下任何线程。
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace-archive-io") as pool:
        try:
            while True:
                item = await loop.run_in_executor(pool, _poll_get, q, abort)
                if item is _DONE or item is _ABORTED:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item  # type: ignore[misc]
        finally:
            abort.set()
            await loop.run_in_executor(pool, thread.join, 10.0)
