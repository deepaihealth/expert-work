# B-175 知识库 v2 整体设计方案(后端 · Web 管理端 · 开放 API)

- **状态**:设计稿,**登记不排期**(2026-10-10 用户拍板:先出完整方案与计划,开工时间等指令)。
- **配套计划**:[`docs/superpowers/plans/2026-10-10-b175-knowledge-base-v2-plan.md`](../plans/2026-10-10-b175-knowledge-base-v2-plan.md)。
- **取代**:[B-171 设计](2026-10-10-b171-knowledge-base-optimization-design.md)(知识库优化)与 [B-172 设计](2026-10-10-b172-material-library-design.md)(物料库)并入本方案;两份旧稿保留作背景,结论以本文为准。B-174(跨副本取消缺租户上下文)与本方案无关,单列。
- **依据**(2026-10-10 同日四路调研 + 此前评估,关键结论均已回到原文 / 源码抽查):
  1. 著名开源项目:RAGFlow、Dify、FastGPT、MaxKB、AnythingLLM、Onyx、LightRAG(读官方文档原文与源码);
  2. 本地仓库:WeKnora(后端四路深挖 + 管理端 / 开放 API)、OpenViking(后端 + Studio / API)、PageIndex;
  3. 行业公开实践:Anthropic、Chroma、Cohere、Pinecone、Weaviate,以及 OpenAI / AWS Bedrock / Google Vertex / Azure 的知识库 API(读原文,出处见 §11);
  4. 本仓库现状盘点(file:line 见 §2 与计划)。

---

## 1. 目标与范围

### 1.1 要解决的问题

1. **客户资料格式多、结构乱**:扫描件、截图、表格、聊天记录、多版本手册混在一起;同一件事散在多份文件,叫法不同,数据互相矛盾。
2. **检索质量差**:中文关键词实际失效,重排配置粗糙,无法量化好坏。
3. **智能体用不好知识库**:不知道库里有什么,只有一个检索动作,不能读全文 / 看前后文,引用无页码。
4. **管理端只能「上传 + 看」**:不能改分段、没有元数据、不分角色、不分页、没有校对流程。
5. **第三方无法对接知识库**:对外 API 完全碰不到知识库。
6. **按客户意图找商品 / 视频物料**(原 B-172)。

### 1.2 范围

| 块 | 含义 |
|---|---|
| **后端** | 入库(接入 / 识别 / 清洗 / 切块 / 索引)、检索、智能体工具、整理与确认、FAQ、物料、评测、权限 / 审计 / 配额 |
| **Web 管理端** | `apps/admin-ui` 知识库相关全部页面 |
| **开放 API** | 给第三方的知识库接口(检索、文档管理、任务查询),含鉴权粒度、文档站 |

**不在范围**:外部商品系统对接(走 MCP);知识图谱 / GraphRAG(§3.4 说明为何暂缓);下单 / 支付。

### 1.3 成功标准(验收以数字说话)

| 指标 | 现状 | 目标(以评测集校准后定稿) |
|---|---|---|
| 真实业务问题 recall@10 | 未测 | 有基线,且每批改动不回退 |
| 自然语言长问句关键词路命中 | 测试库实测 0 | ≥1 命中(固定回归用例) |
| 扫描页内容可检索 | 0(静默丢失) | 抽检扫描页文字可被检索命中 |
| 入库可靠性缺陷 | 3 个已核实 | 0,并有并发集成测试 |
| 第三方可对接 | 不能 | 文档站完整 + 真栈用例通过 |
| 对客模式越权 / 泄露 | — | 0(物料与开放 API 的必过门槛) |

---

## 2. 现状(2026-10-10 实查)

**使用量**:测试环境 1 库 / 1 文档 / 41 段,1 个智能体挂载(第三方耳机应用在用),累计 1 次 `knowledge_search` 调用;生产未查。

| # | 现状 / 缺陷 | 证据 |
|---|---|---|
| 1 | 关键词检索所有词须同时出现,真实查询 0 命中(改「任一词」4~29 命中) | `persistence/knowledge/sql.py:798` `plainto_tsquery` |
| 2 | jieba 切词不稳、无行业词典、无停用词 | 实测「吃代餐」「粉含」「二甲/双/胍」 |
| 3 | 重排专用分支丢分数、`top_n=top_k`,无法设阈值 | `orchestrator/llm/rerank.py:114` |
| 4 | 平台重排配置全局一份,**知识库与长期记忆共用同一实例** | `control_plane/app.py:1747-1761, 1827-1831`;测试环境配置为 `qwen3-vl-rerank` |
| 5 | 智能体只有 `knowledge_search(query, limit)`,固定英文说明,不知道库里有什么,引用无页码 | `orchestrator/tools/knowledge.py:292` |
| 6 | 扫描页 / 图片读不出,按整份文档判字数,混合 PDF 扫描页静默丢失 | `control_plane/knowledge/parsing.py` |
| 7 | 入库三个可靠性 bug:第 5 次尝试崩溃永久卡 processing;恢复任务批量认领串行致重复处理;同名重传旧任务写回旧内容 | `sql.py:49-63, 378-396`;`recovery.py` `run_once` / `_drive` |
| 8 | 切块重叠按 token 截断,中文出乱码(39 种尾切 13 种含 U+FFFD) | `chunking.py` `_tail_text` |
| 9 | 控制台写操作零审计 | `api/knowledge.py` 全文件 `emit` 为 0 |
| 10 | 上传无大小上限、无配额准入;原文件存 PG bytea | `api/knowledge.py:459` |
| 11 | 库列表、文档列表不分页 | 同上 |
| 12 | 管理端不看角色(viewer 能看到全部写按钮);分段只读;无元数据 / 标签;命中测试只显示向量分 | `apps/admin-ui/src/pages/knowledge_detail/*` |
| 13 | **对外 API 完全碰不到知识库**:`/v1/knowledge` 整组 `console_only()`;对外文档写死「Key 只能访问 `/v1/agents/{agent_code}/…` 与 `/v1/agent-catalog`」 | `api/knowledge.py:202`;`docs-site/guide/auth.md:48` |
| 14 | 知识库无独立权限资源,与智能体配置共用 `manifest`;API key 只有 read / write / admin,**不能限定到库** | `control_plane/auth/rbac.py:29-47`;`protocol/service_account.py:50-58` |
| 15 | 评测只有 11 条手造用例 | `tools/eval/datasets/rag/m0_baseline.yaml` |

**测试 RDS 可用扩展(实查)**:`pg_jieba` 1.2.0、`zhparser` 1.0、`pg_bigm` 1.2、`rum` 1.3(可装未装);已装 `vector` 0.8.0.2、`pg_trgm` 1.6;无 `pg_search`。生产须同样实查。

**做得对、保留**:结构感知切块(标题路径、表格不断开)、入库 CAS 租约 + 崩溃恢复骨架、换嵌入模型重建索引、检索 SQL 恒带 `tenant_id`(隔离优于 WeKnora)、控制台检索测试。

---

## 3. 参照结论

### 3.1 行业基线(多数项目 / 云厂商都有 —— 必须达到)

| 基线 | 出处 | 我们 |
|---|---|---|
| 三层资源:库 → 文档 → 分段;检索与人工维护都以分段为单位 | 6 个开源项目、4 家云厂商 | 有,但分段不可维护 |
| 异步入库 + 分阶段状态 + 单文件错误码 | Dify 五阶段、RAGFlow `progress_msg`、OpenAI `last_error.code`、Bedrock 统计 | 四态,无阶段 / 错误码 |
| 混合检索(BM25 + 向量,RRF k=60)+ 重排 + 阈值 + top_k | Anthropic、Azure、ES、5/6 开源 | 有形无实(关键词失效) |
| 结构化元数据过滤(类型化字段 + and/or + 比较运算) | Dify、RAGFlow、OpenAI、Bedrock、Vertex、Azure | 无 |
| 入库前分块预览;入库后分段可改 / 新增 / 停用 | RAGFlow、Dify、FastGPT、MaxKB | 无 |
| 命中测试页 | 4/6 开源 | 有,信息少 |
| 只检索不生成的对外接口 | 6/6 开源、4/4 云厂商 | 无 |
| 返回可引用结构:文档 ID、分数、元数据、页码 / 位置 | 4 家云厂商 | 只有文件名#段号 |
| 嵌入模型入库即锁定,换模型 = 全量重建 | 6/6 | 有 |
| 中文全文检索须分词 + 领域词典 | FastGPT(jieba)、IK、ParadeDB | 有分词无词典,且 AND |
| 多租户隔离服务端强制,租户身份来自凭据 | AWS 原话「否则越权」、Pinecone、Weaviate | 有 |
| 配额与限流:文件大小、批量、导入 / 检索 QPS | 4 家云厂商 | 无 |
| 智能体至少有 search 与 read 两类工具 | Anthropic context engineering、OpenAI file content | 只有 search |
| 离线评测集 + 回归门槛 | RAGAS、Bedrock 评测作业、Cohere 阈值校准 | 无 |

### 3.2 进阶做法(有数据支撑、按需采纳)

| 做法 | 证据 | 决定 |
|---|---|---|
| **上下文化切块**(每块前加 50~100 token 位置说明,再建向量与 BM25) | Anthropic:top-20 失败率 5.7%→3.7%;+BM25 2.9%;+重排 1.9%(-67%) | 采纳为**库级可选**,默认关(有成本);说明与原文分存 |
| **小库直通**(< 20 万 token 整库进上下文) | Anthropic 原话 | 智能体侧用 `knowledge_read` + 目录覆盖;不另做「直通模式」 |
| 父子块 / 小块检索大块返回 | RAGFlow、Dify、Bedrock;FastGPT `chunkSize/indexSize` 分离 | 后期,以评测决定 |
| 一段挂多个检索入口(生成问题 / 摘要 / 人工常问句) | FastGPT 多索引、RAGFlow Extractor、MaxKB 问题关联 | 采纳(FAQ 与条目化依赖) |
| 命中后直接返回原文(FAQ 零幻觉) | MaxKB `directly_return` | 对外检索 API 的 FAQ 结果可选 |
| 每库按 30~50 条业务查询校准重排阈值 | Cohere 官方方法 | 采纳,管理端提供校准助手 |
| 隐式过滤(模型从问题抽元数据条件) | Bedrock | 智能体工具的 `filters` 参数覆盖,不另做 |
| 知识图谱 / GraphRAG | GraphRAG 成本高;LazyGraphRAG 0.1% 成本;LightRAG | **暂缓**:先把「整理成条目」做扎实,图谱只给全局总结类需求 |
| 查询改写 / HyDE | Anthropic 实测 HyDE 效果差;智能体自己写查询 | 智能体场景**默认不做**;对外检索 API 提供 `rewrite_query` 选项,默认关 |
| 回调 webhook | 6 家开源**都没有** | 我们做(第三方必需,差异点),放开放 API 第二期 |

### 3.3 本地仓库借鉴

| 来源 | 借 | 不借 |
|---|---|---|
| WeKnora(MIT) | 智能体三件套工具与说明、库目录注入、「失败≠没找到」、扫描页判定 + VLM OCR 两段提示词、页码定位(`SourceLocator`)、文档 / 库画像提示词、Wiki 抽取 / 逐段引用 / 三层去重 / 「编译不创作」合并规则、FAQ 数据模型、**API key = 能力 × 库白名单 × 未声明即拒绝 × 启动自检**、分段乐观锁 + 修订历史 | PDF 解析器(文字页无表格识别,且会删纯数字行)、asynq/Redis 任务体系、Wiki 不进检索、错误格式 4 种、Owner 能读回明文 key、同列存多模态向量 |
| OpenViking(AGPL,只借思路) | 统一信封 + 字符串错误码、统一 `/tasks`、两段式上传、导入与定时同步合一(`watch_interval`)、检索 `include_provenance`(为什么命中)、OpenAPI 生成客户端 | 代码(AGPL)、文件系统语义作主 API、只能单副本 |
| PageIndex(MIT) | 「先看目录、再按页读」、页级引用、文档描述用于选文档 | SDK 整体(单用户本地形态)、中文标题归一化缺陷 |

### 3.4 开源项目的坑(要避开)

配置不可逆却不在建库时提示(Dify、RAGFlow);改配置后存量静默不更新;命中测试调好的参数不能回写(RAGFlow、Dify);默认阈值把正确答案滤掉(AnythingLLM 自认);key 粒度只到用户 / 团队(RAGFlow、FastGPT、AnythingLLM);无回调只能轮询(全部)。

---

## 4. 设计原则

1. **两层知识**:原始资料层(原样保留,出处与兜底)+ 整理条目层(清洗后的标准条目,检索优先)。原始资料不因整理丢弃。
2. **每句事实有出处**:整理产物必须引用到原文段落,无出处不写入。
3. **矛盾交给人**:平台发现并摆出矛盾,不替客户裁决;人工确认 / 修改过的内容,流水线只能提建议。
4. **平台默认可用,开关只用于收窄或高成本能力**([[platform-defaults-over-configuration]])。
5. **平台文本零租户内容**:工具说明、提示词、示例全部泛化。
6. **先量再做**:评测集先行;收益是假设的能力(父子块、图片向量、上下文化切块)以评测结果决定开不开。
7. **知识库与长期记忆解耦**:共用底座(客户端、凭据、限流、计费),**配置与策略分开**,改一边不动另一边。
8. **同一份逻辑三处复用**:控制台、智能体工具、开放 API 调同一个检索服务与同一套参数语义;命中测试 = 线上检索(Dify 做法),避免「测的不是线上的」。
9. **多副本语义**:后台任务一律 DB CAS / 代际守卫 / advisory lock,不引入 Redis 队列。
10. **对外最小授权**:开放 API 是**定位变更**(原定位「第三方只给 agent run」,2026-08-13),必须独立 scope、默认关闭、可限定到库。

---

## 5. 总体架构

```
            ┌────────────── Web 管理端(控制台 /v1/knowledge/…,裸 JSON)──────────────┐
            │                                                                          │
接入 ─► 识别 ─► 清洗 ─► 切块/索引(原始资料层)─► 检索服务 ◄── 智能体工具(search/read/list)
 │                         │                        ▲
 │                         └─► 整理(条目层)─► 确认台 ─┘(条目优先)
 │                                                  ▲
 └── 开放 API(/v1/kb/… 或 /v1/knowledge-bases/…,信封)──────────────────────────────┘
```

| 层 | 职责 | 计划阶段 |
|---|---|---|
| 接入 | 单个 / 批量 / 文件夹上传、URL、手工录入、OSS 前缀同步 | P0 / P9 |
| 识别 | 逐页扫描判定 → VLM OCR;图片描述 + 图中文字;表格;聊天记录;页码定位 | P5 |
| 清洗 | 页眉页脚、重复文件、空内容、版本 / 生效日期识别、文档画像 | P5 |
| 原始资料层 | 切块、嵌入、关键词索引、分段维护 | P0 / P3 / P5 |
| 整理(条目层) | 抽取 → 去重 → 矛盾 → 确认 → 发布 | P7 |
| 检索服务 | 关键词 + 向量 + 融合 + 重排 + 过滤 + 条目优先 | P2 |
| 智能体集成 | 三件套工具、目录、短编号、页码引用 | P4 |
| 开放 API | 检索、文档管理、任务、回调 | P6 |
| 评测与观测 | 评测集、检索日志、在线指标 | P1 / 贯穿 |

---

## 6. 后端设计

### 6.1 数据模型

所有新表 / 新列:带 `tenant_id` + RLS 策略(进 `test_rls_policy_coverage`);SQL 与内存两套 store 谓词同义(补参数化契约测试);迁移只做 expand,三段式发布。

**`knowledge_base`(增列)**

| 列 | 用途 |
|---|---|
| `kind` | `document` / `faq` / `material`(决定导入、索引、检索分支) |
| `metadata_schema` jsonb | 元数据字段声明:名称、类型(string/number/date/enum/bool)、是否可过滤、给模型看的描述 |
| `generated_profile` jsonb | 库画像(主旨、主题、典型问题、统计、聚合哈希、状态) |
| `retrieval_config` jsonb | 关键词 / 向量权重、重排开关、候选数、阈值(校准结果)、上下文化切块开关 |
| `api_access_enabled` bool | 对外 API 是否可访问本库(默认 false) |
| `lexicon_id` | 关联行业词典(可空) |

**`knowledge_document`(增列)**

| 列 | 用途 |
|---|---|
| `doc_seq` bigint | 租户内单调序号,重传保留;短编号 `D{doc_seq}` |
| `title` | 与文件名分开,可编辑 |
| `source_type` / `source_uri` / `external_id` | `file`/`url`/`oss`/`manual`/`faq`/`entry`;`external_id` 用于第三方幂等 upsert 与同步主键 |
| `metadata` jsonb、`enabled` bool | 用户元数据(按库 schema 校验)、文档级上下线 |
| `ingest_generation` bigint | 只增不减的代际守卫(修缺陷 7) |
| `stage` / `progress` / `error_code` / `error_detail` | 分阶段状态:`queued→parsing→ocr→chunking→embedding→indexing→ready / failed / cancelled` |
| `content_sha256`(已有)、`page_count`、`size_bytes`、`mime` | 同内容重传跳过;配额记账 |
| `summary` / `profile` jsonb | 摘要、主旨、主题、文档类型、典型问题、**生效日期 / 版本** |
| `storage_key` | 原文件迁 OSS 后的位置(PG 只存元数据) |

**`knowledge_chunk`(增列)**

| 列 | 用途 |
|---|---|
| `chunk_type` | `text` / `question` / `summary` / `faq` / `faq_question` / `entry` / `material` / `material_phrase` |
| `parent_chunk_id` | 多检索入口(问题 / 常问句行)折叠回主段;父子块 |
| `heading_path` | 标题路径**单独存**,只拼进嵌入 / 关键词文本,正文保持干净 |
| `context_header` | 上下文化切块生成的位置说明(可空) |
| `source_locator` jsonb | `[{type:"pdf",page:3,bbox?}]`、`sheet`+行区间、`slide` 等 |
| `enabled` / `edited` / `revision` | 分段停用、人工编辑标记、乐观锁 |
| `content_hash` | 内容不变不重算向量 |
| `image_refs` jsonb | 图片原件(OSS)、描述、图中文字,供回答时看原图 |

**新表**

| 表 | 用途 |
|---|---|
| `knowledge_task` | 统一异步任务(导入批次、重解析、删除、重建索引、整理、导入 FAQ):`task_` 前缀 ID、类型、状态、进度、错误、结果统计;开放 API 的 `/tasks` 与管理端进度共用 |
| `knowledge_chunk_revision` | 分段修订历史与回滚 |
| `knowledge_parse_cache` | 按页图片哈希缓存 OCR / 视觉结果;换切块参数不必重新 OCR |
| `knowledge_lexicon` / `knowledge_lexicon_term` | 租户行业词典(药名、产品名、项目代号),写入端与查询端共用 |
| `knowledge_retrieval_log` | 检索记录(来源:控制台测试 / 智能体 / 开放 API;查询、参数、命中、子分数、耗时),命中测试与线上共用 |
| `knowledge_review_item` | 统一待确认队列(新条目、矛盾、AI 生成的问题 / 标签 / 画像、解析异常) |
| `knowledge_entry_source` / `knowledge_entry_revision` / `knowledge_entry_conflict` | 条目化(§6.5) |
| `knowledge_tag` / `knowledge_document_tag` | 标签 |
| `material_type` / `material` | 物料(§6.7) |
| `api_key_grant` | API key 的知识库授权(§8.2) |

### 6.2 入库流水线

**阶段**:接收(配额准入、大小上限、哈希去重)→ 解析(按格式分流)→ OCR / 视觉(按页)→ 清洗 → 切块 → 嵌入 → 索引 → 后处理(画像、生成问题、条目化触发)。每阶段写 `stage` 与进度;失败写 `error_code`(`unsupported_file` / `invalid_file` / `parse_failed` / `ocr_failed` / `embedding_failed` / `quota_exceeded`)与可读原因。

**可靠性(修缺陷 7)**:代际守卫(所有写回带 `ingest_generation`);终态巡检(租约过期且尝试用尽 → failed);认领数 = 可并发数,处理中按 1/3 租约续约;重传不提前删分段,由 `replace_chunks` 事务整体替换;快速通道加全局并发上限;嵌入 429 退避。

**解析分流**(行业共识:有文字层走快速抽取,低质量 / 扫描页走 VLM):

| 输入 | 做法 |
|---|---|
| 有文字层 PDF、Office | pymupdf4llm / MarkItDown(现有),按页输出并记录「行区间 → 页码」 |
| 扫描页 / 低质量页 | 逐页判定(pymupdf4llm 自带 `analyze_page()["needs_ocr"]`,或面积比规则:图片 ≥50%,或字数 <10 且图片 ≥10%)→ 200DPI、长边 ≤2000、JPEG 85 渲染 → 平台视觉模型 OCR(扫描页版式提示词,要求 Markdown 表格)→ **先 OCR 把文字放回原位再切块**;按页哈希缓存;单页失败留占位不整份失败 |
| 文档内图片 | 按尺寸 / 面积 / 跨页重复(logo)/ 每份上限筛选;一次视觉调用「观察 + 描述」,大段文字或图表才追加 OCR;描述与图中文字写回原位置;原图存 OSS |
| 表格 / Excel / CSV | Excel 首行作表头;超大表按行拆并每段带表头;空表头行用首行数据补 |
| 聊天记录 | 识别「谁 · 时间 · 内容」,按会话段切 |
| URL | 抓取正文(SSRF 每跳校验),按 ETag / Last-Modified / 哈希判变化 |

**许可**:pymupdf / pymupdf4llm 为 AGPL / 商业双许可(venv METADATA 实证),网络服务使用的合规性需法务判断;长期替代 pypdfium2(BSD/Apache,已在 venv)+ 自补表格识别。外部 OCR / 解析服务(MinerU、Docling、PaddleOCR-VL)以本地业务样本小规模对比后再选,许可列为否决项。

**切块**:结构感知(标题 / 段落 / 表格为天然边界),目标 300~500 token,重叠 ≤15% 且只取到语义边界(修 U+FFFD);同一父标题下相邻短节合并;标题路径存 `heading_path` 并拼进嵌入与关键词文本;可选上下文化切块(便宜模型生成 50~100 token 说明存 `context_header`)。**改切块参数 = 新版本**:复用解析缓存重切,建好再切换,不出现新旧混杂。

**清洗与画像**:跨页重复页眉页脚;完全重复文件提示;空 / 纯占位块不嵌入;文档画像(每份一次调用,输入采样约 8k 字,不传文件名防编造):摘要、主旨、主题、类型、典型问题、生效日期 / 版本;库画像由文档画像确定性汇总 + 一次小调用,聚合哈希不变不重算。

### 6.3 检索服务

一个服务,三个入口(控制台命中测试、智能体工具、开放 API)共用,参数语义一致。

**关键词路**:
1. 查询端改「任一词命中 + 排序」;停用词;词数上限;只改查询端时现有索引不用重建(jieba 搜索模式切词包含精确模式的词,已实测)。
2. 行业词典:租户级词典,写入端与查询端共用;改词典后后台回填。
3. 分词与排序路线二选一(P2 用评测集对比后定):**A** 应用侧 jieba + `to_tsquery('a|b')` + `ts_rank_cd`,进阶为纯 SQL BM25;**B** 库内分词扩展(`pg_jieba` / `zhparser` 建 text search configuration)+ `rum` 索引排序。可选兜底路:`pg_bigm` 两字切分,专治词典缺词。

**向量路**:查询事务内 `SET LOCAL hnsw.ef_search` 与 `hnsw.iterative_scan = strict_order`(pgvector ≥0.8),候选 `clamp(2k, 100, 200)` 后过阈值;设置失败去掉重跑。

**融合**:加权 RRF(k=60,初值向量 0.7 / 关键词 0.3,以评测重调);每路每个列表各取最好名次;**不拿 RRF 分数当阈值**。

**重排**:
- 专用重排模型为默认(DashScope `gte-rerank-v2` / `qwen3-rerank`;请求体形状开工前核实),`top_n` = 候选总数,回传分数;聊天模型重排降为兜底。
- 候选 50~150,取 top 10~20;阈值作用于重排分,三档降级(阈值 → 阈值×0.7 且 ≥0.3 → 仅最好一条 ≥0.15 → 空);综合分 `0.6×重排 + 0.3×检索 + 0.1×来源权重`;MMR(λ=0.7)去重复。
- 阈值按库校准(Cohere 方法:30~50 条查询,每条配一个「刚好相关」的边界文档,取均值),管理端提供校准助手,结果写回 `retrieval_config`。
- **按用途拆分**:`knowledge` 与 `memory` 两套用途配置(默认都沿用平台那一份,行为不变),策略参数分开;本方案只动 `knowledge`,记忆侧改动并入 B-167 并以 LoCoMo 评测验证。

**过滤与条目优先**:元数据过滤 DSL(`and/or/not` + `eq/ne/gt/gte/lt/lte/in/nin/contains/exists`,**明确缺键语义**:`ne` / `nin` 不匹配缺键文档);`document_ids`、标签;条目层结果加权或先查(以评测定)。

**返回**:分段 ID、短编号、文档 ID / 标题 / 版本、内容、`heading_path`、`source_locator`(页码)、`score` 与子分数(向量 / 关键词 / 重排)、`match_reason`(命中了哪路、哪条问题或常问句)、耗时;**检索失败与「没搜到」分开报告**。每次检索写 `knowledge_retrieval_log`(按租户保留期清理)。

### 6.4 智能体集成

**三件套工具**(英文说明、平台默认注册、不加开关;已有同名工作区工具 `read_document`,新工具改名):

| 工具 | 参数 | 作用 |
|---|---|---|
| `knowledge_search` | `query, mode?(hybrid/semantic/keyword), bases?, filters?, limit?` | 保留原名;说明要求写完整问句,标识符用 keyword |
| `knowledge_read` | `id, offset?, limit?, query?, context?` | `D<n>` 从头读 / 翻页;`D<n>#<k>` 读某段及前后 k 段;`query` 文档内查找;**所有路径受输出预算约束** |
| `knowledge_list_documents` | `base, keyword?, filters?, page?, page_size?` | 列文档与一句话摘要 |

- **短编号**:`D{doc_seq}#{chunk_index}`(推荐;不含空格,穿过输出围栏不被改写,跨轮稳定,不破坏缓存)。
- **库目录**:系统提示尾部 `<knowledge_catalog>`(不可信围栏内,注明「导航提示,不是证据」),只放慢变字段:K 编号、库名、描述、库画像、文档数分档、可过滤元数据字段;每库约 600 token,最多 20 库;构建缓存键带目录哈希;**子智能体同样注入**。
- **辅助信息走 `ToolResult.notice`**(围栏外):省略条数、模式降级、下一页、「失败≠没找到」。
- **跨轮清理**:给现有旧结果清理加「知识引用」找回方式,stub 列出命中编号;沿用阈值触发。
- **引用**:`[D132#7]` → 校验必须来自本次运行的知识工具结果 → 展开为「文档名 · 第 N 页」。
- **系统提示规则**:绑定了库时先查再答;目录与摘要只是导航;精确数字与引文核原文。

### 6.5 整理成条目 + 确认台(解决「资料乱」的核心,差异点)

两家本地参照(WeKnora、OpenViking)与六个开源项目**都没有真正的审核流**;这是本方案的差异点。

**流程**(源文档就绪后自动触发,按库串行 `pg_advisory_xact_lock`):
1. 按文档画像与通用模板(事实 / 问答 / 流程 / 话术)抽取「一事一条」候选;
2. 逐批扫描段落为每个候选标注引用段落;无引用的事实不写入;
3. 三层去重:标题规范化相同直接合并 → `pg_trgm` 取前 5 → 严格模型判断(「相关≠相同」,同系列不同型号不合并,拿不准不合并)+ 代码校验合并目标在候选集内;
4. 合并遵循「编译不创作」;同一事实来源不一致时**不裁决**,生成矛盾项(各方原文、来源、生效日期 / 上传时间、「较新来源」提示);
5. 进入确认台:新条目、矛盾、低把握内容;确认 / 修改 / 驳回;人改过的条目锁定,之后只提 diff 建议;
6. 发布:条目作为 `source_type='entry'` 文档走同一检索,优先返回;
7. 维护:源文档更新 → 受影响条目生成建议;源文档删除 → 单源条目删除,多源条目把被引用原文交给模型撤回对应内容。

**统一待确认队列** `knowledge_review_item`:条目、矛盾、AI 生成的问题 / 标签 / 画像(需「采纳」)、解析异常(OCR 空页、乱码率高)。每个决定写审计。

**成本**:每份源文档约 3 次 + 受影响条目数次模型调用;每库每日预算上限。**开发前先拿一家真实资料包离线验证**(条目准确率、发现矛盾数、条目 vs 原文检索命中率)。

### 6.6 FAQ 类型

库或文档级 `faq`:主条目一行(问 + 答,metadata 存标准问 / 相似问 / 反例问 / 多答案 / 回答策略);每个相似问一行 `faq_question` 子段,各自嵌入与关键词索引,命中折叠回主条目;反例问「归一化比较 + 向量相似度阈值」剔除;xlsx/csv 导入,`dry_run` 预览 + 被拦截行可下载;按内容哈希幂等,问法行 ID 用 `hash(主条目, 规范化问法)`(避开 WeKnora 序号 ID 删不掉旧向量的 bug);保留关键词检索(型号 / 价格纯向量弱);对外检索可选「高于阈值直接返回标准答案」。

### 6.7 物料库(原 B-172)

- 物料是一条记录(`M{seq}`),前端按 ID 渲染卡片,模型无法编造物料;
- 「物料类型」声明字段及属性(可筛选 / 硬排除 / 软加权 / 对客可见),平台预置商品 / 视频 / 图文 / 服务;未配置时只对禁忌、上下架、有效期硬过滤;
- 每条物料生成需求画像(解决什么、适合谁、客户可能怎么说),说法行作为检索入口折叠回物料;禁忌候选须人工确认后生效;
- 入口:表格导入、手工录入、从零散文件抽取(复用 §6.5)、视频(语音转文字 + `ffmpeg` 关键帧 + 视觉描述;**平台目前无语音识别,需新接**);
- `material_search(need, types?, filters?, customer_conditions?, limit?)`:混合召回 → SQL 硬过滤 → 重排 + 软加权 → 类型配额;**对客 / 对员工模式由平台判定,不给模型选**;对客只返回已确认且可对客的物料与字段;
- 门槛:硬规则违反 = 0、对客泄露 = 0。外部商品系统走 MCP,不在范围。

### 6.8 数据源与同步

URL 导入(到期重新抓取,别学 WeKnora「URL 永不刷新」);OSS 前缀同步(增量 + 删除对账,凭据走金库);`external_id` 幂等 upsert(第三方推送与同步共用);导入与定时同步合一(参照 OpenViking `watch_interval`);飞书 / 语雀等连接器按需。

### 6.9 评测与观测

- **评测集**:每个重点租户 / 场景 50~200 条,来源 = 真实对话抽样 + 模型生成后人审 + 困难样本(编号精确匹配、表格、跨文档、应答「无答案」);走完整入库链路,不再用预切语料。
- **离线门槛**:recall@10/@20、MRR 或 nDCG@10、无答案拒答率、Faithfulness 抽检;任何影响检索的改动(切块、模型、分词、重排、条目化)跑回归,数字写进 PR。扩展 `tools/eval/rag.py`。
- **在线指标**:零命中率、同一问题反复检索比例、检索 p95、引用点击 / 点赞点踩;差评与零命中回流评测集。

### 6.10 权限、审计、配额

- **审计**:控制台与开放 API 的全部写操作(建 / 改 / 删库、上传 / 删文档、改分段、确认 / 驳回、改配置、生成 key 授权)写审计。
- **权限资源**:新增 `knowledge` 资源从 `manifest` 拆出(待拍板):viewer 只读、operator 读写含检索测试、admin 含删除与 API 访问开关。管理端按角色隐藏 / 置灰写按钮(现状靠后端 403 兜底)。
- **配额**:单文件大小(默认 50MB,可配)、每库文档数、存储字节、OCR / 视觉页数、嵌入 token、导入并发(每库 1 个批量任务)、检索 QPS;超限 429 + `Retry-After`。
- **保留**:检索日志、任务记录按租户保留期清理;删除文档先对检索不可见再异步物理清理。

---

## 7. Web 管理端设计

技术约定沿用:antd 5、React 19、中英两份文案键一致、`pnpm typecheck` / test / build、跨租户只读置灰语义(W3 / W4)不变;每页补 vitest,关键流程补 e2e。

| 页面 | 功能 | 参照 |
|---|---|---|
| **知识库列表** | 分页;显示类型、文档 / 分段数、处理中 / 失败数、最近同步、需重建索引;按角色显示写操作 | WeKnora、RAGFlow |
| **建库向导** | 类型(文档 / FAQ / 物料)、默认值只暴露关键项;**不可逆项(嵌入模型、类型)明确提示**;可选上传样例看分块预览 | Dify 预览、避开「不可逆不提示」坑 |
| **文档列表** | 分页 / 筛选(状态、来源、标签、元数据、时间)、阶段进度与失败原因(阶段 + 错误码)、批量(启停、重解析、打标签、改元数据、删除、取消) | RAGFlow、WeKnora |
| **上传面板** | 多文件 / 文件夹 / URL / 手工录入;上传队列与逐文件进度;可覆盖本次部分处理配置(OCR、标签、目标文件夹) | WeKnora |
| **文档详情(校对)** | 原文预览 ↔ 分段联动(点段落跳到原文页);分段编辑 / 新增 / 停用 / 删除;「已编辑」标记;修订历史与回滚(乐观锁);生成问题与常问句增删改;摘要 / 元数据编辑 | RAGFlow 联动、Dify Edited、WeKnora 修订 |
| **命中测试** | 参数:查询、模式、top_k、阈值、重排、过滤;结果:子分数(向量 / 关键词 / 重排)、命中原因、耗时、页码;**一键保存为库默认参数**;查看线上检索记录 | Dify Records、OpenViking provenance、避开「不能回写」坑 |
| **阈值校准助手** | 录入 30~50 条查询 + 边界文档,自动算阈值并写回 | Cohere 方法 |
| **元数据与标签** | 元数据字段定义(类型、可过滤、描述)、批量赋值、取值汇总;标签管理 | Dify、RAGFlow summary |
| **确认台** | 统一待确认队列:条目、矛盾(并排显示各来源与日期)、AI 生成项、解析异常;通过 / 修改 / 驳回,留审计 | 差异点 |
| **条目浏览** | 条目列表、来源引用、版本历史、锁定状态 | — |
| **FAQ 管理** | 标准问 / 相似问 / 反例问 / 多答案;导入 `dry_run` 预览 + 被拦截行下载;导出 | WeKnora、MaxKB |
| **物料管理** | 物料类型字段配置、物料列表与卡片预览、需求画像审核、禁忌候选确认 | B-172 |
| **数据源** | URL / OSS 同步配置、定时、删除同步、同步日志 | WeKnora、OpenViking watch |
| **设置** | 检索参数、上下文化切块开关、词典、对外 API 访问开关 | — |
| **活动日志** | 本库审计流水 | WeKnora |
| **平台设置**(system_admin) | 嵌入 / 重排模型按用途(知识库 / 记忆)配置 | §6.3 |
| **API 密钥**(租户设置) | 创建服务账号 key 时勾选知识库能力与库白名单;明文只显示一次 | WeKnora 模型、避开明文回读 |

---

## 8. 开放 API 设计

### 8.1 定位(待拍板 #1)

原定位(2026-08-13):第三方 API 只提供 agent run,key 摸得到的租户管理面能力都算缺口。用户 2026-10-10 要求方案包含知识库开放 API,这是**定位变更**,按以下最小授权落地:

| 档位 | 能力 | 建议 |
|---|---|---|
| A 只检索 | 列库(只含授权库)、检索、读文档 / 分段 | **第一期** |
| B 内容管理 | 上传 / 删除 / 改元数据 / 改分段 / 任务查询 / FAQ 导入 / 回调 | 第二期 |
| C 库管理 | 建库 / 删库 / 改配置 | 不开放(留在控制台) |

### 8.2 鉴权与授权

- 凭据沿用「服务账号 + API key」(`Authorization: Bearer aforge_pat_…`)。
- 新增 key 的**知识库授权**(参照 WeKnora 能力 × 库白名单、Dify 按库 key):能力 `kb:search` / `kb:read` / `kb:write`;库白名单(为空 = 不授权任何库,**默认拒绝**,与 WeKnora「为空 = 全租户」相反);每库 `api_access_enabled` 总开关。
- 存储:`api_key_grant(api_key_id, capability, kb_id)`;所有知识库对外路由显式声明所需能力,未声明即拒绝;启动自检路由与声明表一致。
- 租户身份来自凭据,请求体不接受 tenant;越权与不存在一律 404。
- 终端用户:可选 `user_id`(沿用 `ext:` 映射),用于审计与检索日志,不改变授权。

### 8.3 路由与约定(待拍板 #2:前缀)

- 控制台锁定检查按**字符串前缀**判断(`test_console_lockdown.py:525` `route.path.startswith(_CONSOLE_PREFIXES)`,其中含 `/v1/knowledge`),所以 `/v1/knowledge-bases` 会被当成控制台路径。两种做法:**推荐**把锁定前缀收紧为 `/v1/knowledge/`(段边界)再用 `/v1/knowledge-bases`;或改用 `/v1/kb`。
- 遵守对外清单:`tags=["external"]`、`external_only()` + `reject_nul_path_params`、注册在 `build_agents_router()` 之前、`{success, data, error}` 信封(含 403 / 422 统一改写)、`limit`(1-200)+ `offset` + `total` 或游标、字符串字段 `reject_nul`、按风格指南写文档并更新 `auth.md` 的「Key 只能访问」与 `errors.md`。

### 8.4 资源与端点(以推荐前缀示意)

| 方法 + 路径 | 能力 | 说明 |
|---|---|---|
| `GET /v1/knowledge-bases` | `kb:read` | 只列授权库:ID、名称、描述、画像、元数据 schema、文档数 |
| `GET /v1/knowledge-bases/{kb}` | `kb:read` | 单库 |
| `POST /v1/knowledge-bases/{kb}/search` | `kb:search` | 只检索不生成(§8.5) |
| `POST /v1/knowledge-bases:search` | `kb:search` | 跨多个授权库,`kb_ids[]` |
| `GET /v1/knowledge-bases/{kb}/documents` | `kb:read` | 分页 + 状态 / 元数据 / 标签 / 时间筛选 |
| `GET /v1/knowledge-bases/{kb}/documents/{doc}` | `kb:read` | 状态、阶段、`error{stage,code,message}`、元数据 |
| `GET …/documents/{doc}/content` | `kb:read` | 分页返回解析后的 Markdown(参照 OpenAI file content),带页码 |
| `GET …/documents/{doc}/chunks` | `kb:read` | 分段列表 |
| `POST …/documents` | `kb:write`(二期) | `source: {upload_id \| url \| text}`、`external_id`、`metadata`、`tags`;复用 `upl_` 上传;`Idempotency-Key`;返回 202 + 文档 + `task_id`;同内容返回 `skipped` |
| `POST …/documents:batch` | `kb:write`(二期) | 批量(上限 100) |
| `PATCH …/documents/{doc}` | `kb:write`(二期) | 只改元数据 / 标签 / 启停,不重新向量化 |
| `DELETE …/documents/{doc}` | `kb:write`(二期) | 立即对检索不可见,异步清理,返回 `task_id` |
| `POST …/documents/{doc}:reparse` | `kb:write`(二期) | 重解析 |
| `PATCH …/chunks/{chunk}` | `kb:write`(二期) | 改内容 / 启停,`If-Match` 带 revision |
| `GET /v1/kb-tasks/{task}` | `kb:read` | 统一任务查询(阶段、计数、错误) |
| `POST …/faq-entries:import` | `kb:write`(二期) | `mode` append / replace、`dry_run`,返回被拦截行 |
| 回调 | 二期 | 文档就绪 / 失败、任务完成;复用现有 webhook 签名机制 |

### 8.5 检索请求与返回

**请求**:`query`(≤2000 字符)、`top_k`(1-50,默认 10)、`mode`(hybrid / semantic / keyword)、`filters`(§6.3 DSL)、`document_ids[]`、`rerank`(`{enabled, candidates≤200}`)、`score_threshold`(只作用于重排分;缺省用库校准值)、`rewrite_query`(默认 false)、`include`(`content` / `neighbors` / `provenance`)。

**返回**:`results[{chunk_id, ref("D132#7"), document_id, document_title, document_version, content, heading_path[], location{page_start, page_end}, metadata, score, scores{vector, keyword, rerank}, match_reason}]`、`rewritten_query`、`usage{embedding_tokens, rerank_units}`、`partial_failures[]`。

**错误码**:`INVALID_REQUEST`、`KB_NOT_FOUND`(含越权)、`KB_API_DISABLED`、`CAPABILITY_REQUIRED`、`FILE_TOO_LARGE`、`UNSUPPORTED_FILE`、`TASK_CONFLICT`、`REVISION_CONFLICT`、`RATE_LIMIT_EXCEEDED`(带 `Retry-After`)、`QUOTA_EXCEEDED`。

**配额参照**:检索 20 RPS / 租户(参照 Bedrock)、写入每库 300 RPM(参照 OpenAI)、单文件 50MB、每文档元数据 ≤16 键。

### 8.6 MCP(可选,第三期)

与 HTTP API 同源同权限:`list_knowledge_bases`、`search_knowledge`、`read_document`;工具说明注入授权库清单(参照 RAGFlow)。只做一套,避免 WeKnora 三套 MCP 互不一致。

---

## 9. 待拍板

**开工前必拍**

| # | 题 | 推荐 |
|---|---|---|
| 1 | 开放 API 定位变更与第一期范围 | 第一期只开「只检索」档 |
| 2 | 对外路由前缀 | 锁定前缀收紧为 `/v1/knowledge/`,对外用 `/v1/knowledge-bases` |
| 3 | 知识库是否拆出独立权限资源 `knowledge` | 拆 |
| 4 | API key 授权模型 | 能力 × 库白名单 × 默认拒绝 |
| 5 | 中文关键词路线(应用侧 jieba vs 库内扩展) | 以 P1 评测集对比后定 |

**到对应阶段再拍**

| # | 题 | 推荐 |
|---|---|---|
| 6 | 短编号方案 | `D{doc_seq}#{chunk_index}` |
| 7 | 库目录位置 | 系统提示尾部 |
| 8 | 跨轮清理旧检索结果 | 阈值触发 |
| 9 | 引用协议是否随 P4 一期 | 随 P4 |
| 10 | 矛盾由谁确认(内部实施 / 客户) | 先内部,确认台按角色开放 |
| 11 | 未确认条目能否被检索 | 对员工可(带标记),对外与对客不可 |
| 12 | 原文件迁 OSS 与存量迁移 | 迁,三段式 |
| 13 | 上下文化切块默认开关 | 默认关,评测证明收益后按库开 |
| 14 | 物料库对客模式判定、语音识别供应商、预置类型 | 见原 B-172 §10 |
| 15 | 回调与 MCP 是否做 | 回调随二期,MCP 三期 |

---

## 10. 风险

| 风险 | 缓解 |
|---|---|
| 关键词「任一词命中」常见词候选爆量 | 停用词、候选池上限、`statement_timeout` |
| 行业术语切错 | 租户词典;不够上两字切分兜底路 |
| 重排改动波及长期记忆 | 按用途拆分,本方案只动知识库 |
| 生产扩展 / pgvector 版本与测试不同 | 开工前实查;设置失败自动降级 |
| 开放 API 扩大攻击面 | 独立能力、库白名单默认拒绝、每库开关、审计、限流;安全评审 |
| 整理层编造 / 错误合并 / 成本 | 逐句引用、严格合并、确认台、每日预算;真实资料离线验证先行 |
| AGPL 解析库合规 | 法务判断;长期替代路线 §6.2 |
| 迁移与多副本 | expand-only、三段式、代际守卫;SQL / 内存谓词契约测试 |

---

## 11. 主要出处

- Anthropic Contextual Retrieval:https://www.anthropic.com/news/contextual-retrieval
- Anthropic Effective context engineering:https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Chroma 切块评测:https://research.trychroma.com/evaluating-chunking
- Cohere 重排最佳实践:https://docs.cohere.com/docs/reranking-best-practices
- Azure 混合检索排序:https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking
- OpenAI Retrieval / Vector Stores:https://platform.openai.com/docs/guides/retrieval
- AWS Bedrock Retrieve:https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html
- AWS 多租户元数据过滤:https://aws.amazon.com/blogs/machine-learning/multi-tenancy-in-rag-applications-in-a-single-amazon-bedrock-knowledge-base-with-metadata-filtering/
- Vertex RAG Engine API:https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/rag-api
- RAGFlow HTTP API:https://ragflow.io/docs/http_api_reference
- Dify 知识库 API:https://github.com/langgenius/dify-docs (`en/api-reference/guides/knowledge.mdx`)
- FastGPT 知识库:https://github.com/labring/FastGPT (`document/content/guide/dataset/`)
- MaxKB:https://docs.fit2cloud.com/maxkb/
- Onyx:https://docs.onyx.app/developers/guides/search_api_guide
- RAGAS 指标:https://github.com/explodinggradients/ragas
- 本地:WeKnora(`internal/agent/tools/`、`internal/middleware/api_key_gate.go`、`internal/types/tenant_api_key.go`、`docreader/parser/pdf_parser.py`)、OpenViking(`openviking/server/routers/`)、PageIndex(`pageindex/agent_tools.py`)
