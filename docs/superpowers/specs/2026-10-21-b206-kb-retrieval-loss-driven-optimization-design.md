# B-206 知识库检索丢分优化方案(EnterpriseRAG-Bench 诊断驱动)

- **状态**:设计稿 **v2**(2026-10-21 重调研修订),**登记不排期**(用户拍板:方案落库 + 登记 ROADMAP,开工时间等指令)。
- **v2 修订记录(2026-10-21)**:用户要求「对整个设计重新调研和评估,确保合理性,该细化的细化」+「不要只针对 qwen,会有不同厂家的向量 / 重排模型;文档也不只有示例和评测集」。第二轮调研五路(切块与父子块 / 查询侧与关键词路 / 重排与结果呈现 / 智能体检索与索引增强 / 各厂商嵌入与重排接口),都读原文;仓库现状逐条重核;ERB 正确文档的结构形态新算了一遍。改动汇总见 §0。
- **v2.1(2026-10-21 晚)**:用户「WeKnora 是目前比较火的知识库项目,深度探索一下,有什么可以参考借鉴」→ 四路深读本机 `/Users/mac/src/github/WeKnora`(v0.8.3,2026-10-21,Go)入库切块 / 检索流水线 / 智能体工具层 / 前端管理面,逐条 file:line,借鉴清单见 §5.6,已并进对应 K 项与 §6;同时核实阿里云 RDS 插件表:**`pg_textsearch`(BM25)在 PG 17 / 18 可装、PG 16 不可**,K-9 改成两条路。
- **与 B-175 的关系**:K-2(读文档)、K-12(生成问题)、K-13(列文档)、K-15(元数据抽取)是 [B-175 设计](2026-10-10-b175-knowledge-base-v2-design.md) 里计划过、一直没做的能力;本文给出评测证据和先后顺序,开工时按本文的批次走,B-175 对应条目以本文为准。B-175 §3.2「Anthropic 实测 HyDE 效果差」是误读,已更正(见 §8.3)。
- **用户约束**:① 方案必须**通用**,不能只针对某批资料设计(10-20);② 参考开源项目与行业最佳实践(10-20);③ 加父子块要同时设计 **Web 前端怎么展示**(10-20);④ 评测集只用来诊断,不拿来调参数(10-18 拍板,开发验证仍用华大题);⑤ **不能只针对阿里云 qwen 系列**,平台会接不同厂家的向量模型和重排模型(10-21);⑥ **文档形态不只示例资料和评测集**(10-21)。
- **依据**:
  1. EnterpriseRAG-Bench(Onyx 出品,以下简称 ERB)7 源子集 363 题评测(10-20,104,966 篇 / 85.5 万段,判分 deepseek-flash);
  2. 逐题诊断:答错 / 只找到部分 / 判对但不全的 89 题,逐题对照原文;
  3. 检索重放(10-21):没找到或只找到部分正确文档的 82 题,按评测时的代码重建库,重放模型当时的 369 次检索 + 用题目原文各搜一次,记录正确文档在每一步的名次(§2.4,¥1.24);
  4. 外部调研两轮:Onyx 论文与源码、Dify / RAGFlow / FastGPT / LlamaIndex / LangChain / dsRAG / Unstructured 源码与文档、Anthropic / Cohere / Mixedbread / Flexor / metor / Databricks / NVIDIA / Chroma 文章、阿里云与各厂商 API 文档、论文十余篇——出处见 §11;
  5. 本仓库现状盘点(file:line 见 §3)。

---

## 0. v2 改了什么(给看过 v1 的人)

| 项 | v1 | v2 | 原因 |
|---|---|---|---|
| 设计原则 | 4 条 | +「厂商中立」+「文档形态检验表」 | 用户 10-21 约束 ⑤⑥ |
| K-1 父子块 | 命中后自动带前后各 1 段 | **子段检索、父段整体返回、父段分数 = 子段最高分**;父段 = 结构段 ≤ 1,024 token,无标题结构按字数窗口凑,短文档整篇 | Dify 源码 / RAGFlow / LangChain / Bedrock 都这么做;LongRAG 召回 52→72;ERB 正确文档一半的段没有标题路径(§2.6) |
| K-3 切块 | 最小断点 32→200 | 结构优先、**语义断点默认关**、子段目标 200~400 token、短节合并、**重叠 0**、对话体按行 | 两篇 2025–26 论文:语义切块无稳定收益;Chroma:重叠只降精度;父子模式各家重叠都是 0;语义断点要逐块嵌入,关掉省一遍入库嵌入 |
| K-4 条数 | 默认 5→10 左右 | 默认 **10 个父段 / 总预算 8k token / 每文档 ≤ 3**;字段 = 标题 / 上传时间 / 元数据里的版本与生效日期 / 来源;文档短编号;**加条数必须用真实答题模型验**(有反面证据) | Anthropic 20 段最优;Onyx 25 块预算;《Lost in the Noise》:1 篇无关文档让 DeepSeek-R1 掉 25% |
| K-5 查询侧标记 | DashScope `text_type=query` | 抽象成模型目录能力字段(参数 / 指令前缀 / 无),厂商不支持自动退化 | 约束 ⑤;各家参数名都不同(§5.1 K-5 表) |
| K-6 工具说明 | 两句 | 措辞定稿;依据改口径 | Onyx 论文:多轮探索找全题**文档召回 59.0 > 46.5,但正确率 35.0 < 40.0**——要「搜得全」必须配「读到再下结论」 |
| K-7 重排稳定性 | 换 qwen3-rerank + 退避 | 上限 / 限流**按模型配置**不写常量;**单视图减半调用**;退避参数定稿;新模型 qwen3.7-text-rerank 列入 | 约束 ⑤;阿里 2026-10-12 文档 |
| K-9 BM25 | ParadeDB 不行 → tsvector 上补 | 路径定稿:tsvector 自算 BM25(词频取位置数、文档长度入库存、每库 df 表增量维护);RDS 三个 BM25 扩展都装不了;现有计分**已有 idf**,缺的是词频饱和与长度归一 | 阿里 RDS 插件表(2026-09-23);sql.py:317 |
| K-10 多路查询 | 三路 + 加权 RRF | **降预期**:换说法 > 关键词扩写;原问题必留;按题看「被带偏」;融合加「归一化加权和」对照 | 三篇论文:扩写对强检索器净负 3~6 点;凸组合优于 RRF |
| K-11 / K-12 | 二选一 | **不二选一**,各自 A/B,可同时开;K-12 数字改口径 | 无头对头证据;RAGFlow 两者并列可同开;Doc2Query 的 +16% 是「过滤 vs 不过滤」 |
| K-14 重排调参 | 池 50→100、调权重 | 加「**单视图**:标题 + 节 + 正文拼一串 + 任务说明」对照;池上限按模型 | 双视图加权没有任何外部先例;Cohere / Onyx / Qwen 都是单串 |
| K-16 稀疏向量 | 稠密 26→42 | 数字改:BM25 34.6 / 稀疏 40.5 / 合 42.0,收益主要是**替代** BM25;K-9 之后再看 | BGE-M3 论文 Table 3 / 11 |
| §7 不做 | — | AtlasNav 条理由改(它当时是榜首,找全 66.98);RAPTOR「+20%」注明口径;HyDE 理由改「收益不稳 + 延迟 3 倍」 | 调研核实 |
| §6 前端 | 一张表 | 三处页面 + 设置页逐项字段、接口要补什么 | 用户约束 ③ |
| §9 / §10 | — | 评测资产清单、每批判据、**批 0 评测地基** | 智能体层 A/B 脚本还在会话目录没入库 |
| K-9 路径(v2.1) | tsvector 自算 | **两条路**:RDS 升 PG 17 + `pg_textsearch`(阿里云已上架 1.3.1,仅 PG 17 / 18)或留 PG 16 自算 | 实测测试库 PG 16.13 无任何 BM25 扩展;插件表逐行核对 |
| WeKnora 深读(v2.1) | — | 借 15 条进 K-1 / K-2 / K-3 / K-4 / K-6 / K-7 / K-10 / K-12 / K-14 / K-15 / §6 / §9,清单见 §5.6 | 用户 10-21「比较火的知识库项目,分析借鉴」 |

---

## 1. 评测结果

| 项 | 数 |
|---|---|
| 综合分(363 题) | **70.9**(95% 区间 66.4–75.2);去掉 3 道重排被限流的题 71.2 |
| 同一判分重判公开答案 | RAGFlow 55.9、OpenAI File Search 66.0;逐题配对:比 OpenAI FS 高 4.9(0.3–9.6),比 RAGFlow 高 15.0(10.2–19.9) |
| 折成官方判分口径 | ≈ 69.8(我方判分比官方高约 1.1);按 2026-10-20 的榜:≥ 86 五家(codos 92.64、Flexor 91.48、CDL 90.91、Skyller 87.16、Mixedbread 86.58)、80–83 五家;我们约第 14 档(与 OpenClaw 并列);官方基线 OpenAI FS 61.0、Bash Agent 52.6、BM25 50.6、RAGFlow 50.2、单路向量 37.7 |
| **保留** | 我们只入了 10.5 万篇,榜上各家是 50 万篇全量;同 77 题从 5 千篇扩到 10.5 万篇掉 7.4(−14.0 ~ −1.2,期间代码也有变动)→ 全量大概率更低 |

按题型(我们 / OpenAI FS):基础 83.8 / 75.9;语义改写(94 题)53.3 / 43.7;**找全 19.0 / 46.3**;带条件 69.4 / 79.8;项目关联 42.4 / 39.9;冲突信息 89.0 / 65.6;文档内推理 81.6 / 78.9;杂项 86.4 / 90.9;资料里没有 100 / 100。

丢的 29.1 分:

| 类 | 题数 | 丢分 |
|---|---|---|
| A 正确文档从没出现在任何一次检索结果里 | 48 | 13.2 |
| B 只找到部分正确文档 | 13 | 3.6 |
| C 正确文档都出现了,仍答错 | 30 | 8.3 |
| D 判对但不完整 | 46 | 4.0 |

---

## 2. 诊断结论

### 2.1 头号原因:切块太碎,答案在相邻块里没交给模型

- 89 题正确文档共 4,253 段:**中位 264 字符**(p25 183、p75 396),30% 不到 200 字符;而切块上限是 512 token(英文约 2,000 字符)。
- 答错 / 不全的题里,正确文档平均每段不足 500 字符的占比:C 59%、B 84%、D 59%;**全对的 201 题只有 31%**。正确文档本身并不更长(中位 6.4 KB vs 5.5 KB)。全库 21% 的文档占了 48% 的段。
- 例子:一份 33 KB 会议记录切成 145 段;1.5 KB 的故障复盘切成 9 段(其中一段只有「P1」);1.4 KB 的客户记录切成 3 段。
- 后果:检索命中的常是摘要 / 标题那一小段,答案在旁边那段;模型要么说「资料里没写」,要么拿相似文档凑。**C 类「说资料没写」的 8 题,全部是答案所在的那段没被返回**,不是拒答提示的问题。
- **通用性已核**:测试环境唯一的中文知识库(1 篇、41 段)中位 134 字、83% 不到 200 字,上限 1,024 token——同样碎(样本小,开工时在华大开发集上复核)。
- 代码原因:`chunking.py:97` `MIN_SEMANTIC_CHUNK_TOKENS = 32`,语义断点只要缓冲区够 32 token 就切;B-175 设计 §5 定的目标是 300~500 token、同一父标题下相邻短节合并,实现没达到。

### 2.2 C / B / D 逐题分类(按修复落在哪一层)

| 组 | 检索排序 | 结果呈现(碎片 / 缺上下文 / 缺元数据) | 评测提示词(只评测有) | 判分 | 合计 |
|---|---|---|---|---|---|
| C 找到了仍答错 | 11 | 10 | 6 | 3 | 30 |
| B 只找到部分 | 8 | 4 | 0 | 1 | 13 |
| D 判对但不全 | 14 | 7 | 6 | 19 | 46 |

按原因:选了相似的干扰文档 21、说资料没写 9、列举不全 12、答案段没返回 12、漏检某篇 8、读错 / 版本混 4、判分存疑 23。

### 2.3 判分噪声

D 组 46 题里 19 题我方答案其实写了被判缺的内容(有的近乎逐字);C 组 3 题、B 组 1 题是与标准答案的争议。对别家同样严,**排名对比不受影响,绝对分偏低**。另:qst_0413 标准答案文档重复且对应两份内容冲突的文件;qst_0450 标准答案「8 篇」与语料不符;0061 / 0222 / 0287 / 0082 / 0128 / 0447 有近乎相同的干扰文档,标准答案本身有歧义。

### 2.4 检索重放:A / B 类正确文档丢在哪一步

10-21 按评测时的代码(wt-b198)重建同一个库(104,966 篇 / 855,880 段,嵌入全部命中缓存),重放 82 题里模型当时的 369 次检索,再用题目原文各搜一次;80 / 82 题重放结果与评测时逐篇一致(差的 2 题正是评测时重排被限流的那 2 题),花费 ¥1.24。按(题, 正确文档)计 203 对,取模型所有查询里走得最远的一步:

| 正确文档最远走到 | 模型的查询 | 题目原文 | 含义 |
|---|---|---|---|
| 两路前 50 都没有 | **57(28%)** | 111 | 真正的召回失败;24 题所有正确文档都在这一档 |
| 进了某一路,合并后掉出前 50 | 21(10%) | 16 | 合并名次全在 52~94(向量路 14、关键词路 7) |
| 进了前 50,被重排挤出前 20 | 13(6%) | 4 | 重排池名次 4~48,全部是被「带标题视图」的前 20 截掉 |
| 重排后在前 20,但排在模型要的条数之后 | **37(18%)** | 25 | 369 次检索里 279 次(76%)用的是默认 5 条 |
| 已显示给模型 | 75(37%) | 47 | |

结论:
- **条数太少是最便宜的一块**:37 对卡在「排得进前 20、但只给 5 条」。按文档分组后取前 10 篇,37 对里 20 对会显示;按段取前 15 条会显示 30 对 → 直接支持 K-4。
- **重排环节漏掉 34 对**:合并后掉出前 50 的 21 对,合并名次都在 100 以内 → 重排候选从 50 加到 100 能全部收进来;被带标题视图截掉的 13 对里有 3 对在重排池前 16 → 支持 K-14(两项都做对比)。
- **真正召回失败 57 对(28%)**:题目原文直接搜更差(111 对),说明「怎么问」影响很大;这些正确文档段数偏少(中位 9 段 vs 已显示的 13 段),检索入口少。这一块要靠查询侧(K-5、K-10)或索引侧(K-3、K-11 / K-12、K-9)补,重放分不出哪个更有效 → 在华大开发集上对比决定。

### 2.5 评测工具本身的限制(不算产品问题)

- 评测脚本每题最多 5 次检索:89 题(25%)用满,这些题平均 29.4 分、其余 84.4 分;用满的题里 40 题从头到尾没找到正确文档——**用满多半是找不到的症状**。另有几题模型只搜 2 次就停(0341 / 0364 / 0376)。
- 评测时 85.5 万段单次检索中位 11.8 秒、P90 25.4 秒:这是评测脚本**故意用精确扫描**保证可复现;生产走 HNSW 近似索引(迁移 0021),不能据此推断生产延迟。

### 2.6 正确文档的结构形态(v2 新算,决定父段怎么定义)

89 题的 205 篇正确文档、4,253 段(诊断时导出的分段):

| 项 | 数 |
|---|---|
| 段没有标题路径(文档没有标题结构,或 .txt 按纯文本读) | **2,113 段,50%**;只有 **38%** 的文档有任何标题 |
| 有标题时「节」(同一标题路径下的连续段)的长度 | 1,698 节,**中位 335 字符**、p90 1,166 |
| 文档总长 | 中位 6,411 字符;≤ 4k 33%、**≤ 8k 60%**、≤ 12k 82%、≤ 16k 96% |
| 每篇段数 | 中位 13、p90 47 |
| 现有段「前后各 1 段」窗口 | 中位 829 字符、p90 1,721;「前后各 2 段」中位 1,397 |

含义:**「父段 = 标题节」对一半的资料不成立**(没标题),成立的那一半节也太短(335 字);父段必须按**字数窗口**定义,标题只是优先切点;「短文档整篇」若定在约 2,000 token(英文约 8k 字符)能覆盖 60% 的正确文档。v1 的「带前后各 1 段」只给 829 字符,仍然偏小。

---

## 3. 现状盘点(origin/main,v2 重核)

| 环节 | 现状 | 位置 |
|---|---|---|
| 切块 | Markdown 结构切块 + 语义断点(相邻块余弦距离 ≥ 0.4,**入库时逐块嵌入一遍**);最小断点 32 token;同节内重叠;表格按行切、重复表头;.txt 按空行分段、无标题(B-199);默认 512 token / 重叠 64,每库可改 | `control_plane/knowledge/chunking.py:89,97`、`ingestion.py:420`、`protocol/knowledge.py:26` |
| 索引文本 | `[Document: 文件名标题]` + `[Section: 标题路径]` + 正文——便宜版「上下文头」,标题来自文件名不是内容 | `chunking.py` `chunk_retrieval_text` |
| 父子块 | `knowledge_chunk.chunk_type`(默认 `text`)、`parent_chunk_id` 列已建(迁移 0165),**入库不写、检索不读**;`embedding` 列 NOT NULL(迁移 0021:125) | `migrations/versions/0165_knowledge_model_v2.py`、`0021_knowledge_base.py:125` |
| 关键词路 | jieba 分词 + Postgres `simple`;计分 = **Lucene BM25 的 idf** 加权的「查询词覆盖率」+ 编号 / 整句命中加分 + `ts_rank` 平手;**没有词频饱和、没有长度归一**——不是完整 BM25 | `persistence/knowledge/text_search.py`、`sql.py:317,1495-1501` |
| 向量路 | 嵌入一律走 OpenAI 兼容 `/v1/embeddings`,只发 `{model, input}`,每批 10 条;目录里的嵌入模型:openai `text-embedding-3-large`、glm `embedding-3`、qwen `text-embedding-v4`;**查询没声明 query / document 不对称**;生产 HNSW(`ef_search ≥ 200`,iterative scan) | `orchestrator/llm/embedder.py:39,109`、`protocol/model_catalog.py:181,404,486`、`sql.py:425` |
| 融合 | 加权 RRF,两路等权;每路召回 50(上限 100) | `orchestrator/tools/knowledge.py:53-57,417` |
| 重排 | 两类实现:① DashScope 原生打分接口(目录只有 `qwen3-vl-rerank`),「双视图」:带标题视图取前 20,再用正文视图重排,0.7 / 0.3 合成——**每次检索调两次**;② 其它厂商走 **聊天模型列表排序**(`LLMReranker`,无分数,每条截 600 字符)。任何异常退回融合序并标 `rerank_degraded`,**不重试** | `knowledge.py:60,71-72,247,479,540-582`、`llm/rerank.py:28`、`runtime.py:1161` |
| 嵌入重试 | 入库时 429 / 5xx / 超时退避重试;**检索时不重试** | `ingestion.py`(`_BackoffEmbedder`) |
| 文档字段 | `filename / title / source_type / source_uri / external_id / metadata(JSONB) / created_at / doc_seq(租户内递增短编号)`;**没有日期 / 版本列** | 迁移 0021、0165 |
| 支持格式 | pdf / docx / pptx / xlsx / md / txt / html / csv(markitdown 解析);没有 OCR(B-175 P5 计划) | `knowledge/parsing.py:27` |
| 工具 | 只有 `knowledge_search(query, limit)`,默认 5 条、上限 20;输出 `[文件名#段号]` + 标题路径 + 正文 + 能否作答提示;**不带文档日期 / 版本 / 来源,不按文档分组,不扩展相邻段,没有读文档 / 列文档工具,没有过滤** | `knowledge.py:62-63,611,733` |
| 管理端 | 每库可配:切块上限 / 重叠 / top_k / 相似度阈值 / 检索方式 / 重排开关 / 重排分数下限;分段预览只读分页;检索测试显示命中段 + 向量分 + 路径 | `SettingsTab.tsx:37-43`、`SegmentPreviewDrawer.tsx`、`RetrievalTestTab.tsx` |
| 评测资产 | 华大主集 303 题 + 真实问法集 172 题 + 留出集 22 题(期望文档 + 锚文本,`kb_eval.py` 算 hit / MRR / nDCG);**智能体层 A/B 脚本在会话目录,没入库** | `tools/eval/kb_eval.py`、`datasets/kb/` |

---

## 4. 设计原则

1. **通用**:每条写明对真实租户资料(中文营养资料、合同、操作规程、会议纪要、销售对话)是否成立;只对评测资料管用的不做(§7)。
2. **厂商中立(v2)**:凡涉及模型的条目,写成「能力 × 厂商」:平台抽象成模型目录(`ModelEntry`)上的能力字段,具体参数名 / 上限 / 限流**按模型配置**,代码里不写死某家的常量;厂商不支持的能力**自动退化**(不报错、不静默,打降级标记)。
3. **文档形态检验表(v2)**:每条方案过六种形态——Ⓐ 有标题结构的长文档(手册 / 制度 / 合同)、Ⓑ 没有标题的长文本(会议纪要、纯文本导出、聊天记录)、Ⓒ 短文档(通知、FAQ 条目、一页纸)、Ⓓ 表格(产品表、价目表、xlsx / csv)、Ⓔ 对话体(客服 / 销售聊天,一行一句)、Ⓕ 扫描件 / 图片(目前没有 OCR,进来是空文本)。写「成立 / 不成立 / 要另处理」。
4. **只改平台层**:检索、切块、知识库工具及其说明;评测脚本的提示词不算。
5. **评测集只诊断**:开发阶段在华大开发集(303 题 + 真实问法 172 题)上前后对比;留出集 22 题收尾跑一次;ERB 最后复测一次当留出集。
6. **先量再做**:有外部实测数字的优先;没有的先做对比试验;收益是假设的能力以评测结果决定开不开(同 B-175 原则 6)。
7. **默认开启、不让租户去配**:新能力默认就开;库级参数只留必要的几个,其余是平台默认。
8. **前端同步**:凡是改变了模型拿到什么,分段预览 / 检索测试 / 轨迹面板 / 库设置四处同步改(§6)。

---

## 5. 方案清单

每条:改什么 / 治哪类 / 依据 / 厂商 / 形态 / 代价 / 判据。工作量 S = 1~2 天,M = 3~5 天,L = 1~2 周。

### 5.1 第一梯队:便宜、直接对症

**K-1 父子块:子段检索、父段返回** —— L,要重新入库(与 K-3 同批)

- 改什么:
  - **两层分段**。子段 = 检索单元(嵌入 + 关键词索引,K-3 定大小);父段 = 返回单元,存成 `knowledge_chunk` 一行(`chunk_type = 'parent'`,`embedding` / `tsv` 为空——迁移放宽 0021 的 NOT NULL;召回 SQL 加 `chunk_type <> 'parent'`),子段 `parent_chunk_id` 指向它。
  - **父段怎么凑**(入库时,子段切完后按文档顺序打包):① 整篇 ≤ 父段上限 → 整篇一个父段;② 否则连续子段打包到父段上限(默认 **1,024 token**,库级可调 512~2,048);③ 遇到**一级 / 二级标题变化**且当前父段已 ≥ 上限的一半 → 提前收口(有标题结构时父段尽量对齐章节;没标题时纯按字数);④ 表格:按行切出的子段共用「整张表(带表头)」为父段,超上限按行分几个父段;⑤ 父段带标题路径(取首个子段的)。
  - **检索**:召回、融合、重排仍在子段上做(不变);之后**按父段折叠**:父段分数 = 子段最高分(Dify 同款),记录命中的子段编号;同一父段只出一次;按 K-4 的每文档上限和总预算截断;`limit` 数的是父段。
  - **返回**:父段整段正文(不是「前后各 1 段」);父段超出剩余预算时,只给命中子段前后能装下的部分并标 `[已截断,用 knowledge_read 读全]`。
  - **维护**:父段正文由子段拼出(存一份便于读取);子段被人工编辑(`edited`)时重拼它的父段;重切块 = 新版本整套替换(B-175 规矩)。
  - **细节(WeKnora 同款,v2.1)**:整篇只有一个子段且与父段相同时不建父行(短文档省一份正文);父段行只存不索引,召回 SQL 用「可检索类型白名单」(text / question / faq …)而不是排除法;入库时记子段在父段里的字符区间(`start / end`),§6 的子段高亮靠它。
- 治哪类:诊断里「正确文档已显示、答案在没返回的段里」约 22 题(C 11、B 4、D 7);2.1 的根因在呈现侧的那一半。
- 依据(v2 核实):
  - 业界实现都是「子段命中 → 父段整体返回、父段分数取子段最高」:Dify 源码(`retrieval_service.format_retrieval_documents`,父段 = max 子段分;父 `\n\n` / 1,024 token、子 `\n` / 512,**无重叠**)、RAGFlow(子块带 `Mom` = 父块全文)、LangChain `ParentDocumentRetriever`(示例父 2,000 / 子 400 字符)、AWS Bedrock(层级切块时子块以父块替换返回)。
  - 量化:LongRAG(2406.15319)NQ 检索单元从 120 token 段换成约 4K token 组后 AR@1 **52.24 → 71.69**,且长单元仍用 512 token 子块取最大相似度编码——正是这个做法;dsRAG 的 RSE(把相邻命中段合成段落)KITE 评测 Top-k 4.72 → RSE 6.73(0–10 分);HiChunk(ACL 2026)固定 200 token vs 层级 + 自动合并,证据召回 74.06 → 81.03,且「检索 token 预算 2k→4k 越大越好」;LlamaIndex 自测 AutoMerging 4.27 vs 4.21、SentenceWindow 4.37 vs 4.22(n = 60 / 30,作者自评「差不多」)——所以父段要够大,±1 段这种小窗口收益小。
  - Onyx 的「±5 段」是**模型分类后才扩**,不是自动;v1 写法已改。
- 厂商:与模型无关。
- 形态:Ⓐ 父段对齐章节;Ⓑ 纯按字数窗口(ERB 一半的段就是这种,§2.6);Ⓒ 整篇一个父段;Ⓓ 整表为父段;Ⓔ 按字数窗口(K-3 保证不把一句话切开);Ⓕ 没文本,不适用。
- 代价:存储多一份父段正文(约 +1 倍文本,不加向量);每次结果字数上升(用 K-4 预算压住);召回 SQL 加一个条件。
- 判据:华大开发集 303 题「答案锚文本落在返回文本里」(父段级段命中)相对子段级 chk_hit@10 0.86 上升;文档命中 ≥ 0.95 不退;智能体层 60 题「说资料没写」的错答下降、编造不升;单次结果 token 在预算内。

**K-2 「读文档」工具(模型自己决定读多少)** —— M

- 改什么:新增 `knowledge_read(document, section | page | around | find)`:`document` 用结果里的文档短编号(`doc_seq`,租户内递增整数,结果里显示为 `[doc 1234]`)或文件名;四种读法——`section` 读某个标题下整节;`page` 按父段顺序分页读,每页默认 4,000 token(上限 16,000,与 Onyx `read_file` 的 16,000 字符同量级),返回 `第 n / N 页` 与下一页编号,页码超界时报「有效范围 0..N」而不是返回空;`around = <父段编号>, k` 读命中父段前后各 k 个父段(k ≤ 5,WeKnora `read_document(context=k)` 同款,直接对着「答案在相邻块里」这个头号丢分);`find = <关键词>` 在该文档内做关键词查找(每个词都要命中、大小写不敏感,最多 20 处,每处带前后各 1 个父段,给「找全」题逐篇核对用)。每次结果先给文档元数据头(标题、上传时间、版本、父段总数),正文带标题路径;结果里每个父段标 `match / context_before / context_after`,给轨迹面板用。工具说明写清何时用(结果被截断、要核对相邻内容、「找全」题逐篇核对),并写明「本轮已拿到全文的父段不必再读」。
- 治哪类:同 K-1,加「找全」题、长文档综合题。
- 依据(v2 核实):Mixedbread(2026-03)BrowseComp-Plus 上给智能体加「读整篇」工具,两种检索器都提分:80.00 → 90.48、79.52 → 87.59(上限 93.5);但同一家 toast-1(2026-08)在「文件系统智能体」基线上加搜索 / 读工具只省 token(80.6M → 23M)不提分(都 55)——**提分的前提是基线只看片段**,我们正是。产品先例:Onyx `read_file(file_id, start_char, num_chars ≤ 16000)`、Glean「Read document」、Vectara `web_get(head_lines / tail_lines)`、PageIndex 按树节点读。
- 厂商:与模型无关。
- 形态:全部成立;Ⓓ 表格按行分页;Ⓕ 不适用。
- 代价:多一两次工具调用、每次最多 4k token;受现有工具预算 / 轮次上限约束。
- 判据:智能体层 60 题答对率上升、编造不升;平均工具调用次数增幅 ≤ 1 次 / 题。

**K-3 修切块:结构优先、子段 200~400 token、不重叠** —— M,要重新入库

- 改什么:
  - 子段上限默认 512 → **400 token**(库级可调 128~1,024);**语义断点默认关**(`chunk_markdown_semantic` 保留为对比用,关掉省一遍入库嵌入——现在每个文档的每个块都要先嵌入一次来找断点);`MIN_SEMANTIC_CHUNK_TOKENS` 若保留语义断点则 32 → 200。
  - **短节合并**:标题下不足约 100 token 的节(只有标题、一句话、页码)并入下一节的首个子段(Unstructured `combine_text_under_n_chars`、Onyx `CHUNK_MIN_CONTENT = 256` 同义),标题路径取合并后首节的。
  - **重叠默认 64 → 0**:上下文由父段提供。
  - **对话体 / 一行一句**:纯文本里一段的各行多数以「说话人:」开头或行中位长度 < 40 字 → 按行成块,不把多句合成一个超长段再硬切;会议纪要「人名:发言」同理。
  - 表格:仍按行切、重复表头(B-199 / B-200 已有),父段见 K-1。
  - **规则细节(WeKnora 同款,v2.1)**:短节合并的目标长度取 max(200 token, 子段上限 / 2),只并相邻且标题前缀相同的节;无标题长文本(Ⓑ)除空行外再认「编号行 / 第 X 章 / `---` / 换页符」为切点;表格列数与表头不一致视为新表;**「重叠 = 0」要用显式开关,不能拿 0 值兜底**(WeKnora `overlap <= 0` 被当成默认 80,想关关不掉);切块结果校验四条(单块且文档长 > 2 倍上限;不足 50 字的碎块超过 25%;最大块不足上限 1/4;有块超过 2 倍上限)直接当 §9 形态测试集的 CI 判据。
  - 切块参数变更按 B-175「新版本」规矩:文档上记切块参数指纹,管理端能看出哪些文档是旧版本,「重新摄取」按文档或整库。
- 治哪类:根因,A / B / C / D 都有。
- 依据(v2 核实):
  - 大小:Chroma(2024-07,5 语料 472 问)Recursive 200 无重叠 Recall 88.1 / Precision 7.0,400 无重叠 89.5 / 3.6,800 带重叠 85.4 / 1.5;NVIDIA(2025-06)128 与 2048 两端最差、512~1024 居中、页级最好;中文 CRUD-RAG(2401.17043)单文档问答 64 / 128 / 256 / 512 token 的关键信息召回 68.6 / 70.8 / **72.2** / 68.7;《Rethinking Chunk Size》(2505.21700)事实题小块好、长描述题大块好——**没有普适最优,200~400 是折中**,大块的需求交给父段。
  - 语义切块:Vectara(NAACL 2025,2410.13070)「fixed-size chunking remains a more efficient and reliable choice」;《When Is Complex Chunking Worth It?》(2608.16586)「computationally expensive chunking methods rarely outperform simpler alternatives」,带标题的 enriched 是低成本改进(我们已有)。
  - 重叠:Chroma「reducing chunk overlap improves IoU」;父子模式下 Dify / RAGFlow / Onyx 重叠都为 0(Onyx 注释:「unclear if overlaps actually help quality at all」);只有小嵌入模型才靠重叠保召回。
- 厂商:与模型无关(tiktoken 只做计量)。
- 形态:Ⓐ 结构切 + 短节合并;Ⓑ 按段落 / 字数;Ⓒ 可能只有 1~2 个子段;Ⓓ 按行;Ⓔ 按行成块(新规则);Ⓕ 不适用。
- 代价:所有知识库重新嵌入(生产 10-13 查时 0 个知识库,开工前再核);入库嵌入次数**减半**(不再为语义断点嵌入块)。
- 判据:华大开发集召回层 doc_hit@10 ≥ 0.95、chk_hit@10 不退;段长中位落在 200~400 token;平台形态测试集(§9)六种形态的段长分布、父段数、没有「只剩标题」的段。
- **B-200 回归一起修(10-21 用户拍板不热修)**:`_section_budget` 在「文档名 + 标题路径」超过上限 1/4 时从最外层丢标题,而标题文字只在标题路径里 → 文字彻底消失(真实问法集 3 道题答案整个丢失、4 处锚文本失效,占 MRR 损失 46%)。上限降到 400 后这道闸更紧(超 100 token 就丢),不会顺带消失,K-3 必须显式改成标题文字一个字都不丢;另两处 B-200 副作用(已切好的块被打包再切一刀、放不下重叠就整段不带)随本条「只切一次、重叠 0」消掉。验收:丢失的 3 题与 4 处锚文本全部恢复;真实问法集带重排 MRR@10 ≥ 0.846(B-199 之前的水平);加「每个标题的文字都出现在某一段里」测试 + 形态测试集超长标题样例。归因见 [知识库优化总计划](../plans/2026-10-21-kb-optimization-master-plan.md) §3。

**K-4 结果按文档分组、带文档信息、定预算** —— M

- 改什么:
  - 条数与预算:默认返回 **10 个父段**,**总预算 8,000 token**(库级可调),上限 20 个父段;**每文档最多 3 个父段**(Onyx `MAX_CHUNKS_FOR_RELEVANCE = 3` 是「选段预算每节只数 3 块」,用意相同:防标题好的文档淹没别家);预算先到先截。
  - 每条带:文档短编号 `[doc 1234]`、文档标题、**上传时间**(`created_at`)、`metadata` 里有就显示的 `version / effective_date / source`(平台约定这几个键名;K-15 以后自动抽)、来源类型、标题路径、「第 n / N 父段」。同文档的多条共用一个编号(Onyx 同款)。
  - 格式仍是紧凑文本(不是 JSON,省 token;Anthropic 建议去掉 uuid 一类低信号字段、用短编号):

    ```
    [doc 1234] 产品说明书：益家人.docx · 上传 2026-03-12 · 版本 v3 · 第 2/9 段
    § 保存方式 > 常温
    <父段正文>
    ```
  - 结尾提示:「共 N 个父段相关,显示 10 个;要读更多用 knowledge_read 或换词再搜」。只在**真的裁剪了**时出现(WeKnora `RetrievalTruncation` 同款),没裁剪不加任何免责声明;「候选 N 条但都没过重排分数下限」单独一句(现在只打 `rerank_degraded` 标记,模型看不到);预算截断按分数从低往高丢,第 1 条永远保留。
  - 这些字段也拼进送重排的文本(见 K-14)。
  - 结构化命中明细(父段 / 子段 id、四个分数、是否降级)放进 `ToolResult.meta`,给轨迹面板用(§6),不给模型。
  - **开工前先核(批 0 带出,10-21)**:工具结果超过 12,000 字符会被外置到工作区、上下文里只留开头 2,000 + 结尾 1,000 字符的预览(`tools/overflow.py` `EXTERNALIZE_MIN_CHARS` / `PREVIEW_HEAD_CHARS` / `PREVIEW_TAIL_CHARS`,受 `tool_output_budget_enabled` 总开关控制,默认开)。8,000 token 预算在中文约 1 万字、英文可达 3 万字符 → 会撞上这道闸,模型反而只看到 3,000 字。K-4 要么让 `knowledge_search` 的预算按字符落在闸下,要么给它豁免;先实测哪些智能体会走到外置(有无工作区写入)再定,`context_hit` 也按同样配置量。
- 治哪类:找全、相似文档混淆、项目关联;重放里「排得进前 20、但只给 5 条」的 37 对(18%),按文档分组取前 10 篇可显示其中 20 对(§2.4)。
- 依据(v2 核实):Anthropic Contextual Retrieval「We tried delivering 5, 10, and 20 chunks, and found using 20 to be the most performant」;Onyx 50 命中 → 25 块 × 512 token 预算;FastGPT 引用上限 5,000 token;Databricks(2411.03538)多数模型检索内容从 2k 加到 16~32k token 仍在涨;HiChunk 预算 2k→4k 越大越好;Cohere 建议结构化字段(Title / Date / Content)拼进文本;Glean / Azure 返回都带 title / url / 时间。**反面证据**:《Lost in the Noise》(2601.07226)只加 1 篇随机文档 DeepSeek-R1 72.4 → 54.1,hard negative −34%,「as models use more reasoning tokens, they increasingly misinterpret noise」;《More Documents, Same Length》(EMNLP 2025)定长下文档数 2→20 多数模型 F1 降 5~10%(Qwen 系不受影响)——所以**加条数必须用真实答题模型在智能体层验**,重排分数下限(B-175 PR3 已有)是挡无关文档的闸。
- 厂商:与模型无关;答题模型不同对条数敏感度不同 → 判据按平台默认答题模型跑,再抽一个别家模型复核。
- 形态:Ⓐ~Ⓔ 成立;Ⓓ 表格父段大,预算可能只装 2~3 个 → 表格类父段上限单独更小(512 token)。
- 代价:每次结果 token 约 2~3 倍(5 × 小段 → 10 × 父段);用预算压住;每题 5 次检索约 40k token 上限。
- 判据:智能体层 60 题答对率 ≥ 现状、编造不升(默认答题模型 + 一个别家模型);华大「找全 / 跨文档」题文档覆盖率上升;单次结果 token ≤ 预算。

**K-5 查询侧「这是问题」标记(厂商中立)** —— M,不用重新入库

- 改什么:
  - 模型目录 `ModelEntry` 加嵌入能力字段:`asymmetry`(`none` / `param` / `prefix`)、`query_marker` / `document_marker`(`param` 时是参数名与枚举值,如 `text_type=query` / `input_type=search_query` / `task=retrieval.query` / `task_type=RETRIEVAL_QUERY`;`prefix` 时是只加在查询侧的前缀模板,如 `Instruct: {task}\nQuery: {query}` 或 bge-zh 的固定中文前缀)、`instruction`(自由指令文本,默认英文「Given a search query, retrieve relevant passages that answer the query」,只有 `prefix` 型和阿里 `instruct` 用得上)、`native_endpoint`(走厂商原生接口才能带参数的,如 DashScope)。
  - 嵌入客户端按字段行为:`param` → 查询带查询参数、入库带文档参数(入库侧是厂商默认值时不用重嵌入);`prefix` → 只给查询加前缀,文档不加;`none` → 什么都不做。DashScope 需要新增原生接口客户端(`/api/v1/services/embeddings/text-embedding/text-embedding`),OpenAI 兼容口不支持。**入库与查询必须用同一套标记**(只有 Voyage 承诺带 / 不带互相兼容),所以标记写进知识库的「嵌入模型指纹」里,换了就当换模型处理(重建索引,B-175 已有机制)。
  - 各厂商怎么提供(2026-10 逐家读原文):见 **表 5-A**(§5.5)。目录里现有三家里只有阿里有(要原生口);OpenAI、智谱没有 → `none`,行为不变。
  - 向量分数阈值:加标记后查询向量分布会变,库级 `score_threshold` 要在开发集上重标;同时记录「标记前后同一查询的相似度差」给运维看。
- 治哪类:A 类向量那一路。
- 依据(v2 核实):阿里云 text-embedding-v4 文档「当使用短文本去匹配长文本时,应区分 query 和 document」、`instruct`「仅在 text_type 为 query 时生效…约 1%–5%」(出处是 API 参考与 HF 模型卡,技术报告 2506.05176 没有消融);Qwen3-Embedding 查询格式 `{Instruction} {Query}`、「leaving the document unchanged」;Cohere / Jina / Voyage / Google 都把 query / document 作为必填或建议参数;OpenAI 没有。WeKnora 把这层抽象了(`EmbedInputType document|query` + 厂商声明 `InputTypeField / InputTypeValues`)但只有 NVIDIA 声明,阿里那家故意不声明,注释「text_type / instruct… both change the document vectors」——印证「标记进嵌入指纹、换了当换模型」。
- 厂商:核心就是厂商中立字段;目录里现有三家:openai(无)、glm embedding-3(无)、qwen v4(参数,要原生口)。
- 形态:与文档形态无关。
- 代价:几乎为 0(DashScope `instruct` 按查询条数计 token);一个新客户端;阈值重标一次。
- 判据:华大开发集召回层 mrr@10 / chk_hit@10 不退且向量路单独 mrr 上升;阈值重标后无答案拒答率不变、误拒不升。
- **10-21 开工前实测更正(DashScope `text-embedding-v4`,同一句中文查询,两个接口各调一遍,花费 < ¥0.001)**:① `text_type=query` / `document` / 不带,三者向量**逐位相同**(原生口、兼容口都是)—— v4 上 `text_type` 单独不起作用;② 兼容口请求体顶层带 `"instruct": "<任务说明>"` 会改变查询向量(与不带余弦 0.9185),且与原生口 `text_type=query` + `instruct` 完全相同;放进 `parameters` 里兼容口不认;③ 原生口 `text_type=document` + `instruct` 无效果 → 文档侧不变。**所以阿里 v4 = 「查询侧指令字段」,走现有兼容口即可,不用新增原生口客户端;文档侧不变 → 不进嵌入指纹、不用重新入库**;目录里 qwen 条目的 `text_type=query / document` 写错了,随 K-5 改。WeKnora 注释说两者都改变文档向量,与实测不符。

**K-6 搜索工具说明定稿** —— S

- 改什么:平台 `knowledge_search` 工具说明(英文,随工具走,没挂知识库的智能体看不到)在现有「只按段落明确写了的回答」之后加三条,措辞定稿:
  1. *Use several small, targeted searches rather than one broad one; if a result does not state the fact asked, search again in the documents' own terms — do not repeat the same or a very similar query.*
  2. *For questions that ask for all / every / how many, keep searching with different terms until a search returns no document you have not seen, then read each candidate (knowledge_read) and check it meets every condition in the question before counting it.*
  3. *Before concluding, verify each condition in the question against text you have actually read; conflicting documents are reported as conflicting, not resolved by guessing.*
  4. *Content already returned in full for this task does not need to be read again. A document list shows what exists but cannot prove a base lacks the answer — say you searched and found nothing only when a search actually ran.*(WeKnora `grounding_prompt.go` 同款)
  - 结果末尾的事实型提示(不是命令,B-163)相应加一句「还有 N 个相关父段没显示」。
- 治哪类:C 里漏条件 / 读错;B 里搜两次就停;找全题。
- 依据(v2 核实,口径更正):Onyx ERB 论文 Table 7 找全题(20 题)多轮探索(Bash Agent)**文档召回 59.0 > BM25 46.5,但正确率 35.0 < 40.0**——「搜得全」必须配「读到再下结论」,所以第 2 条把「换词再搜」和「逐篇核对」绑在一起;metor 博客「makes the agent read before it claims」;Onyx `tool_prompts.py`「try again with different tools or arguments. Do not repeat the same or very similar queries」;Anthropic 工具写作指南「making many small and targeted searches instead of a single, broad search」、多智能体研究「start with short, broad queries, then progressively narrow」;OpenAI GPT-4.1 指南「do NOT guess or make up an answer」;Azure agentic retrieval 一问平均拆 3 个子查询(官方数字;「相关性 +40%」只是厂商转述,不引)。WeKnora `config/prompt_templates/agent_system_prompt.yaml:44-52`、`internal/agent/grounding_prompt.go:14-87`:「Rephrase an unproductive query; for exact terms switch to mode=keyword rather than repeating」「Full content already returned for the task does not require another read」「directory profiles… cannot prove a base lacks the answer」「say you searched or found nothing only when a search tool actually ran」;它的失败措辞分三档(整体失败 = 不是「没有」的证据;部分库失败;候选 N 条全被重排拒),其测试对措辞做**正反断言**(空结果必须含「found N candidate chunks」、不得含「switching mode will not help」),§9 照做。
- 厂商 / 形态:与模型、文档形态无关。
- 代价:每题可能多搜一两次。
- 判据:智能体层 60 题编造数不升、「找全」类召回上升;每题平均检索次数增幅 ≤ 1。

### 5.2 稳定性(随第一梯队做)

**K-7 重排:上限按模型配置、限流排队、退避重试、单视图** —— M

- 改什么:
  - 模型目录加重排能力字段:`rerank_api`(`dashscope` / `cohere_style` / `vertex` / `bedrock` / `llm`)、`max_documents`、`max_doc_tokens`(注明单位:token 或字符——智谱、腾讯按字符)、`max_total_tokens`、`query_max_tokens`(查询单独校验,拆批救不了)、`max_concurrency`、`supports_instruction`、`score_scale`(0~1 归一 / sigmoid / 原始 logit / 请求内相对;logit 统一过 sigmoid)、`rpm`(WeKnora `RerankSettings` 字段集同构,它支持 7 种重排协议);代码里的常量 `_MAX_RERANK_CANDIDATES = 100`、`RERANK_DOC_MAX_CHARS = 3000` 改成按模型取值(qwen3-vl-rerank 100 条 / 8,000 token;qwen3-rerank 500 条 / 4,000;qwen3.7-text-rerank 500 条 / 30,000;Cohere 1,000 / 4,096;腾讯 60 条 / 总 2,000 字符……全表见 5-B)。候选池 = min(目录上限, 100 或 K-14 定的值),总 token 超限先按长度截单条再减条数,不让厂商返回 400。
  - **分数阈值绑定模型**:库级 `rerank_score_threshold` 记录它是在哪个重排模型下标定的;重排模型换了就清零并在设置页提示重标(各家分数语义不同:Cohere 按域校准、bce 建议 0.35、腾讯是负 logit、阿里请求内相对)。
  - **10-22 拍板:阶梯降级不做**(用户「不做降级」)。全部低于下限就返回「都没过」,与 K-7b 前一致;精确编号不再单独处理:按线过滤与改动前一致(编号保留规则评审后撤回 —— 参考阈值下零收益、对常见指标名与变体编号误放)。理由:业界多数直接按线过滤;我们的使用者是智能体,能重搜、能说「资料没写」,降级会在资料确实没写时塞回低分段落;WeKnora 默认线 0.3 而 ×0.7 只在 > 0.3 时触发,默认配置下本就不生效。下面一条里阶梯部分保留为当时的设计记录;结果档位(K-7a)与自动分批(K-7c)照做。
  - **阈值阶梯降级(10-22 不做,见上)+ 结果档位**(WeKnora `internal/reranking/rerank.go` 同款,v2.1):全部低于下限时先按 max(0.7 × 下限, 0.3) 再试,仍空则保留最高分 1 条(≥ 0.15),再空才返回「候选 N 条都没过」;精确编号类查询只排序不过滤(重排模型给裸编号打分偏低);`rerank_degraded` 一位扩成 `rerank_outcome` 枚举(`ok / threshold_degraded / fallback_top1 / all_below_threshold / model_error / model_unavailable / no_model`)写进 `ToolResult.meta`,轨迹面板与检索测试都显示。候选超过模型单次上限时自动分批(并发 ≤ 4)再合并排序,不是截掉。
  - 纯文本知识库用纯文本重排模型(目录加 `qwen3-rerank` / `qwen3.7-text-rerank`;其它厂商的在表 5-B);图片分段(B-175 P11)才用多模态重排。
  - 客户端**按秒排队**(阿里按 RPS = RPM / 60 也限,「Request rate increased too quickly」会触发 BurstRate),复用 B-44 的全局厂商 RPM 桶;429 / 5xx / 超时按**指数退避 0.5 s × 2ⁿ 带抖动、上限 8 s、最多 3 次**(外部没有标准数字,这是推断值),仍失败才退回融合序并保留 `rerank_degraded`。
  - **单视图**(K-14 的结论先用):每次检索调一次重排,不再调两次——`qwen3-vl-rerank` 600 RPM 的等效容量翻倍。
  - 通用「Cohere 风格」重排客户端(`POST …/rerank`,`{model, query, documents, top_n}` + `return_documents`):Cohere(`/v2/rerank`)、Jina、Voyage(`top_k`)、SiliconFlow、智谱、千帆、vLLM / TEI / Xinference 自部署都是这个形状,参数名的小差异(`top_n` / `top_k`、分数字段名)用适配表;Google(`topN`、`title` 独立字段)、Bedrock(`numberOfResults`)各一个薄适配;火山、腾讯没有 `top_n`、回一一对应的分数,也走适配。没有重排接口的厂商(OpenAI)仍用 `llm` 档。
- 依据(v2 核实,阿里 2026-10-12 / 10-19 文档):限流 `qwen3-rerank` 5,400 RPM、`qwen3-vl-rerank` 600、`gte-rerank-v2` 5,040、`qwen3.7-text-rerank` 3,000;价格前三 ¥0.5 / 百万 token;单次总 token 上限 120,000,超限 HTTP 400 不截断;`instruct` 参数三个 qwen 重排都支持(建议英文);**没有批量 / 异步重排接口**;错误码文档对 429 建议「匀速调度、指数退避或请求队列缓冲」。评测第二轮实际出现 4 次 429、3 题受影响。
- 厂商:核心就是厂商中立字段;`llm` 档(聊天模型排序)保留给没有重排接口的厂商。
- 形态:无关。

**K-8 嵌入报错** —— S

- 改:入库时 `InternalError` 类 400 重试一次;检索时嵌入失败改走纯关键词并标记 `embedding_degraded`,记日志,不再无声失败;嵌入客户端的「每批条数」「单条上限」也按模型目录取(qwen v4 10 条 / 8,192 token,qwen3.7 20 条,openai 2,048 条)。
- 依据:评测预热时实际遇到 400 `InternalError.Algo.Embedding_pipeline_Error`;Onyx 嵌入带退避重试。

### 5.3 第二梯队:等重放 / 对比结果再定

**K-9 关键词路补成完整 BM25(两条路,对比试验)** —— 路 A:S + 一次 RDS 大版本升级;路 B:M

- 改什么(二选一,按 ⑦ 拍板):
  - **路 A:RDS 升 PG 17 + `pg_textsearch`**(v2.1 新增)。阿里云 RDS 插件表(2026-09-23)`pg_textsearch` 1.3.1 列在 PG 18 / 17,PG 16 及以下「无」;10-21 实测测试库 PG 16.13 可用扩展 146 个里没有它。装上后:`CREATE INDEX … USING bm25 ((标题 ∥ 正文)) WITH (text_config = '<jieba / zhparser 配置>', k1 = 1.2, b = 0.75)`,查询 `ORDER BY 列 <@> to_bm25query(查询, 索引名)`(分数是负数,越小越相关);分词复用 PostgreSQL 的 text search configuration,RDS 上 `pg_jieba` / `zhparser` 都可装。要想清楚两点:① 它的 idf 按**整个索引**算(所有租户所有库),我们现在的计分按**本库**算 idf,多租户大库下有差别,开发集验;② 布尔过滤 + BM25 排序「还没优化成单次索引扫描」,过滤出的行少时最好——我们每次查询都带 `tenant_id + kb_id`,正是这种,但要实测。前置(⑦ 10-21 拍板,B-207):测试库先升 17,生产等 B-206 全部做完、与知识库版本一起升;升级手册 `docs/runbooks/rds-pg-major-upgrade.md`(阿里云四种方式:本地升级 / 蓝绿部署 / 零停机 / DTS 迁移,测试选本地升级)。**过渡期主干必须同时能在 16 和 17 上跑,所以路 A 要做成可选能力**:迁移里插件可用才 `CREATE EXTENSION`,检索时检测不到插件就走路 B / 现有计分(私有化客户的库也不一定装得了,本来就该可选)。`pg_textsearch` 要基于一列文本建索引,而我们现在入库时用 jieba 分词后只存了 tsvector、没存分好词的文本 → 新增一列分词文本(`text_config = 'simple'`),或改用库内 `pg_jieba` 配置;新增列在批 2 重新入库时顺手写入,晚于批 2 则单独补跑一遍(只在应用里分词,不调模型、不重切块)。BM25 值不值得做,批 0 里直接在升级后的测试库上量。
  - **路 B:留在 PG 16,在现有 tsvector 上自算 BM25**:词频 tf = tsvector 里该词的位置数(Postgres 每词最多存 256 个位置,够用);文档长度 dl = 入库时存的子段 token 数(新列,**不要用 `length(tsvector)`**,那是去重词数);每库一张词表 `knowledge_term_stats(kb_id, lexeme, df)` + 库级 `avgdl`,在 `replace_chunks` / 删除时增量更新(**不要查询时跑 `ts_stat`**,那是全表扫描);分数 = Σ idf · tf(k1+1) / (tf + k1(1 − b + b·dl/avgdl)),k1 = 1.2、b = 0.75;现有的编号 / 整句命中加分保留。
  - 零成本对照组:`ts_rank_cd` 开长度归一(normalization = 1)。两条路都与现有计分在同一开发集上三方对比。
- 依据(v2 核实):Onyx ERB 论文 Table 6(500 题)BM25 正确率 68.8 vs 单路向量 51.4(但对照是 top-10 无重排的单路向量,**论文没测混合 + 重排**,只能推出「关键词路值得做好」);《BM25 Wins at Scale》51 万篇全量 BM25 50.5 vs 稠密 29.9(稠密是 0.6B 小模型);BGE-M3 论文 Table 11 分词器对 BM25 影响 ≥ 10 点 → 保留 jieba。**RDS PG 16 装不了 BM25 扩展,PG 17 / 18 可以**(10-21 实测测试库:有 pg_jieba / zhparser / rum / pg_bigm / pase 可装,pgvector 0.8.0 与 pg_trgm 已装;阿里云 RDS 插件表 2026-09-23 版:`pg_textsearch`(TigerData,PostgreSQL 许可,1.0 于 2026-03 发布,只支持 PG 17 / 18)1.3.1 列在 PG 18 / 17;ParadeDB `pg_search`(AGPL)、VectorChord-BM25(AGPL / ELv2)各版本都没有;WeKnora 走的是自带 ParadeDB 镜像的 `pg_search`,托管 RDS 照搬不了);博客实测 SciFact nDCG@10 ts_rank 0.07 / 自算 BM25 0.69 / pgvector 0.66 / 混合 0.70。
- 厂商:与模型无关(纯数据库侧)。
- 形态:Ⓓ 表格行子段很短、长度归一会抬高它们——判据里单看表格题不退。
- 判据:华大开发集 keyword-only 的 mrr@10 / 零命中率优于现有计分;混合 + 重排不退;表格题不退。

**K-10 多路查询(原问题一路必留)** —— M

- 改什么:
  - 三路:**原问题(权重 1.0,锚)** + **换说法一路**(改写式,保留所有专有名词、编号、数字;权重 1.0)+ **关键词一路**(照 Onyx 的 prompt:「purely keywords… as few keywords as necessary… if there are no useful expansions, simply return the original query」;权重 0.7);小模型一次调用同时产出两路,与原问题那一路并行跑;同一轮对话里智能体重复检索时跳过扩写。
  - 融合 A/B:现有等权 RRF(k = 60)vs **归一化(min-max)加权和**,α 取 0.3 / 0.5 两档。
  - 不做 HyDE(见 §7)。
  - 换说法一路的 prompt 借 WeKnora `config/prompt_templates/rewrite.yaml` 两句:「MUST preserve specific entities, keywords」「Do NOT generate meta-instructions like 请在知识库中查找」;关键词一路再加一个零成本对照:本地规则(去疑问词、引号短语、按分隔符切段)不调模型。**反例**:WeKnora 改写后用改写结果替换原问题、原问题不留(`search.go:376`)——正是 K-10 要避免的。
- 依据(v2 核实,**降预期**):Onyx `constants.py` 权重 `LLM_SEMANTIC_QUERY_WEIGHT 1.3 / KEYWORD 1.0 / NON_CUSTOM 0.7 / ORIGINAL 0.5`,EE 搜索页改成原问题 2.0 / 关键词 1.0;《Searching for Best Practices in RAG》(2407.01219)Table 6 查询改写 −2.3 / 分解 −4.1 nDCG@10,HyDE +5.2 但延迟 2.6 s → 7.2 s;《When do Generative Query and Document Expansions Fail?》(2309.08541)扩写**对弱检索器有益、对强检索器有害**(BM25 −4.0、E5 −3.4、MonoT5 −2.8);2505.12694:生成式扩写在「模型不懂该领域 / 问题歧义」时显著变差,**错的专有名词伤检索**,改写式(GaQR)「never fell below the baseline」。融合:《An Analysis of Fusion Functions》(2210.11934)凸组合优于 RRF 且 RRF 对 k 敏感;Weaviate 1.24 起默认 relativeScoreFusion,内部基准召回 +6%;Elasticsearch 仍推荐 RRF(rank_constant 60)。
- 厂商:扩写用平台默认小模型(任一聊天模型);融合与模型无关。
- 形态:Ⓑ Ⓔ(口语、内部术语多)最容易被扩写带偏 → 判据按题看。
- 代价:每次检索多一次小模型调用(0.5~2 s,并行);token 可忽略。
- 判据:华大真实问法集 172 题 mrr@10 上升;**逐题看「原来对、现在错」的数 ≤ 3**;两种融合择优;不达标只留原问题 + 换说法两路或整条不上。

**K-11 入库时给每段写一句上下文(上下文化切块)** —— L,要重新入库,**默认关、开发集验过再开**

- 改什么:每个子段前加 50~100 token 的「这段在整篇里讲什么」说明,存新列 `context_header`(B-175 设计过,0165 没建),拼进嵌入 / 关键词文本,**不给模型看**;生成方式:**每篇文档一次调用**(文档 ≤ 32k token 一次,更长按父段分批),让模型对该篇所有子段各输出一句,成本 ≈ 文档 token + 75 × 段数(逐段带全文调用在没有提示缓存的厂商上是 文档 token × 段数,贵一个数量级);prompt 照 Anthropic 原文翻译。按文档内容哈希缓存。
- 依据(v2 核实):Anthropic(2024-09)top-20 检索失败率 5.7% → 3.7%(−35%)→ 2.9%(+BM25,−49%)→ 1.9%(+重排,−67%),成本 $1.02 / 百万文档 token(有提示缓存);第三方复现全正向但幅度差很大:Denser 同数据集 BM25 R@20 70.5 → 89.3;T2-RAGBench(2604.01733,金融文本 + 表格 2.3 万题)稠密只 **+2.8 pp**、混合 +2.2 pp,同文重排 +12.1 pp 最大;LlamaIndex 单篇 0.857 → 0.929 并注「much depends on the queries, chunk size…」;Onyx、RAGFlow(Augmented context)都有此功能。
- 厂商:任一聊天模型;与嵌入 / 重排厂商无关。
- 形态:Ⓐ Ⓑ 收益最大(段落脱离上下文不知所指);Ⓒ 短文档几乎没收益(整篇就是父段);Ⓓ 表格行「这是 X 表第 n 行」有用。
- 代价:每千段约一次 8k token 的调用(按 qwen-flash 级价格约 ¥2 / 千段;具体开工前按平台默认模型报价);索引文本变长。
- 判据:华大开发集召回层 chk_hit@10 / mrr 上升;按 ¥ / 提升点与 K-12 比。

**K-12 每段配几个「可能的问题」** —— L,要重新入库,**默认关、开发集验过再开**

- 改什么:每个子段生成 ≤ 3 个用户可能的问法,存成 `chunk_type = 'question'` 的行(`parent_chunk_id` 指向子段,再经子段折到父段),只进索引不进返回;召回时折叠回子段再折叠到父段;prompt 照 RAGFlow(「propose the top N important questions… no overlapping meanings… one per line」,temperature 0.2)。
- 依据(v2 核实,**口径更正**):Doc2Query--(2301.03266)MS MARCO 段落检索 BM25 RR@10 0.185 → Doc2Query(每段 80 条)0.279(+51%)→ 过滤留 30% 后 0.323(再 +16%,索引 −33%)——v1 的「+16% / −33%」是过滤前后对比,不是相对 BM25;且那是短段落 + 几十条问题的设定。RAGFlow `auto_questions` **默认 0(关)**,ES 字段权重 `question_tks^20` vs 正文 ^2;FastGPT「多向量映射」一段多组向量任一命中即召回。
- 实现细节(WeKnora 同款,v2.1):prompt 要点「生成这段**最能回答**的问题;每题自包含、无代词;不出一个词就能答的题」,带前后段与文档名做上下文,温度 0.7;问题记录所属子段的 `revision`,子段被编辑后标「过期」并可重生成;存法二选一——`chunk_type = 'question'` 行(B-175 设计)或「同一子段多条索引行」(WeKnora 生成问题与 FAQ 相似问同构:索引行 `source_id = 子段-序号`,命中天然折回子段)。**建议统一成「一个分段多条索引行」机制**,K-12、FAQ 相似问、FAQ 反例问题(命中即剔除)共用。
- 厂商 / 形态:同 K-11;Ⓓ 表格行生成问题特别有效(「X 的价格是多少」)。
- 代价:每段一次短输出;索引行 ×(1 + 3);存储。
- 判据:同 K-11;**K-11 与 K-12 不二选一**——各自单独 A/B 再同开一组(RAGFlow 两者并列可同开),按 ¥ / 提升点择优,可以都上或都不上。

**K-13 「列出所有相关文档」工具** —— M

- 改什么:`knowledge_list_documents(keywords, limit, cursor)`:在现有 tsvector 上按文档聚合,穷举列出命中文档(短编号、标题、上传时间、命中子段数),分页;有 K-15 后支持按元数据过滤。工具说明把它和 K-6 第 2 条、K-2 绑定:列出 → 逐篇读 → 核对。
- 依据(v2 核实):Onyx 论文找全题文档召回 59.0 vs 46.5 **但正确率反降**(所以必须配逐篇核对);《BM25 Wins at Scale》Table 4(150 题全量)「智能体 + 排序检索 + 读块」整套 69.4 vs 单次 BM25 54.8、文档召回 72.4 vs 65.6,找全题文件系统智能体 56 vs BM25 27——支持的是 **K-2 + K-13 组合**,引用时不拆开;原话「Iteration helps most after global ranking, not in place of it」。
- 厂商:与模型无关。形态:全部成立。
- 判据:华大「跨文档 / 找全」题(match: all)覆盖率上升;编造不升。

**K-14 重排环节调参** —— S~M

- 改什么,三项分别在开发集对比:
  - ① **单视图**:送重排的文本改成一串结构化文本(Cohere 建议的 YAML 风格,键序固定):`title: <文档标题>\nsection: <标题路径>\ndate: <有就带>\ncontent: <子段正文>`,支持 `instruct` 的模型加任务说明(英文:「Given a question, judge whether the passage content itself states the answer; the title only identifies the document」),一次打分。对照组 = 现有双视图 0.7 / 0.3;再加一组**正文清洗**(送重排前去掉 Markdown 标记 / URL / 表格线 / HTML,WeKnora `internal/reranking/passage.go` 同款,它的单串 = 标题 + 面包屑 + 清洗正文 + 生成问题)。**必查** B-175 PR2b 当时引入双视图的那个案例(专利证书文件名含菌株号、所有段都「看起来相关」:精确编号 0.958 → 0.917 那组题)。
  - ② 重排候选池从合并后前 50 加到前 100(按模型 `max_documents` 封顶,总 token 按 `max_total_tokens` 卡)。
  - ③ 若双视图保留:两个视图都在整池上打分,或把 `_TITLED_VIEW_WEIGHT` 0.7 降到 0.3~0.5。
- 依据(v2 核实):重放(§2.4):合并后掉出前 50 的 21 对全部在前 100 内;被带标题视图截掉 13 对(含重排池前 16 的 3 对);诊断里标题相似的干扰文档占先的题(0384 / 0400 / 0354 / 0447 / 0082 / 0404)。**双视图加权没有任何外部先例**;Cohere 最佳实践把 Title / Date / Content 以 YAML 拼成一个文档一次打分;Onyx 把 title / metadata 与正文一起送模型选段;Qwen3-Reranker 的训练输入就是「指令 + 查询 + 文档」单串。
- 厂商:①③ 对 `llm` 档(聊天模型排序)同样适用;② 按模型上限。形态:无关。
- 代价:① 调用减半;② 每次重排 token 翻倍(换纯文本模型后 ¥0.5 / 百万 token,可忽略)。
- 判据:华大主集带重排 mrr@10 ≥ 0.914、精确编号 ≥ 0.958(PR2b 前的水平)、真实问法集 ≥ 0.885;ERB 复测时 §2.4 的 34 对回收数。

**第二梯队取舍(重放后定)**:
1. **K-14 先做**(批 1 就带 ①②):有重放数据直接支撑、改动小、顺手减半重排调用;
2. **K-5 已在第一梯队**,先上线再看召回失败还剩多少;
3. **K-10 与 K-11 / K-12 在开发集上对比**:召回失败 57 对分不出是「问法」还是「索引里缺上下文」,三条路都试,按「提升 / 代价」择优;K-11 / K-12 要重新入库,与批 2 的 K-3 同批入库省一次;
4. **K-13 随「找全」类题的对比结果做**,与 K-2、K-6 绑定验收;
5. **K-9 最后**:重放里关键词路单独贡献的正确文档有(已显示 75 对里 12 对只来自关键词路),但没有证据说明现在的计分方式是瓶颈,只做对比试验。

### 5.4 第三梯队:后做

**K-15 清单信息提前抽成表(找全的根治方案)** —— L

- 改:入库时按知识库字段配置(`knowledge_base.metadata_schema` 已有)抽实体 / 金额 / 日期 / 项目 / 负责人,写进文档 `metadata`,支持 K-13 过滤与计数;K-4 显示、K-13 过滤先作为便宜版。
- 依据(v2 核实):ERB 榜 Flexor(91.48,第 2)博客原话「Extract the tabular truth. Entities, amounts, dates, parties pulled into structured tables… Enumeration and aggregation stop being search problems and become queries, with completeness by construction」,且「Both patterns belong in the same platform, chosen per question」(点查走检索、枚举聚合走查表);TAG 论文(2408.14717)「常规方法 ≤ 20%」的语境是**结构化数据库题**(RAG 基线 0%、Text2SQL 17%),不能直接套到文档库;RAGFlow 元数据生成、Dify 自动元数据过滤、Onyx 由模型推断时间 / 来源过滤。
- 厂商:任一聊天模型抽取。形态:Ⓐ Ⓒ Ⓓ 有稳定字段的最适合;Ⓑ Ⓔ 字段难定。
- **便宜版先行(WeKnora 同款,v2.1)**:固定 schema 的文档画像——`summary / gist / topics(3~5)/ doc_type / typical_question`,每篇入库时一次调用、只取开头 8k 字采样;不用租户定字段就能给 K-4 显示、K-13 过滤、§6 库信息。租户自定义字段抽取 WeKnora 也没做。
- 通用:便宜版高(固定字段,不用租户配置);租户自定义字段那一步中高——**每个租户要定自己的字段**,这是代价(待拍板 ④)。

**K-16 稀疏向量** —— L,要重新嵌入,**K-9 之后再评估**

- 依据(v2 数字更正):BGE-M3 论文 Table 3 MLDR 中文长文档 nDCG@10:BM25 34.6 / 稠密 26.0 / 稀疏 40.5 / 稠密 + 稀疏 42.0;Table 11:Lucene 分词的 BM25 均值 64.1 **高于** M3 稀疏 62.2;MIRACL 中文稀疏单独只 35.4(稠密 74.6)。即稀疏向量的收益主要是「替代 BM25」而非叠加,做完 K-9 后增量可能很小。可行性:pgvector 0.7 起有 `sparsevec`(HNSW 只支持 ≤ 1,000 个非零元素),RDS 是 0.8.0;厂商支持见表 5-A——**API 侧只有阿里原生口返回稀疏向量**(`dense&sparse`,新模型 qwen3.7-text-embedding 自称「类 SPLADE」),OpenAI / Cohere / Jina / Voyage / Google / 智谱 / 火山 / 千帆 / 混元都没有;自部署 bge-m3(`return_sparse`)、TEI `/embed_sparse` 才有。
- 厂商:强依赖厂商与部署形态 → 作为能力字段 `supports_sparse`,没有就不做,混合检索的词法那一路靠 K-9 的 BM25。

**K-17 大库性能体检** —— S~M,某租户超约 10 万段时触发

- 改:`EXPLAIN (ANALYZE, BUFFERS)` 查执行计划;批量入库后再建索引、给足 `maintenance_work_mem`;半精度表达式索引减半体积;按租户分区(pgvector 文档:多租户共用近似索引会互相影响召回与速度);K-9 的词表增量维护在大库上的写放大也在这里量。

### 5.5 厂商能力表(v2 新增;K-5 / K-7 / K-8 / K-16 的数据来源)

按各厂商 2026-10 官方文档逐家读原文填写(出处见 §11「厂商接口」);「—」= 文档没有此项。表里的列就是模型目录要加的能力字段。

**表 5-A 嵌入模型**(字段:`asymmetry: none | param | prefix`、`query_marker / document_marker`、`instruction`、`supports_sparse`、`dimensions`、`max_batch`、`max_input_tokens`、`rpm`)

| 模型 | 接口 | 查询 / 文档不对称标记 | 指令 | 稀疏 | 维度可选 | 每批条数 / 单条 token / 限流 | 中文 |
|---|---|---|---|---|---|---|---|
| OpenAI text-embedding-3(Azure 同) | OpenAI `/v1/embeddings` | **无** | 无 | 无 | `dimensions` | 2,048 条 / 8,192 / 每请求总 300k token;Build 档 5,000 RPM | 多语言,无中文专述 |
| 阿里云 text-embedding-v4 / qwen3.7-text-embedding | 兼容口(无标记)**或原生口** | `text_type=query\|document`(原生口) | `instruct`(仅 query,英文,+1~5%) | `output_type=dense&sparse`(仅原生口) | `dimension` 64~2,048 | 10 条(3.7 为 20)/ 8,192 / 1,800 RPM | 是 |
| Cohere embed-v4 / v3 | 私有 `/v2/embed` | `input_type=search_query\|search_document`(**必填**) | 无 | 无 | v4 256~1,536 | 96 条 / v4 128k、v3 512 / 2,000 条每分钟 | 100+ 语含中文 |
| Jina v3 / v4 | 私有(请求形状同 OpenAI) | `task=retrieval.query\|retrieval.passage` | 无 | 无 | 1~2,048 | — / 8,192(v4 32k)/ 100~5,000 RPM | 含中文 |
| Voyage 3 / 3.5 / 4 系 | 私有 `/v1/embeddings` | `input_type=query\|document`(官方公开等价前缀,带 / 不带互相兼容) | 无 | 无 | 256~2,048 | 1,000 条 / 32k 上下文 / 总 120k~1M;2,000 RPM | 多语言 |
| Google Vertex text-embedding-005 / gemini-embedding | 私有 `:predict` / `embedContent` | `task_type=RETRIEVAL_QUERY\|RETRIEVAL_DOCUMENT`(+ `title`);**gemini-embedding-2 改为 prompt 里写指令** | gemini-2 文本指令 | 无 | `outputDimensionality` | 250 条 / 2,048(gemini-2 8,192)/ 总 20k | 001 支持中文;005 仅英语 |
| AWS Bedrock Titan v2 | `InvokeModel` | **无** | 无 | 无 | 256 / 512 / 1,024 | 1 条 / 8,192 | 英语优化 |
| AWS Bedrock Cohere | `InvokeModel` | 同 Cohere `input_type` | 无 | 无 | 同 Cohere | 96 条 | 同 Cohere |
| 智谱 embedding-3 | OpenAI 形状 | **无** | 无 | 无 | 256~2,048 | 64 条 / 3,072 / ¥0.5 百万 | 中文优化 |
| 火山 doubao-embedding | OpenAI 形状 | **无** | 无 | 无(库内 VikingDB 才有) | 2,048,可降 1,024 | 建议 ≤ 4 条或总 ≤ 4,096 token / 15,000 RPM | 中英 |
| 百度千帆(bge-large-zh / tao-8k / Qwen3-Embedding) | OpenAI 形状 | **无** | 无 | 无 | 无 | 16 条(tao-8k 1 条)/ 512~8,192 | bge-large-zh 中文 |
| 腾讯 hunyuan-embedding | 云 API 或 OpenAI 兼容 | **无** | 无 | 无 | 固定 1,024 | 50 条 / 1,024(超长截断)/ 5 次每秒 | 中文 |
| MiniMax embo-01 | 私有 | `type=query\|db`(**必填**) | 无 | 无 | 固定 1,536 | — / 4,096 | 中文 |
| SiliconFlow 托管 bge-m3 / Qwen3-Embedding | OpenAI `/v1/embeddings` | **无**(指令要自己拼进文本) | 无 | 无 | Qwen3 64~4,096 | — / 8,192~32k / 2,000~10,000 RPM | 模型自身 |
| 自部署 bge-m3 | vLLM / TEI / Xinference | **无需**(模型卡明说) | 无 | TEI `/embed_sparse`、原生 `return_sparse` | 固定 1,024 | 8,192 | 100+ 语 |
| 自部署 bge-large-zh-v1.5 | 同上 | **前缀**(query 侧固定中文「为这个句子生成表示以用于检索相关文章:」) | 固定 | 无 | 固定 1,024 | 512 | 中文 |
| 自部署 Qwen3-Embedding / multilingual-e5-instruct | 同上 | **前缀**(query 侧 `Instruct: {task}\nQuery: {query}`,文档不加) | 自由指令(英文) | 无 | Qwen3 32~4,096 | 32k / 512 | 100+ 语 |
| 自部署 nomic-embed-text-v1.5 | 同上 | **前缀**(`search_query: ` / `search_document: `,必须) | 固定 | 无 | MRL | 8,192 | 英文 |

读法:不对称标记只有三种实现——**枚举参数**(Cohere / Voyage / Jina / Google / MiniMax / 阿里原生)、**只加在查询侧的前缀 / 指令**(bge-zh / nomic / E5 / Qwen3 自部署、gemini-embedding-2)、**无**(OpenAI / Azure / 智谱 / 火山 / 千帆 / 混元 / SiliconFlow 托管 / Titan / bge-m3)。所以 K-5 的字段定为 `asymmetry: none | param | prefix` + `query_marker` / `document_marker`(param 时是枚举值,prefix 时是前缀模板)+ 可选 `instruction`(默认英文);入库与查询必须用同一套(只有 Voyage 承诺带 / 不带互相兼容)。**稀疏向量**:API 侧只有阿里原生口返回,其余厂商都没有;自部署才有 → K-16 强依赖部署形态。自部署三件套的差异:vLLM 同时提供 `/v1/embeddings`(OpenAI)、`/v2/embed`(Cohere)、`/rerank`(Jina / Cohere 兼容);TEI 用 `prompt_name` 选 query / document 前缀;Xinference 只有 `/v1/embeddings` 带 `dimensions`——不对称标记在自部署下 = 平台自己拼前缀。

**表 5-B 重排模型**(字段:`rerank_api: dashscope | cohere_style | vertex | bedrock | llm`、`max_documents`、`max_doc_tokens`(注明单位)、`max_total_tokens`、`query_max_tokens`、`supports_instruction`、`score_scale`、`rpm`)

| 模型 | 接口 | 条数 / 单条 / 总上限 | top_n / 回传原文 / 指令 | 分数语义与阈值说法 | 限流 / 价格 | 标题等字段 | 中文 |
|---|---|---|---|---|---|---|---|
| 阿里云 qwen3-rerank / qwen3-vl-rerank / qwen3.7-text-rerank / gte-rerank-v2 | DashScope 原生 | 500 / 4,000 / 120k;文本 100 / 8,000 / 120k;500 / 30,000 / —;— / — / 30k | `top_n`、`return_documents`、`instruct`(三个 qwen 支持,英文) | 请求内相对分,**不可跨请求比较** | 5,400 / 600 / 3,000 / 5,040 RPM;¥0.5(gte 0.8)百万 token | 拼进正文 | 是 |
| Cohere rerank-v3.5 / v4 / v5 | `/v2/rerank` | 10,000(推荐 ≤ 1,000)/ 默认 4,096 自动分块取最高 / v4+ 32k 上下文 | `top_n`;只回 index + score;**明示不支持指令** | [0, 1] 但「query dependent」,按域用 30~50 个代表性查询校准阈值 | 生产 1,000 RPM | **YAML 字符串拼字段,键序重要** | 100+ 语 |
| Jina reranker v2 / v3 | `/v1/rerank` | — / v3 `max_doc_length` ≤ 8,192 / v3 单窗 64 篇 | `top_n`、`return_documents`(默认 true);无指令 | 「越高越相关」,无阈值说法 | 100~5,000 RPM | — | 100+ 语 |
| Voyage rerank-2 / 2.5 / 3 | `/v1/rerank` | 1,000 / 查询 + 单条 ≤ 16k~32k / **总 ≤ 600k** | `top_k`、`return_documents`、`truncation`;2.5 起可把指令前置到查询 | 无范围与阈值说法 | 2,000 RPM;$0.05 百万 | — | 多语言 |
| Google Vertex ranking API | `rankingConfigs:rank` | 1,000 / 1,024(超长截断)/ — | `topN`;无指令 | 0~1,无阈值说法 | — | **`title` 独立字段** | 25 语 |
| AWS Bedrock Rerank(amazon / cohere) | agent-runtime `Rerank` | 1,000 / — / 查询 ≤ 2,048 字符 | `numberOfResults`;无统一指令 | 无阈值说法 | — | 可直接收 JSON 文档 | 仅文本 |
| 智谱 rerank | `/api/paas/v4/rerank` | 128 / 4,096 **字符** / 查询 ≤ 4,096 字符 | `top_n`、`return_documents`、`return_raw_scores`;无指令 | float,未说明 | — | — | 是 |
| 火山 / 豆包 rerank | **方舟无独立重排 API**;VikingDB `/api/knowledge/service/rerank`(HMAC 签名) | 200 / — / — | 无 top_n(回一一对应的 scores);`rerank_instruction`(仅 doubao-seed-rerank) | 「能回答查询的概率」,可选阈值过滤 | — | **`title` 独立字段** | 70~100+ 语 |
| 百度千帆 bce-reranker / Qwen3-Reranker | `/v2/rerank` | — / bce 1k token;Qwen3 32k / — | 文档未写 | 示例 0.69 / 0.37,无阈值说法 | 1,800 RPM | — | 中英日韩 |
| 腾讯 LKE `RunRerank` | 云 API | 60 / — / **查询 + 文档总 ≤ 2,000 字符** | 无 | 原始分**可为负** | 20 次每秒 | — | 中文 |
| SiliconFlow bge-reranker-v2-m3 / Qwen3-Reranker | `/v1/rerank` | — / — / —(bge 可 `max_chunks_per_doc` 分块取最高) | `top_n`、`return_documents`;**无指令参数**(Qwen3 的指令能力在此口不可达) | 无阈值说法 | 2,000 RPM | — | 模型自身 |
| 自部署 bge-reranker-v2-m3 / bce-reranker / Qwen3-Reranker | vLLM `/rerank`(Jina / Cohere 兼容)、`/score`;TEI `/rerank`;Xinference `/v1/rerank` | 8,192 / 512(bce,超长分块)/ 32k(Qwen3) | vLLM `top_n` 默认全部;Qwen3 指令走 `instruction` 字段 + chat template | bge 原始 logit(`normalize` 后 sigmoid);**bce 建议阈值 0.35 / 0.4**;Qwen3 yes / no 概率 | — | — | 多语 / 中英日韩 / 100+ 语 |

读法:共性参数 = `query + documents[] + top_n`(名字有 `top_k` / `topN` / `numberOfResults` 之别;火山、腾讯没有)+ `return_documents`;上限差异极大(条数 60 ~ 10,000,单条 512 token ~ 32k,有的按字符),**所以 K-7 必须按模型配置而不是常量**;指令只有阿里、Voyage 2.5+、火山 seed、自部署 Qwen3 支持,Cohere 明示不支持 → K-14 的 `instruct` 是「有则用」;**分数阈值不可跨模型复用**(Cohere 要按域校准、bce 0.35、腾讯是负 logit、阿里请求内相对)→ 库级 `rerank_score_threshold` 绑定重排模型,换模型清零并提示重标。

### 5.6 WeKnora 深读借鉴清单(v2.1,2026-10-21)

本机 `/Users/mac/src/github/WeKnora`(腾讯开源,v0.8.3,HEAD e18fb23,2026-10-21;Go 后端 + Python docreader + Vue 前端),四路逐文件读;file:line 相对其根目录。B-175 时已记过它的产品层做法,这次只看检索质量相关的实现。

| 借 → 放哪 | WeKnora 的做法(file:line) | 怎么借 |
|---|---|---|
| K-2 | `read_document(id = doc://N \| chunk://N, offset, limit, query, regex, context ≤ 5)`(`internal/agent/tools/read_document.go:26-77`);永远先返回文档元数据头;超界报「valid offsets 0..N」 | `around` / `find` 两个参数、元数据头、超界提示 |
| K-6 | `agent_system_prompt.yaml:44-52`、`grounding_prompt.go:14-87`;失败三档措辞(`search_knowledge.go:262-268,438-466`);测试对措辞正反断言(`search_knowledge_test.go:97-113`) | 第 4 条说明;「候选 N 条都没过」写进结果;§9 测试写法 |
| K-7 / K-14 | 重排阈值阶梯(`internal/reranking/rerank.go:24-33,289-307`)+ `outcome` 枚举(`types/rerank.go:47-71`);`RerankSettings{MaxDocuments, MaxQueryChars, MaxDocumentChars, MaxRequestChars, MaxConcurrency, ScoreScale}`(`models/api/rerank_settings.go:49-58`)+ 7 种重排协议(`models/api/rerank.go:12-38`)+ 超限自动分批并发合并(`models/rerank/protocol.go:32-112`);送重排单串 = 标题 + 面包屑 + 清洗正文(`reranking/passage.go:63-187`) | 阶梯降级 + `rerank_outcome`;补 `max_concurrency / query_max_tokens`;单串对照 + 正文清洗对照 |
| K-4 | `RetrievalTruncation` 只在真裁剪时提示(`types/chat_manage.go:131-162`);模型视图按文档分组、元数据每文档一次、不给分数 / UUID(`modelcontext/model_output.go:569-659`);预算丢低分留首条(`tools/truncate.go`) | 结尾提示的触发条件;预算截断规则 |
| K-1 | 父行 `chunk_type = parent_text` 只存不索引、白名单可检索类型(`knowledgebase_search_results.go:383-388`);单子段 == 父段不建父行(`chunker/strategy.go:172`);`ContextHeader` 与正文分离只在嵌入时拼(`types/chunk.go:225-234`) | 白名单过滤;不建冗余父行;子段字符区间 |
| K-3 | `coalesceTinyChunks` 目标 max(200, size/2) 同标题前缀才并(`chunker/heading_splitter.go:139-146`);校验四条(`chunker/validator.go:26-70`);heuristic 切点(`chunker/profiler.go:215-236`);表格列数不一致 = 新表(`chunker/splitter.go:403-520`);坑:`overlap <= 0` 当默认 80(`chunker/strategy.go:325`) | 短节合并规则;形态测试集判据;Ⓑ 切点;显式开关 |
| K-10 | `rewrite.yaml:24-78` 两句;本地规则扩展(`query_expansion.go:14-186`);**反例** 改写后不留原问题(`chat_pipeline/search.go:376`) | 换说法 prompt;零成本对照;坚持留原问题 |
| K-12 / FAQ | 生成问题存 `chunk.metadata`、每题一条索引行指回原块(`knowledge_process.go:2114-2140`,`types/chunk.go:805-815`);`generate_questions.yaml`;FAQ 相似问每条一行 + 反例问题过滤(`knowledge_faq.go:1935-1985`,`knowledgebase_search_faq.go:188-193`) | 「一个分段多条索引行」统一机制;prompt 要点;过期标记 |
| K-15 | 文档画像 `{summary, gist, topics, doc_type, typical_question}` 头 8k 字采样(`generate_summary.yaml`,`knowledge_profile.go:30-39`) | 便宜版先行 |
| K-5 | `EmbedInputType document\|query` + 厂商 `InputTypeField / InputTypeValues`(`models/api/embeddings_settings.go:83-84`),阿里故意不声明(`providers/aliyun.go:167-169`) | 印证标记进嵌入指纹 |
| §6 | 切块预览抽屉 `KBChunkingDebug.vue:12-200` + `POST /chunker/preview` 返回 `Diagnostics{selected_tier, rejected, profile}`;配置摘要「父 4096 / 子 384」(`KBInfoPopover.vue:299-345`);索引策略变更即问「重新处理 N 篇」(`KnowledgeBaseEditorModal.vue:1829-1850`);生成问题 stale 标记;回答级「未从知识库中检索到相关内容,以上为模型直接回答」(`AgentStreamDisplay.vue:515`);`read_document` 结果 `role: match \| context_before \| context_after`(`types/tool-results.ts:314`) | 见 §6 v2.1 补充 |
| §9 | `logSearchResultChunkTypes` 按分段类型统计命中来源(`knowledge_process.go:295-317`);前端读 `token_count` 后端却不返回(`doc-content.vue:1048-1058` vs `types/chunk.go:134-200`) | K-11 / K-12 线上观测;接口契约测试钉字段 |

**我们已有 / 已超出**:加权 RRF;父段卡片 + 子段折叠(它是子段平铺、父段靠弹层);检索测试页四个分数 + 降级 + 预算(它文档库没有检索测试页,三分数前端不读);轨迹侧栏分数 / 降级(它不显);B-175 的「新版本 + 复用解析结果」重切(它无版本、无缓存、全量重跑);分段启停 / 编辑 / 修订历史(B-175)。

**不借(理由)**:字符计量(我们按 token);父子都带重叠、返回「父 + 子拼接」(我们纯父段);三层自动选切块策略(多一层不可预期);合成分 0.6 模型 + 0.3 检索 + 0.1 来源 + MMR(无外部依据,MMR 压同文档相邻段与 K-1 相反);知识图谱(它默认也关);PG 路径关键词阈值(没落地,不是先例);书本图标内联引用(与 `[doc 1234]` 冲突);`source_locators` 页码 / bbox 定位(要改入库产物,记后续);库画像注入(解决多库路由,B-175 候选);跨轮 KB 结果一行占位(记 B-126 后续)。

**转记别处**:Ⓕ 扫描件判定三常量 + 两段 VLM OCR prompt + OCR 子块挂文本块(`docreader/parser/pdf_parser.py:176-185`、`internal/application/service/image_multimodal.go:32-54`)→ B-175 P5。

---

## 6. Web 前端展示(K-1 / K-2 / K-4 / K-11 / K-12 落地时同步做)

参考 Dify(源码核过)、RAGFlow、FastGPT。四处:

| 页面 | 现在 | 改成 | 要补的接口 |
|---|---|---|---|
| **分段预览抽屉**(`SegmentPreviewDrawer.tsx`) | 一页 20 段平铺、只读 | **父段卡片**:`父段 #n · N token · N 个子段 · 启用`;展开看子段(C-1、C-2…,各自 token 数、标题路径);做了 K-11 / K-12 时在子段下显示生成的上下文说明 / 问题(灰字,标「只进索引」);「被检索次数」要有计数才显示(目前不记,后续)。参考 Dify「父段 · N 字符 · N 次检索」+「N 个子段」折叠列表 | `listChunks` 返回按父段分组(父段行 + children),`chunk_type`、`token_count`、`context_header`、生成的问题 |
| **检索测试**(`RetrievalTestTab.tsx`) | 列出命中段、向量分、走的哪条路径 | 结果 = **父段**:标题 / 文档短编号 / 上传时间 / 版本;「命中 N 个子段」,每个子段显示**四个分数**(向量 / 关键词 / 融合 / 重排;后端已算,接口没全返回)和路径;父段分数 = 子段最高(Dify 同款);子段在父段正文里**高亮**;按文档筛选;「模型实际会收到的文本」预览 + 预算用了多少 token;降级标记(重排 / 嵌入) | `testRetrieval` 返回父段 + 子段明细 + 四个分数 + `rerank_degraded / embedding_degraded` + 预算用量 |
| **会话轨迹面板**(工具行详情) | 显示工具返回的原文 | 模型拿到的原文**照原样**不动;另加不给模型看的侧栏:文档(可点开分段预览)、父段 / 子段编号、子段高亮、四个分数、是否降级、`knowledge_read` 读了哪几页 | `ToolResult.meta` 带结构化命中明细(K-4) |
| **库设置**(`SettingsTab.tsx`) | 切块上限 / 重叠 / top_k / 阈值 / 方式 / 重排 | 字段改成:**子段上限**(默认 400)、**父段上限**(默认 1,024)、**返回预算 token**(默认 8,000)、每文档父段上限(默认 3);重叠、语义断点不再暴露(平台默认 0 / 关;A/B 用环境开关);显示「切块参数版本」与库里有多少文档还是旧版本 + 「全库重新摄取」按钮(复用 DocumentsTab 已有的重新摄取) | 库字段 `parent_max_tokens / result_token_budget / max_parents_per_doc`;文档 `chunk_params_hash` |

轨迹面板的「侧栏」只给 system_admin / 租户管理员(与成本列同门)。

**v2.1 补充(WeKnora 前端对照后加的)**:
- **切块预览抽屉**(库设置页,新):粘一段文本或选一个形态样例(§4 的六种形态当预设)→ 不入库,调同一套切块器 → 显示父段 / 子段数、每段 token、标题路径、短节合并了几处、校验四条的结果;改子段 / 父段上限立刻重算。参考 WeKnora `KBChunkingDebug.vue` + `POST /chunker/preview`。
- **配置摘要行**:库卡片 / 库信息弹层显示「子段 400 / 父段 1,024 / 预算 8,000 token」,一眼看出这个库用的什么参数。
- **保存即问**:库设置保存成功后,若有文档的切块参数指纹与新参数不一致,立刻弹「N 篇文档还是旧参数,是否全库重新摄取」,不等人去找按钮。
- **生成问题的过期标记**(K-12 做了才有):子段被编辑后,其生成问题灰标「基于编辑前内容生成」,可重生成。
- **回答级提示**(对话页,面向普通用户):本轮所有检索 0 命中、或重排 / 嵌入降级时,回答旁一枚小标「未从知识库检索到相关内容,以上为模型直接回答」;轨迹侧栏的细节仍只给管理员。
- **读文档结果标记**:`knowledge_read` 的 `ToolResult.meta` 标每个父段是 `match / context_before / context_after`,侧栏「读了哪几页」按它高亮。
- **接口契约测试**:`listChunks` / `testRetrieval` 新增字段(`chunk_type / token_count / parent_chunk_id / context_header / rerank_outcome`)写进契约测试——WeKnora 前端读 `token_count` 后端却从不返回,这种漏洞 lint 查不出。

---

## 7. 不做(带理由,v2 核实)

| 做法 | 理由 |
|---|---|
| 知识图谱 / GraphRAG / LightRAG / HippoRAG | 《BM25 Wins at Scale》1,144 篇小库上 HippoRAG 2 66.2、LightRAG 48.0、MS-GraphRAG 45.9 vs BM25 74.7,且 MS-GraphRAG 只建到 8,750 篇、LightRAG 2,254 篇(全量估 79 亿 token 建不完);GraphRAG 论文自述解决的是「整个语料的主题」类总结题,不是事实题 |
| 全库地图 / 训练路由器(AtlasNav) | **v2 更正**:AtlasNav 当时是榜首(87.00,找全 66.98、项目题 59.09,都远高于我们),不是「仍弱」;不做的理由是**建库成本**(多视图聚类 + 训练路由器),等 K-1~K-14 做完再看 |
| RAPTOR 分层摘要 | 「+20%」是 RAPTOR + GPT-4 对别家旧模型(82.6 vs 62.3);**同读者受控对比只 +2.0(vs DPR)/ +5.1(vs BM25)**;收益在单篇长文综合,不是我们丢分的地方;建库成本高 |
| 迟分块(late chunking) | 要逐词向量再池化(Jina 原文),只给一个最终向量的 API 不支持;厂商中立的平台不能依赖 |
| 文档摘要索引 / 通用摘要 | Anthropic 原话:「adding generic document summaries to chunks (we experimented and saw very limited gains)… summary-based indexing (we evaluated and saw low performance)」 |
| HyDE | 《Best Practices》里 HyDE 是最强单项(+5.2 nDCG)**但延迟 2.6 s → 7.2 s**,且对强检索器 / 内部术语有害(2309.08541、2505.12694);T2-RAGBench 上 HyDE 低于稠密基线。**v2 理由改为「收益不稳 + 延迟 3 倍」**,不再说「效果差」 |
| 按时间自动降权 / 新版覆盖旧版 | 全国版与地区版政策可以并存;改成显示日期让模型判断(K-4) |
| 激进的相似文档去重 | 会藏掉有冲突的版本;只去字节完全相同的 |
| 不留原问题的纯扩写 | 对内部术语失效(K-10 必须留原问题一路) |
| 按 ERB「语义改写」题风格调提示词 | 那类题是故意压低关键词重合度生成的,属于过拟合 |
| 修字面 `\n` 转义的单行文本 | 评测语料特有格式(9,518 篇),租户资料罕见 |
| 按来源 / 连接器划范围、按目录树导航 | 租户是上传文件,没有 Slack / Jira 连接器与有意义的目录 |
| 无向量树索引(PageIndex)替代向量检索 | FinanceBench 98.7% 是厂商自报(Mafin 2.5,歧义题人工判);第三方 2604.14222 单文档树推理 0.938 vs 向量 0.821,但 1,200 份 SEC 多文档上向量式智能体检索 68% 胜率;10 万篇级没有数字。思路(按目录推理)已被 K-2 的 `section` 读法吸收 |
| 用 grep 代替向量 / 关键词检索(「Claude Code 不用 RAG」) | 只有访谈没有数字;ERB 论文 Bash Agent 60.6 < BM25 68.8、每题 10 分钟;《BM25 Wins at Scale》文件系统智能体 30.7 vs BM25 50.5、每题 895K token;「Iteration helps most after global ranking, not in place of it」——迭代探索应该叠在排序检索之上(K-2 / K-13),不是替代 |
| 重排后合成分(模型分 × 0.6 + 检索分 × 0.3 + 来源分 × 0.1)+ MMR 去冗余(WeKnora) | 没有外部依据;MMR 会压掉同一文档的相邻段,与 K-1「父段整体返回」方向相反;B-175 PR3 模拟过 MMR 无收益 |
| 自建 PostgreSQL 装 ParadeDB `pg_search`(WeKnora 的路) | 与「生产用 RDS 托管」的决策冲突,多一套数据库运维,且社区版 AGPL;RDS 上 PG 17 有 `pg_textsearch` 可走(K-9 路 A) |
| 照搬榜单前几名的答题模型 | 不是检索问题 |

---

## 8. 已修 / 评测工具改进 / 文档更正

### 8.1 已修(评测跑在 B-198 代码上,之后合入)

- B-199(#1857):.txt 按纯文本读、只认 `#` 标题 → 评测里 Confluence `h1.` 标记、`# 编号列表` 被当标题产生的错误标题路径,在主干上已不存在。
- B-200(#1858):段长含标题与路径、表格单行超长也切、嵌入按字节截。

### 8.2 评测工具改进(只改评测脚本,不进产品)

- 每次检索记录返回了哪些段(文件、段号、名次、重排分),以后诊断不用推断。
- 前后两次评测保持「每题最多 5 次检索」不变,保证可比。
- 判分噪声单列,复核 D 组 19 题误判;§2.3 的题目问题在报告里注明。
- 评测脚本碰预算上限要暂停保库,不要退出(本次补答因此多入库一次 3.7 小时)。

### 8.3 文档更正

- B-175 设计 §3.2「查询改写 / HyDE」一行写「Anthropic 实测 HyDE 效果差」:Anthropic 文章只**列举**了 HyDE,没有测;「效果低 / 收益有限」说的是摘要索引与通用文档摘要。已改(#1873),结论见 K-10 与 §7。
- v1 → v2 引用更正:Doc2Query-- 的「+16% / −33%」是过滤 vs 不过滤(K-12);RAPTOR「+20%」是跨模型(§7);Onyx「±5 段」是模型分类后扩展(K-1);Onyx 找全题 59.0 是文档召回、正确率反降(K-6 / K-13);BGE-M3 MLDR 中文数字(K-16);RAGFlow 已重写为 Go,`rag/app/naive.py` 不存在(§11);NVIDIA 博客标题是《Finding the Best Chunking Strategy for Accurate AI Responses》;Dify 父子模式无重叠、「全文模式 10,000 token」只在文档没在源码;AtlasNav 不是「仍弱」(§7);榜单分档按 2026-10-20 更新(§1)。
- v2 → v2.1 更正:「RDS 装不了任何 BM25 扩展」只对 PG 16 成立,`pg_textsearch` 1.3.1 在阿里云 RDS PG 17 / 18 可装(K-9 路 A);B-175 设计 §3.1 WeKnora 一行里「切块在 Python docreader」已过时,v0.8 起切块在 Go `internal/infrastructure/chunker/`。

---

## 9. 验证与发布

### 9.1 评测资产(批 0 先补齐)

| 资产 | 现状 | 要做 |
|---|---|---|
| 华大主集 303 题 / 真实问法集 172 题 / 留出集 22 题(期望文档 + 锚文本) | 在仓库,`kb_eval.py` 跑召回层 hit / MRR / nDCG | 加「父段级段命中」:锚文本是否落在**返回给模型的文本**里;加每次检索的逐段记录 |
| 智能体层 A/B(60 题:资料没写 30 + 有答案 30;判「答对 / 编造 / 正确说没写 / 误拒」) | 脚本在会话目录 `agent_eval/kb_agent_ab.py`,**没入库** | 搬进 `tools/eval/`,参数化答题模型与判分模型,判分缓存;每次约 ¥15(glm-5.3 答题 + deepseek 判分) |
| 平台形态测试集(§4 原则 3 的六种形态) | 没有 | 合成文档(零租户内容):各形态 2~3 篇;单元测试断言子段长度分布、父段数、没有「只剩标题」的段、对话体按行;CI 跑 |
| ERB 7 源子集 363 题 | 语料与判分脚本在会话目录 | 留出复测:同一判分、同一 5 次检索上限;**约 ¥250**(重新嵌入约 ¥156 + 答题与判分约 ¥90),跑前报价等批 |

### 9.2 每批怎么验

| 环节 | 做法 | 花费 |
|---|---|---|
| 每条改动 | 华大主集 + 真实问法集前后对比:召回层(doc_hit@10 / chk_hit@10 / mrr@10)、带重排、父段级段命中;影响呈现的(K-1 / K-2 / K-4 / K-6)再跑智能体层 60 题;逐条过厂商 / 形态检验表;**逐题列「原来对、现在错」** | 召回层一轮约 ¥1(带重排);智能体层一轮约 ¥15;每次单独报价 |
| 切块类(K-3 / K-1 / K-11 / K-12 / K-16) | 「改切块参数 = 新版本」:复用解析结果重切、建好再切换,不出现新旧混杂;测试环境先重新摄取;形态测试集过 CI | 测试环境很小 |
| 厂商中立(K-5 / K-7 / K-8) | 目录里每家至少一个模型跑通:openai(无查询标记、无重排接口 → `llm` 档)、glm、qwen(原生口);不支持的能力看到降级标记而不是报错 | — |
| 留出复测 | 批 2 合入后(至少 K-1 / K-3 / K-4 / K-14 在)ERB 整套重跑一次,出「改进前 → 改进后」对比报告(用户 10-21 拍板:评测报告等这次复测后出) | 约 ¥250,跑前报价 |
| 发布 | 每批同 PR 写真栈用例(runbook),发测试 / 生产按既有流程等指令 | — |

---

## 10. 开工顺序(开工时间等用户指令)

| 批 | 内容 | 说明 |
|---|---|---|
| **批 0** 评测地基 | 智能体层脚本入库、逐次检索记录、父段级段命中、形态测试集;模型目录能力字段(嵌入 / 重排)骨架;~~PG 17 上的「改动前」ERB 基线~~ **10-21 开工时核实撤销**:ERB 答题与重放都走 `kb_eval.open_store("sql")` 默认精确检索(`ann=False`,`enable_indexscan = off`),库是本机临时 pgvector 容器而不是阿里云 RDS —— HNSW 从没用上,70.9 不受 B-208 影响,「改动前」仍用 70.9(复测报告注明期间 B-199 / B-200 也已合入);「改动前」基线改为华大开发集召回层 + 智能体层 60 题各跑一次(约 ¥16,用户 10-21 批)。BM25 对照(`pg_textsearch` 已装在测试库)留在 K-9| S~M;不改检索行为,先有尺子再动刀 |
| **批 1** 不用重新入库 | K-14 ①②(单视图 + 池 100)→ K-7、K-8 → K-5 → K-4 → K-6 | 每条独立 PR,各自在开发集前后对比;K-4 的条数加法要过智能体层 |
| **批 2** 要重新入库 | K-3 → K-1 → K-2 + §6 四处前端 | 父子块整体;测试环境重新摄取;合入后做 ERB 留出复测 |
| **批 3** 第二梯队 | K-10(+ 融合 A/B)‖ K-9 → K-13 → K-11 / K-12 各自 A/B(选中的与下一次重切同批入库) | 顺序理由见 §5.3 末;**K-9 前置:测试库升 PG 17 并装 `pg_textsearch`(⑦ 已拍板,B-207);路 A 必须可选,检测不到插件走路 B / 现有计分** |
| 后续 | K-15(先定租户字段配置方式)、K-16(K-9 之后再评估)、K-17(触发式) | |

**拍板结果(2026-10-21,用户:「其他的按你的建议走」;⑦ 另见下)**:① ✅ 按本节批 0 → 批 1 → 批 2 → 批 3 的顺序,每批合入前在开发集逐题对比改前改后;② ✅ 语义断点默认关、重叠默认 0,现有库在批 2 一起重切;③ ✅ 默认 10 个父段 / 8k token 预算 / 每文档 ≤ 3,批 1 先用真实答题模型验证(有「给多了被带偏」的反面证据);④ ✅ K-15 便宜版(固定字段)先做,租户自定义字段那一步暂不做,等真实租户提需求;⑤ ✅ ERB 留出复测做,放在批 2 之后,开跑前报价;另按 B-208 在批 0 补 PG 17「改动前」基线,同样先报价;⑥ ✅ 批 2 合入并发测试后重新入库(测试 1 篇);生产随生产发布;⑦ ~~RDS 大版本升级 16 → 17 做不做、何时做~~ **✅ 10-21 拍板:测试库先升(B-207),生产等 B-206 全部做完、与知识库版本一起升;K-9 路 A 做成可选,BM25 效果批 0 在测试库上量后再定走哪条路**。

**10-21 用户「开始吧,并行做」开工批 0**:PR 0-1 评测工具(父段级段命中 `context_hit` + 逐次检索记录 + 智能体层评测入库)/ 0-2 文档形态测试集 / 0-3 模型目录能力字段骨架,三个并行。

---

## 11. 出处(都读过原文;v2 新增标 ★)

- Onyx ERB 论文:https://arxiv.org/html/2605.05253v2 ;排行榜:https://huggingface.co/spaces/onyx-dot-app/EnterpriseRAG-Bench-Leaderboard ;仓库:https://github.com/onyx-dot-app/EnterpriseRAG-Bench
- Onyx 产品:`backend/onyx/tools/tool_implementations/search/constants.py`(`MAX_CHUNKS_FOR_RELEVANCE`、查询扩写权重、`RRF_K_VALUE`)、`tools/tool_implementations/utils.py`(结果 JSON 拼法)、`prompts/tool_prompts.py`(★「do not repeat the same or very similar queries」)、`tools/tool_implementations`(★ `read_file` / `open_url`)、`configs/app_configs.py`(★ 512 / 重叠 0 / mini chunk)、`.agents/feature-map/components/internal-search.md`、`indexing-pipeline.md`
- 《BM25 Wins at Scale》:https://arxiv.org/html/2607.26497 ;AtlasNav:https://arxiv.org/html/2608.24764
- Flexor:https://flexor.ai/blog/flexor-ranked-1-enterpriserag-benchmark/ 、https://flexor.ai/blog/managed-context-ai-agents/ ;metor:https://www.metor.com/blog/metor-memory-leads-enterpriserag-bench/ ;Mixedbread:https://www.mixedbread.com/blog/closing-gap 、https://www.mixedbread.com/blog/toast-1
- Anthropic Contextual Retrieval:https://www.anthropic.com/news/contextual-retrieval ;cookbook:https://github.com/anthropics/claude-cookbooks/blob/main/capabilities/contextual-embeddings/guide.ipynb ;★ 第三方复现:https://denser.ai/blog/compare-open-source-paid-models-anthropic-dataset/ 、https://developers.llamaindex.ai/python/examples/cookbooks/contextual_retrieval/ 、T2-RAGBench https://arxiv.org/html/2604.01733
- ★ Anthropic《Writing effective tools for agents》:https://www.anthropic.com/engineering/writing-tools-for-agents ;多智能体研究系统:https://www.anthropic.com/engineering/built-multi-agent-research-system
- Cohere 重排最佳实践:https://docs.cohere.com/docs/reranking-best-practices 、https://docs.cohere.com/reference/rerank
- ★ 切块实证:Chroma https://research.trychroma.com/evaluating-chunking ;NVIDIA https://developer.nvidia.com/blog/finding-the-best-chunking-strategy-for-accurate-ai-responses/ ;语义切块 https://arxiv.org/html/2410.13070 ;https://arxiv.org/html/2505.21700 ;https://arxiv.org/html/2608.16586 ;https://arxiv.org/html/2606.00881 ;中文 CRUD-RAG https://arxiv.org/html/2401.17043 、Meta-Chunking https://arxiv.org/html/2410.12788 ;HiChunk https://arxiv.org/html/2509.11552
- ★ 小检索大返回:LongRAG https://arxiv.org/html/2406.15319 ;Mix-of-Granularity https://arxiv.org/html/2406.00456 ;dsRAG https://github.com/D-Star-AI/dsRAG
- LlamaIndex:auto-merging https://github.com/run-llama/llama_index/blob/main/docs/examples/retrievers/auto_merging_retriever.ipynb 、★ sentence window `docs/examples/node_postprocessor/MetadataReplacementDemo.ipynb` ;★ LangChain `libs/langchain/langchain_classic/retrievers/parent_document_retriever.py` ;★ Unstructured https://docs.unstructured.io/open-source/core-functionality/chunking
- Dify:父子块 https://github.com/langgenius/dify-docs/blob/main/en/cloud/use-dify/knowledge/create-knowledge/chunking-and-cleaning-text.mdx ;检索测试 https://github.com/langgenius/dify-docs/blob/main/en/cloud/use-dify/knowledge/test-retrieval.mdx ;源码 `api/core/rag/datasource/retrieval_service.py`(父段分数 = max 子段)、`api/core/rag/index_processor/processor/parent_child_index_processor.py`、`web/app/components/datasets/documents/detail/completed/segment-card`、`hit-testing/components`
- RAGFlow(★ 已重写为 Go):https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/configuration.md ;源码 `internal/ingestion/component/chunker/general.go`、`internal/ingestion/component/extractor.go`(自动问题 prompt)、`internal/common/parser_config.go`(默认关)、`internal/engine/elasticsearch/chunk.go`(字段权重)
- FastGPT:https://doc.fastgpt.io/docs/guide/dataset/dataset_engine ;★ `packages/global/core/dataset/training/utils.ts`、`packages/service/common/string/textSplitter.ts`
- 阿里云:嵌入 https://help.aliyun.com/zh/model-studio/embedding 、同步接口 https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api 、OpenAI 兼容 https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai 、★ 重排 API https://help.aliyun.com/zh/model-studio/text-rerank-api 、限流 https://help.aliyun.com/zh/model-studio/rate-limit 、错误码 https://help.aliyun.com/zh/model-studio/error-code 、★ RDS PG 插件列表 https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/plug-ins-1/
- ★ 查询侧 / 扩写:Qwen3-Embedding https://arxiv.org/html/2506.05176 、https://huggingface.co/Qwen/Qwen3-Embedding-8B ;Best Practices in RAG https://arxiv.org/html/2407.01219 ;扩写失效 https://arxiv.org/html/2309.08541 、https://arxiv.org/abs/2505.12694 ;query2doc https://arxiv.org/abs/2303.07678 ;RAG-Fusion https://arxiv.org/abs/2402.03367 ;HyDE https://arxiv.org/abs/2212.10496
- ★ 融合:https://ar5iv.labs.arxiv.org/html/2210.11934 ;Weaviate https://weaviate.io/blog/hybrid-search-fusion-algorithms
- ★ Postgres BM25:ParadeDB https://github.com/paradedb/paradedb ;VectorChord-BM25 https://github.com/tensorchord/VectorChord-bm25 ;pg_textsearch https://github.com/timescale/pg_textsearch ;Postgres 文档 textsearch-limitations / textsearch-controls;https://www.paradedb.com/learn/search-in-postgresql/bm25 ;https://www.pedroalonso.net/blog/postgres-bm25-search/
- ★ 条数 / 噪声:Lost in the Middle https://arxiv.org/abs/2307.03172 ;Power of Noise https://arxiv.org/abs/2401.14887 ;Databricks https://arxiv.org/abs/2411.03538 ;More Documents Same Length https://arxiv.org/abs/2503.04388 ;Lost in the Noise https://arxiv.org/abs/2601.07226 ;Chroma Context Rot https://research.trychroma.com/context-rot
- 论文(其它):Doc2Query-- https://arxiv.org/html/2301.03266 ;BGE-M3 https://arxiv.org/html/2402.03216v5 ;late chunking https://arxiv.org/html/2409.04701v3 ;RAPTOR https://arxiv.org/abs/2401.18059 ;TAG https://arxiv.org/abs/2408.14717 ;GraphRAG https://arxiv.org/abs/2404.16130 ;★ PageIndex https://github.com/VectifyAI/PageIndex 、https://arxiv.org/abs/2604.14222
- ★ 产品工具:OpenAI file search https://platform.openai.com/docs/guides/tools-file-search ;Glean https://docs.glean.com/actions/glean/read-document/ ;Vectara https://docs.vectara.com/docs/agents/tools ;AWS Bedrock KB https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html ;Azure agentic retrieval https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview ;Cursor https://cursor.com/docs/context/codebase-indexing
- ★ 厂商接口(表 5-A / 5-B,2026-10 读原文):OpenAI https://platform.openai.com/docs/guides/embeddings ;Cohere https://docs.cohere.com/reference/embed 、https://docs.cohere.com/reference/rerank 、https://docs.cohere.com/docs/rate-limits ;Jina https://api.jina.ai/openapi.json 、https://jina.ai/reranker ;Voyage https://docs.voyageai.com/reference/embeddings-api 、https://docs.voyageai.com/reference/reranker-api 、https://docs.voyageai.com/docs/rate-limits ;智谱 https://docs.bigmodel.cn/api-reference/模型-api/文本嵌入 、…/文本重排序 ;火山 https://www.volcengine.com/docs/82379/1302003 、https://www.volcengine.com/docs/84313/1254474 ;百度千帆 https://cloud.baidu.com/doc/qianfan-api/s/Fm7u3ropn 、https://cloud.baidu.com/doc/qianfan-docs/s/em95lyyjw ;腾讯 https://cloud.tencent.com/document/product/1729/102832 、https://cloud.tencent.com/document/product/1772/115339 ;MiniMax https://platform.minimaxi.com/document/Embeddings ;SiliconFlow https://docs.siliconflow.com/en/api-reference/embeddings/create-embeddings 、…/rerank/create-rerank ;Google https://cloud.google.com/vertex-ai/generative-ai/docs/embeddings/task-types 、https://cloud.google.com/generative-ai-app-builder/docs/ranking ;AWS https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html 、https://docs.aws.amazon.com/bedrock/latest/userguide/rerank.html ;Azure https://learn.microsoft.com/azure/ai-foundry/openai/quotas-limits ;开源模型卡 BAAI/bge-m3、bge-large-zh-v1.5、bge-reranker-v2-m3、Qwen/Qwen3-Embedding-8B、Qwen3-Reranker-8B、nomic-ai/nomic-embed-text-v1.5、intfloat/multilingual-e5-large-instruct、maidalun1020/bce-reranker-base_v1(HuggingFace);vLLM `docs/models/pooling_models/scoring.md`、TEI openapi 1.9.4、Xinference 文档
- pgvector:https://github.com/pgvector/pgvector
- ★ v2.1 WeKnora(本机 `/Users/mac/src/github/WeKnora`,HEAD e18fb23,2026-10-21,https://github.com/Tencent/WeKnora):`internal/infrastructure/chunker/{strategy,heading_splitter,validator,profiler,splitter}.go`、`internal/types/{chunk,knowledgebase,rerank,retrieval_config}.go`、`internal/reranking/{rerank,passage,mmr}.go`、`internal/models/api/{rerank,rerank_settings,embeddings,embeddings_settings}.go`、`internal/agent/tools/{search_knowledge,read_document,list_documents}.go`、`internal/agent/{grounding_prompt,observe}.go`、`internal/modelcontext/{model_output,citations}.go`、`config/prompt_templates/{agent_system_prompt,rewrite,generate_questions,generate_summary}.yaml`、`frontend/src/views/knowledge/settings/{KBChunkingSettings,KBChunkingDebug}.vue`、`website-docs/03-features/{04-chunking,04-rag-pipeline,05-retrieval-engines}.md`
- ★ v2.1 RDS:插件列表 https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/plug-ins-1/(2026-09-23,`pg_textsearch` 行);大版本升级 https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/upgrade-the-major-engine-version-of-an-apsaradb-rds-for-postgresql-instance(2026-07-30);pg_textsearch https://github.com/timescale/pg_textsearch 、https://www.tigerdata.com/docs/learn/search/using-pg-textsearch
