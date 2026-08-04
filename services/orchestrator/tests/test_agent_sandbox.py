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

import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from e2b import CommandExitException, TimeoutException

from orchestrator.tools.agent_sandbox import (
    _SANDBOX_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_S,
    SANDBOX_EXEC_USER,
    SANDBOX_IMAGE_ENV,
    WORKSPACE_ROOT,
    AgentSandboxClient,
)
from orchestrator.tools.sandbox import EgressContext, SandboxSupervisorError


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
    ):
        self.calls.append((cmd, timeout, user, cwd))
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

    async def create(self, **kwargs):
        self.created.append(kwargs)
        if self.create_fails:
            raise RuntimeError("sandbox create failed")
        return self.sandbox

    async def connect(self, sandbox_id: str, **kwargs):
        self.connected.append(sandbox_id)
        self.connect_kwargs.append(kwargs)
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
    #: 二审 Critical —— 覆盖 "回滚本身也失败" 这条路径。
    drop_warm_fails: bool = False
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

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        """占坑成功返 None;已被别人占且赢家已就绪返
        ``(赢家 sandbox_id, container_id)``;赢家还在创建中则 raise。"""
        key = (tenant_id, user_id)
        winner_id = self.warm.get(key)
        if winner_id is None:
            self.warm[key] = sandbox_id
            self.rows[sandbox_id] = {"tenant_id": tenant_id, "user_id": user_id}
            return None
        container_id = self.rows[winner_id].get("container_id")
        if container_id:
            return (winner_id, container_id)
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
        self.rows[sandbox_id]["container_id"] = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        if sandbox_id in self.mark_destroyed_fails_for:
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

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        if self.drop_warm_fails:
            raise RuntimeError("drop_warm unavailable (db blip)")
        self.warm.pop((tenant_id, user_id), None)

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


def make_client(sdk: FakeSdk, store: FakeInstanceStore) -> AgentSandboxClient:
    return AgentSandboxClient(
        domain="gw.example.com",
        api_key="k",
        template="expert-work-sandbox",
        store=store,
        sdk=sdk,
        egress_token_secret="s3cret",
        egress_proxy_host="credential-proxy.expert-work.svc.cluster.local",
        egress_proxy_port=8081,
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
    """临时沙箱没有热会话坑可 ``drop_warm``(它压根不参与 0141 CAS),但
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
    ``sdk.create()`` 失败,必须 ``drop_warm`` 把槽位放出来 —— 否则该
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
    """二审 Critical:``_create_and_track`` 的清理步骤(``drop_warm``)自己
    也可能失败(DB 抖动/连接池耗尽/网络分区)。撤销清理前,``except
    Exception: ... await self.store.drop_warm(...); raise`` 里如果
    ``drop_warm`` 抛出,第 392 行的 ``raise`` 永远执行不到 —— 冒出去的是
    ``drop_warm`` 的异常,不是原始的 ``_create()`` 失败:①调用方按
    ``SandboxSupervisorError`` 设计的 except 链接不住(类型变了)②热会话
    行照样卡死(回滚没做成),Critical-2 描述的症状在自己的失败路径上原样
    重演,只是往下多埋一层。

    这里验证修复后的行为:``drop_warm`` 失败被吞掉(只记日志),但外层
    重新抛出的仍然是 ``_create()`` 触发的那个 ``SandboxSupervisorError``。
    """
    sdk = FakeSdk(create_fails=True)
    store = FakeInstanceStore(drop_warm_fails=True)
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
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(
        tenant_id=uuid4(),
        thread_id="t1",
        user_id=uuid4(),
        seed_files=(("skills/a.md", b"hello"),),
    )

    assert sdk.sandbox.files.written == [("/workspace/skills/a.md", b"hello", SANDBOX_EXEC_USER)]


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
    """
    import orchestrator.tools.e2b_patch as mod

    monkeypatch.setenv("E2B_DOMAIN", "stale.example.com")
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setattr(mod, "_e2b_patched", False)
    monkeypatch.setattr("kruise_agents.patch_e2b.patch_e2b", lambda **_: None)

    with caplog.at_level(logging.WARNING, logger="orchestrator.tools.e2b_patch"):
        mod._ensure_e2b_patched(domain="gw.example.com", api_key="k")

    assert "stale.example.com" in caplog.text
    assert "gw.example.com" in caplog.text
    # setdefault 语义不变 —— 预设值仍然生效,不被这次调用覆盖。
    assert os.environ["E2B_DOMAIN"] == "stale.example.com"


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
    特殊字符注入。做法是先 ``files.write`` 到临时文件再 ``python -I <file>``
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
    assert "python -I " in cmd
    assert run_user == SANDBOX_EXEC_USER


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
    assert envs["HOME"] == "/workspace", "HOME 不是 /workspace 则 PIP_USER 装到工作区外"
    assert envs["PIP_USER"] == "1", "只读 rootfs 上没有 PIP_USER=1 则 pip install 必失败"
    # egress 那组仍在 —— 合并没有把它挤掉。
    assert "HTTPS_PROXY" in envs


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
    """
    dockerfile = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "Dockerfile"
    assert dockerfile.is_file(), f"沙箱镜像 Dockerfile 不在预期位置:{dockerfile}"

    env, workdir = _parse_dockerfile_env_and_workdir(dockerfile.read_text(encoding="utf-8"))

    assert env == SANDBOX_IMAGE_ENV, (
        "沙箱镜像的 ENV 与 AgentSandboxClient 送进云沙箱的 SANDBOX_IMAGE_ENV 已经不一致"
        f"(Dockerfile={env} / 客户端={SANDBOX_IMAGE_ENV})——envd 派生的进程不继承镜像"
        " ENV,只吃 create(envs=) 里的这份,两边必须逐条对齐。"
    )
    assert workdir == WORKSPACE_ROOT, (
        f"镜像 WORKDIR={workdir} 与 WORKSPACE_ROOT={WORKSPACE_ROOT} 不一致 —— "
        "exec 传给 commands.run 的 cwd 就是后者。"
    )


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
