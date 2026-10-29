"""产物下载的两件共用的事:挑哪一版、从哪读字节(B-235)。

三个下载端点(控制台 ``/v1/artifacts/download``、会话面 ``…/workspace/artifacts/{name}/download``、
对外 ``/v1/agents/{code}/artifacts/download``)都按同一套规则挑版本、读内容,只是各自把
结果翻成自己的响应形状(控制台 ``HTTPException``,对外信封)。规则写在这一处,三处不各写
一份。

**挑哪一版**:没传版本号 → 最新版。传了 → 那一版:

* 有副本(``snapshot_key``)→ 就是它;
* B-235 之前登记的(没有副本)且正是最新版 → 照旧读工作区文件(这一点上与以前逐字相同);
* B-235 之前登记的、已经不是最新版 → :class:`VersionNotRetained`:那一版的字节早被同名的
  后续登记盖掉了,拿最新版冒充正是 B-235 要修的事;
* 不存在(从没有过、或已被留存作业清掉),或查到的那一版不属于「最新版」那条产物
  (``agent_key=None`` 时两次按名字的查询可能落在不同产物上)→ :class:`VersionUnknown`。

**从哪读**:有副本读副本,没有读 ``path_in_workspace``。副本读不到(进程内存后端重启、被
误删)就是读不到 —— 抛出来的仍是 ``SandboxSupervisorError`` 家族,端点既有的
「500 权限 / 413 太大 / 404 不在」翻译照用,**不**退回去读工作区里的当前文件。
"""

from __future__ import annotations

from uuid import UUID

from expert_work.protocol import ArtifactVersion
from orchestrator.tools import SandboxSupervisorError, WorkspaceStore
from orchestrator.tools.artifact_snapshots import ArtifactSnapshotStore

#: 每个下载响应都带上实际下发的版本号 —— 对接方据此核对拿到的是哪一版。
VERSION_HEADER = "X-Artifact-Version"


class VersionUnknown(Exception):  # noqa: N818 - a selection outcome, not an error class
    """请求的版本号没有对应的版本行。"""


class VersionNotRetained(Exception):  # noqa: N818 - a selection outcome, not an error class
    """请求的是 B-235 之前登记、且已不是最新的一版:内容没有单独保留。"""

    def __init__(self, *, requested: int, latest: int) -> None:
        super().__init__(f"version {requested} was not retained; latest is {latest}")
        self.requested = requested
        self.latest = latest


def choose_version(
    *,
    latest: ArtifactVersion,
    requested: int | None,
    pinned: ArtifactVersion | None,
) -> ArtifactVersion:
    """按模块 docstring 的规则挑出要下发的那一版。

    ``pinned`` 是调用方按 ``requested`` 查到的版本行(``requested`` 为 ``None`` 时不用查)。
    """
    if requested is None:
        return latest
    # ``agent_key=None`` 的会话里「最新版」与「指定版」是两次按名字的独立查询,同名多行时
    # 可能落在两条不同的产物上 —— 不属于同一条产物的版本当作不存在。
    if pinned is None or pinned.artifact_id != latest.artifact_id:
        raise VersionUnknown(requested)
    if pinned.snapshot_key is not None or pinned.version == latest.version:
        return pinned
    raise VersionNotRetained(requested=requested, latest=latest.version)


async def read_version_bytes(
    version: ArtifactVersion,
    *,
    tenant_id: UUID,
    user_id: UUID,
    snapshots: ArtifactSnapshotStore | None,
    workspace_store: WorkspaceStore,
) -> bytes:
    """读一版的字节:有副本读副本,B-235 之前的版本读工作区路径。"""
    if version.snapshot_key is not None:
        if snapshots is None:
            msg = "artifact snapshot store is not configured"
            raise SandboxSupervisorError(msg)
        return await snapshots.read(
            tenant_id=tenant_id, user_id=user_id, snapshot_key=version.snapshot_key
        )
    return await workspace_store.read_file(
        tenant_id=tenant_id, user_id=user_id, path=version.path_in_workspace
    )
