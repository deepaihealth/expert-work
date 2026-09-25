# Agent 思考模式开关(thinking toggle)

## 背景

Agent 配置界面无思考模式控件。当前 Expert Work 默认**不发**任何思考控制字段
(`_thinking_payload` 在 `effort=None && adaptive_thinking=False` 时返 `None`)→ 走
各厂商自身默认(Claude/doubao-seed/qwen3/glm 多数默认开,Haiku 不思考,
deepseek-reasoner/kimi-k2-thinking 恒思考)。`ModelSpec` 只有 `effort`
(low/med/high/max|None)+ `adaptive_thinking`,**无简单 on/off**,也**无"显式
关思考"报文**。

## 目标(owner 拍板)

1. 配置界面选模型后出现"思考模式"开关,初值 = **该模型的真实默认**;模型不支持
   运行期思考旋钮则不渲开关。
2. 关闭后真生效(各厂商显式 disable 报文)。无法完全关闭的厂商(OpenAI 等
   `reasoning_effort` 系)→ 降最低档 `minimal` + UI 提示。
3. 即使用户关了思考,**错误重试时仍升思考**(模型支持时),且**仅本次对话临时
   升级,不改 Agent 持久配置**。
4. 开关覆盖**主对话模型 + VL 视觉模型**,各自独立(fallback 不给)。

## 决策

- **D1 catalog 标真实默认**:`ModelEntry` 加 `thinking_default: bool = False`,按各
  厂商真实默认逐模型填(仅 `thinking != None` 时有意义)。开关初值精确。
- **D2 ModelSpec 加三态 `thinking_enabled: bool | None = None`**:
  - `None` = **继承**(=今天行为:`effort`/`adaptive_thinking` 驱动,都没设则厂商
    默认)。旧 manifest 字节不变,YAML 高级用户仍可只用 `effort`。
  - `True` = **强制开**(发 enable 报文/沿用 effort 深度)。
  - `False` = **强制关**(发 disable 报文;不能全关的厂商降 `minimal`)。
  UI 开关只写 `True`/`False`(选模型时按 `thinking_default` 落显式值);`None` 只
  留给历史 manifest + 纯 YAML。
- **D3 can-disable 推导,不加字段**:`不能完全关闭 ⟺ thinking=="effort" 且
  provider != "anthropic"`(OpenAI/Azure/DeepSeek 的 `reasoning_effort` 无 off 档)。
  前后端都能从 `(thinking, provider)` 算,无需 catalog 新列。
- **D4 开关挂 `ModelSelect` 复用**:主模型 + VL 都用该 widget → 加一处两边都有
  (decision #4)。

## 各厂商 thinking 报文(impl 期对厂商文档复核)

| shape | provider | enable(True / 沿用) | disable(False) |
|---|---|---|---|
| effort | anthropic | `output_config.effort`(有 effort)/ 默认动态(无)| `thinking:{type:"disabled"}` |
| effort | openai/azure/deepseek | `reasoning_effort:<effort>` / 省略(默认开)| `reasoning_effort:"minimal"`(**不能全关**)|
| budget | qwen | `enable_thinking:true`(+`thinking_budget`)| `enable_thinking:false` |
| effort(B-105 起) | doubao | `reasoning_effort` / 无档位时 `thinking:{type:"enabled"}`(seed-2.1 拒 `auto`) | `thinking:{type:"disabled"}` |
| toggle | glm/kimi | `thinking:{type:"enabled"}` | `thinking:{type:"disabled"}` |
| None | haiku/embeddings/恒思考 | — 无开关 — | — |

## 重试升思考(req #3)

`_escalated_model(model)` 造高一档 ModelSpec → build 期编译成独立的
`escalated_llm_caller`,运行期 `escalate_next` 标志临时换**一轮**(`builder.py:604`),
**不落 manifest**(已临时,天然满足 #3)。改动:当 `thinking_enabled == False` 时,
升级副本设 `thinking_enabled=True` + effort 升一档(toggle 厂商→`high`,effort/budget
→ 从当前 effort 沿 `_EFFORT_LADDER` 走,`None→medium`),即"关→开"。catalog
`thinking==None` 仍不可升。

## 构建期闸

`thinking_enabled is not None` 但 catalog 该模型 `thinking is None`(且 entry 在册)
→ `AgentFactoryError`(fail-fast,镜像现有 `effort` 闸)。off-catalog(entry None)
不闸,透传。

## 前端

- `model_catalog.ts` `CatalogModel` 加 `thinking: "effort"|"budget"|"toggle"|null` +
  `thinking_default: boolean`(端点 `model_dump(mode="json")` 已序列化,仅补类型)。
- `form_model.ts` `ModelFields` 加 `thinking_enabled?: boolean`(index 签名 +
  `readModel`/`writeModel` spread → 主/VL 自动 round-trip)。
- `ModelSelect.tsx`:选模型时若 `entry.thinking != null` → seed
  `thinking_enabled = entry.thinking_default`,否则清掉(`undefined`,manifest 干净);
  `thinking != null` 渲 antd `Switch`("思考模式");`effort && provider!="anthropic"`
  时附提示"该模型仅能最小化思考,无法完全关闭"。
- i18n 三写:`model_select.thinking_label / thinking_cannot_disable`。

## 测试

- 后端单测:catalog `thinking_default` 在册;`_thinking_payload` 对 None/True/False ×
  各 shape;`_escalated_model` 在 `thinking_enabled=False` 时强制开;构建闸触发;
  Anthropic provider disable 报文。
- 前端 vitest:`ModelSelect` 思考模型渲开关 / Haiku·embedding 不渲 / 选模型 seed 默认 /
  effort-非anthropic 提示 / 非思考模型清 `thinking_enabled`;`form_model` round-trip。
- Live(verify_live):各 provider 关→真不思考(响应无 reasoning)/ 开→思考;关 +
  触发重试 → 该轮升思考且后续轮回落关。

## 不做(backlog)

UI 暴露 effort 深度档(仍 YAML-only)/ fallback 模型开关 / per-route 模型开关。
