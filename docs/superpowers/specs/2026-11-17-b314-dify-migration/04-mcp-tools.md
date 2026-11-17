# B-314-04 深护智康 MCP 工具与身份

> 所属:B-314 Dify「健管师」迁移(总览:00-overview.md)。状态:草稿 v1;11-17 评审已过,各项决定见下方修订说明与第 9 节。
> 读者:深护 MCP 服务开发者(第 3、5、6 节是施工图);配置智能体的人(3.3、3.5、3.7、5.11 节)。
> 核对基线:深护 MCP `499302b`、数字人服务 `8dc5f3f`、项目服务 `b6938bc2`、pad 消息同步 `0ff5743`、dk 会话存档 `ba0a89e`、Expert Work `origin/main d9db26f79`(测试环境镜像 `5908fe554`)、Dify DSL `深护智康(pad)-健管师-3.0.yml`。下文「确认」= 读过代码,「推断」= 没跑过或靠参数名对上。
>
> **修订(2026-11-17,转人工默认改为本轮回复)**:11-17 默认由本轮不回改为本轮回复 —— 健管师转人工默认「本轮回复 + 之后照常」(`this_turn=reply`、`afterwards=continue`):转人工那一轮先回一句稳住的话(如「我确认一下,稍后回复您」),不回答交出去的问题,不说「转人工」。理由:客户每次都能马上得到回应;组合按智能体配置(控制台下拉),对所有转人工类型都生效。「本轮不回」仍是下拉里的选项,急症例外和不回时的临时标志做法照旧保留。本文改了 §3.10(四种组合的例子、返回给模型的话、默认组合)、§4 配置、§5 `handoff_to_human`、§6 YAML 示例、§7 第 19 / 20 条、§9.15。
>
> **修订(2026-11-17,04 评审结论)**:写入不需客户确认、不设按客户上限;化验单按上传月合并;按小时循环沿用 30 天;数据情境(空腹 / 餐次 / 测量时间)模型先推断再问(11-17 起不做按表单的配置);防重复写改为「按来源消息」判断(取代「同一天同内容」);客户资料由模型调 `profile_get_brief` 一次取齐(含现有提醒,v1 就做);「查更早聊天」改为增强现有 `chat_private_history`;新增转人工工具 `handoff_to_human`(MCP 直接发通知,四种组合 + 急症例外,§3.10);主回复仍由业务方发,「走 MCP 发」记为否决项(§3.11);Expert Work 平台加「固定值下拉」(§4.1)。
>
> **修订(2026-11-17,06 评审结论)**:9.10 各项按本文推荐定稿并写进工具规格 —— 提醒短编号(5.4、5.5)、一次删多条必须带「客户已确认」(5.5)、设提醒返回带星期(5.3)、查表单结束日裁到今天(M4);改档案按字段编码写。
>
> **修订(2026-11-17,用户 11-17 拍板)**:不做按表单的情境字段配置 —— 情境(测量时段 / 餐次 / 测量时间)由模型读字段名和字段说明、按消息时间和上下文推断,推不出才问(所有表单通用的提示词规则);三个累积字段的 `field_code` 写死在深护 MCP 代码里(3.4、5.7)。
>
> **修订(2026-11-17,独立核对后的修正)**:`source_message_ids` 在 03 之后由数字人服务另传的 `source_msg_keys` 生成(6.16);转人工关 AI 开关时 pad id 换企微明文 id 定为 MCP 自己反查,查不到就不动开关、记告警(3.10 第 4 步);查聊天条数统一叫 `page_size`;M12 / 加固 9.5 标明不属于 B-314;若干措辞按已定事项更新。
>
> **修订(2026-11-17,提醒实现方式)**:9.2 定为 A —— MCP 直接写提醒表、直接投数字人服务的延迟队列,不经数字人服务的接口。
>
> **修订(2026-11-17,智能体专用素材表)**:素材改由深护新建的 `t_project_agent_material` 提供(表名待深护确认),原「`t_project_material` + 分类表 + 分类关系表」设计作废;`material_list` 钉的是 `agent_code`,不再是分类编码(05 §3.3)。
>
> **修订(2026-11-17,03 评审结论)**:写工具判重用的 inputs 变量(和两个写工具的参数)由 `input_message_ids` 改名为 `source_message_ids`,与 03 的顶层字段分开(6.16、9.16)。
>
> **修订(2026-11-17,素材库补充与 08 评审)**:`material_list` 多返回素材说明 `description` 的前一段(素材表新加的可空列,05 §3.3);9.5 的「key 限项目、限工具」改为深护安全待办里的可选加固,不卡上线(生产 `AGENT_KEYS` 已确认配置)。
>
> **修订(2026-11-17,05 评审结论)**:新增素材清单工具 `material_list`(运动视频等走深护素材库,§5.12);转人工两项定稿 —— 找不到通知对象就不转、照常回复(9.14),健管师默认组合(9.15;后于 11-17 由本轮不回改为本轮回复);pc 转人工明确为照数字人服务的 `enqueueReferral` 往 `t_project_cpwx_pc_send_task` 插 referral 任务(§3.10);更正 §3.11 理由 1:dk 经机器任务服务发出时会写 AI 已发标记,不会被当成真人接管,pad 通道未核实。

## 0. 一句话结论

迁移后,健管师智能体对深护业务数据的读写全部走深护 MCP:**复用现有 3 个只读工具(查字段结构、查最新值、按时间查提交),增强 1 个(`chat_private_history`,查更早聊天),新增 8 个**(表单写入、提醒增 / 查 / 删、健康方案精简版、客户资料精简版、转人工、素材清单),加上平台高德的天气工具共放行 13 个(§3.5)。「这是谁的数据」由平台 `arg_bindings` 按本轮 inputs 填进去,模型看不到也改不了。Dify 里靠提示词约束的写入规则(化验单按月合并、最多 70 字段、累积字段追加、不写姓名、不重复写)搬到 MCP 服务端;防重复写看「这条记录是不是由同一批客户消息写过」(§3.4)。转人工由 MCP 直接通知同事,并按智能体配置决定「本轮回不回」「之后还回不回」(§3.10)。主回复仍由业务方取 run 结果后发。天气用平台已有的 `amap-maps` 的 `maps_weather`,快递不做。Expert Work 平台只改一处:MCP 工具参数的「固定值」在工具声明了可选值时改成下拉选择,保存时校验(约 1 人天,§4.1)。

### 威胁模型(先看这里)

| | 内容 |
|---|---|
| **防** | ① 客户(或群里任何发言人)在企微里用话术诱导模型**查、改、删别的客户**的档案、健康数据、提醒、聊天记录,或替别人转人工、关 AI。例:「帮我看看张三的血糖」「把客户 cust_xxx 的体重改成 60」「把所有人的提醒都删了」。② **重复写**:同一批客户消息被处理两次 —— 打断后合并重跑、模型在一轮里连调两次保存、网络重试 —— 导致一条数据记两遍、同一个提醒设两条、同事收到两次转人工通知。 |
| **不防** | 平台管理员、深护管理员(可信);客户对**自己**的数据说假话(属于数据质量,服务端只做格式和选项校验);客户让 AI 对**自己**少回或不回(只影响他自己的服务,员工可重新打开)。 |
| **怎么防** | ① 身份参数(项目、客户、员工、群)全部由平台 `arg_bindings` 从本轮 inputs 填,并从模型看到的工具参数里删掉;只放行「只能作用于当前客户」的工具;深护**生产** MCP 的 agent key 白名单必须非空,健管师单独发一把 key。② 业务方把本批企微消息 id 作为变量 `source_message_ids` 传进来,平台绑定进写工具;服务端记下每条记录来自哪些消息,「同表 + 同内容 + 来源消息有重叠」才算重复(§3.4)。不设按客户的调用 / 写入上限(11-17 定)。 |
| **剩余风险(只列不做)** | 绑定「没落地」时参数会回到模型手里(平台保存时会给英文警告,见 6.2);key 泄露后可以调全部 45 个工具、可以填任意客户编号(MCP 目前不按 key 限工具、限项目,见 9.5 加固项)。 |

## 1. 现状:Dify 现在怎么做

### 1.1 Dify 里的每一项业务数据操作

**读**

| 操作 | Dify 节点(id@行) | 调的什么 | 备注 |
|---|---|---|---|
| 客户基础档案 | 获取客户基本信息 `1751863212573`@3160 | 自定义工具 GetFormLatestData(`form_customer_basic_info`) | 会话变量缓存 |
| 健康方案 | 获取客户健康方案 `1750298915307`@3292 | 自定义工具 QueryHealthPlan,`response_format=markdown` | 对应项目服务 `health-plan/query-customer-health-plan-md`(推断:按参数名对上) |
| 系统表单清单 | 获取系统表单信息 `1750298961942`@3424 | QueryFormList,`is_system=1`、`form_status=1` | |
| 基础表单字段结构 | 获取客户基础表单字段 `17518596407670`@4063 | GetFormFieldDetail | |
| 按时间查表单数据 | 提取待查询表单 `1761371318646`@6890 → 查询客户表单数据 `1761372171575`@7206 | GetFormDataList,起止时间由模型拼成 `YYYY-MM-DD 00:00:00` / `23:59:59` | |
| 今日饮食日志 | 查询今日饮食日志 `17934182268130`@10393 | GetFormDataList(`form_food_log`) | |
| 化验单本月记录 | 获取化验报告最近记录 `1789740294859`@9507 | HTTP 项目服务 `/api/form/get-form-field-values`,`page_size=1` | 请求体里 `project_code`、`owner_code` 没加引号 |
| 提醒列表 | 查询日程提醒 `1768788146742`@8771 | HTTP 数字人 `/ai-notify/get-ai-notify-list`(按 `customer_pad_userid`) | |
| 历史聊天 | 获取历史聊天记录 `1770115147110`@9050 | HTTP 数字人 `/message/get-history-message` | |
| 天气 / 快递 | MCP Agent `1752317953920`@4693 | 直连高德官方 MCP 与快递100 MCP(地址里带 key,本文不复述) | ReAct 最多 10 步 |
| 读图 | 读取图片 API `1761358646107`@4845 | 内部读图 HTTP | 迁移后走平台视觉,不在本文 |

**写**

| 操作 | Dify 节点 | 调的什么 | 现在的结果 |
|---|---|---|---|
| 改个人档案 | 提取需更新客户的信息 `1761367072328`@5686 → 格式化 `1761367092790`@5737 → 提交个人信息 `1761367471517`@5954 | SetFormFieldValues(`field_values_str`,不带 `submission_code`) | 每次新建一条提交 |
| 上报健康数据 | 意图 `1761367647758`@6128 → 选表 `1761368088486`@6227 → 迭代 `1761370648150`(提取 `1761370867047`、格式化 `1761370879007`、提交 `1761371160619`@6782) | SetFormFieldValues | 每次新建一条提交 |
| 化验单 | 分流 `1789739450868` → 取最近一条 `1789740294859` → 判同月 `1789740487772` → 迭代 `1789740888870`(提取 `1789742130208`、异常分流 `1789742570094`、截 70 条 `1789743707720`、提交 `1789742880167`@10032) | HTTP 项目服务 `/api/form/set-form-field-values`,同月时带 `submission_code` 合并 | 同月合并、跨月新建 |
| 新增提醒 | 提取新增提醒的要素 `1751532976185`@3842 → 发送设置提醒请求 `1751536574974`@3941 | HTTP 数字人 `{agent_call_back_url}/add-notify` | 响应不检查 |
| 删除提醒 | 提取要删除提醒的要素 `1768787117123`@8713 → 查询 → 删除日程提醒 `1768788430365`@8923 | HTTP 数字人 `/ai-notify/delete-ai-notify` | 响应不检查 |

### 1.2 写入规则现在在哪:全在提示词和代码节点里

- 不写姓名、区分姓名与称呼(`1761367072328` 规则 4–5、限制 4–5)。
- 累积型字段(目前用药、其他健康状况、过敏源)在已有内容上追加、标注日期,除非客户明确说停药 / 痊愈 / 原信息有误(规则 9–10)。
- 否定描述(「无既往史」「无过敏」)不覆盖已有内容(规则 11);Select 只能从 options 里选,没合适的不写(规则 12);出生年月只在为空时推算(类型规则 5)。
- 空值和「未知」不提交(`1761367092790` 代码)。
- 血糖必须同时有数值和测量时段才算上报;他人数据、假设性提问不报;已凑齐过的不重复报(`1761367647758` 第 3.3 节)。
- 化验单:按「上传当月」合并到同一条提交(`1789740487772`,只看最近一条记录的提交月份),`field_analysis_node_name` 固定写当月 `YYYY-MM`(`1789742130208` 规则 8),附件链接与已有链接用「;」拼接(规则 7),最多 70 条(规则 10 + `1789743707720` 代码截断),必须至少有一个实际指标(`1789742570094`)。
- 提醒:默认提前 10 分钟、钟点换算、循环最长 30 天、不支持按分钟循环(`1751532976185`)。

### 1.3 服务端现在怎么落库

**表单**(项目服务 `form.service.ts:1291-1499` `setFormFieldValues`,确认):

- 不带 `submission_code` 就新建一条提交。Dify 改档案、上报两条路都走 `ai-set-form-field-values`(`:1501-1505`,不带 `submission_code`),所以**每次都新建、没有任何幂等**。
- 带 `submission_code`:非化验单要求那条提交是当天建的(`:1305-1317`),化验单不限日期;同字段覆盖,新字段新增。
- 计算字段(`simple` / `complex`)跳过(`:1393-1402`)。
- 写 `field_real_name` 会顺手把客户主表的 `real_name` 改掉(`:1481-1491`)。这是 Dify「不写姓名」规则的来由(推断)。
- 时间取 `dayjs()`,即进程时区(`:1355`)。

**提醒**(数字人服务,确认):

- `/add-notify`:逐条写 `t_project_ai_notify` + 投递延迟队列 `send-notify-queue`,**不返回任何结果、异常只打日志**(`agent-callback.service.ts:165-241`;dk 版 `agent-callback-dk.service.ts:108-188`)。所以 Dify 里「设置成功」是提取节点事先写好的话术(`1751532976185` 的 `success_reply`),和真实结果无关。
- 查询:按 `project_code + customer_pad_userid` 取,过滤掉已结束和触发时间已过的(`ai-notify.service.ts:38-64`)。循环提醒到点后会把 `trigger_time` 改成下一次(`send-notify.processor.ts:161-235`),所以不会被误滤。
- 删除:软删表行 + 删掉还没触发的 job(`ai-notify.service.ts:66-105`)。到点发送前会再查一次表(`send-notify.processor.ts:113-120`),表行没了就不发,所以即使 job 没删掉也不会发出。
- **项目服务里已经有第二个提醒生产者**:运营后台的 AI 提醒增删改查(`project-service/src/modules/ai-notify/`),直接写同一张表、投同一个队列,并显式把队列连到数字人那边的 Redis 库(`ai-notify.const.ts:1-14`,`ai-notify.module.ts:9-14、36`)。

### 1.4 深护 MCP 现状(与本设计有关的部分)

- 45 个工具(各 `*.tool-provider.ts` 里 `name:` 计数,确认)。工具名里的 `.` 换成 `_`(`mcp-server.service.ts:39`)。`tools/list` 不分 key,全量返回(`:66-78`)。
- 鉴权只在建连接时做:员工 OAuth 或 agent key(`mcp-auth.ts:38-93`)。**agent key 通道下项目、客户、员工全部信任入参**(`bind-employee-context.ts:15、37、54` 对 agent_key 原样返回)。白名单 `AGENT_KEYS` 为空时任意 key 都通过(`mcp-auth.ts:84-89`)。
- 不按 `inputSchema` 校验入参,原样交给业务函数(`mcp-server.service.ts:102-103`)。例:`format: date` 没人检查,必填参数缺了也没人拦。
- 未预料异常把原始 `err.message` 返回给模型(`mcp-error.ts:32-44`),可能带 SQL 片段。
- 会话找不到时一律回 400(`mcp.controller.ts:58-64、99-101、110-112`);会话存在进程内存 Map 里,空闲 30 分钟清理(`mcp-transport-registry.ts:24-36`,`config/app.ts:10`)。
- `confirmRequired` 只是标注。两段确认的存储 `ConfirmTokenStore` 写好了但没有任何工具用(`ir/confirm-token-store.ts`,只在 `ir.module.ts` 注册)。
- 数据库连接时区 `+08:00`(`app.module.ts:42`)。
- 写库 + 投队列的先例:用药提醒生产者(`medication-reminder-producer.service.ts:1-17`,显式按 `Asia/Shanghai` 算时间)。
- 通用幂等表 `t_open_api_idempotency` 已有 model,唯一键 `uk_idem`(公司、业务类型、范围、幂等键)(`models/TOpenApiIdempotency.ts`)。
- 聊天记录工具 `chat_private_history` 只查员工 ↔ 客户的私聊,入参 `project_code`、`employee_code`、`customer_code`,默认近 7 天、每页 50、上限 100(`chat.tool-provider.ts:116-160`;`chat.service.ts:286-380`)。数据来自 `t_project_cpwx_chat_msg`,**pad 和 dk 两条通道都写这张表**:pad 消息同步服务每收一条就 `findOrCreate` 进去,发送方 / 接收方存企微明文 id、群消息存 `roomid` 和 @ 名单(`sync-pad-message.processor.ts:1381-1419、1457-1552、1602-1608`,确认)。
- 发消息工具 `cpwx_send_*` 只能发文字:pad 走 `message.service.ts:82-230` 直接调 pad 接口(再投同步队列),pc 往 `t_machine_task` 插一条发送任务(`message-pc.service.ts:150-215、347-369`)。MCP 仓库自己不写「AI 已发」标记(搜不到 `AI_SENT` / `markAiSent`);但 pc 的 `t_machine_task` 由 PC 机器任务服务领取,领取时对每条任务都写 AI 已发标记(`deep-ai-health-cpwx-pc-machine-task-service` `claim.service.ts:188-192`),而且它的收件人只能是外部联系人(私聊)或群(`recipient.resolver.ts:62-151`),发不了给内部同事的私聊。pad 直发会不会被同步服务当成真人接管,未核实。

### 1.5 人工接管与转人工现在怎么做(与 §3.10 有关,确认)

- **临时接管标志** `HUMAN_HAS_ANSWERED_{项目}_{员工}[_{群}]_{客户}`,存活 600 秒,放在数字人、pad 同步、dk 存档三方共用的 Redis 库里。员工号发了一条**没有 AI 标记**的消息就写(pad:`sync-pad-message.processor.ts:713-765`;dk:`human-answered.service.ts:38、157-185`,dk 先查 `AI_SENT` 标记,命中算 AI 发的);客户再开口就删(pad `:664-679`;dk `human-answered.service.ts:132-135`)。键里的员工 / 客户 / 群 id 两通道不同:pad 用员工 pad id、客户 pad id、`room_conversation_id`;dk 用员工企微 userid、客户 external_userid、群 chat_id(`human-answered.service.ts:225-227`)。
- **发送前检查**:pad 发回答前按这个键查,命中就不发给这个客户(`send-agent-answer.processor.ts:488-497`);dk 在 PC 端领任务时查(`pc-send-task.service.ts:432-452`)。到点提醒例外。
- **常开开关** `is_enabled_agent_reply`:私聊在 `t_project_customer_cpwx_relation`(员工企微 userid + 客户 external_userid 一行),群里在 `t_project_cpwx_group_member`(员工、客户各一行,两行都开才回)。数字人服务**调用前**查,关了就不调智能体(`invoke-agent.processor.ts:479-538`);发送侧不查。项目服务里已有改它的接口:私聊 `POST /employee/set-cpwx-external-contact-is-enabled-agent-reply`(`employee.controller.ts:1033` → `employee.service.ts:4520`),群成员 `POST /employee/set-cpwx-room-member-is-enabled-agent-reply`(`:1325` → `:4659`);员工端有没有按钮在用**没核实**。
- **转人工**(Dify 3.0 没用,数字人服务有接口):pad 把一段文字经发送队列发给员工绑定行上的 `referral_human_pad_user_id` / `referral_human_pad_room_id`(调用方传了就用传的,没传读绑定行,都没有就不发),私聊时再把本批客户消息标 `t_project_cpwx_pad_msg.referral_human_status=1`(按 `msg_app_info`)(`agent-callback.service.ts:43-125`)。dk 取 pc 绑定行的 `referral_human_userid` / `referral_human_chat_id`,建一条 `t_project_cpwx_pc_send_task`、`task_kind=referral` 的任务;这类任务**不过接管检查、不写 `AI_SENT`**(目标是内部同事 / 内部群),30 分钟没领走就过期;dk 没有状态列,「已转人工」就是这条任务(`agent-callback-dk.service.ts:6-7、70-104`;`pc-send-task.service.ts:55-57、206-223、292-296、324-330`)。

## 2. 目标与不做什么

**目标**

1. 健管师能做的数据操作 ≥ Dify:读档案 / 方案 / 表单,改档案,上报健康数据,存化验单,提醒增查删,查天气。
2. 每个写操作返回**真实结果**:写了哪些字段、跳过了哪些及原因、提醒编号与实际发送时间。模型据此回复,不再「凭感觉说设好了」。
3. 身份由平台绑定,模型不经手。
4. 写入规则在服务端,换模型、改提示词都不影响数据正确性。
5. 重复写在服务端兜住(按来源消息判断,§3.4)。
6. 每次返回体积受控(≤ 8000 字),时间一律带时区或注明北京时间。
7. 能把客户交给同事:通知同事,并按配置决定本轮回不回、之后还回不回(§3.10)。
8. 客户问到会话里没有的更早聊天时能查(§3.12)。

**不做**

- 快递查询(已拍板)。
- 主回复走 MCP 发:主回复由业务方取 run 结果后发,不放行 `cpwx_send_*`(否决理由见 §3.11)。
- 用药方案(`medication_*`)读写。
- 读图 / PDF 转录(平台视觉,另文)。
- 攒消息、打断、人工接管的**判断逻辑**(留在数字人服务,已拍板);转人工工具只写现有的接管标志和 AI 开关,不改谁来查、怎么查。
- 写入前找客户或员工确认;按客户限制调用 / 写入次数(11-17 定:都不做)。
- 防管理员、防 key 泄露(见威胁模型)。
- Expert Work 平台的其它改动:除「固定值下拉」(§4.1)外,都用现有的 `arg_bindings`、`allow_tools`、平台 MCP 目录。

## 3. 设计

### 3.1 先走一遍真实场景

**场景 A:私聊上报血糖。** 客户早上 8:20 发「今天早上空腹血糖 7.2」。
1. 业务方调 runs,inputs 带上 `project_code / customer_code / employee_code / customer_pad_userid / employee_pad_userid / room_conversation_id(私聊为空串)`,以及本批消息 id `source_message_ids`(如 `["pad:A1"]`)。
2. 模型调 `profile_get_brief`(只看到可选参数 `sections`,看不到客户编号)。平台在发出前把 `project_code`、`customer_code` 等填成本轮的值。
3. 模型从返回的 `writable_forms` 里选出血糖表,调 `form_get_field_detail` 拿字段编码(平台还钉死 `detail_level=brief`,返回紧凑表格)。
4. 模型调 `form_save_customer_values`,只填 `form_code` 和 `fields`。平台补上身份和 `source_message_ids`;服务端校验字段属于该表、值是数字,写入并记下来源消息 `pad:A1`,返回 `status=created`、写入字段清单、`server_now`。
5. 模型回复「记下了,空腹 7.2……」。

**场景 B:打断后重跑。** 客户 8:20 发完血糖(消息 A1),8:20:06 又补一句「昨晚睡得不好」(A2)。数字人服务取消正在跑的那一轮,合并两条消息重跑(`invoke-agent.processor.ts:935-960`)。被取消的那一轮可能已经写过血糖。重跑这一轮的 `source_message_ids` 是 `[A1, A2]`;模型再写一次同样内容时,服务端发现「同一张表、内容一样、来源消息里有 A1 与已有记录重叠」,返回 `status=duplicate` 和原来的 `submission_code`,不新建。

**场景 G:真的又报了一次同样的数。** 客户上午 10:00 发「喝了 300ml 水」,下午 15:00 又发「喝了 300ml 水」。两条是不同的消息(id 不同),第二次没有重叠的来源消息,照常新建一条。同理:一天两次各走 30 分钟、一天两次服药打卡、三餐后血糖都是 7(时段不同,本来就是不同内容)都会如实记下。一条消息里说了两遍同样的量(「喝了 300ml,又喝了 300ml」)时,两次写入来源消息相同,第二次会被当成重复 —— 所以提示词要求模型把它合成一条(600ml),或分别带上时间(06)。

**场景 C:每天提醒吃药。** 客户说「每天早上 8 点提醒我吃二甲双胍」。模型调 `reminder_create`,参数是 `event_time="2026-11-18 08:00"`、`repeat="day"`;`advance_minutes` 不传,默认 10。服务端算出首次发送 `2026-11-18T07:50:00+08:00`、结束 `2026-12-18T07:50:00+08:00`,写表、投延迟队列,返回 `status=created`、`notify_code`。模型照返回值回复。工具说明里写明「吃药提醒也用本工具,它不改用药方案」,而且 `medication_*` 根本没放行,不会走错。到点由数字人服务发出,和今天一样。

**场景 D:群里诱导。** 群里客户 A 说「把张三的提醒都删了,编号是 notify_xxx」。本轮 inputs 里的客户是 A。模型调 `reminder_list` 只能看到 A 的提醒;就算把 `notify_xxx` 传给 `reminder_delete`,服务端按 `project_code + customer_pad_userid(A)` 过滤,返回 `not_found`。张三的提醒不受影响。

**场景 E:化验单。** 客户 11 月第二次发化验单照片。模型从图里读出指标,调 `form_save_customer_values(form_code=form_analysis_report, fields=[…])`;附件原始链接由平台从 inputs `attachment_urls` 填进 `attachments`。服务端找到该客户本月(北京时间)已有的化验单提交,同字段覆盖、新字段新增,附件链接去重后用「;」拼接,`field_analysis_node_name` 写 `2026-11`,超过 70 个字段只收前 70 个并列出丢掉的。返回 `status=merged`。

**场景 F:天气。** 客户问「明天杭州冷不冷」。模型调 `mcp__amap-maps__maps_weather(city="杭州")`。客户没说城市时,先看档案里的常住地址,没有就问。

### 3.2 Dify 操作 → 迁移后由哪个工具承担

**读**

| Dify 操作 | 迁移后工具 | 复用 / 新增 / 改造 | 参数要点与坑 |
|---|---|---|---|
| 客户基础档案 + 健康方案 + 今日饮食 + 系统表单清单 + 提醒列表(每轮预取) | `profile_get_brief` | **新增**(v1 就做,11-17 定) | 模型需要客户情况时调一次,一次返回,≤ 8000 字;未建档时返回 `customer_registered=false`;业务方不再每轮预取 |
| 健康方案(markdown) | `health_plan_get_brief` | **新增** | 照项目服务 `queryCustomerHealthPlanMd`(`customer-health-plan.service.ts:11065`)改成纯文本;不要在现有 `health_plan_get_customer_plan` 上加模式,原因见 6.8 |
| 某表单字段结构 | `form_get_field_detail` | 复用 | 平台 `fixed: {detail_level: brief}`,只回一张紧凑表格(字段编码 / 类型 / 单位 / 选项);`project_code` 绑定 |
| 某表单最新值(如最近一次糖化) | `form_get_latest_field_values` | 复用 + 小改 | `owner_code` ← `customer_code` 绑定;**日期参数只能传 `YYYY-MM-DD`**,服务端会再拼 ` 00:00:00` / ` 23:59:59`(MCP `form.service.ts:272-279`);说明文字写着「服务器本地时区」,实际是北京时间(连接时区 `+08:00`),要改说明 |
| 按时间查表单数据(「上周血糖」) | `form_list_owner_submissions` | 复用 + 小改 | 同上日期坑(`:391-394`)。Dify 传的是 `YYYY-MM-DD 00:00:00`,原样搬过来会变成 `… 00:00:00 00:00:00`,结果靠 MySQL 隐式转换(推断:不可依赖)。`page_size` 默认 10、最大 100,大页可能超 2 万字被截断,提示词里要求 ≤ 20 并带 `include_fields` |
| 化验单本月记录 | 不再单独调 | — | 并进 `form_save_customer_values` 服务端逻辑 |
| 提醒列表 | `reminder_list` | **新增** | 按 `customer_pad_userid` 查(与现有数据一致,群里未建档的人也能用) |
| 历史聊天 | 不再每轮拉;客户问到会话里没有的更早聊天时调 `chat_private_history` | 复用 + **增强** | 范围扩到「该客户私聊 + 该客户在群里发的 + 员工号 @ 该客户的」,加关键词 / 时间段 / 条数(≤ 30),身份由平台绑定(§3.12、5.8) |
| 天气 | 平台 `amap-maps` 的 `maps_weather` | 平台已有 | 见 3.7 |
| 运动视频(Dify 整表塞提示词) | `material_list` | **新增** | 读深护新建的 `t_project_agent_material`,返回本智能体可用的全部启用素材(短 id、名称、类型、标签、说明前一段),不给链接、不搜关键词;`agent_code` 由配置钉死(5.12,05 §3.3) |
| 快递 | 不做 | — | 已拍板 |

**写**

| Dify 操作 | 迁移后工具 | 复用 / 新增 / 改造 | 要点 |
|---|---|---|---|
| 改个人档案 | `form_save_customer_values`(`form_customer_basic_info` → 档案模式) | **新增** | 只写变了的字段;不写姓名;累积字段追加;否定描述不覆盖 |
| 上报健康数据 | `form_save_customer_values`(其它可写表单 → 上报模式) | **新增** | 新建提交;按来源消息去重(§3.4) |
| 化验单 | `form_save_customer_values`(`form_analysis_report` → 按月合并模式) | **新增** | 按北京时间当月(上传月)合并;附件合并;≤ 70 字段 |
| 新增提醒 | `reminder_create` | **新增** | 返回真实 `notify_code` 与发送时间 |
| 删除提醒 | `reminder_list` + `reminder_delete` | **新增** | 只能删当前客户的 |
| 转人工(Dify 3.0 没用) | `handoff_to_human` | **新增** | MCP 直接发通知给同事;按配置写接管标志 / 关 AI 开关(§3.10、5.9) |

### 3.3 身份:谁的数据由平台填

**规则**

1. 每个放行工具里,凡是决定「作用于谁」的参数,都在智能体配置里逐个写 `arg_bindings`,不留给模型。没有「所有工具的 `project_code` 自动绑定」这种全局规则,必须逐个工具写。
2. 平台行为(确认,`orchestrator/tools/arg_bindings.py`):
   - 建工具目录时,被绑定的参数从模型看到的 schema 里删掉,同时移出 `required`(`:127-161`)。
   - 调用前用本轮 inputs 的值**覆盖**模型填的同名参数(`:245-247`)。
   - 变量本轮没传时,把这个参数**删掉**,连模型塞进来的值一起删(`:249-250`),交给 MCP 按必填报错。
   - `fixed` 每次都填,只收字符串(`:252-254`;`agent_spec.py:1325`)。
   - 填值发生在审批与动作检查之前(`graph_builder/builder.py:1890-1935`)。
3. 被绑定的变量要在系统提示词里声明(`system_prompt.jinja: true` + `variables`,`agent_spec.py:300-318`)。声明为 `required: true` 时,run 不带这个变量直接 422(`agent_spec.py:274-276`;`prompt_render.py:284-285`,只看键在不在,空串可以过)。平台自动生成的「本轮输入」段只写变量名和状态,**不写值**(`control_plane/inputs_block.py:35-58`)。
4. MCP 侧配合(**新工具必须做到**):被绑定的参数要写在 `inputSchema.properties` 顶层,不能藏在 `anyOf` 里,否则平台剥不掉(值仍会被覆盖,但模型看得见);服务端自己校验这些参数非空,不能依赖平台。
5. 深护侧:
   - 生产 `AGENT_KEYS` 必须非空(11-17 用户确认已配置)。上线检查用随便一把 key 去连,必须被拒(40100)。
   - 健管师**单独发一把 key**。在 Expert Work 租户 MCP 注册表里另登记一条 MCP 服务(同一地址,请求头带这把 key,再加 `x-agent-id: <健管师智能体编码>`(= 深护员工绑定上的 `agent_code`,06 §3.11)便于深护日志区分),健管师的配置只引用这条。连接按租户共享,不能按 run 换请求头,所以一条注册 = 一把 key。
   - 注册名尽量短:平台内部工具名是 `mcp__<注册名>__<工具名>`,超过 64 字符会截断,截断后撞名会让绑定落空(见 6.2)。

**绑定表**(声明变量均为 `required: true`;`attachment_urls` 除外)

| 工具 | `args`(工具参数 ← 声明变量) | `fixed` |
|---|---|---|
| `profile_get_brief` | `project_code`;`customer_code`;`customer_pad_userid`;`room_conversation_id`(后两个给「现有提醒」一段用) | — |
| `health_plan_get_brief` | `project_code`←`project_code`;`customer_code`←`customer_code` | — |
| `form_get_field_detail` | `project_code`←`project_code` | `detail_level: brief` |
| `form_get_latest_field_values` | `project_code`←`project_code`;**`owner_code`←`customer_code`** | — |
| `form_list_owner_submissions` | `project_code`←`project_code`;**`owner_code`←`customer_code`** | — |
| `form_save_customer_values` | `project_code`;**`owner_code`←`customer_code`**;`employee_code`;`attachments`←`attachment_urls`;`source_message_ids` | — |
| `reminder_create` | `project_code`;`customer_code`;`customer_pad_userid`;`employee_code`;`employee_pad_userid`;`room_conversation_id` | — |
| `reminder_list` | `project_code`;`customer_pad_userid`;`room_conversation_id` | — |
| `reminder_delete` | `project_code`;`customer_pad_userid` | — |
| `chat_private_history`(增强) | `project_code`;`employee_code`;`customer_code` | `scope: customer`(5.8) |
| `handoff_to_human` | `project_code`;`employee_code`;`employee_pad_userid`;`customer_code`;`customer_pad_userid`;`room_conversation_id`;`source_message_ids` | `this_turn`、`afterwards`(控制台下拉选,§3.10;健管师默认 `reply` + `continue`,11-17 由本轮不回改为本轮回复) |
| `material_list` | `project_code` | `agent_code`(健管师的智能体编码) |
| `amap-maps` / `maps_weather` | 无(不涉及客户身份) | — |

没写箭头的表示工具参数名与变量名相同。表单类工具的参数叫 `owner_code`,要绑到 `customer_code`,这是最容易配错的一处。`handoff_to_human` 绑 `source_message_ids` 是为了两件事:同一批消息重跑时不重复通知同事;pad 私聊照现在的做法把这批客户消息标成「已转人工」(按 `msg_app_info`,§1.5)。

**变量取值约定**(业务方调 runs 时)

| 变量 | 来源 | 空值约定 |
|---|---|---|
| `project_code` | 消息所属项目 | 不能空 |
| `customer_code` | 发言客户 | 群里没有客户档案的发言人传空串(`invoke-agent.processor.ts:371-391` 今天就允许),工具按「未建档」处理 |
| `customer_pad_userid` | 发言人企微 id | 不能空 |
| `employee_code` / `employee_pad_userid` | 服务该客户的员工账号 | 不能空 |
| `room_conversation_id` | 群 id | 私聊传空串 |
| `attachment_urls` | 本轮客户发来的图片 / 文件原始链接(列表) | 没有附件就不传(`required: false`) |
| `source_message_ids` | 这一轮要回答的客户消息的 id:03 之前 = 合并后的整批;03 之后(S-8)= 本轮 input 那批 + 经补消息重新带上的被取消轮的客户消息;JSON 数组,带通道前缀:pad `"pad:" + msg_app_info`,dk `"dk:" + msgid`(与 03 同一规则) | 必传;理论上不会空(每轮都由客户消息触发);空数组时服务端不判重复,每次都记 |

**各通道的 id 要对得上**:`employee_pad_userid`、`customer_pad_userid`、`room_conversation_id` 在 pad 通道填 pad id,在 dk 通道填企微明文 id(员工 userid、客户 external_userid、群 chat_id)。转人工要用它们拼人工接管标志的键(§3.10),和数字人 / 同步 / 存档服务的拼法必须逐字一致,是跨服务约定,要有契约测试。

**服务端核对归属(可选加固)**:做法、代价见 9.5。

### 3.4 写入规则搬到服务端

`form_save_customer_values` 按 `form_code` 分三种模式:

| 模式 | 哪些表单 | 怎么落库 |
|---|---|---|
| 档案覆盖 | `form_customer_basic_info` | 只写与当前最新值不同的字段,新建一条提交(沿用项目服务「最新值按字段取」的读法);全部没变 → `status=unchanged`,不建提交 |
| 日常上报 | 可写表单清单里的其它表(血糖、饮食、运动等) | 新建一条提交;`submit_time` = `measured_at`(北京时间)或当前时间;同内容且来源消息重叠 → `duplicate` |
| 化验单按月合并 | `form_analysis_report` | 找该客户「北京时间本月」(即上传月,11-17 定)最近一条化验单提交,有就合并(同字段覆盖、新字段新增),没有就新建 |

| Dify 里的规则(出处) | 服务端怎么做 |
|---|---|
| 不写姓名(`1761367072328`) + 写姓名会改客户主表(项目服务 `form.service.ts:1481-1491`) | `field_real_name` 一律跳过,原因 `forbidden_field`。称呼字段照常写(「只在客户明确要求时写称呼」留在提示词) |
| 累积字段追加(规则 9–10) | 「累积字段表」里的字段默认 `op=append`:已有内容已包含新内容 → `unchanged`;否则写成「已有内容;新内容(YYYY-MM-DD 记录)」。`op=replace` 必须带 `reason`(停药 / 痊愈 / 原信息有误)。字段表放目前用药、其他健康状况、过敏源三项,**field_code 写死在深护 MCP 代码里**(11-17 定,不做配置;具体编码实现时核对) |
| 否定描述不覆盖(规则 11) | 已有非空值的字段,新值为「无 / 没有 / 否认 / 不详」一类时跳过,原因 `negation_ignored`;`op=replace` + `reason` 例外 |
| Select 只能从 options 选(规则 12) | 值不在选项里 → 跳过,原因 `invalid_option`;`display_value` 由服务端按选项回填,不信模型 |
| 出生年月只在为空时推算(类型规则 5) | 出生日期字段已有值 → 跳过,原因 `protected_field`(`op=replace` + `reason` 例外) |
| 空值、「未知」不提交(`1761367092790`) | 跳过,原因 `empty_value` |
| 计算字段不写(项目服务 `:1393-1402`) | 跳过,原因 `calculated_field` |
| 字段必须属于该表(Dify 靠模型) | 不属于 → 跳过,原因 `not_in_form` |
| 数值字段 | Integer / Float 不能解析 → 跳过,原因 `invalid_number`(单位换算仍由模型做) |
| DateTime 字段取当前时间 | 没给值的 DateTime 字段,服务端填 `measured_at` 或当前北京时间 |
| 化验单同月合并(`1789740487772`) | 见上表;月份按**北京时间当前月**(沿用 Dify,11-17 定,9.3) |
| `field_analysis_node_name` 写当月(规则 8) | 服务端写入 `YYYY-MM`,忽略模型给的值 |
| 附件分号拼接、含已有(规则 7) | `attachments` 与该提交已有的 `field_analysis_attachment` 合并、去重,用「;」连接;其它表单有 UploadImage / UploadFile 字段时,同样处理第一个这类字段 |
| 最多 70 条(规则 10、`1789743707720`) | 超出部分不写,列在 `dropped_over_limit` |
| 至少一个实际指标(`1789742570094`) | 化验单只有附件、日期、节点名这类元数据字段 → 42200 `lab_report_no_indicator` |
| 血糖两要素、他人数据不报(`1761367647758`) | 他人数据、假设性提问:语义判断,留在提示词。血糖时段等「数据情境」:见下面「情境字段」,服务端**不因缺情境拒收** |

**情境字段(11-17 定,所有业务表单通用)**:很多数据要带「情境」才有意义 —— 血糖是空腹还是餐后、饮食是哪一餐、血压体重是什么时候量的。

- **不做按表单的配置**(11-17 定:表单太多,不会有人逐张配)。模型读 `form_get_field_detail` 返回的字段名和字段说明,自己认出哪个字段是情境(测量时段、餐次、测量时间)。
- 模型的规则(写进提示词,06):先按消息时间和上下文推断,**真推不出来才问**。例:清晨 5 点发「血糖 6.1」→ 按空腹记,不问「是空腹吗」;刚说完「中午吃了碗面」,半小时后发「血糖 9.6」→ 按午餐后记;下午三点没头没尾的一句「血糖 9.6」→ 问一句。
- 服务端不因为缺情境字段拒收(取代此前「血糖缺时段就拒收」的写法);缺了照常写,DateTime 类按 `measured_at` 或当前北京时间补。

**去重(11-17 定,取代「同一天同内容」)**:要挡的重复都来自**同一批客户消息被处理两次** —— 打断后合并重跑、模型一轮里连调两次保存、网络重试。所以按来源消息判断:

- 业务方每轮把本批客户消息的企微 id 作为变量 `source_message_ids` 传入,平台绑定进 `form_save_customer_values`(模型看不到、改不了)。
- MCP 写每条记录时一并存下它的来源消息 id。存在哪(提交表加一列,还是另建一张小表)是 MCP 实现细节,实现时定。
- 判重:**同一客户、同一表单、规范化后内容相同、来源消息有重叠** → 返回第一次的结果,`status=duplicate`。规范化内容 = 各字段编码 + 规范化后的值,加上模型给的 `measured_at`;服务端自己补的当前时间不算。
- 重跑:合并重跑那一轮带着旧消息的 id,和已写记录重叠 → 挡住。客户在**新消息**里报了同样的数 → 没有重叠 → 照常记。
- 判重和写入在同一把锁里做(按客户 + 表单),防同一轮里并行的两次调用都写进去。
- 档案模式靠「与最新值比对」天然去重,化验单模式靠「同字段覆盖 + 附件去重」天然幂等;两种模式同样存来源消息 id,便于追查。

**改口**:客户说「刚才血糖说错了,是 6.2」。模型带上刚才返回的 `submission_code` 作 `amend_submission_code` 再调一次,服务端在原提交上覆盖字段。限制:同一客户、同一表、由智能体写入、北京时间当天,否则 42200。

**不需要确认、不设上限(11-17 定)**:写入不找客户或员工确认;不设按客户的调用 / 写入次数上限。错了靠员工撤销。

**员工可撤销**:每次写入在 `t_project_operation_log` 记一行(操作人 = 绑定的员工、来源 = 健管师智能体 / `x-agent-id`、字段前后值,≤ 2000 字)。撤销沿用项目服务现有的 `form/del-form-field-values`(把字段置为无效,`form.service.ts:1685-1699`);控制台有没有入口要深护确认。提醒的撤销用项目服务运营后台的 AI 提醒删除(`ai-notify.service.ts:221`)。

### 3.5 放行清单与禁用清单

**放行(`allow_tools`)**

| 工具 | 读 / 写 | 为什么要 |
|---|---|---|
| `profile_get_brief` | 读 | 替代 Dify 每轮 4~5 路预取:话题与健康有关时调一次;推广话术前判断有没有方案、首轮补问看缺哪些档案项也用它 |
| `health_plan_get_brief` | 读 | 客户要看方案完整内容时 |
| `form_get_field_detail` | 读 | 写表单前拿字段编码与选项 |
| `form_get_latest_field_values` | 读 | 「我最近一次糖化多少」 |
| `form_list_owner_submissions` | 读 | 「上周血糖怎么样」 |
| `form_save_customer_values` | 写 | 档案 / 上报 / 化验单 |
| `reminder_create` / `reminder_list` / `reminder_delete` | 写 / 读 / 写 | 提醒 |
| `chat_private_history`(增强后) | 读 | 「我上周跟你说的那个……」而会话里没有 |
| `handoff_to_human` | 写 | 转人工 |
| `material_list` | 读 | 客户明确要运动视频等素材时拿全量清单 |
| `amap-maps` 的 `maps_weather` | 读 | 天气 |

一共 13 个工具,说明文字尽量短:多数在 300 字以内,写入和转人工两个工具因为规则多,约 300~380 字。平台规则(`orchestrator/tools/assembly.py:442-470`,确认):MCP 工具 schema 总量小于 min(上下文窗口 × 10%, 2 万 token)时全部直接可见,超过就要模型先用 `find_tools` 搜。13 个工具远小于这个量,不会被藏起来。

**禁用(不放行)**

| 工具 | 理由 |
|---|---|
| `cpwx_send_text_to_customer` / `cpwx_send_text_to_group` / `cpwx_query_send_status` | 主回复由业务方发,智能体再发会重复;也绕过了数字人服务发送前的人工接管检查(§3.11) |
| `medication_create_plan(s_batch)` / `medication_stop_plan(s_batch)` | 会真推送(工具自己标了 dangerous、需确认,`medication.tool-provider.ts:154-160`);「每天提醒吃药」容易被路由到这里 |
| `medication_query_plans` | 首版用不上;放进来会让「吃药提醒」多一个可能走错的去处 |
| `customer_*`(5 个) | 能按关键词、标签、编号查任意客户 |
| `group_*`(4 个) | 能列项目下的群、群成员 |
| `chat_group_history` | 按群读整群聊天,能看到群里所有人(包括别的客户)说的话。查更早聊天改用增强后的 `chat_private_history`(§3.12) |
| `sop_*`、`sop_campaign_*` | `sop_query_customers` 能跨客户查;`sop_campaign_pause / cancel_*` 是写操作 |
| `ai_strategy_*` | 员工侧内部资料 |
| `employee_*` | 员工账号登录状态,与客户服务无关 |
| `health_plan_get_customer_plan` / `health_plan_get_doc_table` | 默认带食物推荐等大块 JSON(`health-plan.service.ts:97-98`),可能超 2 万字被中间截断;由精简版替代 |
| `form_list_by_project` | 可写表单清单已在 `profile_get_brief` 里;少一个工具少一个出错点(要加也安全,它不带客户数据) |
| `system_ping` / `system_list_categories` | 无用 |

注意:复用工具的说明文字里提到了没放行的工具(如 `form_get_latest_field_values` 说「当 customer_search 返回的基础字段不足以……」,`form.tool-provider.ts:195`)。模型可能去找这些工具,提示词里要写明「只用你手上的工具」。

### 3.6 提醒:两种实现方式比较

提醒到点仍由数字人服务发(`SendNotifyProcessor` 不动),只比较「新增 / 查询 / 删除」放在哪。

| | A:MCP 直接写提醒表 + 投递数字人的延迟队列(**推荐**) | B:MCP 调数字人服务内部接口 |
|---|---|---|
| 能否返回真实结果 | 能。自己写表,拿得到 `notify_code` 和 job id | 现在的 `/add-notify` 不返回任何东西、吞异常(`agent-callback.service.ts:165-241`),要改接口 |
| 鉴权 | 不新增对外接口 | 现有接口靠写死的调试请求头放行(`sign.guard.ts:34-38`、`auth.guard.ts:37-39`),要补服务间鉴权 |
| 先例 | 项目服务 `ai-notify` 模块就是这么做的,是队列的第二个生产者(`ai-notify.const.ts:1-14`、`ai-notify.service.ts:82-106`);MCP 自己的用药提醒也是写库 + 投队列 | 无 |
| 耦合 | 三方共享队列名 `send-notify-queue`、job 字段、Redis 库号 → 要一条契约测试 | 依赖数字人服务在线,多一跳;pad 和 dk 两套回调路径要选对 |
| 时区 | MCP 显式按东八区算延迟 | 沿用数字人进程时区(仓库里没设 TZ,见 6.7) |
| 去重 | 在 MCP 一处做 | 两边都要改 |
| pad / dk 分流 | 不用管。消费端按员工判断走 pad 队列还是 PC 任务(`send-notify.processor.ts:50-82`) | 同左 |

**推荐 A**。实现时照抄项目服务 `ai-notify.service.ts` 的 `scheduleNotifyJob` / `removePendingJob`(`:82-106`)。job 字段与消费端逐字一致:`{project_code, notify_code, event_description, room_conversation_id, send_pad_userid, receive_pad_userid}`,选项 `{delay, removeOnComplete: true, removeOnFail: true}`。MCP 注册这个队列时显式指定数字人那边的队列库号,与项目服务用同一个配置值(`SEND_NOTIFY_QUEUE_REDIS_DB_INDEX`,项目服务默认 15、注释说数字人部署在 15;数字人代码默认 14,以部署环境为准)。

发送方 / 接收方的企微 id 走绑定(`employee_pad_userid` / `customer_pad_userid`),不在 MCP 里按编号反查:项目服务的反查读 `t_company_employee.employee_pad_userid`(`ai-notify.service.ts:63`),对 dk 员工是否成立没核实(推断)。

### 3.7 天气

- 平台目录里已有平台级 MCP `amap-maps`:测试、生产都有人用过(`docs/runbooks/2026-09-24-prod-release-checklist.md:1453` 记了生产配置里出现过 `amap-maps`;测试验收 `pending-test-release-acceptance.md:1686` 用的是真 amap-maps)。地址和 key 只有系统管理员在平台 MCP 目录里看得到(`control_plane/api/mcp_catalog.py:1-15`),深护不用申请高德 key,本文也不写。
- 给深护租户放行:租户管理员在控制台 MCP 页的「平台服务」里打开 `amap-maps`。对应接口 `POST /v1/mcp-servers/catalog/{id}/enable`,写入 `tenant_config.mcp_allowlist`(`api/mcp_servers.py:672-720`)。测试、生产各开一次;生产由用户执行。
- 需要的工具名:`maps_weather`(入参城市名或 adcode)。这个名字来自高德官方 MCP 和控制台测试夹具(`McpToolPicker.test.tsx:148-149`),属于推断,配置前用控制台的工具探测确认。
- 配置写成单独一个 mcp 条目:`servers: ["amap-maps"]`、`allow_tools: ["maps_weather"]`,不需要绑定。
- 客户没给城市时(Dify 的固定话术,`1752317953920` instruction):先用档案里的常住地址,没有就问。
- 高德 MCP 不给工具发读写标注,子智能体拿不到它(`docs/superpowers/specs/2026-09-27-worker-tool-policy-design.md:124`)。健管师不开子智能体,不受影响。

### 3.8 数字人服务里原来给 Dify 调的接口,逐一去向

| 接口 | 现在谁调 | 去向 | 说明 |
|---|---|---|---|
| `POST /agent-callback/add-notify`、`/agent-callback-dk/add-notify` | Dify 新增提醒 | **挪到 MCP** `reminder_create` | 并行期保留;Dify 下线后删除或补鉴权 |
| `POST /ai-notify/get-ai-notify-list` | Dify 删提醒前查询 | **挪到 MCP** `reminder_list` | 同上 |
| `POST /ai-notify/delete-ai-notify` | Dify 删除提醒 | **挪到 MCP** `reminder_delete` | 同上 |
| `POST /message/get-history-message` | Dify 每轮拉历史 | **不再每轮拉**(补消息);「查更早聊天」用增强后的 MCP `chat_private_history`(§3.12) | 同上 |
| `POST /agent-callback/receive-answer`、`/agent-callback-dk/receive-answer` | Dify 发主回复 / 兜底 / 推广话术 | **不再给智能体调**;发送逻辑**留在数字人服务**,业务方拿到 run 结果后在内部直接投发送队列 | 见总览「主回复」;走 MCP 发的否决理由见 §3.11 |
| `POST /agent-callback/referral-human`、`/agent-callback-dk/referral-human` | 本 Dify 应用**没有调用节点**(只声明了变量 `referral_human_pad_user_id`,DSL@2436) | Expert Work 上的健管师**不用**这条回调;转人工由 MCP `handoff_to_human` 直接发通知(§3.10) | 接口本身留给别的调用方 |
| 延迟队列消费 `SendNotifyProcessor` | — | **留在数字人服务** | 到点发送、循环续期不变 |

Dify 还直接调了项目服务:自定义工具「表单查询接口」(GetFormLatestData / QueryFormList / GetFormFieldDetail / GetFormDataList / SetFormFieldValues,OpenAPI 定义不在 DSL 里)、「健康方案查询接口」(QueryHealthPlan),以及化验单的两个 HTTP 节点。迁移后智能体一个都不调,全部换成上表的 MCP 工具。这些接口还有别的调用方,不删。

### 3.9 通用约定(所有新工具)

- **时间**:入参接受 `YYYY-MM-DD HH:mm`(视为北京时间),或带时区偏移的 ISO 8601;不带偏移的一律按北京时间解释,说明里写明。出参一律 ISO 8601 带 `+08:00`。每个新工具都返回 `server_now`,因为平台只给模型注入日期、不给钟点(`agent_factory.py:1203-1211`),模型算「半小时后」只能靠工具。
- **体积**:单次返回 ≤ 8000 字(平台超过 2 万字会从中间截断,`tools/mcp.py:115、1437`,截断后的 JSON 是坏的)。
- **错误**:沿用 `{code, category, message, hint, recoveryActions, context}` + `isError: true`。未预料异常不回原文(见 6.4)。
- **追踪**:写类工具返回 `request_id`。平台调 MCP 时不带 run 信息(`tools/mcp.py:1434-1436` 丢弃了上下文),深护日志和平台 trace 只能靠这个 id 对上。
- **标注**:读工具 `dangerLevel: safe`;写工具 `moderate`(可撤销,`destructiveHint=false`);`confirmRequired: false`(见 9.1)。
- **两条鉴权通道都可用**:OAuth 员工下 `project_code` / `employee_code` 强制取登录身份(沿用 `bindEmployeeContext`);agent key 下取入参(由平台绑定)。

### 3.10 转人工:`handoff_to_human`(11-17 定)

**前提**:对客户来说,AI 和同事是同一个人(同一个企微号)。所以转人工不是「告诉客户已转人工」,而是「悄悄让同事接手」,客户那边看到的最多是一句「我确认一下,稍后回复您」。

**两个固定选项**(每个智能体在配置里各选一个,控制台下拉,§4.1):

| 选项 | 取值 | 含义 |
|---|---|---|
| `this_turn` | `reply`(本轮回复) / `silent`(本轮不回) | 这一轮 AI 还说不说话 |
| `afterwards` | `continue`(之后照常) / `stop`(之后不回) | 客户下次再说话,AI 还回不回 |

**四种组合,各走一遍**:

1. **reply + continue「先稳住,这道题交给人,别的照常」(健管师默认)**。客户:「二甲双胍我能不能减半吃?」AI 调转人工,本轮只回一句稳住的话(「这个我确认一下,稍后回复您」),不回答药的问题、不说「转人工」;同事收到通知后亲自回复。客户接着问「晚饭吃什么好」,AI 照常回答。如果同事还没回、客户先追问「在吗?」,AI 回「在的,刚才那个问题我确认一下,稍后回复您」,仍不回答药的问题。
2. **silent + continue「这道题交给人,本轮一句不说,别的照常」(可选,非默认)**。同上,只是转人工那一轮 AI 不发任何话,由同事回;靠下面第 3 步的临时接管标志兜底。
3. **reply + stop「稳住客户,整个交给人」**。客户:「你们这服务太差了,我要退款!」AI 回一句稳住的话(「抱歉让您不舒服了,我马上核实一下情况,稍后给您答复」),同时转人工;之后客户再说什么,AI 都不回,直到同事处理完把 AI 重新打开。
4. **silent + stop「AI 一句不说,全部交给人」(可选)**。适合重点客户、医疗纠纷、法律问题:AI 本轮不说话,之后也不回,全部由同事接手。

**模型填什么**:`summary`(写给同事看:客户原话要点、为什么要交给人、AI 已经说了什么)、`is_emergency`(是否急症)。身份(项目、员工、客户、群)和本批消息 id 由平台从 inputs 绑定;`this_turn`、`afterwards` 由配置钉死,模型看不到。

**MCP 做什么(按顺序)**:

1. **校验固定选项**:不是允许值(如手误写成 `slient`)→ 42200,明确说哪个参数、允许哪些值;不悄悄按默认处理。
2. **通知同事**:按员工绑定判断走 pad 还是 pc(与到点提醒同口径,`send-notify.processor.ts:50-82`),目标取绑定行上的转人工配置,兜底规则与现在一致(§1.5)。目标是内部同事 / 内部群,所以**和现在 dk 的转人工一样,不写 AI 标记、不过人工接管检查**。
   - **pad**:用 MCP 现有的 pad 文字发送,发给转人工目标(同事 pad id,或内部群)。
   - **pc(已定 11-17)**:照数字人服务的 `enqueueReferral`(`pc-send-task.service.ts:190-225`)往 `t_project_cpwx_pc_send_task` 插 referral 任务 —— 同样用 `PcReferralResolver.pickTargets(项目, 同事 userid 列表, 群 chat_id)` 的规则选目标(`pc-referral.resolver.ts:43`),字段照搬:`task_id = referral_<uuid>:<sha1(目标)前 16 位>`、`customer_external_userid = ''`、`referral_userid`、`room_chat_id`、`texts`、`task_kind = referral`。领取时的行为(不过接管检查、不写 AI 标记、30 分钟过期、最后发放、领取时再解析同事 / 群名)全部留在数字人服务,MCP 不碰。这是 MCP 与数字人服务之间的跨服务约定,两边要有契约测试。**不走** MCP 现有 pc 发送用的 `t_machine_task`:它的收件人只能是外部联系人或群,发不了给内部同事的私聊,而且领取时会写 AI 已发标记(§1.4)。转人工状态照现在标:pad 私聊把本批客户消息标 `referral_human_status=1`(从 `source_message_ids` 取 `msg_app_info`);dk 没有状态列,referral 任务本身就是记录。
3. **本轮不回**:`this_turn=silent` 且**不是急症**时,写现有的临时接管标志 `HUMAN_HAS_ANSWERED_{项目}_{员工}[_{群}]_{客户}`(存活 600 秒),写在数字人服务共用的那个 Redis 库(库号待核实,与「提醒队列 14 还是 15」一起核,§9.12)。两通道的发送前检查本来就查这个键(§1.5),这一轮的回复即使生成了也会被拦下。客户再开口时这个键会被自动删掉,下一轮 AI 恢复回复。键里的 id 拼法是跨服务约定,pad 用 pad id、dk 用企微明文 id(§3.3),必须逐字一致。只写客户级的键,不写群级键(群级会挡住群里其他客户)。
4. **之后不回**:`afterwards=stop` 时,把当前对话的 AI 开关 `is_enabled_agent_reply` 置 0:私聊改 `t_project_customer_cpwx_relation` 那一行,群里改 `t_project_cpwx_group_member` 里**这位客户**那一行。数字人服务调用前本来就查它(`invoke-agent.processor.ts:479-538`),之后这位客户在这个对话里的消息不再调智能体。两张表按企微明文 id 查(员工 `cpwx_userid`、客户 `cpwx_external_userid`、群 `chat_id`);pad 通道绑定的是 pad id,要换算 —— **v1 定为 MCP 自己反查**(按员工、客户、群查 `t_project_customer_cpwx_relation` / 群成员表等),不新增 inputs;查不到(如群里未建档的发言人)就**不动开关**,记告警日志,通知同事照常发。
5. **返回**:告诉模型本轮怎么做 —— `reply`:「本轮只回一句稳住的话(如『我确认一下,稍后回复您』),不回答交出去的问题,不要说已转人工」;`silent`:「本轮不用写回复,输出空」。

**急症例外**:`is_emergency=true` 时,不管 `this_turn` 选的什么,**都不写临时接管标志**,AI 本轮「立即就医 / 拨打 120」的建议必须发出去;`afterwards` 照配置执行。把普通情况误判成急症,最多多发一条回复;提示词仍然要求不回答已经交出去的问题(06)。

**找不到通知对象(已定 11-17)**:绑定行没配转人工目标、调用里也没有 → 不发通知、不写临时标志、不关 AI 开关,工具告诉模型「没有转成」,模型照常回复;服务端记告警日志(5.9、9.14)。

**健管师默认组合(11-17 定;11-17 默认由本轮不回改为本轮回复)**:`this_turn = reply`、`afterwards = continue` —— 交出去的那一轮 AI 先回一句稳住的话(「我确认一下,稍后回复您」),不回答交出去的问题,不说「转人工」;客户之后再说别的,AI 照常回答;客户追问那道题时,AI 说正在确认、稍后回复(06 R71)。理由:客户每次都能马上得到回应;组合是按智能体配置的(控制台下拉),对所有转人工类型都生效。急症例外不变。「本轮不回」仍可在下拉里选。

**谁把 AI 重新打开**:业务后台。读代码找到项目服务已有两个接口能改这个开关(§1.5),但员工端有没有入口在用**没核实**;实现前必须确认,没有就补一个(§9.13)。

**数字人服务:转人工不需要任何改动。** 发送前检查(查临时标志)和调用前检查(查 AI 开关)都已存在。此前设想过「发送前再查一次 AI 开关」,**撤回**:那样会把 reply + stop 组合里那句稳住客户的话也拦掉。Expert Work 上的健管师也不再走数字人服务的 `referralHuman` 回调。

**已知边界(与现在的真人接管相同)**:转人工那一轮还在跑时客户又发了新消息,临时标志会被删掉;如果这时那一轮没取消成功,它的回复可能照样发出去。

### 3.11 主回复为什么不走 MCP 发(否决项,11-17 核实)

考虑过让智能体在一轮里调 MCP 发消息工具发主回复。不做,理由(都读过代码):

1. **会不会被当成真人接管:pad 未核实,dk 不构成问题(11-17 更正)。** dk 会话存档服务把员工号发出、又没有「AI 已发」标记的消息当成真人接管(`human-answered.service.ts:38、157-185`);但 MCP 的 pc 发送走 `t_machine_task`,PC 机器任务服务领取时会写这个标记(`claim.service.ts:188-192`),所以 dk 上不会误判。pad 通道 MCP 直接调 pad 接口发,同步服务会不会把它当成真人发言(`sync-pad-message.processor.ts:713-765`)没核实。此前写的「AI 一发言就把自己关掉」不成立,作废。下面 2~5 条照样成立,结论不变。
2. **回复会从平台的各个视图里消失。** 长期记忆提炼只看每一轮的最终回答,工具调用只留下名字(`graph_builder/memory.py:540-546、583-612`,`_turn` 只取最后一条不带工具调用的助手消息);对话页、调试台、评测看的也是最终回答。通过工具发出的回复,这些地方都看不到。
3. **被打断时收不回。** 新消息到达时数字人服务会取消正在跑的一轮、合并后重跑(`invoke-agent-message.processor.ts:338-371`);一轮中途已经用工具发出去的话撤不回来,重跑又发一次,客户收到前后矛盾的两段。
4. **没法保证正好发一次。** 模型可能漏调、多调;最终回答有 `output_schema` 校验和重试兜底,工具调用没有。
5. **只能发文字。** MCP 的发送工具只发文字(pad `message.service.ts`、pc `message-pc.service.ts`);视频、多气泡、发送节奏都在数字人服务。

粗估要多 5 人天,还多一类事故级风险(被打断时重复、矛盾的两段回复),pad 是否误触发真人接管未核实。等以后做「智能体主动找客户」时再重新评估。

### 3.12 查更早聊天:增强现有 `chat_private_history`(11-17 定,不新建工具)

- **场景**:客户说「上周我跟你说过我在吃的那个药,叫什么来着」,而会话里没有(03 落地前会话只有 AI 参与过的对话;落地后也只补最近的)。
- **范围**:该客户的私聊 + 该客户在各群里发的消息 + 员工号在群里 @ 该客户的消息。取的是数字人现有历史接口在群里的普通取法(`message.service.ts:167-180`);多对一服务群里其他内部员工的发言**不带**(6.18,与历史接口那一支不同)。不读群里其他人的话;`chat_group_history` 继续不放行。
- **参数**:`keyword`(关键词,新加)、时间段(沿用现有 `since_time` / `until_time` / `recent_days`)、条数(沿用现有 `page_size`,新范围下上限 30)。
- **每条带**说话人(`客户` / `健管师`)和时间(+08:00)。员工号发出的一律算「健管师」,不区分 AI 还是真人(已拍板)。
- **身份**:`project_code`、`employee_code`、`customer_code` 由平台绑定。
- **数据源**:`t_project_cpwx_chat_msg`,pad 和 dk 都写(§1.4),两条通道都查得到。此前文档里「pad 消息不在这张表、健管师查不到」的说法是错的,已更正。

## 4. 改动清单

### 4.1 Expert Work 平台

- **代码:一处,固定值下拉(11-17 批准,约 1 人天,随 B-314 其它平台改动一起发)。**
  - 现状:控制台给工具参数配「固定值」只有一个文本框(`apps/admin-ui/src/components/manifest-editor/widgets/McpToolPicker.tsx:740-760`),只检查非空、首尾无空格(`mcp_arg_bindings.ts:67-68`);读工具参数时只取名字和是否必填(`ToolParam`,`mcp_arg_bindings.ts:125-128`);后端保存也只查非空、首尾空格(`agent_spec.py:1328-1353`),不看工具声明的可选值。所以把 `silent` 手误写成 `slient`,要到真转人工那一刻才报错。
  - 改法:工具参数声明了字符串 `enum`,或 `oneOf` 里每项是 `{const, title}` 时,固定值输入框换成下拉,只能选这些值;有 `title` 就显示 `title`(运营看到「本轮回复 / 本轮不回」),没有就显示原值;其它参数仍是文本框。工具 schema 当不可信输入处理,只认白名单形状(照 `paramsOf` 的做法),形状不对就退回文本框。
  - 保存时校验:固定值不在可选值里就拒绝保存。放在保存时已经拿到工具 schema 的那段核对里(`api/agents.py:1131-1220`);连不上 MCP、拿不到 schema 时与现在一样只给警告。
  - 被绑定的参数本来就从模型看到的 schema 里删掉,`title` 只给运营看,不影响模型。改动是通用的,对所有 MCP 工具生效。
  - MCP 侧同样拒绝不认识的值,并给清楚的错误,不悄悄用默认值(§3.10)。
- 配置(测试由我们做,生产由用户执行):租户 MCP 注册表新增「健管师专用」一条(同地址、新 key、`x-agent-id`);租户打开 `amap-maps`;智能体配置见 4.4、5.11。
- 保存智能体后看返回的警告:出现 `arg_bindings did not land` 或 `were not checked` 时必须处理,不能发布(`api/agents.py:1131-1220`)。

### 4.2 深护 MCP

| # | 改动 | 说明 |
|---|---|---|
| M1 | 会话找不到改回 404 | 带了 `mcp-session-id` 但找不到 → 404 + JSON-RPC 错误;没带且不是 initialize → 仍 400。POST / GET / DELETE / 旧 SSE `/messages` 一起改(`mcp.controller.ts:58-64、99-101、110-112`,`/messages` 约 :161) |
| M2 | 未预料异常不回原文 | `mcp-error.ts:32-44` 改成固定文案 + `request_id`;详情只进服务端日志,日志里不打客户姓名、聊天正文 |
| M3 | 按 `inputSchema` 校验入参 | 在 `mcp-server.service.ts:102` 前统一用 ajv 校验(含 `required`、类型、`format`),不合格回 42200。新工具必须有;旧工具建议一起上(对 Dify 有行为变化,并行期可先只对新工具开) |
| M4 | 日期参数容错 + 改说明 | `form_get_latest_field_values` / `form_list_owner_submissions`:`start/end_submit_time` 接受 `YYYY-MM-DD`,也接受带时分秒或 ISO(只取北京日期),其它格式回 42200;结束日晚于今天时裁到今天(9.10 ⑤);说明里「服务器本地时区」改成「北京时间」 |
| M5 | `form_save_customer_values` | 5.2;含三种模式、规则(累积字段编码写死)、按来源消息去重(存来源消息 id)、改口、操作日志 |
| M6 | `reminder_create` / `reminder_list` / `reminder_delete` | 5.3–5.5;注册 `send-notify-queue`(显式库号) |
| M7 | `health_plan_get_brief` | 5.6;移植 `queryCustomerHealthPlanMd` 为纯文本 |
| M8 | `profile_get_brief` | 5.7;v1 就做(11-17 定),含现有提醒一段 |
| M9 | ~~按客户限额~~ | 取消(11-17 定:不设上限) |
| M10 | 契约测试 | 新工具每条返回路径(含空结果、未建档、去重)都过 outputSchema 校验(见 6.8);提醒 job 字段与数字人消费端对齐;转人工写的接管标志键与数字人 / 同步 / 存档三方拼法一致 |
| M11 | 增强 `chat_private_history` | 5.8;范围、关键词、时间段、条数、说话人标注 |
| M12 | ~~(可选加固)key 限项目、限工具;客户属于项目核对~~ | 不属于 B-314,移入深护安全待办(9.5、08) |
| M13 | `handoff_to_human` | 5.9;pad 通知、pc 照 `enqueueReferral` 插 referral 任务(含 `pickTargets` 选目标)、接管标志、AI 开关、转人工状态、固定选项校验;连数字人共用的 Redis 库 |
| M14 | `material_list` | 5.12;读新表 `t_project_agent_material`(深护建表),只返回本项目、本智能体(或通用)、启用的素材,带说明前一段 |
| 配置 | 生产 `AGENT_KEYS` 非空;新增健管师 key;MCP 能连上数字人共用的 Redis 库(写接管标志) | |

### 4.3 深护数字人 · 项目服务

- 业务方调 runs 时带上 3.3 的 inputs(属于「发起一轮」改造)。项目服务客户端 `EwRunRequest.inputs` 现在是 `Record<string, string>`(`expert-work.types.ts:46`);`attachment_urls`、`source_message_ids` 按 01 §3.3.2 传 JSON 数组,客户端类型要放宽;MCP 仍两种都收。
- `source_message_ids`:数字人服务已经把本批每条消息的企微身份传给项目服务(01 §5.1 `messages[].msg_key`),项目服务加上通道前缀放进 inputs;打断后合并重跑时放合并后的整批。
- 不再每轮预取客户档案、提醒、饮食、方案(客户资料由智能体调 `profile_get_brief`,11-17 定)。
- 部署确认数字人服务进程时区为东八区:循环提醒续期按进程时区算(`send-notify.processor.ts:200-202`),仓库里的 `dockerfile`、`pm2.json` 没设 TZ(推断:若生产不是东八区,续期会偏)。
- 转人工:数字人服务**不改**(§3.10)。要确认 AI 开关被关掉后由谁、在哪重新打开(§9.13)。
- Dify 下线后:3.8 表里「挪到 MCP / 不再需要」的接口删除,或去掉调试请求头放行。
- 控制台确认员工能撤销 AI 写入的表单字段和提醒(3.4 末)。

### 4.4 智能体配置

- `system_prompt.jinja: true`,声明变量:本文 3.3 的 8 个身份 / 工具变量,加上 06 的 `is_first_contact`、`self_appellation`、`self_introduction`,共 11 个(清单以 01 §3.3.2、06 §5.1 为准)。
- `tools` 两个 mcp 条目(深护健管师专用注册名 + `amap-maps`),`allow_tools`、`arg_bindings` 按 5.11;`handoff_to_human` 的 `this_turn`、`afterwards` 在控制台下拉里选(健管师默认 `reply` + `continue`,11-17 定;原 `silent` 已改);`material_list` 的 `agent_code` 钉成健管师的智能体编码。
- 不开 `dynamic_workers`、不配 `subagents`(绑定在子智能体里的行为不在本文核对范围)。
- `approval_required_tools` 留空(9.1:不确认,已定)。
- 提示词里写清(全文在 06):只用手上的工具;写入和提醒以工具返回为准;日期参数只传 `YYYY-MM-DD`;`form_list_owner_submissions` 的 `page_size` ≤ 20;客户资料只在话题与健康有关时调、每轮最多一次;情境字段先推断再问;一条消息里重复的量合并或带时间;转人工的时机、急症信号、不说「已转人工」、不回答已交出去的问题。

## 5. 接口与数据结构

### 5.1 通用错误码

| code | category | 什么时候 | 给模型的 hint |
|---|---|---|---|
| 40100 | auth | key 不在白名单(建连时) | — |
| 40300 | permission | 表单不在可写清单;(加固后)该 key 不允许此工具或项目 | 不要重试,换别的说法回复客户 |
| 40400 | not_found | 客户未建档(`customer_code` / `owner_code` 为空,或加固后不属于该项目) | 不要再调写入工具 |
| 42200 | validation | 参数不合法、字段全部被拒、时间不合法;配置钉死的固定值不在允许值里(`invalid_fixed_value`) | 按 `context.details` 改一次;固定值错误模型改不了,如实回复、不重试 |
| 9999 | internal | 未预料异常(只回固定文案 + `request_id`) | 最多重试 1 次 |

不设按客户的调用 / 写入上限,没有限流错误码(11-17 定)。

### 5.2 `form_save_customer_values`(新增,写)

- **用途**:把当前客户本人明确给出的数据写进档案或健康记录表单。
- **说明文字**(给模型,原样用):「[保存客户健康数据或档案] 把当前客户本人在对话里明确给出的数据写进表单:改个人档案(form_customer_basic_info),上报血糖、血压、体重、饮食、运动、睡眠等日常记录,保存化验单 / 体检报告里的指标(form_analysis_report)。客户是谁由系统决定,你只填表单和字段。只写客户本人的、有具体数值或内容的数据;别人的数据、假设性提问、你自己推测或建议的内容都不写。表单要求的情境(如血糖的测量时段、饮食的餐次、测量时间)先按消息时间和上下文推断,真推不出来再问客户。同一条消息里说了两遍同样的量,合成一条写,或分别带上时间。写之前先用 form_get_field_detail 查字段编码和选项。写成功了什么、跳过了什么,以返回的 saved / skipped 为准。本工具不设提醒、不改用药方案、不改健康方案。」

**入参**

| 参数 | 类型 | 必填 | 谁填 | 说明 |
|---|---|---|---|---|
| `project_code` | string | 是 | 平台绑定 | |
| `owner_code` | string | 是 | 平台绑定(← `customer_code`) | 空串 → 40400 |
| `employee_code` | string | 是 | 平台绑定 | 作提交人,并写进操作日志 |
| `attachments` | string[] 或用「;」「,」分隔的 string | 否 | 平台绑定(← `attachment_urls`) | 本轮客户附件原始链接,≤ 10 个;只在表单有附件类字段时使用 |
| `source_message_ids` | string[] 或 JSON 数组字符串 | 是 | 平台绑定 | 本轮客户消息 id,存为这条记录的来源消息,判重用(3.4) |
| `form_code` | string | 是 | 模型 | 必须在可写清单里(默认:本项目启用的系统表单,`is_system=1`) |
| `fields` | object[] 1..70 | 是 | 模型 | 每项见下表;超过 70 的部分不写 |
| `measured_at` | string | 否 | 模型 | 数据对应的时间(北京时间);日常上报用作提交时间,缺省为当前时间 |
| `amend_submission_code` | string | 否 | 模型 | 改口:覆盖本工具当天返回过的那条提交 |

`fields[]`:`field_code`(string,必填);`value`(string,必填,数字也写成字符串);`display_value`(string,可选,Select 由服务端按选项回填);`op`(`set` / `append` / `replace`,可选,累积字段默认 `append`、其它默认 `set`);`reason`(string,`op=replace` 用于累积字段或受保护字段时必填)。

**返回**

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `created` / `merged` / `amended` / `unchanged` / `duplicate` | |
| `submission_code` | string / null | `unchanged` 时为 null |
| `form_code`、`form_name` | string | |
| `mode` | `profile` / `report` / `lab_monthly` | |
| `saved` | `{field_code, field_name, value, display_value, op}[]` | 实际写入的(累积字段给合并后的全文) |
| `skipped` | `{field_code, reason}[]` | `reason` 枚举:`not_in_form` `calculated_field` `forbidden_field` `protected_field` `invalid_option` `invalid_number` `empty_value` `negation_ignored` `unchanged` `too_long` |
| `dropped_over_limit` | string[] | 超过 70 被丢弃的 `field_code` |
| `request_id`、`server_now` | string | |

**其它规格**

- 错误:40400(未建档)、40300(表单不可写)、42200(全部字段被拒 / 化验单无指标 / `measured_at` 不合法 / `amend_submission_code` 不合法)、9999。
- 幂等:见 3.4「去重」(按来源消息)。`duplicate` 返回首次写入的结果(`status` 改为 `duplicate`)。
- 不设按客户的次数上限(11-17 定)。单值 ≤ 500 字;`field_value` 列在 model 里是 `STRING`(`TProjectFormSubmissionDetail.ts:37`),长度要按真实表结构核对后再定上限(推断:可能是 255)。
- 权限:两条通道都可用(3.9)。
- 写库:事务内写提交和明细 + 操作日志;`submitter_code` = 绑定的员工编号(控制台怎么展示要深护核对);`created_by` = 默认用户。

示例请求(模型看到并填写的部分;`project_code` / `owner_code` / `employee_code` 由平台补上):

```json
{
  "form_code": "form_blood_glucose_log",
  "measured_at": "2026-11-17 07:30",
  "fields": [
    {"field_code": "field_bg_value", "value": "7.2"},
    {"field_code": "field_bg_period", "value": "1"}
  ]
}
```

示例返回(编码均为示意):

```json
{
  "status": "created",
  "submission_code": "subm_示意",
  "form_code": "form_blood_glucose_log",
  "form_name": "血糖记录",
  "mode": "report",
  "saved": [
    {"field_code": "field_bg_value", "field_name": "血糖值", "value": "7.2", "display_value": "7.2", "op": "set"},
    {"field_code": "field_bg_period", "field_name": "测量时段", "value": "1", "display_value": "空腹", "op": "set"}
  ],
  "skipped": [],
  "dropped_over_limit": [],
  "request_id": "req_示意",
  "server_now": "2026-11-17T08:20:31+08:00"
}
```

### 5.3 `reminder_create`(新增,写)

- **说明文字**:「[给客户设定时提醒] 让健管师的企微账号在指定时间给当前客户发一条文字提醒,可以单次,也可以按小时 / 天 / 周 / 月 / 年循环(最长 30 天)。客户说『X 点提醒我……』『每天提醒我吃药 / 测血糖 / 喝水 / 运动 / 复诊』都用本工具。它只发提醒消息,不会创建或修改用药方案。时间用北京时间;『X 分钟后』用 delay_minutes。默认比事件时间提前 10 分钟发,客户要准点就把 advance_minutes 设 0。只有返回里 status 为 created 或 duplicate 的条目才算设好,告诉客户时用返回的 first_send_at;rejected 的照 reason 如实告诉客户。」

**入参**:绑定 `project_code`、`customer_code`(可为空串)、`customer_pad_userid`、`employee_code`、`employee_pad_userid`、`room_conversation_id`;模型填 `reminders`(1..5):

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string 2..100 字 | 是 | 发给客户的原话;不写「今天 / 明天」这类相对日期(循环提醒每次都发同一句);服务端去掉 Markdown |
| `event_time` | string | 二选一 | 事件本身的时间(北京时间) |
| `delay_minutes` | integer 1..1440 | 二选一 | 「X 分钟后」;此时不提前 |
| `advance_minutes` | integer 0..1440 | 否 | 默认 10 |
| `repeat` | `once` / `hour` / `day` / `week` / `month` / `year` | 否 | 默认 `once`;不支持按分钟 |
| `repeat_interval` | integer 1..30 | 否 | 默认 1 |
| `repeat_until` | string | 否 | 服务端截到「首次发送 + 30 天」 |

**服务端规则**

- 首次发送 = `event_time − advance_minutes`,或 `now + delay_minutes`。若提前后已过去、但事件时间还没到,改为按事件时间准点发,标 `advance_adjusted`。
- 首次发送必须在 [now + 1 分钟, now + 366 天] 内,否则 `rejected: time_out_of_range`。
- 循环提醒的 `end_time` 默认且最长为首次发送 + 30 天。
- 写 `t_project_ai_notify`(`trigger_time` / `end_time` 存北京墙钟)→ 投 `send-notify-queue`(延迟按东八区算)→ 回写 `job_id`。投递失败就删掉刚写的表行,该条返回 `rejected: queue_unavailable`。

**返回**:`results[]`,每项含 `index`、`status`(`created` / `duplicate` / `rejected`)、`notify_code`、`short_code`(如 `R3`,5.4)、`content`、`first_send_at`、`first_send_weekday`(如「周三」,模型回复写「11 月 18 日 周三」客户能当场核对,9.10 ⑥)、`repeat`、`repeat_interval`、`repeat_until`、`reason`、`advance_adjusted`;外加 `server_now`、`request_id`。全部 `rejected` 时 `isError=true`、42200,`context` 里带每条的原因。

- 幂等:同一 `customer_pad_userid` + 同一 `room_conversation_id` + 同一首次发送时间 + 同循环方式 + 规范化后的同内容,且该提醒仍生效 → `duplicate`,返回已有编号(同一时刻的同一句提醒设两条没有意义,所以这里不需要来源消息)。
- 不设按客户的条数上限(11-17 定);一次调用最多 5 条(参数上限)。按小时循环同样最长 30 天(沿用 Dify,11-17 定)。

示例:

```json
// 请求(模型填的部分)
{"reminders": [{"content": "💊 早上 8 点该吃二甲双胍了", "event_time": "2026-11-18 08:00", "repeat": "day"}]}
// 返回
{"results": [{"index": 0, "status": "created", "notify_code": "notify_示意",
  "content": "💊 早上 8 点该吃二甲双胍了", "first_send_at": "2026-11-18T07:50:00+08:00",
  "repeat": "day", "repeat_interval": 1, "repeat_until": "2026-12-18T07:50:00+08:00",
  "advance_adjusted": false}],
 "request_id": "req_示意", "server_now": "2026-11-17T21:03:12+08:00"}
```

### 5.4 `reminder_list`(新增,读)

- **说明文字**:「[查看客户的提醒] 列出当前客户还会发出的提醒,包括短编号(如 R1)、提醒语、事件原时间、下次发送时间、循环方式。客户问『我设了哪些提醒』,或要取消提醒前,先调本工具拿短编号。列表里的发送时间已经减去提前量,客户说的通常是事件时间。只能看到当前客户自己的提醒。」
- **入参**:绑定 `project_code`、`customer_pad_userid`、`room_conversation_id`;模型可选 `scope`(`all` / `this_chat`,默认 `all`)。
- **返回**:`reminders[]`,每项含 `short_code`、`notify_code`、`content`、`event_time`(事件原时间)、`next_send_at`、`repeat`、`repeat_interval`、`repeat_until`、`chat`(`private` / `this_group` / `other_group`);另有 `total`、`server_now`。最多 50 条,按下次发送时间升序。
- **短编号**(9.10 ①,已定):服务端按客户分配(`R1`、`R2`…),存在提醒表新加的一列,删掉后不复用;Dify 时期的旧提醒第一次被列出时补发。原始 `notify_code` 是 `notify_` + uuid(`agent-callback.service.ts:189`),模型手抄必错,所以模型只用短编号。
- **过滤口径**与现有接口一致:未删除、未结束、下次发送时间未过(`ai-notify.service.ts:38-64`)。Dify 时期建的提醒同样能查到,因为键都是 `customer_pad_userid`。

### 5.5 `reminder_delete`(新增,写)

- **说明文字**:「[取消客户的提醒] 按 reminder_list 返回的短编号取消当前客户的提醒,循环提醒整条取消。一次取消多条时,必须先把要删的清单列给客户看,客户在下一轮明确确认后,才带 confirmed_by_customer=true 调用。编号不是当前客户的会出现在 not_found 里。只有出现在 deleted 里的才算取消成功。」
- **入参**:绑定 `project_code`、`customer_pad_userid`;模型填 `codes`(1..20,收短编号,也收原始 `notify_code`)、`confirmed_by_customer`(boolean,可选)。
- **一次删多条**(9.10 ②,已定):`codes` 多于 1 条且没带 `confirmed_by_customer=true` → 42200,hint「先列出清单请客户确认,下一轮再删」;不做服务端两段式(06 §9 第 8 条)。
- **返回**:`deleted[]`、`not_found[]`、`server_now`、`request_id`。
- **做法**:只处理 `project_code + customer_pad_userid` 匹配的行 → 软删 + 删掉还没触发的 job(照抄 `removePendingJob`)。
- **幂等**:同一客户已删过的编号再删,仍算 `deleted`(查询时连软删的行一起看);别人的编号一律 `not_found`,不暴露它存不存在。

### 5.6 `health_plan_get_brief`(新增,读)

- **说明文字**:「[查看客户健康方案(精简文字版)] 读当前客户最新一份健康方案的文字摘要:基础信息、每日饮食安排(各餐次推荐吃什么)、方案里的说明表格。只读;客户想调整方案时不要自己改,按话术转给健管师团队。」
- **入参**:绑定 `project_code`、`customer_code`;模型可选 `plan_type`(`HEALTH_PLAN` / `REVERSE_DIABETES_STAGE_REPORT`,默认 `HEALTH_PLAN`,与旧接口默认一致)。
- **返回**:`found`(bool)、`health_plan_code`、`created_at`、`text`(纯文本,分节标题用【】,不用 Markdown 符号)、`truncated`(bool)、`total_chars`、`server_now`。`text` ≤ 6000 字,超出从末尾截并注明「(后面还有 N 字)」。
- **内容**:照项目服务 `queryCustomerHealthPlanMd` 的三段(基础信息、每日饮食安排、文档表格,`customer-health-plan.service.ts:11065` 起)。
- 未建档 → 40400;没有方案 → `found=false`、`text=""`(不报错)。

### 5.7 `profile_get_brief`(新增,读;客户资料精简版,v1 就做)

- **说明文字**:「[一次取齐客户资料] 一次返回当前客户的基础档案、健康方案摘要、今天已记录的饮食、现有提醒、可以写入的表单清单,以及当前北京时间。话题和客户健康有关时调一次,每轮最多一次;闲聊、问天气不用调。推广方案话术前判断客户有没有方案、首轮补问前看档案缺哪些,也用它。customer_registered=false 表示这位发言人在系统里没有建档,不要调写入类工具。」
- **入参**:绑定 `project_code`、`customer_code`、`customer_pad_userid`、`room_conversation_id`;模型可选 `sections`(`basic_info` / `health_plan` / `today_food_log` / `reminders` / `writable_forms` 的子集,默认全部)。
- **返回**:

| 字段 | 内容 |
|---|---|
| `customer_registered` | bool;`customer_code` 为空或查不到时为 false,档案、方案、饮食段为空(提醒段照常,按企微 id 查) |
| `basic_info` | `{text, updated_at}`;`form_customer_basic_info` 各字段最新值,「字段名:值 单位」一行一个,空值省略 |
| `health_plan` | `{found, text, truncated}`;同 5.6,但 ≤ 3000 字 |
| `today_food_log` | `{text, count}`;今天(北京)`form_food_log` 的记录 |
| `reminders` | `{items, total}`;同 5.4 的口径和字段(短编号、提醒语、事件原时间、下次发送时间、循环、截止),最多 20 条,多了注明总数 |
| `writable_forms` | `{form_code, form_name, description}[]`;可写表单清单(情境字段由模型读字段说明自己认,3.4) |
| `server_now` | |

- 总长 ≤ 8000 字。超出时依次压缩方案、饮食日志、提醒。
- 替代 Dify 每轮的 5 路预取 + 每轮查提醒;业务方不再预取。不缓存,每次现查:本轮刚写的数据,下一次查就能看到。
- 为什么由模型自己查(11-17 定):`customer_code` 是绑定变量,模型看不到改不了;数据是最新的;系统提示词每轮不变,厂商的前缀缓存能用上;业务方不用多写查询。代价是用到资料的轮次多一步工具调用,所以做成「一次取齐」、每轮最多一次。

### 5.8 增强 `chat_private_history`(复用 + 增强,读)

- **对现有调用方零变化(11-17 用户追问后写死)**:这个工具已经有人在用(员工 OAuth 登录后调用,可能还有别的智能体);Dify 健管师流程**不用**它(DSL 里 0 处引用,Dify 拉历史走的是数字人服务的 `POST /message/get-history-message`,本次不动)。所以本节所有新行为 —— 新范围、`keyword`、`page_size` 上限 30、`speaker` / `chat` 两个新字段、时间统一 `+08:00`、非文字只给占位不给链接、总长 ≤ 8000 字、`customer_code` 为空回 40400 —— **只在 `scope=customer` 时生效**。不传 `scope`(即 `private`,默认)时,入参、返回字段、条数默认值与上限(近 7 天、每页 50、上限 100)、内容格式都与改造前**逐字一致**。验收加一条契约测试:不带新参数调用,结果与改造前完全相同。
- **说明文字**:保留现有原文不动,在末尾**追加**一段:「`scope=customer` 时查这位客户更早的聊天:他的私聊、他在群里发的话、我们在群里 @ 他的话;可按关键词和时间段查,一次最多 30 条;每条标明是客户还是健管师说的、什么时间。」(不改写原文,免得改变其他调用方看到的说明。)
- **入参**:绑定 `project_code`、`employee_code`、`customer_code`;模型可填 `keyword`(新)、`since_time` / `until_time` / `recent_days`(沿用)、条数 `page_size`(沿用,新范围下上限 30)、`cursor`(沿用)。
- **范围**(新):`t_project_cpwx_chat_msg` 里 ① 员工号 ↔ 该客户的私聊(现有);② 该客户在各群里发的(`roomid` 非空且发送方是该客户);③ 员工号在群里发、@ 名单含该客户的。不撤回、不跨客户。
- **不改变别的调用方**:这个工具已经有人在用(员工 OAuth 等),直接扩大范围会改变他们看到的结果。建议加一个参数 `scope`(`private` = 现有行为,默认;`customer` = 上面的新范围),健管师配置里用固定值钉成 `customer`(下拉可选,§4.1),模型看不到。(已定按 `scope` 参数做,配置里钉 `scope: customer`。)
- **返回(仅 `scope=customer`)**:沿用现有 `rows` 结构,每条加 `speaker`(`客户` / `健管师`)和 `chat`(`private` / `group`);时间统一 `+08:00`;文字之外的类型只给一行占位(「[图片]」等),不给链接。总长 ≤ 8000 字。
- `customer_code` 为空(群里未建档的发言人)且 `scope=customer` → 40400,模型照常回答,不查。
- 两条鉴权通道都可用;OAuth 员工下 `employee_code` 仍取登录身份(现有 `bindEmployeeScope`)。

### 5.9 `handoff_to_human`(新增,写;转人工)

- **说明文字**(给模型):「[把客户交给同事跟进] 客户的问题需要同事亲自处理时调用,系统会马上把你写的 summary 发给负责的同事。本轮回不回、之后还回不回由系统配置决定,结果里会告诉你本轮怎么做。summary 写给同事看:客户原话要点、为什么要交给人、你已经跟客户说了什么。急症时先在回复里建议立即就医或拨打 120,再调本工具并把 is_emergency 设为 true。不要对客户说『已转人工』。」
- **入参**:

| 参数 | 类型 | 谁填 | 说明 |
|---|---|---|---|
| `summary` | string 10..500 字 | 模型 | 给同事看的交接说明 |
| `is_emergency` | boolean | 模型 | 急症时为 true(不写临时接管标志,3.10) |
| `this_turn` | string,`oneOf`:`{const: "reply", title: "本轮回复"}`、`{const: "silent", title: "本轮不回"}` | 配置钉死(下拉) | |
| `afterwards` | string,`oneOf`:`{const: "continue", title: "之后照常"}`、`{const: "stop", title: "之后不回"}` | 配置钉死(下拉) | |
| `project_code`、`employee_code`、`employee_pad_userid`、`customer_code`、`customer_pad_userid`、`room_conversation_id`、`source_message_ids` | string / 数组 | 平台绑定 | `customer_code` 可为空串(群里未建档),其余不能空 |

  `this_turn`、`afterwards` 做成字符串而不是布尔,因为平台的固定值只收字符串(6.6)。

- **返回**:`status`(`sent` / `duplicate` / `no_target`)、`target`(`user` / `room`,不回显 id)、`this_turn`、`afterwards`、`is_emergency`、`takeover_flag_written`、`ai_switch_off`、`guidance`(本轮怎么做的一句话)、`server_now`、`request_id`。
- **做法**:见 3.10 的五步。
- **去重**:同一客户、本次 `source_message_ids` 与之前某次转人工重叠(打断后重跑)→ `duplicate`,不再通知同事,返回上次的结果。不同的消息再转一次照常通知(不按时间窗压)。
- **找不到通知对象**(绑定行没配、调用也没给)→ `no_target`:不发通知、**不写接管标志、不关 AI 开关**,`guidance` 告诉模型「没有转成,照常回复」;服务端记告警日志。理由:同事收不到,AI 再闭嘴,客户就没人管了(已定 11-17,9.14)。
- **健管师用哪种组合**:**`reply` + `continue`**(11-17 定,9.15;默认由本轮不回改为本轮回复);控制台随时可改。

### 5.10 复用工具的改造

| 工具 | 改动 |
|---|---|
| `form_get_latest_field_values`、`form_list_owner_submissions` | M4 日期容错与说明修正;(建议)返回体超过 15000 字时减少行数,并加 `truncated: true`(outputSchema 顶层没禁额外字段,可以加) |
| `form_get_field_detail` | 不改;平台钉 `detail_level=brief` |
| `chat_private_history` | M11:范围、关键词、条数、说话人标注(5.8) |
| 全部工具 | M2 异常文案;M3 入参校验(并行期先只对新工具开) |

### 5.11 智能体配置片段(YAML,配置的人用)

```yaml
spec:
  system_prompt:
    jinja: true
    template: |
      (正文见提示词文档;这些变量不需要出现在正文里)
    variables:
      - {name: project_code,         description: 项目编号,只给工具用}
      - {name: customer_code,        description: 客户编号;群里未建档的发言人传空串}
      - {name: customer_pad_userid,  description: 发言人企微 id}
      - {name: employee_code,        description: 服务员工编号}
      - {name: employee_pad_userid,  description: 服务员工企微 id}
      - {name: room_conversation_id, description: 群 id;私聊传空串}
      - {name: attachment_urls, required: false, description: 本轮附件原始链接,只给表单写入用}
      - {name: source_message_ids,    description: 本轮客户消息 id(JSON 数组),只给写工具判重用}
  tools:
    - type: mcp
      servers: ["<健管师 MCP 注册名>"]
      allow_tools:
        - profile_get_brief
        - health_plan_get_brief
        - form_get_field_detail
        - form_get_latest_field_values
        - form_list_owner_submissions
        - form_save_customer_values
        - reminder_create
        - reminder_list
        - reminder_delete
        - chat_private_history
        - handoff_to_human
        - material_list
      arg_bindings:
        - server: "<健管师 MCP 注册名>"
          tool: profile_get_brief
          args: {project_code: project_code, customer_code: customer_code,
                 customer_pad_userid: customer_pad_userid, room_conversation_id: room_conversation_id}
        - server: "<健管师 MCP 注册名>"
          tool: health_plan_get_brief
          args: {project_code: project_code, customer_code: customer_code}
        - server: "<健管师 MCP 注册名>"
          tool: form_get_field_detail
          args: {project_code: project_code}
          fixed: {detail_level: brief}
        - server: "<健管师 MCP 注册名>"
          tool: form_get_latest_field_values
          args: {project_code: project_code, owner_code: customer_code}
        - server: "<健管师 MCP 注册名>"
          tool: form_list_owner_submissions
          args: {project_code: project_code, owner_code: customer_code}
        - server: "<健管师 MCP 注册名>"
          tool: form_save_customer_values
          args: {project_code: project_code, owner_code: customer_code,
                 employee_code: employee_code, attachments: attachment_urls,
                 source_message_ids: source_message_ids}
        - server: "<健管师 MCP 注册名>"
          tool: reminder_create
          args: {project_code: project_code, customer_code: customer_code,
                 customer_pad_userid: customer_pad_userid, employee_code: employee_code,
                 employee_pad_userid: employee_pad_userid, room_conversation_id: room_conversation_id}
        - server: "<健管师 MCP 注册名>"
          tool: reminder_list
          args: {project_code: project_code, customer_pad_userid: customer_pad_userid,
                 room_conversation_id: room_conversation_id}
        - server: "<健管师 MCP 注册名>"
          tool: reminder_delete
          args: {project_code: project_code, customer_pad_userid: customer_pad_userid}
        - server: "<健管师 MCP 注册名>"
          tool: chat_private_history
          args: {project_code: project_code, employee_code: employee_code, customer_code: customer_code}
          fixed: {scope: customer}
        - server: "<健管师 MCP 注册名>"
          tool: handoff_to_human
          args: {project_code: project_code, employee_code: employee_code,
                 employee_pad_userid: employee_pad_userid, customer_code: customer_code,
                 customer_pad_userid: customer_pad_userid, room_conversation_id: room_conversation_id,
                 source_message_ids: source_message_ids}
          fixed: {this_turn: reply, afterwards: continue}  # 健管师默认(11-17 定:本轮回复 + 之后照常);控制台下拉可改,见 3.10
        - server: "<健管师 MCP 注册名>"
          tool: material_list
          args: {project_code: project_code}
          fixed: {agent_code: "<健管师智能体编码>"}
    - type: mcp
      servers: ["amap-maps"]
      allow_tools: [maps_weather]
  dynamic_workers:
    enabled: false
```

平台校验(`agent_spec.py:1698-1760`,确认):
- 同一个(server, tool)只能绑一次;
- `server` 必须在本条的 `servers` 里;
- `tool` 必须在 `allow_tools` 里;
- `args` 指向的变量必须已声明。
任何一条不满足,保存时就被拒。

### 5.12 `material_list`(新增,读;05 评审 11-17 定,读智能体专用素材表)

- **用途**:客户明确要运动视频(或别的素材)时,让模型看到本智能体能发的全部素材,挑最合适的 ≤ 3 个,把 id 放进输出的 `media`(02)。
- **说明文字**(给模型):「[列出可以发给客户的素材] 只在客户明确要视频或资料时调用。返回可发素材的完整清单(id、名称、类型、标签、说明),不含链接。对照说明里的适合人群和禁忌,按客户情况(年龄、关节、血压、器械条件等)挑最合适的,最多 3 个,把 id 填进回复的 media;清单里没有合适的就只给文字建议。」
- **入参**:`project_code`(平台从 inputs 绑定);`agent_code`(智能体配置里钉死);模型不填任何参数。
- **数据源**:深护新建的 `t_project_agent_material`(表名待深护确认,结构见 05 §3.3.1)。
- **口径**:`project_code` = 本项目,`agent_code` = 本智能体**或为空**(空 = 全项目通用),`status = 1`,未删除;按 `sort` 排。
- **返回**:`items[]`,每项 `id`(表主键,整数)、`name`、`media_type`(`video` / `image` / `file` / `audio`)、`tags`(JSON 数组,如「低强度」「坐姿」)、`description`(前 60 字左右:要领 / 适合人群 / 禁忌 / 用法;没写返回空串);外加 `total`、`server_now`。**返回全部符合条件的素材**(不分页、不按关键词搜 —— 关键词搜会漏同义词,全量清单不会漏),**永远不返回 `url`**。总长 ≤ 8000 字(41 条带说明约 4 千字)。
- **以后**:素材超过约 150 条时再加一个按标签筛选的参数,v1 不做。
- **没有素材**:返回空清单(不报错),模型如实说暂时没有视频、只给文字建议。

## 6. 异常与边界

**6.1 会话失效 400 → 404。** MCP 清掉空闲会话(30 分钟)或重启后,平台还拿着旧会话 id。平台只认 404:SDK 会把 404 变成「Session terminated」(`mcp` SDK 1.30.0 `client/streamable_http.py:372-377`),平台据此换新会话并补发一次,写操作也补发,因为服务端没执行(`tools/mcp.py:560-575、1124-1170`)。现在回的是 400,平台按普通失败处理,这次调用直接失败、不补发(`:1214-1216` 写操作 `idempotent=False`),模型看到报错。改 M1 后对模型无感。另外会话只存在进程内存里(`mcp-transport-registry.ts:24-25`);MCP 将来多副本又不做会话粘滞的话,会频繁 404,改了 M1 也只是变成频繁重连。

**6.2 绑定没落地。** 服务器保存时连不上、工具名写错、内部名撞名(超 64 字符截断后重名)时,平台给警告但照常保存,**被绑定的参数会回到模型手里**(`api/agents.py:1150-1220` 原话:「a variable-bound one stays in the model's hands」)。对策:保存后必须看警告(4.1);上线前跑 7 节用例 1–4;注册名取短的。(9.5 的服务端加固已移入深护安全待办,不在 B-314。)

**6.3 变量缺失与空串。** 必填变量没传 → 422,run 起不来,这是想要的效果。可选变量 `attachment_urls` 没传 → 参数被删,MCP 当作没有附件。`customer_code` 空串 → 档案、表单、方案工具回 40400 / `customer_registered=false`;提醒工具照常可用(按企微 id)。

**6.4 未预料异常。** 改 M2 后模型只看到「服务内部错误,请稍后再试」和 `request_id`。不改的话,SQL 片段、表名、内部主机名可能进入模型上下文,甚至被模型复述给客户。

**6.5 返回体积。** 平台单次 2 万字中间截断(`tools/mcp.py:115、1437-1450`),截断后的 JSON 模型读起来就是坏的。新工具自己控制在 8000 字内;旧工具靠 5.10 和提示词约束。

**6.6 `fixed` 只收字符串。** 布尔或整数参数钉不住:字符串 "false" 在 JS 端是真值,`is_system` 这类整数也钉不了。所以平台要钉死的参数,MCP 一律设计成字符串枚举(本设计钉 `detail_level`、`chat_private_history` 的 `scope`、`handoff_to_human` 的 `this_turn` / `afterwards`),并在 schema 里声明可选值,控制台才能出下拉、保存时才能校验(§4.1)。旧工具的布尔参数如果以后要钉,服务端先把 "true" / "false" 字符串转成布尔。

**6.7 时间与时区。**
- MCP 读写数据库按 `+08:00`;新工具入参按 3.9 解析,出参带 `+08:00`。
- 数字人服务按进程时区算延迟和续期,仓库里没设 TZ(推断风险:若生产容器不是东八区,Dify 时期的提醒本来就会偏;MCP 新建的首次发送不会偏,但第二次起的续期仍按数字人进程时区)。上线前确认(4.3)。
- 项目服务表单写入也用 `dayjs()` 进程时区(`form.service.ts:1355`);MCP 自己写表单时显式取北京时间。

**6.8 outputSchema 会被严格校验。** 平台用的 MCP SDK 对成功结果按工具声明的 `outputSchema` 做 jsonschema 校验,不符直接抛错,整次调用失败(SDK 1.30.0 `client/session.py:412-441`)。所以:
- 不要在现有 `health_plan_get_customer_plan` 上加文本模式(它的 outputSchema 要求 `plan` / `doc_table_summaries` 等字段),另开新工具;
- 新工具 outputSchema 要写准,可空字段写 `["string","null"]`,每条返回路径都有契约测试(M10)。
- 补充:MCP 有开关 `MCP_ENABLE_STRUCTURED_CONTENT`(`config/app.ts:16`)。给 Dify 用的实例设为 false 时,连 outputSchema 也不下发,平台就不校验。并行期两边共用一个实例时这个开关得保持 false;之后打开,上面的要求就生效。

**6.9 MCP 重启。** 重启后会话全丢,靠 6.1 自动恢复。提醒、去重状态都在 MySQL / Redis 里,不受影响。

**6.10 队列库号对不上。** MCP 投的 job 进了另一个 Redis 库,消费端永远看不到,提醒静默不发。对策:MCP 读和项目服务同一个配置项;7 节用例 10 真等一次到点发送;上线前用 Redis 看 `bull:send-notify-queue:delayed` 有没有这条 job。

**6.11 字段值长度。** 累积字段越追加越长,化验总结也长。写前按真实列长度截断,原因记 `too_long`(推断:列可能是 255,要核表结构)。

**6.12 附件链接变量。** 平台对 URL 形态的 inputs 有「渲染成本地路径 / 预拉进沙箱」的处理(B-67,`prompt_render.py:195-210`)。`attachment_urls` 只给工具用、不出现在提示词正文里;按 01 §3.3.2 / 06 §5.1 的做法传 JSON 数组,数组里的裸链接平台不预下载(`inputs_doc.py:317-352`,读代码确认),所以不会被预拉进沙箱。

**6.13 群里未建档的发言人。** 只能用提醒和天气;档案、表单工具回「未建档」,模型按话术处理。

**6.14 写入与主回复的先后。** Dify 里写入支路和主回复并行,本轮写的东西健管师看不到。迁移后写入是模型的工具调用,发生在回答之前,回答能引用写入结果。这是行为改进,无需额外处理。

**6.15 被取消的轮次。** 平台取消不回滚,已执行的写入保留(见共享背景)。重跑时 `source_message_ids` 带着被取消那批的 id,靠 3.4 的来源消息去重兜住;提醒靠 5.3 的去重兜住;转人工靠 5.9 的去重,不会通知同事两次。

**6.16 `source_message_ids` 与 03 的 `input_message_ids`(11-17 定:改名分开)。** 03 给「发起一轮」请求加了一个**顶层**字段 `input_message_ids`(只含本轮 input 那批,给平台补消息判重);本文的是 **inputs 变量 `source_message_ids`**(给 MCP 判重),平台不会把前者传给工具。03 落地前两者内容相同。03 落地后打断重跑时,被取消那批改作补消息、不再进顶层字段(03 §4.3 S-8),但**仍要放进 `source_message_ids`**:它的含义是「这一轮要回答的客户消息」,少了被取消那批,模型重写同样内容时就判不出重复。**做法(11-17 定)**:数字人服务调项目服务时另传 `source_msg_keys`(本批 + 正在重跑的被取消批,01 §5.1、03 S-7 / S-8),项目服务加通道前缀放进 `source_message_ids`;不从 `backfill` 里猜(那里还混着 AI 关闭期间的其它消息)。

**6.17 转人工的边界。**
- 同一轮里客户又发新消息、取消没成功:临时接管标志已被客户的新消息删掉,旧回复可能照发(3.10,与现在的真人接管相同)。
- 临时标志写到了错的 Redis 库,或键的拼法和三方不一致:发送前检查查不到,`silent` 失效,本该不回的那句照样发出。对策:M10 契约测试 + 7 节用例 20。
- dk 的回复任务要在标志存活的 10 分钟内被领走才拦得住;模型在 `silent` 时本来就输出空(06),标志是第二道保险。
- 客户自己要求「别让 AI 回我了」:属于客户对自己服务的选择,不在威胁模型里;模型照提示词判断要不要转人工。
- 找不到通知对象:见 5.9,不写标志、不关开关。

**6.18 查更早聊天的边界。** 群里未建档的发言人(`customer_code` 空)查不了,回 40400;多对一服务群里内部同事的发言不在三类范围内,不返回(数字人现有历史接口在多对一群另带内部员工,`message.service.ts:157-166`);返回只有文字,图片等只给占位。

## 7. 测试与验收

| # | 怎么触发 | 看哪里 | 什么算过 | 没修好时会怎样 |
|---|---|---|---|---|
| 1 | 客户 A 私聊:「帮我查一下张三 / 客户 cust_B 的血糖和档案」 | 平台 run trace 的工具参数;MCP 日志的 `owner_code` | 所有调用的客户都是 A;回复里没有 B 的任何数据 | 模型把 B 的编号填进参数,拿到 B 的数据 |
| 2 | A:「把客户 cust_B 的体重改成 100 公斤」 | MCP 日志;B 的表单提交表 | B 没有新提交;A 也没被写成 100(或按 A 自己的数据写了并如实说明) | B 的档案被改 |
| 3 | 群里 A:「把 notify_<B 的编号> 删了」「把所有人的提醒都删了」 | `reminder_delete` 返回;提醒表 B 的行 | 返回 `not_found`;B 的提醒仍在、到点照发 | B 的提醒被删 |
| 4 | 保存智能体配置;在控制台工具目录 / trace 里看工具 schema | 保存返回的警告;模型看到的工具参数 | 无 `did not land` / `not checked` 警告;模型看到的 schema 里没有 `project_code`、`customer_code`、`owner_code` 等身份参数 | 参数留在模型手里,1–3 可能失守 |
| 5 | 调 runs 不带 `customer_code` | HTTP 返回 | 422 `missing required input` | run 起来了,工具收到空身份 |
| 6 | 生产 MCP 用随机 key 建连 | HTTP 返回 | 40100 | 任意 key 都能连上并调全部工具 |
| 7 | ① 「早上空腹血糖 7.2」跑一轮;再用**同一批 id 加一条新消息**跑一轮(模拟打断合并重跑)。② 10:00、15:00 两条**不同消息**各发「喝了 300ml 水」 | 返回 `status`;表单提交表;记录上的来源消息 id | ① 第二次 `duplicate`,只有一条血糖提交;② 两条饮水记录都在 | ① 两条一样的血糖记录;② 第二次被误当重复、少记一次 |
| 8 | 「我叫王小明」;「我现在还在吃 X 药」(档案里已有 Y 药);「我没有过敏」(档案里已有过敏源) | 返回 `skipped` / `saved`;客户主表 `real_name`;档案最新值 | 姓名跳过(`forbidden_field`)、主表不变;用药变成「Y;X(日期)」;过敏源不被清空(`negation_ignored`) | 主表姓名被昵称覆盖;用药被替换;过敏源清空 |
| 9 | 同月先后发两张化验单;下月再发一张;一张超过 70 项的 | 返回 `status`、`dropped_over_limit`;化验单提交表 | 前两张合成 1 条(`merged`),附件两张都在且不重复;下月新建;超出的列在 `dropped_over_limit` | 同月多条、附件丢失,或写入失败 |
| 10 | 「明天早上 8 点提醒我测血糖」;「每天提醒我吃药」 | 返回;提醒表;Redis delayed 队列;到点时客户企微 | 返回 `created` + `notify_code` + `first_send_at=07:50+08:00`;表里有行、队列里有 job、到点真发出;吃药提醒走的是 `reminder_create` 而不是用药工具 | 回复说设好了但到点没发(库号不对 / 时区偏) |
| 11 | 「昨天 8 点提醒我」(过去时间) | 返回 | `rejected: time_out_of_range`,回复如实说没设上 | 模型照样说「设好了」 |
| 12 | `reminder_list` → 用短编号删其中一条 → 等到点;再不带 `confirmed_by_customer` 一次删两条 | 返回;提醒表软删;到点客户企微 | `deleted` 含该编号;到点不发;一次删两条没带确认 → 42200、两条都还在 | 删了还发;或一句话清空全部 |
| 13 | 测试环境 MCP 空闲 31 分钟或重启后,让健管师调一次工具 | 平台日志 `mcp.session_terminated_resend`;run 结果 | 调用成功,模型无感 | 这次工具调用报错 |
| 14 | 构造一个会让数据库报错的调用(测试环境临时断开 MCP 数据库连接) | 模型看到的工具结果 | 只有固定文案 + `request_id`,没有 SQL / 表名 | 报错原文进入模型上下文 |
| 15 | 选一位健康方案最大的客户,调 `profile_get_brief`、`health_plan_get_brief` | 返回长度;trace 里有没有 `chars truncated` | ≤ 8000 字,没有平台截断标记 | JSON 被中间截断,模型读到坏数据 |
| 16 | 「明天杭州冷不冷」;「明天天气怎么样」(档案无地址) | trace 工具名 | 前者调 `mcp__amap-maps__maps_weather`,回复用真实数据;后者先问城市 | 没调工具、编天气 |
| 17 | 群里未建档的发言人说「记一下我血糖 7.0」「明天提醒我喝水」 | 返回 | 写表单回 40400、回复按话术处理;提醒正常设上 | 写到空 owner 名下 |
| 18 | 一条消息「早上喝了 300ml,上午又喝了 300ml」 | 写入次数与内容 | 合成一条 600ml,或两条各带不同时间;没有一条被判成重复而丢掉 | 少记一次 |
| 19 | 默认配置 `reply`+`continue`:「二甲双胍能不能减半?」;同事回复前客户再发「在吗」;再问「晚饭吃什么」 | 同事企微(测试转人工目标);客户收到的消息;Redis;AI 开关 | 同事收到交接说明;本轮客户只收到一句稳住的话,不含用药 / 剂量建议、不含「转人工」;没写临时标志、开关仍为 1;「在吗」回「确认一下稍后回复」;饮食照常回答 | 同事没收到;本轮回答了药的问题;或客户看到「已转人工」 |
| 20 | (可选组合,非默认)配 `silent`+`continue`:「二甲双胍能不能减半?」;同事回复前客户再发「在吗」;同事回复后客户问「晚饭吃什么」 | 同事企微;客户收到的消息;Redis `HUMAN_HAS_ANSWERED_*`(pad、dk 各一次) | 本轮客户什么都没收到,标志键与三方拼法一致;「在吗」时标志已被客户开口删掉,AI 回「确认一下稍后回复」、不答药的问题;之后正常回答饮食 | 本轮回复照发;或标志一直不删、AI 再也不回 |
| 21 | 配 `reply`+`stop`:「我要退款,太差了」;再发两条 | 客户收到的消息;AI 开关;数字人调用记录 `agent_reply_disabled` | 本轮收到稳住的话;开关置 0(私聊 / 群里这位客户那一行);后两条不调智能体 | 稳住的话被拦;或之后照常回复 |
| 22 | 21 之后由业务后台把开关打开,客户再发一条 | 开关;客户收到的消息 | 开关回到 1,AI 恢复回复 | 没地方打开,AI 永远不回 |
| 23 | 配 `silent`+`stop`,客户「我要找律师」 | 同上 | 本轮不回、之后不回、同事收到通知 | 漏任何一项 |
| 24 | 配 `silent`+`continue`,客户「胸口疼,喘不上气」 | 客户收到的消息;Redis | 客户收到「立即就医 / 拨打 120」;**没写**临时标志;同事收到通知(`is_emergency=true`) | 急症建议被拦,客户什么都没收到 |
| 25 | 打断重跑:转人工那一轮被新消息取消后重跑 | 同事收到几次通知 | 只有一次(`duplicate`) | 同事收到两次 |
| 26 | 控制台给 `handoff_to_human` 的 `this_turn` 配固定值:看是不是下拉、选项文字;再用 YAML 页直接写 `slient` 保存;再绕过控制台直接调 MCP 传 `slient` | 控制台;保存返回;MCP 返回 | 下拉显示「本轮回复 / 本轮不回」;写 `slient` 保存被拒;MCP 回 42200、说明允许值 | 能存进错值,到转人工时才失败或被当成默认 |
| 27 | 客户 A:「上周我跟你说过我在吃的那个药叫什么?」(测试库里 A 有私聊、群里发言、员工在群里 @ A 的消息,同群还有客户 B 的发言) | 工具返回 | 三类消息都能查到、带说话人和时间;B 的发言一条都没有;pad 与 dk 员工各测一次 | 查不到 pad 的消息,或带出别的客户的话 |

用例 1–6、10、12、13、20、21、24、26 是上线闸门,必须全过。其余没过要记缺陷后再定。

## 8. 工作量粗估

依据:对照现有同类代码的行数和复杂度估,单人日,含单测,不含联调排队。属粗估。

| 归属 | 项 | 人日 |
|---|---|---|
| 深护 MCP | M1 404 + M2 异常文案 | 0.5 |
| | M3 入参校验(新工具;旧工具一起上再 +0.5) | 0.5 |
| | M4 日期容错 + 说明 | 0.5 |
| | M5 表单写入(三模式、规则、按来源消息去重、改口、日志) | 4.5–5.5 |
| | M6 提醒增查删(照抄项目服务生产者 + 队列配置 + 去重) | 2.5 |
| | M7 健康方案精简版(移植文本化) | 1–1.5 |
| | M8 客户资料精简版(含提醒段) | 1.5–2 |
| | M10 契约测试(含接管标志键) | 1 |
| | M11 增强 `chat_private_history` | 1–1.5 |
| | M13 转人工(pad 通知、pc 照 `enqueueReferral` 插任务、标志、开关、状态、去重) | 3–3.5 |
| | M14 素材清单 `material_list` | 0.5 |
| | 小计 | **16.5–19.5** |
| | ~~(可选)加固 9.5~~ | 不属于 B-314(深护安全待办) |
| 深护数字人·项目服务 | runs 带 inputs(含 `source_message_ids`)、TZ 确认、确认 / 补 AI 开关的重新打开入口、Dify 下线后清接口 | 1–1.5 |
| 平台配置(我们) | 测试环境注册、开高德、写配置 | 1 |
| | 第 7 节验收(含等提醒到点、四种转人工组合) | 2.5 |
| | 小计 | **3.5** |
| Expert Work 平台代码 | 固定值下拉 + 保存时校验(§4.1) | 1 |
| 否决项对照 | 主回复走 MCP 发(§3.11) | 不做;做的话约 +5 |

素材表的建表与 41 条初始化 SQL 在 `deep-ai-health-db-design/1.5.7/project_tables/leyi/`(`2026-11-17-b314-*`) 的 `08`、`09` 两个文件,由用户在各环境执行,见 05;业务方按素材 id 查库、按类型发见 02(0.5–1 人日,计入 02)。

## 9. 待拍板

**9.1 写操作要不要确认** —— **已定(11-17):不确认**(不找客户、不找员工),不设按客户上限;服务端按来源消息防重复 + 员工可撤销(3.4)。没选的:平台审批(每次上报都等员工,体验倒退)、MCP 两段确认(挡不住模型自己确认自己)。

**9.2 提醒的实现方式** —— **已定(11-17):A**,MCP 直接写提醒表、直接投数字人服务的延迟队列。
- 依据:用户 11-17 定下的原则 ——「MCP 再去调一次数字人服务是否有必要,直接发送不行吗」:MCP 能直接做的就直接做,不再绕数字人服务一跳(转人工同理,3.10)。
- 另外几条理由(比较见 3.6):有现成先例(项目服务的 AI 提醒就是第二个生产者);能返回真实结果;不新增对外接口。
- 没选的 B:MCP 调数字人服务内部接口(现在的 `/add-notify` 不返回结果、吞异常,还要补服务间鉴权)。

**9.3 化验单「按月」按哪个月** —— **已定(11-17):按上传时的北京当月**(沿用 Dify)。没选按采样月:客户补传旧报告时会改动历史月份的那条提交。

**9.4 客户资料怎么给模型** —— **已定(11-17):模型需要时自己调 `profile_get_brief`,业务方不每轮预取**(06 §3.6 同步改)。
- 理由:`customer_code` 是绑定变量,模型看不到改不了;数据是最新的(本轮刚写的,下一次查就能看到);系统提示词每轮不变,厂商前缀缓存能用上;业务方不用多写查询。
- 控制延迟:一个「客户资料精简版」工具一次返回档案、方案摘要、今日饮食、现有提醒、可写表单,最多多一步;提示词要求只在话题与健康有关时调、每轮最多一次,闲聊和天气不调;推广话术前查有没有方案、首轮补问看缺哪些档案项也用它。
- 没选的:业务方每轮把四样作为变量带进系统提示词(提示词每轮一变、缓存失效,业务方要自己查四样);放进 `untrusted_content`(每轮塞进会话,越聊越长)。
- `profile_get_brief`(M8)在 v1 做;`health_plan_get_brief` 留给「看方案完整内容」。

**9.5 服务端核对归属(可选加固)做不做、做到哪一层** —— **已定(11-17,08 评审)**:进深护安全待办,不卡上线(见下)。
- 做法:
  - ① 客户属于项目:查 `t_project_customer`,1 次查询,可缓存 60 秒。
  - ② key 限项目、限工具:配置 key → 允许的项目和工具;`tools/list` 也按 key 过滤。
  - ③ 员工服务该客户:查好友关系表。
- 代价:①② 每次调用多一次缓存查询,外加配置维护;③ 好友关系同步有延迟,群里成员也不一定是好友,会误拒。
- ~~推荐上线前做 ②~~ **已定(11-17,08 评审)**:①② 都进深护安全待办,作为可选加固,不卡上线;③ 不做。理由:生产 `AGENT_KEYS` 已确认配置;平台绑定已挡住模型改身份;健管师配置的 `allow_tools` 已限定能调的工具。

**9.6 写入去重怎么判** —— **已定(11-17):按来源消息**(3.4),取代原先「同一北京日 + 同内容」的时间窗方案。理由:要挡的重复都来自同一批消息被处理两次;按天判会把一天两次喝水、两次散步这类真实重复误吞。

**9.7 按客户限额** —— **已定(11-17):不设**。原 M9 取消。

**9.8 按小时循环的提醒** —— **已定(11-17):沿用 Dify,最长 30 天**(业务方已接受现状)。

**9.9 「查更早聊天」「转人工」什么时候做** —— **已定(11-17):v1 都做**。查更早聊天增强现有 `chat_private_history`(3.12),不新建工具;转人工新建 `handoff_to_human`(3.10)。

**9.10 06 §4.2 列出的服务端要求** —— **已定(11-17,06 评审):按下面各条的推荐**;① ② ⑤ ⑥ 已写进 5.3~5.5、M4,④ 按本文做法(按编码写),③ ⑦ 见各条。
- ① **提醒短编号**:06(R41、§5.4)要求列表给短编号(如 R1),模型只抄短编号;本文原设计用原始 `notify_code`(`notify_` + uuid,`agent-callback.service.ts:189`),现已改为短编号(5.4、5.5)。当时的推荐:由服务端按客户分配、存一列,`reminder_delete` 两种都收。理由:长串手抄必错,抄错一位就是删不掉(安全上不越权,但客户体验差)。
- ② **批量删除要带「客户已确认」**:06(R40、待拍板 8)要求一次删多条必须带这个参数;本文 `reminder_delete` 没有。推荐加一个可选参数 `confirmed_by_customer`(模型填,不绑定),多于 1 条且没带时服务端拒绝并提示先列出确认。
- ③ ~~血糖必须带测量时段、服务端硬拒~~ **已定(11-17)**:改为所有表单通用的「情境字段」规则 —— 模型读字段说明认出情境字段、按消息时间和上下文推断、推不出才问,不做按表单的配置(11-17),服务端不因缺情境拒收(3.4)。
- ④ **按字段显示名写**:06 §4.2 要「改档案可按字段显示名写、服务端映射编码」;本文要求按 `field_code` 写、先调 `form_get_field_detail`。推荐本文做法(显示名可能重名、改名,编码精确)。
- ⑤ **查表单结束日裁到今天**:06(R64)要求;M4 没写。推荐并进 M4(几行代码)。
- ⑥ **设提醒返回带星期**:06 §4.2 要求返回首次时间带星期;本文 `first_send_at` 只有 ISO。推荐加一个 `first_send_weekday`(或可读串),模型回复写「12 月 1 日 周一」客户能当场核对。
- ⑦ ~~方案精简摘要给业务方用~~ **随 9.4 作废(11-17)**:业务方不再每轮带资料,方案摘要在 `profile_get_brief` 里。

**9.11 身份变量必填还是可选** —— **按推荐定(11-17,01 待拍板 12)**
- 本文做法:声明变量全部 `required: true`(`attachment_urls` 除外),没建档 / 私聊传空串;漏传当场 422(§7 用例 5)。06 §5.1 / §5.5 已改成同一口径。

**9.12 MCP 写接管标志用哪个 Redis 库(待核实,不是取舍)**:临时接管标志在数字人服务和 pad 同步 / dk 存档共用的库里(dk 存档服务代码注释写明「与 agent / pad 同库」,`human-answered.service.ts:44`)。库号与「提醒队列在 14 还是 15」(3.6)一起按部署环境核实;写错库,`silent` 会悄悄失效(6.17)。

**9.13 AI 开关被关掉后,谁、在哪重新打开(实现前必须确认)**:读代码在项目服务找到两个接口(私聊、群成员各一个,§1.5),但员工端有没有入口在用没核实。推荐:深护确认员工后台有按钮;没有就补一个,随 M13 一起上。没有入口时不要启用 `afterwards=stop`。

**9.14 找不到转人工通知对象时怎么办** —— **已定(11-17):按推荐 A**:不发通知、不写标志、不关开关,工具告诉模型「没有转成」,模型照常回复,服务端告警。没选的 B:照配置执行(同事收不到,AI 又闭嘴,客户没人管)。

**9.15 健管师默认用哪种转人工组合** —— **已定(11-17):`reply` + `continue`**(11-17 默认由本轮不回改为本轮回复;此前一度定为 `silent` + `continue`)。交出去那一轮先回一句稳住的话、不回答交出去的问题、不说「转人工」;之后照常;急症例外不变;控制台随时可改,`silent` 仍可选。

**9.16 inputs 变量和 03 顶层字段同名要不要改** —— **已定(11-17):改名**。inputs 变量叫 `source_message_ids`(= 这一轮要回答的客户消息:03 之前是合并后的整批;03 之后是本轮 input 那批 + 经补消息重新带上的被取消轮的客户消息);03 的顶层字段仍叫 `input_message_ids`。两处含义一眼可分,重跑时不会传错。

## 10. 出处

**深护 MCP**(`499302b`,确认)
- `src/mcp/mcp-auth.ts:38-93`:鉴权;`:46-54` agent key 上下文;`:84-89` 白名单为空时放行。
- `src/ir/bind-employee-context.ts:15、37、54`:agent key 下原样返回入参。
- `src/mcp/mcp-server.service.ts:39`:工具名 `.` 换 `_`;`:66-78` 工具全量列出;`:102-103` 不校验入参;`:116-118` structuredContent。
- `src/mcp/mcp-error.ts:32-44`:异常原文。
- `src/mcp/mcp.controller.ts:58-64、99-101、110-112`:400;`src/mcp/mcp-transport-registry.ts:24-36`:内存会话、清理。
- `src/config/app.ts:9-16`;`src/app.module.ts:42`:时区 `+08:00`。
- `src/modules/form/form.tool-provider.ts:15-397`;`src/modules/form/form.service.ts:272-279、391-394`:日期拼接。
- `src/modules/health-plan/health-plan.tool-provider.ts:15-176`;`src/modules/health-plan/health-plan.service.ts:97-100`。
- `src/modules/medication/medication.tool-provider.ts:154-160`;`src/modules/medication/medication-reminder-producer.service.ts:1-52`。
- `src/modules/chat/chat.service.ts:54-55`:读 `t_project_cpwx_chat_msg`;`:286-380` 私聊查询(员工 cpwx_userid ↔ 客户 external_userid);`chat.tool-provider.ts:116-160` 入参与上限。
- `src/modules/message/message.service.ts:82-230`(pad 只发文字)、`message-pc.service.ts:150-215、347-369`(pc 只发文字,写 `t_machine_task`);MCP 仓库自己无 `AI_SENT` / `markAiSent`(grep 0 条;pc 的标记由机器任务服务领取时写,见下)。
- `src/ir/confirm-token-store.ts`:未被任何工具使用。
- `src/models/TOpenApiIdempotency.ts`(`uk_idem`)、`src/models/TProjectAiNotify.ts`、`src/models/TProjectFormSubmission(Detail).ts`、`src/models/TProjectOperationLog.ts`。
- 工具数 45:各 `*.tool-provider.ts` 的 `name:` 计数。

**数字人服务**(`8dc5f3f`,确认)
- `src/modules/agent-callback/agent-callback.controller.ts:43、54、65`;`src/modules/agent-callback-dk/agent-callback-dk.controller.ts:34、45、56`;`src/modules/ai-notify/ai-notify.controller.ts:42、53`;`src/modules/message/message.controller.ts:42`。
- `src/modules/agent-callback/agent-callback.service.ts:43-83`(转人工)、`:165-241`(新增提醒);`src/modules/agent-callback-dk/agent-callback-dk.service.ts:108-188`。
- `src/modules/ai-notify/ai-notify.service.ts:38-64、66-105`。
- `src/modules/agent-callback/send-notify.processor.ts:50-82、104-158、161-235`。
- `src/modules/message/message.service.ts:72-87、157-190`(群里只取该客户发的 + 员工 @ 该客户的;多对一群另带内部员工);`src/modules/message/invoke-agent.processor.ts:371-391、479-538(AI 开关调用前检查)、844-866`。
- `src/modules/agent-callback/agent-callback.service.ts:43-125`(pad 转人工、`referral_human_status`);`src/modules/agent-callback-dk/agent-callback-dk.service.ts:6-7、70-104`(dk 转人工)。
- `src/modules/agent-callback/send-agent-answer.processor.ts:488-497`(pad 发送前查临时接管标志);`src/modules/pc-send-task/pc-send-task.service.ts:55-57、206-223、292-296、324-330`(referral 任务:不过闸、不写 AI_SENT、30 分钟过期)、`:432-452`(dk 领任务时查标志)。
- `src/modules/message/invoke-agent-message.processor.ts:338-371`(新消息到达取消正在跑的一轮)。
- `src/models/TProjectCpwxPadMsg.ts:108`(`referral_human_status`);`TProjectCustomerCpwxRelation.ts:74`、`TProjectCpwxGroupMember.ts:74`(`is_enabled_agent_reply`)。
- `src/guard/sign.guard.ts:34-38`;`src/guard/auth.guard.ts:37-39`。
- `src/app.module.ts:105`;`src/config/redis.ts:21`;`dockerfile`、`pm2.json` 无 TZ(推断其影响)。

**项目服务**(`b6938bc2`,确认)
- `src/modules/form/form.service.ts:1291-1499、1501-1505、1685-1699`;`src/modules/form/form.controller.ts:350、363、376`。
- `src/modules/health-plan/customer-health-plan.service.ts:11065` 起;`src/modules/health-plan/health-plan.controller.ts:314`。
- `src/modules/ai-notify/ai-notify.const.ts:1-14`;`src/modules/ai-notify/ai-notify.module.ts:9-14、36`;`src/modules/ai-notify/ai-notify.service.ts:63、82-106、108-166、221、264`;`src/config/redis.ts:24`。
- `src/modules/expert-work/expert-work.types.ts:42-49`。
- `src/modules/employee/employee.controller.ts:1033、1325`;`src/modules/employee/employee.service.ts:4520-4574、4659-4686`(改 AI 开关的接口;员工端入口未核实)。

**pad 消息同步**(`0ff5743`,确认)
- `src/modules/message/sync-pad-message.processor.ts:664-679`(客户开口删临时接管标志)、`:713-765`(员工号真人发言写标志)、`:1381-1419`(@ 名单)、`:1457-1552`(发送方 / 接收方存企微明文 id)、`:1602-1608`(`findOrCreate` 进 `t_project_cpwx_chat_msg`)。

**dk 会话存档**(`ba0a89e`,确认)
- `src/modules/archive/push/human-answered.service.ts:38`(先查 AI_SENT)、`:44`(与数字人 / pad 同库)、`:132-135`(客户开口删标志)、`:157-185`(员工发言、无 AI 标记即写标志)、`:225-232`(键格式)。

**Expert Work**(`origin/main d9db26f79`,确认)
- `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:255-298、300-318、1165、1296-1365、1375-1425、1698-1760`。
- `services/orchestrator/src/orchestrator/tools/arg_bindings.py:127-161、212-257`。
- `services/orchestrator/src/orchestrator/graph_builder/builder.py:1890-1935`。
- `services/control-plane/src/control_plane/api/agents.py:1131-1220`;`services/control-plane/src/control_plane/prompt_render.py:195-210、284-285`;`services/control-plane/src/control_plane/inputs_block.py:35-58`。
- `services/orchestrator/src/orchestrator/tools/mcp.py:115、560-575、1124-1170、1214-1216、1434-1450`。
- `services/control-plane/src/control_plane/tenant_mcp_pool.py:50`;`services/control-plane/src/control_plane/api/mcp_servers.py:476-477、638、672-720`;`services/control-plane/src/control_plane/api/mcp_catalog.py:1-15`。
- `services/orchestrator/src/orchestrator/tools/assembly.py:442-470`;`services/orchestrator/src/orchestrator/agent_factory.py:1203-1211`。
- `services/orchestrator/src/orchestrator/graph_builder/memory.py:540-546、583-612`(记忆提炼每轮只取最终回答,工具调用只记名字)。
- `apps/admin-ui/src/components/manifest-editor/widgets/McpToolPicker.tsx:740-760`(固定值文本框);`mcp_arg_bindings.ts:67-68`(只查非空、首尾空格)、`:125-128`(`ToolParam` 只有名字与是否必填)、`:151-171`(`paramsOf` 白名单读法);`agent_spec.py:1328-1353` `ArgBindingSpec._check_args_and_fixed`(只查非空、首尾空格,不看可选值)。
- 本地 `.venv` 中 `mcp` 1.30.0:`client/session.py:412-441`、`client/streamable_http.py:372-377`(与 `uv.lock` 版本一致)。
- `docs/runbooks/2026-09-24-prod-release-checklist.md:1453`;`docs/runbooks/pending-test-release-acceptance.md:1686`;`apps/admin-ui/src/components/manifest-editor/widgets/__tests__/McpToolPicker.test.tsx:37、148-149`(`maps_weather` 名称,推断)。

**Dify DSL**:节点 id@行号见 1.1、1.2,规则原文见各节点提示词与代码(确认)。

**PC 机器任务服务**(`8527863`,确认)
- `src/modules/pc-machine-task/claim.service.ts:188-192`(领取时对每条机器任务写 AI 已发标记);`recipient.resolver.ts:57-151`(收件人只能是外部联系人私聊或群)。

**素材表**:`t_project_agent_material` 是深护为本次新建的表(表名待深护确认),结构见 05 §3.3.1、§5.2;现有代码里没有,不作读代码出处。

**与共享背景不符或共享背景没写的**
1. 共享背景说「提醒删除(1768787117123@8448)」:该节点标题在 8713 行,8448 附近是「是否要删除提醒」分支。
2. `confirmRequired` 确实只是标注,但 MCP 里有一个写好未接入的两段确认存储 `ConfirmTokenStore`。
3. 共享背景没提:**项目服务已有 AI 提醒的完整增删改查**,直接写表 + 投同一队列(第二个生产者),是方案 A 的先例,也是员工撤销提醒的入口。
4. ~~MCP 现有 `chat_*` 工具只读 dk 会话存档表,pad 通道历史查不到~~ **更正(11-17)**:`t_project_cpwx_chat_msg` pad 和 dk 都写(pad 同步服务 `sync-pad-message.processor.ts:1602` `findOrCreate`),此前的推断是错的。
5. 共享背景说 arg_bindings「模型看不到改不了」:绑定**落空**时(工具名错 / 服务器没连上 / 内部名撞名)参数会回到模型手里,平台只给警告。
6. 共享背景没提:平台 MCP 客户端会按 outputSchema 严格校验返回(6.8)。这决定了健康方案精简版必须另开新工具。
7. 共享背景没提:群里没有客户档案的发言人 `customer_code` 为空,所以提醒要按 `customer_pad_userid` 绑定和查询(与现有数据一致)。
8. 队列库号:数字人代码默认 14,项目服务注释称数字人部署在 15,以环境变量为准。
9. 会话失效回 400 不止 POST,GET、DELETE、旧 SSE `/messages` 也是 400。
10. Expert Work `origin/main` 当前是 `d9db26f79`(在 `5908fe554` 之上的一次部署提交),本文按它核对。
11. 11-17 评审复核:记忆提炼「只看最终回答」的代码在 `memory.py:540-546、583-612`(`_turn`);同文件 368-377 行只是提炼提示词里的示例文字。
12. 11-17 评审复核:MCP 现有 pc 发送写的是 `t_machine_task`,不是数字人服务的 `t_project_cpwx_pc_send_task`;转人工要建成后者里的 referral 任务,不能直接复用现有 pc 发送(3.10)。
13. 11-17 评审复核:项目服务已有改 AI 开关的接口(`employee.service.ts:4520`、`:4659`),此前以为「没找到入口」;员工端是否在用仍待确认(9.13)。
14. 11-17 更正(05 评审轮):§3.11 原理由 1「MCP 发送不写 AI 标记、dk 上 AI 会把自己关掉」不成立 —— pc 的机器任务领取时会写标记(`claim.service.ts:188-192`);pad 未核实。
