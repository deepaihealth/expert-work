# 用户工作区加 agent 维度 —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同一用户下的多个 agent 各有自己的工作区子树与产物命名空间,不再互相读错、写错、覆盖。

**Architecture:** 沙箱挂载点保持 `{root}/{tenant}/{user}` 不动(热沙箱按 user 复用,挂载点无法按 agent 分)。分层做在**路径**上:每个 agent 的默认根变成 `agents/<agent_key>/`,靠沙箱内片段既有的 `realpath` + `startswith` 守卫强制。`agent_key` 复用 `sanitize_agent_key()`(与 `/opt/skills/<agent_key>` 同一个值),经 `configurable` → `ToolContext` 送到工具层。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 + Alembic / LangGraph / pytest(`uv run --no-sync pytest`)

**Spec:** `docs/superpowers/specs/2026-09-10-workspace-agent-scoping-design.md`

## Global Constraints

- `agent_key` 的唯一定义是 `sanitize_agent_key(spec.metadata.name)`(`services/orchestrator/src/orchestrator/tools/skill_seed.py:159`),形状 `<cleaned-name>-<sha256(name)[:8]>`。**不要新造第二种算法。**
- 沙箱挂载点、`workspace_user_root()`、`agent_sandbox.py:388-401` 那条构造期硬闸 —— **一律不动**。
- `ToolContext` 是 `@dataclass(frozen=True)`,per-dispatch 改动走 `dataclasses.replace`。
- 四个文件工具(`read_file`/`write_file`/`list_dir`/`edit_file`)的 agent 边界是**强制**的;`bash`/`exec_python` 绕得过,是**约定**。文档与工具描述必须按这个区分写,不许笼统说「隔离」。
- 本地测试:`uv sync --frozen` 后 `uv run --no-sync pytest <具体文件>`。**绝不跑全仓 pytest,也不跑整个 `services/control-plane/tests`**(14 个 xdist worker 会 0% CPU 挂 26 分钟零输出)。CI 是仲裁。
- 每条新增断言必须 break→red→restore→green 自证,变异前先 commit,`git diff` 确认变异落地,还原一律手写回原文,**永不用 `git checkout` / `git stash`**。重言式变异要拒绝并报告。
- commit message 用中文 conventional commit,结尾加:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DNzoY8vc8jDMsib4wFGFpN
  ```

---

## 文件结构

| 文件 | 职责 | 谁改 |
|---|---|---|
| `services/orchestrator/src/orchestrator/tools/registry.py` | `ToolContext` 加 `agent_key` 字段 | Task 1 |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py` | `_build_tool_context` 读 `agent_key` | Task 1 |
| `services/control-plane/src/control_plane/run_queue_worker.py`<br>`services/control-plane/src/control_plane/api/runs.py`<br>`services/control-plane/src/control_plane/trigger_firing.py`<br>`services/control-plane/src/control_plane/orphan_sweep.py` | 四个 `configurable` 写入点塞 `agent_key` | Task 2 |
| `packages/expert-work-persistence/migrations/versions/0154_artifact_agent_key.py` | 新建:加列 + 回填 + 换唯一约束 | Task 3 |
| `packages/expert-work-persistence/src/expert_work/persistence/artifact/{base,sql,memory}.py`<br>`packages/expert-work-protocol/src/expert_work/protocol/artifact.py` | `ArtifactStore` 四个方法加 `agent_key` | Task 4 |
| `services/orchestrator/src/orchestrator/tools/artifact.py` | `SaveArtifactTool` / `ListArtifactsTool` 传 `agent_key` | Task 5 |
| `services/orchestrator/src/orchestrator/tools/workspace_paths.py` | **新建**:agent 根 / `shared:` 前缀的单源解析 | Task 6 |
| `services/orchestrator/src/orchestrator/tools/file_ops.py` | 四个文件工具用新 `ws` | Task 7 |
| `services/orchestrator/src/orchestrator/tools/{sandbox,agent_sandbox}.py` | exec `cwd` 按 agent | Task 8 |
| `services/control-plane/src/control_plane/api/external_artifacts.py` | 不再 `del agent_code` | Task 9 |
| `apps/admin-ui/docs-site/guide/*.md` | 对外文档 | Task 10 |
| `tools/persistence/migrate_workspace_agent_scoping.py` | **新建**:文件搬迁脚本(库 + CLI) | Task 11 |
| `services/retention-cleanup-job/src/retention_cleanup_job/{workspace_files,orphan_threads}.py` | 路径形状多一层 + `.tool_results` 清理 | Task 12 |

---

## Task 1: `agent_key` 进 `ToolContext` —— ✅ 已交付(#1507)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/registry.py:173-275`
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:2986-3068`
- Test: `services/orchestrator/tests/test_tool_context.py`(若不存在则新建)

**Interfaces:**
- Produces: `ToolContext.agent_key: str = ""` —— 空串表示「没绑 agent」,与 `agent_key_envs("")` 的既有语义一致(返回空 dict,不注入)。

- [ ] **Step 1: 写失败的测试**

```python
# services/orchestrator/tests/test_tool_context.py
from uuid import uuid4

from orchestrator.graph_builder.builder import _build_tool_context


def test_agent_key_lifts_from_configurable() -> None:
    """agent_key 从 configurable 抬进 ToolContext —— 工具层判归属的唯一来源。"""
    ctx = _build_tool_context(
        {"configurable": {"tenant_id": str(uuid4()), "agent_key": "ai-health-plan-1a2b3c4d"}}
    )
    assert ctx.agent_key == "ai-health-plan-1a2b3c4d"


def test_agent_key_defaults_to_empty_when_absent() -> None:
    """没绑 agent 的调用(临时沙箱 / 老 config)拿到空串,不是 None —— 与
    ``agent_key_envs("")`` 的既有「空串=不注入」语义对齐。"""
    ctx = _build_tool_context({"configurable": {"tenant_id": str(uuid4())}})
    assert ctx.agent_key == ""


def test_agent_key_rejects_non_string() -> None:
    """configurable 是不可信输入(与 _string_list / _parse_uuid 同一姿态)。"""
    ctx = _build_tool_context({"configurable": {"agent_key": 12345}})
    assert ctx.agent_key == ""
```

- [ ] **Step 2: 跑,确认红**

Run: `cd services/orchestrator && uv run --no-sync pytest tests/test_tool_context.py -v`
Expected: FAIL —— `AttributeError: 'ToolContext' object has no attribute 'agent_key'`

- [ ] **Step 3: 实现**

`registry.py` —— 在 `ToolContext` 末尾(`turn_image_refs` 之后)加字段:

```python
    #: 工作区分层 —— 本次调用属于哪个 agent。``sanitize_agent_key(spec.metadata.name)``,
    #: 与 ``/opt/skills/<agent_key>`` / ``PYTHONUSERBASE`` 用的是同一个值。
    #: 空串 = 没绑 agent(临时沙箱 / 未升级的调用方),与 ``agent_key_envs("")``
    #: 的「空串=不注入」语义一致,文件工具据此回落用户根(迁移期,见 Task 7)。
    agent_key: str = ""
```

`builder.py` `_build_tool_context` —— 在 `oauth_user_id` 那一段旁边加读取,并在 `return ToolContext(...)` 里加参数:

```python
    # 工作区分层 —— agent 身份。configurable 是不可信输入,非字符串一律当没有。
    agent_key_raw = configurable.get("agent_key")
    agent_key = agent_key_raw if isinstance(agent_key_raw, str) else ""
```

```python
        turn_image_refs=images,
        agent_key=agent_key,
    )
```

- [ ] **Step 4: 跑,确认绿**

Run: `cd services/orchestrator && uv run --no-sync pytest tests/test_tool_context.py -v`
Expected: 3 passed

- [ ] **Step 5: 变异自证**

删掉 `builder.py` 里 `agent_key=agent_key,` 那一行 → 必须红(第一条用例)。把 `isinstance(agent_key_raw, str)` 改成 `agent_key_raw is not None` → 第三条必须红。每次先 `git diff` 确认变异落地,再手写回原文。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/registry.py \
        services/orchestrator/src/orchestrator/graph_builder/builder.py \
        services/orchestrator/tests/test_tool_context.py
git commit -m "feat(workspace): ToolContext 带上 agent_key —— 工作区分层的地基"
```

---

## Task 2: `configurable` 写入点都塞 `agent_key` —— ✅ 已交付(#1507)

> ### ⚠️ 勘误(09-11,实施时实测)—— 本节原文写的「四个入口」是错的
>
> 实测是**九个**「起 agent 图」的调用点、**七个** `configurable` 构造点,
> 而且原文登记的四个里**三个函数名也是错的**:
> `_run_one` / `trigger_run` / `_revive` 实际叫 `_execute` / `spawn_run` / `_respawn`。
>
> **实际的七个构造点**(以 `test_agent_key_plumbing.py` 的登记表为准,不要再照下方原文):
>
> | 构造点 | 取值 | 漏了会怎样 |
> |---|---|---|
> | `control-plane/run_queue_worker.py::_execute` | `record.spec.metadata.name` | 队列路径全落用户根 |
> | `control-plane/api/runs.py::spawn_run` | `record_spec.metadata.name` | SSE 建 run 全落用户根 |
> | `control-plane/api/runs.py::resolve_approval_decision` | `spec_record.spec.metadata.name` | **审批续跑后半程**写用户根、前半程写 agent 子树,同一轮活分两处 |
> | `control-plane/trigger_firing.py::fire_trigger` | `record.spec.metadata.name` | 触发器路径全落用户根 |
> | `control-plane/orphan_sweep.py::_respawn` | `record.spec.metadata.name` | 孤儿复活后半程落用户根 |
> | `control-plane/skill_evolution_wiring.py::_make_replay_config_factory` | `candidate.agent_name` | replay 跑完整 agent 图且带**真实** tenant/user,看不见被 replay 那个 agent 自己的文件,held-out 判定失真 |
> | `orchestrator/tools/_child_run.py::_child_config` | **透传** `ctx.agent_key` | 委派子代干的是父 agent 的活,产物不落父子树 = 父读不到自己 worker 刚写的文件 |
>
> **两处确认不需要**:`control-plane/eval_engine_live.py::_run`(内联 spec,连 `tenant_id` 都不给)、
> `orchestrator/sse.py::run_agent`(merge 点不是构造点,`**` 展开时 `agent_key` 原样穿过;
> `RunRecord` 里也没有 agent 身份)。
>
> **本节下方的原始步骤(测试代码、四处实现片段)按上表理解,不要照抄。**
> 实际落地的测试是三条 AST 不变式(清单穷举 / 每处都填 / 现算必调 `sanitize_agent_key` 且透传必不调),
> 比原文的两条多一条 —— agent name 无字符集约束,未净化的值直接进路径是真汇点。
>
> **踩到的坑(后续 task 照着躲)**:`resolve_approval_decision` 改成读 `spec.metadata.name` 之后,
> `test_approval_timeout_sweep.py` / `test_resume_idempotency_flow.py` 里两处
> `SimpleNamespace(spec=SimpleNamespace())` 的桩比真实类型少一层,CI 红 9 条。
> **改了哪个函数,就 grep 出全部触到它的测试文件再跑**,别只跑自己想到的那几个。


**Files:**
- Modify: `services/control-plane/src/control_plane/run_queue_worker.py:353`
- Modify: `services/control-plane/src/control_plane/api/runs.py:1241`
- Modify: `services/control-plane/src/control_plane/trigger_firing.py:~290`
- Modify: `services/control-plane/src/control_plane/orphan_sweep.py:392`
- Test: `services/control-plane/tests/test_agent_key_plumbing.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `ToolContext.agent_key`
- Produces: 四条执行入口的 `config["configurable"]["agent_key"]`

> **这是四个入口,不是两个。** 仓库里有过先例:「执行入口三个,规矩只写一处就漏两个」
> (run 计费 trace 绑定,#1373+#1382)。这次是四个,一个都不能漏。
> `sse.py:380` 是 merge 点,`RunRecord` 里没有 agent 身份,**拿不到也不该在那里塞**。

- [ ] **Step 1: 写失败的测试 —— 用「穷举入口」的形状,不是逐个点名**

```python
# services/control-plane/tests/test_agent_key_plumbing.py
"""四条执行入口都必须把 agent_key 放进 configurable。

用 AST 扫描而不是逐个 import 跑一遍:这四处的构造上下文(队列 worker /
stream 端点 / 触发器 / 孤儿复活)各自要一整套 store 桩,逐个跑真调用的成本
远大于收益;而这条不变式的形态恰恰是「有没有漏掉某一处」——AST 正好答这个。

新增第五个入口时,把它加进 _ENTRYPOINTS 即可;漏加会被
test_entrypoint_inventory_is_complete 逮住。
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "control_plane"

#: 每条执行入口:(文件, 构造 configurable 的函数名)
_ENTRYPOINTS = (
    ("run_queue_worker.py", "_run_one"),
    ("api/runs.py", "trigger_run"),
    ("trigger_firing.py", "fire_trigger"),
    ("orphan_sweep.py", "_revive"),
)


def _assigns_agent_key(path: Path, func_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name != func_name:
            continue
        for sub in ast.walk(node):
            # configurable["agent_key"] = ...
            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
                if sub.slice.value == "agent_key":
                    return True
            # {"agent_key": ...} 字面量
            if isinstance(sub, ast.Dict):
                for key in sub.keys:
                    if isinstance(key, ast.Constant) and key.value == "agent_key":
                        return True
    return False


def test_every_entrypoint_sets_agent_key() -> None:
    missing = [
        f"{rel}::{fn}"
        for rel, fn in _ENTRYPOINTS
        if not _assigns_agent_key(_SRC / rel, fn)
    ]
    assert not missing, f"这些执行入口没往 configurable 塞 agent_key: {missing}"


def test_entrypoint_inventory_is_complete() -> None:
    """全仓扫「构造 configurable 字面量」的地方,不得多于 _ENTRYPOINTS。

    漏登记一个新入口 = 那个入口的 agent_key 永远为空 = 该路径下所有工具
    静默回落用户根。这条就是防这个。
    """
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign | ast.Assign):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            named = any(
                isinstance(t, ast.Name) and t.id == "configurable" for t in targets
            )
            if named and isinstance(node.value, ast.Dict):
                found.append(str(path.relative_to(_SRC)))
    assert sorted(found) == sorted(rel for rel, _ in _ENTRYPOINTS), (
        f"构造 configurable 的文件集合变了。实际={sorted(found)} "
        f"登记={sorted(rel for rel, _ in _ENTRYPOINTS)}"
    )
```

- [ ] **Step 2: 跑,确认红**

Run: `cd services/control-plane && uv run --no-sync pytest tests/test_agent_key_plumbing.py -v`
Expected: FAIL —— 第一条列出全部四处;第二条如果实际文件集合与登记不符,说明入口清单要先对齐(**先对齐清单,不要改断言去迁就**)。

- [ ] **Step 3: 实现 —— 四处**

`run_queue_worker.py`(`meta.agent_name` 在作用域,`:283` 起):

```python
            if run.user_id is not None:
                configurable["user_id"] = str(run.user_id)
            # 工作区分层 —— 与 /opt/skills/<agent_key> 同一个值。
            configurable["agent_key"] = sanitize_agent_key(meta.agent_name)
```

`api/runs.py`(`record_spec` 在作用域):

```python
    configurable["oauth_user_id"] = oauth_subject
    configurable["agent_key"] = sanitize_agent_key(record_spec.metadata.name)
```

`trigger_firing.py`:

```python
    configurable["trigger_origin"] = True
    configurable["agent_key"] = sanitize_agent_key(spec.metadata.name)
```

`orphan_sweep.py`:

```python
            if orphan.user_id is not None:
                configurable["user_id"] = str(orphan.user_id)
            configurable["agent_key"] = sanitize_agent_key(record.spec.metadata.name)
```

四处都加 import:

```python
from orchestrator.tools.skill_seed import sanitize_agent_key
```

> 各处 spec 变量名不同(`meta.agent_name` / `record_spec` / `spec` / `record.spec`),
> **动手前先读那一段确认变量名**,不要照抄。

- [ ] **Step 4: 跑,确认绿**

Run: `cd services/control-plane && uv run --no-sync pytest tests/test_agent_key_plumbing.py -v`
Expected: 2 passed

- [ ] **Step 5: 变异自证**

四处逐个删掉那一行 → 第一条测试必须逐次报出被删的那一处(四次都要跑,别只跑一次)。再临时新建一个含 `configurable: dict[str, Any] = {...}` 的假入口文件 → 第二条必须红。手写还原。

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/run_queue_worker.py \
        services/control-plane/src/control_plane/api/runs.py \
        services/control-plane/src/control_plane/trigger_firing.py \
        services/control-plane/src/control_plane/orphan_sweep.py \
        services/control-plane/tests/test_agent_key_plumbing.py
git commit -m "feat(workspace): 四条执行入口把 agent_key 送进 configurable"
```

> **Task 1+2 合成 PR1**,标题 `feat(workspace): PR1 —— agent_key 打通到工具层(零行为变更)`。

---

## Task 3: 迁移 —— `artifact` 加 `agent_key` 列、回填、换唯一约束 —— ✅ 已交付(#1509)

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0154_artifact_agent_key.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models.py`(`ArtifactRow`)
- Test: `packages/expert-work-persistence/tests/test_artifact_agent_key_migration.py`

**Interfaces:**
- Produces: `artifact.agent_key TEXT NOT NULL DEFAULT ''`;唯一约束 `artifact_identity_uniq` 从 `(tenant_id, user_id, name)` 变为 `(tenant_id, user_id, agent_key, name)`。

> **回填链比字段名长一跳。** `artifact_version.created_in_thread` 存的是 **`run_id`**
> (`tools/artifact.py:137` 写的是 `str(ctx.run_id)`),不是 thread_id。
> 所以是 `created_in_thread` → `agent_run.id` → `agent_run.thread_id` →
> `thread_meta.agent_name`。回落常量 `_FALLBACK_THREAD_ID` 的那批推不出来,留空串。
>
> **`agent_key` 不能在 SQL 里算** —— `sanitize_agent_key` 含 sha256。照 `0111` 的先例
> (它直接 import 生产代码的 `compute_content_hash`),迁移里 import 它。

- [ ] **Step 1: 写失败的测试**

```python
# packages/expert-work-persistence/tests/test_artifact_agent_key_migration.py
"""0154 的回填与唯一约束 —— 真 Postgres(testcontainers),不是内存栈。

本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

import pytest

pytestmark = pytest.mark.integration


async def test_two_agents_same_artifact_name_are_two_rows(pg_store) -> None:
    """核心不变式:同名不再合并。"""
    tenant_id, user_id = uuid4(), uuid4()
    a = await pg_store.save_version(
        tenant_id=tenant_id, user_id=user_id, agent_key="plan-aaaaaaaa",
        name="报告.docx", kind="document",
        path_in_workspace="agents/plan-aaaaaaaa/报告.docx",
        created_in_thread=str(uuid4()),
    )
    b = await pg_store.save_version(
        tenant_id=tenant_id, user_id=user_id, agent_key="sop-bbbbbbbb",
        name="报告.docx", kind="document",
        path_in_workspace="agents/sop-bbbbbbbb/报告.docx",
        created_in_thread=str(uuid4()),
    )
    assert a.artifact_id != b.artifact_id
    assert a.version == 1 and b.version == 1


async def test_same_agent_same_name_still_bumps_version(pg_store) -> None:
    """收窄不能收过头:同一个 agent 重存同名,仍然是同一行 + 版本 +1。"""
    tenant_id, user_id = uuid4(), uuid4()
    kw = dict(
        tenant_id=tenant_id, user_id=user_id, agent_key="plan-aaaaaaaa",
        name="报告.docx", kind="document",
        path_in_workspace="agents/plan-aaaaaaaa/报告.docx",
    )
    v1 = await pg_store.save_version(**kw, created_in_thread=str(uuid4()))
    v2 = await pg_store.save_version(**kw, created_in_thread=str(uuid4()))
    assert v1.artifact_id == v2.artifact_id
    assert (v1.version, v2.version) == (1, 2)


async def _seed_legacy_artifact(
    pg_conn, *, tenant_id, user_id, name, created_in_thread, agent_name=None
):
    """造一条 0154 之前形状的数据:artifact.agent_key 为空串。

    ``agent_name`` 给了才建 thread_meta + agent_run 这条反推链;不给就是
    「链断了」的形态(run 行已删 / created_in_thread 是回落常量)。
    """
    artifact_id, version_id = uuid4(), uuid4()
    if agent_name is not None:
        thread_id = uuid4()
        await pg_conn.execute(
            text(
                "INSERT INTO thread_meta (thread_id, tenant_id, user_id, agent_name, "
                "agent_version) VALUES (:tid, :ten, :u, :an, '1.0.0')"
            ),
            {"tid": thread_id, "ten": tenant_id, "u": user_id, "an": agent_name},
        )
        await pg_conn.execute(
            text(
                "INSERT INTO agent_run (id, tenant_id, thread_id, user_id, status) "
                "VALUES (:rid, :ten, :tid, :u, 'success')"
            ),
            {"rid": UUID(created_in_thread), "ten": tenant_id, "tid": thread_id, "u": user_id},
        )
    await pg_conn.execute(
        text(
            "INSERT INTO artifact (id, tenant_id, user_id, agent_key, name, kind, "
            "latest_version, created_at, updated_at) VALUES "
            "(:aid, :ten, :u, '', :n, 'document', 1, now(), now())"
        ),
        {"aid": artifact_id, "ten": tenant_id, "u": user_id, "n": name},
    )
    await pg_conn.execute(
        text(
            "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
            "path_in_workspace, created_in_thread, created_at) VALUES "
            "(:vid, :aid, :ten, :u, 1, :p, :cit, now())"
        ),
        {
            "vid": version_id,
            "aid": artifact_id,
            "ten": tenant_id,
            "u": user_id,
            "p": name,
            "cit": created_in_thread,
        },
    )
    return artifact_id


async def _agent_key_of(pg_conn, artifact_id) -> str:
    row = await pg_conn.execute(
        text("SELECT agent_key FROM artifact WHERE id = :id"), {"id": artifact_id}
    )
    return row.scalar_one()


async def test_backfill_resolves_agent_via_run_then_thread(pg_conn, run_backfill) -> None:
    """回填链比字段名长一跳:created_in_thread 存的是 run_id,不是 thread_id。

    所以是 created_in_thread → agent_run.id → agent_run.thread_id →
    thread_meta.agent_name。少一跳就永远填不上。
    """
    tenant_id, user_id, run_id = uuid4(), uuid4(), uuid4()
    artifact_id = await _seed_legacy_artifact(
        pg_conn,
        tenant_id=tenant_id,
        user_id=user_id,
        name="报告.docx",
        created_in_thread=str(run_id),
        agent_name="ai-health-plan",
    )
    await run_backfill()
    assert await _agent_key_of(pg_conn, artifact_id) == sanitize_agent_key("ai-health-plan")


async def test_backfill_leaves_unresolvable_rows_empty(pg_conn, run_backfill) -> None:
    """链断了就留空串,不猜。

    两种断法都要覆盖:created_in_thread 根本不是 UUID(回落常量
    _FALLBACK_THREAD_ID),以及是 UUID 但 agent_run 行已经不在了。
    """
    tenant_id, user_id = uuid4(), uuid4()
    not_a_uuid = await _seed_legacy_artifact(
        pg_conn, tenant_id=tenant_id, user_id=user_id,
        name="a.docx", created_in_thread="artifact-tool", agent_name=None,
    )
    run_gone = await _seed_legacy_artifact(
        pg_conn, tenant_id=tenant_id, user_id=user_id,
        name="b.docx", created_in_thread=str(uuid4()), agent_name=None,
    )
    await run_backfill()
    assert await _agent_key_of(pg_conn, not_a_uuid) == ""
    assert await _agent_key_of(pg_conn, run_gone) == ""


async def test_backfill_picks_the_earliest_version_owner(pg_conn, run_backfill) -> None:
    """一条 artifact 被两个 agent 先后写过时,归**第一个**建它的。

    后来者在新的四元组键下会分出自己的行——把归属判给最后写的那个,会让
    历史上第一个 agent 的产物凭空改姓。
    """
    tenant_id, user_id = uuid4(), uuid4()
    plan_run, sop_run = uuid4(), uuid4()
    artifact_id = await _seed_legacy_artifact(
        pg_conn, tenant_id=tenant_id, user_id=user_id,
        name="报告.docx", created_in_thread=str(plan_run), agent_name="ai-health-plan",
    )
    # 第二个 agent 在同一条 artifact 上追加 v2(0154 之前 ON CONFLICT 就是这么合并的)
    sop_thread = uuid4()
    await pg_conn.execute(
        text(
            "INSERT INTO thread_meta (thread_id, tenant_id, user_id, agent_name, agent_version) "
            "VALUES (:tid, :ten, :u, 'sop2-designer', '1.0.0')"
        ),
        {"tid": sop_thread, "ten": tenant_id, "u": user_id},
    )
    await pg_conn.execute(
        text(
            "INSERT INTO agent_run (id, tenant_id, thread_id, user_id, status) "
            "VALUES (:rid, :ten, :tid, :u, 'success')"
        ),
        {"rid": sop_run, "ten": tenant_id, "tid": sop_thread, "u": user_id},
    )
    await pg_conn.execute(
        text(
            "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
            "path_in_workspace, created_in_thread, created_at) VALUES "
            "(:vid, :aid, :ten, :u, 2, '报告.docx', :cit, now())"
        ),
        {"vid": uuid4(), "aid": artifact_id, "ten": tenant_id, "u": user_id, "cit": str(sop_run)},
    )
    await pg_conn.execute(
        text("UPDATE artifact SET latest_version = 2 WHERE id = :id"), {"id": artifact_id}
    )

    await run_backfill()
    assert await _agent_key_of(pg_conn, artifact_id) == sanitize_agent_key("ai-health-plan")
```

- [ ] **Step 2: 跑,确认红**

Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && cd packages/expert-work-persistence && uv run --no-sync pytest tests/test_artifact_agent_key_migration.py -v`
Expected: FAIL —— `save_version() got an unexpected keyword argument 'agent_key'`

- [ ] **Step 3: 写迁移**

```python
# packages/expert-work-persistence/migrations/versions/0154_artifact_agent_key.py
"""artifact 加 agent_key —— 同一用户下不同 agent 的同名产物不再合并成一行。

工作区分层 spec §6.1。原唯一键 (tenant_id, user_id, name) 使两个 agent 存
同名产物时走 ON CONFLICT DO UPDATE,合并成一行、版本号累加、旧字节被覆盖。

回填链比字段名长一跳:artifact_version.created_in_thread 存的是 **run_id**
(tools/artifact.py:137 写的是 str(ctx.run_id)),所以要
created_in_thread → agent_run.id → agent_run.thread_id → thread_meta.agent_name。
推不出来的(回落常量、run 行已删)留空串,不猜。

agent_key 含 sha256,SQL 算不出来,照 0111 的先例 import 生产代码。

Revision ID: 0154_artifact_agent_key
Revises: 0153_agent_run_supersede
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from orchestrator.tools.skill_seed import sanitize_agent_key

revision: str = "0154_artifact_agent_key"
down_revision: str | Sequence[str] | None = "0153_agent_run_supersede"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "artifact",
        sa.Column("agent_key", sa.Text(), nullable=False, server_default=""),
    )

    bind = op.get_bind()
    # 每个 artifact 取它**最早**那个版本的 created_in_thread 定归属:一条 artifact
    # 若真被两个 agent 写过,归第一个建它的——后来者在新键下会分出自己的行。
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT ON (a.id) a.id AS artifact_id, tm.agent_name
              FROM artifact AS a
              JOIN artifact_version AS av ON av.artifact_id = a.id
              LEFT JOIN agent_run AS r
                     ON r.id::text = av.created_in_thread
              LEFT JOIN thread_meta AS tm
                     ON tm.thread_id = r.thread_id
             ORDER BY a.id, av.version ASC
            """
        )
    ).fetchall()
    for row in rows:
        if not row.agent_name:
            continue  # 推不出来 → 保持空串
        bind.execute(
            sa.text("UPDATE artifact SET agent_key = :k WHERE id = :id"),
            {"k": sanitize_agent_key(row.agent_name), "id": row.artifact_id},
        )

    op.drop_constraint("artifact_identity_uniq", "artifact", type_="unique")
    op.create_unique_constraint(
        "artifact_identity_uniq", "artifact", ["tenant_id", "user_id", "agent_key", "name"]
    )


def downgrade() -> None:
    op.drop_constraint("artifact_identity_uniq", "artifact", type_="unique")
    op.create_unique_constraint(
        "artifact_identity_uniq", "artifact", ["tenant_id", "user_id", "name"]
    )
    op.drop_column("artifact", "agent_key")
```

> **downgrade 有一个真实风险要写进 PR 描述**:回滚时若已存在「同一 (tenant,user,name)
> 不同 agent」的两行,重建旧约束会失败。这是不可逆迁移的正常形态,回滚窗口在
> 「新 agent 尚未存同名产物之前」。**不要为了让 downgrade 一定成功而去删数据。**

`models.py` 的 `ArtifactRow` 同步加 `agent_key: Mapped[str]` 并改 `UniqueConstraint`。

- [ ] **Step 4: 跑,确认绿**

Run: 同 Step 2
Expected: 4 passed

- [ ] **Step 5: 变异自证**

把新约束改回三元组 → 第一条必须红(两个 agent 合并成一行)。把回填的 `ORDER BY av.version ASC` 改成 `DESC` → 若测试逮不住,说明用例没造「一条 artifact 被两个 run 写过」的形状,**补上再说**。把 `if not row.agent_name: continue` 删掉 → 第四条必须红。

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-persistence/migrations/versions/0154_artifact_agent_key.py \
        packages/expert-work-persistence/src/expert_work/persistence/models.py \
        packages/expert-work-persistence/tests/test_artifact_agent_key_migration.py
git commit -m "feat(workspace): artifact 加 agent_key 列 + 唯一键换四元组(迁移 0154)"
```

---

## Task 4: `ArtifactStore` 四个方法加 `agent_key` —— ✅ 已交付(#1509,实际动了七个方法)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/artifact/base.py:30,52,80,101`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/artifact/sql.py:54,116,147`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/artifact/memory.py`
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/artifact.py:44`(`Artifact` 加 `agent_key: str`)
- Test: `packages/expert-work-persistence/tests/test_artifact_store_contract.py`

**Interfaces:**
- Produces:
  - `save_version(*, tenant_id, user_id, agent_key: str, name, kind, path_in_workspace, created_in_thread) -> ArtifactVersion`
  - `list_for_user(*, tenant_id, user_id, agent_key: str | None = None, include_deleted=False) -> list[Artifact]` —— `agent_key=None` 表示不过滤(控制台/留存 job 用),传值则只列该 agent
  - `get_latest_version(*, tenant_id, user_id, agent_key: str, name) -> ArtifactVersion | None`
  - `soft_delete(*, tenant_id, user_id, agent_key: str, name, now) -> bool`
- Consumes: Task 3 的列与约束

> 其余 13 个方法(`list_expired` / `hard_delete` / `delete_versions` …)是**留存与运维视角**,
> 按 artifact_id 或时间批量操作,**不加 `agent_key`**。改它们等于把 agent 维度渗进
> 一个本来不需要它的层。

- [ ] **Step 1: 写失败的测试**(两个后端都跑,SQL 与内存必须同义 —— 仓库既有教训:
  「SQL↔内存 store 谓词必须 byte-identical」)

```python
@pytest.mark.parametrize("store", ["sql", "memory"], indirect=True)
async def test_list_for_user_filters_by_agent_key(store) -> None:
    t, u = uuid4(), uuid4()
    await store.save_version(
        tenant_id=t, user_id=u, agent_key="plan-aaaaaaaa", name="计划.docx",
        kind="document", path_in_workspace="agents/plan-aaaaaaaa/计划.docx",
        created_in_thread=str(uuid4()),
    )
    await store.save_version(
        tenant_id=t, user_id=u, agent_key="sop-bbbbbbbb", name="评审.docx",
        kind="document", path_in_workspace="agents/sop-bbbbbbbb/评审.docx",
        created_in_thread=str(uuid4()),
    )

    only_plan = await store.list_for_user(
        tenant_id=t, user_id=u, agent_key="plan-aaaaaaaa"
    )
    assert {a.name for a in only_plan} == {"计划.docx"}


@pytest.mark.parametrize("store", ["sql", "memory"], indirect=True)
async def test_list_for_user_without_agent_key_returns_all(store) -> None:
    """控制台与留存 job 要看全量 —— agent_key=None 不过滤。"""
    everything = await store.list_for_user(tenant_id=t, user_id=u)
    assert {a.name for a in everything} == {"计划.docx", "评审.docx"}


@pytest.mark.parametrize("store", ["sql", "memory"], indirect=True)
async def test_get_latest_version_is_agent_scoped(store) -> None:
    """两个 agent 各有同名产物时,取的是自己那条。"""
    v = await store.get_latest_version(
        tenant_id=t, user_id=u, agent_key="sop-bbbbbbbb", name="报告.docx"
    )
    assert v.path_in_workspace.startswith("agents/sop-bbbbbbbb/")
```

- [ ] **Step 2: 跑,确认红** — `TypeError: unexpected keyword argument 'agent_key'`

- [ ] **Step 3: 实现** —— `base.py` 改签名与 docstring(类 docstring 那句
  `"""Agent-artifact registry, scoped to ``(tenant_id, user_id)``."""` 一并改成
  `(tenant_id, user_id, agent_key)`);`sql.py` 的 `save_version` 在 `pg_insert(...).values(...)`
  里加 `agent_key=agent_key`,`list_for_user` 在 `agent_key is not None` 时加一条
  `.where(ArtifactRow.agent_key == agent_key)`,`get_latest_version` / `soft_delete`
  的 `where` 加同一条;`memory.py` 逐条对齐。

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证** —— 把 `sql.py` 里 `list_for_user` 的 agent 谓词删掉 → 第一条必须红;
  只删 `memory.py` 那条 → 同一条测试在 memory 档必须红(证明参数化真的两档都跑了,
  没有静默 skip)。

- [ ] **Step 6: 提交**

```bash
git commit -m "feat(workspace): ArtifactStore 四个方法带 agent_key,两个后端同义"
```

---

## Task 5: `SaveArtifactTool` / `ListArtifactsTool` 用 `agent_key` —— ⚠️ 部分交付(#1509)

> **交付状态**:`save_version(agent_key=ctx.agent_key)` 与 `list_artifacts` 只列本 agent
> **已随 PR2 交付**;`path_in_workspace` 加前缀这一半**没做,顺延到 PR3(Task 7)**。
>
> **勘误一(为什么 PR2 不能加前缀)**:`path_in_workspace` 是下载端点真去读的物理路径,
> 而文件是 `write_file` 早先落的盘 —— 它的根目录要到 PR3(Task 6/7)才改。PR2 就加前缀
> = 每一次下载 404。已钉 `test_path_in_workspace_keeps_the_agents_own_relative_path`。
>
> **勘误二(前缀的形状本身也是错的)**:下面写的 `agents/<agent_key>/artifacts/<path>`
> 多了 `artifacts/` 这一段。`save_artifact` 不搬字节,没有任何东西往那个目录写;
> Task 6 的 `resolve_scope` 把 `write_file("报告.docx")` 解析到
> `/workspace/agents/<key>/报告.docx`。正确形状是 **`agents/<agent_key>/<path>`**。
> 见 spec §四勘误(2026-09-12)。下面的代码与断言已按定论改过。


**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/artifact.py:46,130,188`
- Test: `services/orchestrator/tests/test_artifact_tools.py`

**Interfaces:**
- Consumes: `ToolContext.agent_key`(Task 1)、`ArtifactStore`(Task 4)
- Produces: `path_in_workspace` 形如 `agents/<agent_key>/<path>`(**09-12 勘误后**,原写含 `artifacts/` 段)

> **`path_in_workspace` 存含前缀的完整相对路径**,于是 spec §7.3 列的七个下游消费点
> (控制台/对外/会话三处下载 + MIME + 留存 unlink)**一行都不用改** —— 它们都是拿这一列
> join 用户根,前缀在值里就自然对了。

- [ ] **Step 1: 写失败的测试**

```python
async def test_save_artifact_lands_under_agent_dir() -> None:
    store = InMemoryArtifactStore()
    tool = SaveArtifactTool(store=store)
    ctx = _ctx(agent_key="plan-aaaaaaaa")
    await tool.call({"name": "报告.docx"}, ctx=ctx)
    v = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id,
        agent_key="plan-aaaaaaaa", name="报告.docx",
    )
    assert v.path_in_workspace == "agents/plan-aaaaaaaa/报告.docx"


async def test_list_artifacts_only_shows_own() -> None:
    """两个 agent 各存一个,各自只看得见自己的。"""
    store = InMemoryArtifactStore()
    tenant_id, user_id = uuid4(), uuid4()
    for agent_key, name in (("plan-aaaaaaaa", "计划.docx"), ("sop-bbbbbbbb", "评审.docx")):
        await SaveArtifactTool(store=store).call(
            {"name": name},
            ctx=_ctx(tenant_id=tenant_id, user_id=user_id, agent_key=agent_key),
        )

    out = await ListArtifactsTool(store=store).call(
        {}, ctx=_ctx(tenant_id=tenant_id, user_id=user_id, agent_key="sop-bbbbbbbb")
    )
    assert "评审.docx" in out.content
    assert "计划.docx" not in out.content


async def test_save_artifact_without_agent_key_keeps_flat_path() -> None:
    """迁移期回落:没绑 agent 的调用仍写扁平路径,不造出 agents// 这种第三形状。"""
    store = InMemoryArtifactStore()
    ctx = _ctx(agent_key="")
    await SaveArtifactTool(store=store).call({"name": "报告.docx"}, ctx=ctx)
    v = await store.get_latest_version(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, agent_key="", name="报告.docx"
    )
    assert v.path_in_workspace == "报告.docx"
```

- [ ] **Step 2: 跑,确认红**

- [ ] **Step 3: 实现**

```python
def _artifact_path(agent_key: str, path: str) -> str:
    """产物在工作区里的相对路径 —— 含 agent 前缀。

    ``agent_key`` 为空(迁移期未升级的调用方 / 临时沙箱)时保持扁平路径,
    否则会拼出 ``agents//x`` 这种既非旧位置也非新位置的第三种形状。
    """
    if not agent_key:
        return path
    return f"agents/{agent_key}/{path}"
```

`SaveArtifactTool.call()` 里:

```python
        path_in_workspace = _artifact_path(ctx.agent_key, _validate_path(path))
        # (kind / created_in_thread 的算法不变,原样保留)
        version = await self.store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=ctx.agent_key,      # ← 新增的只有这一行
            name=name,
            kind=kind,
            path_in_workspace=path_in_workspace,
            created_in_thread=thread_id,
        )
```

`ListArtifactsTool.call()` 里 `list_for_user(..., agent_key=ctx.agent_key or None)`,
并把 spec 的 description 改成实话:

```python
            description=(
                "List the artifacts you have saved. Scoped to you — artifacts "
                "saved by other agents working for the same user are not listed."
            ),
```

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证** —— 删掉 `agent_key=ctx.agent_key or None` 里的 `or None` 之外的部分
  (即改回 `list_for_user(tenant_id=..., user_id=...)`)→ 第二条必须红。
  把 `_artifact_path` 的空串分支删掉 → 第三条必须红。

- [ ] **Step 6: 提交** —— **Task 3+4+5 合成 PR2**,标题
  `feat(workspace): PR2 —— 产物按 agent 分,同名不再合并`。

---

## Task 6: 工作区路径解析单源模块

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/workspace_paths.py`
- Test: `services/orchestrator/tests/test_workspace_paths.py`

**Interfaces:**
- Produces:
  - `SHARED_PREFIX = "shared:"`
  - `agent_workspace_root(agent_key: str) -> str` —— 返回沙箱内绝对路径
  - `resolve_scope(path: str, *, agent_key: str, tool: str) -> tuple[str, str]` —— 返回 `(ws, rel)`
  - `WriteToSharedError(ValueError)`

- [ ] **Step 1: 写失败的测试**

```python
import pytest

from orchestrator.tools.workspace_paths import (
    WriteToSharedError,
    agent_workspace_root,
    resolve_scope,
)


def test_bare_path_resolves_under_agent_root() -> None:
    ws, rel = resolve_scope("MEMORY.md", agent_key="plan-aaaaaaaa", tool="read_file")
    assert ws == "/workspace/agents/plan-aaaaaaaa"
    assert rel == "MEMORY.md"


def test_shared_prefix_resolves_to_shared_root() -> None:
    ws, rel = resolve_scope("shared:style/PLAN_STYLE.md", agent_key="plan-aaaaaaaa", tool="read_file")
    assert ws == "/workspace/shared"
    assert rel == "style/PLAN_STYLE.md"


def test_empty_agent_key_falls_back_to_user_root() -> None:
    """迁移期回落:没绑 agent 时读写用户根,与今天行为一致。"""
    ws, rel = resolve_scope("MEMORY.md", agent_key="", tool="read_file")
    assert ws == "/workspace"  # == workspace_paths.USER_ROOT
    assert rel == "MEMORY.md"


def test_shared_prefix_is_rejected_for_writes() -> None:
    """写 shared: 必须显式报错,不能静默改写到 agent 目录。"""
    with pytest.raises(WriteToSharedError):
        resolve_scope("shared:x.md", agent_key="plan-aaaaaaaa", tool="write_file")


def test_shared_prefix_still_rejects_traversal() -> None:
    """shared: 不是绕过 .. 校验的后门。"""
    with pytest.raises(ValueError, match="\\.\\."):
        resolve_scope("shared:../../etc/passwd", agent_key="plan-aaaaaaaa", tool="read_file")


def test_agent_key_with_slash_is_refused() -> None:
    """agent_key 来自 configurable(不可信);带 / 的值会把 ws 撬出 /workspace。"""
    with pytest.raises(ValueError):
        resolve_scope("x.md", agent_key="../../etc", tool="read_file")
```

- [ ] **Step 2: 跑,确认红** —— `ModuleNotFoundError: orchestrator.tools.workspace_paths`

- [ ] **Step 3: 实现**

```python
"""工作区路径的作用域解析 —— agent 根 / shared 的唯一真源。

沙箱里的 ``/workspace`` 挂的是用户根(热沙箱按 (tenant,user) 复用,挂载点
无法按 agent 分,见 spec §三)。分层因此做在路径上:每个 agent 的默认根是
``/workspace/agents/<agent_key>``,交给 ``build_*_wrapper(rel, ws=...)`` 的
``ws`` 参数,由沙箱内片段既有的 ``realpath`` + ``startswith`` 守卫强制
(``file_ops.py`` 的 ``_PRELUDE``)——与它挡 ``..`` 是同一道闸。

``shared/`` 存迁移期反推不出归属的 legacy,**可读不可写**,且不与默认根合并:
要读必须写 ``shared:`` 前缀。不合并是有意的——让 legacy 文件默默混进日常列表
正是本设计要治的病。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

#: 显式跨到用户级 shared 区的前缀(照 ADK 的 ``user:`` 约定)。
SHARED_PREFIX = "shared:"

#: 沙箱内的用户工作区根(挂载点)。导出而非私有:``file_ops`` 的迁移期回落要用它,
#: 而那个模块自己也有一个同名私有常量——两处各改各的就会静默分叉。
USER_ROOT = "/workspace"

_AGENTS_DIR = "agents"
_SHARED_DIR = "shared"

#: agent_key 来自 ``config["configurable"]`` —— 不可信。它会被拼进 ``ws``,
#: 一个带 ``/`` 或 ``..`` 的值能把整个作用域撬到 /workspace 之外。
#: 形状与 ``sanitize_agent_key()`` 的产物一致:``[A-Za-z0-9._-]+``。
_AGENT_KEY_OK = re.compile(r"\A[A-Za-z0-9._-]+\Z")

_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


class WriteToSharedError(ValueError):
    """写 ``shared:`` —— 显式拒绝,不静默改写目标。"""


def agent_workspace_root(agent_key: str) -> str:
    """该 agent 在沙箱内的默认根;``agent_key`` 为空时回落用户根。"""
    if not agent_key:
        return USER_ROOT
    if not _AGENT_KEY_OK.match(agent_key):
        msg = f"agent_key is not a safe path segment: {agent_key!r}"
        raise ValueError(msg)
    return f"{USER_ROOT}/{_AGENTS_DIR}/{agent_key}"


def resolve_scope(path: str, *, agent_key: str, tool: str) -> tuple[str, str]:
    """把工具收到的 ``path`` 拆成 ``(ws, rel)``。

    ``ws`` 交给 ``build_*_wrapper(..., ws=ws)``,``rel`` 是相对它的路径。
    ``..`` / 绝对路径 / NUL 的校验仍由调用方的 ``_require_path`` 负责——
    本函数只负责作用域选择,并保证 ``shared:`` 不是绕过那些校验的后门。
    """
    raw = path.strip()
    if raw.startswith(SHARED_PREFIX):
        if tool in _WRITE_TOOLS:
            msg = (
                f"{tool} cannot write to the shared area; "
                f"drop the {SHARED_PREFIX!r} prefix to write into your own workspace"
            )
            raise WriteToSharedError(msg)
        rel = raw[len(SHARED_PREFIX) :].strip()
        if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
            msg = f"{tool} path must be relative and free of '..': {path!r}"
            raise ValueError(msg)
        return f"{USER_ROOT}/{_SHARED_DIR}", rel
    return agent_workspace_root(agent_key), raw
```

- [ ] **Step 4: 跑,确认绿** —— 6 passed

- [ ] **Step 5: 变异自证** —— 删掉 `_AGENT_KEY_OK` 检查 → 最后一条必须红;
  把 `WriteToSharedError` 分支删掉 → 第四条必须红;删掉 `shared:` 分支里的 `..` 检查 → 第五条必须红。

- [ ] **Step 6: 提交**

```bash
git commit -m "feat(workspace): 新增 workspace_paths —— agent 根与 shared: 前缀的单源解析"
```

---

## Task 7: 四个文件工具用新作用域(带迁移期回落)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py:499,563,631,715`(四个 `call()`)
- Modify: 同文件四处 `ToolSpec.description`
- Test: `services/orchestrator/tests/test_file_ops.py`

**Interfaces:**
- Consumes: `resolve_scope()`(Task 6)、`ToolContext.agent_key`(Task 1)

> **迁移期回落**:读类工具(`read_file` / `list_dir`)在 agent 根下找不到目标时,
> **回落一次用户根**再读。写类工具(`write_file` / `edit_file`)**永不回落**——
> 新内容一律落 agent 目录。这样文件搬迁(Task 11)之前后、滚动发布窗口里,
> 新旧 pod 都读得到东西,而新写入从第一天起就分好了。
> 回落在 Task 13(PR6)摘掉。
>
> **代价明说**:回落窗口内读串问题**还是今天的样子** —— 不是新增回归,是尚未修复。

> **⚠️ 从 Task 5 顺延过来的一条(09-12)**:`SaveArtifactTool` 给 `path_in_workspace`
> 加前缀**属于本 task**,不属于 PR2 —— 前缀必须与 `write_file` 的落盘位置同时改,
> 早一步就是每次下载 404。形状取 **`agents/<agent_key>/<path>`**(不含 `artifacts/` 段,
> 见 spec §四勘误);`agent_key` 为空时保持扁平路径。Task 5 里的 `_artifact_path()`
> 与那三条断言原样搬过来即可,顺手把 PR2 钉下的
> `test_path_in_workspace_keeps_the_agents_own_relative_path` 改成新形状
> ——**它是一条有意的「暂缓」哨兵,本 task 落地时必须换掉,不是删掉**。

- [ ] **Step 1: 写失败的测试**

```python
async def test_read_file_resolves_under_agent_root() -> None:
    client = _client(json.dumps({"ok": True, "content": "hi"}))
    await ReadFileTool(client=client).call({"path": "MEMORY.md"}, ctx=_ctx(agent_key="plan-aaaaaaaa"))
    assert '"ws": "/workspace/agents/plan-aaaaaaaa"' in client.execs[-1].code


async def test_write_file_never_falls_back() -> None:
    """写永远落 agent 目录,即使那里还不存在。"""
    client = _client(json.dumps({"ok": True, "size": 2}))
    await WriteFileTool(client=client).call(
        {"path": "MEMORY.md", "content": "hi"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
    )
    assert len(client.execs) == 1
    assert '"ws": "/workspace/agents/plan-aaaaaaaa"' in client.execs[0].code


async def test_read_file_falls_back_to_user_root_once() -> None:
    """迁移期:agent 根下没有 → 回落用户根再读一次,且只一次。"""
    client = _client_sequence([
        json.dumps({"ok": False, "error": "not_found"}),
        json.dumps({"ok": True, "content": "legacy"}),
    ])
    out = await ReadFileTool(client=client).call({"path": "MEMORY.md"}, ctx=_ctx(agent_key="plan-aaaaaaaa"))
    assert out.content == "legacy"
    assert len(client.execs) == 2
    assert '"ws": "/workspace"' in client.execs[1].code


async def test_shared_prefix_reads_shared_root() -> None:
    client = _client(json.dumps({"ok": True, "content": "x"}))
    await ReadFileTool(client=client).call(
        {"path": "shared:style/PLAN_STYLE.md"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
    )
    assert '"ws": "/workspace/shared"' in client.execs[-1].code


async def test_write_to_shared_is_refused() -> None:
    with pytest.raises(WriteToSharedError):
        await WriteFileTool(client=_client()).call(
            {"path": "shared:x.md", "content": "no"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
        )
```

- [ ] **Step 2: 跑,确认红**

- [ ] **Step 3: 实现** —— 每个 `call()` 里把

```python
        rel = _require_path(args, tool="read_file")
        outcome = await run_in_sandbox(self.client, code=build_read_wrapper(rel, cap=...), ...)
```

改成

```python
        raw = _require_path(args, tool="read_file")
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="read_file")
        outcome = await run_in_sandbox(
            self.client, code=build_read_wrapper(rel, cap=self.output_char_cap, ws=ws), ...
        )
        env = parse_envelope(outcome, tool="read_file")
        # 迁移期回落 —— agent 根下没有就到用户根再看一次(Task 14 摘掉)。
        # USER_ROOT 从 workspace_paths 导出,不要用 file_ops 自己那个私有
        # ``_WORKSPACE_ROOT``:两个同名常量各改各的就会静默分叉。
        if _is_not_found(env) and ws != USER_ROOT and not raw.startswith(SHARED_PREFIX):
            outcome = await run_in_sandbox(
                self.client,
                code=build_read_wrapper(rel, cap=self.output_char_cap, ws=USER_ROOT),
                timeout_s=None,
                ctx=ctx,
                tool_label="read_file",
                fallback_thread_id="read_file",
                seed_files=self.skill_seed_files,
            )
            env = parse_envelope(outcome, tool="read_file")
```

> `_require_path` 里那段折 `/workspace/` 前缀的逻辑要保留,并**加一条**:
> `/workspace/agents/<自己的 key>/` 也折掉(模型会照着 `list_dir` 的输出回传绝对路径)。

四处 description 改成实话,例如 `list_dir`:

```python
            description=(
                "List the entries of a directory in your workspace (name, is_dir, "
                "size). Paths are relative to your own workspace root. Files saved "
                "by other agents working for the same user are not visible here. "
                "Prefix a path with 'shared:' to read the shared legacy area "
                "(read-only)."
            ),
```

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证** —— 把 `write_file` 的回落加上(故意)→ `test_write_file_never_falls_back` 必须红;
  把回落条件里的 `and not raw.startswith(SHARED_PREFIX)` 删掉 → 补一条「`shared:` 读不到时不回落」的用例并确认它红。

- [ ] **Step 6: 提交**

---

## Task 8: exec `cwd` 按 agent

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py:1472`
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py`(本地 supervisor 后端同一处语义)
- Test: `services/orchestrator/tests/test_sandbox_runtime_contract.py`

**Interfaces:**
- Consumes: `agent_key`(已绑在 `_AgentKeyBindingClient` 上,`sandbox.py:518`)

> `bash` / `exec_python` 的相对路径默认落自己的目录。**这是约定不是强制** ——
> 它们能 `cd /workspace` 走出去。两个后端的 `cwd` 必须 byte-identical
> (既有 `test_sandbox_runtime_contract.py` 就是钉这个的)。
> `agent_key` 为空时 `cwd` 保持 `/workspace`。

- [ ] **Step 1: 写失败的测试**

```python
async def test_exec_cwd_is_agent_scoped(runtime) -> None:
    await runtime.exec(sandbox_id=sid, code="print(1)", timeout_s=5, agent_key="plan-aaaaaaaa")
    assert runtime.last_cwd == "/workspace/agents/plan-aaaaaaaa"


async def test_exec_cwd_without_agent_key_stays_workspace_root(runtime) -> None:
    await runtime.exec(sandbox_id=sid, code="print(1)", timeout_s=5, agent_key="")
    assert runtime.last_cwd == "/workspace"
```

- [ ] **Step 2-4:** 跑红 → 实现(复用 `workspace_paths.agent_workspace_root()`,**不要再写一遍拼接**)→ 跑绿

- [ ] **Step 5: 变异自证** —— 只改一个后端 → 契约测试必须红(证明两档真的都跑)

- [ ] **Step 6: 提交** —— **Task 6+7+8 合成 PR3**,标题
  `feat(workspace): PR3 —— 工具层按 agent 分层(带迁移期读回落)`

---

## Task 9: 对外平面按 agent 收口(六个 handler,不是三个)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_artifacts.py:12-15,147,198,322`
- Modify: `services/control-plane/src/control_plane/api/external_workspace.py:11-15,77,140`
- Modify: `services/control-plane/src/control_plane/api/external_uploads.py:418`
- Modify: `services/control-plane/src/control_plane/api/_workspace_shared.py:77-105`(`_workspace_files_payload` 加前缀参数)
- Test: `services/control-plane/tests/test_external_artifacts.py`、`tests/test_external_workspace.py`、`tests/test_external_uploads.py`

**Interfaces:**
- Consumes: Task 4 的 `list_for_user(agent_key=...)` / `get_latest_version(agent_key=...)` / `soft_delete(agent_key=...)`
- Produces: `_agent_key_for_code(tenant_id, agent_code, repo) -> str` —— 对外三个模块共用的一处解析

> **全仓 `del agent_code` 有六处,不是三处。** 立项时只点了 `external_artifacts` 那三个:
>
> | 端点 | 文件:行 | 今天 |
> |---|---|---|
> | `GET /{code}/artifacts` | `external_artifacts.py:147` | 列该用户全部 |
> | `GET /{code}/artifacts/download`(按 `name`) | `external_artifacts.py:198` | 跨 agent 也给 |
> | `DELETE /{code}/artifacts` | `external_artifacts.py:322` | 跨 agent 也删 |
> | `GET /{code}/workspace/files` | `external_workspace.py:77` | **`os.walk` 用户根全量平铺** |
> | `GET /{code}/workspace/file`(按 `path`) | `external_workspace.py:140` | 跨 agent 也给 |
> | `GET /{code}/uploads/{upload_id}` | `external_uploads.py:418` | 跨 agent 也给 |
>
> `POST /{code}/uploads` 不在此列 —— 它**已经在用** `agent_code`(过 kill-switch 闸、进 `_resolve_session`)。

### 9.1 工作区两条要做**路径投影**,不是加过滤条件

产物按 `name` 取、上传按 `upload_id` 取,这两种标识搬迁后都不变,加个 `agent_key` 谓词就够。
**工作区按 `path` 取,而 `path` 是搬迁会改的东西** —— `客户案例/x.md` 变成
`agents/<agent_key>/客户案例/x.md`。如果对外原样透出新路径,对接方**缓存过的 path 会永久失效**
(不是发布窗口的事,是永久),而且内部命名 `<agent_key>`(带 sha256 后缀)也漏到了第三方面前。

对外平面**本来就已经按 `agent_code` 分区**,所以对外的 path 应当相对**该 agent 的根**:

```
存储层        {tenant}/{user}/agents/ai-health-plan-1a2b3c4d/客户案例/x.md
对外返回      客户案例/x.md                     ← 与搬迁前逐字节相同
对外收到      客户案例/x.md
服务端拼      agents/<该 code 的 agent_key>/客户案例/x.md
```

于是:对接方**契约零变更、代码零改动**;跨 agent 仍然 404(A 的 code 拼出 A 的根,B 的文件不在那儿);
`agent_key` 挡在对外面之外。

> **投影必须双向且共用一处**。只做出口剥前缀、忘了入口加前缀,下载会全线 404;
> 两边各写一份拼接,将来改一处就静默分叉(仓库里 `workspace_user_root()` 的 docstring
> 记着同一类事故:两处各自拼 `root/tenant/user` 差点漂开)。

### 9.2 `shared/` 对外不可见

`shared/` 装的是搬迁时反推不出归属的 legacy。**对外列表与下载都不投影它** ——
第三方按 `agent_code` 提问,答案里混进「不知道谁的历史文件」没有意义,
而且那批文件正是归属不明的那批。控制台看得见(Task 13 的浏览面分组),对外不给。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_external_workspace.py
async def test_list_files_paths_are_relative_to_agent_root(client, seeded) -> None:
    """对外 path 不带 agents/<key>/ 前缀 —— 与搬迁前逐字节相同。

    这条是「对接方不用改代码」的唯一保证:他们缓存过的 path 必须继续能用。
    """
    r = await client.get("/v1/agents/agent-a/workspace/files", params={"user_id": EXT_UID})
    paths = [f["path"] for f in r.json()["data"]["files"]]
    assert paths == ["客户案例/x.md"]
    assert not any(p.startswith("agents/") for p in paths)


async def test_list_files_excludes_other_agents_and_shared(client, seeded) -> None:
    """B agent 的文件、以及 shared/ 的 legacy,都不出现在 A 的列表里。"""
    r = await client.get("/v1/agents/agent-a/workspace/files", params={"user_id": EXT_UID})
    paths = [f["path"] for f in r.json()["data"]["files"]]
    assert "b-only.md" not in paths
    assert not any("MEMORY.md" in p for p in paths)  # shared/MEMORY.md


async def test_download_accepts_the_path_it_handed_out(client, seeded) -> None:
    """出口剥前缀、入口加前缀 —— 双向必须闭合。

    只做出口忘了入口,这一条会 404;这就是那种「列表看着对、下载全挂」的形态。
    """
    listed = (await client.get(
        "/v1/agents/agent-a/workspace/files", params={"user_id": EXT_UID}
    )).json()["data"]["files"][0]["path"]
    r = await client.get(
        "/v1/agents/agent-a/workspace/file", params={"user_id": EXT_UID, "path": listed}
    )
    assert r.status_code == 200
    assert r.content == b"x-body"


async def test_download_across_agents_is_404(client, seeded) -> None:
    """A 的 code 拿 B 的文件路径 → 与「不存在」同一个不透明 404。"""
    r = await client.get(
        "/v1/agents/agent-a/workspace/file", params={"user_id": EXT_UID, "path": "b-only.md"}
    )
    assert r.status_code == 404


async def test_download_cannot_climb_into_another_agent(client, seeded) -> None:
    """投影不是绕过 .. 校验的后门:显式往上爬也要被 _safe_workspace_relpath 挡。"""
    r = await client.get(
        "/v1/agents/agent-a/workspace/file",
        params={"user_id": EXT_UID, "path": "../agent-b-key/b-only.md"},
    )
    assert r.status_code in (400, 404, 422)


async def test_download_cannot_reach_shared_by_path(client, seeded) -> None:
    """shared/ 对外不可见——直接拼路径也不行。"""
    r = await client.get(
        "/v1/agents/agent-a/workspace/file",
        params={"user_id": EXT_UID, "path": "MEMORY.md"},
    )
    assert r.status_code == 404
```

```python
# tests/test_external_artifacts.py
async def test_list_only_returns_this_agents_artifacts(client, seeded) -> None:
    r = await client.get("/v1/agents/agent-a/artifacts", params={"user_id": EXT_UID})
    assert [a["name"] for a in r.json()["data"]["artifacts"]] == ["a-only.docx"]


async def test_download_across_agents_is_404(client, seeded) -> None:
    r = await client.get(
        "/v1/agents/agent-a/artifacts/download",
        params={"user_id": EXT_UID, "name": "b-only.docx"},
    )
    assert r.status_code == 404


async def test_delete_across_agents_is_404_and_leaves_the_row(client, seeded, store) -> None:
    """404 不能是「删了但假装没有」——B 的行必须还在。"""
    r = await client.delete(
        "/v1/agents/agent-a/artifacts", params={"user_id": EXT_UID, "name": "b-only.docx"}
    )
    assert r.status_code == 404
    assert await store.get_latest_version(
        tenant_id=TENANT, user_id=INT_UID, agent_key="agent-b-key", name="b-only.docx"
    ) is not None


async def test_same_name_under_two_agents_resolves_per_code(client, seeded) -> None:
    """两个 agent 各有 报告.docx —— 各自的 code 取到各自那份。"""
    a = await client.get("/v1/agents/agent-a/artifacts/download",
                         params={"user_id": EXT_UID, "name": "报告.docx"})
    b = await client.get("/v1/agents/agent-b/artifacts/download",
                         params={"user_id": EXT_UID, "name": "报告.docx"})
    assert a.content == b"from-a"
    assert b.content == b"from-b"
```

```python
# tests/test_external_uploads.py
async def test_upload_download_across_agents_is_404(client, seeded) -> None:
    r = await client.get(
        f"/v1/agents/agent-a/uploads/{upload_id_from_agent_b}", params={"user_id": EXT_UID}
    )
    assert r.status_code == 404
```

- [ ] **Step 2: 跑,确认红**

Run:
```
cd services/control-plane && uv run --no-sync pytest \
  tests/test_external_workspace.py tests/test_external_artifacts.py tests/test_external_uploads.py -v
```
Expected: FAIL —— 今天六个 handler 都 `del agent_code`,跨 agent 一律给

- [ ] **Step 3: 实现**

三个模块共用一处解析(**不要各写一份**):

```python
# _workspace_shared.py
async def agent_key_for_code(*, tenant_id: UUID, agent_code: str, repo: AgentSpecStore) -> str:
    """对外 URL 里的 ``agent_code`` → 该 agent 写工作区时用的 ``agent_key``。

    必须与 agent 自己写入时用的是同一个值(``configurable`` → ``ToolContext``
    → ``SaveArtifactTool`` / 文件工具),否则读写两侧看的是两棵树。
    """
    record = await repo.get_latest(tenant_id=tenant_id, code=agent_code)
    ...
    return sanitize_agent_key(record.spec.metadata.name)
```

工作区双向投影(**一处实现,出入口共用**):

```python
def _external_to_storage(rel: str, *, agent_key: str) -> str:
    """对外相对路径 → 存储层相对路径。入口用。"""
    return f"agents/{agent_key}/{rel}" if agent_key else rel


def _storage_to_external(rel: str, *, agent_key: str) -> str | None:
    """存储层相对路径 → 对外相对路径;不属于该 agent 的返回 None(列表里剔掉)。出口用。"""
    prefix = f"agents/{agent_key}/"
    return rel[len(prefix):] if rel.startswith(prefix) else None
```

顺序上**先过 `_safe_workspace_relpath` 再加前缀** —— 校验必须作用在对接方给的原串上,
否则 `../` 会被前缀拼接掩盖成一个看起来合法的路径。

三个模块的 docstring 整段改写。`external_workspace.py:11-15` 现在写着:

> 工作区本身是 `(tenant_id, user_id)` 维度的,不按 agent 分——`agent_code` 只是外部平面
> URL 结构的一部分……**不参与过滤**,和控制台侧 `/v1/workspace/files`(压根没有 agent_code)语义一致

**这句从此不成立**,要改成「按 `(tenant, user, agent)` 分,对外 path 相对 agent 根,
`shared/` 不对外投影」,并写明与控制台侧的差异(控制台看全量并按 agent 分组,对外只看本 agent)。

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证**

**六个 handler 逐个验,不要只验一个** —— 同一个洞的六个断点,仓库里踩过「只预判了一个断点」的亏
(P-2 那次 store 层与端点层两处只补了一处)。

| 变异 | 必须红的用例 |
|---|---|
| `list_for_user` 去掉 `agent_key=` | `test_list_only_returns_this_agents_artifacts` |
| `get_latest_version` 去掉 `agent_key=` | 产物 `test_download_across_agents_is_404` |
| `soft_delete` 去掉 `agent_key=` | `test_delete_across_agents_is_404_and_leaves_the_row` |
| 列表出口不剥前缀 | `test_list_files_paths_are_relative_to_agent_root` |
| **只剥出口、入口不加前缀** | `test_download_accepts_the_path_it_handed_out` ——**这条是双向闭合的唯一证人** |
| `_storage_to_external` 对不匹配的返回 `rel` 而非 `None` | `test_list_files_excludes_other_agents_and_shared` |
| 前缀拼接挪到 `_safe_workspace_relpath` 之前 | `test_download_cannot_climb_into_another_agent` |
| uploads GET 去掉 agent 谓词 | `test_upload_download_across_agents_is_404` |

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_artifacts.py \
        services/control-plane/src/control_plane/api/external_workspace.py \
        services/control-plane/src/control_plane/api/external_uploads.py \
        services/control-plane/src/control_plane/api/_workspace_shared.py \
        services/control-plane/tests/test_external_artifacts.py \
        services/control-plane/tests/test_external_workspace.py \
        services/control-plane/tests/test_external_uploads.py
git commit -m "feat(workspace): 对外六个 handler 按 agent 收口,工作区路径做双向投影"
```

### 9.3 对接方影响 —— 一句话:不用改代码,但会少看见东西

实测(2026-09-10 测试环境):对接方**自己就在同一批终端用户身上跑两个 agent** ——
同一个 project 下 `ai-health-plan` 与 `sop2-designer` 共用终端用户,分别 112 / 35 / 15 / 14 / 2 个会话。
所以下面第 2 条大概率会碰上。

| | 影响 | 要不要改代码 |
|---|---|---|
| 请求签名 / 响应信封 / 字段名 | **零变更**(`agent_code` 本来就在路径里) | 不用 |
| 工作区 path | **零变更**(§9.1 的投影保证) | 不用 |
| 列表返回条数 | 变少 —— 只剩本 agent 的 | 不用,但界面上会少东西 |
| **跨 agent 的下载 / 删除** | **404**,永久 | **要么改成按 agent 分别请求,要么接受看不到** |

**最后一条是有意的收窄,不是 bug**,但因为 404 被刻意做成不透明的
(docstring:第三方不能分辨「用户不存在」「文件不存在」「supervisor 没配」),
他们**分不出「归另一个 agent」和「压根没有」**,排查会很难受。

**这条要在 PR4 合并前告知对接方**,不能等他们踩。Task 10 的文档要写清变更前后对照。

### 9.4 `?scope=user` 并集参数(**已拍板要做**)

**2026-09-11:已告知对接方,确认要做并集入口。**

他们的 app 是**一个** app 同时编排两个 agent、服务同一批终端用户。
「agent 之间不互读」(09-10 拍板,治的是 agent 把别人的客户档案当成自己的事实)
和「他们的后端要不要一次拿到某员工的全部产物」**是两件事,不冲突**。

**契约**:三个列表端点加可选 query 参数 `scope`,取值 `agent`(默认)| `user`。

| 端点 | `scope=agent`(默认) | `scope=user` |
|---|---|---|
| `GET /{code}/artifacts` | 仅该 agent | 该终端用户全部 agent 的 |
| `GET /{code}/workspace/files` | 仅该 agent | 全部 agent,条目带归属标识 |

**下载类端点不加 `scope`。** 理由:下载按 `name` / `path` / `upload_id` 取,
放开 scope 等于让 A 的 code 取到 B 的字节,那正是本设计要挡的。
对接方要下另一个 agent 的东西,**用那个 agent 的 code 去下** —— 列表里已经告诉他们归属了。

**`scope=user` 时工作区 path 怎么给**:`path` 始终相对**它自己那个 agent** 的根
(与 `scope=agent` 语义一致,不撞名),另用 `agent_code` 字段标明归属:

```json
{"path": "客户案例/x.md", "agent_code": "sop2-designer", "size": 1024}
```

对接方拿 `(agent_code, path)` 就能直接去下载。

> **不要用 `agent_key` 做这个标识** —— 那是内部命名(带 sha256 后缀),
> 对外只出现 `agent_code`,映射在服务端做。

**`shared/` 在两种 scope 下都不对外投影**(§9.2)。

- [ ] **补充测试**

```python
async def test_scope_user_returns_all_agents(client, seeded) -> None:
    r = await client.get(
        "/v1/agents/agent-a/artifacts", params={"user_id": EXT_UID, "scope": "user"}
    )
    names = {a["name"] for a in r.json()["data"]["artifacts"]}
    assert names == {"a-only.docx", "b-only.docx", "报告.docx"}


async def test_scope_defaults_to_agent(client, seeded) -> None:
    # 不传 scope = 只看本 agent。默认值错了会静默把隔离整个放开。
    r = await client.get("/v1/agents/agent-a/artifacts", params={"user_id": EXT_UID})
    assert {a["name"] for a in r.json()["data"]["artifacts"]} == {"a-only.docx", "报告.docx"}


async def test_scope_user_files_carry_agent_code_not_agent_key(client, seeded) -> None:
    # 归属标识对外只能是 agent_code —— agent_key 是内部命名,不许漏出去。
    r = await client.get(
        "/v1/agents/agent-a/workspace/files", params={"user_id": EXT_UID, "scope": "user"}
    )
    files = r.json()["data"]["files"]
    assert {f["agent_code"] for f in files} == {"agent-a", "agent-b"}
    assert not any("agent_key" in f for f in files)
    # path 仍相对各自 agent 根,不带 agents/<key>/ 前缀
    assert not any(f["path"].startswith("agents/") for f in files)


async def test_scope_user_does_not_open_downloads(client, seeded) -> None:
    # 下载端点不认 scope —— 传了也不放开跨 agent。
    r = await client.get(
        "/v1/agents/agent-a/artifacts/download",
        params={"user_id": EXT_UID, "name": "b-only.docx", "scope": "user"},
    )
    assert r.status_code == 404


async def test_scope_user_still_excludes_shared(client, seeded) -> None:
    r = await client.get(
        "/v1/agents/agent-a/workspace/files", params={"user_id": EXT_UID, "scope": "user"}
    )
    assert not any("MEMORY.md" in f["path"] for f in r.json()["data"]["files"])


async def test_invalid_scope_is_422(client) -> None:
    r = await client.get(
        "/v1/agents/agent-a/artifacts", params={"user_id": EXT_UID, "scope": "tenant"}
    )
    assert r.status_code == 422
```

- [ ] **补充变异自证**

| 变异 | 必须红 |
|---|---|
| `scope` 默认值改成 `user` | `test_scope_defaults_to_agent` |
| 下载端点也读 `scope` 并放开 | `test_scope_user_does_not_open_downloads` |
| 条目里回 `agent_key` 而不是 `agent_code` | `test_scope_user_files_carry_agent_code_not_agent_key` |
| `scope=user` 时把 `shared/` 也投影出去 | `test_scope_user_still_excludes_shared` |

---

## Task 10: 对外文档

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/chat.md`(产物那节)
- Modify: `apps/admin-ui/docs-site/guide/conventions.md`
- Modify: `apps/admin-ui/docs-site/guide/examples.md`(若有产物示例)

- [ ] **Step 1:** 先按 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md` 自检语气与体例
- [ ] **Step 2:** 写清四件事:
  ① **六个端点**按 agent 收口(产物 list/download/delete、工作区 files/file、上传 download),
  给出变更前后对照;
  ② **工作区 path 契约不变** —— 对外 path 相对该 agent 根,他们缓存过的 path 继续有效,
  **明说这一点**,否则对接方看到「工作区分层」会以为要改;
  ③ 上传归属:经哪个 agent 的会话传的就归哪个;
  ④ **跨 agent 的下载/删除返回 404,且与「不存在」不可区分** —— 这条最要紧,
  他们排查时分不出来,文档里必须明写这是有意的收窄;
  ⑤ **`?scope=user` 并集参数**(§9.4):三个列表端点支持、默认 `agent`;
  下载端点**不支持**,要下别的 agent 的东西就用那个 agent 的 code;
  `scope=user` 的条目带 `agent_code` 字段标明归属
- [ ] **Step 3:** 跑 docs-site 构建 + 死链脚本,确认零死链;侧栏若新增小节要登记
  (既有教训:`examples.md` 侧栏漏登记过)
- [ ] **Step 4: 提交** —— **Task 9+10 合成 PR4**

---

## Task 11: 文件搬迁脚本

**Files:**
- Create: `tools/persistence/migrate_workspace_agent_scoping.py`
- Test: `tools/persistence/test_migrate_workspace_agent_scoping.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Attributions:
    """从库里查出来的归属事实 —— 纯数据,让 plan_migration 保持可单测的纯函数。"""
    #: 该用户只用过一个 agent 时的 agent_key;多 agent 用户为 None
    sole_agent_key: str | None
    #: ``uploads/<name>`` → agent_key(来自 user_upload.ref + thread_id)
    uploads: Mapping[str, str]
    #: ``path_in_workspace`` → agent_key(来自 artifact.agent_key,Task 3 已回填)
    artifacts: Mapping[str, str]
    #: thread_id → agent_key(来自 thread_meta.agent_name)
    threads: Mapping[str, str]
    #: run_id → agent_key(来自 agent_run → thread_meta)
    runs: Mapping[str, str]


@dataclass(frozen=True)
class MigrationPlan:
    tenant_id: UUID
    user_id: UUID
    #: 旧相对路径 → 新相对路径
    moves: Mapping[str, str]
    #: 反推不出归属、要进 ``shared/`` 的旧相对路径
    to_shared: tuple[str, ...]
    #: 保留段等一律不动的旧相对路径(断言守恒时要算进去)
    untouched: tuple[str, ...]


@dataclass(frozen=True)
class MigrationReport:
    moved: int
    to_shared: int
    untouched: int
    artifact_rows_updated: int
```

  - `plan_migration(root: str, tenant_id: UUID, user_id: UUID, *, attributions: Attributions) -> MigrationPlan` —— 纯函数,只算不搬,不碰库
  - `apply_migration(plan: MigrationPlan, *, root: str, dry_run: bool, session_factory=None) -> MigrationReport`
  - `collect_attributions(session_factory, *, tenant_id: UUID, user_id: UUID) -> Attributions` —— 唯一碰库的读函数
  - CLI:`python -m tools.persistence.migrate_workspace_agent_scoping --tenant ... --user ... [--apply]`

> 范本是 `tools/persistence/restore_volume.py`(库 + CLI 双形态,runbook 驱动,
> 默认不自动改,把结果交回运维)。**默认 dry-run**,`--apply` 才真搬。

**归属判定(spec §7.1):**

| 内容 | 归属来源 |
|---|---|
| `uploads/<name>` | `user_upload.ref` 匹配 → `thread_id` → `thread_meta.agent_name` |
| artifact 的 `path_in_workspace` | `artifact.agent_key`(Task 3 已回填) |
| `threads/<thread_id>/` | `thread_meta.agent_name` |
| `.tool_results/<run_id>/` | `agent_run.id` → `thread_id` → `thread_meta.agent_name` |
| 其余一切 | **`shared/`** |

**单 agent 用户的捷径**:该用户的 `thread_meta` 只出现过一个 `agent_name` 时,
整棵树(除保留段 `skills/`)直接归那个 agent,不用逐文件判。实测 64 个用户里 56 个属于这一档。

- [ ] **Step 1: 写失败的测试**

```python
def test_single_agent_user_moves_whole_tree(tmp_path) -> None:
    """56/64 的那一档:整棵树归唯一那个 agent。"""
    t, u = uuid4(), uuid4()
    root = tmp_path / str(t) / str(u)
    _write(root / "MEMORY.md", "m")
    _write(root / "客户案例" / "秀域" / "x.md", "x")
    _write(root / "skills" / "seeded.md", "s")  # 保留段,不该被搬

    plan = plan_migration(str(tmp_path), t, u, attributions=Attributions(
        sole_agent_key="plan-aaaaaaaa", uploads={}, artifacts={}, threads={}, runs={}
    ))
    assert plan.moves["MEMORY.md"] == "agents/plan-aaaaaaaa/MEMORY.md"
    assert plan.moves["客户案例/秀域/x.md"] == "agents/plan-aaaaaaaa/客户案例/秀域/x.md"
    assert plan.to_shared == []


def test_multi_agent_user_splits_by_registry(tmp_path) -> None:
    """8/64 的那一档:有登记行的各归其位。"""
    t, u, tid = uuid4(), uuid4(), uuid4()
    root = tmp_path / str(t) / str(u)
    _write(root / "uploads" / "a.docx", "a")
    _write(root / "threads" / str(tid) / "PLAN.md", "p")
    _write(root / "MEMORY.md", "m")  # 无登记行

    plan = plan_migration(str(tmp_path), t, u, attributions=Attributions(
        sole_agent_key=None,
        uploads={"uploads/a.docx": "plan-aaaaaaaa"},
        artifacts={},
        threads={str(tid): "sop-bbbbbbbb"},
        runs={},
    ))

    assert plan.moves["uploads/a.docx"] == "agents/plan-aaaaaaaa/uploads/a.docx"
    assert plan.moves["threads/<tid>/PLAN.md"].startswith("agents/sop-bbbbbbbb/threads/")


def test_unattributable_goes_to_shared(tmp_path) -> None:
    """无登记行的 legacy 进 shared/,不猜。"""
    assert "MEMORY.md" in plan.to_shared
    assert "style/PLAN_STYLE.md" in plan.to_shared


def test_reserved_prefixes_are_never_moved(tmp_path) -> None:
    """skills/ 是保留段,搬迁不碰。"""
    assert not any(p.startswith("skills/") for p in plan.moves)


def test_file_count_is_conserved(tmp_path) -> None:
    """守恒:搬完文件总数不变,一个不多一个不少。"""
    before = _count_files(tmp_path)
    apply_migration(plan, dry_run=False)
    assert _count_files(tmp_path) == before


def test_dry_run_changes_nothing(tmp_path) -> None:
    before = _snapshot(tmp_path)
    apply_migration(plan, dry_run=True)
    assert _snapshot(tmp_path) == before
```

- [ ] **Step 2: 跑,确认红**

- [ ] **Step 3: 实现** —— 纯 `tmp_path` 文件树,零 docker(照
  `services/orchestrator/tests/test_nas_workspace_store.py` 的姿势)。
  搬完同步 `UPDATE artifact_version SET path_in_workspace = ...`。

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证** —— 把 `to_shared` 分支改成「猜第一个 agent」→ 第三条必须红;
  把保留段过滤删掉 → 第四条必须红;在 `apply_migration` 里故意漏搬一个文件 → 守恒那条必须红。

- [ ] **Step 6: 提交**

---

## Task 12: 留存 job 与孤儿扫描适配新布局

**Files:**
- Modify: `services/retention-cleanup-job/src/retention_cleanup_job/workspace_files.py:205-228`(`iter_thread_dirs`)
- Modify: `services/retention-cleanup-job/src/retention_cleanup_job/orphan_threads.py:56-72`
- Modify: `services/control-plane/src/control_plane/api/sessions.py:996-998`(purge 钩子)
- Modify: `services/orchestrator/src/orchestrator/tools/overflow.py:44-47`(改掉说谎的注释)
- Test: `services/retention-cleanup-job/tests/test_workspace_invariant.py`、`tests/test_orphan_threads.py`

> 三件事:① `threads/<id>/` 现在在 `agents/<agent_key>/threads/<id>/`,枚举器多一层;
> ② `.tool_results/<run_id>/` 按 spec §4.2 纳入同一套(会话 purge 连带删 + 孤儿宽限扫描);
> ③ **「根目录其它文件永不触碰」这条不变式在新布局下必须仍然成立** —— `shared/` 是新的
> 「不许碰」区,`test_workspace_invariant.py` 要覆盖它。

- [ ] **Step 1: 写失败的测试**

```python
def test_shared_dir_is_never_touched(tmp_path) -> None:
    """shared/ 装的是反推不出归属的 legacy —— 留存 job 一个字节都不许动。"""
    user_root = tmp_path / str(uuid4()) / str(uuid4())
    _write(user_root / "shared" / "MEMORY.md", "legacy")
    _write(user_root / "shared" / "style" / "PLAN_STYLE.md", "legacy")
    # 造一个**过期很久**的 mtime,证明「不碰」不是因为它还新
    _age(user_root / "shared", days=400)

    sweep_orphan_thread_dirs(str(tmp_path), _thread_store_with_no_rows())

    assert (user_root / "shared" / "MEMORY.md").exists()
    assert (user_root / "shared" / "style" / "PLAN_STYLE.md").exists()


def test_thread_dirs_are_found_under_agent_dir(tmp_path) -> None:
    dirs = list(iter_thread_dirs(str(tmp_path)))
    assert [d.thread_id for d in dirs] == [tid]


def test_tool_results_dir_swept_when_run_is_gone(tmp_path) -> None:
    """孤儿 .tool_results/<run_id>/ 过了宽限期才删。"""
    t, u, run_id = uuid4(), uuid4(), uuid4()
    d = tmp_path / str(t) / str(u) / "agents" / "plan-aaaaaaaa" / ".tool_results" / str(run_id)
    _write(d / "call_x-bash.txt", "out")
    _age(d, days=2)  # 超过宽限期

    removed = sweep_orphan_tool_results(str(tmp_path), _run_store_with_no_rows())

    assert removed == 1
    assert not d.exists()


def test_tool_results_dir_kept_while_run_row_exists(tmp_path) -> None:
    """run 行还在 → 一律不动,不管多老(与 threads/ 同一条规矩)。"""
```

- [ ] **Step 2-4:** 跑红 → 实现 → 跑绿

- [ ] **Step 5: 变异自证** —— 把 `shared` 从「不碰」名单里删掉 → 第一条必须红;
  把 `.tool_results` 的「run 行还在就不动」判断删掉 → 第四条必须红

- [ ] **Step 6: 提交** —— **Task 11+12+13 合成 PR5**,标题
  `feat(workspace): PR5 —— 文件搬迁 + 留存链适配新布局`

---

## Task 13: 保留段语义与控制台浏览面

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/layout.py:28-61`
- Modify: `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py:616-721`(`list_files`)
- Modify: `services/control-plane/src/control_plane/api/_workspace_shared.py:77-105`
- Modify: `apps/admin-ui/src/pages/user_profile/WorkspacePane.tsx:363-431`
- Test: `packages/expert-work-persistence/tests/test_workspace_layout.py`、
  `services/orchestrator/tests/test_nas_workspace_store.py`、
  `apps/admin-ui/src/pages/user_profile/__tests__/WorkspacePane.test.tsx`

**Interfaces:**
- Consumes: Task 11 搬完之后的布局

> **两件 spec §7.3 点名、前面任务没覆盖的事:**
>
> ① `WORKSPACE_RESERVED_PREFIXES` 今天是 `{"skills", "uploads"}`(`layout.py:58-61`),
> 按**顶层第一段**匹配。`uploads/` 搬进 `agents/<key>/uploads/` 之后,那条顶层规则
> 再也匹配不到,于是上传文件会**突然出现在浏览面的「产物」视图里** —— 这是搬迁带来的
> 静默行为变更,不修就会在验收时被当成 bug 报回来。
>
> ② 浏览面的「文件」块是 `os.walk` 用户根的全量平铺,前端再拍成树。新布局多一层
> `agents/<agent_key>/`,树会自然多一级,但**没有任何东西告诉运维「这一级是 agent」** ——
> 两块(产物 / 文件)之间今天也没有任何关联字段。

- [ ] **Step 1: 写失败的测试**

```python
# packages/expert-work-persistence/tests/test_workspace_layout.py
from expert_work.persistence.workspace.layout import is_reserved_workspace_path


def test_uploads_under_agent_dir_is_still_reserved() -> None:
    """搬迁后 uploads 在 agents/<key>/uploads/ —— 仍须被浏览面隐藏。

    不修的话上传文件会突然出现在「产物」视图里,是搬迁引入的静默行为变更。
    """
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/uploads/a.docx")


def test_legacy_toplevel_uploads_is_still_reserved() -> None:
    """回落期与 shared/ 下的老路径同样要认。"""
    assert is_reserved_workspace_path("uploads/a.docx")
    assert is_reserved_workspace_path("shared/uploads/a.docx")


def test_agent_working_files_are_not_reserved() -> None:
    """收窄不能收过头:agent 自己的工作文件照常可见。"""
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/MEMORY.md")
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/客户案例/秀域/x.md")


def test_tool_results_is_hidden_everywhere() -> None:
    """.tool_results 是纯缓存,任何位置都不该出现在浏览面。"""
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/.tool_results/<run>/x.txt")
```

```tsx
// apps/admin-ui/src/pages/user_profile/__tests__/WorkspacePane.test.tsx
it("groups the file tree by agent", () => {
  renderWithFiles([
    { path: "agents/plan-aaaaaaaa/MEMORY.md", size: 10 },
    { path: "agents/sop-bbbbbbbb/MEMORY.md", size: 20 },
    { path: "shared/style/PLAN_STYLE.md", size: 30 },
  ]);
  expect(screen.getByText("plan-aaaaaaaa")).toBeInTheDocument();
  expect(screen.getByText("sop-bbbbbbbb")).toBeInTheDocument();
  // shared 单独一组,且标出它是「归属不明的历史文件」
  expect(screen.getByText(/历史文件/)).toBeInTheDocument();
});
```

- [ ] **Step 2: 跑,确认红**

Run:
```
cd packages/expert-work-persistence && uv run --no-sync pytest tests/test_workspace_layout.py -v
cd apps/admin-ui && pnpm vitest run src/pages/user_profile/__tests__/WorkspacePane.test.tsx
```
Expected: FAIL —— `is_reserved_workspace_path` 只认顶层第一段;前端还没有分组

- [ ] **Step 3: 实现**

`layout.py` —— 判定从「顶层第一段」改成「路径里出现保留段」,但**只在已知的容器层级下**
(不能变成「任何一段叫 uploads 就隐藏」,那会把 agent 自建的 `客户案例/uploads/` 也吃掉):

```python
def is_reserved_workspace_path(relpath: str) -> bool:
    """该路径是否属于「机器/输入」而非 agent 产出——浏览面据此隐藏。

    历史上只认顶层第一段(``skills/`` / ``uploads/``)。工作区加 agent 维度后
    这些目录挪到了 ``agents/<agent_key>/`` 之下,顶层规则再也匹配不到,
    上传文件会突然冒进「产物」视图。这里改成:剥掉已知的容器前缀
    (``agents/<key>/`` 或 ``shared/``)之后,再看第一段。

    只剥**已知容器**、只看剥完的第一段——不是「任何一段叫 uploads 就算」,
    那会把 agent 自己建的 ``客户案例/uploads/`` 也误伤。
    """
    parts = PurePosixPath(relpath.strip()).parts
    if not parts:
        return False
    if parts[0] == _AGENTS_DIR and len(parts) >= 3:
        parts = parts[2:]
    elif parts[0] == _SHARED_DIR and len(parts) >= 2:
        parts = parts[1:]
    return parts[0] in WORKSPACE_RESERVED_PREFIXES
```

`WORKSPACE_RESERVED_PREFIXES` 加上 `.tool_results`(纯缓存,不该出现在浏览面):

```python
WORKSPACE_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR, OVERFLOW_DIR}
)
```

`WorkspacePane.tsx` —— `buildFileTree` 之前先按第一段分组:`agents/<key>/` 各一组
(组标题 = agent_key),`shared/` 一组(标题写明是「归属不明的历史文件」),其余归「其它」。

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证**

把 `layout.py` 里剥容器前缀那两个分支删掉 → 第一条必须红。
把「只看剥完的第一段」改成「任何一段命中即算」→ 第三条(`客户案例/`)必须红 ——
**若不红,说明用例里没造 `客户案例/uploads/` 这个形状,补上再说。**
前端:把分组去掉 → 那条 tsx 用例必须红。

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-persistence/src/expert_work/persistence/workspace/layout.py         packages/expert-work-persistence/tests/test_workspace_layout.py         services/orchestrator/src/orchestrator/tools/nas_workspace_store.py         services/control-plane/src/control_plane/api/_workspace_shared.py         apps/admin-ui/src/pages/user_profile/WorkspacePane.tsx         apps/admin-ui/src/pages/user_profile/__tests__/WorkspacePane.test.tsx
git commit -m "feat(workspace): 保留段认 agent 容器层 + 浏览面按 agent 分组"
```

> `pnpm typecheck` 是必跑项 —— 裸 `tsc --noEmit` 在这个 solution 式 tsconfig 上恒绿,查不出任何东西。

---

## Task 14: 摘掉迁移期读回落

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py`(Task 7 加的回落分支)
- Test: `services/orchestrator/tests/test_file_ops.py`

**前置:** Task 11 的搬迁在**测试环境与生产都跑完并验收**之后才做这一步。

- [ ] **Step 1:** 把 `test_read_file_falls_back_to_user_root_once` 改成
  `test_read_file_does_not_fall_back`,断言 `len(client.execs) == 1`
- [ ] **Step 2:** 跑,确认红
- [ ] **Step 3:** 删掉四处回落分支与 `_is_not_found` 辅助函数
- [ ] **Step 4:** 跑,确认绿
- [ ] **Step 5:** 变异自证 —— 把回落加回去 → 新断言必须红
- [ ] **Step 6: 提交** —— **PR6**,标题 `chore(workspace): 摘掉迁移期读回落`

---

## PR 与班车

| PR | Tasks | 上哪班车 |
|---|---|---|
| PR1 agent_key 地基 | 1, 2 | ✅ **已合 #1507(09-11)**,零行为变更 |
| PR2 产物按 agent 分 | 3, 4, 5(5 只做了一半) | ✅ **已合 #1509(09-12)**,带迁移 `0154`。**`0154` 尚未在任何真环境跑过 —— 发测试环境并核回填结果是 PR3 的前置** |
| PR3 工具层分层 | 6, 7, 8 + Task 5 顺延的 `path_in_workspace` 前缀 | 合后泡测试 |
| PR4 对外端点 + 文档 | 9, 10 | **对外行为变更**,要提前告知对接方 |
| PR5 搬迁 + 留存 + 浏览面 | 11, 12, 13 | 搬迁先在测试环境跑,验收后再上生产 |
| PR6 摘回落 | 14 | 搬迁验收完之后 |

## 验收判据(照 spec §九逐条)

1. 同用户两个 agent 各存同名产物 → 两条独立 artifact 行,各自 v1,字节互不覆盖 → Task 3 测试
2. agent A 写 `MEMORY.md`,agent B 读到的是自己的 → Task 7 测试
3. agent B 的 `list_artifacts` / `list_dir` 不含 A 的东西 → Task 5 / Task 7 测试
4. `shared/` 两个 agent 都可读,写入被拒 → Task 6 / Task 7 测试
5. 对外 `GET /v1/agents/{code}/artifacts` 只返回该 agent 的 → Task 9 测试
6. 单 agent 用户迁移后一个文件不少,全在 `agents/<key>/` 下 → Task 11 守恒测试
7. 多 agent 用户可反推的各归其位、不可反推的全在 `shared/`,总数守恒 → Task 11 测试
8. 「根目录其它文件永不触碰」在新布局下仍成立 → Task 12 测试

## 真栈验收(PR5 合并发测试环境后,主线做)

用 `99d3c664` 那个用户(435 文件 / 112 会话 / 两个 agent)做判据:

- 搬迁前后 `find | wc -l` 相等
- `agents/ai-health-plan-*/` 与 `agents/sop2-designer-*/` 各自非空
- `shared/` 里有 `MEMORY.md`、`style/`、`客户案例/`(反推不出的那批)
- 从控制台起一轮 ai-health-plan 的对话,`list_dir(".")` 看不到 sop2-designer 的目录
- `shared:style/PLAN_STYLE.md` 读得到
