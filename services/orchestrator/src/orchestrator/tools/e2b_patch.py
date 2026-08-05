"""``e2b`` SDK 的阿里云私有协议补丁 —— 惰性、幂等的 :func:`_ensure_e2b_patched`.

只被 :mod:`orchestrator.tools.agent_sandbox` 用(``AgentSandboxClient._sdk`` /
``_sdk_exceptions`` 两个真实 SDK 导入点)。单独成模块纯粹是给
``agent_sandbox.py`` 让出行数——那个文件顶到了仓内 800 行的单文件硬上限,
而这一段(一个进程级 guard + 一个同步函数 + 解释它为什么长这样的一大段
docstring)是它剩下最独立的一块。搬家逐字照搬,不改签名或语义。

以下这一节原样来自 ``agent_sandbox.py`` 的模块 docstring —— 它描述的正是
本模块这段代码,跟着代码一起搬过来;其中"本模块"指的仍然是当初写下它时
的 ``agent_sandbox.py``(即"``AgentSandboxClient`` 这一侧不在模块顶层无条件
patch")。

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
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: 进程级 guard —— ``_ensure_e2b_patched`` 幂等的关键,见模块 docstring
#: "私有协议 + patch_e2b 导入顺序"一节。
#:
#: CodeQL 的 ``py/unused-global-variable`` 在这里误报"从未被使用":这个名字
#: 的每一次读写都在 ``_ensure_e2b_patched`` 内部、``global`` 声明之后
#: (:func:`_ensure_e2b_patched` 首行就是 ``global _e2b_patched``),模块作用
#: 域里除了这行初始化确实没有第二次出现。测试 ``monkeypatch.setattr(mod,
#: "_e2b_patched", False)`` 也证明它是活的模块属性。
_e2b_patched = False  # codeql[py/unused-global-variable]


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
