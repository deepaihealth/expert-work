"""``WorkspaceJanitorWorker`` —— 沙箱迁移波 3 PR-2(spec § 五)。

Periodic background worker driving the NAS-workspace housekeeping
phases: archiving soft-deleted user workspaces to the object store
(``_sweep_archives``), reclaiming expired ``agents/<key>/inputs/`` entries
(``_sweep_agent_inputs``, B-61 T12), refreshing per-user size accounting
(``_sweep_sizes``), and reaping stale ``_scratch`` sandbox-tmp directories
(``_sweep_scratch``). One cycle every ``interval_s`` (default 1800s = 30
minutes, spec § 五).

All phases are implemented: ``_sweep_archives`` uploads soft-deleted
users' workspaces then ``rm -rf``s the NAS directory (marking the row
archived), ``_sweep_agent_inputs`` expires injected-variable caches and
per-run directories by TTL, ``_sweep_sizes`` walks every tenant/user
directory and refreshes its size accounting, and ``_sweep_scratch`` reaps
stale ``_scratch`` sandbox-tmp directories. Structure mirrors
:class:`~control_plane.sandbox_reap_worker.SandboxReapWorker` (start/stop/
loop) and :class:`~control_plane.skill_curator.SkillCurator` (advisory-lock
wrapper around the cycle body).

No DLQ: a stale ``_scratch`` dir or a failed phase is retried on the next
cycle for free (both are idempotent — reaping an already-gone dir, or an
already-swept size, is a no-op). A losing replica on the advisory lock
silently skips the whole cycle (``JanitorRunStats(skipped=True)``); a single
failing phase logs and lets the remaining phases run (``_run_cycle``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import WORKSPACE_JANITOR_LOCK_CLASSID
from control_plane.workspace_archive import (
    empty_tar_gz_bytes,
    stream_directory_tar_gz,
    workspace_archive_key,
)
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.persistence.workspace.layout import (
    WORKSPACE_AGENTS_DIR,
    WORKSPACE_INPUTS_DIR,
    WORKSPACE_UPLOADS_DIR,
)
from expert_work.runtime.storage import ObjectStore
from orchestrator.tools.nas_workspace_store import DELETED_DIR, workspace_user_root
from orchestrator.tools.prefetch_script import CACHE_DIRNAME, CACHE_TTL_S

logger = logging.getLogger(__name__)

#: How often to run a full cycle. Spec § 五: 30 minutes.
_INTERVAL_S = 1800.0

#: ``stop()`` 等待当前这一轮 cycle 收尾的上限,超时就取消。照
#: ``SandboxReapWorker`` 的教训:**别用** ``interval_s + 5`` 这种公式——
#: 本 worker 的 interval 是分钟级,那个式子给出的「上界」比 K8s 默认 30s
#: 优雅期还长,等于没有上界。5 秒足够一轮正常 cycle 收尾;收不了尾就取
#: 消——三个阶段都是周期性、幂等的,下次启动会重来。
_STOP_TIMEOUT_S = 5.0

#: 这个超时守的不是「正常一轮该多久」,而是「一个挂死/泄漏的锁会话最多
#: 赖多久」——连接掉线本就会立即放锁,超时只在会话活着但卡住时兜底。真
#: 正的约束是:它必须是**任何**合理一轮 cycle 时长的上界,包括部署当天
#: 第一轮——PR-1→PR-2 之间攒的整批归档 backlog + 首次全树 du 一次性追
#: 平,时长和稳态后的 30 分钟一轮不是一个量级。60 分钟撑不住那一轮:命中
#: 后 PG 杀掉赢家的锁会话,锁释放,另一副本起并发 cycle(重复 multipart
#: 上传;tar 打包与 rmtree 赛跑,可能用半成品档案覆盖掉刚打完的完整档
#: 案),赢家侧 ``finally: rollback()`` 再报一个误导性的 ``cycle_failed``。
#: 改成 12 小时,把部署当天的 backlog 轮也罩住。曾考虑「每步 ping 一下续
#: 命」代替长超时,否决:单个用户的多 GiB 上传本身就可能撑爆任何按步长
#: 定的 ping 节奏假设,续命点找不到一个处处安全的粒度。
_LOCK_TXN_TIMEOUT_MS = 12 * 60 * 60 * 1000

#: spec § 五:临时沙箱寿命 ≤20min,72 倍余量。判据只看目录 mtime,不查 DB。
_SCRATCH_MAX_AGE_S = 24 * 3600.0

#: 与 orchestrator ``agent_sandbox._SCRATCH_DIR`` 同值(私名不跨包 import)。
_SCRATCH_DIR = "_scratch"

#: 这两层目录名**从生产者那边 import,不抄字面量**:``WORKSPACE_INPUTS_DIR`` 是共享包
#: 的公开名(``inputs_doc.inputs_rel_dir`` 与浏览面的保留前缀都用它),``CACHE_DIRNAME``
#: 是 ``tools/prefetch_script`` 的公开名。上面 ``_SCRATCH_DIR`` 之所以只能抄,是因为对面
#: 那个是私名;这两个不是,抄了就是留一条会静默走散的缝。
_INPUTS_DIR = WORKSPACE_INPUTS_DIR
_INPUTS_CACHE_DIR = CACHE_DIRNAME

#: ``tools/prefetch_script._rewrite`` 的临时文件名(``inputs.json`` + ``.tmp``)。
#:
#: **这个名字今天在 ``inputs/`` 这一层打不到东西**:`_rewrite` 写的是
#: ``inputs_path + ".tmp"``,而 ``inputs_path`` 是 ``inputs/<run_id>/inputs.json``,
#: 残留只会落在 run 目录里、随整棵目录被收(brief 给的理由有误,已回报)。留着是
#: 防御性的 —— 生产者哪天把改写挪上一层,这里不用再想起来。
#: **但承载它的机制不是死的**:``file_names`` 非 ``None`` = 「只收名单里的文件」,
#: 正是这道闸让 ``inputs/README.md`` 这类别人的文件活下来(变异 M6 实证)。
_INPUTS_TMP_NAME = "inputs.json.tmp"


@dataclass(frozen=True)
class _ReclaimPolicy:
    """一条回收策略。``enabled=False`` 的条目本批不生效,代码路径仍然走到。"""

    label: str  # 指标与日志用
    ttl_s: float
    enabled: bool

    def expired(self, mtime: float, now: float) -> bool:
        """这个条目该收了吗。

        ``enabled`` 判在**这里**、而不是在调用方用 ``if`` 跳掉整条策略:关着的
        策略照样被扫、照样逐条走到判定,只是答案恒为「不收」。B-63 要开 uploads
        与产物那两条时拨的是这个开关,不是补一段从没跑过的新代码。
        """
        return self.enabled and now - mtime >= self.ttl_s


#: per-run 目录的 TTL。**判据是「距最后一次预拉写入」,不是「run 还活不活着」**:
#: 写 ``inputs/<run_id>/`` 的只有 START 侧的 inputs 节点,**续跑不重新经过它**,所以一个
#: 等审批的 run 挂得比 TTL 久,回来时 ``inputs.json`` 已经没了 —— 模型退回从提示词手抄
#: 长串,正是 B-61 要治的病。30 天覆盖现实中的长审批挂起(与 Codespaces「30 天」同一条
#: 先例)。**敢从 7 天抬到 30 天是因为 T11**:内容寻址之后同一个 URL 跨轮只存一份,占用
#: 不再随轮数相乘,真正吃配额的是缓存条目不是这些几 KB 的 JSON。
_RUN_DIR_TTL_S = 30 * 24 * 3600

#: 缓存条目的 TTL。**不变式:``_CACHE_TTL_S > _RUN_DIR_TTL_S + CACHE_TTL_S``**
#: (``CACHE_TTL_S`` = 预拉侧的新鲜期 24h,从那边 import,不抄)。
#:
#: 为什么必须严格大于:命中**不 touch**(T11 裁定 A),所以条目的 mtime 最多比「最近一次
#: 被引用」早 24 小时,而 run 目录的 mtime 就是那次引用的时刻。两条 TTL 相等时,缓存条目
#: 会比引用它的 ``inputs.json`` **先死最多 24 小时** —— 那段时间里 ``inputs.json`` 的
#: ``local_path`` 非空却指向一个已被删掉的文件,而工具描述对模型的承诺是「非空 = 平台已经
#: 下好了,直接用,不用再联网」。
#:
#: 所以这里**不写字面量,直接把不等式写成代码**:滞后项取预拉侧的 ``CACHE_TTL_S`` 本人
#: (它哪天改了这里跟着走),再加 2 天余量 —— 30 + 1 + 2 = **33 天**。要改成写死的数字,
#: 先读上面这段;``test_cache_ttl_outlives_the_run_dir_that_names_it`` 钉着这条不等式。
_CACHE_TTL_S = _RUN_DIR_TTL_S + CACHE_TTL_S + 2 * 24 * 3600

#: B-61 T12 本批只启用 inputs 两条;uploads 与产物的条目**先放在这里但关着**——
#: 它们删的是用户数据,需要产品定 N、需要发布前告知、还要对外删除端点(B-62)当自救
#: 出口,那是 B-63 的事。机制一次写好,B-63 落地时是把开关拨开 + 接 touch 点,不是重写。
#:
#: **B-63 拨开关前要先处理的两件事**(不然拨开当天就说谎):① ``JanitorRunStats`` 的
#: ``inputs_files_removed`` / ``inputs_dirs_removed`` 是**所有策略合计**,uploads 的删除会
#: 计进名字里写着 inputs 的字段 —— 先拆开或改名;② ``uploads`` 的落点只覆盖三处之一
#: (见 :data:`_TARGETS`)。
_POLICIES = (
    _ReclaimPolicy(label="inputs_cache", ttl_s=_CACHE_TTL_S, enabled=True),
    _ReclaimPolicy(label="inputs_run_dir", ttl_s=_RUN_DIR_TTL_S, enabled=True),
    _ReclaimPolicy(label="uploads", ttl_s=90 * 24 * 3600, enabled=False),
    _ReclaimPolicy(label="artifacts", ttl_s=90 * 24 * 3600, enabled=False),
)


@dataclass(frozen=True)
class _ReclaimTarget:
    """一条策略在 agent 子树里的落点:``agents/<key>/<subdir>/`` 下收什么。

    只看这一层的直接子项(不递归):收的东西要么是一整棵 per-run 目录、要么是一个
    条目文件,再往下就是它们自己的内容。
    """

    subdir: str
    #: 收目录:只收名字是 UUID 的(per-run 目录)。``False`` = 这一层的目录一律不碰。
    #:
    #: ``cache/`` 取 ``False`` 是**刻意与 ``inputs/`` 那一层相反**的:``inputs/`` 下别人
    #: 建的目录不是垃圾(不碰),而 ``cache/`` 整个命名空间是平台的,里面本不该有目录
    #: (生产侧只写文件:``_store`` 的 ``mkstemp`` + ``os.replace``)。代价写在这里:
    #: agent 代码能在自己的 ``/workspace/inputs/cache/`` 里 ``mkdir``,那种目录**两侧都
    #: 不回收**(生产侧 ``_cached_name`` 对目录 ``os.unlink`` 必然失败并被 suppress),
    #: 于是它是一处**不会自愈、也没有信号**的配额泄漏。要收它得先决定「平台该不该删
    #: agent 在平台目录里建的东西」—— 那是个产品决定,不在本批里(已上报)。
    uuid_dirs: bool
    #: 收文件:``None`` = 这一层的文件全收;否则只收名字在集合里的。
    file_names: frozenset[str] | None


#: 每条策略去哪儿收,键是 :attr:`_ReclaimPolicy.label`;``None`` = **还没有落点**。
#: 两条关着的策略各自欠着一笔,拨开关之前都得先还:
#:
#: * ``uploads`` 这一条**只覆盖 ``agents/<key>/uploads/``**(B-50 搬迁之后的位置),
#:   而上传件一共落在三处:① 这里;② **用户根的 ``uploads/``** —— ``api/
#:   _workspace_shared.workspace_agent_path`` 在 ``agent_key`` 为空(会话没绑 agent /
#:   机器线程)时**原样返回** ``rel``,是**今天仍在写**的路径;③ ``shared/uploads/``
#:   —— 搬迁留下的 legacy(只读、不再写入),被
#:   ``test_legacy_toplevel_and_shared_uploads_are_still_reserved`` 钉成合法位置。
#:   ②③ **不在 ``agents/`` 下**,本 phase 的遍历(用户根 → ``agents/`` → agent key)
#:   结构上就到不了 —— B-63 拨开这个开关要补的是**第二条遍历**,不是在这张表上多加
#:   一行;不补就是个只收三分之一、自己不会说话的开关。
#: * ``artifacts`` **显式写 None**(不是漏配):产物没有保留前缀 —— ``save_artifact``
#:   只是把 agent 子树里任意位置的一个文件**登记成一行**(``tools/artifact.py``),
#:   纯文件系统的清扫器认不出哪个文件是产物,``layout.py`` 的
#:   ``WORKSPACE_RESERVED_PREFIXES`` 正是为此只列 machinery 与 uploads。「产物在哪」
#:   要等 B-63 先定义(它的方案是让目录自描述)。
#:
#: 两条都一样:**开关拨开 ≠ 立刻生效**,先补落点/遍历,否则拨开也收不到东西。
_TARGETS: dict[str, _ReclaimTarget | None] = {
    "inputs_cache": _ReclaimTarget(
        subdir=f"{_INPUTS_DIR}/{_INPUTS_CACHE_DIR}", uuid_dirs=False, file_names=None
    ),
    "inputs_run_dir": _ReclaimTarget(
        subdir=_INPUTS_DIR, uuid_dirs=True, file_names=frozenset({_INPUTS_TMP_NAME})
    ),
    "uploads": _ReclaimTarget(subdir=WORKSPACE_UPLOADS_DIR, uuid_dirs=False, file_names=None),
    "artifacts": None,
}

if {policy.label for policy in _POLICIES} != _TARGETS.keys():
    # 导入期就炸,不给「静默什么都不收」留位置:两张按 label 索引的表一旦对不上,
    # 漏配的那条策略拨开 enabled 也是个哑开关,而没有任何运行期信号会提到它。
    # 用显式 raise 不用 assert —— assert 在 ``-O`` 下会被整段裁掉。
    raise RuntimeError("workspace_janitor: _POLICIES 与 _TARGETS 的 label 必须一一对应")


def _is_uuid_name(name: str) -> bool:
    """名字是不是 ``str(run_id)`` 的规范形状。

    比「``UUID(name)`` 解析得了」严:后者还接受 32 位无横杠 hex、``{...}``、
    ``urn:uuid:…`` 与横杠乱放的写法。生产者写进去的只有 ``str(run_id)``,而这是一条
    **删除**路径、兄弟目录是 agent 自己造的 —— 宽松的代价是别人的
    ``inputs/aaaa…aaaa/`` 被整棵 rmtree。既有 ``_list_uuid_dirs`` 也是宽的那种写法,
    但它枚举的是平台自己造的目录,代价不一样。
    """
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


#: 打开目录用的 flags。``O_NOFOLLOW`` = 不跟随**末段**软链,``O_DIRECTORY`` = 打开
#: 的必须真是目录。这两个位是本 phase 安全性的本体,见 :func:`_open_dir`。
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY


@contextlib.contextmanager
def _open_dir(name: str, *, parent: int | None = None) -> Iterator[int]:
    """相对 ``parent`` 打开一个目录拿 fd,**绝不跟随软链**;用完即关。

    这是本 phase 唯一的下降方式,理由是它删的东西在 **agent 自己写得了的目录**里:
    B-60 之后每次 exec 把 ``agents/<key>/`` bind 成 ``/workspace``,沙箱里由模型驱动
    的代码可以 ``rm -rf inputs && ln -s <任意目标> inputs``。而 ``os.scandir(路径)``
    会跟随末段软链 —— 跟过去一次,这个以控制面身份、对整棵 NAS 有写权限的后台任务
    就成了越界删除器(评审 PoC:``inputs`` 指向租户目录时,另一个用户的整棵工作区被
    ``rmtree``;``inputs/cache`` 指向绝对路径时连容器自己的文件都进射程)。

    ``O_NOFOLLOW`` 让这一步在**打开的那一刻**就失败(Linux ELOOP / macOS ENOTDIR),
    之后的 ``scandir`` / ``unlink`` / ``rmtree`` 全部相对 fd 做:路径字符串再也进不了
    删除调用,check 与 use 之间也就没有可以插一条软链的窗口。
    """
    fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _is_plain_dir(entry: os.DirEntry[str]) -> bool:
    """是不是一个**不经软链**的真目录;stat 失败(并发删 / ESTALE)当作不是。"""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


@dataclass(frozen=True)
class _ReclaimScope:
    """一次回收的坐标 —— **只为日志存在**:谁的、哪个 agent。

    fd 化之后底层只拿得到一个裸 fd,失败日志就没法再打 ``path=``(那本来也只是这三个
    标识拼成的字符串)。所以把标识显式带下来:失败路径与成功路径(``reclaimed``)、
    兄弟告警(``reclaim_base_unusable`` / ``reclaim_agent_failed``)口径一致 ——
    **永远只有标识,永远没有条目名**(条目名是租户内容)。
    """

    tenant_id: UUID
    user_id: UUID
    agent_key: str


def _reclaim_entries(
    fd: int, policy: _ReclaimPolicy, target: _ReclaimTarget, now: float, scope: _ReclaimScope
) -> tuple[int, int]:
    """在**已经打开**的这一层目录里按策略收条目,返回 ``(文件数, 目录数)``。

    删除一律走 ``dir_fd``(``os.unlink(name, dir_fd=fd)`` /
    ``shutil.rmtree(name, dir_fd=fd)`` —— 后者 ``avoids_symlink_attacks`` 为真,内部
    也是 ``O_NOFOLLOW`` 逐级下降),不拼路径字符串。单条目失败 log + 继续,扫描中途
    失败(ESTALE)也只是提前收尾:**已经删掉的照样记数**,不然 stats 会少报。
    """
    files = dirs = 0
    try:
        with os.scandir(fd) as entries:  # fd 归调用方所有,scandir 不会关掉它
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir:
                        # 只认 UUID 形状的目录名:别人建的目录不是垃圾。
                        if not target.uuid_dirs or not _is_uuid_name(entry.name):
                            continue
                    elif entry.is_file(follow_symlinks=False):
                        if target.file_names is not None and entry.name not in target.file_names:
                            continue
                    else:
                        continue  # 软链 / 特殊文件:不是本平台造的,不碰
                    if not policy.expired(entry.stat(follow_symlinks=False).st_mtime, now):
                        continue
                except OSError:
                    continue
                try:
                    if is_dir:
                        shutil.rmtree(entry.name, dir_fd=fd)
                        dirs += 1
                    else:
                        os.unlink(entry.name, dir_fd=fd)
                        files += 1
                except OSError:
                    logger.warning(
                        "workspace_janitor.reclaim_failed tenant=%s user=%s agent=%s policy=%s",
                        scope.tenant_id,
                        scope.user_id,
                        scope.agent_key,
                        policy.label,
                    )
    except OSError:
        logger.warning(
            "workspace_janitor.reclaim_scan_failed tenant=%s user=%s agent=%s policy=%s",
            scope.tenant_id,
            scope.user_id,
            scope.agent_key,
            policy.label,
        )
    return files, dirs


def _reclaim_target(
    agent_fd: int, policy: _ReclaimPolicy, target: _ReclaimTarget, now: float, scope: _ReclaimScope
) -> tuple[int, int]:
    """下降到 ``target.subdir`` 再收 —— **每一段**都相对上一级的 fd 打开。

    **逐段是承重墙,不是写法偏好**:``O_NOFOLLOW`` 只管**最后**一段。折成一次
    ``openat(agent_fd, "inputs/cache", O_NOFOLLOW)`` 的话,``inputs`` 就成了中间段、
    照样被跟随 —— ``inputs`` 是软链且指向的目录里恰好有个真的 ``cache/`` 时,里面的
    过期文件就被删了(评审 PoC 实测 ``(1,0)``)。而 ``inputs`` 与 ``inputs/cache`` 两段
    都在 agent 自己写得了的目录里。钉住这条的是
    ``test_sweep_does_not_traverse_a_symlinked_intermediate_segment``。

    目录不存在(绝大多数 agent 没有 ``inputs/``)与软链都从这里抛 ``OSError``
    给调用方分流。
    """
    with contextlib.ExitStack() as stack:
        fd = agent_fd
        for part in target.subdir.split("/"):
            fd = stack.enter_context(_open_dir(part, parent=fd))
        files, dirs = _reclaim_entries(fd, policy, target, now, scope)
    return files, dirs


def _reclaim_user(
    user_dir: Path, *, tenant_id: UUID, user_id: UUID, now: float
) -> dict[str, tuple[int, int]]:
    """一个用户工作区里按 :data:`_POLICIES` 逐条回收,返回 ``{label: (文件数, 目录数)}``。

    ``agents/`` 下是 agent key(**不是 UUID**),复用不了 ``_list_uuid_dirs``;而且整条
    下降链必须走 :func:`_open_dir`,不能把路径字符串交给 ``scandir``。单个 agent 失败
    log + 继续 —— 一个 ESTALE 不该带走这一轮其余的 agent。

    删成功就地记一条 ``(tenant, user, agent, policy)`` 粒度的日志:这是一条**不可逆**
    的删除路径,事后要答得出「谁的哪个 agent、按哪条策略、丢了几个」。**只记标识与计
    数,永不记条目名** —— 名字是租户内容(上传件的文件名、产物名可能是客户的人名)。
    """
    tally = {policy.label: (0, 0) for policy in _POLICIES}
    agents_root = user_dir / WORKSPACE_AGENTS_DIR
    try:
        with _open_dir(str(agents_root)) as agents_fd:
            with os.scandir(agents_fd) as entries:
                agent_keys = sorted(entry.name for entry in entries if _is_plain_dir(entry))
            for agent_key in agent_keys:
                scope = _ReclaimScope(tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)
                try:
                    with _open_dir(agent_key, parent=agents_fd) as agent_fd:
                        for policy in _POLICIES:
                            target = _TARGETS[policy.label]
                            if target is None:
                                # 还没有落点的策略(``artifacts``),见 _TARGETS 上方注释。
                                # 注意这与 ``enabled=False`` 的 ``uploads`` **不对称**:
                                # uploads 有落点,于是照样逐 agent 下降 + 全量 scandir,
                                # 只为对每个条目求一个恒假的谓词(「关着的策略也要走到判
                                # 定」是 brief 的明确要求,B-63 拨开关那天才不是新代码);
                                # 而没有落点的这条连下降都做不了。代价:上传件多的用户,
                                # 每轮白扫一遍 uploads/。拨开关时这笔开销就变成了实际工作。
                                continue
                            try:
                                files, dirs = _reclaim_target(agent_fd, policy, target, now, scope)
                            except FileNotFoundError:
                                continue  # 这个 agent 没有这一层目录(常态)
                            except OSError:
                                # 软链(ELOOP/ENOTDIR)、权限、ESTALE —— 都只跳过这一条
                                logger.warning(
                                    "workspace_janitor.reclaim_base_unusable "
                                    "tenant=%s user=%s agent=%s policy=%s",
                                    tenant_id,
                                    user_id,
                                    agent_key,
                                    policy.label,
                                )
                                continue
                            if files or dirs:
                                logger.info(
                                    "workspace_janitor.reclaimed "
                                    "tenant=%s user=%s agent=%s policy=%s files=%d dirs=%d",
                                    tenant_id,
                                    user_id,
                                    agent_key,
                                    policy.label,
                                    files,
                                    dirs,
                                )
                            had_files, had_dirs = tally[policy.label]
                            tally[policy.label] = (had_files + files, had_dirs + dirs)
                except OSError:
                    logger.warning(
                        "workspace_janitor.reclaim_agent_failed tenant=%s user=%s agent=%s",
                        tenant_id,
                        user_id,
                        agent_key,
                    )
    except FileNotFoundError:
        return tally  # 用户根下还没有 agents/(没跑过任何 agent)
    except OSError:
        logger.warning("workspace_janitor.agents_scan_failed path=%s", agents_root)
    return tally


def _list_uuid_dirs(path: Path) -> list[tuple[UUID, Path]]:
    """列出 path 下目录名可解析为 UUID 的子目录——布局约定
    (workspace_user_root)之外的东西(_scratch/.deleted/lost+found/垃圾)
    天然被 UUID 解析挡掉。单目录扫描失败(NFS ESTALE、权限问题等 OSError)
    不炸整轮——log + 返回已收集到的部分,让调用方(``_sweep_sizes``)继续
    处理其余兄弟目录(照 Global Constraint「单目录/单用户失败 log + 继
    续」,呼应 ``workspace_quota._du`` 同样的容错口径)。"""
    out: list[tuple[UUID, Path]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                try:
                    out.append((UUID(entry.name), Path(entry.path)))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("workspace_janitor.scan_failed path=%s", path)
        return out
    return sorted(out, key=lambda t: str(t[0]))


@dataclass
class JanitorRunStats:
    """One cycle's tally — returned by :meth:`WorkspaceJanitorWorker.run_once`."""

    archived: int = 0
    reharvested: int = 0
    refreshed: int = 0
    scratch_removed: int = 0
    #: ``_sweep_agent_inputs`` 这一轮收掉的条目数(按 :data:`_POLICIES` 全部策略合计)。
    inputs_files_removed: int = 0
    inputs_dirs_removed: int = 0
    skipped: bool = False


class WorkspaceJanitorWorker:
    """Runs the four-phase sweep on a timer, single-flight across replicas.

    Safe to deploy on every replica: a losing replica's
    ``pg_try_advisory_xact_lock`` attempt returns immediately with
    ``skipped=True`` rather than duplicating work (unlike
    ``SandboxReapWorker``, this worker's archive phase has a real side
    effect — an object-store upload — so duplicating it across replicas
    would be wasted GiB-scale transfer, not just wasted CPU; hence the lock,
    same call as ``SkillCurator``).
    """

    def __init__(
        self,
        *,
        user_workspaces: UserWorkspaceStore,
        quota_service: WorkspaceQuotaService,
        object_store: ObjectStore,
        workspace_root: str,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        interval_s: float = _INTERVAL_S,
        archive_enabled: bool = True,
    ) -> None:
        self._user_workspaces = user_workspaces
        self._quota_service = quota_service
        self._object_store = object_store
        self._workspace_root = workspace_root
        self._session_factory = session_factory
        self.interval_s = interval_s
        # 归档 phase **自己**的前提:对象存储得是持久后端。内存后端重启即丢,
        # 90 天恢复承诺会悄悄落空。其余 phase(回收 / 记账 / scratch)与对象
        # 存储无关,不跟着一起关 —— 见 :meth:`_sweep_archives` 与 app.py 的装配点。
        self._archive_enabled = archive_enabled

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    async def _loop(self) -> None:
        # No sweep at startup — a restart is exactly when in-flight uploads
        # / sandbox tmp dirs are most likely mid-flight, and nothing
        # degrades by waiting one interval (照 SandboxReapWorker)。
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("workspace_janitor.cycle_failed")

    async def run_once(self) -> JanitorRunStats:
        """Run one cycle, single-flight across replicas.

        ``session_factory=None`` (single-process / in-memory stack) skips
        the lock entirely — no cross-replica race to guard against. With a
        factory, a ``pg_try_advisory_xact_lock`` miss returns
        ``JanitorRunStats(skipped=True)`` immediately, no cycle attempted
        (照 ``SkillCurator.run_once``)。
        """
        if self._session_factory is None:
            stats = JanitorRunStats()
            await self._run_cycle(stats)
            return stats
        async with self._session_factory() as lock_session:
            # Long-hold guard: the lock txn stays open for the whole cycle;
            # keep it off any idle-in-transaction reaper.
            await lock_session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            got = (
                await lock_session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:cid, hashtext(:k))"),
                    {"cid": WORKSPACE_JANITOR_LOCK_CLASSID, "k": "workspace_janitor"},
                )
            ).scalar_one()
            if not got:
                await lock_session.rollback()
                return JanitorRunStats(skipped=True)
            stats = JanitorRunStats()
            try:
                await self._run_cycle(stats)
                return stats
            finally:
                # rollback ends the txn → releases the xact advisory lock.
                await lock_session.rollback()

    async def _run_cycle(self, stats: JanitorRunStats) -> None:
        # 回收排在 ``_sweep_sizes`` **之前**:那一步本来就逐用户跑全树 du,
        # 排在它前面回收,同一轮的体积记账天然反映回收量,零额外开销;排到最后
        # 就得再 du 一遍全树(见 test_reclaim_lands_in_the_same_cycle_size_accounting)。
        for phase in (
            self._sweep_archives,
            self._sweep_agent_inputs,
            self._sweep_sizes,
            self._sweep_scratch,
        ):
            try:
                await phase(stats)
            except Exception:  # 单阶段炸不拖累后续阶段;下轮自然重试
                logger.exception("workspace_janitor.phase_failed phase=%s", phase.__name__)

    async def _sweep_archives(self, stats: JanitorRunStats) -> None:
        """软删标记文件为发现源(``.deleted/{user}``,``user_purge`` 只
        落标记不碰 DB 行)——按 tenant 目录下 ``DELETED_DIR`` 里能解析成
        UUID 的条目逐用户归档。单用户失败 log + 继续,不拖累其余用户;标
        记文件本身永不删除(墓碑,见 :meth:`_archive_one`)。

        ``archive_enabled=False``(对象存储不是持久后端)时整个 phase 跳过:归档
        会 ``rm -rf`` 用户目录,而内存 object store 重启即丢 —— 那等于把数据删了
        却没有档案。**只跳这一个 phase**,回收 / 记账 / scratch 照跑。
        """
        if not self._archive_enabled:
            logger.info("workspace_janitor.archive_disabled_non_durable_object_store")
            return
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
            except OSError:
                logger.warning("workspace_janitor.marker_scan_failed path=%s", tenant_dir)
                return out
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
            await self._user_workspaces.soft_delete(workspace_id=ws.id, now=datetime.now(UTC))
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
                logger.info("workspace_janitor.reharvested tenant=%s user=%s", tenant_id, user_id)
            return

        if ws.archived_object_key is not None:
            return  # 稳态墓碑:标记留着挡 acquire,行已收口
        if key not in await self._object_store.list_prefix(key):
            # 生前无目录(或上传前崩且目录本来就空缺)→ 统一产出空档案
            await self._object_store.put(key, empty_tar_gz_bytes(), content_type="application/gzip")
        await self._user_workspaces.mark_archived(workspace_id=ws.id, archived_object_key=key)
        stats.archived += 1

    async def _sweep_agent_inputs(self, stats: JanitorRunStats) -> None:
        """按 :data:`_POLICIES` 回收 ``agents/<agent_key>/`` 下的过期条目。

        发现源与 :meth:`_sweep_sizes` 同:tenant / user 两层 UUID 目录;再往下由
        :func:`_reclaim_user` 接手 —— ``agents/`` 里是 agent key(**不是 UUID**),
        而且从那一层起整条下降链都走 :func:`_open_dir`(fd 相对、不跟随软链)。

        **判据只看 mtime,不碰 atime**:NAS 多半挂成 ``noatime``/``relatime``,
        atime 不可信。cache 条目的 mtime 由预拉脚本维持成「最近一次下载时间」——
        24h 内命中不重下(mtime 不动)、超期命中会重下刷新 mtime —— 于是它就是
        「最近引用时间」的 24h 粒度近似;per-run 目录的 mtime 则被预拉每写一次
        ``inputs.json`` 顶新,所以**预拉刚写过的目录一定是新鲜的**(删错它就打断了
        正在跑的那一轮,见 ``test_sweep_never_touches_a_recently_written_run_dir``)。

        这句话**只在「预拉写过之后 TTL 之内」成立,不等于「run 还活着就安全」**:写
        ``inputs/<run_id>/`` 的只有 START 侧的 inputs 节点,续跑不重新经过它,于是一个
        等审批等了超过 :data:`_RUN_DIR_TTL_S` 的 run 回来时文件已经没了。TTL 取 30 天
        就是为了让这个窗口盖住现实中的长审批挂起 —— 见 :data:`_RUN_DIR_TTL_S`。

        ``now`` 取一次、整轮共用:同一轮里先后扫到的条目按同一条时间线判定。
        """
        root = Path(self._workspace_root)
        now = time.time()
        totals = {policy.label: (0, 0) for policy in _POLICIES}
        for tenant_id, tenant_dir in await asyncio.to_thread(_list_uuid_dirs, root):
            for user_id, user_dir in await asyncio.to_thread(_list_uuid_dirs, tenant_dir):
                tally = await asyncio.to_thread(
                    _reclaim_user, user_dir, tenant_id=tenant_id, user_id=user_id, now=now
                )
                for label, (files, dirs) in tally.items():
                    had_files, had_dirs = totals[label]
                    totals[label] = (had_files + files, had_dirs + dirs)
                    stats.inputs_files_removed += files
                    stats.inputs_dirs_removed += dirs
        # 每轮一条汇总(即使是 0):这是一条不可逆的删除 phase,「这一轮跑过、收了多
        # 少」本身就是要能在日志里查到的事实。逐 (tenant, user, agent, policy) 的明细
        # 由 _reclaim_user 在真删掉东西时记,空轮不产生任何明细行。
        #
        # 三个字段**同一个口径**:全部取本 phase 的 ``totals``。别拿 ``stats.inputs_*``
        # 去填 files/dirs —— 那是**整轮累计**的,今天与 phase 局部相等只是因为这个 phase
        # 每轮跑一次,而这条巧合没人会记得;哪天 phase 被调用两次,同一行里的
        # files/dirs 与 by_policy 就会自相矛盾。
        logger.info(
            "workspace_janitor.reclaim_summary files=%d dirs=%d by_policy=%s",
            sum(files for files, _ in totals.values()),
            sum(dirs for _, dirs in totals.values()),
            ",".join(f"{label}:{files}/{dirs}" for label, (files, dirs) in totals.items()),
        )

    async def _sweep_sizes(self, stats: JanitorRunStats) -> None:
        """文件系统为发现源:按 tenant/user 两层 UUID 目录全量扫,逐用户调
        :meth:`WorkspaceQuotaService.refresh`(建行 + 软删早退 + du +
        ``update_size``)。行不存在也扫——``refresh`` 自己会建行。单用户失败
        log + 继续,不拖累其余用户(NAS 并发写删场景常态)。
        """
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

    async def _sweep_scratch(self, stats: JanitorRunStats) -> None:
        scratch_root = Path(self._workspace_root) / _SCRATCH_DIR

        def _stale_dirs() -> list[Path]:
            cutoff = time.time() - _SCRATCH_MAX_AGE_S
            out: list[Path] = []
            try:
                with os.scandir(scratch_root) as it:
                    for entry in it:
                        try:
                            if (
                                entry.is_dir(follow_symlinks=False)
                                and entry.stat(follow_symlinks=False).st_mtime < cutoff
                            ):
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


__all__ = ["JanitorRunStats", "WorkspaceJanitorWorker"]
