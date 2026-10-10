# B-171 知识库模块整体优化 —— 设计方案

> **2026-10-10 晚:本稿已并入 [B-175 知识库 v2 整体设计](2026-10-10-b175-knowledge-base-v2-design.md),结论以 B-175 为准;本稿保留作背景。**

- **状态**:设计稿,**登记不排期**(2026-10-10 用户拍板:先出方案记入 ROADMAP,什么时候做等指令)。
- **性质**:这是方案,不是实施计划。每一批开工前另写 `docs/superpowers/plans/` 计划,并按当时代码重新核对本文引用的行号。
- **来源**:2026-10-10 一次会话里的现状评估(含测试环境实查)、三个开源仓库对标(OpenViking / PageIndex / WeKnora,本地 `~/src/github/`)和 WeKnora 四路源码深挖。下文 file:line 除注明外均为 expert-work 当前 main(`4124a112`)。

---

## 1. 要解决的问题

**用户原话**:客户的原始资料格式多种多样,而且结构混乱。

一个典型资料包:几年前的产品手册 PDF(部分页是扫描件)、新的价目表 Excel、培训 PPT、聊天截图、员工自己整理的 Word。直接入库会出现:

1. **读不出来**:扫描页、截图、文档里的图片 → 内容直接丢。
2. **同一件事散在多份文件**:检索一次只拿到几段碎片,拼不完整。
3. **同一个东西多种叫法**:检索把它当成几件事。
4. **信息互相矛盾**:旧手册和新价目表价格不同,模型随便挑一个说。
5. **智能体不知道库里有什么、只能做一种动作**:不会去查,查了也没法看上下文、读全文。

现状的流程是「原样解析 → 切块 → 检索」,**没有清洗和整理这一步**;检索层本身也有硬伤(§2)。

**有效性的证据**:测试环境唯一一份知识库是人工整理成「一条一事、带类别 / 常见问法 / 事实答案」格式的资料,那次真实运行里检索第一条就命中正确条目;团队目前也靠技能(`process-faq`、`sop-pre-intake` 等)离线人工整理客户资料。**说明「先整理、再检索」有效,只是没进平台。**

## 2. 现状(2026-10-10 实查)

**使用量**(测试环境只读 SQL):1 个知识库、1 份 `.md`(41 段)、1 个智能体挂载、累计 1 次运行调用过 `knowledge_search`。生产未查。

**已核实的缺陷**:

| # | 缺陷 | 证据 | 后果 |
|---|---|---|---|
| D1 | 关键词检索所有词必须同时出现 | `persistence/knowledge/sql.py:798` `plainto_tsquery`;测试库用真实运行里模型发出的两条查询实测:全部同时出现 **0 命中**,任一出现 29 / 4 命中 | 「混合检索」实际≈纯向量,药名 / 编号 / 型号兜不住 |
| D2 | 中文切词不稳 | jieba 实测「吃代餐」「粉含」「二甲 / 双 / 胍」;无行业词典;无停用词 | 即使改成任一词命中也有噪声 |
| D3 | 智能体不知道库里有什么 | 工具说明是固定一句英文(`orchestrator/tools/knowledge.py:292`),不带库名 / 描述 / 文档清单 | 何时该查全靠提示词手写 |
| D4 | 只有一个检索动作 | 同上文件,只有 `knowledge_search(query, limit)` | 无法看前后文、读全文、列文档、按文档过滤;引用只有「文件名#段号」,无页码 |
| D5 | 重排默认让聊天模型排序且丢分数 | 默认 `rerank_model="qwen-plus"`(`settings.py:298`)走 `LLMReranker`,每段截 600 字;专用重排分支 `top_n=top_k` 且不回传分数(`orchestrator/llm/rerank.py:114`) | 无法设阈值;排序质量一般 |
| D6 | 相似度阈值只卡向量命中 | `tools/knowledge.py:193` | 关键词命中不受过滤 |
| D7 | 扫描件 / 图片读不出 | `control_plane/knowledge/parsing.py`:按**整份文档**判断字数是否过少,无 OCR;图片丢弃 | 混合 PDF 的扫描页静默丢失 |
| D8 | 入库第 5 次尝试时崩溃 → 永久卡在 processing | 可认领条件 `attempts < max_attempts`(`sql.py:49-63`),终态失败只在「抛异常且 attempts≥max」时写(`recovery.py` `_drive`) | 文档永远不就绪也不报失败 |
| D9 | 恢复任务批量认领、串行处理 | `recovery.py` `run_once` 的 for 循环 | 排在后面的租约先过期,被别的副本重复处理 |
| D10 | 同名重传时旧任务会把旧内容写回 | `upsert_document` 把 attempts 归零并提前删 chunk(`sql.py:378-396`);`set_document_status` / `replace_chunks` 写回只按 tenant+id | 旧内容覆盖新内容并标就绪;重传期间文档搜不到 |
| D11 | 同内容重传仍重新嵌入 | `content_sha256` 算了但不比较 | 白花嵌入费用 |
| D12 | 切块重叠按 token 截断,中文出乱码 | `chunking.py` `_tail_text` / `_split_by_tokens` 对 cl100k token 切片后 decode;实测一句中文 39 种尾部切法 13 种含 U+FFFD | 相邻段开头出现乱码字符 |
| D13 | 上传无大小上限、原文件存 PG bytea | `api/knowledge.py` `upload_document` 直接 `await file.read()` | 大文件撑内存、撑库 |
| D14 | 评测只有 11 条手造用例 | `tools/eval/datasets/rag/m0_baseline.yaml`,语料每条 2~3 句、跳过解析与切块 | 任何改动都量不出真实效果 |
| D15 | 向量检索在大表上可能对小库返回不足 | HNSW 全局索引 + 租户 / 库过滤,未设 `hnsw.iterative_scan`(推断,数据量未到,**未实测**) | 规模上来后的隐患 |

**做得对、保留的**:结构感知切块(标题路径前缀、表格不断开、超长表格按行拆带表头)、入库 CAS 租约 + 崩溃恢复骨架、换嵌入模型重建索引、检索 SQL 恒带 `tenant_id` 且库列表为空直接返回空(隔离比 WeKnora 好)、控制台检索测试页。

## 3. 设计原则

1. **两层知识**:原始资料层(原样保留,用于出处与兜底检索)+ 整理条目层(清洗后的标准条目,检索优先)。原始资料永不因整理而丢弃。
2. **每句事实必须有出处**:整理产出的事实必须引用到原文段落,无出处不写入。这是防编造的核心。
3. **矛盾交给人,不替客户决定**:平台只负责发现和摆出矛盾(含来源与日期),由人确认。已确认、人工改过的内容,流水线只能提修改建议,不能覆盖。
4. **平台默认,不加开关**:基础可用性不靠租户去配([[platform-defaults-over-configuration]]);开关只用于收窄或成本高的可选能力(如图片向量)。
5. **平台文本零租户内容**:工具说明、整理提示词、示例一律泛化,不出现任何客户的产品名 / 文件名。
6. **先量再做**:先建真实基准(§5.8),每批用它验收;收益是假设的批次(父子块、图片向量)以基准结果决定做不做。
7. **多副本语义**:所有后台任务按多副本设计(DB CAS / 代际守卫 / advisory lock),不引入 Redis 任务队列。

## 4. 总体架构

```
接入 ──► 识别 ──► 清洗 ──► 切块/索引(原始资料层) ──► 检索 ──► 智能体使用
                          │                          ▲
                          └──► 整理(条目层)──► 人工确认 ┘(条目优先返回)
```

| 层 | 职责 | 对应批次 |
|---|---|---|
| 接入 | 上传(批量 / 文件夹)、URL 导入、OSS 目录同步 | 批 5 |
| 识别 | 逐页判扫描 → OCR;图片描述 + 图中文字;表格;聊天记录;页码定位 | 批 3 |
| 清洗 | 页眉页脚、重复文件、空内容、版本 / 日期识别、文档画像 | 批 3 |
| 原始资料层 | 切块、嵌入、关键词索引 | 批 0 / 1 / 3 |
| 整理(条目层) | 抽取条目 → 去重合并 → 发现矛盾 → 人工确认 → 发布 | 批 4 |
| 检索 | 关键词 OR + 排序、向量、融合、专用重排、条目优先 | 批 1 |
| 智能体使用 | 三件套工具、库目录、短编号、页码引用 | 批 2 |
| 评测 | 真实资料 + 真实问题的基准 | 贯穿,批 1 前置 |

## 5. 各模块设计

### 5.1 入库可靠性(批 0)—— 修 D8~D11、D13

- **代际守卫**:`knowledge_document` 加只增不减的 `ingest_generation`;每次认领与重传都 +1,认领时把代际交给 worker;`replace_chunks` / `set_document_status` / `mark_document_failed_terminal` 写回加条件 `status='processing' AND ingest_generation=:mine`,不匹配就放弃写回。**不能用 attempts 当守卫**(重传会把它归零)。
- **终态巡检**:恢复任务每轮先把 `processing 且租约过期且 attempts≥max` 的行置失败。
- **认领节奏**:恢复任务一次只认领可并发处理的数量,或处理中按租约 1/3 周期续约。
- **重传不留空窗**:去掉 `upsert_document` 里提前删 chunk;新 chunk 由 `replace_chunks` 一个事务整体替换。内容哈希相同且已就绪 → 直接返回「未变化」。
- **并发上限**:快速通道 `asyncio.create_task` 加全局信号量;嵌入调用补 429 退避。
- **上传上限**:按配置拒收超大文件(413),流式读入。原文件迁 OSS 放批 5(需迁移存量)。
- **验证**:每条修复配测试,先变异让它变红再恢复;D10 用两个并发 worker 的集成测试复现「旧覆盖新」。

### 5.2 检索(批 1)—— 修 D1、D2、D5、D6、D15

**关键词**(不装扩展,PG16 自带能力):

1. 查询端改用 jieba 搜索模式切词(实测其输出包含精确模式的词,**现有索引不用重建**)+ 中文停用词 + 单字过滤 + 词数上限,拼成「任一词命中」的 `to_tsquery('simple', 'a | b | …')`,用 `ts_rank_cd` 排序。
2. 行业用户词典(`jieba.load_userdict`,写入端与查询端共用;改词典后需回填 `content_tsv`)。
3. 进阶:纯 SQL 实现 BM25(文档频率用 GIN 实时计数并缓存,词频取 tsvector 位置数),候选池上限 + `statement_timeout`。
4. 可选第三路:中文两字切分的 tsvector 列,专治词典缺词(加列 + 回填;以基准决定)。
5. **RDS 可用中文扩展(2026-10-10 测试 RDS 实查)**:`pg_available_extensions` 列出 `pg_jieba` 1.2.0、`zhparser` 1.0、`pg_bigm` 1.2、`rum` 1.3(均可装、未装);已装 `vector` 0.8.0.2、`pg_trgm` 1.6;**无 `pg_search`**。生产实例同系列,开工前须同样实查。这意味着方案 1~3 之外多一条路:库内中文分词(`pg_jieba` / `zhparser` 建 text search configuration,写入与查询两端同一分词)+ `rum` 索引(带位置信息,可做排序与短语距离)或 `pg_bigm`(两字切分,不怕词典缺词)。批 1 计划阶段用基准对比「应用侧 jieba + OR」与「库内分词扩展」再定;两者都不行再做纯 SQL BM25。**不用 ParadeDB/pg_search**(RDS 不提供,许可也需核)。

**向量**:查询事务内 `SET LOCAL hnsw.ef_search` + `SET LOCAL hnsw.iterative_scan = strict_order`(pgvector ≥0.8;测试 RDS 实查 0.8.0.2,生产版本待核;设置失败则去掉重跑),先取 `clamp(2k, 100, 200)` 候选再过阈值。

**融合**:加权 RRF(k=60;初值向量 0.7 / 关键词 0.3,以基准重调),每路每个列表各自取最好名次。

**重排**:

- 默认改用 DashScope 专用重排模型(`gte-rerank-v2` 或 `qwen3-rerank`;**请求体是字符串数组还是 `{text}` 对象需核实**);`top_n` 填候选总数,回传分数;聊天模型重排降为兜底。
- 送重排的文本 = 文档标题 + 标题路径 + 清洗后的正文。
- 阈值三档:正常 → 阈值×0.7 且不低于 0.3 → 仅保留最好一条(≥0.15)→ 全拒。综合分 `0.6×模型分 + 0.3×检索分 + 0.1×来源权重`。
- MMR(λ=0.7,jieba 词集 Jaccard)去掉内容重复的结果。
- 失败 / 超时退回融合顺序;**检索失败与「没搜到」分开报告**(失败不能当作库里没有)。

**参考**:WeKnora `reranking/rerank.go`、`mmr.go`、`knowledgebase_search_fusion.go`、`repository/retriever/postgres/repository.go`(只借思路与参数,它本身有关键词阈值不生效、扩展结果分数虚高等缺陷,不照搬)。

### 5.3 智能体工具与上下文(批 2)—— 修 D3、D4

**三件套工具**(英文说明、平台默认注册,`KnowledgeSpec` 不加开关;我们已有同名的工作区工具 `read_document`,新工具不能沿用这个名字):

| 工具 | 参数 | 作用 |
|---|---|---|
| `knowledge_search` | `query, mode?(hybrid/semantic/keyword), bases?, limit?` | 保留原名兼容现有配置;说明里要求查询写成完整问句,标识符用 keyword 模式 |
| `knowledge_read` | `id, offset?, limit?, query?, context?` | `D<n>` 从头读 / 翻页;`D<n>#<k>` 读某段及前后 k 段;`query` 在文档内找;**所有路径都受输出预算约束** |
| `knowledge_list_documents` | `base, keyword?, page?, page_size?` | 列文档及一句话摘要,按名称找文档 |

- **短编号**(待拍板,推荐):数据库确定性编号 `D{doc_seq}#{chunk_index}`。`doc_seq` 为租户内单调序号、重传保留;`chunk_index` 现由 `enumerate` 连续生成(`ingestion.py`),读邻居直接按序号。不含空格,穿过输出围栏不被改写([[llm-retypes-opaque-strings]]),跨轮稳定,不破坏缓存;不照搬 WeKnora 每请求现编的句柄(要求每次调用前改写全部历史)。
- **库目录**(待拍板,推荐):放系统提示尾部的 `<knowledge_catalog>`,包在不可信围栏内,写明「目录是导航提示,不是证据」。只放变化慢的字段:K 编号、库名、描述、库画像(主旨 / 主题 / 典型问题)、文档数分档;每库约 600 token,最多 20 个库,超出写「另有 N 个」。构建缓存键带目录哈希。**子智能体同样注入**(B-37 教训:worker 不继承父提示)。
- **辅助信息走 `ToolResult.notice`**(围栏外):省略条数、模式降级、下一页 offset、「失败≠没找到」。
- **跨轮清理**(待拍板):给 B-126 的旧结果清理加「知识引用」找回方式(stub 列出命中编号,可用 `knowledge_read` 重读);推荐沿用阈值触发,不学 WeKnora 每轮无条件清。
- **引用**(待拍板是否一期):`[D132#7]` → 校验必须出现在本次运行的知识工具结果中 → SSE / 会话条目层展开为「文档名 · 第 N 页」链接。涉及流式暂存与前端,可单独一期。
- **提示词**:系统提示加两条通用规则——绑定了知识库时先查再答、目录和摘要只是导航、精确数字和引文要核原文。

**参考**:WeKnora `internal/agent/tools/{search_knowledge,read_document,list_documents}.go`、`internal/modelcontext/`、`prompts.go`、`grounding_prompt.go`。避开其缺陷:看前后文无预算、文档清单摘要不截断、不可信文本未转义直接拼进结构。

### 5.4 识别与清洗(批 3)—— 修 D7、D12

- **逐页扫描判定**:用 pymupdf4llm 自带的 `ocr.analyze_page(page)["needs_ocr"]`(本仓库 venv 已有),或移植 WeKnora 的面积比规则(图片面积 ≥50%,或字数 <10 且图片面积 ≥10%)。
- **OCR**:200DPI、长边 ≤2000px、JPEG 85 渲染;走平台视觉模型;用扫描页版式提示词(要求 Markdown 表格、空页回复固定串);过滤「无文字」类空回复。**先 OCR、把文字放回该页位置,再切块**(WeKnora 是切块后整页挂一个子块,粒度太粗)。按页图片哈希缓存结果,恢复重跑不重复花钱。单页失败留占位、整份不失败。
- **图片**:按尺寸 / 面积 / 跨页重复(logo)/ 每份上限筛选;一次视觉调用同时「观察 + 描述」,只有大段文字或图表才追加 OCR;描述与图中文字以引用块写回原位置再切块;chunk 记录 `image_refs`(原图存 OSS),回答时图画类原图可交给视觉模型(每次 ≤3 张)。
- **页码定位**:`pymupdf4llm.to_markdown(page_chunks=True)` 记录「行区间 → 页码」,切块时按 markdown-it 行区间映射到页码集合,存 `source_locator` JSONB。**页码不写进正文**(避免污染嵌入与关键词)。
- **表格 / Excel**:空表头行用首行数据补;Excel 首行作表头;表格块之后的重叠只取到整行边界。
- **切块修复**:重叠改成按语义边界(段落 > 换行 > 句号)截取,修 U+FFFD;同一父标题下相邻短节合并到约一半预算;`_hard_split` 保留原换行。
- **聊天记录**:识别「谁 · 时间 · 内容」结构,按会话段落切块。
- **清洗**:跨页重复的页眉页脚;完全重复文件(哈希)提示;空内容 / 纯占位块不嵌入。
- **文档画像**(每份 1 次模型调用,输入采样约 8k 字,不传文件名防编造):摘要、一句话主旨、主题词、文档类型、典型问题、**生效日期 / 版本**(整理层判断新旧要用)。库画像由文档画像确定性汇总 + 一次小调用生成,汇总哈希不变不重算。
- **许可备注**:pymupdf / pymupdf4llm 为 AGPL 或商业双许可(venv METADATA 实证),以网络服务方式使用的合规性需法务判断;长期替代路线是 pypdfium2(BSD/Apache,已在 venv)+ 自补表格识别。**不抄 WeKnora 的 PDF 解析器**:文字页无表格识别,且清理图表残字的规则会整片删除纯数字行(化验单 / 财务表危险)。

### 5.5 整理成条目 + 人工确认(批 4)—— 解决「资料乱」的核心

**流程**(源文档就绪后自动触发,按库串行):

1. **抽取**:按文档画像与平台通用模板,抽出「一事一条」的候选条目。通用条目类型:事实(产品 / 价格 / 政策 / 规则)、问答、流程、话术;字段:标题、类型、正文事实、适用条件、常见问法、生效时间。
2. **逐句引用**:逐批扫描段落,为每个候选标注引用段落(用短编号映射回 chunk);无引用的事实不写入。
3. **三层去重**:标题规范化后完全相同直接合并 → 字面相似度(pg_trgm)取前 5 → 严格模型判断(「相关≠相同」,同系列不同型号 / 不同套餐不合并,拿不准不合并)+ 代码校验「合并目标必须在候选集内」。
4. **合并与矛盾**:合并提示词遵循「整理不创作、不推断」。同一事实来源之间数值或说法不一致时**不裁决**,生成一条待确认矛盾,附各方原文、来源文档、文档生效日期 / 上传时间,并给出「较新来源」提示。
5. **人工确认台**(控制台):新条目、待确认矛盾、低把握内容三个列表;确认 / 修改 / 驳回;人改过的条目锁定(`last_edit_source='user'`),之后流水线只提修改建议(diff),不覆盖。
6. **发布**:条目作为 `knowledge_document(source_type='entry')` 走现有切块与检索,删除 / 开关 / 引用全部复用;检索时条目优先(加权或先查条目层,以基准定)。未确认的条目是否可被检索(带「未确认」标记)作为待拍板项。
7. **维护**:源文档更新 → 受影响条目重算并生成修改建议;源文档删除 → 只来自它的条目删除,多来源条目把该文档被引用过的原文交给模型撤回对应内容(不是只给一句摘要)。

**数据**:

```sql
-- knowledge_document 增加:source_type('file'|'url'|'oss'|'faq'|'entry'), entry_status('draft'|'confirmed'|'rejected'),
--   entry_slug, entry_aliases jsonb, entry_version int, last_edit_source('pipeline'|'user')
-- UNIQUE(tenant_id, kb_id, entry_slug) WHERE source_type='entry'
CREATE TABLE knowledge_entry_source   (entry_doc_id, source_doc_id, chunk_ids uuid[], PRIMARY KEY(entry_doc_id, source_doc_id));
CREATE TABLE knowledge_entry_revision (entry_doc_id, version, content, edit_source, editor, created_at, PRIMARY KEY(entry_doc_id, version));
CREATE TABLE knowledge_entry_conflict (id, tenant_id, kb_id, entry_doc_id, claim, candidates jsonb, status('open'|'resolved'|'dismissed'), resolved_by, resolved_at);
CREATE TABLE knowledge_entry_job      (id, tenant_id, kb_id, source_doc_id, op('ingest'|'retract'), status, attempts, generation, lease_until, last_error);
```

全部带 `tenant_id` 与 RLS 策略;任务沿用 CAS 租约 + 代际守卫;按库 `pg_advisory_xact_lock` 串行。

**成本**:每份源文档约 3 次 + 受影响条目数次模型调用;每库每日预算上限。

**开发前先验证**:取一家客户的真实资料包离线跑「抽取 → 去重 → 摆矛盾」,量:条目数与人工抽查准确率、发现的矛盾数、用条目检索 vs 原始资料检索的命中率。数字出来再定范围。

**参考**:WeKnora Wiki 管线的抽取 / 逐段引用 / 三层去重 / 合并提示词(`internal/agent/prompts_wiki.go`,冲突规则见第 326 行)、删除撤回流程。**不抄**:页面不进检索、模型自行裁决矛盾、无确认流程、人工编辑会被模型重写覆盖(其乐观锁失效)、页面重写失败被静默吞掉、目录规划 / 交叉链接 / Redis 锁。

### 5.6 FAQ 类型、数据源接入、父子块、标签(批 5,按需)

- **FAQ 类型**:文档级 `source_type='faq'`;主条目一行 chunk(问 + 答,metadata 存标准问 / 相似问 / 反例问 / 多答案 / 回答策略);每个相似问一行子 chunk,各自嵌入与关键词索引,命中后折叠回主条目;反例问用「归一化比较 + 向量相似度阈值」剔除;xlsx/csv 导入,按内容哈希幂等,问法行 ID 用 `hash(主条目, 规范化问法)`(避开 WeKnora 序号 ID 导致删不掉旧向量的 bug);保留关键词检索(型号 / 价格纯向量很弱)。适合销售话术与 FAQ 类资料。
- **数据源**:URL 导入(每一跳 SSRF 校验;按 ETag / Last-Modified / 哈希判断变化,到期重新抓取)、OSS 前缀同步(增量 + 删除对账,凭据走金库);飞书等连接器按需。原文件迁 OSS,PG 只存元数据。
- **父子块 / 邻居扩窗**:以基准决定;扩窗按 `(document_id, chunk_index±k)`,不需改表;父子块新表 `knowledge_parent_chunk`。
- **标签与自动标签**:`knowledge_tag` / `knowledge_document_tag`(带 `tenant_id`);自动标签与文档画像合成一次调用;检索支持按标签过滤。
- **文档元数据**:`title`(与文件名分开)、`folder_path`、`enabled`、`custom_metadata`、`mime`、`size_bytes`;chunk 级 `enabled`、`chunk_type`、`parent_chunk_id`、`heading_path`、`content_hash`。

### 5.7 图片向量(批 6,条件触发)

- **触发条件**:基准证明「图转文字」找不到某类问题(例如查询本身是图片,或内容只有视觉形态)。
- **设计**:独立表 `knowledge_image`(原图 OSS 地址、哈希、页码、描述、图中文字、`embedding vector(D)`、`embedding_model`),D 由选定的多模态模型决定,**与平台共享的 1024 维列无关**(该列长期记忆也在用,不同模型空间不可混放)。查询用多模态模型的文本侧再嵌入一次,作为 RRF 第三路;跳过扫描页与文字密集截图;纯文本重排对图画类命中要有兜底保留;库级开关默认关。
- **不学 WeKnora**:它把图片向量与文本放同一列,前提是整个库换成多模态嵌入模型;且其混合 PDF 只要有一页扫描就把全部图片当扫描页处理,图片向量全被跳过(`docreader/parser/pdf_parser.py:1753`)。
- 可先试多模态重排(DashScope `qwen3-vl-rerank`,效果未核)作为更便宜的中间方案。

### 5.8 评测基准(贯穿,批 1 前置)

- 用一家客户的真实资料(含扫描件、表格、聊天记录)+ 50~100 道真实问题,人工标注期望段落 / 条目。
- 走完整入库链路(解析 + 切块),不再用预切好的语料。
- 指标:recall@k、MRR、答案正确率;固定回归用例「自然语言长问句至少 1 命中」。
- 扩展 `tools/eval/rag.py`;每批前后各跑一次,数字写进该批 PR。

## 6. 数据模型变更汇总

| 表 | 变更 | 批次 |
|---|---|---|
| `knowledge_document` | `ingest_generation` | 0 |
| `knowledge_document` | `doc_seq`、`summary`、`profile` jsonb | 2 / 3 |
| `knowledge_base` | `generated_profile` jsonb | 3 |
| `knowledge_chunk` | `source_locator` jsonb、`image_refs` jsonb | 3 |
| `knowledge_document` | `source_type`、条目相关列 | 4 |
| 新表 | `knowledge_entry_source / revision / conflict / job` | 4 |
| `knowledge_document` / `knowledge_chunk` | 元数据列、`chunk_type`、`parent_chunk_id`、`heading_path`、`content_hash`、`enabled` | 5 |
| 新表 | `knowledge_tag`、`knowledge_document_tag`、`knowledge_parent_chunk` | 5 |
| 新表 | `knowledge_image` | 6 |

所有新表带 `tenant_id` + RLS 策略(纳入 `test_rls_policy_coverage`);SQL 与内存两套 store 的谓词保持同义;迁移走三段式发布。

## 7. 分批路线

| 批 | 内容 | 依赖 | 粗估 |
|---|---|---|---|
| 0 | 入库可靠性(§5.1) | 无 | 1~1.5 人日 |
| 基准 | 真实资料基准(§5.8) | 需一家客户资料包 | 2~3 人日 |
| 1 | 检索(§5.2) | 基准 | 3~5 人日 |
| 2 | 智能体工具与上下文(§5.3) | 批 1;待拍板 1~4 | 5~8 人日 |
| 3 | 识别与清洗(§5.4) | 批 0 | 5~8 人日 |
| 4 | 整理成条目 + 确认台(§5.5) | 批 3 的文档画像;离线验证 | 验证 2 人日 + MVP 5~8 人日 + 控制台 |
| 5 | FAQ / 数据源 / 父子块 / 标签(§5.6) | 按需 | 各 3~5 人日 |
| 6 | 图片向量(§5.7) | 基准证明需要 | 6~10 人日 |

粗估来自会话中的源码分析,未细排;开工前按计划重估。批 3 与批 4 是用户「资料乱」痛点的直接解法,建议在批 0~2 之后连续做。每个 PR 同 PR 写真栈用例,随统一发测试验收。

## 8. 对标结论摘要

| 仓库 | 结论 | 借什么 |
|---|---|---|
| WeKnora(腾讯,MIT) | 不整体接入(Go 72 万行、与我们租户 / 智能体 / 沙箱 / 记忆重叠、依赖 ParadeDB) | 智能体三件套与上下文设计、OR+BM25 思路、专用重排三档阈值 + MMR、逐页扫描 OCR 与提示词、页码定位、文档 / 库画像提示词、Wiki 的抽取 / 引用 / 去重 / 合并提示词、FAQ 数据模型 |
| PageIndex(MIT) | 不接入(单用户本地形态,开源版无 OCR / 跨文档;中文标题归一化为空串;98.7% 实为其商业产品 Mafin 2.5 成绩) | 「先看目录、再按页读」的工具契约、页级引用、节点摘要调度思路 |
| OpenViking(字节,AGPL) | 不接入、不抄代码(许可;本地后端只能单副本;宣传的分层检索 09-29 已删) | 检索 → 定位 → 读取的工具组合、专用重排 + 候选 2×limit、URL / 批量导入与定时同步 |

## 9. 待拍板

1. **短编号方案**:数据库确定性编号 `D{doc_seq}#{chunk_index}`(推荐,需迁移加 `doc_seq`),还是线程级句柄表。
2. **库目录位置**:系统提示尾部(推荐),还是每轮平台上下文。
3. **跨轮清理旧检索结果**:阈值触发(推荐),还是每轮无条件。
4. **引用协议**:是否放进批 2 第一期。
5. **矛盾由谁确认**:内部实施人员还是客户本人 → 决定确认台做成内部工具还是客户界面。
6. **未确认条目能否被检索**(带「未确认」标记),还是确认后才发布。
7. **批 3 / 批 4 是否提前**到批 1 / 2 之前(资料乱是当前痛点;但检索与工具不修,整理出的条目也用不好)。

## 10. 风险

- 关键词 OR 遇常见词候选爆量 → 停用词、候选池上限、`statement_timeout`。
- jieba 词典缺医疗 / 行业术语 → 用户词典;不够再上两字切分路。
- 生产 pgvector 版本与生产 RDS 可用扩展(测试 RDS 已实查,见 §5.2)、DashScope 重排请求体形状 → 开工前先核实。
- 整理层成本与编造 → 逐句引用 + 严格合并规则 + 每日预算 + 人工确认;用人工整理稿做对照评测。
- AGPL 解析库的合规 → 法务判断;长期替代路线见 §5.4。
- 新表的 RLS 覆盖、SQL / 内存 store 谓词同义、多副本并发 → 沿用现有测试规矩,每条新断言先变异自证。
