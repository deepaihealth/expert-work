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
  Important-3/4,详见类 docstring)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from orchestrator.tools.agent_sandbox import SANDBOX_EXEC_USER, AgentSandboxClient
from orchestrator.tools.sandbox import EgressContext, SandboxSupervisorError


@dataclass
class FakeCommands:
    calls: list[tuple[str, int | None]] = field(default_factory=list)
    result_stdout: str = ""
    result_stderr: str = ""
    result_exit: int = 0

    async def run(self, cmd: str, timeout: int | None = None):
        self.calls.append((cmd, timeout))
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

    async def write(self, path: str, data: bytes | str, *, user: str | None = None) -> None:
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

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        self.rows[sandbox_id]["container_id"] = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        row = self.rows.pop(sandbox_id, None)
        if row is not None:
            key = (row["tenant_id"], row["user_id"])
            if self.warm.get(key) == sandbox_id:
                del self.warm[key]

    async def drop_warm(self, *, tenant_id: UUID, user_id: UUID) -> None:
        self.warm.pop((tenant_id, user_id), None)

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        return self.rows.get(sandbox_id, {}).get("container_id")


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
    """唤醒失败(库存不足/欠费/保留期过被删)→ 丢弃旧行 → 重建。

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
    """egress=None(exec_python 等未绑定 EgressContext 时)不铸 token。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    await client.acquire(tenant_id=uuid4(), thread_id="t1", user_id=uuid4())

    assert sdk.created[0]["envs"] == {}


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
    避免的那类全局副作用,见 agent_sandbox.py 模块 docstring),又能验证
    "env 先设、patch 后调"这个顺序关系。``monkeypatch`` 的自动回滚保证
    ``_e2b_patched`` 在测试结束后仍是 ``False``,不影响其它测试。
    """
    import orchestrator.tools.agent_sandbox as mod

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
