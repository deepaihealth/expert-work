# B-60 写入侧隔离:每次 exec 一个私有 `/workspace` —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 沙箱里任意代码写 `/workspace/<x>` 落的就是 `agents/<agent_key>/<x>`,并且看不见同一用户其他 agent 的目录 —— 靠文件系统,不靠约定。

**Architecture:** NAS 用户根改挂 `/mnt/workspace`;每次 exec 在自己的 user+mount namespace 里(`unshare -Urm`)把 `agents/<key>` bind 成 `/workspace`、`shared/` 只读 bind 进来、tmpfs 盖掉 `/mnt/workspace`。热沙箱按 `(tenant, user)` 复用不变;沙箱内**所有**代码(文件工具片段、用户代码)看同一个视图。两个后端共用一段 sh 字面量,契约测试钉两边。热会话加 `layout` 列自动换代。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 + Alembic / util-linux `unshare`+`mount`(dash)/ docker(runc、runsc)/ e2b SDK(ACS)/ pytest(`uv run --no-sync pytest`)

**Spec:** `docs/superpowers/specs/2026-09-14-workspace-exec-mount-namespace-design.md`(**先读 §零 修订表**,拍板稿有 9 处按实证改过)

## Global Constraints

- 两个路径常量只有一个真源:`sandbox_image_contract.NAS_MOUNT = "/mnt/workspace"`、`EXEC_VIEW = "/workspace"`。**沙箱内代码(工具片段、用户代码、提示词)永远不出现 `/mnt/workspace`**;后端拼命令串、建沙箱、本地 `--volume/--workdir` 才用它。runtime 包自带副本 `SANDBOX_NAS_MOUNT` / `SANDBOX_EXEC_VIEW`,由契约文件末尾的闸钉相等。
- 挂载仍按 `(tenant, user)`:`agent_sandbox.py:388-401` 硬闸、`_workspace_subpath`、CSI subPath **一律不动**。
- sh 脚本体 `EXEC_VIEW_SCRIPT` 在 `orchestrator/tools/exec_view.py` 与 `infra/sandbox-image/runner.py` 各一份**逐字相同**的字面量(镜像代码不能 import 仓库),由 `test_exec_view_script_matches_the_sandbox_image` 用 `ast` 钉住。改一边必红。
- 任何 bind/mount 失败 → exec 非零退出 → `SandboxSupervisorError`,**不回落**到用户根。
- 沙箱镜像 `infra/sandbox-image/Dockerfile` **不动**,且不许预建 `/workspace`(ACS 老代码 + 新镜像 = symlink 建不上)。ACS 运行期 `mkdir -p /workspace`,本地 docker `--tmpfs /workspace:ro,size=4k`。
- seccomp:只加两条无 cap 门控规则(`unshare` mask 到 `CLONE_NEWUSER|CLONE_NEWNS`;`mount` + 七个新挂载 API);`umount2` / `setns` / `pivot_root` 保持 cap 门控。本地后端的 `seccomp_profile_path=None` 从此是**启动错误**。
- `layout` 字面量只在 `expert_work.persistence.sandbox_instance_store`:`SANDBOX_LAYOUT_USER_ROOT = "user-root"`、`SANDBOX_LAYOUT_AGENT_NS = "agent-ns"`;迁移 0155 的 `server_default` 与之同值。
- 未绑 agent(`agent_key == ""`)与临时沙箱行为**不变**:视图 = 整个用户根,不盖、不挂 shared。
- 工具描述与文档口径(spec §五):说「默认视图里只有你自己的目录」,**不说「隔离」**。
- 本地测试:`uv run --no-sync pytest <具体文件>`。**绝不跑全仓 pytest,也不跑整个 `services/control-plane/tests`。** 需要 docker 的用例先 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。CI 是仲裁。
- 每条新增断言必须 break→red→restore→green 自证;变异前先 commit,`git diff` 确认变异落地,还原一律手写回原文,**永不用 `git checkout` / `git stash`**。重言式变异要拒绝并报告。
- commit message 用中文 conventional commit,结尾加:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DNzoY8vc8jDMsib4wFGFpN
  ```

---

## 文件结构

| 文件 | 职责 | Task |
|---|---|---|
| `packages/expert-work-persistence/migrations/versions/0155_sandbox_instance_layout.py` | **新建**:`sandbox_instance.layout` 列 | 1 |
| `packages/expert-work-persistence/src/expert_work/persistence/models/sandbox_instance.py` | ORM 列 | 1 |
| `packages/expert-work-persistence/src/expert_work/persistence/sandbox_instance_store.py` | 两个 layout 常量;`claim_warm` / `create_ephemeral` 收 `layout`,`claim_warm` 返四元组(SQL + 内存) | 2 |
| `services/orchestrator/src/orchestrator/tools/sandbox_instance_store.py` | Protocol 签名 | 3 |
| `services/orchestrator/src/orchestrator/tools/agent_sandbox.py` | `layout` 字段、`layout_mismatch` 重建(T3);mountPath / chown / `mkdir -p /workspace` / exec 命令串 / layout 翻 `agent-ns`(T7) | 3, 7 |
| `services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py` | `NAS_MOUNT` / `EXEC_VIEW`(T4);删 `WORKSPACE_ROOT`(T12) | 4, 12 |
| `services/orchestrator/src/orchestrator/tools/workspace_paths.py` | `agent_nas_root` / `agent_view_alias`(T4);`resolve_scope` 指向视图(T6);删 `USER_ROOT` / `agent_workspace_root`(T12) | 4, 6, 12 |
| `services/orchestrator/src/orchestrator/tools/exec_view.py` | **新建**:sh 字面量、argv 前缀、`build_exec_command` | 5 |
| `services/orchestrator/src/orchestrator/tools/file_ops.py` | `ws` = 视图;`shared/` 保留段;定位片段去认领 | 6 |
| `services/orchestrator/src/orchestrator/tools/artifact.py` | 折叠/保留段同规则;去认领话术与 `location` | 6 |
| `services/orchestrator/src/orchestrator/tools/read_document.py` | `ws` = 视图 | 6 |
| `services/orchestrator/src/orchestrator/tools/sandbox.py` | `HTTPSupervisorRuntime.exec` 发 `agent_root` | 6 |
| `infra/sandbox-image/seccomp-profile.json` | 两条新规则 | 8 |
| `services/sandbox-supervisor/src/sandbox_supervisor/seccomp.py` | `None` 拒绝启动 | 8 |
| `infra/docker-compose.yml` | 挂 profile + 设 env | 8 |
| `packages/expert-work-runtime/src/expert_work/runtime/sandbox/runtime_provider.py` | 两常量;argv:NAS 卷/tmpfs、`/workspace` 只读 tmpfs、workdir、apparmor | 9 |
| `infra/sandbox-image/runner.py` | 脚本字面量;argv;`agent_root` | 10 |
| `services/sandbox-supervisor/src/sandbox_supervisor/{schemas,runner_link,supervisor,app}.py` | `cwd` → `agent_root` | 10 |
| `services/orchestrator/tests/test_sandbox_runtime_contract.py` | 六条视图契约用例 + 三道漂移闸 | 11 |
| `services/sandbox-supervisor/tests/test_supervisor_integration.py` | seccomp 路径(T8);视图验收用例(T11) | 8, 11 |
| `.github/workflows/{ci,sandbox-gvisor}.yml` | sysctl 步骤 | 11 |
| 文档:B-50 spec §5.3 补记、ROADMAP B-60 行、工具描述口径 | | 12 |

---

## PR 切分与波次(给用户拍板)

| PR | 内容 | 可独立上线? |
|---|---|---|
| **PR-A**(= #1552,已开) | spec(含 §零 修订)+ 本计划 | docs |
| **PR-B** | Task 1-3:`layout` 列 + store 进出 + acquire 布局闸(传 `user-root`) | ✅ 零行为变化(所有行都是 `user-root`,闸不触发) |
| **PR-C** | Task 4-12:写入侧全量 | ✅ 但**必须整体合**:工具层视图语义与两个后端的挂载点改动缺一边就是回归(工具写用户根 / 命令串 bind 不到) |

**PR-B 与 PR-C 可并行开工**(不同 worktree;PR-C 的 Task 3 依赖只在 Task 7 翻 `layout` 默认值那一行,PR-B 先合再 rebase 即可)。

PR-C 内波次(按文件冲突分析,同一波内文件两两不相交):

| 波 | 并行任务 | 依赖 |
|---|---|---|
| 1 | T4(orchestrator 常量 + workspace_paths)∥ T8(seccomp + supervisor 启动 + compose + 验收 fixture)∥ T9(runtime_provider argv) | — |
| 2 | T5(exec_view.py)∥ T10(runner.py + supervisor 线协议) | T5 依赖 T4;T10 的脚本字面量抄 spec §4.3(与 T5 同源),T11 的闸对齐 |
| 3 | T6(工具层 + 拆认领 + HTTP 客户端)∥ T7(ACS 后端) | 都依赖 T4/T5 |
| 4 | T11(契约 + 漂移闸 + 验收 + CI sysctl) | 全部 |
| 5 | T12(清理旧名 + Dockerfile 闸 + 文档) | 全部 |
| 合并发测后 | T13(真栈验收) | — |

---

## PR-B

### Task 1: 迁移 0155 + ORM 列

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0155_sandbox_instance_layout.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/sandbox_instance.py:59`(`destroy_reason` 之后)
- Test: `packages/expert-work-persistence/tests/test_sandbox_instance_layout.py`(新建)

**Interfaces:**
- Produces: `SandboxInstanceRow.layout: Mapped[str]`,`server_default 'user-root'`,NOT NULL。

- [ ] **Step 1: 写失败的测试**

```python
"""B-60 —— ``sandbox_instance.layout``:热会话的沙箱内布局版本(迁移 0155)。"""

from __future__ import annotations

from sqlalchemy import Text

from expert_work.persistence.models.sandbox_instance import SandboxInstanceRow


def test_layout_column_is_text_not_null_default_user_root() -> None:
    col = SandboxInstanceRow.__table__.c["layout"]
    assert isinstance(col.type, Text)
    assert col.nullable is False
    assert col.server_default is not None
    # 与迁移 0155 的 server_default 同一个字面量;存量行(0155 之前建的热会话)
    # 全部落这个值,acquire 据此把它们销毁重建。
    assert col.server_default.arg.text == "'user-root'"
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_sandbox_instance_layout.py -q`
Expected: FAIL `KeyError: 'layout'`

- [ ] **Step 3: 写迁移**

```python
"""sandbox_instance 加 layout —— 热会话的沙箱内布局版本(B-60)。

spec ``docs/superpowers/specs/2026-09-14-workspace-exec-mount-namespace-design.md`` §4.6。
``'user-root'`` = NAS 直接挂 ``/workspace`` 的旧布局(本迁移之前建的每一行都是它);
``'agent-ns'`` = NAS 挂 ``/mnt/workspace``、每次 exec 在自己的命名空间里把 agent
目录 bind 成 ``/workspace``。``AgentSandboxClient.acquire`` 拿到与本进程不同布局的热
会话就销毁重建(``destroy_reason='layout_mismatch'``)—— 所以这是 expand-only,
不需要回填:默认值就是存量行的真实布局。

Revision ID: 0155_sandbox_instance_layout
Revises: 0154_artifact_agent_key
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0155_sandbox_instance_layout"
down_revision: str | Sequence[str] | None = "0154_artifact_agent_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "sandbox_instance",
        sa.Column("layout", sa.Text(), nullable=False, server_default="user-root"),
    )


def downgrade() -> None:
    op.drop_column("sandbox_instance", "layout")
```

- [ ] **Step 4: 加 ORM 列**(`destroy_reason` 之后)

```python
    #: B-60 —— 沙箱内布局版本(迁移 0155)。``'user-root'`` = NAS 直接挂 ``/workspace``
    #: 的旧布局;``'agent-ns'`` = NAS 挂 ``/mnt/workspace``、每次 exec 在自己的命名空间
    #: 里把 agent 目录 bind 成 ``/workspace``。``AgentSandboxClient.acquire`` 拿到与本
    #: 进程不同布局的热会话就销毁重建(``destroy_reason='layout_mismatch'``)。字面量
    #: 的单源在 ``sandbox_instance_store.SANDBOX_LAYOUT_*``,这里只是 server_default。
    layout: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user-root'"))
```

- [ ] **Step 5: 跑,确认绿**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_sandbox_instance_layout.py -q`
Expected: PASS

- [ ] **Step 6: 迁移链自检**

Run: `grep -rn "down_revision" packages/expert-work-persistence/migrations/versions/0155_*.py && ls packages/expert-work-persistence/migrations/versions | grep -c "^015[45]_"`
Expected: `down_revision = "0154_artifact_agent_key"`;计数 2(没有第二个 0155)。

- [ ] **Step 7: Commit**

```bash
git add packages/expert-work-persistence/migrations/versions/0155_sandbox_instance_layout.py packages/expert-work-persistence/src/expert_work/persistence/models/sandbox_instance.py packages/expert-work-persistence/tests/test_sandbox_instance_layout.py
git commit -m "feat(persistence): sandbox_instance 加 layout 列(迁移 0155,expand-only)"
```

---

### Task 2: store 的 `layout` 进出(SQL + 内存)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/sandbox_instance_store.py`(常量区 ~:109;`claim_warm` :164-323;`create_ephemeral` :324-356;`_MemRow` :671;内存 `claim_warm` :723;内存 `create_ephemeral` :762)
- Test: `packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py`、`packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py`

**Interfaces:**
- Produces:
  - `SANDBOX_LAYOUT_USER_ROOT: str = "user-root"`、`SANDBOX_LAYOUT_AGENT_NS: str = "agent-ns"`
  - `claim_warm(*, tenant_id, user_id, sandbox_id, layout: str) -> tuple[UUID, str, datetime | None, str] | None`(第四元 = **赢家那一行**建时写的 layout)
  - `create_ephemeral(*, tenant_id, sandbox_id, layout: str) -> None`

- [ ] **Step 1: 写失败的测试(内存)**

在 `test_in_memory_sandbox_instance_store.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_claim_warm_returns_the_winners_layout_not_the_callers() -> None:
    """B-60 —— 输家拿到的第四元是赢家那一行建时写的布局。``acquire`` 靠它判
    「这个热会话是不是按我认得的布局建的」,判错方向会把好会话重建、坏会话复用。"""
    store = InMemorySandboxInstanceStore()
    tenant_id, user_id, winner_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(
            tenant_id=tenant_id, user_id=user_id, sandbox_id=winner_id, layout="user-root"
        )
        is None
    )
    await store.set_container_id(sandbox_id=winner_id, container_id="sbx-warm")

    result = await store.claim_warm(
        tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4(), layout="agent-ns"
    )

    assert result is not None
    assert result[0] == winner_id
    assert result[3] == "user-root"
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py -q -k winners_layout`
Expected: FAIL `TypeError: ... unexpected keyword argument 'layout'`

- [ ] **Step 3: 常量 + SQL store**

在 `_REASON_STUCK_CREATE_TAKEOVER` 附近加:

```python
#: B-60 —— 沙箱内布局版本(``sandbox_instance.layout``,迁移 0155)。由**调用方**
#: (``AgentSandboxClient``)传进 :meth:`claim_warm` / :meth:`create_ephemeral` 写进
#: INSERT;``claim_warm`` 把赢家行的 layout 随同一次 SELECT 返回。字面量只在这里:
#: 迁移的 server_default 是 ``'user-root'`` 的第二份副本,``test_sandbox_instance_layout``
#: 钉它俩相等。
SANDBOX_LAYOUT_USER_ROOT = "user-root"
SANDBOX_LAYOUT_AGENT_NS = "agent-ns"
```

`SqlSandboxInstanceStore.claim_warm`:签名改为

```python
    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID, layout: str
    ) -> tuple[UUID, str, datetime | None, str] | None:
```

INSERT 的 `.values(...)` 末尾加 `layout=layout,`;SELECT 列表加 `SandboxInstanceRow.layout,`;解包与返回改为:

```python
            winner_id, existing_container_id, winner_acquired_at, winner_layout = found
            if existing_container_id:
                return (
                    winner_id,
                    str(existing_container_id),
                    winner_acquired_at,
                    str(winner_layout),
                )
```

`create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID, layout: str)`,`.values(...)` 末尾加 `layout=layout,`。

- [ ] **Step 4: 内存 store**

`_MemRow` 加字段(在 `container_id` 之后):

```python
    #: Mirrors ``SandboxInstanceRow.layout``(B-60)。
    layout: str = SANDBOX_LAYOUT_USER_ROOT
```

内存 `claim_warm(self, *, tenant_id, user_id, sandbox_id, layout: str)`:占坑分支 `_MemRow(tenant_id=tenant_id, user_id=user_id, layout=layout)`;输家分支 `return (existing_id, existing.container_id, existing.acquired_at, existing.layout)`。
内存 `create_ephemeral(self, *, tenant_id, sandbox_id, layout: str)`:`_MemRow(tenant_id=tenant_id, user_id=None, layout=layout)`。

- [ ] **Step 5: 既有调用点补 `layout=`**

两个测试文件顶部 `from expert_work.persistence.sandbox_instance_store import (...)` 加 `SANDBOX_LAYOUT_USER_ROOT`。单行调用用 sed:

```bash
for f in packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py; do
  sed -i '' -E 's/(claim_warm\(tenant_id=[^,]+, user_id=[^,]+, sandbox_id=[^),]+)\)/\1, layout=SANDBOX_LAYOUT_USER_ROOT)/g; s/(create_ephemeral\(tenant_id=[^,]+, sandbox_id=[^),]+)\)/\1, layout=SANDBOX_LAYOUT_USER_ROOT)/g' "$f"
done
rg -n "claim_warm\(|create_ephemeral\(" packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py | rg -v "layout=|def |async def "
```

最后一条命令列出的是**跨行**调用,手工逐个补 `layout=SANDBOX_LAYOUT_USER_ROOT`;直到它输出为空。三元组断言(如 `assert result == (winner_id, "sbx-warm", backdated)`,`grep -n "== (" 两文件`)改成四元组,第四元 `SANDBOX_LAYOUT_USER_ROOT`。

- [ ] **Step 6: SQL 版同名用例**(`test_sql_sandbox_instance_store.py` 末尾,已有 `store` fixture 与 `pytestmark = integration`)

```python
@pytest.mark.asyncio
async def test_claim_warm_returns_the_winners_layout_not_the_callers(
    store: SqlSandboxInstanceStore,
) -> None:
    tenant_id, user_id, winner_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(
            tenant_id=tenant_id, user_id=user_id, sandbox_id=winner_id, layout="user-root"
        )
        is None
    )
    await store.set_container_id(sandbox_id=winner_id, container_id="sbx-warm")

    result = await store.claim_warm(
        tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4(), layout="agent-ns"
    )

    assert result is not None
    assert (result[0], result[3]) == (winner_id, "user-root")
```

- [ ] **Step 7: 跑两份**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_in_memory_sandbox_instance_store.py -q`
Expected: 全绿。
Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run --no-sync pytest packages/expert-work-persistence/tests/test_sql_sandbox_instance_store.py -q -m integration`
Expected: 全绿(跑 alembic head → 0155 已被真库执行过一次)。

- [ ] **Step 8: 自证一条**:把 SQL store 返回的 `str(winner_layout)` 临时改成 `layout`(返回调用方传的)→ 内存/SQL 两条新用例都红 → 手写还原 → 绿。

- [ ] **Step 9: Commit**

```bash
git add packages/expert-work-persistence
git commit -m "feat(persistence): sandbox_instance store 的 layout 进出 —— claim_warm 返回赢家布局"
```

---

### Task 3: acquire 的布局闸(本 PR 传 `user-root`,零行为变化)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox_instance_store.py:38-60`(Protocol)
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(import 区 :131 附近;常量区 :200 之后;字段区 :360 附近;`acquire` :570-600;`_claim_warm` :990;`_create_ephemeral_row` :1001)
- Test: `services/orchestrator/tests/test_agent_sandbox.py`(`FakeInstanceStore` :217-300;新用例放 `test_acquire_rebuilds_warm_session_past_age_cap` :1909 之后)

**Interfaces:**
- Consumes: Task 2 的 `claim_warm(..., layout)` 四元组、`create_ephemeral(..., layout)`、`SANDBOX_LAYOUT_USER_ROOT`。
- Produces: `AgentSandboxClient.layout: str`(本 PR 默认 `SANDBOX_LAYOUT_USER_ROOT`);`_LAYOUT_MISMATCH_DESTROY_REASON = "layout_mismatch"`。

- [ ] **Step 1: FakeInstanceStore 跟上 Task 2 的签名**

```python
    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID, layout: str
    ) -> tuple[UUID, str, datetime | None, str] | None:
        """占坑成功返 None;已被别人占且赢家已就绪返
        ``(赢家 sandbox_id, container_id, acquired_at, layout)``;赢家还在创建中则
        raise。``acquired_at`` / ``layout`` 供测试直接改写 ``self.rows[winner_id][...]``
        摆前置状态(年龄封顶 / 布局不合)。"""
        key = (tenant_id, user_id)
        winner_id = self.warm.get(key)
        if winner_id is None:
            self.warm[key] = sandbox_id
            self.rows[sandbox_id] = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "acquired_at": datetime.now(UTC),
                "layout": layout,
            }
            return None
        row = self.rows[winner_id]
        container_id = row.get("container_id")
        if container_id:
            return (winner_id, container_id, row.get("acquired_at"), row["layout"])
        msg = f"a sandbox is already being created for tenant={tenant_id} user={user_id}"
        raise RuntimeError(msg)

    async def create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID, layout: str) -> None:
        self.rows[sandbox_id] = {"tenant_id": tenant_id, "user_id": None, "layout": layout}
```

- [ ] **Step 2: 写失败的测试**(`test_acquire_rebuilds_warm_session_past_age_cap` 之后)

```python
@pytest.mark.asyncio
async def test_acquire_rebuilds_a_warm_session_built_with_another_layout() -> None:
    """B-60 spec §4.6 —— 旧布局的热会话(NAS 还挂在 /workspace)在新命令串下第一条
    bind 就会失败;与其让每次 exec 都 fail-closed,不如在 acquire 就换代。走的是与
    年龄封顶同一条重建路径,destroy_reason 不同。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    old_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    store.rows[old_id]["layout"] = "some-older-layout"

    new_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert new_id != old_id, "布局不合必须重建,不能复用旧 sandbox_id"
    assert (old_id, _LAYOUT_MISMATCH_DESTROY_REASON) in store.mark_destroyed_calls
    assert len(sdk.created) == 2, "必须真的重建(第二次 sdk.create),不是复用"
    assert store.rows[new_id]["layout"] == client.layout


@pytest.mark.asyncio
async def test_acquire_reuses_a_warm_session_with_the_same_layout() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    tenant_id, user_id = uuid4(), uuid4()

    old_id = await client.acquire(tenant_id=tenant_id, thread_id="t1", user_id=user_id)
    new_id = await client.acquire(tenant_id=tenant_id, thread_id="t2", user_id=user_id)

    assert new_id == old_id
    assert len(sdk.created) == 1
    assert all(reason != _LAYOUT_MISMATCH_DESTROY_REASON for _, reason in store.mark_destroyed_calls)


@pytest.mark.asyncio
async def test_acquire_records_the_clients_layout_on_warm_and_ephemeral_rows() -> None:
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)

    warm_id = await client.acquire(tenant_id=uuid4(), thread_id="t1", user_id=uuid4())
    ephemeral_id = await client.acquire(tenant_id=uuid4(), thread_id="t2")

    assert store.rows[warm_id]["layout"] == client.layout
    assert store.rows[ephemeral_id]["layout"] == client.layout
```

顶部 import 处加 `_LAYOUT_MISMATCH_DESTROY_REASON`(与 `_WARM_AGE_DESTROY_REASON` :76 同一行组)。

- [ ] **Step 3: 跑,确认红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_agent_sandbox.py -q -k "layout"`
Expected: FAIL `ImportError: cannot import name '_LAYOUT_MISMATCH_DESTROY_REASON'`

- [ ] **Step 4: Protocol**(`orchestrator/tools/sandbox_instance_store.py`)

`claim_warm` 签名与返回类型改成 Task 2 的四元组形态;docstring 第二个要点末尾加一句:「第四元 ``layout`` 是赢家那一行建时写的布局(B-60),供 ``acquire`` 判是否需要 ``layout_mismatch`` 重建;同样是同一次 SELECT 顺带取出。」`create_ephemeral` 加 `layout: str`。

- [ ] **Step 5: agent_sandbox.py**

import:

```python
from expert_work.persistence.sandbox_instance_store import SANDBOX_LAYOUT_USER_ROOT
```

常量(`_WORKSPACE_DELETED_RACE_REASON` 之后):

```python
#: ``destroy_reason`` written when :meth:`AgentSandboxClient.acquire` finds a warm
#: session whose ``sandbox_instance.layout`` is not the one this build lays out
#: (B-60 spec §4.6). Rebuilt through the same path as ``_WARM_AGE_DESTROY_REASON``;
#: a distinct literal so an operator can tell "换代" from "token 快到期" in the column.
_LAYOUT_MISMATCH_DESTROY_REASON = "layout_mismatch"
```

字段(`quota_gate` 之前):

```python
    #: B-60 —— 本进程给热会话铺的沙箱内布局;写进每一行,``acquire`` 拿到不同值的热会话
    #: 就 ``layout_mismatch`` 重建。PR-B 先落 ``user-root``(零行为变化),PR-C 随 exec
    #: 命令串一起翻成 ``SANDBOX_LAYOUT_AGENT_NS``。
    layout: str = SANDBOX_LAYOUT_USER_ROOT
```

`_claim_warm` / `_create_ephemeral_row`:签名加返回四元组;调用 store 时加 `layout=self.layout`。

`acquire`(:572-600)—— 把

```python
            winner_id, winner_container_id, winner_acquired_at = existing
            if (
                winner_acquired_at is not None
                and (_utc_now() - winner_acquired_at).total_seconds() > self._max_warm_age_s()
            ):
                # #1b ...(原注释)
                logger.info(
                    "warm sandbox %s past age cap (%ss), rebuilding for a fresh egress token",
                    winner_id,
                    self._max_warm_age_s(),
                )
                await self.destroy(sandbox_id=winner_id, reason=_WARM_AGE_DESTROY_REASON)
```

改成

```python
            winner_id, winner_container_id, winner_acquired_at, winner_layout = existing
            # B-60 —— 两种「不能复用,必须重建」走同一条路:destroy 真 kill + 清行,重占坑,
            # 往下落进重建分支。布局不合先判、更硬:年龄只是「快到期」,布局是「跑不了」
            # (旧热会话的 NAS 还挂在 /workspace,新命令串第一条 bind 就失败)。
            rebuild_reason: str | None = None
            if winner_layout != self.layout:
                rebuild_reason = _LAYOUT_MISMATCH_DESTROY_REASON
            elif (
                winner_acquired_at is not None
                and (_utc_now() - winner_acquired_at).total_seconds() > self._max_warm_age_s()
            ):
                # #1b ...(原注释原样保留)
                rebuild_reason = _WARM_AGE_DESTROY_REASON
            if rebuild_reason is not None:
                logger.info(
                    "warm sandbox %s not reusable (%s; layout=%s wanted=%s; age cap %ss), rebuilding",
                    winner_id,
                    rebuild_reason,
                    winner_layout,
                    self.layout,
                    self._max_warm_age_s(),
                )
                await self.destroy(sandbox_id=winner_id, reason=rebuild_reason)
```

后面那段(`if user_id is not None: await self._claim_warm(...)` 起到 `just_created = True`)一字不动。

- [ ] **Step 6: 跑**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_agent_sandbox.py -q`
Expected: 全绿(含既有年龄封顶两条)。

- [ ] **Step 7: 自证**:把 `if winner_layout != self.layout:` 改成 `if False:` → `test_acquire_rebuilds_a_warm_session_built_with_another_layout` 红 → 手写还原 → 绿。

- [ ] **Step 8: Commit + 开 PR-B**

```bash
git add services/orchestrator
git commit -m "feat(sandbox): acquire 按 sandbox_instance.layout 判热会话是否可复用(B-60 闸,先传 user-root)"
```

PR 正文写明「零行为变化:所有行都是 user-root,闸不触发;PR-C 翻默认值」。

---

## PR-C

### Task 4: 常量拆分 + `workspace_paths` 两个新函数

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py:24-25`
- Modify: `services/orchestrator/src/orchestrator/tools/workspace_paths.py:27-30, 70-81`
- Test: `services/orchestrator/tests/test_workspace_paths.py`

**Interfaces:**
- Produces:`NAS_MOUNT = "/mnt/workspace"`、`EXEC_VIEW = "/workspace"`(`sandbox_image_contract`);`WORKSPACE_ROOT = EXEC_VIEW`、`USER_ROOT = EXEC_VIEW`、`agent_workspace_root` **暂留为过渡别名**(Task 12 删);`agent_nas_root(agent_key) -> str`、`agent_view_alias(agent_key) -> str`(空/坏 key 一律 `ValueError`)。
- 本 Task **不改** `resolve_scope` 的返回(值本来就是 `/workspace...`,语义切换放 Task 6 与工具测试一起)。

- [ ] **Step 1: 写失败的测试**(`test_workspace_paths.py` 末尾;顶部 import 加 `agent_nas_root, agent_view_alias` 与 `from orchestrator.tools.sandbox_image_contract import EXEC_VIEW, NAS_MOUNT`)

```python
def test_agent_nas_root_points_under_the_nas_mount() -> None:
    """B-60 —— 后端拼 exec 用的 bind 源:NAS 挂载点下的真实目录。"""
    assert agent_nas_root("plan-aaaaaaaa") == "/mnt/workspace/agents/plan-aaaaaaaa"


def test_agent_view_alias_is_the_legacy_absolute_spelling() -> None:
    """只用于折叠模型照旧写的 ``/workspace/agents/<key>/x``;视图里没有这个目录。"""
    assert agent_view_alias("plan-aaaaaaaa") == "/workspace/agents/plan-aaaaaaaa"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a b", "../x", "a\x00b"])
def test_nas_root_and_alias_refuse_unsafe_or_empty_keys(bad: str) -> None:
    """空 key 也拒:未绑 agent 没有「自己的目录」,调用方自己分支,别让它拿到用户根。"""
    with pytest.raises(ValueError):
        agent_nas_root(bad)
    with pytest.raises(ValueError):
        agent_view_alias(bad)


def test_view_and_mount_are_distinct_roots() -> None:
    """视图根与挂载点互不包含 —— tmpfs 盖挂载点时不能把视图也盖掉。"""
    assert (NAS_MOUNT, EXEC_VIEW) == ("/mnt/workspace", "/workspace")
    assert not NAS_MOUNT.startswith(EXEC_VIEW + "/")
    assert not EXEC_VIEW.startswith(NAS_MOUNT + "/")
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_workspace_paths.py -q`
Expected: FAIL `ImportError`

- [ ] **Step 3: 常量**(`sandbox_image_contract.py` :24-25 换成)

```python
#: 沙箱内用户根的**挂载点**(B-60):建沙箱 ``metadata.mountPath`` / 兜底 ``chown`` /
#: 本地 ``--volume``、``--workdir``。沙箱内的代码(工具片段、用户代码、提示词)**永远
#: 不该**出现这个字符串 —— exec 进了命名空间之后它被 tmpfs 盖住,不存在。
NAS_MOUNT = "/mnt/workspace"
#: 每次 exec 的命名空间里看到的根(B-60):命令串里 bind 的目标、提示词与工具描述的
#: 锚、文件工具的 ``ws``。绑了 agent 就是它自己的目录,没绑就是整个用户根。
EXEC_VIEW = "/workspace"
#: B-60 过渡别名 —— 调用点分批切到 NAS_MOUNT / EXEC_VIEW,Task 12 删除。
WORKSPACE_ROOT = EXEC_VIEW
```

- [ ] **Step 4: workspace_paths.py**

import 加 `from orchestrator.tools.sandbox_image_contract import EXEC_VIEW, NAS_MOUNT`;`USER_ROOT = "/workspace"` 换成 `USER_ROOT = EXEC_VIEW`(注释:过渡别名,Task 12 删)。`agent_workspace_root` 换成:

```python
def _require_safe_key(agent_key: str) -> None:
    if not agent_key or agent_key in _DOTTED or not _AGENT_KEY_OK.match(agent_key):
        msg = f"agent_key is not a safe path segment: {agent_key!r}"
        raise ValueError(msg)


def agent_nas_root(agent_key: str) -> str:
    """NAS 挂载下该 agent 的真实目录 —— **只给两个后端拼 exec 用**(命名空间里 bind
    到 ``EXEC_VIEW`` 上的源;B-60 spec §4.3)。空 key 拒绝:未绑 agent 没有「自己的
    目录」,调用方自己分支(未绑 → bind 整个用户根),别让它悄悄拿到 ``NAS_MOUNT``。
    """
    _require_safe_key(agent_key)
    return f"{NAS_MOUNT}/{AGENTS_DIR}/{agent_key}"


def agent_view_alias(agent_key: str) -> str:
    """模型照旧可能写的 ``/workspace/agents/<key>`` 拼法 —— **只用于折叠**
    (``file_ops._require_path`` / ``artifact._validate_path``),不指向任何真实目录:
    视图里没有 ``agents/``。空 key 拒绝,同上。
    """
    _require_safe_key(agent_key)
    return f"{EXEC_VIEW}/{AGENTS_DIR}/{agent_key}"


def agent_workspace_root(agent_key: str) -> str:
    """B-60 过渡别名 —— 语义仍是 B-50 的(未绑 → 用户根;绑了 → 别名拼法)。
    Task 6/7 把调用点换成 ``agent_nas_root`` / ``agent_view_alias`` / ``EXEC_VIEW``,
    Task 12 删除。"""
    if not agent_key:
        return EXEC_VIEW
    return agent_view_alias(agent_key)
```

- [ ] **Step 5: 跑**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_workspace_paths.py services/orchestrator/tests/test_file_ops.py services/orchestrator/tests/test_artifact_tools.py -q`
Expected: 全绿(过渡别名保证既有用例不动)。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py services/orchestrator/src/orchestrator/tools/workspace_paths.py services/orchestrator/tests/test_workspace_paths.py
git commit -m "refactor(tools): NAS_MOUNT / EXEC_VIEW 拆分 + agent_nas_root / agent_view_alias(B-60 T4)"
```

---

### Task 5: `exec_view.py` —— 两个后端的命令串单源

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/exec_view.py`
- Test: `services/orchestrator/tests/test_exec_view.py`

**Interfaces:**
- Consumes: `agent_nas_root`(T4)、`SANDBOX_PYTHON_FLAGS`。
- Produces: `EXEC_VIEW_SCRIPT: str`、`EXEC_VIEW_ARGV_PREFIX: tuple[str, ...]`、`exec_view_argv(agent_key: str, python_argv: Sequence[str]) -> list[str]`、`build_exec_command(agent_key: str, script_path: str) -> str`。

- [ ] **Step 1: 写失败的测试**

```python
"""B-60 —— 每次 exec 一个私有 ``/workspace`` 的命令串单源(spec §4.3)。"""

from __future__ import annotations

import shlex
import subprocess

import pytest

from orchestrator.tools.exec_view import (
    EXEC_VIEW_ARGV_PREFIX,
    EXEC_VIEW_SCRIPT,
    build_exec_command,
    exec_view_argv,
)

_PY = ["python", "-E", "-P", "/tmp/ew-exec-abc.py"]  # noqa: S108 — 沙箱 tmpfs 路径,不是本机


def test_script_fails_closed_and_names_both_roots() -> None:
    assert EXEC_VIEW_SCRIPT.startswith("set -eu\n")
    assert 'mount --bind "$root" /workspace\n' in EXEC_VIEW_SCRIPT
    assert "mount -t tmpfs -o size=1k none /mnt/workspace\n" in EXEC_VIEW_SCRIPT
    assert "mount --bind /mnt/workspace /workspace\n" in EXEC_VIEW_SCRIPT  # 未绑分支
    assert EXEC_VIEW_SCRIPT.endswith('cd /workspace\nexec "$@"\n')
    # spec §零 #2:uploads 在 agents/<key>/uploads,挂用户根的 uploads 会遮住它
    assert "uploads" not in EXEC_VIEW_SCRIPT


def test_shared_is_bound_read_only_with_locked_flags_kept() -> None:
    assert "mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared\n" in EXEC_VIEW_SCRIPT


def test_script_parses_as_posix_sh() -> None:
    assert subprocess.run(["sh", "-n"], input=EXEC_VIEW_SCRIPT, text=True, check=False).returncode == 0


def test_argv_prefix_is_unshare_user_and_mount_namespaces() -> None:
    assert EXEC_VIEW_ARGV_PREFIX == (
        "unshare", "-Urm", "--propagation", "private", "--",
        "sh", "-c", EXEC_VIEW_SCRIPT, "ew-exec-view",
    )


def test_argv_bound_passes_the_nas_root_as_first_positional() -> None:
    assert exec_view_argv("plan-aaaaaaaa", _PY) == [
        *EXEC_VIEW_ARGV_PREFIX, "/mnt/workspace/agents/plan-aaaaaaaa", *_PY,
    ]


def test_argv_unbound_passes_an_empty_root() -> None:
    assert exec_view_argv("", _PY) == [*EXEC_VIEW_ARGV_PREFIX, "", *_PY]


def test_build_exec_command_bound_and_unbound_verbatim() -> None:
    prefix = "umask 077 && unshare -Urm --propagation private -- sh -c " + shlex.quote(EXEC_VIEW_SCRIPT)
    assert build_exec_command("plan-aaaaaaaa", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa python -E -P /tmp/ew-exec-abc.py"
    )
    assert build_exec_command("", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view '' python -E -P /tmp/ew-exec-abc.py"
    )


def test_script_path_is_quoted_not_interpolated() -> None:
    cmd = build_exec_command("", "/tmp/a b;rm -rf x.py")  # noqa: S108
    assert shlex.split(cmd[len("umask 077 && ") :])[-1] == "/tmp/a b;rm -rf x.py"  # noqa: S108


@pytest.mark.parametrize("bad", ["..", "a/b", "a b"])
def test_bad_agent_key_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        build_exec_command(bad, "/tmp/x.py")  # noqa: S108
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_exec_view.py -q`
Expected: FAIL `ModuleNotFoundError: orchestrator.tools.exec_view`

- [ ] **Step 3: 写模块**

```python
"""每次 exec 一个私有的 ``/workspace``(B-60 spec §4.3)—— 两个后端共用的命令串单源。

沙箱里 ``/mnt/workspace`` 挂的是整个用户根(热沙箱按 ``(tenant, user)`` 复用,挂载
点没法按 agent 分)。每次 exec 起一个新的 user + mount namespace(``unshare -Urm``,
无特权 mount 的前提是当前 uid 在新 user ns 里映射成 root),在里面把 agent 目录 bind
成 ``/workspace``、``shared/`` 只读 bind 进来、再用 tmpfs 把 ``/mnt/workspace`` 整个
盖掉 —— 于是 exec 里的任何代码(含子进程)写 ``/workspace/x`` 落的就是
``agents/<key>/x``,而且看不见别的 agent。命名空间按进程树,同一沙箱里两个 agent 的
exec 并发互不影响。

脚本体是**字面量**,参数走位置参数(``$1`` = bind 源,余下 = python argv),不做字符串
拼接。``infra/sandbox-image/runner.py`` 是镜像代码、不能 import 仓库,自带一份逐字相同
的字面量,``test_exec_view_script_matches_the_sandbox_image`` 用 ``ast`` 钉两份相等。

失败即失败:``set -e`` 让任一条 mount 失败都非零退出,exec 报错,**不回落**到用户根。
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from orchestrator.tools.sandbox_image_contract import SANDBOX_PYTHON_FLAGS
from orchestrator.tools.workspace_paths import agent_nas_root

#: 逐条见 spec §4.3。``remount,bind,ro,nosuid,nodev,noexec``:userns 里 remount 不能
#: **去掉**源挂载上锁住的 flag,但可以**加**,三个都写上永远合法。``size=1k`` 让盖住
#: 用户根的 tmpfs 没法当可写盘。**不挂 uploads**:它在 ``agents/<key>/uploads``。
EXEC_VIEW_SCRIPT = """\
set -eu
root="$1"; shift
if [ -n "$root" ]; then
  mkdir -p "$root"
  mount --bind "$root" /workspace
  if [ -d /mnt/workspace/shared ]; then
    mkdir -p /workspace/shared
    mount --bind /mnt/workspace/shared /workspace/shared
    mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared
  fi
  mount -t tmpfs -o size=1k none /mnt/workspace
else
  mount --bind /mnt/workspace /workspace
fi
cd /workspace
exec "$@"
"""

#: ``sh -c <script> <$0> <$1> <python argv…>``;``$0`` 只是报错时的名字。
EXEC_VIEW_ARGV_PREFIX: tuple[str, ...] = (
    "unshare",
    "-Urm",
    "--propagation",
    "private",
    "--",
    "sh",
    "-c",
    EXEC_VIEW_SCRIPT,
    "ew-exec-view",
)


def exec_view_argv(agent_key: str, python_argv: Sequence[str]) -> list[str]:
    """完整 argv:前缀 + ``$1``(绑了 agent 是 NAS 上的真实目录,未绑是空串)+ python。"""
    root = agent_nas_root(agent_key) if agent_key else ""
    return [*EXEC_VIEW_ARGV_PREFIX, root, *python_argv]


def build_exec_command(agent_key: str, script_path: str) -> str:
    """ACS 后端 ``commands.run``(``/bin/bash -l -c``)用的命令串。

    ``umask 077`` 在最前:命名空间里 ``mkdir -p "$root"`` 建出来的 agent 目录也要落
    ``0o700``(与 ``NasWorkspaceStore._DIR_MODE`` 同一个数字)。其余全部 ``shlex.join``,
    脚本路径不进任何拼接。
    """
    argv = exec_view_argv(agent_key, ["python", *SANDBOX_PYTHON_FLAGS, script_path])
    return "umask 077 && " + shlex.join(argv)
```

- [ ] **Step 4: 跑**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_exec_view.py -q`
Expected: PASS

- [ ] **Step 5: 自证**:把脚本里 `set -eu` 改成 `set -u` → `test_script_fails_closed_and_names_both_roots` 红 → 还原 → 绿。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/exec_view.py services/orchestrator/tests/test_exec_view.py
git commit -m "feat(tools): exec_view —— 每次 exec 一个私有 /workspace 的命令串单源(B-60 T5)"
```

---

### Task 6: 工具层看视图 + 拆 #1551 认领分支 + HTTP 客户端发 `agent_root`

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/workspace_paths.py:84-105`(`resolve_scope`)
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py`(import :55-77;`_require_path` :133-156;`_ARTIFACT_LOCATE_MAIN` :424-461;`build_artifact_locate_wrapper` :490-515)
- Modify: `services/orchestrator/src/orchestrator/tools/artifact.py`(import :34;`_validate_path` :68-95;`call` :187-240;`_locate_file` :242-296)
- Modify: `services/orchestrator/src/orchestrator/tools/read_document.py:42-47`
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py:40, 292-300`
- Test: `services/orchestrator/tests/test_workspace_paths.py`、`test_file_ops.py`、`test_artifact_tools.py`、`test_sandbox_trace_propagation.py:215-252`

**Interfaces:**
- Consumes: `EXEC_VIEW`、`agent_view_alias`、`agent_nas_root`(T4)。
- Produces: 文件工具/`read_document`/`save_artifact` 的 `ws` 恒为 `EXEC_VIEW`;`build_artifact_locate_wrapper(rel, *, ws=EXEC_VIEW)`,信封 `{"ok": True, "size": N}` / `{"ok": False, "error": "not_found" | "not_a_file" | "path_escapes_workspace"}`;`SaveArtifactTool.call` 的 `meta` 不再有 `location`;`HTTPSupervisorRuntime.exec` 绑了 agent 时 payload 带 `"agent_root": agent_nas_root(key)`,未绑不带。

- [ ] **Step 1: 写失败的测试**

`test_workspace_paths.py`:把 `test_bare_path_resolves_under_agent_root` 改成

```python
def test_bare_path_resolves_to_the_exec_view() -> None:
    """B-60 —— 文件工具的片段与用户代码在同一个命名空间里,看同一个视图:
    绑了 agent 时 /workspace **就是** agent 目录,ws 不再拼 agents/<key>。"""
    ws, rel = resolve_scope("MEMORY.md", agent_key="plan-aaaaaaaa", tool="read_file")
    assert (ws, rel) == (EXEC_VIEW, "MEMORY.md")
```

`test_empty_agent_key_falls_back_to_user_root` 的断言改 `assert ws == EXEC_VIEW`;:131 的 `f"{workspace_paths.USER_ROOT}/…"` 改 `f"{EXEC_VIEW}/…"`;:34 `"/workspace/shared"` 不变。

`test_file_ops.py`:文件顶部若有 `_AGENT_WS = '"ws": "/workspace/agents/plan-aaaaaaaa"'` 之类常量(`grep -n "_AGENT_WS =\|_USER_WS =" services/orchestrator/tests/test_file_ops.py`),加 `_VIEW_WS = '"ws": "/workspace"'`。改:

- `test_absolute_agent_path_folds_to_relative`:`assert _AGENT_WS in code` → `assert _VIEW_WS in code`。
- `test_projection_writer_still_targets_the_user_root` 整条换成:

```python
async def test_projection_writer_targets_the_exec_view() -> None:
    """B-60 —— 投影(``threads/<tid>/PLAN.md``)随视图落进 agent 目录:绑了 agent 的进程
    里根本没有用户根可写。删除侧 ``sessions.py`` 与留存 job 早已两处都删
    (``agents/<key>/threads/…`` 与用户根 ``threads/…``),不需要跟着改。"""
    client = _client(json.dumps({"ok": True, "size": 2}))
    writer = SandboxWorkspaceWriter(client=client, ctx=_ctx(agent_key="plan-aaaaaaaa"))
    await writer.write(rel="threads/t1/PLAN.md", content="x")
    assert _VIEW_WS in client.execs[-1][1]
```

- 新增保留段用例(放 `test_another_agents_absolute_path_is_not_folded` 之后):

```python
@pytest.mark.parametrize("path", ["shared/x.md", "shared", "/workspace/shared/x.md"])
async def test_bare_shared_segment_is_reserved_when_bound(path: str) -> None:
    """视图里 /workspace/shared 是只读 bind;裸 shared/… 写会 EROFS、读会读到别的东西。
    拒掉并指向 shared: 前缀,而不是静默 io_error。"""
    with pytest.raises(ValueError, match="shared:"):
        await ReadFileTool(client=_client()).call({"path": path}, ctx=_ctx(agent_key="plan-aaaaaaaa"))


async def test_bare_shared_segment_is_plain_when_unbound() -> None:
    client = _client(json.dumps({"ok": True, "content": "", "content_hash": "x", "size": 0}))
    await ReadFileTool(client=client).call({"path": "shared/x.md"}, ctx=_ctx())
    assert '"rel": "shared/x.md"' in client.execs[-1][1]
```

- 定位片段(:855-950)整段换成:

```python
# ---------------------------------------------------------------------------
# save_artifact 的定位片段 —— 真跑在 tmp_path 上。B-60 之后只在视图里 stat,不认领:
# exec 已经写不到用户根,认领分支是带着洞形状的死代码(spec §4.8)。
# ---------------------------------------------------------------------------


def test_locate_finds_a_file_under_ws(tmp_path: Path) -> None:
    (tmp_path / "deck.pptx").write_bytes(b"x" * 7)
    env = _run_snippet(build_artifact_locate_wrapper("deck.pptx", ws=str(tmp_path)))
    assert env == {"ok": True, "size": 7}


def test_locate_reports_not_found_and_never_looks_outside_ws(tmp_path: Path) -> None:
    """用户根上有同名文件、ws 是它的子目录 —— 必须 not_found,不能「找到」。"""
    ws = tmp_path / "agents" / "me-aaaaaaaa"
    ws.mkdir(parents=True)
    (tmp_path / "deck.pptx").write_bytes(b"y")
    env = _run_snippet(build_artifact_locate_wrapper("deck.pptx", ws=str(ws)))
    assert env == {"ok": False, "error": "not_found"}
    assert (tmp_path / "deck.pptx").exists(), "不认领:源文件必须原地不动"


def test_locate_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / "outputs").mkdir()
    env = _run_snippet(build_artifact_locate_wrapper("outputs", ws=str(tmp_path)))
    assert env == {"ok": False, "error": "not_a_file"}


def test_locate_escape_is_blocked(tmp_path: Path) -> None:
    env = _run_snippet(build_artifact_locate_wrapper("../../etc/passwd", ws=str(tmp_path)))
    assert env == {"ok": False, "error": "path_escapes_workspace"}
```

`test_artifact_tools.py`:
- `_sandbox` 默认信封改 `{"ok": True, "size": 10}`;所有 `result.meta == {...}` 严格断言去掉 `"location": "agent"`(`grep -n '"location"' services/orchestrator/tests/test_artifact_tools.py`,逐条删)。
- 删除 `test_save_artifact_claims_a_file_left_at_the_user_root`、`test_save_artifact_refuses_to_claim_from_a_foreign_scope`。
- :401-403 的 `assert "/workspace/agents/ai-health-plan-30817804" in code` 改 `assert '"ws": "/workspace"' in code`。
- 新增:

```python
@pytest.mark.asyncio
async def test_save_artifact_refuses_the_reserved_shared_segment() -> None:
    with pytest.raises(ValueError, match="shared"):
        await SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox()).call(
            {"name": "x", "path": "shared/x.md"}, ctx=_ctx(agent_key="me-aaaaaaaa")
        )


@pytest.mark.asyncio
async def test_save_artifact_result_has_no_claim_note_or_location() -> None:
    result = await SaveArtifactTool(store=InMemoryArtifactStore(), client=_sandbox()).call(
        {"name": "a.md"}, ctx=_ctx(agent_key="me-aaaaaaaa")
    )
    assert "moved into your agent workspace" not in result.content
    assert "location" not in result.meta
```

`test_sandbox_trace_propagation.py` :230 → `assert body["agent_root"] == "/mnt/workspace/agents/my-agent"`;:250 → `assert "agent_root" not in body`;两条测试名里的 `cwd` 改 `agent_root`。

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_workspace_paths.py services/orchestrator/tests/test_file_ops.py services/orchestrator/tests/test_artifact_tools.py services/orchestrator/tests/test_sandbox_trace_propagation.py -q`
Expected: 多条 FAIL(`ws` 仍是 `/workspace/agents/…`、`location` 仍在、`build_artifact_locate_wrapper` 签名不对、payload 键仍是 `cwd`)。

- [ ] **Step 3: `resolve_scope`**

```python
        return f"{EXEC_VIEW}/{_SHARED_DIR}", rel
    # B-60 —— 绑不绑都是视图根:文件工具的片段与用户代码在同一个命名空间里,绑了
    # agent 时 /workspace 就是 agent 目录。agent_key 仍校验(不可信输入,坏 key 早点炸)。
    if agent_key:
        agent_view_alias(agent_key)
    return EXEC_VIEW, raw
```

- [ ] **Step 4: `file_ops.py`**

import:`from orchestrator.tools.sandbox_image_contract import EXEC_VIEW`;`workspace_paths` 的 import 改为 `AGENTS_DIR, SHARED_PREFIX, agent_view_alias, resolve_scope`;`_WORKSPACE_ROOT = EXEC_VIEW`(注释改:视图根,B-60)。

`_require_path` :133-156 换成:

```python
    if not prefix and agent_key:
        own = f"{agent_view_alias(agent_key)}/"
        if cleaned == own.rstrip("/"):
            cleaned = "."
        elif cleaned.startswith(own):
            cleaned = cleaned[len(own) :]
    if cleaned in (EXEC_VIEW, EXEC_VIEW + "/"):
        cleaned = "."
    elif cleaned.startswith(EXEC_VIEW + "/"):
        cleaned = cleaned[len(EXEC_VIEW) + 1 :]
    if cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        msg = f"{tool} path must be a relative workspace path without '..': {raw!r}"
        raise ValueError(msg)
    # B-50 —— ``agents/`` 是布局的保留段(原注释保留)。
    # B-60 —— ``shared/`` 同样保留:视图里 /workspace/shared 是只读 bind(spec §4.3),裸
    # 相对路径写进去是 EROFS、读则读到 legacy 区。要读就显式 ``shared:``,写一律不许。
    if agent_key and not prefix:
        head = PurePosixPath(cleaned).parts[:1]
        if head == (AGENTS_DIR,):
            msg = (
                f"{tool} path must be relative to your own workspace; "
                f"{AGENTS_DIR!r} is a reserved layout segment: {raw!r}"
            )
            raise ValueError(msg)
        if head == (_SHARED_DIR_NAME,):
            msg = (
                f"{tool}: {_SHARED_DIR_NAME!r} is the read-only shared area — read it with "
                f"the {SHARED_PREFIX!r} prefix; it is not a directory in your workspace: {raw!r}"
            )
            raise ValueError(msg)
    return prefix + cleaned
```

`_ARTIFACT_LOCATE_MAIN` 换成:

```python
_ARTIFACT_LOCATE_MAIN = """

def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    if os.path.isdir(full):
        return {"ok": False, "error": "not_a_file"}
    if not os.path.isfile(full):
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "size": os.path.getsize(full)}


print(json.dumps(_main()))
"""
```

`build_artifact_locate_wrapper`:

```python
def build_artifact_locate_wrapper(rel: str, *, ws: str = _WORKSPACE_ROOT) -> str:
    """Snippet ``save_artifact`` runs before registering ``rel``: stat it under ``ws``.

    B-60 —— only looks inside the exec view. The #1551 "claim from the user root"
    branch is gone with the hole it papered over: exec code cannot write to the
    user root any more (spec §4.8). Envelope: ``{"ok": True, "size": N}`` or
    ``{"ok": False, "error": "not_found" | "not_a_file" | "path_escapes_workspace"}``.
    """
    return _snippet({"ws": ws, "rel": rel}, _ARTIFACT_LOCATE_MAIN)
```

- [ ] **Step 5: `artifact.py`**

import 改 `from orchestrator.tools.sandbox_image_contract import EXEC_VIEW` + `from orchestrator.tools.workspace_paths import AGENTS_DIR, SHARED_PREFIX, agent_view_alias`;`_SHARED_DIR_NAME = SHARED_PREFIX.rstrip(":")` 放模块级。

`_validate_path`:

```python
    cleaned = path.strip()
    if agent_key:
        own = f"{agent_view_alias(agent_key)}/"
        if cleaned.startswith(own):
            cleaned = cleaned[len(own) :]
    if cleaned.startswith(f"{EXEC_VIEW}/"):
        cleaned = cleaned[len(EXEC_VIEW) + 1 :]
    parts = PurePosixPath(cleaned).parts
    if not cleaned or cleaned.startswith("/") or ".." in parts:
        msg = f"artifact path must be a relative workspace path without '..': {path!r}"
        raise ValueError(msg)
    if agent_key and parts and parts[0] in (AGENTS_DIR, _SHARED_DIR_NAME):
        msg = f"artifact path must not address the reserved {parts[0]}/ tree: {path!r}"
        raise ValueError(msg)
    return cleaned
```

`call`:`location = await self._locate_file(rel, ctx=ctx)` → `await self._locate_file(rel, ctx=ctx)`;删掉 `claimed = ...` / `logger.info("save_artifact.claimed_from_user_root"...)` / `note = (...)` 三段;返回块改成:

```python
        return ToolResult(
            content=(
                f"Saved artifact {name!r} (kind={kind}) as version {version.version}. "
                "The user can now download it directly from the conversation; tell them "
                "it is ready and refer to it by name — do not fabricate a download link "
                "or URL (the interface renders the download for them)."
            ),
            meta={"artifact": name, "version": version.version, "kind": kind},
        )
```

`_locate_file(self, rel, *, ctx) -> None`:`code=build_artifact_locate_wrapper(rel)`;`if env.get("ok"): return`;删 `forbidden_scope` 分支;docstring 改成「只在视图里 stat;找不到就是没写,让模型自己修」。

- [ ] **Step 6: `read_document.py`** —— `from orchestrator.tools.sandbox_image_contract import EXEC_VIEW`,`_WORKSPACE_ROOT = EXEC_VIEW`,去掉 `USER_ROOT` import。

- [ ] **Step 7: `sandbox.py`** —— import 改 `from orchestrator.tools.workspace_paths import agent_nas_root`;:292-300 换成:

```python
        # B-60 —— 绑了 agent 时把它在 NAS 上的真实目录发给 supervisor,runner 在每次 exec
        # 自己的 mount ns 里把它 bind 成 /workspace(spec §4.3)。未绑不发:runner 收不到
        # 就 bind 整个用户根。两个后端都调 agent_nas_root 这一个函数,取值逐字一致
        # (``test_sandbox_runtime_contract.py`` 钉这件事)。
        if agent_key:
            payload["agent_root"] = agent_nas_root(agent_key)
```

- [ ] **Step 8: 跑**

Run: 同 Step 2 四个文件 + `services/orchestrator/tests/test_tool_assembly.py services/orchestrator/tests/test_read_document*.py`
Expected: 全绿。`rg -n "claimed_from_user_root|forbidden_scope|user_ws" services/orchestrator` 输出为空。

- [ ] **Step 9: 自证一条**:把 `_require_path` 里 `head == (_SHARED_DIR_NAME,)` 改成 `head == ("never",)` → 保留段用例红 → 还原 → 绿。

- [ ] **Step 10: Commit**

```bash
git add services/orchestrator
git commit -m "feat(tools): 文件工具与 save_artifact 只看 exec 视图;拆 #1551 认领分支;supervisor 客户端发 agent_root(B-60 T6)"
```

---

### Task 7: ACS 后端 —— NAS 挂 `/mnt/workspace`、root 建 `/workspace`、命令串、layout 翻 `agent-ns`

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(import :123-133;字段 `layout`;post-create try :678-700;`_chown_workspace_mount` :711-760;`_create` :1185;`exec` :1466-1500)
- Test: `services/orchestrator/tests/test_agent_sandbox.py`(:977-1031、:1079、:1138、:1190、:2575-2600 及新增)

**Interfaces:**
- Consumes: `NAS_MOUNT`、`EXEC_VIEW`(T4)、`build_exec_command`(T5)、`SANDBOX_LAYOUT_AGENT_NS`(T2)。
- Produces: `AgentSandboxClient.layout` 默认 `SANDBOX_LAYOUT_AGENT_NS`;`_ensure_exec_view_dir(sbx, *, sandbox_id)`。

- [ ] **Step 1: 改既有断言 + 写失败的测试**

- :1008 `assert cmd.startswith("umask 077 && python ")` → `assert cmd.startswith("umask 077 && unshare -Urm --propagation private -- sh -c ")`。
- :1031 → `assert cwd == NAS_MOUNT == "/mnt/workspace"`(import `NAS_MOUNT`,替换该文件里 `WORKSPACE_ROOT` 的 import 与 :1079/:1138/:1190 三处引用为 `NAS_MOUNT`)。
- :2575 `test_exec_enters_the_agent_directory_after_tightening_umask` 整条换成:

```python
@pytest.mark.asyncio
async def test_exec_command_is_the_shared_exec_view_command() -> None:
    """B-60 —— 命令串由 ``exec_view.build_exec_command`` 生成,与本地 runner 同一段
    脚本;绑了 agent 时 ``$1`` 是 NAS 上的真实目录,未绑是空串。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store)
    sid = await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5, agent_key="plan-aaaaaaaa")
    bound, *_ = sdk.sandbox.commands.calls[-1]
    await client.exec(sandbox_id=sid, code="print(1)", timeout_s=5)
    unbound, *_ = sdk.sandbox.commands.calls[-1]

    assert re.fullmatch(
        re.escape("umask 077 && ") + r"unshare -Urm --propagation private -- sh -c '.*' "
        r"ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa python -E -P /tmp/ew-exec-[0-9a-f]{32}\.py",
        bound,
        re.DOTALL,
    ), bound
    assert re.fullmatch(
        re.escape("umask 077 && ") + r"unshare -Urm --propagation private -- sh -c '.*' "
        r"ew-exec-view '' python -E -P /tmp/ew-exec-[0-9a-f]{32}\.py",
        unbound,
        re.DOTALL,
    ), unbound


@pytest.mark.asyncio
async def test_post_create_creates_the_exec_view_dir_as_root_before_chown() -> None:
    """镜像里没有 /workspace(池 pod 实测),bind 目标由 post-create 以 root 建。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    client = make_client(sdk, store, workspace_pv_name="pv-nas")
    await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())

    root_cmds = [cmd for cmd, _, user, _ in sdk.sandbox.commands.calls if user == "root"]
    assert root_cmds[:2] == ["mkdir -p /workspace", "chown 10000:10000 /mnt/workspace"]


@pytest.mark.asyncio
async def test_post_create_mkdir_failure_discards_the_sandbox() -> None:
    """与 chown 相反,这句不是 best-effort:没有它每次 exec 都在第一条 bind 上 fail-closed。"""
    sdk, store = FakeSdk(), FakeInstanceStore()
    sdk.sandbox.commands.run_error = RuntimeError("mkdir: read-only file system")
    client = make_client(sdk, store)

    with pytest.raises(SandboxSupervisorError, match="post-create setup failed"):
        await client.acquire(tenant_id=uuid4(), thread_id="t", user_id=uuid4())
```

(第三条的「沙箱被拆」断言照 `grep -n "post-create setup failed" services/orchestrator/tests/test_agent_sandbox.py` 找到的既有 Important-6 用例,复用它对 `sdk` kill 记录的同一断言写法。)

- 再加一条:

```python
def test_client_lays_out_agent_ns_by_default() -> None:
    assert make_client(FakeSdk(), FakeInstanceStore()).layout == SANDBOX_LAYOUT_AGENT_NS
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_agent_sandbox.py -q`
Expected: 上述用例 FAIL;`test_exec_runs_in_workspace_cwd` FAIL(`cwd == "/workspace"`)。

- [ ] **Step 3: 实现**

import:`WORKSPACE_ROOT` → `EXEC_VIEW, NAS_MOUNT`;删 `from orchestrator.tools.workspace_paths import agent_workspace_root`;加 `from orchestrator.tools.exec_view import build_exec_command`、`from expert_work.persistence.sandbox_instance_store import SANDBOX_LAYOUT_AGENT_NS`(替换 T3 的 `SANDBOX_LAYOUT_USER_ROOT` import);字段默认 `layout: str = SANDBOX_LAYOUT_AGENT_NS`,注释改为「本进程铺的就是 agent-ns」。

`_create`:`"mountPath": NAS_MOUNT`。`_chown_workspace_mount`:`{WORKSPACE_ROOT}` → `{NAS_MOUNT}`,docstring 里的 `/workspace` 改 `/mnt/workspace`。

新方法(放 `_chown_workspace_mount` 之前):

```python
    async def _ensure_exec_view_dir(self, sbx: Any, *, sandbox_id: UUID) -> None:
        """B-60 —— 以 root 建 ``EXEC_VIEW`` 空目录,当每次 exec 的 bind 目标。

        镜像里**没有** ``/workspace``(W2 Task 9 删的,且 B-59 刷镜像时也不许加回:
        ACS 老代码 + 预建目录 = NAS symlink 建不上);rootfs 是可写 overlay(2026-09-14
        池 pod 实测),建沙箱后补一个即可。**不是 best-effort**:没有它每一次 exec 都会在
        第一条 bind 上 fail-closed,整个沙箱等于废的 —— 与 :meth:`_chown_workspace_mount`
        的取舍相反(那句失败无害),这句失败必须让 post-create 失败、把沙箱拆掉。
        """
        del sandbox_id  # 只为日志/对称;失败由调用方统一包成 post-create 失败
        await sbx.commands.run(f"mkdir -p {shlex.quote(EXEC_VIEW)}", user="root")
```

post-create try 块里 `if just_created:` 改为:

```python
            if just_created:
                await self._ensure_exec_view_dir(sbx, sandbox_id=sandbox_id)
                await self._chown_workspace_mount(sbx, sandbox_id=sandbox_id)
```

`exec`:删掉 `agent_cwd = ...` / `enter = (...)` 两段与它们的注释,`commands.run` 改为:

```python
            # B-60 —— 命令串由 exec_view.build_exec_command 生成:umask 077,再在自己的
            # user+mount ns 里把 agent 目录 bind 成 /workspace(spec §4.3)。cwd= 传挂载点
            # 只是因为 E2B 执行前校验它存在;进 ns 之后脚本自己 cd /workspace。
            result = await sbx.commands.run(
                build_exec_command(agent_key, script),
                user=SANDBOX_EXEC_USER,
                timeout=effective,
                cwd=NAS_MOUNT,
                envs=envs or None,
            )
```

exec 的 docstring 里「`mkdir -p` + `cd`」那几段删掉,换一句指向 `exec_view` 模块 docstring。

- [ ] **Step 4: 跑**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_agent_sandbox.py -q`
Expected: 全绿。

- [ ] **Step 5: 自证**:把 `_ensure_exec_view_dir` 的调用注释掉 → `test_post_create_creates_the_exec_view_dir_as_root_before_chown` 红 → 还原 → 绿。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator
git commit -m "feat(sandbox): ACS 后端 NAS 挂 /mnt/workspace、post-create 建 /workspace、exec 走命名空间命令串,layout=agent-ns(B-60 T7)"
```

---

### Task 8: seccomp 两条规则 + 本地后端必配 profile + compose + 验收 fixture

**Files:**
- Modify: `infra/sandbox-image/seccomp-profile.json`(`syscalls` 数组末尾)
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/seccomp.py:22-31`
- Modify: `infra/docker-compose.yml`(`sandbox-supervisor` 服务的 `environment:` 与 `volumes:`;:574-581 注释)
- Modify: `services/sandbox-supervisor/tests/test_supervisor_integration.py:262-277, 716-719`
- Test: `services/sandbox-supervisor/tests/test_seccomp.py`

**Interfaces:**
- Produces: profile 无 cap 放行 `unshare`(mask `4026400767`)、`mount`、`open_tree`、`move_mount`、`fsopen`、`fsconfig`、`fsmount`、`fspick`、`mount_setattr`;`validate_seccomp_profile(None)` → `SeccompProfileError`。

- [ ] **Step 1: 写失败的测试**(`test_seccomp.py`)

`test_none_is_noop` 换成:

```python
def test_none_fails_closed_since_b60() -> None:
    # B-60:每次 exec 都要在自己的 user ns 里 mount;宿主 Docker 默认 profile 把 unshare /
    # mount 都关在 CAP_SYS_ADMIN 后面(本机实测 EPERM),不配钉住的 profile 沙箱等于废的。
    with pytest.raises(SeccompProfileError, match="B-60"):
        validate_seccomp_profile(None)
```

:106 的参数化改 `["bpf", "perf_event_open", "setns"]`;末尾追加:

```python
def _ungated_rules_for(profile: dict, name: str) -> list[dict]:
    return [
        grp
        for grp in profile["syscalls"]
        if name in grp["names"]
        and grp["action"] == "SCMP_ACT_ALLOW"
        and not grp.get("includes", {}).get("caps")
    ]


def test_unshare_is_allowed_only_for_user_and_mount_namespaces() -> None:
    # B-60 —— 只放 CLONE_NEWUSER|CLONE_NEWNS(mask 掉这两位后其余必须为 0);pid/net/ipc/
    # uts/cgroup namespace 仍然拒。
    rules = _ungated_rules_for(_load_pinned(), "unshare")
    assert len(rules) == 1
    assert rules[0]["names"] == ["unshare"]
    assert rules[0]["args"] == [
        {"index": 0, "value": 4026400767, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}
    ]
    assert 4026400767 == 0xFFFFFFFF & ~(0x00020000 | 0x10000000)  # ~(NEWNS | NEWUSER)


def test_mount_and_the_new_mount_api_are_allowed_without_caps() -> None:
    # util-linux 2.41 走 open_tree/move_mount/fsopen…;默认 ERRNO 回 EPERM 不是 ENOSYS,
    # libmount 不回落经典 mount(2)(本机实测 "permission denied")。
    allow = _unconditional_allow(_load_pinned())
    for name in ("mount", "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr"):
        assert name in allow, name


def test_umount_setns_pivot_root_stay_cap_gated() -> None:
    allow = _unconditional_allow(_load_pinned())
    for name in ("umount2", "setns", "pivot_root"):
        assert name not in allow, name
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest services/sandbox-supervisor/tests/test_seccomp.py -q`
Expected: 四条 FAIL。

- [ ] **Step 3: profile** —— `syscalls` 数组末尾追加两个对象(保持文件其余字节不动):

```json
    {
      "names": ["unshare"],
      "action": "SCMP_ACT_ALLOW",
      "args": [
        { "index": 0, "value": 4026400767, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ" }
      ]
    },
    {
      "names": [
        "mount", "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr"
      ],
      "action": "SCMP_ACT_ALLOW"
    }
```

- [ ] **Step 4: `seccomp.py`**

```python
def validate_seccomp_profile(path: str | None) -> None:
    """Validate the pinned seccomp profile, fail-closed.

    ``None`` is refused since B-60: every exec bind-mounts its own view inside an
    unprivileged user namespace, and the host Docker default profile keeps
    ``unshare``/``mount`` behind CAP_SYS_ADMIN (which the sandbox drops) — so a
    supervisor without the pinned profile launches sandboxes whose every exec
    fails closed on its first mount. A non-``None`` path must point at an
    existing, readable file whose contents parse as JSON with a ``defaultAction``.
    """
    if path is None:
        msg = (
            "EXPERT_WORK_SANDBOX_SECCOMP_PROFILE_PATH is required (B-60): the host Docker "
            "default seccomp profile denies unshare/mount without CAP_SYS_ADMIN, so every "
            "exec would fail closed on its first bind mount. Point it at "
            "infra/sandbox-image/seccomp-profile.json bind-mounted at the same host path."
        )
        raise SeccompProfileError(msg)
```

(其余不动。)

- [ ] **Step 5: compose** —— `sandbox-supervisor` 服务:`environment:` 加

```yaml
      # B-60 —— 钉住的 profile 是本地后端的硬前提(见 seccomp.py):从仓库根目录
      # `docker compose -f infra/docker-compose.yml …` 起,宿主与容器内同一路径。
      EXPERT_WORK_SANDBOX_SECCOMP_PROFILE_PATH: ${PWD}/infra/sandbox-image/seccomp-profile.json
```

`volumes:` 加 `- ${PWD}/infra/sandbox-image/seccomp-profile.json:${PWD}/infra/sandbox-image/seccomp-profile.json:ro`;:574-581 那段「Unset here: dev rides the host default」注释改成「B-60 起必配;dev 也走仓内 profile」。

- [ ] **Step 6: 验收 fixture** —— `test_supervisor_integration.py` 顶部加

```python
_SECCOMP_PROFILE = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "seccomp-profile.json"
```

两处 `SandboxRuntimeProvider(oci_runtime=_OCI_RUNTIME, egress_network=_NETWORK, ...)` 都加 `seccomp_profile_path=str(_SECCOMP_PROFILE),`。

- [ ] **Step 7: 跑**

Run: `uv run --no-sync pytest services/sandbox-supervisor/tests/test_seccomp.py services/sandbox-supervisor/tests/test_supervisor_unit.py -q`
Expected: 全绿。若 `test_supervisor_unit.py` 里 `create_app(SandboxSupervisorSettings(), supervisor=h.supervisor, ...)`(:856/:1145)因注入分支也跑到 `validate_seccomp_profile` 而红,给那两处 `SandboxSupervisorSettings(seccomp_profile_path=str(_PINNED_PROFILE))`(常量同 `test_seccomp.py` 的 `_PINNED_PROFILE`)。

- [ ] **Step 8: 本地 docker 复跑 spike**(不是测试,是把 profile 改动在真 docker 上过一遍;需要本机镜像 `expert-work-sandbox:dev`)

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
docker run --rm --entrypoint sh --read-only --user 10000:10000 --cap-drop ALL \
  --security-opt no-new-privileges --security-opt "seccomp=$PWD/infra/sandbox-image/seccomp-profile.json" \
  --tmpfs /tmp:rw,size=64m,mode=1777 --tmpfs /mnt/workspace:rw,size=64m,uid=10000,gid=10000 \
  --tmpfs /workspace:ro,size=4k --workdir /mnt/workspace expert-work-sandbox:dev -c \
  'mkdir -p /mnt/workspace/agents/a1 && unshare -Urm --propagation private -- sh -c "set -e; mount --bind /mnt/workspace/agents/a1 /workspace && mount -t tmpfs -o size=1k none /mnt/workspace && cd /workspace && touch ok && ls /workspace && ls -A /mnt/workspace | wc -l"'
```

Expected: 输出 `ok` 与 `0`。

- [ ] **Step 9: Commit**

```bash
git add infra/sandbox-image/seccomp-profile.json services/sandbox-supervisor infra/docker-compose.yml
git commit -m "feat(sandbox): seccomp 放行 userns 内 mount;本地后端必配钉住的 profile(B-60 T8)"
```

---

### Task 9: 本地 `docker run` argv —— NAS 挂 `/mnt/workspace`、`/workspace` 只读 tmpfs、apparmor

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/sandbox/runtime_provider.py`(常量 :44-50 之后;argv :235-236、:262-265;`_workspace_mount` :302-328)
- Test: `packages/expert-work-runtime/tests/test_sandbox_runtime_provider.py`;`services/sandbox-supervisor/tests/test_supervisor_unit.py:645-660`

**Interfaces:**
- Produces: `SANDBOX_NAS_MOUNT = "/mnt/workspace"`、`SANDBOX_EXEC_VIEW = "/workspace"`;argv 含 `--workdir /mnt/workspace`、`--tmpfs /workspace:ro,size=4k`、`--security-opt apparmor=unconfined`;卷/tmpfs 挂 `/mnt/workspace`。

- [ ] **Step 1: 改既有断言 + 新增**

`test_sandbox_runtime_provider.py`:
- 所有 `"/workspace:rw,size=…,mode=1777"` → `"/mnt/workspace:rw,size=…,mode=1777"`(:45、:137、:152);
- `_flag_value(argv, "--workdir") == "/workspace"` → `"/mnt/workspace"`(:66、:95);
- :164 `"expert-work-ws-abc:/workspace"` → `"expert-work-ws-abc:/mnt/workspace"`;
- :166-171 的 tmpfs 列表首位插入 `"/workspace:ro,size=4k"`;
- 新增:

```python
def test_argv_mounts_a_tiny_read_only_tmpfs_as_the_exec_view_target() -> None:
    # B-60 — the image has no /workspace; docker creates the mount point for a
    # tmpfs even on a read-only rootfs. Read-only: an exec that somehow skipped
    # its namespace hits EROFS instead of silently writing into the container.
    for vol in (None, "expert-work-ws-abc"):
        argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1", workspace_volume=vol)
        tmpfs = [argv[i + 1] for i, t in enumerate(argv) if t == "--tmpfs"]
        assert "/workspace:ro,size=4k" in tmpfs


def test_argv_disables_apparmor_confinement_for_userns_mounts() -> None:
    # B-60 — docker-default AppArmor carries ``deny mount,``; cap-drop ALL + our
    # seccomp profile stay, mount is only reachable inside the exec's own userns.
    argv = _runc_provider().docker_run_argv(image="img", container_name="sb-1")
    opts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--security-opt"]
    assert "apparmor=unconfined" in opts
    assert "no-new-privileges" in opts
```

`test_supervisor_unit.py` :651 `f"{workspace_volume_name(tenant, user)}:/workspace"` → `:/mnt/workspace`;:654-659 tmpfs 列表首位插入 `"/workspace:ro,size=4k"`;:669 注释里 `--workdir /workspace` → `/mnt/workspace`。

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest packages/expert-work-runtime/tests/test_sandbox_runtime_provider.py services/sandbox-supervisor/tests/test_supervisor_unit.py -q -k "workspace or workdir or tmpfs or apparmor or hardening or volume"`
Expected: 多条 FAIL。

- [ ] **Step 3: 实现**

常量(`SANDBOX_AGENT_HOME` 之后):

```python
#: B-60 —— 与 orchestrator ``sandbox_image_contract.NAS_MOUNT`` / ``EXEC_VIEW`` 同值。这个
#: 包不能 import orchestrator,契约文件末尾的 ``test_workspace_roots_match_across_packages``
#: 钉住相等。NAS 卷 / 临时 tmpfs 挂在 ``SANDBOX_NAS_MOUNT``;``SANDBOX_EXEC_VIEW`` 只是每次
#: exec 在自己的命名空间里 bind 的目标,容器层面给它一个只读的空 tmpfs 当挂载点。
SANDBOX_NAS_MOUNT = "/mnt/workspace"
SANDBOX_EXEC_VIEW = "/workspace"
```

argv:`"--workdir", "/workspace",` → `"--workdir", SANDBOX_NAS_MOUNT,`;`*self._workspace_mount(limits, workspace_volume),` 之后插入:

```python
            # B-60 — the bind target for the per-exec view (spec §4.3). A tiny
            # read-only tmpfs: docker creates the mount point on the read-only
            # rootfs (the image deliberately has no /workspace), and read-only
            # means an exec that somehow skipped the namespace fails loudly
            # (EROFS) instead of silently writing into the container.
            "--tmpfs",
            f"{SANDBOX_EXEC_VIEW}:ro,size=4k",
```

`"--security-opt", "no-new-privileges",` 之后插入:

```python
            # B-60 — docker-default AppArmor carries ``deny mount,``; the per-exec
            # view needs mount(2) inside the unprivileged user namespace each exec
            # creates for itself. --cap-drop ALL and the pinned seccomp profile
            # stay; on Ubuntu 24.04 hosts the CI workflows also lift
            # kernel.apparmor_restrict_unprivileged_userns (see ci.yml).
            "--security-opt",
            "apparmor=unconfined",
```

`_workspace_mount`:tmpfs 分支 `f"{SANDBOX_NAS_MOUNT}:rw,size={limits.workspace_size_mb}m,mode=1777"`;卷分支 `return ["--volume", f"{workspace_volume}:{SANDBOX_NAS_MOUNT}"]`;docstring 与注释里的 `/workspace` 改 `/mnt/workspace`。

- [ ] **Step 4: 跑**

Run: `uv run --no-sync pytest packages/expert-work-runtime/tests/test_sandbox_runtime_provider.py services/sandbox-supervisor/tests/test_supervisor_unit.py -q`
Expected: 全绿。

- [ ] **Step 5: 自证**:删掉 `--tmpfs /workspace:ro,size=4k` 那两行 → 新用例红 → 还原 → 绿。

- [ ] **Step 6: Commit**

```bash
git add packages/expert-work-runtime services/sandbox-supervisor/tests/test_supervisor_unit.py
git commit -m "feat(runtime): 本地沙箱 NAS 挂 /mnt/workspace、/workspace 只读 tmpfs 当 bind 目标、apparmor=unconfined(B-60 T9)"
```

---

### Task 10: `runner.py` 进命名空间 + supervisor 线协议 `cwd` → `agent_root`

**Files:**
- Modify: `infra/sandbox-image/runner.py`(模块 docstring :7-14;常量区 :35-44 之后;`run_once` :52-122;`handle_request` :140-142)
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/schemas.py:97-106`
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/runner_link.py:53-70, 105-112`
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/supervisor.py:393, 411-414, 424`
- Modify: `services/sandbox-supervisor/src/sandbox_supervisor/app.py:343`
- Test: `services/sandbox-supervisor/tests/test_supervisor_unit.py:89-107, 1300-1342`

**Interfaces:**
- Produces: 线协议字段 `agent_root: str | None`(runner 请求 JSON 与 `ExecRequest` 同名);`run_once(code, timeout_s, envs=None, agent_root=None)`;runner 子进程 argv = `["unshare","-Urm","--propagation","private","--","sh","-c",_EXEC_VIEW_SCRIPT,"ew-exec-view", agent_root or "", sys.executable,"-E","-P","-c", code]`。

- [ ] **Step 1: supervisor 单测改名 + 值**

`FakeRunnerLink`:`exec_cwd_calls` → `exec_agent_root_calls`,`exec(..., agent_root: str | None = None)`;:1302-1342 三条测试:名字里 `cwd` → `agent_root`,调用 `h.supervisor.exec(..., agent_root="/mnt/workspace/agents/plan-aaaaaaaa")`,断言列表相应改;第二条断言 `link.exec_agent_root_calls == [None]`。

- [ ] **Step 2: 跑,确认红**

Run: `uv run --no-sync pytest services/sandbox-supervisor/tests/test_supervisor_unit.py -q -k agent_root`
Expected: FAIL `TypeError: unexpected keyword argument 'agent_root'`

- [ ] **Step 3: supervisor 三处 + schema + app**

`schemas.py` :97-106 换成:

```python
    #: B-60 —— the agent's directory on the NAS mount (``/mnt/workspace/agents/<key>``),
    #: bind-mounted as ``/workspace`` inside the private mount namespace the runner
    #: creates for this one exec. Per-call for the same reason as ``envs``: one warm
    #: session serves every agent of a ``(tenant, user)``. ``None`` → unbound: the
    #: whole user root becomes ``/workspace`` (pre-feature view). Not a cwd — the
    #: child's cwd is always ``/workspace``.
    agent_root: str | None = None
```

`runner_link.py`:Protocol 与实现的 `cwd` 形参改 `agent_root`,docstring 同上口径;`if agent_root: request["agent_root"] = agent_root`。`supervisor.py:393` 形参改 `agent_root`,:411-414 docstring 改,:424 `agent_root=agent_root`。`app.py:343` `agent_root=body.agent_root`。

- [ ] **Step 4: runner.py**

模块 docstring 示例改成 `"agent_root": "/mnt/workspace/agents/a1"`,说明「runner 在每次 exec 自己的 mount namespace 里把它 bind 成 /workspace」。常量区加(`MAX_OUTPUT_CHARS` 之后):

```python
#: B-60 —— 每次 exec 自己的 user+mount namespace 里把 agent 目录 bind 成 /workspace、
#: shared/ 只读挂入、tmpfs 盖掉 /mnt/workspace。**与 orchestrator
#: ``exec_view.EXEC_VIEW_SCRIPT`` 逐字相同**(这个文件是镜像代码,不能 import 仓库;
#: 契约测试 ``test_exec_view_script_matches_the_sandbox_image`` 用 ast 比对两份字面量,
#: 改一边必红)。参数走位置参数:``$1`` = bind 源(空串 = 未绑,bind 整个用户根)。
_EXEC_VIEW_SCRIPT = """\
set -eu
root="$1"; shift
if [ -n "$root" ]; then
  mkdir -p "$root"
  mount --bind "$root" /workspace
  if [ -d /mnt/workspace/shared ]; then
    mkdir -p /workspace/shared
    mount --bind /mnt/workspace/shared /workspace/shared
    mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared
  fi
  mount -t tmpfs -o size=1k none /mnt/workspace
else
  mount --bind /mnt/workspace /workspace
fi
cd /workspace
exec "$@"
"""
```

`run_once`:

```python
def run_once(
    code: str,
    timeout_s: int,
    envs: dict[str, str] | None = None,
    agent_root: str | None = None,
) -> Response:
    """Run ``code`` in a child Python process; capture stdout / stderr / exit.

    ``timeout_s`` is clamped to ``[1, MAX_TIMEOUT_S]``. On timeout the child is
    killed and ``timed_out`` is ``True`` with ``exit_code`` -1.

    ``envs`` (sandbox migration wave 2, spec 决策 10) is merged onto this runner
    process's own environment for the child only.

    ``agent_root`` (B-60) is the agent's directory on the NAS mount. The child
    runs inside its own user + mount namespace (``unshare -Urm``) where that
    directory is bind-mounted as ``/workspace``, ``shared/`` is bound read-only
    and the NAS mount itself is covered by an empty tmpfs — so absolute
    ``/workspace/x`` lands in the agent's directory and other agents' directories
    are simply not there. ``None`` → the whole user root is ``/workspace``.
    The directory is created inside the namespace (``mkdir -p``, umask 0o077
    inherited from this process). Any mount failure exits non-zero before the
    code runs: fail closed, never fall back to the shared root.
    """
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))
    child_env = {**os.environ, **envs} if envs else None
    argv = [
        "unshare",
        "-Urm",
        "--propagation",
        "private",
        "--",
        "sh",
        "-c",
        _EXEC_VIEW_SCRIPT,
        "ew-exec-view",
        agent_root or "",
        # -E -P, deliberately NOT -I(原注释保留)
        sys.executable,
        "-E",
        "-P",
        "-c",
        code,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - arbitrary code execution is the tool
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=child_env,
        )
```

(`except` / `return` 段不动。)`handle_request`::140-142 换成

```python
    raw_root = request.get("agent_root")
    agent_root = raw_root if isinstance(raw_root, str) and raw_root else None
    return run_once(code, timeout_s, envs, agent_root)
```

- [ ] **Step 5: 跑**

Run: `uv run --no-sync pytest services/sandbox-supervisor/tests/test_supervisor_unit.py -q && python3 -c "import ast,sys; ast.parse(open('infra/sandbox-image/runner.py').read()); print('runner parses')"`
Expected: 全绿;`runner parses`。`rg -n "\bcwd\b" services/sandbox-supervisor/src infra/sandbox-image/runner.py` 只剩与 B-60 无关的注释或为空。

- [ ] **Step 6: Commit**

```bash
git add infra/sandbox-image/runner.py services/sandbox-supervisor
git commit -m "feat(sandbox-image): runner 在每次 exec 自己的命名空间里 bind agent 目录;线协议 cwd → agent_root(B-60 T10)"
```

---

### Task 11: 契约用例 + 三道漂移闸 + 验收用例 + CI sysctl

**Files:**
- Modify: `services/orchestrator/tests/test_sandbox_runtime_contract.py`(docstring「其四」:76-86;新用例放 `test_exec_relative_write_lands_in_workspace` 之后;闸放 `_runner_py_exec_flags` :1010 附近与 `test_exec_contract_constants_match_the_sandbox_image` 之后)
- Modify: `services/sandbox-supervisor/tests/test_supervisor_integration.py`(:453-470 泄漏用例之后)
- Modify: `.github/workflows/ci.yml`(「Run pytest (integration)」之前)、`.github/workflows/sandbox-gvisor.yml`(「Run sandbox acceptance suite under gVisor」之前)

**Interfaces:**
- Consumes: `EXEC_VIEW_SCRIPT` / `EXEC_VIEW_ARGV_PREFIX`(T5)、`NAS_MOUNT` / `EXEC_VIEW`(T4)、`SANDBOX_NAS_MOUNT` / `SANDBOX_EXEC_VIEW`(T9)、runner 的 `_EXEC_VIEW_SCRIPT`(T10)、`runtime.exec(..., agent_key=)`。

- [ ] **Step 1: 漂移闸(无 integration marker)**

`_runner_py_exec_flags` 改成在 argv 里**找** `sys.executable`(不再要求它是列表头):

```python
def _runner_py_exec_argv() -> list[str | None]:
    """ast 抠 runner.py 的子进程 argv:字符串常量原样,``_EXEC_VIEW_SCRIPT`` 名字替换成
    它的字面量,``sys.executable`` 记成 ``"<sys.executable>"``,其它表达式记 ``None``。"""
    runner = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "runner.py"
    tree = ast.parse(runner.read_text(encoding="utf-8"))
    script = _runner_py_string_constant("_EXEC_VIEW_SCRIPT")
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not any(
            isinstance(e, ast.Attribute) and e.attr == "executable" for e in node.elts
        ):
            continue
        out: list[str | None] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            elif isinstance(elt, ast.Name) and elt.id == "_EXEC_VIEW_SCRIPT":
                out.append(script)
            elif isinstance(elt, ast.Attribute) and elt.attr == "executable":
                out.append("<sys.executable>")
            else:
                out.append(None)
        return out
    raise AssertionError("runner.py 的子进程 argv(含 sys.executable 的列表)没找到")


def _runner_py_string_constant(name: str) -> str:
    runner = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "runner.py"
    for node in ast.parse(runner.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            return node.value.value
    raise AssertionError(f"runner.py 没有模块级字符串常量 {name}")


def _runner_py_exec_flags() -> list[str]:
    argv = _runner_py_exec_argv()
    start = argv.index("<sys.executable>") + 1
    return list(argv[start : argv.index("-c", start)])  # type: ignore[arg-type]
```

新闸(`test_exec_contract_constants_match_the_sandbox_image` 之后):

```python
def test_exec_view_script_matches_the_sandbox_image() -> None:
    """B-60 —— 两个后端同一段 sh:orchestrator ``exec_view.EXEC_VIEW_SCRIPT`` 与镜像
    ``runner.py`` 的 ``_EXEC_VIEW_SCRIPT`` 逐字相等;argv 前缀也相等,前缀之后紧跟
    ``agent_root``(非常量)再 ``sys.executable``。"""
    from orchestrator.tools.exec_view import EXEC_VIEW_ARGV_PREFIX, EXEC_VIEW_SCRIPT

    assert _runner_py_string_constant("_EXEC_VIEW_SCRIPT") == EXEC_VIEW_SCRIPT
    argv = _runner_py_exec_argv()
    n = len(EXEC_VIEW_ARGV_PREFIX)
    assert argv[:n] == list(EXEC_VIEW_ARGV_PREFIX), argv[:n]
    assert argv[n] is None, "前缀之后必须是 agent_root(表达式),不是常量"
    assert argv[n + 1] == "<sys.executable>"


def test_workspace_roots_match_across_packages() -> None:
    """B-60 —— runtime 包(本地 docker argv)与 orchestrator(命令串、工具)对挂载点与
    视图根各有一份字面量;这里钉它们相等,且脚本体确实用的是这两个路径。"""
    from expert_work.runtime.sandbox.runtime_provider import SANDBOX_EXEC_VIEW, SANDBOX_NAS_MOUNT
    from orchestrator.tools.exec_view import EXEC_VIEW_SCRIPT
    from orchestrator.tools.sandbox_image_contract import EXEC_VIEW, NAS_MOUNT

    assert (SANDBOX_NAS_MOUNT, SANDBOX_EXEC_VIEW) == (NAS_MOUNT, EXEC_VIEW)
    assert f'mount --bind "$root" {EXEC_VIEW}\n' in EXEC_VIEW_SCRIPT
    assert f"mount -t tmpfs -o size=1k none {NAS_MOUNT}\n" in EXEC_VIEW_SCRIPT
```

- [ ] **Step 2: 跑闸,确认绿**(T5/T10 已就位;若红,说明两份字面量不同,**改 runner 或 exec_view 使之相等**,不改闸)

Run: `uv run --no-sync pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -q -m "not integration"`

- [ ] **Step 3: 自证闸**:runner.py 脚本里删一个空格 → `test_exec_view_script_matches_the_sandbox_image` 红 → 还原 → 绿。

- [ ] **Step 4: 契约用例(两后端参数化)**

```python
_KEY_A, _KEY_B = "plan-aaaaaaaa", "sop-bbbbbbbb"


async def _cleanup(runtime: SandboxRuntime, sid: UUID, *paths: str) -> None:
    """探针残留写在共享测试集群 NAS 上;经未绑 exec 删(CI runner 无 NFS 路由)。"""
    code = "import os, shutil\n" + "".join(
        f"shutil.rmtree({p!r}, ignore_errors=True) if os.path.isdir({p!r}) else "
        f"(os.remove({p!r}) if os.path.exists({p!r}) else None)\n"
        for p in paths
    )
    await runtime.exec(sandbox_id=sid, code=code, timeout_s=30)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_view_is_the_agents_own_directory(runtime: SandboxRuntime) -> None:
    """B-60 spec 目标 1:绑了 agent 的 exec 里 /workspace **就是** agents/<key>。
    比 (st_dev, st_ino):绑定 exec 里 stat('/workspace') 与未绑 exec 里
    stat('/workspace/agents/<key>') 必须是同一个 inode,且写进去的文件从另一边读得到。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c-view", user_id=uuid4())
    try:
        bound = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import os\n"
                "st = os.stat('/workspace')\n"
                "open('/workspace/probe.txt', 'w').write('VIEW_OK')\n"
                "print(st.st_dev, st.st_ino, os.getcwd())"
            ),
            timeout_s=30,
            agent_key=_KEY_A,
        )
        assert bound.exit_code == 0, bound.stderr
        dev, ino, cwd = bound.stdout.split()
        assert cwd == "/workspace"
        unbound = await runtime.exec(
            sandbox_id=sid,
            code=(
                f"import os\n"
                f"st = os.stat('/workspace/agents/{_KEY_A}')\n"
                f"print(st.st_dev, st.st_ino, open('/workspace/agents/{_KEY_A}/probe.txt').read())"
            ),
            timeout_s=30,
        )
        assert unbound.stdout.split() == [dev, ino, "VIEW_OK"], unbound.stdout
    finally:
        await _cleanup(runtime, sid, f"/workspace/agents/{_KEY_A}")
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_view_hides_the_user_root_and_other_agents(runtime: SandboxRuntime) -> None:
    """目标 2:视图里没有别的 agent,挂载点被空 tmpfs 盖住。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c-hide", user_id=uuid4())
    try:
        await runtime.exec(
            sandbox_id=sid,
            code=f"import os; os.makedirs('/workspace/agents/{_KEY_B}', exist_ok=True); "
            f"open('/workspace/agents/{_KEY_B}/secret.txt', 'w').write('theirs'); "
            "open('/workspace/root-level.txt', 'w').write('root')",
            timeout_s=30,
        )
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import os\n"
                "print(sorted(os.listdir('/workspace')))\n"
                "print(os.listdir('/mnt/workspace'))\n"
                f"print(os.path.exists('/workspace/agents/{_KEY_B}/secret.txt'))"
            ),
            timeout_s=30,
            agent_key=_KEY_A,
        )
        assert outcome.exit_code == 0, outcome.stderr
        lines = outcome.stdout.splitlines()
        assert "agents" not in lines[0] and "root-level.txt" not in lines[0], lines[0]
        assert lines[1] == "[]", "挂载点必须被空 tmpfs 盖住"
        assert lines[2] == "False"
    finally:
        await _cleanup(runtime, sid, f"/workspace/agents/{_KEY_A}", f"/workspace/agents/{_KEY_B}", "/workspace/root-level.txt")
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_view_shared_is_read_only(runtime: SandboxRuntime) -> None:
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c-shared", user_id=uuid4())
    try:
        await runtime.exec(
            sandbox_id=sid,
            code="import os; os.makedirs('/workspace/shared', exist_ok=True); "
            "open('/workspace/shared/legacy.md', 'w').write('LEGACY')",
            timeout_s=30,
        )
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import errno\n"
                "print(open('/workspace/shared/legacy.md').read())\n"
                "try:\n"
                "    open('/workspace/shared/x', 'w')\n"
                "    print('WRITABLE')\n"
                "except OSError as e:\n"
                "    print('EROFS' if e.errno == errno.EROFS else e.errno)\n"
            ),
            timeout_s=30,
            agent_key=_KEY_A,
        )
        assert outcome.stdout.split() == ["LEGACY", "EROFS"], outcome.stdout
    finally:
        await _cleanup(runtime, sid, "/workspace/shared", f"/workspace/agents/{_KEY_A}")
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_agents_exec_concurrently_and_see_only_themselves(runtime: SandboxRuntime) -> None:
    """目标 3:同一沙箱、两个 agent、真并发(asyncio.gather)。命名空间按进程树,互不串。"""
    import asyncio

    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c-conc", user_id=uuid4())
    try:

        def probe(tag: str) -> str:
            return (
                "import os, time\n"
                f"open('/workspace/{tag}.txt', 'w').write({tag!r})\n"
                "time.sleep(1.0)\n"
                "print(sorted(os.listdir('/workspace')))"
            )

        a, b = await asyncio.gather(
            runtime.exec(sandbox_id=sid, code=probe("A"), timeout_s=30, agent_key=_KEY_A),
            runtime.exec(sandbox_id=sid, code=probe("B"), timeout_s=30, agent_key=_KEY_B),
        )
        assert a.exit_code == 0 and b.exit_code == 0, (a.stderr, b.stderr)
        assert "A.txt" in a.stdout and "B.txt" not in a.stdout, a.stdout
        assert "B.txt" in b.stdout and "A.txt" not in b.stdout, b.stdout
    finally:
        await _cleanup(runtime, sid, f"/workspace/agents/{_KEY_A}", f"/workspace/agents/{_KEY_B}")
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unbound_exec_sees_the_whole_user_root(runtime: SandboxRuntime) -> None:
    """目标 4:未绑 agent 一字不改 —— 视图 = 整个用户根,agents/ 与 shared/ 都在。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c-unbound", user_id=uuid4())
    try:
        await runtime.exec(sandbox_id=sid, code="open('/workspace/mine.txt','w').write('x')", timeout_s=30, agent_key=_KEY_A)
        outcome = await runtime.exec(
            sandbox_id=sid,
            code=f"import os; print(os.path.exists('/workspace/agents/{_KEY_A}/mine.txt'), os.getcwd())",
            timeout_s=30,
        )
        assert outcome.stdout.split() == ["True", "/workspace"], outcome.stdout
    finally:
        await _cleanup(runtime, sid, f"/workspace/agents/{_KEY_A}")
        await runtime.destroy(sandbox_id=sid, reason="contract-test")
```

(supervisor 档的 `HTTPSupervisorRuntime.exec` 接口签名已带 `agent_key`;`_cleanup` 用未绑 exec。)
docstring「其四」段末尾加一句:「**B-60 之后**:云后端 exec 进了自己的命名空间,`/workspace` 是 bind 挂载点不是符号链接,`getcwd()` 报 `/workspace`;比 inode 的写法保留,它更强。」

- [ ] **Step 5: 验收用例**(`test_supervisor_integration.py`,泄漏用例之后;用该文件既有的 `expert_work` harness 与带 `user_id` 的 `AcquireRequest` 写法)

```python
@pytest.mark.asyncio
async def test_exec_agent_root_is_bound_as_workspace_and_hides_the_rest(expert_work: _Harness) -> None:
    """B-60 —— 真容器(runc / runsc 各跑一次):agent_root 被 bind 成 /workspace,
    /mnt/workspace 被盖,另一个 agent_root 看不到前者的文件,未绑看到全部。"""
    tenant, user = uuid4(), uuid4()
    response = await expert_work.supervisor.acquire(
        AcquireRequest(tenant_id=tenant, thread_id="b60", user_id=user)
    )
    sid = response.sandbox_id
    try:
        a = await expert_work.supervisor.exec(
            sid,
            code="import os; open('/workspace/a.txt','w').write('A'); print(os.getcwd(), os.listdir('/mnt/workspace'))",
            agent_root="/mnt/workspace/agents/a",
        )
        assert a.exit_code == 0, a.stderr
        assert a.stdout.strip() == "/workspace []"
        b = await expert_work.supervisor.exec(
            sid, code="import os; print(sorted(os.listdir('/workspace')))", agent_root="/mnt/workspace/agents/b"
        )
        assert "a.txt" not in b.stdout, b.stdout
        root = await expert_work.supervisor.exec(sid, code="print(open('/workspace/agents/a/a.txt').read())")
        assert root.stdout.strip() == "A"
    finally:
        await expert_work.supervisor.destroy(sid, reason="b60-acceptance")
```

- [ ] **Step 6: CI sysctl 步骤**(两处同文)

`ci.yml`「Run pytest (integration)」之前:

```yaml
      - name: Allow unprivileged user namespaces for the sandbox exec view (B-60)
        # Ubuntu 24.04 restricts userns creation to AppArmor profiles that grant
        # ``userns``; the sandbox runs ``apparmor=unconfined`` (docker-default
        # carries ``deny mount,``), so lift the restriction on this runner only.
        # Production is ACS (no AppArmor at all); this is a CI-host setting.
        run: |
          if sysctl -n kernel.apparmor_restrict_unprivileged_userns >/dev/null 2>&1; then
            sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
          fi
```

`sandbox-gvisor.yml`「Run sandbox acceptance suite under gVisor」之前同一段。

- [ ] **Step 7: 本地跑验收(runc)**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
EXPERT_WORK_TEST_SANDBOX_IMAGE=expert-work-sandbox:dev uv run --no-sync pytest services/sandbox-supervisor/tests/test_supervisor_integration.py -q -m integration -k "agent_root or persist or leak"
```

Expected: 全绿。契约档 supervisor 参数需要起 compose(见契约文件 docstring),本机可选;**CI 集成 job 是仲裁**。

- [ ] **Step 8: 推 PR 观察三个 job**:`Test (integration)`(runc,ubuntu-latest)、`Acceptance suite under runsc`、`Contract suite`。
  - runc 红在 `unshare`/`mount` EPERM → 看 sysctl 步骤是否执行、`docker info` 的 AppArmor 状态;按 spec §4.5 的分支处理,**不删用例**。
  - runsc 红 → **停下,向用户拍板**(spec §4.5 最后一条)。

- [ ] **Step 9: Commit**

```bash
git add services/orchestrator/tests/test_sandbox_runtime_contract.py services/sandbox-supervisor/tests/test_supervisor_integration.py .github/workflows/ci.yml .github/workflows/sandbox-gvisor.yml
git commit -m "test(sandbox): B-60 视图契约六条 + runner/runtime 漂移闸 + 验收用例;CI 放开 userns(B-60 T11)"
```

---

### Task 12: 删过渡别名 + Dockerfile 闸 + 口径 + 文档

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py`(删 `WORKSPACE_ROOT`;头注释 :19/:28/:32 提到它的句子改成 `NAS_MOUNT`)
- Modify: `services/orchestrator/src/orchestrator/tools/workspace_paths.py`(删 `USER_ROOT`、`agent_workspace_root`;模块 docstring 第 3-9 行改成视图口径)
- Modify: `services/orchestrator/tests/test_workspace_paths.py`(:78-97 两条 `agent_workspace_root` 用例改成 `agent_nas_root`)
- Modify: `services/orchestrator/tests/test_agent_sandbox.py:1527-1570`
- Modify: `services/orchestrator/src/orchestrator/tools/bash.py:11, 58` 与 `exec_python` 工具描述(`rg -n "isolat|隔离" services/orchestrator/src/orchestrator/tools/*.py`)
- Modify: `docs/superpowers/specs/2026-09-10-workspace-agent-scoping-design.md`(§三 补记之后再加一句)、`docs/superpowers/ROADMAP.md`(B-60 行)

- [ ] **Step 1: 删别名并证明没人用**

删 `WORKSPACE_ROOT`、`USER_ROOT`、`agent_workspace_root`(含 `_require_safe_key` 之外的过渡 docstring)。然后:

```bash
rg -n "WORKSPACE_ROOT\b|USER_ROOT\b|agent_workspace_root" services packages infra tools --type py -g '!**/.venv/**'
```

Expected: 只剩 `agent_sandbox.py:362` 那条注释里的 `WORKSPACE_ROOT`(改成 `NAS_MOUNT`)与 `retention_cleanup_job` 的 `EXPERT_WORK_RETENTION_WORKSPACE_ROOT`(无关,不动)。跑 `uv run --no-sync pytest services/orchestrator/tests/test_workspace_paths.py services/orchestrator/tests/test_file_ops.py services/orchestrator/tests/test_artifact_tools.py services/orchestrator/tests/test_agent_sandbox.py services/orchestrator/tests/test_read_document*.py -q` 全绿。

- [ ] **Step 2: Dockerfile 闸**(`test_image_starts_as_root_and_leaves_workspace_free`)

```python
def test_image_leaves_both_mount_points_bare() -> None:
    """平台在 mountPath(B-60 起是 /mnt/workspace)建 symlink,预建目录会挡住它;
    /workspace 也不预建 —— ACS 老代码 + 预建目录同样建不上,运行期 mkdir -p 是规则
    (``AgentSandboxClient._ensure_exec_view_dir``),不是过渡。"""
    text = _dockerfile_text()
    assert "\nUSER agent" not in text
    assert "WORKDIR /workspace" not in text and "WORKDIR /mnt/workspace" not in text
    assert "mkdir -p /workspace" not in text and "mkdir -p /mnt/workspace" not in text
    assert "HOME=/home/agent" in text
    assert "mkdir -p /opt/skills /opt/agents" in text
```

`test_image_env_matches_dockerfile` 的断言消息里 `WORKSPACE_ROOT` → `NAS_MOUNT`。

- [ ] **Step 3: 工具描述口径**(spec §五):`bash.py` 与 `exec_python` 的 LLM 可见描述若写「isolated」「隔离」,改成「Runs in /workspace, which is your agent's own directory; other agents' files are not visible there.」—— 不加「请写相对路径」的劝导。

- [ ] **Step 4: 文档**

B-50 spec §三 补记之后加:「§5.3 的「约定」自 B-60 起成为边界:`bash` / `exec_python` 的 `/workspace` 就是 agent 目录(每次 exec 一个私有 mount namespace)。」
ROADMAP B-60 行末尾加:「**PR-B #<n> / PR-C #<n> 已合**,待发测(T13)。」

- [ ] **Step 5: Commit + 开 PR-C**

```bash
git add -A services packages infra docs
git commit -m "chore(sandbox): 删 WORKSPACE_ROOT/USER_ROOT 过渡别名,Dockerfile 闸改双挂载点,工具描述按视图口径(B-60 T12)"
```

PR-C 正文:列 spec §零 九条、本地 docker 实证、CI 三个 job 的预期与红了怎么办。

---

### Task 13: 真栈验收(PR-B、PR-C 合并、`release.sh test` 之后)

**不是代码任务。** 逐条打勾,任一条不过就停。

- [ ] **Step 1: 发测试环境** —— `tools/deploy/release.sh test`(从工作树建,记录 PR 照惯例),smoke 全绿,金丝雀 `CANARY_OK`。
- [ ] **Step 2: 热会话换代** —— 测试集群 control-plane pod 内(DSN 走 `Settings().db_dsn`,**不 dump env**):

```sql
SELECT layout, destroy_reason, count(*)
  FROM sandbox_instance
 WHERE acquired_at > now() - interval '1 day' OR destroyed_at > now() - interval '1 day'
 GROUP BY 1, 2 ORDER BY 1, 2;
```

Expected: 存量 `user-root` 行带 `layout_mismatch`;新行全部 `agent-ns`;没有 `warm_reconnect_failed` 激增。
**滚动窗口有界抖动是预期的,不是信号**(spec §零 第 11 条 / §7):2 副本默认 RollingUpdate,
翻转 `layout` 默认值那段窗口(约 1-2 分钟)内新旧 pod 会互相把对方刚建的热会话判成
`layout_mismatch` 重建,可能连带打断另一侧在跑的 exec —— 拍板「不改部署形状」,接受为有界抖动;
`layout_mismatch` 计数在窗口内偏高、窗口过后(只剩一个版本)迅速回落属于正常,别当 bug 停下。

- [ ] **Step 3: 探针用户复现 §一**(user `pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation`,API key 走 stdin heredoc、不进 argv、不落文件):一个 run,让 agent `exec_python` 写 `/workspace/probe-b60.pptx`,再 `save_artifact`,再下载。
  Expected:下载 200 且字节数 > 0;`save_artifact` 结果里**没有**「moved into your agent workspace」;NAS 上 `agents/<probe-key>/probe-b60.pptx` 在、用户根上**没有**同名文件(control-plane pod `ls /mnt/workspaces/<tenant>/<user>`)。
- [ ] **Step 4: 未绑路径** —— 金丝雀(`release-canary`,user `canary:release`)整套 PASS 即算。
- [ ] **Step 5: 交测试人员** —— 他们的对接流程(对方 agent `ai-health-plan`)自己跑;**我不动那两个 agent**。
- [ ] **Step 6: 通过后** —— 执行单重定 B1/B2 钉子与日期;ROADMAP B-60 销案;班车 1 重排。

---

## 自审

1. **spec 覆盖**:§4.2 → T4/T6;§4.3 → T5/T10;§4.4 → T7;§4.5 → T8/T9/T10/T11;§4.6 → T1/T2/T3/T7;§4.7 → T12;§4.8 → T6;§六每一行 → T2/T3/T5/T6/T7/T8/T9/T10/T11/T13;§七 → PR 表 + T13。
2. **占位符**:无 TBD/TODO;每个代码步骤给了代码;T7 Step 1 第三条「沙箱被拆」断言指向既有用例写法(grep 关键字给出)。
3. **类型一致**:`claim_warm` 四元组 `(UUID, str, datetime | None, str)` 在 T2/T3 一致;`agent_nas_root` / `agent_view_alias`(T4)被 T5/T6 引用;`EXEC_VIEW_ARGV_PREFIX` 在 T5/T11 一致;线协议字段名 `agent_root` 在 T6(客户端)/T10(supervisor、runner)/T11(验收)一致;`build_artifact_locate_wrapper(rel, *, ws)` 在 T6 内一致。
