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
"""

from __future__ import annotations

import base64
import logging
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


def _ensure_e2b_patched() -> None:
    """在首次真正需要 ``e2b`` SDK 时,惰性、幂等地打私有协议补丁。

    必须先跑这个再 ``import e2b``——``kruise_agents.patch_e2b`` 改写的是
    ``e2b`` 内部构造网关 URL 的逻辑,补丁生效前已经拿到的旧引用不会被追溯
    修正。``https=False`` 是必须显式传的(签名默认 ``True``):ALB 只监听
    HTTP 80,默认值会打到 ALB 的 503(探针报告 § 一)。

    调用点收敛到 :meth:`AgentSandboxClient._sdk` 一处 —— 完整理由见模块
    docstring;不要在别处直接 ``import e2b``。
    """
    global _e2b_patched
    if _e2b_patched:
        return
    from kruise_agents.patch_e2b import patch_e2b

    patch_e2b(https=False)
    _e2b_patched = True


class SandboxInstanceStore(Protocol):
    """``sandbox_instance`` 表上 ``AgentSandboxClient`` 需要的操作。

    ``exec``(Task 8)/``reap``(Task 9)要用的方法(如按 idle 扫活跃会话、
    回填 ``last_used_at``)留给那两个任务补充 —— 今天不预先猜测形状。
    """

    async def claim_warm(self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID) -> str | None:
        """占 ``(tenant, user)`` 的热会话坑(spec § 6.2 CAS)。

        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` 的封装:

        * 占到 → 返回 ``None``,调用方负责建沙箱并回填 :meth:`set_container_id`。
        * 没占到、赢家已就绪(``container_id`` 非空)→ 返回赢家的 container_id,
          调用方 connect 上去。
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
        _ensure_e2b_patched()
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
        existing: str | None = None
        if user_id is not None:
            existing = await self._claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )

        just_created = False
        if existing is not None:
            try:
                sbx = await self._connect(existing)
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
                sbx = await self._create(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress)
                just_created = True
        else:
            sbx = await self._create(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress)
            just_created = True

        for relpath, data in seed_files:
            await sbx.files.write(f"{WORKSPACE_ROOT}/{relpath}", data, user=SANDBOX_EXEC_USER)

        if just_created:
            # 连到既有热会话时该行的 container_id 已经是对的(existing 正是
            # 从那里读出来的)—— 只有本次自己建/重建了沙箱才需要回填。
            await self.store.set_container_id(sandbox_id=sandbox_id, container_id=sbx.sandbox_id)
        return sandbox_id

    async def _claim_warm(self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID) -> str | None:
        """``store.claim_warm`` 套上 § 6.5 的统一错误契约。"""
        try:
            return await self.store.claim_warm(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
            )
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox warm-session claim failed: {exc}") from exc

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
