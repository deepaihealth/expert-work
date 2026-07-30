# 对话输出语义频道(conversation output channels)设计

> 2026-07-30。独立 PR,不属于阿里云部署波次。触发:测试环境验收发现
> 过场话(「第一章的资料已获取,现在撰写第一章正文。」)混进对话正文
> (UX 反馈 #8 的 join 修法把一轮里所有 assistant 文本拼成一篇答案)。

## 问题

一个多步 turn 里,LLM 会产出多条 assistant 消息:与 `tool_calls` 同帧的
过场旁白(「现在搜索第二章资料」)和终结正文。当前两端消费者都无法区分:

- **调试台** `TurnCard` 把全部文本 `join("\n\n")` 成一篇「答案」——旁白
  变成正文里的错句;
- **第三方 API**(`GET /v1/sessions/{thread_id}/messages`)返回平面
  `[{role, content}]`,17 条旁白被渲染成 17 条 assistant 消息,终端
  没有任何信号知道哪条是正文。

## 调研结论(方案依据)

三路调研 + 五家国产模型实测 + 本地 deer-flow / hermes-agent 源码验证:

1. **业界共识 = 换频道不打语义猜测**。OpenAI Responses API 的
   `phase: commentary|final_answer`、Harmony 的
   `channel: analysis/commentary/final`、A2A 的 Artifact vs Message、
   deer-flow 的 assistantText 过程步 vs 组尾终结消息、hermes 的
   interim callback vs `final_response`——全部是**结构位置**分频道,
   没有一家从文本内容猜语义。
2. **五家国产模型实测**(glm/kimi/deepseek/qwen/doubao,存档
   `scratchpad/vendor-shape-probe-2026-07-30.md`):`tool_calls` 与
   content 同帧 10/10;`reasoning_content` 与 content 分离 100% 可靠
   (providers/openai.py 已归一化);content 内部旁白/正文形态五家互斥
   ——**任何基于长度/位置/顺序的文本推导都会翻车,不做**。
3. **hermes 同款坑的注释原话**:工具还挂着时 top-level content 可能
   同时含 commentary 和答案,把它当 progress 会提前泄漏答案——所以
   「与 tool_calls 同帧的 content」必须整体归 commentary,哪怕里面
   粘了正文(glm 线上 run 实锤此形态)。deer-flow 的取舍相同:该消息
   以过程步样式**完整可见**但不进正文区。
4. **final 不能在产出时刻钉死**(A2A 因此删掉 final 标记):本仓
   `_should_continue` 无 tool_calls → END,但 reflect 开启时
   `_after_reflect` 可把 agent 打回再跑——「无 tool_calls」消息可能
   被追加的新消息取代。final 只能在**完结后按结构位置**判定。

## 设计

### 频道词汇(全栈单一规则,零启发式)

对一个 user 消息切出的段(segment)内的 assistant 消息:

| 频道 | 判定(纯结构,无文本推导) | 载体 |
|---|---|---|
| `analysis` | `additional_kwargs.reasoning_content`(厂商级分离) | 已有,不改 |
| `commentary` | 段内**非末条** assistant 消息,或末条但 `tool_calls` 非空 | 新标注 |
| `final` | 段内**末条** assistant 消息且 `tool_calls` 为空 | 新标注 |

推论:approval 暂停轮 / 末步失败轮(末条带 tool_calls)→ 该段无
`final`;reflect 打回后旧的候选答案自动变 `commentary`(它不再是末条)。
与 token 流帧已有的 `channel: content|reasoning|tool_args` 不冲突——
那是**传输层**词汇(per-token),本设计是**消息层**词汇(per-message),
两层各自封闭。

### 后端(第三方 API)

1. `MessageTurn`(persistence/thread_message/base.py)加
   `channel: str | None = None`(assistant 专属;user 恒 None)。带默认
   值,mirror sweep(`sync_thread`)与 `quality_monitor_worker` 零改动,
   `thread_message` 表不加列(内容搜索用不到)。
2. `read_turns`(control_plane/transcript.py)按上表打 channel:先按
   role=user 切段,段内应用末条规则。`tool_calls` 从 checkpoint 的
   AIMessage 对象直接读(结构事实,非新数据)。
3. `GET /v1/sessions/{thread_id}/messages` 响应行加 `"channel"` 字段
   (user 行为 `null`)。加字段=向后兼容,老客户端不受影响。
4. **SSE 帧零改动**。`updates` 帧本来就带每条 AI 消息的完整序列化
   (含 `tool_calls`),实时消费者用同一条规则自行推导——规则写进
   API 文档。不在流式帧里打 `final` 标(产出时刻不可知,见调研结论 4)。

### 顺带修:reflect feedback 污染对话历史

`reflect.py` revise 路径注入的 `[Reflection] …` HumanMessage 没标
`expert_work_hide_from_ui` → `/messages` 里出现假 user 消息:第三方看到
一条用户没说过的话;`buildHistoryTurns` 的 order-pairing 因 user 数 ≠
runs 数而整体降级。修法:该 HumanMessage 加
`additional_kwargs={"expert_work_hide_from_ui": True}`。模型 in-prompt
仍然看到它(hide 只作用于 UI 投影),faithful 路径(mirror/审计)不变
——与 RT-ADR-9 语义一致。同时它也是 channel 分段正确性的前提(假 user
消息会把一个 turn 错切成两段)。

### 前端(调试台,#8 返工)

1. `turn_summary.ts`:`assistantTexts: string[]` 替换为
   `segments: { text: string; channel: "commentary" | "final" }[]`
   (按到达序;channel 用与后端相同的末条规则,数据源 = updates 帧里
   现成的 `tool_calls` 字段)。`finalText` 语义收紧为 final 段文本
   (无 final 段 → null;null 的「找审批闸」信号角色保留且更准——
   暂停轮末条必带 tool_calls)。`reasoning` 不变。
2. `TurnCard` 答案区序列渲染(deer-flow 的分区形态):
   - `commentary` 段:弱化行——`Text type="secondary"` 小字号 +
     MessageSquareText 图标,pre-wrap,超 240 字符截断 + 复用
     `FullTextTrigger`(StepTimeline 同款惯例)。**完整可见,不删除**
     (glm 粘帧里的正文用户仍能读到)。
   - `final` 段:`MarkdownView` 正文(现状渲染器)。
   - 全 commentary 无 final(失败/暂停轮):渲染 commentary 序列,
     状态语义由现有 error Alert / approval 闸承担,不加新占位。
   - 流式期间:已 settle 段照 channel 渲染;正在流的文本仍走现有
     pre-wrap 打字机路径(token 流与步卡不动)。
   - 「查看全文」弹窗:全序列拼接文本(阅读/复制场景,保持一份全文)。
3. `history_turns.ts` 的 fallback 路径(/messages order-mismatch 时的
   降级视图)**顺带受益**:服务端已算好 channel,`fallbackAnswer` 从
   join 后的单字符串改为携带 channel 的行数组
   `{ text: string; channel: "commentary" | "final" | null }[]`,渲染
   复用 TurnCard 的段样式(commentary 弱化 / final 正文)。
4. 对话详情页(ConversationDetail)经 components/turn 家族自动同步,
   无独立改动。

### 不做(YAGNI / 已判死)

- 不做任何基于文本内容的 commentary/final 推导(实测判死)。
- 不改 token 流帧格式、不动 useTokenStream(传输层已封闭)。
- 不在 SSE 流式帧打 final 标(A2A 教训)。
- 不给 `/messages` 加 reasoning 暴露(第三方场景未出现,后补不破坏)。
- 不动 `thread_message` mirror 表结构。
- 不加提示词层约束(deer-flow 有「思考里别写全文」提示词;我们后续
  观察,不进本 PR)。

## 测试

- `read_turns` 单测:混合序列(commentary→final / reflect 打回 /
  末条带 tool_calls 无 final / hide 过滤后分段)。
- `/messages` 契约测:channel 字段形状 + user 行 null。
- reflect 单测:revise feedback 带 hide 标记。
- `turn_summary` 单测:五家实测形态 fixture(glm 同帧粘正文 /
  deepseek 先正文后空 / qwen 全空 content)。
- `TurnCard` 渲染测:commentary 弱化 + final markdown + 无 final 轮 +
  流式中间态。
