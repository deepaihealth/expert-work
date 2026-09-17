# B-67 本轮输入由平台整段接管 —— 设计

> 日期:2026-09-17。承接 B-61(`2026-09-15-injected-variables-by-reference-design.md`,测试环境 T10 已过)。
> 起因:2026-09-16 晚用户对 B-61 数据面提出三个疑问 —— ① 「把变量写进文件」是否合适;② 让对接方改提示词、靠人记得写「inputs 里有 xxx」不可靠;③ 「有些写有些不写」后面的人没法判断。本文是对这三问的回答与设计。
> 用户拍板(09-16 晚):方向通过,写 spec。

## 零、读之前要知道的(都在代码 / 两个参考仓库里核过,不是推测)

1. **参考仓库怎么做的**(`~/src/github/hermes-agent`、`~/src/github/openclaw`,2026-09-16 读源码):
   - 两家的提示词作者**只写行为**(`SOUL.md` / `AGENTS.md`),这些文件**没有变量语法**。作者根本没有「变量该不该写」这个问题。
   - 「本轮来了什么」全由平台在**固定位置、固定格式**写:附件 → 平台下载到本地,把**短路径**贴在**用户消息最前面**(openclaw `src/auto-reply/reply/prompt-prelude.ts:39-44` 的 `[media attached: media/inbound/x.png]`;hermes `gateway/run_inbound.py:1495-1512` 的 `[The user sent … It is saved at: …]`)。系统提示词尾部只放低风险的环境信息。
   - 两家都**不**解决「模型抄错长串」:文本值全部内联(openclaw webhook `{{payload.x}}`,`src/gateway/hooks-mapping.ts:482-500`;hermes `@file:` 展开),模型照抄路径。它们只是把路径做得短(`media/inbound/<原文件名>`)。
   - 唯一让作者写变量的地方(openclaw webhook `messageTemplate`)和我们今天的形态一样:作者自己挑字段、全部内联、平台不补齐。
2. **我们今天的链路**:
   - 系统提示词在 control-plane 渲染(`prompt_render.render_system_prompt`,`api/runs.py:499` 内的 `build_run_graph_input`),**早于**预拉(orchestrator 图的 `inputs` 节点,`agent_factory.py:1691`,START 之后第一个节点)。所以渲染时**不知道预拉成不成**。
   - `build_run_graph_input` 是流式 POST 与队列 worker 共用的唯一入口(`run_queue_worker.py:327`);触发器走自己的 `SystemMessage(content=built.system_prompt)`(`trigger_firing.py:277`)且**没有 inputs 概念**,不在本文范围。
   - 隐藏消息是现成惯用法:`HumanMessage(additional_kwargs={"expert_work_hide_from_ui": True})`(委派提醒,`builder.py:2088-2100`)。它进模型、进 durable 记录与镜像(`transcript_mirror_sweep.py:155` `include_hidden=True`),**不进**对外会话消息(`external_sessions.py:298` `include_hidden=False`)、不进控制台气泡(`runs.py:1866`)、不进对话条目(`conversation_derive.py:245`)。Anthropic 适配把每条 `HumanMessage` 各自映成一条 `role=user`(`llm/providers/anthropic.py:843`),连续两条 user 已在委派提醒上跑过真栈(B-36)。
   - 工具调用发出前的唯一咽喉:`tools_node` 里 `_fill_bound_args` 之后、审批门与 action screening 之前(`builder.py:1320-1400`);拒绝一个调用的现成形状是合成 `ToolMessage(status="error")`(`builder.py:1424-1436`,action screening 的 block 分支)。
   - `BuiltAgent`(`orchestrator/built_agent.py:33`)已有 `prompt_jinja / prompt_variables / prompt_base / prompt_suffix / spotlight_nonce / unmatched_arg_bindings`,**没有**「已落地的绑定表」。
   - `PromptVariableSpec`(`agent_spec.py:195`)= `name / trusted / required / description`;`ai-health-plan` 十一个变量**全部已有 description**(`docs/agents/ai-health-plan.md:46-89`)。
   - 预拉落盘名是 `inputs/cache/<sha256(url)[:32]><ext>`(`prefetch_script.py:104`);`EXPERT_WORK_INPUTS` 由 `sandbox.agent_key_envs(run_id=…)` 注入(`tools/sandbox.py:97-123`),两后端契约测试钉着。
   - `materials` 在 `ai-health-plan` 契约里是**JSON 数组字符串**;今天 `build_inputs_doc` 原样存字符串、`_sites` 只走 dict/list(`prefetch_script.py:292-326`),所以字符串内部的 URL 平台看不见、不预拉。
   - 事故基线:测试环境会话 `9400f96a…`,glm-5.3,三轮三次把 `org_logo` URL 抄错(`1789`→`17889`,编辑距离 1)。

## 一、问题

B-61 把值搬进了文件,但把**三件事留给了写提示词的人**:

1. 记得在模板里写「输入在文件里」;
2. 判断哪些变量该内联、哪些该改成「见输入文件」;
3. 把已经写着 `{{ org_logo }}` 的旧模板改掉。

每一件都是「人记得就对、忘了就静默退回抄错」。而且「本轮输入」段放在系统提示词里,长 run 到写代码那一刻它已沉到上下文中段 —— 模型最需要它的时候最容易漏看。

## 二、目标与非目标

**目标**

- 写模板的人只需要知道一条:**「模板只写怎么做事;输入不用写。写了也不会错。」**
- 对接方 addendum 里的 5 处「必改」变成 0 处。
- 模型抄错长串这件事**结构上走不通**,不依赖它看没看见提示。
- 存量 Agent 零改动照跑,行为变化只有一种:URL 值不再以原文出现在提示词里。

**非目标**

- 不动绑定面(B-61 §五)。同名全局绑定仍是否决项。
- 不做 `${inputs.x}` 引用语法(B-61 §十二 已后延)。
- 不改触发器路径(没有 inputs)。
- 不做通用「长串检测」:只处理 URL 形态;非 URL 的长 ID 没有真实用例,不提前造规则。

## 三、总览:四件事

| # | 做什么 | 落点 | 解决哪一问 |
|---|---|---|---|
| A | 预拉文件按**变量名**命名(符号链接) | `prefetch_script.py`、`sandbox.agent_key_envs` | ①(路径短、可读、能直接说) |
| B | `{{ var }}` 按**值的形态**渲染 | `prompt_render.py` | ③(写了也不会错) |
| C | 平台生成「本轮输入」段,作为**隐藏消息贴在用户消息之后** | `api/runs.py` `build_run_graph_input` | ②(人不写平台写)+ 注意力(在末尾) |
| D | 手抄守卫:代码里出现输入 URL 的原文或近似 → 拦下并告知正确用法 | `builder.py` `tools_node` | 不靠注意力的那层 |

四件事**独立可用、独立可回滚**;顺序 A → B → C → D,每一步单独发测试。

## 四、A —— 预拉文件按变量名命名

### 4.1 目录形状

```
agents/<key>/inputs/<run_id>/
    inputs.json
    org_logo.png            -> ../cache/9f2a…386.png          (顶层 URL 变量)
    materials/
        1-示范视频.mp4      -> ../../cache/4d17…581.mp4      (列表项:序号-说明)
        3-饮食指南.pdf      -> ../../cache/…
agents/<key>/inputs/cache/<digest><ext>                     (不变,内容寻址)
```

- **符号链接,不是硬链接**:缓存 7 天回收的账不能被 30 天的 run 目录拖住(硬链接会让字节活到最后一个链接消失)。链接失效 = 今天已接受的降级(B-61 §4.5),消费方判据「为空或本地不存在」不变。
- 相对目标(`../cache/…`):NAS 视角与沙箱 `/workspace` 视角下都成立(B-60 的 exec view 是 agent 目录的 bind)。
- 命名:顶层 URL 变量 → `<name><ext>`;dict 字段 → `<name>.<field><ext>`;列表项 → `<name>/<i>-<slug><ext>`,`slug` 取该项 `description` 前 40 字符,只保留 `[\w一-鿿.-]`,空则省略 `-<slug>`。同名撞车加 `-2`。
- **`ext` 只取 URL 路径后缀**(`pick_suffix` 的 URL 分支),**不看 Content-Type**:渲染层(§五)在预拉之前就要说出这个名字,它只有 URL。URL 没有后缀就没有后缀。Content-Type 只继续决定 cache 文件名,不变。链接名的计算函数与渲染层**共用同一个**(`inputs_doc.link_name(var, path, url)`),用测试钉住两边同义。
- 建链接的时机:预拉脚本每拉完(或命中)一个 site 就建,和 `_rewrite` 同一节奏;链接**先删后建**(`os.symlink` 到已存在路径会 `FileExistsError`)。
- `inputs.json` 里 `local_path` **改指链接**(`inputs/<run_id>/org_logo.png`),不再指 cache。老消费方按路径打开,两种都能打开;新消费方拿到的是可读的名字。

### 4.2 环境变量

`agent_key_envs(run_id=…)` 多注入一项 `EXPERT_WORK_INPUTS_DIR=/workspace/inputs/<run_id>`。`EXPERT_WORK_INPUTS` 保留(清单绝对路径)。契约测试(`test_sandbox_runtime_contract.py`)两后端同步加断言。

### 4.3 JSON 字符串值

`build_inputs_doc` 与 `_sites` 增加一条同义规则:**字符串值若能 `json.loads` 成 list/dict,视作该结构参与 site 扫描与 `local_path` 回填**,结果写到同级 `value_parsed`;`value` 仍原样(契约不变)。`_sites` 宿主侧/沙箱侧同义,沿用 `test_site_walk_matches_the_host_side_implementation` 钉住。上限:字符串 ≤ 64 KiB 才尝试解析(与 `read_document` 内联上限同数量级,防 CPU)。

### 4.4 清扫

`_reclaim_entries` 对 run 目录整棵 `rmtree(dir_fd=…)`,不跟随链接;对 `cache/` 层只看文件,链接不在那一层。**零改动**;补一条测试:run 目录里带指向 cache 的链接时,rmtree 只删链接不删目标。

## 五、B —— `{{ var }}` 按值的形态渲染

`render_system_prompt`(`prompt_render.py:42`)构造 `context` 时,本轮**传了**的变量经 `render_value(var, raw, *, nonce)`:

| 值的形态(判定顺序) | 渲染成 | 备注 |
|---|---|---|
| 本轮未传(不进 `render_value`) | 今天的值(trusted `""`,untrusted 空围栏) | 被绑定也一样 —— 平台此时什么都不填,`if` / `default()` 不能被翻过来 |
| `render: raw` | 原值(今天行为) | 收窄开关 |
| `str` 且 `http(s)://` 开头 | `$EXPERT_WORK_INPUTS_DIR/<name><ext>` + `（已就位；不在则按输入清单里的原地址下载）`;URL 后面跟着的说明文字照留 | **不等预拉结果**:名字是确定的;失败时文件不存在,清单 `value` 里永远有原 URL |
| `list` / `dict` / 可解析 JSON 字符串,且内部有 URL | 另起一行逐项:`<i>. <description或序号> → $EXPERT_WORK_INPUTS_DIR/<name>/<i>-<slug><ext>；`(dict 是 `- <键> → <路径>；`,不是对象的列表项是 `<i>. <路径>；`);无 URL 的项照原样;块尾是同一句尾注 | 用 §4.3 的 `value_parsed`;**trusted 的真 list / dict 保留结构**(下标、属性、遍历、`length`、`tojson` 看到的是 URL 换成路径的副本,直接 `{{ x }}` 输出的是逐项块);untrusted 与 JSON 字符串仍是逐项块(改动前它们在模板里就是字符串) |
| 其它(短文本、枚举、多行规则) | 原值(今天行为) | `trusted: false` 仍走 spotlight 围栏 |

- **为什么 URL 渲染不需要知道预拉结果**:路径由名字决定,不由下载决定。渲染层只做字符串替换,不发网络请求,不查文件系统。
- 名字用 §4.1 的同一个 `link_name`,所以提示词里说的名字与预拉建出来的链接**逐字相同**。
- **被绑定的变量同样按形态渲染**(不再是「平台自动填」):绑定是逐工具的,有绑定的工具 schema 里已经没有这个参数,藏值换不来什么;漏绑的工具(部分绑定是常态)却会静默拿不到值。绑定状态由 §六「本轮输入」段报告。
- `trusted: false`(裁定 P8):从值推出来的**整段**(说明、dict 键、路径 —— 路径里带着 slug)合成**一个**围栏;只有尾注是平台文本,在围栏外。带路径的行以「；」收尾,datamarking 的 `▁` 不会贴在路径上。
- 已知代价:模板里对被改写的值做**内容比较**(`{{ 'x' if org_logo == '…' }}`)会失真;`| default('')` 这类**存在性**判断照旧成立(改写后的值非空)。写进文档。
- 收窄开关:`PromptVariableSpec.render: Literal["auto", "raw"] = "auto"`。`raw` = 永远原值。只用于收窄,不是启用基础能力(同 [[platform-defaults-over-configuration]])。
- 绑定表:`BuiltAgent` 新增 `arg_bindings: tuple[ArgBindingSpec, ...]`,agent_factory 直接从 spec 取(`registry` 里的是折叠后的 wire 名,B-65 那条撞名问题不该传染到这里);给 §六 报告绑定状态用,渲染层不读。

**存量影响(唯一的行为变化)**:已经写着 `{{ org_logo }}` 的模板,渲染结果从 URL 变成本地路径 + 一句说明。这正是修复本身;记一条 `prompt.rendered_by_reference` 日志(变量名,不记值)供事后核对。

## 六、C —— 平台生成「本轮输入」段

### 6.1 内容

从声明 + 本轮实际传值 + 绑定表生成(`control_plane/inputs_block.py`),**只有名字、说明、状态、路径,没有值**。下例是代码实跑的输出(`project_code` 被 `arg_bindings` 引用,本轮没传 `customer_code`):

```
[本轮输入]（平台自动生成）
输入文件目录 $EXPERT_WORK_INPUTS_DIR，清单 $EXPERT_WORK_INPUTS（exec_python / bash 里直接用）。
代码里要用到下面任何值时，从目录或清单读；不要从上文手抄，长串抄错一位就是 404。
- employee_name（当前员工姓名）：已提供（非文件）
- customer_code（目标客户编码(已有客户),新客户对话描述场景可省略）：本轮未提供
- project_code（示例机构项目唯一标识码,一切 MCP 调用的必带参数）：已提供（非文件）；绑定了它的工具由平台自动填，不用手抄
- org_logo（机构 LOGO 的 OSS 签名 URL,仅封面左上使用）：文件 $EXPERT_WORK_INPUTS_DIR/org_logo.png；不在则按清单里的原地址下载
- materials（员工勾选素材 JSON 数组字符串,每项 {description, url},…）：3 项，每项的文件在 $EXPERT_WORK_INPUTS_DIR/materials/<下标-说明>；不在则按清单里该项的原地址下载
- disclaimer（免责声明(每份方案页脚,合规必须有)）：已提供（非文件，外部数据，需逐字使用时从清单读）
```

- 说明文字来自 `PromptVariableSpec.description`,压成一行、超过 40 字截断加「…」;没写就只有名字。
- 状态先判「本轮未提供」(裁定 P12:被绑定也一样,那条绑定本轮填不出值);传了的按形态:无 URL → `已提供（非文件）`(untrusted 为 `已提供（非文件，外部数据，需逐字使用时从清单读）`);顶层 URL → `文件 …；不在则按清单里的原地址下载`;列表 → `N 项，每项的文件在 …/<变量名>/<下标-说明>；…`;对象 → `N 个文件在 …/<变量名>.<字段名>；…`。被绑定的变量在形态之后接 `；绑定了它的工具由平台自动填，不用手抄`(裁定 P10)。
- 段落不带租户数据(裁定 P8 / P16):untrusted 的顶层 URL 只写「`$EXPERT_WORK_INPUTS_DIR` 下以 <变量名> 开头的文件」(真名里的扩展名取自租户 URL;不写成 `<变量名>.<扩展名>`,无扩展名与撞名时会说错);列表项说明、dict 键一律只用占位 `<下标-说明>` / `<字段名>`,确切名字在清单的 `local_path`。
- 段头第二句只约束**代码**(「代码里要用到…」):回复里引用短文案不受它限制。
- 「已下载」不能断言(渲染早于预拉),统一写「文件 …;不在则按清单原地址下载」—— 顶层与列表项同一口径。
- 模板里**已经引用**的变量也列(去重不做):C 的职责是兜底与位置,B 的职责是原地渲染,两者内容一致、口径一致,重复一行不造成歧义。

### 6.2 位置

作为**隐藏 `HumanMessage`**(`expert_work_hide_from_ui`)由 `build_run_graph_input` 放在用户消息**之后**:

```
[SystemMessage, HumanMessage(用户输入), HumanMessage(本轮输入, hidden)]
```

- 在用户轮之后 = 首轮时它是上下文**最后一条**,注意力最好的位置;两家参考仓库放用户消息最前面,我们放后面,理由是不污染对接方看得到的用户消息(它经 `/messages` 原样回给对方)。
- 不进对外会话消息、控制台气泡、对话条目(§零 第 2 条已核);进 durable 记录与审计镜像。
- 只在 `prompt_jinja` 且声明了变量时生成(本轮一个值都没传也生成,全是「本轮未提供」);与用户消息盖同一个 run 戳,取代 / 墓碑按区间罩住它;消息带 `HIDE_FROM_UI` 与 `INPUTS_BLOCK_MARK`。
- `:regenerate` 重放 [System, Human, 本轮输入段] 三条原件(`replay_graph_input` 接受 2 或 3 条,靠 `INPUTS_BLOCK_MARK` 认出这一段);审批续跑与孤儿复活不重建消息(检查点里已有)。三条路径要的**原始 inputs**(填绑定参数、`EXPERT_WORK_INPUTS` 指向首段目录)由 #1577 从本轮首段的 `system_prompt` 帧取回;`:regenerate` 时变量声明已改则 422(改用 `:edit`)。
- 委派的子 run 与触发器路径不生成(它们不经过 `build_run_graph_input`;子 run 没有自己的 inputs.json,B-61 §4.2 同一道闸)。
- 读「最后一条 HumanMessage」的地方都跳过隐藏消息(裁定 P14:judge / screening 对齐、planner、记忆召回与抽取、窗口轮数、会话标题、技能演化重放、压缩摘要);反思轨迹保留隐藏消息(裁定 P22:revise 反馈本身就是隐藏消息)。

### 6.3 不做每轮提醒

曾提议每轮写代码前再贴一行提醒。否决:**守卫(D)拦下时回给模型的那句话就是最准时的提醒**;没抄错的轮次贴提醒不产生任何价值,只费 token。

## 七、D —— 手抄守卫

### 7.1 判定

`tools_node` 里 `_fill_bound_args` 之后,对 `exec_python` / `bash` 的代码参数(`_SANDBOX_CODE_ARGS`)做一次扫描;命中只在派发处(`_bounded`)生效,不改审批门与 action screening 的判定和下标语义(裁定 5):

- 候选集 = 本轮 `PROMPT_INPUTS_KEY` 里所有 URL site(`inputs_doc.linked_sites`,与预拉 / 渲染同一个 walker 与命名,含 §4.3 的解析形态),记 `(变量名, URL, 链接名)`。
- **完全一致**(裁定 P25):按子串在代码里找候选 URL 的原文,原文后面不是 URL 的延续(只隔着 ASCII 句读 `.,;:!?)` 也算)→ 命中。不依赖字面量抽取,所以路径里带全角标点或括号的 URL 也认得出。代码里抽出的 URL 字面量(见下)与候选只差 scheme / host 大小写,同样算完全一致。
- **近似**:从代码里抽 URL 字面量(`https?://` 不分大小写,到空白 / 引号 / 反引号 / 尖括号 / 右括号 / 右方括号 / 常见全角标点为止,去掉尾随的 ASCII 句读),与同 scheme+host(不分大小写)的候选比 host 之后的全部(路径 + query + fragment,裁定 4)。允许的编辑距离随**候选**尾串长度走:`min(3, 尾串长度 // 16)`(裁定 P25)—— 尾串短于 16 字符只拦完全一致(`/v1` 与 `/v2`、`img_0.png` 与 `img_7.png` 本来就是不同的地址)。事故里的尾串约 30 字符、距离 1,在界内。进 DP 前先过长度差与字符多重集差两道下界;DP 是带状 Levenshtein(裁定 P19),host 之后超过 8192 字符不比。
- 归属:先找完全一致;否则按字面量在代码里的顺序,**第一个**有近似命中的字面量即返回(它对多个候选取最小距离)。
- **失败放行**(裁定 P26):候选构建或比对抛任何异常 → 整批放行,记 `tools.input_url_guard_skipped`(只带异常类型,异常文本里可能有 URL);每次调用的近似比较预算约 200 万个 DP 格子(尾串长度 × 带宽累计),用完就停、已找到的照常返回,记 `tools.input_url_guard_budget_exhausted`(只有计数)。A 的链接命名(`url_suffix`,宿主与沙箱两份)遇到畸形 URL 不再抛;沙箱预拉里一个变量出错只丢那一个变量。
- 命中即拦:该调用不执行,合成 `ToolMessage(status="error")`。trusted 变量回显写法与链接名;近似命中另给出路(真是别的地址就从它自己的来源取):

  ```
  [blocked] 代码里的地址 https://files.example.com/brand/cover-17263948851207.png 像是输入 org_logo 的地址手抄出来的（平台比对：疑似抄错 1 处）。若是它：这个文件应在 $EXPERT_WORK_INPUTS_DIR/org_logo.png（不在则按清单里的原地址下载）；请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 org_logo 的原地址，不要手抄。如果它确实是另一个地址，请让代码从它自己的来源取得（读文件、接口返回或上一步的输出），不要在代码里手写这串地址。
  ```

  untrusted 变量与委派子 run(按 `configurable["child_run"]` 判,裁定 P24)的提示不写链接名、不回显那串 —— 这条合成消息不过 spotlight 围栏(下例为完全一致;近似时同样带上面那句出路):

  ```
  [blocked] 代码里有一处地址是输入 materials 的手抄件（平台比对：与输入一致）。这个文件应在 $EXPERT_WORK_INPUTS_DIR 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里 materials 对应条目的 local_path（不在则按清单里的原地址下载）；请用代码从清单里读路径或原地址，不要手抄。
  ```

  抄对了也拦:这次对不代表下次对;拦一次模型这一轮就改道。
- 错误分类记 `invalid_arguments`(改参数、别原样重发),**不**用 `blocked_by_policy`(裁定 P23):后者的恢复提示是「等审批 / 报给用户、别绕过」,与守卫要模型做的事相反。工具计数只加 `blocked`,不记 0 秒延迟样本。
- 同批其它调用照常执行(不同于 action screening 的整批拒绝):被拦的只是那一条。
- 写一条 `tool:blocked` 审计(现成动作),审计行 `reason = "input_url_retyped"`,`details` 只多记 `input_variable` 与 `edit_distance`;`args` 去掉代码键(裁定 6),**不记 URL、不记代码**。

### 7.2 边界

- 只看 `exec_python` / `bash`;MCP 参数走绑定面。
- 只比 URL 候选;非 URL 值不比。
- 模型代码里**读清单再拼 URL**(`d["variables"]["org_logo"]["value"]`)不含字面量,不会命中 —— 这正是想要的用法。
- `render: raw` 的变量同样在守卫范围内(守卫不看渲染方式)。
- 归属:完全一致优先;否则取代码里**第一个**有近似命中的字面量,再对它取距离最小的候选(`…-1.png` 与 `…-2.png` 都是输入时,抄错的那串归给更近的那个);消息里写「疑似」并给出路。
- 误拦代价:模型多一轮改道;漏拦代价:404。取拦。
- 已知误拦(裁定 P25):尾串较长、与某个输入只差一两位的**非输入**地址(同目录编号相邻的公开素材、`/openapi/v1/…` 输入旁边调 `/openapi/v2/…`)仍会被拦一次,靠近似提示里的出路换路;上线后看 KPI 的命中明细,有真实误拦再收紧。
- 接受的漏拦:字符串拼接 / f-string / 变量间接引用 / 编码、`http` ↔ `https`、percent-encoding 过的 host、先写文件再执行、非沙箱工具;短尾串原文紧跟 shell 字符(`|`、`\`、`&&`);被全角标点截断后的**近似**抄错(完全一致的仍认得出)。
- 已知代价(裁定 5):`bash` 同时在 `approval_required_tools` 里且抄错时,会先走一轮审批,批准后仍被拦;审批面板里人改过的代码同样受守卫约束。

### 7.3 为什么放 tools_node 不放中间件

`before_tool_dispatch` 中间件的 payload 只有 `tool_name / tool_args`(`builder.py:2597`),拿不到本轮 inputs;`tools_node` 里 `_fill_bound_args` 已经在读 `configurable[PROMPT_INPUTS_KEY]`,守卫放同一处,顺手。

## 八、安全

- 提示词里**少了** URL,没有新增任何值进上下文;C 段零值。
- `value_parsed` 与 `value` 同受 `_null_local_paths` 闸(调用方自带的 `local_path` 清空)。
- 符号链接只由平台预拉脚本在 run 目录内创建、目标限定 `../cache/`;janitor 不跟随链接(§4.4)。
- 守卫消息里回显的是模型自己写的那串(完全一致时与输入值相同),只对 trusted 变量回显;`tool:blocked` 审计与守卫日志不记 URL、不记代码。
- `render_value` 只做字符串替换,不发网、不读盘。

## 九、存量、迁移、回滚

- 存量 Agent:唯一变化是 URL 值的渲染(§五)。发布清单写明;对接方 addendum 状态改为「模板不必改;§4 的 5 处降为可选」。
- 回滚:四件事各自独立。A 回滚 = 预拉脚本不建链接、`local_path` 指回 cache(老消费方无感);B 回滚 = `render_value` 直通;C 回滚 = 不追加隐藏消息;D 回滚 = revert 该 PR(不加「只记日志不拦」的运行期开关,裁定 7)。**没有 schema 迁移**;`PromptVariableSpec.render` 是可选字段带默认值,旧版本读到带 `render:` 的 manifest 会 `extra="forbid"` 报错 —— 与 B-61 的 `arg_bindings` 同一个坑,同一条纪律:回滚窗口内别配 `render`。

## 十、可观测性

- `prompt.rendered_by_reference`(变量名列表)—— 每次渲染一条。
- `inputs.block_injected`(变量数、已绑定数、URL 数)—— 每 run 一条。
- `tool:blocked` + `reason=input_url_retyped` + 编辑距离 —— 命中一次一条。**这是本项目的 KPI**:上线后应从「有」趋向「零」(模型学会读文件);长期不为零说明提示或工具描述还有漏洞。
- 现有 `inputs.prefetch_done` 不变。

## 十一、测试

- 单元:`render_value` 六条分支各一;链接命名(中文 slug、无后缀 URL 双链接、撞名);`value_parsed` 与 `_sites` 宿主/沙箱同义;守卫的完全一致 / 距离 1 / 距离 4 不拦 / 读清单拼 URL 不拦;C 段对「未传可选变量」「已绑定」「JSON 字符串素材」三种形状的文本。
- 契约:两后端 `EXPERT_WORK_INPUTS_DIR`。
- janitor:run 目录含链接时 rmtree 不删目标。
- **真栈验收(必做,带数字)**:复刻 `ai-health-plan` 的模板形态与 glm-5.3,探针变量含一个带 13 位时间戳的 LOGO URL,跑 N=10:记录 ① 守卫命中次数、② 404 次数、③ 最终是否用了本地文件。对照组:B/C/D 关闭。判据:实验组 404 = 0;守卫命中次数报告出来,不设阈值(它是学习曲线的读数)。
- 每条新断言按 [[fix-tests-certify-broken-version]] 自证。

## 十二、PR 切分

| PR | 内容 | 可独立发测试 |
|---|---|---|
| 1 | A:链接命名 + `EXPERT_WORK_INPUTS_DIR` + `value_parsed` + janitor 测试 | 是 |
| 2 | B:`render_value` + `BuiltAgent.arg_bindings` + `render` 字段 | 是(依赖 1 的命名约定,不依赖代码) |
| 3 | C:隐藏「本轮输入」段 + replay 同源 | 是 |
| 4 | D:守卫 + 审计 | 是 |
| 5 | 文档:addendum 改「不必改」、`chat.md §2.7`、两处工具描述(`tools/bash.py:97`、`tools/sandbox.py:795`)提 `EXPERT_WORK_INPUTS_DIR`、发布清单加回滚纪律 | 随 4 |
| 6 | 真栈验收记录 + ROADMAP 销案 | — |

## 十三、否决的方案(连理由一起记)

1. **「本轮输入」段放系统提示词尾部** —— 长 run 里沉到中段,模型写代码时最容易漏;两家参考仓库也不放那里。
2. **把段落拼进用户自己的消息前面**(两家的做法)—— 我们的用户消息经 `/messages` 原样回给对接方,平台文字会污染他们的记录。改成隐藏消息放其后。
3. **每轮写代码前再贴提醒** —— 守卫拦下时的回复就是最准时的提醒;不抄错的轮次提醒无价值。
4. **硬链接** —— 让缓存的字节活到 run 目录过期(30 天),把 B-61 T11/T12 刚封住的增长口子重新打开。
5. **渲染层等预拉结果再决定渲染成什么** —— 要把渲染从 control-plane 挪进图里,动 `build_run_graph_input` 的单一入口约定;而名字本就确定,不需要等。
6. **通用长串检测(高熵 / 长度阈值)** —— 没有真实用例,会误伤多行规则与免责声明。
7. **作者完全不能写 `{{ }}`**(照两家把变量语法删掉)—— 存量模板全要改,且作者失去措辞与位置控制;「写了也不会错」已经达到同样的效果。
8. **让 `before_tool_dispatch` 中间件做守卫** —— payload 没有 inputs;放 `tools_node` 与绑定填参同处。

## 十四、待定 / 后续

- `materials` 这类 JSON 字符串契约,长期应改成真 JSON 数组(对接方侧改动);`value_parsed` 是过渡。
- 守卫的近似阈值 `min(3, 尾串长度 // 16)` 是按事故与终审误拦分析取的(裁定 P25);真栈验收与上线后命中明细出来后再定。
- 非 URL 长 ID 若出现真实用例,再议是否加 `kind: id` 一类声明(倾向不加,优先走绑定)。
- spotlight nonce 是「每次构建一个、构建跨 run 缓存」(`agent_factory.py:1101`),`spotlight.py` 模块注释写的 per-run 不准;顺手改注释,不开票。
