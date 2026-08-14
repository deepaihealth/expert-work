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

---

> ## ⚠️ 2026-08-14 重做裁定 —— 先读这一段
>
> **Task 1 的"生产者预分配 seq"方案、Task 2.5 的不变式、以及 Task 3 的整套接合算法
> (pending 重排窗口 / 两个泄压维度 / missing 名单)全部作废,由下方的 【Task 3R】取代。**
>
> 起因:用户要求先看业界怎么解决。查下来结论很干脆 —— **乱序是我们自己造出来的**。
> Redis Streams(`XADD *` 由服务端在 append 那一刻发号)、Kafka(broker 作为单写者
> 在 append 时分配 offset,顺序与幂等去重都建立在这个串行化点上)、LangGraph Platform
> (事件自带顺序元数据),做法一致:**发号权归日志,不归生产者**。
>
> 我们现在是生产者先领号、再 `await` 推 bridge,领号与入队之间那个 await 就是乱序的
> 来源;而 bridge 自己的发号(`memory.py::_next_id`)又恰好写在临界区**外面**。
> 把发号挪进 `async with stream.condition` 并让 `publish` 返回 seq,
> **bridge 顺序恒等于 seq 顺序,乱序在物理上不可能发生**,撞号也由锁本身杜绝
> (比"抢在 await 之前"这种时序约定可靠得多)。
>
> 于是 pending / missing / `_REORDER_WINDOW` / 泄压两维度**全部退场** —— 删掉的正是
> 全 PR 最难证明正确的那一段。**净减代码。**
>
> 已完成且**不受影响**的:Task 2(端到端 seq 自证)、Task 4(回放分页)、Task 5(end 帧状态)。

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

### Task 2.5:加固 Task 1 的不变式钉子(实施反馈补入)

**Files:**
- Modify: Task 1 Step 7 那条并发不撞号的测试(位置见 `.superpowers/sdd/2026-08-14-p3-pr1/task-1-report.md`)

**为什么补这个 Task**:Task 1 的实施者自报了一条软肋 —— 那条测试红在"落库行数不连续",真正的归因(`duplicate seq=1`)只出现在**日志**里,不在断言里;而且它依赖一个强制交错的 `_YieldingBridge` 桩,**换成裸 bridge 会退化成恒绿**。这正是本仓库记录过的失败形态:测试在"不可能失败的条件下"跑绿,看起来跟真的钉住了一模一样。

- [ ] **Step 1:把归因搬进断言**

不要断言"行数连续"这种间接量。直接收集全部分配到的 seq,断言:

```python
seqs = [r.seq for r in persisted_rows]
assert len(seqs) == len(set(seqs)), f"seq 撞号: {sorted(s for s in seqs if seqs.count(s) > 1)}"
assert sorted(seqs) == list(range(2 * N))
```

第一条断言的失败信息必须**直接指出撞的是哪个号**,不依赖读日志。

- [ ] **Step 2:让桩的必要性变成显式的**

`_YieldingBridge` 是这条测试成立的前提(裸 bridge 不强制交错 → 测试恒绿 → 假验证)。两件事:
1. 在测试的 docstring 里写明这一点 —— "本测试**必须**用强制交错的桩,换成裸 bridge 会退化成恒绿"。
2. 加一条**元断言**钉住这个前提:在测试里断言所用 bridge 确实是那个桩(`assert isinstance(bridge, _YieldingBridge)`),这样后人把它换成裸 bridge 时测试会立刻红,而不是静静地变成空转。

- [ ] **Step 3:变异复验**

把 `_alloc_seq` 的自增挪到 `await` 之后,断言必须红,**且失败信息里直接出现撞掉的那个 seq**(不用去翻日志)。`git diff` 先确认变异落地。恢复。

- [ ] **Step 4:提交**

```bash
git add -A && git commit -m "test(sse): 并发不撞号的断言直接归因,并钉住交错桩这个前提"
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
   - entry.seq == last + 1    → yield,last += 1;然后把 pending 里
                                 last+1、last+2 … 连续的部分一并 yield 掉
   - entry.seq >  last + 1    → 暂存进 pending(见下),**不要立刻当缺口处理**
   - entry.seq <= last 但 seq ∈ missing → **照发**(见第 5 条),并从 missing 移除
3. pending 泄压,两个维度**任一**触发:
     (a) len(pending) > _REORDER_WINDOW(=32),或
     (b) min(pending) - last - 1 > _REORDER_WINDOW
   触发后认定 last+1 起真的缺了 —— 去 event_store.list(since_seq=last, limit=…)
   把 last+1 .. min(pending) - 1 补齐并 yield(补到多少算多少),再排空 pending 里
   连续的部分。
4. 补不齐的那些 seq 记进 missing 集合,打一条 warning
   (`missing_from=… missing_to=…`),不抛异常。
5. missing 是"写过检讨但还没死心"的名单:后到的帧只要在 missing 里就照发,
   不因为 seq <= last 被丢。它保证**任何真实存在过的帧都不会因为本算法的
   判断失误而消失**,代价只是它可能乱序到达(客户端本来就按 seq 排)。
6. 流结束(end 帧)前:把 pending 里剩下的按 seq 升序全部 yield 掉,别吞。
```

**为什么泄压要两个维度 + 一个 missing 名单**(Task 3 实施反馈,控制方 2026-08-14 裁定):
只有 (a) 一个维度时,"跳号之后 run 恰好安静下来"这种情况永远撑不爆窗口 —— 缺的那段
在这条 live 连接上根本不补,而"服务端补齐"正是本项对外承诺的东西。加了 (b) 之后,
一个孤立的大跳号立刻触发回填。
但 (b) 单独存在会**重新引入它本来要避免的伤害**:并发 worker 上限可配到 64,理论上
真有可能 40 帧同时在飞、跳号 40 却一帧没丢;此时查库(后台攒批 writer 还没落盘)拿不到,
等真帧到达时又已经 `seq <= last` 被丢弃。missing 名单就是为这一种情况兜底 —— 我们
可以判断错,但**不能因为判断错就把真实存在的帧扔掉**。

**为什么要 pending 重排窗口,而不是见缺口就查库**(Task 1 实施反馈补入):`_publish_frame` 是"同步分号 → await publish",并发 worker 下**先分到号的帧可能后进 bridge**。所以 seq 跳号的第一解释是"乱序到达",不是"丢了"。见缺口就查库会有两个后果:一是把还在路上的帧当丢帧去查一次库(此时后台攒批 writer 多半还没落盘,查了也拿不到),二是等真帧从 bridge 到达时它已经 `<= last` 被丢弃 —— **本来没丢的帧被这个逻辑弄丢了**。重排窗口先给乱序一个收敛机会,窗口撑爆了才认定是真缺口。

**为什么仍然需要缺口回填(第 3 步)**:bridge 缓冲区只有 256 帧、drop-oldest,而落库走的是攒批后台 writer。补库读完到挂上实时流之间,如果有超过 256 帧飞过,中间那段既不在缓冲区里也可能还没落库。第 3 步是唯一能把这种情况变成"可观测的 warning"而不是"静默丢帧"的写法。

`entry.seq` 不在 `StreamEvent` 上 —— 从 `entry.id` 解析(`int(entry.id.rsplit("-", 1)[1])`),`id is None` 即 token 帧。在 `_run_event_stream.py` 里写一个模块级 `def _seq_of(entry) -> int | None` 承担这件事,别在循环里内联。

- [ ] **Step 1:写失败测试(三条)**

1. `test_live_reconnect_backfills_from_store`:run 处于 RUNNING;库里已有 seq 0..4;带 `since_seq=1` 重连 → 必须先收到 seq 2,3,4。
2. `test_live_reconnect_dedupes_overlap`:补库给到 seq 4,随后 bridge 推 seq 3,4,5 → 客户端只收到 5,且 3/4 不重复。
3. `test_live_reconnect_fills_gap`:补库给到 seq 2,bridge **只**推 seq 35(跳过 3..34,超过重排窗口),而库里此时有 3,4 → 客户端按序收到 3,4,然后 35,并且日志里有一条 warning 说明 5..34 补不齐。
4. `test_live_out_of_order_frames_are_reordered_not_dropped`:bridge 按 `5,3,4,6` 的顺序推(模拟并发 worker 下先分号后进 bridge),客户端收到的必须是 `3,4,5,6` —— **一帧不丢**。这条钉住重排窗口:见缺口就查库的写法会让 3、4 到达时已经 `<= last` 而被丢弃,这条测试会红。
5. `test_pending_frames_are_flushed_before_end`:bridge 推 `3,5` 然后 end,窗口没撑爆 → 5 必须在 end 之前被 yield 掉,不能被吞。
6. `test_isolated_large_jump_triggers_backfill`(维度 b):补库给到 seq 2,bridge **只推一帧** seq 35 之后就安静,库里此时有 3,4 → 必须**立刻**回填 3,4 并发出 35,不能等到 end。这条钉住泄压条件的第二个维度 —— 只有 `len(pending) > 窗口` 那一维时它永远撑不爆,必然红。
7. `test_late_frame_written_off_is_still_delivered`(missing 名单):制造一次回填失败(库里没有 5),让 5 进 missing;随后 bridge 才把 seq 5 推过来 → **必须照发**,不能因为 `5 <= last` 被丢。这条钉住"判断可以错,但不能因此扔掉真实存在的帧"。

五条都必须在 **RUNNING** 状态的 run 上跑(`is_terminal=False`),否则走的是 replay 分支,测了等于没测。

- [ ] **Step 2:跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_run_event_stream.py -v -k live`

- [ ] **Step 3:实现接合算法**

按上面的伪码改 `_stream_live`。`scope` 工厂在 live 分支的补库读上同样要用(控制台调用方传的是 `lambda: applied_scope(scope)`,单次可用 —— **每次读都要重新调工厂**,`build_event_producer` 的 docstring 已经记了这条坑)。

- [ ] **Step 4:跑测试确认通过**

- [ ] **Step 5:变异自证(逐条)**

- 把去重条件从 `<= last` 改成 `< last` → 测试 2 必须红。
- 删掉缺口回填分支 → 测试 3 必须红。
- 把重排窗口去掉、改成见缺口立刻查库 → 测试 4 必须红。
- 把 end 前排空 pending 那步删掉 → 测试 5 必须红。
- 去掉泄压条件的 (b) 维度 → 测试 6 必须红。
- 去掉 missing 名单(后到帧一律按 `seq <= last` 丢) → 测试 7 必须红。
- 把补库循环改成只读一页 → 造一个 >`MAX_LIST_LIMIT` 帧的 RUNNING run 的测试必须红(如果现有测试都不咬这个变异,就补一条)。

每次变异前 `git diff` 确认真的改到了文件。

- [ ] **Step 6:提交**

```bash
git add -A && git commit -m "fix(sse): live 分支认 since_seq——补库接合 + 缺口回填"
```

---

### 【Task 3R】重做:发号权归 bridge,接合退化成去重

**本 Task 取代 Task 1 的"生产者预分配 seq"、Task 2.5 的不变式、以及 Task 3 的整套接合算法。**
已落地的 `57d0f648`(Task 3)和 `3432a82d`(Task 3-fix)的接合逻辑要重写;`ee49b042`(Task 2.5)
钉的不变式将不复存在,那条测试必须**改成钉新不变式或删除** —— 不允许留一条测不到任何东西的测试。

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/base.py`
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/stream_bridge/memory.py`
- Modify: `services/orchestrator/src/orchestrator/sse.py`
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py`
- Test: `packages/expert-work-runtime/tests/test_in_memory_stream_bridge.py`
- Test: `services/control-plane/tests/test_run_event_stream.py`
- Test: Task 2.5 那条并发测试所在文件

#### 为什么重做(裁定依据)

现在是**生产者预分配**:`seq = _alloc_seq()` → `await bridge.publish(...)` → `_enqueue_event(seq, ...)`。
领号和入队之间隔着一个 `await`,并发 worker 一交错就乱序。而 bridge 自己的发号
(`memory.py::_next_id`)又恰好写在 `async with stream.condition` **外面**。

业界做法一致 —— **发号权归日志,不归生产者**:

- **Redis Streams**:`XADD *` 由服务端在 append 那一刻发 `<毫秒>-<序号>`。客户端自带 ID 时,
  必须自己保证严格大于流中现有全部 ID,否则命令失败。
- **Kafka**:broker 作为单写者在 append 时分配 offset;顺序与幂等去重都建立在这个串行化点上。
  生产者预分配序号正是它明确不做的事。
- **LangGraph Platform**(本 bridge 结构上照抄的对象):事件自带顺序元数据,客户端凭
  "最后见过的那个"续接。

把发号挪进临界区并让 `publish` 返回号之后:**bridge 顺序恒等于 seq 顺序,乱序在物理上
不可能发生**;撞号由锁本身杜绝,比"抢在 await 之前"这种时序约定可靠得多。

#### 契约(照这个实现,别自创)

```python
# base.py —— 两个方法代替原来一个,避免 Optional 返回值污染调用方
async def publish(self, run_id: UUID, event: str, data: Any) -> int:
    """发布一帧可回放的事件,返回 bridge 分配的 seq。

    seq 的分配与入队在同一个临界区内完成 —— 这是本类的核心不变式:
    **订阅者看到的帧顺序恒等于 seq 顺序**。调用方不得自己发号。
    """

async def publish_ephemeral(self, run_id: UUID, event: str, data: Any) -> None:
    """发布一帧一次性事件(当前只有 ``token``):不发号、不带 ``id:``、不可回放。"""

async def seed_seq(self, run_id: UUID, *, next_seq: int) -> None:
    """HA failover:被接管的 run 在新副本上重入时,把发号器推过前任已落库的尾部,
    否则新号会与前任的行撞 ``(run_id, seq)`` 主键。"""
```

`memory.py` 的实现要点:

```python
async def publish(self, run_id, event, data) -> int:
    stream = self._get_or_create_stream(run_id)
    async with stream.condition:                    # ← 发号必须在锁内
        seq = self._counters[run_id]
        self._counters[run_id] = seq + 1
        entry = StreamEvent(id=f"{int(time.time() * 1000)}-{seq}", event=event, data=data)
        stream.events.append(entry)
        ...(溢出丢弃逻辑不变)...
        stream.condition.notify_all()
    return seq
```

`seed_seq` 用 `max(现值, next_seq)` 写入,防止迟到的播种把发号器往回拨。
`_next_id` 删除。

`sse.py` 的改动:

- **删掉** `_alloc_seq` 和 `event_seq` 这个 nonlocal —— 生产者不再持有任何计数器。
- `_publish_frame` 变成两行:`seq = await bridge.publish(run_id, event_name, data)`
  然后 `_enqueue_event(seq, event_name, data)`。
- `_publish_token` 改调 `publish_ephemeral`。
- resume 播种从 `event_seq = await event_store.next_seq(...)` 改成
  `await bridge.seed_seq(run_id, next_seq=await event_store.next_seq(run_id=run_id))`,
  触发条件不变(`event_store is not None and is_resume`)。

#### live 接合(取代 Task 3 的整套算法)

```
last = since_seq if since_seq is not None else -1

1. 补库:循环 event_store.list(run_id, since_seq=last, limit=MAX_LIST_LIMIT)
   直到某页返回条数 < limit。每行 yield,last = row.seq。
2. 挂实时流 async for entry in bridge.subscribe(run_id, ...):
   - entry.id is None(token 帧) → 直接 yield
   - seq <= last   → 丢弃(补库阶段已发过)
   - seq == last+1 → yield,last = seq
   - seq >  last+1 → **真缺口**(唯一成因是 bridge 缓冲区 256 帧 drop-oldest)。
       先 event_store.list(since_seq=last, limit=MAX_LIST_LIMIT) 补齐能补的,
       逐行 yield 并推进 last;
       若补完仍有 last+1 < seq,yield 一帧 `gap`:
           event: gap
           data: {"from": last+1, "to": seq-1}
       再 yield entry,last = seq。
```

**pending / missing / `_REORDER_WINDOW` / 两个泄压维度 / end 前排空 pending —— 全部删除。**
跳号现在**没有歧义**:bridge 顺序恒等于 seq 顺序,所以 `seq > last+1` 只可能是真缺口。

**为什么用 `gap` 帧而不是服务端记账**:缺口天然有界(一帧就是一帧,不占内存),而且把
"这里缺了 5~34"变成客户端看得见、能自己决定要不要重拉的信息,而不是服务端偷偷积累一个
无上限的集合。pub/sub 领域的通行做法(缺口 tombstone 帧)就是这个。`gap` 帧**无 `id:`、不落库**
——它描述的是这条连接的状况,不是 run 的事件。

- [ ] **Step 1:写失败测试(六条)**

1. `test_bridge_order_equals_seq_order_under_concurrency`(**本 Task 的命门**)——
   N 个协程并发 `publish`,订阅者收到的 numbered 帧 id 里的 seq 必须**严格递增**,
   且 `publish` 各自的返回值集合恰为 `range(N)`、无重复。
2. `test_ephemeral_frames_carry_no_id_and_no_seq` —— `publish_ephemeral` 的帧 `id is None`,
   且不消耗号(前后两帧 numbered 的 seq 连续)。
3. `test_seed_seq_pushes_counter_past_durable_tail` —— 播种后第一个 `publish` 返回 `next_seq`;
   迟到的小值播种不会把发号器拨回去。
4. `test_live_reconnect_backfills_from_store` —— **RUNNING** 状态的 run,库里已有 seq 0..4,
   带 `since_seq=1` 重连 → 收到 2,3,4。
5. `test_live_reconnect_dedupes_overlap` —— 补库给到 seq 4,bridge 推 3,4,5 → 只收到 5。
6. `test_live_unfillable_gap_emits_gap_frame` —— 补库给到 seq 2,bridge 推 seq 8,库里只有 3,4
   → 依次收到 3、4、一帧 `gap {"from":5,"to":7}`、然后 8。

第 4~6 条**必须在 RUNNING 状态的 run 上跑**(`is_terminal=False`),否则走的是 replay 分支,
测了等于没测。

- [ ] **Step 2:跑测试确认失败**

Run:
```bash
cd packages/expert-work-runtime && DOCKER_HOST= uv run pytest tests/test_in_memory_stream_bridge.py -v
cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_run_event_stream.py -v -k live
```

- [ ] **Step 3:实现契约变更(bridge + sse.py)**

- [ ] **Step 4:重写 live 接合,删掉旧机制**

删除清单(逐项确认没有残留死代码):`_REORDER_WINDOW`、pending 容器、missing 容器、
两个泄压维度、end 前排空 pending 的分支、`sse.py::_alloc_seq`、`event_seq` nonlocal。

- [ ] **Step 5:处置 Task 2.5 那条并发测试**

它钉的不变式("同步分配抢在 await 之前")已经不存在。两条路二选一,在报告里说明选了哪条:
(a) 改成钉新不变式 —— 与测试 1 合并;(b) 删除。**不允许原样留着** —— 一条测不到任何
东西的测试比没有测试更坏,它会给坏版本发合格证。

- [ ] **Step 6:跑测试确认通过**

- [ ] **Step 7:变异自证(四条)**

| 变异 | 必须变红 |
|---|---|
| 把发号从 `async with stream.condition` 里挪到锁外面,**并在发号与加锁之间插一句 `await asyncio.sleep(0)`** | 测试 1 |
| 去重条件 `<= last` 改成 `< last` | 测试 5 |
| 删掉 `gap` 帧那一支 | 测试 6 |
| 补库循环改成只读一页(需配一个 >`MAX_LIST_LIMIT` 帧的 RUNNING run 测试;现有六条不咬就补第七条) | 新测试 |

**⚠️ 关于第一条变异 —— 本节初稿的机理是错的,已由实施实测修正(2026-08-14)。**

初稿写的是"未争用的 `asyncio.Lock` 获取不会让出控制权,所以加一句 `sleep(0)` 就能让协程交错"。
**实测:加了 `sleep(0)` 变异照样存活。** 真正的机理是:

> 单线程事件循环的就绪队列是 **FIFO**。如果变异给**每个**协程加的让出点是**等长的**
> (恰好一次),那么"发号 → 让出 → 入队"整体平移同样多轮次,**相对顺序被原样保住** ——
> 只看帧顺序的黑盒测试看不出任何区别。

所以纯黑盒的顺序断言**杀不死这条变异**,必须换判据。两条都要:

1. **白盒原子性探针**(测试 1):并发观察者在每个调度点检查
   `next_seq == 缓冲区里 numbered 帧的条数`。发号一旦离开锁就存在一个可观测的窗口
   (号已加、帧未入队),**不依赖调度运气**。代价是它读 `_RunStream` 的字段名 ——
   改名会让它红,这是提醒不是缺陷。
2. **桩里用不等长延迟**:让不同 worker 让出的轮次数不同(例如 worker `a` 多让出 2 轮)。
   真 bridge 本来每次耗时就不同,这是**更真实**,不是为了造红。

**顺带一条已证伪的想当然**:把读/自增拆到锁**内**的两句之间
(`seq = next_seq; await sleep(0); next_seq = seq + 1`)**不是 bug**(实测 12 passed)。
`Condition` 的锁会让第二个 `publish` 等在门外,读-改-写仍然原子。这正面印证了
**安全性来自那把锁,不来自"不让出控制权"**。

每次变异前 `git diff` 确认落地;还原**一律用 scratchpad 副本**,不许用任何 git 命令
还原未提交状态。

- [ ] **Step 8:提交**

```bash
git add -A && git commit -m "refactor(sse): 发号权归 bridge——锁内原子分配,接合退化成去重 + gap 帧"
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
- Modify: `services/orchestrator/src/orchestrator/sse.py`(`sse_consumer`,约 `:1449`)
- Test: `services/control-plane/tests/test_run_event_stream.py`
- Test: `services/orchestrator/tests/`(`sse_consumer` 的 end 帧)

**Interfaces:**
- Consumes:Task 1 的 `publish_end(status=...)`;Task 4 的 `EventStreamPlan`
- Produces:`event: end` / `data: {"status": "success"|"paused"|"interrupted"|"error", "run_id": "<uuid>"}`

**背景**:现在 run 被取消只发 `end` + `data: null`,第三方分不清"正常答完"和"被取消",得再查一次 REST。

**⚠️ 本 Task 有第三条路径,是 Task 1 实施反馈补入的 —— 而且它是第三方的主路径。**
`sse_consumer`(`sse.py:1449`)在 `is_end(entry)` 时写死 `yield format_sse("end", None)`,**把 Task 1 刚放进 bridge end 帧里的 status 又丢掉了**。它服务于 `runs.py:1091`、`runs.py:1797`、`external_approvals.py:311` —— 也就是 **`POST /v1/agents/{code}/runs` 且 `mode: "stream"`** 那条流(第三方最常用的路径,经 `spawn_run` 走到这里)以及审批决策的续跑流。只改 `_run_event_stream.py` 的话,F 只在"断线重连"这条次要路径上生效,主路径依然发 `data: null`,而且两条流的 `end` 帧字段集合会分叉 —— 这个仓库刚在 P2-a 修过一次同类的"重放响应头是首次响应的真子集"问题。

三条路径的 status 来源不同,**都要接**:
- replay 分支:`run.status` 映射(`SUCCESS→success` / `PAUSED→paused` / `INTERRUPTED→interrupted` / `ERROR`、`TIMEOUT→error`)。`build_event_producer` 现在只收 `is_terminal: bool`,要改成同时收终局状态 —— 让调用方传 `run.status`,`is_terminal` 在函数内部推导(现有 docstring 已经写明"`is_terminal` 在函数内部推导,所以只有一处可能弄错",保持这个立场)。
- live 分支:从 bridge 的 end 帧 data 里取(Task 1 Step 4 已存 `end_status`)。
- `sse_consumer`:同样从 bridge 的 end 帧 data 里取 —— `is_end(entry)` 那一支现在手上就有 `entry.data`,把它透传即可,别再合成一个 `None`。`run_id` 从 `record.run_id` 取。

- [ ] **Step 1:写失败测试**

1. `test_end_frame_carries_status_on_cancelled_run`:终态为 `INTERRUPTED` 的 run 回放 → `end` 帧 data 是 `{"status": "interrupted", "run_id": ...}`。
2. `test_end_frame_status_on_paused_run`:`PAUSED` → `"paused"`(**不是** `error` —— 等审批不是失败)。
3. `test_live_end_frame_carries_status`:live 分支收到的 `end` 帧同样带 status。
4. `test_sse_consumer_end_frame_carries_status`:走 `sse_consumer` 的那条流(POST 建 run 的 stream 模式),被取消的 run 的 `end` 帧同样带 `{"status": "interrupted", "run_id": ...}`。
5. `test_both_sse_paths_emit_the_same_end_shape`:同一个 run,`sse_consumer` 那条流和 `GET .../events` 那条流的 `end` 帧 **data 字段集合必须相同**。这条是防分叉的哨兵 —— 只改一条路径时它必须红。

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

- [ ] **Step 2:补 `truncated` / `gap` 帧和 `end.status` 到事件表**

`sse-events.md` 的事件类型表加三行(`truncated`、`gap`;`end` 行补 `data` 说明)。完整的帧字段详解归 PR-2。

三条**必须写清楚**的口径(都是本 PR 造成的行为变化,漏了就是留说谎文档):

1. **不带 `since_seq` 重连 = 从落库第 0 帧回放整个 run**,不是只补最近一段。
   (旧行为是从 bridge 缓冲区最早保留的那帧开始,最多回看 256 帧。)
2. **`truncated` 收尾的那一页不发 `end`**,所以那一页没有 `status` —— 客户端要循环拉到
   收到 `end` 才看得到终局状态。
3. **`gap` 帧的含义**:这一段帧在**这条连接**上补不到了(服务端缓冲已滚过、且尚未落盘),
   不代表它们不存在。run 结束后重新回放通常能拿到。`gap` 帧无 `id:`、不可回放。

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

PR 描述要列出:

- 四条缺陷各自的**会红条件**与对应测试、变异自证的结果。
- 前端 `end` 消费点的核查结论(实测是 **10 处**,不是本计划初稿写的 6 处)。
- 两处对 spec 的增补及其理由:`truncated` 帧(浏览器 `EventSource` 读不到响应头,
  只给 header 的信号对一整类客户端不可用)、`gap` 帧(缺口天然有界,且把状况交给
  客户端判断而不是服务端积累无上限的记账)。
- **发号权归 bridge 这个架构裁定**及其依据(Redis Streams / Kafka / LangGraph Platform
  一致的做法:发号权归日志不归生产者),以及它删掉了哪些东西(pending 重排窗口、
  missing 名单、`_REORDER_WINDOW`、两个泄压维度)。
- **行为变化**:`since_seq=None` 的 live 重连从"bridge 缓冲区最早保留帧(≤256)"
  变成"落库第 0 帧起回放整个 run";控制台调试台 / 对话详情页 / run 详情页三个页面
  都吃这条路径。
