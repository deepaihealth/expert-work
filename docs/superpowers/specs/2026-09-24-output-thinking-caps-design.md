# B-105 输出上限 / 思考上限 —— 设计

- 日期：2026-09-24
- 状态：**已拍板（2026-09-24，用户「ok」，§7 五条按推荐）**，待 T0 真调回填 §8
- 前置：B-64 / B-102~104 已合；调研原文见会话 scratchpad `vendor-caps-research.md`（搜索摘录，未逐页核对 → 本设计把「真调核实」放在实现之前）

## 1. 问题（现象，不是归因）

1. **配置页的「输出上限」只对 Anthropic 生效。** 其他 8 家（OpenAI / Azure / 自部署 / Kimi / GLM / DeepSeek / 通义 / 豆包）的请求体里不带任何输出上限（`agent_factory.py` 只在 Anthropic 分支传 `max_tokens`；`openai.py::_build_request_body` 没有长度字段）。配置页提示文案自己也写着「OpenAI 系提供商忽略此值」。
2. **没有独立的「思考长度上限」。** 通义 / 豆包的思考预算是 `max_tokens × 档位比例` 推出来的，用户无法直接设。
3. **豆包的思考预算字段可能根本不存在。** 我们发 `thinking.budget_tokens`，方舟 Chat API 文档里查不到（文档用 `reasoning_effort`）；B-64 真栈看到过一次看图输出 11,753 token。
4. **OpenAI 的档位取值可能不合法。** 关思考时发 `minimal`、effort=max 时原样发 `max`，这两个值都不在 gpt-5.5 的取值表里（厂商的最高档叫 `xhigh`，关闭叫 `none`）。
5. **输出被截断时没人管。** `finish_reason=length` / `stop_reason=max_tokens` 只被记进 metadata，运行时不读 —— 截成空回答或半截工具参数时，run 照常往下走。

## 2. 实测：一旦「输出上限」真生效，谁会被影响

测试环境 2026-09-24，只统计聚合数：

| 项 | 数 |
|---|---|
| 有效 Agent | 10 个；主模型 7 个是 `max_tokens=4096`（全部是 GLM / Kimi，**这个值从没生效过**），3 个是 40960 |
| manifest 里存的值 | 全部是显式数字 —— 表单按默认值写回，**「用户没设」和「用户设了 4096」在库里分不出来** |
| glm-5.3 单行输出 > 4096 | 3,832 行中 215 行（5.6%）；p99 = 11,415；最大 49,682 |
| 输出 > 4096 的行，按 Agent 分 | ai-health-plan 171 行（它配的是 40960，最大 33,522，**不会被截**）；sop2-designer 37 行、test-agent 45 行、sop2-designer-worker 28 行（这几个是 4096，**会被截**） |
| 测试环境能真调的厂商 | GLM、Kimi、DeepSeek、通义、豆包（平台 key 就这 5 家）；**OpenAI / Azure / Anthropic 没有 key** |

注：`token_usage` 从 B-104 起基本是一次调用一行，更早的行可能是整轮合计，所以上面的「> 4096」是上限估计。

**结论：直接把现存的 4096 发出去，sop2-designer 这类 Agent 大约 5% 的调用会被截断。**所以默认值的语义必须先改，不能只是「把字段接通」。

## 3. 目标 / 不做

**目标**
- 平台只有一个「输出上限」概念，含义统一为**思考 + 回答的合计**，对每家都真的生效。
- 通义这种厂商真支持的地方，提供独立的「思考长度上限」；其他厂商在配置页明说「只能调档位」，不静默忽略。
- 截断到答案不可用时，给出可见的错误，并告诉用户去哪里改。
- 新模型接进来，只需要改目录，不需要写新的代码分支（用户要求：不要为豆包定制）。

**不做**
- 截断后自动续写或重试（YAGNI：新默认值是「厂商默认」，已经很大；只有用户自己设小了才会截断）。
- Anthropic 4.6 及更早模型的 `budget_tokens`（厂商已废弃，4.7 起发了直接 400）。
- B-113（stream_deadline_s 被抬到 180）和 B-112（模型名写错）另开票。

## 4. 设计

### 4.0 为什么「输出上限」= 思考 + 回答合计

- 平台层主流就是这个结构：OpenRouter 的 `max_tokens` 是总上限，思考预算必须严格小于它；Claude Code 用 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 管总量、`MAX_THINKING_TOKENS` 管其中的思考；Anthropic API 的 `max_tokens` 同样是合计。
- 厂商层分两派：Anthropic / OpenAI / DeepSeek / Kimi / GLM 只有「合计」字段；通义（`thinking_budget` 单独管思考）和豆包（`max_tokens` 只管回答、`max_completion_tokens` 管合计）可以分开管。
- 选「合计」有两个理由：① 在「合计派」厂商那里，没办法只限制回答长度 —— 思考用得少，回答就会多出来，所以「只限回答」这个承诺平台对一半厂商兑现不了；② 思考 token 按输出计费、同样耗时，用户设上限，通常是想给单次调用的钱和时间封顶。
- 字段怎么落地：先总上限，思考预算是它里面的一块（只有支持的厂商才有这块）。T0 实测：5 家都有一个真正表示「合计」的字段（见 §8），**不需要拼合计**。


### 4.1 目录（`ModelEntry`）新增三个字段

| 字段 | 含义 | 例子 |
|---|---|---|
| `output_cap_field: "max_tokens" \| "max_completion_tokens"` | 这家用哪个字段表示「思考 + 回答合计」的上限 | 实测（§8）：GLM、DeepSeek = `max_tokens`（**发 `max_completion_tokens` 会被静默忽略**）；通义、豆包 = `max_completion_tokens`（它们的 `max_tokens` 只管回答）；kimi-k3 两个都生效，按文档取 `max_completion_tokens`；OpenAI / Azure 按文档取 `max_completion_tokens`（未真调） |
| `max_output_tokens: int \| None` | 厂商公布的输出上限，用来做保存校验和配置页占位提示 | kimi-k3 1,048,576；GLM 128K；豆包 65,536 |
| `thinking_cap: bool` | 是否支持独立的思考长度上限 | 目前只有通义带思考的模型为 True |

档位映射从代码分支挪进目录：新增 `effort_map: dict[平台档位, 厂商取值]`，以及 `effort_off: str | None`（关思考时发什么）。GLM、Kimi、OpenAI、豆包现在散落在 `_thinking_enable_payload` / `_thinking_disable_payload` 里的特例改成读目录；**请求怎么拼（线格式）仍按厂商写在代码里**，档位值则由目录决定。这样以后换模型、加模型，只需要改一行目录。

### 4.2 Manifest（`ModelSpec`）

- `max_tokens: int | None`，**默认从 4096 改为 None**。None 的含义：
  - OpenAI 兼容这几家：请求里**不带**上限，用厂商默认值（和今天的实际行为逐字节一致）。
  - Anthropic（必须带上限）：用目录里的 `default_output_tokens`（仅 Anthropic 条目需要填，取值以厂商文档为准）。
- 显式填了值：按目录里的 `output_cap_field` 发送，而且**只发这一个字段**（豆包两个字段同时发会 400，这条改成结构上不可能发生）。保存时校验不能超过 `max_output_tokens`。
- 新增 `thinking_max_tokens: int | None`：
  - 目录里 `thinking_cap=True` 的模型：直接作为通义的 `thinking_budget` 发出，不再用「`max_tokens` × 比例」去推。
  - 其他模型填了这个字段：**保存时直接拒绝（422）**，错误信息写明「该模型只能调思考档位」。这和现有 `effort` 填在不支持的模型上就构建报错是同一个套路。
  - 没填、但设了档位：通义沿用按比例推算，只是基数从 `max_tokens` 换成 `max_tokens`（若有）或 81,920。
- 目录外的模型 / 自部署：显式填了值就发 `max_tokens`，不填就什么都不发。

### 4.3 存量数据迁移（一次性，alembic 数据迁移）

- 范围：`agent_spec.spec_json` 和 `draft_spec_json`，覆盖所有 ModelSpec 位置（主模型、fallback、`vision.model`、worker 模型）。历史 revision 不动。
- 规则：**非 Anthropic 的模型里，值恰好等于 4096 的删掉**（它是旧默认值，从来没生效过，删掉后行为和今天逐字节一致）；其他显式值原样保留，从此开始真的生效。迁移结束时按「厂商 / 值」打印聚合数（不打印 Agent 名）。
- Anthropic 的值一律不动（一直是生效的）。
- 同一次提交里，`seed_canary.py` 的两处 `max_tokens: 4096` 也删掉。

### 4.4 截断处理

- provider 层把「被截断」标准化：`finish_reason == "length"`，或 Anthropic 的 `stop_reason == "max_tokens"` → `response_metadata["truncated"] = True`。
- agent 节点在收到回复之后判断：
  - 被截断，且**没有可用内容**（正文为空，或者工具调用参数解析失败）→ 抛 `OutputTruncatedError`（归入现有的 `GuidedTimeoutError` 家族，都是「带操作指引的错误」）。run 以可见错误结束，文案：「模型输出被截断（已用满输出上限 N，含思考）。请在模型配置里调大『输出上限』，或者降低思考档位。」**不触发 fallback**（换备用模型也是同一个上限）。
  - 被截断，但正文可用 → 照常交付，同时计一次指标 `expert_work_llm_output_truncated_total{provider,model}`，并打一条 warning 日志。
- 看图（ask_image）走同一个 provider，自动享受上面的处理；快看（quick）档本来就短，不单独处理。

### 4.5 配置页

- 「输出上限」：字段名直接写「输出上限（含思考）」；留空时占位文字写「厂商默认（最大 N）」；提示文案改为「单次输出上限，**含思考**。留空 = 厂商默认。」删掉「OpenAI 系忽略」这句。
- 「思考长度上限」：放在「推理深度」下面。模型支持时可以编辑；不支持时置灰，并写「该模型只能调思考档位，不能限制思考长度。」
- 文案按用户要求保持简洁。

### 4.6 看图快看（ask_image depth=quick）的通用兜底

- 快看的机制本身不针对任何厂商：`_thinking_off` 按目录把看图模型的思考关掉，关的方式和 Agent 配置里关思考走同一条路。它的缺口不在机制，在**目录登记错**和**有几家没法真调**（§8.6）。
- **兜底**：快看请求被厂商以参数错误拒掉（4xx，不含 401/403/429）时，自动按 Agent 原本的看图配置（等同 deep）重试一次，并计指标 `expert_work_vision_quick_fallback_total{provider,model}`、打 warning 日志。这样新接入的模型即使关思考的参数写错，最坏也只是退回正常看图，不会看图失败。OpenAI（`minimal` 不在 gpt-5.5 取值表里、没有 key 实调）在真调之前就靠这一层兜住。
- **上架规矩**（写进 `model_catalog.py` 模块注释）：新增或修改带视觉的模型，目录必须写明思考形态（开关 / 档位 / 预算）、默认开还是关、怎么关，并用 T0 探针（`thinking disabled` / `enable_thinking=false` / 默认各一次）实调后再合并。

## 5. 真调核实（实现之前先做，结果回填目录）

在测试集群 pod 里跑探针，直接用平台 key 调厂商，只打印聚合结果。每个型号的核对项：

| 核对项 | 判据 | 覆盖 |
|---|---|---|
| A. 上限字段生效，并且包含思考 | 设 cap=300 + 开思考 → `finish_reason=length`，且 usage 里 completion ≤ 300（含 reasoning） | glm-5.3 / 5.2 / 5.3-flash、kimi-k3、deepseek-v4-pro / flash、qwen3.8-max / 3.7-max、豆包 seed-2-1-pro |
| B. 豆包两个字段同时发会 400 | 同时发 `max_tokens` 和 `max_completion_tokens` → 4xx | 豆包 |
| C. 豆包 `budget_tokens` 是否被忽略 | 同一道题，`budget_tokens=256` 与不发，比较 reasoning token 数；再用 `reasoning_effort` 的 low 和 high 各跑一次比较 | 豆包 |
| D. 通义 `thinking_budget` 是硬上限 | budget=200 → reasoning ≤ 200 附近，而且仍然给出回答 | qwen 两款 |
| F. 通义 `max_tokens` 算不算思考 | 开思考，cap=300、不设 `thinking_budget` → 看 reasoning 是否超过 300；决定 §4.0 最后一条要不要拼合计 | qwen 两款 |
| E. 每个档位取值厂商都接受 | 目录 `effort_map` 里的每个值 + `effort_off` 各发一次，不出 4xx | 全部 5 家 |

- 探针可以重放，放在 scratchpad 里；结论回写到本 spec 的 §8。
- **核实结果和调研结论不一致时，以实测为准，并在目录注释里写明「实测日期 + 现象」。**
- OpenAI / Azure / Anthropic 测试环境没有 key，处理方式见拍板点 ②。

## 6. 任务拆分（初稿，拍板后再写计划）

| # | 内容 | 依赖 |
|---|---|---|
| T0 | 真调探针（§5），回填 §8 | — |
| T1 | 目录加字段、填值；`effort_map` / `effort_off` 迁入目录，并改写两个 payload 函数（附等价测试：已核实的这几家，线格式和今天逐字节一致，除非 T0 证明今天发错了） | T0 |
| T2 | `ModelSpec.max_tokens` 改为可空、新增 `thinking_max_tokens`、保存校验；provider 按目录发送输出上限 | T1 |
| T3 | alembic 数据迁移（§4.3）+ seed_canary | T2 |
| T4 | 截断的标准化、`OutputTruncatedError`、指标；§4.6 快看被拒时退回正常看图 | T2 |
| T5 | 配置页：两个字段、置灰、文案、i18n | T2 |
| T6 | 测试环境发布，真栈回归：ai-health-plan 读 PDF 场景 + sop2-designer 长输出场景，各 5 家主模型设小 cap 一次，确认截断报错可见 | 全部 |

PR 切分倾向：T0 不进 PR；T1–T4 一个后端 PR；T5 一个前端 PR；然后一个测试环境发布记录 PR。

## 7. 拍板点（2026-09-24 均按推荐项定）

1. **默认值语义**：None = 不发，走厂商默认（推荐；存量行为不变）。另一种是「平台统一给个默认值」，比如 32K —— 这样每家行为都会变，而且得逐家去挑数。
2. **没 key 的三家（OpenAI / Azure / Anthropic）**：
   - (a) 推荐：只做「显式填了值才按文档字段发送」+ 截断报错；档位映射（`minimal` → `none`、`max` → `xhigh`）**先不改**，登记成 backlog，等有 key 真调之后再改。
   - (b) 按文档一起改，标注「未真调」。
3. **存量迁移**：只删 4096（推荐）/ 非 Anthropic 的显式值全部删掉（最保守，但会丢掉用户自己设的 40960）。
4. **思考长度上限填在不支持的模型上**：保存时拒绝（推荐，和 `effort` 的做法一致）/ 只告警。
5. **生产上线前**：需要你在生产库跑一条只读 SQL（我来给），数一下非 Anthropic 的非 4096 显式值有多少，以及 Anthropic Agent 有多少 —— 迁移之后这些值开始真正生效。

## 8. 核实结果（T0，2026-09-24 测试集群实调）

探针：scratchpad `b105-t0/probe.py`（可重放），原始输出 `b105-t0/run1.txt`。题目是一道排列组合题，保证模型会思考。

### 8.1 哪个字段是「合计」上限（cap=300，开思考）

| 模型 | `max_tokens=300` | `max_completion_tokens=300` | 结论 |
|---|---|---|---|
| glm-5.3 / 5.2 / 5.3-flash | length，完成 300（思考 300、正文 0） | **被忽略**（完成 459～1439，正常结束） | 用 `max_tokens` |
| deepseek-v4-pro / flash | length，完成 300（正文 0） | **被忽略**（pro 完成 7,736） | 用 `max_tokens` |
| kimi-k3 | length，完成 300（含思考 239） | length，完成 300 | 两个都行，用 `max_completion_tokens` |
| qwen3.8-max / 3.7-max | **不限思考**（完成 703 / 1,008，正常结束） | length，完成 300（正文 0） | 用 `max_completion_tokens` |
| doubao-seed-2-1-pro | **不限思考**（180 秒超时） | length，完成 300（正文 0） | 用 `max_completion_tokens` |

- 豆包同时发两个字段 → 400：「max_tokens and max_completion_tokens cannot be set at the same time」。**已证实。**
- **设错字段在 GLM / DeepSeek 上是静默的**（200 正常返回，只是上限不生效）。所以这张映射必须登记在目录里、配等价测试，不能靠报错兜底。
- 截断的形态一致：`finish_reason=length`，正文为空，思考吃满额度。§4.4 的空回答报错覆盖的正是这种情况。

### 8.2 思考长度上限

| 模型 | 结果 |
|---|---|
| qwen3.8-max / 3.7-max `thinking_budget=200` | 思考 **正好 200**，之后照常作答（正文 596 / 193 字）→ 硬上限，§4.2 成立 |
| 豆包 `thinking.budget_tokens=256` | **180 秒超时** —— 思考没被限住，和不传没区别 → **字段被忽略，已证实**（今天带 effort 的豆包配置，这个值一直没起作用） |
| GLM / Kimi / DeepSeek | 没有这个参数（按文档，未另测） |

### 8.3 档位取值

| 模型 | 实测 |
|---|---|
| glm-5.3、glm-5.3-flash | `reasoning_effort=medium` → 400；`thinking.type=disabled` → **400**（「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」）。low / high / max 可用 |
| glm-5.2 | low / medium / high / max、disabled 全部可用 |
| kimi-k3 | low / medium / high / max 全部可用（文档里没有 medium，但实测接受） |
| deepseek-v4-pro / flash | low / medium / high / max 可用；**`none` 被接受但不关思考**（pro 思考 5,016）；`thinking.type=disabled` 真关 |
| qwen3.8-max / 3.7-max | `reasoning_effort` low / medium / high 被接受；`enable_thinking=false` 真关 |
| doubao-seed-2-1-pro | `reasoning_effort=minimal` = 不思考（思考 0）；low 3,676、medium 5,315、high 超时（>180 秒）；`thinking.type=disabled` 真关；**默认开思考时，一道小题就思考了 8,985 token** |

### 8.4 顺带发现的现存 bug

1. **glm-5.3 关思考会 400**：目录里只给 glm-5.3-flash 标了 `always_thinking`，glm-5.3 没标，所以关思考时会发 `thinking.type=disabled`。测试环境目前没有 Agent 给 glm-5.3 关思考（§2 的分布里只有 True / None），属于潜伏 bug。随 T1 修：给 glm-5.3 补上 `always_thinking=True`。
2. **豆包的档位从来没生效过**：见 8.2。T1 改成 `reasoning_effort`，档位对照：low→low、medium→medium、high→high、max→high。
3. 豆包默认思考非常长（小题 9K token、high 超过 3 分钟）。这和 B-64 真栈看到的 11,753 token 一致。看图默认走快看（关思考）已经避开了这一点；主模型用豆包时，建议配置页在豆包上提示「默认思考很长」—— 列为可选项，不在本期范围。

### 8.6 看图模型的「关思考」补测（2026-09-24）

| 模型 | 厂商默认 | 关思考 | 目录现状 | 处理 |
|---|---|---|---|---|
| glm-4.6v | **默认思考**（小题超过 180 秒没答完；显式开时思考 2,114） | `thinking.type=disabled` 有效（思考 0，完成 28） | `thinking=None` → **快看完全不起作用** | 改成 `toggle` + 默认开；glm-4.5v 实调：disabled 有效（思考 0）、enabled 思考 2,738 → 同样处理 |
| glm-5v-turbo | 默认思考（1,767） | disabled 有效 | toggle | 不变 |
| kimi-k2.6 | 默认思考（1,883） | disabled 有效 | toggle | 不变 |
| qwen3-vl-plus / flash | **默认不思考** | `enable_thinking=false` 有效；显式开时思考 5K | `thinking=None` → 用户开不了思考 | 改成 `budget` + 默认关 |
| qwen3.6-plus | 默认思考 | `enable_thinking=false` 有效 | budget | 不变 |
| Anthropic opus / sonnet、OpenAI gpt-5.5 系列 | —（没有 key） | 未测 | effort | 靠 §4.6 的兜底；OpenAI 档位映射见拍板点 ② |

### 8.5 对设计的修正

- §4.0：不需要拼合计（每家都有真正的合计字段）。
- §4.1：`output_cap_field` 按 8.1 填。
- §4.2：通义的 `thinking_budget` 已实测是硬上限，按原设计做。
- T1 补目录修正：glm-5.3 标 `always_thinking`；豆包档位从 `budget` 改成 `effort` 形态；glm-4.6v / 4.5v 改成 `toggle` + 默认开（两款都已实调）；qwen3-vl-plus / flash 改成 `budget`、默认关。
- 新增 §4.6：快看被拒时自动退回正常看图，并加上架规矩。
