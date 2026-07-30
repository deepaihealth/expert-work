# 执行轨迹 Gantt 时间线设计

> 2026-07-31。独立 PR(纯前端)。触发:用户提出多分支/多 subagent/并发的
> run 用竖列表看不出并行关系,提议 graph 展示;调研(Langfuse Agent
> Graphs 2025-11 GA/Arize Phoenix Agent Graph/Openlayer Gantt/Braintrust
> timeline)定案三线:短线=W2-PR3 部署新版 Langfuse 拿原生 graph;
> **中线=本 PR,调试台执行轨迹升级 Gantt 时间线**;远线=内嵌自渲染
> graph(等原生图验证需求后再议)。交互原型经用户确认:
> 嵌入/放大两档 + 标签列 tooltip。

## 问题

调试台「执行轨迹」视图是竖列表:并发执行的工具、并行委托的 worker 被
排成先后;找慢步要逐行读时长数字。行业(Openlayer/Phoenix/Braintrust)
的 trace timeline/Gantt 形态:行=执行单元、横条=起止时长,并发重叠
一眼可见。

## 数据地基(侦察结论:后端零改动)

1. **服务端毫秒时戳已在 SSE `id` 里,live/replay 同构**:
   - live:stream_bridge/memory.py `_next_id` → `"{ms}-{seq}"`;
   - replay:runs.py `_stream_replay` → `"{created_at_ms}-{seq}"`
     (run_event 表 `created_at_ms` 列)。
   - 前端 `SseEvent.id` 已解析保存。**历史轮 replay 的 `receivedAt`
     全挤在重放瞬间,绝对时间必须取 `id` 的 ms 段**,不用 receivedAt。
   - 契约钉住:未来 redis StreamBridge 实现(factory.py M1+ 占位)必须
     保持 `"{server_ms}-{seq}"` id 形态,否则 Gantt 绝对时间轴失真。
2. **时长现成**:agent 步/辅助节点 `durationMs`(timeline.ts,4a 地基)、
   工具 `durationMs`(tool_timeline.ts)、worker `_duration_ms`
   (worker_timeline.ts)。
3. **委托树现成**:worker 帧 `parentToolCallId` 挂到发起工具。
4. 帧时刻语义:updates/tool 帧在动作**完成后**入队 → `id_ms ≈ 结束时刻`,
   `start = id_ms − durationMs`。

## 设计

### 形态(原型已确认)

「执行轨迹」视图从竖列表(StepTimeline)升级为 Gantt:

- **行 = 执行单元**,按开始时刻排序,层级缩进:LLM 步骤(顶层)/
  辅助节点(记忆召回、planner、reflect、writeback,顶层)/工具
  (缩进 1,`└` 前缀)/委托 worker 内部步骤(缩进 2,挂在 sub_agent
  工具行下)。marker 类(compaction/retry/error/approval/guard/end)
  不占行,渲染为时间轴上的竖线刻度 + tooltip(无时长的瞬时事件)。
- **横轴 = 时间**:0 = 首事件时刻;刻度自适应(总长 <10s 取 1s 格,
  <60s 取 10s 格,否则 30s 格)。条形色按语义:LLM 步=info 蓝、辅助
  节点=紫、工具=成功绿、worker=橙、终结步(channel=final 语义,与
  #1072 词汇打通)=亮绿。全部走既有 `--ew-*` 语义色令牌(#979 双主题),
  不写死色值。
- **两档展示**:
  - 嵌入态(TurnCard 内,右栏 ≈1100px):标签列 176px,模型名收起,
    名字截断 + **antd Tooltip 显全名**(用户点名),条形时长标签
    默认隐藏、悬停该行显示;时间区吃满剩余宽,窄于 560px 时容器内
    横向滚动。
  - 放大态:卡片头部放大按钮 → **92vw Modal**(FullTextModal 尺寸
    先例),标签列 292px + 模型名 + 时长常显。同一组件,`variant`
    prop 切换。
- **点击行展开详情**:行下展开该单元的现有详情内容(复用 StepTimeline
  的单卡渲染,提取为可复用单元后两视图共用),再点收起;一次一行。
- **流式**:run 进行中,已完结单元照常渲染;进行中的 LLM 步(收到
  step 帧前)渲染「生长条」——从上一事件结束时刻起到当前时刻,半透明
  + 动画,每秒 tick 重算(仅 running 时挂 interval)。live 场景当前
  时刻用客户端 now 与最近帧 `id_ms` 的偏差校准(now − lastFrameAge)。
- 不引图表库:纯 div 绝对定位 + CSS(原型即此实现)。
- 尊重 `prefers-reduced-motion`(生长动画/入场动画禁用)。

### 数据层

新 `api/gantt_timeline.ts`:`buildGanttRows(events: readonly SseEvent[]): GanttModel`

```ts
export interface GanttRow {
  key: string;
  label: string;            // 行名(工具名+摘要 / 步骤 N / 节点名)
  model?: string;           // LLM 步的模型名(嵌入态收起)
  kind: "agent" | "aux" | "tool" | "worker" | "final";
  depth: 0 | 1 | 2;
  startMs: number;          // 相对首事件,毫秒
  durationMs: number | null; // null = 进行中(生长条)
  detailRef: TimelineItem | ToolActivity | WorkerStepSummary; // 详情展开的数据源
}
export interface GanttMarker {
  atMs: number;
  kind: "compaction" | "retry" | "error" | "approval" | "guard" | "end";
  text: string;
}
export interface GanttModel {
  rows: GanttRow[];
  markers: GanttMarker[];
  totalMs: number;          // 轴长(含进行中推算)
  t0Ms: number;             // 首事件服务端 ms(绝对)
}
```

复用现有解析器(timeline.ts / tool_timeline.ts / worker_timeline.ts)
产出的结构拼装,不重复解析帧;`id` 的 ms 段解析容错(id 为 null 或
非法 → 该行退化为按序拼接:上一行结束时刻起、无重叠,保持可用)。

### 覆盖面

TurnCard 的 eventView === "timeline" 分支换用 GanttTimeline 组件——
调试台 + 对话详情页(共享 TurnCard)自动双页生效。「工具调用」「原始
事件」「精确」三视图不动;exact 视图(Langfuse 瀑布)保留作为跨 run
span 级精确兜底。

### 不做(YAGNI)

- 不做自渲染 graph(远线);不动 exact 视图;不引图表库/虚拟滚动
  (单 turn 行数几十的量级,直接渲染)。
- 不做时间轴缩放/拖拽(放大态已覆盖阅读需求;真需求出现再加)。
- token 流帧不入 Gantt(provisional,权威 updates 帧覆盖)。
- 后端不改(时戳契约现成;redis bridge 同构要求已在本 spec 钉住,
  实现时那侧对照)。

## 测试

- `gantt_timeline` 单测:start=end−duration 推导 / id 缺失退化拼接 /
  并发工具重叠(两行 start 相同)/ worker 挂树缩进 / marker 提取 /
  进行中行 durationMs=null / totalMs 推算。
- 组件测:嵌入态标签列 Tooltip 存在 + 时长悬停显示;放大态 Modal 打开
  (92vw)+ 全信息;点击行展开详情、再点收起;reduced-motion 下无动画
  class;final 行用 final 色 class。
- 回归:TurnCard 其余三视图不受影响;全量 vitest + tsc。
