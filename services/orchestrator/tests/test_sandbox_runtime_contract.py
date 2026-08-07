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


def _supervisor_runtime() -> SandboxRuntime:
    url = os.environ.get("EXPERT_WORK_SANDBOX_SUPERVISOR_URL")
    if not url:
        pytest.skip("EXPERT_WORK_SANDBOX_SUPERVISOR_URL 未设 —— supervisor 契约档跳过")
    from orchestrator.tools.sandbox import HTTPSupervisorRuntime

    return HTTPSupervisorRuntime(base_url=url)


def _agent_sandbox_runtime(*, workspace_pv_name: str = "") -> SandboxRuntime:
    """``workspace_pv_name`` —— sandbox migration wave 2 Task 7 e2b NAS 挂载档
    (:func:`_agent_sandbox_runtime_with_workspace_mount`)专用的额外配置;
    默认空串保持这个函数对既有调用方(``runtime`` fixture)零行为变化——
    ``AgentSandboxClient.workspace_pv_name`` 未配时 ``_create`` 完全不带
    ``metadata`` 键(见该字段 docstring),与波 1 逐字相同。
    """
    api_key = os.environ.get("EXPERT_WORK_SANDBOX_E2B_API_KEY")
    if not api_key:
        pytest.skip("E2B 凭据未设 —— agent_sandbox 契约档跳过")
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
    )


def _agent_sandbox_runtime_with_workspace_mount() -> SandboxRuntime:
    """:func:`_agent_sandbox_runtime` 加配 ``workspace_pv_name`` —— e2b NAS 挂载
    档(``test_agent_sandbox_nas_mount_shares_workspace_across_two_sandboxes``)
    专用。E2B 凭据 / 真 Postgres 两个既有前提检查仍由
    :func:`_agent_sandbox_runtime` 做;这里在它之前多加一层
    ``EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME`` 的 skip 闸。

    这一档即便三个环境变量全设,今天大概率仍然跑不通,原因不止"云上
    SandboxSet 还在用旧镜像"(task-9-report.md,Task 8 换 tag 前):
    task-1-report.md 问题一记录了更底层的一条——这个集群的 ACS Agent Sandbox
    动态存储挂载(``csi-volume-config``)需要"特权容器 + hostPath
    /var/run/csi"安全豁免,工单尚未批复,``create(metadata=...)`` 今天在
    平台侧直接 500。这些都是留给 Task 8 之后处理的基础设施缺口,不是这份
    契约测试要绕过或弱化断言去伪造绿的目标(brief 明确要求"env 设了就跑,
    不要为了让它绿而弱化断言")——一旦工单批复 + 镜像换新,这条测试原样
    就是验收开关。
    """
    pv_name = os.environ.get("EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME")
    if not pv_name:
        pytest.skip("EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME 未设 —— e2b NAS 挂载档跳过")
    return _agent_sandbox_runtime(workspace_pv_name=pv_name)


@pytest.fixture(params=["supervisor", "agent_sandbox"])
def runtime(request: pytest.FixtureRequest) -> SandboxRuntime:
    return {"supervisor": _supervisor_runtime, "agent_sandbox": _agent_sandbox_runtime}[
        request.param
    ]()


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
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c8")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import os; print(os.getcwd())", timeout_s=30
        )
        assert outcome.stdout.strip() == "/workspace"
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
