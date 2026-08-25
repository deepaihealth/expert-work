# 对话条目模型(conversation items)设计

2026-08-25 · 对外 API v1

> 修订记录:初稿之后经过一轮对实时链路的侦察与 PR1 实现反馈,§三 / §四 / §五 /
> §六 / §八 / §十 / §十一 都有实质修正。凡标注「**修正**」的段落都是初稿写错、
> 照初稿实现会出真 bug 的地方。
>
> PR5 收尾时又对着**已合入的实现**扫了一遍,把所有悬置段落落成结论:标「**已定**」
> 的是「当初写着待验证 / 待补,现在有答案了」。这份 spec 是本 program 的单一事实源,
> 已经发生过「照它写任务书,把一个错数字传给下一个人」——**引用它的数字与结论之前,
> 先跟代码里的常量对一次。**

## 一、要解决的问题

第三方(workbuddy / openclaw 这类 agent 客户端)的真实场景:

1. 用户在会话列表里点开一段历史会话
2. 前端渲染出**与实时对话视觉一致**的界面 —— 同样的气泡、工具卡、计划卡
3. 用户直接继续说话,新内容追加到**同一个列表**

第 3 步决定了一切:第三方的聊天组件里只能有一个列表、一个 reducer。历史给一种形状、
实时给另一种形状,他们就得维护两套状态模型再合并。

今天做不到这件事,缺口有三:

* 会话级历史没有聚合接口。只能 `GET /runs` 拿轮次,再逐轮 `GET /runs/{id}/events`。
* 任何一路都不带**终端用户自己发的消息** —— 它是 graph 输入,躺在 checkpoint 里,
  从没进过事件流。
* 实时流的 `updates` 帧是 LangGraph 的节点更新,不是对话的形状。第三方要渲染一次
  工具调用,得从 `updates.agent.messages[]` 刨出 AIMessage 的 `tool_calls`,再从
  `updates.tools.messages[]` 刨 ToolMessage,靠 `tool_call_id` 配对。

## 二、行业对照

| 平台 | 历史接口 | 单位 |
| --- | --- | --- |
| OpenAI Conversations | `GET /v1/conversations/{id}/items` | Item 联合类型(26 种),流式事件裹同一个 item 对象 |
| Dify | `GET /v1/messages?first_id=` | message(含 `query` / `answer` / `agent_thoughts`) |
| Coze | `/v3/chat/message/list` | message,含中间结果 |
| LangGraph Platform | `/threads/{id}/history` | checkpoint 状态 |
| Vercel AI SDK | 持久化 `UIMessage` | 同一类型喂流式与回放 |

没有一家把历史做成事件回放。共识是**有类型的条目**,同构靠共享数据类型而非共享传输形态。

## 三、条目模型

对话的唯一表示。公共字段:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 条目标识。只保证同一响应内唯一、同一查询可重复,**不跨接口稳定** |
| `type` | string | 下表七种之一 |
| `run_id` | string | 产生它的那一轮 |
| `created_at` | string \| null | ISO8601。取不到时给 `null`,绝不编 |

七种类型:

| type | 专有字段 | 来源 |
| --- | --- | --- |
| `user_message` | `content`, `attachments` | checkpoint 的 HumanMessage |
| `assistant_message` | `content`, `channel`(`final` / `commentary`) | checkpoint 的 AIMessage |
| `tool_call` | `call_id`, `name`, `args`, `worker` | AIMessage 的 `tool_calls[]`,每个一条 |
| `tool_result` | `call_id`, `name`, `status`, `content`, `artifact`, `duration_ms` | checkpoint 的 ToolMessage |
| `plan` | `goal`, `steps[]` | 该轮 `plan` 帧,取最后一个 |
| `approval` | 见下 | 该轮 `approval` 帧 |
| `error` | `message`, `name` | 该轮 `error` 帧 |

**修正 —— `approval` 的字段**。初稿写的 `status` / `tool` / `args` 是错的:`status`
在任何帧里都不存在(`approval` 帧只在暂停那一刻发一次,人的裁定不在任何 SSE 帧里),
`tool` 在 policy_gate 下只以散文形式藏在 `action_summary`,agent 主动发起时连散文都没有。
按真实帧(= `ApprovalRequest` 的 json dump)取字段:

`request_id` / `node` / `reason_kind` / `action_summary` / `proposed_args` /
`requested_at` / `timeout_at`,外加一个 live 缺席、历史可能有的 `decision`。

一个都不能少的理由:`reason_kind` 是客户端**在提交决策之前**判断「拒绝会不会直接
终结这次 run」的唯一依据;`requested_at` / `timeout_at` 是倒计时的唯一来源,而对外
文档明令禁止把默认值写死在客户端。

`binding_digest` **有意不进条目**:它是平台内部的参数绑定校验值,对外文档写明客户端
原样忽略,提交决策的请求体也不收它。放进来只会让客户端以为自己该校验点什么。

`decision` 的取值域用 `ApprovalRecord.status`(`approved` / `rejected` / `modified` /
`timeout`)而不是 `ApprovalDecision.decision`(approve / reject / modify)——前者是终态
语义且多一个 `timeout`,正是历史条目要表达的东西。

**修正 —— `error` 要带 `name`**。`error` 帧是 `{"message", "name"}`,而对外文档已经
承诺了 `MaxStepsExceededError` 这个取值的语义。只留 `message` 会丢掉它。

**修正 —— `tool_result` 要带 `duration_ms`**。真实写入点在 ToolMessage 的
`additional_kwargs.duration_ms`。内部调试台的工具卡就靠它,第三方同样要。

`channel` 沿用既有判定,不新造语义 —— 判定实现已抽到
`expert_work.common.conversation_channel`,`transcript.extract_turns` 与条目推导共用
同一份,不允许第二份存在。

`tool_result.content` 给**还原后**的文本。包装函数是 `common/spotlight.py` 的
`spotlight_untrusted`;还原侧此前**只存在于前端 TypeScript**,现已在同一模块补上
`unspotlight`,与包装共用一份围栏格式常量。注意它不是双射:包装时空白段被压成一个
空格、换行已经丢了,还原只能恢复词、恢复不了版式。

一期不做 `system_step`(`memory_recall` / `workspace_ingest` / `reflect` /
`memory_writeback`):对第三方渲染价值低,先不让词表膨胀。

### 词表是契约

类型集合与字段集合必须由一处常量定义,并有契约测试钉住。本仓库有过同一词表分散多处
实现然后漂移的先例。

## 四、三条产出路径与同源

items 会从三条路径产出,任何一条漂了,第三方就会看到「实时和刷新后不一样」:

| 路径 | 场景 | 输入 |
| --- | --- | --- |
| 实时 SSE | 用户正在对话 | LangGraph chunk / token sink |
| 单 run 回放 | 刷新页面接上进行中那轮 | 落库的 legacy 帧 |
| 会话历史 | 点开历史会话 | checkpoint messages + 该轮 legacy 帧 |

**核心推导只有一个函数**(`expert_work.common.conversation_derive`),输入是消息列表
加辅助信号,输出是 items。三条路径都能提供这个输入 —— `updates` 帧里裹着的就是
messages。

这一点已被证据链坐实:`sse.py` 在 publish 之前就调了 `_to_jsonable`(BaseMessage →
`model_dump()` 递归),bridge 与事件库都原样存取,所以**实时与落库两条路径上
`messages` 的元素类型完全一致**,都是已经 jsonable 的 dict,推导函数不需要类型分支。
推导按鸭子类型读消息,不绑 LangChain 具体类。

### 转换发生在消费端

`_publish_frame`(`orchestrator/sse.py`)是所有持久帧的唯一收口,事件库是**所有连接
共享的一份**。而 `stream_format` 是每条连接的选择。因此:

* 事件库永远只存 legacy 帧。不双写、不迁移。
* 回放在读取时转换(`_run_event_stream.py` 的 `_encode`,`hide_events` 判断之后)。
* 实时在转发时转换(`sse.py` 的常规帧唯一出口,在 `is_end` 与 `hide_events` 之后)。

`_encode` 已经返回 `list[bytes]`,六个调用点全是 `for chunk in _encode(...)`,所以
一转多的扇出天然合法,不用改调用侧。

游标 / `truncated` 判定 / live 去重 / 缺口回填四条不变式**不受影响** —— 它们全部用
未编码的原始行计算,转换器物理上够不到。

### 修正 —— item id 必须确定性派生,不能用自增计数器

`_encode` 的六个调用点里有三条会毁掉自增状态:

* **补洞重发是乱序的** —— 空洞里的序号结构上恒小于已发过的帧
* **回放截断把一条流切成两条连接** —— 客户端带 `since_seq` 新建连接,计数器归零
* **live 接合会重放**缓冲区里的陈旧帧

所以 id 派生自 `(seq, 消息下标, 调用下标)`:乱序无害、跨页稳定,并且顺带满足「同一
查询可重复」的承诺。

### 修正 —— `channel="final"` 在实时路径判不出来

判定要向后看一条消息(`nxt = collected[i+1]`),流式时那条还不存在。这是本设计头号
风险「同源漂移」的具体着火点:实时给 `final`、刷新后历史给 `commentary`。

解法:实时先一律发 `commentary`;run 结束时,对最后一条符合 final 条件的
`assistant_message` **补发一个 `item.done`** 把 channel 改成 `final`。客户端 reducer
本来就必须把 `item.done` 当 upsert 处理(见 §五),所以不引入新机制。

**另有一处规则差异,已实测确认不等价**:整 thread 判定与单轮判定在「审批续跑」这条
路径上会分歧 —— continuation run 从 checkpoint 续跑、不写新用户消息,整 thread 算法
看到上一轮尾巴后面还有非开段行判 commentary,单轮算法把本轮结束当段尾判 final。今天
这条路径上停在审批前的助手消息必带 `tool_calls`(带了才会被拦),两边都判 commentary,
所以分歧暂时观察不到 —— 但规则确实不同,不要当成等价。

### 黄金测试

跑一个真 run,收集实时产出的 items,再从单 run 回放与会话历史两条路径分别重建,
断言三者相等(`id` 与 `worker` 除外)。没有这个测试,同源只是口头承诺。

**`worker` 必须与 `id` 一起排除**,因为 §五 拍板了它只在历史里填(PR5 实现)。但
排除不等于不测:那是设计允许的差异,不是可以不管的差异,所以两个方向都要正向断言
—— 历史侧确实填了(`test_external_session_items.py`),实时与回放侧确实没填
(`test_stream_items.py` / `test_run_event_stream_items.py`)。只排除不断言的话,
以后有人顺手在转换器里也填一份,三条路径真分叉了也不会有测试变红。

**这个 run 必须是多步的、带工具调用、带多条 assistant_message。** 只跑一问一答时,
实时的局部判定(无 tool_calls ⟹ final)恰好与历史一致,测试会全绿而 bug 仍在 —— 那
正是「修复自带的测试给坏版本发合格证」那一类。

## 五、实时:条目生命周期事件

| 事件 | 时机 | data |
| --- | --- | --- |
| `item.added` | 条目出现,内容可能为空 | 完整 item,字段可能不全 |
| `item.delta` | 文本逐字产出 | `{id, field, text}`,`field` ∈ `content` / `reasoning` |
| `item.done` | 条目完成 | 完整 item,字段齐全 |

**修正 —— `field` 没有 `args`**。初稿写了 `args` 频道,但工具参数今天根本不流式
(`streaming_redact.py` 明确写了这一点),没有任何生产者。留作保留值,不写进对外文档。

**硬约束 —— `item.delta` 不带 seq。** 它由今天的 `token` 帧转换而来,而 `token` 是
ephemeral 的:不落库、不占序号。一旦让不可回放的帧占用 seq,客户端从实时流解析出的
续传位点就会跑到 `since_seq` 实际能回放的范围之外,断线重连**静默漏事件**。客户端的
续传位点只能取自带 seq 的帧。

**修正 —— 事件集是 11 个,不是 9 个。** 初稿写的「9 个」既漏了 `worker`(本节下面
自己拍板它留在 wire 上),列出的另外十个加起来也不是 9。写文档一律以
`orchestrator.stream_items.ITEMS_WIRE_EVENTS` 这个常量为准去数,别照下面的散文抄 ——
PR3 已经用显式字面量把 11 个钉进 `tests/test_stream_items_vocabulary.py::
test_items_wire_vocabulary_is_closed`。

items 模式下的事件集(11 个):

* 内容 —— `item.added` / `item.delta` / `item.done`
* 流控 —— `metadata` / `end` / `gap` / `truncated`
* 过程提示(可忽略)—— `guard` / `compaction` / `retry` / `worker`

不再发 `token` / `updates` / `plan` / `approval` / `error`:前两个被条目生命周期取代,
后三个变成 item。

### 修正 —— 三条路径的事件序列不对称

回放里根本没有 `token` 帧(它走 `publish_ephemeral`,完全绕开落库),所以:

| 路径 | 序列 |
| --- | --- |
| 实时 | `item.added` → `item.delta`* → `item.done` |
| 单 run 回放 | 只有 `item.done` |
| live 接合 | 补库段只有 `item.done`,接上实时后才有完整三段 |

**客户端 reducer 必须把 `item.done` 当 upsert 处理**(允许对一个从没 `added` 过的 id
直接 done)。这条必须写进对外文档 —— 第三方若按 OpenAI 的 added→done 严格配对写
reducer,会在回放上崩。

### 修正 —— `worker` 的处置(拍板)

items 模式下 `worker` **仍作为独立事件发,不转换**;`tool_call.worker` 字段只在
**历史**里填(从落库的 worker 帧重建)。否则实时要等子任务 `end` 帧才能发工具卡的
`item.done`,时机语义会很别扭。

这是唯一不完全同构处,对外文档诚实标注。

重建规则照抄前端现有分层:深度 1 的 worker 按 `parent_tool_call_id` 挂到工具卡(它就
是 LangChain 的 `tool_call_id`,与 `updates` 里 `ai.tool_calls[].id` 同值),更深的按
`parent_worker_id` 挂树 —— 孙 worker 的 `parent_tool_call_id` 指向子 run 内部的
tool_call,那个 id 从来不出现在父 run 的 `updates` 里。

### 已定 —— `tool_call` 的 `item.added` 用 `call_id` 配对

> **本节原为「待验证」,PR3 已落定,两个候选分支一个都没走。** 初稿的前提
> ——「`tool_args` 帧只有 `tool_index`,拿不到配对键」—— 在 PR #1278 之后就不
> 成立了:那个 PR 把 `call_id` 放进了 `tool_args` 帧。下面是实现的实际做法。

`item.added` 照发,键用 **`call_id`**(`stream_items.py` 的 `_tool_preview`)。
`call_id` 与 `AIMessage.tool_calls[].id` 同值,而权威 `updates` 帧那一侧用的是同
一个公式,所以预览卡与随后的 `item.done` **天然同号**,不需要任何连接级配对状态。

**绝不能用 `tool_index`。** 它不是 `tool_calls[]` 的数组下标:Anthropic 路径上它
是**内容块**下标(text / thinking 块也占号),而厂商不发 index 时它还会塌成 0
(#1283)。拿它配对会配到错的那个工具,或者让两次调用撞同一个 id。

两条路径的 `id` 公式(实现为准):

| 路径 | `id` |
| --- | --- |
| 实时 / 单 run 回放(`ItemStreamConverter`) | 助手消息 `{run}:step:{step_count}` · 工具调用 `{run}:call:{call_id}` · 工具结果 `{run}:result:{call_id}` · 计划 `{run}:plan` · 审批 `{run}:approval:{request_id}`;键取不到时退回位置号 `{run}:{seq}:{消息下标}[:{调用下标}]` |
| 会话历史(`derive_run_items` 直接给) | `{run_id}:{n}`,`n` 是条目在**这一轮推导结果**里的 0 基下标 |

跨路径 `id` 不承诺一致,上表正是它的样子。这不成问题:历史不返回活跃的那一轮,
同一个 run 不会同时出现在两边,两套编号永远不落进同一个列表。

### 已知会咬人的两条

* **`token.step` 在瞬时重试后会重复** —— 重试以 `graph_input=None` 从 checkpoint 重入,
  重跑的 agent 节点再次产出同一个 `step_count + 1`。`(step, channel)` 作键会撞。
* **live 接合会重放陈旧 token** —— 订阅时不传 `last_event_id`,bridge 从缓冲区最早
  一条开始重放;带 seq 的帧被去重挡掉,但 token 帧 `seq is None`,无条件放行(仓库
  里有测试正面确认这个行为)。legacy 下只是多看到几段陈旧打字机文本,items 下会给
  一个**已经 done 的条目重开 `item.added`**。

**已定 —— 两条用同一个机制挡住(PR3)。** `ItemStreamConverter._done_steps` 记下
「权威 `updates` 帧已经到过的 step」,该 step 之后再来的 token 一律丢弃:陈旧重放
与重试撞号的判据是同一句话。重试那次的权威帧仍会在新的 seq 上到达,以同一个 `id`
upsert 出最终文本,内容不会丢。测试:`test_run_event_stream_items.py::
test_live_attach_drops_stale_tokens_for_settled_steps`(摆的是真实接合形态 —— 补库
先发完权威帧,再挂实时流重放缓冲区)。

## 六、接口

### 6.1 会话历史(新增)

```
GET /v1/agents/{agent_code}/sessions/{session_id}/items
    ?user_id=<必填>
    &limit=5           轮数,1-20,默认 5
    &before=<run_id>   往更早翻
```

权限 `require("session", "read")`,与 `/messages`、`/events` 同档。无新暴露面 ——
同样的数据单 run 回放本来就能拉,这只是聚合加重整形态。

```json
{
  "success": true,
  "data": {
    "items": [ ... ],
    "runs": [{"run_id": "r1", "status": "success", "created_at": "...", "duration_ms": 8421, "error": null}],
    "has_more": true,
    "first_run_id": "r1",
    "active_run_id": null
  },
  "error": null
}
```

**分页单位是轮。** OpenAI 与 Dify 按条目分页,但他们有 message 表、有真 id;本平台没有
条目表,造一张要新表加写入路径加迁移。run 是一等实体,有表有 id,天然能做 keyset 游标。
用户上滑要的也是「更早的几轮对话」,按条目切会切在一轮中间。

`items` 按时间正序,客户端整页 prepend。

`runs` 单列一个数组承载轮级信息(失败与否、耗时),不往每个条目里塞。

**活跃的那一轮不进 `items`**,只给 `active_run_id`,客户端拿它走实时接口,否则与实时
流重复。

### 6.2 修正 —— `stream_format` 要加在**四处**,不是两处

初稿只列了两处,漏掉的两条同样是第三方 API key 直接消费的对外 SSE 流。漏任一处,
第三方的列表里就会出现「前面是 items、这段退回 legacy」,正是本节想避免的症状。

| 入口 | 链路 |
| --- | --- |
| `POST /v1/agents/{code}/runs` | → `spawn_run` → `sse_consumer` |
| `GET /v1/agents/{code}/runs/{run_id}/events` | → `build_events_response` → `build_event_producer` |
| **审批续跑** `POST /v1/agents/{code}/runs/{run_id}:decide` | → `sse_consumer`(`external_approvals.py`) |
| **幂等重放** | `Idempotency-Key` 命中 → `_idempotent_run_response` → `build_events_response` |

取值 `legacy`(默认)/ `items`。默认保持 legacy,已在对接的第三方零感知;`spawn_run`
与 `build_event_producer` 各有一个控制台调用点,默认值必须让它们不受影响。

两条要写进文档的副作用:

* `ExternalRunRequest` 是 `extra="forbid"`,**不加这个字段,第三方传 `stream_format`
  会直接 422**。
* 幂等指纹是整个请求体的哈希,所以同一个 `Idempotency-Key` 只改 `stream_format` 会
  拿到 `IDEMPOTENCY_KEY_REUSED` 422。这大概是想要的行为,但必须说清。

## 七、第三方的完整流程

```
点开历史会话
  → GET /sessions/{id}/items                                      最近 5 轮,渲染
  → active_run_id 非空
      → GET /runs/{id}/events?stream_format=items&since_seq=0     接上进行中那轮
用户上滑
  → GET /sessions/{id}/items?before=<first_run_id>                prepend
用户继续说话
  → POST /runs {session_id, stream_format: "items"}               追加到同一个列表
```

一个 reducer,从头到尾。

## 八、边界

* **没盖 `run_id` 戳的老消息**归不到轮,不返回。生产未上线,存量只在测试环境,文档写明。
* **上下文压缩丢弃过中段**的会话,那几轮 checkpoint 消息没了,只剩 legacy 帧能给的部分。
  返回不完整而不是报错。
* **一条 AIMessage 带多个 `tool_calls`** 时,每个 `tool_call` 一条 item,`id` 带子序号。
* **修正 —— ToolMessage 不盖戳。** `stamp_messages` 全仓只有四个调用点,盖的是 agent
  节点的 AIMessage 与入口的 HumanMessage,ToolMessage 没有。所以 `tool_result` 的
  `created_at` 只能为 `null`,`run_id` 从所属轮继承。
* **修正 —— `plan` / `error` 条目的时刻不在 payload 里。** 两种帧的 `data` 都不含时刻,
  时刻只在 SSE 的 `id:` 前缀上。调用侧要把帧的落库时间一起传进推导函数,否则这两种
  条目的 `created_at` 只能是 `null`。

## 九、兼容

现有 13 个事件一个不动,`/messages` 保留(纯文本记录、搜索、导出用途)。文档把 items
作为主路径,legacy 降级成兼容说明并标注后续废弃。

## 十、PR 切分

| PR | 内容 | 依赖 |
| --- | --- | --- |
| PR1 | 条目模型 + 核心推导纯函数 + channel 判定抽到 common + 单测 | — |
| PR2 | 会话历史接口 + `RunEventStore.list` 加 `event_names` 过滤 + 集成测 + `query.md` | PR1 |
| PR3 | 四处 `stream_format` + 消费端转换 + 事件名词表闸 + `sse-events.md` / `chat.md` / `run-control.md` | PR1 |
| PR4 | 同源黄金测试 | PR2 + PR3 |
| PR5 | 文档收敛 + `tool_call.worker` 历史回填(PR2 / PR3 之间漏做的一块) | PR2 + PR3 |

波次:PR1 → (PR2 ∥ PR3) → (PR4 ∥ PR5)

文件归属(并行无冲突):

* PR2 —— `api/external_session_items.py`(新)、`runs/event_store.py`、`guide/query.md`
* PR3 —— `orchestrator/sse.py`、`api/_run_event_stream.py`、`api/external_events.py`、
  `api/agents.py`、`api/external_approvals.py`、`guide/sse-events.md`、`guide/chat.md`、
  `guide/run-control.md`
* PR5 —— `guide/quickstart.md`、`guide/examples.md`、`guide/best-practices.md`、
  `.vitepress/config.mts`、`docs/api/streaming-events.md`

**PR5 实际还动了四处**(PR2 / PR3 收工后才发现的缺口,与它们已合入 main 不冲突):
`api/external_session_items.py`(worker 回填)、`common/conversation_derive.py`(一句
错的 docstring)、`guide/query.md`(补 `tool_call.worker` 字段)、`guide/sse-events.md`
(3.4 补深度大于 1 的挂载规则 —— 原先只写了 `parent_tool_call_id`,客户端照着写会把
孙子任务挂丢)。

**修正 —— `run-control.md` 归 PR3**。整个审批流程建立在 `approval` 事件上,共六处
依赖 legacy 帧,与 approval item 强相关,不能留到收尾 PR。

`quickstart.md` 的「读懂返回的事件流」一节同样要改:它的示例直接是 `metadata` /
`updates` / `end` 三帧,还写了「`end`、`token` 这类事件没有 `id:` 行」——items 模式下
`item.delta` 同样没有。

`docs/api/streaming-events.md` 是一份 12 项的老列表,**今天已经与文档站不一致**,顺手对齐。

各文档提及事件流的密度(排期参考):`sse-events.md` 106 / `examples.md` 20 /
`run-control.md` 15 / `chat.md` 11 / `quickstart.md` 10 / `query.md` 9 /
`best-practices.md` 4。`best-practices.md` 的联调自测清单引用了 5.3,要改指新接口。

## 十一、风险

**同源漂移**是最大的一个,黄金测试为它而设 —— 但测试本身必须跑多步带工具的 run,
见 §四。

**PR3 动 `sse.py` 的热路径**,legacy 模式必须逐帧证明没变。回归网按密度是
`test_run_event_stream.py`(22)、`test_sse_persistence.py`(15)、`test_runs_api.py`(15)、
`test_sse_plan_events.py`(11)、`test_streaming_redact.py`(10)、`test_sse.py`(10,含
**字节级** `format_sse` 断言)、`test_external_events.py`(4)。

**修正 —— 全仓没有任何测试断言对外事件名的全集。** 名字最像的那个对外契约测试只有
一条 SSE 断言(`"event: end" in body`),不校验事件名集合。而 items 模式下有六个事件
要原样透传,写漏一个 = 静默丢帧且不会红。PR3 必须补一个词表闸,照 end-status 词表
那条现成套路(AST 扫 `sse.py` 里的字面量事件名 → 对齐常量表)。

**已补(PR3)**:`services/orchestrator/tests/test_stream_items_vocabulary.py`。扫描器
解析不了的表达式一律当场报错、不静默跳过 —— 「扫不到就当没有」的扫描器会让整道闸
空转。写对外文档时以那里的 `ITEMS_WIRE_EVENTS` 断言(11 个字面量)为准去数。

**修正 —— `_encode` 从无状态变有状态。** 现有 docstring 论证的是「无状态过滤不打乱
游标」,那段推理照抄不能用来给转换器背书:新增风险不在游标上,而在转换器自己的状态
被乱序 / 重放 / 跨页三条路径污染。让 id 确定性派生就是为了把这部分状态消掉。

条目推导依赖 `run_id` 戳,戳的写入路径出问题会**静默少内容**而非报错,测试要专门覆盖。

## 十二、文档写作

对外文档一律先读 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`
再动手。读者是第三方开发工程师,不是内部人。

## 十三、follow-up

前端 `untrusted_clean.ts` 的 `cleanUntrusted` 现在是跨语言的第二份还原实现。PR3 上线
后前端可以直接吃已还原的 `tool_result.content`,那份 TS 可以退役。
