"""``WorkspaceStore`` 契约测试 —— 两个实现一套用例(design spec § 九,W2 Task 7)。

沙箱迁移波 2 把持久工作区从 supervisor 管理的 docker 卷搬到共享 NAS 卷:本地
/CI 用的 ``SupervisorWorkspaceStore``(代理到 sandbox-supervisor 的
workspace-file HTTP API,行为未变)和生产云侧用的 ``NasWorkspaceStore``
(control-plane 直读挂载的 NAS 树,W2 Task 3)必须在
``orchestrator.tools.workspace_store.WorkspaceStore`` Protocol 边界上表现一
致 —— ``NasWorkspaceStore`` 模块 docstring 的"Parity contract with
SupervisorWorkspaceStore"一节明确点名本文件是防两者漂移的手段。

跑法(照 ``test_sandbox_runtime_contract.py`` 现骨架,同一份纪律不重复解
释)——两档任一环境没准备好就 ``pytest.skip``,不是失败:

* **supervisor 档** —— 同 ``test_sandbox_runtime_contract.py`` 的 supervisor
  档,需要一个真跑起来的 sandbox-supervisor(``docker compose -f
  infra/docker-compose.yml --profile full up -d postgres migrate
  sandbox-supervisor credential-proxy``),再设
  ``EXPERT_WORK_SANDBOX_SUPERVISOR_URL=http://localhost:<映射端口>``。
* **nas 档** —— 零前置,``tmp_path`` 就是 ``NasWorkspaceStore.root``,每条
  用例都在一棵全新的临时目录树上跑。

除 caps 漂移闸(``test_workspace_cap_constants_match_the_supervisor``,只比
较两侧模块里的字面量,不连任何真实环境,同
``test_sandbox_runtime_contract.py`` 的
``test_idle_ttl_matches_supervisor_default`` 手法)之外,每条测试单独打
``@pytest.mark.integration``。

**2000 条 list 上限为什么只在 nas 档测**——``test_list_files_caps_at_the_shared_limit``
需要真建 2000+ 个文件;nas 档是本地文件系统,建 2000 个空文件是毫秒级操
作。supervisor 档要对同一个 docker 卷逐个走它的单文件写路径(每次起一个
容器跑 shell 脚本,``docker_client.py``),2000+ 次会把这条用例拖到分钟
级,不值得——两侧的上限值本身已经由
``test_workspace_cap_constants_match_the_supervisor`` 钉住相等,真正需要
防的漂移是"两侧数值是否还一样",不是"nas 的截断逻辑今天工作、supervisor
的哪天不工作了"这种需要每次都在两侧都实测一遍的场景;后者(supervisor
真的会在 2000 条截断)留给该服务自己的单测覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.sandbox import SandboxSupervisorError
from orchestrator.tools.workspace_store import SupervisorWorkspaceStore, WorkspaceStore


def _supervisor_store() -> WorkspaceStore:
    url = os.environ.get("EXPERT_WORK_SANDBOX_SUPERVISOR_URL")
    if not url:
        pytest.skip("EXPERT_WORK_SANDBOX_SUPERVISOR_URL 未设 —— supervisor 契约档跳过")
    return SupervisorWorkspaceStore(base_url=url)


@pytest.fixture(params=["supervisor", "nas"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> WorkspaceStore:
    if request.param == "supervisor":
        return _supervisor_store()
    return NasWorkspaceStore(root=str(tmp_path))


@pytest.mark.integration
async def test_write_read_list_delete_roundtrip(store: WorkspaceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()

    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path="out/report.txt", data=b"hello"
    )
    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="out/report.txt")
    assert data == b"hello"

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert [f.path for f in files] == ["out/report.txt"]
    assert files[0].size == 5

    await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="out/report.txt")
    files_after = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert files_after == []


@pytest.mark.integration
async def test_list_files_hides_reserved_prefixes(store: WorkspaceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="uploads/in.txt", data=b"in")
    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path="skills/foo/SKILL.md", data=b"skill"
    )
    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path="out.txt", data=b"agent output"
    )

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert [f.path for f in files] == ["out.txt"]


@pytest.mark.integration
async def test_delete_file_rejects_reserved_path(store: WorkspaceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="uploads/in.txt", data=b"in")

    with pytest.raises(SandboxSupervisorError):
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="uploads/in.txt")

    # 拒绝删除意味着文件原样还在。
    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="uploads/in.txt")
    assert data == b"in"


@pytest.mark.integration
async def test_write_file_rejects_over_cap(store: WorkspaceStore) -> None:
    from orchestrator.tools.nas_workspace_store import _MAX_WRITE_BYTES

    tenant_id, user_id = uuid4(), uuid4()
    data = b"\x00" * (_MAX_WRITE_BYTES + 1)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path="huge.bin", data=data)


@pytest.mark.integration
async def test_read_file_rejects_over_cap(store: WorkspaceStore) -> None:
    from orchestrator.tools.nas_workspace_store import _MAX_READ_BYTES

    tenant_id, user_id = uuid4(), uuid4()
    data = b"\x00" * (_MAX_READ_BYTES + 1)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="big.bin", data=data)

    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=tenant_id, user_id=user_id, path="big.bin")


@pytest.mark.integration
async def test_mark_deleted_is_idempotent(store: WorkspaceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)
    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)  # 幂等,不抛


@pytest.mark.integration
async def test_mark_deleted_does_not_affect_existing_files(store: WorkspaceStore) -> None:
    """``mark_deleted`` 是生命周期标记(供 ``SandboxRuntime.acquire`` 的软删
    闸 / 之后的清扫任务消费),不是这四个文件方法本身的行为开关 —— 两个实
    现都不该因为一次 ``mark_deleted`` 就让已有文件从这层接口上消失或读不
    到(内部表示完全不同:supervisor 记一列 DB ``deleted_at``,
    ``NasWorkspaceStore`` 落一个 marker 文件——但两者在 ``WorkspaceStore``
    Protocol 的这四个方法上必须表现一致)。"""
    tenant_id, user_id = uuid4(), uuid4()
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="out.txt", data=b"still here")

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="out.txt")
    assert data == b"still here"
    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert [f.path for f in files] == ["out.txt"]


@pytest.mark.integration
async def test_list_files_caps_at_the_shared_limit(tmp_path: Path) -> None:
    """Task 3 遗留账:``_MAX_LIST_ENTRIES`` 此前没有专测。只在 nas 档跑 ——
    见模块 docstring。"""
    from orchestrator.tools.nas_workspace_store import _MAX_LIST_ENTRIES

    tenant_id, user_id = uuid4(), uuid4()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    for i in range(_MAX_LIST_ENTRIES + 5):
        (user_root / f"f{i:05d}.txt").write_bytes(b"")

    files = await NasWorkspaceStore(root=str(tmp_path)).list_files(
        tenant_id=tenant_id, user_id=user_id
    )
    assert len(files) == _MAX_LIST_ENTRIES


def test_workspace_cap_constants_match_the_supervisor() -> None:
    """caps parity 漂移闸 —— ``NasWorkspaceStore`` 模块 docstring("Parity
    contract with SupervisorWorkspaceStore"一节)明确点名本文件是"pins the
    two implementations together and would catch a drift"的手段。两侧刻意
    各自重新声明一份私有模块级常量(不是互相 import——``orchestrator`` 与
    ``sandbox-supervisor`` 是两个独立服务,不该有运行期依赖),这里逐个比
    对字面量,不连任何真实环境(同
    ``test_sandbox_runtime_contract.py`` 的
    ``test_idle_ttl_matches_supervisor_default`` 手法),故意不打
    ``integration`` marker —— 理应在每一次
    ``pytest -q -m "not integration"`` 全仓扫描里就跑到。
    """
    from orchestrator.tools.nas_workspace_store import (
        _MAX_LIST_ENTRIES,
        _MAX_READ_BYTES,
        _MAX_WRITE_BYTES,
    )
    from sandbox_supervisor.supervisor import (
        _MAX_ARTIFACT_BYTES,
        _MAX_WORKSPACE_LIST_ENTRIES,
        _MAX_WORKSPACE_WRITE_BYTES,
    )

    assert _MAX_READ_BYTES == _MAX_ARTIFACT_BYTES, (
        f"NasWorkspaceStore 的读上限 {_MAX_READ_BYTES} 与 supervisor 的"
        f" _MAX_ARTIFACT_BYTES={_MAX_ARTIFACT_BYTES} 已经不一致。"
    )
    assert _MAX_WRITE_BYTES == _MAX_WORKSPACE_WRITE_BYTES, (
        f"NasWorkspaceStore 的写上限 {_MAX_WRITE_BYTES} 与 supervisor 的"
        f" _MAX_WORKSPACE_WRITE_BYTES={_MAX_WORKSPACE_WRITE_BYTES} 已经不一致。"
    )
    assert _MAX_LIST_ENTRIES == _MAX_WORKSPACE_LIST_ENTRIES, (
        f"NasWorkspaceStore 的 list 上限 {_MAX_LIST_ENTRIES} 与 supervisor 的"
        f" _MAX_WORKSPACE_LIST_ENTRIES={_MAX_WORKSPACE_LIST_ENTRIES} 已经不一致。"
    )
