# B-314-03 人机混合与补消息

> 所属:B-314 Dify「健管师」迁移(总览:00-overview.md)。状态:草稿 v1;11-17 用户已拍板 §9 全部按推荐(「后面再按测试情况调整」)。
>
> **修订(2026-11-17,独立核对后的修正)**:S-7 / S-8 加上数字人服务另传 `source_msg_keys`(本批 + 正在重跑的被取消批),项目服务据此生成业务方的 inputs 变量 `source_message_ids`;S-5 是否与 01 的会话映射表合并由深护实现时定;§9-13 每行 `HH:mm` 留不留有意留到 PR-3 实测;§3.7 / §3.8 的「推荐」改为已定;版本号口径统一为 `origin/main d9db26f79`。(已被 11-17 拍板修订取代)
>
> **修订(2026-11-17,03 评审结论)**:§9 第 1~11、13、14 条按推荐定稿,上线后按测试情况调整;pad 竞态(S-3)**先不管**,留在本文随 03 一起做,不提前 —— 03 之前 pad 上一轮多条气泡的后几条可能被误拦(已知风险,08 S12);§3.8 加一行:转人工选了「本轮不回」时的未送达口径;业务方给写工具判重的 inputs 变量改名为 `source_message_ids`,本文的顶层字段仍叫 `input_message_ids`。
>
> **修订(2026-11-17,04 评审结论)**:「查更早聊天」已定为增强深护 MCP 现有 `chat_private_history`(04 §3.12);转人工由深护 MCP 写现有的临时接管标志和 AI 开关(04 §3.10),本文的「人工接管开关不动」指判断逻辑不动。业务方另有 inputs 变量给写工具判重,03 评审时改名为 `source_message_ids`,与本文顶层字段 `input_message_ids` 分开(04 §6.16)。
> 读者:Expert Work 平台开发、深护数字人服务 / 项目服务 / 会话存档服务开发。平台接口与改动(§4.1、§5.1~§5.5)按通用能力写,不带业务名词;§3 为了讲清楚用本次迁移的场景举例;深护部分可以写业务名词。

## 0. 一句话结论

客户在企微里看到的是一条完整的聊天:AI 回的、真人健管师手打的、到点提醒、SOP、兜底话术混在一起,对客户来说都是同一个健管师。迁移后,智能体的会话也要是这条完整时间线。

做法分三件:

1. **补消息**:深护每次发起一轮时,把「会话里还没有的企微消息」作为 `backfill` 一起带上。平台在本轮开跑之前、在会话已归本轮所有的时刻,按企微消息身份去重、按企微里的真实发送时间写进会话(客户发的算客户原话,员工号发的算健管师自己说的),再开跑。深护只跳过「某一轮的回答、由深护发出去的那几条」,靠发送时登记「企微消息身份 → 轮次编号」认出它们。
2. **未送达**:回答生成了但没到客户手里(被打断、真人接管、发送失败),深护调一个新接口报「未送达」;平台从下一轮起把那一条回答从模型看到的内容和长期记忆提炼里拿掉,客户那句话保留。
3. **消息时间**:模型看到每条客户消息和补进来的健管师消息的发送时间(只加在模型看到的内容里,不改存储)。

另附带修掉深护 pad 通道现有的竞态(AI 发的消息被当成真人发言,打开人工接管标志)。

工作量粗估:平台约 10.5 人日(三个 PR),深护约 11.5 人日(§8)。

**排期(已拍板 11-17)**:本文放到整个迁移的最后一个阶段做;在它之前智能体不拉企微历史,只看会话本身保存的、AI 参与过的对话。03 之前的已知缺口与临时做法见 01 §3.11。

---

## 1. 现状:Dify 现在怎么做(带出处)

### 1.1 Dify 每轮怎么拿上下文

- 每一轮先调数字人服务的 `message/get-history-message` 拉企微聊天(DSL 节点 `1770115147110`,定义在第 9055 行附近),再由代码节点 `1770115347090`(第 9151 行附近)整理:`sender_role=self` 记作 assistant,`customer` 记作 user,缺字段时按员工 pad id 比对;只留字符串正文;每条带 `timestamp`;切出最近 2 / 6 / 30 条。健管师大模型用最近 30 条(DSL 第 7481 行),意图分析等用 2 / 6 条。(读代码确认)
- 历史接口的范围(数字人服务 `message.service.ts`,读代码确认):
  - pad 查 `t_project_cpwx_pad_msg`,只取文字和语音(`msg_type` 0 / 2 / 16),排除撤回(:142-152)。
  - 普通群:该客户发的 + 本员工号 @ 该客户的;多对一服务群:该客户 + 群内内部员工 + 本员工号(:157-181);私聊:双向(:183-192)。
  - 按时间倒序取 40 条,多对一群至少 60 条(:195-202,:37)。
  - `sender_role` 三种:`customer`、`self`(本员工号,AI 回复、SOP、提醒、真人手打都算,分不开)、`other_employee`(:215-216)。
  - dk 查 `t_project_cpwx_chat_msg`,同样只取文字和语音,范围规则相同(:296-331)。
  - 本批消息的排除方式两个通道不一样:pad 只排除本批里 msg_id 最大的一条(:128-156),dk 整批排除(:296-307)。
- 所以 Dify 里「人机混合」是隐式解决的:每轮看企微里最近 30 条,真人回复自然在里面。代价:超过 30 条就忘;图片、文件看不到;AI 自己的回答也只能从企微反查;没有长期记忆。

### 1.2 深护现在怎么区分「AI 发的」和「真人发的」(只用于人工接管)

**pad 通道**

- 数字人服务每发一条气泡(文字、图片、视频、文件各自一次 HTTP),拿到 pad 返回的整包回执后,带 `is_digital_human: 1` 投进同步队列,俗称「回写」(`send-agent-answer.processor.ts:78-107`,媒体同样写法 :174-192、:243-261、:311-329、:385-403)。回执没有类型声明,从消费方反推含 `app_info`、`sendtime`、`sender`、`receiver`、`msgtype`、`server_id`、`msg_id` 等(同步服务 `sync-pad-message.processor.ts:283-284`,推断)。
- pad 盒子对员工号发出的每条消息还会独立回调一次,回调入队时不带 `is_digital_human`(pad 回调服务 `callback.service.ts:459-476`)。
- 同步服务对这两路按 `(project_code, employee_code, msg_app_info)` 做 findOrCreate,谁先到谁建行,后到的直接 return(`sync-pad-message.processor.ts:599-627`)。消息表没有任何「是不是 AI 发的」列,`is_digital_human` 只活在队列任务里,不落库。(读代码确认)
- 提醒、SOP、兜底话术,以及用药提醒、唤醒等其它服务以员工号发出的消息,回写时同样带 `is_digital_human: 1`。所以 `is_digital_human` 的真实含义是「不是真人手打」,不是「某一轮的回答」。(读代码确认)

**dk 通道**

- PC 发送任务在**发放给 PC 的那一刻**(不是发送成功后)写 `AI_SENT:{项目}:{员工}:{对象}` 有序集合:文字记归一化后的 sha1 指纹,图片 / 文件 / 视频只记类型(`pc-send-task.service.ts:315`,函数体 :521-541;`ai-sent.util.ts:14-75`)。
- 读的一方是会话存档服务:存档入库后对员工发言调 `isAiSent`,文字按指纹、媒体按类型、时间窗都是 ±600 秒,命中后不消费标记(`human-answered.service.ts:17-25,141-218`)。
- PC 端 RPA 的回报只有状态和分段明细,没有企微消息 id(`pc-send-task.service.ts:667-698`)。(读代码确认)

**结论**:两个通道今天都**认不出「这条企微消息是哪一轮的回答」**。要补消息,必须新加「发送登记」。

### 1.3 现有竞态:AI 发的消息被当成真人

pad 回调先于回写被处理时,那一行按「员工本人、不是数字人」处理:私聊写 `HUMAN_HAS_ANSWERED_{项目}_{员工pad}_{客户}`(该员工给这个客户的消息已多于 1 条时),群里给每个被 @ 的人写一个,多对一群另写群级标志,全部 600 秒过期;私聊文字还会把当天待转人工的记录标成已回复。随后到的回写因为行已存在直接 return,既不纠正、也不撤标志(`sync-pad-message.processor.ts:629-637,700-765`)。(读代码确认)

后果(推断):标志在 600 秒到期或客户再开口(:662-678 删除)时消失。实际损害是同一轮后面的气泡在发送闸门被拦(`send-agent-answer.processor.ts:470-509`);如果客户在 AI 分条发送期间先开口、随后 AI 气泡的回调又把标志写上,客户这条消息会在调用闸门被拦(`invoke-agent.processor.ts:734-744`)而得不到回复。另外,群里那条 AI 气泡因为回调行没有 @ 信息,会从该客户的群历史里消失。

### 1.4 回答没发出去时现在会怎样

- pad 文字逐条发送没有单独兜错,一条失败,这一批剩下的文字和媒体全部放弃;媒体单条失败跳过;不重试、不回报(`send-agent-answer.processor.ts:555-610,632-772`)。
- 发送前接管闸门命中:群级命中整批 return,按人命中过滤掉该人,只打日志,不留记录(:415-417、:466-468、:477-480、:507-509)。
- 新消息到达会取消正在跑的 Dify 调用,但已进发送队列的任务不撤;Dify 在取消前已回调的答案照常发出。`receiveAnswer` 里按 `INVOKE_CANCELLED:{invoke_token}` 丢弃迟到答案的兜底,因为 Dify 回调不带 `invoke_token`,实际不生效(`agent-callback.service.ts:130-157`;推断)。
- dk 有落库的终态:`t_project_cpwx_pc_send_task.status` 为 skipped / rejected / expired / report_timeout / reported(`pc-send-task.service.ts:301-305,595-631,667-698`)。

### 1.5 平台现在的会话

- 会话里只有每一轮的输入和智能体自己的输出(外加定时任务投递)。没有从会话外写消息进来的口子。
- 取消、失败、超时都不回滚:已提交的步骤留在会话里;最终回答已经生成后才被取消的,回答留在会话里,下一轮模型以为自己说过。没有「未送达」标记;「重新生成 / 编辑重发」只能作用于最后一轮,而且会连用户那句话一起剔除(`packages/expert-work-common/src/expert_work/common/supersede.py:66-90` 按 run 戳整轮剔除)。
- 模型看不到时间:系统提示词只有日期,构建时固定(`orchestrator/agent_factory.py:1203-1211,1885-1908`),精确时刻要模型自己调 `exec_python`;消息的发送时刻只存在 `additional_kwargs.expert_work_created_at`,不进模型看到的内容。

---

## 2. 目标与不做什么

**目标(每条都能验)**

1. 会话里的消息 = 客户在企微里看到的这条聊天(在 §3.3 的范围规则内),每条只出现一次。
2. 补进来的消息按企微的真实发送时间排列,模型看得到时间。
3. 长期记忆:补进来的客户消息被第一次提炼当成客户原话(带它自己的日期);补进来的健管师消息各自成段、按助手一方处理;不重复提炼,不顶替智能体原来的回答。
4. 没送到客户手里的回答,从下一轮起模型看不到;标记在该轮记忆提炼之前到达时,记忆也不提炼它。客户那句话保留。
5. 平台改动对所有对接方通用,平台文字不出现行业、厂商名词。

**不做**

- 不区分 AI 和真人(已拍板 5)。员工号发的一律是健管师一方。
- 平台不主动去任何外部系统拉消息;补什么由对接方决定。
- 第一期补消息只收文字:图片、文件、语音、视频、链接用占位文字(语音有转写时带转写),不带附件实体(§9-6)。
- 补进来的消息不进 `/items` 的轮次条目(只在 `/messages` 和控制台对话页出现,§3.6)。
- 「未送达」不撤回已经写进长期记忆的内容(第二期再做,§9-7)。
- 不提供「只写不跑」的接口:AI 关闭期间不需要同步,下次开跑时一起补。
- 「查更早聊天」只读工具不在本文:11-17 定为增强深护 MCP 现有 `chat_private_history`(04 §3.12)。
- 人工接管开关和临时接管标志的判断逻辑不动(仍在数字人服务);04 的转人工工具只是多了一个写它们的地方(04 §3.10)。转人工让 AI 关闭期间同事与客户的对话,同样由本文的补消息在 AI 重新打开后补进会话。

---

## 3. 设计

### 3.1 场景一:真人接手一段,AI 重新开启

人物:客户甲;员工号「小丘」(AI 和真人都用这个号发)。会话是「客户甲 × 健管师智能体」(已拍板:私聊和客户甲所在各群共用一段会话;本场景只发生在私聊)。

| 时间 | 企微里发生了什么 | 深护做什么 | 平台会话里多了什么 |
|---|---|---|---|
| 09:00 | 客户:「我早上空腹 7.8 正常吗?」 | 攒 8 秒,发起第 R1 轮 | R1 的输入、R1 的回答 |
| 09:01 | 小丘号发出 2 条文字(R1 的回答) | 每条拿到回执 → **登记** `(pad:a1 → R1)`、`(pad:a2 → R1)` | — |
| 10:00 | 员工在后台关掉客户甲的 AI | — | — |
| 10:05 | 客户:「我想换个运动方案」 | AI 关着,不调用 | — |
| 10:07 | 员工手打:「好的,我下午帮您调整」 | — | — |
| 10:30 | 客户发了一张图 | — | — |
| 14:00 | 到点提醒:「该测餐后血糖了」(提醒不受接管影响) | — | — |
| 15:00 | 员工打开 AI | — | — |
| 15:20 | 客户:「调整好了吗?」 | 攒完后做**缺口查询**:同步位置(R1 时扫到的 09:00 那一行)之后有 a1、a2、10:05、10:07、10:30、14:00、15:20 七条;a1、a2 已登记为 R1 的回答,跳过;15:20 是本批,进 input;其余四条要补 | — |
| 15:20 | — | 发起 R2:`input`=「调整好了吗?」,`input_message_ids`=[本批 id],`backfill`=四条 | 平台在开跑前写入四条,再开跑 R2 |

R2 开跑时,模型看到的内容(提示词视图,示意;时间标注格式见 §3.7):

```
[系统提示词 …]
用户:[2026-11-17 Mon 09:00] 我早上空腹 7.8 正常吗?
助手:{"messages": ["…", "…"], …}              ← R1 自己的回答,不加时间
用户:[2026-11-17 Mon 10:05] 我想换个运动方案
助手:[2026-11-17 Mon 10:07] 好的,我下午帮您调整  ← 补进来的,健管师一方
用户:[2026-11-17 Mon 10:30] [image]
助手:[2026-11-17 Mon 14:00] 该测餐后血糖了
用户:[2026-11-17 Mon 15:20] 调整好了吗?
```

智能体据此知道:「下午帮您调整」是健管师自己答应的、到现在还没兑现,它应该给出调整或说明进度,而不是反问「调整什么」;也知道下午 2 点已经提醒过测血糖,不会再重复提醒。

R2 回答由深护发出并登记,同步位置前进到这次扫过的最大行号。R2 结束后的记忆写回(§3.9):10:05、15:20 两句被第一次提炼当成客户原话,分别带 11-17 的日期;10:07、14:00 两条各自一段,按助手一方;R1 的回答仍是 R1 那一段的回答,不会被 10:07 那条顶替。

### 3.2 场景二:回答生成了,但没到客户手里

**(a) 被新消息打断**

16:00 客户发「晚饭吃什么好」(A),8 秒后发起 R3。R3 跑了 10 秒,客户又发「我不吃辣」(B)。数字人服务取消 R3(R3 可能已经写出回答,也可能还在排队没开跑),攒完 B 发起 R4:

- `input` = B,`input_message_ids` = [B];
- `backfill` 里带 A(客户,16:00)。平台按 A 的身份查会话:R3 已开跑的话,A 已作为 R3 的输入在会话里(R3 的输入带着 `input_message_ids`),跳过;R3 是在排队时被取消的,A 从没写进去,这里补上。两种情况 A 都恰好出现一次。
- 深护对 R3 报「未送达」(原因 `cancelled`)。R3 若写出了回答,下一轮起模型看不到;R3 做过的工具调用(比如设了提醒)保留,因为那是真发生了的事。

模型看到:A → (R3 的工具调用,如果有)→ B,然后回答两句话。

**(b) 真人接管**

R5 生成期间员工手打回复了 s2。深护发送前查到接管标志,不发 R5 的回答 → 报「未送达」(`human_took_over`)。客户下次开口时 s2 被补进来。模型看到:客户的问题 → s2(健管师)→ 客户的新消息;看不到 R5 的回答。

**(c) 发送失败**

pad 发第一条文字就报错,这一批一条都没发出去 → 报「未送达」(`send_failed`)。部分发出去时按 §3.8 的规则判。

### 3.3 哪些消息要补

| 企微里的消息 | 补不补 | 角色 | 依据 |
|---|---|---|---|
| 客户在私聊 / 群里发的(本批之外) | 补 | customer | 已拍板 6 |
| 员工号真人手打 | 补 | staff | 已拍板 5、6 |
| 到点提醒、SOP、兜底话术,其它服务以员工号发的消息 | 补 | staff | 已拍板 6 |
| 多对一服务群里的内部员工 | 补 | staff,带发送人名 | 范围同现有历史接口 |
| **某一轮的回答,由深护发出去的**(`messages` 的文字气泡、`media` 的素材;固定话术由模型写在 `messages` 里,也在其中) | **不补** | — | 发送登记认出(§3.4) |
| 本批客户消息 | 不补,进 `input` | — | 同时放进 `input_message_ids` |
| 群里其他客户发的 | 不补 | — | 已拍板 4 |
| 普通群里员工号没 @ 该客户的 | 不补 | — | 范围同现有历史接口(`message.service.ts:157-181`) |
| 已撤回的 | 不补 | — | 同现有历史接口 |

范围规则与 §1.1 的历史接口一致,区别有四处:**私聊和该客户所在各群合在一次查**(会话不分窗口,已拍板;历史接口是按单个窗口查);**所有消息类型都要**(不只文字、语音);**按入库顺序**而不是按发送时间取;**排除已登记的回答**而不是排除本批里的某一条。

固定话术说明:11-17 拍板后,课程、方案推广话术由模型按提示词把原文写进 `messages`(02 §3.6),本来就是这一轮的输出,来源 `reply`,登记、不补。

### 3.4 怎么分出「已经在会话里」:三层

1. **深护发送登记(主)**:深护每发出一条属于某一轮回答的企微消息,就登记「企微消息身份 → 轮次编号」(pad 用发送回执里的 `app_info`,dk 在会话存档入库匹配上指纹时登记,§4.3)。缺口查询排除已登记的。
2. **平台按身份去重**:`backfill` 每条带对接方的消息身份 `external_id`;`input` 也可以带它由哪些外部消息拼成(`input_message_ids`)。平台写入前扫一遍会话:已经作为补消息写过的、已经作为某一轮输入写过的,一律跳过。补进来的消息 id 由「会话 + 外部身份」确定性生成,即使两次写入撞在一起,`add_messages` 也只会原地替换同一条,不会出现两条。
3. **平台内容兜底(只对 staff)**:登记失败(回执缺 `app_info`、dk 指纹没对上)时,那条回答会被当成员工消息补进来。平台对 `role=staff` 的条目再做一次内容比对:与本会话最近 5 轮的最终回答比,规整后(只留文字、字母、数字)长度 ≥ 6 且是某条最终回答的一部分,发送时间落在那条回答生成后 30 分钟内(前放 2 分钟),就跳过,计入 `skipped_as_run_output`。标了「未送达」的轮不参与比对(员工手动把没送达的回答再发一遍时,那条要补)。

三层都漏掉时的后果:同一段话在会话里出现两次(一次是智能体的回答,一次是健管师一方的补消息)。只多占点上下文,模型不会因此做错事;联调时用 §7.3 D2 盯住比例。

### 3.5 写进会话的时机

**规则**:在本轮开跑之前、在会话已归本轮所有的时刻,用一次 `aupdate_state` 单独写入;不进本轮的图输入,不带新一轮的 run_id。

为什么这样(都已核实):

- 有别的轮在跑时往检查点追加,会被在跑那一轮写回时覆盖丢失(`control_plane/trigger_delivery.py:156-177` 的说明,测试环境复现过)。
- `aupdate_state` 的 config 不能带新一轮的 run_id:LangGraph 会把紧随其后、run_id 相同的图输入当成重入,丢掉新一轮的输入(`services/orchestrator/src/orchestrator/approval_turn.py:165-167`)。
- 塞进本轮图输入会弄坏「重新生成」:它按 `[System, Human, (本轮输入段)?]` 的形状取原件(`control_plane/supersede.py:406-421`)。
- 复用 `trigger_delivery.inject_delivery` 的骨架:读检查点 → 按身份查重 → 一次 `aupdate_state` 追加(`trigger_delivery.py:87-153`)。`as_node` 同样用「有历史 `agent`、空会话 `__start__`」。写完后图上可能挂着一个待跑节点(开了长期记忆时 `agent` 之后会路由到 `memory_writeback`,`graph_builder/builder.py:2544-2580`),本轮输入一到,LangGraph 会丢弃上一个检查点里没跑的任务(LangGraph 1.2.19 `pregel/_loop.py:1118-1140`),所以不影响本轮;测试要钉住「本轮结束时记忆写回只跑一次」(§7.1 U6)。

**三个写入点**(都紧跟在现有的 `close_previous_turn` 之后,这时本轮已占住会话):

| 路径 | 位置 | 条件 |
|---|---|---|
| 流式、会话空闲 | `api/runs.py:1479` 之后,仍在 8620 会话锁内 | `payload.mode != "queue"` 且 `queued_behind is None` |
| 流式、前面有轮在排 | `api/runs.py:1642` `_launch` 里 | 轮到本轮时 |
| queue 模式 | `run_queue_worker.py:340` 之后 | 出队时 |

后两条路径的补消息要随 `enqueued_input` 落库(`api/runs.py:1497-1504,1529-1536`),出队时读回。孤儿接管重跑时若再写一次,按身份去重,不会重复。

**写入失败**:不在缺了一段时间线的会话上跑。空闲流式路径是在建 run 行之前写的,失败直接回 503 信封 `BACKFILL_WRITE_FAILED`(请求自己新建的会话按 B-293 撤掉);两条排队路径已有 run 行,收成 error,错误串 `backfill_write_failed`(流式回 `end` 帧 status=error,queue 模式走 `_fail`)。对接方用同一批重试即可,写过的会被去重。

**校验失败**(格式、上限):在建会话、起 run 之前同步 422,不留任何副作用(§5.1)。

### 3.6 补进来的消息在会话里的样子

- **客户消息**:`HumanMessage`,由 `_build_human_message` 构造(它是用户输入唯一的构造点,带 `expert_work_user_input` 白名单标记),`sent_at` 用企微时间而不是当前时间;另加 `expert_work_backfill` 标记;`expert_work_created_at` = 企微时间。
- **健管师消息**:`AIMessage`,不带工具调用,带 `expert_work_backfill` 标记和 `expert_work_created_at`。它**自己开一段**(和定时任务投递同一个道理:否则它会接在上一轮回答后面,把那一轮的 `final` 抢走,`conversation_channel.py:68-71`)。
- **都不盖 `expert_work_run_id`**。盖了会出三个问题:被归到下一轮,「重新生成」那一轮时补消息被一起剔除(`common/supersede.py:66-90`);`/items` 里那一轮会多出好几条 `final`,对接方按 `final` 取回答会取错;那一轮的形状不再是 `[System, Human, …]`,重新生成会 422。
- **顺序**:同一批按 `sent_at` 升序,追加在会话末尾(本轮输入之前)。不往历史中间插:改中段会让前缀缓存从插入点往后全部失效,也会挪动长期记忆证据指针的序号。企微时间早于会话最后一条的(比如员工在上一轮生成期间手打的)照样追加在后面,真实时间靠 §3.7 的标注体现。
- **读面**:
  - `/messages`(对外与控制台)会出现这些消息,`run_id` 为空,新增 `origin: "backfill"`、`external_id`。
  - `/items` 按轮组织,不出现(`external_session_items.py:425` 用 `group_messages_by_run`,没盖戳的消息本来就丢弃)。
  - 控制台对话页现在只要有一条消息没有 run_id 就整段退回「按顺序配对」(`apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts:96-133`),必须改:补消息单独显示成「会话外消息」行,不参与按轮分组。

### 3.7 让模型看到时间

**只能写进模型看到的内容**:厂商适配器只发角色和正文,`additional_kwargs` 到不了模型。做法是在构造提示词视图时给消息加一个时间标注,检查点里的原件不动(与计划、记忆段等同一契约 CM-C4)。位置:`graph_builder/builder.py:846-850` 剔除被取代轮之后、各道上下文闸之前。

**范围(已定,§9-2)**:会话里出现过补消息时才开;开了以后,给所有不隐藏的用户消息(补进来的客户消息 + 每一轮的输入)和补进来的健管师消息加标注;**智能体自己的回答不加**。

- 每一轮的输入带标注,模型就知道「现在几点」。现在系统提示词只有日期,精确时刻要调 `exec_python`,健管师设提醒(「半小时后提醒我」)正需要这个。
- 智能体自己的回答不加:它的时间从前后消息就看得出;给它自己的历史回答加标注,模型很容易照着在新回答开头也写一个。
- 补进来的健管师消息要加:「下午 2 点已经提醒过」「昨晚真人回复过」这类判断靠它。
- 只在有补消息的会话开:不影响其它对接方的智能体(平台提示词跟着能力走);一个会话第一次出现补消息时,前面所有用户消息一次性加上标注,前缀缓存断一次,之后稳定。

**格式**:`[2026-11-17 Mon 10:05] `,按智能体时区(与系统提示词里日期同一口径 `resolve_agent_timezone`),星期用英文缩写(与平台日期段一致,不受语言环境影响)。由该消息存的时间算出,逐字节稳定,不破前缀缓存。带 `sender_name` 的条目在时间后加名字:`[2026-11-17 Mon 10:05 · 王医生]`。批次里有 `backfill_omitted_before > 0` 时,那一批第一条的标注写成 `[2026-11-17 Mon 10:05 · 150 earlier messages not included]`。

**说明一次**:会话有补消息的那几步,在尾部 `<platform-context>` 段(B-162,`graph_builder/platform_context.py`)里加一句平台文字,大意:「带方括号时间的消息,方括号是平台标注的发送时间(时区 X),不是消息内容;回复里不要写这种标注。带时间标注的助手消息是对接方在对话之外以你的身份发出的,视同你说过的话。」平台文字用平台现有惯例的语言,不出现行业词。

**成本**:每条标注约 10~15 个 token;一个有 200 条用户消息的会话多约 3K token,绝大部分在缓存前缀里;每轮新增只有本轮那一两条。

**风险**:健管师补消息带标注,模型仍可能模仿。验收 A3 专门看回答开头有没有 `[20`;出现了再考虑在输出侧剥掉开头的标注(§6-12)。

### 3.8 未送达

**接口(已定单独接口,§9-1)**:`POST /v1/agents/{agent_code}/runs/{run_id}:undelivered`,与 `:cancel` 同一形状(`external_runs.py:615`),字段见 §5.3。

**存在哪**:`agent_run` 行上新增 `undelivered_at`、`undelivered_reason` 两列。不写检查点:随时可调(哪怕下一轮已经在跑),不用拿锁、不用判忙。

**效果**:

- **模型视野**:每轮开跑时,控制面把本会话被标未送达的 run_id 放进 `configurable`;提示词视图在剔除被取代轮的同一处(`builder.py:850`)再剔掉「这些轮各自的最后一条不带工具调用、有正文的助手消息」。只剔这一条:客户那句话、这一轮的工具调用和结果都留着(它们真发生过)。
- **长期记忆**:写回任务处理时从库里查本会话的未送达轮,算出那几条回答的消息 id,提炼时 `_turn` 不把它们当成该轮的回答(`memory.py:583-620`)。不删消息、不挪序号,证据指针和切片起点不受影响。
- **读面**:`/items` 的 `runs[]`、run 列表、`/messages` 里那条回答都带 `undelivered: true`;控制台在那条回答上显示「未送达」。

**时序上的局限**:写回任务在本轮结束后几秒内就会处理,深护发送失败往往也在这几秒里才知道。标记晚于写回到达时,这一条回答提炼出的「助手一方推断」类记忆不会自动撤回;客户原话不受影响。第二期可按记忆的证据指针(`evidence_message_id`,`persistence/memory/sql.py:122-124`)撤回只以这条回答为依据的记忆(§9-7)。

**深护什么时候报**:

| 情况 | 报不报 | 原因码 |
|---|---|---|
| 一条文字气泡都没发出去(发送报错、超时) | 报 | `send_failed` |
| 至少一条文字气泡发出去了,后面的失败 | 不报 | — |
| 只有媒体失败 | 不报 | — |
| 发送前查到真人接管,这一轮不发 | 报 | `human_took_over` |
| 转人工选了「本轮不回」,模型仍写了回复、在发送前被临时接管标志拦下(04 §3.10) | 报 | `human_took_over` |
| 转人工选了「本轮不回」,模型按要求输出空(`silent`) | 不报(没有回答,无事可报) | — |
| 深护取消的轮(被新消息打断等) | 一律报(没有回答时平台不做任何事) | `cancelled` |
| dk 任务 skipped / rejected / expired | 报 | 对应 `human_took_over` / `send_failed` |
| dk 任务 report_timeout(不知道发没发) | 不报,只记日志 | — |

「至少一条文字发出去就算送达」的理由:第一条通常是主要内容;整条标未送达会让模型以为什么都没说,下一轮可能整段重说,客户看到重复;部分送达的细分(只剔没发出去的那几条)要平台按气泡拆回答,第一期不做。

### 3.9 长期记忆怎么对待补进来的消息

逐条对着代码走一遍(都是 origin/main 读代码确认):

1. **怎么切段**:写回把整段对话切成段,「一条不隐藏的用户消息或定时投递」开一段(`memory.py:549-567` 用 `opens_segment`)。改动后,补进来的健管师消息也开段(§4.1 P-7)。于是:
   - 补进来的客户消息 → 各自一段,开段消息是它自己;
   - 补进来的健管师消息 → 各自一段,开段消息是这条助手消息;
   - 智能体原来的回答仍在它那一轮的段里,不会因为后面紧跟一条健管师消息而被并到一起。若不改 `opens_segment`,健管师消息会并入上一轮,而 `_turn` 一段只留最后一条助手消息(`memory.py:605-610`),原回答就被它顶替了。
2. **第一次提炼(只看客户原话)**:读 `expert_work_user_input` 白名单标记(`common/user_input.py`)。补进来的客户消息由 `_build_human_message` 构造,带标记,第一次提炼当客户原话;标记里的 `sent_at` 是企微时间,提炼按这一天换算「今天早上」(`memory.py:446-454`)。白名单测试钉的是「仓库里只有 `_build_human_message` 写这个标记」(`services/control-plane/tests/test_user_input_whitelist.py:72-96`),补消息调用它而不是自己写标记,测试规则不用放宽;要改的是它的 `sent_at` 不再固定取当前时间(`api/runs.py:521`)。
3. **第二次提炼**:客户消息是 `[user]` 行;健管师消息所在段没有用户行,只有一条 `[assistant]` 行,带它自己的时间(`memory.py:605-610`)。
4. **不重复提炼**:后台写回的起点是「同一会话上一个任务的消息数」(`control_plane/memory/writeback_processor.py:326-347`)。补消息追加在上一轮结束之后,序号一定 ≥ 上一个任务的消息数,只会被下一个任务提炼一次。
5. **会话第一次写回**:没有更早的任务时,起点按时间:开段消息的 `expert_work_created_at` 在 30 天内的段才算新段(`writeback_processor.py:130,338-341`;`memory_slice.py:86-94`)。补进来的消息用企微时间,超过 30 天的不提炼。首次切换只补最近 48 小时(§4.3),不受影响。
6. **退回同步写回时的局限**:任务落不进表、退回同步写回时,只提炼本轮最后一段(`memory.py:3160-3167` `_this_turn`),这一批补消息那一次不提炼;下一个后台任务的起点早于它们,会补上。这是现有退回逻辑的既有行为,不另处理。
7. **未送达**:见 §3.8。

### 3.10 威胁模型

- **防**:
  - 客户冒充健管师:角色由对接方按发送账号判定,客户在企微里打什么字都只能是 customer。
  - 客户在消息里写一段假的时间标注混淆模型:标注只由平台按存储的时间生成;客户写的同样字样留在正文里,影响有限(§6-13 列出,第一期不转义)。
  - 补消息被当成平台指令:客户消息是用户角色,健管师消息是助手角色,都不是系统消息,也不进 `<platform-context>`。
  - 补消息正文进日志:平台与深护只记条数、外部身份的前 8 位,不记正文、不记发送人名。
- **不防**:持有对外 API key 的对接方(可信)。它本来就能在 `input` 里写任何东西,现在还能以助手身份写消息。
- 超出范围只列不做:对接方被入侵后伪造大批助手消息诱导模型——靠对接方自己的密钥管理(08 文档)。

---

## 4. 改动清单

### 4.1 Expert Work 平台

行号均为 origin/main `d9db26f79`(与测试环境镜像 `5908fe554` 代码相同,`d9db26f79` 只改了部署清单和验收文档)。

**PR-1 补消息**

| # | 改什么 | 在哪 | 说明 |
|---|---|---|---|
| P-1 | 请求体加三个字段 | `api/agents.py:855-940` `ExternalRunRequest` | `backfill`、`backfill_omitted_before`、`input_message_ids`(§5.1)。结构、上限、NUL 字节、时区、同批 id 重复都用这个模型上的 validator 拦,走 FastAPI 统一 422,在建会话之前,无副作用(与 `_no_nul_*` 同一路径)。幂等指纹自动覆盖新字段(`api/_idempotency.py:42-56` 对整个请求体 `model_dump`)。 |
| P-2 | 内部请求体与传递 | `api/runs.py:290` `RunRequest`;`api/agents.py:1897-1905` 构造处 | 加同样三个字段,只有对外端点会填。 |
| P-3 | 新模块 `control_plane/backfill.py` | 新文件 | `write_backfill(graph, *, thread_id, tenant_id, messages, recorder) -> BackfillOutcome`,`messages` 由调用方先用 P-5 的 `build_backfill_messages` 构造好传入(本模块不 import `api/runs.py`):读检查点 → 收集已有身份(补消息的 `external_id` + 各轮输入的 `expert_work_external_ids`)→ 去重 → 内容兜底(§3.4 第 3 层)→ 按 `sent_at` 排序 → 一次 `aupdate_state`(config 只带 thread_id / tenant_id,`as_node` 同 `trigger_delivery.py:143`)→ 更新会话消息数(同 `trigger_delivery.py:145-153`)。返回 `received / written / skipped_duplicate / skipped_as_run_output`。 |
| P-4 | 三个写入点 + 排队落库 | `api/runs.py:1479` 之后(仅空闲流式)、`:1642` `_launch`、`run_queue_worker.py:340` 之后;`api/runs.py:1497-1504,1529-1536` | §3.5。写入失败:空闲流式回 503 `BACKFILL_WRITE_FAILED`(撤掉请求自建的会话);排队路径按现有收口方式收成 error,错误串 `backfill_write_failed`。`BackfillOutcome` 写进 run 行 `backfill_summary`(迁移见 P-13,随 PR-1 先合)。 |
| P-5 | `_build_human_message` 加两个关键字参数 | `api/runs.py:475-547` | `sent_at: datetime \| None = None`(缺省仍取当前时间,`:521`);`placeholder: str \| None = None`(非文字消息的占位,排在正文之后,不计入 `text_chars`)。同文件新增 `build_backfill_messages(built, thread_id, entries)`:customer 条目调 `_build_human_message` 构造,staff 条目构造 `AIMessage`,统一加标记、确定性 id、`expert_work_created_at`。放在 `runs.py` 而不放进 `backfill.py`:白名单测试按文件判定写标记的只有 `runs.py`;`runs.py` 又要调 `backfill.py` 的写入函数,构造放进 `backfill.py` 会互相引用。`run_queue_worker` 已经经 `_enqueued_input.py` 用到 `runs.py` 的构造函数,照此引用。 |
| P-6 | 输入带外部身份 | `api/runs.py:550-590` `build_run_graph_input`;`api/_enqueued_input.py:22-49` | 有 `input_message_ids` 时给本轮用户消息加 `expert_work_external_ids`(列表),排队路径从 `enqueued_input` 读回。 |
| P-7 | 公共标记与开段规则 | `packages/expert-work-common/src/expert_work/common/conversation_channel.py:68-71,152-166` | 新常量 `BACKFILL = "expert_work_backfill"`、`is_backfill()`;`opens_segment` 对带 `BACKFILL` 的助手消息返回真。这一改动同时作用于 `final` 判定(上一轮回答不被抢走)和记忆切段,两处正是想要的。 |
| P-8 | 首轮判断排除补消息 | `orchestrator/graph_builder/agent_notes.py:125-133` `_first_turn` | 现在按「开段数 ≤ 1」判断是不是第一轮;首次切换补了 20 条,开段数远大于 1,会话第一轮就不再注入记忆索引段(B-236)。改成只数不带 `BACKFILL` 的开段消息。 |
| P-9 | 读面 | `control_plane/transcript.py:82-122`(`MessageTurn` 加 `origin`、`external_id`);对外 `api/external_sessions.py:307-322`;控制台 `api/runs.py:2341` 起 | `/messages` 每条新增 `origin`(`run` / `backfill`)、`external_id`(仅补消息有)。 |
| P-10 | 控制台对话页 | `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts:116-133`;`apps/admin-ui/src/api/sessions.ts:131-150` `HistoryMessage` | 分组时跳过 `origin=backfill` 的行,不再因为它们没有 run_id 整段退回按顺序配对;把它们按时间渲染成「会话外消息」行,标出角色和时间。 |
| P-11 | 对外文档 | `apps/admin-ui/docs-site/guide/` | §4.1 末「对外文档要补的说明」。 |
| P-12 | 用户输入白名单说明 | `common/user_input.py` 模块注释;`tests/test_user_input_whitelist.py` | 注释里「控制台与排队输入两个入口」改成三个入口(加补消息);测试加两条:`sent_at` 传入时用传入值;`placeholder` 不计入 `text_chars`。 |

**PR-2 未送达**

| # | 改什么 | 在哪 | 说明 |
|---|---|---|---|
| P-13 | 迁移 | `packages/expert-work-persistence/migrations/versions/`(下一个可用编号,当前最新 0192);模型 `persistence/models/agent_run.py` | `agent_run` 加 `undelivered_at timestamptz null`、`undelivered_reason text null`、`backfill_summary jsonb null`。三列一个迁移,**随 PR-1 合**(PR-1 要写 `backfill_summary`),PR-2 只用。 |
| P-14 | 接口 | `api/external_runs.py:615` 旁 | `:undelivered`(§5.3)。用 `load_owned_run` 校验归属;非终态 409;幂等;审计记 `run.undelivered`。 |
| P-15 | 每轮把未送达的 run_id 放进 config | `api/runs.py:1794`(`_start_run`)、`:1198`(审批续跑)、`run_queue_worker.py:397`、`orphan_sweep.py:415` | 与 `run_segment_config` 并排加一个 `undelivered_run_ids`(本会话所有带 `undelivered_at` 的 run)。定时任务的临时会话(`trigger_firing.py:296`)不需要。 |
| P-16 | 提示词视图剔除 | `builder.py:850` 之后;公共函数放 `common/supersede.py` 旁 | `undelivered_final_ids(messages, run_ids) -> set[str]`:按 run 戳分组(复用 `group_messages_by_run`),取每组最后一条不带工具调用、有正文的助手消息 id。视图剔除这些 id。 |
| P-17 | 记忆排除 | `memory.py:583-620` `_turn` / `:623` `_extraction_inputs`;`writeback_processor.py:309-321` | 处理器查本会话未送达的 run → 算 id 集合 → 传进提炼;`_turn` 的 `finals` 排除这些 id。同步写回路径从 config 取。 |
| P-18 | 读面 | `external_session_items.py:479-507`(`runs[]`)、run 列表、两个 `/messages` | `runs[]` 加 `undelivered`、`undelivered_reason`、`backfill`(即 `backfill_summary`);`/messages` 那条回答加 `undelivered: true`;控制台显示「未送达」。 |
| P-19 | 内容兜底跳过未送达轮 | P-3 的模块 | §3.4 第 3 层。 |

**PR-3 消息时间**

| # | 改什么 | 在哪 | 说明 |
|---|---|---|---|
| P-20 | 时间标注 | 新模块 `graph_builder/message_times.py`;接在 `builder.py:850`(P-16 之后) | §3.7:会话里有补消息才开;给不隐藏的用户消息和补进来的助手消息生成带标注的副本(字符串正文前加;内容块列表在最前面插一个文字块);用 `resolve_agent_timezone`。 |
| P-21 | 说明一句 | `graph_builder/platform_context.py` | 会话有补消息的步骤往 `<platform-context>` 里加一行(§3.7)。 |

**每个 PR 都要**:往 `docs/runbooks/pending-test-release-acceptance.md` 加对应的真栈用例(§7.2);日志与指标只记条数(`backfill_written_total`、`backfill_skipped_total{reason}`、`run_undelivered_total{reason}`),不记正文。

**对外文档要补的说明**(`apps/admin-ui/docs-site/guide/`):

- `chat.md`:§2.3 参数表加三个字段;新增一节「会话外发生的消息(补消息)」:什么时候用、身份怎么取(同一会话内唯一且稳定)、要排除什么(你自己发出去的那一轮回答)、平台怎么去重、写入时机、上限、顺序、在 `/messages` 里怎么出现、对长期记忆的影响(customer 条目算用户原话)、与 `Idempotency-Key` 的关系(重试必须带同一批);§2.5「上一轮还没结束时」补一句:排队的轮补消息在轮到它时才写。
- `run-control.md`:新增「标记未送达」一节:什么时候调、效果、只剔最终回答、时序局限(已写的记忆不撤回)、错误码。
- `query.md`:§5.3 历史消息加 `origin`、`external_id`、`undelivered`;§5.4 run 列表加 `undelivered`、`undelivered_reason`、`backfill`。
- `errors.md`:新错误码 `RUN_NOT_FINISHED`(409)、`BACKFILL_WRITE_FAILED`(503)与补消息的 422 文案;run 的 error 串 `backfill_write_failed`。
- `best-practices.md` §9.5 加一问:「人工客服与智能体共用一个账号时,怎么让智能体知道人工说过什么」。
- `changelog.md` 记一条。

### 4.2 深护 MCP

无改动。

### 4.3 深护数字人服务 · 项目服务 · 会话存档服务

路径缩写:DHA = `deep-ai-health-digital-human-agent`,SYNC = `deep-ai-health-sync-cpwx-pad-msg-service`,ARCH = `deep-ai-health-cpwx-archive-save-service`,PS = `deep-ai-health-project-service`。

| # | 改什么 | 在哪 | 说明 |
|---|---|---|---|
| S-1 | 发送登记表 | 项目库新表 `t_project_ai_sent_msg`(§5.6) | pad 由 DHA 写,dk 由 ARCH 写,缺口查询读。 |
| S-2 | pad 登记 | DHA `send-agent-answer.processor.ts:78-107`(文字)及 :174-192、:243-261、:311-329、:385-403(媒体) | 发送任务新增 `ew_session_id`、`ew_run_id`、`part_seq`(取回 run 结果后入队时带上;提醒、兜底话术不带)。每条发送成功后,带 `ew_run_id` 的才写登记:`msg_key = "pad:" + 回执.app_info`,`match_kind = receipt`。回执缺 `app_info` 时不登记,记一条计数日志(不打正文),交给平台内容兜底。登记直接写库,不经同步队列(回执缺 `msgtype` 时同步队列会静默丢弃该任务,SYNC :126-129)。 |
| S-3 | pad 竞态修复 | DHA 发送前;SYNC `sync-pad-message.processor.ts:629-637,713-765` | 复用 dk 的做法:DHA 对**所有**以「不是真人」身份发出的消息(回答、提醒、兜底话术),在调 pad 发送**之前**写 `AI_SENT` 标记(同一工具 `ai-sent.util.ts` 的键格式与文字归一化;键里的员工用 pad id)。SYNC 在「员工本人、`!is_digital_human`」分支先查这个标记,命中就按数字人处理:不写接管标志、不调 `processReferralHuman`。回写任务后到时仍按现在的逻辑直接 return。效果:谁先到都一样。SOP、用药提醒、唤醒、MCP 发送等其它以员工号发消息的服务也有同样的竞态(它们同样只靠回写带 `is_digital_human`),第一期只修数字人服务自己发的,其它服务照同一做法跟进;开放接口服务的 pad 发送现在不带 `is_digital_human`(`deep-ai-health-open-api-service` `pad-send.service.ts:183-194`),按真人处理,不在本次范围。 |
| S-4 | dk 登记 | DHA `pc-send-task.service.ts:315,521-541`;`t_project_cpwx_pc_send_task` 加列 `ew_session_id`、`ew_run_id`;ARCH `human-answered.service.ts:193-218` | 发放任务写 `AI_SENT` 时,带 `ew_run_id` 的任务再写一份登记用标记 `AI_SENT_RUN:{项目}:{员工}:{对象}`(成员 = 原成员 + run_id + 分段序号,600 秒)。ARCH `isAiSent` 命中后,查同一时间窗里的 `AI_SENT_RUN`:文字按指纹取、媒体按类型取时间最近且还没被登记的那一个,写登记 `msg_key = "dk:" + msgid`,`match_kind = fingerprint`。现有接管判断逻辑一字不改。 |
| S-5 | 同步位置表 | 项目库新表 `t_project_ew_session_sync`(§5.6) | 每个平台会话(= 客户 + 智能体,私聊和各群共用,已拍板)每个通道一行,记本会话已经补到消息表的哪一行(按入库自增 id)。要不要并进 01 复用的会话表 `t_project_expert_work_session`(11-17 定复用,01 §5.4),由深护实现时定,不影响任何接口。 |
| S-6 | 缺口查询 | DHA `src/modules/message/` 新函数,复用 `message.service.ts:142-192`(pad)、`:296-331`(dk)的范围条件 | 规则见 §5.7。 |
| S-7 | 调用改造 | DHA `invoke-agent.processor.ts:746-801`(汇总本批)、`:844-866`(请求);PS `src/modules/expert-work/service/client/expert-work.types.ts:42-48` `EwRunRequest` 与发起请求处 | 本批成员 `msg_app_info` 加通道前缀成 `input_message_ids`;调缺口查询得到 `backfill` 与 `backfill_omitted_before`;经项目服务原样转给平台。另外把「这一轮要回答的客户消息」的身份作为 `source_msg_keys`(01 §5.1)传给项目服务:本批,加上 S-8 重跑时的被取消那批;项目服务加通道前缀生成 inputs 变量 `source_message_ids`(给深护 MCP 写工具判重,04 §6.16)。`backfill` 不变。缺口查询的结果随 `invoke_token` 存一份,同一次调用重试时原样复用(否则幂等键报 `IDEMPOTENCY_KEY_REUSED`)。 |
| S-8 | 打断后的重跑改造 | DHA `invoke-agent.processor.ts:875-990` | 现在被取消那一批会合并进重跑的 input。改成:重跑的 `input` 只放取消之后新到的消息;被取消那一批的客户消息不进 input,由缺口查询作为补消息带上(同步位置在取消时不前进,所以它们一定在缺口里),平台按身份判断它们是否已在会话里(§3.2 a)。被取消那批的身份**仍要放进 `source_msg_keys`**(S-7),否则模型重跑时重复写入判不出来。 |
| S-9 | 同步位置前进 | DHA 调用结束处 | run 以 success / incomplete 结束才前进到这次缺口查询扫过的最大行号;error、取消、请求被拒都不前进(下次重扫,平台去重)。 |
| S-10 | 未送达回报 | DHA `send-agent-answer.processor.ts` 发送入口(`handlerProcessSendAgentAnswer`,覆盖 :415-417、:466-468、:477-480、:507-509 的静默返回和 :770 的兜错);dk 任务终态处(`pc-send-task.service.ts:301-305,595-631,667-698`);取消处;PS `expert-work-client.ts:572` 旁加 `markUndelivered` | 规则见 §3.8 表。发送前接管命中现在不留记录,改成记一条调用日志(复用 `t_project_ai_agent_invoke_log` 的 result_type)。 |
| S-11 | 新会话补最近一段 | S-6 | 平台会话第一次被调用(没有同步位置)时,补最近 48 小时、最多 30 条(§9-9)。首次切换走这条(01 已拍板不按空闲换会话;404 重建会话时也走)。 |
| S-12 | 日志 | 新增代码 | 不打印正文、客户名、发送人名(深护现有日志问题见 08 文档)。 |

### 4.4 智能体配置(06 文档落地)

- 系统提示词说明业务含义:会话里健管师一方的消息有的是你写的,有的是同事在别的时间直接发的,都算你说过的;注意时间,已经提醒过的不重复,答应过的事要跟进。
- Dify 用「历史为空」判断首轮并补问基础信息(DSL 节点 `1761376427497` / `1761376653874`)。迁移后首次切换会补最近一段,会话不再为空;首轮补问改由数字人服务查消息表传 `is_first_contact`(沿用 Dify 口径「这名员工与这个客户之间从没有过文字消息」,已拍板 11-17),模型再看档案缺项(02 待拍板 6、06 节 N);不依赖会话是否为空,补消息不影响它。

### 4.5 ROADMAP 登记建议

不另开编号,在 B-314 一行(若还没有就新建这一行)里写子项:

> **B-314-03 平台:补消息 / 未送达 / 消息时间**(设计 `specs/2026-11-17-b314-dify-migration/03-human-handoff-backfill.md`)—— PR-1 补消息(对外字段、开跑前写入、开段与首轮判断、`/messages` 与控制台显示、文档);PR-2 未送达(`agent_run` 两列 + `:undelivered` 接口 + 模型视野与记忆提炼剔除最终回答);PR-3 消息时间(提示词视图时间标注,只在有补消息的会话开)。依赖:技术上无(PR-1 的记忆部分要在 B-300 之上验);排期已拍板放在迁移最后一个阶段。深护侧 S-1~S-12 在 01 / 02 的业务方改造上线之后做。

---

## 5. 接口与数据结构

### 5.1 `POST /v1/agents/{agent_code}/runs` 新字段

| 字段 | 类型 | 必填 | 上限 | 说明 |
|---|---|---|---|---|
| `backfill` | `BackfillMessage[]` | 否 | 50 条;所有 `content` 合计 ≤ 65,536 字符 | 会话外发生、会话里还没有的消息 |
| `backfill_omitted_before` | int | 否,默认 0 | 0 ~ 1,000,000 | 比 `backfill` 第一条更早、这次没带的条数 |
| `input_message_ids` | string[] | 否 | 50 个,每个 1~255 字符 | 本轮 `input` 由哪些外部消息拼成,用于以后去重 |

`BackfillMessage`:

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `external_id` | string | 是 | 1~255 字符;同一会话内唯一、重试时不变 |
| `role` | `"customer"` \| `"staff"` | 是 | customer = 会话的用户本人;staff = 以智能体身份说话的一方 |
| `sent_at` | string | 是 | ISO 8601,必须带时区;不得晚于平台当前时间 10 分钟以上 |
| `type` | `"text"` \| `"voice"` \| `"image"` \| `"file"` \| `"video"` \| `"link"` \| `"other"` | 否,默认 `text` | |
| `content` | string | text 必填非空;其它可空 | ≤ 4,000 字符。text = 正文;voice = 转写(没有就空);file / link = 名称或标题;其它可空 |
| `sender_name` | string | 否 | ≤ 64 字符;只在 staff 有多个人时用 |

校验(全部 422,在建会话之前):条数、单条与合计长度、NUL 字节、`role` / `type` 取值、`sent_at` 无时区或太靠后、text 正文为空、同一请求里 `external_id` 重复、某个 id 同时出现在 `backfill` 与 `input_message_ids`。

正文与占位的构造规则(平台):

| type | 进会话的正文 | customer 条目里算不算「用户原话」 |
|---|---|---|
| text | `content` | 算 |
| voice | `content`(转写)+ 空行 + `[voice message]` | 转写部分算 |
| image | `[image]` | 不算 |
| file | `[file: {content}]` 或 `[file]` | 不算 |
| video | `[video]` | 不算 |
| link | `[link: {content}]` 或 `[link]` | 不算 |
| other | `[other message]` | 不算 |

占位文字是平台文字,不带行业词;staff 消息同样规则,只是没有「用户原话」一说。

示例:

```json
{
  "user_id": "wx:demo:cus:10086",
  "session_id": "0b0f5a3e-2c1d-4b8a-9f61-3d2c9e7a1b20",
  "input": "调整好了吗?",
  "input_message_ids": ["pad:APPINFO_0007"],
  "backfill_omitted_before": 0,
  "backfill": [
    {"external_id": "pad:APPINFO_0003", "role": "customer", "sent_at": "2026-11-17T10:05:12+08:00", "type": "text", "content": "[私聊]我想换个运动方案"},
    {"external_id": "pad:APPINFO_0004", "role": "staff", "sent_at": "2026-11-17T10:07:40+08:00", "type": "text", "content": "[私聊]好的,我下午帮您调整"},
    {"external_id": "pad:APPINFO_0005", "role": "customer", "sent_at": "2026-11-17T10:30:02+08:00", "type": "image"},
    {"external_id": "pad:APPINFO_0006", "role": "staff", "sent_at": "2026-11-17T14:00:00+08:00", "type": "text", "content": "[私聊]该测餐后血糖了"}
  ]
}
```

响应形状不变。写入结果(`received / written / skipped_duplicate / skipped_as_run_output`)在 run 写入后可从 run 列表、`/items` 的 `runs[]` 读到(`backfill` 字段)。

### 5.2 run 上的新字段(run 列表、`/items` 的 `runs[]`)

| 字段 | 类型 | 说明 |
|---|---|---|
| `undelivered` | bool | 是否被标未送达 |
| `undelivered_reason` | string \| null | 标记时传的原因 |
| `backfill` | object \| null | `{received, written, skipped_duplicate, skipped_as_run_output}`;本轮没带补消息为 null |

### 5.3 `POST /v1/agents/{agent_code}/runs/{run_id}:undelivered`

请求:

```json
{"user_id": "wx:demo:cus:10086", "reason": "send_failed"}
```

| 字段 | 规则 |
|---|---|
| `user_id` | 必填,run 必须属于这个用户和这个智能体,否则 404 `RUN_NOT_FOUND`(不透露存在与否) |
| `reason` | `send_failed` / `human_took_over` / `cancelled` / `other`,默认 `other` |

规则:run 不是终态(queued / running / paused)→ 409 `RUN_NOT_FINISHED`;重复调用返回同一结果(保留第一次的时间和原因);只影响之后开跑的轮。

响应:

```json
{"success": true, "data": {"run_id": "…", "undelivered": true, "had_final_answer": true}, "error": null}
```

`had_final_answer` 告诉调用方这一轮在会话里有没有最终回答(没有时标记只是记录,不影响模型)。

### 5.4 `GET …/sessions/{session_id}/messages` 新字段

| 字段 | 说明 |
|---|---|
| `origin` | `run`(某一轮的输入或输出)/ `backfill`(补进来的) |
| `external_id` | 补消息的外部身份;其它为 null |
| `undelivered` | 这条是被标未送达那一轮的最终回答时为 true |

补消息的 `role` 照常是 `user` / `assistant`,`run_id` 为 null,`created_at` 是对接方给的 `sent_at`。

### 5.5 检查点里的消息形状(给平台开发看)

```python
# 补进来的 customer 条目
HumanMessage(
    id=str(uuid5(BACKFILL_NAMESPACE, f"{thread_id}:{external_id}")),
    content="我想换个运动方案",
    additional_kwargs={
        "expert_work_user_input": {"text_chars": 8, "images": 0, "files": 0,
                                   "external_blocks": 0, "sent_at": "2026-11-17T02:05:12+00:00"},
        "expert_work_backfill": {"external_id": "pad:APPINFO_0003", "role": "customer",
                                 "type": "text", "omitted_before": 0},
        "expert_work_created_at": "2026-11-17T02:05:12+00:00",
        # 不盖 expert_work_run_id
    },
)

# 补进来的 staff 条目
AIMessage(
    id=str(uuid5(BACKFILL_NAMESPACE, f"{thread_id}:{external_id}")),
    content="好的,我下午帮您调整",
    additional_kwargs={
        "expert_work_backfill": {"external_id": "pad:APPINFO_0004", "role": "staff",
                                 "type": "text", "sender_name": None},
        "expert_work_created_at": "2026-11-17T02:07:40+00:00",
    },
)

# 每一轮的输入(只多一个键)
HumanMessage(..., additional_kwargs={..., "expert_work_external_ids": ["pad:APPINFO_0007"]})
```

`omitted_before` 只记在这一批写入的第一条上。

### 5.6 深护的两张新表(MySQL,建议)

```sql
-- 发送登记:这条企微消息是哪一轮的输出
CREATE TABLE t_project_ai_sent_msg (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  project_code  VARCHAR(80)  NOT NULL,
  employee_code VARCHAR(80)  NOT NULL,
  channel       VARCHAR(8)   NOT NULL,            -- pad / dk
  msg_key       VARCHAR(255) NOT NULL,            -- "pad:" + app_info 或 "dk:" + msgid
  ew_session_id CHAR(36)     NOT NULL,
  ew_run_id     CHAR(36)     NOT NULL,
  part_seq      INT          NOT NULL DEFAULT 0,  -- 这一轮的第几条气泡
  match_kind    VARCHAR(16)  NOT NULL,            -- receipt / fingerprint
  sent_at       DATETIME     NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_msg (project_code, msg_key),
  KEY idx_run (ew_run_id)
);

-- 同步位置:每个平台会话已补到消息表的哪一行
CREATE TABLE t_project_ew_session_sync (
  id                 BIGINT PRIMARY KEY AUTO_INCREMENT,
  project_code       VARCHAR(80)  NOT NULL,
  channel            VARCHAR(8)   NOT NULL,       -- pad / dk,决定下面的行号属于哪张消息表
  customer_key       VARCHAR(100) NOT NULL,       -- pad: customer_pad_userid;dk: external_userid
  ew_session_id      CHAR(36)     NOT NULL,       -- 会话 = 客户 + 智能体,私聊和各群共用(已拍板)
  last_msg_row_id    BIGINT       NULL,           -- 消息表自增 id;NULL = 还没同步过
  last_synced_at     DATETIME     NULL,
  created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_session (ew_session_id, channel),
  KEY idx_customer (project_code, customer_key)
);
```

用入库自增 id 而不是发送时间当位置:dk 会话存档有拉取延迟,晚入库的消息发送时间可能早于位置,按时间会永远漏掉;按入库顺序它一定排在位置之后(§9-11)。

### 5.7 缺口查询规则(DHA)

输入:会话的同步位置行、本批消息的 id 列表。

1. **范围**:会话不分私聊 / 群(已拍板),所以一次查「这个客户在私聊 + 他所在各群里、与该客户相关的消息」:私聊 = 该客户与绑定本智能体的员工号之间的双向消息;各群里的取法照抄历史接口(普通群只取该客户发的 + 员工号 @ 该客户的;多对一服务群另含群内内部员工;`message.service.ts:157-192`、`:310-331`)。pad 查 `t_project_cpwx_pad_msg`,dk 查 `t_project_cpwx_chat_msg`;`is_revoke = 0`(dk 排除 revoke 类型);**不限消息类型**。每条记下来自私聊还是哪个群(第 9 条)。
2. **起点**:`id > last_msg_row_id`。没有位置时:发送时间在最近 48 小时内的最后 30 条。
3. **排除**:本批消息;`msg_key` 在发送登记表里的。
4. **排序与上限**:按 `id` 升序;超过 50 条只取最后 50 条,其余条数填 `backfill_omitted_before`。
5. **角色**:发送人是该客户 → customer;是本员工号或多对一群的内部员工 → staff(内部员工带 `sender_name`,本员工号不带)。
6. **正文与类型**:文字取 `msg_data.content`(去掉 @ 文本,同 `message.service.ts:249-252`);语音 → `type=voice`,有 `speech_to_text` 就放进 `content`,没有不现场转写;图片 / 文件 / 视频 / 链接卡片 / 其它按 §5.1 的 type,文件和链接把名称、标题放进 `content`。
7. **外部身份**:pad `"pad:" + msg_app_info`,dk `"dk:" + msgid`(与 `input_message_ids` 同一规则,平台才能跨两者去重)。
8. **返回**:`backfill`、`backfill_omitted_before`、本次扫过的最大 `id`(用于 S-9 前进位置)。
9. **来源窗口**:会话合并了私聊和各群,补进来的每条要让模型知道是在哪儿说的。第一期由深护在 `content` 开头加简短来源(「[私聊]」「[群聊]」,不写群名,群名可被成员改),平台不改;要不要做成平台字段见 §9-14。

---

## 6. 异常与边界

1. **发送登记失败**(pad 回执缺 `app_info`;dk 指纹没对上,如 RPA 改了文字):那条回答被当成员工消息补进来。平台内容兜底多数能拦住;拦不住时同一段话在会话里出现两次。
2. **登记还没写完就做了缺口查询**:回答发出到下一次调用至少隔着客户开口 + 8 秒攒消息,正常不会发生;发生了同第 1 条。
3. **dk 会话存档延迟**:晚入库的消息下一次调用才补,位置按入库 id 前进,不会漏;它在会话里排在更晚的位置,靠时间标注体现真实时间。
4. **员工在本轮生成期间手打**:它的企微时间早于本轮回答,但会在下一次调用时追加在本轮回答之后。时间标注写的是真实时间。
5. **消息补进来之后被撤回**:会话里不撤(平台不提供删除补消息的接口)。撤回通常是发错,影响有限;以后需要再加。
6. **超过 50 条**:只补最后 50 条,第一条标注写「更早还有 N 条没带」。智能体可在提示词里被告知此时不要假装知道更早的内容。
7. **会话正忙**(深护有同一客户锁,正常不会):本轮排队,补消息在轮到它时写,写在前一轮回答之后。
8. **重试**:同一次调用重试必须带同一批补消息和同一个幂等键;深护按 `invoke_token` 缓存缺口查询结果(S-7)。换了一批又用同一个键会得到 422 `IDEMPOTENCY_KEY_REUSED`。
9. **本轮被取消在排队时**:它的输入从没写进会话;下一次调用里这批消息作为补消息出现(S-8),平台写入。
10. **重新生成 / 编辑重发**:补消息没有 run 戳,不会被剔除,也不会被带进重放的原件。
11. **长会话变大**:补消息让会话增长更快。会话已拍板不切(01 §3.4.3),压缩门槛见 01 §3.4.4;压缩时补消息和其它消息一样会被摘要,摘要里不保证留时间。
12. **模型模仿时间标注**:见 §3.7 风险;A3 盯住。真出现时可加输出侧剥除(只剥回答开头匹配平台格式的那一段),列为后续。
13. **客户自己打出像时间标注的文字**:第一期不转义;观察到被利用再仿照 `<platform-context>` 的伪造转义处理。
14. **连续多条助手消息、连续多条用户消息**:补消息常造成同角色连续。平台现在已有这种情况(定时投递紧跟上一轮回答、隐藏的本轮输入段紧跟用户消息),但没有专门合并同角色消息的代码;验收要在平台已上架的几家模型上各跑一遍 A1(§7.2),哪家报错再在适配器里合并。
15. **智能体用结构化输出**:它自己的回答在会话里是 JSON 原文,补进来的健管师消息是纯文字,模型看到两种格式。结构化输出不合格会带 schema 重发(`builder.py:1655-1700`),影响有限;小评测里看重发率有没有上升。
16. **主回复改由智能体调 MCP 发送(02 的另一选项,11-17 已拍板不采用)**:MCP 调用不带 run 上下文(`tools/mcp.py:1434-1436`),发出去的消息无法登记到轮次,会以员工消息补回来,与工具调用里的同一段话重复。本文按「业务方取回结果后发」设计。
17. **群里其他客户 @ 健管师问问题**:不在该客户的范围里,不会补进该客户的会话(已拍板 4);其他客户自己的会话里会有。
18. **会话第一次写回只看最近 30 天**:补进来的超过 30 天的消息不提炼(§3.9 第 5 条)。

---

## 7. 测试与验收

### 7.1 平台自测(单元 / 集成,随 PR)

| # | 测什么 | 没修好时会怎样 |
|---|---|---|
| U1 | 补消息写入后,`/messages` 顺序、`origin`、`created_at`、无 run_id;同一批重放 `written=0, skipped_duplicate=N` | 重复出现 / 时间是写入时刻 |
| U2 | `input_message_ids` 里的 id 再作为补消息出现时跳过 | 被打断场景同一句出现两次 |
| U3 | 补一条 staff 后,上一轮回答的 `channel` 仍是 `final` | 上一轮回答变 `commentary`,对接方取不到 |
| U4 | 记忆切段:客户补消息带白名单标记、`sent_at` 是传入值;staff 补消息自成一段、上一轮回答仍是上一段的 `[assistant]` | 第一次提炼看不到客户原话;原回答被顶替 |
| U5 | `_first_turn`:首次补 20 条后第一轮仍注入记忆索引段 | 记忆索引段永远不注入 |
| U6 | 开了长期记忆的智能体:写补消息后跑一轮,`memory_writeback` 只在本轮结束时跑一次,本轮输入没被吞 | 补消息触发多余写回,或本轮输入被丢 |
| U7 | 三个写入点:空闲流式 / 排队流式 / queue 模式都在开跑前写入;忙会话不写 | 排队轮的补消息丢失 |
| U8 | 422 校验各条(§5.1),且被拒请求没建会话 | 留下空会话 / 500 |
| U9 | 重新生成补消息之后的那一轮:补消息仍在提示词视图里 | 重新生成把补消息一起剔掉 |
| U10 | 未送达:提示词视图只剔该轮最终回答,客户消息和工具调用留着;记忆 `_turn` 不取它;非终态 409;幂等 | 整轮被剔 / 客户的话也没了 |
| U11 | 内容兜底:与最近回答相同的 staff 跳过;未送达轮不参与;customer 不参与;短于 6 字不参与 | 员工重发的未送达回答被吞 |
| U12 | 时间标注:只在有补消息的会话出现;智能体自己的回答没有;同一会话两步之间标注逐字节相同;检查点里没有标注 | 破缓存 / 标注落库 |
| U13 | 白名单测试(既有)仍过;新增 `sent_at`、`placeholder` 两条 | 别处偷写标记 |

### 7.2 平台真栈验收(测试环境,写进 `pending-test-release-acceptance.md`)

准备:测试租户里一个开了长期记忆、开了结构化输出的测试智能体;对外 API key;每条用例用新的 `user_id`。用例文字避开注入防御会拦的说法。

| # | 怎么触发 | 看哪里 | 什么算过 | 没修好时会怎样 |
|---|---|---|---|---|
| A1 | 发起一轮,带 4 条补消息(客户 2 条含 1 张图占位、staff 2 条),`sent_at` 设在 2 小时前 | `GET …/messages` | 4 条按 `sent_at` 排在本轮输入之前,`origin=backfill`,`created_at` 是给的时间,`run_id` 为 null;本轮回答 `final` | 消息缺失、时间是写入时刻、或本轮报错 |
| A2 | A1 的会话再发一轮:同一批 4 条补消息,外加 1 条 customer 条目(`external_id` 等于 A1 那一轮 `input_message_ids` 里的值),新输入 | `/messages`;`/items` 的 `runs[].backfill` | 没有新增补消息行;`written=0, skipped_duplicate=5` | 重复行 |
| A3 | 补一条 staff「记得明早空腹测血糖」(`sent_at` 今天 14:00),输入「你今天几点跟我说测血糖的事?」 | 回答;Langfuse 里本轮提示词 | 回答说出 14:00 / 下午两点;提示词里补消息和用户消息带 `[YYYY-MM-DD Ddd HH:MM]`,智能体自己的回答没有;回答开头没有 `[20` | 答不出时间;标注出现在自己的回答上;回答模仿标注 |
| A4 | A1 之后再补一条 staff | `/messages` 上一轮回答的 `channel` | 仍为 `final` | 变成 `commentary` |
| A5 | 补一条客户消息「我对青霉素过敏」(`sent_at` 两天前)+ 一条 staff「建议每天走 6000 步」,本轮输入随意;等写回完成 | 控制台记忆管理页该用户 | 有一条来源为用户自述的过敏事实,事件日期是两天前那天;「6000 步」若被记下,来源是助手推断;上一轮回答相关的记忆不受影响 | 过敏没记 / 来源成了任务说明 / 日期是今天 |
| A6 | 跑 R1 得到回答;调 `:undelivered`;再发一轮问「你刚才说了什么?」 | 回答;Langfuse 提示词;`/items` `runs[]`;`/messages` | 提示词里没有 R1 的回答但有 R1 的输入;R1 `undelivered=true`;回答不复述 R1 的内容 | R1 整轮消失或仍在 |
| A7 | 对一个正在跑的长轮(调慢工具)用 queue 模式发 R2 带补消息 | `/messages` | 补消息在 R1 回答之后、R2 输入之前,一条不少 | 补消息丢失(被 R1 写回覆盖) |
| A8 | 逐条发不合格请求(51 条、无时区、未来 1 小时、同批 id 重复) | 响应;会话列表 | 都是 422,会话列表没有新增会话 | 500 或留空会话 |
| A9 | 同一 `Idempotency-Key` 同一请求体发两次 | `/messages`;run 列表 | 同一个 run,补消息只写一次 | 两个 run / 两份补消息 |
| A10 | A1 的会话在控制台打开对话页 | 页面 | 各轮按轮显示,补消息以「会话外消息」行显示在对应位置 | 整段退回平铺 |
| A11 | 对正在运行的 run 调 `:undelivered` | 响应 | 409 `RUN_NOT_FINISHED` | 200 且影响正在跑的轮 |
| A12 | A1 在平台已上架的每家主力模型各跑一次 | run 状态 | 全部成功 | 某家因同角色连续消息报 400(§6-14) |

### 7.3 深护联调(测试环境)

| # | 怎么触发 | 看哪里 | 什么算过 | 没修好时会怎样 |
|---|---|---|---|---|
| D1 | §3.1 场景:关 AI → 员工手打 2 条、客户 2 条(含图)→ 提醒到点 → 开 AI → 客户发消息 | DHA 调用日志的补消息条数;平台 `/messages` | 补消息 5 条,角色、顺序、时间对;回答提到「下午帮您调整」的事 | 漏补 / 角色反了 / 回答反问 |
| D2 | AI 回答 3 条气泡 + 1 个视频,之后客户再发 | `t_project_ai_sent_msg`;下一次的补消息 | 登记 4 行带同一 run_id;下一次补消息里没有它们;联调一周内 `runs[].backfill.skipped_as_run_output` 合计占补消息总数 < 1%(建议阈值) | AI 回答以员工消息补回来(会话里重复) |
| D3 | pad 竞态:测试环境给回写任务人为加 5 秒延迟,让回调先到 | Redis `HUMAN_HAS_ANSWERED_*`;客户随后的消息有没有 AI 回复 | 没有写接管标志;客户随后的消息正常得到回复 | 写了标志,后续气泡被拦 / 客户消息没人回 |
| D4 | dk 账号走 PC 发送一轮回答,等会话存档入库 | 登记表 `match_kind=fingerprint` 行;下一次补消息 | 登记成功;下一次不补 | 被当员工消息补回 |
| D5 | pad 发送失败(测试环境把该员工 pad 设备断开) | 平台 run 的 `undelivered`;下一轮提示词 | 标 `send_failed`;下一轮看不到那条回答 | 模型以为说过 |
| D6 | AI 生成期间员工手打 | 平台 run;下一次补消息 | 原回答标 `human_took_over`;员工那条被补进来 | 两份回答都在 / 员工那条漏掉 |
| D7 | 客户连发 A、B(间隔 10 秒,第一轮已开跑) | 平台 `/messages` | A、B 各出现一次;被取消的轮标 `cancelled` | A 出现两次或没有 |
| D8 | 普通群:员工 @ 客户甲回复一条,客户乙说话,员工没 @ 任何人说一句 | 客户甲会话的补消息 | 只有 @ 客户甲那条和客户甲自己的消息 | 混进客户乙的话 |
| D9 | 新客户首次切换(之前在 Dify 聊过 2 天) | 第一轮的补消息 | 最近 48 小时内最后 30 条,含 Dify 时期 AI 的回复(staff) | 会话从空开始 / 补了几百条 |

---

## 8. 工作量粗估

**平台,约 10.5 人日(粗估)**,依据是改动点个数和相近的已做项(定时投递写会话、P-1 取代标记的读写两侧):

| PR | 内容 | 人日 |
|---|---|---|
| PR-1 补消息 | P-1~P-12 + P-13 的迁移:对外字段与校验、写入模块、三个写入点与排队落库、构造函数两个参数、开段与首轮判断、两个 `/messages`、控制台分组与显示、文档、U1~U9 与 U13 | 5 |
| PR-2 未送达 | P-14~P-19:接口、四处 config、视图剔除、记忆排除、读面、U10~U11 | 3 |
| PR-3 消息时间 | P-20~P-21、U12 | 1.5 |
| 真栈验收 | A1~A12 | 1 |

**深护,约 11.5 人日(粗估)**,不含 01 / 02 的基础改造(从调 Dify 换成调平台):

| 项 | 人日 |
|---|---|
| S-1、S-2 发送登记表 + pad 登记 | 1 |
| S-3 pad 竞态修复 | 0.5 |
| S-4 dk 登记(DHA 写标记 + ARCH 匹配写表 + 任务表加列) | 1.5 |
| S-5、S-6、S-11 同步位置 + 缺口查询(两个通道、三种范围、占位、上限、首次) | 2.5 |
| S-7~S-9 调用改造、打断重跑改造、位置前进 | 2 |
| S-10 未送达回报(pad 发送入口收口、dk 终态、取消) | 1.5 |
| 联调 D1~D9 | 2.5 |

---

## 9. 待拍板

> 11-17 用户拍板:**都按推荐,后面再按测试情况调整**。下面保留选项与理由备查。

1. ~~未送达用单独接口,还是放在下一轮请求里带?~~ **已定(11-17):A 单独接口 `:undelivered`**。没选 B(下一轮请求加 `undelivered_run_ids`)。理由:发送失败当场就能报,比等客户下次开口早,能赶在部分记忆写回之前;不依赖客户还会不会再说话;深护的发送失败处理和发起调用是两段代码,单独接口不用把状态带来带去。

2. ~~时间标注加给谁、什么时候开?~~ **已定(11-17):A**:有补消息的会话才开;所有用户消息 + 补进来的健管师消息加,智能体自己的回答不加。没选的:B 只给补进来的消息加(模型仍不知道现在几点);C 全加(模型模仿风险最大、token 多一倍);D 配置开关(平台惯例是缺口先改默认)。

3. ~~平台内容兜底默认开吗?~~ **已定(11-17):开**(只对 staff、≥ 6 字、最近 5 轮、30 分钟内、跳过未送达轮)。理由:深护两个通道的登记都有失败的可能,兜底只会跳过与回答一模一样的员工消息。

4. ~~补消息上限~~ **已定(11-17)**:每次 50 条、每条 4,000 字、合计 65,536 字(与 `input` 同);更长的空档靠 `backfill_omitted_before` 告诉模型,并靠长期记忆衔接。

5. ~~多对一服务群里其他内部员工的话怎么算?~~ **已定(11-17):A** —— 按 staff 补进来,**时间标注里带发送人名字**。客户眼里他们也是服务方;带名字让模型不把医生说的话当成自己说的。没选的:B 不带名字;C 不补(会漏掉医生的建议)。

6. ~~图片、文件第一期只给占位吗?~~ **已定(11-17):是**,第一期只给占位文字;客户提到那张图(「我早上发的化验单你看了吗」)时,深护在本轮 `files` 里把它重新带上(01 / 02)。

7. ~~未送达要不要撤回已写的记忆?~~ **已定(11-17)**:第一期不做;第二期按证据指针撤回「只以那条回答为依据的助手推断类记忆」,等 B-300 上生产稳定后做。

8. ~~被打断后的重跑改成「新消息进 input,旧批走补消息」(S-8)?~~ **已定(11-17):是**。代价是深护打断逻辑要改。注意:业务方的 inputs 变量 `source_message_ids` 仍要含被取消那批 —— 数字人服务另传 `source_msg_keys`(S-7、01 §5.1),项目服务据此生成(04 §6.16)。

9. ~~新会话第一次补多少?~~ **已定(11-17)**:从没补过消息的会话,第一次补**最近 48 小时、最多 30 条**(和 Dify 现在给健管师看的 30 条一致);之后只补同步位置之后的消息。

10. ~~补消息要不要出现在 `/items`?~~ **已定(11-17):第一期不出现**;`/messages` 和控制台能看到就够调试,以后需要再加一种条目类型。

11. ~~同步位置按入库顺序还是发送时间?~~ **已定(11-17):入库顺序**(消息表自增 id)。理由见 §5.6:dk 存档有延迟,按发送时间会永远漏掉晚入库的消息。

12. ~~固定话术原文补不补进会话~~ **作废(11-17)**:02 拍板取消 `fixed_reply`,话术由模型写进 `messages`,本来就是这一轮的输出(§3.3),不存在补不补。

13. ~~PR-3 上线时,时间标注与 01 的本轮时间怎么并存~~ **已定(11-17)**:v1 只用 01 的做法(input 首行时间 + 每行 `HH:mm`);PR-3 上线时,本智能体去掉 input 首行时间、统一用平台标注;每行 `HH:mm` 留不留**有意留到 PR-3 实测时再定**(要看两种标注同时出现时模型会不会模仿或混淆)。

14. ~~会话合并私聊与群之后,补消息的来源窗口怎么标~~ **已定(11-17):A** —— 第一期深护在 `content` 开头加「[私聊]」「[群聊]」(§5.7 第 9 条),平台不改;本轮输入的来源同样写在 input 首行(01 §3.3.1)。B(平台加 `source_label` 字段)等别的对接方也需要时再做。

**pad 竞态(S-3)什么时候做** —— **已定(11-17):先不管**,随 03 一起做,不单独提前。已知风险:03 之前,pad 上一轮多条气泡的后几条可能被误设的接管标志拦下(08 S12)。

---

## 10. 出处

### Expert Work 平台(`origin/main` `d9db26f79`,代码与 `5908fe554` 相同;均读代码确认)

- 对外发起一轮:`services/control-plane/src/control_plane/api/agents.py:855-940`(`ExternalRunRequest`)、`:1708-1998`(`run_agent_for_user`)、`:525-560`(`external_run_bounds_error`)、`:1890-1905`。
- 幂等指纹覆盖整个请求体:`api/_idempotency.py:42-56`。
- 会话接单锁、排队、三个收口点:`api/runs.py:1414`(8620 锁,`advisory_locks.py:118`)、`:1479`、`:1497-1504`、`:1529-1536`、`:1573`、`:1637-1663`;`run_queue_worker.py:327-369`。
- 用户输入构造点:`api/runs.py:475-547`(`sent_at` 在 `:521`)、`:550-590`(`build_run_graph_input`,`:590` 盖戳)、`:290`(`RunRequest`);排队还原 `api/_enqueued_input.py:22-49`。
- 白名单:`packages/expert-work-common/src/expert_work/common/user_input.py`;`services/control-plane/tests/test_user_input_whitelist.py:72-96,99-108`。
- 定时投递写会话骨架:`control_plane/trigger_delivery.py:87-153`(`as_node` 在 `:143`)、`:156-177`(忙判断)、`:241-256`(两把锁)。
- `aupdate_state` 不带 run_id:`services/orchestrator/src/orchestrator/approval_turn.py:156-183`(说明在 `:165-167`)。共享背景写的是 `control_plane/approval_turn.py`,实际在 orchestrator。
- 收口上一轮:`control_plane/approval_void.py:73-110,135-222`。
- 取代与按轮分组:`packages/expert-work-common/src/expert_work/common/supersede.py:42-63,66-90`;重放原件形状 `control_plane/supersede.py:406-421`。
- 开段与 `final`:`common/conversation_channel.py:68-71`(`SCHEDULED_DELIVERY`)、`:152-166`(`opens_segment`)、`:187-245`(`visible_turns`)。
- `/items`:`api/external_session_items.py:425`、`:479-507`;`/messages`:`api/external_sessions.py:307-322`、控制台 `api/runs.py:2341-2420`;`control_plane/transcript.py:82-122`。
- 控制台分组:`apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts:96-133`;`apps/admin-ui/src/api/sessions.ts:140`。
- 记忆:`orchestrator/graph_builder/memory.py:446-454`(`_user_sent_at`)、`:549-567`(`_segments`)、`:583-620`(`_turn`)、`:623`、`:3160-3167`(`_this_turn`);`memory_slice.py:86-94`;`control_plane/memory/writeback_processor.py:130`(30 天)、`:309-347`;证据指针 `packages/expert-work-persistence/src/expert_work/persistence/memory/sql.py:122-124`。
- 首轮判断:`orchestrator/graph_builder/agent_notes.py:125-133`。
- 提示词视图与路由:`orchestrator/graph_builder/builder.py:846-850`、`:1305-1308`(`with_platform_context`)、`:2544-2580`(`end_target` 指向 `memory_writeback`)、`:3509-3530`(`_should_continue`);`graph_builder/platform_context.py:1-35`。
- 上一个检查点没跑的任务在新输入到来时被丢弃:LangGraph 1.2.19 `langgraph/pregel/_loop.py:1118-1140`(本地 `.venv`,读代码确认)。
- 系统提示词日期:`orchestrator/agent_factory.py:1203-1211,1885-1908`。
- config 组装点:`api/runs.py:1198,1794`;`run_queue_worker.py:397`;`orphan_sweep.py:415`;`trigger_firing.py:296`。
- 取消接口形状:`api/external_runs.py:615-672`。
- MCP 调用不带 run 上下文:`services/orchestrator/src/orchestrator/tools/mcp.py:1434-1436`(`del ctx`)。
- 结构化输出不合格时带 schema 重发:`orchestrator/graph_builder/builder.py:1655-1700`。
- 迁移最新编号:`packages/expert-work-persistence/migrations/versions/0192_rate_card_qwen4_max.py`。

### Dify DSL(`深护智康(pad)-健管师-3.0.yml`,读代码确认)

- 拉历史 HTTP 节点 `1770115147110`(`id:` 在第 9055 行),请求体含 `room_conversation_id`、`employee_pad_userid`、`customer_pad_userid`、`msg_app_info`。
- 整理节点 `1770115347090`(`id:` 在第 9151 行,代码在其上方):角色判定、每条带 `timestamp`、切 2 / 6 / 30 条。共享背景写的「只留文字」对,但没提每条带时间。
- 健管师大模型 `1761372447236`(第 7512 行)用 30 条(第 7481 行);首轮补问 `1761376427497` / `1761376653874`。

### 深护(读代码确认,除标推断的)

- DHA `src/libs/cpwx-pad/request.ts:43-51`、`message.ts:23-162`;`src/modules/agent-callback/send-agent-answer.processor.ts:78-107,174-192,243-261,311-329,385-403,415-509,555-772`。
- DHA `src/modules/message/message.service.ts:37,128-252,296-371`;`many-to-one-history.util.ts:21,110`。
- DHA `invoke-agent-message.processor.ts:41-48,80-85,219-233,338-373,444-507`;`invoke-agent.processor.ts:545-559,632-701,714-744,746-801,844-866,875-990`;`agent-callback.service.ts:130-157`;`send-notify.processor.ts:50-81,123-141`。
- DHA `src/modules/agent-callback-dk/ai-sent.util.ts:14-75`;`src/modules/pc-send-task/pc-send-task.service.ts:301-315,433-454,521-541,595-631,667-698`。
- SYNC `src/modules/message/sync-pad-message.processor.ts:126-129,283-290,599-637,662-678,700-765`;`src/models/TProjectCpwxPadMsg.ts`;`src/config/global.ts:28,30`。
- pad 回调服务 `src/modules/callback/callback.service.ts:459-476`。
- ARCH `src/modules/archive/push/human-answered.service.ts:13-25,141-218`;`src/modules/archive/sync/msgaudit-sync.core.ts:337-388`;`src/modules/archive/push/agent-push.service.ts:130-134,175-203`;`src/models/TProjectCpwxChatMsg.ts`;`src/modules/archive/shared/persist.service.ts:222-246`。
- PS `src/modules/expert-work/service/client/expert-work.types.ts:42-48`;`expert-work-client.ts:489,572`。
- 推断:pad 回执字段(无类型声明,从消费方反推);回调里的 `app_info` 与回执里的是同一个值(同一去重键);`INVOKE_CANCELLED` 兜底不生效(Dify 回调不带 `invoke_token`);竞态的实际损害路径。

### 与共享背景不符之处(以本文核实为准)

1. `approval_turn.py` 在 `services/orchestrator/src/orchestrator/`,不在 control_plane。
2. pad 回执:`send-agent-answer.processor.ts:78-86` 拿到的是整包回执,代码里没有单独取 `app_info` 的地方;`is_digital_human` 只是队列任务字段,不落库。
3. 竞态的后果不是「AI 停 10 分钟」:标志 600 秒过期或客户再开口时被删;真正的损害是同一轮后续气泡被拦、以及客户在 AI 分条发送期间开口时那条消息没人回。
4. dk `AI_SENT`:时间窗是 ±600 秒(10-07 从 120 秒放宽),在发放任务时写而不是发送成功后,读的一方是会话存档服务,媒体只按类型比对、命中不消费。
5. 历史接口:pad 只排除本批里 msg_id 最大的一条,dk 整批排除;只返回文字和语音。
6. Dify 历史每条带 `timestamp`,所以现在的健管师其实看得到历史消息的时间。
7. 新发现(共享背景没有):定时投递式写入在开了长期记忆的智能体上会留下一个待跑的写回节点(被下一轮输入丢弃);控制台对话页只要有一条消息没有 run_id 就整段退回平铺;记忆索引段的首轮判断会被补消息打乱;同步写回的退回路径不提炼补消息。
8. 总览 §2 原写「项目服务查出会话里还没有的消息」;本文建议缺口查询放在数字人服务(消息表和范围规则都在那里),项目服务只转发(11-17 交叉核对时已同步改 00)。
