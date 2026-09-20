# run 完成信号（B-85 ③）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一个「图跑完了但事没做成」的 run 能被机器判出来，而 `status` 的语义一个字不动。

**Architecture:** 两条腿。A（平台盖章）——图的每个退出点由**知道答案的那一行代码**写下 `exit_reason`，`tools` 节点每批都记下这批里非 transient 的工具失败；终局把 `exit_reason` + 算出的 `completed` 落到 `agent_run` 两个新列，并随 `end` 帧发给对外消费者。B（模型自述）——系统提示词加一段 completion contract，让模型自己把做不下去的事说出来，平台**不解析**。

**Tech Stack:** Python 3.12 / LangGraph / SQLAlchemy + asyncpg / Alembic / pydantic-settings / pytest

**Spec:** `docs/superpowers/specs/2026-09-20-run-completion-signal-design.md`

## Global Constraints

逐条来自 spec §9「明确不做」与 §5「对外契约」，每个 task 的要求都隐含包含本节。

1. **`RunStatus` 的取值与语义一个字不动。** 不加值、不改判定、不改任何 `set_status` 现有调用点传的 status。
2. **不新增 SSE 事件类型。** 新信号只能是 `end` 帧上的字段。对接方的流处理有事件白名单，新事件类型会被**静默丢掉**。
3. **`None` → 字段缺席，不是 `null`。** 照抄 `end_frame_data` 已有的 `artifacts` / `usage_by_model` 口径：缺席 = 老 run 无记录，不是「确有其事的零值」。
4. **两条 SSE 流的 `end` 帧字段集合必须逐字相同。** 共四个构造点：`sse.py:1772`、`_run_event_stream.py:304 / 484 / 518`。`end_frame_data` 的 docstring 写着这里分叉过一次同类问题。
5. **不推断模型意图。** 判据只能用客观事实。特别地：**不做「零产物 = 模型放弃」这类判据**。
6. **不解析模型正文里的 `[blocked]`。**
7. **两个路由函数一行不改**：`_should_continue`（`builder.py:2229`）与 `_after_tools`（`:2245`）是 LangGraph 的 conditional-edge 函数，只返回路由、不写 state。
8. **不改 `error_classifier` 的关键词表。**
9. `exit_reason` 的取值封闭为六个：`text_response` / `max_steps` / `no_progress` / `token_budget` / `approval_pending` / `approval_rejected`。
10. 本地跑测试一律 `uv run --no-sync pytest ...`（仓库惯例；mypy 仍以 CI 为准）。

---

### Task 1: `AgentState` 两个通道 + `tools` 节点写入

**Files:**
- Modify: `services/orchestrator/src/orchestrator/state.py`（`AgentState` 通道声明，约 :224 附近）
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:1729-1732`（`tools` 节点返回值）
- Test: `services/orchestrator/tests/test_run_exit_reason.py`（新建）

**Interfaces:**
- Produces：`AgentState["last_batch_failures"]: list[ClassifiedToolError]` —— **每跑一批工具就无条件覆盖一次**（包括写空列表），只保留 `error_class != "transient"` 的条目。`AgentState["exit_reason"]: str`。Task 2/3 都读这两个。

- [ ] **Step 1: 写失败的测试**

```python
# services/orchestrator/tests/test_run_exit_reason.py
"""B-85 ③ —— run 的退出原因与「做没做成」。

判据纪律(spec §4):只用客观事实,不推断模型意图。这里钉住的是
「最后一批工具调用里还有没有未解决的非 transient 失败」,不是「模型是不是放弃了」。
"""
from orchestrator.tools.error_classifier import ClassifiedToolError


def test_tools_node_overwrites_last_batch_failures_with_an_empty_list() -> None:
    """批 1 失败 → 批 2 全成功 → 通道必须被**清空**。

    不无条件写(只在非空时写)的话,通道里会一直留着批 1 的失败,
    于是一个已经自我恢复的 run 被判成 completed=false —— 误报。
    这正是 spec §6.2 那条防线。
    """
    failed_batch = _run_tools_node(failures=[_non_transient("boom")])
    assert failed_batch["last_batch_failures"] == [_non_transient("boom")]

    clean_batch = _run_tools_node(failures=[])
    assert "last_batch_failures" in clean_batch, "跑了一批工具就必须写,哪怕是空的"
    assert clean_batch["last_batch_failures"] == []


def test_transient_failures_are_not_recorded() -> None:
    """transient 是可重试的抖动,不是「没做成」的证据 —— 与 ``error_signal``(builder.py:925)同一条谓词。"""
    out = _run_tools_node(failures=[_transient("504"), _non_transient("boom")])
    assert [f.message for f in out["last_batch_failures"]] == ["boom"]
```

（`_run_tools_node` / `_non_transient` / `_transient` 是本文件内的小工厂：构造一个
最小 `AgentState`、调 `tools` 节点、返回它的 `result_dict`。照 `services/orchestrator/tests/`
里既有的图节点测试写法，不要起真图。）

- [ ] **Step 2: 跑测试，确认它红**

```
uv run --no-sync pytest services/orchestrator/tests/test_run_exit_reason.py -v
```
Expected: FAIL —— `KeyError: 'last_batch_failures'`

- [ ] **Step 3: 加通道声明**

```python
# services/orchestrator/src/orchestrator/state.py —— 挨着 tool_failures 声明
    #: B-85 ③ —— 最近一批工具调用里**未解决的**失败(只留非 transient)。
    #: 与 ``tool_failures`` 的区别是它**不按轮重置**:``tool_failures`` 被
    #: ``agent_node`` 读完发完 ``<recovery-advisory>`` 就清空(builder.py:1244/1284),
    #: 走到 END 时拿不到;这一条由 ``tools`` 节点**每批无条件覆盖**(空批写 ``[]``),
    #: 所以终局读到的恒是「最后一批的情况」。
    #: 写在 ``tools`` 节点而不是 ``agent_node``:后者看到空列表时分不清
    #: 「这批工具全成功」与「这一轮压根没跑工具」,前者只在真跑过一批时才执行。
    last_batch_failures: NotRequired[list[ClassifiedToolError]]

    #: B-85 ③ —— run 从哪个出口结束的,由**知道答案的那一行**盖章(spec §5.3)。
    #: 封闭取值:text_response / max_steps / no_progress / token_budget /
    #: approval_pending / approval_rejected。
    exit_reason: NotRequired[str]
```

- [ ] **Step 4: `tools` 节点无条件写**

```python
# builder.py:1729-1732 —— 原来是 ``if tool_failures: result_dict["tool_failures"] = tool_failures``
        if tool_failures:
            result_dict["tool_failures"] = tool_failures
        # B-85 ③ —— **无条件**写(空批也写),否则批 1 失败、批 2 全成功之后
        # 通道里还留着批 1 的失败,一个已自我恢复的 run 会被误判成没做成。
        result_dict["last_batch_failures"] = [
            f for f in tool_failures if f.error_class != "transient"
        ]
```

- [ ] **Step 5: 跑测试，确认绿**

```
uv run --no-sync pytest services/orchestrator/tests/test_run_exit_reason.py -v
```
Expected: PASS

- [ ] **Step 6: 变异自证**

把 Step 4 改回 `if tool_failures:` 才写 → `test_tools_node_overwrites_last_batch_failures_with_an_empty_list` 必须红。确认红之后改回来，`git diff` 确认还原干净（替换文本不唯一会静默失败，先验唯一再验还原）。

- [ ] **Step 7: 提交**

```bash
git add services/orchestrator/src/orchestrator/state.py \
        services/orchestrator/src/orchestrator/graph_builder/builder.py \
        services/orchestrator/tests/test_run_exit_reason.py
git commit -m "feat(b85): tools 节点每批记下未解决的工具失败（B-85 ③ 第 1 步）"
```

---

### Task 2: `agent_node` 与 `tools` 节点盖 `exit_reason`

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`（`agent_node` 的 `budget_exhausted` 计算处 :613 与返回处 :1244/:1284；`tools` 节点写 `pending_approval` / `approval_outcome` 的同一处）
- Test: `services/orchestrator/tests/test_run_exit_reason.py`（续写）

**Interfaces:**
- Consumes: Task 1 的 `AgentState["exit_reason"]` 通道
- Produces: 六个封闭取值之一恒被写入。Task 3 读它。

- [ ] **Step 1: 写失败的测试**

```python
def test_budget_exhausted_stamps_the_specific_reason_not_text_response() -> None:
    """收尾轮的响应天然没有 tool_calls —— 不显式排除就会被 text_response 盖掉。

    这三种都走「一次无工具的收尾轮」再从 ``_should_continue`` 出去
    (builder.py:601-607),到路由那一步**已经分不出来**;能分辨的唯一时刻
    是 agent_node 在 :613 算出三个布尔的那一行。
    """
    assert _run_agent_node(step_count=40, max_steps=40)["exit_reason"] == "max_steps"
    assert _run_agent_node(no_progress_streak=3, max_no_progress=3)["exit_reason"] == "no_progress"
    assert _run_agent_node(token_tripped=True)["exit_reason"] == "token_budget"


def test_budget_reason_precedence_is_pinned() -> None:
    """三个同时为真时的取值顺序**钉死**,否则它随分支顺序隐式漂移。"""
    out = _run_agent_node(
        step_count=40, max_steps=40, no_progress_streak=3, max_no_progress=3, token_tripped=True
    )
    assert out["exit_reason"] == "max_steps"


def test_plain_stop_stamps_text_response() -> None:
    assert _run_agent_node(tool_calls=[])["exit_reason"] == "text_response"


def test_tools_node_stamps_approval_exits() -> None:
    assert _run_tools_node(pending_approval=_a_request())["exit_reason"] == "approval_pending"
    assert _run_tools_node(approval_outcome="rejected")["exit_reason"] == "approval_rejected"
```

- [ ] **Step 2: 跑测试，确认它红**

```
uv run --no-sync pytest services/orchestrator/tests/test_run_exit_reason.py -v -k exit_reason
```
Expected: FAIL —— `KeyError: 'exit_reason'`

- [ ] **Step 3: `agent_node` 盖章**

在 :613 算出 `budget_exhausted` 的紧后面求出理由，返回时带上：

```python
        budget_exhausted = (max_steps > 0 and step_count >= max_steps) or stuck or token_tripped
        # B-85 ③ —— 盖章要盖在**知道答案的那一行**。这三个布尔到了
        # ``_should_continue`` 就全看不见了(收尾轮把 tool_calls 剥掉,
        # 出口与自然结束逐字相同)。顺序钉死,别依赖分支书写顺序。
        budget_reason: str | None = (
            "max_steps" if (max_steps > 0 and step_count >= max_steps)
            else "no_progress" if stuck
            else "token_budget" if token_tripped
            else None
        )
```

返回处（:1244 / :1284 两个 `result_dict` 构造点都要）：

```python
            # B-85 ③ —— 预算用尽优先:收尾轮的响应没有 tool_calls,
            # 不排除就会被 text_response 覆盖掉。
            "exit_reason": budget_reason
            or ("text_response" if not _extract_tool_calls(response) else None),
```

（值为 `None` 时不写进 `result_dict` —— 这一轮还要继续，没到出口，盖章会误导。
用一个局部 `if` 而不是把 `None` 写进通道。）

- [ ] **Step 4: `tools` 节点盖 approval 两个**

在写 `pending_approval` / `approval_outcome = "rejected"` 的同一处，各加一行
`result_dict["exit_reason"] = "approval_pending"` / `"approval_rejected"`。
**不要**在 `_after_tools` 里写 —— 它是路由函数（Global Constraint 7）。

- [ ] **Step 5: 跑测试，确认绿**

```
uv run --no-sync pytest services/orchestrator/tests/test_run_exit_reason.py -v
```
Expected: PASS（含 Task 1 的三条）

- [ ] **Step 6: 提交**

```bash
git commit -am "feat(b85): 图的每个出口盖 exit_reason 章（B-85 ③ 第 2 步）"
```

---

### Task 3: 终局持久化（两列 + 从 graph snapshot 读）

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/<下一个序号>_run_completion.py` —— 序号取 `ls packages/expert-work-persistence/migrations/versions/ | sort | tail -1` 的下一个；**`revision` 字符串上限 32 字符**（踩过），`run_completion` 这个名字是按这条选的
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/agent_run.py:83` 附近
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py`（`RunInfo` 加两个字段）
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:157`（`set_status` 签名）+ `:682`（SQL 实现）
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/manager.py:355` 附近（透传）
- Modify: `services/orchestrator/src/orchestrator/sse.py:840-873`（从已有的 `snapshot.values` 里读）
- Test: `packages/expert-work-runtime/tests/test_run_completion_signal.py`（新建）

**Interfaces:**
- Consumes: Task 2 写入的 `AgentState["exit_reason"]`、Task 1 的 `last_batch_failures`
- Produces: `RunInfo.completed: bool | None` / `RunInfo.exit_reason: str | None`；`set_status(..., completed=..., exit_reason=...)`，两者与 `artifacts` 同款语义（`None` 不写，不清空既有值）

- [ ] **Step 1: 写失败的测试**

```python
def test_completed_is_false_when_the_last_tool_batch_still_had_failures() -> None:
    """B-85 那次的逐字重放:正常从 text_response 出口结束、status 仍是 success,
    但最后一批工具调用里有未解决的非 transient 失败 → completed=false。"""
    completed = compute_completed(
        exit_reason="text_response", last_batch_failures=[_non_transient("claim failed")]
    )
    assert completed is False


def test_completed_is_true_on_a_clean_text_response_exit() -> None:
    assert compute_completed(exit_reason="text_response", last_batch_failures=[]) is True


def test_every_non_text_response_exit_is_not_completed() -> None:
    """平台**主动**中止的,按定义就没跑完。"""
    for reason in ("max_steps", "no_progress", "token_budget",
                   "approval_pending", "approval_rejected"):
        assert compute_completed(exit_reason=reason, last_batch_failures=[]) is False
```

- [ ] **Step 2: 跑测试，确认它红**

```
uv run --no-sync pytest packages/expert-work-runtime/tests/test_run_completion_signal.py -v
```
Expected: FAIL —— `ImportError: cannot import name 'compute_completed'`

- [ ] **Step 3: 实现 `compute_completed`**

放在 `packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py`（与 `RunStatus` 同文件，它是这个判据的唯一定义处）：

```python
def compute_completed(*, exit_reason: str, last_batch_failures: Sequence[Any]) -> bool:
    """B-85 ③ —— run 做没做成。**只用客观事实,不推断模型意图**(spec §4)。

    `True` 要求两件都成立:
    * run 是因为**模型不再调工具**而结束的(``text_response``),不是被平台
      主动中止的(撞预算 / 挂审批 / 被否决);
    * **最后一批**工具调用里没有未解决的非 transient 失败。

    第二条就是 B-85 那次的形状:工具失败 → 模型不再动作 → 图正常收尾 →
    ``status=success`` 而零产物。注意这里判的是「结束得紧挨着一批失败」这个
    **事实**,不是「模型放弃了」这个**意图** —— 后者猜不准(模型合理地换了方法
    也长这样),而前者客观可判。
    """
    return exit_reason == "text_response" and not last_batch_failures
```

- [ ] **Step 4: 跑测试，确认绿**

```
uv run --no-sync pytest packages/expert-work-runtime/tests/test_run_completion_signal.py -v
```
Expected: PASS

- [ ] **Step 5: 迁移 + 列**

```python
# migrations/versions/<序号>_run_completion.py —— expand-only,只加列、不回填
def upgrade() -> None:
    op.add_column("agent_run", sa.Column("completed", sa.Boolean(), nullable=True))
    op.add_column("agent_run", sa.Column("exit_reason", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("agent_run", "exit_reason")
    op.drop_column("agent_run", "completed")
```

**刻意不回填**：`NULL` = 本改动上线前的老 run，正好对上「字段缺席 = 无记录」的对外口径（Global Constraint 3）。

模型侧（`agent_run.py`）：

```python
    #: B-85 ③ —— NULL = 本改动上线前的老 run(不是「没做成」)。
    completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 6: `set_status` 透传**

`store.py:157` 抽象签名与 `:682` SQL 实现各加两个 keyword-only 参数，**照 `artifacts` 的既有语义**：`None` 时不写（所以 → RUNNING 这类非终局转换不会清掉终局写下的值）。docstring 里把这条明写出来。`manager.py` 同样透传。

- [ ] **Step 7: `sse.py` 终局读 snapshot**

`sse.py:840-852` 已经在读 `snapshot = await graph.aget_state(effective_config)` 取 `pending_approval`。**在同一个 try 块里**多读两个键，然后传给 `set_status`：

```python
                exit_reason = snapshot.values.get("exit_reason")
                last_batch_failures = snapshot.values.get("last_batch_failures") or []
```

失败时的降级**照已有契约**：`aget_state` 抛异常时当作「没记录」（两个值留 `None`），
**不能让 run 失败** —— 这与上面那段注释写的「A failing `aget_state` degrades to
'not paused' … rather than failing the run」是同一条纪律。

```python
        await run_manager.set_status(
            run_id, final, artifacts=_manifest_snapshot(),
            completed=(
                compute_completed(exit_reason=exit_reason, last_batch_failures=last_batch_failures)
                if exit_reason is not None else None
            ),
            exit_reason=exit_reason,
        )
```

- [ ] **Step 8: 跑本地 integration（改了 SQL/约束就本地跑）**

```
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run --no-sync pytest packages/expert-work-runtime/tests -v -k "run_store or completion"
```
Expected: PASS

- [ ] **Step 9: 审计行也带上（spec §8 第 5 行的另一半）**

`_emit_run_end_audit`（`sse.py:1575`）现在写 `details={"run_id":…, "status":…}`。
加两个键即可，**不需要迁移**（`details` 是 JSON）：

```python
    details={
        "run_id": str(record.run_id),
        "status": status,
        # B-85 ③ —— 审计行自己能回答「这个 run 做成了没有」,不用回头 JOIN agent_run。
        # ``None`` 时不放键(与 end 帧同一条「缺席 ≠ false」口径)。
        **({"completed": completed} if completed is not None else {}),
        **({"exit_reason": exit_reason} if exit_reason else {}),
    },
```

`_emit_run_end_audit` 的签名相应加两个 keyword-only 参数（默认 `None`）。
**两个调用点（`sse.py:910` 与 `:946`）都要传** —— 只改一处的话，两条终局路径
的审计行字段集合会分叉，正是 `end_frame_data` 那条教训的同构版本。

写完跑一遍既有的审计断言：

```
uv run --no-sync pytest services/orchestrator/tests -v -k "audit and run"
```
Expected: PASS

- [ ] **Step 10: 提交**

```bash
git commit -am "feat(b85): completed/exit_reason 落到 agent_run 两个新列 + 审计行（B-85 ③ 第 3 步）"
```

---

### Task 4: 对外契约 —— `end` 帧加两个字段

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/base.py:154`（`publish_end` 签名）+ `memory.py:138,150,186`（实现与回放）
- Modify: `services/orchestrator/src/orchestrator/sse.py:1076`（`publish_end` 调用）、`:1764-1777`（消费）、`:1821`（`end_frame_data`）
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py:304, 484, 518`（三个构造点）
- Test: `services/control-plane/tests/test_end_frame_completion_fields.py`（新建）

**Interfaces:**
- Consumes: Task 3 写下的 `RunInfo.completed` / `.exit_reason`
- Produces: `end_frame_data(..., completed: bool | None = None, exit_reason: str | None = None)`

- [ ] **Step 1: 写失败的测试**

```python
def test_absent_not_null_for_an_old_run() -> None:
    """老 run 无记录 → 字段**缺席**。放 null 会被读成「确有其事的 false」。"""
    data = end_frame_data(run_id=_RID, status="success", completed=None, exit_reason=None)
    assert "completed" not in data and "exit_reason" not in data


def test_b85_shape_success_but_not_completed() -> None:
    data = end_frame_data(run_id=_RID, status="success", completed=False, exit_reason="text_response")
    assert data["status"] == "success", "status 一个字都不许动"
    assert data["completed"] is False


def test_both_sse_paths_emit_the_same_field_set() -> None:
    """四个构造点里漏传任何一个,两条流的 end 帧就分叉 ——
    ``end_frame_data`` 的 docstring 记着这里分叉过一次同类问题。"""
    live = _collect_end_frame(_stream_from_orchestrator())
    replay = _collect_end_frame(_stream_from_control_plane())
    assert set(live) == set(replay)
```

- [ ] **Step 2: 跑测试，确认它红**

```
uv run --no-sync pytest services/control-plane/tests/test_end_frame_completion_fields.py -v
```
Expected: FAIL —— `TypeError: end_frame_data() got an unexpected keyword argument 'completed'`

- [ ] **Step 3: `end_frame_data` 加两个参数**

```python
    if completed is not None:
        data["completed"] = completed
    if exit_reason is not None:
        data["exit_reason"] = exit_reason
```

docstring 里补一段，口径与 `artifacts` / `usage_by_model` 并列：

```
``completed`` / ``exit_reason``(B-85 ③)—— 「事做没做成」与「从哪个出口结束的」。
**与 ``status`` 正交**:``status="success"`` 且 ``completed=false`` 是合法且有意义的
组合,正是 B-85 那次(工具失败后模型不再动作,图正常收尾)。``None`` 时**字段缺席**
——缺席 = 本改动上线前的老 run,别当成「没做成」。
```

- [ ] **Step 4: `publish_end` 与四个构造点全传上**

- `publish_end(run_id, *, status, artifacts=None, completed=None, exit_reason=None)`，`memory.py` 存进 `end_*` 三个字段并在 `subscribe` 回放时放进 end 帧 `data`
- `sse.py:1076` 调用处传（值来自 Task 3 已经算好的那两个局部变量）
- `sse.py:1764` 与 `_run_event_stream.py:518` 从 `entry.data` 取
- `_run_event_stream.py:304`（库回放）与 `:484`（live 补库终局）从 run 行取

- [ ] **Step 5: 跑测试，确认绿**

```
uv run --no-sync pytest services/control-plane/tests/test_end_frame_completion_fields.py services/orchestrator/tests -v -k "end_frame or sse"
```
Expected: PASS

- [ ] **Step 6: 变异自证**

四个构造点里**任挑一个**把新参数删掉 → `test_both_sse_paths_emit_the_same_field_set` 必须红。四个都验一遍（这条不变式在四处各有四分之一，只测一处等于没测）。

- [ ] **Step 7: 提交**

```bash
git commit -am "feat(b85): end 帧带 completed/exit_reason，status 不动（B-85 ③ 第 4 步）"
```

---

### Task 5: 第二条腿 —— completion contract 提示词块

**Files:**
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py:1677`（`_assemble_system_prompt`）
- Test: `services/orchestrator/tests/test_completion_contract_prompt.py`（新建）

**Interfaces:**
- 与 A 腿**无数据依赖**，可与 Task 1-4 并行。

- [ ] **Step 1: 写失败的测试**

```python
def test_completion_contract_is_always_present() -> None:
    """平台级,无开关。opt-in 的开关有人会忘,忘了之后静默跑偏比统一的坏更糟。"""
    prompt = _assemble_system_prompt(base="你是一个助手。", skill_fragments=[])
    assert "<completion-contract>" in prompt


def test_contract_tells_the_model_to_say_what_is_blocked() -> None:
    prompt = _assemble_system_prompt(base="x", skill_fragments=[])
    block = prompt.split("<completion-contract>")[1].split("</completion-contract>")[0]
    assert "[blocked]" in block
    assert "缺什么" in block
```

- [ ] **Step 2: 跑测试，确认它红**

```
uv run --no-sync pytest services/orchestrator/tests/test_completion_contract_prompt.py -v
```
Expected: FAIL

- [ ] **Step 3: 加块**

```python
_COMPLETION_CONTRACT = """<completion-contract>
在每一项被要求的事都做完、或者被你显式标成 [blocked] 并说明缺什么之前，不要把任务当作已完成。
工具失败导致做不下去时，明说是哪一步、缺什么，不要用一段看起来完整的话把它盖过去。
</completion-contract>"""
```

拼进 `pieces`，位置与 `tool_use_enforcement` 同档（行为指令靠前）。
注意 `_assemble_system_prompt` 开头有一个「什么都没有就直接返回 `base`」的短路
（:1706-1717）—— 本块是**无条件**的，所以那个短路要一并调整，否则一个没有任何
skill / patch 的 agent 拿不到这段。**这条容易漏，测试第一条就是钉它的。**

- [ ] **Step 4: 跑测试，确认绿 + 跑既有提示词测试确认没打破**

```
uv run --no-sync pytest services/orchestrator/tests -v -k "prompt or assemble"
```
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git commit -am "feat(b85): 系统提示词加 completion contract，让模型自己说做不下去（B-85 ③ 第 5 步）"
```

---

### Task 6: 对外文档两处 + 通知对接方

**Files:**
- Modify: `docs/api/streaming-events.md`
- Modify: `apps/admin-ui/docs-site/guide/sse-events.md`
- Create: 通知稿（发出去即可，不入库）

**Interfaces:** 无代码依赖，但**必须在 Task 4 之后**——文档描述的是已经存在的字段。

- [ ] **Step 1: 两处文档都写 `end` 帧的两个新字段**

照既有 `usage_by_model` 那一节的写法：字段表穷举取值、写清缺席语义。`exit_reason`
六个取值逐个给出「什么时候会看到」。

- [ ] **Step 2: 明写 `status` 的真实含义**

这是本 task 的核心，不是附注：

> `status=success` 表示**这一轮跑完了、没有抛异常**，**不表示要做的事做成了**。
> 要判「事做成了」，读 `completed`。
> 一个 `status=success` 且 `completed=false` 的 run 是合法的、也是有意义的：
> 它通常意味着某个工具失败之后模型没有再继续。

- [ ] **Step 3: 按 style-guide 自检**

`docs/superpowers/specs/2026-08-17-external-docs-style-guide.md` —— 读者是第三方开发工程师，
大纲按读者任务排，字段表穷举取值，不造黑话。

- [ ] **Step 4: 通知对接方**

spec §10 写死了：这一步**不是可选的收尾，是交付物**。加了字段但对面不读，
等于又一次「把该做的事留给人」。通知里要有：新字段是什么、`status` 的真实含义、
建议他们把成功判据从 `status === "success"` 改成 `status === "success" && completed !== false`
（`!== false` 而不是 `=== true`，这样老 run 的字段缺席不会被误判成失败）。

- [ ] **Step 5: 提交**

```bash
git commit -am "docs(b85): 对外文档写清 completed/exit_reason 与 status 的真实含义（B-85 ③ 第 6 步）"
```

---

## 收尾（全部 task 完成后）

- [ ] 真栈验收：测试环境跑一个必然失败工具的 run，`end` 帧里 `completed=false` 且 `status=success`（spec §11 判据 7）
- [ ] ROADMAP `B-85` 行销案：③ 从「等用户拍板」改成已交付，并记下「判据从『零产物=放弃』改成『结束得紧挨着一批失败』」这条纠正
