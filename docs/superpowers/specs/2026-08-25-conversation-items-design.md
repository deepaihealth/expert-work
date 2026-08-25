# 对话条目模型(conversation items)设计

2026-08-25 · 对外 API v1

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
| `created_at` | string | ISO8601 |

七种类型:

| type | 专有字段 | 来源 |
| --- | --- | --- |
| `user_message` | `content`, `attachments` | checkpoint 的 HumanMessage |
| `assistant_message` | `content`, `channel`(`final` / `commentary`) | checkpoint 的 AIMessage |
| `tool_call` | `call_id`, `name`, `args`, `worker` | AIMessage 的 `tool_calls[]`,每个一条 |
| `tool_result` | `call_id`, `name`, `status`, `content`, `artifact` | checkpoint 的 ToolMessage |
| `plan` | `goal`, `steps[]` | 该轮 `plan` 帧,取最后一个 |
| `approval` | `status`, `tool`, `args` | 该轮 `approval` 帧 |
| `error` | `message` | 该轮 `error` 帧 |

`channel` 沿用 `transcript.extract_turns` 的既有判定,不新造语义。

`tool_result.content` 给**还原后**的文本 —— 现在文档写着「带有防注入包装,直接显示是
乱码」,把内部表示翻译成产品表示正是这一层的价值。

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

**核心推导只有一个函数**,输入是消息列表加辅助信号,输出是 items。三条路径都能提供
这个输入 —— `updates` 帧里裹着的就是 messages。

### 转换发生在消费端

`_publish_frame`(`orchestrator/sse.py`)是所有持久帧的唯一收口,事件库是**所有连接
共享的一份**。而 `stream_format` 是每条连接的选择。因此:

* 事件库永远只存 legacy 帧。不双写、不迁移。
* 回放在读取时转换(`_run_event_stream.build_event_producer`)。
* 实时在转发时转换(`sse.sse_consumer`)。

同一个转换器服务两条路径,实时与回放自动同源。

### 黄金测试

跑一个真 run,收集实时产出的 items,再从单 run 回放与会话历史两条路径分别重建,
断言三者相等(`id` 除外)。没有这个测试,同源只是口头承诺。

## 五、实时:条目生命周期事件

| 事件 | 时机 | data |
| --- | --- | --- |
| `item.added` | 条目出现,内容可能为空 | 完整 item,字段可能不全 |
| `item.delta` | 文本逐字产出 | `{id, field, text}`,`field` ∈ `content` / `reasoning` / `args` |
| `item.done` | 条目完成 | 完整 item,字段齐全 |

`item.delta` 覆盖今天 `token` 的能力,`item.done` 覆盖 `updates` 的能力。

items 模式下的事件集(9 个):

* 内容 —— `item.added` / `item.delta` / `item.done`
* 流控 —— `metadata` / `end` / `gap` / `truncated`
* 过程提示(可忽略)—— `guard` / `compaction` / `retry`

不再发 `token` / `updates` / `plan` / `approval` / `error`:前两个被条目生命周期取代,
后三个变成 item。

`worker` 是唯一不完全同构处:子任务进度在历史里是 `tool_call` 的 `worker` 字段,实时
仍是独立事件。这是平台特有的高级功能,文档诚实标注。

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

### 6.2 `stream_format` 参数(两处)

* `POST /v1/agents/{agent_code}/runs`
* `GET /v1/agents/{agent_code}/runs/{run_id}/events`

取值 `legacy`(默认)/ `items`。默认保持 legacy,已在对接的第三方零感知。

第二处容易漏但不能漏:用户在对话进行中刷新页面,靠它接上那一轮,这时必须也能拿 items,
否则列表里前面是 items、当前轮是 legacy。

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
* **一条 AIMessage 带多个 `tool_calls`** 时,每个 `tool_call` 一条 item,`id` 需带子序号。

## 九、兼容

现有 13 个事件一个不动,`/messages` 保留(纯文本记录、搜索、导出用途)。文档把 items
作为主路径,legacy 降级成兼容说明并标注后续废弃。

## 十、PR 切分

| PR | 内容 | 依赖 |
| --- | --- | --- |
| PR1 | 条目模型 + 核心推导纯函数 + 单测。零对外变化 | — |
| PR2 | 会话历史接口 + `RunEventStore.list` 加 `event_names` 过滤 + 集成测 + `query.md` | PR1 |
| PR3 | 实时与回放的 items 模式(两端点 `stream_format`)+ `sse-events.md` + `chat.md` | PR1 |
| PR4 | 同源黄金测试 | PR2 + PR3 |
| PR5 | 文档收敛:`quickstart.md` / `examples.md` / `best-practices.md` / 侧边栏 / 锚点扫尾 | PR2 + PR3 |

波次:PR1 → (PR2 ∥ PR3 ∥ PR5 起草) → (PR4 ∥ PR5 定稿)

文件归属(并行无冲突):

* PR2 —— `api/external_session_items.py`(新)、`runs/event_store.py`、`guide/query.md`
* PR3 —— `orchestrator/sse.py`、`api/_run_event_stream.py`、`api/external_events.py`、
  `api/agents.py`、`guide/sse-events.md`、`guide/chat.md`
* PR5 —— `guide/quickstart.md`、`guide/examples.md`、`guide/best-practices.md`、
  `.vitepress/config.mts`

## 十一、风险

**同源漂移**是最大的一个,黄金测试为它而设。

**PR3 动 `sse.py` 的热路径**,legacy 模式必须逐帧证明没变 —— 用现有对外契约测试当回归网。

条目推导依赖 `run_id` 戳,戳的写入路径出问题会**静默少内容**而非报错,测试要专门覆盖。

## 十二、文档写作

对外文档一律先读 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`
再动手。读者是第三方开发工程师,不是内部人。
