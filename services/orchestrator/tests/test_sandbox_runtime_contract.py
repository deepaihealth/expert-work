"""``SandboxRuntime`` 契约测试 —— 一套用例两个实现(design spec § 九)。

design spec 明确把这份文件定为波 1 防两套实现漂移的**唯一手段**:本地/CI
用的 ``HTTPSupervisorRuntime``(docker sandbox-supervisor)和云上用的
``AgentSandboxClient``(ACS Agent Sandbox / E2B SDK)服务同一个
``SandboxRuntime`` Protocol,上层六类工具(``exec_python``/``bash``/……)对
到底跑在哪个后端零感知——如果两者对同一段代码给出不同的观测结果,漂移只
会在生产环境暴露,而不是在这里。

跑法(两档任一环境没准备好就 ``pytest.skip`` 对应参数,不是失败):

* **supervisor 档** —— 需要一个真跑起来的 sandbox-supervisor:
  ``docker compose -f infra/docker-compose.yml --profile full up -d
  postgres migrate sandbox-supervisor credential-proxy``(``sandbox-supervisor``
  本体只在 ``full`` profile 里——``sandbox`` profile 只启 ``credential-proxy``
  单件;``credential-proxy`` 必须一起起,它是唯一会让 compose 创建
  ``expert-work-sandbox-egress`` docker 网络的服务,supervisor 派生的沙箱
  容器靠 ``--network expert-work-sandbox-egress`` 启动,网络不存在会导致
  ``docker run`` 立刻退出、supervisor 报 "runner closed the connection"),
  再设 ``EXPERT_WORK_SANDBOX_SUPERVISOR_URL=http://localhost:<映射端口>``
  (端口见该服务的 ``ports:``,当前是 8001)。
* **agent_sandbox 档** —— 需要 E2B 凭据(``EXPERT_WORK_SANDBOX_E2B_API_KEY``/
  ``_DOMAIN``/``_TEMPLATE``)+ 一个已经跑到 head(至少含迁移 0141)的真
  Postgres(``EXPERT_WORK_DB_DSN``,``postgresql+asyncpg://`` scheme)——
  ``sandbox_instance`` 表是 CAS 的凭据,不能用内存假件替代真集成。

marker 策略:每条契约测试各自单独打 ``@pytest.mark.integration``(真连
基础设施,任一环境变量未设就 skip),但文件末尾的两条漂移断言
(``test_exec_contract_constants_match_the_sandbox_image`` /
``test_idle_ttl_matches_supervisor_default``)刻意**不**打这个 marker
——它们只是比较各处定义的字面量,不连任何真实环境,理应在每一次
``pytest -q -m "not integration"`` 全仓扫描里就跑到,而不是只在偶尔真连
测试集群的场次才被验证到(那正是它们想防止的"没有任何东西会发现漂移")。

已知的**不可弥合**差异,写清楚是为了不让后来者当 bug 顺手"修"掉。

**其一:超时路径的输出。** ``runner.py``(docker supervisor 里的 PID 1)包了
一层 ``subprocess.run``,子进程被 SIGKILL 之前已经写出的部分 stdout/stderr 仍
读得到,``_cap()`` 后原样放进响应。E2B 这边追到 SDK 源码(而非公开文档,
见 task-8-report.md § 1)确认 ``AsyncCommandHandle.wait()``
(``e2b/sandbox_async/commands/command_handle.py:127-137,172-183``)超时时
直接 ``raise self._iteration_exception``,异常对象本身不携带任何已产生的
输出——这一层压根没有把它们塞进去的代码路径,不是文档没写全。
``test_exec_timeout_contract`` 因此只断言 ``exit_code``/``timed_out``,不
断言 stdout/stderr 内容。

**其二:超出 ``[1, 300]`` 的 ``timeout_s`` 的处置。** 契约点 1 是"clamp 到
``[1, MAX_TIMEOUT_S]``",两个后端对**范围内**的值行为一致,对**范围外**的
值则不一致,而且这条差异端到端弥合不了:``AgentSandboxClient.exec`` 在进程
内 clamp(``max(1, min(x, 300))``,与 ``runner.py:51`` 同一公式);
supervisor 那侧请求还没到 ``runner.py`` 就先被 HTTP schema 拦下了——
``sandbox_supervisor/schemas.py:68,90`` 是 ``Field(default=None, gt=0,
le=300)``,``timeout_s=0`` / ``timeout_s=9999`` 拿到的是 422,经
``HTTPSupervisorRuntime.exec`` 变成 ``SandboxSupervisorError``。也就是说
``runner.py`` 那个 clamp 对 HTTP 入口而言是够不着的代码。要弥合就得改 HTTP
schema,而 supervisor 后端这一波是冻结的。因此**没有**范围外取值的端到端
用例;clamp 的三个常量本身改由
``test_exec_contract_constants_match_the_sandbox_image`` 逐个钉住(不需要任何
真实环境),范围内的默认值行为由
``test_exec_default_timeout_is_the_shared_default`` 端到端覆盖。

再审 Minor:上一句里"逐个钉住"钉的是**决定行为的那一处**,不是一律钉
``runner.py``。上界这条尤其要紧——既然本节自己已经论证 ``runner.py`` 的
clamp 在 HTTP 路径上够不着,拿它当闸就是拿一段死代码当闸:真正生效的是
``schemas.ExecRequest.timeout_s`` 的 ``le``,而那个数此前没有任何东西钉。
把它从 300 改成 600,两个后端的实际上界当场分叉,而这道闸照样绿。

**其三:输出截断的字节格式。** 契约表只约束长度上限(1_000_000 chars),不
约束展示格式。``runner.py`` 的 ``_cap()`` 保留头尾各半、中间插一行
``[... N chars truncated ...]`` 标记,所以它的返回值实际比上限**长**几十个
字符;``AgentSandboxClient`` 走的是简单头部截断 ``[:MAX_OUTPUT_CHARS]``
(理由见其 ``exec`` docstring 契约点 2)。``test_exec_output_is_capped``
因此断言的是"被截到了上限附近",不是某个精确长度。

**其四:``/workspace`` 的物理路径。**(波 2 真栈复跑实测)supervisor 档的
``/workspace`` 是容器里一个真目录,``os.getcwd()`` 就报 ``/workspace``;
agent_sandbox 档的 ``/workspace`` 是**平台建的符号链接**,指向
``/run/csi/mount-root/nas/<hash>``(hash 每次挂载现算),而 ``getcwd(2)``
按定义返回解析后的物理路径。这条弥合不了——符号链接是平台注入 NAS 挂载的
方式,不是我们能选的。功能上无影响:相对路径读写、``/workspace/...``
绝对路径、跨 exec 持久化全部照常(各由自己的用例覆盖)。
``test_exec_cwd_is_workspace`` 因此比 ``(st_dev, st_ino)`` 而不是路径字符串。

**留给上层的一条**:云后端上,agent 自己跑 ``os.getcwd()``(或任何打印绝对
路径的报错)会看到 ``/run/csi/mount-root/nas/<hash>`` 而不是 ``/workspace``。
纯观感,但 LLM 读到自己的 cwd 长这样可能会困惑;真要治得在提示词或工具输出
层做路径回写,不在这一层。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.tools.agent_sandbox import MAX_OUTPUT_CHARS
from orchestrator.tools.sandbox import SandboxRuntime
from orchestrator.tools.workspace_store import WorkspaceStore


def _supervisor_runtime() -> SandboxRuntime:
    url = os.environ.get("EXPERT_WORK_SANDBOX_SUPERVISOR_URL")
    if not url:
        pytest.skip("EXPERT_WORK_SANDBOX_SUPERVISOR_URL 未设 —— supervisor 契约档跳过")
    from orchestrator.tools.sandbox import HTTPSupervisorRuntime

    return HTTPSupervisorRuntime(base_url=url)


def _agent_sandbox_runtime(
    *, workspace_pv_name: str = "", workspace_root: str = ""
) -> SandboxRuntime:
    """``workspace_pv_name`` 默认从环境读,**没配就 skip**。

    ``workspace_root``(Task C 新增关键字,默认空串)—— 不从环境读、不影响
    其余调用点的既有行为,只有 :func:`test_agent_sandbox_workspace_root_is_not_world_accessible`
    显式传值。原因见下面那段"``workspace_root`` 仍然不配"——那段话描述的是
    这份契约档 *其余* 十八条用例的现状,不是这个参数本身的能力上限;那条新
    用例恰恰要验的是 ``workspace_root`` 配上之后 ``_prepare_workspace_mount``
    真的落了 ``0o700``,所以它需要一个不影响别人、只给自己开的口子。

    这个参数原本默认空串,理由是"保持对既有调用方零行为变化——未配时
    ``_create`` 完全不带 ``metadata`` 键,与波 1 逐字相同"。Task 9 之后那句
    话不再成立(波 2 收尾复跑真栈时坐实):镜像不再预建 ``/workspace``,不挂
    NAS 就等于沙箱里根本没有 ``/workspace``,而 ``exec`` 无条件传
    ``cwd=/workspace`` —— 18 条 exec 用例齐刷刷炸在
    ``InvalidArgumentException: cwd '/workspace' does not exist``。这是典型的
    跨任务缝隙:Task 7 写这个默认值时它是对的,Task 9 把地基抽走了。

    生产装配点(``build_sandbox_runtime``)本就强制要求 ``PV_NAME``,所以
    "不挂载的 agent_sandbox"是一个生产里不存在的配置——契约测试没有理由去
    测它。改成读同一个环境变量、没配就 skip,与生产保持同一个形状。

    ``workspace_root`` 仍然不配:那是"把 NAS 挂进 control-plane Pod"的那半边,
    GitHub runner 对 NAS 没有 NFS 路由。缺了它 ``_prepare_workspace_mount``
    整段跳过,挂载点目录改由平台建(``root:root 0755``,集群实测),沙箱侧
    ``AgentSandboxClient._chown_workspace_mount``(方向变更前叫
    ``_chmod_workspace_mount``)那道兜底因此成为这一档唯一的权限来源 ——
    也正是这一档真正在验的东西之一。
    """
    api_key = os.environ.get("EXPERT_WORK_SANDBOX_E2B_API_KEY")
    if not api_key:
        pytest.skip("E2B 凭据未设 —— agent_sandbox 契约档跳过")
    workspace_pv_name = workspace_pv_name or os.environ.get(
        "EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME", ""
    )
    if not workspace_pv_name:
        pytest.skip("EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME 未设 —— agent_sandbox 契约档跳过")
    dsn = os.environ.get("EXPERT_WORK_DB_DSN")
    if not dsn:
        pytest.skip("EXPERT_WORK_DB_DSN 未设 —— 契约档需要真 sandbox_instance 表")

    # 注意:不是 `expert_work.persistence.sandbox_instance` / `SqlSandboxInstanceStore
    # (engine=...)`——那是任务 brief 草稿里两处对不上实际代码的笔误(模块名少了
    # `_store` 后缀;构造函数吃的是 `session_factory: async_sessionmaker`,不是
    # 裸 `engine` kwarg)。这里照 test_sql_sandbox_instance_store.py 已确立的
    # 用法接。
    from expert_work.persistence import (
        DatabaseConfig,
        create_async_engine_from_config,
        create_async_session_factory,
    )
    from expert_work.persistence.sandbox_instance_store import SqlSandboxInstanceStore
    from orchestrator.tools.agent_sandbox import AgentSandboxClient

    engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
    store = SqlSandboxInstanceStore(create_async_session_factory(engine))
    return AgentSandboxClient(
        domain=os.environ["EXPERT_WORK_SANDBOX_E2B_DOMAIN"],
        api_key=api_key,
        template=os.environ["EXPERT_WORK_SANDBOX_E2B_TEMPLATE"],
        store=store,
        egress_token_secret=os.environ.get(
            "EXPERT_WORK_EGRESS_TOKEN_SECRET", "contract-test-secret"
        ),
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
        workspace_pv_name=workspace_pv_name or None,
        workspace_root=workspace_root or None,
    )


def _agent_sandbox_runtime_with_workspace_mount() -> SandboxRuntime:
    """``test_agent_sandbox_nas_mount_shares_workspace_across_two_sandboxes``
    的具名入口。

    历史上这个函数存在是因为 :func:`_agent_sandbox_runtime` 默认**不**挂 NAS,
    只有这一条用例需要挂;那个默认值已经随波 2 收尾去掉了(见该函数
    docstring),两者现在配置完全相同。保留这个名字而不是让用例直接调基函数:
    调用点读起来仍然点名"这条测的是 NAS 挂载",而不是碰巧和别的用例共用了同一
    个 runtime 构造器。

    这条用例曾经因为两层基础设施缺口跑不通——集群 SandboxSet 还是波 1 老镜像、
    以及 ``csi-volume-config`` 的特权豁免工单未批。两者都已解决(工单已批;
    镜像 tag 在 Task 8 的 runbook 步骤里换新),2026-08-07 真栈复跑已能挂上
    NAS。
    """
    return _agent_sandbox_runtime()


@pytest.fixture(params=["supervisor", "agent_sandbox"])
def runtime(request: pytest.FixtureRequest) -> SandboxRuntime:
    return {"supervisor": _supervisor_runtime, "agent_sandbox": _agent_sandbox_runtime}[
        request.param
    ]()


def _supervisor_workspace_store() -> WorkspaceStore:
    """control-plane 读 supervisor 后端工作区文件的真实客户端。

    与生产 ``build_workspace_store`` 在 ``sandbox_supervisor_url`` 真值分支
    返回的是同一个类,读的是与 :func:`_supervisor_runtime` 完全相同的
    ``EXPERT_WORK_SANDBOX_SUPERVISOR_URL``——两者代理到同一个 supervisor 实
    例,天然指向同一个用户的同一棵工作区卷(卷名由 ``(tenant_id, user_id)``
    算出,两条客户端各自独立拼,不经过共享状态)。
    """
    url = os.environ.get("EXPERT_WORK_SANDBOX_SUPERVISOR_URL")
    if not url:
        pytest.skip("EXPERT_WORK_SANDBOX_SUPERVISOR_URL 未设 —— supervisor 契约档跳过")
    from orchestrator.tools.workspace_store import SupervisorWorkspaceStore

    return SupervisorWorkspaceStore(base_url=url)


def _agent_sandbox_workspace_store() -> WorkspaceStore:
    """control-plane 读 agent_sandbox 后端工作区文件的真实客户端。

    ``NasWorkspaceStore`` 直读挂载的 NAS 树——生产 ``build_workspace_store``
    在 ``workspace_nas_root`` 真值分支返回的正是这个类。

    ``EXPERT_WORK_WORKSPACE_NAS_ROOT`` 是这份契约档里第一次真正被消费的地
    方::func:`_agent_sandbox_runtime` 的 docstring 与 ``_FIXTURE_ENV_
    DISPOSITION`` 都点名它"刻意不配"——但那句话说的是 ``AgentSandboxClient``
    自己那份(沙箱侧预挂载 mkdir 用的 ``workspace_root`` 字段,GitHub runner
    对 NAS 没有 NFS 路由,配不了)。这里是完全独立的第二个消费者:
    control-plane 自己读工作区文件的客户端,只有这条用例和
    :func:`test_agent_sandbox_workspace_root_is_not_world_accessible` 需要真机 NAS
    路由时才配,不改变其余十八条既有用例的行为。
    """
    root = os.environ.get("EXPERT_WORK_WORKSPACE_NAS_ROOT")
    if not root:
        pytest.skip(
            "EXPERT_WORK_WORKSPACE_NAS_ROOT 未设 —— 这条用例需要本进程也能直接"
            "触达 NAS 树(模拟 control-plane 直读),契约档默认不配这一项"
            "(GitHub runner 无 NFS 路由),只在真栈复跑时给。"
        )
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

    return NasWorkspaceStore(root=root)


@pytest.fixture(params=["supervisor", "agent_sandbox"])
def runtime_and_control_plane_store(
    request: pytest.FixtureRequest,
) -> tuple[str, SandboxRuntime, WorkspaceStore]:
    """(后端名, 写方 SandboxRuntime, 读方 WorkspaceStore) 三元组——同一个后端,
    同一棵工作区树,两个不同身份。

    既有的 ``runtime`` fixture 只给 ``SandboxRuntime`` 一个,这份文件其余每
    一条用例的写、读都靠它(两次 ``runtime.exec`` 调用)。W2-BUG-1 之所以能
    在 19/19 全绿的套件下活下来,根子正是这个模式:写和读永远是同一个进
    程、同一个身份。这条 fixture 是 Task C 存在的理由——第二个身份是
    control-plane 实际读取工作区文件所用的同一个 ``WorkspaceStore`` 实现
    (``build_workspace_store`` 的产物),不是又一次 ``runtime.exec``。

    返回的后端名(``request.param``)让调用方能按后端选写入方式——见
    ``test_written_file_is_readable_by_the_control_plane_identity`` 的
    docstring:两个后端"读方是谁"这件事本身就不同构(supervisor 走一个丢弃
    了 ``CAP_DAC_OVERRIDE`` 的 root 辅助容器,agent_sandbox 走这个测试进程
    自己的 ``os.open``),没有一种写入 mode 能同时对两者都是"读方身份敏感"
    的,只能按后端分别选一种能证明点什么的写法。
    """
    if request.param == "supervisor":
        return request.param, _supervisor_runtime(), _supervisor_workspace_store()
    return request.param, _agent_sandbox_runtime(), _agent_sandbox_workspace_store()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_returns_stdout(runtime: SandboxRuntime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c1")
    try:
        outcome = await runtime.exec(sandbox_id=sid, code="print('CONTRACT_OK')", timeout_s=30)
        assert "CONTRACT_OK" in outcome.stdout
        assert outcome.exit_code == 0
        assert outcome.timed_out is False
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_nonzero_exit_is_reported(runtime: SandboxRuntime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c2")
    try:
        outcome = await runtime.exec(sandbox_id=sid, code="import sys; sys.exit(3)", timeout_s=30)
        assert outcome.exit_code == 3
        assert outcome.timed_out is False
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_timeout_contract(runtime: SandboxRuntime) -> None:
    """契约点 3(§ 6.1)在两个实现上必须一致:``exit_code=-1`` 且
    ``timed_out=True``。**不**断言 stdout/stderr 内容——见模块 docstring
    的"已知不可弥合差异"一节,E2B 的超时异常不携带任何累积输出。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c3")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import time; time.sleep(30)", timeout_s=2
        )
        assert outcome.timed_out is True
        assert outcome.exit_code == -1
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_default_timeout_is_the_shared_default(runtime: SandboxRuntime) -> None:
    """契约点 1 的另一半:``timeout_s=None`` 走同一个 30 秒缺省值。

    supervisor 那侧是"HTTP 请求体不带 timeout_s → 服务端用
    ``SandboxSupervisorSettings.default_timeout_s``";agent_sandbox 那侧是
    ``AgentSandboxClient.DEFAULT_TIMEOUT_S``。两条完全不同的路子,观测结果
    必须相同 —— 睡 45 秒的代码在两个后端上都该在 30 秒被掐掉。三个常量本身
    的对齐见 ``test_exec_contract_constants_match_the_sandbox_image``。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c11")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import time; time.sleep(45)", timeout_s=None
        )
        assert outcome.timed_out is True
        assert outcome.exit_code == -1
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_output_is_capped(runtime: SandboxRuntime) -> None:
    """契约点 2:输出上限 1_000_000 chars,两个后端都不能把 5MB 原样吐回来。

    断言"截到了上限附近"而不是精确长度 —— 两边的截断格式不同(见模块
    docstring 差异其三):``runner.py`` 头尾各半 + 中间插标记,所以会比上限
    略长几十个字符;``AgentSandboxClient`` 是简单头部截断,正好等于上限。
    """
    produced = 5_000_000
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c12")
    try:
        outcome = await runtime.exec(sandbox_id=sid, code=f"print('x' * {produced})", timeout_s=60)
        assert len(outcome.stdout) < produced, "5MB 原样返回 = 上限根本没生效"
        assert len(outcome.stdout) <= MAX_OUTPUT_CHARS + 200, (
            f"截断后仍有 {len(outcome.stdout)} chars,超出 1_000_000 上限 + 截断标记的余量"
        )
        assert len(outcome.stdout) > MAX_OUTPUT_CHARS // 2, (
            "截得过狠 —— 上限是 1_000_000,不该只剩零头"
        )
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stderr_captured(runtime: SandboxRuntime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c4")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import sys; print('to-err', file=sys.stderr)",
            timeout_s=30,
        )
        assert "to-err" in outcome.stderr
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_cwd_is_workspace(runtime: SandboxRuntime) -> None:
    """全分支终审 Important-2 —— 这条以前不存在,正是云后端 cwd 错了几个月
    没人发现的原因:其它每一条用例都用绝对 ``/workspace/...`` 路径,对 cwd
    完全不敏感。

    supervisor 档靠 ``docker run --workdir /workspace``(W2 Task 6,
    ``SandboxRuntimeProvider.docker_run_argv`` —— 镜像自己不再声明
    ``WORKDIR``,W2 Task 9 为了给 ACS 的 NAS-mount symlink 让路把它删了;
    ``runner.py`` 是容器 PID 1,``subprocess.run`` 继承这个 run-time cwd);
    agent_sandbox 档靠 ``commands.run(cwd=...)`` —— envd 派生的进程不继承
    镜像 ``WORKDIR``(即便镜像声明了也一样),实测落在 ``/home/agent``。
    两条路子不同,观测结果必须相同。

    **比 inode 身份,不比路径字符串**(波 2 收尾真栈复跑)。这条以前断言
    ``os.getcwd() == "/workspace"``,在波 2 之前是对的:那时 ``/workspace``
    两个后端都是真目录。云后端现在不是了 —— 平台把 ``/workspace`` 建成指向
    ``/run/csi/mount-root/nas/<hash>`` 的**符号链接**,而 ``getcwd(2)`` 按定义
    返回解析后的物理路径,于是这条用例报
    ``assert '/run/csi/mount-root/nas/...' == '/workspace'``。

    那个字符串不是我们能承诺的东西(hash 由平台每次挂载现算),而这条用例
    真正要问的是「这个进程是不是站在工作区里」。比 ``(st_dev, st_ino)`` 正好
    回答那个问题:``os.stat`` 跟随符号链接,所以 supervisor 档(真目录)与
    agent_sandbox 档(symlink)都成立,而且比字符串相等更强 —— 一个 cwd 恰好
    叫 ``/workspace`` 但其实是另一棵树的实现骗不过它。顺带把 ``getcwd()``
    一起打出来,失败时不用再猜它到底站在哪。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c8")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import os\n"
                "here, ws = os.stat('.'), os.stat('/workspace')\n"
                "print((here.st_dev, here.st_ino) == (ws.st_dev, ws.st_ino), os.getcwd())"
            ),
            timeout_s=30,
        )
        assert outcome.stdout.split()[:1] == ["True"], outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_relative_write_lands_in_workspace(runtime: SandboxRuntime) -> None:
    """cwd 契约的用户可见后果:LLM 代码里的 ``open('out.csv','w')`` 必须落在
    ``/workspace``,否则 ``file_ops``(只构造绝对 ``/workspace/...`` 路径)
    根本看不见 agent 刚写出来的文件。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c9")
    try:
        await runtime.exec(
            sandbox_id=sid, code="open('relative.txt','w').write('REL_OK')", timeout_s=30
        )
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="print(open('/workspace/relative.txt').read())",
            timeout_s=30,
        )
        assert "REL_OK" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_created_files_are_not_masked_by_the_sandbox_default_umask(
    runtime: SandboxRuntime,
) -> None:
    """Task 4 审查 Critical 后续(原为跨 uid 写冲突而加)—— agent 代码自己
    ``mkdir``/``open`` 出的嵌套目录/文件必须是 world-writable(两后端都在
    exec 路径上把 umask 设成 000:supervisor 档 ``runner.py.main()`` 的
    ``os.umask(0)``,agent_sandbox 档 ``commands.run`` 命令串的
    ``umask 000 &&`` 前缀),不能被沙箱默认 umask(常见 ``0o022``)掩成
    ``0o755``/``0o644``。

    **这条机制原本的理由已经不成立**(方向变更之后——共享 gid 改统一
    uid,见 ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-
    design.md`` § 六):以前 control-plane 与沙箱的 agent 是不同 uid,
    ``0o755``/``0o644`` 在 ``read``/``list`` 路径上完全不可见(两者仍然
    通),只有 control-plane 经宿主机卷/NAS 挂载以**另一个 uid** 尝试删除
    或覆盖该文件时才会撞 ``EACCES``。现在两侧同 uid,属主位本身就够,不再
    需要靠这条机制兜底跨 uid 访问。这条用例本身留着不删——
    ``0o777``/``0o666`` 是比统一 uid 之后真正需要的 mode 更宽的**安全超
    集**,不是错,只是不再最小;收紧它是一个需要真栈验证的后续任务(见
    ``AgentSandboxClient.exec`` 与 ``runner.py`` 的 docstring),这条契约用
    例本身照旧断言权限位——POSIX 权限语义本身与"谁去读"这个 uid 无关,
    ``0o777``/``0o666`` 早已蕴含了"任何 uid 都能写"这件事,不需要真的换一
    个 uid 的进程来验证。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c9b")
    try:
        code = (
            "import os\n"
            "os.makedirs('reports/nested')\n"
            "open('reports/nested/out.txt', 'w').close()\n"
            "print('DIR_MODE=%o' % (os.stat('reports').st_mode & 0o777))\n"
            "print('NESTED_MODE=%o' % (os.stat('reports/nested').st_mode & 0o777))\n"
            "print('FILE_MODE=%o' % (os.stat('reports/nested/out.txt').st_mode & 0o777))\n"
        )
        outcome = await runtime.exec(sandbox_id=sid, code=code, timeout_s=30)
        assert outcome.exit_code == 0, outcome.stderr
        assert "DIR_MODE=777" in outcome.stdout, outcome.stdout
        assert "NESTED_MODE=777" in outcome.stdout, outcome.stdout
        assert "FILE_MODE=666" in outcome.stdout, outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_written_file_is_readable_by_the_control_plane_identity(
    runtime_and_control_plane_store: tuple[str, SandboxRuntime, WorkspaceStore],
) -> None:
    """一套用例两实现:沙箱(agent)写的文件,control-plane 实际读取工作区
    文件所用的同一个 ``WorkspaceStore`` 读得回来。

    **为什么这条要进契约套件**:W2-BUG-1(agent 写的 ``MEMORY.md``
    control-plane 读不动,前端列得出、下载 404)在 19/19 全绿的套件下活了
    下来,根子是原套件只验"写进去读得出",而套件里写和读永远是同一个进
    程、同一个身份——这份文件其余每一条工作区用例(比如
    ``test_workspace_files_survive_across_exec``)延续的正是这个模式:两次
    ``runtime.exec`` 调用,同一个身份读自己刚写的东西。真实部署里写方是沙
    箱、读方是 control-plane;同 uid 方向变更(见
    ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
    § 六)让这两个身份重新
    统一,这条用例钉的就是那个前提本身:写方走 ``runtime.exec``(沙箱进
    程),读方走 ``build_workspace_store`` 在生产也会用的同一个
    ``WorkspaceStore`` 实现(``SupervisorWorkspaceStore`` /
    ``NasWorkspaceStore``),不是又一次 ``runtime.exec``。

    **两个后端的写法故意不同,理由不是偷懒**。最初的写法是两个后端共用
    ``tempfile.mkstemp`` + ``os.replace``(mkstemp 恒定落地 ``0o600``,与调
    用方 umask 无关,是生产 ``file_ops.py`` 结构化 ``write_file`` 工具现在
    真正落地的 mode——Task A 删掉了那处显式变宽的 ``chmod``)——这样"读方
    是不是与写方同一个身份"才是唯一的决定因素,是最严格的写法。但**实测**
    (mutation 的一种:换一种输入,看断言会不会因为错误的原因倒下)这个写
    法在 supervisor 档上必现 404("Permission denied"):supervisor 的
    ``read_volume_file`` 用一个 ``--cap-drop ALL`` 的辅助容器读卷
    (``docker_client.py`` ``_AUX_CONTAINER_HARDENING_ARGS``,注释自己写着
    "stays root... forcing --user here would risk it being unable to
    read/write a volume whose top-level ownership it doesn't control")——
    丢 ``ALL`` capability 也丢了 ``CAP_DAC_OVERRIDE``,这个容器虽然是 uid 0
    但**不能**绕过普通 DAC 权限检查,遇到 ``0o600``(属主 10000、其它人全
    零)的文件与遇到任何非属主 uid 一样是 EACCES。这与本次方向变更的 uid
    统一无关——supervisor 走的是这条完全独立、Global Constraints 明确"不
    动"的 docker 卷模型,它"读方是谁"这件事从来就不是"控制面进程自己的
    uid",而是这个丢了 capability 的根身份;这条契约测试的边界是"同一套
    用例两个实现",不是"顺手把 supervisor 的这个下载兼容性缺口也堵上"——
    那个发现已经写进本次任务的报告,留给独立的后续任务判断是否要修
    ``file_ops.py`` 或 supervisor 的读路径。这里退一步用裸 ``open()``
    (两个后端都已经在用的既有写法,umask=0 落地 ``0o666``——见
    ``test_exec_created_files_are_not_masked_by_the_sandbox_default_umask``)
    换 supervisor 档能通过、且理由与"两个身份重新统一"无关的读方式;
    agent_sandbox 档保留 ``mkstemp``,因为它是这条用例唯一还能真正压中"读方
    身份是否与写方一致"这件事的那一档(见下方 assert 之前的实现说明)。
    """
    backend, runtime, store = runtime_and_control_plane_store
    tenant_id, user_id = uuid4(), uuid4()
    sid = await runtime.acquire(tenant_id=tenant_id, thread_id="c19", user_id=user_id)
    try:
        if backend == "supervisor":
            write_code = (
                "open('/workspace/control_plane_probe.txt', 'w').write('CONTROL_PLANE_CAN_READ_ME')"
            )
        else:
            write_code = (
                "import os, tempfile\n"
                "fd, tmp = tempfile.mkstemp(dir='/workspace')\n"
                "os.write(fd, b'CONTROL_PLANE_CAN_READ_ME')\n"
                "os.close(fd)\n"
                "os.replace(tmp, '/workspace/control_plane_probe.txt')\n"
            )
        outcome = await runtime.exec(sandbox_id=sid, code=write_code, timeout_s=30)
        assert outcome.exit_code == 0, outcome.stderr

        data = await store.read_file(
            tenant_id=tenant_id, user_id=user_id, path="control_plane_probe.txt"
        )
        assert data == b"CONTROL_PLANE_CAN_READ_ME"
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_sandbox_workspace_root_is_not_world_accessible() -> None:
    """用户工作区根目录 mode 是 ``0o700``——other 档全零。

    **名字里的 ``agent_sandbox`` 前缀是承重的,不是命名风格**:唯一带真
    E2B 凭据的 CI job(``.github/workflows/sandbox-contract.yml``)用
    ``-k agent_sandbox`` 选测试。本用例不走 parametrize(见下),名字里没有
    这个子串就会被**静默 deselect**——比 skip 更坏,报告里连一行都不留。
    改名前它在任何自动化路径上都跑不到:CI 选不中,而本机又没有 NAS 路由。
    参照同文件已有的
    ``test_agent_sandbox_nas_mount_shares_workspace_across_two_sandboxes``。

    波 2 期间(``NasWorkspaceStore``/共享 gid 方案下)这里是 ``0o2770``/
    ``0o777``(group-/world-writable),靠 subPath 挂载范围做隔离,POSIX 位
    本身不设防,并因此留下两条 dismissed 的 CodeQL high。同 uid 方向变更
    (见 ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
    § 六)之后不需要给任何"另一方"开口子了——``NasWorkspaceStore.
    _DIR_MODE`` 与 ``AgentSandboxClient._ensure_workspace_dir`` 都已收紧到
    ``0o700``。这条断言防的是有人为了排查方便(比如临时调宽方便手工核对
    NAS 上的文件)又把它放宽回去。

    **只在 agent_sandbox 后端跑,不参数化 ``runtime``**(同
    ``test_agent_sandbox_nas_mount_shares_workspace_across_two_sandboxes``
    的既有先例——那条也不参数化,理由类似)。``0o700`` 是 wave 2 NAS 直读
    设计独有的常量;supervisor 后端走的是完全不同、这次方向变更明确"不
    动"的 docker 卷模型(brief Global Constraints:"仅改 control-plane 的
    uid...不动...sandbox-supervisor")——**实测** supervisor 档 ``/workspace``
    是 ``0o755``(docker 具名卷挂载点的默认 mode,``chown_volume`` 只改属主
    不改 mode,是这个后端一直以来的行为,与本次 uid 统一无关)。把同一条
    ``== 0o700`` 断言套到 supervisor 档上会是一条恒假的契约,不是"两个后端
    一套用例"该有的样子。

    **为什么需要 ``workspace_root``**(这份契约档其余用例都不配的一项)。
    ``_ensure_workspace_dir`` 只在 ``AgentSandboxClient.workspace_root`` 非
    空时才跑(``_prepare_workspace_mount`` "workspace_root 未配... 整段跳
    过")——这份契约档默认不配它(GitHub runner 对 NAS 没有 NFS 路由,见
    ``_agent_sandbox_runtime`` 与 ``_FIXTURE_ENV_DISPOSITION`` 的既有注
    释),这条用例专门为自己多要一项(读同一个
    ``EXPERT_WORK_WORKSPACE_NAS_ROOT``,通过 Task C 新增的 ``workspace_root=``
    关键字传给 ``_agent_sandbox_runtime``),不改变其余十八条用例的既有行
    为。少了它,目录改由平台建(``root:root 0755``),沙箱侧
    ``AgentSandboxClient._chown_workspace_mount`` 那道兜底只 chown 属主、不
    chmod mode(见其 docstring"为什么 chown 而不是 chmod")——这条断言会在
    错误的原因下失败(不是"mode 放宽了",是"这条腿本来就没把 control-plane
    侧 mkdir 接上"),意义不一样。

    **mode 从沙箱内部 ``os.stat`` 读,不从这个测试进程自己读**——
    ``_ensure_workspace_dir`` 的 mkdir/chmod 跑在这个测试进程自己的 uid 下
    (不一定是 10000),但 ``stat(2)`` 只需要祖先目录的搜索权限、不需要目标
    本身的任何权限位,所以沙箱(真的是 uid 10000)总能 ``stat`` 到这个目
    录、如实报出它的 mode——这条断言因此不依赖"跑这条测试的进程本身是不
    是 uid 10000"这件事,只依赖 NAS 路由是否可达。
    """
    nas_root = os.environ.get("EXPERT_WORK_WORKSPACE_NAS_ROOT")
    if not nas_root:
        pytest.skip(
            "EXPERT_WORK_WORKSPACE_NAS_ROOT 未设 —— 这条用例需要本进程也能直接"
            "触达 NAS 树上验 control-plane 侧 mkdir+chmod 的落地结果,契约档默"
            "认不配这一项(GitHub runner 无 NFS 路由),只在真栈复跑时给。"
        )
    runtime = _agent_sandbox_runtime(workspace_root=nas_root)
    tenant_id, user_id = uuid4(), uuid4()
    sid = await runtime.acquire(tenant_id=tenant_id, thread_id="c20", user_id=user_id)
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import os; print('MODE=%o' % (os.stat('/workspace').st_mode & 0o777))",
            timeout_s=30,
        )
        assert outcome.exit_code == 0, outcome.stderr
        assert "MODE=700" in outcome.stdout, outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_sees_the_image_environment(runtime: SandboxRuntime) -> None:
    """镜像 ``ENV`` 在两个后端都要到达沙箱进程。

    supervisor 档白拿(容器环境被 ``runner.py`` 的 ``subprocess.run`` 继承);
    agent_sandbox 档必须由 ``create(envs=...)`` 显式送(实测 envd 派生进程
    这几项全是 ``None``)。挑的三项各自对应一条真实故障:``PIP_USER`` 空 →
    只读 rootfs 上 ``pip install`` 必失败;``HOME`` 不是 ``/workspace`` →
    用户级安装落在工作区外;``MPLCONFIGDIR`` 空 → matplotlib 没有可写配置目录。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c10")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import os\n"
                "for k in ('HOME', 'PIP_USER', 'MPLCONFIGDIR', 'LANG'):\n"
                "    print(k, '=', os.environ.get(k))\n"
            ),
            timeout_s=30,
        )
        assert "HOME = /home/agent" in outcome.stdout
        assert "PIP_USER = 1" in outcome.stdout
        assert "MPLCONFIGDIR = /home/agent/.mplconfig" in outcome.stdout
        assert "LANG = zh_CN.UTF-8" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_injects_per_agent_pythonuserbase(runtime: SandboxRuntime) -> None:
    """sandbox migration wave 2(spec 决策 10)—— 同用户双 agent 共享一个
    沙箱时,``$HOME/.local`` 默认共享,pip --user 装包会互相覆盖/并发损坏。
    ``agent_key`` 非空时两个后端都必须把它转成同一个 ``PYTHONUSERBASE`` 值
    (``orchestrator.tools.sandbox.agent_key_envs`` 单源)。"""
    from expert_work.persistence import SANDBOX_AGENTS_ROOT

    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c16")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import os; print(os.environ.get('PYTHONUSERBASE'))",
            timeout_s=30,
            agent_key="contract-agent",
        )
        assert outcome.stdout.strip() == f"{SANDBOX_AGENTS_ROOT}/contract-agent"
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_files_land_under_the_sandbox_skills_root(runtime: SandboxRuntime) -> None:
    """sandbox migration wave 2 (spec § 四) — seed lands under
    ``SANDBOX_SKILLS_ROOT`` (sandbox-local), not ``/workspace`` (NAS-backed,
    wave 2's whole point is skills no longer occupying user workspace quota).
    ``relpath`` here is exactly what the caller passes — the ``<agent_key>/``
    namespace prefix is the *caller's* job (``build_skill_seed_files``), not
    this layer's."""
    from expert_work.persistence import SANDBOX_SKILLS_ROOT

    sid = await runtime.acquire(
        tenant_id=uuid4(),
        thread_id="c5",
        seed_files=(("seeded.txt", b"SEED_CONTENT"),),
    )
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=f"print(open('{SANDBOX_SKILLS_ROOT}/seeded.txt').read())",
            timeout_s=30,
        )
        assert "SEED_CONTENT" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_files_land_under_the_agent_key_skill_namespace(runtime: SandboxRuntime) -> None:
    """sandbox migration wave 2 Task 7 —— skill_seed 落点契约。

    ``build_skill_seed_files``(``orchestrator.tools.skill_seed``)产出的
    relpath 形如 ``<agent_key>/<skill_name>/SKILL.md``(该模块的
    ``candidates`` 列表首项)。上一条测试
    (``test_seed_files_land_under_the_sandbox_skills_root``)已经覆盖了
    "任意 seed_files 落在 ``SANDBOX_SKILLS_ROOT`` 之下"这个更宽的契约点;
    这条额外精确复现生产真实用的两层命名空间形状(``<agent_key>/<skill>/``),
    不经过 ``build_skill_seed_files`` 本身(那需要一整套
    ``SkillVersion``/object-store 前置),直接用
    :func:`~orchestrator.tools.skill_seed.sanitize_agent_key` 的真实输出做
    ``agent_key``——两个后端各自 seed 后必须能在
    ``{SANDBOX_SKILLS_ROOT}/<agent_key>/<skill>/SKILL.md`` 这个精确路径读
    回同一份内容。"""
    from expert_work.persistence import SANDBOX_SKILLS_ROOT
    from orchestrator.tools.skill_seed import sanitize_agent_key

    agent_key = sanitize_agent_key("Contract Test Agent")
    skill_md = "---\nname: contract-skill\n---\ncontract skill body\n"
    sid = await runtime.acquire(
        tenant_id=uuid4(),
        thread_id="c18",
        seed_files=((f"{agent_key}/contract-skill/SKILL.md", skill_md.encode("utf-8")),),
    )
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=(
                f"print(open('{SANDBOX_SKILLS_ROOT}/{agent_key}/contract-skill/SKILL.md').read())"
            ),
            timeout_s=30,
        )
        assert outcome.stdout.strip() == skill_md.strip()
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workspace_files_survive_across_exec(runtime: SandboxRuntime) -> None:
    """ "热"的是文件系统而非 Python 变量 —— 两个实现都该如此。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c6")
    try:
        await runtime.exec(
            sandbox_id=sid,
            code="open('/workspace/persisted.txt','w').write('STILL_HERE')",
            timeout_s=30,
        )
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="print(open('/workspace/persisted.txt').read())",
            timeout_s=30,
        )
        assert "STILL_HERE" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_python_variables_do_not_survive_across_exec(runtime: SandboxRuntime) -> None:
    """反过来:变量不保持——两个实现的每次 ``exec`` 都是一个全新的
    ``python -E -P`` 子进程(``runner.py``)/ ``commands.run`` 调用(E2B),不是
    同一个长驻解释器里的连续求值。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c7")
    try:
        await runtime.exec(sandbox_id=sid, code="X = 42", timeout_s=30)
        outcome = await runtime.exec(sandbox_id=sid, code="print('X' in dir())", timeout_s=30)
        assert "False" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_user_site_survives_to_the_next_exec(runtime: SandboxRuntime) -> None:
    """PR-C #2 —— user site 必须在 exec 子进程的 ``sys.path`` 上。

    第一步把模块文件落进 ``site.getusersitepackages()``(镜像 HOME=/home/agent,
    可写),第二步全新子进程 import 它 —— 等价于「pip install --user 之后
    下一次 exec import 得到」,但不依赖网络。旧旗标 ``-I``(含 ``-s``)下
    第二步必失败。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c13")
    try:
        seeded = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import pathlib, site\n"
                "d = pathlib.Path(site.getusersitepackages())\n"
                "d.mkdir(parents=True, exist_ok=True)\n"
                "(d / 'ew_contract_usersite.py').write_text(\"MARK = 'usersite-ok'\")\n"
                "print('seeded', d)\n"
            ),
            timeout_s=30,
        )
        assert seeded.exit_code == 0, seeded.stderr
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import ew_contract_usersite; print(ew_contract_usersite.MARK)",
            timeout_s=30,
        )
        assert outcome.exit_code == 0, outcome.stderr
        assert "usersite-ok" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_sys_path_excludes_cwd_and_script_dir(runtime: SandboxRuntime) -> None:
    """PR-C #2 —— ``-P``:cwd(supervisor 的 ``-c`` 模式)与脚本目录
    (云后端的 /tmp 脚本模式)都不得进 ``sys.path``,否则 LLM 落在
    /workspace 或 /tmp 的文件会遮蔽 stdlib。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c14")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import sys; print(repr(sys.path))", timeout_s=30
        )
        assert outcome.exit_code == 0, outcome.stderr
        paths = ast.literal_eval(outcome.stdout.strip())
        assert "" not in paths, paths
        assert "/tmp" not in paths, paths  # noqa: S108 — membership check on sys.path, not a filesystem write target
        assert "/workspace" not in paths, paths
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_pip_user_install_then_import(runtime: SandboxRuntime) -> None:
    """PR-C #2 的端到端本尊:``pip install --user`` 之后,下一次 exec 的
    全新子进程要 import 得到。选 sortedcontainers(纯 py、无依赖、镜像
    requirements 未收);第一步先断言它当前 import 不到,防镜像哪天把它
    收编后本用例退化成空转。走真网络,超时给足。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c15")
    try:
        installed = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import importlib.util, subprocess, sys\n"
                "assert importlib.util.find_spec('sortedcontainers') is None, "
                "'already baked into the image — pick another probe package'\n"
                "r = subprocess.run([sys.executable, '-m', 'pip', 'install', '--user',\n"
                "                    '--quiet', '--no-input', 'sortedcontainers==2.4.0'])\n"
                "print('pip-rc', r.returncode)\n"
            ),
            timeout_s=240,
        )
        assert installed.exit_code == 0, installed.stderr
        assert "pip-rc 0" in installed.stdout, installed.stdout
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import sortedcontainers; print('import-ok', sortedcontainers.__version__)",
            timeout_s=30,
        )
        assert outcome.exit_code == 0, outcome.stderr
        assert "import-ok 2.4.0" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_sandbox_nas_mount_shares_workspace_across_two_sandboxes() -> None:
    """sandbox migration wave 2 Task 7 —— e2b NAS 挂载档(brief Step 3)。

    Not parametrized over ``runtime`` — this is an ``agent_sandbox``-only
    property (the docker supervisor backend's workspace persistence is
    already covered end to end by ``test_workspace_files_survive_across_exec``
    and separately by ``test_workspace_store_contract.py``; there is no
    supervisor-side equivalent of "mount the *same* NAS subtree into two
    independently created sandboxes" to parametrize against).

    Proves the NAS mount, not just the docker-volume-equivalent "same sandbox,
    two execs" persistence: write from a **first** sandbox, force it to be
    genuinely destroyed (not the routine ``release`` that keeps a warm session
    alive — reusing the same warm sandbox for the second ``acquire`` would
    prove nothing about the mount, since the file would just still be sitting
    on that one sandbox's own view of ``/workspace``), then ``acquire`` a
    **second**, independently created sandbox for the same ``(tenant, user)``
    and read the file back purely via its own ``exec`` — never via a local
    filesystem read of the NAS tree from this test process, since a GitHub
    Actions runner (or any machine without the ``workspace-nas`` PVC mounted)
    has no NFS route to it. Two distinct sandbox ids is the actual proof that
    authority lives on the NAS, not on either sandbox's local disk. Cleans up
    the probe file via a third ``exec`` before destroying the second sandbox
    (this suite writes real files onto the shared test-cluster NAS volume —
    see task-1-report.md § 7 for why leftover probe residue there is a real,
    previously-flagged annoyance, not a hypothetical one).
    """
    runtime = _agent_sandbox_runtime_with_workspace_mount()
    tenant_id, user_id = uuid4(), uuid4()

    sandbox_1 = await runtime.acquire(tenant_id=tenant_id, thread_id="mount-1", user_id=user_id)
    try:
        outcome = await runtime.exec(
            sandbox_id=sandbox_1,
            code="open('/workspace/contract-probe.txt', 'w').write('NAS_SHARED_OK')",
            timeout_s=30,
        )
        assert outcome.exit_code == 0, outcome.stderr
    finally:
        # 真 destroy,不是 release —— release 对带 user_id 的沙箱是保温
        # (下一次 acquire 会 connect 回同一个沙箱),那样"第二个沙箱"其实
        # 是同一个,证明不了任何跨沙箱共享的东西。
        await runtime.destroy(sandbox_id=sandbox_1, reason="contract-test-mount-1")

    sandbox_2 = await runtime.acquire(tenant_id=tenant_id, thread_id="mount-2", user_id=user_id)
    assert sandbox_2 != sandbox_1, (
        "acquire 为同一个 (tenant, user) 返回了同一个 sandbox_id —— 第一个沙箱"
        "没有被真的 destroy 掉,读到同内容不能证明跨沙箱共享(有可能只是同一"
        "个热会话的第二次 exec)。"
    )
    try:
        outcome = await runtime.exec(
            sandbox_id=sandbox_2,
            code="print(open('/workspace/contract-probe.txt').read())",
            timeout_s=30,
        )
        assert "NAS_SHARED_OK" in outcome.stdout
    finally:
        # NAS 清理 —— 探针文件是这条测试写到共享测试集群 NAS 卷上的真实
        # 残留,通过 exec 删(不经本地文件系统:同上,CI runner 没有 NFS
        # 路由),再 destroy 第二个沙箱。
        await runtime.exec(
            sandbox_id=sandbox_2,
            code="import os; os.remove('/workspace/contract-probe.txt')",
            timeout_s=30,
        )
        await runtime.destroy(sandbox_id=sandbox_2, reason="contract-test-mount-2")


def _runner_py_constants() -> dict[str, int]:
    """``infra/sandbox-image/runner.py`` 里的模块级 int 常量。

    用 ``ast`` 解析而不是 import:那是镜像代码(沙箱容器里的 PID 1),既不在
    ``sys.path`` 上,也没有理由为读三个字面量把它执行进测试进程。
    """
    runner = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "runner.py"
    assert runner.is_file(), f"沙箱镜像 runner.py 不在预期位置:{runner}"
    values: dict[str, int] = {}
    for node in ast.parse(runner.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, int) or isinstance(node.value.value, bool):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                values[target.id] = node.value.value
    return values


def _runner_py_exec_flags() -> list[str]:
    """ast 抠 runner.py subprocess argv 里 sys.executable 与 "-c" 之间的旗标。"""
    runner = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "runner.py"
    tree = ast.parse(runner.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        if isinstance(head, ast.Attribute) and head.attr == "executable":
            flags = []
            for elt in node.elts[1:]:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                    break
                if elt.value == "-c":
                    return flags
                flags.append(elt.value)
    raise AssertionError("runner.py 的 subprocess argv([sys.executable, ..., '-c', code])没找到")


def _supervisor_max_timeout_s() -> int:
    """supervisor 侧**真正决定** ``timeout_s`` 上界的那个数。

    不是 ``runner.py`` 的 ``MAX_TIMEOUT_S``:HTTP 入口的 pydantic schema
    (``ExecRequest.timeout_s`` 的 ``Field(gt=0, le=300)``)先把超界请求拦成
    422,runner.py 那个 clamp 在 HTTP 路径上够不着(模块 docstring 差异其二
    已经把这条写清楚了)。从 ``model_fields`` 的 ``annotated_types.Le`` 里读,
    不重述字面量——重述就等于又造一份会漂的副本。
    """
    from annotated_types import Le

    from sandbox_supervisor.schemas import ExecRequest

    bounds = [m.le for m in ExecRequest.model_fields["timeout_s"].metadata if isinstance(m, Le)]
    assert len(bounds) == 1, f"ExecRequest.timeout_s 的上界约束不再是唯一一条 Le:{bounds}"
    return int(bounds[0])


def test_exec_contract_constants_match_the_sandbox_image() -> None:
    """``exec`` 契约点 1/2 的三个常量必须与 supervisor 侧**真正生效**的那份一致。

    端到端测不到的那部分由这里补上:clamp 的上/下界没有便宜的运行时观测口
    ——范围外取值在两个后端的处置本就不同(见模块 docstring 差异其二),而
    "300 秒真的会在第 300 秒被掐"这种用例要跑 5 分钟。所以钉常量。

    再审 Minor —— 每个值各自钉到**决定行为的那一处**,而不是一律钉
    ``runner.py``:

    * ``MAX_TIMEOUT_S`` → ``schemas.ExecRequest.timeout_s`` 的 ``le``。
      这条以前钉的是 ``runner.py`` 的同名常量,而按上一轮自己的发现,
      runner.py 那个 clamp 在 HTTP 路径上**根本够不着**(schema 先返 422)。
      于是有人把 ``le`` 从 300 改成 600 时,两个后端的实际上界立刻分叉,而这
      道闸照样绿——闸是摆设。现在钉 ``le``,改它必红。
    * ``MAX_OUTPUT_CHARS`` → 仍钉 ``runner.py``:截断确实是 runner.py 干的,
      结果经 HTTP 原样回传,那才是决定行为的地方。
    * ``DEFAULT_TIMEOUT_S`` → 仍钉 ``SandboxSupervisorSettings.default_timeout_s``
      (``timeout_s=None`` 时 supervisor 用的是设置项),并额外与 runner.py
      的同名默认值比一道——这个值两处都可能生效。

    与 ``test_idle_ttl_matches_supervisor_default`` 同理,刻意不打
    ``integration`` marker:只比较字面量,不连任何真实环境。
    """
    from orchestrator.tools.agent_sandbox import (
        DEFAULT_TIMEOUT_S,
        MAX_OUTPUT_CHARS,
        MAX_TIMEOUT_S,
        SANDBOX_PYTHON_FLAGS,
    )
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    runner = _runner_py_constants()
    supervisor_max = _supervisor_max_timeout_s()
    assert MAX_TIMEOUT_S == supervisor_max, (
        f"AgentSandboxClient.exec 把 timeout_s clamp 到 {MAX_TIMEOUT_S}s,而 supervisor 的"
        f" HTTP 入口(schemas.ExecRequest.timeout_s 的 le)只接受到 {supervisor_max}s"
        " —— 同一次 exec 请求在两个后端会拿到不同待遇。注意这里刻意不比 runner.py 的"
        " MAX_TIMEOUT_S:那个 clamp 在 HTTP 路径上够不着(schema 先返 422);runner 侧"
        " 的值由下面的 PR-C #9 断言单独钉住。"
    )
    assert MAX_OUTPUT_CHARS == runner["MAX_OUTPUT_CHARS"], (
        f"AgentSandboxClient 的输出上限 {MAX_OUTPUT_CHARS} 与 infra/sandbox-image/runner.py"
        f" 真正执行截断的 MAX_OUTPUT_CHARS={runner['MAX_OUTPUT_CHARS']} 已经不一致。"
    )
    supervisor_default = SandboxSupervisorSettings.model_fields["default_timeout_s"].default
    assert DEFAULT_TIMEOUT_S == supervisor_default == runner["DEFAULT_TIMEOUT_S"], (
        f"timeout_s=None 时 agent_sandbox 用 {DEFAULT_TIMEOUT_S}s,"
        f" supervisor 用 settings.default_timeout_s={supervisor_default}s,"
        f" runner.py 自己的默认值是 {runner['DEFAULT_TIMEOUT_S']}s —— 三者必须一致。"
    )
    # PR-C #9 — runner 的 MAX_TIMEOUT_S 此前没有闸钉着,改一边就静默分叉。
    assert MAX_TIMEOUT_S == runner["MAX_TIMEOUT_S"], (
        f"MAX_TIMEOUT_S 漂移:contract={MAX_TIMEOUT_S} runner.py={runner['MAX_TIMEOUT_S']}"
    )
    # PR-C #2 — 解释器旗标单源:runner argv 必须与 SANDBOX_PYTHON_FLAGS 一致。
    assert _runner_py_exec_flags() == list(SANDBOX_PYTHON_FLAGS), (
        f"exec 旗标漂移:runner.py={_runner_py_exec_flags()} contract={list(SANDBOX_PYTHON_FLAGS)}"
    )


def test_egress_token_ttl_matches_supervisor_default() -> None:
    """再审 Important-3 追加的第三道漂移闸(手法同下面那条)。

    出网 token 只在 ``create(envs=...)`` 送一次——``connect`` 没有 ``envs``
    形参(e2b 2.24.0,已核对源码),所以热会话重连**不会**换发新 token。I-4
    之前这不成问题:不传 ``timeout`` 的沙箱 300 秒就被平台 kill,每次复用都是
    重建 + 新 token。I-4 给 ``connect`` 也传了 ``timeout``,热会话可以无限期
    活着,于是"token 必须活得比沙箱久"从一句废话变成一条真约束——活不过就是
    出网一律 407 且没有自愈路径。

    钉到 supervisor 的同名默认值上,而不是钉一个孤立的数字:两个后端服务同一
    个 credential-proxy、共享同一个密钥、铸同一种 token,同一个 agent 在两边
    理应拿到同样的待遇。这也顺带说明为什么 24h 不是新的暴露面——supervisor
    今天就在按这个值铸。

    刻意不打 ``integration`` marker:只比较两个 Python 常量。
    """
    from orchestrator.tools.agent_sandbox import AgentSandboxClient
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    supervisor_default = SandboxSupervisorSettings.model_fields["egress_token_ttl_s"].default
    client_default = AgentSandboxClient.__dataclass_fields__["egress_token_ttl_s"].default
    assert client_default == supervisor_default, (
        f"AgentSandboxClient 铸的出网 token 活 {client_default}s,docker supervisor 铸的活"
        f" {supervisor_default}s —— 同一个 agent 在两个后端拿到不同待遇。"
        " 调低这个值之前先确认热会话的最长存活期仍然短于它(connect 不重发 token,"
        " 活过期就是出网一律 407 且没有自愈路径)。"
    )

    # 全分支终审 I-1:``build_sandbox_runtime``(control_plane/runtime.py)现在
    # 总是显式传 ``settings.sandbox_egress_token_ttl_s`` 给 ``AgentSandboxClient``
    # 的构造函数——上面 ``client_default`` 那条比较的 dataclass 默认值再也到不了
    # 生产,只在没有 Settings 注入的裸构造(比如这份契约测试自己)里才会用到。
    # 权威值变成了 control-plane 的 ``Settings.sandbox_egress_token_ttl_s``,这里
    # 补第三道钉子,否则调低这个字段的默认值不会被任何测试发现。
    from control_plane.settings import Settings

    cp_default = Settings.model_fields["sandbox_egress_token_ttl_s"].default
    assert cp_default == supervisor_default, (
        f"control-plane 的 Settings.sandbox_egress_token_ttl_s 默认值({cp_default}s)"
        f" 与 docker supervisor 的 egress_token_ttl_s 默认值({supervisor_default}s)"
        " 不一致 —— runtime.py 现在总是显式传 settings.sandbox_egress_token_ttl_s"
        " 给 AgentSandboxClient,这个字段才是云后端实际铸出的 TTL,AgentSandboxClient"
        " 自己的 dataclass 默认值已经到不了生产。"
    )


#: 仓库根 —— 本文件在 services/orchestrator/tests/ 下,上溯三级。
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 两侧共享的出网配置项。左=control-plane ``Settings`` 字段名,
#: 右=sandbox-supervisor ``SandboxSupervisorSettings`` 字段名。
_SHARED_EGRESS_FIELDS = [
    ("sandbox_egress_token_secret", "egress_token_secret"),
    ("sandbox_egress_token_ttl_s", "egress_token_ttl_s"),
]


def test_shared_egress_settings_resolve_to_the_same_env_var() -> None:
    """两侧的同名配置必须解析到**同一个**环境变量名。

    这是「比真实配置」的结构性保证:名字一样,部署里改一次两个后端一起改;
    名字一旦分叉(比如有人给 control-plane 那侧改了字段名),运维设一个变量
    只会生效一边,而比默认值的闸完全看不见这种劈叉。
    """
    from control_plane.settings import Settings
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    cp_prefix = Settings.model_config["env_prefix"]
    sup_prefix = SandboxSupervisorSettings.model_config["env_prefix"]

    for cp_field, sup_field in _SHARED_EGRESS_FIELDS:
        assert cp_field in Settings.model_fields, f"control-plane 少了 {cp_field}"
        assert sup_field in SandboxSupervisorSettings.model_fields, f"supervisor 少了 {sup_field}"
        cp_env = f"{cp_prefix}{cp_field}".upper()
        sup_env = f"{sup_prefix}{sup_field}".upper()
        assert cp_env == sup_env, (
            f"control-plane 的 {cp_field} 读 {cp_env},supervisor 的 {sup_field} 读"
            f" {sup_env} —— 两个名字不一样,部署里设一个只会生效一边。"
        )


def test_compose_never_sets_a_shared_egress_var_for_only_one_service() -> None:
    """docker-compose 里这些变量要么两边都设、要么都不设,且取值表达式相同。

    compose 是唯一两个服务同时在跑的地方(k8s 上没有 sandbox-supervisor
    部署)。control-plane 走 ``x-control-plane-base`` 锚点,supervisor 有自己的
    environment 块 —— 只给一边设,就是两个后端铸出不同待遇的 token,而且
    「默认值一致」的闸看不见。
    """
    compose = (_REPO_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    # 锚点块:从 `x-control-plane-base:` 到下一个顶格键;supervisor 块:从
    # `  sandbox-supervisor:` 到下一个同级服务键。
    cp_block = re.search(r"^x-control-plane-base:.*?(?=^\S)", compose, re.S | re.M)
    sup_block = re.search(r"^  sandbox-supervisor:.*?(?=^  \S)", compose, re.S | re.M)
    assert cp_block is not None, "compose 里找不到 x-control-plane-base 锚点"
    assert sup_block is not None, "compose 里找不到 sandbox-supervisor 服务块"

    from sandbox_supervisor.settings import SandboxSupervisorSettings

    prefix = SandboxSupervisorSettings.model_config["env_prefix"]
    for _cp_field, sup_field in _SHARED_EGRESS_FIELDS:
        var = f"{prefix}{sup_field}".upper()
        cp_line = re.search(rf"^\s*{var}:\s*(\S.*)$", cp_block.group(0), re.M)
        sup_line = re.search(rf"^\s*{var}:\s*(\S.*)$", sup_block.group(0), re.M)
        assert (cp_line is None) == (sup_line is None), (
            f"{var} 只在一边设了(control-plane={cp_line is not None},"
            f" supervisor={sup_line is not None})—— 两个后端会拿到不同的值。"
        )
        if cp_line is not None and sup_line is not None:
            assert cp_line.group(1).strip() == sup_line.group(1).strip(), (
                f"{var} 两边取值表达式不同:control-plane={cp_line.group(1).strip()!r}"
                f" vs supervisor={sup_line.group(1).strip()!r}"
            )

    # 全分支终审 M-4 —— 上面的循环只比 control-plane 锚点与 supervisor 块,
    # 从没看过真正的验证方:credential-proxy。它的 egress secret 键名不同
    # (``EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET``,不在 _SHARED_EGRESS_FIELDS
    # 那组"两侧同名"字段里),只改 credential-proxy 那一行,铸方(control-plane
    # / supervisor)与验方(proxy)就分家了——proxy 会拒掉云侧/supervisor 铸的
    # 每一个 token(见 x-control-plane-base 里那条注释),而这条测试此前对此
    # 完全失明。
    cred_proxy_block = re.search(r"^  credential-proxy:.*?(?=^  \S)", compose, re.S | re.M)
    assert cred_proxy_block is not None, "compose 里找不到 credential-proxy 服务块"

    sup_secret_var = f"{prefix}egress_token_secret".upper()
    sup_secret_line = re.search(rf"^\s*{sup_secret_var}:\s*(\S.*)$", sup_block.group(0), re.M)
    assert sup_secret_line is not None, f"{sup_secret_var} 在 supervisor 块里找不到了"
    cred_proxy_secret_line = re.search(
        r"^\s*EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET:\s*(\S.*)$",
        cred_proxy_block.group(0),
        re.M,
    )
    assert cred_proxy_secret_line is not None, (
        "compose 里 credential-proxy 块找不到 EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET"
    )
    assert cred_proxy_secret_line.group(1).strip() == sup_secret_line.group(1).strip(), (
        "credential-proxy 的 EXPERT_WORK_CRED_PROXY_EGRESS_TOKEN_SECRET="
        f"{cred_proxy_secret_line.group(1).strip()!r} 与铸 token 那一方(supervisor/"
        f"control-plane)的 {sup_secret_var}={sup_secret_line.group(1).strip()!r} 不一致"
        " —— proxy 会拒掉铸出来的每一个 token。"
    )


def test_idle_ttl_matches_supervisor_default() -> None:
    """独立审查追加的漂移检测(task-9-report.md § 11.4 Minor-2)。

    ``AgentSandboxClient.reap`` 的空闲 TTL 口径来自
    ``expert_work.persistence.sandbox_instance_store._IDLE_TTL_S``——一个
    硬编码镜像值,理由是该 package 不能反向依赖 ``sandbox-supervisor`` 服务
    的 ``Settings``(见该常量自己的 docstring)。但镜像的源头
    ``SandboxSupervisorSettings.session_idle_ttl_s`` 是可以用
    ``EXPERT_WORK_SANDBOX_SESSION_IDLE_TTL_S`` 环境变量覆盖的——运维改了那
    个值,``_IDLE_TTL_S`` 不会跟着变,此前没有任何东西会发现两边已经不一致。

    这条断言故意**不**打 ``@pytest.mark.integration``——比较两个包各自的
    Python 常量不需要 docker/E2B/Postgres 中的任何一个,理应在每一次
    ``pytest -q -m "not integration"`` 全仓扫描里都跑到。
    """
    from expert_work.persistence.sandbox_instance_store import _IDLE_TTL_S
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    supervisor_default = SandboxSupervisorSettings.model_fields["session_idle_ttl_s"].default
    assert _IDLE_TTL_S == supervisor_default, (
        f"AgentSandboxClient.reap 的空闲 TTL 镜像值(_IDLE_TTL_S={_IDLE_TTL_S}s)"
        f" 与 docker supervisor 的 session_idle_ttl_s 默认值({supervisor_default}s)"
        " 已经不一致 —— force=False 的空闲清扫在两个后端上会有不同的判定口径。"
        " 改了任一边的值,记得同步改另一边(或者把这条断言更新成有意为之的新值)。"
    )


def test_default_max_sandboxes_matches_supervisor_default() -> None:
    """#8 云后端租户配额的漂移闸 —— 手法同
    ``test_egress_token_ttl_matches_supervisor_default``。

    ``AgentSandboxClient._enforce_quota`` 在 ``tenant_quota`` 表未设
    ``sandboxes`` 行时落回 ``default_max_sandboxes``;docker supervisor 的
    ``_enforce_quota``(``supervisor.py:713-727``)落回同名 settings 字段。
    两个后端共用同一张 ``tenant_quota`` 表(平台级配额,不分后端),未设行
    时的缺省上限也理应一致,否则同一个租户在两个后端拿到不同的默认额度。

    刻意不打 ``integration`` marker:只比较两个 Python 常量。
    """
    from orchestrator.tools.agent_sandbox import AgentSandboxClient
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    assert (
        AgentSandboxClient.__dataclass_fields__["default_max_sandboxes"].default
        == SandboxSupervisorSettings.model_fields["default_max_sandboxes"].default
    )


def test_max_warm_age_leaves_room_under_the_egress_token_ttl() -> None:
    """#1b 的自愈闸只有在这条不等式成立时才真的自愈 —— 手法同
    ``test_platform_timeout_outlives_idle_ttl``(``test_agent_sandbox.py``):
    比较两个由同一处代码派生的数字,不连任何真实环境。

    ``AgentSandboxClient._max_warm_age_s()``(``egress_token_ttl_s // 2``)是
    "热会话必须死在 token 之前"这条约束唯一的强制手段 —— token 只在
    ``create(envs=...)`` 送一次、``connect`` 不重发(见 ``agent_sandbox.py``
    模块 docstring 再审 Important-3),活过 token 的会话出网一律 407 且没有
    任何自愈路径。这条闸如果不严格小于 TTL,#1b 想堵的洞会原样复活 —— 例如
    把除法系数从 ``// 2`` 改成 ``* 2``,年龄封顶就会晚于 token 过期才触发,
    热会话在被强制重建之前已经先撞上 407。

    第二条断言额外留出一次最长工具调用(``MAX_TIMEOUT_S``,exec 的 clamp
    上界)的余量:cap 命中的判定发生在 ``acquire`` 入口,cap 命中之后、
    重建完成之前,进行中的那次调用仍可能用旧沙箱跑到 ``MAX_TIMEOUT_S``——
    光是"cap < ttl"不够,cap 还必须比 ttl 早到足够让这次收尾调用也来得及
    在 token 过期前结束。
    """
    from orchestrator.tools.agent_sandbox import AgentSandboxClient
    from orchestrator.tools.sandbox_image_contract import MAX_TIMEOUT_S

    client = AgentSandboxClient(
        domain="gw.example.com",
        api_key="k",
        template="expert-work-sandbox",
        store=object(),  # type: ignore[arg-type]  # 方法不碰 store,见上方 docstring。
        egress_token_secret="s3cret",
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
    )

    assert client._max_warm_age_s() < client.egress_token_ttl_s, (
        f"年龄封顶 {client._max_warm_age_s()}s 必须严格小于出网 token TTL"
        f" {client.egress_token_ttl_s}s,否则热会话会先撞 407 才轮到强制重建"
        " —— #1b 想堵的洞原样复活。"
    )
    assert client._max_warm_age_s() + MAX_TIMEOUT_S < client.egress_token_ttl_s, (
        f"年龄封顶 {client._max_warm_age_s()}s 加一次最长工具调用"
        f"({MAX_TIMEOUT_S}s)必须仍然小于 token TTL {client.egress_token_ttl_s}s,"
        "否则 cap 命中之后、重建完成之前那次收尾调用可能跑到 token 过期。"
    )


def test_max_warm_age_leaves_room_at_the_ttl_floor() -> None:
    """全分支终审 I-3 —— 上一条测试只钉了**默认值**(24h)下不变式成立,没钉
    ``settings.py`` 的 ``sandbox_egress_token_ttl_s`` **下界本身**选得够不够高。
    运维可以把这个字段配到任意大于下界的值;下界选低了(比如本 PR 之前的
    ``gt=0``),照样能配出一个让上一条测试永远测不到、但在生产上让 #1b 的
    自愈闸失效的 TTL —— 症状是出网全量 407 且没有自愈路径
    (``docs/runbooks/control-plane.md`` 记的那个最难查的形态)。

    从 ``Settings.model_fields`` 读**实际配置的** ``Gt`` 下界,而不是重述字面量
    ——手法同 ``_supervisor_max_timeout_s``(本文件上方):重述就是又造一份会
    漂的副本,将来谁把下界调松都测不出来。这里故意反着来:直接拿"字段允许
    的最小值"喂给 ``AgentSandboxClient``,如果下界选得不够高,这条不变式会在
    这个最小值上先破——不用等到运维真的配了一个危险值。
    """
    from annotated_types import Gt

    from control_plane.settings import Settings
    from orchestrator.tools.agent_sandbox import AgentSandboxClient
    from orchestrator.tools.sandbox_image_contract import MAX_TIMEOUT_S

    field_info = Settings.model_fields["sandbox_egress_token_ttl_s"]
    bounds = [m.gt for m in field_info.metadata if isinstance(m, Gt)]
    assert len(bounds) == 1, f"sandbox_egress_token_ttl_s 的下界约束不再是唯一一条 Gt:{bounds}"
    minimum_ttl = int(bounds[0]) + 1

    client = AgentSandboxClient(
        domain="gw.example.com",
        api_key="k",
        template="expert-work-sandbox",
        store=object(),  # type: ignore[arg-type]  # 方法不碰 store,见上方 docstring。
        egress_token_secret="s3cret",
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
        egress_token_ttl_s=minimum_ttl,
    )

    assert client._max_warm_age_s() + MAX_TIMEOUT_S < client.egress_token_ttl_s, (
        f"字段允许的最小 TTL({minimum_ttl}s)下,年龄封顶 {client._max_warm_age_s()}s"
        f" 加一次最长工具调用({MAX_TIMEOUT_S}s)已经不小于 TTL 本身 —— 下界选低了,"
        " 运维配得出一个让 #1b 自愈闸失效的值,且默认值那道闸完全看不见。"
    )


# --------------------------------------------------- 生产必配项 ↔ 契约档配置 漂移闸
#
# W2 收尾「前提清扫」补的一道。这一波真栈复跑栽的第一跤就是这条缝:Task 9 之后
# ``EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME`` 在生产装配点变成必配,而这份契约档的
# fixture 的默认值还停在「不配 = 波 1 行为」的旧世界 —— 18 条 exec 用例齐炸
# ``cwd '/workspace' does not exist``。
#
# 为什么代码审查逮不到:肇事任务(Task 9,改镜像)的 diff 里根本没有 fixture 那
# 一行,它一个字符都没改。审查者读的是 diff,这条缝住在「未被改动、但被本次改动
# 作废」的代码里。真栈跑逮到了,但那是最贵最晚的一层。
#
# 这道闸把那份「散文前提」变成机器可检的:名字集合对不上就红,不用等任何基础设施。

#: 生产装配点每一个必配环境变量在**这份契约档**里的处置。值是理由,不是装饰 ——
#: 工厂以后多要一个变量,下面那条断言会红,而写这行值的人被迫做一次显式判断:
#: 契约档到底该要它,还是有理由不要。
_FIXTURE_ENV_DISPOSITION = {
    "EXPERT_WORK_SANDBOX_E2B_DOMAIN": "required —— _agent_sandbox_runtime 直接读,缺了 KeyError",
    "EXPERT_WORK_SANDBOX_E2B_API_KEY": "required —— 缺了 skip(整个 agent_sandbox 档没法跑)",
    "EXPERT_WORK_SANDBOX_E2B_TEMPLATE": "required —— _agent_sandbox_runtime 直接读,缺了 KeyError",
    "EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME": (
        "required —— 缺了 skip。镜像不再预建 /workspace(W2 Task 9),不挂 NAS "
        "就等于沙箱里没有 cwd;生产工厂也强制它,契约档没有理由去测一个生产里"
        "不存在的配置。"
    ),
    "EXPERT_WORK_WORKSPACE_NAS_ROOT": (
        "刻意不配 —— 这是「把 NAS 挂进 control-plane Pod」的那半边,GitHub runner "
        "对 NAS 没有 NFS 路由,配不了。缺了它 _prepare_workspace_mount 整段跳过,"
        "挂载点目录改由平台建(root:root 0755,集群实测),沙箱侧 "
        "AgentSandboxClient._chown_workspace_mount(方向变更前叫 "
        "_chmod_workspace_mount)那道兜底因此成为这一档唯一的"
        "权限来源 —— 也正是这一档真正在验的东西之一。"
        "**不只是少了 _chown_workspace_mount 兜底覆盖**:这个变量还是本次改动"
        "两条旗舰契约用例的唯一开关——"
        "test_written_file_is_readable_by_the_control_plane_identity 与 "
        "test_agent_sandbox_workspace_root_is_not_world_accessible 都在函数体内 "
        "os.environ.get 这个变量,未设直接 pytest.skip,不打任何 xfail/xskip 标"
        "记留痕。`grep -rn EXPERT_WORK_WORKSPACE_NAS_ROOT .github/` 目前无命中,"
        "即两条测试今天在任何 CI pipeline 里都不执行,只在人工连了真 NAS 的机器"
        "上跑得到——是 real-stack-only,不是本地/CI 也覆盖、只是少一层兜底断言。"
    ),
}


def test_contract_fixture_accounts_for_every_mandated_env() -> None:
    """生产工厂的必配项集合 == 这份契约档显式处置过的集合。

    不连任何真实环境(同 ``test_shared_egress_settings_resolve_to_the_same_env_var``
    的手法),所以每一次 ``pytest -m "not integration"`` 全仓扫描都跑得到 —— 这
    正是它想防的东西:别再靠一次 30 分钟的镜像构建 + 真栈跑来发现「契约档的配置
    形状和生产对不上了」。
    """
    from control_plane.runtime import AGENT_SANDBOX_REQUIRED_ENV

    mandated = set(AGENT_SANDBOX_REQUIRED_ENV)
    disposed = set(_FIXTURE_ENV_DISPOSITION)

    assert mandated == disposed, (
        f"生产工厂必配 {sorted(mandated)},契约档处置了 {sorted(disposed)} —— "
        f"未处置 {sorted(mandated - disposed)},多余 {sorted(disposed - mandated)}。"
        " 每多一项都要在 _FIXTURE_ENV_DISPOSITION 里显式决定:契约档要它,还是"
        " 有理由不要(把理由写下来)。"
    )
