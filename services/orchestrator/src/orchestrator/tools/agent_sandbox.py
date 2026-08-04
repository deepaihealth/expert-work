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
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from expert_work.common.egress_token import mint_egress_token
from orchestrator.tools.e2b_patch import _ensure_e2b_patched
from orchestrator.tools.sandbox import EgressContext, SandboxOutcome, SandboxSupervisorError
from orchestrator.tools.sandbox_instance_store import SandboxInstanceStore

logger = logging.getLogger(__name__)

#: 沙箱内工作区挂载点 —— 与 supervisor 实现一致。
WORKSPACE_ROOT = "/workspace"

#: 沙箱镜像是 ``USER agent``(uid 10000,``nologin``)——E2B SDK 默认以用户
#: ``user`` 执行 ``commands.run`` / ``files.write``,那个账号在我们的镜像里
#: 不存在(``AuthenticationException: invalid username: 'user'``,2026-08-04
#: 探针报告实测);``user="root"`` 同样不行(``InvalidArgumentException``)。
#: 做成常量而非散落字面量 —— Task 8 的 ``exec`` 也要用同一个值。
SANDBOX_EXEC_USER = "agent"

#: 沙箱镜像 ``infra/sandbox-image/Dockerfile`` 声明的那套 ``ENV``,在这里重述
#: 一份显式送进云沙箱。
#:
#: **为什么需要**:envd 派生的进程**不继承镜像的 ``ENV``/``WORKDIR``**。
#: 2026-08-04 集群探针实测(不是推断):``cwd=/home/agent``(镜像是
#: ``WORKDIR /workspace``)、``HOME=/home/agent``(镜像是 ``/workspace``)、
#: ``PIP_USER``/``LANG``/``MPLCONFIGDIR`` 一律 ``None``。本地 supervisor 那边
#: ``runner.py`` 是容器 PID 1、``subprocess.run`` 直接继承容器环境,这些白
#: 拿——所以这是云后端独有的缺口,不是两边都有的老问题。缺了它们:
#: ``pip install`` 在只读 rootfs 上装 ``/usr/local`` 必失败(``PIP_USER=1``
#: 正是为此而设)、matplotlib 没有可写配置目录、``LANG`` 空则中文输出有编码
#: 风险、``HOME`` 指向 ``/home/agent`` 让用户级安装/缓存落在工作区外。
#:
#: **单一事实源为什么落在这里**:Dockerfile 的 ``ENV`` 是给 docker/containerd
#: 的构建期声明,编排进程这一侧在运行时读不到它(镜像不在本进程,也不该为
#: 取几个常量去拉镜像元数据),所以"共享一个变量"物理上做不到,只能是两份
#: 副本 + 一道对齐闸。这份 dict 是唯一真的会被送进云沙箱的副本,放在唯一
#: 使用它的模块里;
#: ``services/orchestrator/tests/test_agent_sandbox.py::test_image_env_matches_dockerfile``
#: 直接解析 Dockerfile 的 ``ENV``/``WORKDIR`` 指令逐条比对,任一边单方面改动
#: 都会红——与 ``test_idle_ttl_matches_supervisor_default`` 防 TTL 漂移是同一
#: 手法,也是本仓对"同一语义分散两处"的既定处置。
#:
#: 只在 ``create(envs=...)`` 传一次:``connect`` 没有 ``envs`` 形参(e2b
#: 2.24.0 ``sandbox_async/main.py``),热会话重连不会重发。这里全是与沙箱
#: 实例无关的常量,建时定死即可——与同样走 ``envs`` 的 egress 变量不同,
#: 那些带 per-sandbox token(见 :meth:`_egress_env`)。
SANDBOX_IMAGE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "HOME": WORKSPACE_ROOT,
    "MPLCONFIGDIR": f"{WORKSPACE_ROOT}/.mplconfig",
    "LANG": "zh_CN.UTF-8",
    "LC_ALL": "zh_CN.UTF-8",
    "PIP_USER": "1",
}

#: 契约常量 —— 与 infra/sandbox-image/runner.py:28-37 逐字对齐(``exec``,
#: Task 8)。runner.py 是本地 docker 沙箱里的 PID 1,这三个值是它的
#: ``DEFAULT_TIMEOUT_S`` / ``MAX_TIMEOUT_S`` / ``MAX_OUTPUT_CHARS``;云沙箱
#: 与本地 supervisor 对同一次 exec 请求要给出等价的 clamp/truncate 行为。
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 1_000_000

#: 传给 ``create()`` / ``connect()`` 的沙箱存活上限(秒)。
#:
#: **必须显式传**。e2b 2.24.0(``e2b/sandbox_async/main.py:171-198``)的
#: ``timeout`` 默认 **300 秒**,且 ``lifecycle.on_timeout`` 默认是 ``"kill"``
#: 而不是 ``"pause"``。2026-08-04 集群实测印证:``get_info()`` 回
#: ``started_at=10:49:10 / end_at=10:54:10``(正好 300s)、``lifecycle=None``。
#: 也就是说不传的话每个沙箱 5 分钟就被平台杀掉——热会话表面上还在,下次
#: acquire 的 connect 失败、``drop_warm`` 重建,能自愈(波 1 验收的热复用
#: 那一项恰好在 5 分钟窗口内跑完,所以没暴露),代价是白付一次 35-40s 冷启
#: 外加用户工作区里的文件没了。
#:
#: **为什么是 20 分钟**:要的是"我们自己的 reap 空闲扫是主角、平台超时只是
#: 兜底",两者不能互抢。下界由 ``_IDLE_TTL_S``(15 分钟,热会话空闲回收线)
#: 定死——比它短则平台先动手,一个还没到我们空闲线的活跃热会话会被平台
#: 掐掉;上界是"编排进程整体失联时兜底还要及时",设成几小时等于让兜底名存
#: 实亡。5 分钟余量约等于 ``SandboxReapWorker._INTERVAL_S``(240s)一整轮再
#: 加富余,保证正常情况下永远是 reap 先到。
#: ``test_platform_timeout_outlives_idle_ttl`` 钉住这个不等式。
#:
#: **刻意不设** ``lifecycle``:``{"on_timeout": "pause"}`` 是 E2B 的语义,
#: 阿里云 ACS 这侧是否真的实现了 pause/resume **未经验证**(探针只跑过
#: create/connect/kill)。一个没验证过的休眠配置正是本轮要修掉的那条失真
#: docstring 的由来,不重蹈。要开 pause 先上集群实测。
_SANDBOX_TIMEOUT_S = 20 * 60

#: ``destroy_reason`` written when :meth:`AgentSandboxClient.release` tears
#: down a non-warm (no ``user_id``) sandbox. Same literal as the docker
#: supervisor's ``DESTROY_REASON_RELEASE``
#: (``sandbox_supervisor/domain.py:33``) so ``sandbox_instance`` rows read
#: the same regardless of which backend wrote them — mirrored rather than
#: imported, for the same wrong-direction-dependency reason the persistence
#: package mirrors that service's idle TTL instead of importing its settings.
_RELEASE_DESTROY_REASON = "release"


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
                # spec § 6.3 —— 重连失败(库存不足/欠费/超过
                # _SANDBOX_TIMEOUT_S 被平台 kill)必须能重建,不能把 run 打死。
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
            if user_id is None:  # 临时沙箱不经过 claim_warm,得先插一行——见 create_ephemeral。
                await self._create_ephemeral_row(tenant_id=tenant_id, sandbox_id=sandbox_id)
            sbx = await self._create_and_track(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id, egress=egress
            )
            just_created = True

        # 全分支终审 Important-6:这一段以前是裸的。``create()`` 一旦返回,
        # 沙箱就已经在平台上跑起来了,但它的 id 要到下面 set_container_id
        # 才落库 —— 这中间任何一处抛异常(种子文件写失败 / 回填时 DB 抖),
        # 留下的是一个活着的 microVM,而 destroy / reap / list_stuck_creating
        # 全都找不到它(前两个按 container_id 找,最后一个刻意不 connect/kill
        # ——它面对的正是没有 container id 的行)。唯一会收走它的是平台超时,
        # 这正是 _SANDBOX_TIMEOUT_S 那条兜底真正承重的地方。
        #
        # 顺带补上 § 6.5 的统一错误契约:``files.write`` 原样抛的是 e2b 自己
        # 的异常类型,写 ``except SandboxSupervisorError`` 的调用方接不住
        # (LLM 那条路径因为 tools 节点 catch 宽 Exception 而无感,但契约就是
        # 契约)。
        try:
            for relpath, data in seed_files:
                await sbx.files.write(f"{WORKSPACE_ROOT}/{relpath}", data, user=SANDBOX_EXEC_USER)
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
                await self._discard_new_sandbox(
                    sbx, tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id
                )
            raise SandboxSupervisorError(f"sandbox post-create setup failed: {exc}") from exc
        return sandbox_id

    async def _discard_new_sandbox(
        self, sbx: Any, *, tenant_id: UUID, user_id: UUID | None, sandbox_id: UUID
    ) -> None:
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
        await self._unwind_slot(
            tenant_id=tenant_id,
            user_id=user_id,
            sandbox_id=sandbox_id,
            reason="post_create_failed",
        )

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

    async def _create_ephemeral_row(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """``store.create_ephemeral`` 套上 § 6.5 的统一错误契约(同 :meth:`_claim_warm`)。"""
        try:
            await self.store.create_ephemeral(tenant_id=tenant_id, sandbox_id=sandbox_id)
        except Exception as exc:
            raise SandboxSupervisorError(f"sandbox row creation failed: {exc}") from exc

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
        复现脚本独立验证过,见报告)。Task 10 追加:临时沙箱同理,见 ``else`` 分支。

        全分支终审 Important-6:上面那套清理本身抽成 :meth:`_unwind_slot`,
        因为 ``create()`` 已经**成功返回**之后的那段(种子文件 +
        ``set_container_id`` 回填)也要走同一套清理,见 :meth:`acquire`。
        """
        try:
            return await self._create(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress)
        except Exception:
            await self._unwind_slot(
                tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id, reason="create_failed"
            )
            raise

    async def _unwind_slot(
        self, *, tenant_id: UUID, user_id: UUID | None, sandbox_id: UUID, reason: str
    ) -> None:
        """让出这次 acquire 已经占下的 CAS 槽位 / 清掉它已经插下的行。

        本身**永不抛出** —— 两个调用方都是在处理另一个异常的路上顺手清理,
        清理失败不该把原始错误换掉(那会让按 ``SandboxSupervisorError`` 写的
        except 链接不住,而且槽位照样没清干净,只是把病根往下埋一层)。
        热会话分支单独 catch + ``logger.error`` 标明需要人工介入;临时沙箱
        分支尽力而为,清不掉也不阻塞。

        与 C-1(``claim_warm`` 接管过期孤儿行)的关系,两条路径不会互踩:
        这里是**同进程内的快路径**,成功了行就没了,C-1 那边无行可接管;
        这里失败(DB 抖动)时行仍停在 ``IN_USE``+NULL,正好落进 C-1 的
        ``_STUCK_CREATE_TTL_S`` 兜底,5 分钟后由下一次 ``claim_warm`` 接管。
        两者都是"把槽位放出来",谁先做完另一个就自然没事可做,不存在
        double-drop —— ``drop_warm`` 删的是 ``(tenant, user)`` 那一行,
        C-1 是在一次新的 ``claim_warm`` 里对同一行改写终态。
        """
        if user_id is not None:
            try:
                await self.store.drop_warm(tenant_id=tenant_id, user_id=user_id)
            except Exception:
                logger.error(
                    "drop_warm failed while unwinding a failed acquire (%s) — "
                    "tenant=%s user=%s stays wedged behind the 0141 warm-slot "
                    "index until the claim_warm stuck-create TTL takes it over",
                    reason,
                    tenant_id,
                    user_id,
                    exc_info=True,
                )
        else:
            # 尽力而为:清不掉也不阻塞,list_stuck_creating 的 TTL 兜底稍后会捡走。
            with contextlib.suppress(Exception):
                await self.store.mark_destroyed(sandbox_id=sandbox_id, reason=reason)

    async def _create(
        self, *, tenant_id: UUID, sandbox_id: UUID, egress: EgressContext | None
    ) -> Any:
        """建一个新沙箱。

        ``envs`` 一次送两组:镜像自己声明、但 envd 派生进程拿不到的那套
        (:data:`SANDBOX_IMAGE_ENV`,全分支终审 Important-2),加上本次
        acquire 的 egress 变量。egress 放后面覆盖——今天两组键零重叠,
        顺序只是把"哪一组是特化"这件事写死,免得以后加键时靠运气。
        """
        try:
            return await self._sdk().create(
                template=self.template,
                envs={
                    **SANDBOX_IMAGE_ENV,
                    **self._egress_env(tenant_id=tenant_id, sandbox_id=sandbox_id, egress=egress),
                },
                timeout=_SANDBOX_TIMEOUT_S,
                domain=self.domain,
                api_key=self.api_key,
            )
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
        return None

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
        """强制拆除 —— 真 kill 沙箱并让出热会话坑。"""
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
        """
        effective = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        effective = max(1, min(effective, MAX_TIMEOUT_S))

        sbx = await self._attach(sandbox_id, touch_last_used=True)
        timeout_exc, exit_exc = self._sdk_exceptions()

        script = f"/tmp/ew-exec-{uuid4().hex}.py"  # noqa: S108 — sandbox container tmpfs, not host; name has 128 bits of random entropy
        try:
            await sbx.files.write(script, code, user=SANDBOX_EXEC_USER)
            result = await sbx.commands.run(
                f"python -I {script}",
                user=SANDBOX_EXEC_USER,
                timeout=effective,
                cwd=WORKSPACE_ROOT,
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
