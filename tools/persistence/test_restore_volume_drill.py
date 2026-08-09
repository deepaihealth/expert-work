"""Stream J.15-补强-2 — restore drill for the volume backup pipeline.

End-to-end: write a tar.gz blob via the lifecycle manager's archive
path into an in-memory ObjectStore, then run ``restore_volume_from_object``
+ ``restore_latest_archive_to_volume`` (with a writer callback that
avoids docker — same pattern as K15 pg-restore drill) and assert the
pulled bytes match.
"""

from __future__ import annotations

import subprocess
from uuid import UUID, uuid4

import pytest
from tools.persistence.restore_volume import (
    _format_new_volume_name,
    _hydrate_volume_with_docker,
    _select_latest_archive_key,
    restore_latest_archive_to_volume,
    restore_volume_from_object,
)

from expert_work.runtime.storage import InMemoryObjectStore


@pytest.mark.asyncio
async def test_restore_volume_from_object_pulls_bytes_to_writer() -> None:
    store = InMemoryObjectStore()
    key = "volume-archive/abc/def/expert-work-ws-x.tar.gz"
    payload = b"FAKE-TAR-GZ-12345"
    await store.put(key, payload, content_type="application/gzip")

    captured: dict[str, bytes] = {}

    async def _writer(volume_name: str, blob: bytes) -> None:
        captured[volume_name] = blob

    report = await restore_volume_from_object(
        object_store=store,
        object_key=key,
        new_volume_name="expert-work-ws-x_restored_drill",
        writer=_writer,
    )

    assert report.object_key == key
    assert report.new_volume_name == "expert-work-ws-x_restored_drill"
    assert report.size_bytes == len(payload)
    assert captured["expert-work-ws-x_restored_drill"] == payload


@pytest.mark.asyncio
async def test_select_latest_archive_prefers_archive_over_backup() -> None:
    """When both J-36 archive and J-29 backup exist, archive wins."""
    store = InMemoryObjectStore()
    tenant_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("22222222-2222-2222-2222-222222222222")

    archive_key = f"volume-archive/{tenant_id}/{user_id}/v.tar.gz"
    backup_key = f"volume-backups/{tenant_id}/{user_id}/2026-05-21/v.tar.gz"
    await store.put(archive_key, b"ARCHIVE", content_type="application/gzip")
    await store.put(backup_key, b"BACKUP", content_type="application/gzip")

    picked = await _select_latest_archive_key(
        object_store=store,
        tenant_id=tenant_id,
        user_id=user_id,
        archive_prefix="volume-archive",
        backup_prefix="volume-backups",
    )
    assert picked == archive_key


@pytest.mark.asyncio
async def test_select_latest_archive_falls_back_to_dated_backup() -> None:
    store = InMemoryObjectStore()
    tenant_id, user_id = uuid4(), uuid4()
    older = f"volume-backups/{tenant_id}/{user_id}/2026-05-20/v.tar.gz"
    newer = f"volume-backups/{tenant_id}/{user_id}/2026-05-21/v.tar.gz"
    await store.put(older, b"OLDER", content_type="application/gzip")
    await store.put(newer, b"NEWER", content_type="application/gzip")

    # No date pinned → lexicographically latest (2026-05-21 > 2026-05-20).
    picked = await _select_latest_archive_key(
        object_store=store,
        tenant_id=tenant_id,
        user_id=user_id,
        archive_prefix="volume-archive",
        backup_prefix="volume-backups",
    )
    assert picked == newer

    # Date pinned → that day's key.
    pinned = await _select_latest_archive_key(
        object_store=store,
        tenant_id=tenant_id,
        user_id=user_id,
        archive_prefix="volume-archive",
        backup_prefix="volume-backups",
        date="2026-05-20",
    )
    assert pinned == older


@pytest.mark.asyncio
async def test_select_latest_archive_returns_none_when_empty() -> None:
    store = InMemoryObjectStore()
    picked = await _select_latest_archive_key(
        object_store=store,
        tenant_id=uuid4(),
        user_id=uuid4(),
        archive_prefix="volume-archive",
        backup_prefix="volume-backups",
    )
    assert picked is None


def test_format_new_volume_name_is_deterministic() -> None:
    assert _format_new_volume_name("expert-work-ws-x", suffix="manual") == (
        "expert-work-ws-x_restored_manual"
    )
    assert _format_new_volume_name("v", suffix="2026-05-21") == "v_restored_2026-05-21"


@pytest.mark.asyncio
async def test_restore_latest_archive_raises_when_no_artifact() -> None:
    store = InMemoryObjectStore()
    with pytest.raises(RuntimeError, match="no archive or backup found"):
        await restore_latest_archive_to_volume(
            object_store=store,
            tenant_id=uuid4(),
            user_id=uuid4(),
            archive_prefix="volume-archive",
            backup_prefix="volume-backups",
            image="expert-work-sandbox:dev",
        )


# --- 真 docker 档:hydrate 的能力集 -------------------------------------


def _docker_available() -> bool:
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "info"],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


@pytest.mark.integration
def test_hydrate_preserves_non_root_ownership_and_modes() -> None:
    """``_hydrate_volume_with_docker`` 必须把归档里 uid 10000 的属主与 mode 原样还原。

    **为什么这条要用真 docker,不能靠断言 argv**:上面那批 drill 用例走的是
    ``writer`` 回调路径,``_hydrate_volume_with_docker`` 一次都没被执行过——
    所以「``--cap-drop ALL`` 之下 GNU tar 根本 chown 不了」这个缺陷在全绿的
    套件下活了很久。断言 argv 里有没有某个 flag 只能锁住"我写的还是我写的",
    锁不住"这组 flag 到底够不够 tar 用";这两者的差值正是这条缺陷本身。

    **归档由真 GNU tar 造,不用 ``tarfile`` 手搓**,而且是生产那一条命令
    (``docker_client.py:582`` 的 ``cd /ws && tar -czf - .``)。这一步是踩出
    来的:手搓版第一版只放 ``sub/`` + ``sub/g.txt``,拿掉 ``--cap-add
    FOWNER`` 照样绿——等于给缺陷发合格证;补上顶层条目之后又变成**连
    baseline 都红**(``./sub/g.txt: Cannot open: Permission denied``),因为
    ``tarfile`` 写出的目录条目与 GNU tar 的不同构,解包侧的延迟定权限逻辑
    走了另一条路。两次都是"归档形状不对"而不是"被测代码不对"。让生产的
    打包命令自己造归档,这类不同构就没有存在的余地。

    树的四个条目各自承重:根 ``.``(属主 10000)与 ``sub``(``0700``)逼 tar
    延迟定目录权限——否则 root 进不去自己刚交出去的目录,这是
    ``DAC_OVERRIDE`` 用不上的原因;``f.txt``(``0644``)逼它在 chown 之后再
    chmod 一个自己已经不是属主的文件,这是 ``FOWNER`` 用得上的原因;
    ``sub/g.txt``(``0600``)确认嵌套层的属主与 mode 也原样落地。
    """
    if not _docker_available():
        pytest.skip("docker 不可用")

    payload = b"restored-bytes"
    image = "debian:bookworm-slim"  # 与沙箱镜像同族(GNU tar,非 busybox tar)
    built = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "mkdir -p /src/sub"
            " && echo top-level > /src/f.txt"
            f" && printf %s {payload.decode()} > /src/sub/g.txt"
            " && chown -R 10000:10000 /src"
            " && chmod 755 /src && chmod 644 /src/f.txt"
            " && chmod 700 /src/sub && chmod 600 /src/sub/g.txt"
            " && cd /src && tar -czf - .",  # 与 docker_client.py:582 逐字同形
        ],
        check=True,
        capture_output=True,
    )

    volume = f"expert-work-restore-drill-{uuid4().hex[:12]}"
    try:
        _hydrate_volume_with_docker(new_volume_name=volume, blob=built.stdout, image=image)
        probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [  # noqa: S607
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{volume}:/ws",
                "--entrypoint",
                "sh",
                image,
                "-c",
                "stat -c '%a %u:%g %n' /ws /ws/f.txt /ws/sub /ws/sub/g.txt; cat /ws/sub/g.txt",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "volume", "rm", "-f", volume],  # noqa: S607
            check=False,
            capture_output=True,
        )

    assert "755 10000:10000 /ws\n" in probe.stdout, probe.stdout
    assert "644 10000:10000 /ws/f.txt" in probe.stdout, probe.stdout
    assert "700 10000:10000 /ws/sub" in probe.stdout, probe.stdout
    assert "600 10000:10000 /ws/sub/g.txt" in probe.stdout, probe.stdout
    assert payload.decode() in probe.stdout, probe.stdout
