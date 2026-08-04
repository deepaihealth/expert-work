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

marker 策略:7 条契约测试各自单独打 ``@pytest.mark.integration``(真连
基础设施,任一环境变量未设就 skip),但文件末尾的 TTL 漂移断言
(``test_idle_ttl_matches_supervisor_default``)刻意**不**打这个 marker
——它只是比较两个包各自定义的 Python 常量,不连任何真实环境,理应在每一次
``pytest -q -m "not integration"`` 全仓扫描里就跑到,而不是只在偶尔真连
测试集群的场次才被验证到(那正是它想防止的"没有任何东西会发现漂移")。

已知的一条**不可弥合**差异,写清楚是为了不让后来者当 bug 顺手"修"掉:
超时路径的输出。``runner.py``(docker supervisor 里的 PID 1)包了一层
``subprocess.run``,子进程被 SIGKILL 之前已经写出的部分 stdout/stderr 仍
读得到,``_cap()`` 后原样放进响应。E2B 这边追到 SDK 源码(而非公开文档,
见 task-8-report.md § 1)确认 ``AsyncCommandHandle.wait()``
(``e2b/sandbox_async/commands/command_handle.py:127-137,172-183``)超时时
直接 ``raise self._iteration_exception``,异常对象本身不携带任何已产生的
输出——这一层压根没有把它们塞进去的代码路径,不是文档没写全。
``test_exec_timeout_contract`` 因此只断言 ``exit_code``/``timed_out``,不
断言 stdout/stderr 内容。
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from orchestrator.tools.sandbox import SandboxRuntime


def _supervisor_runtime() -> SandboxRuntime:
    url = os.environ.get("EXPERT_WORK_SANDBOX_SUPERVISOR_URL")
    if not url:
        pytest.skip("EXPERT_WORK_SANDBOX_SUPERVISOR_URL 未设 —— supervisor 契约档跳过")
    from orchestrator.tools.sandbox import HTTPSupervisorRuntime

    return HTTPSupervisorRuntime(base_url=url)


def _agent_sandbox_runtime() -> SandboxRuntime:
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
    )


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
async def test_seed_files_land_in_workspace(runtime: SandboxRuntime) -> None:
    sid = await runtime.acquire(
        tenant_id=uuid4(),
        thread_id="c5",
        seed_files=(("seeded.txt", b"SEED_CONTENT"),),
    )
    try:
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="print(open('/workspace/seeded.txt').read())",
            timeout_s=30,
        )
        assert "SEED_CONTENT" in outcome.stdout
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
    ``python -I`` 子进程(``runner.py``)/ ``commands.run`` 调用(E2B),不是
    同一个长驻解释器里的连续求值。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c7")
    try:
        await runtime.exec(sandbox_id=sid, code="X = 42", timeout_s=30)
        outcome = await runtime.exec(sandbox_id=sid, code="print('X' in dir())", timeout_s=30)
        assert "False" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


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
