# P-1 对外「重新生成 / 编辑重发」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对接方能对会话**最后一轮**调 `POST …/runs/{run_id}:regenerate` / `:edit`,旧轮原地标「已被取代」(读面可见、agent 看不见、计划回退),新一轮照常跑。

**Architecture:** B 方案:不分叉、不删消息。`supersede_run` 内核在 per-thread advisory lock 里用 checkpoint 历史(`aget_state_history(filter={"run_id"})`)划出旧轮的下标区间 `[s, e)`,一次 `aupdate_state(as_node="agent")` 把该区间每条消息按**原 id** 写回带 `expert_work_superseded_by` 标记的副本并把 `plan` 回退到轮前值;再顺序写三张 SQL 表(`agent_run.superseded_by_run_id` / `thread_message.superseded_by` / `thread_meta.message_count`)。构图侧在 `agent_node` 取到 `state["messages"]` 之后、working_window 之前整轮剔除带标记的消息。新一轮走既有 `spawn_run`,只多一个 `supersede=` 参数;`:regenerate` 用旧轮的 System+Human 两条消息原样(换 id、换戳)重放,`:edit` 用新 input 走普通路径。

**Tech Stack:** Python 3.13 / FastAPI / LangGraph(`add_messages` reducer、`AsyncPostgresSaver`)/ SQLAlchemy async + Alembic / pytest + testcontainers;admin-ui React + antd + vitest;docs-site VitePress。

**Spec:** `docs/superpowers/specs/2026-09-09-regenerate-edit-resend-design.md`(B 方案,spike `scratchpad/spike_p1_supersede.py` 5/5 已验)。

## 与 spec 的出入(全部在 main `4ac73c71` 工作树里重新核过)

spec 行号以 `723c5a40` 为准,中间合了 #1447/#1449/#1452/#1453/#1455/#1456/#1457。逐条核对结果(**以本表为准**):

| spec 写法 | 现状(`4ac73c71`) | 对计划的影响 |
|---|---|---|
| `orchestrator/state.py:183` add_messages | ✅ `:183` 不变 | — |
| `models/thread_message.py:22-44` | ✅ `:22-44` | 加 `superseded_by` 列(Task 2) |
| `models/agent_run.py:27-108` | 类体实际到 `:165`(索引/唯一约束在 116-163) | 两列加在 `agent_spec_sha256`(`:108`)之后 |
| `message_stamp.py:21-34` | `:21-23` 常量,`stamp_message` `:28-36`,`stamp_messages` `:38-40` | — |
| `transcript.py:93-111` read_messages | ✅ `:93-111` | — |
| `/messages` `external_sessions.py:211-280` | **`:243-312`**(`get_messages`) | Task 5 |
| `/items` `external_session_items.py:427-436` / `_group_messages_by_run :104-130` | ✅ `:431`(read_messages)/`:436`(分组调用)/`:104-130` | Task 1 下沉分组 |
| 控制台 `runs.py:1659-1690` | `get_thread_messages` 实际 `:1659-1757`;`list_thread_runs` `:1759-1850` | Task 5 两处都要加字段 |
| `thread_stats.py:32-45` | `:32-56`(`record` 在 40-56) | 不改;`extract_turns` 默认含被取代轮,口径自动一致 |
| `spawn_run :953-1206` / `build_run_graph_input :455-504` / 审批续跑 `:857-926` | ✅ `:953-1206` / `:455-504` / `:857-951` | Task 7 |
| `api/plan.py` `_WRITE_BLOCKED_STATUSES`「running/pending/paused…」 | **实际 = `frozenset({PENDING, RUNNING})`(`plan.py:46`),无 QUEUED 也无 PAUSED** | 忙碌集合改用 `external_sessions._ACTIVE_RUN_STATUSES`(`:129`,PENDING/QUEUED/RUNNING)的同一集合;PAUSED 单独判 |
| `trigger_delivery.py:59-75` advisory lock | classid 常量 `:54`,`delivery_thread_lock` **`:57-88`** | Task 4 照抄,classid **8620**(见下) |
| `builder.py:656` | 文件在 `services/orchestrator/src/orchestrator/graph_builder/builder.py`,`messages = list(state["messages"])` 在 **`:645`**;`:656` 是 pruner 那行 | Task 6 插在 `:645` 之后、`:655` pruner 之前 |
| `external_runs.py:169` `:cancel` | ✅ `:169` | Task 9 |
| 三张路由表 `test_external_only_gate.py:66` / `test_console_lockdown.py:251` / `test_external_path_param_nul_guard.py:502` | `_EXTERNAL_ROUTES` **`:67`** / `_EXTERNAL_AGENT_ROUTES` ✅ `:251` / **nul_guard `:513` 的 `_AGENTS_ROUTER_EXTERNAL_ROUTES` 只登记 `agents.py` 自己 router 上的四条路由,`external_runs.py` 的路由由 `tags=["external"]` 自动发现(`:427-445`),那张表不该加** | 第三张「表」的实际动作 = 在 nul_guard 加两条逐端点测试(照 `test_cancel_run_nul_agent_code_is_422` `:259`)。Task 10 |
| §2「ToolMessage 与 SystemMessage 没有戳」 | ✅ 全仓 `stamp_message(`/`stamp_messages(` 生产调用点恰 **3 处**:`api/runs.py:490`(本轮 Human)、`graph_builder/builder.py:2300`(`_stamp_agent_messages`,agent 节点产出)、`trigger_firing.py:279`(触发器种子);`tools_node` 零处 | 划轮只能靠 checkpoint 历史(取法 A) |
| §2「没有 per-thread run 互斥」 | ✅ `rg -n "status_code=409" api/runs.py api/agents.py api/plan.py api/sessions.py` 共 10 处,全是「会话非 active / 未绑 agent / 无草稿 / 审批已裁 / 取消已终局」,零处因「该会话有 run 在跑」拒建 run;`rg "THREAD_BUSY"` 全仓 0 | Task 4 新建闸 |
| §2「读面全部读最新 checkpoint」 | ✅ control-plane 读 checkpoint 的生产代码 **12 处**(`read_messages`/`read_turns`/`aget_tuple`/`aget_state`),`aget_state_history` **0 处**;清单在 Task 5 | 所有读面都会看到标记 |
| §3.1-4「同一事务」 | checkpoint 走 psycopg 连接池,SQL 走 SQLAlchemy session,**跨不了一个事务** | 改成:锁内先写 checkpoint、再顺序写三张表;`agent_run` 链接悬空(新 run 行不存在)视为未取代,可重做(Task 4) |
| §3.1-5「同一起始下标 s 的版本 > 5」 | 每次重发的新轮追加在末尾,**起始下标各不相同**;「同一轮的版本」= `agent_run.regenerated_from_run_id` 链 | 墓碑按链长计(Task 4) |
| §3.3「`:regenerate` 从被取代轮的 HumanMessage 取」 | 采纳,且连同该轮 SystemMessage 一起原样重放(换 id/换戳);queue 模式把两条消息序列化进 `enqueued_input["replay_messages"]` | Task 7;引出第五个错误码 `RUN_INPUT_UNAVAILABLE`(目标轮没留下检查点:构建前就失败的 run) |
| 审批续跑轮 | spec 未提:审批把一轮切成 PAUSED run + continuation run,continuation 的最早 checkpoint 是 `source="loop"` 不是 `"input"` | Task 4 沿 `approval.continuation_run_id` 往前串成「链」,链首才是 `source="input"`;链上每个 run 都标 `superseded_by_run_id` |
| 迁移号 | ✅ `ls migrations/versions/` 最新 `0151_backfill_approval_user_id.py`;P-2 占 `0152_feedback_run_scope` | 本线 `0153_agent_run_supersede`(24 字符 < 32),`down_revision="0152_feedback_run_scope"` → **P-2 PR1 先合** |
| advisory classid | 既有 1 / 2 / 8615 / 8616 / 8617 / 8618 / **8619 已被 `trigger_delivery.py:54` 与 `workspace_janitor.py:69` 同时占用**(各自注释都说自己独占,键串不同所以没撞;只记录不改) | 本线取 **8620** |
| `audit.py` Literal(#1457 动过) | 复用 `resource_type="session"` + `AuditAction.SESSION_WRITE` + `details["stage"]`,**不加新 Literal 值** | — |
| i18n | `en.ts` 有两个 `conversations_detail:`(`:396` 是 `TranslationKeys` 接口、`:3547` 是值),`zh-CN.ts:406` | Task 11 三处都加 |
| §4「缺省不出现(对接方按既有约定忽略未知字段)」 | 改成**始终出现、缺省 `null` / `false`**:与 P-2 的 `feedback: null` 同一约定,对接方已确认无 strict 解析;字段恒在,对接方不用区分「没这个字段」与「值为空」 | Task 5;spec §4 该句已在本 PR 同步改口 |

## Global Constraints

- 术语:**轮** = 一次 run;**被取代** = 仍在历史里、agent 看不见、界面划掉(spec 顶部)。
- 只对**最后一轮**;会话有 run 处于 PENDING/QUEUED/RUNNING → 409 `THREAD_BUSY`;目标轮 PAUSED → 409 `RUN_AWAITING_APPROVAL`;已取代 → 409 `RUN_ALREADY_SUPERSEDED`;非最后一轮 → 422 `RUN_NOT_LAST`;`:regenerate` 目标轮无检查点 → 422 `RUN_INPUT_UNAVAILABLE`(spec §4 四码 + 本计划新增一码,文档随 PR3/PR4 同步)。
- 同一逻辑轮旧版本最多留 **5** 份,超出的最老版本内容置墓碑(`content=""`、`expert_work_tombstone=True`),**不用 `RemoveMessage`**。
- `aupdate_state` 必须 `as_node="agent"`(spike:`"__start__"` 让 `next=("agent",)`)。
- 副作用不撤销、两轮都计费、SSE 无新帧、缺省字段不出现(对接方忽略未知字段已确认)。
- 权限:`external_only()` + `require("session","write")`;归属 `load_owned_run` 404 不泄露存在性;`Idempotency-Key` 与 `POST …/runs` 同语义;`on_disconnect=CONTINUE`。
- 标记键(common 唯一定义):`expert_work_superseded_by`(值 = 新 run_id 字符串)、`expert_work_superseded_at`(ISO8601)、`expert_work_tombstone`(bool)。
- 迁移 `0153_agent_run_supersede`,revision id ≤ 32 字符;`agent_run` 加 `superseded_by_run_id` / `regenerated_from_run_id`,`thread_message` 加 `superseded_by`,三列均 `UUID NULL`、无外键。
- 本地命令:测试 `uv run --no-sync pytest <path> -q`(集成测加 `-m integration`,先 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`);lint `uv run --no-sync ruff check <paths>` + `uv run --no-sync ruff format <paths>`;mypy CI 范围 = `uv run mypy packages services/orchestrator/src …`(**含 packages 下 tests**,lambda 挂 `no-untyped-call` 用具名函数;control-plane 不在 mypy 范围但照样写类型)。admin-ui:`pnpm typecheck`(裸 `tsc --noEmit` 恒绿不算)、`pnpm vitest run <file>`;改页面前 `rg <testid> apps/admin-ui/e2e/`。i18n 两 locale 同时加键,同 object 内不得重复键。
- 每条新断言按「实现改坏 → 红 → 改回 → 绿」自证;每个 Task 写明改坏哪一行、预期哪条断言红。
- 真栈验收只用探针 user `pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation` 与金丝雀 agent `release-canary`(user `canary:release`);**永不碰 `ai-health-plan` / `sop2-designer`**;API key 只经 stdin heredoc,永不落文件、永不上 argv。
- 不改 ROADMAP / overlays / Secrets;不起后台任务;不 `git checkout`/`stash` 丢工作。
- 对外文档按 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`(词表零禁用词、正文全角标点、表 ≤ 4 列、字段表穷举取值、读者 = 第三方开发工程师)。

---

## 文件结构总览

| 文件 | 职责 | Task |
|---|---|---|
| `packages/expert-work-common/src/expert_work/common/conversation_channel.py` | 标记常量 + `superseded_by()` / `is_tombstone()` 判定 + `VisibleTurn` 两字段 + `visible_turns(include_superseded=)` | 1, 3 |
| `packages/expert-work-common/src/expert_work/common/supersede.py`(新) | `group_messages_by_run`(从 `/items` 下沉)/ `filter_superseded_turns`(整轮过滤)/ `mark_superseded` / `tombstone_message` | 1 |
| `packages/expert-work-persistence/migrations/versions/0153_agent_run_supersede.py`(新) | 三列 | 2 |
| `packages/expert-work-persistence/src/expert_work/persistence/models/{agent_run,thread_message}.py` | ORM 列 | 2 |
| `packages/expert-work-persistence/src/expert_work/persistence/thread_message/{base,memory,sql}.py` | `MessageTurn` 两字段 + `mark_superseded` | 2 |
| `packages/expert-work-runtime/src/expert_work/runtime/runs/{schemas,store,manager}.py` | `RunInfo` 两字段、`RunStore.mark_superseded`、`create/enqueue(regenerated_from_run_id=)` | 2 |
| `services/control-plane/src/control_plane/transcript.py` | `extract_turns(include_superseded=)` 投影两字段 | 3 |
| `services/control-plane/src/control_plane/supersede.py`(新) | 锁 / 定轮 / 写入 / 墓碑 / 错误 | 4 |
| `services/control-plane/src/control_plane/api/{external_sessions,external_session_items,runs,conversations,external_runs}.py` | 读面字段 | 5 |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py:645` | 整轮过滤 | 6 |
| `services/control-plane/src/control_plane/api/runs.py`(`spawn_run`、`replay_graph_input`) + `run_queue_worker.py` | 新一轮 | 7 |
| `services/control-plane/src/control_plane/api/external_runs.py` + `api/agents.py`(抽两个共享函数) | 两端点 | 9 |
| 三张路由表测试 + `docs-site/guide/*.md` + `.vitepress/config.mts` | 登记 / 文档 | 10, 11 |
| `apps/admin-ui/src/{api,components/turn,components/console,pages}` + i18n | 折叠态 | 12 |
| `docs/runbooks/production-release.md` | 一句 | 13 |

---

### Task 0: 实施前核实(spec §8-1 / §8-2 / §8-4)并回填 spec

**Files:**
- Modify: `docs/superpowers/specs/2026-09-09-regenerate-edit-resend-design.md`(§8 三条划线 + 一句结论)
- Create(不入仓,scratchpad): `scratchpad/t0_compressor_probe.py`、`scratchpad/t0_history_timing.py`

**Interfaces:**
- Consumes: 无
- Produces: spec §8 的三条结论;Task 4/7 依赖 §8-4 的锁放置点

- [ ] **Step 1: §8-1 静态读码结论(先写下来,再用 Step 2 动态验证)**

读 `services/orchestrator/src/orchestrator/graph_builder/builder.py`:
- `:645` `messages = list(state["messages"])` 之后一切都是 prompt 视图(`:653` 注释「the checkpointed history is never rewritten (CM-C4)」);
- `:782-833` 压缩器 `messages = await context_compressor.compress(...)` 只重绑本地变量;
- agent_node 的两个返回字典 `update_mw`(`:1206-1226`)与 `update_plain`(`:1254-1263`)的 `"messages"` 只装 `persisted_messages` / `emit_messages` = LLM 回复(+ advisory / dispatch),**从不装压缩后的列表**;
- `context/compressor.py:732-736` 包出的 `<context-summary>` SystemMessage 只出现在 `compress()` 的返回值里。

静态结论:**压缩摘要不落检查点、无 run 戳;§6 的「摘要撤不回」顾虑不成立,supersede 时无需对摘要打标**。下一轮从原始历史重新压缩,被取代轮已被 Task 6 剔除,自然不进摘要。

- [ ] **Step 2: §8-1 动态验证 —— 真 graph + 内存 checkpointer,强制压缩一次,断言检查点里没有摘要**

写 `scratchpad/t0_compressor_probe.py`(不入仓):

```python
"""Task 0-①:压缩摘要是否落检查点。跑法:
uv run --no-sync pytest /private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad/t0_compressor_probe.py -q -s
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph


@dataclass
class _LLM:
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any) -> AIMessage:
        self.prompts.append(list(messages))
        return AIMessage(content=f"reply-{len(self.prompts)}")


class _AlwaysCompress:
    """最小 ContextCompressor 替身:每次都把中间换成一条摘要。"""

    def should_compress(self, messages: Sequence[BaseMessage]) -> bool:
        return len(messages) >= 3

    async def compress(self, messages: Sequence[BaseMessage], **_: Any) -> list[BaseMessage]:
        head, *_middle, tail = list(messages)
        return [head, SystemMessage(content="<context-summary>\nSUMMARY-MARK\n</context-summary>"), tail]


@pytest.mark.asyncio
async def test_summary_never_lands_in_checkpoint() -> None:
    llm = _LLM()
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry(), context_compressor=_AlwaysCompress())
        )
        cfg = {"configurable": {"thread_id": "t0", "tenant_id": "t", "run_id": "r1"}}
        await compiled.ainvoke({"messages": [SystemMessage(content="sys"), HumanMessage(content="U1")], "step_count": 0, "max_steps": 3}, config=cfg)
        cfg2 = {"configurable": {"thread_id": "t0", "tenant_id": "t", "run_id": "r2"}}
        await compiled.ainvoke({"messages": [SystemMessage(content="sys"), HumanMessage(content="U2")], "step_count": 0, "max_steps": 3}, config=cfg2)
        persisted = (await compiled.aget_state({"configurable": {"thread_id": "t0"}})).values["messages"]
    # 压缩确实发生过(第二轮 prompt 含摘要)……
    assert any("SUMMARY-MARK" in str(m.content) for m in llm.prompts[-1])
    # ……但检查点里一条摘要都没有
    assert not any("<context-summary>" in str(m.content) for m in persisted), [type(m).__name__ for m in persisted]
    print("\n[T0-1] checkpoint kinds:", [type(m).__name__ for m in persisted])
```

跑:`uv run --no-sync pytest <上面路径> -q -s`。预期 PASS 且打印的 kinds 里没有第三条 SystemMessage。若 `build_react_graph` 的 `context_compressor` 参数对替身有属性要求(它在 `builder.py:436` 类型注解为 `ContextCompressor | None`,只鸭子调用 `should_compress` / `compress`),按报错补属性,不改仓库代码。

- [ ] **Step 3: §8-2 生产版 `langgraph-checkpoint-postgres` 上 `filter={"run_id"}` 的 SQL 与耗时(测试环境 pod 内,只读)**

`aget_state_history(filter=…)` 落到 `AsyncPostgresSaver.alist`,SQL 形如 `SELECT … FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s AND metadata @> %s ORDER BY checkpoint_id DESC`。只用 psycopg 跑 `EXPLAIN (ANALYZE, BUFFERS)`,不 import checkpointer(它的 `setup()` 会 `CREATE TABLE IF NOT EXISTS`,虽是幂等但不算只读)。

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
kubectl get pods -A | grep control-plane          # 记下 NAMESPACE 与一个 Running 的 pod 名
kubectl -n <NAMESPACE> exec -i <POD> -- python - <<'PY'
import asyncio, json, time
import psycopg
from control_plane.settings import Settings

async def main() -> None:
    dsn = Settings().db_dsn.replace("+asyncpg", "")   # 永不打印
    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.cursor() as cur:
        await cur.execute("SELECT thread_id, count(*) FROM checkpoints GROUP BY thread_id ORDER BY 2 DESC LIMIT 1")
        thread_id, n = await cur.fetchone()
        await cur.execute(
            "SELECT metadata->>'run_id' FROM checkpoints WHERE thread_id=%s AND metadata ? 'run_id' ORDER BY checkpoint_id DESC LIMIT 1",
            (thread_id,),
        )
        (run_id,) = await cur.fetchone()
        t0 = time.perf_counter()
        await cur.execute(
            "EXPLAIN (ANALYZE, BUFFERS) SELECT checkpoint_id, parent_checkpoint_id FROM checkpoints "
            "WHERE thread_id=%s AND checkpoint_ns='' AND metadata @> %s::jsonb ORDER BY checkpoint_id DESC",
            (thread_id, json.dumps({"run_id": run_id})),
        )
        plan = [r[0] for r in await cur.fetchall()]
        dt = time.perf_counter() - t0
    print("longest thread checkpoints =", n, "| filtered query wall =", round(dt * 1000, 1), "ms")
    print("\n".join(plan))

asyncio.run(main())
PY
```

判据:最长会话上该查询 < 100 ms 且 plan 是 `Index Scan`/`Bitmap` 走 `(thread_id, checkpoint_ns, checkpoint_id)` 主键再按 `metadata @>` 过滤。若 > 100 ms 或 Seq Scan,把「`agent_run` 加 `base_checkpoint_id` 锚点列」作为 Task 4 的备选写进 spec §8-2 结论,本计划不做。

- [ ] **Step 4: §8-4 queue 模式 supersede 与 worker 认领的先后**

读 `services/control-plane/src/control_plane/run_queue_worker.py:172-232`:`run_once` → `self._runs.list_queued()` 只拿 `status='queued'` 的行 → `_claim_and_start` → `claim_queued` CAS(`runs/store.py:557`)→ `_execute`。worker **只可能看到已经存在的 QUEUED 行**。

结论(Task 7 照此实现):`spawn_run` 里 `supersede_thread_lock` 包住「`supersede_run` → `run_manager.enqueue` / `run_manager.create`」整段;QUEUED 行在 supersede 全部写完之后才 INSERT,worker 在此之前没有任何可认领的东西;stream 模式同理(`run_agent` task 在锁外起,但那时标记已落)。锁的放置点 = `api/runs.py` `spawn_run` 内 `run_id = uuid4()` 之后到两个分支的建行调用为止。

- [ ] **Step 5: 回填 spec §8 并提交**

把 §8-1 / §8-2 / §8-4 三条改成划线 + `**✅ 已核实(日期)**:<一句结论>`,格式照 §8-3 / §8-5。

```bash
git add docs/superpowers/specs/2026-09-09-regenerate-edit-resend-design.md
git commit -m "docs(spec): P-1 §8 三条实施前核实回填(摘要不落检查点 / history 过滤耗时 / queue 锁放置点)"
```

---

### Task 1: common 侧标记常量 + 按轮分组下沉 + 整轮过滤

**Files:**
- Modify: `packages/expert-work-common/src/expert_work/common/conversation_channel.py`(常量与判定加在 `HIDE_FROM_UI` 旁,`:20-31`;`__all__`)
- Create: `packages/expert-work-common/src/expert_work/common/supersede.py`
- Modify: `services/control-plane/src/control_plane/api/external_session_items.py:98-130`(删 `_stamped_run_id` / `_group_messages_by_run`,改 import;`:436` 调用改名)
- Test: `packages/expert-work-common/tests/test_supersede.py`(新)

**Interfaces:**
- Consumes: `conversation_channel.message_field`、`message_stamp.STAMP_RUN_ID`
- Produces(后续 Task 全部只认这些名字):
  - `conversation_channel.SUPERSEDED_BY = "expert_work_superseded_by"`、`SUPERSEDED_AT = "expert_work_superseded_at"`、`TOMBSTONE = "expert_work_tombstone"`
  - `conversation_channel.superseded_by(msg) -> str | None`、`is_superseded(msg) -> bool`、`is_tombstone(msg) -> bool`
  - `supersede.stamped_run_id(msg) -> str | None`
  - `supersede.group_messages_by_run(messages: Sequence[Any]) -> dict[str, list[Any]]`
  - `supersede.filter_superseded_turns(messages: Sequence[Any]) -> list[Any]`
  - `supersede.mark_superseded(msg: BaseMessage, *, new_run_id: str, now: datetime) -> BaseMessage`
  - `supersede.tombstone_message(msg: BaseMessage) -> BaseMessage`

- [ ] **Step 1: 写失败测试**

```python
# packages/expert-work-common/tests/test_supersede.py
"""P-1 —— 标记 / 分组 / 整轮过滤(common 唯一实现)。"""
from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import (
    SUPERSEDED_AT,
    SUPERSEDED_BY,
    TOMBSTONE,
    is_superseded,
    is_tombstone,
    superseded_by,
)
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.common.supersede import (
    filter_superseded_turns,
    group_messages_by_run,
    mark_superseded,
    stamped_run_id,
    tombstone_message,
)

NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def _turn(run_id: str, *, text: str, marked: bool = False) -> list[object]:
    """一轮:System(无戳)+ Human(戳)+ AI(tool_calls,戳)+ Tool(无戳)+ AI(final,戳)。"""
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content=f"U-{text}", additional_kwargs={STAMP_RUN_ID: run_id}),
        AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {}, "id": f"c-{run_id}", "type": "tool_call"}],
            additional_kwargs={STAMP_RUN_ID: run_id},
        ),
        ToolMessage(content="ok", tool_call_id=f"c-{run_id}"),
        AIMessage(content=f"A-{text}", additional_kwargs={STAMP_RUN_ID: run_id}),
    ]
    if marked:
        msgs = [mark_superseded(m, new_run_id="r-new", now=NOW) for m in msgs]
    return msgs


def test_mark_superseded_keeps_id_and_existing_kwargs() -> None:
    msg = HumanMessage(content="x", id="id-1", additional_kwargs={STAMP_RUN_ID: "r1"})
    out = mark_superseded(msg, new_run_id="r2", now=NOW)
    assert out.id == "id-1"
    assert out.additional_kwargs[STAMP_RUN_ID] == "r1"
    assert out.additional_kwargs[SUPERSEDED_BY] == "r2"
    assert out.additional_kwargs[SUPERSEDED_AT] == NOW.isoformat()
    assert superseded_by(out) == "r2" and is_superseded(out)
    assert msg.additional_kwargs.get(SUPERSEDED_BY) is None  # 原对象不动


def test_tombstone_clears_content_and_tool_calls_but_keeps_id_and_marks() -> None:
    ai = mark_superseded(
        AIMessage(content="A", id="id-9", tool_calls=[{"name": "t", "args": {}, "id": "c", "type": "tool_call"}]),
        new_run_id="r2",
        now=NOW,
    )
    stone = tombstone_message(ai)
    assert stone.id == "id-9" and stone.content == "" and stone.tool_calls == []
    assert stone.additional_kwargs[TOMBSTONE] is True
    assert superseded_by(stone) == "r2" and is_tombstone(stone)


def test_group_messages_by_run_attaches_tool_results_to_previous_stamped_run() -> None:
    msgs = [*_turn("r1", text="one"), *_turn("r2", text="two")]
    grouped = group_messages_by_run(msgs)
    assert set(grouped) == {"r1", "r2"}
    assert [type(m).__name__ for m in grouped["r2"]] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert stamped_run_id(msgs[3]) is None  # ToolMessage 无戳


def test_filter_drops_marked_turn_entirely_and_keeps_others() -> None:
    msgs = [*_turn("r1", text="one"), *_turn("r2", text="two", marked=True)]
    kept = filter_superseded_turns(msgs)
    assert kept == msgs[:5]


def test_filter_drops_unmarked_tool_result_when_its_run_is_marked() -> None:
    """同进同出:哪怕某条 ToolMessage 漏了标记,它所在轮的其它消息带标记就整轮剔除。"""
    turn = _turn("r2", text="two")
    marked = [mark_superseded(m, new_run_id="r-new", now=NOW) if i != 3 else m for i, m in enumerate(turn)]
    kept = filter_superseded_turns([*_turn("r1", text="one"), *marked])
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "c-r2" for m in kept)
    assert len(kept) == 5


def test_filter_drops_tombstones() -> None:
    stones = [tombstone_message(m) for m in _turn("r2", text="two", marked=True)]
    assert filter_superseded_turns([*_turn("r1", text="one"), *stones]) == _turn("r1", text="one")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest packages/expert-work-common/tests/test_supersede.py -q`
Expected: FAIL,`ImportError: cannot import name 'SUPERSEDED_BY'`。

- [ ] **Step 3: 在 `conversation_channel.py` 加常量与判定**

在 `SCHEDULED_DELIVERY` 常量之后(`:26`)加:

```python
#: P-1「已被取代」—— 值 = 取代它的新 run_id(字符串 UUID)。带这个标记的消息
#: 留在历史里、进读面,但 agent 的 prompt 视图整轮剔除(``supersede.filter_superseded_turns``)。
SUPERSEDED_BY = "expert_work_superseded_by"
#: 打标时刻,ISO8601。
SUPERSEDED_AT = "expert_work_superseded_at"
#: 墓碑:超过保留份数的最老版本,正文已清空、id 与下标保留。墓碑必然同时带 SUPERSEDED_BY。
TOMBSTONE = "expert_work_tombstone"
```

在 `is_hidden` 之后加:

```python
def superseded_by(msg: Any) -> str | None:
    """取代这条消息的新 run_id;没被取代就是 ``None``。"""
    value = _kwargs(msg).get(SUPERSEDED_BY)
    return value if isinstance(value, str) and value else None


def is_superseded(msg: Any) -> bool:
    return superseded_by(msg) is not None


def is_tombstone(msg: Any) -> bool:
    return bool(_kwargs(msg).get(TOMBSTONE))
```

`__all__` 加 `"SUPERSEDED_AT", "SUPERSEDED_BY", "TOMBSTONE", "is_superseded", "is_tombstone", "superseded_by"`(保持字母序)。

- [ ] **Step 4: 新建 `supersede.py`**

```python
"""P-1「重新生成 / 编辑重发」—— 按轮分组 + 整轮过滤 + 打标 / 墓碑,全平台唯一一份。

为什么在 common:``/items``(control-plane)按 run 分组条目,``agent_node``
(orchestrator)按轮剔除被取代消息,两处必须是同一个分组规则 —— 一轮的
AI(tool_calls) 与它的 ToolMessage 必须同进同出,否则孤儿 tool_call 厂商 400。
orchestrator 不能 import control-plane,所以规则住这里。标记常量与判定在
:mod:`conversation_channel`(与 ``HIDE_FROM_UI`` 同一处),本模块只做组合。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from expert_work.common.conversation_channel import (
    SUPERSEDED_AT,
    SUPERSEDED_BY,
    TOMBSTONE,
    is_superseded,
    message_field,
)
from expert_work.common.message_stamp import STAMP_RUN_ID

__all__ = [
    "filter_superseded_turns",
    "group_messages_by_run",
    "mark_superseded",
    "stamped_run_id",
    "tombstone_message",
]


def stamped_run_id(msg: Any) -> str | None:
    """写入侧盖的 ``expert_work_run_id``,没盖就是 ``None``。"""
    stamp = (message_field(msg, "additional_kwargs") or {}).get(STAMP_RUN_ID)
    return stamp if isinstance(stamp, str) else None


def group_messages_by_run(messages: Sequence[Any]) -> dict[str, list[Any]]:
    """按 ``run_id`` 戳把检查点消息分到各轮,保持原始顺序。

    * 盖了戳的消息归戳上那一轮。
    * 工具结果消息(``type == "tool"``)从来不盖戳(写入侧只给 agent 节点的
      助手消息与入口的用户消息盖戳),归到前一条已归属消息的那一轮 —— 工具
      结果结构上必定紧跟发起调用的那条助手消息。
    * 其余没盖戳的消息(每轮的 SystemMessage、上线前的老消息)归不到任何一轮,
      直接丢弃 —— 编一个归属会让它出现在错误的轮次里。
    """
    grouped: dict[str, list[Any]] = {}
    current: str | None = None
    for msg in messages:
        stamped = stamped_run_id(msg)
        if stamped is not None:
            current = stamped
        elif message_field(msg, "type") != "tool":
            continue
        if current is None:
            continue
        grouped.setdefault(current, []).append(msg)
    return grouped


def filter_superseded_turns(messages: Sequence[Any]) -> list[Any]:
    """agent 的 prompt 视图:剔除被取代的消息,并且**按轮整段剔除**。

    两条规则叠加:带 ``SUPERSEDED_BY`` 的消息一律不要(墓碑也带这个标记,一并
    剔除);此外,某一轮只要有任何一条带标记,这一轮里盖了戳的消息与它的工具
    结果全部不要 —— 防某条漏标的 ToolMessage 变成孤儿。每轮开头那条无戳的
    SystemMessage 只靠第一条规则(supersede 时按下标区间打标,它必然带标记)。
    """
    superseded_runs = {
        run_id
        for run_id, group in group_messages_by_run(messages).items()
        if any(is_superseded(m) for m in group)
    }
    out: list[Any] = []
    current: str | None = None
    for msg in messages:
        stamped = stamped_run_id(msg)
        if stamped is not None:
            current = stamped
        if is_superseded(msg):
            continue
        member = stamped is not None or message_field(msg, "type") == "tool"
        if member and current is not None and current in superseded_runs:
            continue
        out.append(msg)
    return out


def mark_superseded(msg: BaseMessage, *, new_run_id: str, now: datetime) -> BaseMessage:
    """返回带「已被取代」标记的**新**消息,原 id 与既有 kwargs 原样保留(不可变约定)。

    id 必须保留:``add_messages`` 只按 id 原地替换,丢了 id 会被当新消息追加
    (spike 反证:15 条而非 10 条)。
    """
    merged = {**msg.additional_kwargs, SUPERSEDED_BY: new_run_id, SUPERSEDED_AT: now.isoformat()}
    return msg.model_copy(update={"additional_kwargs": merged})


def tombstone_message(msg: BaseMessage) -> BaseMessage:
    """正文置空、tool_calls 清空、打 TOMBSTONE;id / 下标 / 既有标记不动。"""
    update: dict[str, Any] = {
        "content": "",
        "additional_kwargs": {**msg.additional_kwargs, TOMBSTONE: True},
    }
    if isinstance(msg, AIMessage):
        update["tool_calls"] = []
    return msg.model_copy(update=update)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --no-sync pytest packages/expert-work-common/tests/test_supersede.py -q`
Expected: 6 passed。

- [ ] **Step 6: 变异自证**

把 `filter_superseded_turns` 里 `if member and current is not None and current in superseded_runs:` 整行注释掉 → 跑 → 预期 `test_filter_drops_unmarked_tool_result_when_its_run_is_marked` 红(kept 里出现 `c-r2` 的 ToolMessage);改回 → 绿。再把 `mark_superseded` 的 `update` 里加 `"id": None` → 预期 `test_mark_superseded_keeps_id_and_existing_kwargs` 红;改回。

- [ ] **Step 7: `/items` 改用共享分组**

`services/control-plane/src/control_plane/api/external_session_items.py`:删掉 `:98-130` 的 `_stamped_run_id` 与 `_group_messages_by_run`;`:46` 的 `from expert_work.common.message_stamp import STAMP_RUN_ID` 若无其它使用则删;加 `from expert_work.common.supersede import group_messages_by_run`;`:436` 改为 `by_run = group_messages_by_run(messages)`。

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_session_items.py services/control-plane/tests/test_conversation_items_parity.py -q`
Expected: 全绿(行为未变)。

- [ ] **Step 8: lint / mypy / 提交**

```bash
uv run --no-sync ruff check packages/expert-work-common services/control-plane/src/control_plane/api/external_session_items.py
uv run --no-sync ruff format packages/expert-work-common services/control-plane/src/control_plane/api/external_session_items.py
uv run --no-sync mypy packages/expert-work-common
git add packages/expert-work-common services/control-plane/src/control_plane/api/external_session_items.py
git commit -m "feat(common): P-1 被取代标记 + 按轮分组下沉 common + 整轮过滤"
```

---

### Task 2: 迁移 0153 + ORM 列 + RunInfo / RunStore / RunManager / ThreadMessageStore

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0153_agent_run_supersede.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/agent_run.py:108`(之后加两列)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/thread_message.py:35`(之后加一列)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/thread_message/base.py`(`MessageTurn` 两字段、抽象 `mark_superseded`)、`memory.py`、`sql.py`
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py:136-150`(`RunInfo` 两字段)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`(抽象 `mark_superseded` `:452` 之后;`InMemoryRunStore` `:883` 之后;`_row_to_dto` `:1036`;`SqlRunStore.create` `:1068`;`SqlRunStore.mark_superseded` `:1484` 之后)
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/manager.py`(`RunRecord` `:43`、`_record_to_info` `:88`、`create` `:175`、`enqueue` `:234`)
- Test: `packages/expert-work-runtime/tests/test_run_store_supersede.py`(新)、`packages/expert-work-persistence/tests/test_thread_message_store.py`(加)、`packages/expert-work-persistence/tests/test_migration_0153_supersede.py`(新,integration)

**Interfaces:**
- Consumes: 无
- Produces:
  - `RunInfo.superseded_by_run_id: UUID | None = None`、`RunInfo.regenerated_from_run_id: UUID | None = None`
  - `RunStore.mark_superseded(*, run_id: UUID, tenant_id: UUID, superseded_by_run_id: UUID) -> bool`
  - `RunManager.create(..., regenerated_from_run_id: UUID | None = None)`、`RunManager.enqueue(..., regenerated_from_run_id: UUID | None = None)`
  - `MessageTurn.superseded_by: UUID | None = None`、`MessageTurn.tombstone: bool = False`
  - `ThreadMessageStore.mark_superseded(*, thread_id: UUID, tenant_id: UUID, seq_from: int, seq_to: int, superseded_by: UUID) -> int`(`seq_from` 含、`seq_to` 不含;返回更新行数)

- [ ] **Step 1: 写失败测试(runtime in-memory)**

```python
# packages/expert-work-runtime/tests/test_run_store_supersede.py
"""P-1 —— agent_run 两列在 RunInfo / RunStore / RunManager 上的贯通(内存实现)。"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunManager, RunStatus


def _info(*, run_id, tenant_id, thread_id, regenerated_from=None) -> RunInfo:
    now = datetime.now(UTC)
    return RunInfo(
        run_id=run_id, tenant_id=tenant_id, thread_id=thread_id, user_id=None,
        status=RunStatus.SUCCESS, on_disconnect=DisconnectMode.CONTINUE, is_resume=False,
        error=None, created_at=now, updated_at=now, finished_at=now,
        regenerated_from_run_id=regenerated_from,
    )


@pytest.mark.asyncio
async def test_mark_superseded_sets_link_and_is_tenant_scoped() -> None:
    store = InMemoryRunStore()
    tenant, thread, old, new = uuid4(), uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=old, tenant_id=tenant, thread_id=thread))
    assert await store.mark_superseded(run_id=old, tenant_id=uuid4(), superseded_by_run_id=new) is False
    assert await store.mark_superseded(run_id=old, tenant_id=tenant, superseded_by_run_id=new) is True
    row = await store.get(run_id=old, tenant_id=tenant)
    assert row is not None and row.superseded_by_run_id == new


@pytest.mark.asyncio
async def test_manager_create_and_enqueue_persist_regenerated_from() -> None:
    store = InMemoryRunStore()
    manager = RunManager(store=store)
    tenant, thread, old = uuid4(), uuid4(), uuid4()
    r1, r2 = uuid4(), uuid4()
    await manager.create(run_id=r1, thread_id=thread, tenant_id=tenant, regenerated_from_run_id=old)
    await manager.enqueue(run_id=r2, thread_id=thread, tenant_id=tenant, enqueued_input={"input": "x"}, regenerated_from_run_id=old)
    for rid in (r1, r2):
        row = await store.get(run_id=rid, tenant_id=tenant)
        assert row is not None and row.regenerated_from_run_id == old
    plain = uuid4()
    await manager.create(run_id=plain, thread_id=thread, tenant_id=tenant)
    assert (await store.get(run_id=plain, tenant_id=tenant)).regenerated_from_run_id is None
```

在 `packages/expert-work-persistence/tests/test_thread_message_store.py` 末尾加:

```python
@pytest.mark.asyncio
async def test_mark_superseded_updates_only_the_seq_range() -> None:
    store = InMemoryThreadMessageStore()
    thread, tenant, new_run = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    turns = [MessageTurn(seq=s, role="user" if s % 2 else "assistant", content=f"m{s}") for s in (1, 3, 5, 7)]
    await store.sync_thread(thread_id=thread, tenant_id=tenant, turns=turns, synced_at=now)
    assert await store.mark_superseded(thread_id=thread, tenant_id=tenant, seq_from=3, seq_to=6, superseded_by=new_run) == 2
    assert await store.mark_superseded(thread_id=thread, tenant_id=uuid4(), seq_from=0, seq_to=99, superseded_by=new_run) == 0
    marked = {seq: turn.superseded_by for (tid, seq), (_t, turn) in store._turns.items() if tid == thread}
    assert marked == {1: None, 3: new_run, 5: new_run, 7: None}
```

(该文件已有 `InMemoryThreadMessageStore` / `MessageTurn` / `datetime` / `uuid4` 的 import 则不重复加。)

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest packages/expert-work-runtime/tests/test_run_store_supersede.py packages/expert-work-persistence/tests/test_thread_message_store.py -q`
Expected: FAIL,`TypeError: RunInfo.__init__() got an unexpected keyword argument 'regenerated_from_run_id'` 与 `AttributeError: 'InMemoryThreadMessageStore' object has no attribute 'mark_superseded'`。

- [ ] **Step 3: 迁移文件**

```python
# packages/expert-work-persistence/migrations/versions/0153_agent_run_supersede.py
"""P-1 重新生成 / 编辑重发 —— agent_run 两列 + thread_message 一列。

``agent_run.superseded_by_run_id``:这一轮被哪个新 run 取代(NULL = 未被取代)。
``agent_run.regenerated_from_run_id``:这一轮是对哪个旧 run 的重新生成 / 编辑
重发(NULL = 普通轮)。同一逻辑轮的版本链 = 沿 ``regenerated_from_run_id`` 回溯,
墓碑上限按链长计。
``thread_message.superseded_by``:搜索镜像上的同一标记 —— 镜像写入是
``ON CONFLICT (thread_id, seq) DO NOTHING``,永远学不到检查点上后加的标记,
所以 supersede 时显式 UPDATE 这一列。

三列都不加外键:新 run 行在旧轮打标**之后**才 INSERT(锁内顺序写),外键会
逼出错误的写入顺序。

Revision ID: 0153_agent_run_supersede
Revises: 0152_feedback_run_scope
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0153_agent_run_supersede"
down_revision: str | Sequence[str] | None = "0152_feedback_run_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("superseded_by_run_id", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("agent_run", sa.Column("regenerated_from_run_id", PG_UUID(as_uuid=True), nullable=True))
    op.add_column("thread_message", sa.Column("superseded_by", PG_UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("thread_message", "superseded_by")
    op.drop_column("agent_run", "regenerated_from_run_id")
    op.drop_column("agent_run", "superseded_by_run_id")
```

`down_revision` 指向 P-2 的 `0152_feedback_run_scope`:**本 Task 必须在 P-2 PR1 合并、本分支 rebase 之后才能跑集成测**(见文末波次表)。

- [ ] **Step 4: ORM 列**

`models/agent_run.py`,`agent_spec_sha256`(`:108`)之后:

```python
    # P-1 —— 被哪个新 run 取代 / 是哪个旧 run 的重发。无外键,理由见迁移 0153。
    superseded_by_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    regenerated_from_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
```

`models/thread_message.py`,`content`(`:35`)之后:

```python
    # P-1 —— 镜像上的「已被取代」标记(值 = 新 run_id)。检查点是真相,这一列
    # 由 supersede 显式 UPDATE 同步(DO NOTHING 学不到)。
    superseded_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
```

- [ ] **Step 5: `MessageTurn` + `ThreadMessageStore.mark_superseded` 三实现**

`thread_message/base.py` `MessageTurn` 末尾加:

```python
    #: P-1 —— 取代这条消息的新 run;``None`` = 未被取代。
    superseded_by: UUID | None = None
    #: P-1 —— 墓碑(正文已清理),此时 ``content == ""``。
    tombstone: bool = False
```

抽象类加:

```python
    @abc.abstractmethod
    async def mark_superseded(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        seq_from: int,
        seq_to: int,
        superseded_by: UUID,
    ) -> int:
        """把镜像里 ``seq ∈ [seq_from, seq_to)`` 的行标成被 ``superseded_by`` 取代,返回更新行数。

        镜像可能落后于检查点(sweep 还没跑到),更新 0 行不是错误 —— sweep
        之后写入的行也拿不到标记,所以镜像的这一列只是尽力而为的搜索/审计
        辅助,真相永远在检查点。
        """
```

`memory.py`:

```python
    async def mark_superseded(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        seq_from: int,
        seq_to: int,
        superseded_by: UUID,
    ) -> int:
        updated = 0
        for (tid, seq), (row_tenant, turn) in list(self._turns.items()):
            if tid != thread_id or row_tenant != tenant_id or not (seq_from <= seq < seq_to):
                continue
            self._turns[(tid, seq)] = (row_tenant, replace(turn, superseded_by=superseded_by))
            updated += 1
        return updated
```

(`from dataclasses import replace`。)`sql.py`:

```python
    async def mark_superseded(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        seq_from: int,
        seq_to: int,
        superseded_by: UUID,
    ) -> int:
        async with self._sf() as session:
            result = await session.execute(
                update(ThreadMessageRow)
                .where(
                    ThreadMessageRow.thread_id == thread_id,
                    ThreadMessageRow.tenant_id == tenant_id,
                    ThreadMessageRow.seq >= seq_from,
                    ThreadMessageRow.seq < seq_to,
                )
                .values({"superseded_by": superseded_by})
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
```

(`from sqlalchemy import exists, select, update`。)

- [ ] **Step 6: `RunInfo` / `RunStore` / `RunManager`**

`runs/schemas.py` `RunInfo` 末尾(`agent_spec_sha256` 之后):

```python
    #: P-1 —— 这一轮被哪个新 run 取代;``None`` = 未被取代。
    superseded_by_run_id: UUID | None = None
    #: P-1 —— 这一轮是对哪个旧 run 的重新生成 / 编辑重发;``None`` = 普通轮。
    regenerated_from_run_id: UUID | None = None
```

`runs/store.py` 抽象类,`set_agent_spec_sha256` 之后:

```python
    @abc.abstractmethod
    async def mark_superseded(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        superseded_by_run_id: UUID,
    ) -> bool:
        """P-1 —— 记下这一轮被 ``superseded_by_run_id`` 取代。幂等覆盖;
        返回 ``True`` iff 行存在且同租户(跨租户探测返回 ``False`` 以隐藏存在性)。"""
```

`InMemoryRunStore`(照 `set_agent_spec_sha256` `:883-894`):

```python
    async def mark_superseded(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        superseded_by_run_id: UUID,
    ) -> bool:
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[run_id] = replace(row, superseded_by_run_id=superseded_by_run_id)
        return True
```

`_row_to_dto`(`:1036`)末尾加 `superseded_by_run_id=row.superseded_by_run_id, regenerated_from_run_id=row.regenerated_from_run_id,`;`SqlRunStore.create`(`:1068`)的 `AgentRunRow(...)` 加 `superseded_by_run_id=info.superseded_by_run_id, regenerated_from_run_id=info.regenerated_from_run_id,`;`SqlRunStore.mark_superseded` 照 `set_agent_spec_sha256`(`:1484-1498`)把 `.values({"superseded_by_run_id": superseded_by_run_id})`。

`runs/manager.py`:`RunRecord` 加字段 `regenerated_from_run_id: UUID | None = None`(放 `trace_id` 之后);`_record_to_info` 加 `regenerated_from_run_id=record.regenerated_from_run_id`;`create(...)` 签名加 `regenerated_from_run_id: UUID | None = None` 并传给 `RunRecord(...)`;`enqueue(...)` 签名加同名参数并传给 `RunInfo(...)`。

- [ ] **Step 7: 跑测试确认通过**

Run: `uv run --no-sync pytest packages/expert-work-runtime/tests/test_run_store_supersede.py packages/expert-work-persistence/tests/test_thread_message_store.py packages/expert-work-runtime/tests -q -m "not integration"`
Expected: 全绿。

- [ ] **Step 8: 变异自证**

`InMemoryRunStore.mark_superseded` 里 `row.tenant_id != tenant_id` 改成 `False` → `test_mark_superseded_sets_link_and_is_tenant_scoped` 第一条断言红;改回。`_record_to_info` 去掉 `regenerated_from_run_id=` → `test_manager_create_and_enqueue_persist_regenerated_from` 红;改回。`memory.py mark_superseded` 的条件 `not (seq_from <= seq < seq_to)` 改成 `not (seq_from <= seq)`(去掉上界)→ seq 7 也被标,镜像测试 `marked == {...}` 红;改回。

- [ ] **Step 9: 集成测(真 Postgres:迁移 + SQL 两个 store)**

```python
# packages/expert-work-persistence/tests/test_migration_0153_supersede.py
"""迁移 0153 三列 + SqlRunStore / SqlThreadMessageStore 的 P-1 写读。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from testcontainers.postgres import PostgresContainer

from expert_work.persistence.database import DatabaseConfig, create_async_engine_from_config, create_async_session_factory
from expert_work.persistence.thread_message import MessageTurn, SqlThreadMessageStore
from expert_work.persistence.thread_meta import SqlThreadMetaStore
from expert_work.protocol import ThreadMeta
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.runs.store import SqlRunStore

pytestmark = pytest.mark.integration
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _async_dsn(container: PostgresContainer) -> str:
    return str(container.get_connection_url()).replace("+psycopg2", "+asyncpg")


def _upgrade(container: PostgresContainer) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", str(container.get_connection_url()))
    command.upgrade(cfg, "head")


@pytest.mark.asyncio
async def test_0153_columns_and_sql_stores_round_trip(postgres_container: PostgresContainer) -> None:
    _upgrade(postgres_container)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container), pgbouncer_mode=False))
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("agent_run")})
            tm_cols = await conn.run_sync(lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("thread_message")})
        assert {"superseded_by_run_id", "regenerated_from_run_id"} <= cols
        assert "superseded_by" in tm_cols

        sf = create_async_session_factory(engine)
        runs, msgs, threads = SqlRunStore(sf), SqlThreadMessageStore(sf), SqlThreadMetaStore(sf)
        tenant, thread, old, new = uuid4(), uuid4(), uuid4(), uuid4()
        now = datetime.now(UTC)
        await threads.create(ThreadMeta(thread_id=thread, tenant_id=tenant, created_at=now))
        await runs.create(RunInfo(run_id=old, tenant_id=tenant, thread_id=thread, user_id=None, status=RunStatus.SUCCESS,
                                  on_disconnect=DisconnectMode.CONTINUE, is_resume=False, error=None,
                                  created_at=now, updated_at=now, finished_at=now))
        await runs.create(RunInfo(run_id=new, tenant_id=tenant, thread_id=thread, user_id=None, status=RunStatus.PENDING,
                                  on_disconnect=DisconnectMode.CONTINUE, is_resume=True, error=None,
                                  created_at=now, updated_at=now, finished_at=None, regenerated_from_run_id=old))
        assert await runs.mark_superseded(run_id=old, tenant_id=tenant, superseded_by_run_id=new) is True
        assert (await runs.get(run_id=old, tenant_id=tenant)).superseded_by_run_id == new
        assert (await runs.get(run_id=new, tenant_id=tenant)).regenerated_from_run_id == old

        await msgs.sync_thread(thread_id=thread, tenant_id=tenant, synced_at=now,
                               turns=[MessageTurn(seq=s, role="user", content=f"m{s}") for s in (1, 6, 8)])
        assert await msgs.mark_superseded(thread_id=thread, tenant_id=tenant, seq_from=5, seq_to=10, superseded_by=new) == 2
    finally:
        await engine.dispose()
```

`SqlThreadMetaStore.create` / `ThreadMeta` 的必填字段以 `packages/expert-work-persistence/tests/test_sql_thread_meta_store.py` 现有用法为准(那份测试建行的写法照抄)。mypy 扫 packages/tests:`conn.run_sync(lambda …)` 是对**有类型**的 `run_sync` 的调用,不触发 `no-untyped-call`;若 CI 仍报,改成具名函数 `def _columns(sync_conn: Connection, table: str) -> set[str]`。

Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run --no-sync pytest packages/expert-work-persistence/tests/test_migration_0153_supersede.py -q -m integration`
Expected: 1 passed。变异:迁移里注释掉 `thread_message` 那行 `add_column` → 断言 `"superseded_by" in tm_cols` 红;改回。

- [ ] **Step 10: lint / mypy / 提交**

```bash
uv run --no-sync ruff check packages && uv run --no-sync ruff format packages
uv run --no-sync mypy packages
git add packages/expert-work-persistence packages/expert-work-runtime
git commit -m "feat(persistence): P-1 迁移 0153 —— agent_run 取代/重发链接列 + thread_message 镜像标记 + store/manager 贯通"
```

---

### Task 3: `visible_turns` / `extract_turns` 投影「被取代」与「墓碑」

**Files:**
- Modify: `packages/expert-work-common/src/expert_work/common/conversation_channel.py:119-172`(`VisibleTurn`、`visible_turns`)
- Modify: `services/control-plane/src/control_plane/transcript.py:63-91`(`extract_turns`)、`:114-141`(`read_turns` 透传)
- Test: `packages/expert-work-common/tests/test_conversation_channel_superseded.py`(新)、`services/control-plane/tests/test_transcript_extract.py`(加)

**Interfaces:**
- Consumes: Task 1 的常量/判定;Task 2 的 `MessageTurn` 两字段
- Produces:
  - `VisibleTurn.superseded_by: str | None = None`、`VisibleTurn.tombstone: bool = False`
  - `visible_turns(raw, *, include_hidden=True, include_superseded=True)`;`include_superseded=False` 时被取代消息(含墓碑)不产出
  - `extract_turns(raw, *, include_hidden=True, include_superseded=True)` → `MessageTurn(superseded_by=UUID|None, tombstone=…)`
  - `read_turns(checkpointer, thread_id, *, include_hidden=True, include_superseded=True)`
  - 墓碑投影规则:`text == ""`、`channel=None`、`tombstone=True`,**不参与** final/commentary 判定,按 `seq` 排回原位

- [ ] **Step 1: 写失败测试**

```python
# packages/expert-work-common/tests/test_conversation_channel_superseded.py
from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage

from expert_work.common.conversation_channel import visible_turns
from expert_work.common.supersede import mark_superseded, tombstone_message

NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def _thread() -> list[object]:
    old = [mark_superseded(m, new_run_id="r3", now=NOW) for m in (HumanMessage(content="U2"), AIMessage(content="A2"))]
    return [HumanMessage(content="U1"), AIMessage(content="A1"), *old, HumanMessage(content="U3"), AIMessage(content="A3")]


def test_default_keeps_superseded_turns_with_marker() -> None:
    turns = visible_turns(_thread())
    assert [(t.seq, t.role, t.superseded_by) for t in turns] == [
        (0, "user", None), (1, "assistant", None), (2, "user", "r3"), (3, "assistant", "r3"), (4, "user", None), (5, "assistant", None)
    ]
    assert [t.channel for t in turns if t.role == "assistant"] == ["final", "final", "final"]


def test_include_superseded_false_drops_them_without_moving_seq() -> None:
    turns = visible_turns(_thread(), include_superseded=False)
    assert [t.seq for t in turns] == [0, 1, 4, 5]


def test_tombstone_is_emitted_empty_and_does_not_steal_final() -> None:
    msgs = _thread()
    msgs[2], msgs[3] = tombstone_message(msgs[2]), tombstone_message(msgs[3])
    turns = visible_turns(msgs)
    stones = [t for t in turns if t.tombstone]
    assert [(t.seq, t.role, t.text, t.channel, t.superseded_by) for t in stones] == [
        (2, "user", "", None, "r3"), (3, "assistant", "", None, "r3")
    ]
    # A1 仍是自己段落的 final(墓碑不算「后面还有一行」)
    assert next(t for t in turns if t.seq == 1).channel == "final"
    assert [t.seq for t in turns] == [0, 1, 2, 3, 4, 5]
```

`services/control-plane/tests/test_transcript_extract.py` 末尾加:

```python
def test_extract_turns_projects_superseded_by_and_tombstone() -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    from expert_work.common.supersede import mark_superseded, tombstone_message

    new_run = UUID("11111111-2222-4333-8444-555555555555")
    marked = mark_superseded(AIMessage(content="A1"), new_run_id=str(new_run), now=datetime(2026, 9, 10, tzinfo=UTC))
    stone = tombstone_message(mark_superseded(HumanMessage(content="U0"), new_run_id=str(new_run), now=datetime(2026, 9, 10, tzinfo=UTC)))
    turns = extract_turns([stone, marked, HumanMessage(content="U2")])
    assert [(t.seq, t.superseded_by, t.tombstone, t.content) for t in turns] == [
        (0, new_run, True, ""), (1, new_run, False, "A1"), (2, None, False, "U2")
    ]
    assert [t.seq for t in extract_turns([stone, marked, HumanMessage(content="U2")], include_superseded=False)] == [2]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest packages/expert-work-common/tests/test_conversation_channel_superseded.py services/control-plane/tests/test_transcript_extract.py -q`
Expected: FAIL,`TypeError: visible_turns() got an unexpected keyword argument 'include_superseded'`。

- [ ] **Step 3: 实现 `visible_turns`**

`VisibleTurn` 末尾加两字段:

```python
    #: P-1 —— 取代这条消息的新 run_id;``None`` = 未被取代。
    superseded_by: str | None = None
    #: P-1 —— 墓碑:正文已清理,``text == ""``、``channel is None``。
    tombstone: bool = False
```

`visible_turns` 整函数替换为:

```python
def visible_turns(
    raw_messages: Sequence[Any], *, include_hidden: bool = True, include_superseded: bool = True
) -> list[VisibleTurn]:
    """抽出带文本的用户/助手轮次,并给助手轮定 ``channel``。

    (原 docstring 全文保留)……

    ``include_superseded=True``(默认)= 被取代的消息照常产出并带 ``superseded_by``,
    墓碑产出为空文本、``tombstone=True``、``channel=None``,且**不参与**
    final/commentary 判定(它不是「后面还有一行」);``False`` = 两者都不产出,
    其余消息的 ``seq`` 不变(下标是镜像的主键,不能因视图而漂)。
    """
    collected: list[tuple[int, str, str, bool, bool, str | None]] = []
    stones: list[VisibleTurn] = []
    for seq, msg in enumerate(raw_messages):
        mtype = message_field(msg, "type")
        if mtype not in ("human", "ai"):
            continue
        if not include_hidden and is_hidden(msg):
            continue
        by = superseded_by(msg)
        if by is not None and not include_superseded:
            continue
        if is_tombstone(msg):
            role = "user" if mtype == "human" else "assistant"
            stones.append(VisibleTurn(seq=seq, role=role, text="", channel=None, superseded_by=by, tombstone=True))
            continue
        text = message_text(message_field(msg, "content", ""))
        if not text.strip():
            continue
        collected.append((seq, mtype, text, has_tool_calls(msg), opens_segment(msg), by))

    out: list[VisibleTurn] = []
    for i, (seq, mtype, text, tool_calls, _opens, by) in enumerate(collected):
        if mtype == "human":
            out.append(VisibleTurn(seq=seq, role="user", text=text, channel=None, superseded_by=by))
            continue
        nxt = collected[i + 1] if i + 1 < len(collected) else None
        last_in_segment = nxt is None or nxt[4]
        channel = CHANNEL_FINAL if last_in_segment and not tool_calls else CHANNEL_COMMENTARY
        out.append(VisibleTurn(seq=seq, role="assistant", text=text, channel=channel, superseded_by=by))
    if not stones:
        return out
    return sorted([*out, *stones], key=attrgetter("seq"))
```

顶部 `from operator import attrgetter`。

- [ ] **Step 4: 实现 `extract_turns` / `read_turns`**

```python
def extract_turns(
    raw_messages: list[Any], *, include_hidden: bool = True, include_superseded: bool = True
) -> list[MessageTurn]:
    out: list[MessageTurn] = []
    for turn in visible_turns(raw_messages, include_hidden=include_hidden, include_superseded=include_superseded):
        ak = getattr(raw_messages[turn.seq], "additional_kwargs", None) or {}
        out.append(
            MessageTurn(
                seq=turn.seq,
                role=turn.role,
                content=turn.text,
                channel=turn.channel,
                created_at=_parse_stamp_created_at(ak),
                run_id=_parse_stamp_run_id(ak),
                superseded_by=_parse_uuid_or_none(turn.superseded_by),
                tombstone=turn.tombstone,
            )
        )
    return out


def _parse_uuid_or_none(raw: str | None) -> UUID | None:
    if raw is None:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
```

`read_turns` 加 `include_superseded: bool = True` 并透传。docstring 各补一句「P-1:`include_superseded`」。

- [ ] **Step 5: 跑测试确认通过**

Run: 同 Step 2 的命令 + `uv run --no-sync pytest services/control-plane/tests/test_thread_stats_recorder.py services/control-plane/tests/test_transcript_mirror_sweep.py services/control-plane/tests/test_external_sessions.py packages/expert-work-common/tests/test_conversation_derive.py -q`
Expected: 全绿(默认参数下既有行为不变)。

- [ ] **Step 6: 变异自证**

`visible_turns` 里 `if is_tombstone(msg):` 分支整段注释掉 → `test_tombstone_is_emitted_empty_and_does_not_steal_final` 红(墓碑正文空白被当空气泡丢掉,`[t.seq …] == [0,1,2,3,4,5]` 失败);改回。`if by is not None and not include_superseded: continue` 注释掉 → `test_include_superseded_false_drops_them_without_moving_seq` 红;改回。

- [ ] **Step 7: lint / mypy / 提交**

```bash
uv run --no-sync ruff check packages/expert-work-common services/control-plane/src/control_plane/transcript.py services/control-plane/tests/test_transcript_extract.py
uv run --no-sync ruff format packages/expert-work-common services/control-plane/src/control_plane/transcript.py services/control-plane/tests/test_transcript_extract.py
uv run --no-sync mypy packages/expert-work-common
git add packages/expert-work-common services/control-plane/src/control_plane/transcript.py services/control-plane/tests/test_transcript_extract.py
git commit -m "feat(transcript): P-1 visible_turns/extract_turns 投影 superseded_by 与墓碑(include_superseded 开关)"
```

---

### Task 4: supersede 内核 `control_plane/supersede.py` + 真 Postgres 集成测

**Files:**
- Create: `services/control-plane/src/control_plane/supersede.py`
- Test: `services/control-plane/tests/test_supersede_kernel_integration.py`(新,`-m integration`,真 Postgres checkpointer + 真 ReAct 图)

**Interfaces:**
- Consumes: Task 1 `mark_superseded` / `tombstone_message` / `is_tombstone`;Task 2 `RunStore.mark_superseded`、`RunInfo.superseded_by_run_id/regenerated_from_run_id`、`ThreadMessageStore.mark_superseded`、`ThreadMetaStore.update_message_count`;Task 3 `extract_turns`;`ApprovalStore.get_by_run`;`trigger_delivery.delivery_thread_lock` 的写法
- Produces:
  - `SupersedeError(code: str, message: str, status_code: int)`(属性同名)
  - `SUPERSEDE_LOCK_CLASSID = 8620`;`SUPERSEDE_BUSY_STATUSES = frozenset({PENDING, QUEUED, RUNNING})`;`MAX_SUPERSEDED_VERSIONS = 5`
  - `supersede_thread_lock(session_factory: async_sessionmaker[AsyncSession] | None, thread_id: UUID) -> AsyncIterator[None]`(asynccontextmanager;`None` = no-op)
  - `TurnLocation(start: int, end: int, plan_before: Any, chain_run_ids: tuple[UUID, ...])`
  - `locate_turn(graph, config, *, run_ids: Sequence[UUID], current_len: int, current_plan: Any) -> TurnLocation`
  - `SupersedeResult(location: TurnLocation, replay_messages: tuple[BaseMessage, BaseMessage] | None, superseded_run_ids: tuple[UUID, ...])`
  - `supersede_run(*, graph, thread_id, tenant_id, target_run_id, new_run_id, runs: RunStore, approvals: ApprovalStore, thread_messages: ThreadMessageStore, threads: ThreadMetaStore, require_replay: bool) -> SupersedeResult`(**调用方必须已持 `supersede_thread_lock`**)
  - 错误码:`RUN_NOT_FOUND`(404)/ `RUN_ALREADY_SUPERSEDED`(409)/ `RUN_AWAITING_APPROVAL`(409)/ `THREAD_BUSY`(409)/ `RUN_NOT_LAST`(422)/ `RUN_INPUT_UNAVAILABLE`(422)

- [ ] **Step 1: 写失败的集成测试(主场景先写,其余场景 Step 6 补)**

```python
# services/control-plane/tests/test_supersede_kernel_integration.py
"""P-1 supersede 内核 —— 真 Postgres checkpointer + 真 ReAct 图(spike 的生产形态)。

跑法::

    export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
    uv run --no-sync pytest services/control-plane/tests/test_supersede_kernel_integration.py -m integration -q
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.api.runs import build_run_graph_input
from control_plane.supersede import (
    MAX_SUPERSEDED_VERSIONS,
    SupersedeError,
    supersede_run,
    supersede_thread_lock,
)
from control_plane.transcript import read_messages
from expert_work.common.conversation_channel import SUPERSEDED_BY, TOMBSTONE
from expert_work.persistence.approval import InMemoryApprovalStore
from expert_work.persistence.database import DatabaseConfig, create_async_engine_from_config, create_async_session_factory
from expert_work.persistence.thread_message import InMemoryThreadMessageStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.protocol import ThreadMeta
from expert_work.protocol.approval import ApprovalRecord, ApprovalStatus
from expert_work.protocol.plan import Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus
from orchestrator import GraphRunner, ToolContext, ToolRegistry, ToolResult, ToolSpec, build_react_graph

pytestmark = pytest.mark.integration

GOAL_ONE, GOAL_TWO = "PLAN-GOAL-ONE", "PLAN-GOAL-TWO"
TENANT = uuid4()


def _sync_dsn(container: PostgresContainer) -> str:
    return str(container.get_connection_url()).replace("+psycopg2", "")


def _async_dsn(container: PostgresContainer) -> str:
    return str(container.get_connection_url()).replace("+psycopg2", "+asyncpg")


@pytest.fixture
async def engine(postgres_container: PostgresContainer):
    eng = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container), pgbouncer_mode=False))
    try:
        yield eng
    finally:
        await eng.dispose()


@dataclass
class _ScriptedLLM:
    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any) -> AIMessage:
        self.prompts.append(list(messages))
        if not self.script:
            raise RuntimeError("scripted LLM exhausted")
        return self.script.pop(0)


@dataclass
class _PlanTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="set_plan", description="plan writer", parameters={"type": "object", "properties": {"goal": {"type": "string"}}})

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        goal = str(args["goal"])
        return ToolResult(content="ok", state_updates={"plan": Plan(goal=goal, steps=(PlanStep(id="1", description=goal),))})


def _built_stub() -> Any:
    return SimpleNamespace(supports_vision=False, spotlight_nonce=None, max_steps=8, max_no_progress=0,
                           system_prompt="You are the kernel-test agent.", prompt_jinja=False)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _turn_script(goal: str | None, final: str) -> list[AIMessage]:
    if goal is None:
        return [AIMessage(content=final)]
    return [AIMessage(content="", tool_calls=[_tool_call("set_plan", {"goal": goal}, f"tc-{goal}-{uuid4().hex[:6]}")]), AIMessage(content=final)]


def _cfg(thread_id: UUID) -> RunnableConfig:
    return {"configurable": {"thread_id": str(thread_id), "tenant_id": str(TENANT)}}


@dataclass
class _Stack:
    """一套内核依赖:真 checkpointer 上的图 + 三个内存 store + 真 advisory lock。"""

    compiled: Any
    llm: _ScriptedLLM
    runs: InMemoryRunStore
    approvals: InMemoryApprovalStore
    thread_messages: InMemoryThreadMessageStore
    threads: InMemoryThreadMetaStore
    session_factory: Any
    thread_id: UUID

    async def run_turn(self, run_id: UUID, text: str, *, status: RunStatus = RunStatus.SUCCESS, regenerated_from: UUID | None = None) -> None:
        graph_input = build_run_graph_input(_built_stub(), input_text=text, image_refs=[], untrusted_content=None, run_id=run_id)
        cfg: RunnableConfig = {"configurable": {**_cfg(self.thread_id)["configurable"], "run_id": str(run_id)}}
        async for _ in self.compiled.astream(graph_input, cfg, stream_mode="updates"):
            pass
        await self.add_run_row(run_id, status=status, regenerated_from=regenerated_from)

    async def add_run_row(self, run_id: UUID, *, status: RunStatus, regenerated_from: UUID | None = None) -> None:
        now = datetime.now(UTC)
        await self.runs.create(RunInfo(run_id=run_id, tenant_id=TENANT, thread_id=self.thread_id, user_id=None, status=status,
                                       on_disconnect=DisconnectMode.CONTINUE, is_resume=False, error=None, created_at=now,
                                       updated_at=now, finished_at=now if status is not RunStatus.RUNNING else None,
                                       regenerated_from_run_id=regenerated_from))
        await asyncio.sleep(0.001)  # list_by_thread 按 created_at 排,保证严格递增

    async def messages(self) -> list[BaseMessage]:
        return list((await self.compiled.aget_state(_cfg(self.thread_id))).values["messages"])

    async def supersede(self, target: UUID, new: UUID, *, require_replay: bool = True):
        async with supersede_thread_lock(self.session_factory, self.thread_id):
            return await supersede_run(graph=self.compiled, thread_id=self.thread_id, tenant_id=TENANT, target_run_id=target,
                                       new_run_id=new, runs=self.runs, approvals=self.approvals,
                                       thread_messages=self.thread_messages, threads=self.threads, require_replay=require_replay)


async def _stack(cp: Any, engine: AsyncEngine, script: list[AIMessage], *, approval_required: frozenset[str] = frozenset()) -> _Stack:
    llm = _ScriptedLLM(script=script)
    registry = ToolRegistry()
    registry.register(_PlanTool())
    compiled = GraphRunner(checkpointer=cp).compile(
        build_react_graph(llm_caller=llm, tool_registry=registry, approval_required_tools=approval_required)
    )
    threads = InMemoryThreadMetaStore()
    thread_id = uuid4()
    await threads.create(ThreadMeta(thread_id=thread_id, tenant_id=TENANT, created_at=datetime.now(UTC)))
    return _Stack(compiled=compiled, llm=llm, runs=InMemoryRunStore(), approvals=InMemoryApprovalStore(),
                  thread_messages=InMemoryThreadMessageStore(), threads=threads,
                  session_factory=create_async_session_factory(engine), thread_id=thread_id)


def _marks(msgs: Sequence[BaseMessage]) -> list[str | None]:
    return [m.additional_kwargs.get(SUPERSEDED_BY) for m in msgs]


def _dump(msgs: Sequence[BaseMessage]) -> str:
    return json.dumps([m.model_dump() for m in msgs], ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 主场景:两轮 → 取代第二轮 → 标记/下标/内容/plan/next/SQL 三表 → 第三轮从入口正常跑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_marks_in_place_reverts_plan_and_links_rows(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(GOAL_ONE, "A1-final"), *_turn_script(GOAL_TWO, "A2-final")])
        r1, r2, r3 = uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        before = await st.messages()
        assert len(before) == 10
        # 镜像先同步一次(模拟 sweep 已跑过),再看 supersede 是否显式更新它
        from control_plane.transcript import extract_turns
        await st.thread_messages.sync_thread(thread_id=st.thread_id, tenant_id=TENANT, turns=extract_turns(before), synced_at=datetime.now(UTC))
        await st.threads.update_message_count(st.thread_id, 4, tenant_id=TENANT)

        result = await st.supersede(r2, r3)

        assert (result.location.start, result.location.end) == (5, 10)
        assert result.superseded_run_ids == (r2,)
        assert result.replay_messages is not None
        assert isinstance(result.replay_messages[0], SystemMessage) and isinstance(result.replay_messages[1], HumanMessage)
        snap = await st.compiled.aget_state(_cfg(st.thread_id))
        after = list(snap.values["messages"])
        assert [m.id for m in after] == [m.id for m in before]                      # 条数 / 下标不变
        assert [m.content for m in after] == [m.content for m in before]            # 内容不变
        assert _marks(after) == [None] * 5 + [str(r3)] * 5                          # 标记只在 [5,10)
        assert snap.values["plan"].goal == GOAL_ONE                                 # plan 回退
        assert snap.next == ()                                                      # as_node="agent"
        # SQL 三表
        assert (await st.runs.get(run_id=r2, tenant_id=TENANT)).superseded_by_run_id == r3
        mirror = {seq: t.superseded_by for (tid, seq), (_x, t) in st.thread_messages._turns.items() if tid == st.thread_id}
        assert mirror == {1: None, 4: None, 6: r3, 9: r3}
        assert (await st.threads.get(st.thread_id, tenant_id=TENANT)).message_count == 4  # 与 /messages 同口径:被取代轮仍计
        # 新 saver 再读,标记仍在
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp2:
        raw = await read_messages(cp2, st.thread_id)
        assert _marks(raw) == [None] * 5 + [str(r3)] * 5


@pytest.mark.asyncio
async def test_plan_reverts_to_none_when_first_turn_had_no_plan(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(GOAL_TWO, "A2")])
        r1, r2, r3 = uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        assert (await st.compiled.aget_state(_cfg(st.thread_id))).values["plan"].goal == GOAL_TWO
        loc = (await st.supersede(r2, r3)).location
        assert (loc.start, loc.end) == (3, 8) and loc.plan_before is None
        assert (await st.compiled.aget_state(_cfg(st.thread_id))).values.get("plan") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run --no-sync pytest services/control-plane/tests/test_supersede_kernel_integration.py -m integration -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'control_plane.supersede'`。

- [ ] **Step 3: 实现内核**

```python
# services/control-plane/src/control_plane/supersede.py
"""P-1「重新生成 / 编辑重发」内核 —— 把会话**最后一轮**标成「已被取代」。

B 方案(spec §3.1):不分叉、不删消息。同一 thread 的检查点里,旧轮的每条
消息按**原 id** 写回一份带 ``expert_work_superseded_by`` 的副本 —— ``add_messages``
reducer 同 id 原地替换,条数 / 下标 / 内容都不变(spike 反证:丢 id 会被追加)。
``plan`` 通道同一次 ``aupdate_state`` 回退到该轮开始前的值。

**定轮不靠消息戳**(ToolMessage 与每轮的 SystemMessage 没有戳),靠检查点
metadata 的 ``run_id``(langchain ``ensure_config`` 把 configurable 标量复制进
metadata;spike 实测 keys = parents / run_id / source / step / tenant_id):
该 run 最早的 checkpoint(``source == "input"``)的 ``parent_config`` 就是
「该轮之前」—— 它的 ``len(messages)`` 是起始下标,它的 ``plan`` 是回退值。

**审批链**:审批把一轮切成 PAUSED run + continuation run(``runs.py:857-951``,
``is_resume=True``、``graph_input=None``),continuation 的最早 checkpoint 是
``source="loop"``。所以先沿 ``ApprovalRecord.continuation_run_id`` 往前把 run
串成链,链首才是 ``source="input"``;链上每个 run 都记 ``superseded_by_run_id``。

**写入顺序**(spec 说「同一事务」,但 checkpoint 走 psycopg 池、SQL 走
SQLAlchemy,跨不了一个事务):锁内 ① 检查 ② checkpoint 一次 ``aupdate_state``
③ ``agent_run`` ④ ``thread_message`` 镜像 ⑤ ``thread_meta.message_count``。
③ 之后崩掉 = 检查点已标、``agent_run`` 未链:下一次 supersede 看到
``superseded_by_run_id`` 为空或指向不存在的 run,视为**未取代**,重做一遍
(同 id 再替换一次,幂等)。**调用方必须持有 :func:`supersede_thread_lock`**。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.transcript import extract_turns
from expert_work.common.conversation_channel import is_hidden, is_tombstone
from expert_work.common.supersede import mark_superseded, tombstone_message
from expert_work.persistence.approval import ApprovalStore
from expert_work.persistence.thread_message import ThreadMessageStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.runtime.runs import RunInfo, RunStatus, RunStore

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SUPERSEDED_VERSIONS",
    "SUPERSEDE_BUSY_STATUSES",
    "SUPERSEDE_LOCK_CLASSID",
    "SupersedeError",
    "SupersedeResult",
    "TurnLocation",
    "locate_turn",
    "supersede_run",
    "supersede_thread_lock",
]

#: advisory classid。既有取值:workspace_lock 1、mcp_oauth_refresh_lock 2、
#: quality_drift 8615、memory_consolidator 8616、skill_curator 8617、
#: tenant_resource_lock 8618、trigger_delivery / workspace_janitor 8619(历史
#: 上重复占用,键串不同没撞)—— 本模块取 8620,永不共键。
SUPERSEDE_LOCK_CLASSID = 8620

#: 会话里任一 run 处于这些状态 → 409 THREAD_BUSY。与
#: ``api/external_sessions._ACTIVE_RUN_STATUSES`` 同集合(PENDING / QUEUED /
#: RUNNING);``api/plan.py`` 的 ``_WRITE_BLOCKED_STATUSES`` 少了 QUEUED,不能用。
SUPERSEDE_BUSY_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING}
)

#: 同一逻辑轮(沿 ``regenerated_from_run_id`` 回溯的链)最多保留的旧版本数;
#: 超出的最老版本正文置墓碑(spec §3.1-5,拍板 5)。
MAX_SUPERSEDED_VERSIONS = 5


class SupersedeError(Exception):
    """内核拒绝 —— 端点把它渲染成对外信封(code / message / status_code)。"""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@asynccontextmanager
async def supersede_thread_lock(
    session_factory: async_sessionmaker[AsyncSession] | None,
    thread_id: UUID,
) -> AsyncIterator[None]:
    """per-thread 阻塞式 advisory xact lock,照 ``trigger_delivery.delivery_thread_lock``。

    临界区 = 「查状态 → 读 checkpoint → aupdate_state → 三张表 → 建新 run 行」。
    两副本并发对同一轮 supersede:后进的在锁外等,进来后看到
    ``superseded_by_run_id`` 已指向一个**存在的** run → 409。
    ``session_factory=None``(内存栈 / 单测)= no-op。
    """
    if session_factory is None:
        yield
        return
    async with session_factory() as lock_session:
        await lock_session.execute(
            text("SELECT pg_advisory_xact_lock(:cid, hashtext(:k))"),
            {"cid": SUPERSEDE_LOCK_CLASSID, "k": str(thread_id)},
        )
        try:
            yield
        finally:
            await lock_session.rollback()


@dataclass(frozen=True)
class TurnLocation:
    """一轮在 ``messages`` 通道里的区间 ``[start, end)`` + 轮前的 plan。"""

    start: int
    end: int
    plan_before: Any
    chain_run_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class SupersedeResult:
    location: TurnLocation
    #: ``:regenerate`` 要重放的两条原始消息(该轮的 SystemMessage + 用户
    #: HumanMessage,**打标之前**的原件);``require_replay=False`` 或该轮没有
    #: 这个形状时为 ``None``。
    replay_messages: tuple[BaseMessage, BaseMessage] | None
    superseded_run_ids: tuple[UUID, ...]


async def locate_turn(
    graph: Any,
    config: RunnableConfig,
    *,
    run_ids: Sequence[UUID],
    current_len: int,
    current_plan: Any,
) -> TurnLocation:
    """取法 A:按 run 过滤检查点历史,链首 run 最早的 ``source=="input"`` 快照的
    parent 就是「该轮之前」。

    ``run_ids[0]`` 是目标(最新的),之后是它的 PAUSED 前驱(审批链)。没有任何
    检查点(run 在图开始前就失败)→ 空区间 ``[current_len, current_len)``、
    plan 不动 —— 只链接 ``agent_run`` 行。
    """
    oldest: Any = None
    newest: Any = None
    for run_id in run_ids:
        snaps = [s async for s in graph.aget_state_history(config, filter={"run_id": str(run_id)})]
        if not snaps:
            continue
        if newest is None:
            newest = snaps[0]  # 历史最新在前;run_ids[0] 是目标,它的最新就是轮尾
        oldest = snaps[-1]
    if oldest is None or newest is None:
        return TurnLocation(start=current_len, end=current_len, plan_before=current_plan, chain_run_ids=tuple(run_ids))
    if (oldest.metadata or {}).get("source") != "input":
        # 链首不是入口写入 —— 只可能是历史损坏或链没串全;宁可拒绝也别乱标。
        raise SupersedeError(
            "RUN_NOT_LAST", "run boundary could not be established from checkpoint history", 422
        )
    if oldest.parent_config is None:
        start, plan_before = 0, None
    else:
        parent = await graph.aget_state(oldest.parent_config)
        start = len(parent.values.get("messages") or [])
        plan_before = parent.values.get("plan")
    end = len(newest.values.get("messages") or [])
    if not (0 <= start <= end <= current_len):
        raise SupersedeError("RUN_NOT_LAST", "run boundary is outside the current history", 422)
    return TurnLocation(start=start, end=end, plan_before=plan_before, chain_run_ids=tuple(run_ids))


async def _approval_chain(
    target: RunInfo, rows: Sequence[RunInfo], *, approvals: ApprovalStore, tenant_id: UUID
) -> list[UUID]:
    """``[target, PAUSED 前驱, 更早的 PAUSED 前驱, …]`` —— 前驱必须是 PAUSED 且它的
    审批单 ``continuation_run_id`` 正好指向链上后一个 run。"""
    chain = [target.run_id]
    by_id = {r.run_id: r for r in rows}
    ordered = [r.run_id for r in rows]  # list_by_thread 最老在前
    cursor = target.run_id
    while True:
        idx = ordered.index(cursor)
        if idx == 0:
            return chain
        prev = by_id[ordered[idx - 1]]
        if prev.status is not RunStatus.PAUSED:
            return chain
        record = await approvals.get_by_run(run_id=prev.run_id, tenant_id=tenant_id)
        if record is None or record.continuation_run_id != cursor:
            return chain
        chain.append(prev.run_id)
        cursor = prev.run_id


async def _version_chain(target: RunInfo, *, runs: RunStore, tenant_id: UUID) -> list[RunInfo]:
    """同一逻辑轮的版本,新在前:``[target, target.regenerated_from, …]``。"""
    out = [target]
    seen = {target.run_id}
    cursor = target.regenerated_from_run_id
    while cursor is not None and cursor not in seen:
        row = await runs.get(run_id=cursor, tenant_id=tenant_id)
        if row is None:
            break
        out.append(row)
        seen.add(row.run_id)
        cursor = row.regenerated_from_run_id
    return out


def _replay_pair(turn: Sequence[BaseMessage]) -> tuple[BaseMessage, BaseMessage] | None:
    """``build_run_graph_input`` 的形状:[System, 非隐藏 Human, …]。不是这个形状就没有可重放的输入。"""
    if len(turn) < 2:
        return None
    system, human = turn[0], turn[1]
    if not isinstance(system, SystemMessage) or not isinstance(human, HumanMessage) or is_hidden(human):
        return None
    return system, human


async def supersede_run(
    *,
    graph: Any,
    thread_id: UUID,
    tenant_id: UUID,
    target_run_id: UUID,
    new_run_id: UUID,
    runs: RunStore,
    approvals: ApprovalStore,
    thread_messages: ThreadMessageStore,
    threads: ThreadMetaStore,
    require_replay: bool,
) -> SupersedeResult:
    """把 ``target_run_id`` 这一轮标成被 ``new_run_id`` 取代。**调用方持锁**。"""
    rows = await runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    target = next((r for r in rows if r.run_id == target_run_id), None)
    if target is None:
        raise SupersedeError("RUN_NOT_FOUND", "run not found", 404)
    if target.superseded_by_run_id is not None:
        successor = await runs.get(run_id=target.superseded_by_run_id, tenant_id=tenant_id)
        if successor is not None:
            raise SupersedeError("RUN_ALREADY_SUPERSEDED", "this run was already regenerated", 409)
        # 悬空链接(上一次在建新 run 行之前崩了)→ 视为未取代,重做。
    if target.status is RunStatus.PAUSED:
        raise SupersedeError(
            "RUN_AWAITING_APPROVAL", "this run is waiting for an approval decision; decide it first", 409
        )
    busy = [r for r in rows if r.status in SUPERSEDE_BUSY_STATUSES]
    if busy:
        raise SupersedeError("THREAD_BUSY", "the session has a run in progress", 409)
    if rows[-1].run_id != target_run_id:
        raise SupersedeError("RUN_NOT_LAST", "only the last run of a session can be regenerated", 422)

    config: RunnableConfig = {"configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}}
    snapshot = await graph.aget_state(config)
    messages: list[BaseMessage] = list((snapshot.values or {}).get("messages") or [])
    chain = await _approval_chain(target, rows, approvals=approvals, tenant_id=tenant_id)
    location = await locate_turn(
        graph, config, run_ids=chain, current_len=len(messages), current_plan=(snapshot.values or {}).get("plan")
    )
    turn = messages[location.start : location.end]
    replay = _replay_pair(turn)
    if require_replay and replay is None:
        raise SupersedeError(
            "RUN_INPUT_UNAVAILABLE", "this run left no reusable input; send :edit with a new input", 422
        )

    now = datetime.now(UTC)
    updates: list[BaseMessage] = [mark_superseded(m, new_run_id=str(new_run_id), now=now) for m in turn]

    # 留 5 份:版本链(含本次要被取代的 target)超过上限 → 最老的置墓碑。
    versions = await _version_chain(target, runs=runs, tenant_id=tenant_id)
    for old in versions[MAX_SUPERSEDED_VERSIONS:]:
        old_chain = await _approval_chain(old, rows, approvals=approvals, tenant_id=tenant_id)
        old_loc = await locate_turn(
            graph, config, run_ids=old_chain, current_len=len(messages), current_plan=None
        )
        for m in messages[old_loc.start : old_loc.end]:
            if not is_tombstone(m):
                updates.append(tombstone_message(m))

    if updates:
        # 空区间且无墓碑(图开始前就失败的 run)不写 checkpoint —— 没有消息可标,也不该动 plan。
        await graph.aupdate_state(config, {"messages": updates, "plan": location.plan_before}, as_node="agent")

    for run_id in chain:
        await runs.mark_superseded(run_id=run_id, tenant_id=tenant_id, superseded_by_run_id=new_run_id)
    if turn:
        await thread_messages.mark_superseded(
            thread_id=thread_id, tenant_id=tenant_id, seq_from=location.start, seq_to=location.end, superseded_by=new_run_id
        )
    after = list(((await graph.aget_state(config)).values or {}).get("messages") or [])
    await threads.update_message_count(
        thread_id, len(extract_turns(after, include_hidden=False)), tenant_id=tenant_id
    )
    logger.info(
        "supersede.applied thread=%s target=%s new=%s range=[%d,%d) chain=%d tombstoned=%d",
        thread_id, target_run_id, new_run_id, location.start, location.end, len(chain), len(updates) - len(turn),
    )
    return SupersedeResult(location=location, replay_messages=replay if require_replay else None,
                           superseded_run_ids=tuple(chain))
```

- [ ] **Step 4: 跑 Step 1 两条,预期通过**

Run: 同 Step 2 命令。Expected: 2 passed。

- [ ] **Step 5: 变异自证(每条至少一次)**

1. `mark_superseded` 调用改成 `m.model_copy(update={"id": None, …})`(或直接在 `updates` 列表推导后加 `updates = [u.model_copy(update={"id": None}) for u in updates]`)→ `[m.id for m in after] == [m.id for m in before]` 红(15 条);改回。
2. `as_node="agent"` 改 `"__start__"` → `snap.next == ()` 红(`("agent",)`);改回。
3. 去掉 `"plan": location.plan_before` → 主场景 `plan.goal == GOAL_ONE` 红、`test_plan_reverts_to_none…` 红;改回。
4. 注释掉 `thread_messages.mark_superseded(...)` → `mirror == {...}` 红;改回。
5. 注释掉 `runs.mark_superseded(...)` 循环 → `superseded_by_run_id == r3` 红;改回。

- [ ] **Step 6: 补场景测试(追加到同一文件)**

```python
# ---------------------------------------------------------------------------
# 墓碑:第 6 次取代,最老版本正文清空,id / 下标 / 条数不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sixth_version_tombstones_the_oldest(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1")] + [m for i in range(7) for m in _turn_script(None, f"A2-v{i}")])
        r1, v0 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(v0, "U2-v0")
        prev = v0
        versions = [v0]
        for i in range(1, 7):  # v1..v6:每次先取代上一版,再以 regenerated_from 跑新版
            new = uuid4()
            await st.supersede(prev, new)
            await st.run_turn(new, f"U2-v{i}", regenerated_from=prev)
            versions.append(new)
            prev = new
        msgs = await st.messages()
        ids = [m.id for m in msgs]
        # v0..v5 六个被取代版本 → 超过 5 份的最老一个(v0)成墓碑;v1..v5 仍带正文
        assert msgs[3].content == "" and msgs[3].additional_kwargs[TOMBSTONE] is True   # v0 的 System
        assert msgs[4].content == "" and msgs[4].additional_kwargs[SUPERSEDED_BY]       # v0 的 Human,标记仍在
        assert "U2-v1" in msgs[7].content                                                # v1 未清理
        assert len(msgs) == 3 + 3 * 7 and ids == [m.id for m in await st.messages()]    # 每轮 3 条,下标稳定
        assert "U2-v0" not in _dump(msgs) and "U2-v6" in _dump(msgs)


# ---------------------------------------------------------------------------
# 审批链:PAUSED run + continuation run 是一轮;取代 continuation 必须连 PAUSED 段一起标
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_covers_the_approval_chain(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(GOAL_TWO, "A2-final")],
                          approval_required=frozenset({"set_plan"}))
        r1, paused, cont, r3 = uuid4(), uuid4(), uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(paused, "U2", status=RunStatus.PAUSED)         # 停在 set_plan 的审批门
        pending = (await st.compiled.aget_state(_cfg(st.thread_id))).values.get("pending_approval")
        assert pending is not None
        now = datetime.now(UTC)
        await st.approvals.create(ApprovalRecord(
            id=uuid4(), tenant_id=TENANT, run_id=paused, thread_id=st.thread_id, request_id=str(pending["request_id"]),
            node="tools", reason_kind="policy_gate", action_summary="set_plan", requested_at=now,
            timeout_at=now, status=ApprovalStatus.APPROVED, continuation_run_id=cont,
        ))
        # 续跑:照 runs.py:857-926 的写法 —— 写裁定、as_node="agent"、graph_input=None 起新 run
        await st.compiled.aupdate_state(_cfg(st.thread_id), {"pending_approval": None, "approval_resume": {
            "decision": "approve", "modified_args": None, "reason": None, "binding_digest": pending.get("binding_digest", "")}}, as_node="agent")
        cont_cfg: RunnableConfig = {"configurable": {**_cfg(st.thread_id)["configurable"], "run_id": str(cont)}}
        async for _ in st.compiled.astream(None, cont_cfg, stream_mode="updates"):
            pass
        await st.add_run_row(cont, status=RunStatus.SUCCESS)
        before = await st.messages()

        result = await st.supersede(cont, r3)

        assert result.superseded_run_ids == (cont, paused)
        assert result.location.start == 3                              # U2 这一轮从第一轮之后开始
        after = await st.messages()
        assert _marks(after) == [None] * 3 + [str(r3)] * (len(before) - 3)
        assert (await st.runs.get(run_id=paused, tenant_id=TENANT)).superseded_by_run_id == r3
        assert (await st.runs.get(run_id=cont, tenant_id=TENANT)).superseded_by_run_id == r3
        assert (await st.compiled.aget_state(_cfg(st.thread_id))).values.get("plan") is None


# ---------------------------------------------------------------------------
# 拒绝语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejections(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(None, "A2"), *_turn_script(None, "A3")])
        r1, r2 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        with pytest.raises(SupersedeError) as e:
            await st.supersede(r1, uuid4())
        assert (e.value.code, e.value.status_code) == ("RUN_NOT_LAST", 422)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(uuid4(), uuid4())
        assert e.value.code == "RUN_NOT_FOUND"
        # 有 run 在跑
        running = uuid4()
        await st.add_run_row(running, status=RunStatus.RUNNING)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(running, uuid4())
        assert (e.value.code, e.value.status_code) == ("THREAD_BUSY", 409)
        await st.runs.set_status(run_id=running, tenant_id=TENANT, status=RunStatus.ERROR, error="x")
        # 图开始前就失败的 run(无检查点):regenerate 拿不到输入 → 422;edit 可以,只链接行
        with pytest.raises(SupersedeError) as e:
            await st.supersede(running, uuid4(), require_replay=True)
        assert (e.value.code, e.value.status_code) == ("RUN_INPUT_UNAVAILABLE", 422)
        r3 = uuid4()
        result = await st.supersede(running, r3, require_replay=False)
        assert (result.location.start, result.location.end) == (6, 6)
        assert (await st.runs.get(run_id=running, tenant_id=TENANT)).superseded_by_run_id == r3
        await st.add_run_row(r3, status=RunStatus.SUCCESS, regenerated_from=running)
        # 已取代(后继存在)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(running, uuid4(), require_replay=False)
        assert e.value.code == "RUN_ALREADY_SUPERSEDED"
        # 目标 PAUSED
        p = uuid4()
        await st.add_run_row(p, status=RunStatus.PAUSED)
        with pytest.raises(SupersedeError) as e:
            await st.supersede(p, uuid4(), require_replay=False)
        assert (e.value.code, e.value.status_code) == ("RUN_AWAITING_APPROVAL", 409)


@pytest.mark.asyncio
async def test_dangling_link_is_treated_as_not_superseded(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    """agent_run 指向一个不存在的 run(上次在建新行前崩了)→ 允许重做。"""
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(None, "A2")])
        r1, r2 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        ghost = uuid4()
        await st.supersede(r2, ghost)               # ghost 行永远没建
        real = uuid4()
        result = await st.supersede(r2, real)       # 不该 409
        assert _marks(await st.messages())[-1] == str(real)
        assert result.superseded_run_ids == (r2,)


# ---------------------------------------------------------------------------
# 两副本并发:同一轮只成一条
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_replicas_concurrent_supersede_single_winner(postgres_container: PostgresContainer, engine: AsyncEngine) -> None:
    async with make_checkpointer("postgres", _sync_dsn(postgres_container)) as cp:
        st = await _stack(cp, engine, [*_turn_script(None, "A1"), *_turn_script(None, "A2")])
        r1, r2 = uuid4(), uuid4()
        await st.run_turn(r1, "U1")
        await st.run_turn(r2, "U2")
        other_factory = create_async_session_factory(engine)   # 第二个「副本」自己的 session factory

        async def attempt(factory: Any, new: UUID) -> str:
            try:
                async with supersede_thread_lock(factory, st.thread_id):
                    await supersede_run(graph=st.compiled, thread_id=st.thread_id, tenant_id=TENANT, target_run_id=r2,
                                        new_run_id=new, runs=st.runs, approvals=st.approvals,
                                        thread_messages=st.thread_messages, threads=st.threads, require_replay=True)
                    await st.add_run_row(new, status=RunStatus.PENDING, regenerated_from=r2)  # 锁内建行,与 spawn_run 同序
                return "ok"
            except SupersedeError as exc:
                return exc.code

        outcomes = await asyncio.gather(attempt(st.session_factory, uuid4()), attempt(other_factory, uuid4()))
        assert sorted(outcomes) == ["RUN_ALREADY_SUPERSEDED", "ok"]
        assert len({m.additional_kwargs.get(SUPERSEDED_BY) for m in (await st.messages())[3:]}) == 1
```

`ApprovalRecord` 字段见 `packages/expert-work-protocol/src/expert_work/protocol/approval.py:223-251`(`reason_kind` 是 `Literal["policy_gate", "missing_info", "ambiguous_requirement", "approach_choice", "risk_confirmation"]`,`:72-78`);`pending_approval` 的 `request_id` / `binding_digest` 由 `orchestrator/graph_builder/_approval.py:174/185` 的 `build_approval_request` 写入。

Run: 同 Step 2。Expected: 7 passed。变异:`_approval_chain` 里 `record.continuation_run_id != cursor` 改成 `True` → 审批链测试 `superseded_run_ids == (cont, paused)` 红;改回。`versions[MAX_SUPERSEDED_VERSIONS:]` 改成 `versions[MAX_SUPERSEDED_VERSIONS + 1:]` → 墓碑测试红;改回。`supersede_thread_lock` 的 `execute(...)` 注释掉 → 并发测试大概率两边都 "ok"(红);改回。

- [ ] **Step 7: lint / 提交**

```bash
uv run --no-sync ruff check services/control-plane/src/control_plane/supersede.py services/control-plane/tests/test_supersede_kernel_integration.py
uv run --no-sync ruff format services/control-plane/src/control_plane/supersede.py services/control-plane/tests/test_supersede_kernel_integration.py
git add services/control-plane/src/control_plane/supersede.py services/control-plane/tests/test_supersede_kernel_integration.py
git commit -m "feat(control-plane): P-1 supersede 内核 —— per-thread 锁 + 取法 A 定轮(含审批链)+ 原 id 原地打标 + plan 回退 + 墓碑上限 5"
```

---

### Task 5: 读面字段 —— `/messages`、`/items`、控制台 messages/runs、conversations、对外 `/runs`

读 checkpoint 的生产代码全部 12 处(`rg -n "read_messages\(|read_turns\(|\.aget_tuple\(|\.aget_state\(|aget_state_history\(" services/control-plane/src/control_plane --type py`):`transcript.py:107/140`、`transcript_mirror_sweep.py:157`、`trigger_delivery.py:122/205/239`、`quality_monitor_worker.py:294`、`api/plan.py:142/200`、`api/runs.py:1738`、`api/external_sessions.py:291`、`api/external_session_items.py:431`、`api/_session_title.py:50`。要改投影的是三个对用户/对接方的读面(`runs.py:1738`、`external_sessions.py:291`、`external_session_items.py:431`)+ 三个 run 行投影(`runs.py:1826-1846`、`conversations.py:149-170`、`external_runs.py:120-140`);其余读面(镜像 sweep、投递去重、质量监控、会话标题)默认 `include_superseded=True`,行为不变,**不改**。

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_sessions.py:296-305`(`out` 投影)
- Modify: `services/control-plane/src/control_plane/api/external_session_items.py:436-494`(`by_run`、每条 item、`runs[]`)
- Modify: `services/control-plane/src/control_plane/api/runs.py:1745-1755`(`get_thread_messages` 投影)、`:1826-1846`(`list_thread_runs` 投影)
- Modify: `services/control-plane/src/control_plane/api/conversations.py:149-170`(`_run_to_dict`)
- Modify: `services/control-plane/src/control_plane/api/external_runs.py:120-140`(`list_runs` 投影)
- Test: `services/control-plane/tests/test_external_sessions.py`、`test_external_session_items.py`、`test_external_runs_list.py`(各加一条)、`services/control-plane/tests/test_read_faces_superseded.py`(新,控制台两端点 + conversations)

**Interfaces:**
- Consumes: Task 2 `RunInfo.superseded_by_run_id/regenerated_from_run_id`;Task 3 `MessageTurn.superseded_by/tombstone`;Task 1 `is_tombstone`、`group_messages_by_run`
- Produces(wire 字段,缺省 `null` / `false`,**始终出现**):
  - `/messages` 每条:`superseded_by: string|null`、`tombstone: boolean`
  - `/items` 每个条目:`superseded_by`、`tombstone`;`runs[]`:`superseded_by`、`regenerated_from`
  - 控制台 `GET /v1/sessions/{id}/messages` 每条:`superseded_by`、`tombstone`;`GET /v1/sessions/{id}/runs` 与 `GET /v1/conversations/{id}` 的 `runs[]`:`superseded_by`、`regenerated_from`
  - 对外 `GET /v1/agents/{code}/runs` 每条:`superseded_by`、`regenerated_from`

- [ ] **Step 1: 写失败测试(对外 `/messages`)**

在 `services/control-plane/tests/test_external_sessions.py` 末尾加(夹具与该文件既有 `ctx` / 检查点 seed 同款;若该文件没有 seed 检查点的 helper,照 `test_external_session_items.py:53-67` 的 `_seed_thread_messages` 抄一份):

```python
@pytest.mark.asyncio
async def test_messages_expose_superseded_by_and_tombstone(ctx: _Ctx) -> None:
    from datetime import UTC, datetime
    from expert_work.common.supersede import mark_superseded, tombstone_message

    await ctx.seed_agent()
    thread_id = await ctx.bind_session("cust-77")
    new_run = uuid4()
    now = datetime(2026, 9, 10, tzinfo=UTC)
    old = [mark_superseded(m, new_run_id=str(new_run), now=now) for m in (HumanMessage(content="U1"), AIMessage(content="A1"))]
    stone = tombstone_message(mark_superseded(HumanMessage(content="U0"), new_run_id=str(new_run), now=now))
    await _seed_thread_messages(ctx.checkpointer, str(thread_id), [stone, *old, HumanMessage(content="U2"), AIMessage(content="A2")])

    resp = await ctx.client.get(f"/v1/agents/support-bot/sessions/{thread_id}/messages", params={"user_id": "cust-77"}, headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]["messages"]
    assert [(r["content"], r["superseded_by"], r["tombstone"]) for r in rows] == [
        ("", str(new_run), True), ("U1", str(new_run), False), ("A1", str(new_run), False), ("U2", None, False), ("A2", None, False)
    ]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_sessions.py -q -k superseded`
Expected: FAIL,`KeyError: 'superseded_by'`。

- [ ] **Step 3: `/messages` 投影**

`external_sessions.py` `out = [...]`(`:296-305`)每条加两行:

```python
                "superseded_by": str(t.superseded_by) if t.superseded_by else None,
                "tombstone": t.tombstone,
```

- [ ] **Step 4: `/items` 投影 + `runs[]`**

`external_session_items.py` 在 `items.extend(item.to_wire() for item in derived)`(`:474`)处改成:

```python
            superseded_by = str(run.superseded_by_run_id) if run.superseded_by_run_id else None
            tombstoned = any(is_tombstone(m) for m in by_run.get(key, []))
            for item in derived:
                wire = item.to_wire()
                # P-1 —— 轮级标记:被取代的一轮,它的每个条目都带 superseded_by;
                # 墓碑轮的条目 content 已是空串,再打 tombstone 让客户端能区分「空回答」与「已清理」。
                wire["superseded_by"] = superseded_by
                wire["tombstone"] = tombstoned
                items.append(wire)
```

`runs[]` 每条(`:481-490`)加:

```python
                            "superseded_by": str(run.superseded_by_run_id) if run.superseded_by_run_id else None,
                            "regenerated_from": str(run.regenerated_from_run_id) if run.regenerated_from_run_id else None,
```

import `from expert_work.common.conversation_channel import is_tombstone, message_field`(`message_field` 原有)。

- [ ] **Step 5: 控制台两端点 + conversations + 对外 `/runs`**

`runs.py` `get_thread_messages` 的 `out`(`:1745-1752`)每条加 `"superseded_by": str(t.superseded_by) if t.superseded_by else None, "tombstone": t.tombstone,`;`list_thread_runs` 的 `out`(`:1826-1846`)每条加 `"superseded_by": str(r.superseded_by_run_id) if r.superseded_by_run_id else None, "regenerated_from": str(r.regenerated_from_run_id) if r.regenerated_from_run_id else None,`;`conversations.py` `_run_to_dict` 加同两键(取 `info.`);`external_runs.py` `list_runs` 的每条(`:127-136`)加同两键(取 `r.`)。

- [ ] **Step 6: 其余测试**

`test_external_session_items.py` 末尾加(用该文件的 `_Ctx.seed_run`-类 helper 建 run 行时传 `superseded_by_run_id=` —— 该 helper 直接构造 `RunInfo`,加关键字即可):

```python
@pytest.mark.asyncio
async def test_items_and_runs_expose_supersede_links(ctx: _Ctx) -> None:
    from expert_work.common.supersede import mark_superseded

    await ctx.seed_agent()
    thread_id = await ctx.bind_session("cust-77")
    old_run, new_run = uuid4(), uuid4()
    t0 = _BASE
    old_msgs = [mark_superseded(m, new_run_id=str(new_run), now=t0) for m in (
        HumanMessage(content="U1", additional_kwargs=_stamp(old_run, t0)),
        AIMessage(content="A1", additional_kwargs=_stamp(old_run, t0 + timedelta(seconds=1))),
    )]
    new_msgs = [HumanMessage(content="U1'", additional_kwargs=_stamp(new_run, t0 + timedelta(seconds=5))),
                AIMessage(content="A1'", additional_kwargs=_stamp(new_run, t0 + timedelta(seconds=6)))]
    await _seed_thread_messages(ctx.checkpointer, str(thread_id), [*old_msgs, *new_msgs])
    await ctx.seed_run(thread_id, old_run, created_at=t0, status=RunStatus.SUCCESS, superseded_by_run_id=new_run)
    await ctx.seed_run(thread_id, new_run, created_at=t0 + timedelta(seconds=5), status=RunStatus.SUCCESS, regenerated_from_run_id=old_run)

    resp = await ctx.client.get(f"/v1/agents/support-bot/sessions/{thread_id}/items", params={"user_id": "cust-77"}, headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    by_run = {r["run_id"]: r for r in data["runs"]}
    assert by_run[str(old_run)]["superseded_by"] == str(new_run) and by_run[str(old_run)]["regenerated_from"] is None
    assert by_run[str(new_run)]["regenerated_from"] == str(old_run) and by_run[str(new_run)]["superseded_by"] is None
    old_items = [i for i in data["items"] if i["run_id"] == str(old_run)]
    assert old_items and all(i["superseded_by"] == str(new_run) and i["tombstone"] is False for i in old_items)
    assert all(i["superseded_by"] is None for i in data["items"] if i["run_id"] == str(new_run))
```

`test_external_runs_list.py` 加一条:seed 两个 run 行带链接 → `GET /v1/agents/support-bot/runs?user_id=cust-77` 每条有 `superseded_by` / `regenerated_from`。新建 `services/control-plane/tests/test_read_faces_superseded.py`:夹具照 `test_external_session_items.py` 但用员工 JWT(`sub_type="user"`,照 `test_runs_api.py` 的 `ctx`),seed 同上,断言 `GET /v1/sessions/{thread_id}/messages` 每条含 `superseded_by`/`tombstone`、`GET /v1/sessions/{thread_id}/runs` 与 `GET /v1/conversations/{thread_id}` 的 `runs[]` 含两个链接键。

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_sessions.py services/control-plane/tests/test_external_session_items.py services/control-plane/tests/test_external_runs_list.py services/control-plane/tests/test_read_faces_superseded.py services/control-plane/tests/test_conversation_items_parity.py -q`
Expected: 全绿。

- [ ] **Step 7: 变异自证**

`/items` 里 `wire["superseded_by"] = superseded_by` 改成 `= None` → `old_items` 断言红;`/messages` 的 `"tombstone": t.tombstone` 改 `False` → Step 1 测试第一元组红;改回。

- [ ] **Step 8: lint / 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane
git commit -m "feat(api): P-1 读面字段 —— /messages /items 控制台 messages/runs conversations 对外 /runs 带 superseded_by / regenerated_from / tombstone"
```

**PR1 到此**(Task 1-5)。开 PR 前跑:`uv run --no-sync pytest packages services/control-plane/tests -q -m "not integration" -n auto` + 上述三条集成测 + `uv run --no-sync mypy packages`。

---

### Task 6: 构图侧整轮过滤(`agent_node`,working_window 之前)+ 真 graph 探针测试

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:645`(之后插一行)+ 顶部 import
- Test: `services/orchestrator/tests/test_supersede_prompt_filter.py`(新)

**Interfaces:**
- Consumes: Task 1 `filter_superseded_turns`、`mark_superseded`
- Produces: `agent_node` 的 prompt 视图不含任何带 `SUPERSEDED_BY` 的消息,且被取代轮的 AI(tool_calls)/ToolMessage 成对消失

- [ ] **Step 1: 写失败测试(直接断言送进 LLM 的 messages 列表,不看日志)**

```python
# services/orchestrator/tests/test_supersede_prompt_filter.py
"""P-1 —— agent_node 的 prompt 视图整轮剔除被取代消息(真 ReAct 图 + 内存 checkpointer)。

验收判据 = **送进 LLM 的 messages 列表**里没有被取代轮的任何文本 / 工具调用 / 计划;
这是 spec §7 PR2 要求的「探针」形态:``_ScriptedLLM`` 就是能复述上下文的探针。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.message_stamp import stamp_message
from expert_work.common.supersede import mark_superseded
from expert_work.protocol.plan import Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolContext, ToolRegistry, ToolResult, ToolSpec, build_react_graph

GOAL_TWO = "PLAN-GOAL-TWO"


@dataclass
class _ScriptedLLM:
    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any) -> AIMessage:
        self.prompts.append(list(messages))
        return self.script.pop(0)


@dataclass
class _PlanTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="set_plan", description="plan", parameters={"type": "object", "properties": {"goal": {"type": "string"}}})

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        return ToolResult(content="ok", state_updates={"plan": Plan(goal=str(args["goal"]), steps=(PlanStep(id="1", description="s"),))})


def _cfg(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": "t", "tenant_id": "tenant", "run_id": run_id}}


def _turn_input(text: str, run_id: str) -> dict[str, Any]:
    human = stamp_message(HumanMessage(content=text), run_id=run_id, now=datetime.now(UTC))
    return {"messages": [SystemMessage(content="sys"), human], "step_count": 0, "max_steps": 6}


def _dump(msgs: Sequence[BaseMessage]) -> str:
    return json.dumps([m.model_dump() for m in msgs], ensure_ascii=False, default=str)


async def _two_turns_then_supersede_second(llm: _ScriptedLLM, cp: Any) -> Any:
    registry = ToolRegistry()
    registry.register(_PlanTool())
    compiled = GraphRunner(checkpointer=cp).compile(build_react_graph(llm_caller=llm, tool_registry=registry))
    await compiled.ainvoke(_turn_input("U1", "r1"), config=_cfg("r1"))
    await compiled.ainvoke(_turn_input("U2", "r2"), config=_cfg("r2"))
    base: RunnableConfig = {"configurable": {"thread_id": "t", "tenant_id": "tenant"}}
    snap = await compiled.aget_state(base)
    msgs = list(snap.values["messages"])
    turn2 = msgs[3:]                                   # 第一轮 3 条(System/Human/AI),第二轮 5 条(含 tool_calls + Tool)
    copies = [mark_superseded(m, new_run_id="r3", now=datetime.now(UTC)) for m in turn2]
    await compiled.aupdate_state(base, {"messages": copies, "plan": None}, as_node="agent")
    return compiled


@pytest.mark.asyncio
async def test_superseded_turn_never_reaches_the_llm() -> None:
    llm = _ScriptedLLM(script=[
        AIMessage(content="A1-final"),
        AIMessage(content="", tool_calls=[{"name": "set_plan", "args": {"goal": GOAL_TWO}, "id": "tc2", "type": "tool_call"}]),
        AIMessage(content="A2-final"),
        AIMessage(content="A3-final"),
    ])
    async with make_checkpointer("memory") as cp:
        compiled = await _two_turns_then_supersede_second(llm, cp)
        await compiled.ainvoke(_turn_input("U3", "r3"), config=_cfg("r3"))
    prompt = llm.prompts[-1]
    dumped = _dump(prompt)
    assert "U1" in dumped and "A1-final" in dumped and "U3" in dumped
    assert "U2" not in dumped and "A2-final" not in dumped and GOAL_TWO not in dumped and "tc2" not in dumped
    assert not any(isinstance(m, ToolMessage) for m in prompt)        # 工具对成对消失,没有孤儿
    assert "## Execution plan" not in dumped                            # plan 已回退到 None


@pytest.mark.asyncio
async def test_negative_control_marks_alone_do_not_hide_anything() -> None:
    """反证:不是「标记本身」让 LangGraph 跳过什么 —— 拿掉过滤器,被标消息**会**进 prompt。
    这条测试在实现里把 filter 行注释掉时必须变绿、恢复后必须变红 —— 它就是变异自证的镜像。"""
    from expert_work.common.conversation_channel import SUPERSEDED_BY

    llm = _ScriptedLLM(script=[
        AIMessage(content="A1-final"),
        AIMessage(content="", tool_calls=[{"name": "set_plan", "args": {"goal": GOAL_TWO}, "id": "tc2", "type": "tool_call"}]),
        AIMessage(content="A2-final"),
        AIMessage(content="A3-final"),
    ])
    async with make_checkpointer("memory") as cp:
        compiled = await _two_turns_then_supersede_second(llm, cp)
        base: RunnableConfig = {"configurable": {"thread_id": "t", "tenant_id": "tenant"}}
        persisted = list((await compiled.aget_state(base)).values["messages"])
        assert sum(1 for m in persisted if m.additional_kwargs.get(SUPERSEDED_BY)) == 5   # 标记确实在检查点里
        await compiled.ainvoke(_turn_input("U3", "r3"), config=_cfg("r3"))
    assert not any(m.additional_kwargs.get(SUPERSEDED_BY) for m in llm.prompts[-1])       # 但一条都没进 prompt
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_supersede_prompt_filter.py -q`
Expected: 两条都 FAIL(`"U2" not in dumped` 与最后一条断言)。

- [ ] **Step 3: 插过滤**

`graph_builder/builder.py` 顶部 import 区加 `from expert_work.common.supersede import filter_superseded_turns`;`:645` `messages = list(state["messages"])` 之后、`# Stream CM-12` 注释之前插:

```python
        # P-1 —— 被取代的轮整段剔除,放在一切上下文闸(pruner / working_window /
        # compressor)之前:它们不占窗口预算、不进摘要。只改 prompt 视图,检查点
        # 不动(CM-C4 同一契约);读面照常看到这些消息。
        messages = filter_superseded_turns(messages)
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 + `uv run --no-sync pytest services/orchestrator/tests -q -m "not integration" -n auto`
Expected: 全绿。

- [ ] **Step 5: 变异自证**

把 Step 3 那一行注释掉 → `test_superseded_turn_never_reaches_the_llm` 红(`"U2" not in dumped`)且 `test_negative_control…` 最后一条断言红;恢复 → 绿。再把过滤挪到 `working_window.apply` 之后(`:671` 之后)→ 两条测试仍绿但违反 spec「working_window 之前」—— 位置由 code review 把关,测试不能测位置;在 PR 描述里明写行号。

- [ ] **Step 6: lint / mypy / 提交**

```bash
uv run --no-sync ruff check services/orchestrator && uv run --no-sync ruff format services/orchestrator
uv run --no-sync mypy services/orchestrator/src
git add services/orchestrator
git commit -m "feat(orchestrator): P-1 agent_node 在 working_window 之前整轮剔除被取代消息(探针测试断言 LLM 输入)"
```

---

### Task 7: `spawn_run(supersede=)` + `:regenerate` 重放输入 + queue worker 重放分支 + 锁包住「supersede → 建行」

**Files:**
- Modify: `services/control-plane/src/control_plane/api/runs.py`(`build_run_graph_input` `:455-504` 之后加 `SupersedeRequest` / `replay_graph_input`;`spawn_run` `:953-1206`:签名、audit details、`run_id = uuid4()` 之后到两分支建行)
- Modify: `services/control-plane/src/control_plane/run_queue_worker.py:313-328`(重放分支)
- Test: `services/control-plane/tests/test_spawn_run_supersede.py`(新)、`services/control-plane/tests/test_run_queue_worker_replay.py`(新)

**Interfaces:**
- Consumes: Task 4 `supersede_run` / `supersede_thread_lock` / `SupersedeResult` / `SupersedeError`;Task 2 `RunManager.create/enqueue(regenerated_from_run_id=)`;`app.state.session_factory`(`app.py:2406`)、`app.state.thread_message_store`(`:2421`)、`app.state.thread_meta_repo`、`runtime.run_manager.store`
- Produces:
  - `SupersedeRequest(target_run_id: UUID, replay: bool)`(frozen dataclass,`api/runs.py`)
  - `spawn_run(..., supersede: SupersedeRequest | None = None)`;`supersede` 非空时:锁内 `supersede_run(require_replay=supersede.replay)` → 建行(`regenerated_from_run_id=supersede.target_run_id`);`SupersedeError` 原样抛给调用方
  - `replay_graph_input(built: Any, replay: Sequence[BaseMessage], *, run_id: UUID) -> dict[str, Any]`:两条消息换新 id、Human 重新盖戳,**不写** `turn_documents` / `turn_image_refs`(检查点保留被取代轮自己的值 = 同一批附件)
  - queue 模式 `enqueued_input = {"replay_messages": [message_to_dict(m), …]}`;worker 见到 `replay_messages` 走 `replay_graph_input`

- [ ] **Step 1: 写失败测试(spawn_run 顺序 + 重放输入形状)**

```python
# services/control-plane/tests/test_spawn_run_supersede.py
"""P-1 —— spawn_run(supersede=):锁内「supersede → 建行」顺序、regenerated_from、重放输入。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from control_plane.api import runs as runs_mod
from control_plane.api.runs import SupersedeRequest, replay_graph_input
from control_plane.supersede import SupersedeError, SupersedeResult, TurnLocation
from expert_work.common.conversation_channel import SUPERSEDED_BY
from expert_work.common.message_stamp import STAMP_RUN_ID, stamp_message
from expert_work.common.supersede import mark_superseded


def _built() -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(max_steps=5, max_no_progress=0)


def test_replay_graph_input_fresh_ids_new_stamp_no_attachment_keys() -> None:
    old_run, new_run = uuid4(), uuid4()
    system = SystemMessage(content="sys", id="sys-old")
    human = mark_superseded(
        stamp_message(HumanMessage(content="U", id="h-old"), run_id=str(old_run), now=datetime(2026, 9, 1, tzinfo=UTC)),
        new_run_id=str(new_run), now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    out = replay_graph_input(_built(), [system, human], run_id=new_run)
    sys_out, human_out = out["messages"]
    assert sys_out.content == "sys" and sys_out.id not in (None, "sys-old")
    assert human_out.content == "U" and human_out.id not in (None, "h-old")
    assert human_out.additional_kwargs[STAMP_RUN_ID] == str(new_run)
    assert SUPERSEDED_BY not in human_out.additional_kwargs
    assert out["step_count"] == 0 and out["max_steps"] == 5
    assert "turn_documents" not in out and "turn_image_refs" not in out


class _Recorder:
    """记 supersede_run / create / enqueue 的先后。"""

    def __init__(self) -> None:
        self.order: list[str] = []


@pytest.mark.asyncio
async def test_spawn_run_supersedes_before_creating_the_row(monkeypatch: pytest.MonkeyPatch, spawn_ctx: Any) -> None:
    """``spawn_ctx`` 夹具见下:一个能直接调 spawn_run 的最小 app 环境(照 test_runs_api.py 的 ctx 拼)。"""
    rec = _Recorder()
    target = uuid4()

    async def fake_supersede_run(**kwargs: Any) -> SupersedeResult:
        rec.order.append("supersede")
        assert kwargs["target_run_id"] == target and kwargs["require_replay"] is True
        return SupersedeResult(
            location=TurnLocation(start=0, end=2, plan_before=None, chain_run_ids=(target,)),
            replay_messages=(SystemMessage(content="sys"), HumanMessage(content="U")),
            superseded_run_ids=(target,),
        )

    real_enqueue = spawn_ctx.runtime.run_manager.enqueue

    async def spy_enqueue(**kwargs: Any) -> None:
        rec.order.append("enqueue")
        assert kwargs["regenerated_from_run_id"] == target
        assert set(kwargs["enqueued_input"]) == {"replay_messages"}
        await real_enqueue(**kwargs)

    monkeypatch.setattr(runs_mod, "supersede_run", fake_supersede_run)
    monkeypatch.setattr(spawn_ctx.runtime.run_manager, "enqueue", spy_enqueue)
    resp = await spawn_ctx.spawn(mode="queue", supersede=SupersedeRequest(target_run_id=target, replay=True))
    assert resp.status_code == 202
    assert rec.order == ["supersede", "enqueue"]


@pytest.mark.asyncio
async def test_spawn_run_propagates_supersede_error_without_creating_a_row(monkeypatch: pytest.MonkeyPatch, spawn_ctx: Any) -> None:
    async def fake_supersede_run(**kwargs: Any) -> SupersedeResult:
        raise SupersedeError("THREAD_BUSY", "busy", 409)

    monkeypatch.setattr(runs_mod, "supersede_run", fake_supersede_run)
    before = len(await spawn_ctx.run_store.list_by_thread(thread_id=spawn_ctx.thread_id, tenant_id=spawn_ctx.tenant_id))
    with pytest.raises(SupersedeError):
        await spawn_ctx.spawn(mode="queue", supersede=SupersedeRequest(target_run_id=uuid4(), replay=False))
    after = len(await spawn_ctx.run_store.list_by_thread(thread_id=spawn_ctx.thread_id, tenant_id=spawn_ctx.tenant_id))
    assert after == before
```

`spawn_ctx` 夹具(同文件;app 构造照 `test_external_runs_cancel.py:137-165`,`_SPEC` / `_spec()` / `_build_settings()` 从那份文件抄):

```python
from starlette.requests import Request

from control_plane.api.runs import RunRequest, spawn_run


class _SpawnCtx:
    def __init__(self, app: Any, tenant_id: UUID, thread_id: UUID, run_store: Any) -> None:
        self.app, self.tenant_id, self.thread_id, self.run_store = app, tenant_id, thread_id, run_store
        self.runtime = app.state.agent_runtime

    async def spawn(self, *, mode: str, supersede: SupersedeRequest | None) -> Any:
        record = (await self.app.state.agent_spec_repo.list_by_tenant(tenant_id=self.tenant_id, name="support-bot", limit=1))[0]
        built = await self.runtime.get_agent(tenant_id=self.tenant_id, name="support-bot", version=record.version, spec=record.spec, user_id=None)
        # queue 分支只读 request.app.state;stream 分支还读 headers / is_disconnected —— 本文件只测 queue。
        request = Request({"type": "http", "app": self.app, "headers": [], "method": "POST", "path": "/", "query_string": b""})
        return await spawn_run(
            runtime=self.runtime, audit=self.app.state.audit_logger, approvals=self.app.state.approval_store,
            request=request, settings=self.app.state.settings, built=built, record_spec=record.spec,
            thread_id=self.thread_id, tenant_id=self.tenant_id, actor_id="sa-test", effective_user_id=None,
            oauth_subject="sa-test", payload=RunRequest(input="U", mode=mode), trace_id="0" * 32, supersede=supersede,
        )


@pytest.fixture
async def spawn_ctx() -> AsyncIterator[_SpawnCtx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    app = create_app(settings=_build_settings(), lifecycle=lifecycle, jwt_verifier=build_test_jwt_verifier(),
                     audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
                     agent_runtime=stub_agent_runtime(run_store=run_store), run_repo=run_store)
    tenant_id = uuid4()
    await app.state.agent_spec_repo.create(tenant_id=tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed")
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(ThreadMeta(thread_id=thread_id, tenant_id=tenant_id, created_at=datetime.now(UTC),
                                                       agent_name="support-bot", agent_version="1.0.0"))
    yield _SpawnCtx(app, tenant_id, thread_id, run_store)
```

`ThreadMeta` 的其余必填字段以 `packages/expert-work-protocol/src/expert_work/protocol/thread_meta.py:25-60` 为准(`status` 默认 ACTIVE 时不用传;若必填则传 `ThreadStatus.ACTIVE`)。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest services/control-plane/tests/test_spawn_run_supersede.py -q`
Expected: FAIL,`ImportError: cannot import name 'SupersedeRequest'`。

- [ ] **Step 3: `SupersedeRequest` + `replay_graph_input`**

`api/runs.py`,紧接 `build_run_graph_input` 之后:

```python
@dataclass(frozen=True)
class SupersedeRequest:
    """P-1 —— 让 ``spawn_run`` 先把 ``target_run_id`` 这一轮标成被本次新 run 取代。

    ``replay=True``(``:regenerate``)= 新一轮的输入就是旧轮的 System + Human
    两条原件;``False``(``:edit``)= 调用方给了新 ``payload.input``。
    """

    target_run_id: UUID
    replay: bool


def replay_graph_input(built: Any, replay: Sequence[BaseMessage], *, run_id: UUID) -> dict[str, Any]:
    """``:regenerate`` 的图输入:旧轮的 [System, Human] 原件换**新 id**、Human 重新盖戳。

    id 必须换:``add_messages`` 同 id 是原地替换(supersede 正是靠这一点),不换
    id 这两条会顶掉旧轮的位置而不是追加成新轮。``expert_work_created_at`` /
    ``expert_work_run_id`` 由 ``stamp_message`` 覆盖为本轮;被取代标记(若调用方
    传进来的是打标后的副本)一并剥掉。

    **故意不写** ``turn_documents`` / ``turn_image_refs``:``build_run_graph_input``
    每轮都写这两个键是为了不让上一轮附件漏进这一轮;重新生成要的恰恰是「同一批
    附件」,而检查点里此刻的值就是被取代轮自己写的那一份 —— 省略键 = 沿用。
    """
    now = datetime.now(UTC)
    fresh: list[BaseMessage] = []
    for msg in replay:
        kwargs = {k: v for k, v in msg.additional_kwargs.items() if k not in (SUPERSEDED_BY, SUPERSEDED_AT)}
        fresh.append(msg.model_copy(update={"id": str(uuid4()), "additional_kwargs": kwargs}))
    system, human = fresh
    return {
        "messages": [system, stamp_message(human, run_id=str(run_id), now=now)],
        "step_count": 0,
        "max_steps": built.max_steps,
        "max_no_progress": built.max_no_progress,
    }
```

`api/runs.py` 新增 import(`SupersedeRequest` 定义在本文件,不从别处 import):`from contextlib import nullcontext`、`from dataclasses import dataclass`、`from collections.abc import Sequence`、`from langchain_core.messages import BaseMessage, message_to_dict`(`SystemMessage` 已有)、`from expert_work.common.conversation_channel import SUPERSEDED_AT, SUPERSEDED_BY`、`from control_plane.supersede import supersede_run, supersede_thread_lock`。

- [ ] **Step 4: `spawn_run` 改造**

签名末尾加 `supersede: SupersedeRequest | None = None`;docstring 加一段「P-1」。`emit(...)` 的 `details` 加 `**({"supersedes_run_id": str(supersede.target_run_id), "replay": supersede.replay} if supersede is not None else {})`。把 `run_id = uuid4()` 到 stream 分支 `run_record = await runtime.run_manager.create(...)` 这一段改成:

```python
    run_id = uuid4()
    prior_runs = await runtime.run_manager.list_by_thread(thread_id, tenant_id=tenant_id)

    # P-1 —— supersede 与建行必须在同一把 per-thread 锁里(spec §8-4):queue worker
    # 只认已存在的 QUEUED 行,行在 supersede 全部写完之后才 INSERT,worker 抢不到
    # 「标记未落、run 已跑」的窗口;两副本并发 supersede 同一轮,后进的锁内看到
    # 已链接的后继 → 409。没有 supersede 时 nullcontext,原路径一字节不变。
    state = request.app.state
    lock = (
        supersede_thread_lock(state.session_factory, thread_id)
        if supersede is not None
        else nullcontext()
    )
    async with lock:
        replay_messages: tuple[BaseMessage, BaseMessage] | None = None
        regenerated_from: UUID | None = None
        if supersede is not None:
            result = await supersede_run(
                graph=built.graph,
                thread_id=thread_id,
                tenant_id=tenant_id,
                target_run_id=supersede.target_run_id,
                new_run_id=run_id,
                runs=runtime.run_manager.store,
                approvals=approvals,
                thread_messages=state.thread_message_store,
                threads=state.thread_meta_repo,
                require_replay=supersede.replay,
            )
            replay_messages = result.replay_messages
            regenerated_from = supersede.target_run_id

        # Stream 9.5 — queue mode: persist as ``queued`` + return 202.
        if payload.mode == "queue":
            enqueued_input: dict[str, Any] = {
                "input": payload.input,
                "image_refs": payload.image_refs,
                "untrusted_content": payload.untrusted_content,
                "inputs": payload.inputs,
                "document_names": payload.document_names,
            }
            if replay_messages is not None:
                enqueued_input = {"replay_messages": [message_to_dict(m) for m in replay_messages]}
            await runtime.run_manager.enqueue(
                run_id=run_id,
                thread_id=thread_id,
                tenant_id=tenant_id,
                user_id=effective_user_id,
                enqueued_input=enqueued_input,
                is_resume=bool(prior_runs),
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                regenerated_from_run_id=regenerated_from,
            )
            logger.info("control_plane.run.enqueued run_id=%s", run_id)
            content: dict[str, Any] = {"run_id": str(run_id), "thread_id": str(thread_id), "status": "queued"}
            if envelope:
                content = {"success": True, "data": content, "error": None}
            return JSONResponse(status_code=202, content=content)

        run_record = await runtime.run_manager.create(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=effective_user_id,
            on_disconnect=on_disconnect,
            is_resume=bool(prior_runs),
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            regenerated_from_run_id=regenerated_from,
        )
```

之后的 `graph_input = build_run_graph_input(...)` 改成:

```python
    if replay_messages is not None:
        graph_input = replay_graph_input(built, replay_messages, run_id=run_id)
    else:
        graph_input = build_run_graph_input(
            built,
            input_text=payload.input,
            image_refs=payload.image_refs,
            untrusted_content=payload.untrusted_content,
            inputs=payload.inputs,
            run_id=run_id,
            document_names=payload.document_names,
        )
```

`from contextlib import nullcontext`。`app.state.session_factory` 在内存栈是 `None` → 锁 no-op(单测形态)。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --no-sync pytest services/control-plane/tests/test_spawn_run_supersede.py services/control-plane/tests/test_runs_api.py services/control-plane/tests/test_external_idempotency.py -q`
Expected: 全绿。

- [ ] **Step 6: 变异自证**

把 `await supersede_run(...)` 挪到 `async with lock:` 之后、`enqueue` 之后(即先建行再 supersede)→ `rec.order == ["supersede", "enqueue"]` 红;改回。`replay_graph_input` 里去掉 `"id": str(uuid4())` → `sys_out.id not in (None, "sys-old")` 红;改回。

- [ ] **Step 7: queue worker 重放分支 + 测试**

`run_queue_worker.py` `:313-328` 改成:

```python
            payload = run.enqueued_input or {}
            replay = payload.get("replay_messages")
            if replay:
                # P-1 ``:regenerate`` queue 模式:spawn_run 把旧轮的 [System, Human]
                # 原件序列化进来;这里反序列化后走同一个 replay_graph_input。
                graph_input = replay_graph_input(built, messages_from_dict(replay), run_id=run.run_id)
            else:
                document_names = list(payload.get("document_names") or [])
                image_refs = list(payload.get("image_refs") or [])
                graph_input = build_run_graph_input(
                    built,
                    input_text=payload.get("input"),
                    image_refs=image_refs,
                    untrusted_content=payload.get("untrusted_content"),
                    inputs=payload.get("inputs") or {},
                    run_id=run.run_id,
                    document_names=document_names,
                )
```

import `from control_plane.api.runs import build_run_graph_input, replay_graph_input`、`from langchain_core.messages import messages_from_dict`。

```python
# services/control-plane/tests/test_run_queue_worker_replay.py
"""P-1 —— queue worker 对 enqueued_input.replay_messages 的重放分支。"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, message_to_dict

from control_plane import run_queue_worker as worker_mod
from expert_work.common.message_stamp import STAMP_RUN_ID


@pytest.mark.asyncio
async def test_worker_uses_replay_graph_input_when_present(monkeypatch: pytest.MonkeyPatch, worker_ctx: Any) -> None:
    """``worker_ctx``:照 test_run_queue_worker.py 现有夹具 —— 一个 RunQueueWorker + InMemoryRunStore + stub runtime。"""
    captured: dict[str, Any] = {}

    async def fake_run_agent(**kwargs: Any) -> None:
        captured["graph_input"] = kwargs["graph_input"]

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    run_id = uuid4()
    await worker_ctx.enqueue(run_id, enqueued_input={"replay_messages": [
        message_to_dict(SystemMessage(content="sys")), message_to_dict(HumanMessage(content="U-old")),
    ]})
    assert await worker_ctx.worker.run_once() == 1
    msgs = captured["graph_input"]["messages"]
    assert [type(m).__name__ for m in msgs] == ["SystemMessage", "HumanMessage"]
    assert msgs[1].content == "U-old" and msgs[1].additional_kwargs[STAMP_RUN_ID] == str(run_id)
    assert "turn_documents" not in captured["graph_input"]


def test_replay_messages_survive_the_jsonb_round_trip() -> None:
    """``:regenerate`` queue 分支的存亡判据:旧轮 [System, Human] 原件经
    ``message_to_dict`` → JSON(JSONB 列)→ ``messages_from_dict`` 之后,``id`` /
    ``additional_kwargs``(run 戳 + 被取代标记)/ 多段 ``content``(text + image 块,
    ``build_run_graph_input`` 在 ``supports_vision=True`` 下的真实产出)逐项相等。
    只有 langchain 文档背书的往返,这里用真实形状钉死。"""
    import json
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from langchain_core.messages import messages_from_dict

    from control_plane.api.runs import build_run_graph_input
    from expert_work.common.conversation_channel import SUPERSEDED_AT, SUPERSEDED_BY
    from expert_work.common.supersede import mark_superseded

    built = SimpleNamespace(supports_vision=True, spotlight_nonce=None, max_steps=5, max_no_progress=0,
                            system_prompt="You are the replay probe.", prompt_jinja=False)
    tenant_id, thread_id, old_run, new_run = uuid4(), uuid4(), uuid4(), uuid4()
    image_ref = f"expert_work://image/{tenant_id}/{thread_id}/{uuid4()}.png"
    graph_input = build_run_graph_input(
        built, input_text="看一下这张图", image_refs=[image_ref], untrusted_content=["<ticket>外部文本</ticket>"],
        run_id=old_run, document_names=["report.pdf"],
    )
    system, human = graph_input["messages"]
    assert isinstance(human.content, list) and len(human.content) >= 2            # 真实多段形态,不是自己编的
    system = system.model_copy(update={"id": "sys-id-1"})                           # reducer 补 id 之前就带 id 的形态也要过
    human = mark_superseded(human.model_copy(update={"id": "human-id-1"}), new_run_id=str(new_run), now=datetime(2026, 9, 10, tzinfo=UTC))

    wire = json.loads(json.dumps([message_to_dict(m) for m in (system, human)]))   # 走一遍 JSON,与 JSONB 列同形
    back_system, back_human = messages_from_dict(wire)

    assert (back_system.id, back_human.id) == ("sys-id-1", "human-id-1")
    assert back_system.content == system.content
    assert back_human.content == human.content                                     # 每个 block 的 type / text / image 引用逐项相等
    assert back_human.additional_kwargs == human.additional_kwargs                 # STAMP_RUN_ID / created_at / SUPERSEDED_BY / SUPERSEDED_AT 全在
    assert back_human.additional_kwargs[STAMP_RUN_ID] == str(old_run)
    assert back_human.additional_kwargs[SUPERSEDED_BY] == str(new_run) and SUPERSEDED_AT in back_human.additional_kwargs
    assert type(back_system).__name__ == "SystemMessage" and type(back_human).__name__ == "HumanMessage"
```

`worker_ctx` 夹具照 `services/control-plane/tests/test_run_queue_worker.py`(`ls services/control-plane/tests | rg run_queue_worker` 找到的那份)里构造 `RunQueueWorker` 的写法抄,并暴露 `enqueue(run_id, enqueued_input)`(调 `runtime.run_manager.enqueue(..., thread_id=<seed 的 thread>, tenant_id=...)`)与 `worker`。

Run: `uv run --no-sync pytest services/control-plane/tests/test_run_queue_worker_replay.py services/control-plane/tests/test_run_queue_worker.py -q`
Expected: 全绿。变异:worker 里 `if replay:` 改 `if False:` → 断言 `msgs[1].content == "U-old"` 红(走了 `input=None` 的普通路径);改回。往返测试:在 `wire = …` 之后临时加 `wire[0]["data"]["id"] = None; wire[1]["data"]["id"] = None`(模拟序列化丢 id)→ `(back_system.id, back_human.id) == ("sys-id-1", "human-id-1")` 红;再把 `wire[1]["data"]["additional_kwargs"].pop(SUPERSEDED_BY)` → `additional_kwargs ==` 那条红;两处都去掉 → 绿。

- [ ] **Step 8: lint / 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane
git commit -m "feat(runs): P-1 spawn_run(supersede=) —— 锁内先取代再建行、regenerated_from、:regenerate 原件重放(stream + queue worker)"
```

**PR2 到此**(Task 6-7)。验收清单(spec §7 PR2):新 run 的模型上下文不含被取代轮文本与计划(Task 6 探针);必调工具的 agent 编辑重发后厂商不 400(Task 6 工具对成对消失);在飞 run 409 / 两副本并发只成一条 / PAUSED 409(Task 4 集成测,PR1 已合)。

---

### Task 8: 对外端点 `:regenerate` / `:edit`(`external_runs.py`)+ `agents.py` 抽两个共享函数

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py:1530-1621`(把「files → image_refs/document_names」与「untrusted/inputs 上限」两段抽成模块级函数,`run_agent_for_user` 改调)
- Modify: `services/control-plane/src/control_plane/api/external_runs.py`(两个请求模型 + 共享处理函数 + 两条路由,放在 `:cancel` `:169-226` 之后)
- Test: `services/control-plane/tests/test_external_runs_regenerate.py`(新)

**Interfaces:**
- Consumes: Task 7 `spawn_run(supersede=)` / `SupersedeRequest`;Task 4 `SupersedeError`;`_external.load_owned_run` / `external_error` / `reject_nul` / `reject_nul_deep`;`agents._envelope_error` / `_idempotent_run_response` / `ExternalFileRef`;`_idempotency.request_digest` / `IDEMPOTENCY_HEADER` / `MAX_IDEMPOTENCY_KEY_LEN`;`_quota_admission.check_admission`;`_run_event_stream.EXTERNAL_HIDDEN_EVENTS`;`orchestrator.stream_items.STREAM_FORMAT_LEGACY/ITEMS`
- Produces:
  - `agents.resolve_external_files(*, files: Sequence[ExternalFileRef], tenant_id: UUID, end_user_id: UUID, thread_id: UUID, uploads_store: UserUploadStore) -> tuple[list[str], list[str]]`(抛 `ExternalScopeError` / `HTTPException`,语义与今天 `:1544-1577` 完全一致)
  - `agents.external_run_bounds_error(*, untrusted_content: Sequence[str], inputs: Mapping[str, Any]) -> JSONResponse | None`(今天 `:1584-1621` 两段)
  - `POST /v1/agents/{agent_code}/runs/{run_id}:regenerate`,body `ExternalRegenerateRequest{user_id, mode?, stream_format?}`
  - `POST /v1/agents/{agent_code}/runs/{run_id}:edit`,body `ExternalEditRequest{user_id, input(必填), mode?, stream_format?, untrusted_content?, inputs?, files?}`
  - 响应 = `POST …/runs`;错误信封 `{success:false, data:null, error:{code,message}}`

- [ ] **Step 1: 抽共享函数(纯搬家,先跑既有测试守住)**

`agents.py` 在 `_safe_document_name_or_422`(`:336`)之后加:

```python
async def resolve_external_files(
    *,
    files: Sequence[ExternalFileRef],
    tenant_id: UUID,
    end_user_id: UUID,
    thread_id: UUID,
    uploads_store: UserUploadStore,
) -> tuple[list[str], list[str]]:
    """``files[]`` → 内部 ``(image_refs, document_names)``(对外附件模型统一,Task 3)。

    从 ``run_agent_for_user`` 抽出,P-1 的 ``:edit`` 端点复用同一份查表分流;
    语义一字不改:格式不对 → 422 INVALID_UPLOAD_ID;查不到 / 不属于这个
    end_user / 已软删 → 404 UPLOAD_NOT_FOUND;图片行还必须绑本会话;文档名过
    ``_safe_document_name_or_422``;图片数超上限 → 422 TOO_MANY_IMAGE_REFS。
    抛 :class:`ExternalScopeError` 或 :class:`HTTPException`,由端点各自渲染。
    """
    image_refs: list[str] = []
    document_names: list[str] = []
    for item in files:
        uid = parse_upload_id(item.upload_id)
        if uid is None:
            raise ExternalScopeError(
                "INVALID_UPLOAD_ID",
                "upload_id must be the value returned by POST /v1/agents/{agent_code}/uploads",
                422,
            )
        row = await uploads_store.get(upload_id=uid, tenant_id=tenant_id)
        if row is None or row.user_id != end_user_id or row.deleted_at is not None:
            raise ExternalScopeError("UPLOAD_NOT_FOUND", "upload not found", 404)
        if row.kind == "image":
            if row.thread_id != thread_id:
                raise ExternalScopeError("UPLOAD_NOT_FOUND", "upload not found", 404)
            image_refs.append(row.ref)
        else:
            document_names.append(_safe_document_name_or_422(row.ref))
    if len(image_refs) > MAX_RUN_IMAGE_REFS:
        raise ExternalScopeError("TOO_MANY_IMAGE_REFS", f"files[] 里的图片不能超过 {MAX_RUN_IMAGE_REFS} 张", 422)
    return image_refs, document_names


def external_run_bounds_error(
    *, untrusted_content: Sequence[str], inputs: Mapping[str, Any]
) -> JSONResponse | None:
    """手工构造 ``RunRequest`` 之前的三条上限预检(P2-a 安全修复);超限返回 422 信封,否则 ``None``。"""
    for idx, block in enumerate(untrusted_content):
        if len(block) > MAX_UNTRUSTED_CONTENT_BLOCK_CHARS:
            return _envelope_error(
                "UNTRUSTED_CONTENT_BLOCK_TOO_LONG",
                f"untrusted_content[{idx}] 超过 {MAX_UNTRUSTED_CONTENT_BLOCK_CHARS} 字符",
                422,
            )
    violation = check_run_inputs_bound(dict(inputs), check_total_bytes=True)
    if violation is None:
        return None
    if violation.kind == "too_many_keys":
        return _envelope_error("TOO_MANY_INPUT_KEYS", f"inputs 最多 {MAX_RUN_INPUT_KEYS} 个键", 422)
    if violation.kind == "value_too_long":
        return _envelope_error(
            "INPUT_VALUE_TOO_LONG", f"inputs['{violation.key}'] 超过 {MAX_RUN_INPUT_VALUE_CHARS} 字符", 422
        )
    return _envelope_error(
        "TOO_MANY_INPUT_BYTES", f"inputs 序列化后总大小不能超过 {MAX_RUN_INPUT_TOTAL_BYTES} 字节", 422
    )
```

`run_agent_for_user` 里 `:1540-1621` 替换为:

```python
        uploads_store: UserUploadStore = request.app.state.user_upload_store
        try:
            image_refs, document_names = await resolve_external_files(
                files=payload.files, tenant_id=tenant_id, end_user_id=end_user_id, thread_id=thread_id, uploads_store=uploads_store
            )
        except ExternalScopeError as exc:
            return _envelope_error(exc.code, exc.message, exc.status_code)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return _envelope_error(detail.get("code", "INVALID_FILE_REF"), detail.get("message", "invalid file reference"), exc.status_code)
        bounds = external_run_bounds_error(untrusted_content=payload.untrusted_content, inputs=payload.inputs)
        if bounds is not None:
            return bounds
```

(原注释块可保留在函数 docstring 里。)Run:`uv run --no-sync pytest services/control-plane/tests/test_external_idempotency.py services/control-plane/tests/test_external_uploads.py services/control-plane/tests/test_external_run_inputs.py -q`(后两个文件名以 `ls services/control-plane/tests | rg "external_upload|run_input|external_run"` 的实际结果为准,全部跑)。Expected:全绿。提交:`git commit -m "refactor(agents): 抽 resolve_external_files / external_run_bounds_error 供 P-1 端点复用(行为不变)"`。

- [ ] **Step 2: 写失败测试(端到端:真 supersede + 假 LLM)**

```python
# services/control-plane/tests/test_external_runs_regenerate.py
"""P-1 —— POST /v1/agents/{code}/runs/{run_id}:regenerate / :edit。

夹具照 test_external_runs_cancel.py(内存 store + stub_agent_runtime + 服务账号 JWT);
stub 的图跑在 InMemorySaver 上,aget_state_history / aupdate_state 都可用,所以
「先跑一轮再 :regenerate」是**真** supersede 内核 + 假 LLM 的端到端。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.api import external_runs as external_runs_mod
from control_plane.supersede import SupersedeError


async def _first_turn(ctx: Any, user_id: str = "cust-77") -> tuple[UUID, UUID]:
    """queue 模式起第一轮并让它跑完;返回 (thread_id, run_id)。"""
    resp = await ctx.client.post("/v1/agents/support-bot/runs", json={"user_id": user_id, "input": "hello", "mode": "stream"}, headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    async for _ in resp.aiter_lines():
        pass
    run_id = UUID(resp.headers["X-Expert-Work-Run-Id"])
    thread_id = UUID(resp.headers["X-Expert-Work-Session-Id"])
    await ctx.wait_terminal(run_id)
    return thread_id, run_id


@pytest.mark.asyncio
async def test_regenerate_marks_old_turn_and_runs_a_new_one(ctx: Any) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _first_turn(ctx)
    resp = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:regenerate", json={"user_id": "cust-77", "mode": "queue"}, headers=ctx.headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["success"] is True and body["data"]["thread_id"] == str(thread_id)
    r2 = UUID(body["data"]["run_id"])
    old = await ctx.run_store.get(run_id=r1, tenant_id=ctx.tenant_id)
    new = await ctx.run_store.get(run_id=r2, tenant_id=ctx.tenant_id)
    assert old.superseded_by_run_id == r2 and new.regenerated_from_run_id == r1
    assert set(new.enqueued_input) == {"replay_messages"}
    msgs = await ctx.client.get(f"/v1/agents/support-bot/sessions/{thread_id}/messages", params={"user_id": "cust-77"}, headers=ctx.headers)
    rows = msgs.json()["data"]["messages"]
    assert rows and all(r["superseded_by"] == str(r2) for r in rows)


@pytest.mark.asyncio
async def test_edit_uses_the_new_input(ctx: Any) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _first_turn(ctx)
    resp = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:edit", json={"user_id": "cust-77", "input": "hello again", "mode": "queue"}, headers=ctx.headers)
    assert resp.status_code == 202, resp.text
    r2 = UUID(resp.json()["data"]["run_id"])
    new = await ctx.run_store.get(run_id=r2, tenant_id=ctx.tenant_id)
    assert new.enqueued_input["input"] == "hello again" and new.regenerated_from_run_id == r1


@pytest.mark.asyncio
async def test_edit_requires_input(ctx: Any) -> None:
    await ctx.seed_agent()
    _thread, r1 = await _first_turn(ctx)
    resp = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:edit", json={"user_id": "cust-77"}, headers=ctx.headers)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_other_user_and_other_agent_are_404_envelopes(ctx: Any) -> None:
    await ctx.seed_agent()
    _thread, r1 = await _first_turn(ctx)
    for path, body in (
        (f"/v1/agents/support-bot/runs/{r1}:regenerate", {"user_id": "someone-else"}),
        (f"/v1/agents/other-bot/runs/{r1}:regenerate", {"user_id": "cust-77"}),
        (f"/v1/agents/support-bot/runs/{uuid4()}:edit", {"user_id": "cust-77", "input": "x"}),
    ):
        resp = await ctx.client.post(path, json=body, headers=ctx.headers)
        assert resp.status_code == 404, resp.text
        assert resp.json() == {"success": False, "data": None, "error": {"code": "RUN_NOT_FOUND", "message": "run not found"}}


@pytest.mark.asyncio
async def test_not_last_run_is_422(ctx: Any) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _first_turn(ctx)
    resp = await ctx.client.post("/v1/agents/support-bot/runs", json={"user_id": "cust-77", "session_id": str(thread_id), "input": "second", "mode": "stream"}, headers=ctx.headers)
    async for _ in resp.aiter_lines():
        pass
    await ctx.wait_terminal(UUID(resp.headers["X-Expert-Work-Run-Id"]))
    resp = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:regenerate", json={"user_id": "cust-77"}, headers=ctx.headers)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "RUN_NOT_LAST"


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "status"), [("THREAD_BUSY", 409), ("RUN_AWAITING_APPROVAL", 409), ("RUN_ALREADY_SUPERSEDED", 409), ("RUN_INPUT_UNAVAILABLE", 422)])
async def test_kernel_errors_map_to_envelopes(ctx: Any, monkeypatch: pytest.MonkeyPatch, code: str, status: int) -> None:
    await ctx.seed_agent()
    _thread, r1 = await _first_turn(ctx)

    async def boom(**kwargs: Any) -> Any:
        raise SupersedeError(code, "why", status)

    monkeypatch.setattr(external_runs_mod, "spawn_run", boom)
    resp = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:regenerate", json={"user_id": "cust-77"}, headers=ctx.headers)
    assert resp.status_code == status and resp.json()["error"] == {"code": code, "message": "why"}
```

`ctx` 夹具:照 `test_external_runs_cancel.py:106-165`,另加 `wait_terminal(run_id)`(轮询 `run_store.get` 直到 `status in TERMINAL_RUN_STATUSES`,最多 5 秒)。stream 模式 `POST /runs` 用 `aiter_lines` 排空 SSE 直到 `end`。`stub_agent_runtime` 的假 LLM 一步收尾,首轮 3 条消息(System / Human / AI)。

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_runs_regenerate.py -q`
Expected: FAIL,`:regenerate` 返回 404(路由不存在 —— 注意是 FastAPI 的 404 而非信封,断言 `body["success"]` 处 KeyError)。

- [ ] **Step 4: 请求模型 + 共享处理 + 两条路由**

`external_runs.py` 加(imports 见代码内注释):

```python
from typing import Annotated, Any, Literal
from collections.abc import Mapping

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.api._external import ExternalScopeError, external_error, load_owned_run, load_owned_session, lookup_external_user_id, reject_nul, reject_nul_deep, reject_nul_path_params
from control_plane.api._idempotency import IDEMPOTENCY_HEADER, MAX_IDEMPOTENCY_KEY_LEN, request_digest
from control_plane.api._quota_admission import check_admission
from control_plane.api._run_event_stream import EXTERNAL_HIDDEN_EVENTS
from control_plane.api.agents import ExternalFileRef, _envelope_error, _idempotent_run_response, external_run_bounds_error, resolve_external_files
from control_plane.api.runs import MAX_RUN_INPUT_CHARS, RunRequest, SupersedeRequest, spawn_run
from control_plane.supersede import SupersedeError
from expert_work.protocol import AgentSpecStatus
from expert_work.runtime.runs import DisconnectMode
from expert_work.runtime.runs.store import RunIdempotencyConflict
from orchestrator.stream_items import STREAM_FORMAT_ITEMS, STREAM_FORMAT_LEGACY


class ExternalRegenerateRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}:regenerate`` —— 同一输入再跑一次。

    ``input`` / ``files`` **不接受**(``extra="forbid"``):输入就是被取代那一轮的
    原件,含附件引用;要改输入用 ``:edit``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    mode: Literal["stream", "queue"] = "stream"
    stream_format: Literal[STREAM_FORMAT_LEGACY, STREAM_FORMAT_ITEMS] = STREAM_FORMAT_LEGACY


class ExternalEditRequest(BaseModel):
    """Body for ``…:edit`` —— 改输入后再跑一次。字段语义与 ``POST …/runs`` 同名字段完全一致。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    input: str = Field(min_length=1, max_length=MAX_RUN_INPUT_CHARS)
    mode: Literal["stream", "queue"] = "stream"
    stream_format: Literal[STREAM_FORMAT_LEGACY, STREAM_FORMAT_ITEMS] = STREAM_FORMAT_LEGACY
    untrusted_content: list[str] = Field(default_factory=list, max_length=16)
    inputs: dict[str, Any] = Field(default_factory=dict)
    files: list[ExternalFileRef] = Field(default_factory=list, max_length=64)

    @field_validator("input")
    @classmethod
    def _input_no_nul(cls, value: str) -> str:
        return reject_nul(value, field="input")

    @field_validator("untrusted_content", "inputs")
    @classmethod
    def _deep_no_nul(cls, value: Any) -> Any:
        return reject_nul_deep(value, field="untrusted_content/inputs")


def _get_runtime(request: Request) -> Any:
    return request.app.state.agent_runtime


async def _supersede_and_run(
    *,
    op: Literal["regenerate", "edit"],
    agent_code: str,
    run_id: UUID,
    user_id: str,
    mode: Literal["stream", "queue"],
    stream_format: str,
    payload: ExternalRegenerateRequest | ExternalEditRequest,
    request: Request,
    runs: RunStore,
    threads: ThreadMetaStore,
    users: TenantUserStore,
    idempotency_key: str | None,
) -> StreamingResponse | JSONResponse:
    """两端点共用:幂等 → 归属 → agent 状态 → 配额 → 构建 → (edit: 附件/上限)→ spawn_run(supersede=)。

    顺序照 ``agents.run_agent_for_user``:幂等命中在任何副作用之前返回;归属 404
    不泄露存在性;``SupersedeError`` 在 ``spawn_run`` 里从内核抛出、这里渲染成信封。
    """
    state = request.app.state
    tenant_id: UUID = request.state.tenant_id
    actor_id: str = request.state.actor_id
    trace_id = current_trace_id_hex()
    runtime = state.agent_runtime

    key: str | None = None
    digest: str | None = None
    if idempotency_key is not None:
        key = idempotency_key.strip()
        if not key or len(key) > MAX_IDEMPOTENCY_KEY_LEN:
            return _envelope_error("INVALID_IDEMPOTENCY_KEY", f"Idempotency-Key must be 1-{MAX_IDEMPOTENCY_KEY_LEN} non-blank characters", 422)
        try:
            reject_nul(key, field="Idempotency-Key")
        except ValueError as exc:
            return _envelope_error("INVALID_IDEMPOTENCY_KEY", str(exc), 422)
        # 指纹里折进 run_id 与操作名:同一个 key 对同一 agent 的不同 run / 不同操作
        # 必须是 IDEMPOTENCY_KEY_REUSED,与 ``request_digest`` 折进 agent_code 同理。
        digest = request_digest(payload, agent_code=f"{agent_code}\x00{run_id}\x00{op}")
        existing = await runs.find_by_idempotency_key(tenant_id=tenant_id, key=key)
        if existing is not None:
            if existing.request_digest != digest:
                return _envelope_error("IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was already used with a different request", 422)
            return await _idempotent_run_response(existing, mode=mode, event_store=getattr(state, "run_event_store", None), stream_bridge=runtime.stream_bridge, run_store=runs, tenant_id=tenant_id, stream_format=stream_format)

    try:
        target, meta = await load_owned_run(tenant_id=tenant_id, agent_code=agent_code, user_id=user_id, run_id=run_id, runs=runs, threads=threads, users=users)
    except ExternalScopeError as exc:
        return external_error(exc)
    end_user_id = meta.user_id
    assert end_user_id is not None  # load_owned_session 已保证 meta.user_id == 该 user
    if await state.agent_disable_service.is_disabled(tenant_id, agent_code):
        return _envelope_error("AGENT_DISABLED", f"agent {agent_code!r} is disabled", 403)
    active = await state.agent_spec_repo.list_by_tenant(tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, name=agent_code, limit=1)
    if not active:
        return _envelope_error("AGENT_NOT_FOUND", f"agent {agent_code!r} not found", 404)
    record = active[0]

    denial = await check_admission(quota=state.quota_service, audit=state.audit_logger, tenant_id=tenant_id, actor_id=actor_id, agent=agent_code, resource_kind="run")
    if denial is not None:
        return denial
    try:
        built = await runtime.get_agent(tenant_id=tenant_id, name=agent_code, version=record.version, spec=record.spec, user_id=str(end_user_id))
    except AgentFactoryError as exc:
        return _envelope_error("AGENT_BUILD_FAILED", f"agent cannot be built: {exc}", 422)

    if isinstance(payload, ExternalEditRequest):
        try:
            image_refs, document_names = await resolve_external_files(files=payload.files, tenant_id=tenant_id, end_user_id=end_user_id, thread_id=meta.thread_id, uploads_store=state.user_upload_store)
        except ExternalScopeError as exc:
            return external_error(exc)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return _envelope_error(detail.get("code", "INVALID_FILE_REF"), detail.get("message", "invalid file reference"), exc.status_code)
        bounds = external_run_bounds_error(untrusted_content=payload.untrusted_content, inputs=payload.inputs)
        if bounds is not None:
            return bounds
        run_payload = RunRequest(input=payload.input, mode=mode, image_refs=image_refs, untrusted_content=payload.untrusted_content, inputs=payload.inputs, document_names=document_names)
    else:
        run_payload = RunRequest(input=None, mode=mode)

    try:
        return await spawn_run(
            runtime=runtime, audit=state.audit_logger, approvals=state.approval_store, request=request, settings=state.settings,
            built=built, record_spec=record.spec, thread_id=meta.thread_id, tenant_id=tenant_id, actor_id=actor_id,
            effective_user_id=end_user_id, oauth_subject=str(end_user_id), payload=run_payload, trace_id=trace_id,
            extra_headers={"X-Expert-Work-Session-Id": str(meta.thread_id)}, on_behalf_of=str(end_user_id),
            idempotency_key=key, request_digest=digest, envelope=True, hide_events=EXTERNAL_HIDDEN_EVENTS,
            stream_format=stream_format, on_disconnect=DisconnectMode.CONTINUE,
            supersede=SupersedeRequest(target_run_id=target.run_id, replay=(op == "regenerate")),
        )
    except SupersedeError as exc:
        return _envelope_error(exc.code, exc.message, exc.status_code)
    except RunIdempotencyConflict:
        if key is None:  # pragma: no cover
            raise
        winner = await runs.find_by_idempotency_key(tenant_id=tenant_id, key=key)
        if winner is None:  # pragma: no cover
            raise
        if winner.request_digest != digest:
            return _envelope_error("IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was already used with a different request", 422)
        return await _idempotent_run_response(winner, mode=mode, event_store=getattr(state, "run_event_store", None), stream_bridge=runtime.stream_bridge, run_store=runs, tenant_id=tenant_id, stream_format=stream_format)
```

`app.state` 属性名(已按 `rg -o "request\.app\.state\.[a-z_]+" api/agents.py | sort -u` 与 `app.py:2398-2501` 核过):`agent_runtime` / `agent_spec_repo` / `agent_disable_service` / `quota_service` / `approval_store` / `user_upload_store` / `settings` / `audit_logger` / `thread_meta_repo` / `run_store` / `thread_message_store`(`app.py:2421`)/ `session_factory`(`app.py:2406`);`run_event_store` 用 `getattr(state, "run_event_store", None)`(`agents.py:481-484` 同款)。import:`from expert_work.common.observability import current_trace_id_hex`、`from orchestrator import AgentFactoryError`、`from fastapi.responses import JSONResponse, StreamingResponse`。

两条路由(`:cancel` 之后):

```python
    @router.post("/{agent_code}/runs/{run_id}:regenerate", response_model=None)
    async def regenerate_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalRegenerateRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> StreamingResponse | JSONResponse:
        """P-1 —— 同一输入再跑一次(旧轮标「已被取代」,agent 看不见它)。"""
        return await _supersede_and_run(op="regenerate", agent_code=agent_code, run_id=run_id, user_id=payload.user_id,
                                        mode=payload.mode, stream_format=payload.stream_format, payload=payload, request=request,
                                        runs=runs, threads=threads, users=users, idempotency_key=idempotency_key)

    @router.post("/{agent_code}/runs/{run_id}:edit", response_model=None)
    async def edit_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalEditRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
    ) -> StreamingResponse | JSONResponse:
        """P-1 —— 改输入后再跑一次。"""
        return await _supersede_and_run(op="edit", agent_code=agent_code, run_id=run_id, user_id=payload.user_id,
                                        mode=payload.mode, stream_format=payload.stream_format, payload=payload, request=request,
                                        runs=runs, threads=threads, users=users, idempotency_key=idempotency_key)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_runs_regenerate.py services/control-plane/tests/test_external_runs_cancel.py -q`
Expected: 全绿。若 `test_regenerate_marks_old_turn…` 的 `/messages` 断言拿到 `superseded_by=None`:stub 图的 `aget_state_history(filter=)` 在 `InMemorySaver` 上要求 metadata 含 `run_id` —— `spawn_run` 的 config 已带 `run_id`,若仍空,改用 `MemorySaver` 之外的路径不可取,先在测试里打印 `[s.metadata for s in history]` 定位;这是 Task 4 内核在内存 saver 上的唯一差异点。

- [ ] **Step 6: 变异自证**

`_supersede_and_run` 里 `replay=(op == "regenerate")` 改成 `replay=False` → `test_regenerate_marks_old_turn…` 的 `set(new.enqueued_input) == {"replay_messages"}` 红;改回。`load_owned_run` 的 `except ExternalScopeError` 改成返回 `_envelope_error("SESSION_NOT_FOUND", …)` → `test_other_user…` 红;改回。

- [ ] **Step 7: lint / 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane
git commit -m "feat(external): P-1 POST …/runs/{run_id}:regenerate 与 :edit(信封错误码 / 附件 / 上限 / on_disconnect=CONTINUE)"
```

---

### Task 9: Idempotency-Key 测试 + 三张路由表 + NUL 逐端点测试 + `errors.md` 五个错误码(随 PR3 一起上线)

**Files:**
- Modify: `services/control-plane/tests/test_external_only_gate.py:67-95`(`_EXTERNAL_ROUTES` 加两条)
- Modify: `services/control-plane/tests/test_console_lockdown.py:251-286`(`_EXTERNAL_AGENT_ROUTES` 加两条)
- Modify: `services/control-plane/tests/test_external_path_param_nul_guard.py`(`:259` `test_cancel_run_nul_agent_code_is_422` 之后加两条;`:348` `test_cancel_run_nul_run_id_is_422` 之后加一条)
- Modify: `services/control-plane/tests/test_external_runs_regenerate.py`(加幂等测试)
- Modify: `apps/admin-ui/docs-site/guide/errors.md`(§8.1 速查表五行 + 端点路径两行、§8.7 一段、§8.10 两条)—— PR3 合并后 docs-site 随 admin-ui 发测试环境,对接方看到的必须是完整的错误码行,不能等 PR4

**Interfaces:**
- Consumes: Task 8 两条路由
- Produces: 三张表登记的精确行(下);`errors.md` 五个错误码的完整行(链接先指本页 §8.7 / §8.10 —— chat §2.9 要到 Task 10 才存在,指过去会让 `check_links.py` 红;Task 10 再把链接改指 chat §2.9)

- [ ] **Step 1: 先跑完备性断言,看它红**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_only_gate.py::test_route_table_covers_every_live_external_agents_route services/control-plane/tests/test_console_lockdown.py::test_agents_prefix_is_partitioned_exactly -q`
Expected: 两条 FAIL,消息里列出 `('POST', '/v1/agents/{agent_code}/runs/{run_id}:regenerate')` 与 `…:edit`。

- [ ] **Step 2: 登记三张表**

`test_external_only_gate.py` `_EXTERNAL_ROUTES`,紧接 `("POST", "/v1/agents/{agent_code}/runs/{run_id}:cancel"),`(`:77`)之后加:

```python
        # P-1 —— 重新生成 / 编辑重发(external_runs.py),与 :cancel 同族。
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:regenerate"),
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}:edit"),
```

`test_console_lockdown.py` `_EXTERNAL_AGENT_ROUTES`,紧接 `:255` 的 `:cancel` 行之后加同样两行(注释同)。

`test_external_path_param_nul_guard.py`,`test_cancel_run_nul_agent_code_is_422`(`:258-266`)之后加:

```python
@pytest.mark.asyncio
async def test_regenerate_run_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/runs/{uuid4()}:regenerate",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)


@pytest.mark.asyncio
async def test_edit_run_nul_agent_code_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        f"/v1/agents/support{_NUL}bot/runs/{uuid4()}:edit",
        json={"user_id": "cust-77", "input": "x"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)
```

`test_cancel_run_nul_run_id_is_422`(`:347-354`)之后加:

```python
@pytest.mark.asyncio
async def test_edit_run_nul_in_input_is_422(ctx: _Ctx) -> None:
    """body 字段走 field_validator(reject_nul),与 ``POST …/runs`` 的 ``input`` 同一条路。"""
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{uuid4()}:edit",
        json={"user_id": "cust-77", "input": f"a{_NUL}b"},
        headers=ctx.headers,
    )
    _assert_envelope_422(resp)
```

- [ ] **Step 3: 跑三个文件全绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_only_gate.py services/control-plane/tests/test_console_lockdown.py services/control-plane/tests/test_external_path_param_nul_guard.py -q`
Expected: 全绿(员工 JWT 对两条新路由 403、admin key 可达、无凭据 401、NUL 422 全部由参数化自动覆盖)。变异:从 `_EXTERNAL_ROUTES` 删掉 `:edit` 那行 → `test_route_table_covers_every_live_external_agents_route` 红;改回。

- [ ] **Step 4: 幂等测试(追加到 `test_external_runs_regenerate.py`)**

```python
@pytest.mark.asyncio
async def test_idempotency_key_replays_instead_of_creating_a_second_run(ctx: Any) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _first_turn(ctx)
    headers = {**ctx.headers, "Idempotency-Key": "regen-1"}
    body = {"user_id": "cust-77", "mode": "queue"}
    first = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:regenerate", json=body, headers=headers)
    second = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:regenerate", json=body, headers=headers)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    rows = await ctx.run_store.list_by_thread(thread_id=thread_id, tenant_id=ctx.tenant_id)
    assert len(rows) == 2                                   # 首轮 + 一条重发,没有第二条
    # 同 key 换操作 / 换 run → 422 IDEMPOTENCY_KEY_REUSED
    reused = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:edit", json={**body, "input": "x"}, headers=headers)
    assert reused.status_code == 422 and reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_both_runs_keep_their_token_usage_rows(ctx: Any) -> None:
    """spec §7 PR3:两轮都计费,明确不回滚 —— 旧轮的 token_usage 行不因被取代而消失。"""
    await ctx.seed_agent()
    thread_id, r1 = await _first_turn(ctx)
    old = await ctx.run_store.get(run_id=r1, tenant_id=ctx.tenant_id)
    resp = await ctx.client.post(f"/v1/agents/support-bot/runs/{r1}:regenerate", json={"user_id": "cust-77", "mode": "queue"}, headers=ctx.headers)
    assert resp.status_code == 202
    still = await ctx.run_store.get(run_id=r1, tenant_id=ctx.tenant_id)
    assert still.trace_id == old.trace_id and still.status == old.status      # 行未被改写,计费归属不变
```

(`token_usage` 表在内存栈不存在;真栈验收 Task 12 用 `GET /v1/runs/{id}` 的 `tokens` 核两轮都在。)

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_runs_regenerate.py -q`。Expected:全绿。变异:`_supersede_and_run` 里 digest 的 salt 去掉 `\x00{op}` → `reused.status_code == 422` 红(变成幂等命中 202);改回。

- [ ] **Step 5: `errors.md` 五个错误码(完整行,随 PR3 发布)**

§8.1 速查表在 `RUN_NOT_FOUND` 行(`:32`)之后加五行(链接指本页小节;Task 10 再改指 chat §2.9):

```markdown
| [`RUN_NOT_LAST`](#_8-10-422-请求参数不合法) | 422 | 重新生成 / 编辑重发 | 目标不是这段会话的最后一轮。只能对最后一轮操作 |
| [`RUN_INPUT_UNAVAILABLE`](#_8-10-422-请求参数不合法) | 422 | 重新生成 | 目标轮在开始执行前就失败了，没有可复用的输入。改用编辑重发并提供 `input` |
| [`THREAD_BUSY`](#_8-7-409-冲突) | 409 | 重新生成 / 编辑重发 | 这段会话有一轮正在执行或排队。等它结束或先取消它 |
| [`RUN_AWAITING_APPROVAL`](#_8-7-409-冲突) | 409 | 重新生成 / 编辑重发 | 目标轮正等待审批决策。先决策，再对续跑后的那一轮操作 |
| [`RUN_ALREADY_SUPERSEDED`](#_8-7-409-冲突) | 409 | 重新生成 / 编辑重发 | 目标轮已经被重新生成过。对最新的那一轮操作 |
```

`RUN_NOT_FOUND` 行(`:32`)的「端点」列改为「取消 run / 审批决策 / 事件接口 / 重新生成 / 编辑重发」。§8.1 端点路径清单(`:10-14`)加两行:

```markdown
- 重新生成：`POST /v1/agents/{agent_code}/runs/{run_id}:regenerate`，同一输入再跑一次
- 编辑重发：`POST /v1/agents/{agent_code}/runs/{run_id}:edit`，改输入后再跑一次
```

§8.7 首句「两个端点会返回 409，都是标准格式。」改「四个端点会返回 409，都是标准格式。」,并在「产物下载」段之后加:

```markdown
**重新生成与编辑重发**（`POST /v1/agents/{agent_code}/runs/{run_id}:regenerate` 与 `…:edit`）：`THREAD_BUSY` 表示这段会话有一轮正在执行或排队，等它结束或先取消它再试；`RUN_AWAITING_APPROVAL` 表示目标轮正等待审批决策，先决策（[4.2](./run-control#_4-2-审批决策)），再对续跑后的那一轮操作；`RUN_ALREADY_SUPERSEDED` 表示目标轮已经被重新生成过，对最新的那一轮操作。三个错误都不要原样重试。两个端点只对一段会话的最后一轮有效，旧轮不删除、标成「已被取代」，副作用不撤销、两轮都计费。
```

§8.10「另外三种独立的 422」(`:267`)改「另外五种独立的 422」,表里追加:

```markdown
| `RUN_NOT_LAST` | 重新生成 / 编辑重发的目标不是这段会话的最后一轮 |
| `RUN_INPUT_UNAVAILABLE` | 重新生成的目标轮在开始执行前就失败了，没有可复用的输入；改用编辑重发并提供 `input` |
```

自检:`rg -n "图|节点|落库|游标|回放|帧|注入|铸造|mint|闸|门禁|终态|载荷|通路|编排|graph|LangGraph|checkpoint|supervisor" apps/admin-ui/docs-site/guide/errors.md` 零新增命中;`cd apps/admin-ui/docs-site && pnpm build && python3 scripts/check_links.py` 过(锚点 `#_8-7-409-冲突` / `#_8-10-422-请求参数不合法` 是本页既有标题的 VitePress slug,与 `:37` 行 `ARTIFACT_VERSION_MISMATCH` 用的同一个)。

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/tests apps/admin-ui/docs-site/guide/errors.md
git commit -m "test(external): P-1 三张路由表登记 + NUL 逐端点 + Idempotency-Key 重发不产生第二条 run;docs: errors.md 五个错误码"
```

**PR3 到此**(Task 8-9)。

---

### Task 10: 对外文档(chat / run-control / sse-events / query / errors / examples / best-practices)+ 侧栏

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/chat.md`(§2.8 之后新增 §2.9)
- Modify: `apps/admin-ui/docs-site/guide/run-control.md:1-6`(章首加一句指引)
- Modify: `apps/admin-ui/docs-site/guide/sse-events.md`(§3.4 `metadata` 小节末,`:186-227` 内)
- Modify: `apps/admin-ui/docs-site/guide/query.md`(§5.3 响应字段表 `:208-219`、§5.4 响应 `:284-297`、§5.8 条目公共字段 `:799-811` + 轮的信息 `:969-983`)
- Modify: `apps/admin-ui/docs-site/guide/errors.md`(只改 Task 9 写的五行 + §8.7 段落里的链接目标 → chat §2.9;错误码正文 PR3 已上线)
- Modify: `apps/admin-ui/docs-site/guide/examples.md`(§10.8 之后新增 §10.9)
- Modify: `apps/admin-ui/docs-site/guide/best-practices.md`(§9.5 常见问题加一问)
- Modify: `apps/admin-ui/docs-site/.vitepress/config.mts:40-52`(chat 子项加 2.9)

**Interfaces:**
- Consumes: Task 8/9 的端点、body、错误码;Task 5 的读面字段
- Produces: 六页(+run-control 一句)文档;`errors.md` 五个码行的链接改指 chat §2.9(码行本身 Task 9 已上线)

- [ ] **Step 1: `chat.md` 新节(放在 §2.8「重复请求的响应」之后、文件末尾)**

```markdown
## 2.9 重新生成与编辑重发

终端用户对 Agent 的最新一轮回答不满意时，你的界面可以提供两个动作：**重新生成**（同一输入再跑一次）和**编辑重发**（改了输入再跑一次）。两个动作都只对一段会话的**最后一轮**有效；旧的那一轮不会删除，而是标成「已被取代」——历史消息接口仍能读到它，但 Agent 在后续对话里不再看见它，它期间写下的计划也会退回到这一轮开始前的状态。

### 请求

重新生成：`POST /v1/agents/{agent_code}/runs/{run_id}:regenerate`

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 终端用户标识，必须与这个 run 所属会话的用户一致 |
| `mode` | 否 | `stream`（默认）或 `queue`，含义同 2.4 |
| `stream_format` | 否 | `legacy`（默认）或 `items`，含义同 3.7 |

重新生成不接受 `input` 与 `files`：输入就是被取代那一轮的原文与附件，服务端原样复用。

编辑重发：`POST /v1/agents/{agent_code}/runs/{run_id}:edit`

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | 同上 |
| `input` | 是 | 新的用户输入，1 到 32000 个字符 |
| `mode` | 否 | 同上 |
| `stream_format` | 否 | 同上 |
| `untrusted_content` | 否 | 同 2.7，最多 16 段 |
| `inputs` | 否 | 同 2.7 的模板变量 |
| `files` | 否 | 同 2.6，最多 64 个 `upload_id`。省略时这一轮**不带任何附件**——编辑重发不会沿用旧轮的附件 |

`input` 的长度上限以 [2.3 请求参数详解](#_2-3-请求参数详解) 为准。

### 响应

与 [2.2 发起对话](#_2-2-发起对话) 完全一致：`stream` 模式直接返回事件流，`queue` 模式返回 202 与新的 `run_id`。事件流里没有新增的事件类型；新一轮的 `run_id` 与被取代那一轮不同，`session_id` 相同。

### 三条规则

1. **只对最后一轮。** 对更早的轮调用会返回 422 `RUN_NOT_LAST`。要改更早的内容，只能从那一轮之后逐轮重做。
2. **旧轮的副作用不撤销。** 被取代那一轮如果写过工作区文件、登记过产物、记下过长期记忆，这些都保留；新一轮可能再次产生同类内容。
3. **两轮都计费。** 被取代的一轮已经消耗的模型调用照常计入用量，不退还。

另外两条限制：同一轮旧版本最多保留 5 个，更早的版本正文会被清理（历史消息接口返回 `tombstone: true` 且 `content` 为空）；同一会话正在执行或等待审批时会被拒绝，见下面的错误表。

### 历史消息里怎么识别

| 字段 | 出现位置 | 说明 |
|---|---|---|
| `superseded_by` | 历史消息每条、对话条目每条、`runs[]` 每轮 | 取代它的新 `run_id`；未被取代时为 `null` |
| `regenerated_from` | `runs[]` 每轮 | 这一轮是对哪个旧 `run_id` 的重新生成或编辑重发；普通轮为 `null` |
| `tombstone` | 历史消息每条、对话条目每条 | `true` 表示正文已清理，此时 `content` 为空字符串 |

字段总表见 [5.3 历史消息](./query#_5-3-历史消息) 与 [5.8 对话条目](./query#_5-8-对话条目)。

### 错误

| 错误码 | HTTP 状态 | 含义与处理 |
|---|---|---|
| `RUN_NOT_FOUND` | 404 | `run_id` 不存在，或不属于这个 `user_id` 与 `agent_code` |
| `RUN_NOT_LAST` | 422 | 目标不是这段会话的最后一轮 |
| `RUN_INPUT_UNAVAILABLE` | 422 | 只在重新生成时出现：目标轮在开始执行前就失败了，没有可复用的输入。改用编辑重发并提供 `input` |
| `THREAD_BUSY` | 409 | 这段会话有一轮正在执行或排队。等它结束（或先取消它）再试 |
| `RUN_AWAITING_APPROVAL` | 409 | 目标轮正等待审批决策。先决策（4.2），再对续跑后的那一轮操作 |
| `RUN_ALREADY_SUPERSEDED` | 409 | 目标轮已经被重新生成过。对最新的那一轮操作 |

### 防重复下发

两个端点都支持 `Idempotency-Key`，规则同 [2.8](#_2-8-防重复下发-idempotency-key)：同一个键重发不会产生第二轮；同一个键换了请求体、换了 `run_id` 或换了端点，返回 422 `IDEMPOTENCY_KEY_REUSED`。
```

`input` 上限数值(32000)以 `services/control-plane/src/control_plane/api/runs.py` 的 `MAX_RUN_INPUT_CHARS` 为准(`rg -n "^MAX_RUN_INPUT_CHARS" services/control-plane/src/control_plane/api/runs.py`),与 §2.3 现有写法一致后再定稿。

- [ ] **Step 2: 其余六处**

`run-control.md` 第 3 行(标题后的导语)末尾追加一句:`重新生成与编辑重发（同一输入再跑一次、改输入再跑一次）不在本章，见 [2.9](./chat#_2-9-重新生成与编辑重发)。`

`sse-events.md` §3.4 `metadata` 小节末(`:227` 之前)加一段:

```markdown
重新生成或编辑重发产生的一轮，事件流与普通的一轮完全一样，没有新增事件；`metadata` 里的 `run_id` 是新一轮的 id。旧的那一轮只在历史消息接口里以 `superseded_by` 字段标出，见 [2.9](./chat#_2-9-重新生成与编辑重发)。
```

`query.md` §5.3 响应字段表加两行:

```markdown
| `superseded_by` | string \| null | 取代这条消息的新 `run_id`；未被取代为 `null`。见 [2.9](./chat#_2-9-重新生成与编辑重发) |
| `tombstone` | boolean | `true` 表示正文已清理，此时 `content` 为空字符串 |
```

§5.4 run 列表响应字段表加两行 `superseded_by`(string \| null,同上)与 `regenerated_from`(string \| null,「这一轮是对哪个旧 `run_id` 的重新生成或编辑重发；普通轮为 `null`」);§5.8「条目的公共字段」加 `superseded_by` / `tombstone` 两行,「轮的信息」加 `superseded_by` / `regenerated_from` 两行(文字同上)。

`errors.md`(码行与段落 Task 9 已随 PR3 上线,这里只改链接目标):§8.1 速查表里 Task 9 加的五行,链接从 `(#_8-10-422-请求参数不合法)` / `(#_8-7-409-冲突)` 改为 `(./chat#_2-9-重新生成与编辑重发)`;§8.1 导语「取消 run 与审批决策两个端点的错误码详解在…」后补一句「重新生成与编辑重发的错误码详解在 [2.9](./chat#_2-9-重新生成与编辑重发)」;§8.7「重新生成与编辑重发」段末追加「完整规则见 [2.9](./chat#_2-9-重新生成与编辑重发)」。

`examples.md` §10.8 之后加 §10.9(结构照 §10.6 取消 run:curl + Python + Node.js 各一段,示例值与全站一致):

````markdown
## 10.9 重新生成与编辑重发

对最新一轮调用；旧轮会标成「已被取代」，新一轮的响应与发起对话完全一样。规则与错误见 [2.9](./chat#_2-9-重新生成与编辑重发)。

```bash [curl]
# 重新生成：同一输入再跑一次（queue 模式，返回新的 run_id）
curl -sS -X POST "$BASE_URL/v1/agents/support-bot/runs/$RUN_ID:regenerate" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "cust-77", "mode": "queue"}'

# 编辑重发：改输入再跑一次
curl -sS -X POST "$BASE_URL/v1/agents/support-bot/runs/$RUN_ID:edit" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "cust-77", "input": "换成 3 天的行程", "mode": "queue"}'
```

```python [Python]
import requests

resp = requests.post(
    f"{BASE_URL}/v1/agents/support-bot/runs/{run_id}:edit",
    headers={"Authorization": f"Bearer {API_KEY}", "Idempotency-Key": f"edit-{run_id}"},
    json={"user_id": "cust-77", "input": "换成 3 天的行程", "mode": "queue"},
    timeout=30,
)
body = resp.json()
if not body["success"]:
    raise RuntimeError(body["error"]["code"])   # RUN_NOT_LAST / THREAD_BUSY / …
new_run_id = body["data"]["run_id"]
```

```js [Node.js]
const resp = await fetch(`${BASE_URL}/v1/agents/support-bot/runs/${runId}:regenerate`, {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}`, "Content-Type": "application/json" },
  body: JSON.stringify({ user_id: "cust-77", mode: "queue" }),
});
const body = await resp.json();
if (!body.success) throw new Error(body.error.code);
const newRunId = body.data.run_id;
```
````

`best-practices.md` §9.5 加一问:

```markdown
### 重新生成之后历史消息里为什么还有旧回答

被取代的一轮不会删除，历史消息接口照常返回它，只是每条都带 `superseded_by`（指向新一轮的 `run_id`）。界面上把带这个字段的消息折叠或划掉即可；Agent 在后续对话里已经看不见它。旧轮的用量照常计费，见 [2.9](./chat#_2-9-重新生成与编辑重发)。
```

`config.mts:51` `2.8` 那行之后加:`{ text: "2.9 重新生成与编辑重发", link: "/guide/chat#_2-9-重新生成与编辑重发" },`。

- [ ] **Step 3: 按 style-guide §12 自检 + 构建 + 死链**

```bash
rg -n "图|节点|落库|游标|回放|帧|注入|铸造|mint|闸|门禁|终态|载荷|通路|编排|graph|LangGraph|checkpoint|supervisor|真栈|审计|Task" apps/admin-ui/docs-site/guide/chat.md apps/admin-ui/docs-site/guide/errors.md apps/admin-ui/docs-site/guide/query.md apps/admin-ui/docs-site/guide/examples.md apps/admin-ui/docs-site/guide/best-practices.md apps/admin-ui/docs-site/guide/sse-events.md | rg -v "^.*:\d+:\s*#|事件流|图片"
cd apps/admin-ui/docs-site && pnpm install --frozen-lockfile && pnpm build && python3 scripts/check_links.py
```

Expected:第一条只剩「图片」类合法命中(文档里「带图片和文档」是既有标题);构建过、死链零。正文标点检查:`rg -n "[,:;()] " apps/admin-ui/docs-site/guide/chat.md | rg -v "^\d+:\s*(\||```|[a-zA-Z\-]+ )"` 命中的半角标点逐条改全角(代码块与表格内保持 ASCII)。

- [ ] **Step 4: 提交**

```bash
git add apps/admin-ui/docs-site
git commit -m "docs(external): P-1 重新生成与编辑重发 —— chat §2.9 + 六页字段/错误码/示例/FAQ + 侧栏"
```

---

### Task 11: 控制台对话详情页折叠态

改页面前核 e2e:`rg -n "console-turn|conversation-message-|conversation-messages" apps/admin-ui/e2e/` → 只有 `playground-upload.spec.ts:115-118` 用 `console-turn`(playground 页,不受本任务影响);本任务不改任何既有 testid。

**Files:**
- Modify: `apps/admin-ui/src/api/sessions.ts:122-134`(`HistoryMessage` 加 `superseded_by?` / `tombstone?`)
- Modify: `apps/admin-ui/src/api/runs.ts:282-296`(`ThreadRunSummary` 加 `supersededBy` / `regeneratedFrom`)与 `:318-326`(mapper)
- Modify: `apps/admin-ui/src/api/conversations.ts:57-75`(`ConversationRun` 加 `superseded_by` / `regenerated_from`)
- Modify: `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts:45-58`(`HistoryTurn` 加 `supersededBy` / `tombstone`)、`:99-126`(赋值)与 ORDER 路径(`null` / `false`)
- Modify: `apps/admin-ui/src/components/console/types.ts:25-45`(`ConsoleTurn` 加同两字段)、`console_turns.ts:60-`(history 赋值;live 恒 `null` / `false`)
- Create: `apps/admin-ui/src/components/turn/SupersededFold.tsx`
- Modify: `apps/admin-ui/src/components/console/Transcript.tsx:212-244`(history 循环包一层)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts:396-`(接口)与 `:3547-`(值)、`zh-CN.ts:406-`
- Test: `apps/admin-ui/src/components/console/__tests__/Transcript.superseded.test.tsx`(新)、`apps/admin-ui/src/pages/agent_detail/playground/__tests__/history_turns.superseded.test.ts`(新;若该目录没有 `__tests__`,放到 `history_turns.test.ts` 旁的现有测试目录)

**Interfaces:**
- Consumes: Task 5 后端字段
- Produces: `data-testid="superseded-fold"`(`<details>`)、`data-testid="superseded-fold-link"`、`data-testid="tombstone-label"`;i18n 键 `conversations_detail.superseded_tag` / `superseded_link` / `tombstone_label`

- [ ] **Step 1: 写失败测试**

```tsx
// apps/admin-ui/src/components/console/__tests__/Transcript.superseded.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Transcript } from "../Transcript";
import type { ConsoleTurn } from "../types";

function turn(over: Partial<ConsoleTurn>): ConsoleTurn {
  return {
    key: "r1", seq: 0, source: "history", runId: "r1", loadState: "done", fallbackLines: [],
    tokens: null, timing: null, createdAt: null, supersededBy: null, tombstone: false,
    turn: { id: "r1", input: "U1", attachments: [], events: [], status: "done", error: null, approval: null },
    ...over,
  } as ConsoleTurn;
}

const noop = vi.fn();
const props = {
  flatHistory: [], taskResults: [], threadId: "t", selectedKey: null, onSelectTurn: noop, onInspectTurn: noop,
  onInspectRow: noop, streamTurnKey: null, liveByStep: new Map(), registerHistoryRow: () => () => {},
  rateBook: null, isSystemAdmin: false, readOnly: true, allowDecide: false, isTenantSwitched: false,
  onDecide: noop, deciding: false, onExport: noop, exportingKey: null, onDownloadArtifact: async () => {},
  runHrefOf: (t: ConsoleTurn) => (t.runId ? `/runs/t/${t.runId}` : null),
};

describe("Transcript superseded fold", () => {
  it("folds a superseded history turn and links to the new run", () => {
    render(<Transcript {...props} turns={[turn({ key: "r1", runId: "r1", supersededBy: "r2" }), turn({ key: "r2", runId: "r2", seq: 1 })]} />);
    const fold = screen.getByTestId("superseded-fold");
    expect(fold).not.toHaveAttribute("open");
    expect(screen.getByTestId("superseded-fold-link")).toHaveAttribute("href", "/runs/t/r2");
    expect(screen.getAllByTestId("console-turn")).toHaveLength(2);
  });

  it("labels a tombstoned turn", () => {
    render(<Transcript {...props} turns={[turn({ supersededBy: "r2", tombstone: true })]} />);
    expect(screen.getByTestId("tombstone-label")).toBeInTheDocument();
  });

  it("renders a normal turn without a fold", () => {
    render(<Transcript {...props} turns={[turn({})]} />);
    expect(screen.queryByTestId("superseded-fold")).toBeNull();
  });
});
```

`Transcript` 的必填 props 以 `Transcript.tsx` 顶部的 props 接口为准,缺的补 `noop`;若既有 `Transcript.test.tsx` 有现成的 props 构造 helper,复用它。

```ts
// history_turns.superseded.test.ts
import { describe, expect, it } from "vitest";
import { buildHistoryTurns } from "../history_turns";

describe("buildHistoryTurns supersede fields", () => {
  it("copies supersededBy from the run and tombstone from its messages", () => {
    const turns = buildHistoryTurns(
      [
        { role: "user", content: "", run_id: "r1", superseded_by: "r2", tombstone: true },
        { role: "assistant", content: "", channel: "final", run_id: "r1", superseded_by: "r2", tombstone: true },
        { role: "user", content: "U", run_id: "r2" },
        { role: "assistant", content: "A", channel: "final", run_id: "r2" },
      ],
      [
        { runId: "r1", status: "success", isResume: false, createdAt: "2026-09-10T00:00:00Z", finishedAt: null, error: null, tokens: null, supersededBy: "r2", regeneratedFrom: null },
        { runId: "r2", status: "success", isResume: true, createdAt: "2026-09-10T00:01:00Z", finishedAt: null, error: null, tokens: null, supersededBy: null, regeneratedFrom: "r1" },
      ],
    );
    expect(turns?.map((t) => [t.runId, t.supersededBy, t.tombstone])).toEqual([["r1", "r2", true], ["r2", null, false]]);
  });
});
```

Run: `cd apps/admin-ui && pnpm vitest run src/components/console/__tests__/Transcript.superseded.test.tsx src/pages/agent_detail/playground` → Expected: 红(`supersededBy` 不在类型上、testid 不存在)。

- [ ] **Step 2: 类型与数据流**

`api/sessions.ts` `HistoryMessage` 加:

```ts
  /** P-1 —— 取代这条消息的新 run_id;未被取代 null;老后端缺省。 */
  superseded_by?: string | null;
  /** P-1 —— 正文已清理(content 为空串)。 */
  tombstone?: boolean;
```

`api/runs.ts` `ThreadRunSummary` 加 `supersededBy: string | null; regeneratedFrom: string | null;`,mapper 加 `supersededBy: r.superseded_by ?? null, regeneratedFrom: r.regenerated_from ?? null,`(`r` 的类型加同名可选字段)。`api/conversations.ts` `ConversationRun` 加 `superseded_by: string | null; regenerated_from: string | null;`。

`history_turns.ts` `HistoryTurn` 加:

```ts
  /** P-1 —— 这一轮被哪个新 run 取代(``ThreadRunSummary.supersededBy``);null = 未被取代。 */
  supersededBy: string | null;
  /** P-1 —— 这一轮的消息已置墓碑(正文清理)。 */
  tombstone: boolean;
```

`buildHistoryTurns` 的 run 路径加 `supersededBy: r.supersededBy ?? null, tombstone: own.some((m) => m.tombstone === true),`;ORDER 路径(`pairs` → turns)加 `supersededBy: null, tombstone: false`。`console/types.ts` `ConsoleTurn` 加同两字段(注释同);`console_turns.ts` history 分支 `supersededBy: h.supersededBy, tombstone: h.tombstone`,live 分支 `supersededBy: null, tombstone: false`。

- [ ] **Step 3: `SupersededFold` + `Transcript`**

```tsx
// apps/admin-ui/src/components/turn/SupersededFold.tsx
/**
 * P-1 —— 对话详情页里「已被取代」的一轮:默认折叠,标签带跳到新轮的链接;
 * 墓碑轮再加一句「内容已清理」。只是一层壳,里面照常渲染 TurnBlock。
 */
import { Tag } from "antd";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

export interface SupersededFoldProps {
  supersededBy: string;
  tombstone: boolean;
  newRunHref: string | null;
  children: ReactNode;
}

export function SupersededFold({ supersededBy, tombstone, newRunHref, children }: SupersededFoldProps) {
  const { t } = useTranslation();
  return (
    <details data-testid="superseded-fold" style={{ opacity: 0.75, marginBottom: 8 }}>
      <summary style={{ cursor: "pointer", listStyle: "none" }}>
        <Tag color="default">{t("conversations_detail.superseded_tag")}</Tag>
        {newRunHref ? (
          <Link data-testid="superseded-fold-link" to={newRunHref}>
            {t("conversations_detail.superseded_link", { runId: supersededBy.slice(0, 8) })}
          </Link>
        ) : (
          <span data-testid="superseded-fold-link">{supersededBy.slice(0, 8)}</span>
        )}
        {tombstone && (
          <Tag data-testid="tombstone-label" style={{ marginInlineStart: 8 }}>
            {t("conversations_detail.tombstone_label")}
          </Tag>
        )}
      </summary>
      <div style={{ textDecoration: tombstone ? "none" : "line-through" }}>{children}</div>
    </details>
  );
}
```

`Transcript.tsx` history 循环(`:212-244`)改成:

```tsx
      {historyPart.map((turn) => {
        const block = (
          <TurnBlock
            key={turn.key}
            {...同原来的全部 props}
          />
        );
        if (turn.supersededBy === null) return block;
        const successor = turns.find((t) => t.runId === turn.supersededBy) ?? null;
        return (
          <SupersededFold
            key={turn.key}
            supersededBy={turn.supersededBy}
            tombstone={turn.tombstone}
            newRunHref={successor && runHrefOf ? runHrefOf(successor) : null}
          >
            {block}
          </SupersededFold>
        );
      })}
```

(`{...同原来的全部 props}` 指把 `:213-243` 现有的 TurnBlock props 原样搬进去,一个不少;`turns` 是 `Transcript` 已有的 props 名。)`import { SupersededFold } from "../turn/SupersededFold";`。

- [ ] **Step 4: i18n(三处)**

`en.ts` `TranslationKeys.conversations_detail`(`:396-`)加 `superseded_tag: string; superseded_link: string; tombstone_label: string;`;`en.ts` 值(`:3547-`)加:

```ts
    superseded_tag: "Superseded",
    superseded_link: "→ regenerated as run {{runId}}",
    tombstone_label: "content cleared",
```

`zh-CN.ts`(`:406-`)加:

```ts
    superseded_tag: "已被取代",
    superseded_link: "→ 新轮 {{runId}}",
    tombstone_label: "内容已清理",
```

- [ ] **Step 5: 跑测试 / typecheck / 变异**

```bash
cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/components/console src/pages/agent_detail/playground src/pages/__tests__/ConversationDetail.test.tsx src/i18n
```

Expected: 全绿(i18n 测试会核两 locale 键集合一致)。变异:`Transcript.tsx` 里 `if (turn.supersededBy === null) return block;` 改成 `return block;` → 第一条测试 `getByTestId("superseded-fold")` 红;改回。`buildHistoryTurns` 的 `tombstone: own.some(...)` 改 `false` → history_turns 测试红;改回。

- [ ] **Step 6: 提交**

```bash
git add apps/admin-ui/src
git commit -m "feat(admin-ui): P-1 对话详情页被取代轮折叠态(SupersededFold)+ 墓碑标签 + run 字段透传"
```

---

### Task 12: runbook 一句 + 真栈验收(探针 user + 金丝雀 agent)

**Files:**
- Modify: `docs/runbooks/production-release.md:274-284`(§2 日常发布)
- Create(不入仓): `scratchpad/p1_live_acceptance.sh`

**Interfaces:**
- Consumes: 测试环境已部署 PR1-PR4;金丝雀 agent `release-canary` 已 seed;探针 user 的 write key 由用户经 stdin 提供

- [ ] **Step 1: runbook**

§2「日常发布」列表末尾加一条:

```markdown
- P-1(重新生成 / 编辑重发)上线那次:`alembic upgrade head` 会带上 `0153_agent_run_supersede`(三列,秒级);发布后用探针 user 对 `release-canary` 跑一次 `POST …/runs/{run_id}:regenerate`(queue 模式),再拉 `/messages` 看旧轮每条带 `superseded_by`、`GET /v1/runs/{id}` 两轮 `tokens` 都在 —— 脚本形态见 plan `docs/superpowers/plans/2026-09-09-regenerate-edit-resend.md` Task 12。
```

- [ ] **Step 2: 真栈验收脚本(key 只经 stdin heredoc,永不落盘、永不上 argv)**

```bash
# scratchpad/p1_live_acceptance.sh —— 用法:bash p1_live_acceptance.sh <<< "$(pbpaste)"(key 在剪贴板)或
#   bash p1_live_acceptance.sh <<'EOF'
#   <write key>
#   EOF
set -euo pipefail
read -r API_KEY
BASE_URL="${BASE_URL:-https://<测试环境域名>}"   # 从 kustomize overlay 的 ingress host 取,不写死
AGENT=release-canary
USER_ID='canary:release'
auth=(-H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json")

# 1) 首轮(stream,拿 run_id / session_id)
hdr=$(curl -sS -D - -o /dev/null "${auth[@]}" -X POST "$BASE_URL/v1/agents/$AGENT/runs" \
      -d "{\"user_id\":\"$USER_ID\",\"input\":\"请把你能看到的上一条用户输入原样复述一遍;如果没有就说 NONE\",\"mode\":\"queue\"}")
RUN1=$(printf '%s' "$hdr" | tr -d '\r' | awk 'tolower($1)=="x-expert-work-run-id:"{print $2}')
SESSION=$(printf '%s' "$hdr" | tr -d '\r' | awk 'tolower($1)=="x-expert-work-session-id:"{print $2}')
echo "run1=$RUN1 session=$SESSION"
until curl -sS "${auth[@]}" "$BASE_URL/v1/agents/$AGENT/runs?user_id=$USER_ID&session_id=$SESSION" | jq -e --arg r "$RUN1" '.data.runs[]|select(.run_id==$r and (.status=="success"))' >/dev/null; do sleep 2; done

# 2) 编辑重发:新输入是探针指令 —— 让 agent 复述它能看到的历史;旧轮文本不该出现
RUN2=$(curl -sS "${auth[@]}" -X POST "$BASE_URL/v1/agents/$AGENT/runs/$RUN1:edit" \
       -H "Idempotency-Key: p1-edit-$RUN1" \
       -d "{\"user_id\":\"$USER_ID\",\"input\":\"请逐字列出你在这段对话里能看到的所有用户消息,一行一条;看不到就只写 NONE\",\"mode\":\"queue\"}" | jq -r '.data.run_id')
echo "run2=$RUN2"
until curl -sS "${auth[@]}" "$BASE_URL/v1/agents/$AGENT/runs?user_id=$USER_ID&session_id=$SESSION" | jq -e --arg r "$RUN2" '.data.runs[]|select(.run_id==$r and (.status=="success"))' >/dev/null; do sleep 2; done

# 3) 读面:旧轮每条 superseded_by == RUN2;新轮回答里不含旧轮输入的文字
curl -sS "${auth[@]}" "$BASE_URL/v1/agents/$AGENT/sessions/$SESSION/messages?user_id=$USER_ID" | tee /dev/stderr \
  | jq -e --arg r1 "$RUN1" --arg r2 "$RUN2" '
      ([.data.messages[]|select(.run_id==$r1)|.superseded_by]|all(.==$r2)) and
      ([.data.messages[]|select(.run_id==$r2)|.superseded_by]|all(.==null)) and
      ([.data.messages[]|select(.run_id==$r2 and .role=="assistant")|.content]|join(" ")|test("原样复述")|not)'
# 4) 幂等:同 key 重发不产生第二条 run
RUN2B=$(curl -sS "${auth[@]}" -X POST "$BASE_URL/v1/agents/$AGENT/runs/$RUN1:edit" -H "Idempotency-Key: p1-edit-$RUN1" \
        -d "{\"user_id\":\"$USER_ID\",\"input\":\"请逐字列出你在这段对话里能看到的所有用户消息,一行一条;看不到就只写 NONE\",\"mode\":\"queue\"}" | jq -r '.data.run_id')
[ "$RUN2" = "$RUN2B" ] && echo "idempotent OK"
# 5) 拒绝语义:对旧轮再来一次 → 409 RUN_ALREADY_SUPERSEDED;对不存在的 run → 404 信封
curl -sS "${auth[@]}" -X POST "$BASE_URL/v1/agents/$AGENT/runs/$RUN1:regenerate" -d "{\"user_id\":\"$USER_ID\",\"mode\":\"queue\"}" | jq -e '.error.code=="RUN_ALREADY_SUPERSEDED"'
curl -sS "${auth[@]}" -X POST "$BASE_URL/v1/agents/$AGENT/runs/00000000-0000-4000-8000-000000000000:regenerate" -d "{\"user_id\":\"$USER_ID\",\"mode\":\"queue\"}" | jq -e '.error.code=="RUN_NOT_FOUND"'
echo "P-1 live acceptance PASS"
```

两轮都计费:控制台 `GET /v1/runs/{RUN1}` 与 `GET /v1/runs/{RUN2}`(员工 JWT,Playwright 登录态复用)的 `tokens` 都非空。探针 user `pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation` 用对接方租户的 key 时把 `USER_ID` 换成它、`AGENT` 仍是 `release-canary`;**永不对 `ai-health-plan` / `sop2-designer` 调用**。

- [ ] **Step 3: 提交 runbook**

```bash
git add docs/runbooks/production-release.md
git commit -m "docs(runbook): P-1 上线那次的迁移与真栈验收一句"
```

**PR4 到此**(Task 10-12)。

---

## PR 切分与并行波次表

| PR | Task | 触及文件集合 | 依赖 / 可并行 |
|---|---|---|---|
| **PR1 内核 + 读面** | 1, 2, 3, 4, 5 | `packages/expert-work-common/src/expert_work/common/{conversation_channel,supersede}.py`;`packages/expert-work-persistence/{migrations/versions/0153_agent_run_supersede.py, src/expert_work/persistence/models/{agent_run,thread_message}.py, src/expert_work/persistence/thread_message/{base,memory,sql}.py}`;`packages/expert-work-runtime/src/expert_work/runtime/runs/{schemas,store,manager}.py`;`services/control-plane/src/control_plane/{transcript,supersede}.py`;`services/control-plane/src/control_plane/api/{external_sessions,external_session_items,runs,conversations,external_runs}.py`;对应 tests | **等 P-2 PR1 合并**(迁移 0152 是 0153 的 down_revision;`external_session_items.py` / `external_sessions.py` / `external_runs.py` 三个文件 P-2 也改)→ rebase 后再开 |
| **PR2 执行侧** | 6, 7 | `services/orchestrator/src/orchestrator/graph_builder/builder.py`;`services/control-plane/src/control_plane/{api/runs.py, run_queue_worker.py}`;tests | 依赖 PR1(`supersede_run` / `RunManager` 签名);Task 6 与 Task 7 文件不相交,可两个 worktree 并行 |
| **PR3 对外端点** | 8, 9 | `services/control-plane/src/control_plane/api/{external_runs,agents}.py`;`services/control-plane/tests/{test_external_only_gate,test_console_lockdown,test_external_path_param_nul_guard,test_external_runs_regenerate}.py`;`apps/admin-ui/docs-site/guide/errors.md`(五个错误码行,随 PR3 发到测试环境) | 依赖 PR2(`spawn_run(supersede=)`);三张路由表与 P-2 PR1 同文件 —— P-2 先合,本 PR 在其后追加行 |
| **PR4 文档 + 控制台** | 10, 11, 12 | `apps/admin-ui/docs-site/guide/{chat,run-control,sse-events,query,errors,examples,best-practices}.md` + `.vitepress/config.mts`;`apps/admin-ui/src/{api/{sessions,runs,conversations}.ts, pages/agent_detail/playground/history_turns.ts, components/console/{types,console_turns,Transcript}.{ts,tsx}, components/turn/SupersededFold.tsx, i18n/locales/{en,zh-CN}.ts}`;`docs/runbooks/production-release.md` | Task 10(文档)与 Task 11(控制台)文件不相交,可并行;Task 10 只依赖 PR3 的 wire 契约(可与 PR3 同步写,合并顺序在 PR3 后);Task 12 要测试环境已部署 PR1-3 |
| Task 0 | 0 | `docs/superpowers/specs/2026-09-09-regenerate-edit-resend-design.md` | 第一天,单独一个小 PR,不阻塞 PR1 开工(PR1 的 Task 4 若 §8-2 耗时判据不过,加锚点列另议) |

**与 P-2 线(`docs/superpowers/specs/2026-09-09-external-feedback-eval-loop-design.md`)的文件交集**:

| 文件 | P-2 改什么 | 本线改什么 | 约定 |
|---|---|---|---|
| `packages/expert-work-persistence/migrations/versions/` | `0152_feedback_run_scope` | `0153_agent_run_supersede`(`down_revision=0152`) | **P-2 PR1 先合**;本线 PR1 rebase 后 `alembic heads` 必须只有一个 |
| `api/external_session_items.py` | `runs[]` 与条目加 `feedback`;可能重排 `_group_messages_by_run` 调用附近 | 删本地分组函数改 import;条目/`runs[]` 加 `superseded_by` / `tombstone` / `regenerated_from` | 本线 rebase 时保留 P-2 的 `feedback` 行,在它旁边加本线三键 |
| `api/external_sessions.py` | `/messages` 每条加 `feedback` | 每条加 `superseded_by` / `tombstone` | 同上 |
| `api/external_runs.py` | P-2 新建 `api/external_feedback.py`,不改本文件(spec §4.1 只说「同一路由族」) | 加两条路由 | 无冲突;若 P-2 实施时改了本文件,以 P-2 为基 rebase |
| `tests/test_external_only_gate.py` / `test_console_lockdown.py` / `test_external_path_param_nul_guard.py` | 各登记 `POST …/runs/{run_id}/feedback` | 各登记 `:regenerate` / `:edit` | 两线都在 `:cancel` 行之后追加,rebase 时两组行都保留 |
| `docs-site/guide/{chat,query,errors,examples,best-practices}.md` + `config.mts` | chat 新节「给一轮回答打分」、query `feedback` 字段、errors 404/422、examples 四语言、best-practices 一句 | chat §2.9、query 四张表、errors 五码、examples §10.9、best-practices 一问 | 章节号冲突点:P-2 若占 chat **§2.9**,本线顺延为 **§2.10**(侧栏、全站锚点同步改);errors §8.1 表两组行都加 |
| `apps/admin-ui/src/pages/ConversationDetail.tsx` | 每轮脚部显示 👍/👎(P-2 PR3) | 本线不改该文件(折叠在 `Transcript.tsx` / `SupersededFold.tsx`) | 无冲突 |
| `docs/runbooks/production-release.md` | 无 | §2 一句 | 无冲突 |

**波次**:
1. 波 0(第一天):Task 0 → spec 回填 PR。
2. 波 1:等 P-2 PR1 合 → PR1(Task 1-5 串行,同一 worktree;Task 4 的 7 条集成测是本线的存亡判据,过不了不往下走)。
3. 波 2:PR1 合 → Task 6、Task 7 两个 worktree 并行 → 合成 PR2。
4. 波 3:PR2 合 → PR3(Task 8-9);同时另一个 worktree 起 Task 10(文档)与 Task 11(控制台),各自 PR 或合成 PR4,合并顺序 PR3 → PR4。
5. 发测试环境 → Task 12 真栈验收 → 记录进 ROADMAP 销案(由用户/主线做,本计划不改 ROADMAP)。

---

## 自审记录(writing-plans §Self-Review)

**1. Spec 覆盖**(逐节对 Task):§1 目标三条 → Task 4/6(agent 看不见)、Task 5/11(读面可见)、Task 4 墓碑 5 份(审计可回溯);§3.1 六步 → Task 4(锁 ①、取法 A ②、一次 aupdate_state ③、三张表 ④、留 5 份 ⑤、PAUSED 409 ⑥);§3.2 → Task 1(分组下沉 + 整轮)+ Task 6(位置在 working_window 之前 + 墓碑剔除);§3.3 → Task 7(`supersede=`、`:regenerate` 原件重放含附件、`:edit` 新 input、`regenerated_from_run_id`);§4 两端点/body/Idempotency-Key/错误码/读面字段/SSE 无新帧/三张表/六页文档 → Task 8、9、5、10;§5 折叠态 + 墓碑「内容已清理」→ Task 11;调试台不加入口 → 未触及 playground(无任务,符合);§6 不回退项写进文档 → Task 10 chat §2.9「三条规则」;§7 四 PR 验收 → 各 PR 末尾清单;§8 三条核实 → Task 0。**无缺口**;spec 未覆盖而本计划补的两处已在「与 spec 的出入」表列出(审批链、`RUN_INPUT_UNAVAILABLE`)。

**2. 占位符扫描**:`rg -n "TBD|TODO|implement later|fill in|Similar to Task|add appropriate|handle edge cases|写测试" docs/superpowers/plans/2026-09-09-regenerate-edit-resend.md` → 0 命中(Task 11 Step 3 的 `{...同原来的全部 props}` 是对 `Transcript.tsx:213-243` 现有 props 的逐行搬运指令,已写明行号与「一个不少」;Task 8 Step 4 的 `app.state` 名单已核过全列出)。

**3. 类型/名字一致性**:`SUPERSEDED_BY/SUPERSEDED_AT/TOMBSTONE` 定义在 `conversation_channel`(Task 1),Task 3/4/5/7 全部从它 import;`group_messages_by_run/filter_superseded_turns/mark_superseded/tombstone_message/stamped_run_id` 在 `supersede`(Task 1),Task 4/5/6/7 同名;`RunStore.mark_superseded(*, run_id, tenant_id, superseded_by_run_id) -> bool` 与 `ThreadMessageStore.mark_superseded(*, thread_id, tenant_id, seq_from, seq_to, superseded_by) -> int` 在 Task 2 定义、Task 4 调用签名一致;`supersede_run(...)` 的 11 个关键字参数在 Task 4 定义、Task 7 调用、Task 7/8 测试 fake 三处一致;`SupersedeRequest(target_run_id, replay)` 在 Task 7 定义、Task 8 使用;`RunManager.create/enqueue(..., regenerated_from_run_id=)` Task 2 定义、Task 7 调用;前端 `supersededBy/tombstone/regeneratedFrom` 在 `ThreadRunSummary`→`HistoryTurn`→`ConsoleTurn` 三层同名(Task 11)。

**执行者注意**:本计划里所有行号都以 main `4ac73c71` 为准;PR1 在 P-2 PR1 之后 rebase 时,`external_session_items.py` / `external_sessions.py` / 三张路由表 / docs-site 五页的行号会漂,按文中给的函数名 / 锚文本重新定位,不按行号硬改。
