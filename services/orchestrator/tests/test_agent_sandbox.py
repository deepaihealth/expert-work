"""AgentSandboxClient —— E2B SDK 实现的 SandboxRuntime(波 1 Task 7/8/9)。

SDK 用假件替身:真实 SDK 调用在契约测试(Task 10)与端到端(Task 11)覆盖。

假件对派发方 task-7-brief.md 给的版本做了几处改动,均在 task-7-report.md
记录了理由,概述:

* ``FakeFiles.write`` 加 ``user`` 关键字参数并计入 ``written`` —— 探针报告
  实测 ``files.write`` 必须传 ``user="agent"`` 才不炸 ``AuthenticationException``
  (E2B 默认用户 ``user`` 在我们的沙箱镜像里不存在);既然这是任务里点名的
  必须修正项,断言就该验证它真的被传了,而不是只让假件"能吞下"这个参数。
* ``FakeSdk.connect`` 加 ``**kwargs`` —— 真实实现对 ``connect`` 也显式传
  ``domain=`` / ``api_key=``(与 ``create`` 同理,见 task-7-report.md),假件
  需要能吞下这些多余关键字参数。
* ``FakeSdk.create`` 加 ``create_fails`` 开关(审查 Critical-2)—— 覆盖
  "claim_warm 已经提交了 CAS 行,但 sdk.create() 本身失败"这条此前完全没
  测过的路径。
* ``FakeInstanceStore`` 加 ``get_container_id``——brief Step 6 原文就说明
  "FakeInstanceStore 跟着加",不是遗漏;``claim_warm`` 的返回类型 + "赢家
  还在创建中则 raise" 改成与两个生产 store(SQL/内存)同语义(审查
  Important-3/4,详见类 docstring);再加 ``drop_warm_fails`` 开关(二审
  Critical)—— 覆盖"CAS 回滚本身也失败"这条路径。

Task 9(``reap``)对 task-9-brief.md 草稿的一处偏离,记在这里:草稿的
``test_reap_ignores_sandboxes_not_ours`` 让 ``FakeSdk`` 加 ``list()`` +
``foreign: list[str]`` 字段,模拟"E2B 账号下还有别的沙箱"。但顶层任务说明
定的最终设计是"以 ``sandbox_instance`` 表为准,完全不问 SDK 的
``list()``"——``reap()`` 的实现从头到尾没有一处调用 ``sdk.list()``,那条
草稿测试无论 ``foreign`` 是否存在断言都成立,是摆设,不是真的在验证"忽略
账号里的其它沙箱"这件事。这里改成 ``FakeSdk`` 压根不提供 ``list()``
方法(真退回到"按 SDK 列表拆"的实现会在任何 reap 测试里立刻
``AttributeError``,而不是被一个看似相关实则测不到东西的测试静默放过)、
``FakeInstanceStore`` 加 ``idle_sandbox_ids: set[UUID]`` 测试缝
(``list_active(only_idle=True)`` 只返回这里列出的 id)——覆盖
``AgentSandboxClient.reap`` 把 ``force`` 正确翻给 ``list_active`` 的
``only_idle=not force`` 这条真实存在的逻辑;真实的 TTL/时间戳判定语义是
``SqlSandboxInstanceStore.list_active`` 的职责,由
``test_sql_sandbox_instance_store.py`` 的容器集成测覆盖,这里不重复建模。

Task 9 独立审查 Important-1/2 追加(task-9-report.md 有完整记录):
``FakeInstanceStore`` 加 ``stuck_creating_sandbox_ids: set[UUID]`` 测试缝
(``list_stuck_creating()`` 只返回这里列出、且仍在 ``rows`` 里、
``container_id`` 仍未回填的 id)——验证 ``reap(force=True)`` 会清掉编排
进程死于 ``claim_warm``/``set_container_id`` 两次写之间的孤儿、
``force=False`` 不会;同理不建模真实的 ``acquired_at`` 阈值,那是容器集成
测的职责。再加 ``touched: list[UUID]``,记录每次
``touch_and_get_container_id`` 被调用的 sandbox_id——验证 ``exec()`` 真的
在推进 ``last_used_at``。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpcore
import pytest
from e2b import CommandExitException, TimeoutException

from expert_work.persistence import SANDBOX_AGENTS_ROOT, SANDBOX_SKILLS_ROOT
from expert_work.persistence.sandbox_instance_store import (
    _STUCK_CREATE_TTL_S,
    InMemorySandboxInstanceStore,
    _missing_row_message,
)
from orchestrator.tools import agent_sandbox as agent_sandbox_module
from orchestrator.tools.agent_sandbox import (
    _SANDBOX_TIMEOUT_S,
    _WARM_AGE_DESTROY_REASON,
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_S,
    SANDBOX_EXEC_USER,
    SANDBOX_IMAGE_ENV,
    WORKSPACE_ROOT,
    AgentSandboxClient,
)
from orchestrator.tools.nas_workspace_store import workspace_deleted_marker
from orchestrator.tools.sandbox import EgressContext, SandboxSupervisorError
from orchestrator.tools.sandbox_instance_store import SandboxInstanceStore


@dataclass
class FakeCommands:
    """Task 8 加 ``user`` 关键字参数并计入 ``calls`` —— 同 ``FakeFiles.write`` 的理由(见类
    docstring 顶部):探针报告实测 ``commands.run`` 必须传 ``user="agent"``
    才不炸 ``AuthenticationException``,断言就该验证它真的被传了,不是只让
    假件"能吞下"这个参数。

    全分支终审 Important-2 起 ``calls`` 是 4 元组
    ``(cmd, timeout, user, cwd)`` —— ``cwd`` 与 ``user`` 同理:envd 派生的
    进程不继承镜像的 ``WORKDIR``,不显式传就落在 ``/home/agent``,断言要能
    看见它真的被传了。

    ``run_error`` 是 Task 8 加的第二个测试缝:非 ``None`` 时 ``run`` 直接
    ``raise`` 它而不是返回假结果——覆盖"超时"/"非零退出码"两条 E2B 特有的
    异常路径(``TimeoutException`` / ``CommandExitException``),不需要靠
    "整个方法换掉"这种更粗糙的猴子补丁。
    """

    calls: list[tuple[str, int | None, str | None, str | None]] = field(default_factory=list)
    #: spec 决策 10 — the ``envs`` kwarg passed to each ``run`` call, kept out
    #: of ``calls`` (same reason as ``egress_calls``/``exec_agent_keys`` in
    #: ``orchestrator.tools.sandbox``): existing 4-tuple assertions on
    #: ``calls`` stay unchanged.
    envs_calls: list[dict[str, str] | None] = field(default_factory=list)
    result_stdout: str = ""
    result_stderr: str = ""
    result_exit: int = 0
    run_error: BaseException | None = None

    async def run(
        self,
        cmd: str,
        timeout: int | None = None,
        *,
        user: str | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
    ):
        self.calls.append((cmd, timeout, user, cwd))
        self.envs_calls.append(envs)
        if self.run_error is not None:
            raise self.run_error
        return type(
            "R",
            (),
            {
                "stdout": self.result_stdout,
                "stderr": self.result_stderr,
                "exit_code": self.result_exit,
            },
        )()


@dataclass
class FakeFiles:
    written: list[tuple[str, bytes | str, str | None]] = field(default_factory=list)
    #: 全分支终审 Important-6 测试缝 —— 非 None 时 ``write`` 直接抛它。覆盖
    #: "create() 已经返回、沙箱已经在跑,但 set_container_id 还没落库"这段
    #: 窗口里的失败。默认放一个真的 e2b 异常类型,顺带验证它被归一成
    #: SandboxSupervisorError(§ 6.5 统一错误契约)。
    write_error: BaseException | None = None

    async def write(self, path: str, data: bytes | str, *, user: str | None = None) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written.append((path, data, user))


@dataclass
class FakeSandbox:
    sandbox_id: str = "sbx-1"
    killed: bool = False
    commands: FakeCommands = field(default_factory=FakeCommands)
    files: FakeFiles = field(default_factory=FakeFiles)
    envs: dict[str, str] = field(default_factory=dict)

    async def kill(self) -> None:
        self.killed = True


@dataclass
class FakeSdk:
    """替身 SDK —— 记录 create/connect 调用。"""

    created: list[dict] = field(default_factory=list)
    connected: list[str] = field(default_factory=list)
    #: 全分支终审 Important-4 —— connect 也要显式传 timeout(SDK 语义"只延长
    #: 不缩短"),记下 kwargs 才能断言它真的被传了。
    connect_kwargs: list[dict] = field(default_factory=list)
    sandbox: FakeSandbox = field(default_factory=FakeSandbox)
    connect_fails: bool = False
    #: 审查 Critical-2 —— 覆盖 "CAS 行已提交但 create() 本身失败" 这条路径。
    create_fails: bool = False
    #: 再审 Important-1 测试缝 —— ``create()`` / ``connect()`` **返回之前**跑
    #: 一段别的协程。这是唯一能精确构造出"A 还卡在这一步里,B 已经在同一个
    #: ``(tenant, user)`` 上往前走了"这类交错的地方,而清理误伤别人的行只在
    #: 这类交错下才看得见:C-1 的接管窗口正是"行已提交、container_id 还没回
    #: 填",在真实代码里就是 ``create()`` 的执行期间;重连失败那条分支的窗口
    #: 则是 ``connect()`` 的执行期间。
    on_create: Callable[[], Awaitable[None]] | None = None
    on_connect: Callable[[], Awaitable[None]] | None = None

    async def create(self, **kwargs):
        self.created.append(kwargs)
        if self.create_fails:
            raise RuntimeError("sandbox create failed")
        if self.on_create is not None:
            await self.on_create()
        return self.sandbox

    async def connect(self, sandbox_id: str, **kwargs):
        self.connected.append(sandbox_id)
        self.connect_kwargs.append(kwargs)
        if self.on_connect is not None:
            await self.on_connect()
        if self.connect_fails:
            raise RuntimeError("sandbox gone")
        return self.sandbox


@dataclass
class FakeInstanceStore:
    """sandbox_instance 表的替身 —— CAS 语义由 claim_warm 表达。

    ``warm`` 存的是赢家那一行**真实存在**的 sandbox_id(不是 container_id)
    ——``claim_warm`` 输家分支要把这个 id 连同 container_id 一起返回给调用
    方(审查 Important-3:``acquire`` 自己在函数开头新铸的 uuid4() 从未插
    入任何行,如果原样返回它,后续 ``destroy()`` 会对着一个不存在的行静默
    no-op)。"赢家已占坑但还在创建中(container_id 仍是空)"时 raise,与两
    个生产 store(SQL/内存)同语义(审查 Important-4)。
    """

    warm: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    rows: dict[UUID, dict] = field(default_factory=dict)
    #: 二审 Critical —— 覆盖 "回滚本身也失败" 这条路径。再审 Important-1 之
    #: 前这个开关叫 ``drop_warm_fails``:回滚走的那个方法已经删了(按
    #: ``(tenant, user)`` 坐标定位会误伤 C-1 的接管者),现在统一走
    #: ``mark_destroyed``,开关跟着改名,覆盖的路径不变。
    mark_destroyed_fails: bool = False
    #: 全分支终审 Important-6 —— 覆盖 "沙箱建好了,但回填 container_id 这一
    #: 步失败" 这条路径(那之后没有任何东西找得到这个还活着的沙箱)。
    set_container_id_fails: bool = False
    #: 全分支终审 Important-1 —— 覆盖 "release 的 store 读本身也失败" 这条
    #: 路径(按保温处置,不把已经跑完的工具调用变成错误)。
    is_warm_session_fails: bool = False
    #: Task 9 测试缝 —— ``list_active(only_idle=True)`` 只返回这里列出的
    #: sandbox_id。不建模真实的 last_used_at/acquired_at 时间戳(那是两个
    #: 生产 store 的职责);这里只用来验证 AgentSandboxClient.reap() 把
    #: ``force`` 正确翻译成 ``only_idle=not force`` 并如实按 list_active()
    #: 的返回值行动,见类 docstring "Task 9 对 brief 草稿的偏离"一节。
    idle_sandbox_ids: set[UUID] = field(default_factory=set)
    #: 独立审查 Important-1 测试缝 —— ``list_stuck_creating()`` 只返回这里
    #: 列出、且仍在 ``rows`` 里、``container_id`` 仍未回填的 sandbox_id。
    #: 不建模真实的 acquired_at 时间戳阈值(那是两个生产 store 的职责,由
    #: 容器集成测覆盖);这里只验证 AgentSandboxClient.reap(force=True) 真
    #: 的会调用它、force=False 不会,以及清理时不尝试 connect/kill。
    stuck_creating_sandbox_ids: set[UUID] = field(default_factory=set)
    #: 独立审查 Important-2 测试缝 —— 记录每次 touch_and_get_container_id
    #: 被调用时的 sandbox_id,验证 exec() 真的在推进 last_used_at,而
    #: get_container_id(destroy 等只读路径)不会。
    touched: list[UUID] = field(default_factory=list)
    #: Task 10 测试缝 —— 记录每次 mark_destroyed 被调用的 (sandbox_id,
    #: reason),即使那个 sandbox_id 从未在 rows 里出现过。用来证明
    #: test_ephemeral_create_failure_clears_the_row 的清理代码路径真的执行
    #: 了,而不是"store 本来就是空的"这种巧合通过。
    mark_destroyed_calls: list[tuple[UUID, str]] = field(default_factory=list)
    #: 全分支终审测试缝 —— 这些 sandbox_id 上的 mark_destroyed 直接抛,用来
    #: 验证 reap 的一趟清扫不会被一行掐断。
    mark_destroyed_fails_for: set[UUID] = field(default_factory=set)
    #: #8 测试缝 —— ``sandbox_limit_for_tenant`` 的预置返回值。``None``
    #: (默认)时调用方落回 ``AgentSandboxClient.default_max_sandboxes``,与两
    #: 个生产 store"未设行返 None"同义。
    quota_limit: int | None = None

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str, datetime | None] | None:
        """占坑成功返 None;已被别人占且赢家已就绪返
        ``(赢家 sandbox_id, container_id, acquired_at)``;赢家还在创建中则
        raise。``acquired_at`` 供 #1b 的年龄封顶测试直接改写
        ``self.rows[winner_id]["acquired_at"]`` 摆前置状态。"""
        key = (tenant_id, user_id)
        winner_id = self.warm.get(key)
        if winner_id is None:
            self.warm[key] = sandbox_id
            self.rows[sandbox_id] = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "acquired_at": datetime.now(UTC),
            }
            return None
        container_id = self.rows[winner_id].get("container_id")
        if container_id:
            return (winner_id, container_id, self.rows[winner_id].get("acquired_at"))
        msg = f"a sandbox is already being created for tenant={tenant_id} user={user_id}"
        raise RuntimeError(msg)

    async def create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """Task 10 契约测试实测发现的缺口(完整理由见生产代码
        ``SandboxInstanceStore.create_ephemeral`` 的 docstring)—— 给不带
        ``user_id`` 的 acquire 建一行,不进 ``warm`` 字典(不参与 CAS)。"""
        self.rows[sandbox_id] = {"tenant_id": tenant_id, "user_id": None}

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        if self.set_container_id_fails:
            raise RuntimeError("set_container_id unavailable (db blip)")
        row = self.rows.get(sandbox_id)
        if row is None:
            # 再审 Important-2 —— 契约是"行不在/已销毁必须 raise"。此前这里
            # 是 self.rows[sandbox_id][...] 的裸 KeyError:行为碰巧对,但看不
            # 出是契约还是漏了个 .get()。显式化,与两个生产 store 对齐。
            raise RuntimeError(_missing_row_message(sandbox_id))
        row["container_id"] = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        if self.mark_destroyed_fails or sandbox_id in self.mark_destroyed_fails_for:
            raise RuntimeError(f"mark_destroyed unavailable for {sandbox_id}")
        # 记录每次调用(哪怕行早就不在了)—— 让
        # test_ephemeral_create_failure_clears_the_row 能证明清理代码真的
        # 跑了,而不是碰巧"store 本来就是空的"这种巧合通过。
        self.mark_destroyed_calls.append((sandbox_id, reason))
        row = self.rows.pop(sandbox_id, None)
        if row is not None:
            key = (row["tenant_id"], row["user_id"])
            if self.warm.get(key) == sandbox_id:
                del self.warm[key]

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        return self.rows.get(sandbox_id, {}).get("container_id")

    async def is_warm_session(self, *, sandbox_id: UUID) -> bool:
        """全分支终审 Important-1 —— ``release`` 的分流判据。``rows`` 只存
        活行(``mark_destroyed`` 直接 pop),所以"行在 + user_id 非空"就是
        两个生产 store 那三条谓词的等价物,见生产 Protocol 的 docstring。

        ``is_warm_session_fails`` 是配套的测试缝:覆盖"store 读不到时
        release 按保温处置"这条不该把已经跑完的工具调用变成错误的路径。
        """
        if self.is_warm_session_fails:
            raise RuntimeError("is_warm_session unavailable (db blip)")
        row = self.rows.get(sandbox_id)
        return row is not None and row.get("user_id") is not None

    async def touch_and_get_container_id(self, *, sandbox_id: UUID) -> str | None:
        self.touched.append(sandbox_id)
        return self.rows.get(sandbox_id, {}).get("container_id")

    async def list_active(self, *, only_idle: bool) -> list[tuple[UUID, str]]:
        active = [
            (sandbox_id, row["container_id"])
            for sandbox_id, row in self.rows.items()
            if row.get("container_id") is not None
        ]
        if not only_idle:
            return active
        return [(sid, cid) for sid, cid in active if sid in self.idle_sandbox_ids]

    async def list_stuck_creating(self) -> list[UUID]:
        return [
            sandbox_id
            for sandbox_id in self.stuck_creating_sandbox_ids
            if sandbox_id in self.rows and self.rows[sandbox_id].get("container_id") is None
        ]

    async def count_active_for_tenant(self, *, tenant_id: UUID) -> int:
        """#8 —— 与两个生产 store 同义:数 ``rows`` 里这个 tenant 的活行(``rows``
        只存活行,同生产 in-memory store)。不是一个独立预置字段:测试要"预置
        N 个已存在的活跃沙箱",直接调 ``create_ephemeral``/``claim_warm`` 建
        真行即可,与 :meth:`AgentSandboxClient._enforce_quota` 的
        insert-then-check 语义(本次调用自己插的行也数进去)天然对齐。"""
        return sum(1 for row in self.rows.values() if row["tenant_id"] == tenant_id)

    async def sandbox_limit_for_tenant(self, *, tenant_id: UUID) -> int | None:
        del tenant_id
        return self.quota_limit


def make_client(
    sdk: FakeSdk,
    store: SandboxInstanceStore,
    *,
    workspace_pv_name: str | None = None,
    workspace_subpath_prefix: str = "",
    workspace_root: str | None = None,
) -> AgentSandboxClient:
    return AgentSandboxClient(
        domain="gw.example.com",
        api_key="k",
        template="expert-work-sandbox",
        store=store,
        sdk=sdk,
        egress_token_secret="s3cret",
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
        # 沙箱迁移波 2 Task 4 —— 三项全默认 None/"":调用方不传就是波 1 行为,
        # 与 AgentSandboxClient 自己的字段默认值同一个理由(见类 docstring)。
        workspace_pv_name=workspace_pv_name,
        workspace_subpath_prefix=workspace_subpath_prefix,
        workspace_root=workspace_root,
    )


@pytest.mark.asyncio
async def test_acquire_creates_and_records_container_id() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)

    assert len(sdk.created) == 1
    assert store.rows[sandbox_id]["container_id"] == "sbx-1"


@pytest.mark.asyncio
async def test_acquire_without_user_id_records_a_findable_row() -> None:
    """Task 10 契约测试实测揪出的真实 bug,先于本任务从未被任何测试覆盖过
    (全文件所有 acquire 调用此前无一例外都传了 user_id):``acquire`` 不带
    ``user_id`` 时从未经过 ``claim_warm``,而 ``acquire`` 结尾无条件调用
    ``set_container_id`` 去 UPDATE 一个此前从未 INSERT 过的行——生产 SQL
    实现是静默 0 行生效,``FakeInstanceStore``/内存实现是直接 ``KeyError``。
    真连 E2B 测试集群跑契约测试时当场炸穿:``exec()`` 报
    ``SandboxSupervisorError: sandbox ... has no recorded container id``。

    这条测试验证修复后的行为:``store.rows`` 里必须能找到这一行,且
    ``container_id`` 必须已经回填——不只是"acquire 不抛异常"这种弱断言。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id = uuid4()

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t1")

    assert sandbox_id in store.rows, "临时沙箱必须有一行记录,否则后续 exec/destroy/reap 都找不到它"
    assert store.rows[sandbox_id]["container_id"] == "sbx-1"
    assert await store.get_container_id(sandbox_id=sandbox_id) == "sbx-1"


@pytest.mark.asyncio
async def test_exec_after_ephemeral_acquire_actually_works() -> None:
    """``test_acquire_without_user_id_records_a_findable_row`` 的直接
    payoff——这正是真连测试集群时实际炸穿的那一步(``_attach`` 读不到
    container_id)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sdk.sandbox.commands.result_stdout = "CONTRACT_OK\n"

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t1")
    outcome = await client.exec(sandbox_id=sandbox_id, code="print('CONTRACT_OK')", timeout_s=30)

    assert "CONTRACT_OK" in outcome.stdout


@pytest.mark.asyncio
async def test_destroy_after_ephemeral_acquire_actually_works() -> None:
    """同上,针对 ``destroy`` —— 没有这一行的话 ``_attach`` 会因为
    "no recorded container id" 直接抛错(destroy 的 broad except 会吞掉它、
    误判成"沙箱已经不在",行为上不算崩溃,但连 kill 都没有真的发生)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t1")
    await client.destroy(sandbox_id=sandbox_id, reason="ops")

    assert sdk.sandbox.killed is True, "destroy 必须真的 kill 到沙箱,不是被 no-container-id 吞掉"
    assert sandbox_id not in store.rows


@pytest.mark.asyncio
async def test_ephemeral_create_failure_clears_the_row() -> None:
    """临时沙箱没有热会话坑(它压根不参与 0141 CAS),但
    ``acquire`` 已经在 ``sdk.create()`` 之前插入了一行 ``container_id`` 仍是
    NULL 的 ``IN_USE`` 行——create 失败后必须清掉,不能让它一直卡到
    ``list_stuck_creating`` 的 TTL 兜底才被 ``reap(force=True)`` 捡走。"""
    sdk, store = FakeSdk(create_fails=True), FakeInstanceStore()
    client = make_client(sdk, store)

    with pytest.raises(SandboxSupervisorError):
        await client.acquire(tenant_id=uuid4(), thread_id="t1")

    assert store.rows == {}, "create 失败后临时沙箱的行必须被清掉,不能卡在 container_id=NULL"
    # 只看 rows 为空不够——store 从一开始就是空的,这个断言碰巧也会通过
    # (不能证明清理代码真的跑了)。必须证明 mark_destroyed 真的被调用过。
    assert [reason for _, reason in store.mark_destroyed_calls] == ["create_failed"]


@pytest.mark.asyncio
async def test_concurrent_acquire_has_one_winner() -> None:
    """第二路 acquire 不新建,connect 到赢家 —— 且返回值必须是赢家那行
    **真实存在**的 sandbox_id,不是自己开头新铸、从未插入任何行的 uuid4()
    (审查 Important-3:否则后续 destroy() 会静默 no-op,见
    ``test_destroy_after_reuse_actually_works``)。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    first_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    second_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 1, "第二路不该再建沙箱"
    assert sdk.connected == ["sbx-1"], "第二路该 connect 赢家"
    assert second_id == first_id, "复用热会话必须返回赢家那行真实的 sandbox_id"


@pytest.mark.asyncio
async def test_destroy_after_reuse_actually_works() -> None:
    """审查 Important-3 的直接后果验证:复用路径返回的 id 必须是能真正
    ``destroy`` 掉的、已入库的 id —— 不是自铸的空壳 uuid(那样 ``destroy()``
    会静默 no-op:``get_container_id`` 查不到、``mark_destroyed`` 的
    ``WHERE`` 匹配 0 行,两处都不报错,沙箱杀不掉也不清行)。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    reused_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    await client.destroy(sandbox_id=reused_id, reason="ops")

    assert sdk.sandbox.killed is True, "destroy 必须真的 kill 到沙箱,不是静默 no-op"
    assert reused_id not in store.rows, "行必须被清掉,热会话槽位必须放出来"


@pytest.mark.asyncio
async def test_connect_failure_rebuilds() -> None:
    """重连失败(库存不足/欠费/超时被平台 kill)→ 丢弃旧行 → 重建。

    工作区权威在外部存储,重建无损 —— spec § 6.3。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    sdk.connect_fails = True

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 2, "connect 失败后必须重建"
    assert store.rows[sandbox_id]["container_id"] == "sbx-1"


@pytest.mark.asyncio
async def test_create_failure_releases_warm_slot() -> None:
    """审查 Critical-2:``claim_warm`` 已经持久化提交了
    ``state=IN_USE``/``container_id=NULL`` 的一行,如果紧接着的
    ``sdk.create()`` 失败,必须把那一行清掉、槽位放出来 —— 否则该
    ``(tenant, user)`` 被迁移 0141 的部分唯一索引永久卡住:``claim_warm``
    看到"行存在但 container_id 是 NULL"会永远 raise "already being
    created",没有任何东西能再解开它(手工改库之外)。
    """
    sdk, store = FakeSdk(create_fails=True), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    with pytest.raises(SandboxSupervisorError):
        await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)

    assert (tenant_id, user_id) not in store.warm, "create 失败后必须放出热会话槽位"

    # 槽位真的空出来了的话,下一次 acquire 应该能正常创建成功,不再是
    # "already being created"。
    sdk.create_fails = False
    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)
    assert store.rows[sandbox_id]["container_id"] == "sbx-1"


@pytest.mark.asyncio
async def test_create_failure_rollback_does_not_mask_original_error() -> None:
    """二审 Critical:``_create_and_track`` 的清理步骤(``_unwind_slot`` →
    ``mark_destroyed``)自己也可能失败(DB 抖动/连接池耗尽/网络分区)。撤销
    清理前,``except Exception: ... await self._unwind_slot(...); raise`` 里
    如果清理抛出,那句 ``raise`` 永远执行不到 —— 冒出去的是清理的异常,不是
    原始的 ``_create()`` 失败:①调用方按 ``SandboxSupervisorError`` 设计的
    except 链接不住(类型变了)②热会话行照样卡死(回滚没做成),Critical-2
    描述的症状在自己的失败路径上原样重演,只是往下多埋一层。

    这里验证修复后的行为:清理失败被吞掉(只记日志),但外层重新抛出的仍然
    是 ``_create()`` 触发的那个 ``SandboxSupervisorError``。
    """
    sdk = FakeSdk(create_fails=True)
    store = FakeInstanceStore(mark_destroyed_fails=True)
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    with pytest.raises(SandboxSupervisorError, match="sandbox create failed"):
        await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)


@pytest.mark.asyncio
async def test_claim_warm_not_ready_surfaces_as_sandbox_supervisor_error() -> None:
    """审查 Important-4:两个生产 store(SQL/内存)在"赢家已占坑但还在创建
    中(container_id 仍是 NULL)"时都会 raise —— 之前只有裸 store 层测过,
    ``AgentSandboxClient._claim_warm`` 把这类异常包成 ``SandboxSupervisorError``
    那段代码在 client 层零覆盖。这个窗口是探针报告实测的 35-40s E2B 冷启
    宽窗口,是并发 acquire 最可能撞上的真实结果,不是理论边界。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    # 第一路直接对 store 占坑、不让它走到 set_container_id —— 精确模拟
    # "赢家还在创建中"这个状态,不依赖 acquire() 的完整流程凑巧卡在那。
    await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())

    with pytest.raises(SandboxSupervisorError, match="already being created"):
        await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)


@pytest.mark.asyncio
async def test_seed_files_written_before_first_exec() -> None:
    """sandbox migration wave 2 — seed lands under SANDBOX_SKILLS_ROOT
    (sandbox-local), not WORKSPACE_ROOT; relpath already carries the
    agent_key namespace prefix (the caller's job, build_skill_seed_files)."""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(
        tenant_id=uuid4(),
        thread_id="t1",
        user_id=uuid4(),
        seed_files=(("agent-1/pptx/a.md", b"hello"),),
    )

    assert sdk.sandbox.files.written == [
        (f"{SANDBOX_SKILLS_ROOT}/agent-1/pptx/a.md", b"hello", SANDBOX_EXEC_USER)
    ]


@pytest.mark.asyncio
async def test_destroy_kills_and_marks_row() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    await client.destroy(sandbox_id=sandbox_id, reason="ops")

    assert sdk.sandbox.killed is True
    assert sandbox_id not in store.rows


@pytest.mark.asyncio
async def test_egress_env_empty_when_no_policy() -> None:
    """egress=None(exec_python 等未绑定 EgressContext 时)不铸 token。

    全分支终审 Important-2 起 ``envs`` 不再是空 dict —— 镜像那套 ENV 恒定
    随 ``create`` 送(见 :data:`SANDBOX_IMAGE_ENV`),所以这里断言的是"一个
    代理相关的键都没有",而不是"整个 envs 是空的"。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(tenant_id=uuid4(), thread_id="t1", user_id=uuid4())

    assert sdk.created[0]["envs"] == SANDBOX_IMAGE_ENV


@pytest.mark.asyncio
async def test_egress_env_mints_token_when_policy_set() -> None:
    """egress 策略非 none 时铸 token 并塞 HTTP(S)_PROXY —— 与 supervisor 的
    ``_egress_env`` 同语义(见 sandbox_supervisor/supervisor.py:790)。

    这条测试独立发现的问题(不在派发方点名的三处修正里):brief 给的
    ``_egress_env`` 草稿直接读 ``egress.tenant_id`` / ``egress.sandbox_id``,
    但 ``EgressContext``(orchestrator/tools/sandbox.py)根本没有这两个字段
    (它只有 policy/agent_name/agent_version/allowlist/denylist)—— 那份草稿
    只要 egress 非 None 就必炸 AttributeError。真实实现改成从 ``acquire``
    的入参里取 tenant_id / sandbox_id,而不是指望 EgressContext 携带它们。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    egress = EgressContext(policy="proxy", agent_name="demo", agent_version="1")

    await client.acquire(tenant_id=uuid4(), thread_id="t1", user_id=uuid4(), egress=egress)

    envs = sdk.created[0]["envs"]
    assert envs["HTTPS_PROXY"] == envs["HTTP_PROXY"]
    assert envs["HTTPS_PROXY"].startswith("http://")
    assert "@credential-proxy.expert-work.svc.cluster.local:8081" in envs["HTTPS_PROXY"]
    assert envs["NO_PROXY"] == "credential-proxy.expert-work.svc.cluster.local,localhost,127.0.0.1"


@pytest.mark.asyncio
async def test_ensure_e2b_patched_sets_domain_env_before_patching(monkeypatch) -> None:
    """审查 Critical-1:独立复现过
    ``env -u E2B_DOMAIN ./.venv/bin/python3 -c "from kruise_agents.patch_e2b
    import patch_e2b; patch_e2b(https=False)"`` → ``KeyError: 'E2B_DOMAIN'``。
    ``kruise_agents.patch_e2b()`` 第一行就无条件读裸环境变量
    ``os.environ['E2B_DOMAIN']``(没有 ``.get()`` 兜底),仓内没有任何地方
    设过这个裸变量(我们自己的配置是完全不同名字/通道的
    ``Settings.sandbox_e2b_domain``)——第一次真调用必炸。
    ``_ensure_e2b_patched`` 必须在调 ``patch_e2b()`` 之前把 domain/api_key
    写进这个裸环境变量。

    用 ``monkeypatch`` 把真正的 ``kruise_agents.patch_e2b`` 换成一个只记录
    调用瞬间 ``os.environ`` 状态的替身——既不用真的跑私有协议 monkeypatch
    (避免污染同一 pytest 进程里的其它测试,这正是本模块整体设计要严格
    避免的那类全局副作用,见 e2b_patch.py 模块 docstring),又能验证
    "env 先设、patch 后调"这个顺序关系。``monkeypatch`` 的自动回滚保证
    ``_e2b_patched`` 在测试结束后仍是 ``False``,不影响其它测试。
    """
    import orchestrator.tools.e2b_patch as mod

    monkeypatch.delenv("E2B_DOMAIN", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setattr(mod, "_e2b_patched", False)

    seen: dict[str, object] = {}

    def _fake_patch_e2b(https: bool = True, validate_key: bool = True) -> None:
        del validate_key
        seen["E2B_DOMAIN"] = os.environ.get("E2B_DOMAIN")
        seen["E2B_API_KEY"] = os.environ.get("E2B_API_KEY")
        seen["https"] = https

    monkeypatch.setattr("kruise_agents.patch_e2b.patch_e2b", _fake_patch_e2b)

    mod._ensure_e2b_patched(domain="gw.example.com", api_key="k")

    assert seen["E2B_DOMAIN"] == "gw.example.com"
    assert seen["E2B_API_KEY"] == "k"
    assert seen["https"] is False


@pytest.mark.asyncio
async def test_ensure_e2b_patched_warns_on_domain_mismatch(monkeypatch, caplog) -> None:
    """二审建议(已采纳)——``setdefault`` 保持"运维显式设置优先"这条语义
    不变,但如果预设的 ``E2B_DOMAIN`` 真的跟这次 ``AgentSandboxClient`` 的
    ``domain`` 不一致,``patch_e2b()`` 会把(旧的)预设值一次性烤进
    ``E2B_API_URL``、之后不再重读,而 ``create``/``connect`` 每次仍然把
    正确的 ``domain`` 当 kwarg 传给 SDK —— 两边不一致时报出来的是让人摸不
    着头脑的认证/网络错,不是"连不上"那种一眼能看懂的信号。这里验证不
    一致时至少有一条 warning 日志把这个歧义摆到明面上,且 ``setdefault``
    行为本身不变(预设值仍然生效,不被覆盖)。

    ``caplog.at_level(..., logger="orchestrator.tools.e2b_patch")`` 把
    断言收紧到自家 logger——仓内已有先例(#1077)证明不这样做容易被其它
    模块的噪音日志(如 otel exporter)搞出 flaky。

    断言走 ``record.args`` 而不是 ``in caplog.text``,两个原因:
    ``caplog.text`` 是所有日志拼成的一整块,``"x" in`` 它只能证明这串字符
    在某处出现过,证不了它出现在**这条**日志的**这个**占位符上;而且
    CodeQL 的 ``py/incomplete-url-substring-sanitization`` 会把
    ``"<域名字面量>" in <字符串>`` 一律判成 URL 校验绕过(生产代码里那确实
    是典型漏洞形态),在测试断言上是误报,但它会卡住合并。按位置断言把
    两件事一起解决 —— 顺带钉住了参数顺序:第 2、3 个占位符都是新 domain,
    错配时 ``in`` 版本照样绿。
    """
    import orchestrator.tools.e2b_patch as mod

    stale, fresh = "stale.example.com", "gw.example.com"
    monkeypatch.setenv("E2B_DOMAIN", stale)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setattr(mod, "_e2b_patched", False)
    monkeypatch.setattr("kruise_agents.patch_e2b.patch_e2b", lambda **_: None)

    with caplog.at_level(logging.WARNING, logger="orchestrator.tools.e2b_patch"):
        mod._ensure_e2b_patched(domain=fresh, api_key="k")

    [warned] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned.args == (stale, fresh, fresh)
    # setdefault 语义不变 —— 预设值仍然生效,不被这次调用覆盖。
    assert os.environ["E2B_DOMAIN"] == stale


# ---------------------------------------------------------------------------
# Task 8 —— AgentSandboxClient.exec,四个契约点(spec § 6.1,源头
# infra/sandbox-image/runner.py:28-72)+ 一条 E2B 特有分支(非零退出码不
# 是异常)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_clamps_timeout_high() -> None:
    """契约 1:timeout clamp 到 [1, 300](runner.py:51)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=9999)

    _, timeout, user, _cwd = sdk.sandbox.commands.calls[-1]
    assert timeout == MAX_TIMEOUT_S == 300
    assert user == SANDBOX_EXEC_USER, (
        "commands.run 必须传 user=,否则真实 E2B 炸 AuthenticationException"
    )


@pytest.mark.asyncio
async def test_exec_clamps_timeout_low_and_defaults() -> None:
    """契约 1 的另外两支:0(及以下)clamp 到 1,``None`` 落到缺省 30
    (runner.py:45-51,同一个 ``max(1, min(x, MAX))`` 公式)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=0)
    assert sdk.sandbox.commands.calls[-1][1] == 1

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=None)
    assert sdk.sandbox.commands.calls[-1][1] == DEFAULT_TIMEOUT_S == 30


@pytest.mark.asyncio
async def test_exec_truncates_output_at_one_million_chars() -> None:
    """契约 2:输出上限 1_000_000 chars(runner.py:37)。

    简单头部截断 ``[:MAX_OUTPUT_CHARS]``,不是 runner.py ``_cap`` 头尾各半
    + 省略标记那套人类可读格式(见 ``exec`` 实现注释里的理由)——这里只
    验证长度这一个契约点,不验证展示格式。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.sandbox.commands.result_stdout = "x" * (MAX_OUTPUT_CHARS + 500)
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    outcome = await client.exec(sandbox_id=sid, code="print('x'*2_000_000)", timeout_s=5)

    assert len(outcome.stdout) == MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_exec_timeout_maps_to_minus_one_and_flag() -> None:
    """契约 3:超时 → exit_code=-1, timed_out=True(runner.py:60-66)。

    真实异常类型实测(2026-08-04,读 e2b==2.24.0 源码,非公开文档猜测):
    ``commands.run(timeout=...)`` 超时抛的是 ``e2b.exceptions.TimeoutException``,
    不是内置 ``TimeoutError``——见 ``AgentSandboxClient.exec`` 实现里的
    详细注释。这里直接 raise 真实类型,drive 的是与生产代码完全相同的
    except 子句,不是一个凑巧同名的假件类型(否则测试"能过"但咬不住真正
    的类型不匹配这类回归)。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    sdk.sandbox.commands.run_error = TimeoutException("deadline")
    outcome = await client.exec(sandbox_id=sid, code="import time;time.sleep(99)", timeout_s=1)

    assert outcome.exit_code == -1
    assert outcome.timed_out is True


@pytest.mark.asyncio
async def test_exec_timeout_also_covers_the_blank_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约 3 的另一半:超时**不总是**以 ``TimeoutException`` 的形态到达。

    2026-08-06 在真栈契约测试上两次实测到这条路径:
    ``test_exec_default_timeout_is_the_shared_default``(``time.sleep(45)``
    配 30 秒时限)红在 ``exec`` 的兜底分支上,报错正文是空的——
    ``SandboxSupervisorError: sandbox exec failed: ``。日志里 e2b 的映射表
    露了底:envd 掐断长命令时 gRPC 流中断走 anyio(``BrokenResourceError``
    / ``EndOfStream``)→ httpx 的 ``map_exceptions`` → ``httpcore.ReadError``
    / ``ReadTimeout``,而这一族异常的 ``str()`` 恰好是空字符串。

    这不是"测试偶尔红":同一条路径在生产上会把一次本该返回
    ``timed_out=True`` 的正常超时升级成 ``SandboxSupervisorError``,让整个
    run 挂掉——LLM 写个长任务跑满时限就会踩到。

    用真实的 ``httpcore.ReadError`` 而不是自造一个空消息异常:现场那个类
    就是它,凑一个同形状的假件会让"SDK 换版本后异常族变了"这类回归照样
    绿。``_monotonic`` 接缝摆出"跑满了时限"的状态,见它的 docstring。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    ticks = iter([100.0, 100.0 + 30.0])
    monkeypatch.setattr(agent_sandbox_module, "_monotonic", lambda: next(ticks))
    sdk.sandbox.commands.run_error = httpcore.ReadError()

    outcome = await client.exec(sandbox_id=sid, code="import time;time.sleep(45)", timeout_s=30)

    assert outcome.timed_out is True
    assert outcome.exit_code == -1


@pytest.mark.asyncio
async def test_exec_early_connection_failure_stays_an_error_and_names_the_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个异常,**没跑满时限**就断 → 仍然是基础设施故障,不是超时。

    归因判据是时长而不是异常类型,所以必须有这条反向用例:否则把
    ``_TIMEOUT_ATTRIBUTION_RATIO`` 改成 0(即"任何异常都算超时")也照样
    绿,那等于把一切故障静默伪装成超时。

    顺带钉住错误消息带类型名:走到这一支的异常有一整族 ``str()`` 为空,
    只插值 ``{exc}`` 会得到 ``sandbox exec failed: ``,恰恰在最需要诊断
    的时候什么都不说。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    ticks = iter([100.0, 100.5])  # 30 秒时限里只过了半秒
    monkeypatch.setattr(agent_sandbox_module, "_monotonic", lambda: next(ticks))
    sdk.sandbox.commands.run_error = httpcore.ReadError()

    with pytest.raises(SandboxSupervisorError) as excinfo:
        await client.exec(sandbox_id=sid, code="print(1)", timeout_s=30)

    message = str(excinfo.value)
    assert "ReadError" in message
    assert "0.5s" in message


@pytest.mark.asyncio
async def test_exec_nonzero_exit_returns_outcome_not_error() -> None:
    """AgentSandboxClient 特有分支,runner.py 没有(实测发现,不在 brief
    点名的四个契约点里,但同样是"跟本地沙箱行为对齐"的一部分——见
    ``exec`` 实现注释)。

    E2B 的 ``commands.run()`` 在子进程以非零退出码结束时抛
    ``CommandExitException``;runner.py 包的 ``subprocess.run(check=False)``
    从不因为非零退出码抛异常,只把 ``exit_code`` 原样放进响应。LLM 代码里
    "未处理异常 / 断言失败 / ``sys.exit(1)``"是最常见的失败场景,如果不
    单独接住这个类型,会被兜底的 ``except Exception`` 误判成"沙箱基础设施
    故障"(``SandboxSupervisorError``,整个 run 挂掉),而不是 runner.py
    契约要求的"正常返回一个非零 exit_code"——这条测试钉住"必须是后者"。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    sdk.sandbox.commands.run_error = CommandExitException(
        stdout="partial output\n",
        stderr="Traceback (most recent call last):\nValueError: boom\n",
        exit_code=1,
        error="exit status 1",
    )

    outcome = await client.exec(sandbox_id=sid, code="raise ValueError('boom')", timeout_s=5)

    assert outcome.exit_code == 1
    assert outcome.timed_out is False
    assert outcome.stdout == "partial output\n"
    assert "ValueError: boom" in outcome.stderr


@pytest.mark.asyncio
async def test_exec_writes_code_to_file_not_shell_arg() -> None:
    """已知偏差(spec § 6.1):code 不能拼进命令行(引号注入)。

    E2B ``commands.run(cmd: str, ...)`` 内部固定走 ``/bin/bash -l -c cmd``
    (不像 runner.py 的 ``subprocess.run([..., "-c", code])`` 那样是不经过
    shell 的 argv 列表),把任意 code 直接嵌进这个 shell 字符串会被引号/
    特殊字符注入。做法是先 ``files.write`` 到临时文件再 ``python -E -P <file>``
    执行。副作用是 ``-c`` 模式下 ``__file__`` 不存在、文件模式下存在 ——
    这条差异由此测试钉住(测试本身只钉"不拼命令行"这一半;``__file__``
    语义差异是文档记录,不是这条测试能直接断言的运行时行为)。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    nasty = "print('it\\'s \"quoted\"; rm -rf /')"
    await client.exec(sandbox_id=sid, code=nasty, timeout_s=5)

    path, _, write_user = sdk.sandbox.files.written[-1]
    assert path.endswith(".py"), "code 必须先落文件"
    assert write_user == SANDBOX_EXEC_USER, (
        "files.write 必须传 user=,否则真实 E2B 炸 AuthenticationException"
    )

    cmd, _, run_user, _cwd = sdk.sandbox.commands.calls[-1]
    assert nasty not in cmd, "code 绝不能出现在命令行里"
    assert "python -E -P " in cmd
    assert run_user == SANDBOX_EXEC_USER


@pytest.mark.asyncio
async def test_exec_sets_permissive_umask_before_running_the_script() -> None:
    """``commands.run`` 走 ``/bin/bash -l -c cmd``,所以命令串必须以
    ``umask 000 && `` 打头,让 bash 先放开 umask 再 exec python。与本地
    supervisor 后端 ``runner.py.main()`` 的 ``os.umask(0)`` 是同一件事在
    两个后端各自的落点,契约测试(``test_sandbox_runtime_contract.py``)
    钉住两者不会分叉。

    **这条机制的原始理由已经作废**(2026-08-08 统一 uid,见
    ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
    § 六):它当初是为"跨 uid 写冲突"而设——默认 umask 掩出的
    ``0o755``/``0o644`` 会让**另一个 uid** 的 control-plane 读得进却删/写
    不进。现在两侧同 uid,属主位本身就够。断言留着不删:``umask 000`` 落出
    的 ``0o777``/``0o666`` 是比现在真正需要的 mode 更宽的**安全超集**,不是
    错,只是不再最小;收紧它要真栈验证,是独立的后续任务。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5)

    cmd, *_ = sdk.sandbox.commands.calls[-1]
    assert cmd.startswith("umask 000 && python "), cmd


# ---------------------------------------------------------------------------
# 全分支终审 Important-2 —— 沙箱进程既不继承镜像的 WORKDIR 也不继承它的 ENV
# (2026-08-04 集群探针实测)。cwd 靠 commands.run(cwd=),环境变量靠
# create(envs=)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_runs_in_workspace_cwd() -> None:
    """不传 ``cwd`` 时 envd 把进程扔在 ``/home/agent``:``bash`` 工具对 LLM
    宣称的 "Runs in /workspace" 是假的,LLM 写的相对路径文件会落到
    ``file_ops``(只认绝对 ``/workspace/...``)看不见的地方。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5)

    _, _, _, cwd = sdk.sandbox.commands.calls[-1]
    assert cwd == WORKSPACE_ROOT == "/workspace"


@pytest.mark.asyncio
async def test_create_passes_the_image_environment() -> None:
    """镜像声明的 ENV 必须显式随 ``create(envs=)`` 送进去,且不能踩掉
    egress 那组(两组同走 ``envs`` 这一个通道)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(
        tenant_id=uuid4(),
        thread_id="t",
        user_id=uuid4(),
        egress=EgressContext(policy="allowlist", agent_name="a", agent_version=1),
    )

    envs = sdk.created[-1]["envs"]
    for key, value in SANDBOX_IMAGE_ENV.items():
        assert envs[key] == value, f"镜像环境变量 {key} 没送进沙箱"
    # 沙箱迁移 W2 Task 9:HOME 迁出 WORKSPACE_ROOT——/workspace 现在必须空着
    # 让平台建 NAS 挂载 symlink,HOME 改落 useradd -m 建好的 /home/agent。
    assert envs["HOME"] == "/home/agent", "HOME 不是 /home/agent 则 PIP_USER 装到镜像预建目录之外"
    assert envs["PIP_USER"] == "1", "只读 rootfs 上没有 PIP_USER=1 则 pip install 必失败"
    # egress 那组仍在 —— 合并没有把它挤掉。
    assert "HTTPS_PROXY" in envs


# ---------------------------------------------------------------- 沙箱迁移波 2 Task 4:
# csi-volume-config 挂载注入 + acquire 前的软删闸/mkdir+chown。


@pytest.mark.asyncio
async def test_create_injects_csi_volume_config() -> None:
    """``workspace_pv_name`` 配了时,``create(metadata=)`` 必须带
    ``e2b.agents.kruise.io/csi-volume-config``,JSON 解出的三键与 subPath
    拼接对——``prefix=""``(生产配置)不留前导 ``"/"``。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_pv_name="workspace-nas")
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    metadata = sdk.created[-1]["metadata"]
    volumes = json.loads(metadata["e2b.agents.kruise.io/csi-volume-config"])
    assert volumes == [
        {
            "pvName": "workspace-nas",
            "mountPath": WORKSPACE_ROOT,
            "subPath": f"{tenant_id}/{user_id}",
        }
    ]


def test_workspace_subpath_prefix_conflicts_with_pv_name_raises() -> None:
    """Task 4 审查 Important-1 —— ``workspace_pv_name`` 配了、非空
    ``workspace_subpath_prefix`` 也配了这个组合会让沙箱与 ``NasWorkspaceStore``
    静默各写各的树(前者按前缀挂,后者压根不理解前缀),必须在构造期就大
    声拒绝,不要等到跑起来才靠"用户上传的文件沙箱里看不到"这种症状去反查。
    """
    with pytest.raises(ValueError, match="workspace_subpath_prefix"):
        make_client(
            FakeSdk(),
            FakeInstanceStore(),
            workspace_pv_name="workspace-nas",
            workspace_subpath_prefix="ew",
        )


def test_workspace_subpath_prefix_empty_string_is_fine_with_pv_name() -> None:
    """空串(生产配置)与 ``workspace_pv_name`` 同时配不该被这条新校验挡住
    ——``__post_init__`` 只拒非空前缀,不拒配置项本身共存。"""
    make_client(FakeSdk(), FakeInstanceStore(), workspace_pv_name="workspace-nas")


def test_workspace_subpath_prefix_alone_is_fine() -> None:
    """未配 ``workspace_pv_name`` 时,``workspace_subpath_prefix`` 非空不该
    被拒——两者的冲突只在 ``workspace_pv_name`` 也配了时才成立。"""
    make_client(FakeSdk(), FakeInstanceStore(), workspace_subpath_prefix="ew")


@pytest.mark.asyncio
async def test_create_without_pv_name_omits_metadata() -> None:
    """未配 ``workspace_pv_name``(波 1 默认)—— 不出现 ``metadata`` 键,行为
    与波 1 完全一致。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    assert "metadata" not in sdk.created[-1]


@pytest.mark.asyncio
async def test_ephemeral_create_mounts_scratch_subpath() -> None:
    """临时沙箱(``user_id=None``)也必须挂(spec 决策 9)——subPath 走
    ``_scratch/<sandbox_id>``,``sandbox_id`` 是本次 acquire 返回的那个 id。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_pv_name="workspace-nas")

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t")

    metadata = sdk.created[-1]["metadata"]
    volumes = json.loads(metadata["e2b.agents.kruise.io/csi-volume-config"])
    assert volumes == [
        {
            "pvName": "workspace-nas",
            "mountPath": WORKSPACE_ROOT,
            "subPath": f"_scratch/{sandbox_id}",
        }
    ]


@pytest.mark.asyncio
async def test_acquire_mkdirs_user_workspace(tmp_path: Path) -> None:
    """``workspace_root`` 配了 —— acquire 前把该用户在 NAS 上的目录建出来
    (spec § 二之二"附带事实":不 mkdir 就没有目录可挂/可放开权限)。真
    ``mkdir``/``chmod`` 都不需要 monkeypatch 了(Critical 修复后):chmod
    自己拥有的目录不需要 root,不像 chown 到别的 uid 那样会 EPERM。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    assert (tmp_path / str(tenant_id) / str(user_id)).is_dir()


@pytest.mark.asyncio
async def test_acquire_mkdirs_ephemeral_scratch_dir(tmp_path: Path) -> None:
    """临时沙箱同理建 ``_scratch/<sandbox_id>``——本任务对 brief 留白问题
    ("_scratch 是否也需要 mkdir+放开权限")的取舍:走同一段逻辑,同一个真因
    (NAS 新建目录默认权限挡住沙箱内的 agent uid)在这个 subPath 下同样
    成立。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert (tmp_path / "_scratch" / str(sandbox_id)).is_dir()


@pytest.mark.asyncio
async def test_acquire_chowns_the_mount_from_inside_the_sandbox() -> None:
    """波 2 收尾(集群实测坐实)—— 挂载点目录若由**平台**建,是 ``root:root
    0755``,沙箱里的 agent(uid 10000)第一次写 ``/workspace`` 就
    ``PermissionError``。权威修法是 control-plane 在 ``create()`` 之前
    mkdir 好(``_prepare_workspace_mount``),这里钉的是够不到那半边时的
    兜底:沙箱建好后以 **root** 跑一句 ``chown 10000:10000 /workspace``。

    断言 ``user="root"`` 而不是 ``SANDBOX_EXEC_USER``:agent 不是属主,以
    agent 身份 chown 一个 root 属主的目录必然 EPERM,这一句就白跑了。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_pv_name="workspace-nas")

    await client.acquire(tenant_id=uuid4(), thread_id="t")

    chowns = [c for c in sdk.sandbox.commands.calls if c[0].startswith("chown ")]
    assert chowns == [(f"chown 10000:10000 {WORKSPACE_ROOT}", None, "root", None)]


@pytest.mark.asyncio
async def test_acquire_skips_the_mount_chown_when_no_pv_is_configured() -> None:
    """没配 ``workspace_pv_name`` 就根本没有挂载、``/workspace`` 也不存在
    ——那一句 chown 只会在 envd 侧留一条无意义的失败,不发。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert [c for c in sdk.sandbox.commands.calls if c[0].startswith("chown ")] == []


@pytest.mark.asyncio
async def test_acquire_survives_a_failing_mount_chown() -> None:
    """刻意 best-effort —— 生产路径上目录早已是 control-plane 建的、属主
    已经是 10000,而这一句以 root 跑;NAS 若开 ``root_squash``,root 被映射
    成 nobody,对别人属主的目录 chown 必然 EPERM。那种失败是无害的(目录本来
    就是对的),把它抬成 ``create`` 失败会判死一个完全可用的沙箱。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.sandbox.commands.run_error = RuntimeError("chown: Operation not permitted")
    client = make_client(sdk, store, workspace_pv_name="workspace-nas")

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert sandbox_id is not None
    assert sdk.sandbox.killed is False


@pytest.mark.asyncio
async def test_acquire_chmods_user_workspace_to_0700(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """方向变更(共享 gid → 统一 uid,见
    ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
    § 六)—— control-plane 与沙箱里
    的 agent 现在是同一个 uid,谁先建这棵目录都是它的属主,不需要再对"另一
    侧"开任何口子。这里 monkeypatch 记录调用参数,断言目标 mode 是
    ``0o700``(此前的 gid 方案里短暂是 ``0o777`` world-writable;方向变更后
    收紧到属主独占)。"""
    calls: list[tuple[str, int]] = []

    def fake_chmod(path: object, mode: int) -> None:
        calls.append((str(path), mode))

    monkeypatch.setattr(os, "chmod", fake_chmod)
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    assert len(calls) == 1
    chmoded_path, mode = calls[0]
    assert chmoded_path == str(tmp_path / str(tenant_id) / str(user_id))
    assert mode == 0o700


@pytest.mark.asyncio
async def test_acquire_survives_a_chmod_permission_error_on_the_user_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审 I-2 —— ``chmod`` 只对**属主**放行。uid 迁移(control-plane
    10002 → 10000)落地当天,存量用户根的属主还是旧 uid,``chmod`` 会
    ``EPERM``;把这些目录改回新 uid 属主是一次性迁移 Job(Task D)的职责,
    不是每次 acquire 都跑的这条路径该做的事。

    这条之前会把整个 acquire 判死(``SandboxSupervisorError``)——迁移 Job
    跑完之前,受影响用户连一次 acquire 都做不成,比"某些操作降级"糟得多。
    现在 ``PermissionError`` 是尽力而为:``mkdir`` 已经确认目录存在,
    acquire 继续往下走,真正的读写权限问题留给沙箱 ``exec``/文件工具接触
    到时用 ``docs/superpowers/plans/2026-08-08-workspace-gid-sharing.md``
    Task A Step 7 保留清单 item 1 的窄类型 ``WorkspacePermissionError``
    自然诊断。"""

    def fake_chmod(path: object, mode: int) -> None:
        del path, mode
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", fake_chmod)
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    tenant_id, user_id = uuid4(), uuid4()

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    assert sandbox_id is not None
    # mkdir 本身(不是 chmod)仍然真跑了——目录确实存在,acquire 没有跳过创建。
    assert (tmp_path / str(tenant_id) / str(user_id)).is_dir()


@pytest.mark.asyncio
async def test_acquire_surfaces_chmod_failure_as_sandbox_supervisor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chmod 失败(NAS 权限异常 / 挂载点抖动)同样必须大声抛
    ``SandboxSupervisorError``,不静默——静默的话沙箱会挂载到一个 agent
    用户写不进去的目录,直到执行期才报一个远离根因的错误。"""

    def fake_chmod(path: object, mode: int) -> None:
        del path, mode
        raise OSError("simulated NAS permission error")

    monkeypatch.setattr(os, "chmod", fake_chmod)
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))

    with pytest.raises(SandboxSupervisorError, match="simulated NAS permission error"):
        await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())


def _plant_marker(root: Path, tenant_id: UUID, user_id: UUID) -> None:
    """按 ``NasWorkspaceStore.mark_deleted`` 的真实落点造软删标记。"""
    marker = workspace_deleted_marker(str(root), tenant_id, user_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


@pytest.mark.asyncio
async def test_acquire_refuses_deleted_workspace(tmp_path: Path) -> None:
    """软删闸 —— ``{root}/{tenant}/.deleted/{user}`` 存在时 acquire 必须拒
    (supervisor 对软删工作区同样在 acquire 拒,可观察契约一致:统一包成
    ``SandboxSupervisorError``)。"""
    tenant_id, user_id = uuid4(), uuid4()
    _plant_marker(tmp_path, tenant_id, user_id)
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))

    with pytest.raises(SandboxSupervisorError, match="workspace deleted"):
        await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    # 拒绝发生在 claim_warm 之前 —— 没有留下任何行。
    assert store.rows == {}


@pytest.mark.skipif(os.geteuid() == 0, reason="root 绕过 DAC,0o000 目录照样读得动")
@pytest.mark.asyncio
async def test_acquire_refuses_when_the_delete_marker_is_unreadable(
    tmp_path: Path,
) -> None:
    """软删闸必须 **fail closed** —— 读不动 ``.deleted/`` 时拒绝,不是放行。

    原实现用 ``Path.exists()``,它对 ``EACCES`` 的处理跨版本不一致(实测
    3.12.8/3.13.1 抛、**3.14.0 吞掉返回 ``False``**)。3.14 那条是真的
    fail-open:读不动等于判定"没被软删",已 purge 的用户被静默放行拿到沙箱。
    ``requires-python = ">=3.12"`` 允许 3.14,所以"今天生产跑 3.12"不是理由。

    读不动的窗口在发布流程里真实存在:``{tenant}/.deleted/`` 是 ``0o700``
    属主 control-plane,而 uid 统一(见
    ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md``
    § 六)之后、存量迁移 Job(Task D)
    跑之前,它的属主还是旧 uid 而进程已经是新 uid。

    **本用例在 3.12/3.13 上也有意义**:那两个版本虽然不放行,但裸
    ``PermissionError`` 穿过整个 ``acquire`` 冒上去,归因不明——断言匹配的是
    ``delete-marker unreadable`` 而不是仅仅"抛了个异常",两种坏行为都能挡。

    与本次改动其余部分同一个主题:权限失败被吞成"东西不在"。
    """
    tenant_id, user_id = uuid4(), uuid4()
    _plant_marker(tmp_path, tenant_id, user_id)
    marker = workspace_deleted_marker(str(tmp_path), tenant_id, user_id)
    marker.parent.chmod(0o000)  # 目录不可穿透 —— stat marker 撞 EACCES
    try:
        sdk, store = FakeSdk(), FakeInstanceStore()
        client = make_client(sdk, store, workspace_root=str(tmp_path))

        # 关键:不是 "workspace deleted"(那说明它读到了 marker),而是明确的
        # "读不动"——两者都拒,但归因不同,且都不能是放行。
        with pytest.raises(SandboxSupervisorError, match="delete-marker unreadable"):
            await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

        assert store.rows == {}
    finally:
        marker.parent.chmod(0o700)  # 放回去,否则 pytest 清不掉 tmp_path


@pytest.mark.asyncio
async def test_acquire_ignores_a_marker_file_written_inside_the_mounted_tree(
    tmp_path: Path,
) -> None:
    """全分支终审 C-1(跨任务接缝)—— ``{tenant}/{user}`` 整棵树经 subPath 挂
    进沙箱的 ``/workspace``,沙箱里的 agent(跑 LLM 生成的代码 / 处理带注入的
    上传文档)可以直接 ``write_file(".ew-workspace-deleted")``。软删闸读的
    marker 必须不在这棵树里,否则一次注入就能把活跃用户的工作区永久标记成待
    回收:此后每一次 acquire(含热复用)都拒,而 ``list_files`` 又把这个文件
    过滤掉,UI 上看不到任何异常。"""
    tenant_id, user_id = uuid4(), uuid4()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    # 沙箱视角:直接写 /workspace/.ew-workspace-deleted。
    (user_root / ".ew-workspace-deleted").write_text("")
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))

    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    assert sandbox_id in store.rows


@pytest.mark.asyncio
async def test_mark_deleted_then_acquire_is_refused_end_to_end(tmp_path: Path) -> None:
    """写点与读点必须真的是同一个位置 —— 两边各自的单测都不会发现它们指向了
    不同的路径(C-1 修复把 marker 搬了家,这条钉住搬完之后两侧仍然对齐)。"""
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

    tenant_id, user_id = uuid4(), uuid4()
    await NasWorkspaceStore(root=str(tmp_path)).mark_deleted(tenant_id=tenant_id, user_id=user_id)
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))

    with pytest.raises(SandboxSupervisorError, match="workspace deleted"):
        await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)


@pytest.mark.asyncio
async def test_acquire_rejects_when_workspace_deleted_appears_after_claim_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 4 审查 Important-2 —— 软删闸的 TOCTOU:初次闸只在 ``claim_warm``
    之前查一次,之后不再复查。构造"闸通过后、marker 才出现"的精确时序:
    ``claim_warm`` 真正把这次 acquire 的行提交成功之后(模拟并发
    ``mark_deleted`` 恰好插在这个窗口),marker 才落盘——不是提前造好(那
    会被初次闸本身挡住,测不出 TOCTOU)。

    断言 acquire 必须抛错,**且这次调用占到的 CAS 槽位必须被让出**——不能
    占着一行不放,那会让这个 ``(tenant, user)`` 之后所有 acquire 都卡死。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    tenant_id, user_id = uuid4(), uuid4()
    real_claim_warm = store.claim_warm

    async def _claim_then_plant_marker(
        *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str, object] | None:
        result = await real_claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
        # 模拟并发 mark_deleted 恰好在 claim_warm 提交之后写下 marker。
        _plant_marker(tmp_path, tenant_id, user_id)
        return result

    monkeypatch.setattr(store, "claim_warm", _claim_then_plant_marker)

    with pytest.raises(SandboxSupervisorError, match="workspace deleted"):
        await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    # 槽位已让出:没有残留的行,_warm 指针也没有悬空指向一个已清行的 id。
    assert store.rows == {}
    assert store.warm == {}
    assert [reason for (_sid, reason) in store.mark_destroyed_calls] == ["workspace_deleted_race"]


@pytest.mark.asyncio
async def test_acquire_soft_delete_gate_does_not_apply_to_ephemeral_sandboxes(
    tmp_path: Path,
) -> None:
    """临时沙箱没有持久身份可被软删——即使某个用户的目录碰巧有同名标记
    文件(不该发生,但也不该被临时沙箱的 acquire 误读到),``user_id=None``
    的路径压根不检查它,只检查/建自己的 scratch 子树。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))

    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t")

    assert (tmp_path / "_scratch" / str(sandbox_id)).is_dir()


@pytest.mark.asyncio
async def test_exec_injects_pythonuserbase_when_agent_key_set() -> None:
    """sandbox migration wave 2(spec 决策 10)—— ``agent_key`` 非空时随
    ``commands.run(envs=)`` 注入 ``PYTHONUSERBASE``,与本地 supervisor 后端
    同一份值(``agent_key_envs`` 单源,两后端契约见
    ``test_sandbox_runtime_contract.py``)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5, agent_key="my-agent")

    assert sdk.sandbox.commands.envs_calls[-1] == {
        "PYTHONUSERBASE": f"{SANDBOX_AGENTS_ROOT}/my-agent"
    }


@pytest.mark.asyncio
async def test_exec_omits_pythonuserbase_when_agent_key_unset() -> None:
    """默认(未绑定 agent_key 的调用方,如老测试直调 exec)不该往 envs 里塞
    任何东西 —— envs=None,不是 envs={}(避免猜 SDK 对空 dict 的语义)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5)

    assert sdk.sandbox.commands.envs_calls[-1] is None


def _dockerfile_text() -> str:
    dockerfile = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "Dockerfile"
    assert dockerfile.is_file(), f"沙箱镜像 Dockerfile 不在预期位置:{dockerfile}"
    return dockerfile.read_text(encoding="utf-8")


def _parse_dockerfile_env_and_workdir(text: str) -> tuple[dict[str, str], str | None]:
    """从 Dockerfile 文本里抽出最终的 ``ENV`` 集合与最后一条 ``WORKDIR``。

    处理反斜杠续行,以及续行块内部的注释行(``ENV`` 那段真的有一段
    ``# Read-only rootfs at runtime: ...`` 夹在中间)。
    """
    statements: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if pending and line.startswith("#"):
            continue
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        statements.append((pending + line).strip())
        pending = ""

    env: dict[str, str] = {}
    workdir: str | None = None
    for statement in statements:
        if statement.startswith("ENV "):
            for token in shlex.split(statement[len("ENV ") :]):
                key, _, value = token.partition("=")
                env[key] = value
        elif statement.startswith("WORKDIR "):
            workdir = statement[len("WORKDIR ") :].strip()
    return env, workdir


def test_image_env_matches_dockerfile() -> None:
    """镜像与客户端两份副本的漂移闸(手法同
    ``test_idle_ttl_matches_supervisor_default``)。

    Dockerfile 的 ``ENV`` 是构建期声明,编排进程运行时读不到,所以
    :data:`SANDBOX_IMAGE_ENV` 只能是第二份副本。**双向**比对:少了一条 →
    云沙箱里那个变量是空的;多了一条 → 送进去一个镜像早就不再声明的值。
    ``WORKDIR`` 同理钉住 :data:`WORKSPACE_ROOT`。

    刻意不打 ``@pytest.mark.integration``、也刻意不 ``skip``:漂移闸在跳过
    时就等于不存在(见 sandbox-contract 工作流那次"结构性报绿"的教训),
    而这个文件在仓库 checkout 里必然存在——真找不到说明目录结构变了,
    那正该红。

    沙箱迁移 W2 Task 9:镜像不再声明 ``WORKDIR``(该指令本身会创建
    ``WORKSPACE_ROOT``,与平台在这个路径建 NAS 挂载 symlink 冲突)——闸的
    期望改成"Dockerfile 里压根没有 WORKDIR",cwd 完全交给 exec 显式传
    :data:`WORKSPACE_ROOT`(见 ``AgentSandboxClient.exec`` 的
    ``commands.run(cwd=...)``,W1 全分支终审 Important-2 已经在传)。
    """
    env, workdir = _parse_dockerfile_env_and_workdir(_dockerfile_text())

    assert env == SANDBOX_IMAGE_ENV, (
        "沙箱镜像的 ENV 与 AgentSandboxClient 送进云沙箱的 SANDBOX_IMAGE_ENV 已经不一致"
        f"(Dockerfile={env} / 客户端={SANDBOX_IMAGE_ENV})——envd 派生的进程不继承镜像"
        " ENV,只吃 create(envs=) 里的这份,两边必须逐条对齐。"
    )
    assert workdir is None, (
        f"镜像不该再声明 WORKDIR(现为 {workdir!r})—— 该指令自带创建目录的"
        f" 副作用,会跟平台在 {WORKSPACE_ROOT} 建 NAS 挂载 symlink 冲突;"
        " cwd 改由 exec 显式传 WORKSPACE_ROOT。"
    )


def test_image_starts_as_root_and_leaves_workspace_free() -> None:
    """平台在 mountPath 建 symlink,且 envd 要 fork/exec 存储 helper —— 见
    spec § 二之二(``docs/superpowers/specs/2026-08-07-sandbox-migration-w2-
    design.md``)。"""
    text = _dockerfile_text()
    assert "\nUSER agent" not in text  # 容器必须 root 启动(agent 用户仍在,执行时降权)
    assert "WORKDIR /workspace" not in text  # WORKDIR 指令本身会创建目录
    assert "mkdir -p /workspace" not in text
    assert "HOME=/home/agent" in text
    assert "mkdir -p /opt/skills /opt/agents" in text


# ---------------------------------------------------------------------------
# 全分支终审 Important-6 —— create() 返回之后、set_container_id 落库之前的
# 那段窗口。失败在这里的话,沙箱已经在平台上跑着,而 destroy / reap /
# list_stuck_creating 三条回收路径全都找不到它(前两个按 container_id 找,
# 最后一个面对的正是没有 container id 的行、刻意不 connect/kill)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_failure_after_create_kills_the_sandbox_and_frees_the_slot() -> None:
    """种子文件写失败 → 沙箱被 kill、CAS 槽位让出、错误归一成 SandboxSupervisorError。

    ``write_error`` 用真的 e2b 异常类型:没有这层归一的话,按 § 6.5 契约写
    ``except SandboxSupervisorError`` 的调用方接不住它。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.sandbox.files.write_error = TimeoutException("envd unreachable")
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    with pytest.raises(SandboxSupervisorError):
        await client.acquire(
            tenant_id=tenant_id,
            thread_id="t",
            user_id=user_id,
            seed_files=(("skill.md", b"x"),),
        )

    assert sdk.sandbox.killed is True, "沙箱已经在跑却没人记得它 —— 必须当场 kill"
    assert store.warm == {}, "CAS 槽位必须让出来,否则这个 (tenant, user) 卡死"

    # 槽位真的可用了:下一次 acquire 能正常走完(不是只把 dict 清空了事)。
    sdk.sandbox.files.write_error = None
    assert await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)


@pytest.mark.asyncio
async def test_container_id_backfill_failure_kills_the_sandbox() -> None:
    """回填那一步失败同样漏沙箱 —— 而且这条路径连种子文件都不需要。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    store.set_container_id_fails = True
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    with pytest.raises(SandboxSupervisorError):
        await client.acquire(tenant_id=tenant_id, thread_id="t", user_id=user_id)

    assert sdk.sandbox.killed is True
    assert store.warm == {}


@pytest.mark.asyncio
async def test_ephemeral_post_create_failure_clears_the_row() -> None:
    """临时沙箱(无 user_id)没有 CAS 槽位,但有一行 create_ephemeral 插的行
    ——同样要 kill + 清行,否则 list_active 会一直把它当活跃会话。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.sandbox.files.write_error = RuntimeError("write failed")
    client = make_client(sdk, store)

    with pytest.raises(SandboxSupervisorError):
        await client.acquire(tenant_id=uuid4(), thread_id="t", seed_files=(("skill.md", b"x"),))

    assert sdk.sandbox.killed is True
    assert [reason for _, reason in store.mark_destroyed_calls] == ["post_create_failed"]
    assert store.rows == {}


# ---------------------------------------------------------------------------
# 再审 Important-1 —— C-1 的接管让"槽位当前的主人不是我"成为可达状态,而所有
# 按 (tenant, user) 坐标定位的清理都会误伤接管者。
#
# 这两条用**真的** InMemorySandboxInstanceStore(不是本文件的 FakeInstanceStore):
# 要验的正是"C-1 的接管发生之后清理动了谁的行",而接管那条谓词只活在两个生产
# store 里,手写假件复刻一份就等于拿测试替身给自己作证。
# ---------------------------------------------------------------------------


def _backdate_pending_rows(store: InMemorySandboxInstanceStore) -> None:
    """把所有还没回填 ``container_id`` 的行拨老到超过 ``_STUCK_CREATE_TTL_S``。

    等价于"持有这些行的那个 pod 卡了 5 分钟以上"——C-1 接管的前置条件。公开
    API 造不出"很久以前",与两个 store 自己的同款测试用 backdate 是一个手法。
    """
    long_ago = datetime.now(UTC) - timedelta(seconds=_STUCK_CREATE_TTL_S * 2)
    for row in store._rows.values():
        if row.container_id is None:
            row.acquired_at = long_ago


@pytest.mark.asyncio
async def test_unwind_after_a_takeover_leaves_the_new_owners_row_alone() -> None:
    """A 被 B 合法接管之后再失败,清理**不能**动 B 那行。

    交错(C-1 之前不可能出现,之后是预期状态):

    1. A ``claim_warm`` 拿到槽位,行是 ``IN_USE`` + ``container_id=NULL``;
    2. A 卡在 ``create()`` 里超过 ``_STUCK_CREATE_TTL_S``(pod 被冻/慢盘/
       网络分区,5 分钟不是理论值,C-1 就是为真实发生过的这件事修的);
    3. B 的 ``acquire`` 走 C-1 的接管:清掉 A 的过期行、自己 INSERT、建沙箱、
       回填 container_id —— B 现在是**合法**的槽位主人,沙箱活着;
    4. A 的 ``create()`` 终于返回,但紧接着的种子文件写失败 → 走 Important-6
       那层守卫(kill 自己的沙箱 + ``_unwind_slot``)。

    修复前 ``_unwind_slot`` 调 ``drop_warm(tenant_id, user_id)`` —— 按坐标删
    "此刻占着槽位的那一行",也就是 **B 的**。后果不是"多清了一行"那么轻:B
    的 microVM 还在平台上跑着,但它的行没了 ⇒ ``list_active`` 看不见它 ⇒ 周期
    reaper 永远收不走,只剩 ``release()`` 或 20 分钟平台超时兜底。
    """
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id = uuid4(), uuid4()

    sdk_b = FakeSdk(sandbox=FakeSandbox(sandbox_id="sbx-B"))
    client_b = make_client(sdk_b, store)
    b_id: UUID | None = None

    async def _b_takes_over_while_a_is_still_creating() -> None:
        nonlocal b_id
        _backdate_pending_rows(store)
        b_id = await client_b.acquire(tenant_id=tenant_id, thread_id="B", user_id=user_id)

    sdk_a = FakeSdk(sandbox=FakeSandbox(sandbox_id="sbx-A"))
    sdk_a.on_create = _b_takes_over_while_a_is_still_creating
    sdk_a.sandbox.files.write_error = TimeoutException("envd unreachable")
    client_a = make_client(sdk_a, store)

    with pytest.raises(SandboxSupervisorError):
        await client_a.acquire(
            tenant_id=tenant_id,
            thread_id="A",
            user_id=user_id,
            seed_files=(("skill.md", b"x"),),
        )

    assert b_id is not None, "前置没成立:B 的接管压根没跑到"
    assert sdk_a.sandbox.killed is True, "A 自己那个没人记得的沙箱仍然必须被 kill"
    assert sdk_b.sandbox.killed is False, "B 的沙箱不是 A 建的,一根汗毛都不能碰"
    # 核心断言:B 那行还在,并且仍然是 reap 看得见的活跃行。
    assert await store.get_container_id(sandbox_id=b_id) == "sbx-B"
    assert await store.is_warm_session(sandbox_id=b_id) is True
    assert await store.list_active(only_idle=False) == [(b_id, "sbx-B")], (
        "B 的行必须仍然出现在 list_active 里 —— 否则周期 reaper 永远收不走它那个还活着的 microVM"
    )
    # 槽位仍归 B:下一次 acquire 应当复用 B,而不是发现槽位空了又建一个。
    claim_result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())
    assert claim_result is not None
    b_winner_id, b_container_id, b_acquired_at = claim_result
    assert (b_winner_id, b_container_id) == (b_id, "sbx-B")
    assert b_acquired_at is not None


@pytest.mark.asyncio
async def test_caller_a_fails_loudly_after_its_row_was_taken_over() -> None:
    """再审 Important-2 的客户端侧后果——被接管的调用方 A 自己会怎样。

    与 ``test_unwind_after_a_takeover_leaves_the_new_owners_row_alone`` 同一个
    交错,但 A 这次**不**在种子文件上失败:它一路走到回填。它的行早就被 B 接
    管掉了,所以 ``set_container_id`` 按契约抛错(再审 Important-2:两个生产
    store 现在都 raise,而不是 SQL 静默 no-op、内存 KeyError 各走各的)。

    修复前的 SQL 语义下这里是最坏的结果:回填静默成功 → ``acquire`` 正常返回
    → 一个 ``(tenant, user)`` 上两个活 microVM,A 那个的行是终态,
    ``list_active`` 看不见 ⇒ 周期 reap 永远收不走它,只有 ``release()``
    (进程活到那一步的话)或 20 分钟平台超时能收。

    修复后:抛错被 Important-6 的守卫接住 —— kill 掉 A 自己那个没人记得的沙
    箱、unwind(对早已销毁的 A 行是 no-op,按 N-1 不碰 B)、冒泡成可重试的
    ``SandboxSupervisorError``。B 全程无感。
    """
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id = uuid4(), uuid4()

    sdk_b = FakeSdk(sandbox=FakeSandbox(sandbox_id="sbx-B"))
    client_b = make_client(sdk_b, store)
    b_id: UUID | None = None

    async def _b_takes_over_while_a_is_still_creating() -> None:
        nonlocal b_id
        _backdate_pending_rows(store)
        b_id = await client_b.acquire(tenant_id=tenant_id, thread_id="B", user_id=user_id)

    sdk_a = FakeSdk(sandbox=FakeSandbox(sandbox_id="sbx-A"))
    sdk_a.on_create = _b_takes_over_while_a_is_still_creating
    client_a = make_client(sdk_a, store)

    with pytest.raises(SandboxSupervisorError, match="post-create setup failed"):
        await client_a.acquire(tenant_id=tenant_id, thread_id="A", user_id=user_id)

    assert b_id is not None, "前置没成立:B 的接管压根没跑到"
    assert sdk_a.sandbox.killed is True, (
        "A 的行已经不归它了,回填必须抛错、沙箱必须当场 kill —— 否则这是一个"
        "谁也回收不了的 microVM(reap 按行找,而那一行是终态)"
    )
    assert sdk_b.sandbox.killed is False
    assert await store.list_active(only_idle=False) == [(b_id, "sbx-B")], (
        "整个 (tenant, user) 上只该剩 B 一个活跃沙箱"
    )


@pytest.mark.asyncio
async def test_warm_reconnect_failure_only_clears_the_row_it_could_not_reach() -> None:
    """同一个根因的第二处:``acquire`` 的"重连失败则重建"分支。

    它拿着 ``claim_warm`` 刚返回的赢家 ``sandbox_id``,所以照样该按 id 清那一
    行,而不是按 ``(tenant, user)`` 清"此刻占着槽位的那一行"。**不争用**时两
    种写法效果一样(``test_connect_failure_rebuilds`` 覆盖那一半),所以这里
    必须构造争用,否则这条测试对着按坐标清的旧写法照样绿:

    1. A、B 拿到同一个赢家行(平台已经把那个沙箱超时 kill 了,两边都连不上);
    2. B 先跑完整条重建链:清掉连不上的旧行 → 重新占坑 → 建 ``sbx-B`` → 回填。
       B 现在是槽位的合法主人,沙箱活着;
    3. A 这才从失败的 ``connect`` 里出来,开始它自己的清理。

    按坐标清 = 清掉 **B 那行**,A 随后重新占坑成功、也建出自己的沙箱 —— 结果
    是一个 ``(tenant, user)`` 上两个活 microVM,B 那个还没人记得。按 id 清则
    是对早已销毁的旧行 no-op,A 重新占坑输给 B,后续回填按契约抛错、刚建的
    沙箱被 Important-6 的守卫 kill 掉,错误冒泡成可重试的 ``SandboxSupervisorError``。
    """
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id = uuid4(), uuid4()

    sdk_seed = FakeSdk()
    stale_id = await make_client(sdk_seed, store).acquire(
        tenant_id=tenant_id, thread_id="seed", user_id=user_id
    )

    sdk_b = FakeSdk(sandbox=FakeSandbox(sandbox_id="sbx-B"), connect_fails=True)
    client_b = make_client(sdk_b, store)
    b_id: UUID | None = None

    async def _b_finishes_its_own_rebuild_first() -> None:
        nonlocal b_id
        if b_id is None:  # 只在 A 第一次 connect 时插队,别递归
            b_id = await client_b.acquire(tenant_id=tenant_id, thread_id="B", user_id=user_id)

    sdk_a = FakeSdk(sandbox=FakeSandbox(sandbox_id="sbx-A"), connect_fails=True)
    sdk_a.on_connect = _b_finishes_its_own_rebuild_first
    client_a = make_client(sdk_a, store)

    with pytest.raises(SandboxSupervisorError):
        await client_a.acquire(tenant_id=tenant_id, thread_id="A", user_id=user_id)

    assert b_id is not None and b_id != stale_id, "前置没成立:B 的重建没跑到"
    assert sdk_b.sandbox.killed is False, "B 的沙箱不是 A 建的,一根汗毛都不能碰"
    assert sdk_a.sandbox.killed is True, "A 自己那个没人记得的沙箱必须被 kill"
    assert await store.get_container_id(sandbox_id=stale_id) is None, "连不上的旧行必须清掉"
    assert await store.list_active(only_idle=False) == [(b_id, "sbx-B")], (
        "槽位与唯一的活跃行都该归 B —— A 不能既清掉 B 的行又留下第二个活 microVM"
    )


@pytest.mark.asyncio
async def test_seed_failure_on_warm_reuse_does_not_kill_the_reused_sandbox() -> None:
    """复用别人早就建好的热会话时,种子文件写失败不该把那个沙箱连锅端了
    ——它不是这次调用建的,那一行也已经健康登记过。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()
    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)

    sdk.sandbox.files.write_error = RuntimeError("write failed")
    with pytest.raises(SandboxSupervisorError):
        await client.acquire(
            tenant_id=tenant_id,
            thread_id="t2",
            user_id=user_id,
            seed_files=(("skill.md", b"x"),),
        )

    assert sdk.sandbox.killed is False, "复用的热会话不是本次建的,不能拆"
    assert store.warm[(tenant_id, user_id)] is not None, "热会话槽位应原样保留"


# ---------------------------------------------------------------------------
# 全分支终审 Important-4 —— 不传 timeout 的话 e2b 默认 300s + on_timeout=kill
# (集群实测 end_at - started_at 正好 300s、lifecycle=None),沙箱 5 分钟就没。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_passes_an_explicit_timeout() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    assert sdk.created[-1]["timeout"] == _SANDBOX_TIMEOUT_S, (
        "不显式传 timeout 则吃 e2b 的 300s 默认值 —— 沙箱 5 分钟就被平台 kill"
    )


@pytest.mark.asyncio
async def test_warm_reconnect_extends_the_platform_timeout() -> None:
    """平台的存活钟从**建**沙箱起算,``connect`` 只延长不缩短 —— 复用时不
    重新传 timeout 的话,一个被反复使用的热会话仍然在"原始创建 + 20 分钟"
    被杀,"热"的部分越用越短。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert sdk.connected, "第二次 acquire 应该复用热会话(走 connect)"
    assert sdk.connect_kwargs[-1]["timeout"] == _SANDBOX_TIMEOUT_S


def test_platform_timeout_outlives_idle_ttl() -> None:
    """设计不变式:我们自己的空闲 reap 是主角,平台超时只是兜底。

    ``_SANDBOX_TIMEOUT_S`` 一旦被调到 ``_IDLE_TTL_S`` 以下,两者就从"主备"
    变成"互抢"——平台会先掐掉一个还没到我们空闲线的活跃热会话,表现是随机
    的 connect 失败 + 白付 35-40s 冷启,而不是任何一条报错。这条断言和
    ``test_idle_ttl_matches_supervisor_default`` 一样刻意不打 integration
    marker:它只比较两个 Python 常量。
    """
    from expert_work.persistence.sandbox_instance_store import _IDLE_TTL_S

    assert _SANDBOX_TIMEOUT_S > _IDLE_TTL_S, (
        f"平台超时 {_SANDBOX_TIMEOUT_S}s 必须大于热会话空闲 TTL {_IDLE_TTL_S}s,"
        " 否则平台会抢在 reap 之前掐掉还没空闲够的热会话。"
    )
    assert _SANDBOX_TIMEOUT_S - _IDLE_TTL_S >= 240, (
        "余量至少要覆盖 SandboxReapWorker 的一个完整扫描周期(240s),"
        " 否则 reap 可能刚好错过一轮、让平台先动手。"
    )


# ---------------------------------------------------------------------------
# #1b —— 热会话年龄封顶:token 只在 create(envs=...) 送一次、无法重发(见模块
# docstring 再审 Important-3),持续被复用的热会话会活过自己的出网 token,
# 之后每次出网都是 407 且没有任何自愈路径。acquire 的复用分支在 connect 之前
# 先检查赢家那行的 acquired_at,超过 egress_token_ttl_s // 2 就强制重建。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_rebuilds_warm_session_past_age_cap() -> None:
    """超过 egress_token_ttl_s//2 的热会话在下次 acquire 被强制重建。

    token 只在 create(envs=...) 送一次(重发三条路 2026-08-04 已否决,
    见 agent_sandbox.py 模块 docstring),活过 24h 的会话出网全 407 且
    永不自愈 —— 年龄封顶是唯一自愈路径。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    old_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    store.rows[old_id]["acquired_at"] = datetime.now(UTC) - timedelta(
        seconds=client._max_warm_age_s() + 60
    )

    new_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert new_id != old_id, "超龄必须重建,不能复用旧 sandbox_id"
    assert (old_id, _WARM_AGE_DESTROY_REASON) in store.mark_destroyed_calls
    assert len(sdk.created) == 2, "必须真的重建(第二次 sdk.create),不是复用"
    assert store.rows[new_id]["container_id"] == "sbx-1"


@pytest.mark.asyncio
async def test_acquire_reuses_warm_session_under_age_cap() -> None:
    """acquired_at 在封顶内(1 小时前,远小于默认 12h 上限)→ 走 connect 复用,
    sdk.create 不被调用,返回旧 winner_id —— 守住"封顶不误伤"。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    first_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    store.rows[first_id]["acquired_at"] = datetime.now(UTC) - timedelta(hours=1)

    second_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 1, "年龄封顶内不该重建"
    assert second_id == first_id


@pytest.mark.asyncio
async def test_acquire_never_age_caps_null_acquired_at() -> None:
    """第三元为 None → 年龄不可知,不封顶,照常复用(同 C-1 拒绝接管的姿态)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    first_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    store.rows[first_id]["acquired_at"] = None

    second_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 1, "acquired_at 缺失时年龄不可知,不该被封顶重建"
    assert second_id == first_id


# ---------------------------------------------------------------------------
# Task 9 —— AgentSandboxClient.reap:以 sandbox_instance 表为准,不问 SDK
# 账号级的 list()(见类 docstring "Task 9 对 brief 草稿的偏离"一节)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_force_kills_every_active_sandbox_of_ours() -> None:
    """force=True 拆掉表里记着的每个活跃沙箱,返回拆除数,并放出热会话坑。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    t1, t2 = uuid4(), uuid4()
    u1, u2 = uuid4(), uuid4()
    await client.acquire(tenant_id=t1, thread_id="a", user_id=u1)
    sdk.sandbox = FakeSandbox(sandbox_id="sbx-2")
    await client.acquire(tenant_id=t2, thread_id="b", user_id=u2)

    reaped = await client.reap(force=True)

    assert reaped == 2
    assert store.rows == {}
    assert store.warm == {}, "热会话坑必须放出来,否则 (tenant, user) 再也 acquire 不到"


@pytest.mark.asyncio
async def test_reap_survives_a_row_that_fails_to_clear() -> None:
    """一行清不掉不能掐断整趟清扫 —— reap 现在是云后端唯一的回收机制,
    也是"卡死行"的人工恢复入口,一行拖垮一轮的代价比以前高。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    ids = []
    for i in range(3):
        sdk.sandbox = FakeSandbox(sandbox_id=f"sbx-{i}")
        ids.append(await client.acquire(tenant_id=uuid4(), thread_id=f"t{i}", user_id=uuid4()))
    store.mark_destroyed_fails_for = {ids[0]}

    reaped = await client.reap(force=True)

    assert reaped == 2, "坏行不计数,但其余两行仍然被回收"
    assert set(store.rows) == {ids[0]}, "只剩那一行没清掉,下一轮再收"


@pytest.mark.asyncio
async def test_reap_survives_a_stuck_creating_row_that_fails_to_clear() -> None:
    """孤儿行(``list_stuck_creating``)那个循环同理 —— 两个循环都要逐行包。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    bad, good = uuid4(), uuid4()
    for sandbox_id in (bad, good):
        store.rows[sandbox_id] = {"tenant_id": uuid4(), "user_id": uuid4()}
    store.stuck_creating_sandbox_ids = {bad, good}
    store.mark_destroyed_fails_for = {bad}

    reaped = await client.reap(force=True)

    assert reaped == 1
    assert set(store.rows) == {bad}


@pytest.mark.asyncio
async def test_reap_without_force_only_reaps_rows_the_store_marks_idle() -> None:
    """``force=False``(默认路径)必须把 ``only_idle=True`` 传给
    ``store.list_active`` —— 只拆 store 判定为空闲的行,活跃的留着不动。

    这里不建模真实的 last_used_at/acquired_at TTL 计算(那是
    ``SqlSandboxInstanceStore.list_active`` 的职责,由
    ``test_sql_sandbox_instance_store.py`` 的容器集成测覆盖);只验证
    ``AgentSandboxClient.reap`` 把 ``force`` 正确翻译成
    ``only_idle=not force`` 并如实按 ``list_active`` 的返回值行动 —— 用
    ``FakeInstanceStore.idle_sandbox_ids`` 直接指定哪个 sandbox_id 是"空闲
    的",不掺时间戳。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    busy_id = await client.acquire(tenant_id=uuid4(), thread_id="a", user_id=uuid4())
    sdk.sandbox = FakeSandbox(sandbox_id="sbx-2")
    idle_id = await client.acquire(tenant_id=uuid4(), thread_id="b", user_id=uuid4())
    store.idle_sandbox_ids.add(idle_id)

    reaped = await client.reap(force=False)

    assert reaped == 1
    assert idle_id not in store.rows, "标记为空闲的行必须被拆"
    assert busy_id in store.rows, "没被标记为空闲的活跃行不该被碰"


@pytest.mark.asyncio
async def test_reap_cleans_row_even_when_sandbox_already_gone() -> None:
    """沙箱在平台侧已经不存在(超时被平台 kill / 被平台回收 / 手工删了)时,
    ``connect`` 会失败 —— 但行必须仍然被清掉,否则热会话槽位永远占着,该
    ``(tenant, user)`` 被迁移 0141 的部分唯一索引挡住、再也 acquire 不到,
    与 Task 7 修过的"CAS 行卡死"是同一类故障。这条不变式很容易在重构中
    丢失(比如把 ``mark_destroyed`` 挪进 ``try`` 块,一旦 ``connect`` 抛异常
    就跳过它),单独验证。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()
    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="a", user_id=user_id)
    sdk.connect_fails = True

    reaped = await client.reap(force=True)

    assert reaped == 1, "即使连不上,也要算作已清理"
    assert sandbox_id not in store.rows, "行必须被清掉,热会话槽位必须放出来"
    assert (tenant_id, user_id) not in store.warm, "热会话坑必须放出来,否则再也 acquire 不到"


# ---------------------------------------------------------------------------
# 独立审查 Important-1/2(task-9-report.md 有完整记录)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_force_clears_orphaned_stuck_creating_row() -> None:
    """Important-1:编排进程在 ``claim_warm`` 提交行之后、
    ``set_container_id`` 回填之前死掉(pod OOM-kill / 驱逐 / 滚动更新,按
    本系统的多副本生产设计这些都是预期会发生的事件)留下的孤儿——
    ``container_id`` 永远是 NULL,``list_active()`` 两种模式都不会返回它
    (正常"还在合法冷启窗口内"的行也长这样,不能靠 list_active 处理)。
    ``force=True`` 必须能通过 ``list_stuck_creating()`` 把它清掉,并且不
    尝试 connect/kill——压根没有容器可连。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    # 直接对 store 占坑、不让它走到 set_container_id —— 精确模拟"死在两次
    # 写之间",不依赖 acquire() 的完整流程凑巧卡在那。
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )
    store.stuck_creating_sandbox_ids.add(sandbox_id)

    reaped = await client.reap(force=True)

    assert reaped == 1
    assert sandbox_id not in store.rows, "孤儿行必须被清掉,热会话槽位必须放出来"
    assert (tenant_id, user_id) not in store.warm
    assert sdk.connected == [], "没有容器可连,不该尝试 connect"


@pytest.mark.asyncio
async def test_reap_without_force_ignores_stuck_creating_rows() -> None:
    """Important-1 的范围限定:孤儿创建行的清理只在 ``force=True`` 时发生
    ——``force=False`` 的常规空闲清扫不该碰"看起来还在创建"的行,那会跟
    一个正在合法冷启窗口内的 ``acquire()`` 抢同一行。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )
    store.stuck_creating_sandbox_ids.add(sandbox_id)

    reaped = await client.reap(force=False)

    assert reaped == 0
    assert sandbox_id in store.rows, "force=False 不该碰还在创建中的行"


@pytest.mark.asyncio
async def test_exec_touches_last_used_at() -> None:
    """Important-2:``exec()`` 必须推进 ``last_used_at``,否则
    ``force=False`` 的空闲 TTL 清扫实际是按 acquire 时间清扫,一个持续被
    ``exec`` 的热会话会被误判成空闲、被误杀。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5)

    assert sid in store.touched


def test_repr_does_not_leak_credential_fields() -> None:
    """独立审查 I-2:``field(repr=False)`` 标在 ``api_key``/
    ``egress_token_secret`` 上(agent_sandbox.py 那两行紧邻的注释写的
    "Task 10 实测:普通 dataclass 的 __repr__ 会把凭据吐进 pytest
    traceback")此前只用一个手工脚本验证过,没有进仓库的回归测试。

    这条测试是**字段枚举式**的:它守的是现有这两个字段不回退,守不住
    新加的第三个凭据字段——那个字段漏标 ``repr=False`` 时本测试照样绿。
    加新凭据字段的人必须自己在这里补一行断言。
    """
    client = AgentSandboxClient(
        domain="gw.example.com",
        api_key="MARKER-API-KEY-DO-NOT-LEAK",
        template="expert-work-sandbox",
        store=FakeInstanceStore(),
        sdk=FakeSdk(),
        egress_token_secret="MARKER-EGRESS-SECRET-DO-NOT-LEAK",
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
    )

    rendered = repr(client)

    assert "MARKER-API-KEY-DO-NOT-LEAK" not in rendered
    assert "MARKER-EGRESS-SECRET-DO-NOT-LEAK" not in rendered


# ---------------------------------------------------------------------------
# 全分支终审 Important-1 —— release() 对无 user_id 的沙箱必须真销毁。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_keeps_a_user_scoped_warm_session_alive() -> None:
    """带 ``user_id`` 的热会话:``release`` 保温,不 kill、不清行——留给该
    用户的下一次 run,由空闲 TTL 清扫回收。与本地 supervisor 的 ``release``
    同语义(``supervisor.py:345-364``)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()
    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)

    await client.release(sandbox_id=sandbox_id)

    assert sdk.sandbox.killed is False
    assert sandbox_id in store.rows, "热会话行必须留着,container_id 是下次 connect 的凭据"
    assert store.mark_destroyed_calls == []


@pytest.mark.asyncio
async def test_release_destroys_an_ephemeral_sandbox() -> None:
    """全分支终审 Important-1 的核心:``release`` 此前对两种情况都无条件
    ``return None``。``run_in_sandbox``(``sandbox.py:444-462``)是每次工具
    调用 acquire+release 一次,``ctx.user_id`` 又是 ``UUID | None`` —— 云
    后端下每一次无 user 的工具调用都漏一个活的 microVM,外加一行永远停在
    ``IN_USE``/``destroyed_at IS NULL`` 的 ``sandbox_instance``。本地
    supervisor 没有这个洞正是因为它这条分支走的是 ``destroy``。
    """
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t1")

    await client.release(sandbox_id=sandbox_id)

    assert sdk.sandbox.killed is True, "临时沙箱必须真 kill,否则 microVM 一直活着"
    assert store.mark_destroyed_calls == [(sandbox_id, "release")], (
        "行必须被标记销毁,且 reason 与本地 supervisor 的 DESTROY_REASON_RELEASE 一致"
    )
    assert sandbox_id not in store.rows


@pytest.mark.asyncio
async def test_release_removes_the_empty_scratch_dir(tmp_path: Path) -> None:
    """全分支终审 M-3 —— ``run_in_sandbox`` 每次工具调用 acquire+release 一
    次,无 user 的 run 因此每调用一次工具就在 NAS 上留一个 ``_scratch/<id>``
    空目录。释放时顺手 ``rmdir``,把"按工具调用数无界增长"压回零。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t1")
    scratch = tmp_path / "_scratch" / str(sandbox_id)
    assert scratch.is_dir(), "acquire 应当先建出 scratch 子树(挂载点必须存在)"

    await client.release(sandbox_id=sandbox_id)

    assert not scratch.exists()


@pytest.mark.asyncio
async def test_release_keeps_a_non_empty_scratch_dir(tmp_path: Path) -> None:
    """目录里真有 agent 写出的字节 —— ``rmdir`` 失败(非空),留着不动:这条
    清理路径只负责收掉自己建出来的空壳,不负责递归删用户数据。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t1")
    scratch = tmp_path / "_scratch" / str(sandbox_id)
    (scratch / "out.txt").write_text("agent output")

    await client.release(sandbox_id=sandbox_id)  # 不抛

    assert (scratch / "out.txt").is_file()


@pytest.mark.asyncio
async def test_release_does_not_touch_the_user_tree_of_a_warm_session(tmp_path: Path) -> None:
    """保温分支压根不走清理 —— 断言用户子树原样在,防止把 ``_scratch``
    清理误接到热会话上(那会删掉用户工作区目录)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_root=str(tmp_path))
    tenant_id, user_id = uuid4(), uuid4()
    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)

    await client.release(sandbox_id=sandbox_id)

    assert (tmp_path / str(tenant_id) / str(user_id)).is_dir()
    assert sdk.sandbox.killed is False


@pytest.mark.asyncio
async def test_release_keeps_warm_when_the_store_lookup_fails() -> None:
    """``release`` 是清理路径不是取数路径:store 抖一下(连接池耗尽/网络
    分区)不该把一次本来已经跑完的工具调用变成错误,也不该在判据不明时
    去 kill 一个可能正被用户使用的热会话。两种误判取代价小的那个。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sandbox_id = await client.acquire(tenant_id=uuid4(), thread_id="t1")
    store.is_warm_session_fails = True

    await client.release(sandbox_id=sandbox_id)

    assert sdk.sandbox.killed is False
    assert store.mark_destroyed_calls == []


# ---------------------------------------------------------------------------
# #8 —— 云后端租户沙箱配额。对称 supervisor 的 ``_enforce_quota``
# (``sandbox_supervisor/supervisor.py:713-727``),一处刻意更强:
# insert-then-check——本次调用自己的行已经落库,count 必然含自己,条件严格
# ``>``,超限则 ``_unwind_slot`` 拆自己的行再抛。只挂在**全新建行**路径上
# (``acquire`` 的 ``existing is None`` 分支);热会话复用与年龄封顶/重连失败
# 的 1 换 1 重建路径不查——见 ``AgentSandboxClient._enforce_quota`` 的
# docstring。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_rejects_when_tenant_quota_exhausted() -> None:
    """预置 ``default_max_sandboxes`` 个已存在的活跃沙箱、limit 未设(回落
    默认)→ 新建路径必炸,``sdk.create`` 从未被调用,本次调用自己插的那一行
    被 unwind(``mark_destroyed`` reason ``"quota_exceeded"``),不留死行。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id = uuid4()
    for _ in range(client.default_max_sandboxes):
        await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=uuid4())

    with pytest.raises(SandboxSupervisorError, match="quota"):
        await client.acquire(tenant_id=tenant_id, thread_id="t1")

    assert len(sdk.created) == 0, "配额闸必须在 sdk.create() 之前拦截"
    quota_unwinds = [c for c in store.mark_destroyed_calls if c[1] == "quota_exceeded"]
    assert len(quota_unwinds) == 1
    unwound_id, _ = quota_unwinds[0]
    assert unwound_id not in store.rows, "本次调用自己插的行必须被拆掉,不留死行"
    assert await store.count_active_for_tenant(tenant_id=tenant_id) == client.default_max_sandboxes


@pytest.mark.asyncio
async def test_acquire_warm_reuse_skips_quota_check() -> None:
    """已有可复用热会话 + count 已到(甚至超过)上限 → 复用照常成功——supervisor
    同款语义:复用不新建、不检查。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    first_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    for _ in range(client.default_max_sandboxes + 5):
        await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=uuid4())

    second_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert second_id == first_id
    assert len(sdk.created) == 1, "复用分支不该走 sdk.create()"


@pytest.mark.asyncio
async def test_acquire_respects_tenant_quota_row_over_default() -> None:
    """租户自己的 ``sandboxes`` 配额行覆盖默认值:limit=2,已有 1 个活跃 →
    新建成功(本次插入后 count=2,不严格大于 limit);已有 2 个活跃 → 拒绝
    (本次插入后 count=3 > limit=2)。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    store.quota_limit = 2

    tenant_id = uuid4()
    await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=uuid4())
    sandbox_id = await client.acquire(tenant_id=tenant_id, thread_id="t1")
    assert sandbox_id in store.rows

    other_tenant_id = uuid4()
    for _ in range(2):
        await store.create_ephemeral(tenant_id=other_tenant_id, sandbox_id=uuid4())
    with pytest.raises(SandboxSupervisorError, match="quota"):
        await client.acquire(tenant_id=other_tenant_id, thread_id="t2")


@pytest.mark.asyncio
async def test_acquire_rebuilds_over_age_warm_session_at_quota_limit() -> None:
    """租户**已经在配额上限**(``count_active_for_tenant`` == limit,含即将
    被重建的这一行本身)时,年龄封顶的 1 换 1 重建路径必须照常成功 ——
    ``_enforce_quota`` 只挂在 :meth:`AgentSandboxClient.acquire` 的全新建行
    分支(``existing is None``)上,这是刻意的设计决定(见其 docstring):
    重建不改变总活跃行数,查了反而会把已到上限租户的存量热会话变成不可
    重建的死会话。

    这条测试钉住这个决定本身:如果未来有人把 ``_enforce_quota`` 挪到分支
    分裂点之上、统一套在所有路径上,租户在上限时的这次重建就会被拒
    ——这里必须变红。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    old_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    for _ in range(client.default_max_sandboxes - 1):
        await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=uuid4())
    assert await store.count_active_for_tenant(tenant_id=tenant_id) == client.default_max_sandboxes
    store.rows[old_id]["acquired_at"] = datetime.now(UTC) - timedelta(
        seconds=client._max_warm_age_s() + 60
    )

    new_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert new_id != old_id, "超龄必须重建,不能复用旧 sandbox_id"
    assert len(sdk.created) == 2, "配额已满不该拦住 1 换 1 重建(第二次 sdk.create 必须发生)"
    assert (old_id, _WARM_AGE_DESTROY_REASON) in store.mark_destroyed_calls
    quota_unwinds = [c for c in store.mark_destroyed_calls if c[1] == "quota_exceeded"]
    assert quota_unwinds == [], "重建路径不查配额,不该有任何 quota_exceeded 的 unwind"


@pytest.mark.asyncio
async def test_acquire_rebuilds_reconnect_failed_warm_session_at_quota_limit() -> None:
    """同上一条,换成重连失败(spec § 6.3)那条 1 换 1 重建路径:租户已在配额
    上限时,``connect`` 炸掉之后的强制重建也必须照常成功 —— 同一份
    ``_enforce_quota`` docstring 说的是"年龄封顶/重连失败的 1 换 1 重建路径
    都不查",两条路径都要各自钉住,免得只保住一条、另一条被后续重构悄悄
    套上配额闸而没有任何测试发现。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    old_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    for _ in range(client.default_max_sandboxes - 1):
        await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=uuid4())
    assert await store.count_active_for_tenant(tenant_id=tenant_id) == client.default_max_sandboxes
    sdk.connect_fails = True

    new_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert new_id != old_id, "连不上必须重建,不能返回旧 sandbox_id"
    assert len(sdk.created) == 2, "配额已满不该拦住 1 换 1 重建(第二次 sdk.create 必须发生)"
    assert (old_id, "warm_reconnect_failed") in store.mark_destroyed_calls
    quota_unwinds = [c for c in store.mark_destroyed_calls if c[1] == "quota_exceeded"]
    assert quota_unwinds == [], "重建路径不查配额,不该有任何 quota_exceeded 的 unwind"
