"""AgentSandboxClient —— E2B SDK 实现的 SandboxRuntime(波 1 Task 7/8/9)。

SDK 用假件替身:真实 SDK 调用在契约测试(Task 10)与端到端(Task 11)覆盖。

三处假件对派发方 task-7-brief.md 给的版本做了改动,均在 task-7-report.md
"发现的问题" 一节记录了理由,概述:

* ``FakeFiles.write`` 加 ``user`` 关键字参数并计入 ``written`` —— 探针报告
  实测 ``files.write`` 必须传 ``user="agent"`` 才不炸 ``AuthenticationException``
  (E2B 默认用户 ``user`` 在我们的沙箱镜像里不存在);既然这是任务里点名的
  必须修正项,断言就该验证它真的被传了,而不是只让假件"能吞下"这个参数。
* ``FakeSdk.connect`` 加 ``**kwargs`` —— 真实实现对 ``connect`` 也显式传
  ``domain=`` / ``api_key=``(与 ``create`` 同理,见 task-7-report.md),假件
  需要能吞下这些多余关键字参数。
* ``FakeInstanceStore`` 加 ``get_container_id`` —— brief Step 6 原文就说明
  "FakeInstanceStore 跟着加",不是遗漏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from orchestrator.tools.agent_sandbox import SANDBOX_EXEC_USER, AgentSandboxClient


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

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return self.sandbox

    async def connect(self, sandbox_id: str, **kwargs):
        self.connected.append(sandbox_id)
        if self.connect_fails:
            raise RuntimeError("sandbox gone")
        return self.sandbox


@dataclass
class FakeInstanceStore:
    """sandbox_instance 表的替身 —— CAS 语义由 claim_warm 表达。"""

    warm: dict[tuple[UUID, UUID], str] = field(default_factory=dict)
    rows: dict[UUID, dict] = field(default_factory=dict)

    async def claim_warm(self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID) -> str | None:
        """占坑成功返 None;已被别人占返赢家的 container_id。"""
        key = (tenant_id, user_id)
        if key in self.warm:
            return self.warm[key]
        self.warm[key] = ""
        self.rows[sandbox_id] = {"tenant_id": tenant_id, "user_id": user_id}
        return None

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        self.rows[sandbox_id]["container_id"] = container_id
        row = self.rows[sandbox_id]
        self.warm[(row["tenant_id"], row["user_id"])] = container_id

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        row = self.rows.pop(sandbox_id, None)
        if row is not None:
            self.warm.pop((row["tenant_id"], row["user_id"]), None)

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
    """第二路 acquire 不新建,connect 到赢家。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert len(sdk.created) == 1, "第二路不该再建沙箱"
    assert sdk.connected == ["sbx-1"], "第二路该 connect 赢家"


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
    from orchestrator.tools.sandbox import EgressContext

    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    egress = EgressContext(policy="proxy", agent_name="demo", agent_version="1")

    await client.acquire(tenant_id=uuid4(), thread_id="t1", user_id=uuid4(), egress=egress)

    envs = sdk.created[0]["envs"]
    assert envs["HTTPS_PROXY"] == envs["HTTP_PROXY"]
    assert envs["HTTPS_PROXY"].startswith("http://")
    assert "@credential-proxy.expert-work.svc.cluster.local:8081" in envs["HTTPS_PROXY"]
    assert envs["NO_PROXY"] == "credential-proxy.expert-work.svc.cluster.local,localhost,127.0.0.1"
