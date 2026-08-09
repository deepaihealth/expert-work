"""``AgentSandboxClient`` —— ACS Agent Sandbox 上的 :class:`SandboxRuntime`.

E2B SDK 打底,私有协议(见下)。与 ``HTTPSupervisorRuntime`` 的分工:那个打
本地 docker supervisor(开发/CI),这个打云上平台。两者由 ``build_sandbox_runtime``
按 ``sandbox_backend`` 选,上层工具零感知。

本模块只做 波1 Task 7:``acquire`` / ``release`` / ``destroy`` + 热会话 CAS。
``exec``(Task 8)/``reap``(Task 9)留给后续任务;
:class:`~orchestrator.tools.sandbox_instance_store.SandboxInstanceStore`
Protocol 已预留它们要用的方法位置(见其 docstring)——那个 Protocol 现在
住在自己的模块里(本文件曾顶到仓内 800 行的单文件硬上限,而它通篇是契约
docstring、零可执行代码,是最独立的一刀)。

热会话(spec § 6.2):``(tenant, user)`` 的活跃沙箱记在 ``sandbox_instance``
表,E2B sandbox id 存在既有的 ``container_id`` 列(同一语义:外部运行时给
的实例标识)。并发 acquire 靠迁移 0141 的部分唯一索引定单赢家。

重连失败(spec § 6.3):``connect`` 会因库存不足/欠费/沙箱已被平台超时
kill(见 :data:`_SANDBOX_TIMEOUT_S`——``on_timeout`` 的平台默认行为是 kill,
本实现也**没有**配置任何休眠/暂停语义)而失败 —— 丢弃该行、重新
``create``。工作区权威在外部存储(波 2 起 NAS;波 1 本就还没挂持久工作区),
重建无损。

## 私有协议 + ``patch_e2b`` 导入顺序

E2B 原生协议要求泛域名而我们的证书只覆盖一层子域名,所以走阿里云的私有
协议,这要求在 **导入 ``e2b`` 之前** 调一次 ``patch_e2b(https=False)``。
这个顺序约束连同它的全部理由(为什么不放模块顶层、不放
``build_sandbox_runtime``、为什么单测永远碰不到这个全局副作用)一起住在
:mod:`orchestrator.tools.e2b_patch`,由 :meth:`AgentSandboxClient._sdk` /
:meth:`AgentSandboxClient._sdk_exceptions` 两个唯一的真实 SDK 导入点调用。
不要在别处直接 ``import e2b``。

## 其余两处对 task-7-brief.md 的修正(探针报告已点名)

* ``AsyncSandbox.create()`` 的 ``domain`` / ``api_key`` 走 ``**opts``,不是
  brief 草稿写的具名参数——这里按实测签名以关键字形式传,效果一样。
* ``commands.run`` / ``files.write`` 必须传 ``user="agent"``(见
  :data:`SANDBOX_EXEC_USER`),否则 E2B 默认用户 ``user`` 在我们的沙箱镜像里
  不存在,炸 ``AuthenticationException: invalid username: 'user'``。
  (这里原本写"我们的沙箱镜像(``USER agent``,uid 10000)"—— W2 Task 9 起
  镜像不再声明 ``USER agent``,容器 root 启动;``agent`` 用户仍在、uid 仍是
  10000,结论不变,只是引用的理由是条已删的指令。同一句在
  :data:`SANDBOX_EXEC_USER` 那边当时改对了,这一处漏了。)

## 独立发现、brief 草稿之外的问题(task-7-report.md 有完整记录)

* brief 草稿里 ``_egress_env`` 直接读 ``egress.tenant_id`` / ``egress.sandbox_id``,
  但 :class:`~orchestrator.tools.sandbox.EgressContext` 根本没有这两个字段
  ——只要 egress 非 None 就必炸 ``AttributeError``。这里改成显式接收
  ``tenant_id`` / ``sandbox_id`` 两个参数,由调用方(``acquire``)传入。
* brief 草稿里 ``acquire`` 结尾无条件调用 ``set_container_id``,但"connect
  到已就绪热会话"这条分支从未创建过新的 ``sandbox_id`` 行(``claim_warm``
  的"已被占"分支不建行)——对 SQL 实现是静默 0-row UPDATE,对手写假件
  (``FakeInstanceStore``)是直接 ``KeyError``,``test_concurrent_acquire_has_one_winner``
  会崩。改成只在"这次调用自己建了/重建了沙箱"时才回填。

## 独立审查一轮后追加修复的两处(task-7-report.md "审查修复"一节)

* ``patch_e2b()`` 需要 ``os.environ['E2B_DOMAIN']``——见 ``_ensure_e2b_patched``
  docstring,不重复。
* ``claim_warm`` 原来只返回 container_id 字符串:CAS 输家 connect 成功后,
  ``acquire`` 仍然返回自己在函数开头新铸的 ``uuid4()``——那个 id 从未插进
  任何行,后续 ``destroy(sandbox_id=<那个 id>)`` 会静默 no-op(``get_container_id``
  查不到、``mark_destroyed`` 的 ``WHERE id=...`` 影响 0 行、都不报错)。改成
  ``claim_warm`` 输家分支返回 ``(赢家的真实 sandbox_id, container_id)`` 二元组,
  ``acquire`` 复用成功时把返回值改写成赢家的真实 id——查 SQL store 时这两个
  值本来就在同一次 SELECT 里,不需要多打一次库。同时 ``_create`` 失败(SDK
  冷启炸掉)不再让 CAS 行永久卡死:``claim_warm``/重新占坑已经提交的
  ``state='IN_USE', container_id=NULL`` 行,如果紧接着的 ``create()`` 失败,
  改成先清掉那一行释放槽位再把异常抛出去——否则那个 ``(tenant, user)``
  会被迁移 0141 的部分唯一索引永久卡住,不会有任何东西再去把它解开。

## 再审 Important-1:让槽位一律按 ``sandbox_id``,不按 ``(tenant, user)``

C-1 让"槽位当前的主人不是我"变成**可达**状态,按坐标定位的清理会误伤接
管者。两处全改成按 ``sandbox_id``,``SandboxInstanceStore.drop_warm`` 随
之删除(零调用方,留着就是留一个默认不安全的写法)。完整推理见
:meth:`AgentSandboxClient._unwind_slot`。

## 再审 Important-3:出网 token 只能靠"活得比沙箱久",重发这条路走不通

取值与安全论证在 :attr:`AgentSandboxClient.egress_token_ttl_s`;**被否决的三
条**记在这里免得下一个人重走(完整推理见本轮报告):``connect(envs=...)``
——e2b 2.24.0 没这个形参(核对过 ``ApiParams``);``commands.run(envs=...)``
——SDK 层确实支持,但 ``SandboxRuntime.exec`` 签名里没有 egress
(``EgressContext`` 只绑在 ``acquire`` 上),要拿到它得改 Protocol,而
``HTTPSupervisorRuntime`` 本波次**冻结**、沙箱环境是 ``docker run`` 时烤死的,
跟不了;让沙箱重读 token ——要改两个后端**共用**的镜像里的 sitecustomize,是
波 2 规模的改动。真正的自愈解法是给热会话总年龄设上限、到点强制重建(需要
``claim_warm`` 把 ``acquired_at`` 也透出来)——**已落地(PR-B #1b)**:
:meth:`AgentSandboxClient.acquire` 复用分支按 :meth:`AgentSandboxClient._max_warm_age_s`
(``egress_token_ttl_s // 2``)封顶,超龄强制重建。原「连续 24 小时每 15 分钟
被用一次的热会话撞 407」的残留风险随之关闭;407 的可观测兜底另见
``control_plane`` 的 egress 审计指标(PR-B #1a)。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from expert_work.common.egress_token import mint_egress_token
from expert_work.persistence import SANDBOX_SKILLS_ROOT
from orchestrator.tools.e2b_patch import _ensure_e2b_patched
from orchestrator.tools.nas_workspace_store import workspace_deleted_marker, workspace_user_root
from orchestrator.tools.sandbox import (
    EgressContext,
    SandboxOutcome,
    SandboxSupervisorError,
    agent_key_envs,
)
from orchestrator.tools.sandbox_image_contract import (
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_S,
    SANDBOX_EXEC_UID,
    SANDBOX_EXEC_USER,
    SANDBOX_IMAGE_ENV,
    SANDBOX_PYTHON_FLAGS,
    WORKSPACE_ROOT,
)
from orchestrator.tools.sandbox_instance_store import SandboxInstanceStore

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


#: 传给 ``create()`` / ``connect()`` 的沙箱存活上限(秒)。
#:
#: **必须显式传**:e2b 2.24.0(``e2b/sandbox_async/main.py:171-198``)的
#: ``timeout`` 默认 **300 秒**,``lifecycle.on_timeout`` 默认 ``"kill"`` 而非
#: ``"pause"``;2026-08-04 集群实测印证(``get_info()`` 回
#: ``started_at=10:49:10 / end_at=10:54:10``,正好 300s;``lifecycle=None``)。
#: 不传的话沙箱 5 分钟就被平台杀掉——下次 acquire 的 connect 失败 →
#: 清行 → 重建,能自愈(波 1 验收的热复用那一项恰好在 5 分钟窗口内
#: 跑完,所以没暴露),代价是白付一次 35-40s 冷启 + 工作区里的文件没了。
#:
#: **为什么是 20 分钟**:要"我们自己的 reap 空闲扫是主角、平台超时只是兜底",
#: 两者不能互抢。下界由 ``_IDLE_TTL_S``(15 分钟)定死——比它短则平台先动
#: 手,掐掉还没到我们空闲线的活跃热会话;上界是"编排进程整体失联时兜底还要
#: 及时",几小时等于让兜底名存实亡。5 分钟余量 ≈ ``SandboxReapWorker`` 一个
#: 完整扫描周期(240s)再加富余。
#: ``test_platform_timeout_outlives_idle_ttl`` 钉住这个不等式。
#:
#: **刻意不设** ``lifecycle``:``{"on_timeout": "pause"}`` 是 E2B 的语义,
#: 阿里云 ACS 这侧是否真的实现了 pause/resume **未经验证**(探针只跑过
#: create/connect/kill)。没验证过的休眠配置正是本轮那条失真 docstring 的
#: 由来,不重蹈 —— 要开 pause 先上集群实测。
_SANDBOX_TIMEOUT_S = 20 * 60

#: ``destroy_reason`` written when :meth:`AgentSandboxClient.release` tears
#: down a non-warm (no ``user_id``) sandbox. Same literal as the docker
#: supervisor's ``DESTROY_REASON_RELEASE``
#: (``sandbox_supervisor/domain.py:33``) so ``sandbox_instance`` rows read
#: the same regardless of which backend wrote them — mirrored rather than
#: imported, for the same wrong-direction-dependency reason the persistence
#: package mirrors that service's idle TTL instead of importing its settings.
_RELEASE_DESTROY_REASON = "release"

#: ``destroy_reason`` written when :meth:`AgentSandboxClient.acquire` cannot
#: reconnect to a warm session and tears its row down before rebuilding
#: (spec § 6.3). Same literal the removed ``drop_warm`` wrote, so existing
#: ``sandbox_instance.destroy_reason`` histories stay comparable.
_WARM_RECONNECT_DESTROY_REASON = "warm_reconnect_failed"

#: ``destroy_reason`` written when :meth:`AgentSandboxClient.acquire` retires
#: a warm session that outlived half its egress token's TTL (#1b). Distinct
#: from ``_WARM_RECONNECT_DESTROY_REASON`` so an operator reading
#: ``destroy_reason`` can tell "token 快到期,主动换血" from "connect 失败"。
_WARM_AGE_DESTROY_REASON = "warm_age_expired"

#: ``destroy_reason``(经 :meth:`AgentSandboxClient._unwind_slot`)写的值 ——
#: Task 4 审查 Important-2:``claim_warm`` 成功之后、``create()`` 之前复查
#: 到软删标记出现(见 :meth:`AgentSandboxClient._reverify_workspace_not_deleted`
#: 的完整推理)。与其它 ``_unwind_slot`` reason 常量同一套命名习惯——独立
#: 一个字面量,方便运维在 ``destroy_reason`` 列里按根因区分这条与
#: ``"create_failed"``/``"post_create_failed"``。
_WORKSPACE_DELETED_RACE_REASON = "workspace_deleted_race"

#: :meth:`AgentSandboxClient.exec` 兜底分支判"这是超时"的时长门槛,按
#: ``effective`` 的比例算。envd 掐断一条跑满时限的命令时,SDK 抛的**不总是**
#: ``TimeoutException``:gRPC 流被中断会经 anyio(``BrokenResourceError`` /
#: ``EndOfStream``)→ httpx 的 ``map_exceptions`` 变成 ``httpcore.ReadError``
#: / ``ReadTimeout`` 冒泡上来,而那些异常的 ``str()`` 恰好是空字符串。按
#: **异常类型**判超时因此会漏,漏了就把一次正常超时升级成
#: ``SandboxSupervisorError``,让整个 run 挂掉。改按"跑满了我们自己设的时
#: 限"判,这个判据不依赖 SDK 或 httpx 的异常形态,换版本也不会失效。
#: 0.9 而不是 1.0:``effective`` 是传给 envd 的值,envd 掐断总是稍早于我们
#: 这侧测到的墙钟差,留一成余量。
_TIMEOUT_ATTRIBUTION_RATIO = 0.9

#: ``Sandbox.create(metadata={...})`` 里挂载 NAS 工作区用的 key —— ACS 官方
#: 文档化的动态挂载配方(spec § 一.1),"申请时"挂载,每挂载点一个 dict
#: (``pvName``/``mountPath``/``subPath``)。
_CSI_VOLUME_CONFIG_METADATA_KEY = "e2b.agents.kruise.io/csi-volume-config"

#: 临时沙箱(无 ``user_id``)的 scratch 子树在 NAS 数据根下的目录名。与租户
#: 目录平级、不在任何用户子树内,``NasWorkspaceStore`` 从不遍历到这里。
_SCRATCH_DIR = "_scratch"


def _workspace_subpath(
    *, prefix: str, tenant_id: UUID, user_id: UUID | None, sandbox_id: UUID
) -> str:
    """``csi-volume-config`` 的 ``subPath`` —— 用户工作区 or 临时沙箱的 scratch 子树。

    ``user_id`` 非 None(热会话沙箱)—— ``{prefix}/{tenant_id}/{user_id}``,
    落在 NAS 上的 :data:`~orchestrator.tools.nas_workspace_store` 那棵
    per-(tenant,user) 权威工作区树,与 ``NasWorkspaceStore`` 的
    ``{root}/{tenant_id}/{user_id}`` 布局同一份 subPath 语义(spec § 三)。

    ``user_id`` 为 None(临时沙箱,spec 决策 9)—— 镜像不再预建
    ``/workspace``(Task 9),不挂就没有这个目录、cwd 与文件工具全踩空;
    落 ``{prefix}/_scratch/{sandbox_id}``,每次调用用一个全新的 ``sandbox_id``
    (``uuid4()``),天然不与任何用户或其它临时沙箱撞名。``_scratch`` 不需要
    是 ``WORKSPACE_RESERVED_PREFIXES`` 的成员——它在 NAS 数据根下与租户目录
    平级,不落在任何用户子树内,``NasWorkspaceStore`` 从不遍历到这里。空目
    录的清理并入波 3 的扫描 job(spec 决策 9),不是本方法的职责。

    ``"/".join(p for p in (...) if p)`` 而非 f-string 直接拼接:
    ``prefix=""`` 时(生产配置,见 :attr:`AgentSandboxClient.workspace_subpath_prefix`
    的 docstring)不留前导 ``"/"``——集群实测的生产 PV ``path`` 已经是
    ``/workspaces``,subPath 前面多一段空字符串会拼出
    ``/tenant/user``(前导斜杠),与探针钉死的"相对 PV path"语义不符。
    """
    parts = (
        (prefix, str(tenant_id), str(user_id))
        if user_id is not None
        else (prefix, _SCRATCH_DIR, str(sandbox_id))
    )
    return "/".join(p for p in parts if p)


def _scratch_dir(root: str, sandbox_id: UUID) -> Path:
    """临时沙箱 scratch 子树在 **control-plane 本地挂载点**下的路径。

    与 :func:`_workspace_subpath` 的 ``user_id is None`` 分支是同一个目录的
    两种表示(前者是给平台的 ``subPath``,这里是本进程能 ``mkdir``/``rmdir``
    的本地路径),共用 :data:`_SCRATCH_DIR` 常量——同
    :func:`~orchestrator.tools.nas_workspace_store.workspace_user_root` 之于
    用户子树的理由:同一个位置两处独立拼接迟早会静默失配。
    """
    return Path(root, _SCRATCH_DIR, str(sandbox_id))


def _monotonic() -> float:
    """墙钟接缝 —— 只为让单测能精确摆出"这条命令跑满了时限"这个状态。

    不让测试直接 monkeypatch ``time.monotonic``:那是全局的,asyncio 自己的
    事件循环调度也走它,替换掉会连测试框架一起影响。
    """
    return time.monotonic()


@dataclass
class AgentSandboxClient:
    """:class:`~orchestrator.tools.sandbox.SandboxRuntime` 的 Agent Sandbox 实现。"""

    domain: str
    #: Task 10 实测:普通 dataclass 的 __repr__ 会把凭据吐进 pytest traceback。
    api_key: str = field(repr=False)
    template: str
    store: SandboxInstanceStore
    egress_token_secret: str = field(repr=False)
    egress_proxy_host: str
    egress_proxy_port: int
    #: 测试缝 —— 注入 SDK 替身。``None`` 时用真实 ``e2b.AsyncSandbox``。
    #: ``e2b`` 没有 py.typed 标记(见 pyproject.toml 的 mypy override 说明),
    #: 手写测试假件也不共享一个正式 Protocol —— 两边都用 ``Any``,不是疏漏。
    sdk: Any | None = None
    #: 出网 token 的存活期。**必须长于沙箱可能活着的时间**:token 只在
    #: ``create(envs=...)`` 送一次,之后再没机会换(见 :meth:`_egress_env`)。
    #:
    #: 再审 Important-3:原值 1 小时,在 I-4 之前够用——不传 ``timeout`` 的沙箱
    #: 300 秒就被平台 kill,每次复用都是重建 + 新 token。I-4 给 ``connect`` 也
    #: 传了 ``timeout``,热会话从此可以无限期活下去,于是持续使用的会话会活过
    #: 自己的 token:出网一律 407,且**没有任何自愈路径**(沙箱还活着、reap 不
    #: 收它、connect 不重发环境变量)。这是那次改动引入的用户可见回归。
    #:
    #: 取值 = supervisor 的 ``SandboxSupervisorSettings.egress_token_ttl_s``
    #: 默认值,``test_egress_token_ttl_matches_supervisor_default`` 钉住。
    #: **为什么安全**:① 不是新暴露面——同一密钥、同一 proxy、同一 token 格式,
    #: supervisor 今天就按 24h 铸,两个后端本就该给同一个 agent 同样待遇;
    #: ② token 绑死 ``(tenant, agent, version, sandbox_id, allowlist,
    #: denylist)``,proxy 只按这份签名过的策略放行,泄露不构成越权——沙箱里的
    #: 代码本来就能做这些事(见 ``expert_work.common.egress_token`` docstring);
    #: ③ proxy 是 ClusterIP,集群外够不着
    #: (``infra/k8s/base/credential-proxy/service.yaml``)。
    #:
    #: 为什么不改成"重发 token":见模块 docstring 末尾一节。
    egress_token_ttl_s: int = 24 * 60 * 60
    #: 租户级沙箱数上限的缺省值 —— ``tenant_quota`` 表 SANDBOXES 维度未设
    #: 行时回落。与 supervisor 的 ``default_max_sandboxes``(settings.py:112,
    #: default=50)drift-lock 钉死(``test_default_max_sandboxes_matches_supervisor_default``)。
    default_max_sandboxes: int = 50
    #: 沙箱迁移波 2 —— NAS 工作区挂载的三项配置,dataclass 层面全部默认
    #: None/"",让直接构造这个类的单测不必每次都摆全三项;**生产装配点
    #: ``build_sandbox_runtime`` 会强制要求前两项**(波 2 终审 Important-2:
    #: Task 9 之后镜像不再预建 ``/workspace``,少配 ``workspace_pv_name`` 或
    #: ``workspace_root`` 不再等价于"波 1 行为",而是每次工具调用都炸在 envd
    #: 层 —— 见 ``runtime.py`` 那段 ``missing`` 列表的注释)。这里不重复那道
    #: 校验:三个 Settings 字段本就同源,拆成三个字段是为了每个职责单一。
    #:
    #: ``workspace_pv_name`` —— ``csi-volume-config`` 的 ``pvName``(spec § 三
    #: "沙箱"消费者一节)。
    workspace_pv_name: str | None = None
    #: ``csi-volume-config`` 的 ``subPath`` 前缀(spec § 二之二集群实测:生产
    #: PV ``workspace-nas`` 的 ``path`` 已经是 ``/workspaces``,所以这里是空
    #: 串,拼出的 subPath 就是 ``{tenant}/{user}``)。见 :func:`_workspace_subpath`。
    #:
    #: **必须与 :attr:`workspace_pv_name` 同时配时保持空串**(Task 4 审查
    #: Important-1)—— 这个前缀只影响沙箱侧 ``csi-volume-config`` 的
    #: ``subPath``,而 ``NasWorkspaceStore``(control-plane 直读那棵树的
    #: store,见 :func:`~orchestrator.tools.nas_workspace_store.workspace_user_root`)
    #: 完全不理解前缀,它的布局恒为 ``{root}/{tenant_id}/{user_id}``。非空
    #: 前缀会让沙箱实际挂载到 ``{root}/{prefix}/{tenant}/{user}``,而
    #: mkdir/chmod/软删闸(:meth:`_prepare_workspace_mount`)与
    #: ``NasWorkspaceStore`` 的文件读写全都还在算 ``{root}/{tenant}/{user}``
    #: ——两棵树静默分叉,用户在工作区浏览页看到的文件和沙箱里 `/workspace`
    #: 看到的文件不是同一份,且没有任何报错。``__post_init__`` 在构造期直
    #: 接拒绝这个组合,而不是尝试把前缀透传进 ``NasWorkspaceStore``(那会
    #: 动到 Task 3 加固过的路径解析核心)。
    workspace_subpath_prefix: str = ""
    #: control-plane Pod 本地的 NAS 挂载点(与 ``NasWorkspaceStore.root`` 同一
    #: 个值)—— ``acquire`` 用它做 mkdir + chmod(control-plane 与沙箱内的
    #: agent 现在是同一个 uid 10000,谁先建这棵目录都是它的属主,只需要把
    #: mode 收到 0o700 即可,不需要跨 uid 授权)与软删闸(检查
    #: :func:`~orchestrator.tools.nas_workspace_store.workspace_deleted_marker`
    #: 的落点 ``{root}/{tenant}/.deleted/{user}`` —— **不在**用户子树里,
    #: 见该模块 docstring 与全分支终审 Critical-1)。这是
    #: control-plane 进程能直接 ``os.mkdir``/``os.chmod`` 到的本地路径,不是
    #: 沙箱内路径 —— 与 :data:`WORKSPACE_ROOT`(沙箱内挂载点,恒为
    #: ``/workspace``)是两个不同维度的常量,不要混淆。
    workspace_root: str | None = None

    def __post_init__(self) -> None:
        """构造期不变式检查(同 ``ToolResult.__post_init__`` 的既有惯例:
        ``registry.py`` 里"构造期就报,不要放到用了才报"的模式)。

        Task 4 审查 Important-1 —— ``workspace_pv_name`` 配了、
        ``workspace_subpath_prefix`` 也非空这个组合会让沙箱与
        ``NasWorkspaceStore`` 静默各写各的树(见 :attr:`workspace_subpath_prefix`
        的 docstring),这类失配没有任何运行期报错,只能靠人肉发现"用户上
        传的文件沙箱里看不到"这种症状去反查——比"配置阶段就大声拒绝"贵得
        多。放在 ``__post_init__`` 而不是工厂 ``build_sandbox_runtime`` 里:
        这样无论谁构造 ``AgentSandboxClient``(生产工厂、测试里直接
        构造)都逃不掉这条检查,不依赖调用方记得走工厂。
        """
        if self.workspace_pv_name and self.workspace_subpath_prefix:
            msg = (
                "AgentSandboxClient.workspace_subpath_prefix must be empty "
                "when workspace_pv_name is configured — NasWorkspaceStore "
                "has no concept of a subpath prefix (its layout is always "
                "{root}/{tenant_id}/{user_id}), so a non-empty prefix here "
                "would mount the sandbox's /workspace at a NAS path "
                "NasWorkspaceStore never reads or writes: a silent, "
                "hard-to-diagnose split between what the sandbox sees and "
                "what the workspace-browse/upload/download endpoints see."
            )
            raise ValueError(msg)

    def _max_warm_age_s(self) -> int:
        """热会话总年龄上限 —— 派生自 ``egress_token_ttl_s``,不设第二个常量。

        取一半:token 只在 ``create(envs=...)`` 送一次、无法重发(模块
        docstring 末节,三条重发路线全被否决),所以会话必须死在 token
        之前;一半留足「cap + 单次工具调用最长(≤300s exec)≪ TTL」的
        余量,默认值下 = 12h。到点重建的代价实测(2026-08-05):池命中
        ~2-3s,池空最坏 ~33s,每持续使用的会话 12h 一次。顺带把「热会话
        沿用创建时烤死的旧 egress 政策」的 staleness 也封顶在同一值。
        """
        return self.egress_token_ttl_s // 2

    def _sdk(self) -> Any:
        if self.sdk is not None:
            return self.sdk
        _ensure_e2b_patched(domain=self.domain, api_key=self.api_key)
        from e2b import AsyncSandbox

        return AsyncSandbox

    def _sdk_exceptions(self) -> tuple[Any, Any]:
        """``(TimeoutException, CommandExitException)`` —— ``exec`` 的
        ``except`` 子句要用的两个真实 ``e2b`` 异常类。是 :meth:`_sdk` 的
        兄弟方法,不是又一个独立的 import 入口:``_sdk`` 收敛成唯一入口的
        全部理由,就是让正确性不依赖调用顺序——如果这里改成"反正 ``exec``
        总是先 ``_attach`` → ``_sdk``,到这行时补丁必然已经打过",这个推理
        今天是对的,但恰恰是单一入口这个设计本来要消灭的那种"靠调用顺序
        成立"的论证;下一个人抄这个写法加第三个符号时未必会重新验证。

        生产/测试分支与 :meth:`_sdk` 完全对应:``self.sdk is None``(真实
        SDK)先 ``_ensure_e2b_patched`` 再 import;``self.sdk`` 非 ``None``
        (单测注入 :class:`FakeSdk`)整段跳过 ``_ensure_e2b_patched``,保持
        模块 docstring"私有协议 + patch_e2b 导入顺序"第 3 点的不变式——
        整套单测套件从未真正触发全局 ``kruise_agents`` 补丁。

        返回 ``tuple[Any, Any]`` 而不是
        ``tuple[type[Exception], type[Exception]]`` 是刻意的:``e2b`` 没有
        ``py.typed`` 标记,这两个类在 mypy 下本来就是 ``Any``(见上面
        ``sdk`` 字段的 docstring);标成更窄的 ``type[Exception]`` 会让
        :meth:`exec` 里 ``except exit_exc as exc:`` 绑定到的异常对象被 mypy
        收窄回裸 ``Exception``,丢失 ``CommandExitException`` 专有的
        ``stdout``/``stderr``/``exit_code`` 属性访问。
        """
        if self.sdk is None:
            _ensure_e2b_patched(domain=self.domain, api_key=self.api_key)
        from e2b import CommandExitException, TimeoutException

        return TimeoutException, CommandExitException

    async def _connect(self, container_id: str) -> Any:
        """裸 connect —— 不做错误处理,调用方各自决定失败策略(``acquire``
        的"重连失败则重建" vs ``destroy`` 的"已经不在也无妨,继续清行")。

        显式传 ``timeout=_SANDBOX_TIMEOUT_S``:平台的存活钟是从**建**沙箱那
        一刻起算的,不传的话一个被反复复用的热会话仍然在原始创建时间 + 20
        分钟被杀,"热"的部分越用越短。SDK 的语义是"只延长、不缩短"
        (``connect(timeout=)`` 的 docstring + ``_cls_connect``),所以每次
        重连把兜底推到"此刻 + 20 分钟",与我们自己按 ``last_used_at`` 判空闲
        的 reap 口径对齐——两把钟都跟着"最后一次用"走。

        ``destroy``/``reap`` 也走这里(它们 connect 完就 kill),给一个马上
        要死的沙箱续期无意义但也无害,不值得为此拆成两个方法。
        """
        return await self._sdk().connect(
            container_id,
            domain=self.domain,
            api_key=self.api_key,
            timeout=_SANDBOX_TIMEOUT_S,
        )

    async def _attach(self, sandbox_id: UUID, *, touch_last_used: bool = False) -> Any:
        """从 store 读 ``container_id`` 再 ``connect`` —— ``destroy``/``exec``
        共用的"拿到一个可操作 sandbox 句柄"步骤(Task 8 从 ``destroy`` 原有的
        内联逻辑里抽出,brief Step 3 点名要求"一并抽出来")。

        统一抛 :class:`SandboxSupervisorError`(行不存在/``container_id``
        从未回填,或 connect 本身失败)—— 与 :meth:`_connect` 的"裸、不处理
        错误"不同,这里的调用方(``exec``)没有"静默继续"这个选项,直接让
        错误按 § 6.5 的统一契约冒泡给 ReAct ``tools`` 节点。``destroy`` 仍然
        保留自己外层的 broad ``except Exception``("沙箱已经不在也无妨,继续
        清行")——``SandboxSupervisorError`` 本身就是 ``Exception`` 子类,原样
        被那层接住,行为与重构前(``container_id is None`` 时整段跳过
        connect)等价。

        独立审查 Important-2 追加:``touch_last_used=True``(仅 :meth:`exec`
        传)把"读 container_id"换成
        :meth:`SandboxInstanceStore.touch_and_get_container_id`——同一次
        往返里把该行的 ``last_used_at`` 一并推进到当前时间,而不是"先读
        container_id 再单独一次 UPDATE"两次往返。``destroy`` 保持默认
        ``False``(纯读)——销毁前推进"最后使用时间"这个专给空闲判定用的
        字段没有意义,行马上就要被 ``mark_destroyed`` 了。
        """
        if touch_last_used:
            container_id = await self.store.touch_and_get_container_id(sandbox_id=sandbox_id)
        else:
            container_id = await self.store.get_container_id(sandbox_id=sandbox_id)
        if container_id is None:
            raise SandboxSupervisorError(f"sandbox {sandbox_id} has no recorded container id")
        try:
            return await self._connect(container_id)
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox attach failed: {exc}") from exc

    def _egress_env(
        self, *, tenant_id: UUID, sandbox_id: UUID, egress: EgressContext | None
    ) -> dict[str, str]:
        """出网环境变量 —— 与 supervisor 的 ``_egress_env`` 同语义
        (``services/sandbox-supervisor/src/sandbox_supervisor/supervisor.py:790``)。

        沙箱靠标准 Basic proxy auth 认证到 credential-proxy;token 由共享的
        ``mint_egress_token`` 铸,密钥必须与 proxy 侧的
        ``EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET`` 一致。``tenant_id`` /
        ``sandbox_id`` 显式接参 —— ``EgressContext`` 本身不携带这两项(它只
        绑定 policy + agent 身份),二者来自 ``acquire`` 的调用上下文。
        """
        if egress is None or egress.policy in (None, "none"):
            return {}
        token = mint_egress_token(
            self.egress_token_secret,
            tenant_id=str(tenant_id),
            agent_name=egress.agent_name,
            agent_version=egress.agent_version,
            sandbox_id=str(sandbox_id),
            expires_at=time.time() + self.egress_token_ttl_s,
            allowlist=egress.allowlist,
            denylist=egress.denylist,
        )
        proxy_url = f"http://{token}:@{self.egress_proxy_host}:{self.egress_proxy_port}"
        no_proxy = f"{self.egress_proxy_host},localhost,127.0.0.1"
        # sitecustomize 阵营与 supervisor 沙箱镜像共用一份(design spec
        # § 三.4:credential-proxy/egress 机制原样保留),stdlib urllib 在
        # HTTPS CONNECT 时会丢代理 URL 里的 userinfo —— 同样需要这个显式
        # Basic-auth 头供镜像内的 sitecustomize shim 使用。
        proxy_auth = base64.b64encode(f"{token}:".encode()).decode("ascii")
        return {
            "HTTPS_PROXY": proxy_url,
            "HTTP_PROXY": proxy_url,
            "https_proxy": proxy_url,
            "http_proxy": proxy_url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
            "EXPERT_WORK_EGRESS_PROXY_AUTH": proxy_auth,
        }

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        del thread_id  # 波 1:热会话行不记 thread_id(见 SQL store 的说明)。
        sandbox_id = uuid4()
        # 沙箱迁移波 2 —— 软删闸 + mkdir/chmod,claim_warm 之前(spec § 二之
        # 二)。见 :meth:`_prepare_workspace_mount`(``workspace_root`` 未配
        # 时该方法自己整段跳过,行为与波 1 完全一致)。
        await self._prepare_workspace_mount(
            tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
        )
        existing: tuple[UUID, str, datetime | None] | None = None
        if user_id is not None:
            existing = await self._claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )

        just_created = False
        if existing is not None:
            winner_id, winner_container_id, winner_acquired_at = existing
            if (
                winner_acquired_at is not None
                and (_utc_now() - winner_acquired_at).total_seconds() > self._max_warm_age_s()
            ):
                # #1b:会话活过 token TTL 的一半 —— 再复用下去 token 到期后
                # 出网全 407 且无自愈路径。走与 connect 失败同构的重建:
                # destroy 真 kill + 清行,重占坑,往下落进重建分支。
                # None-acquired_at 不封顶:年龄不可知,同 C-1 拒绝接管的姿态。
                # destroy 与重占坑之间输给第三方竞争者时的安全性,与下面
                # connect-失败分支同一套推理(重占坑返回值同样弃用,输了则
                # set_container_id 按契约抛错、Important-6 守卫拆新沙箱)。
                logger.info(
                    "warm sandbox %s past age cap (%ss), rebuilding for a fresh egress token",
                    winner_id,
                    self._max_warm_age_s(),
                )
                await self.destroy(sandbox_id=winner_id, reason=_WARM_AGE_DESTROY_REASON)
                if user_id is not None:
                    await self._claim_warm(
                        tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                    )
                # 审查 Important-2 —— claim_warm 成功之后、create() 之前的
                # 窄窗口复查软删标记,见 _reverify_workspace_not_deleted。
                await self._reverify_workspace_not_deleted(
                    tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                )
                sbx = await self._create_and_track(
                    tenant_id=tenant_id, sandbox_id=sandbox_id, user_id=user_id, egress=egress
                )
                just_created = True
            else:
                try:
                    sbx = await self._connect(winner_container_id)
                except Exception:
                    # spec § 6.3 —— 重连失败(库存不足/欠费/超过
                    # _SANDBOX_TIMEOUT_S 被平台 kill)必须能重建,不能把 run 打死。
                    logger.warning("warm sandbox connect failed, rebuilding", exc_info=True)
                    if user_id is not None:
                        # 再审 Important-1(与 _unwind_slot 同一个根因):清的是
                        # 连不上的**那一行**(winner_id,claim_warm 刚返回给我们
                        # 的),不是"此刻占着 (tenant, user) 槽位的那一行"——C-1
                        # 的接管让这两者不再恒等,按坐标删会误伤接管者。
                        await self.store.mark_destroyed(
                            sandbox_id=winner_id, reason=_WARM_RECONNECT_DESTROY_REASON
                        )
                        # 重新占坑,让本次 acquire 的 sandbox_id 拥有一行 ——
                        # 否则下面的 set_container_id 无行可回填。槽位刚被让出,
                        # 正常情况下这次必赢;真撞上第三方竞争者(重新占坑输了)
                        # 时这里拿不到行,下面的 set_container_id 会按契约抛错、
                        # 由 Important-6 那层守卫 kill 掉刚建的沙箱并冒泡成一个
                        # 可重试的错误 —— 不会再悄悄多留一个 microVM。
                        await self._claim_warm(
                            tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                        )
                    # 审查 Important-2 —— 同上,重占坑之后、create() 之前
                    # 复查软删标记。
                    await self._reverify_workspace_not_deleted(
                        tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                    )
                    sbx = await self._create_and_track(
                        tenant_id=tenant_id,
                        sandbox_id=sandbox_id,
                        user_id=user_id,
                        egress=egress,
                    )
                    just_created = True
                else:
                    # 复用成功 —— 返回赢家那一行**真实存在**的 sandbox_id,不是
                    # 本次调用开头自铸、从未插入任何行的 uuid4()(审查 Important-3:
                    # 否则后续 destroy(sandbox_id=<自铸 id>) 会静默 no-op,见
                    # SandboxInstanceStore.claim_warm 的 docstring)。
                    sandbox_id = winner_id
        else:
            if user_id is None:  # 临时沙箱不经过 claim_warm,得先插一行——见 create_ephemeral。
                await self._create_ephemeral_row(tenant_id=tenant_id, sandbox_id=sandbox_id)
            # 此刻本次调用自己的行已落库(warm 分支由 claim_warm 插,临时分支由
            # 上一行插)—— 配额检查放在这之后、create() 之前(#8):提前查做不
            # 到,复用路径与新建路径在 claim_warm 返回前无法区分,提前查会把
            # "已到上限租户复用既有会话"也拒掉。
            await self._enforce_quota(tenant_id, own_sandbox_id=sandbox_id)
            # 审查 Important-2 —— 首次占坑成功之后、create() 之前复查软删
            # 标记(ephemeral 分支 user_id 为 None,内部整段跳过)。
            await self._reverify_workspace_not_deleted(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )
            sbx = await self._create_and_track(
                tenant_id=tenant_id, sandbox_id=sandbox_id, user_id=user_id, egress=egress
            )
            just_created = True

        # 全分支终审 Important-6:这一段以前是裸的。create() 一返回沙箱就已经
        # 在平台上跑着,而它的 id 要到下面 set_container_id 才落库——中间任何
        # 一处抛异常都留下一个活着的 microVM,且 destroy / reap /
        # list_stuck_creating 全都找不到它(前两个按 container_id 找,最后一个
        # 面对的正是没有 container id 的行、刻意不 connect/kill)。唯一收得走
        # 它的是平台超时,这正是 _SANDBOX_TIMEOUT_S 真正承重的地方。顺带补上
        # § 6.5 的统一错误契约:files.write 原样抛的是 e2b 自己的异常类型。
        try:
            if just_created:
                await self._chown_workspace_mount(sbx, sandbox_id=sandbox_id)
            for relpath, data in seed_files:
                # sandbox migration wave 2 (spec § 四) — skills live on sandbox-
                # local disk under SANDBOX_SKILLS_ROOT, not the user's NAS-backed
                # WORKSPACE_ROOT; relpath is already namespaced under
                # <agent_key>/ by the caller (build_skill_seed_files).
                await sbx.files.write(
                    f"{SANDBOX_SKILLS_ROOT}/{relpath}", data, user=SANDBOX_EXEC_USER
                )
            if just_created:
                # 连到既有热会话时该行的 container_id 已经是对的(existing 正是
                # 从那里读出来的)—— 只有本次自己建/重建了沙箱才需要回填。
                await self.store.set_container_id(
                    sandbox_id=sandbox_id, container_id=sbx.sandbox_id
                )
        except Exception as exc:
            if just_created:
                # 只有"这次自己建的"才拆:复用既有热会话时 sbx 是别人的沙箱、
                # 那一行也已经健康登记过,种子文件写失败不该把它连锅端了。
                await self._discard_new_sandbox(sbx, sandbox_id=sandbox_id)
            raise SandboxSupervisorError(f"sandbox post-create setup failed: {exc}") from exc
        return sandbox_id

    async def _chown_workspace_mount(self, sbx: Any, *, sandbox_id: UUID) -> None:
        """沙箱侧兜底:把自己刚挂上的 ``/workspace`` 属主改成 uid 10000。

        **为什么需要第二道**(集群实测,2026-08-07)。挂载点的 subPath 目录
        如果在 ``create()`` 时还不存在,**平台会替你建**——建成 ``root:root
        0755``(实测:``/workspaces/<tenant>/<user>`` 就是这个 mode)。沙箱里
        的 agent 以 uid 10000 跑,属主是 root 的目录它写不进去,于是第一次
        往 ``/workspace`` 写就是 ``PermissionError``。权威的修法是
        :meth:`_prepare_workspace_mount` —— control-plane 在 ``create()``
        **之前** 就把目录 mkdir 好(control-plane 与沙箱 agent 现在是同一个
        uid,谁先建都是它的属主),平台因此只会看到一个已存在的目录、不会
        自己建。这一句不取代它,只补它够不到的场合:

        * ``workspace_root`` 没配(把 NAS 挂进 control-plane Pod 的那半边不
          可用——例如契约测试跑在 GitHub runner 上,它对 NAS 没有 NFS 路由)
          时,``_prepare_workspace_mount`` 整段跳过,目录就只能由平台来建;
        * 就算配了,``_prepare_workspace_mount`` 与平台建目录之间理论上仍有
          竞态(我们 mkdir 之前平台已经因为别的沙箱建过了)。

        **为什么 chown 而不是 chmod**:平台自动建的目录属主是 root,不是
        control-plane/agent 共用的 10000。``chmod`` 只能改权限位,换不来
        "属主是 10000"这件事——工作区目录本就该收在 0o700,不该为了给一个
        不共用 uid 的属主开口子而放宽 mode。只有 ``chown`` 能把属主换成
        10000,而非 root 进程无权 ``chown``,所以这一句必须以 root 身份跑。

        **刻意 best-effort**。真正跑得通的生产路径上,目录早就是
        control-plane 建的、属主已经是 10000;而这句以 root 身份跑,NAS 若
        开了 ``root_squash``,root 会被映射成 ``nobody``,对一个别人属主的
        目录 ``chown`` 必然 ``EPERM``。那种情况下失败是**无害**的(目录本来
        就已经是对的),把它抬成 ``create`` 失败会把一个可用的沙箱判死。反
        过来,在这一句是唯一权限来源的场合它失败了,下一次 ``exec`` 会以
        ``PermissionError`` 大声报出来——不会静默变成"能跑但写不进去"。

        ``/workspace`` 是平台建的 **符号链接**(指向
        ``/run/csi/mount-root/nas/<hash>``),``chown`` 默认跟随符号链接,所以
        改到的是挂载点真身,不是链接本身。``workspace_pv_name`` 没配时根本没有
        挂载、``/workspace`` 也不存在,直接跳过。
        """
        if not self.workspace_pv_name:
            return
        try:
            await sbx.commands.run(
                f"chown {SANDBOX_EXEC_UID}:{SANDBOX_EXEC_UID} {WORKSPACE_ROOT}", user="root"
            )
        except Exception:
            logger.info(
                "sandbox workspace chown skipped (sandbox_id=%s) — expected when "
                "control-plane already created the directory and the NAS export "
                "squashes root; only load-bearing where control-plane has no NAS route",
                sandbox_id,
                exc_info=True,
            )

    async def _discard_new_sandbox(self, sbx: Any, *, sandbox_id: UUID) -> None:
        """拆掉一个本次调用刚建起来、但还没能被任何一行记住的沙箱。

        先 kill 再让槽位:kill 是尽力而为(沙箱可能本来就没起来 / 平台侧已
        经不在),失败也要继续走 :meth:`_unwind_slot`,否则就把"漏一个
        microVM"换成了"卡死一个 (tenant, user) 的热会话槽",两害相权更糟。
        两者都不抛,调用方那句 ``raise`` 抛的始终是原始故障。
        """
        try:
            await sbx.kill()
        except Exception:
            logger.warning(
                "failed to kill a sandbox that was created but never recorded "
                "(sandbox_id=%s) — it will be reclaimed by the platform timeout",
                sandbox_id,
                exc_info=True,
            )
        await self._unwind_slot(sandbox_id=sandbox_id, reason="post_create_failed")

    async def _prepare_workspace_mount(
        self, *, tenant_id: UUID, user_id: UUID | None, sandbox_id: UUID
    ) -> None:
        """软删闸 + mkdir/chmod,``claim_warm``/``create_ephemeral`` **之前**
        运行(spec § 二之二)。``workspace_root`` 未配(波 1 默认)整段跳过
        ——不是 belt-and-braces,是这个方法唯一的"关"开关,``acquire`` 无
        条件调它。

        用户沙箱(``user_id`` 非 None)—— 先查软删标记
        (:meth:`_reject_if_workspace_deleted`),再 mkdir + chmod 该用户在
        NAS 上的目录。查在 mkdir 之前:已软删的工作区不该被这次 acquire
        意外复活出一个空目录(下一次 purge 扫描到的应该还是"只有 marker
        的空壳",不是被误建出的新鲜子树)。

        临时沙箱(``user_id`` 为 None)—— 没有软删概念(临时沙箱没有持久
        身份可被软删),只需要 mkdir + chmod 它自己的 scratch 子树
        (``_scratch/<sandbox_id>``)。**取舍**(brief 明确留给实现判断的
        一点):镜像不再预建 ``/workspace``(Task 9)之后,scratch 子树跟用户
        子树一样是 NAS 上全新的目录,权限跟用户子树同一套——不放开权限的话临时沙箱
        的 ``/workspace`` 同样会撞 ``Permission denied``,这不是"要不要顺带
        修"的问题,是同一个真因(spec § 二之二)在另一个 subPath 下的必然
        重现。让两个分支共用同一个 :meth:`_ensure_workspace_dir`,而不是只
        给用户分支修,是本任务对这道取舍的结论。

        无条件跑一次(不区分"这次 acquire 会不会真的 create()")——热会话
        复用(``connect``)不会重新触发挂载,目录早在第一次 ``create()``
        时就已经建好,这里的 mkdir(``exist_ok=True``)/chmod 因此在复用路径
        上是幂等的空操作,不是在"为将要发生的事做准备",而是在"确保这件
        事已经发生过"——比在 :meth:`acquire` 里分叉出"是否会新建"的判断
        更简单。

        自愈**有条件**(复审 I-2,纠正上一版这里"哪怕运维手工改过目录权
        限,下一次 acquire 就会把它修回来"的无条件说法):见
        :meth:`_ensure_workspace_dir` 的 docstring——只在这个目录已经是
        control-plane 属主时,chmod 才会真的把手工改动修回来;目录还没被
        uid 迁移 Job(Task D)接管、属主还是旧 uid 时,chmod 是尽力而为,
        失败不影响 mkdir/acquire 本身继续。
        """
        root = self.workspace_root
        if not root:
            return
        if user_id is not None:
            await self._reject_if_workspace_deleted(root, tenant_id=tenant_id, user_id=user_id)
            target = workspace_user_root(root, tenant_id, user_id)
        else:
            target = _scratch_dir(root, sandbox_id)
        await self._ensure_workspace_dir(target)

    async def _reject_if_workspace_deleted(
        self, root: str, *, tenant_id: UUID, user_id: UUID
    ) -> None:
        """软删闸(spec § 二之二 / 五之二)—— supervisor 对软删工作区同样在
        acquire 拒(HTTP 客户端把 4xx 包成 ``SandboxSupervisorError``),这里
        走同一个可观察契约:直接读 ``NasWorkspaceStore.mark_deleted`` 写的标
        记文件,路径经
        :func:`~orchestrator.tools.nas_workspace_store.workspace_deleted_marker`
        与该 store 共享同一个函数算出(Task 4 审查 Minor:两处独立拼接过路
        径,曾经能各拼各的、静默失配),不经过 ``NasWorkspaceStore`` 实例本
        身——这里只需要"这个文件存在吗"这一个布尔判断,不需要该 store 的读
        写/路径穿越防护机器。

        **marker 不在 ``{tenant}/{user}`` 子树里**(全分支终审 Critical-1):
        那棵树整个经 ``subPath`` 挂进沙箱的 ``/workspace``,沙箱里的 agent
        直接写一个同名文件就能让这道闸永久拒掉该用户——这道闸读的是
        ``{root}/{tenant}/.deleted/{user}``,与用户目录平级,任何 subPath 都
        挂不到,见该函数与 ``nas_workspace_store`` 模块 docstring。

        本方法既是 :meth:`_prepare_workspace_mount` 的初次软删闸,也是
        Task 4 审查 Important-2 修复后 :meth:`_reverify_workspace_not_deleted`
        的复查窗口复用的同一份判定——两处调用同一个函数,不重复一份逻辑。
        """
        marker = workspace_deleted_marker(root, tenant_id, user_id)

        def _marker_present() -> bool:
            """``os.stat`` 而不是 ``Path.exists()`` —— 这道闸必须 fail closed。

            ``Path.exists()`` 对 ``EACCES`` 的处理**跨版本不一致**(四个解释器
            实测):

            ==========  ==========================================
            3.12 / 3.13  抛 ``PermissionError``
            **3.14**     **吞掉,返回 ``False``**
            ==========  ==========================================

            两种都不能要。3.14 那条是真的 fail-open:这道闸的语义是"确认没被
            软删才放行",返回 ``False`` 等于把"我看不见"当成"它不存在",
            已 purge 的用户被静默放行拿到本该拒掉的沙箱,而且不留任何说明是
            权限问题的日志。3.12/3.13 那条不放行,但裸 ``PermissionError``
            穿过整个 ``acquire`` 冒上去,归因不明。

            ``pyproject.toml`` 的 ``requires-python = ">=3.12"`` 允许 3.14,
            所以"今天生产跑 3.12"不构成理由——这与 ``NasWorkspaceStore``
            里 ``Path.is_dir()`` 那处是同一个 pathlib 陷阱的第二个实例。

            读不动本身也不是假想:``{tenant}/.deleted/`` 是 ``0o700``、属主
            control-plane,而 uid 统一(方向变更,见
            ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
            § 六)之后、存量迁移 Job(Task D)跑之前,它的属主还是旧 uid 10002
            而本进程已经是 10000——正好读不动。
            软删闸恰恰在那个窗口里最脆弱,而那个窗口是发布流程的一部分。

            与本次改动的其余部分同一个主题:权限失败被吞成"东西不在"。这里的
            "东西"是"这个用户被删过"这一事实。
            """
            try:
                os.stat(marker)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise SandboxSupervisorError(
                    f"workspace delete-marker unreadable for user {user_id} (tenant {tenant_id})"
                ) from exc
            return True

        if await asyncio.to_thread(_marker_present):
            raise SandboxSupervisorError(
                f"workspace deleted for user {user_id} (tenant {tenant_id})"
            )

    async def _reverify_workspace_not_deleted(
        self, *, tenant_id: UUID, user_id: UUID | None, sandbox_id: UUID
    ) -> None:
        """Task 4 审查 Important-2 —— ``claim_warm`` 成功之后、``create()``
        真正把沙箱建到平台上之前,复查一次软删标记。

        :meth:`_prepare_workspace_mount` 的软删闸只在 ``claim_warm`` **之
        前**查一次。那次检查通过之后、到这次调用真正的 CAS 行落库(或重新
        占坑)之间,存在一个真实的竞态窗口:并发的
        ``NasWorkspaceStore.mark_deleted`` 完全可能在这个窗口里插进来——
        它先落 marker,再调 ``instance_store.get_warm`` 找这个用户的热会
        话;如果这次 ``acquire`` 的行此刻还没提交、或提交了但
        ``container_id`` 还没回填,``get_warm`` 会返回 ``None``,
        ``mark_deleted`` 什么都拆不到,marker 却已经落盘。不补这一步复查
        的话,这次 acquire 会继续往下把一个全新的热会话建给一个"已经被声
        明软删"的用户,且没有任何后续机制会再发现它——只能靠 idle TTL /
        :meth:`_max_warm_age_s` 兜底(15 分钟到 12 小时的窗口)。

        直接复用 :meth:`_reject_if_workspace_deleted`(不重写一份判定),
        marker 出现时按 :meth:`_unwind_slot` 让出这次调用刚占到的 CAS 槽
        位再把异常原样往外抛——与 ``_create_and_track``/
        ``_discard_new_sandbox`` 失败清理走的是同一套收口。``user_id`` 为
        None(临时沙箱)整段跳过,同 :meth:`_prepare_workspace_mount`:临时
        沙箱没有软删概念。
        """
        root = self.workspace_root
        if not root or user_id is None:
            return
        try:
            await self._reject_if_workspace_deleted(root, tenant_id=tenant_id, user_id=user_id)
        except SandboxSupervisorError:
            await self._unwind_slot(sandbox_id=sandbox_id, reason=_WORKSPACE_DELETED_RACE_REASON)
            raise

    async def _ensure_workspace_dir(self, path: Path) -> None:
        """``mkdir(parents=True, exist_ok=True)`` + best-effort ``chmod(0o700)``,off-loop。

        control-plane 与沙箱里的 agent 现在是同一个 uid(方向变更,spec §
        六:共享 gid → 统一 uid)——不管谁先建这棵目录,建的人都是它的属
        主,两侧读写都靠属主位,不需要再对"另一侧"开任何口子。这个目录经
        ``subPath`` 只挂进这一个用户自己的沙箱,不与其它租户共享,收到
        0o700 既不影响 control-plane/agent 任一方的访问,也比此前的
        0o777(world-writable)更紧。

        复审 I-2 —— ``chmod`` 是**尽力而为**,不是无条件必须成功:``mkdir``
        用 ``exist_ok=True``,这个目录几乎总是已经存在(同一个用户后续每一
        次 acquire 都会再跑一遍这个方法),``chmod`` 对一个我们不是属主的既
        存目录一律 EPERM。uid 迁移(control-plane 镜像 10002 → 10000)落地
        当天,存量用户根的属主还是旧 uid 10002——把这些目录改成新 uid 属主是
        一次性迁移 Job(见 Task D)的职责,不是这条每次 acquire 都会跑的路
        径该做的事,也做不到(同 ``NasWorkspaceStore._open_parent_dir_fd``
        建用户根那半边的同款理由,见其注释"修存量目录是一次性迁移 Job 的
        职责,这条写路径不该顺手兼职")。

        这半句纠正的是上一版这里的自愈说法:"哪怕运维手工改过目录权限,
        下一次 acquire 就会把它修回来"只在**这个目录已经是 control-plane
        属主**时成立(这时 chmod 会成功,自愈确实发生);目录还没被迁移
        Job 接管时,chmod 失败是**预期**状态,不该把整个 ``acquire`` 判死
        ——那样迁移 Job 跑完之前,受影响用户会连一次 acquire 都做不成
        (比"某些操作降级"糟得多)。目录本身当前的 mode/属主是否真的允许
        读写,由沙箱真正 ``exec``/文件工具去接触它时自然验证,失败会走
        ``docs/superpowers/plans/2026-08-08-workspace-gid-sharing.md`` Task A
        Step 7 保留清单 item 1 那条窄类型 :class:`WorkspacePermissionError`
        归因路径,不会被这里的静默吞掉误导成别的故障。

        ``mkdir`` 失败(NAS 权限异常 / 挂载点抖动)仍按
        :class:`SandboxSupervisorError` 抛,不静默——那是这条路径本身没法
        绕过的失败,不是"属主还没轮到我们"这种可以延后处理的状态。
        """

        def _do() -> None:
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except PermissionError:
                logger.warning(
                    "workspace dir chmod skipped (not owner yet — expected until the "
                    "uid-migration Job re-chowns legacy user roots, see "
                    "docs/runbooks/sandbox-image-release.md § 存量迁移): %s",
                    path,
                )

        try:
            await asyncio.to_thread(_do)
        except OSError as exc:
            raise SandboxSupervisorError(
                f"failed to prepare workspace directory {path}: {exc}"
            ) from exc

    async def _claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str, datetime | None] | None:
        """``store.claim_warm`` 套上 § 6.5 的统一错误契约。"""
        try:
            return await self.store.claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox warm-session claim failed: {exc}") from exc

    async def _create_ephemeral_row(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """``store.create_ephemeral`` 套上 § 6.5 的统一错误契约(同 :meth:`_claim_warm`)。"""
        try:
            await self.store.create_ephemeral(tenant_id=tenant_id, sandbox_id=sandbox_id)
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox row creation failed: {exc}") from exc

    async def _enforce_quota(self, tenant_id: UUID, *, own_sandbox_id: UUID) -> None:
        """对称 supervisor 的 ``_enforce_quota``(supervisor.py:713-727),
        一处刻意更强:insert-then-check 而非 check-then-insert。

        supervisor 先查后插,两个并发 acquire 可各自读到 limit-1 双双过闸
        小幅超限;这里本次调用的行**已经**落库(它就是 CAS 凭证),count
        必然包含自己 —— 条件用严格 ``>``,超限则 :meth:`_unwind_slot` 拆掉
        自己的行再抛。并发**新建**竞争者之间数得到对方已插的行,彼此不会
        超限 —— 比 supervisor 的竞态语义更紧,顺带免了「拒绝路径漏行」的
        清理债。**但这不是"落库活跃行数任何时刻都 ≤ limit"的全局保证**:
        下面的年龄封顶/重连失败重建路径刻意不经过这道闸(见下一段),它们
        的 destroy 与重占坑之间存在窗口,一个并发的**新建**请求可以在那个
        窗口里数到"重建者旧行已删、新行未插"之间的活跃数、把腾出来的名额
        吃掉 —— 极端交错下总活跃行数可以短暂到 ``limit + 进行中的重建数``。
        自限(重建数有限,不会像真正的配额洞一样无界增长)且自愈(重建
        完成后下一次新建请求照常被这道闸拦住,不需要人工介入)。

        只挂在 :meth:`acquire` 的**全新建行**路径(``existing is None``
        分支)上:热会话复用与年龄封顶/重连失败的 1 换 1 重建路径都不查
        ——总活跃行数不变,查了反而会把已到上限租户的存量会话变成不可重建
        的死会话。

        store 查询本身失败(DB 抖动)时,拆自己的行再抛 ——
        既不能吞掉(fail-open 让配额闸形同虚设),也不能保留这行不拆
        (行已经插入,槽位会一直卡着);拆 + 抛是唯一在两难之间都不留后遗症
        的选择。

        与 supervisor 的第二处不对称:supervisor 拒绝时额外写一条
        ``AuditAction.SANDBOX_QUOTA_DENIED`` 审计行(``supervisor.py:713-727``);
        这里刻意不写 —— ``AgentSandboxClient`` 没有接审计存储的依赖,补一个
        纯为了这一处调用不值得。拆行时留下的
        ``destroy_reason="quota_exceeded"`` 就是可查询的痕迹,已知的能力
        缺口,不是遗漏。
        """
        try:
            limit = await self.store.sandbox_limit_for_tenant(tenant_id=tenant_id)
            active = await self.store.count_active_for_tenant(tenant_id=tenant_id)
        except Exception as exc:
            await self._unwind_slot(sandbox_id=own_sandbox_id, reason="quota_lookup_failed")
            raise SandboxSupervisorError(f"sandbox quota lookup failed: {exc}") from exc
        if limit is None:
            limit = self.default_max_sandboxes
        if active > limit:
            await self._unwind_slot(sandbox_id=own_sandbox_id, reason="quota_exceeded")
            raise SandboxSupervisorError(
                f"sandbox quota exceeded for tenant {tenant_id}: {active - 1}/{limit} active"
            )

    async def _create_and_track(
        self,
        *,
        tenant_id: UUID,
        sandbox_id: UUID,
        user_id: UUID | None,
        egress: EgressContext | None,
    ) -> Any:
        """``_create`` 加上失败时的 CAS 槽位清理(审查 Critical-2)。

        到这一步,``claim_warm``/重新占坑已经**持久化提交**了一行
        (``state='IN_USE'``, ``container_id`` 仍是 NULL)—— 这行本身就是
        CAS 的凭据。如果紧接着的 ``sdk.create()`` 失败(网络抖动/配额/
        SandboxSet 暂时不可用……),没有这层清理的话,那行会永久停在
        ``IN_USE`` + NULL:migration 0141 的部分唯一索引挡住这个
        ``(tenant, user)`` 之后所有新的 ``claim_warm`` INSERT,而
        ``claim_warm`` 自己看到"行存在但 container_id 是 NULL"时的约定行为
        是 raise "already being created"(见 Protocol docstring)—— 没有任何
        东西会再去把这行解开,该用户的热会话彻底卡死,只能手工改库。清掉这
        一行把槽位放出来,让下一次 acquire 能重新正常竞争。

        二审发现(task-7-report.md "二审修复"一节):那次清理自己也
        可能失败(DB 抖动/连接池耗尽/网络分区)。如果不特殊处理,外层
        ``raise`` 会被清理抛出的新异常盖过 —— 调用方按
        ``SandboxSupervisorError`` 设计的 except 链接不住原始错误(类型变
        了),而且槽位依然没清理成功,与本方法开头描述的卡死症状原样重演,
        只是往下多埋一层。用嵌套 try/except 把清理的失败单独
        捕获+记日志(标明需要人工介入),但外层永远重新抛出 ``_create()``
        的原始异常 —— 内层的 except 块正常结束(没有再 raise)后,外层
        bare ``raise`` 重新抛出的是外层 except 语句本来在处理的那个异常,
        不是内层被捕获又丢弃的那个(标准 Python 异常处理语义,已用最小
        复现脚本独立验证过,见报告)。Task 10 追加:临时沙箱同理,见 ``else`` 分支。

        全分支终审 Important-6:上面那套清理本身抽成 :meth:`_unwind_slot`,
        因为 ``create()`` 已经**成功返回**之后的那段(种子文件 +
        ``set_container_id`` 回填)也要走同一套清理,见 :meth:`acquire`。
        """
        try:
            return await self._create(
                tenant_id=tenant_id, sandbox_id=sandbox_id, user_id=user_id, egress=egress
            )
        except Exception:
            await self._unwind_slot(sandbox_id=sandbox_id, reason="create_failed")
            raise

    async def _unwind_slot(self, *, sandbox_id: UUID, reason: str) -> None:
        """清掉这次 acquire 自己插下的那一行(顺带让出它占着的 CAS 槽位)。

        本身**永不抛出** —— 两个调用方都是在处理另一个异常的路上顺手清理,
        清理失败不该把原始错误换掉(那会让按 ``SandboxSupervisorError`` 写的
        except 链接不住,而且槽位照样没清干净,只是把病根往下埋一层)。

        再审 Important-1 —— 热会话分支原来调 ``drop_warm(tenant_id,
        user_id)``:按坐标定位,清的是**此刻占着槽位的那一行**,不一定是本
        次调用插的那一行。C-1 之前两者恒等所以安全(``claim_warm`` 对所有后
        来者都 raise,槽位主人只能是插入它的调用方);C-1 之后不再恒等——A 卡
        在 ``create()`` 里超过 ``_STUCK_CREATE_TTL_S``(5 分钟)时 B 会**合
        法地**接管槽位,A 随后失败走到这里就把 B 那行健康的活行标成销毁,B
        的 microVM 从此不在 ``list_active`` 里、周期 reap 永远看不见它,只剩
        ``release()`` 或 20 分钟平台超时能收。

        改按 ``sandbox_id``(两个调用方都恰好只拥有这一行):腾 0141 槽位的
        效果完全一样——索引键在 ``state='IN_USE' AND destroyed_at IS NULL
        AND user_id IS NOT NULL`` 上,``mark_destroyed`` 两个都写;别人的行
        一根汗毛都不碰。热会话与临时沙箱因此收敛成同一条语句。

        与 C-1 不会互踩:这里是同进程快路径,成功了行就没了、C-1 无行可接
        管;这里失败(DB 抖)时行仍停在 ``IN_USE``+NULL,正好落进 C-1 的
        ``_STUCK_CREATE_TTL_S`` 兜底。
        """
        try:
            await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)
        except Exception:
            logger.error(
                "failed to clear sandbox row %s while unwinding a failed "
                "acquire (%s) — the row stays IN_USE with a NULL container_id "
                "until claim_warm's stuck-create TTL takes it over (or a "
                "force reap clears it)",
                sandbox_id,
                reason,
                exc_info=True,
            )

    async def _create(
        self,
        *,
        tenant_id: UUID,
        sandbox_id: UUID,
        user_id: UUID | None,
        egress: EgressContext | None,
    ) -> Any:
        """建一个新沙箱。

        ``envs`` 一次送两组:镜像自己声明、但 envd 派生进程拿不到的那套
        (:data:`SANDBOX_IMAGE_ENV`,全分支终审 Important-2),加上本次
        acquire 的 egress 变量。egress 放后面覆盖——今天两组键零重叠,
        顺序只是把"哪一组是特化"这件事写死,免得以后加键时靠运气。

        沙箱迁移波 2 —— :attr:`workspace_pv_name` 配了时,额外传
        ``metadata`` 挂载该用户(或临时沙箱的 scratch 子树,``user_id is
        None`` 时,spec 决策 9)的 NAS 工作区。``user_id`` 是新参数(brief
        原文:"``_create`` 签名加 ``user_id: UUID | None``");未配
        ``workspace_pv_name``(波 1 默认)完全不带 ``metadata`` 键,行为与
        波 1 逐字相同——不是传 ``metadata=None``,真的不出现在 kwargs 里,
        因为没有实测过真实 ``e2b`` SDK 对 ``metadata=None`` 的行为,不值得
        为一个从不需要的分支去赌。
        """
        kwargs: dict[str, Any] = {
            "template": self.template,
            "envs": {
                **SANDBOX_IMAGE_ENV,
                **self._egress_env(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress),
            },
            "timeout": _SANDBOX_TIMEOUT_S,
            "domain": self.domain,
            "api_key": self.api_key,
        }
        if self.workspace_pv_name:
            subpath = _workspace_subpath(
                prefix=self.workspace_subpath_prefix,
                tenant_id=tenant_id,
                user_id=user_id,
                sandbox_id=sandbox_id,
            )
            kwargs["metadata"] = {
                _CSI_VOLUME_CONFIG_METADATA_KEY: json.dumps(
                    [
                        {
                            "pvName": self.workspace_pv_name,
                            "mountPath": WORKSPACE_ROOT,
                            "subPath": subpath,
                        }
                    ]
                )
            }
        try:
            return await self._sdk().create(**kwargs)
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox create failed: {exc}") from exc

    async def release(self, *, sandbox_id: UUID) -> None:
        """常规拆除 —— 用户级热会话保温,临时沙箱真销毁。

        与本地 supervisor 的 ``release`` 逐条对齐
        (``sandbox_supervisor/supervisor.py:345-364``):带 ``user_id`` 的
        热会话**保活**(留给该用户的下一次 run,由空闲 TTL 清扫回收),
        没有 ``user_id`` 的沙箱**销毁**。

        全分支终审 Important-1:这里原本对两种情况都无条件 ``return
        None``。而 ``run_in_sandbox``(``sandbox.py:444-462``)是每次工具
        调用 acquire 一次、release 一次,``ctx.user_id`` 又是
        ``UUID | None`` —— 云后端下每一次无 user 的工具调用都会漏一个活的
        microVM,外加一行永远停在 ``IN_USE``/``destroyed_at IS NULL`` 的
        ``sandbox_instance``,没有任何东西会去销毁它。本地 supervisor 没有
        这个问题正是因为它这条分支走的是 ``destroy``。

        保温分支仍然不碰 store:热会话行保持 ``IN_USE``,``container_id``
        就是下次 connect 的凭据。
        """
        if await self._is_warm_session(sandbox_id):
            return None
        await self.destroy(sandbox_id=sandbox_id, reason=_RELEASE_DESTROY_REASON)
        await self._remove_scratch_dir(sandbox_id)
        return None

    async def _remove_scratch_dir(self, sandbox_id: UUID) -> None:
        """临时沙箱的 ``_scratch/<sandbox_id>`` 目录随沙箱一起收掉(全分支终审 M-3)。

        为什么需要:``run_in_sandbox``(``sandbox.py``)是**每次工具调用**
        acquire 一次、release 一次,不是每个 run 一次。没有 ``user_id`` 的 run
        因此每调用一次工具就在 NAS 上永久留一个空目录 —— spec 决策 9 把清理推
        给波 3 是有意的,但推的时候按"每沙箱一个"估的量,实际是"每工具调用一
        个",差一到两个数量级。NAS 上百万级空目录的代价不是容量而是 inode 与
        目录遍历:波 3 的归档/配额扫描 job 正是要遍历这棵树。

        为什么是 ``rmdir`` 而不是递归删:此刻是唯一一个"沙箱已经死了、这个目
        录再没有写手"的时刻,``rmdir`` 只在目录**确实是空的**时候成功;非空
        意味着 agent 真往里写了字节,那就留着(既不该被这条清理路径悄悄删
        掉,也没有其它机制会去读它——留下的是可诊断的痕迹,不是垃圾)。

        整段 best-effort:``release`` 是清理路径,NAS 抖一下不该把一次已经跑
        完的工具调用变成错误(同 :meth:`_is_warm_session` 的取舍)。
        ``workspace_root`` 未配(波 1 / 本地 docker 后端)整段跳过。

        为什么不改成 ``_scratch/<yyyymmdd>/<id>`` 分片:那只是把同样多的目录
        换个地方摆,总量不变,还得改 ``_workspace_subpath``(探针钉死的
        ``subPath`` 语义)并让两处拼接多一个必须同步的维度。
        """
        root = self.workspace_root
        if not root:
            return
        try:
            await asyncio.to_thread(_scratch_dir(root, sandbox_id).rmdir)
        except OSError:
            # 目录非空(agent 写了东西)/ 压根没建过 / NAS 抖动 —— 都不是错误。
            logger.debug("release: scratch dir for sandbox %s not removed", sandbox_id)

    async def _is_warm_session(self, sandbox_id: UUID) -> bool:
        """``store.is_warm_session`` 读不到时,按"保温"处置。

        ``release`` 是清理路径,不是取数路径:store 抖一下(连接池耗尽 /
        网络分区)不该把一次本来已经跑完的工具调用变成错误。两种误判里
        选代价小的那个——误当热会话最多是多留一个沙箱到空闲 TTL 到期被
        ``reap`` 收走(``reap`` 现在有周期任务在跑,见控制面 lifespan);
        误当临时沙箱则是把一个用户正在用的热会话直接 kill 掉。
        """
        try:
            return await self.store.is_warm_session(sandbox_id=sandbox_id)
        except Exception:
            logger.warning(
                "release: is_warm_session lookup failed for sandbox %s — "
                "keeping it warm (the idle reap sweep will reclaim it if it "
                "was in fact ephemeral)",
                sandbox_id,
                exc_info=True,
            )
            return True

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        """强制拆除 —— 真 kill 沙箱并让出热会话坑。

        「kill 失败但 ``mark_destroyed`` 已成功」留下的活沙箱,唯一兜底是
        平台 ``_SANDBOX_TIMEOUT_S``(20 分钟)超时:行终结后
        ``get_container_id``/``touch_and_get_container_id`` 都只读活行
        (两 store 同义),没有任何路径会再 connect 它续期。不做「二次
        destroy 重杀」—— 代码里没有会二次 destroy 同一 id 的调用方,为
        不可达路径保留 SQL↔memory 谓词分歧不值得(PR-A #5a)。
        """
        try:
            sbx = await self._attach(sandbox_id)
            await sbx.kill()
        except Exception:
            # 沙箱已不在(container_id 从未回填 / 超时被平台 kill / 被平台回收)——
            # 仍要往下清行,否则热会话坑永远占着,该 (tenant, user) 再也
            # acquire 不到。_attach 把"没有 container_id"和"connect 失败"
            # 都统一包成 SandboxSupervisorError,这里的 broad except 一样接
            # 得住,行为与重构前(container_id is None 时整段跳过 connect)
            # 等价。
            logger.info("destroy: sandbox %s already gone", sandbox_id)
        await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None, agent_key: str = ""
    ) -> SandboxOutcome:
        """波 1 Task 8(spec § 6.1 四契约点)。契约源头是
        ``infra/sandbox-image/runner.py:28-72``——本地 docker 沙箱里的
        PID 1,以下行号均指该文件。四个契约点,与它逐字对齐:

        1. timeout clamp ``[1, MAX_TIMEOUT_S]``,缺省 ``DEFAULT_TIMEOUT_S``
           (runner.py:45-51,同一个 ``max(1, min(x, MAX))`` 公式)。
        2. 输出截到 ``MAX_OUTPUT_CHARS``(runner.py:37/138-144)——这里用
           简单头部截断 ``[:MAX_OUTPUT_CHARS]``,不复刻 runner.py ``_cap``
           头尾各半 + 中间插省略标记那套人类可读展示格式:契约表只写了
           "输出上限 1_000_000 chars"这一个长度约束,不是字节级展示格式;
           E2B 侧的输出根本不经过 runner.py(没有本地那层子进程 JSON 协议
           转发),不存在"跟 runner.py 字节对字节相同"这个目标,只需要满足
           同一个长度上限。
        3. 超时 → ``exit_code=-1, timed_out=True``(runner.py:60-66)——已知
           偏差第三条(前两条见上/下文):这里 ``stdout``/``stderr`` 固定回
           空串,runner.py 是自己包了一层 ``subprocess.run``,子进程被杀前
           已经写出的部分输出仍读得到。E2B 这边追到 SDK 源码(而非公开
           文档)确认这条不可弥合:``AsyncCommandHandle.wait``
           (``e2b/sandbox_async/commands/command_handle.py``)超时时直接
           ``raise self._iteration_exception``,不像同一方法里非零退出码
           那支 ``raise CommandExitException(stdout=self._stdout,
           stderr=self._stderr, ...)`` 那样顺手把累积输出带上——超时异常
           对象本身根本不携带任何已产生的输出,没有可以在 ``except`` 里
           读出来的部分结果,只能记文档。
        4. 响应固定 4 键 —— 由 :class:`SandboxOutcome` 的字段集合结构性
           保证,下面每条返回路径都填满这 4 个字段。

        另有一条 runner.py 没有、AgentSandboxClient 特有的分支:E2B 的
        ``commands.run()`` 在子进程以非零退出码结束时抛
        ``e2b.exceptions.CommandExitException``,不像 runner.py 包的
        ``subprocess.run(..., check=False)`` 那样把 ``exit_code`` 原样放进
        返回值、从不因为非零退出码抛异常。不单独接住这个类型的话,LLM 代码
        里最常见的失败场景(未处理异常、断言失败、``sys.exit(1)``……)会全部
        落进下面兜底的 ``except Exception``,被误判成"沙箱基础设施故障"
        (``SandboxSupervisorError``,让整个 run 挂掉),而不是 runner.py
        契约要求的"正常返回一个非零 exit_code"。``CommandExitException``
        是个多继承 dataclass(同时继承 ``SandboxException`` 和
        ``CommandResult``),自带 ``stdout``/``stderr``/``exit_code`` 三个
        字段,直接用来构造 :class:`SandboxOutcome`,不需要重新跑一次命令。

        实测确认(2026-08-04,读 e2b==2.24.0 源码而非公开文档):
        ``commands.run(timeout=...)`` 超时抛的不是内置 ``TimeoutError``,是
        ``e2b.exceptions.TimeoutException``——envd RPC 层把 gRPC
        ``Code.deadline_exceeded``(超出这里传的 ``timeout=``)、
        ``Code.unavailable``(沙箱自身空闲超时被平台回收)、``Code.canceled``
        (超出 ``request_timeout``)统一映射到这一个类
        (``e2b/envd/rpc.py::_DEFAULT_RPC_ERROR_MAP``),不需要也没办法按根因
        细分,catch 这一个类型就覆盖了全部。

        code 先用 :meth:`_attach` 拿到的句柄 ``files.write`` 到沙箱内一个
        临时脚本文件,再 ``python -E -P <path>``(:data:`SANDBOX_PYTHON_FLAGS`)
        执行,不拼进命令行——E2B
        ``commands.run(cmd: str, ...)`` 内部固定走 ``/bin/bash -l -c cmd``
        (envd 的 ``ProcessConfig``,见 e2b SDK
        ``sandbox_async/commands/command.py`` 的 ``_start``),把任意 LLM
        生成的 ``code`` 直接嵌进这个 shell 字符串会被引号/特殊字符注入;
        runner.py 那边不存在这个风险,是因为它用
        ``subprocess.run([sys.executable, "-E", "-P", "-c", code], ...)`` 这种
        不经过 shell 的 argv 列表形式,没有以上问题。这是"一个已知偏差"
        (spec § 6.1),不是延续 runner.py 的写法——副作用是 ``-c`` 模式下
        ``__file__`` 不存在、文件模式下存在,测试钉住这条差异。

        全分支终审 Important-2:``commands.run`` 显式传 ``cwd=WORKSPACE_ROOT``。
        envd 派生的进程不继承镜像的 ``WORKDIR``,不传则落在 ``/home/agent``
        (2026-08-04 集群实测)—— ``bash`` 工具的 LLM 可见描述写着"Runs in
        /workspace",而 LLM 代码里的 ``open('out.csv','w')`` 这类相对路径写
        出的文件会落到 ``file_ops``(只认绝对 ``/workspace/...``)看不见的地
        方。配套的镜像环境变量见 :data:`SANDBOX_IMAGE_ENV`。

        独立审查 Important-2:``_attach(sandbox_id, touch_last_used=True)``
        在拿句柄的同一次往返里把这一行的 ``last_used_at`` 推进到当前
        时间——``reap`` 的空闲 TTL 清扫(``force=False``)按这一列判断
        "多久没被真正用过",不推进的话空闲判定实际退化成"多久以前
        acquire 的",一个持续被 ``exec`` 的热会话会被误杀。见
        :meth:`_attach` 与
        :meth:`SandboxInstanceStore.touch_and_get_container_id` 的
        docstring。

        sandbox migration wave 2(spec 决策 10):``agent_key`` 非空时随
        ``commands.run(envs=...)`` 注入 ``PYTHONUSERBASE``(:func:`agent_key_envs`
        单源计算,与本地 supervisor 后端同一份值 —— 契约测试钉住)。egress
        token 走 ``create(envs=...)``(§ 模块 docstring "Important-3")只送一
        次;这里不同 —— SDK 的 ``commands.run`` 本就支持逐次传 envs,同用户
        双 agent 共享一个已建好的热会话时,才需要"每次 exec 都能换一个不同
        的 agent_key"。

        ``umask 000 && `` 前缀(Task 4 审查 Critical 后续,原为跨 uid 写冲突
        而加):``commands.run`` 走 ``/bin/bash -l -c cmd``(见上方
        docstring),所以能在同一个 shell 里先设 umask 再 exec python——这个
        bash 进程与它派生的 python 子进程(以及 python 里 LLM 代码自己
        ``os.mkdir``/``open`` 出的每一个文件)全都继承它。不设的话,agent
        代码在 ``/workspace`` 下自己建的目录/文件用沙箱默认 umask(常见
        ``0o022``)掩过,变成 ``0o755``/``0o644``。

        **方向变更之后(共享 gid → 统一 uid,见
        ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
        § 六)这条前缀原本的理由已经不成立**——control-plane 现在与沙箱 agent 是同一个 uid
        (10000),属主位本身就够两侧读写/删除,不再需要靠 world-writable 的
        group/other 位兜底跨 uid 访问。留着它是**安全的超集**
        (``0o777``/``0o666`` 严格宽于统一 uid 之后真正需要的
        ``0o700``/``0o600``,不新增任何可见性),不是错,只是不再最小。收
        紧它需要一次真栈验证(exec 产出的文件在统一 uid 之后确实还能被两侧
        正常读写删除)——本次方向变更任务没有预算做这一步,留作后续任务,
        这里刻意不动。跟本地 docker 后端(``runner.py`` 的 ``main()``,同一处
        ``os.umask(0)``)同款的约束不变:两后端仍必须同款,契约测试
        (``test_sandbox_runtime_contract.py``)钉住,不是各修各的。
        """
        effective = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        effective = max(1, min(effective, MAX_TIMEOUT_S))

        sbx = await self._attach(sandbox_id, touch_last_used=True)
        timeout_exc, exit_exc = self._sdk_exceptions()

        script = f"/tmp/ew-exec-{uuid4().hex}.py"  # noqa: S108 — sandbox container tmpfs, not host; name has 128 bits of random entropy
        started = _monotonic()
        envs = agent_key_envs(agent_key)
        try:
            await sbx.files.write(script, code, user=SANDBOX_EXEC_USER)
            result = await sbx.commands.run(
                f"umask 000 && python {' '.join(SANDBOX_PYTHON_FLAGS)} {script}",
                user=SANDBOX_EXEC_USER,
                timeout=effective,
                cwd=WORKSPACE_ROOT,
                envs=envs or None,
            )
        except timeout_exc:
            return SandboxOutcome(stdout="", stderr="", exit_code=-1, timed_out=True)
        except exit_exc as exc:
            return SandboxOutcome(
                stdout=exc.stdout[:MAX_OUTPUT_CHARS],
                stderr=exc.stderr[:MAX_OUTPUT_CHARS],
                exit_code=exc.exit_code,
                timed_out=False,
            )
        except Exception as exc:
            # 上面那支 ``except timeout_exc`` 接不住全部超时:envd 掐断一条跑
            # 满时限的命令时,gRPC 流中断会经 anyio → httpx ``map_exceptions``
            # 变成 ``httpcore.ReadError`` / ``ReadTimeout`` 冒泡上来,而不是
            # ``e2b.exceptions.TimeoutException``。2026-08-06 在真栈契约测试
            # 上两次实测到:``test_exec_default_timeout_is_the_shared_default``
            # (``time.sleep(45)`` + 30 秒时限)红在这一支,报错正文是空的
            # ——``httpcore.ReadError`` 的 ``str()`` 就是空字符串。
            #
            # 生产后果不是"测试偶尔红":一次本该返回 ``timed_out=True`` 的
            # 正常超时会变成 ``SandboxSupervisorError``,把整个 run 挂掉。
            # 所以这里按**跑满了我们自己设的时限**归因,而不是按异常类型
            # ——见 :data:`_TIMEOUT_ATTRIBUTION_RATIO`。
            elapsed = _monotonic() - started
            if elapsed >= effective * _TIMEOUT_ATTRIBUTION_RATIO:
                return SandboxOutcome(stdout="", stderr="", exit_code=-1, timed_out=True)
            # 没跑满时限就断,是真的基础设施故障。带上类型名:走到这里的异常
            # 有一整族 ``str()`` 为空,只插值 ``{exc}`` 会得到
            # ``sandbox exec failed: ``,恰恰在最需要诊断的时候什么都不说。
            raise SandboxSupervisorError(
                f"sandbox exec failed after {elapsed:.1f}s "
                f"(timeout={effective}s): {type(exc).__name__}: {exc}"
            ) from exc
        return SandboxOutcome(
            stdout=result.stdout[:MAX_OUTPUT_CHARS],
            stderr=result.stderr[:MAX_OUTPUT_CHARS],
            exit_code=result.exit_code,
            timed_out=False,
        )

    async def reap(self, *, force: bool) -> int:
        """扫掉空闲(或 ``force`` 时全部)的热会话,返回拆除数。

        以 ``sandbox_instance`` 表(:meth:`SandboxInstanceStore.list_active`)
        为准,不问 E2B SDK 账号级的 ``list()``——同一个 E2B 账号下可能有
        别的来源建的沙箱(别的环境 / 别人手工建的 / 以后别的服务),按 SDK
        列表拆会误伤不属于我们的沙箱。``force=True`` 拆掉表里记着的每一个
        活跃沙箱,不看空闲时间——运维强制拆除与 M0→M1 Gate E2E 都依赖这个
        确定性语义;``force=False`` 走 :meth:`SandboxInstanceStore.list_active`
        的空闲 TTL 过滤,口径与本地 supervisor 自己的 reaper 相同(该
        Protocol 方法的 docstring 有完整说明;:meth:`exec` 的
        ``touch_last_used=True`` 保证 ``last_used_at`` 会被推进,空闲判定
        不会退化成"多久以前 acquire 的"——独立审查 Important-2)。

        沙箱在平台侧已经不存在时(超时被平台 kill / 被平台回收 / 手工删了)——
        ``connect``/``kill`` 会失败,但仍要清掉那一行,否则热会话槽位永远
        占着,该 ``(tenant, user)`` 被迁移 0141 的部分唯一索引挡住、再也
        acquire 不到——与 :meth:`destroy` 的 broad ``except`` 是同一类
        故障、同一个修法:``mark_destroyed`` 必须在 ``except`` 之外,对
        ``list_active`` 交回来的每一行都无条件跑一次,不能因为
        ``connect``/``kill`` 失败就跳过。

        独立审查 Important-1:``force=True`` 时额外调用
        :meth:`SandboxInstanceStore.list_stuck_creating`,清掉编排进程死于
        "``claim_warm`` 提交行"和"``set_container_id`` 回填"两步之间留下的
        孤儿(pod OOM-kill / 驱逐 / 滚动更新)——``list_active`` 两种模式都
        故意排除这类行(见其 docstring),不这样做的话"``force=True`` 拆掉
        每一个活跃会话"这句 brief 原文承诺的确定性语义就不成立。这类行没有
        对应的 E2B 容器,不尝试 ``connect``/``kill``,直接
        ``mark_destroyed``。``force=False`` 不碰这类行——常规空闲清扫不该
        跟一个正在合法冷启窗口内的 ``acquire()`` 抢同一行,见
        :meth:`list_stuck_creating` 的 docstring。
        """
        rows = await self.store.list_active(only_idle=not force)
        reaped = 0
        for sandbox_id, container_id in rows:
            try:
                sbx = await self._connect(container_id)
                await sbx.kill()
            except Exception:
                # 沙箱已不在(超时被平台 kill / 被平台回收 / 手工删了)也要清行,
                # 否则热会话坑永远占着,该 (tenant, user) 再也 acquire 不到
                # ——与 destroy() 的 broad except 同理,见上面 docstring。
                logger.info("reap: sandbox %s already gone", container_id)
            reaped += await self._clear_reaped_row(sandbox_id, reason="reap")
        if force:
            for sandbox_id in await self.store.list_stuck_creating():
                # 没有对应的 E2B 容器——connect/kill 无从谈起,直接清行。
                # 见上面 docstring "独立审查 Important-1"。
                logger.info(
                    "reap: sandbox %s stuck mid-create (orphaned by a process "
                    "death between claim_warm and set_container_id), clearing "
                    "with no container to kill",
                    sandbox_id,
                )
                reaped += await self._clear_reaped_row(sandbox_id, reason="reap_orphaned_create")
        return reaped

    async def _clear_reaped_row(self, sandbox_id: UUID, *, reason: str) -> int:
        """清一行并计数;这一行失败只记日志回 ``0``,让清扫继续往下走。

        全分支终审:两个循环原本都把 ``mark_destroyed`` 摆在 per-row ``try``
        之外,一行出问题(被并发清了 / DB 抖 / 约束冲突)整趟清扫就地中止,
        这一轮后面该回收的全漏。本地 supervisor 的 reaper 正是为此逐行包
        (``sandbox_supervisor/reaper.py``),这里照抄那个形状;而且 ``reap``
        现在是云后端唯一的回收机制、也是 C-1 卡死行的人工恢复入口。
        """
        try:
            await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)
        except Exception:
            logger.warning(
                "reap: failed to clear row %s (%s) — sweep continues with the "
                "remaining rows; this one is left for the next sweep",
                sandbox_id,
                reason,
                exc_info=True,
            )
            return 0
        return 1
