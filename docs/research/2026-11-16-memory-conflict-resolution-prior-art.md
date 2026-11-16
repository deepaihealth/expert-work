# 新说法和旧记忆冲突时,别家怎么办(B-300 调研,2026-11-16)

- **用途**:给 [B-300 提炼与去重合并重做设计](../superpowers/specs/2026-11-15-b300-extraction-redesign-design.md)第 9 版(「写和退分开」)当先例依据。设计里只留一张短表,细节与出处在这里。
- **范围**:对照表 20 行(mem0 新旧两条管线各算一行;16 行读了源码,其余 4 行只有官方文档),另引 12 篇论文。只查了一件事:新说法与已有记忆冲突时怎么处理、置信度用在哪、谁能改谁。
- **局限**:三家消费级产品(ChatGPT / Claude / Gemini)的内部规则不公开;Cognee 默认写入路径、MemOS 检索侧没读;论文只搜了两轮,不算穷尽。各家自报的分数读题模型和判官不同,不能当排名。
- 本地仓库的路径(`~/src/github/…`、`dl/…`)是调研时读码的位置,不在本仓库里。
- **哪些出处在写设计第 9 版时复核过、哪些没有**:`~/src/github/` 下的 6 个仓库(mem0、OpenViking、deer-flow、openclaw、hermes-agent、claude-code-source)本机还在,抽查了其中 mem0 的一处(`prompts.py:693-699`),对得上;其余行号没有逐条重开。`dl/` 下的 graphiti、langmem,以及只给了 github.com 地址的 Letta、Cognee、MemOS、A-MEM、Mastra、Hindsight、Memobase,**没有重新打开**。全部 12 篇论文的数字、Supermemory / ChatGPT / Claude / Gemini 的文档说法都取自调研当时读到的原文,本仓库里没有副本,**无法在本地复核**;编号为 26xx 的几篇是很新的预印本,引用前建议再核一次。


## 结论

- **没有一家用「模型给每条记忆打的置信度」去拦对旧记忆的更新。** 查过的 20 来家里,置信度只用于排序、淘汰、入库门槛,或者干脆写进正文。
- **「并存」本身是正经做法,但别家的并存都带关系**:旧的标成「已被取代」、连到新的、带日期。我们现在的并存没有连线、没有标记,也没人收拾,所以召回分不清。
- **主流是「新的用户说法取代旧的,旧的留着做历史」**,由模型判断「是不是同一件事、能不能同时成立」,程序只做它自己确知的事(时间先后、谁说的)。
- **犹豫的话留在正文里**(「用户说可能是 Z,不确定」),不做成分数,也不做成闸门。
- 实测证据偏向「别拦」:拦得保守会丢真更新;错在「漏更新」的远多于「改错了」。

标注:【读码】= 调研时读了源码原文;【文档】= 官方文档;【论文】;【推断】= 没直接读到。本地仓库给 文件:行,网上给 URL。

## 1. 对照表

七个问题:① 冲突时怎么办、谁定 ② 有没有置信度 / 重要度、拦不拦更新 ③ 有没有来源分层保护 ④ 犹豫怎么表示 ⑤ 时间有效期 / 新的优先 ⑥ 召回给什么 ⑦ 实测数字(见第 2 节)。

| 系统 | ① 冲突处理 | ② 置信度 | ③ 来源分层 | ④ 犹豫 | ⑤ 时间 | ⑥ 召回 |
|---|---|---|---|---|---|---|
| **mem0 旧管线** | 模型选 ADD/UPDATE/DELETE;矛盾 → DELETE,向量库里真删,只在历史表留一笔【读码 `~/src/github/mem0/mem0/configs/prompts.py:264`;`mem0/memory/main.py:2114-2136`】 | 无 | 无 | 无 | 无 | 只有最新 |
| **mem0 V3(现行)** | **只新增,不改不删**;新记忆带 `linked_memory_ids` 连到相关旧记忆(含「矛盾」这一类)【读码 `prompts.py:693-699`;文档 `docs/changelog/highlights.mdx:234`】。平台版在写入时把被推翻的旧记忆标「已被取代」并连到新的,不删【文档 `docs/platform/features/dream.mdx:60-62`】 | 无单条置信度;检索分数是多路融合。「记忆衰减」只调排序,0.3×~1.5×,「从不过滤」【文档 `docs/platform/features/memory-decay.mdx`】 | 模型标 `attributed_to: user/assistant`,只存不拦【读码 `prompts.py:935`;`main.py:1038-1039`】 | 无字段;要求把「试试看 / 暂时」写进正文【读码 `prompts.py:620`】 | 提炼时把变化写成「从 X 换成 Y」【`prompts.py:612-618`】;平台版有取代标记 | 默认新旧都给、旧的带「已被取代」徽标;`latest_only=true` 只给现值【`dream.mdx:83-87`】 |
| **Zep / Graphiti** | 模型判「重复」和「矛盾」**两张分开的清单**;矛盾的旧边不删,`invalid_at` 设成新边的 `valid_at`【读码 `graphiti_core/prompts/dedupe_edges.py:25-33,85-97`;`utils/maintenance/edge_operations.py:748-783`;论文 2501.13956 §2.2.3「consistently prioritizes new information」】。矛盾要有「旧的不再成立」的明确证据,「光凭时间新」不算【`dedupe_edges.py:87-90`】 | 记忆本身无。可选的判定客户端对「**这次矛盾判断**」取概率,≥0.8 才算矛盾【读码 `llm_client/openai_decisions_client.py:61-62`;`decision_prompts.py:286-305`】—— 门槛在「判断有多确定」上,不在「记忆有多确定」上 | 无层级;每条事实带来源消息 | 提炼时去掉模型自己加的犹豫词【`prompts/extract_nodes_and_edges.py:149`】;用户的犹豫没有专门规则 | 四个时间:何时为真 / 何时不再为真 / 系统何时记下 / 何时作废。程序按时间比较:只有更早的旧边才会被新边作废【`edge_operations.py:773-781`】 | 每条事实带「从何时到何时」,到「至今」算有效;作废的也会给【读码 `search/search_helpers.py:23-58`】 |
| **OpenViking** | 画像:冲突的可变状态「换成最新确认的值,这里不留历史」;事件只新增【读码 `~/src/github/OpenViking/openviking/system_assets/memory/memory_templates/builtin/profile.yaml:67-68`】。模型定 | 无 | 只收参与者「明说或确认的」;助手单方面的话、推测 → 丢【`profile.yaml:9-11,36-37`】。是入口过滤,不是更新保护 | 推测直接不记 | 每条末尾「(as of 日期)」= 最近一次确认的日期【`profile.yaml:78-82`】;链接类型里有 `contradicts` / `evolved_from`【`session/memory/dataclass.py:52-53,95-98`】 | 画像只有现值;事件全留 |
| **deer-flow** | 模型给 `factsToRemove`,程序直接删【读码 `~/src/github/deer-flow/backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/updater.py:1027-1030`;提示词 `core/prompts/memory_update.chat.yaml:118`】 | **有,模型打**(0.9+ 明说、0.7~0.8 强烈暗示、0.5~0.6 推断)【`memory_update.chat.yaml:66-69`】。用在三处:**入库门槛** 0.7【`updater.py:1133`】、超上限时淘汰低分的【`updater.py:72-85`】、召回排序。**不拦删旧的** —— 删不删是另一个决定 | 「更正」类别置信度 ≥0.95、不被按年龄清理【`memory_update.chat.yaml:116`;`updater.py:421-424`】 | 低分 = 不入库 | 无有效期字段;有「预计多少天后复查」 | 只有现存的,按置信度排 |
| **openclaw** | 后台整理:模型选 added / merged / superseded;**程序要求「取代」必须带血缘键**,否则拒绝【读码 `~/src/github/openclaw/extensions/memory-core/src/dreaming-consolidation.ts:23-30,260-263`】。wiki 层:主张有状态(supported / contested / contradicted / superseded)【`extensions/memory-wiki/src/claim-health.ts:10`】 | wiki 主张有置信度,**只进排序和显示**(+置信度×10;有争议 −6)【`memory-wiki/src/query-scoring.ts:313-332`;`claim-health.ts:95-105`】 | 来源由程序按会话结构定;带网页结果的轮次不晋升(见 11-15 调研,设计 §1.3) | 无 | 新鲜度分三档,进排序 | 矛盾成簇列出,交给检查工具【`memory-wiki/src/tool.ts:213-229`】 |
| **hermes-agent** | 智能体自己用工具改 / 删条目,就地覆盖【读码 `~/src/github/hermes-agent/tools/memory_tool.py:372-394`】 | 无 | 无 | 无 | 无 | 只有现文 |
| **Claude Code 记忆** | 「发现错了或过时就更新或删除」;后台整理「两份文件矛盾就改错的那份」【读码 `~/src/github/claude-code-source/src/memdir/memdir.ts:215`;`src/services/autoDream/consolidationPrompt.ts:51,60`】。模型定 | 无 | 无 | 无 | 召回时提醒「这条记忆 N 天前的,是当时的观察不是现状」【`src/memdir/memoryAge.ts:33-42`】;「与眼前所见冲突,信眼前的,并去改记忆」【`memoryTypes.ts:201-202`】 | 只有现文 + 年龄提醒 |
| **Letta / MemGPT** | 就地改写记忆块,模型定;反思子代理:「新信息与旧记忆矛盾,在原处改掉,不要并排放」「以最新证据为准」【读码 github.com/letta-ai/letta-code/blob/main/src/agent/subagents/builtin/reflection.md;archive 分支 `letta/functions/function_sets/base.py`】 | 无 | 无(反而鼓励推断) | 无 | 只约定写绝对日期 | 核心记忆只有现文;历史在 git 里,不进上下文 |
| **LangMem** | 模型在一次调用里打补丁 / 新增 / 删除(删除默认关);「错了或过时就按新信息更新」【读码 `dl/langmem/src/langmem/knowledge/extraction.py:473,568-570`】。原地覆盖 | 无字段。要求把不确定**写进正文**:「用置信水平 p(x) 加理由注明」【`extraction.py:191,203`】 | 无 | 写在正文里 | 提示词只说「按可靠性和新近程度加强」【`:196`】 | 只有最新 |
| **Cognee** | 默认不处理;可选的事后复审:冲突事实**不删不改只打标** current / superseded / conflicting,模型判;用户可以经接口裁决【读码 github.com/topoteretes/cognee `cognee/infrastructure/llm/prompts/review_conflicts.txt`、`cognee/modules/improve/stages.py`】 | 无单条置信度;反馈权重只调排序 | **用户的裁决不被自动复审推翻**【读码 `cognee/tasks/memify/review_conflicts/write_conflicts.py`】—— 保护的是「裁决」,不是「事实来源」 | 「计划中 / 提议中」的值不算冲突 | 会随时间变的属性:最新为现值、更早的为被取代;日期排不出先后 → 全标冲突;不会变的属性 → 永不自动定论 | 新旧都给,带状态和日期 |
| **MemOS** | 重组器(默认关):模型判矛盾后融合,「优先更新的信息、置信度更高的数据」;融合不成 → 程序按更新时间**删掉旧的**。反馈路:改旧条、旧版归档【读码 github.com/MemTensor/MemOS `src/memos/memories/textual/tree_text_memory/organize/handler.py`、`templates/tree_reorganize_prompts.py`】 | 有字段,但看到的赋值全是写死的 0.99;只作为给模型的参考,**没有程序闸门** | 有来源字段;没找到更新保护 | 写进正文(「大约 2025 年 6 月」「具体日期不明」) | 状态 + 归档版本,无有效期字段 | 归档的默认不召回【推断】 |
| **A-MEM** | 不处理矛盾,一律新增并存;「演化」只改邻居的标签和上下文,不改内容【读码 github.com/agiresearch/A-mem `agentic_memory/memory_system.py`】 | 无 | 无 | 无 | 只有创建时间 | 新旧都按相似度给,带时间,回答的模型自己分辨 |
| **Supermemory** | `updates` 关系:新的在检索上取代旧的,历史留着【文档 supermemory.ai/docs/concepts/graph-memory.md】 | 无 | **模型推断出来的记忆标 `isInference`、进待审队列**,批准才转正【文档 supermemory.ai/docs/memory-review.md】 | 无 | `isLatest`;「矛盾时,更新的赢」 | 默认最新 |
| **Mastra 观察记忆** | 只追加带日期的观察;压缩时「用户改主意了,新旧都留、带日期、说明哪个是现值」【读码 github.com/mastra-ai/mastra `packages/memory/src/processors/observational-memory/reflector-agent.ts`】 | 模型打三档优先级,只管压缩时留不留 | 「用户的陈述是权威」;后来的提问不推翻先前的陈述 | 不确定的归低优先级,措辞留在正文(「用户可能更喜欢深色模式」) | 程序按时间重排 | 新旧都在,带日期 |
| **Hindsight** | 原始事实只追加;汇总观察由模型逐条判 无关 / 重复 / 改变 / 结束;「改变」写新值并可附旧值【读码 github.com/vectorize-io/hindsight `engine/consolidation/prompts.py`】 | 原来只在「观点」上有置信度,**2026-04 整列删掉了**(迁移 `g2h3i4j5k6l7_remove_opinion_fact_type.py`) | 无推断分层 | — | 「最近**说的**值是现值,不是最后处理的那条」;**时间先后由程序比**,迟到的旧事实只能作为「早先状态」附上 | 现值 + 括号里的早先状态和日期 |
| **Memobase** | 按槽位,模型选 追加 / 更新 / 放弃,原地覆盖【读码 github.com/memodb-io/memobase `prompts/merge_profile.py`】 | 无 | 无 | 无 | 时间写在正文里 | 只有现值 |
| **ChatGPT 记忆** | 官方没讲算法;用户可以说「这条错了 / 过时了」【文档 help.openai.com/en/articles/8590148-memory-faq】。泄露的工具说明里,更正是写进正文的【社区,非官方】。论文 2410.10813 §3.4:「聊久了会覆盖掉关键信息」 | 无 | 区分「保存的记忆」和「聊天历史」,没说谁保护谁 | 未说明 | 有「版本历史」控件 | 未说明 |
| **Claude 记忆** | 记忆工具:就地改文件,模型定;只有「保持最新、连贯」一句,无矛盾专门指引【文档 platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool】 | 无 | 无 | 无 | 无 | 只有现文 |
| **Gemini 记忆** | 「直接在对话里纠正」;官方承认「不一定总对」【文档 support.google.com/gemini/answer/16598469】 | 未说明 | 未说明 | 未说明 | 未说明 | 未说明 |

## 2. 实测证据

各家自报的数字,读题模型和判官不同,不能当排名用。

**LongMemEval「知识更新」类(78 题)**

| 系统 | 做法 | 得分 | 来源 |
|---|---|---|---|
| 全文塞给 gpt-4o / mini | 不做记忆 | 78.2 / 76.9 | 论文 2501.13956 表 3 |
| Zep | 作废 + 时间戳 | 83.3(gpt-4o)/ 74.4(mini,低于全文) | 同上 |
| mem0 旧(写入时改 / 删) | 模型选操作 | 79.5 | mem0.ai/blog/mem0-the-token-efficient-memory-algorithm |
| mem0 新(只新增) | 读的时候排新旧 | 93.6 | 同上 |
| Supermemory | 版本链 | 88.5(另一页写 100,口径对不上) | 论文 2512.12818 表 3 |
| Mastra | 只追加带日期 | 85.9~96.2 | mastra.ai/research/observational-memory |
| Hindsight | 事实只追加 | 84.6~94.9 | 论文 2512.12818 表 3 |

- 这一类的判分规则:答案里有新值就算对,同时提到旧值也不扣分(论文 2410.10813 图 10)。所以「新旧都给」的做法不吃亏。
- mem0 从 79.5 到 93.6 不是干净的对照(同一版还改了别的)。它自己的说法:写入时的合并「正是上下文被毁掉的地方,覆盖有时抹掉关键信息,删除有时删掉以后有用的」。

**专门测更新 / 冲突的**

- **MemoryAgentBench**(论文 2507.05257 表 19):同一模型换三种覆盖策略 —— 默认 36、「一律信后来的」40、「保守,只在明确否定时才覆盖」**28**。作者:保守规则「过于谨慎,挡住了有效的更新」。**【推断】这是类比,不是同一件事**:那三种策略多半是答题时「新旧冲突信谁」的规则,不是写入时的闸门;它只说明「保守地不让新的盖旧的」在那个测试里吃亏,不能直接当成我们写入侧拦截的效果数。
- **HaluMem**(论文 2511.03506 表 3):更新这一步,多数系统漏掉一半以上该做的更新(mem0 漏 74%、Zep 漏 52%),改错的不到 2%。主要问题是漏,不是改错。作者与 MemOS 有关联。
- **BEAM**(论文 2510.27246):「解决矛盾」是所有方法最弱的一项(0.00~0.05)。
- **STALE**(论文 2605.06527):后来的话没有明说否定、但让旧记忆不成立,这种情形各框架只有 5~18 分。
- **MemClaw**(论文 2606.24535 §8)报告的线上事故和我们是同一类:一道写入前的闸门把矛盾的新写入挡掉了,后面负责取代旧值的那一步根本没机会跑。

**置信度靠不靠得住**

- 模型口头给的置信度普遍偏高,集中在 80%~100%,多是 5 的倍数(Xiong 等,论文 2306.13063)。0.95 对 0.8 的差别大半是噪声。
- 论文 2606.22030:按措辞估可靠度可以被骗(「一句说得很笃定的假话拿到 0.96」)。
- 论文 2606.29279:旁边挂一个「未核实」标签会被下游忽略;**把犹豫的措辞留在记忆正文里**才管用。
- 存来源标签(谁说的)有效:越权操作 16.9% → 0.0%(论文 2608.01679)。所以「谁说的」值得做成字段,「有多确定」不值得。

## 3. 没人做的事

- **没有一家用模型给单条记忆打的置信度,去拦对已有记忆的更新。** 逐家核对过:上表 20 家中,有置信度的只有 deer-flow(管入库门槛、淘汰、排序)、openclaw wiki(排序)、MemOS(写死 0.99,给模型参考)、LangMem(写进正文)、Hindsight(已删)。Graphiti 的 0.8 门槛是对「这次矛盾判断」的,不是对记忆的。
  - 没能核实:ChatGPT / Claude / Gemini 的内部规则不公开;Cognee 默认写入路径、MemOS 检索侧没读;论文只搜了两轮,不算穷尽。
- **没有一家把「用户亲口说的」和「用户对助手的回答」分成两层、让前者挡住后者。** 分层的都只分「用户的 / 模型推断的」(Supermemory、OpenViking、Mastra、AuthMem)。我们的 2 层压 1 层是自己的设计,没有先例。
- **没有一家让并存的冲突行既不带标记也不带连线。** 并存的都有「已取代」状态、关系边或成对日期。
- 相反方向也没人做:没有一家在冲突时去问用户(Cognee 只提供事后裁决的接口)。

## 4. 我们三次补丁的共同病根

去重合并的「更新」把两个决定捆在了一起:**新的写不写**,和**旧的退不退**。拦截规则本意是「不许退旧的」,结果连新的也一起处理了(先是丢,后来改成并存)。Graphiti 是两张分开的清单,mem0 V3 是「永远写新的 + 另外标取代」,deer-flow 是 `newFacts` 和 `factsToRemove` 两个字段。把这两个决定拆开,「更新被拦所以新值没了」这一类问题在结构上就不存在。

另一个:11-16 回归里带事件日期的 9 条错了 6 条(设计 §15 第 8 条)。模型打的标记当硬闸门用,已经吃过一次亏;置信度是同一类东西,而且更糊。

## 5. 这份调研怎么用到设计里

- 用户 2026-11-16 据此拍板「方案二:写和退分开」:用户一方的候选只要不是重复就一定写下;「让哪几条旧记忆不再成立」是去重合并输出里单独的一张清单,程序逐条检查后才退;层级只用于「助手 vs 用户」和更正;犹豫留在正文里;模型打的置信度退出所有拦截。做法、逐场景的结果、评测线见设计第 9 版 §6.3、§7、§11、§15 第 10 条。
- 没选的两条路:
  - **只把同层的置信度比较去掉**(半天的改动):只治当天第三个补丁那个洞,短回答式的改口仍并存,已经并存的旧行没人收拾。
  - **只新增、读的时候再定新旧**(mem0 V3 / Mastra 的路):写入侧最省,但要在召回这条主链路上认出「这几条说的是同一件事」并排新旧,等于把去重合并搬到读路径;那些高分的前提是很强的读取侧重排,我们没有。
- 调研时没把握、设计里已经去查的:一条新行退多条旧行存储层要不要改(要改接口、不要迁移,设计 §7.8);「用户确认」的误标率(只有部分数据,设计 §6.5);模型分辨「犹豫」的准确率(没量过,设计 §11 加了评测线)。
