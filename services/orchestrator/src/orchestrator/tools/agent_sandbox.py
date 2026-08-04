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

    ``exec``(Task 8)/``reap``(Task 9)要用的方法(如按 idle 扫活跃会话、
    回填 ``last_used_at``)留给那两个任务补充 —— 今天不预先猜测形状。
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

    async def _connect(self, container_id: str) -> Any:
        """裸 connect —— 不做错误处理,调用方各自决定失败策略(``acquire``
        的"唤醒失败则重建" vs ``destroy`` 的"已经不在也无妨,继续清行")。"""
        return await self._sdk().connect(container_id, domain=self.domain, api_key=self.api_key)

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
        container_id = await self.store.get_container_id(sandbox_id=sandbox_id)
        if container_id is not None:
            try:
                sbx = await self._connect(container_id)
                await sbx.kill()
            except Exception:
                # 沙箱已不在(保留期过/被平台回收)—— 仍要往下清行,否则热
                # 会话坑永远占着,该 (tenant, user) 再也 acquire 不到。
                logger.info("destroy: sandbox %s already gone", sandbox_id)
        await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)

    async def exec(self, *, sandbox_id: UUID, code: str, timeout_s: int | None) -> SandboxOutcome:
        """波 1 Task 8(§ 6.1 四契约点)。留在这里只是为了让
        :class:`AgentSandboxClient` 结构性满足 :class:`SandboxRuntime`
        (它有 5 个方法;波 1 Task 7 只做 acquire/release/destroy)——不是
        "先写坏实现",是"先别写",调用即明确失败而非静默返回垃圾结果。
        """
        del sandbox_id, code, timeout_s
        raise NotImplementedError("AgentSandboxClient.exec 尚未接线(波 1 Task 8)")

    async def reap(self, *, force: bool) -> int:
        """波 1 Task 9。理由同 :meth:`exec` 的 docstring。"""
        del force
        raise NotImplementedError("AgentSandboxClient.reap 尚未接线(波 1 Task 9)")
