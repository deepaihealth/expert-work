# PR-A:sandbox_instance 表按后端限定行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sandbox_instance` 表被 docker supervisor 与 AgentSandboxClient 两个后端共用,但双方查询都没按后端限定行 —— 本 PR 让两侧的集合查询与 0141 部分唯一索引都按后端隔离,并顺带收掉波 1 遗留的两条 store 语义分歧(#5a/#5b)。这是沙箱迁移波 2 的硬前提。

**Architecture:** 后端标记已经存在 —— AgentSandboxClient 写行时把 `image_ref`/`node` 等 docker-only NOT NULL 列填成惰性标记 `"agent-sandbox"`(现私有常量 `_UNUSED_TEXT`),docker supervisor 写的是真实镜像引用。把它升为公开常量 `AGENT_SANDBOX_IMAGE_REF`,三处消费:①迁移 0142 重建 0141 索引、WHERE 加 `image_ref = 'agent-sandbox'`(索引只 police 本后端,docker 行彻底脱离管辖);②agent 侧 3 个集合查询加 `image_ref ==` 谓词;③supervisor 侧 2 个集合查询加 `image_ref !=` 谓词。按 `sandbox_id`(uuid4 PK)定位的方法**刻意不加** image_ref 谓词 —— id 恒来自本后端自己的 acquire 返回值,跨后端撞 id 不存在,加谓词是无收益的面积。

**Tech Stack:** SQLAlchemy 2 async + alembic + testcontainers(真 PG 集成测)+ pytest。

## 背景:不修会发生什么(reviewer 校准用)

- `AgentSandboxClient.reap`(现在有周期 worker 每 240s 跑)的 `list_active` 会把 docker 后端的 IN_USE 行全捞出来 → 拿 docker container id 去 E2B `connect` 必失败 → `mark_destroyed` 把 **docker 后端名下每一行**标销毁,supervisor 毫不知情,容器全泄漏。`force=True` 同理更狠。
- `claim_warm` 输家分支的 SELECT 会把 docker warm 行的 container id 当 E2B sandbox id 交出去;重连失败还会把 docker 的行标销毁。
- 0141 索引不分后端:一个 `(tenant, user)` 全局只许一行 IN_USE。环境切换 `sandbox_backend` 后,旧后端残留的 warm 行会把新后端同用户的 claim 永久卡死;supervisor 侧 UPDATE 行到 IN_USE 也可能撞索引炸 acquire。
- supervisor 的 `list_idle_sessions` 捞 IN_USE 全表 → docker reaper 会试图 `docker stop` 一个 E2B sandbox id、失败后走自己的销毁记账,把 agent 侧的热会话行毁掉。
- supervisor 的 `count_active_for_tenant` 把 E2B 行算进 docker 配额(算多 = 保守方向,伤害小,但一并修)。

## Global Constraints

- 后端标记单一来源:公开常量 `AGENT_SANDBOX_IMAGE_REF = "agent-sandbox"`,定义在 `expert_work.persistence.sandbox_instance_store`。迁移文件里必须用字面量(alembic 迁移不 import 应用代码,仓内惯例),但注释指向常量名。supervisor 侧 **import 常量**,不镜像字面量(supervisor 已依赖 `expert_work.persistence.models`,方向合法)。
- SQL store 与 in-memory store 的谓词必须**同义**(本仓反复踩过的教训)。in-memory store 天然只被 agent 后端写入(单进程 dev,没有 docker 行共存),image_ref 维度对它**空洞地成立** —— 用 docstring 写明,不加假字段。
- 任何 SQL 谓词改动,必须 file-scope 跑真 PG 集成测复验(`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`,in-memory 不校验部分索引/谓词)。
- 按 id 定位的方法(`get_container_id`/`touch_and_get_container_id`/`set_container_id`/`mark_destroyed`/`is_warm_session`、supervisor 的 `get`/`update`/`claim_ready`)**不加** image_ref 谓词,理由见 Architecture;#5a 给 `get_container_id` 加的是 `state`/`destroyed_at` 谓词,是另一个维度(活行对齐),不要混淆。
- supervisor 的 `delete_all_for_user`(purge_user)**刻意不过滤** agent 行:purge 语义就是删该用户一切;行删掉后 microVM 无人续期,≤20 分钟平台超时自愈。加注释,不改行为。
- 部署顺序:迁移先于代码(仓内 migrate one-shot 惯例)。旧索引 + 新代码的中间态是"claim 撞 docker 行时大声 raise 而非交出错误 id" —— 比现状安全,可接受。
- lint/type/test 口径:`ruff check` 全库(含 tests)、mypy 按 CI scope、pytest 跑 persistence 与 supervisor 两个包受影响文件 + orchestrator 的 `test_agent_sandbox.py`。
- Commit 格式:conventional commits,无 attribution footer。

---

### Task 1: 迁移 0142 —— 0141 索引按后端限定 + 公开常量

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0142_sandbox_warm_backend_scope.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/sandbox_instance_store.py`(仅常量段)
- Test: `packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py`(追加)

**Interfaces:**
- Consumes: 无(首任务)。
- Produces: 公开常量 `AGENT_SANDBOX_IMAGE_REF: str = "agent-sandbox"`(模块 `expert_work.persistence.sandbox_instance_store`);迁移后索引 `ix_sandbox_instance_warm_unique` 的 WHERE 含 `image_ref = 'agent-sandbox'`。Task 2/3 都消费这个常量。

- [ ] **Step 1: 升常量为公开**

在 `sandbox_instance_store.py` 中,把现有:

```python
#: Inert marker for the docker-supervisor-only NOT NULL text columns this
#: backend has no real value for (see module docstring).
_UNUSED_TEXT = "agent-sandbox"
```

改为:

```python
#: 本后端(AgentSandboxClient)写进 ``image_ref`` 的标记值 —— 同时是
#: ``sandbox_instance`` 表**按后端限定行**的判据:docker supervisor 写的是
#: 真实镜像引用,永远不会等于这个值。三处消费:迁移 0142 的部分唯一索引
#: WHERE(字面量,alembic 不 import 应用代码)、本模块的集合查询谓词、
#: docker supervisor 侧 ``DbSandboxStore`` 的排除谓词(import 本常量)。
AGENT_SANDBOX_IMAGE_REF = "agent-sandbox"

#: Inert marker for the docker-supervisor-only NOT NULL text columns this
#: backend has no real value for (see module docstring). Alias of
#: :data:`AGENT_SANDBOX_IMAGE_REF` — the marker doubles as the backend
#: discriminator, one value on purpose.
_UNUSED_TEXT = AGENT_SANDBOX_IMAGE_REF
```

- [ ] **Step 2: 写迁移 0142**

新建 `0142_sandbox_warm_backend_scope.py`,照 0141 的文件结构(docstring/`revision`/`down_revision`/`__all__`):

```python
"""0142 — scope the warm-session unique index to the agent-sandbox backend.

0141 的部分唯一索引不分后端:一个 ``(tenant, user)`` 全局只许一行
IN_USE,docker supervisor 的行与 AgentSandboxClient 的行互相顶死 ——
环境切换 ``sandbox_backend`` 后旧后端残留 warm 行会把新后端同用户的
claim 永久卡死,supervisor 侧 UPDATE 到 IN_USE 也会撞索引。WHERE 加
``image_ref = 'agent-sandbox'``(AgentSandboxClient 写行时的惰性标记,
见 ``expert_work.persistence.sandbox_instance_store.AGENT_SANDBOX_IMAGE_REF``
—— 字面量而非 import,alembic 迁移不依赖应用代码,仓内惯例),索引从此
只 police agent-sandbox 后端自己的行。

非 CONCURRENTLY 重建(同 0141 的理由):低写表,runbook 记一笔。

Revision ID: 0142_sandbox_warm_backend_scope
Revises: 0141_sandbox_warm_unique
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0142_sandbox_warm_backend_scope"
down_revision: str | Sequence[str] | None = "0141_sandbox_warm_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sandbox_instance_warm_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
          ON sandbox_instance (tenant_id, user_id)
          WHERE state = 'IN_USE' AND destroyed_at IS NULL
            AND user_id IS NOT NULL AND image_ref = 'agent-sandbox'
        """
    )


def downgrade() -> None:
    # 还原 0141 原形。注意:若并存期间已产生「同 (tenant, user) 两后端各一行
    # IN_USE」的合法数据,这条 CREATE 会因唯一冲突失败 —— 预期行为,降级前
    # 需人工清掉其中一行。
    op.execute("DROP INDEX IF EXISTS ix_sandbox_instance_warm_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_sandbox_instance_warm_unique
          ON sandbox_instance (tenant_id, user_id)
          WHERE state = 'IN_USE' AND destroyed_at IS NULL AND user_id IS NOT NULL
        """
    )
```

- [ ] **Step 3: 写失败测试(先跑红——在迁移生效前只跑得红一半,以真 PG 为准)**

在 `test_sql_sandbox_instance_store.py` 追加。文件已有 `store` fixture(testcontainers PG + alembic upgrade head)、`_sync_dsn`/`_async_dsn` helper。追加一个直插 docker 形状行的 helper(绕过两个 store —— 谁的 store 都不该会写对方的行,直插才是对的建模):

```python
async def _insert_docker_row(
    store: SqlSandboxInstanceStore,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    state: str = "IN_USE",
    container_id: str | None = "docker-cafe",
    acquired_at: datetime | None = None,
) -> UUID:
    """直插一行 docker-supervisor 形状的行(真实 image_ref/node,非标记值)。"""
    row_id = uuid4()
    async with store._sf() as session:  # noqa: SLF001 — 测试直插,见上方注释
        session.add(
            SandboxInstanceRow(
                id=row_id,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=None,
                image_ref="registry.example.com/expert-work/sandbox:py312",
                node="dev-host-1",
                container_id=container_id,
                state=state,
                thread_id="thread-1",
                cpu_quota=1,
                memory_mb=1024,
                pids_limit=128,
                timeout_s=300,
                acquired_at=acquired_at or datetime.now(tz=UTC),
            )
        )
        await session.commit()
    return row_id


@pytest.mark.asyncio
async def test_docker_warm_row_does_not_block_agent_claim(
    store: SqlSandboxInstanceStore,
) -> None:
    """0142:docker 后端同 (tenant, user) 的 IN_USE 行不再顶死 agent claim。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _insert_docker_row(store, tenant_id=tenant_id, user_id=user_id)

    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())

    assert result is None  # agent 侧照常赢得自己的槽位
    async with store._sf() as session:  # noqa: SLF001
        count = len(
            (
                await session.execute(
                    select(SandboxInstanceRow.id).where(
                        SandboxInstanceRow.tenant_id == tenant_id,
                        SandboxInstanceRow.state == "IN_USE",
                    )
                )
            ).all()
        )
    assert count == 2  # 两后端各一行,合法共存


@pytest.mark.asyncio
async def test_agent_rows_still_unique_per_tenant_user(
    store: SqlSandboxInstanceStore,
) -> None:
    """0142 没放松 agent 行自己的唯一性:直插第二行 agent IN_USE 必炸。"""
    tenant_id, user_id = uuid4(), uuid4()
    await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())

    with pytest.raises(IntegrityError):
        async with store._sf() as session:  # noqa: SLF001
            session.add(
                SandboxInstanceRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=None,
                    image_ref=AGENT_SANDBOX_IMAGE_REF,
                    node=AGENT_SANDBOX_IMAGE_REF,
                    container_id=None,
                    state="IN_USE",
                    thread_id=AGENT_SANDBOX_IMAGE_REF,
                    cpu_quota=0,
                    memory_mb=0,
                    pids_limit=0,
                    timeout_s=0,
                    acquired_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()
```

需要的新 import:`from sqlalchemy.exc import IntegrityError`、`AGENT_SANDBOX_IMAGE_REF`(加进现有的 `from expert_work.persistence.sandbox_instance_store import ...`)。

- [ ] **Step 4: file-scope 真 PG 跑测试,确认全绿**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py -v
```

预期:新增 2 条过,存量全过(存量里凡依赖 0141 行为的用例不该有 —— 若有测试断言"跨后端也唯一",那是在钉本 PR 要改掉的行为,改断言并在 commit message 里说明)。

- [ ] **Step 5: 变异自验(教训条款:SQL 变异复验必须 file-scope)**

临时把迁移 0142 `upgrade()` 里的 `AND image_ref = 'agent-sandbox'` 删掉,重跑 Step 4 —— `test_docker_warm_row_does_not_block_agent_claim` 必须变红(claim_warm 撞索引后走 SELECT,行为退化)。确认后还原。报告里记录变异结果。

- [ ] **Step 6: Commit**

```bash
git add packages/expert-work-persistence/migrations/versions/0142_sandbox_warm_backend_scope.py \
        packages/expert-work-persistence/src/expert_work/persistence/sandbox_instance_store.py \
        packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py
git commit -m "fix(sandbox): 0142 迁移——0141 warm 唯一索引按后端限定(image_ref='agent-sandbox')"
```

---

### Task 2: agent 侧 store 集合查询加后端谓词 + #5a 活行对齐 + #5b create_ephemeral 对齐

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/sandbox_instance_store.py`
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(仅 `destroy` docstring)
- Test: `packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py`、`packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `AGENT_SANDBOX_IMAGE_REF` 与迁移 0142、`_insert_docker_row` 测试 helper。
- Produces: `SqlSandboxInstanceStore` 三个集合查询(`claim_warm` 的输家 SELECT、`list_active`、`list_stuck_creating`)带 `image_ref == AGENT_SANDBOX_IMAGE_REF` 谓词;`get_container_id` 两 store 语义统一为「只读活行」。Protocol 签名零变化,Task 3 与终审依赖这一点。

- [ ] **Step 1: 写失败测试(SQL 侧)**

`test_sql_sandbox_instance_store.py` 追加(复用 Task 1 的 `_insert_docker_row`):

```python
@pytest.mark.asyncio
async def test_collection_queries_exclude_docker_rows(
    store: SqlSandboxInstanceStore,
) -> None:
    """list_active / list_stuck_creating / claim_warm 输家 SELECT 只看本后端的行。

    不修的后果(本 PR 的背景一节):周期 reap 会把 docker 后端名下每一行
    标销毁;claim_warm 会把 docker container id 当 E2B sandbox id 交出去。
    """
    tenant_id, user_id = uuid4(), uuid4()
    # 一行活跃 docker 行 + 一行 stuck-creating 形状(container 未回填、超龄)的 docker 行
    await _insert_docker_row(store, tenant_id=tenant_id, user_id=user_id)
    stale = datetime.now(tz=UTC) - timedelta(seconds=_STUCK_CREATE_TTL_S + 60)
    await _insert_docker_row(
        store, tenant_id=tenant_id, user_id=None, container_id=None, acquired_at=stale
    )

    assert await store.list_active(only_idle=False) == []
    assert await store.list_stuck_creating() == []

    # claim_warm 输家 SELECT:agent 行才是槽位主人 —— docker 行在场时第二个
    # agent claim 拿到的必须是 agent 赢家的 (id, container),不是 docker 的。
    agent_winner = uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=agent_winner)
    ) is None
    await store.set_container_id(sandbox_id=agent_winner, container_id="e2b-123")
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())
    assert result == (agent_winner, "e2b-123")


@pytest.mark.asyncio
async def test_get_container_id_returns_none_for_destroyed_row(
    store: SqlSandboxInstanceStore,
) -> None:
    """#5a:SQL 向 in-memory 对齐 —— get_container_id 只读活行。

    终审时曾以「重杀兜底」为由保留旧行为;复盘推翻:兜底要求对同一
    sandbox_id 二次调 destroy,代码里无此路径,而行终结后无人续期、
    平台 20 分钟超时是确定性兜底(_SANDBOX_TIMEOUT_S 的既有职责)。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    await store.set_container_id(sandbox_id=sandbox_id, container_id="e2b-dead")
    await store.mark_destroyed(sandbox_id=sandbox_id, reason="test")

    assert await store.get_container_id(sandbox_id=sandbox_id) is None
```

- [ ] **Step 2: 跑红**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py -v
```

预期:两条新用例 FAIL(`list_active` 捞到 docker 行 / `get_container_id` 返回 `"e2b-dead"`)。

- [ ] **Step 3: 改 SQL store**

`sandbox_instance_store.py` 四处:

① `claim_warm` 输家 SELECT 的 `.where(...)` 追加一行谓词,并在既有 "Select id + container_id together" 注释块后补一句:

```python
                        ).where(
                            SandboxInstanceRow.tenant_id == tenant_id,
                            SandboxInstanceRow.user_id == user_id,
                            SandboxInstanceRow.state == _STATE_IN_USE,
                            SandboxInstanceRow.destroyed_at.is_(None),
                            SandboxInstanceRow.image_ref == AGENT_SANDBOX_IMAGE_REF,
                        )
```

(注释补充:`image_ref` 谓词与迁移 0142 的索引 WHERE 同义 —— 表是两后端共用的,docker supervisor 的 warm 行不是本 CAS 的参与者;0142 之前的 DB 上这条过滤让"撞上 docker 行"表现为大声的 could-not-claim raise,而不是把 docker container id 当 E2B sandbox id 交出去。)

② `list_active` 的 `.where(...)` 追加 `SandboxInstanceRow.image_ref == AGENT_SANDBOX_IMAGE_REF`,docstring 补一句:reap 以此保证只清本后端的行 —— 否则周期 reap 会把 docker 后端名下每一行标销毁。

③ `list_stuck_creating` 的 `.where(...)` 追加同一谓词,docstring 补:belt-and-braces —— docker 的 mid-create 行 state='CREATING' 本就不命中,谓词为三个集合查询口径一致。

④ `get_container_id` (#5a):

```python
    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """只读活行 —— 与 in-memory store(``mark_destroyed`` 即 pop)同义。

        原先无谓词:已销毁行照样返回 container_id,同族第四条 SQL↔memory
        分歧(前三条:``set_container_id`` / ``touch_and_get_container_id`` /
        ``mark_destroyed``,每一条都是 SQL 向 memory 对齐收场)。曾以
        「destroy 二次调用可重杀 kill 失败的沙箱」为由保留 —— 复盘推翻:
        代码里没有任何路径会对同一 sandbox_id 二次 destroy,而行终结后
        没人再 connect 续期,平台 ``_SANDBOX_TIMEOUT_S``(20 分钟)超时是
        确定性兜底。刻意不加 ``image_ref`` 谓词:按 id 定位,id 恒来自
        本后端 acquire 的返回值(Global Constraints)。
        """
        async with self._sf() as session:
            container_id = (
                await session.execute(
                    select(SandboxInstanceRow.container_id).where(
                        SandboxInstanceRow.id == sandbox_id,
                        SandboxInstanceRow.state == _STATE_IN_USE,
                        SandboxInstanceRow.destroyed_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        return str(container_id) if container_id is not None else None
```

- [ ] **Step 4: in-memory 侧(#5b + docstring)**

① `create_ephemeral` 对齐 SQL 的 `ON CONFLICT DO NOTHING`:

```python
    async def create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """Mirrors :meth:`SqlSandboxInstanceStore.create_ephemeral` — a plain
        insert, no ``_warm`` bookkeeping (ephemeral rows never participate in
        the warm-session CAS). First writer wins, same as the SQL side's
        ``ON CONFLICT DO NOTHING``: 无条件 ``self._rows[...] = ...`` 会在
        id 碰撞时把一行活着的 warm 行整个换成 ``user_id=None`` 的临时行,
        ``_warm`` 指针悬空(指向一行不再是 warm 的行)。"""
        if sandbox_id in self._rows:
            return
        self._rows[sandbox_id] = _MemRow(tenant_id=tenant_id, user_id=None)
```

② `InMemorySandboxInstanceStore` 类 docstring 追加一段:image_ref 维度(SQL 侧三个集合查询的 `AGENT_SANDBOX_IMAGE_REF` 谓词)对本实现**空洞地成立** —— 本 store 只被 agent 后端进程写入,不存在 docker 行共存,`_rows` 里的每一行定义上都是本后端的行;不为此加假字段。

- [ ] **Step 5: in-memory 失败测试 + 全绿**

`test_in_memory_sandbox_instance_store.py` 追加:

```python
@pytest.mark.asyncio
async def test_create_ephemeral_does_not_overwrite_existing_row() -> None:
    """#5b:id 碰撞时先写者赢(对齐 SQL 的 ON CONFLICT DO NOTHING)。"""
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)

    await store.create_ephemeral(tenant_id=uuid4(), sandbox_id=sandbox_id)

    # warm 行原样健在:user_id 仍在、_warm 指针没悬空
    assert await store.is_warm_session(sandbox_id=sandbox_id) is True
```

先跑红(现实现会覆盖 → `is_warm_session` 变 False),改完跑绿:

```bash
uv run pytest packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py \
              packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py -v
```

(SQL 文件仍带 `DOCKER_HOST`。)

- [ ] **Step 6: 更新 orchestrator destroy docstring + 核对 FakeInstanceStore**

`agent_sandbox.py` 的 `destroy` docstring(`"""强制拆除 —— 真 kill 沙箱并让出热会话坑。"""`)扩为:

```python
        """强制拆除 —— 真 kill 沙箱并让出热会话坑。

        「kill 失败但 ``mark_destroyed`` 已成功」留下的活沙箱,唯一兜底是
        平台 ``_SANDBOX_TIMEOUT_S``(20 分钟)超时:行终结后
        ``get_container_id``/``touch_and_get_container_id`` 都只读活行
        (两 store 同义),没有任何路径会再 connect 它续期。不做「二次
        destroy 重杀」—— 代码里没有会二次 destroy 同一 id 的调用方,为
        不可达路径保留 SQL↔memory 谓词分歧不值得(PR-A #5a)。
        """
```

同时核对 `services/orchestrator/tests/test_agent_sandbox.py` 的 `FakeInstanceStore.get_container_id`:若它对已销毁行仍返回 container_id(与新契约不符),对齐成「行不在/已销毁 → None」并确认相关 destroy 测试仍绿(destroy 的 broad except 本就把 attach 失败当"已不在"处理,行为端不变)。

```bash
uv run pytest services/orchestrator/tests/test_agent_sandbox.py -v
```

- [ ] **Step 7: Commit**

```bash
git add packages/expert-work-persistence/src/expert_work/persistence/sandbox_instance_store.py \
        packages/expert-work-persistence/tests/ \
        services/orchestrator/src/orchestrator/tools/agent_sandbox.py \
        services/orchestrator/tests/test_agent_sandbox.py
git commit -m "fix(sandbox): agent 侧 store 集合查询按后端限定+get_container_id/create_ephemeral 两 store 语义对齐"
```

---

### Task 3: supervisor 侧 DbSandboxStore 排除 agent 后端行

**Files:**
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/store.py`
- Create: `services/sandbox-supervisor/tests/test_db_store_backend_scope.py`

**Interfaces:**
- Consumes: `AGENT_SANDBOX_IMAGE_REF`(Task 1);root `conftest.py` 的 `postgres_container` fixture(testcontainers,repo 根,所有测试目录可见)。
- Produces: `DbSandboxStore.count_active_for_tenant` 与 `list_idle_sessions` 带 `image_ref != AGENT_SANDBOX_IMAGE_REF` 谓词。Protocol 签名零变化。

- [ ] **Step 1: 写失败测试**

新建 `services/sandbox-supervisor/tests/test_db_store_backend_scope.py`。harness 照 `packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py` 的形态(alembic upgrade 到 head + async factory;`ALEMBIC_INI` 路径换算到 repo 根下 `packages/expert-work-persistence/alembic.ini`),`pytestmark = pytest.mark.integration`:

```python
"""DbSandboxStore 的按后端限定谓词 —— 真 PG 集成测(PR-A Task 3)。

docker supervisor 与 AgentSandboxClient 共用 ``sandbox_instance`` 表;
supervisor 的两个集合查询原先不分后端:``list_idle_sessions`` 会把 E2B
热会话行交给 docker reaper(``docker stop`` 一个 E2B id、失败后销毁记账
把 agent 侧的行毁掉),``count_active_for_tenant`` 把 E2B 行算进 docker
配额。谓词 ``image_ref != AGENT_SANDBOX_IMAGE_REF``。
"""
```

用例(一个即可,两断言分开写成两条用例更清晰):插一条 docker 真行(走 `DbSandboxStore.insert`,`SandboxRecord` 用真实 image_ref/node,state=IN_USE,`last_used_at` 拨旧到 idle 线外)+ 直插一条 agent 形状行(`image_ref=AGENT_SANDBOX_IMAGE_REF`,state=IN_USE,同 tenant,`last_used_at` 同样拨旧):

```python
@pytest.mark.asyncio
async def test_count_active_excludes_agent_sandbox_rows(...) -> None:
    assert await store.count_active_for_tenant(tenant_id) == 1  # 只数 docker 行

@pytest.mark.asyncio
async def test_list_idle_sessions_excludes_agent_sandbox_rows(...) -> None:
    idle = await store.list_idle_sessions(now=..., idle_ttl_s=60)
    assert [r.id for r in idle] == [docker_row_id]  # E2B 行不进 docker reaper
```

注意:`SandboxRecord` 构造签名看 `sandbox_supervisor/domain.py`;agent 形状行直插用 `SandboxInstanceRow`(同 Task 1 helper 的写法,填标记值/零值)。

- [ ] **Step 2: 跑红**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/sandbox-supervisor/tests/test_db_store_backend_scope.py -v
```

预期:两条都 FAIL(count == 2 / idle 含 agent 行)。

- [ ] **Step 3: 改 supervisor store**

`store.py` 两处,import 加 `from expert_work.persistence.sandbox_instance_store import AGENT_SANDBOX_IMAGE_REF`(supervisor 已依赖 persistence 包,方向合法):

```python
    async def count_active_for_tenant(self, tenant_id: UUID) -> int:
        # 表与 AgentSandboxClient 共用(PR-A):E2B 行不占 docker 配额 ——
        # 云后端的配额语义由云侧自己管(波 2)。
        async with self._sf() as session:
            result = await session.execute(
                select(SandboxInstanceRow.id).where(
                    SandboxInstanceRow.tenant_id == tenant_id,
                    SandboxInstanceRow.state.in_([s.value for s in _ACTIVE_STATES]),
                    SandboxInstanceRow.image_ref != AGENT_SANDBOX_IMAGE_REF,
                )
            )
            return len(result.fetchall())
```

`list_idle_sessions` 的 `.where(...)` 同样追加 `SandboxInstanceRow.image_ref != AGENT_SANDBOX_IMAGE_REF`,注释:E2B 行交给云侧自己的周期 reap(`control_plane.sandbox_reap_worker`),docker reaper 对它们既 stop 不动也不该记账。

`delete_all_for_user` **不改**,在其 docstring 末尾追加一句:刻意不排除 agent 后端的行 —— purge 语义就是删该用户一切;E2B 行删掉后 microVM 无人续期,≤20 分钟平台超时回收(PR-A 拍板)。

- [ ] **Step 4: 跑绿 + supervisor 存量单测**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/sandbox-supervisor/tests/test_db_store_backend_scope.py -v
uv run pytest services/sandbox-supervisor/tests/ -m "not integration" -q
```

- [ ] **Step 5: Commit**

```bash
git add services/sandbox-supervisor/src/sandbox_supervisor/store.py \
        services/sandbox-supervisor/tests/test_db_store_backend_scope.py
git commit -m "fix(sandbox): supervisor 侧 count_active/list_idle 排除 agent-sandbox 后端行"
```

---

## 收尾(终审前自查口径)

- `ruff check .`(全库含 tests)、`ruff format --check .`
- mypy 按 CI scope(照 `.github/workflows/ci.yml` 的 mypy step 原样)
- 三个测试文件 file-scope 真 PG 全绿(`DOCKER_HOST` 已 export)
- `uv run pytest packages/expert-work-persistence/tests/ -q`(含非本 PR 文件,防顺序污染回归)
