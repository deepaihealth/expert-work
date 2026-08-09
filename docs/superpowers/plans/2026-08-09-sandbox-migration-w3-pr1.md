# 沙箱迁移波 3 PR-1(配额链)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给云沙箱路径装上工作区配额:租户级上限维度 + 领沙箱/上传两道闸 + 三层记账(上传增量 / release 防抖重算 / 全量扫兜底在 PR-2)。

**Architecture:** 上限来源单一化——新 `QuotaDimension.WORKSPACE_BYTES_PER_USER`(存储型,只作配置值,**不接**令牌桶)→ 回退共享默认 10GiB。记账权威在 `user_workspace.size_bytes`(云路径开始 `resolve()` 建行)。闸 A 在 `AgentSandboxClient.acquire`(注入可选 gate Protocol,orchestrator 不 import control-plane);闸 B 在 `uploads.py`。gate 的 control-plane 实现(`WorkspaceQuotaService`)同时提供 du 重算与 60s 防抖。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / pytest;React + antd + i18next(admin-ui)。

**Spec:** `docs/superpowers/specs/2026-08-09-sandbox-migration-w3-design.md`(§ 三、§ 六、§ 八)。

## Global Constraints

- 默认上限 = `10 * 1024**3` 字节(10 GiB),共享常量 `DEFAULT_WORKSPACE_BYTES_PER_USER`,唯一定义点 `expert_work/protocol/quota.py`。
- 维度字符串 = `"workspace_bytes_per_user"`。**禁止**在 `redis_quota.py` / `in_memory.py` 的 bucket ladder 里给它加分支——它是存储型上限,不是令牌桶维度(spec § 3.1 明示)。
- 闸 A 谓词 = `size_bytes >= limit`(已超才拦);闸 B 谓词 = `size_bytes + incoming > limit`(写完会超就拦)。**两个谓词刻意不同,不得"统一"**(spec § 3.3)。
- release 侧 refresh 防抖 = 60s per-`(tenant_id, user_id)`,进程内。
- `user_workspace.size_limit_bytes` 列**云闸不读**(supervisor 冻结路径专用)。
- HTTP 错误 detail 用固定文案不插值异常对象(`uploads.py` 既有约定);429 detail = `"workspace is full — delete files to free space"`。
- 新 except 分支必须排在 `SandboxSupervisorError` 宽 except **之前**(子类顺序,W2 教训)。
- SQL 与 in-memory store 同一方法的谓词/语义**逐字节同义**(仓库铁律)。
- 每条新断言变异自证:break→red→restore→green(重点:谓词 `>=`/`>` 互换、防抖去掉、ladder 加分支)。
- i18n 双语(zh-CN + en),新键先 grep 确认不撞既有键。
- supervisor / compose 路径零行为变化。
- 本地验证命令:persistence `cd packages/expert-work-persistence && uv run pytest`;orchestrator `cd services/orchestrator && DOCKER_HOST= uv run pytest`;control-plane `cd services/control-plane && uv run pytest`;SQL 集成测需 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`;前端 `cd apps/admin-ui && pnpm typecheck && pnpm test`。

---

## File Structure(全景)

| 文件 | 动作 | 职责 |
|---|---|---|
| `packages/expert-work-protocol/src/expert_work/protocol/quota.py` | 改 | 维度枚举 + 默认常量 |
| `packages/expert-work-persistence/src/expert_work/persistence/workspace/{base,sql,memory}.py` | 改 | `add_size` |
| `services/orchestrator/src/orchestrator/tools/sandbox.py` | 改 | `WorkspaceQuotaExceededError` + 工具层 `ToolBlockedError` 映射 |
| `services/orchestrator/src/orchestrator/tools/agent_sandbox.py` | 改 | gate Protocol + acquire 闸 + identity 映射 + release refresh 钩子 |
| `services/control-plane/src/control_plane/workspace_quota.py` | 新 | `WorkspaceQuotaService`(effective_limit / check / check_upload / note_written / refresh / refresh_soon)|
| `services/control-plane/src/control_plane/app.py` | 改 | 构建 service + post-assign 到 client + `app.state` |
| `services/control-plane/src/control_plane/api/uploads.py` | 改 | 闸 B + 429 + 写后入账 |
| `apps/admin-ui/src/api/tenant_quotas.ts` + `pages/SettingsTenantQuotas.tsx` | 改 | 维度选项 + 字节格式化 |
| `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx` + `i18n/locales/{zh-CN,en}.ts` | 改 | 上传 429 文案 |

---

### Task 1: 维度枚举 + 默认常量 + bucket 惰性回归测

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/quota.py:43-70`(`QuotaDimension` 枚举体内)
- Test: `services/control-plane/tests/test_quota_in_memory.py`(追加)

**Interfaces:**
- Produces: `QuotaDimension.WORKSPACE_BYTES_PER_USER`(值 `"workspace_bytes_per_user"`);`DEFAULT_WORKSPACE_BYTES_PER_USER: int = 10 * 1024**3`(模块级常量,`quota.py` 顶部常量区)。后续所有任务从 `expert_work.protocol.quota` import 这两个名字。

- [ ] **Step 1: 写失败测试**——bucket 引擎对新维度惰性(行存在但不产生任何桶/不影响 admission)。先读 `services/control-plane/tests/test_quota_in_memory.py` 找一个现成的「配了 quota 行 → check 被拒/放行」用例作骨架,新用例:给租户 upsert 一行 `dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER, limit_value=1`(极小值,若被误接进 bucket 必然拒绝),然后跑一次原本会放行的 admission check,断言**仍然放行**。

```python
async def test_workspace_bytes_per_user_row_is_inert_in_bucket_engine() -> None:
    """WORKSPACE_BYTES_PER_USER 是存储型上限(spec § 3.1),bucket ladder 不认识它。

    limit_value=1 的极小值是哨兵:如果有人往 ladder 里加了这个维度的分支,
    这一行会立刻把 admission 打成拒绝,测试变红。
    """
    # 用本文件既有 fixture 的 quota service + store;伪代码骨架,按现有用例风格改写:
    await store.upsert(
        tenant_id=tenant_id,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.WORKSPACE_BYTES_PER_USER,
            limit_value=1,
        ),
        updated_by="test",
    )
    result = await service.check(CheckRequest(tenant_id=tenant_id, ...))  # 与既有放行用例同参
    assert result.allowed
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_quota_in_memory.py::test_workspace_bytes_per_user_row_is_inert_in_bucket_engine -v`
Expected: FAIL,`AttributeError: WORKSPACE_BYTES_PER_USER`(枚举成员不存在)

- [ ] **Step 3: 加枚举成员 + 常量**。在 `QuotaDimension` 枚举尾部(`ARTIFACT_*` 成员之后)加:

```python
    # 沙箱迁移波 3 (spec § 3.1) — 每用户工作区字节黏性上限。存储型配置值:
    # 由 workspace 配额闸直接读 limit_value,**不接** bucket ladder
    # (redis_quota.py / in_memory.py 对它必须保持惰性;有回归测试钉住)。
    WORKSPACE_BYTES_PER_USER = "workspace_bytes_per_user"
```

在模块顶部常量区(`QuotaDimension` 类定义之前或之后的模块级)加:

```python
#: 沙箱迁移波 3 (spec § 3.1) — 租户没配 WORKSPACE_BYTES_PER_USER 时的
#: 平台默认每用户工作区上限。唯一定义点;闸与前端提示都以此为准。
DEFAULT_WORKSPACE_BYTES_PER_USER: int = 10 * 1024**3
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/control-plane && uv run pytest tests/test_quota_in_memory.py -v`
Expected: 新用例 PASS,既有用例全 PASS

- [ ] **Step 5: 变异自证**:在 `services/control-plane/src/control_plane/quota/in_memory.py` 的维度 ladder 里临时给 `WORKSPACE_BYTES_PER_USER` 加一个 `elif` 分支(照 `IMAGE_STORAGE_BYTES` 样子,capacity=limit_value, refill=0)→ 跑 Step 4 命令 → 新用例必须变 RED → 撤销 → 变 GREEN。若加了分支仍绿,说明哨兵没咬住,回 Step 1 修用例。

- [ ] **Step 6: Commit**

```bash
git add packages/expert-work-protocol/src/expert_work/protocol/quota.py services/control-plane/tests/test_quota_in_memory.py
git commit -m "feat(quota): WORKSPACE_BYTES_PER_USER 维度 + 10GiB 默认常量——存储型上限,bucket ladder 惰性有哨兵回归测钉住"
```

---

### Task 2: `UserWorkspaceStore.add_size`(SQL + memory 同义)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/base.py`(`update_size` 之后)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/sql.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/memory.py`
- Test: `packages/expert-work-persistence/tests/test_in_memory_user_workspace_store.py` + `packages/expert-work-persistence/tests/test_sql_user_workspace_store.py`(各追加)

**Interfaces:**
- Consumes: 既有 `resolve()` / `update_size()`。
- Produces: `async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None` —— 原子增量,落地值 `max(0, size_bytes + delta_bytes)`(下限 0 防负数;PR-1 只传正数,负数下限是防御不是功能)。

- [ ] **Step 1: 两个测试文件各写失败测试**(先读各自文件的 fixture 风格再落笔;两份断言语义一致):

```python
async def test_add_size_accumulates() -> None:
    ws = await store.resolve(tenant_id=tid, user_id=uid)
    await store.add_size(workspace_id=ws.id, delta_bytes=100)
    await store.add_size(workspace_id=ws.id, delta_bytes=50)
    got = await store.get(tenant_id=tid, user_id=uid)
    assert got is not None and got.size_bytes == 150

async def test_add_size_floors_at_zero() -> None:
    ws = await store.resolve(tenant_id=tid, user_id=uid)
    await store.add_size(workspace_id=ws.id, delta_bytes=-999)
    got = await store.get(tenant_id=tid, user_id=uid)
    assert got is not None and got.size_bytes == 0
```

SQL 侧额外加并发原子性用例(真容器,`@pytest.mark.integration` 按该文件既有标法):

```python
async def test_add_size_concurrent_increments_are_atomic() -> None:
    ws = await store.resolve(tenant_id=tid, user_id=uid)
    await asyncio.gather(*[
        store.add_size(workspace_id=ws.id, delta_bytes=10) for _ in range(20)
    ])
    got = await store.get(tenant_id=tid, user_id=uid)
    assert got is not None and got.size_bytes == 200
```

- [ ] **Step 2: 跑两边确认失败**

Run: `cd packages/expert-work-persistence && uv run pytest tests/test_in_memory_user_workspace_store.py -k add_size -v`(FAIL: no attribute)
Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && cd packages/expert-work-persistence && uv run pytest tests/test_sql_user_workspace_store.py -k add_size -v`(FAIL: no attribute)

- [ ] **Step 3: 实现**。`base.py`(`update_size` 抽象方法之后):

```python
    @abc.abstractmethod
    async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None:
        """Atomically add ``delta_bytes`` to ``size_bytes``, flooring at 0.

        沙箱迁移波 3 (spec § 3.2 记账第 1 层) — 上传成功后增量入账用。
        与 :meth:`update_size`(绝对值覆写,扫描/重算用)互补。负数下限 0
        是防御性语义,两实现必须一致(SQL ``GREATEST``,memory ``max``)。
        """
```

`sql.py`(照 `update_size` 的既有实现风格):

```python
    async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None:
        async with self._sf() as session:
            await session.execute(
                update(UserWorkspaceRow)
                .where(UserWorkspaceRow.id == workspace_id)
                .values(
                    size_bytes=func.greatest(
                        UserWorkspaceRow.size_bytes + delta_bytes, 0
                    )
                )
            )
            await session.commit()
```

(`func` 从 `sqlalchemy` import,该文件若未引入则补。)

`memory.py`(照 `update_size` 的既有 `model_copy` 风格):

```python
    async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None:
        for key, row in self._rows.items():
            if row.id == workspace_id:
                self._rows[key] = row.model_copy(
                    update={"size_bytes": max(row.size_bytes + delta_bytes, 0)}
                )
                return
```

(先确认 `memory.py` 的 `update_size` 按什么键定位行,逐字照抄它的定位方式——上面按 `update_size` 现状写,若不符以现状为准。)

- [ ] **Step 4: 跑 Step 2 两条命令**,全 PASS;再全量跑 `uv run pytest tests/test_in_memory_user_workspace_store.py tests/test_sql_user_workspace_store.py`。

- [ ] **Step 5: 变异自证**:SQL 实现去掉 `func.greatest`(裸 `+ delta_bytes`)→ floors 用例 RED → 还原 GREEN;memory 同理去 `max`。

- [ ] **Step 6: Commit**

```bash
git add packages/expert-work-persistence/src/expert_work/persistence/workspace/ packages/expert-work-persistence/tests/
git commit -m "feat(persistence): UserWorkspaceStore.add_size 原子增量——上传入账用,SQL/memory 下限 0 同义,SQL 并发原子性真容器测"
```

---

### Task 3: orchestrator 侧——异常 + gate Protocol + acquire 闸 + release refresh 钩子

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py`(异常 :134 旁;工具 helper ~:571 acquire 调用处)
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(dataclass 字段 ~:276-347;`acquire` ~:519;`release` ~:1150;`destroy`)
- Test: `services/orchestrator/tests/test_agent_sandbox.py`(追加)

**Interfaces:**
- Consumes: 无(gate 是新 Protocol,control-plane 在 Task 4 实现)。
- Produces:
  - `WorkspaceQuotaExceededError(SandboxSupervisorError)`(`orchestrator/tools/sandbox.py`,紧挨 `WorkspacePermissionError` 之后定义)。
  - `WorkspaceQuotaGate` Protocol(`agent_sandbox.py`):
    ```python
    class WorkspaceQuotaGate(Protocol):
        async def check(self, *, tenant_id: UUID, user_id: UUID) -> None:
            """超限抛 WorkspaceQuotaExceededError;未超返回 None。"""
        def refresh_soon(self, *, tenant_id: UUID, user_id: UUID) -> None:
            """fire-and-forget 触发该用户目录重算(实现侧自带 60s 防抖)。同步方法,内部自行调度。"""
    ```
  - `AgentSandboxClient` 新可选字段 `quota_gate: WorkspaceQuotaGate | None = None`(Task 4 由 app.py post-assign 注入)。

- [ ] **Step 1: 写失败测试**。先读 `test_agent_sandbox.py` 现有 `FakeInstanceStore` / client 构造 fixture,照风格写四个用例:

```python
class FakeQuotaGate:
    def __init__(self, *, over: bool) -> None:
        self.over = over
        self.check_calls: list[tuple[UUID, UUID]] = []
        self.refresh_calls: list[tuple[UUID, UUID]] = []

    async def check(self, *, tenant_id: UUID, user_id: UUID) -> None:
        self.check_calls.append((tenant_id, user_id))
        if self.over:
            raise WorkspaceQuotaExceededError("user workspace is over quota")

    def refresh_soon(self, *, tenant_id: UUID, user_id: UUID) -> None:
        self.refresh_calls.append((tenant_id, user_id))


async def test_acquire_over_quota_raises_and_creates_nothing():
    # gate over=True → acquire 抛 WorkspaceQuotaExceededError,
    # 且 store 里没有新增 IN_USE 行(闸在 claim_warm 之前,spec § 3.3)。

async def test_acquire_without_gate_unchanged():
    # quota_gate=None(默认)→ 现有 acquire happy path 用例行为完全不变
    #(挑一个既有 acquire 用例,复制后显式断言 gate 缺省仍走通)。

async def test_ephemeral_acquire_skips_gate():
    # user_id=None + gate over=True → 不抛(临时沙箱不查),gate.check_calls == []。

async def test_release_fires_refresh_for_user_session_including_warm():
    # 带 user_id acquire → release → gate.refresh_calls 含 (tenant, user);
    # 热会话分支(release 早退保温)也必须已触发 refresh —— 断言在早退路径上。

async def test_quota_error_maps_to_tool_blocked():
    # 工具层:sandbox.py 的 acquire 包装把 WorkspaceQuotaExceededError
    # 转成 ToolBlockedError(信息含 "workspace is full"),而不是裸异常穿透。
```

- [ ] **Step 2: 跑确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_sandbox.py -k "quota or refresh_for_user" -v`
Expected: FAIL,`ImportError: WorkspaceQuotaExceededError`

- [ ] **Step 3: 实现**。

`sandbox.py:134` 之后:

```python
class WorkspaceQuotaExceededError(SandboxSupervisorError):
    """用户工作区已到配额上限(沙箱迁移波 3 spec § 3.3 闸 A/B)。

    control-plane 上传路径映射 429;run 内工具路径由 sandbox.py 的
    acquire 包装转成 ToolBlockedError。与 sandbox_supervisor.domain 里的
    同名异常无关,互不 import(那侧是冻结的 supervisor 服务内部错误)。
    """
```

`agent_sandbox.py`:
1. `WorkspaceQuotaGate` Protocol(module 级,`SandboxInstanceStore` import 之后;`from typing import Protocol` 若未引入则补)。
2. dataclass 字段区(`workspace_root: str | None = None` 之后):

```python
    #: 沙箱迁移波 3 —— 可选工作区配额闸。None(默认)= 无闸,行为与波 2
    #: 完全一致(本地 compose / 未配 NAS 的部署)。control-plane 在 app.py
    #: 里 post-assign(照 resolved_workspace_store.http 的先例),不走
    #: 构造参数 —— tenant_quota store 的构建晚于本 client。
    quota_gate: WorkspaceQuotaGate | None = None
    #: acquire 时记下 sandbox_id → (tenant_id, user_id),release/destroy 时
    #: 反查身份触发配额重算。进程内(acquire/release 同进程成对);重启丢失
    #: 无害 —— janitor 全量扫兜底(spec § 3.2 第 3 层)。
    _session_identity: dict[UUID, tuple[UUID, UUID | None]] = field(
        default_factory=dict, init=False, repr=False
    )
```

3. `acquire`:`await self._prepare_workspace_mount(...)` 之后、`claim_warm` 之前插入:

```python
        if user_id is not None and self.quota_gate is not None:
            # 闸 A(spec § 3.3):已超才拦(>=)。放在 claim_warm 之前——
            # 拦下时不留任何 store 行。
            await self.quota_gate.check(tenant_id=tenant_id, user_id=user_id)
```

acquire 各成功出口前记身份(热复用与新建两条路都要):`self._session_identity[<返回的 sandbox_id>] = (tenant_id, user_id)`(热复用分支用赢家真实 id——注意该分支会改写返回 id,记录点放在**确定最终返回值之后**)。

4. `release` 开头(warm 早退**之前**):

```python
        identity = self._session_identity.get(sandbox_id)
        if identity is not None and identity[1] is not None and self.quota_gate is not None:
            # spec § 3.2 记账第 2 层:每次工具调用 release 都触发,防抖在
            # gate 实现侧(60s)。fire-and-forget:同步调度,不 await。
            self.quota_gate.refresh_soon(tenant_id=identity[0], user_id=identity[1])
```

非保温分支(destroy 后)与 `destroy` 里 `self._session_identity.pop(sandbox_id, None)`。

5. `sandbox.py` 工具 helper(~:571 `client.acquire(...)` 调用处)包异常:

```python
    try:
        sandbox_id = await client.acquire(...)  # 既有调用原样
    except WorkspaceQuotaExceededError as exc:
        # 波 3 闸 A:配额满是用户可自救的状态,不是基础设施错误 —— 转
        # ToolBlockedError,LLM 拿到可转述的行动指引(spec § 3.3)。
        msg = (
            "user workspace is full (storage quota exceeded) — "
            "ask the user to delete files from their workspace, then retry"
        )
        raise ToolBlockedError(msg) from exc
```

- [ ] **Step 4: 跑测试**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_sandbox.py -v`
Expected: 新旧全 PASS

- [ ] **Step 5: 变异自证**:①闸移到 `claim_warm` 之后 → `creates_nothing` 用例 RED;②去掉 warm 早退前的 refresh 调用 → `including_warm` 用例 RED;③`user_id is not None` 条件删掉 → `ephemeral` 用例 RED。各还原 GREEN。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/sandbox.py services/orchestrator/src/orchestrator/tools/agent_sandbox.py services/orchestrator/tests/test_agent_sandbox.py
git commit -m "feat(sandbox): 闸 A——acquire 配额检查(claim 前拦)+ release 触发防抖重算 + ToolBlockedError 映射;gate 未注入零行为变化"
```

---

### Task 4: control-plane `WorkspaceQuotaService` + 注入

**Files:**
- Create: `services/control-plane/src/control_plane/workspace_quota.py`
- Modify: `services/control-plane/src/control_plane/app.py`(`resolved_tenant_quotas`(~:780)之后、参照 :1346 post-assign 先例的位置)
- Test: `services/control-plane/tests/test_workspace_quota_service.py`(新)

**Interfaces:**
- Consumes: Task 1 的维度+常量;Task 2 的 `add_size`;Task 3 的 `WorkspaceQuotaGate` Protocol + `WorkspaceQuotaExceededError`;既有 `UserWorkspaceStore` / `TenantQuotaStore` / `workspace_user_root`(`orchestrator.tools.nas_workspace_store`)/ `Settings.workspace_nas_root`。
- Produces(Task 5/6 与 PR-2 依赖):

```python
class WorkspaceQuotaService:
    def __init__(self, *, user_workspaces: UserWorkspaceStore,
                 tenant_quotas: TenantQuotaStore, workspace_root: str,
                 debounce_s: float = 60.0) -> None: ...
    async def effective_limit(self, *, tenant_id: UUID) -> int: ...
    async def check(self, *, tenant_id: UUID, user_id: UUID) -> None: ...          # 闸 A:>= 拦
    async def check_upload(self, *, tenant_id: UUID, user_id: UUID,
                           incoming_bytes: int) -> None: ...                        # 闸 B:+incoming > 拦
    async def note_written(self, *, tenant_id: UUID, user_id: UUID,
                           delta_bytes: int) -> None: ...                           # resolve + add_size
    async def refresh(self, *, tenant_id: UUID, user_id: UUID) -> None: ...        # du → update_size(无防抖,janitor/测试直调)
    def refresh_soon(self, *, tenant_id: UUID, user_id: UUID) -> None: ...         # 防抖 + create_task(refresh),异常吞掉只 log
```

- [ ] **Step 1: 写失败测试**(`tmp_path` 当假 NAS 根;store 用 in-memory 两个;时间用可注入 clock 或 monkeypatch `time.monotonic`):

```python
async def test_effective_limit_default_when_unconfigured():   # == DEFAULT_WORKSPACE_BYTES_PER_USER
async def test_effective_limit_reads_tenant_row():           # upsert WORKSPACE_BYTES_PER_USER limit=123 → 123
async def test_effective_limit_ignores_expired_row():        # effective_until 在过去 → 回默认
async def test_check_blocks_at_limit_not_below():            # size==limit → raise;size==limit-1 → pass(>= 谓词)
async def test_check_upload_blocks_when_sum_exceeds():       # size+incoming>limit → raise;== limit → pass(> 谓词)
async def test_note_written_creates_row_and_accumulates():   # 行不存在 → resolve 建行 + add_size
async def test_refresh_walks_dir_lstat_no_symlink_follow():  # tmp 树:普通文件 + 指向大文件的软链 → size 只计 lstat 值
async def test_refresh_soon_debounces_60s():                 # 同 user 60s 内两次 → 只跑一次 refresh;不同 user 不互相防抖
```

- [ ] **Step 2: 跑确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_quota_service.py -v`
Expected: FAIL,`ModuleNotFoundError: control_plane.workspace_quota`

- [ ] **Step 3: 实现** `workspace_quota.py`。要点(完整写,此处列关键代码):

```python
async def effective_limit(self, *, tenant_id: UUID) -> int:
    now = datetime.now(UTC)
    rows = await self._tenant_quotas.list_by_tenant(tenant_id=tenant_id)
    for row in rows:
        if (
            row.dimension is QuotaDimension.WORKSPACE_BYTES_PER_USER
            and not row.scope
            and row.effective_from <= now
            and (row.effective_until is None or row.effective_until > now)
        ):
            return row.limit_value
    return DEFAULT_WORKSPACE_BYTES_PER_USER

async def check(self, *, tenant_id: UUID, user_id: UUID) -> None:
    ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    if ws.size_bytes >= await self.effective_limit(tenant_id=tenant_id):
        raise WorkspaceQuotaExceededError(
            "user workspace is over its storage quota"
        )

async def check_upload(self, *, tenant_id: UUID, user_id: UUID, incoming_bytes: int) -> None:
    ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    if ws.size_bytes + incoming_bytes > await self.effective_limit(tenant_id=tenant_id):
        raise WorkspaceQuotaExceededError(
            "user workspace is over its storage quota"
        )

async def refresh(self, *, tenant_id: UUID, user_id: UUID) -> None:
    root = workspace_user_root(self._workspace_root, tenant_id, user_id)
    def _du() -> int:
        total = 0
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += st.st_size
            except FileNotFoundError:
                return 0 if d == root else total
            except OSError:
                continue
        return total
    size = await asyncio.to_thread(_du)
    ws = await self._user_workspaces.resolve(tenant_id=tenant_id, user_id=user_id)
    await self._user_workspaces.update_size(workspace_id=ws.id, size_bytes=size)

def refresh_soon(self, *, tenant_id: UUID, user_id: UUID) -> None:
    key = (tenant_id, user_id)
    now = time.monotonic()
    last = self._last_refresh.get(key)
    if last is not None and now - last < self._debounce_s:
        return
    self._last_refresh[key] = now
    async def _run() -> None:
        try:
            await self.refresh(tenant_id=tenant_id, user_id=user_id)
        except Exception:  # spec § 3.2:失败吞掉,janitor 兜底
            logger.exception("workspace_quota.refresh_failed tenant=%s user=%s", tenant_id, user_id)
    asyncio.get_running_loop().create_task(_run())
```

`_du` 里 `FileNotFoundError` 对根目录返回 0(用户还没写过任何东西)。`WorkspaceQuotaExceededError` / `workspace_user_root` 分别从 `orchestrator.tools.sandbox` / `orchestrator.tools.nas_workspace_store` import(control-plane 依赖 orchestrator 是既有方向,`runtime.py` 大量先例)。

`app.py`(`resolved_tenant_quotas` 赋值之后):

```python
    # 沙箱迁移波 3 —— 工作区配额闸。tenant_quota store 建得比 sandbox
    # runtime 晚,所以走 post-assign(同 :1346 resolved_workspace_store.http
    # 的先例)。仅 agent_sandbox 后端 + 配了 NAS 根才有闸;其余部署
    # quota_gate 保持 None,零行为变化。
    resolved_workspace_quota: WorkspaceQuotaService | None = None
    if (
        isinstance(resolved_sandbox_runtime, AgentSandboxClient)
        and resolved_settings.workspace_nas_root
    ):
        resolved_workspace_quota = WorkspaceQuotaService(
            user_workspaces=resolved_user_workspace_store,
            tenant_quotas=resolved_tenant_quotas,
            workspace_root=resolved_settings.workspace_nas_root,
        )
        resolved_sandbox_runtime.quota_gate = resolved_workspace_quota
```

加 `app.state.workspace_quota_service = resolved_workspace_quota`(挨着 :2207 一带的其它 state 赋值)。`AgentSandboxClient` 若 app.py 未 import 则补(先 grep 现状)。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && uv run pytest tests/test_workspace_quota_service.py -v`
Expected: 全 PASS

- [ ] **Step 5: 变异自证**:①`check` 的 `>=` 改 `>` → `blocks_at_limit` RED;②`check_upload` 的 `>` 改 `>=` → `sum_exceeds` 里 `== limit → pass` 半边 RED;③`refresh_soon` 防抖判断删掉 → debounce 用例 RED;④`follow_symlinks=False` 改 True → symlink 用例 RED。各还原 GREEN。

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/workspace_quota.py services/control-plane/src/control_plane/app.py services/control-plane/tests/test_workspace_quota_service.py
git commit -m "feat(control-plane): WorkspaceQuotaService——租户维度取上限(时效窗口)/两谓词检查/du 重算 60s 防抖;app.py post-assign 注入 AgentSandboxClient"
```

---

### Task 5: 闸 B——上传检查 + 429 + 写后入账

**Files:**
- Modify: `services/control-plane/src/control_plane/api/uploads.py`(`_handle_document_upload`,写 NAS 前 + 写成功后)
- Test: `services/control-plane/tests/test_uploads_api.py`(追加)

**Interfaces:**
- Consumes: Task 4 的 `app.state.workspace_quota_service`(`WorkspaceQuotaService | None`)+ `WorkspaceQuotaExceededError`。

- [ ] **Step 1: 写失败测试**(先读 `test_uploads_api.py` 既有 document-upload 用例的 app/fixture 搭法,复用):

```python
async def test_document_upload_429_when_over_quota():
    # app.state.workspace_quota_service 换成会 raise 的假 service →
    # POST 上传 → 429,detail == "workspace is full — delete files to free space",
    # 且 workspace_store.write_file 未被调用(闸在写之前)。

async def test_document_upload_accounts_size_on_success():
    # 正常上传 → 假 service 的 note_written 收到 delta_bytes == len(raw)。

async def test_document_upload_no_service_behaves_as_today():
    # app.state.workspace_quota_service = None → 既有 201 路径原样(挑现有
    # happy 用例参数复跑 + 显式断言 201)。
```

- [ ] **Step 2: 跑确认失败**

Run: `cd services/control-plane && uv run pytest tests/test_uploads_api.py -k quota -v`
Expected: FAIL(429 分支不存在,拿到 201)

- [ ] **Step 3: 实现**。`_handle_document_upload` 里 `_reject_zip_bomb(raw, ext)` 之后、`workspace_path = ...` 之前:

```python
    quota_service = getattr(request.app.state, "workspace_quota_service", None)
    if quota_service is not None:
        try:
            # 闸 B(spec § 3.3):写完会超就拦(+incoming >)。与闸 A 的
            # >= 谓词刻意不同 —— 上传知道 incoming 大小,acquire 不知道。
            await quota_service.check_upload(
                tenant_id=tenant_id, user_id=caller_user_id, incoming_bytes=len(raw)
            )
        except WorkspaceQuotaExceededError as exc:
            # 固定文案不插值(本文件既有约定);429 与 supervisor 时代的
            # quota 语义一致。给用户留了自救路:删文件永远放行。
            raise HTTPException(
                status_code=429,
                detail="workspace is full — delete files to free space",
            ) from exc
```

写成功后(audit_emit 之前或之后均可,放 audit 之后紧邻 return 前):

```python
    if quota_service is not None:
        # 记账第 1 层(spec § 3.2):增量入账,便宜且即时。同名覆盖上传会
        # 重复计数 —— 已知偏差,janitor 全量扫兜正,不做减法补偿。
        await quota_service.note_written(
            tenant_id=tenant_id, user_id=caller_user_id, delta_bytes=len(raw)
        )
```

`WorkspaceQuotaExceededError` import 挨着该文件既有的 `WorkspacePermissionError` import。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && uv run pytest tests/test_uploads_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: 变异自证**:闸移到 `write_file` 之后 → `未被调用` 断言 RED;还原 GREEN。

- [ ] **Step 6: Commit**

```bash
git add services/control-plane/src/control_plane/api/uploads.py services/control-plane/tests/test_uploads_api.py
git commit -m "feat(uploads): 闸 B——上传前配额检查(+incoming > 拦)429 固定文案 + 写后增量入账;service 未接零行为变化"
```

---

### Task 6: 前端——维度选项 + 字节格式化 + 上传 429 文案

**Files:**
- Modify: `apps/admin-ui/src/api/tenant_quotas.ts:14-21`(union)
- Modify: `apps/admin-ui/src/pages/SettingsTenantQuotas.tsx:45-53`(`DIMENSION_OPTIONS`)+ limit_value 列 render(:157 一带)
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx:321-328`(catch 分支)
- Modify: `apps/admin-ui/src/i18n/locales/zh-CN.ts` + `en.ts`(`playground` 节 + `settings_ops` 节;**新键先 grep 双文件确认不撞**)
- Test: `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx`(追加)

**Interfaces:**
- Consumes: 后端维度字符串 `"workspace_bytes_per_user"`;上传 429。

- [ ] **Step 1: 写失败测试**。读 `PlaygroundTab.test.tsx` 既有上传相关用例(有 mock `uploadDocument` 的搭法则照抄),新用例:mock `uploadDocument` reject `new ApiError("workspace is full — delete files to free space", "HTTP_429", 429)` → 触发文档上传 → 断言渲染文案含「工作区已满」(zh locale 下)。

- [ ] **Step 2: 跑确认失败**

Run: `cd apps/admin-ui && pnpm vitest run src/pages/__tests__/PlaygroundTab.test.tsx`
Expected: 新用例 FAIL(显示的是 `HTTP_429: workspace is full …` 原始串)

- [ ] **Step 3: 实现**。

`tenant_quotas.ts` union 加 `| "workspace_bytes_per_user"`。

`SettingsTenantQuotas.tsx`:
1. `DIMENSION_OPTIONS` 数组尾部加 `"workspace_bytes_per_user"`。
2. 文件顶部加:

```tsx
// 字节维度:limit_value 列附带人类可读换算,建行表单下方提示同一换算。
const BYTES_DIMENSIONS: ReadonlySet<QuotaDimension> = new Set([
  "image_storage_bytes",
  "workspace_bytes_per_user",
]);

function formatBytesHint(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "";
  const gib = v / 1024 ** 3;
  return gib >= 1 ? `${gib % 1 === 0 ? gib : gib.toFixed(1)} GiB` : `${(v / 1024 ** 2).toFixed(0)} MiB`;
}
```

3. limit_value 列 render 改为:维度 ∈ `BYTES_DIMENSIONS` 时显示 `{v} ({formatBytesHint(v)})`,否则原样。

`PlaygroundTab.tsx` catch(:321-328):在既有 `err instanceof ApiError` 分支前插一档:

```tsx
          const message =
            err instanceof ApiError && err.status === 429 && kind === "document"
              ? t("playground.workspace_full")
              : err instanceof ApiError
                ? `${err.code}: ${err.message}`
                : err instanceof Error
                  ? err.message
                  : "upload failed";
```

i18n(两文件 `playground` 节;先 grep `workspace_full` 确认不存在):
- zh-CN:`workspace_full: "工作区已满,请清理文件后重试"`
- en:`workspace_full: "Workspace is full — delete some files and retry"`

- [ ] **Step 4: 跑测试 + typecheck**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/pages/__tests__/PlaygroundTab.test.tsx`
Expected: PASS(typecheck 同时钉住 union/OPTIONS 一致性)

- [ ] **Step 5: 全量前端验证**

Run: `cd apps/admin-ui && pnpm test`
Expected: 全 PASS(改了共享组件按仓库铁律跑全套 vitest)

- [ ] **Step 6: Commit**

```bash
git add apps/admin-ui/src/api/tenant_quotas.ts apps/admin-ui/src/pages/SettingsTenantQuotas.tsx apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx apps/admin-ui/src/i18n/locales/zh-CN.ts apps/admin-ui/src/i18n/locales/en.ts
git commit -m "feat(admin-ui): 租户配额页 workspace_bytes_per_user 维度 + 字节维度 GiB 换算提示 + 文档上传 429 工作区已满文案(双语)"
```

---

### Task 7: 终验 + CI 范围自查

**Files:** 无新改动(只跑命令修零星问题)。

- [ ] **Step 1: 后端全量**

```bash
cd services/control-plane && uv run pytest
cd services/orchestrator && DOCKER_HOST= uv run pytest
cd packages/expert-work-persistence && uv run pytest
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && cd packages/expert-work-persistence && uv run pytest -m integration
```

- [ ] **Step 2: lint/type(CI 同款范围)**

```bash
ruff check . && ruff format --check .
uv run mypy packages services/orchestrator/src
pre-commit run --all-files
```

- [ ] **Step 3: 前端全量**

```bash
cd apps/admin-ui && pnpm typecheck && pnpm test && pnpm build
```

- [ ] **Step 4: 发现问题就地修,全绿后 Commit(若有修正)**

```bash
git add -A && git commit -m "chore: PR-1 终验扫尾"
```

---

## Self-Review 记录

- **Spec 覆盖**:§ 3.1(Task 1+4 effective_limit+Task 6 UI)/§ 3.2 记账 1、2 层(Task 5、Task 3+4;第 3 层 janitor 属 PR-2)/§ 3.3 闸 A(Task 3)、闸 B(Task 5)、未注入零变化(Task 3/4/5 各有守卫用例)/§ 六 上传文案(Task 6;运维页已用/上限属 PR-2)/§ 八 变异自证点全部落在各 Task Step 5。
- **占位符**:无 TBD/TODO;Task 1 Step 1 与 Task 5 Step 1 的「照既有 fixture 骨架」是指令不是占位(测试逻辑与断言已给全)。
- **类型一致性**:`WorkspaceQuotaGate.check/refresh_soon` 签名在 Task 3 定义、Task 4 实现、Task 5 用 `check_upload/note_written`(service 独有方法,Protocol 只含 client 需要的两个——client 依赖面最小);`add_size(workspace_id, delta_bytes)` Task 2 定义、Task 4/5 经 service 间接消费。
