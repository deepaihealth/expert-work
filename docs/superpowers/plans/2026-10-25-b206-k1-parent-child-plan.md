# B-206 K-1 父子块逐 task 计划(与 K-3 合成一个单元)

- **状态**:2026-10-25 用户拍板「K-3 与 K-1 合成一个单元一起过闸门」(总计划 §3.1)。本计划接在分支 `feat/kb-k3-rechunk`(K-3 修正版)之上。
- **设计依据**:`docs/superpowers/specs/2026-10-21-b206-kb-retrieval-loss-driven-optimization-design.md` §5.1「K-1」与 §6 前端。本文只写「怎么落地」和与设计不同的取舍。
- **代码核对**:2026-10-25,`wt-k3` HEAD `91bca5cfc`。file:line 来自当日源码。
- **全局约定**(沿用波 2 计划):测试先行;每条新断言「改坏 → 红 → 恢复 → 绿」自证;迁移 expand-only;SQL 与内存两套存储谓词同义;真栈用例写进 `docs/runbooks/pending-test-release-acceptance.md`。

---

## 0. 闸门(这个单元怎么算过)

- 开发集两个问题集(主集 `huada_v1.yaml`、真实问法集 `huada_v1.chat.yaml`),对照臂 = 主干 `519ebb0`(已有结果,不重跑)。
- **主判据 = `context_hit`(答案原文落在交给模型的文本里)**,两个集都不比主干差;同时报「父段级 MRR@10」和每次检索交给模型的平均 token 数(父段更长,命中率上升有一部分是多给了字,要摆出来)。
- 底线:文档命中@10 ≥ 0.95;B-200 那 4 处「原文找不到」保持恢复;「原来对、现在错」逐题列。
- 真实问法集 ≥ 0.846 在步 2 全部做完(加 K-2)时再验(10-24 拍板)。
- 呈现方式变了,合入前再跑一次智能体层 60 题(约 ¥15,跑前报价)。

**10-26 结果:过闸门**(开发集 ¥7.58;智能体层两臂都在当前代码上重跑 ¥24.67 —— 10-22 之后检索改过,老结果不能当对照):

| | 主集 主干 → 本单元 | 真实问法集 主干 → 本单元 |
|---|---|---|
| `context_hit`(主判据) | 0.976 → **0.983**(+3 / −1 题) | 0.921 → **0.928**(+6 / −5 题) |
| MRR@10(主干段级,本单元父段级) | 0.943 → 0.951 | 0.804 → 0.798(19 升 22 降) |
| `chunk_coverage@10` | 0.907 → 0.923 | 0.743 → 0.773 |
| 文档命中@10 | 0.973 → 0.976 | 0.980 → 0.980 |
| 锚文本不存活 | 0 → 0 | 4 → 1(B-200 那 4 处保持恢复;新增 `chat_qa-0010` 跨两个整块) |
| 每次检索交给模型的字数 均值 / P90 | 2,428 / 2,885 → **4,655 / 5,323** | 2,239 / 2,863 → **4,844 / 5,710** |

- 「原来对、现在错」(`context_hit`):主集 `hard_cross-0008`;真实问法集 `chat_qa-0060`、`0072`、`0108`、`0122`、`0149`。两集的净变化都在噪声内(符号检验 p = 0.62 / 1.00),结论只能是「不比主干差」。
- 每次检索约 4.9k token,`[已截断]` 0 条;整块 9,913 行 + 自成整块的小段 155 个,整块 token p50 854 / max 1,014;小段 45,128 个,p50 225 / max 391。
- 智能体层 60 题(主干 → 本单元):有答案 答对 29 → 30;编造 3 → 3,两臂编造的题**完全不重叠**(同一题两次跑结果不同的噪声);资料没写 正确说没写 28 → 24(「其它」3 = 没拒答也没编造)。答题模型花费 ¥4.31 → ¥6.71(+56%)。
- 真实问法集 MRR 离 0.846 仍远,交给 K-11(入库补上下文,10-25 已定提前)。

## 1. 与设计不同的取舍(实现时以本节为准)

1. **先切父段、再在父段里切子段**(Dify 同款),不是「子段切完再打包」。理由:K-3 的子段会重复引导行、表头、关键列和一句重叠,从子段拼回父段会带重复;从原文块拼父段就没有。子段永远不跨父段,父子关系天然成立。
2. **父段上限先用常量 1,024 token**,不做库级设置(设计写「库级可调 512~2,048」)。等有真实需要再开,开关只用来收窄(平台默认优先)。
3. **子段在父段里的字符区间先不存**。前端高亮先按「子段正文在父段里能找到就高亮,找不到整段淡色」处理;精确区间记后续。
4. **没有编辑子段的接口**(全仓没有写 `edited` 的地方),设计里「子段被编辑后重拼父段」先不做,记后续。
5. **结果预算按字符算**:每次 `knowledge_search` 交给模型的正文合计 ≤ 10,000 字符,低于工具输出外置闸 `EXTERNALIZE_MIN_CHARS = 12_000`(`orchestrator/tools/overflow.py:85`),不用给知识库开例外。

## 2. 数据形状

- `ChunkType.PARENT = "parent"`(`packages/expert-work-protocol/src/expert_work/protocol/knowledge.py:246`)。另加 `SEARCHABLE_CHUNK_TYPES`(除 `PARENT` 外的全部类型),所有召回 SQL 用白名单,不用排除法。
- 父段行:`chunk_type='parent'`、`embedding` 为空、`content_tsv` 为空、`chunk_index` 在父段自己的编号空间(0..m-1)、`heading_path` = 父段首个子段的。
- 子段行:与今天一样,`chunk_index` 0..n-1 连续;`parent_chunk_id` 指向父段行。
- **只有一个子段且与父段正文、标题路径都相同 → 不建父行**,子段 `parent_chunk_id` 为空,检索时自己就是父段(WeKnora 同款,短文档省一份正文)。
- `chunk_count`(文档 / 库统计、「可服务」判断)仍只数子段。
- `CHUNKER_VERSION` `"k3"` → `"k3k1"`,`chunk_params_hash` 把父段上限算进去 —— 测试环境重新入库时所有文档都会被标「参数已变」。

## 3. Task 列表

### T1 切块器出父段(control-plane,`knowledge/chunking.py`)

- 新增 `ParentChunk(body, heading_path, children: tuple[TextChunk, ...])` 和 `chunk_markdown_parented(...) / chunk_markdown_semantic_parented(...)`;旧的 `chunk_markdown` / `chunk_markdown_semantic` 保持签名,内部改为把各父段的子段按顺序摊平(行为对现有调用方不变,除了「子段不跨父段」带来的边界差异)。
- 父段怎么凑(在 `_Block` 序列上做,复用 `_prepare_block` / `_is_lead_in`):
  1. 按文档顺序把**整块**装进当前父段,装不下(含插入的标题行)就收口;
  2. 一级或二级标题变化、且当前父段已 ≥ 上限一半 → 提前收口;
  3. 引导行(`_is_lead_in`)不能是父段最后一块,跟下一块进同一父段;
  4. 单块就超过父段上限(整张表、超长段落)→ 先用 `_prepare_block(block, 父段上限)` 切成父段大小的几块(表格每块带表头、超长行续段带关键列 —— K-3 已有的规则),每块单独成父段或与相邻块打包;
  5. 父段正文 = 各块原文按顺序用空行连接;块的标题路径相对上一块变化时,先插入变化出来的那几级标题文字(第一块相对父段标题路径);
  6. 子段 = 对该父段的块序列跑现有 `_pack_blocks`(子段上限、重叠、语义断点、短节合并都照旧,只是不跨父段)。语义断点的块序号按父段内重新编号。
- 测试(`tests/test_knowledge_chunking.py` 等 + `test_knowledge_doc_forms.py:516` 的 `TODO(B-206 K-1)` 占位):
  - 每个子段都属于且只属于一个父段;子段顺序摊平后与 `chunk_markdown` 输出一致;
  - **父段正文包含其所有子段的全部原文字符**(去掉标题路径与重叠后),且不含重复的表头 / 引导行;
  - 父段 token ≤ 1,024(单块超限时按块切后也满足);
  - 一级 / 二级标题变化提前收口;引导行不落在父段末尾;
  - 文档形态测试集每种形态(Ⓐ~Ⓕ)断言父段形状;
  - 「每个标题文字都出现在某个子段里」继续成立(K-3 的测试),并加「也出现在某个父段里」。

### T2 存储(persistence 包 + 迁移 0175)

- 迁移 `0175_kb_k1_parent_chunks.py`(down_revision `0174_kb_k3_chunking`):
  - `knowledge_chunk.embedding` 去掉 NOT NULL(HNSW 不索引 NULL);
  - 唯一约束 `(document_id, chunk_index)` 换成 `(document_id, chunk_type, chunk_index)`(新约束先建、旧约束后删);
  - 配迁移测试(仿 `tests/test_migration_0174_kb_k3_chunking.py`)。
- ORM `models/knowledge.py:251` `embedding` 改 nullable;`:285` 约束同步。
- 协议 `KnowledgeChunk.embedding`(`protocol/knowledge.py:485`)改为可空。
- SQL 存储 `knowledge/sql.py`:
  - 写:`_chunk_row`(:479)父段写 `embedding=None`、`content_tsv=None`;`replace_chunks`(:1503)先插父段再插子段;
  - 召回加白名单:`search_scored` 条件(:1660)、`_kb_hits` 子查询(:405)、`keyword_search_scored` 的 `in_scope`(:1737 —— 它决定 BM25 的文档总数,父段混进去会改 idf)、`chunk_embeddings_by_hash`(:1621);
  - 读:`list_chunks`(:1560)加参数 `chunk_types: Collection[ChunkType] | None`,**默认只返回可检索类型**(现有调用方行为不变);`_to_chunk_with_embedding`(:313)兼容空向量;
  - 新方法 `get_chunks(tenant_id, chunk_ids) -> list[KnowledgeChunk]`(按 id 批量取,检索后取父段用,带租户条件)。
- 内存存储 `knowledge/memory.py` 同义改动:`replace_chunks`(:797,唯一性按 `(chunk_type, chunk_index)`)、`list_chunks`(:833)、`search_scored`(:887)、`keyword_search_scored`(:925)、`get_chunks`。
- 协议 `knowledge/base.py` 加 `get_chunks` 与 `list_chunks` 新参数。
- 测试:两套存储契约测试同义(`test_knowledge_store_model_v2_contract.py`),父段不出现在向量 / 关键词召回里,BM25 文档总数不含父段,`list_chunks` 默认不含父段、指定类型能取到,`get_chunks` 跨租户取不到。

### T3 入库接线(control-plane `knowledge/ingestion.py`、`knowledge/chunk_rows.py`)

- `_chunk_embed_store`(:587)改调带父段的切块函数;`ChunkRows`(`chunk_rows.py:31`)同时产出父段行(无向量)与子段行(带 `parent_chunk_id`,父段行 id 入库前生成);只嵌入子段。
- `chunk_count` 仍是子段数(:939 / :959;`recovery.py:242/262`)。
- 重建向量 `_reindex_document`(:1073)/ `_collect_chunks`(:1108):只重嵌子段,父段原样带过 `replace_chunks`(否则重建向量会把父段删掉)。
- `chunk_params_hash` / `CHUNKER_VERSION` 见 §2。
- 测试:入库后父子关系、父段无向量、重建向量后父段仍在且关系不变、`chunk_count` 只数子段、单子段文档不建父行。

### T4 检索折叠 + 工具输出(orchestrator `tools/knowledge.py`)

- 召回、融合、重排、门槛仍在子段上做(不变)。**`[:limit]` 截断改到折叠之后**:无重排路径(:554 / :578 / :586)与 `kept = [...][:limit]`(:485)都先保留整个候选池,再按父段折叠后截 `limit` 个父段。
- 折叠:按排序依次看子段,键 = `parent_chunk_id`(为空用自己的 id);父段分数 = 最先出现(最高分)的子段分数;同一父段只出一次,记下命中的全部子段编号。批量 `get_chunks` 取父段正文。
- `RetrievedChunk`(:199)加 `parent_chunk_id`、`parent_index`、`hit_chunk_indexes`,`content` 改为父段正文(无父段时为子段本身)。下游读者逐个核对(`domain_research.py:71`、`kb_eval`、管理端检索测试接口)。
- 工具输出 `_format_results`(:860):每条 `[文件名#命中子段编号]` + 父段正文;总正文预算 10,000 字符(§1.5),放不下的父段只给命中子段前后能装下的部分并标 `[已截断]`;一个都放不下时至少给第一条的命中子段。
- 测试:折叠去重、父段分数取最高子段、`limit` 数的是父段、无重排路径同样折叠、预算截断与标记、无父段文档行为不变、门槛语义不变。

### T5 评测接线(`tools/eval/kb_eval.py`、`kb_metrics.py`)

- 结果行按父段记:`retrieval_text` = 交给模型的父段文本;MRR@10 / 段命中按父段算;搜索日志写父段编号 + 命中子段编号 + 每条正文 token 数。
- 汇总加「每次检索交给模型的平均 / P90 token」。
- 段长分位数(`_all_chunk_texts` :849)只数子段,另报父段分位数。
- 离线回放(缓存全中时零花费)对得上:同一份代码跑两次结果一致。

### T6 管理端(control-plane `api/knowledge.py` + admin-ui)

- 接口:`GET .../documents/{id}/chunks`(:1009)加 `view=parents|children`(默认 `children`,前端不改时行为不变);`POST /bases/{name}/test`(:1064)结果带父段正文与命中子段编号。
- 前端(设计 §6):`SegmentPreviewDrawer.tsx` 按父段分组显示子段(可折叠);`RetrievalTestTab.tsx` 显示父段并高亮命中子段(§1.3 的找得到才高亮);计数口径「段数」= 子段数不变;中英文案同步(`field_top_k_hint` 改成「条结果」)。
- 测试:接口两种视图、检索测试返回结构;前端 vitest(`RetrievalTestTab.test.tsx`、`KnowledgeDetail.test.tsx`)+ 现有 e2e 里用到的 testid 先 grep 再改。

## 4. 顺序与并行

| 波 | Task | 冲突核对 |
|---|---|---|
| 0 | 协议:`ChunkType.PARENT`、`SEARCHABLE_CHUNK_TYPES`、`KnowledgeChunk.embedding` 可空(我来提交,后续都基于它) | — |
| 1 | T1 ‖ T2 | T1 只动 `chunking.py` 与切块测试;T2 只动 persistence 包与迁移 —— 不交叉 |
| 2 | T3 ‖ T4 ‖ T6 接口 | T3 动 `ingestion.py` / `chunk_rows.py`;T4 动 orchestrator;T6 接口动 `api/knowledge.py` —— 不交叉 |
| 3 | T5 ‖ T6 前端 | 评测脚本 vs admin-ui |
| 4 | 开发集付费评测(跑前报价)→ 智能体层 60 题(跑前报价)→ 汇报 → 开 PR(K-3 + K-1 后端一个 PR,前端一个 PR) | — |

## 5. 记后续(本单元不做)

- 父段上限库级可调;子段在父段里的精确字符区间;编辑子段后重拼父段。
- K-2 读文档工具会用到父段编号(`around = 父段编号`),本单元的父段编号就是它的寻址单位。
