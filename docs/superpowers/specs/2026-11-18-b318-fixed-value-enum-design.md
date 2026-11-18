# B-318 MCP 工具参数「固定值」：有可选值时改下拉，保存时校验

日期：2026-11-18 · 状态：已定，实现中 · 来源：B-314 04 评审（`2026-11-17-b314-dify-migration/00-overview.md:145`、`04-mcp-tools.md` §4.1，验收 `07-testing-and-cutover.md:103,157`）

## 1. 问题

智能体配置里，MCP 工具参数可以钉成「固定值」（B-127）。现在固定值只是一个文本框，工具自己声明了只能取哪几个值也不管。深护 `handoff_to_human` 的 `this_turn` 只能是 `reply` / `silent`，写成 `slient` 照样能保存，要到真转人工那一刻才失败（平台调用前的参数校验拦下，模型看到「arguments failed schema validation」）。

## 2. 现状（file:line）

| 位置 | 现在做什么 |
|---|---|
| `apps/admin-ui/src/components/manifest-editor/widgets/McpToolPicker.tsx:740-768` | 选「固定值」后出一个 `Input`；只用 `fixedValueValid` 标红空值 / 首尾空格。文件已 779 行，接近 800 上限 |
| `.../widgets/mcp_arg_bindings.ts:67-68` `fixedValueValid`；`:125-128` `ToolParam`（只有名字、是否必填）；`:151` `paramsOf`（白名单读 schema） | 不读参数自己的 schema |
| `apps/admin-ui/src/components/mcp_catalog/CatalogToolsTab.tsx:51-65` `extractParams`；`:141-171` 参数表三列 | 只显示名字 / 类型 / 描述，不显示可选值。这是平台 MCP 目录详情页（`pages/McpCatalogDetail.tsx`）的「工具」页签 |
| `apps/admin-ui/src/pages/SettingsMcpServers.tsx:530-536` | 租户 MCP 页**没有参数表**，工具只是一排名字标签，描述在悬浮提示里。grep 过 admin-ui，参数表只有 `CatalogToolsTab` 一处（本次两页共用，见第 4 节） |
| `packages/expert-work-protocol/.../agent_spec.py:1325-1356` `ArgBindingSpec` | 固定值只查非空、首尾空格、不与 `args` 重名。协议层看不到工具 schema，查不了可选值 |
| `services/control-plane/.../api/agents.py:1036-1134` `_check_buildable` | 保存时用真 builder 试建一次（挂 MCP 池、实时 `list_tools`）。建不出来 → 422；MCP 连不上、绑定落空 → 只告警（`:1137` `_skipped_mcp_server_warning`、`:1150` `_unmatched_binding_warning`） |
| 同文件 `create_agent:1420`、`update_agent:2189`、`publish_draft:2401` 拦；`save_draft:2302` 不拦，原因放进 `build_error` 照存 | 草稿的既有规矩：「建不出来不拦保存，但必须说出来」 |
| `services/orchestrator/.../tools/mcp.py:1550` `register_mcp_tools`，`:1646-1662` | 平台唯一同时拿着「绑定表」和「工具真实 schema」的地方；已在这里判「参数不在」并记到 registry |
| `.../tools/registry.py:233` `UnmatchedArgBinding`、`:754` `note_unmatched_arg_binding`；`built_agent.py:112`；`agent_factory.py:1433` | 落空绑定从 registry 经 `BuiltAgent` 交给保存时的试建 |
| `.../graph_builder/builder.py:4771` `_validate_tool_args` | 运行时：填完固定值后按工具原 schema 校验，错值在这里被拦，调用不会发到 MCP |

YAML 视图和表单视图存的是同一份 `manifest_yaml`，走同一组接口（`pages/agent_detail/ManifestTab.tsx:177,184`；新建走 `CreateAgentModal.tsx:108` → `POST /v1/agents`），后端校验天然覆盖两边。

## 3. 「可选值」怎么认（前后端同一套规则）

只看参数自己那一层（`properties[参数名]`），按顺序看第一个**出现**的键：

1. `enum`：非空列表、每项都是字符串 → 每项一个选项，没有标题。
2. `oneOf`，再 `anyOf`：非空列表、每项是对象且 `const` 是字符串 → 每项一个选项；`title` 是非空字符串就当标题。项里的其它键（`type`、`description`）不管。
3. 以上都没有，或者第一个出现的键形状不对（有非字符串、缺 `const`、空列表、不是列表）→ **没有可选值**：前端照旧文本框，后端不查。

重复值去重、保持顺序。不认的写法：单个 `const`、`enumNames` / `x-enumNames`、数字 / 布尔枚举（固定值只收字符串，`agent_spec.py:1325`）、`["string","null"]` 和 `anyOf: [{enum}, {type:null}]` 这类可空组合。schema 是第三方给的，按 `paramsOf` / `_top_level_params` 的做法只认白名单形状，不猜、不抛。

## 4. 改什么

**前端**
- 新建 `apps/admin-ui/src/utils/mcp_param_options.ts`：`paramOptionsOf(inputSchema, param) → {value, title}[] | null`，按第 3 节。绑定编辑器和参数表共用。
- 新建 `widgets/FixedValueInput.tsx`，把 `McpToolPicker.tsx:740-768` 那段搬出去（顺带让主文件回到 800 行以内）：
  - 有可选值 → `Select`，选项 label = `title`，没有 title 用值本身；写回的是值（`const`）。
  - 没有可选值 → 原来的 `Input` 和 `fixedValueValid` 提示，行为不变。
  - 当前值为空 → 下拉空着、标红、提示「选一个值」。
  - 当前值不在可选值里（老配置、YAML 手写）→ 下拉照样显示这个原值（antd 对不在选项里的值原样显示），`status="error"`，下面红字「当前值 slient 不在工具允许的值里，可选：本轮回复 reply / 本轮不回 silent」。**不清掉、不自动改**，人重新选才写回。
  - 和现在一样不在前端挡保存，由后端把关。
- 参数表抽成共用组件 `components/mcp_catalog/ToolParamsTable.tsx`（参数名 + 必填 *、类型、描述；描述下面加一行小字「可选：本轮回复 `reply` / 本轮不回 `silent`」，没有标题只列值；没有参数写「无参数。」）。类型列不动。
  - `CatalogToolsTab.tsx` 删掉自己的 `extractParams` 和列定义，改用它。
  - 租户页 `SettingsMcpServers.tsx`（11-18 用户定加）：展开一台服务后，原来那排工具名标签改成和平台目录页同样的折叠列表（工具名 + 描述），每个工具展开就是 `ToolParamsTable`。
    - 数据：租户页的 `GET /v1/mcp-servers/{name}/tools`（`mcp_servers.py:1084-1088`）本来就返回 `input_schema`（自定义服务和租户启用的平台服务都是现场探测），不用补后端。
    - 拿不到时：连不上照旧显示「无法连接」（平台目录页是同一句意思的 `tools_unreachable`）；OAuth 按用户授权的平台服务后端回 409，租户页这一行本来就不能展开、显示「需你授权」链接（平台目录页对应的是「OAuth 连接器按用户各自授权，无法在此列出工具」），口径一致，不改。
- i18n 加 3 个键（中英）：下拉占位、错值提示、「可选：」。

**后端**
- `tools/arg_bindings.py` 加 `fixed_value_options(input_schema, param)`，规则同第 3 节。
- `registry.py` 加 `InvalidFixedValue(server, tool, param, allowed)` 和 `note_invalid_fixed_value` / `invalid_fixed_values()`（同一条去重，同 `note_unmatched_arg_binding`）。`allowed` 带值和标题，**不带配置里写的那个错值**（沿用 B-127「绑定记录不带值」的规矩，这条记录也会进运行期日志）。
- `mcp.py register_mcp_tools`：在「参数不在」处理之后，对 `bound.fixed` 每个参数取可选值，值不在里面就记一条，并打一行 `mcp.fixed_value_not_allowed server tool param` 告警（不打值）。**绑定照旧下发，运行期行为不变**（调用前校验照样拦）。
- `BuiltAgent.invalid_fixed_values`（带默认值，存量构造点不改）；`agent_factory.py:1433` 旁边传进去。
- `_check_buildable`：试建成功后，`invalid_fixed_values` 非空 → 按「建不出来」处理：`rejected` = 422 `MANIFEST_UNBUILDABLE`（沿用现有错误码，前端不用改），`error` 同文；指标记 `rejected`。文案：
  `fixed value is not one the tool allows: deepcare/handoff_to_human this_turn — allowed: reply (本轮回复), silent (本轮不回)`，多条用 `; ` 连。
  于是：新建、PUT、发布 → 拒绝；存草稿 → 照存，红条显示同一句（草稿既有规矩）；回滚不经过这里，不拦。
- 告警文案：`_skipped_mcp_server_warning` 改成「…so their tools, arg_bindings and fixed values were not checked: …」。

## 5. 拿不到 schema 时：放行 + 告警（推荐，与 04 §4.1 已批的写法一致）

「拿不到」有三种，固定值都不查，保存照常：
- 保存时那台 MCP 连不上（B-294 跳过）：告警点名「这台服务的固定值没核对」（`_skipped_mcp_server_warning`，本单改了文案）。
- 按用户 OAuth 授权的服务：保存时 `user_id=None`，这类服务根本不挂载，绑定走的是既有的 `tool_missing` 告警（「no such tool in the assembled catalog」）。**本单不改它的文案**，所以这条告警不会专门提到固定值。
- 工具不在目录里：同上，既有的 `tool_missing` 告警。

另记一笔：试建抛 `PlatformNotConfiguredError`（平台缺凭据 / 缺嵌入模型）时，`_check_buildable` 直接放行 + 告警、拿不到构建结果，**固定值检查也跟着跳过**；凭据补上之后的下一次保存 / 发布才会查。

理由：
- 拒绝等于第三方服务一抖动就锁死这个智能体的所有修改，包括出事时最急的改提示词、关工具。平台对 MCP 一贯是「连不上只跳过，不卡保存」（`agents.py` `_check_buildable` docstring、B-61 §5.4、B-294），这里不破例。
- 错值漏过去后果有限：运行时平台调用前按工具原 schema 校验（`builder.py:4771`），错值根本发不到 MCP；深护 MCP 自己也会回 42200 `invalid_fixed_value`。后果是这个工具调不通、轨迹里有报错，不会写错数据。
- 服务连不上时，表单里绑定编辑器本身就打不开（拿不到工具就没有参数行），能写错值的只剩 YAML 手写这一条路，而告警会当场说「没核对」。服务恢复后下一次保存 / 发布就会查出来。

拒绝的唯一好处是「错值一定进不来」；代价是可用性，对这类「调不通但不写错」的错误不值。

## 6. 测试计划（每条都做改坏验证，确认会红）

| 层 | 用例 | 改坏方式 |
|---|---|---|
| 前端 `mcp_param_options.test.ts` | enum / oneOf / anyOf 读出选项；无 title 用值；非字符串、缺 const、空列表、`properties` 不是对象 → null；去重 | 去掉「全是字符串」判断；title 回退写反；去掉去重 |
| 前端 `McpToolPicker.fixedOptions.test.tsx`（新文件；原文件已 900 多行） | ① oneOf 参数选「固定值」→ 出下拉不出文本框，选项是「本轮回复 / 本轮不回」；② 选「本轮不回」→ 写回的 manifest 里是 `silent`；③ 没有可选值的参数仍是文本框；④ 已存 `slient` → 显示原值、标红、提示列出可选值，manifest 不被改 | 一律渲染 Input；写回 title；一律渲染 Select；过滤掉不认识的值 / 不标红 |
| 前端 `CatalogToolsTab`（`SettingsMcpCatalog.test.tsx` 里加） | 有可选值的参数显示「可选：本轮回复 reply / 本轮不回 silent」；没有的不显示 | 删掉那一行渲染；无可选值也渲染 |
| 前端租户页（`SettingsMcpServers.test.tsx` 里加） | 展开服务 → 展开工具：参数、必填 *、类型、描述、「可选：」；无参数写「无参数。」 | 删掉那一行；租户页不用 `ToolParamsTable`；去掉必填星号 |
| e2e | `agent-mcp-picker.spec.ts` (f) 下拉 + 选值 + axe；`mcp-servers.spec.ts` (g) 租户页参数表 + 「可选：」+ axe | 一律文本框；删掉「可选：」行 |
| 后端 `test_arg_bindings.py` | `fixed_value_options` 与前端同一组用例 | 同前端 |
| 后端 `test_mcp_arg_bindings_wiring.py` | 错值记一条、带允许值、不带错值；对值不记；无可选值不记；服务连不上不记、记 skipped；工具没声明的参数只报 `params_absent` 不报错值；绑定照旧下发 | 去掉记录调用；比较写反；连不上时当错值报；没有可选值也当错 |
| 前端 `ToolParamsTable.test.tsx`（审查后补） | 第三方畸形 schema 不抛：参数 spec 是 null、`required` 不是数组 / 混非字符串、`type` / `description` 不是字符串、`properties` 不是对象 | 去掉各处形状判断 |
| 后端 `test_agents_api.py`（builder 替身） | 新建 / PUT / 发布 → 422 且文案列出允许值、没落库；存草稿 → 200 + `build_error`；对值 → 无错；连不上 → 201 + 告警含「fixed values were not checked」 | `_check_buildable` 不看新字段；草稿分支也拦 |
| 后端 `test_agent_factory.py` | 真 builder + 假 MCP 客户端：`BuiltAgent.invalid_fixed_values` 被带出来 | factory 不传字段 |

跑法：`uv run --no-sync pytest` 相关文件 + CI 同参数全量；ruff、mypy；admin-ui `pnpm typecheck`（`tsc -b --noEmit`）、`pnpm test`、`pnpm build`、相关 e2e。

## 7. 验收用例

`docs/runbooks/pending-test-release-acceptance.md` 末尾加 **§124**（现最大 §123；合并前若被占用就顺延）：控制台给 `handoff_to_human.this_turn` 选固定值看下拉；YAML 手写 `slient` 存草稿看红条、点发布看 422；切回表单看错值标红；平台 MCP 目录详情、租户 MCP 页各看一次「可选：」。测试环境深护 `handoff_to_human` 还没上时，用任意一个参数表里出现「可选：」的工具代替。写清「改坏时会看到什么」：发布成功、运行时该工具报 schema 校验失败。

## 8. 工作量

约 1.4 人天：后端 0.4（规则 + registry + 保存校验 + 测试），前端 0.8（规则 + 下拉组件 + 共用参数表 + 租户页 + 测试 + e2e），改坏验证、验收用例、自审 0.2。

## 9. 已定（2026-11-18，五点都按推荐）

1. **拿不到 schema 时放行 + 告警**（第 5 节）。
2. **存草稿不拦**，红条里说同一句；新建 / PUT / 发布拒绝（422）。
3. **错误信息不回显错值**，只说哪个参数、列允许值；错值在表单里标红可见。
4. **用户 11-18 定：租户页同步加参数表，与平台目录页共用组件**（`ToolParamsTable`）。理由：两个页面看到同一个工具的内容要一致。
5. **只认第 3 节的形状**；可空组合、数字枚举等先不支持（文本框 + 不查），有真实工具用到再加。
