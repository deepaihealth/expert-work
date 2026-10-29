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
* 不存在(从没有过、或已被留存作业清掉)→ :class:`VersionUnknown`。

指定版本一律从「最新版」那一行按 id 取(``get_version_by_id(artifact_id=latest.artifact_id)``),
不按名字查第二次 —— 两次按名字的查询之间同名产物可能被更新,落到另一条产物上。

**已知限制(``agent_key=None`` 的会话)**:``thread_meta.agent_name`` 为空的会话不按 agent
收口,同一用户两个 agent 的同名产物都看得见,按名字只能取到 ``updated_at`` 最新的那一条。
这一轮登记的是 agent A 的 X v2,之后 agent B 也登记了 X,那么这一轮的「第 2 版」解析到
B 的那条产物上(B 有 v2 就下发 B 的 v2,没有就 404)。今天建会话的三条路径(控制台建会话、
对外发起对话、定时任务)都写 ``agent_name``,为空的只有早期的历史会话;要根治得让产物清单
带上产物行 id(对外可见的新字段),目前没有做。

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
    # 调用方从「最新版」那一行按 id 取 ``pinned``,所以它们本该是同一条产物;不是就当作
    # 不存在(防御:绝不把别的产物的内容当这一版下发)。
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
