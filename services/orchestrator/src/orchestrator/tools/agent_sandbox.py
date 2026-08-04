"""``AgentSandboxClient`` —— ACS Agent Sandbox 上的 :class:`SandboxRuntime`.

E2B SDK 打底,私有协议(见下)。与 ``HTTPSupervisorRuntime`` 的分工:那个打
本地 docker supervisor(开发/CI),这个打云上平台。两者由 ``build_sandbox_runtime``
按 ``sandbox_backend`` 选,上层工具零感知。

本模块只做 波1 Task 7:``acquire`` / ``release`` / ``destroy`` + 热会话 CAS。
``exec``(Task 8)/``reap``(Task 9)留给后续任务;:class:`SandboxInstanceStore`
Protocol 已预留它们要用的方法位置(见其 docstring)。

热会话(spec § 6.2):``(tenant, user)`` 的活跃沙箱记在 ``sandbox_instance``
表,E2B sandbox id 存在既有的 ``container_id`` 列(同一语义:外部运行时给
的实例标识)。并发 acquire 靠迁移 0141 的部分唯一索引定单赢家。

唤醒失败(spec § 6.3):``connect`` 会因库存不足/欠费/保留期已过被平台删
而失败 —— 丢弃该行、重新 ``create``。工作区权威在外部存储(波 2 起
NAS;波 1 本就还没挂持久工作区),重建无损。

## 私有协议 + ``patch_e2b`` 导入顺序(2026-08-04 探针实测报告 § 一)

我们的证书只覆盖一层子域名(``*.deepaihealth.com``),E2B 原生协议要求
泛域名(``<port>-<sandbox_id>.<domain>``,两层)。改走阿里云的私有协议
(``<domain>/kruise/<sandbox_id>/<port>``,单层)需要装 ``kruise-agents``
扩展包并在 **导入 ``e2b`` 之前** 调用一次 ``patch_e2b(https=False)``——
``https=False`` 是必须的(默认 ``True``),ALB 只监听 HTTP 80,不传会拿
ALB 的 503。

这个"必须先 patch 后 import"的顺序要求,本模块用「二选一都不做」——不放
在模块顶层无条件执行,也不依赖"调用方凑巧先 import 对了顺序"——而是把
patch 挪进 :meth:`AgentSandboxClient._sdk` 唯一的真实 SDK 惰性导入点,用一个
进程级幂等 guard 包一层(见 :func:`_ensure_e2b_patched`)。理由:

1. **不能放模块顶层。** ``orchestrator.tools`` 的 ``__init__.py`` 习惯性把
   每个 tool 模块的公开类型都重新导出一遍(参见该文件里 ``HTTPSupervisorRuntime``
   / ``SandboxRuntime`` 等的写法)。如果本模块一被导入就跑
   ``patch_e2b()``,那么任何只想用本地 docker supervisor(``sandbox_backend
   == "supervisor"``,今天的默认值)、根本不碰 Agent Sandbox 的进程,只要
   ``import orchestrator.tools`` 就会被迫装 ``kruise_agents`` + 对全局
   ``e2b`` 模块做 monkeypatch —— 这既不必要,又把一个未使用的 GitHub 依赖
   变成了硬性导入失败点,还可能悄悄影响将来某个"以为在用原生 E2B 语义"的
   代码路径(即便今天仓库里没有这种路径,这类隐藏的进程级副作用也是那类
   "过后很久才炸、极难定位"的坑)。
2. **不能放 ``control_plane.runtime.build_sandbox_runtime`` 里。** 那会把
   "SDK 内部导入顺序有硬约束"这条本该是 ``agent_sandbox.py`` 自己管好的
   内部细节,泄漏给调用方。且不是只有 ``build_sandbox_runtime`` 会构造
   :class:`AgentSandboxClient`——Task 10 的契约测试、Task 11 的端到端脚本
   多半会直接构造它。把 patch 的触发点分散到"每一个构造/使用它的地方都要
   记得先 patch"是明摆着的陷阱;集中到 :class:`AgentSandboxClient` 自己唯一
   的 SDK 入口,任何构造路径都自动安全。
3. **懒加载还有一个附带的好处**:构造 :class:`AgentSandboxClient` 本身(纯
   dataclass 字段赋值)不触发任何导入;真正需要 SDK 时(``sdk`` 字段为
   ``None``,即非测试场景)才第一次触发,且只触发一次(进程级
   ``_e2b_patched`` 布尔 guard)。单测永远注入假件(``sdk=FakeSdk(...)``),
   所以整套单测套件里这个全局 monkeypatch 从未真正执行 —— 不会有"构造一个
   测试替身对象,副作用是偷偷改了全进程 ``e2b`` 模块"这种测试间相互污染的
   隐患。
4. **幂等、无需加锁。** ``_ensure_e2b_patched`` 全程是同步代码、不 await
   任何东西,所以哪怕两个协程"同时"调用 ``_sdk()``,也不会有交错执行的
   窗口(asyncio 单线程协作式调度,同步函数体一次片内跑完);guard 只是
   避免重复 import + 重复 patch 的浪费,不是为了防真并发。

``_ensure_e2b_patched`` 除了打补丁,还负责把 ``self.domain``/``self.api_key``
写进裸环境变量 ``E2B_DOMAIN``/``E2B_API_KEY``——``kruise_agents.patch_e2b()``
自己的第一行就无条件读 ``os.environ['E2B_DOMAIN']``(没有就 ``KeyError``),
这不是 kwarg 能绕开的,详细证据与理由见该函数自己的 docstring。

代价说明白:这不是"进程启动时就能发现 kruise_agents 没装"的 fail-fast 设计
——配置了 ``sandbox_backend="agent_sandbox"`` 但依赖缺失,会在**第一次真正
acquire** 时才报错,而不是进程启动时。权衡过后选择了这个方向,因为反过来
(在 ``__post_init__`` 里就 patch)会让单测也担这个全局副作用的风险,得不
偿失;真实部署里 Task 11 的端到端验收本就会在上线前先跑一次真 acquire。

## 其余两处对 task-7-brief.md 的修正(探针报告已点名)

* ``AsyncSandbox.create()`` 的 ``domain`` / ``api_key`` 走 ``**opts``,不是
  brief 草稿写的具名参数——这里按实测签名以关键字形式传,效果一样。
* ``commands.run`` / ``files.write`` 必须传 ``user="agent"``(见
  :data:`SANDBOX_EXEC_USER`),否则 E2B 默认用户 ``user`` 在我们的沙箱镜像
  (``USER agent``,uid 10000)里不存在,炸
  ``AuthenticationException: invalid username: 'user'``。

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
  改成先 ``drop_warm`` 释放槽位再把异常抛出去——否则那个 ``(tenant, user)``
  会被迁移 0141 的部分唯一索引永久卡住,不会有任何东西再去把它解开。
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from expert_work.common.egress_token import mint_egress_token
from orchestrator.tools.sandbox import EgressContext, SandboxOutcome, SandboxSupervisorError

logger = logging.getLogger(__name__)

#: 沙箱内工作区挂载点 —— 与 supervisor 实现一致。
WORKSPACE_ROOT = "/workspace"

#: 沙箱镜像是 ``USER agent``(uid 10000,``nologin``)——E2B SDK 默认以用户
#: ``user`` 执行 ``commands.run`` / ``files.write``,那个账号在我们的镜像里
#: 不存在(``AuthenticationException: invalid username: 'user'``,2026-08-04
#: 探针报告实测);``user="root"`` 同样不行(``InvalidArgumentException``)。
#: 做成常量而非散落字面量 —— Task 8 的 ``exec`` 也要用同一个值。
SANDBOX_EXEC_USER = "agent"

#: 契约常量 —— 与 infra/sandbox-image/runner.py:28-37 逐字对齐(``exec``,
#: Task 8)。runner.py 是本地 docker 沙箱里的 PID 1,这三个值是它的
#: ``DEFAULT_TIMEOUT_S`` / ``MAX_TIMEOUT_S`` / ``MAX_OUTPUT_CHARS``;云沙箱
#: 与本地 supervisor 对同一次 exec 请求要给出等价的 clamp/truncate 行为。
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 1_000_000

#: 进程级 guard —— ``_ensure_e2b_patched`` 幂等的关键,见模块 docstring
#: "私有协议 + patch_e2b 导入顺序"一节。
_e2b_patched = False


def _ensure_e2b_patched(*, domain: str, api_key: str) -> None:
    """在首次真正需要 ``e2b`` SDK 时,惰性、幂等地打私有协议补丁。

    必须先跑这个再 ``import e2b``——``kruise_agents.patch_e2b`` 改写的是
    ``e2b`` 内部构造网关 URL 的逻辑,补丁生效前已经拿到的旧引用不会被追溯
    修正。``https=False`` 是必须显式传的(签名默认 ``True``):ALB 只监听
    HTTP 80,默认值会打到 ALB 的 503(探针报告 § 一)。

    **审查发现(task-7-report.md 修复记录)**:``kruise_agents.patch_e2b()``
    第一行就是 ``os.environ["E2B_API_URL"] = f"...{os.environ['E2B_DOMAIN']}..."``
    ——无条件读裸环境变量 ``E2B_DOMAIN``(直接 ``[]`` 取值,没有 ``.get()``
    兜底),没设就 ``KeyError``。仓内没有任何地方设过这个裸变量(我们自己的
    配置走 ``EXPERT_WORK_`` 前缀的 ``Settings.sandbox_e2b_domain``,是完全
    不同的名字/通道)——所以第一次真正调用 ``patch_e2b()`` 必炸。这里用
    ``setdefault`` 在调用前把 domain/api_key 写进裸环境变量:``setdefault``
    而非直接赋值,是为了让运维如果真的自己设了同名环境变量时那个值优先,
    这里只是提供一个不炸的兜底,不是要覆盖别人的显式配置。``E2B_API_KEY``
    同理防御性地写一份——``patch_e2b()`` 本体不读它,但 ``validate_key=True``
    (这里的默认值,没有传 ``False``)时 e2b SDK 自己的 key 校验逻辑不排除
    某些内部路径会退回读这个环境变量而不是每次都吃调用方传的显式 kwarg。

    调用点收敛到 :meth:`AgentSandboxClient._sdk` 一处 —— 完整理由见模块
    docstring;不要在别处直接 ``import e2b``。

    二审发现(task-7-report.md "二审修复"一节):``setdefault`` 保持"运维
    显式设置优先"这条语义没错,但如果预设值真的跟 ``self.domain`` 不一致,
    后果比"连不上"更难查——``patch_e2b()`` 用(此刻的)``E2B_DOMAIN`` **一次
    性**算好 ``E2B_API_URL`` 并写死,之后不会再读;而 ``_create``/``_connect``
    每次调用仍然把**正确的** ``self.domain`` 当 kwarg 传给 SDK。两个值一旦
    不一致,报出来的是认证/网络层面让人摸不着头脑的错,不是"域名连不上"
    这种一眼能看懂的信号。这里把这种静默的同/异歧义变成可见的一条
    warning 日志,``setdefault`` 的行为本身不变(运维的显式值仍然生效)。
    """
    global _e2b_patched
    if _e2b_patched:
        return
    existing_domain = os.environ.get("E2B_DOMAIN")
    if existing_domain is not None and existing_domain != domain:
        logger.warning(
            "E2B_DOMAIN is already set to %r (different from this "
            "AgentSandboxClient's configured domain %r) — patch_e2b() bakes "
            "the pre-existing env value into E2B_API_URL once and never "
            "re-reads it, while create()/connect() keep passing %r explicitly "
            "per call; a mismatch here surfaces as a confusing auth/network "
            "error, not a clear connection failure",
            existing_domain,
            domain,
            domain,
        )
    os.environ.setdefault("E2B_DOMAIN", domain)
    os.environ.setdefault("E2B_API_KEY", api_key)
    from kruise_agents.patch_e2b import patch_e2b

    patch_e2b(https=False)
    _e2b_patched = True


class SandboxInstanceStore(Protocol):
    """``sandbox_instance`` 表上 ``AgentSandboxClient`` 需要的操作。

    ``exec``(Task 8)最终没再加新方法——四契约点靠既有的
    :meth:`get_container_id`/``_attach`` 就够。``reap``(Task 9)加了
    :meth:`list_active`。

    独立审查(Important-1/2,task-9-report.md 有完整记录)追加两个方法:
    :meth:`touch_and_get_container_id`(``exec`` 用,读 container_id 的同时
    推进 ``last_used_at``,空闲判定不再退化成"多久以前 acquire 的")、
    :meth:`list_stuck_creating`(``reap(force=True)`` 用,清掉编排进程死于
    ``claim_warm``/``set_container_id`` 两次写之间留下的孤儿——
    :meth:`list_active` 两种模式都故意不返回这类行,见其 docstring)。
    """

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        """占 ``(tenant, user)`` 的热会话坑(spec § 6.2 CAS)。

        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` 的封装:

        * 占到 → 返回 ``None``,调用方负责建沙箱并回填 :meth:`set_container_id`。
        * 没占到、赢家已就绪(``container_id`` 非空)→ 返回
          ``(赢家那一行的 sandbox_id, container_id)`` 二元组 —— **不是**只
          返回 container_id。调用方(``acquire``)本次调用开头自己铸的
          ``uuid4()`` 从未插入任何行;如果 ``acquire`` 复用热会话成功后仍
          返回那个自铸 id,后续任何 ``destroy(sandbox_id=<那个 id>)`` 都会
          静默 no-op(``get_container_id`` 查不到、``mark_destroyed`` 的
          ``WHERE id=...`` 影响 0 行,两处都不报错)——沙箱杀不掉,热会话槽
          位也放不出来。返回赢家的真实行 id 让 ``acquire`` 能直接复用它,
          不需要再多打一次库去查。
        * 没占到、赢家还在创建中(``container_id`` 仍是 NULL)→ 允许实现
          raise(SQL 实现如此)。E2B 冷启实测 35-40s(见探针报告),这不是
          罕见边界窗口,调用方必须能收到一个明确错误,而不是悄悄再建一个
          沙箱(破坏"同时只有一个热沙箱"的不变式)或者在后续
          :meth:`set_container_id` 时对着一个从未插入的行操作。
        """

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        """回填 E2B sandbox id。只应在调用方本次自己创建/重建了沙箱时调用
        ——connect 到一个早已就绪的热会话时,该行的 container_id 已经是对的,
        重复回填对 SQL 实现是浪费,对某些手写假件实现甚至可能因为该
        ``sandbox_id`` 从未被插入而出错。"""

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        """标记销毁并让出热会话坑。"""

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        """丢弃一个失效的热会话行(唤醒失败后重建前调)。"""

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """读某行的 E2B sandbox id;行不存在或未回填返 ``None``。"""

    async def touch_and_get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """独立审查 Important-2:跟 :meth:`get_container_id` 一样的读语义
        (行不存在或未回填返 ``None``),但同一次往返里把该行的
        ``last_used_at`` 一并推进到当前时间——:meth:`AgentSandboxClient.exec`
        用,替代"先读 container_id 再单独一次 UPDATE last_used_at"两次
        往返(``exec`` 已经在热路径上,不该多打一次库)。``destroy`` 之类
        只需要读的调用方仍然用 :meth:`get_container_id`。
        """

    async def list_active(self, *, only_idle: bool) -> list[tuple[UUID, str]]:
        """列出活跃(``state='IN_USE'``、未销毁、``container_id`` 已回填)的
        热会话行,返回 ``(sandbox_id, container_id)``——``reap`` 用。

        ``only_idle=True``(常规清扫,``force=False``)只返回超过空闲 TTL
        的行——判定口径与本地 docker-supervisor 自己的 reaper 相同(见
        ``sandbox_supervisor/store.py`` 的 ``_session_idle``):以
        ``last_used_at`` 为准(``exec`` 每次成功 attach 都会推进,见
        :meth:`touch_and_get_container_id`),缺失(还没 exec 过)退回
        ``acquired_at``;两者都缺失的行不返回。``only_idle=False``
        (``force`` 路径)返回全部活跃行,不看时间戳——运维强制拆除与
        M0→M1 Gate E2E 依赖这个确定性语义。

        还在创建中(``container_id`` 仍是 NULL)的行两种模式都不返回:E2B
        冷启 35-40s 的窗口里可能有另一路 ``acquire()`` 正在往这行写
        ``container_id``,此时 ``reap`` 抢着连一个还没写完的行既连不上、
        也没什么可 kill 的;那类行**正常**的清理由 ``_create_and_track``
        自己的失败分支(``drop_warm``)负责,不是 ``list_active``/``reap``
        常规路径的职责。

        独立审查 Important-1 追加:上一句的"正常"两个字是关键——
        ``_create_and_track`` 的 ``except`` 只在 ``_create()`` **在同一个
        协程里同步抛异常**时才跑得到。如果编排进程是在 ``claim_warm``
        提交行之后、``set_container_id`` 完成之前**被杀掉**的(pod
        OOM-kill / 驱逐 / 滚动更新——按本系统的多副本生产设计,这些都是
        预期会发生的事件,不是理论边界),没有任何异常处理器会运行,那行
        永久停在 ``IN_USE`` + ``container_id=NULL``,``list_active`` 两种
        模式都会永远跳过它——这正是 Task 7 修过的"孤儿 CAS 行卡死
        ``(tenant, user)``"同一类故障,只是走了 Task 7 覆盖不到的"进程死于
        两次写之间"路径。全仓没有任何周期性清道夫会碰这类行(只有
        ``user_purge.py`` 的显式删用户路径会捎带清掉)。这类真正的孤儿由
        :meth:`list_stuck_creating` 负责,只在 ``force=True`` 时由 ``reap``
        调用——见该方法的 docstring。
        """

    async def list_stuck_creating(self) -> list[UUID]:
        """独立审查 Important-1:列出卡在"创建中"、且已经远超合法冷启窗口
        的 ``IN_USE`` 行——编排进程在 ``claim_warm`` 提交行之后、
        ``set_container_id`` 回填之前死掉(pod OOM-kill / 驱逐 / 滚动更新)
        留下的孤儿,不是正常还在 35-40s 冷启窗口内、随时可能被
        ``set_container_id`` 补完的行。判定阈值是
        :data:`~expert_work.persistence.sandbox_instance_store._STUCK_CREATE_TTL_S`
        (数分钟量级,留足余量不会跟合法冷启窗口混淆)。

        只应该在 ``force=True`` 的 ``reap`` 里调用——常规的 ``only_idle=True``
        空闲清扫不该碰"看起来还在创建"的行,那会跟一个正在合法冷启中的
        ``acquire()`` 抢同一行。返回值只有 ``sandbox_id``,没有
        ``container_id``(这类行压根没有对应的 E2B 容器)——调用方不该对
        返回的 id 尝试 ``connect``/``kill``,直接 ``mark_destroyed`` 即可。
        """


@dataclass
class AgentSandboxClient:
    """:class:`~orchestrator.tools.sandbox.SandboxRuntime` 的 Agent Sandbox 实现。"""

    domain: str
    api_key: str
    template: str
    store: SandboxInstanceStore
    egress_token_secret: str
    egress_proxy_host: str
    egress_proxy_port: int
    #: 测试缝 —— 注入 SDK 替身。``None`` 时用真实 ``e2b.AsyncSandbox``。
    #: ``e2b`` 没有 py.typed 标记(见 pyproject.toml 的 mypy override 说明),
    #: 手写测试假件也不共享一个正式 Protocol —— 两边都用 ``Any``,不是疏漏。
    sdk: Any | None = None
    egress_token_ttl_s: int = 3600

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
        的"唤醒失败则重建" vs ``destroy`` 的"已经不在也无妨,继续清行")。"""
        return await self._sdk().connect(container_id, domain=self.domain, api_key=self.api_key)

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
        existing: tuple[UUID, str] | None = None
        if user_id is not None:
            existing = await self._claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )

        just_created = False
        if existing is not None:
            winner_id, winner_container_id = existing
            try:
                sbx = await self._connect(winner_container_id)
            except Exception:
                # spec § 6.3 —— 唤醒失败(库存不足/欠费/保留期已过被平台删)
                # 必须能重建,不能把 run 打死。
                logger.warning("warm sandbox connect failed, rebuilding", exc_info=True)
                if user_id is not None:
                    await self.store.drop_warm(tenant_id=tenant_id, user_id=user_id)
                    # 重新占坑,让本次 acquire 的 sandbox_id 拥有一行 ——
                    # 否则下面的 set_container_id 无行可回填。槽位刚被让出,
                    # 正常情况下这次必赢;真撞上第三方竞争者也只是这次
                    # acquire 的返回值不完全精确热复用,不影响正确性上限
                    # (与 brief 原始草稿同等严谨程度,未过度设计)。
                    await self._claim_warm(
                        tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                    )
                sbx = await self._create_and_track(
                    tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id, egress=egress
                )
                just_created = True
            else:
                # 复用成功 —— 返回赢家那一行**真实存在**的 sandbox_id,不是
                # 本次调用开头自铸、从未插入任何行的 uuid4()(审查 Important-3:
                # 否则后续 destroy(sandbox_id=<自铸 id>) 会静默 no-op,见
                # SandboxInstanceStore.claim_warm 的 docstring)。
                sandbox_id = winner_id
        else:
            sbx = await self._create_and_track(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id, egress=egress
            )
            just_created = True

        for relpath, data in seed_files:
            await sbx.files.write(f"{WORKSPACE_ROOT}/{relpath}", data, user=SANDBOX_EXEC_USER)

        if just_created:
            # 连到既有热会话时该行的 container_id 已经是对的(existing 正是
            # 从那里读出来的)—— 只有本次自己建/重建了沙箱才需要回填。
            await self.store.set_container_id(sandbox_id=sandbox_id, container_id=sbx.sandbox_id)
        return sandbox_id

    async def _claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        """``store.claim_warm`` 套上 § 6.5 的统一错误契约。"""
        try:
            return await self.store.claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox warm-session claim failed: {exc}") from exc

    async def _create_and_track(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID | None,
        sandbox_id: UUID,
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
        东西会再去把这行解开,该用户的热会话彻底卡死,只能手工改库。
        ``drop_warm`` 把槽位放出来,让下一次 acquire 能重新正常竞争。

        二审发现(task-7-report.md "二审修复"一节):``drop_warm`` 自己也
        可能失败(DB 抖动/连接池耗尽/网络分区)。如果不特殊处理,外层
        ``raise`` 会被 ``drop_warm`` 的新异常盖过 —— 调用方按
        ``SandboxSupervisorError`` 设计的 except 链接不住原始错误(类型变
        了),而且槽位依然没清理成功,与本方法开头描述的卡死症状原样重演,
        只是往下多埋一层。用嵌套 try/except 把 ``drop_warm`` 的失败单独
        捕获+记日志(标明需要人工介入),但外层永远重新抛出 ``_create()``
        的原始异常 —— 内层的 except 块正常结束(没有再 raise)后,外层
        bare ``raise`` 重新抛出的是外层 except 语句本来在处理的那个异常,
        不是内层被捕获又丢弃的那个(标准 Python 异常处理语义,已用最小
        复现脚本独立验证过,见报告)。
        """
        try:
            return await self._create(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress)
        except Exception:
            if user_id is not None:
                try:
                    await self.store.drop_warm(tenant_id=tenant_id, user_id=user_id)
                except Exception:
                    logger.error(
                        "drop_warm failed while unwinding a create() failure — "
                        "tenant=%s user=%s stays wedged behind the 0141 warm-slot "
                        "index; needs manual cleanup",
                        tenant_id,
                        user_id,
                        exc_info=True,
                    )
            raise

    async def _create(
        self, *, tenant_id: UUID, sandbox_id: UUID, egress: EgressContext | None
    ) -> Any:
        try:
            return await self._sdk().create(
                template=self.template,
                envs=self._egress_env(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress),
                domain=self.domain,
                api_key=self.api_key,
            )
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox create failed: {exc}") from exc

    async def release(self, *, sandbox_id: UUID) -> None:
        """常规拆除 —— 让平台按休眠保留期回收,不主动 kill。

        与 supervisor 实现的差异:那边 ``release`` 是 ``docker rm``;这里
        沙箱进入平台的休眠流程(内存态保留,下次 acquire 唤醒 1-10s)。
        热会话行保留(``state`` 仍是 ``IN_USE``),``container_id`` 就是
        下次 connect 的凭据 —— 不碰 store,是刻意的空实现。
        """
        del sandbox_id
        return None

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        """强制拆除 —— 真 kill 沙箱并让出热会话坑。"""
        try:
            sbx = await self._attach(sandbox_id)
            await sbx.kill()
        except Exception:
            # 沙箱已不在(container_id 从未回填 / 保留期过 / 被平台回收)——
            # 仍要往下清行,否则热会话坑永远占着,该 (tenant, user) 再也
            # acquire 不到。_attach 把"没有 container_id"和"connect 失败"
            # 都统一包成 SandboxSupervisorError,这里的 broad except 一样接
            # 得住,行为与重构前(container_id is None 时整段跳过 connect)
            # 等价。
            logger.info("destroy: sandbox %s already gone", sandbox_id)
        await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)

    async def exec(self, *, sandbox_id: UUID, code: str, timeout_s: int | None) -> SandboxOutcome:
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
        临时脚本文件,再 ``python -I <path>`` 执行,不拼进命令行——E2B
        ``commands.run(cmd: str, ...)`` 内部固定走 ``/bin/bash -l -c cmd``
        (envd 的 ``ProcessConfig``,见 e2b SDK
        ``sandbox_async/commands/command.py`` 的 ``_start``),把任意 LLM
        生成的 ``code`` 直接嵌进这个 shell 字符串会被引号/特殊字符注入;
        runner.py 那边不存在这个风险,是因为它用
        ``subprocess.run([sys.executable, "-I", "-c", code], ...)`` 这种
        不经过 shell 的 argv 列表形式,没有以上问题。这是"一个已知偏差"
        (spec § 6.1),不是延续 runner.py 的写法——副作用是 ``-c`` 模式下
        ``__file__`` 不存在、文件模式下存在,测试钉住这条差异。

        独立审查 Important-2:``_attach(sandbox_id, touch_last_used=True)``
        在拿句柄的同一次往返里把这一行的 ``last_used_at`` 推进到当前
        时间——``reap`` 的空闲 TTL 清扫(``force=False``)按这一列判断
        "多久没被真正用过",不推进的话空闲判定实际退化成"多久以前
        acquire 的",一个持续被 ``exec`` 的热会话会被误杀。见
        :meth:`_attach` 与
        :meth:`SandboxInstanceStore.touch_and_get_container_id` 的
        docstring。
        """
        effective = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        effective = max(1, min(effective, MAX_TIMEOUT_S))

        sbx = await self._attach(sandbox_id, touch_last_used=True)
        timeout_exc, exit_exc = self._sdk_exceptions()

        script = f"/tmp/ew-exec-{uuid4().hex}.py"  # noqa: S108 — sandbox container tmpfs, not host; name has 128 bits of random entropy
        try:
            await sbx.files.write(script, code, user=SANDBOX_EXEC_USER)
            result = await sbx.commands.run(
                f"python -I {script}", user=SANDBOX_EXEC_USER, timeout=effective
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
            raise SandboxSupervisorError(f"sandbox exec failed: {exc}") from exc
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

        沙箱在平台侧已经不存在时(保留期过 / 被平台回收 / 手工删了)——
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
                # 沙箱已不在(保留期过 / 被平台回收 / 手工删了)也要清行,
                # 否则热会话坑永远占着,该 (tenant, user) 再也 acquire 不到
                # ——与 destroy() 的 broad except 同理,见上面 docstring。
                logger.info("reap: sandbox %s already gone", container_id)
            await self.store.mark_destroyed(sandbox_id=sandbox_id, reason="reap")
            reaped += 1
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
                await self.store.mark_destroyed(
                    sandbox_id=sandbox_id, reason="reap_orphaned_create"
                )
                reaped += 1
        return reaped
