# P3 PR-1:SSE 契约修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉 SSE 流的四条契约缺陷(seq 错位 / live 忽略 `since_seq` / 回放无分页 / 取消态无信号),让第三方能可靠地断线重连并还原完整的 agent 交互过程。

**Architecture:** 帧序号收敛成**唯一一套** —— 落库 seq。bridge 不再自己发号,改由 `run_agent` 把已分配的落库 seq 一并传进 `publish`;token 帧无 seq、不带 `id:` 行。live 分支据此可以先补库再接实时流,按 seq 去重并检测缺口。回放分支改成游标分页,截断时不再假装流结束。`end` 帧带上终局状态。

**Tech Stack:** Python 3.12 / FastAPI / asyncio;前端 React + TypeScript(admin-ui)。

## Global Constraints

- **spec 出处**:`docs/superpowers/specs/2026-08-11-external-api-v1-design.md` §六 C/D/E/F/G。本计划是该节的实现。
- **这个模块是控制台面和对外面共用的**(`api/_run_event_stream.py`,P1 特意合并的)。任何改动同时影响调试台、对话详情页、run 详情页和第三方 API。**不允许**为了对外面新增一份副本。
- **seq 分配必须在任何 `await` 之前同步完成**。`sse.py:449-456` 的注释记录了这条铁律:并发 worker 会交错 await,两帧读到同一个 `event_seq` 会撞 `(run_id, seq)` 主键。重构后这条不变式必须仍然成立,并且有测试钉住。
- **C 和 D 属于静默丢数据类缺陷**(spec §十 原话:"最需要变异自证")。每条新断言必须 break→red→restore→green 自证。
- **验证必须在缺陷真会出现的条件下做**(见下方「验证条件矩阵」)。在不可能失败的条件下跑绿不构成证据。
- 对外契约变更要同步文档站,**不允许把说谎的文档留在 main 上**(Task 7)。
- 提交信息用中文,遵循 `<type>: <description>`。

## 验证条件矩阵(每个 Task 的测试必须落在"会红"那一列)

| 改的东西 | 会红的条件 | 恒绿的假验证(明确避开) |
|---|---|---|
| C seq 归一 | run **必须含 token 帧** | 无 token 的 run —— 两个计数器恰好相等,测了等于没测 |
| D live 补库接合 | run **仍在跑**时重连,且断点前后都有落库帧 | 已终态的 run(走 replay 分支,根本不经过改动代码) |
| E 分页 | 帧数 **> 页大小** | 短 run |
| F end status | run **被取消 / 走到 PAUSED** | 正常跑完的 run(status 恒 `success`) |

---

## File Structure

- `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/base.py` — `StreamEvent.id` 变为可空;`publish` 增加 `seq`;`publish_end` 增加 `status`。
- `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/memory.py` — 删掉自增计数器,改用调用方给的 seq;end 帧携带 status。
- `services/orchestrator/src/orchestrator/sse.py` — seq 分配与发布合并成一个 helper;token 帧走无 seq 路径;`publish_end` 带终局状态。
- `services/control-plane/src/control_plane/api/_run_event_stream.py` — 本 PR 的主战场:live 接合 + 回放分页 + end 帧 status。
- `services/control-plane/src/control_plane/api/runs.py` / `external_events.py` — 两个调用点跟随签名变化 + 新响应头。
- `apps/admin-ui/src/**` — 帧 id 解析复验 + 游标翻页。
- `apps/admin-ui/docs-site/guide/{sse-events,run-agent,quickstart}.md` — 最小事实校正(完整重写在 PR-2)。

---

### Task 1:帧序号收敛到落库 seq

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/base.py`
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/memory.py`
- Modify: `services/orchestrator/src/orchestrator/sse.py`
- Test: `packages/expert-work-runtime/tests/test_in_memory_stream_bridge.py`

**Interfaces:**
- Produces:
  - `StreamEvent.id: str | None`(token 帧为 `None`)
  - `StreamBridge.publish(run_id, event, data, *, seq: int | None = None)` —— `seq is None` ⇒ 帧无 id
  - `StreamBridge.publish_end(run_id, *, status: str)` 
- Consumes:无(本 Task 是地基)

**背景(实现者必读)**:现在有**两个**独立的计数器。`memory.py:_next_id` 每发布一帧就 +1,**包含 token 帧**;`sse.py:_enqueue_event` 里的 `event_seq` 只在落库帧上 +1,**跳过 token 帧**。于是 live 流里 `id: {ms}-{seq}` 的 seq 恒 ≥ 落库 seq。文档教客户端从帧 id 里取 seq 当 `since_seq` —— 回放时就**跳过了真实存在的帧**,而且没有任何报错。这是本 PR 头号缺陷。

- [ ] **Step 1:写失败测试 —— 同一 run 里 token 帧不占号**

在 `test_in_memory_stream_bridge.py` 加:

```python
@pytest.mark.asyncio
async def test_token_frames_carry_no_id_and_do_not_consume_seq() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    await bridge.publish(run_id, "metadata", {"a": 1}, seq=0)
    await bridge.publish(run_id, "token", {"text": "hi"})          # 无 seq
    await bridge.publish(run_id, "updates", {"b": 2}, seq=1)
    await bridge.publish_end(run_id, status="success")

    got = [e async for e in bridge.subscribe(run_id, heartbeat_interval=0.05)]
    frames = [e for e in got if e.event not in ("__heartbeat__", "__end__")]
    assert [f.event for f in frames] == ["metadata", "token", "updates"]
    assert frames[1].id is None
    assert [f.id.rsplit("-", 1)[1] for f in frames if f.id is not None] == ["0", "1"]
```

- [ ] **Step 2:跑测试确认失败**

Run: `cd packages/expert-work-runtime && DOCKER_HOST= uv run pytest tests/test_in_memory_stream_bridge.py -v`
Expected: FAIL —— `publish()` 不接受 `seq` 关键字。

- [ ] **Step 3:改 `base.py`**

`StreamEvent.id` 改成 `str | None`,docstring 说明 `None` 表示这帧不可回放(当前只有 `token`)。`publish` 加 `*, seq: int | None = None`,`publish_end` 加 `*, status: str`。两处 docstring 写清楚:**seq 由调用方分配,必须与落库 `run_event.seq` 同源**;bridge 不再自己发号。

`END_SENTINEL` 保持模块级单例用于"流结束"的身份判断,但**状态数据不能挂在单例上**(会跨 run 串味)。改法:`subscribe` 在流结束时 yield `StreamEvent(id=None, event=END_SENTINEL.event, data={"status": <本 run 的>})`,消费方改用 `entry.event == END_SENTINEL.event` 判断而不是 `is END_SENTINEL`。在 `base.py` 导出一个 `def is_end(entry: StreamEvent) -> bool` 供消费方使用,避免每个调用点各写各的。

- [ ] **Step 4:改 `memory.py`**

删掉 `_counters` / `_next_id`(以及 `_get_or_create_stream` 和 `cleanup` / `close` 里对它的维护)。`publish` 里 `id = None if seq is None else f"{int(time.time() * 1000)}-{seq}"`。`_RunStream` 加 `end_status: str | None = None`,`publish_end` 写入它。`_resolve_start_offset` 用 `entry.id == last_event_id` 匹配 —— `entry.id` 现在可能是 `None`,`None == str` 天然为 False,无需特判,但加一行注释说明这是有意的(token 帧不是合法的续接锚点)。

- [ ] **Step 5:改 `sse.py` —— 分配与发布合并**

把 `_enqueue_event` 拆成同步的号段分配 + 落库入队,并新增统一发布口:

```python
def _alloc_seq() -> int:
    """同步分配落库 seq —— 全程无 await。并发 worker 交错调用本函数
    不会撞号(sse.py 原 _enqueue_event 的铁律,提取后仍然成立)。"""
    nonlocal event_seq
    seq = event_seq
    event_seq += 1
    return seq

async def _publish_frame(event_name: str, data: Any) -> None:
    """一帧同时进 bridge(实时)和落库队列(回放),共用同一个 seq。"""
    seq = _alloc_seq()
    await bridge.publish(run_id, event_name, data, seq=seq)
    _enqueue_event(seq, event_name, data)
```

`_enqueue_event(seq, event_name, data)` 不再自己分配号。改写以下发布点全部走 `_publish_frame`:`_publish_compaction`、`_publish_worker`、`_publish_guard`、`metadata`(:484)、`updates` chunk(:548)、`retry`(:573)、`approval`(:649)、`error` 两处(:733/:763)。`_publish_token` 改成 `await bridge.publish(run_id, "token", frame)`(不传 seq),**不落库**,保持现状语义。

`finally` 里的 `await bridge.publish_end(run_id)` 改成 `await bridge.publish_end(run_id, status=_external_end_status(session_outcome))`。新增模块级纯函数:

```python
#: 内部 outcome 词表 → 对外 end 帧的 status。对外只暴露四值:
#: 内部把"用户取消"分成 INTERRUPTED 与 RunCancelledError 两条路径,
#: 对第三方没有区别;max_steps 对客户端而言就是失败。
#: ``paused`` 必须独立 —— 它是"等人审批,对话还会继续",不是错误,
#: 客户端要弹审批界面而不是报错。
_EXTERNAL_END_STATUS = {
    "success": "success",
    "paused": "paused",
    "interrupted": "interrupted",
    "cancelled": "interrupted",
    "max_steps": "error",
    "error": "error",
}


def _external_end_status(session_outcome: str) -> str:
    return _EXTERNAL_END_STATUS.get(session_outcome, "error")
```

- [ ] **Step 6:跑测试确认通过**

Run: `cd packages/expert-work-runtime && DOCKER_HOST= uv run pytest tests/test_in_memory_stream_bridge.py -v`
Expected: PASS

- [ ] **Step 7:钉住并发不撞号的不变式**

`services/orchestrator/tests/` 下已有 worker 并发相关测试(用 `rg -l "worker_event_sink|_publish_worker" services/orchestrator/tests` 定位)。补一条:两个并发协程各调 `_publish_frame` N 次,断言落库队列里的 seq 集合恰好是 `range(2N)`、无重复。这条是防止 Step 5 的提取把同步分配意外挪到 await 之后。

- [ ] **Step 8:变异自证**

把 `_alloc_seq` 的 `event_seq += 1` 挪到 `await bridge.publish(...)` 之后(即恢复"分配跨 await"),Step 7 的测试必须变红。`git diff` 先确认变异真的落到了文件里,再读结果。恢复。

- [ ] **Step 9:提交**

```bash
git add -A && git commit -m "fix(sse): 帧序号收敛到落库 seq,token 帧不再占号"
```

---

### Task 2:C 的端到端自证 —— 含 token 帧的真 run

**Files:**
- Test: `services/control-plane/tests/test_sse_seq_alignment.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `publish(seq=...)` 契约

**为什么单列一个 Task**:Task 1 的单测证明的是 bridge 层的行为。真正要证明的是**端到端**:同一个 run,live 流里某帧 id 的 seq,与它落库那行的 seq 相等。这条只有在 run 含 token 帧时才可能失败 —— 见「验证条件矩阵」。先读 `services/control-plane/tests/` 下已有的驱动真 graph 的测试(`rg -l "run_agent" services/control-plane/tests`,`test_token_step_alignment.py` 是同类先例)找到治具。

- [ ] **Step 1:写测试**

驱动一次带 token 输出的 run,同时收集 live SSE 帧和 `RunEventStore` 里的行,断言:

```python
live_ids = [f.id for f in live_frames if f.event != "token"]
live_seqs = [int(i.rsplit("-", 1)[1]) for i in live_ids]
assert live_seqs == [r.seq for r in rows]
assert all(f.id is None for f in live_frames if f.event == "token")
assert live_seqs == list(range(len(live_seqs)))   # 连续无缺口
```

- [ ] **Step 2:跑测试;在**修复前的**代码上必须失败**

先 `git stash` 掉 Task 1 的实现(只留测试),跑一遍确认红 —— 红的形态应当是 `live_seqs` 比 `rows` 的 seq 大(token 帧占了号)。恢复。这一步是这条测试的合格证:它证明测试真的咬得住这个 bug,而不是碰巧绿。

- [ ] **Step 3:跑测试确认通过**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_sse_seq_alignment.py -v`

- [ ] **Step 4:提交**

```bash
git add -A && git commit -m "test(sse): 端到端钉住 live 帧 id 与落库 seq 同源"
```

---

### Task 3:live 分支认 `since_seq` —— 补库 + 接合 + 缺口回填

**Files:**
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py`
- Test: `services/control-plane/tests/test_run_event_stream.py`

**Interfaces:**
- Consumes: Task 1(live 帧带落库 seq,才可能去重)
- Produces:`_stream_live` 的接合语义(下面写死)

**背景**:`_stream_live()` 现在连 `since_seq` 都没引用 —— 参数被静默丢弃。run 还在跑时重连,客户端拿到的是 bridge 缓冲区里当前留着的最多 256 帧,从头重推。

**接合算法(照抄实现,别自己发明)**:

```
last = since_seq if since_seq is not None else -1
1. 先从库补:循环 event_store.list(run_id, since_seq=last, limit=MAX_LIST_LIMIT)
   直到某页返回条数 < limit。每行 yield,并 last = row.seq。
   (live 分支不做分页截断 —— 截断是 replay 分支的语义,见 Task 4)
2. 挂实时流:async for entry in bridge.subscribe(run_id, ...)
   - entry 无 seq(token 帧) → 直接 yield(它本就是一次性预览,重复或缺失都无害)
   - entry.seq <= last        → 丢弃(补库阶段已经发过)
   - entry.seq == last + 1    → yield,last += 1
   - entry.seq >  last + 1    → 缺口。先 event_store.list(since_seq=last, limit=…)
     把 last+1 .. entry.seq-1 补齐并 yield(补到多少算多少),再 yield entry,
     last = entry.seq。补不齐时打 warning 日志,不抛异常
     —— 这一段帧在 run 结束后仍可由客户端重新回放拿到。
```

**为什么要缺口回填**:bridge 缓冲区只有 256 帧、drop-oldest,而落库走的是攒批后台 writer。补库读完到挂上实时流之间,如果有超过 256 帧飞过,中间那段既不在缓冲区里也可能还没落库。上面第 4 分支是唯一能把这种情况变成"可观测的 warning"而不是"静默丢帧"的写法。

`entry.seq` 不在 `StreamEvent` 上 —— 从 `entry.id` 解析(`int(entry.id.rsplit("-", 1)[1])`),`id is None` 即 token 帧。在 `_run_event_stream.py` 里写一个模块级 `def _seq_of(entry) -> int | None` 承担这件事,别在循环里内联。

- [ ] **Step 1:写失败测试(三条)**

1. `test_live_reconnect_backfills_from_store`:run 处于 RUNNING;库里已有 seq 0..4;带 `since_seq=1` 重连 → 必须先收到 seq 2,3,4。
2. `test_live_reconnect_dedupes_overlap`:补库给到 seq 4,随后 bridge 推 seq 3,4,5 → 客户端只收到 5,且 3/4 不重复。
3. `test_live_reconnect_fills_gap`:补库给到 seq 2,bridge 直接推 seq 5,而库里此时有 3,4 → 客户端按序收到 3,4,5。

三条都必须在 **RUNNING** 状态的 run 上跑(`is_terminal=False`),否则走的是 replay 分支,测了等于没测。

- [ ] **Step 2:跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_run_event_stream.py -v -k live`

- [ ] **Step 3:实现接合算法**

按上面的伪码改 `_stream_live`。`scope` 工厂在 live 分支的补库读上同样要用(控制台调用方传的是 `lambda: applied_scope(scope)`,单次可用 —— **每次读都要重新调工厂**,`build_event_producer` 的 docstring 已经记了这条坑)。

- [ ] **Step 4:跑测试确认通过**

- [ ] **Step 5:变异自证(逐条)**

- 把去重条件从 `<= last` 改成 `< last` → 测试 2 必须红。
- 删掉缺口回填分支 → 测试 3 必须红。
- 把补库循环改成只读一页 → 造一个 >`MAX_LIST_LIMIT` 帧的 RUNNING run 的测试必须红(如果现有三条测试都不咬这个变异,就补第四条)。

每次变异前 `git diff` 确认真的改到了文件。

- [ ] **Step 6:提交**

```bash
git add -A && git commit -m "fix(sse): live 分支认 since_seq——补库接合 + 缺口回填"
```

---

### Task 4:回放分页 —— 截断不再假装流结束

**Files:**
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py`
- Modify: `services/control-plane/src/control_plane/api/runs.py`
- Modify: `services/control-plane/src/control_plane/api/external_events.py`
- Test: `services/control-plane/tests/test_run_event_stream.py`

**Interfaces:**
- Produces:
  - `build_event_producer` 变成 **async**,返回 `EventStreamPlan(producer: AsyncIterator[bytes], next_seq: int | None)`
  - 响应头 `X-Expert-Work-Next-Seq: <int>`(仅截断时出现)
  - 截断时的收尾帧:`event: truncated` / `data: {"next_seq": N}`,**不发 `end`**

**背景与一处必须知道的约束**:spec §六E 写的是"响应头给 `X-Expert-Work-Next-Seq`"。但 HTTP 响应头在流开始前就发完了,而"是否截断"只有读完一页才知道 —— **在生成器体内没有任何办法再改响应头**。所以:

1. `build_event_producer` 改成 `async def`,回放分支的第一页在**返回迭代器之前**就读掉,`next_seq` 因此在构造 `StreamingResponse` 时已知。附带好处:数据库出错会变成正常的 500 JSON,而不是一个已经开始流式输出、半截截断的 body。
2. **同时发一个 `truncated` 帧**。这是对 spec 的**增补**,理由:浏览器 `EventSource` 读不到响应头 —— 只给 header 的信号对一整类客户端不可用。帧和头同时给,成本是几行代码。

页大小沿用 `MAX_LIST_LIMIT`(=500,`runs/store.py:507`)。截断判定:读回的行数 == 页大小,且 `event_store.list(since_seq=最后一行.seq, limit=1)` 非空。**不要**用"行数 == 页大小"单独判定 —— 恰好整除时会误报截断。

- [ ] **Step 1:写失败测试**

1. `test_replay_truncates_without_end_frame`:造 `MAX_LIST_LIMIT + 10` 帧的终态 run → 返回头有 `X-Expert-Work-Next-Seq`,body 最后一帧是 `truncated` 而**不是** `end`,帧数 == 页大小 + 1。
2. `test_replay_exact_page_size_is_not_truncated`:恰好 `MAX_LIST_LIMIT` 帧 → **没有** `X-Expert-Work-Next-Seq`,最后一帧是 `end`。(这条钉住上面那个 off-by-one。)
3. `test_replay_cursor_loop_covers_every_frame`:按头里的 `next_seq` 循环拉,直到收到 `end`;把各页帧拼起来,seq 必须是 `range(总帧数)`、无重复无缺口。

- [ ] **Step 2:跑测试确认失败**

- [ ] **Step 3:实现**

改 `build_event_producer` 为 async + 返回 `EventStreamPlan`(frozen dataclass)。三个调用点跟随:`runs.py:1719`、`external_events.py:102`、以及 `external_events.build_events_response`(它被 `agents.py` 的幂等重放分支复用 —— 那条路径也要拿到同样的头,否则重放和首次响应的头集合又会分叉,P2-a 刚修过这类问题)。`build_events_response` 因此也变成 async,`agents.py` 的两个调用点跟着 await。

- [ ] **Step 4:跑测试确认通过**

- [ ] **Step 5:变异自证**

把截断判定改回"行数 == 页大小" → 测试 2 必须红。删掉 `truncated` 帧只留头 → 测试 1 必须红。

- [ ] **Step 6:提交**

```bash
git add -A && git commit -m "fix(sse): 回放分页——截断发 truncated 帧 + Next-Seq 头,不再补假 end"
```

---

### Task 5:`end` 帧带终局状态

**Files:**
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py`
- Test: `services/control-plane/tests/test_run_event_stream.py`

**Interfaces:**
- Consumes:Task 1 的 `publish_end(status=...)`;Task 4 的 `EventStreamPlan`
- Produces:`event: end` / `data: {"status": "success"|"paused"|"interrupted"|"error", "run_id": "<uuid>"}`

**背景**:现在 run 被取消只发 `end` + `data: null`,第三方分不清"正常答完"和"被取消",得再查一次 REST。

两条路径的 status 来源不同,**都要接**:
- replay 分支:`run.status` 映射(`SUCCESS→success` / `PAUSED→paused` / `INTERRUPTED→interrupted` / `ERROR`、`TIMEOUT→error`)。`build_event_producer` 现在只收 `is_terminal: bool`,要改成同时收终局状态 —— 让调用方传 `run.status`,`is_terminal` 在函数内部推导(现有 docstring 已经写明"`is_terminal` 在函数内部推导,所以只有一处可能弄错",保持这个立场)。
- live 分支:从 bridge 的 end 帧 data 里取(Task 1 Step 4 已存 `end_status`)。

- [ ] **Step 1:写失败测试**

1. `test_end_frame_carries_status_on_cancelled_run`:终态为 `INTERRUPTED` 的 run 回放 → `end` 帧 data 是 `{"status": "interrupted", "run_id": ...}`。
2. `test_end_frame_status_on_paused_run`:`PAUSED` → `"paused"`(**不是** `error` —— 等审批不是失败)。
3. `test_live_end_frame_carries_status`:live 分支收到的 `end` 帧同样带 status。

**必须用被取消 / PAUSED 的 run** —— 正常跑完的 run 上 status 恒 `success`,任何写错的映射表都能跑绿。

- [ ] **Step 2:跑测试确认失败**

- [ ] **Step 3:实现**

- [ ] **Step 4:跑测试确认通过**

- [ ] **Step 5:核查前端 6 处 `end` 消费点**

`rg -n 'event === "end"' apps/admin-ui/src` 逐一读:`components/turn/useHistoryTurns.ts:171,181`、`api/timeline.ts:142`、`pages/run_detail/EventStreamPanel.tsx:91`、`pages/ConversationDetail.tsx:170`、`pages/agent_detail/PlaygroundTab.tsx:462,481,589,606`。当前它们只做 `break` / 判断最后一帧是不是 `end`,不读 `data` —— 所以 `data: null → 对象` 是纯加法。**逐处确认这一点并在报告里写出结论**(不是"应该没事",是读过之后的判断)。若发现有读 `data` 的地方,一并改。

- [ ] **Step 6:提交**

```bash
git add -A && git commit -m "feat(sse): end 帧带终局状态,取消与答完可区分"
```

---

### Task 6:前端跟改 —— 帧 id 解析复验 + 游标翻页

**Files:**
- Verify: `apps/admin-ui/src/api/sse_id.ts`、`api/timeline.ts`、`api/tool_timeline.ts`、`api/worker_timeline.ts`、`api/gantt_timeline.ts`
- Modify: 消费 `GET .../runs/{run_id}/events` 的前端调用点(用 `rg -n 'runs/.*/events' apps/admin-ui/src` 定位)
- Test: `apps/admin-ui/src/**/__tests__/`

**Interfaces:**
- Consumes:Task 4 的 `truncated` 帧 + `X-Expert-Work-Next-Seq` 头

- [ ] **Step 1:复验 token 帧不进 timeline 路径**

`serverMsOf(id)` 从帧 id 取毫秒段,正则是 `^(\d{10,})-\d+$`。Task 1 之后 token 帧的 id 是 `None`。**实证**(不是查记忆):token 帧是否会走到 `parseTimeline` / `tool_timeline` / `worker_timeline` / `gantt_timeline`。记忆里有"token 帧不进 `turn.events`"的结论,但那是 2026-07 的,必须以当前代码定论。写一条测试把结论钉住,而不是只在报告里陈述。

- [ ] **Step 2:写失败测试 —— 截断后前端要继续拉**

给消费 events 端点的 hook 加测试:mock 一个先返回 `truncated` + `next_seq=500`、再返回 `end` 的两页流,断言 hook 最终持有两页的全部帧。

- [ ] **Step 3:跑测试确认失败**

Run: `cd apps/admin-ui && pnpm vitest run <新测试文件>`

- [ ] **Step 4:实现游标循环**

收到 `truncated` 帧就用其 `next_seq` 重新发起同一请求,拼接帧,直到收到 `end`。加一个循环上限(比如 20 页)并在超限时打日志 —— 别写出一个能无限拉的循环。

- [ ] **Step 5:跑全套前端测试**

Run: `cd apps/admin-ui && pnpm vitest run && pnpm tsc --noEmit`
(编辑器诊断在这个仓库多次给过 stale 的假阳性,**以真 tsc + vitest 定论**。)

- [ ] **Step 6:提交**

```bash
git add -A && git commit -m "fix(admin-ui): 事件流跟游标翻页,长 run 不再静默截断"
```

---

### Task 7:文档最小事实校正

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/sse-events.md`
- Modify: `apps/admin-ui/docs-site/guide/run-agent.md`
- Modify: `apps/admin-ui/docs-site/guide/quickstart.md`

**这个 Task 的边界**:只做"改完之后原文档变成假话"的那些句子。**完整重写在 PR-2**,不要在这里提前动手。

- [ ] **Step 1:逐条改**

| 文件:行 | 现在写的 | 改成 |
|---|---|---|
| `sse-events.md:10-14` | 整个 `::: warning` 块说 live 分支忽略 `since_seq`、要客户端自己去重 | live 分支现在认 `since_seq` 了。改写这一段:重连一律带上已处理到的 seq,服务端会补齐并去重;客户端仍需处理 token 帧不回放 |
| `sse-events.md:19,25` | "除 `end` 帧外每一帧都带 `id:`" | "除 `end` / `truncated` / `token` 外每帧都带 `id:`" |
| `sse-events.md:31` | metadata 含 "trace id" | 删掉 —— payload 实际只有 `run_id` / `thread_id`(`sse.py:484` 为准) |
| `sse-events.md:37` | `end` 行的说明 | 补上 `data` 里的 `status` 四值含义 |
| `sse-events.md:75` | `since_seq` "只在 run 已经结束的回放分支上生效" | 两条分支都生效 |
| `run-agent.md:247` | 同样说 `since_seq` 在 live 分支不生效 | 同步改 |
| `quickstart.md:38` | 样例 `data: {"run_id":"...","thread_id":"...","trace_id":"..."}` | 删掉 `trace_id` |

- [ ] **Step 2:补 `truncated` 帧和 `end.status` 到事件表**

`sse-events.md` 的事件类型表加两行(`truncated` 一行;`end` 行补 `data` 说明)。完整的帧字段详解归 PR-2。

- [ ] **Step 3:文档站构建**

Run: `cd apps/admin-ui/docs-site && pnpm build`
Expected: 构建通过。

- [ ] **Step 4:机密红线自查**

改动里不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径(沿用 #1151 的红线)。

- [ ] **Step 5:提交**

```bash
git add -A && git commit -m "docs: SSE 契约变更同步——since_seq/id/end.status/trace_id 勘误"
```

---

### Task 8:全链自测

**Files:** 无新增

- [ ] **Step 1:后端全量**

Run:
```bash
cd services/control-plane && DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest -m "not integration" -q --timeout=300
cd packages/expert-work-runtime && DOCKER_HOST= uv run pytest -q --timeout=300
cd services/orchestrator && DOCKER_HOST= uv run pytest -q --timeout=300
```

- [ ] **Step 2:CI 同款 lint / 类型**

Run:
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy <CI 里的同款范围——照 .github/workflows/ci.yml 抄,别自己缩小>
```

- [ ] **Step 3:前端全量**

Run: `cd apps/admin-ui && pnpm vitest run && pnpm tsc --noEmit && pnpm build`

- [ ] **Step 4:开 PR**

PR 描述要列出:四条缺陷各自的**会红条件**与对应测试、变异自证的结果、前端 6 处 `end` 消费点的核查结论、以及 `truncated` 帧这一处对 spec 的增补及其理由。
