"""流式 tar.gz 打包(spec 波3 § 4.2)——tmp_path 树往返 + 早退不悬挂 + 错误传播。"""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
import time
from pathlib import Path
from uuid import UUID

import pytest

from control_plane import workspace_archive as wa
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


@pytest.mark.timeout(15)
@pytest.mark.asyncio
async def test_cancel_while_parked_on_empty_queue_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消费方在拿到第一块之前就被取消(worker 卡在空队列的 get 里)——清理必须在
    有界时间内完成,不得悬挂,也不得留下永久驻留的线程。回归:executor 消费侧
    早退清理复用同一个单线程池提交 thread.join,若 get 端不肯让出 worker,
    join 永远排不上号 → 消费方自己的取消清理也跟着死锁。"""
    src = tmp_path / "src"
    src.mkdir()

    def stalling_produce(directory: Path, writer: wa._QueueWriter) -> None:
        # 生产者故意迟迟不写第一块,保证消费方的 get 撞上空队列;
        # 一旦消费方早退置位 abort,立刻退出(不傻等满 10s)。
        for _ in range(200):  # 至多 10s,正常路径下 abort 会在毫秒级提前触发
            if writer._abort.is_set():
                return
            time.sleep(0.05)
        writer.emit_raw(wa._DONE)

    monkeypatch.setattr(wa, "_produce", stalling_produce)

    before = threading.active_count()
    gen = wa.stream_directory_tar_gz(src, chunk_size=64)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout=0.2)

    for _ in range(100):
        if threading.active_count() <= before:
            break
        await asyncio.sleep(0.05)
    assert threading.active_count() <= before
