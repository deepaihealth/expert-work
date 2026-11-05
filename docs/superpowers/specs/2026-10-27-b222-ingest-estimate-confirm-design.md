# B-222 知识库入库前先预估费用、确认后才开始(合做 K-21 ①③ + 入库中可取消)

- **状态**:设计 v1(2026-10-27),§14 已拍板。排期见[知识库优化总计划](../plans/2026-10-21-kb-optimization-master-plan.md) §2.2 波 1 B 线。分两个 PR:PR-1 入库中可取消(§8);PR-2 预估 + 确认 + 副本(其余各节)。
- **来源**:ROADMAP B-222;用户 10-26「无论费用大小,都要让用户确认再开始」(否决「超过阈值才确认」,¥0.00 也要点确认);B-206 设计 §5.7 K-21 ①(上传时发现内容完全相同 → 提示、默认不入库、可选仍入库)和 ③(预估里副本标出、不计费);同节「并进已有条目」B-222 行(入库中可取消,批量模型调用每批检查取消标记)。
- **约束**:① 预估本身不调任何模型,只做本地解析 + 计数;② 平台文本零租户内容,对不同厂商的模型、六种文档形态都成立;③ 迁移只做 expand,新表带 `tenant_id` + RLS,SQL 与内存两套 store 谓词同义并有参数化契约测试;④ 跨租户只读的人看得到金额和状态,按钮点不了。
- **对标**:WeKnora `CancelKnowledgeParse`(`internal/application/service/knowledge_process.go:3232-3350`:置 cancelled、保留已写数据、各环节靠检查点自停)与问题生成每批开头查状态(`:2367-2379`);WeKnora 没有费用预估,Dify 建库「预估」只给分段数 + 预览、不给价格 —— 预估部分无先例可借。

---

## 0. 一句话方案

上传、同名重传、重新摄取、补识别、按新参数重新入库都先进「估算中」:在现有入库队列上跑一遍**不花钱的本地解析 + 计数**,结果存成快照,算出分项金额后进「待确认」。用户确认这份预估(按预估 id 做 CAS)后才进 `pending`,走原来的付费流水线,直接读快照、不再解析一遍。内容完全相同的副本上传时就标出,默认不入库、不计费。付费阶段每个模型调用批次之前查一次文档是否还归自己;取消即代次 +1,下一批之前停下。

## 1. 现状(origin/main `e90903f7e`)

- **队列就是文档行**:状态 `pending / processing / ready / failed`(`protocol/knowledge.py:47`),`status` 列无 CHECK 约束。上传 `upload_document`(`api/knowledge.py:727-783`)先 `upsert_document` 置 `pending`,内容没变回 200 `unchanged`,否则 `runner.submit`。快路径 `KnowledgeIngestionRunner._run`(`knowledge/ingestion.py:904`):并发默认 4、`claim_document` CAS `pending → processing`、租约 300 s 每 100 s 续约、所有写回带代次栅栏(`StaleIngestionError` 时什么都不写)。兜底 `KnowledgeIngestRecoveryWorker.run_once`(`knowledge/recovery.py:120`)每 30 s 认领 `pending` 或租约过期的 `processing`。`knowledge_task` 表只有 ORM 和过期删除,无人读写。
- **花钱的环节**:扫描页识别(每 16 页一批,批前有带栅栏的写);图片描述(所有图并发,中途无检查);话题断点的块嵌入(每窗 200 块);分段嵌入(每窗 200 段,每请求 10 条,中途无检查)。价目来自平台级 `model_rate_card`(每百万 token 微元;markup 目前 0);没有价目行的用量成本记 0。换嵌入模型的重建索引(`_run_reindex`)不记租户账。
- **可复用的免费能力**:B-214 `structural_chunk_count` / `ensure_chunk_count`(不嵌入数结构段);按 `content_hash` 复用本文档同模型旧向量;B-215 断点缓存;B-209 识别 / 描述按文档缓存(`ON DELETE CASCADE`);同名同字节重传直接跳过。
- **缺口**:没有预估和确认;入库中不能取消(只有重传 / 删除间接让代次失效,最长约 100 s 才停,分段嵌入和图片描述中途没有检查);内容相同、文件名不同的文件当新文档全量付费。

## 2. 入口穷举

| # | 入口 | B-222 后 | 理由 |
|---|---|---|---|
| 1 | 控制台上传单个文件(`POST /v1/knowledge/bases/{name}/documents`) | 先估后确认 | 用户原话 |
| 2 | 批量 / 文件夹上传(客户端并发调同一接口) | 先估后确认,一批给合计、可一键确认 | 同上 |
| 3 | 同名重传、内容变了 | 先估后确认;撤回则旧版照常 | 同上 |
| 4 | 同名重传、内容没变 | 不变(`unchanged`) | 不花钱 |
| 5 | 不同名但内容相同(同库副本) | 标出、默认不入库、不计费;选「仍入库」先估再确认 | K-21 ①③ |
| 6 | 重新摄取(单份) | 先估后确认,只算真要花钱的部分 | 同上 |
| 7 | 补识别 | 先估后确认(只算失败 / 待识别的页和图 + 新出文字的嵌入) | 同上 |
| 8 | 改库切块参数后全库重切 | C 线 PR-C1 新增的 `POST /bases/{name}/reingest-stale`(只挑 `chunk_params_hash` 与库当前参数不同、留有原文件的文档)在 PR-2 改为批量先估后确认,不另开新接口;批量操作里的「重新入库」同样先估后确认 | ROADMAP 明写 |
| 9 | 换平台嵌入模型后的重建索引(`/reindex`) | 不走租户确认 | 平台出钱 |
| 10 | 崩溃恢复 / 排队兜底 | 只续跑已确认的;`estimating` 只跑免费估算;`awaiting_confirmation` 与 `cancelled` 永不认领 | — |
| 11 | 扫描页识别、图片描述 | 不是独立入口,随所在入口一起估、一起确认 | — |
| 12 | 开放 API / 第三方 | 现在没有(`/v1/knowledge` 两个路由前缀都挂 `console_only()`);B-175 P6 做时必须复用本设计的预估与确认 | — |
| 13 | 智能体 / 技能写知识库 | 现在没有(编排器只有只读的 `knowledge_search`) | — |
| 14 | 评测脚本 | 不在产品范围,跑前人工报价 | — |

数法(仓库根目录):`grep -c "@router\.\(get\|post\|put\|patch\|delete\)" services/control-plane/src/control_plane/api/knowledge.py services/control-plane/src/control_plane/api/knowledge_figures.py` → 15 + 1;`git grep -n "runner\.submit\(_reindex\)\?(" -- 'services/**/src/**/*.py'` → 3 处(`_redrive` / reindex / upload);`git grep -n "ingest_document_bytes(\|ingest_markdown(" -- 'services/**/src/**/*.py'` → 快路径与恢复各 1;`git grep -n 'name="knowledge_' -- 'services/orchestrator/src/**/*.py'` → 只有 `knowledge_search`。

## 3. 状态机

```
上传 / 重传(内容变) / 重新摄取 / 补识别 / 按当前参数重新入库   (代次 +1)
        ▼
  estimating 估算中:本地解析 + 计数,不调模型
    │ 解析失败 / 超字数 / 超段数(免费发现)      │ 估算完成:预估 + 快照同一事务写入(带栅栏)
    ▼                                           ▼
  failed                          awaiting_confirmation 待确认(7 天)
                                    │ 确认(CAS 预估 id;代次 +1)   │ 取消 / 到期 → 撤回(§6.2)
                                    ▼
       pending ──认领──▶ processing 入库中(可取消)
          │取消             │取消                 │ 开工复核:模型变了 / 合计涨超阈值
          ▼                 ▼                     └──▶ awaiting_confirmation(新预估,一分没花)
       cancelled 已取消 ◀───┘        完成 → ready      失败 → failed
副本(同库内容相同):上传即 awaiting_confirmation,不解析、默认不入库;「仍入库」→ estimating
```

- 新增状态 `cancelled`(PR-1)、`estimating`、`awaiting_confirmation`(PR-2)。不复用「`pending` + 标记」:滚动发布期间旧 pod 的恢复程序会认领任何 `pending`,未确认的文档可能被直接付费入库;新状态旧镜像不认领。
- 估算走同一套队列:`_claimable` 加「`estimating` 且租约为空或过期」;认领 `estimating` 只设租约、尝试数 +1、状态不变;`ClaimedIngestion.phase` 告诉执行方跑估算还是付费入库;快路径与恢复共用 `process_claim()`。估算享有波 1 的全部可靠性(代次栅栏、续约、尝试上限、并发上限、解析进程池、原始字节预算);`sweep_terminal` 也扫尝试用尽的 `estimating`。
- 栅栏:续约 / 写阶段 / 终态失败 / 写页数 / 写图片的栅栏改为「状态 ∈ {processing, estimating} 且代次相等」;`READY` 与 `replace_chunks` 仍只认 `processing`。确认、取消、撤回都让代次 +1,估算阶段的残留 worker 写不到付费阶段上。
- 不用 `knowledge_task`:批次就是这批上传的文档 id 集合,库级待确认合计由列表接口按状态汇总。
- 存量:发版那一刻已在 `pending` / `processing` 的文档视为已确认。生产目前 0 个库。

## 4. 预估怎么算

### 4.1 分项(只列记在租户名下的 `knowledge_ingest`;平台出钱的项不列,测试断言分项只有三类)

| 项 | 本地怎么数(全部不调模型) | 折算 |
|---|---|---|
| 向量化 | 按库切块参数切(结构切块;B-215 缓存里已有的话题断点照用),检索文本去重,减去本文档同一嵌入模型已有向量的段;话题切分要嵌入的块(新纯函数 `semantic_break_inputs` 复刻 `_semantic_breaks` 取块规则,减去缓存已有的对);识别 / 描述将产出的文字按页 / 张均值 | 本地 token × 模型换算系数 × 嵌入输入价 |
| 扫描页识别 | PDF 逐页判定(B-209 三信号)得出要识别的页;扣超过 `max_pages` 的;扣缓存里有这份字节、按当前读法(模型 + 请求参数 + 渲染参数 + 渲染库版本)读过的页(B-238 / B-256:上次入库的逐页记录不算数) | × 每页平均输入 / 输出 token × 识别模型价;另按 10% 页数加兜底模型 |
| 图片描述 | 过了单文档过滤的候选图 → 同租户重复过滤 → 每份 50 张上限;字节没变时扣上次已描述的 | × 每张平均 token × 描述模型价 |

- 金额:`token × 每百万 token 微元 // 1_000_000`,与汇总任务同一套整数算法,租户价走 `apply_markup`。价目由注入的 `PriceSource` 调 `ModelRateCardStore.resolve`。没有价目行的项标「未定价,暂不计费」、金额 0。识别模型没配 → 扫描页记「待识别,不收费」;平台没开图片描述 → 图片不处理。
- 不计入(行上用提示码注明):扫描页上的插图;识别文字超单份字数上限的提前停(会多估);同批重复出现的 logo(多估);副本。
- 估算阶段能免费发现的错误直接 failed、一分不花:解析失败、`TEXT_TOO_LARGE`、`TOO_MANY_CHUNKS`。

### 4.2 均值和换算系数放哪(厂商中立)

模型目录 `ModelEntry` 新增可选 `estimate_caps`(`page_input_tokens`、`page_output_tokens`、`figure_input_tokens`、`figure_output_tokens`、`embed_tokens_per_local_token`);没填的用平台缺省(同类实测最大值,偏多估),行上注明「按通用均值估」。PR-2 开工第一步免费校准:① 用手册 §55.6 记过厂商计数的合成文件(3,588,096 token)本地重算分段 + 断点 token,得 text-embedding-v4 的系数;② 测试库只读查 `token_usage`(`usage_kind='knowledge_ingest'`)按模型求每次调用平均输入 / 输出,定每页 / 每张均值;③ 话题切分的额外 token 用评测的只读嵌入缓存量。中英文系数差超过 15% 就分「中日韩字符 / 其它」两档。

### 4.3 准确度怎么说给用户

行上显示「预估 ¥0.12」;小于 ¥0.01 显示「< ¥0.01」;0 显示「¥0.00」,也要点确认。悬浮逐项列出段数 / token、页数(含「上次已识别、不重复收费」)、张数。底注:段数、页数、张数是本地解析数出来的;token 按本地分词换算,识别和图片按平均用量估;实际按厂商返回的用量计费,入库结束后这里显示实际花费。入库结束(完成 / 失败 / 取消)写 `ingest_actual`,行上显示「预估 ¥x · 实际 ¥y」。**校准前不对外承诺误差百分比**,校准后的实测写进手册。

## 5. 解析快照:确认后不再解析一遍

- 新表 `knowledge_parse_snapshot`(`tenant_id`、`document_id` 主键 + 外键 `ON DELETE CASCADE`、`estimate_id`、`format`、`payload bytea`(gzip JSON)、`size_bytes`、`created_at`、`expires_at`),RLS ENABLE + FORCE。一份文档只保留最新一份。
- 存本地阶段全部产物:纯文本类 = 最终 Markdown;Word / 幻灯片 = 带图片占位的 Markdown + 候选图;PDF = 逐页判定 + 文字页文字 + 候选图。
- 寿命 = 待确认有效期(7 天);确认后延 1 天给崩溃重试;付费阶段落定后立即删;撤回 / 到期时删;恢复程序每轮再清 `expires_at < now()` 的残留。
- 存 PG,所有 pod 共享;快照与「估算中 → 待确认」同一事务写(`finish_estimate`),不留孤儿行。删文档 / 删库显式删 + 外键兜底;资料可能有健康数据,快照不出租户、不出文档。
- 压缩后超 32 MiB 不存,付费阶段免费重解析一次;快照读不了(格式版本变了)同样重解析,再做 §6.3 复核。

## 6. 确认 / 撤回 / 取消 / 到期

- **6.1 确认**:逐份 `{document_id, estimate_id}`,只有 `awaiting_confirmation` 且预估 id 相等才转 `pending`(代次 +1、尝试数清零),预估 id 不对回 `estimate_changed`。全库一键确认带 `{all, expected_count, expected_total_micros}`,服务端加锁重算,相等才全部确认,否则 409 带新汇总;副本不在「全部」之内。
- **6.2 撤回**(估算中 / 待确认时点取消,或到期):进入估算时在 `ingest_estimate.restore` 记下原 `status / error / error_code / stage`。新上传 → **删除文档,连同原文件和快照**;同名重传 → 新版作废,有在服务的旧版就 ready;重新摄取 / 补识别 / 按新参数重新入库 → 按 `restore` 恢复、代次 +1。
- **6.3 开工复核**(一分钱还没花):读快照,用当前模型和价目重算;**嵌入 / 识别 / 描述模型与预估时不同一律退回待确认;合计比确认时高出 20% 以上且至少 ¥0.01 退回待确认**;其余照常入库。
- **6.4 取消**(`pending` / `processing`,PR-1):置 `cancelled`、代次 +1、清租约、`error="cancelled by user"`。已写的识别缓存、断点缓存保留(重新入库不再付费);这一代存的原图由现有 `tidy` 删。之前有在服务的旧版就继续服务(检索不按文档状态过滤),换嵌入模型重建索引时照样重新嵌入(重建索引处理 ready / failed / cancelled 的全部分段;有分段的文档正在重新入库(pending / processing)时不动它;重建结束时库里只要还有可检索分段不是新模型的(正在重新入库的文档,或重建途中被重新上传、随后又取消 / 失败的文档),库的模型就不盖章,`needs_reindex` 保持为真)。已完成的文档回 `not_cancellable`;`processing` 且 `stage=indexing`(正在换上新分段,随后 READY)是不可撤回点,也回 `not_cancellable` —— 带栅栏的「进入 indexing」阶段写与取消在同一行上串行,取消不会落在换分段与 READY 之间;零分段的新版清空分段前同样先进 indexing;租约过期也不例外(worker 可能是换完分段、写 READY 前死的,新分段已在服务),这种文档由恢复任务重新认领。「丢弃新版」(`discard-pending`)同一规则:不可撤回点上回 409。
- **6.5 到期**:`confirm_by` = 进入待确认 + **7 天**。恢复程序 `run_once` 用 `FOR UPDATE SKIP LOCKED` 扫过期的待确认文档,按 6.2 撤回,每份写一条执行人为 `system` 的审计;多 pod 同时扫幂等。页面显示剩余天数。
- **实际超出预估不自动暂停**:只显示「预估 / 实际」,用户随时可取消。

## 7. 副本(K-21 ①③)

- **只在同一知识库内判定**;跨库内容相同照常入库(同一份资料放进两个库常是有意为之)。
- 上传时算 `sha256`,查同库里其它文件名、正文或待处理槽哈希相同的文档(`find_same_content`)。命中:直接进 `awaiting_confirmation`,预估为 `{duplicate_of: {id, doc_seq, filename}}`,**不解析、不 submit**;行上显示「已有同样内容的文档 D12《…》,默认不入库」,按钮「仍入库」,金额不计入合计。
- 估算完成时再查一次,兜住同时上传的竞态:后创建的那份标为副本,它已有真实预估,「仍入库」即确认。上传即判为副本的点「仍入库」→ `POST …/documents/{id}/estimate` → 估算 → 确认。
- 批量确认按钮写明「确认 N 份 · 跳过 M 份副本」,被跳过的副本按 6.2 撤回。原文档已撤回时,列表读出时去掉副本标记。

## 8. 入库中可取消:每批检查(PR-1)

| 批次 | 检查点 | 取消后最多还会花 |
|---|---|---|
| 分段嵌入 / 断点块嵌入 | `_GuardedEmbedder` 包在 `_BackoffEmbedder` 外,每次 `embed()`(一窗 ≤ 200 条)前 `guard()` | 一窗(约 8 万 token,text-embedding-v4 约 ¥0.04) |
| 扫描页识别 | 现有 `before_batch`,每 16 页一批 | 一批 16 页 |
| 图片描述 / 图表转数据 | `FigureDescriber.describe(before_call=…)`,每次调模型前(拿到模型并发名额之后,重试的每次尝试也查) | 已在途的调用(≤ 模型并发名额) |
| 图中文字(识别模型) | `recognize(checkpoint=…)`,每次调模型前(同上,拿到名额后、每次重试) | 已在途的调用(≤ 模型并发名额) |

`guard()` = 带栅栏的 `renew_lease`,返回 False 抛 `StaleIngestionError`,走现有「什么都不写」分支;续约本身出错(数据库抖动)只记日志、照常继续(最终的带栅栏写仍然把关)。图片的检查同理:只有 `StaleIngestionError` 算拒绝(之后的调用不再问、直接拒绝),别的错误记日志、该次调用照常。同 pod 立即停:一个进程一份 `LocalIngests`(`document_id → 停止回调`),上传入库的 runner 和恢复任务共用,取消接口调 `cancel_local`,在途 HTTP 请求被中止(沿用 `lost` 语义,不写 FAILED);同一文档旧任务收尾时只注销自己的回调。被中止时正在进程池里跑的调用(解析池、图片池同样),名额等那个 worker 跑完才让出(否则下一份文档在池子队列里空等、超时算在它头上),但这次调用的截止时间照旧:过了截止时间就杀掉进程池(和超时一样,同池被连累的调用不扣尝试次数)并让出名额。跨 pod 靠批次检查,最迟 100 s 续约被拒(上传入库和恢复任务都是:续约被拒即停;恢复任务付费部分做完后被拒不再打断,收尾照常)。PR-1 顺带让 SQL 读到不认识的状态时按 failed 读(`error` 写明),给以后回滚兜底。

## 9. 接口

| 接口 | 变化 |
|---|---|
| `POST /bases/{name}/documents` | 202,进 `estimating`;副本进 `awaiting_confirmation` 带 `duplicate_of`;`unchanged` 不变 |
| `POST …/documents/{id}/reingest`、`…/recognize` | 202,进 `estimating` |
| `POST /bases/{name}/reingest-stale`(PR-C1 已加)、`POST …/documents/batch` 的 reingest | 改为进 `estimating`(只挑旧参数且留有原文件的 ready / failed / cancelled 文档),不再直接入库 |
| `GET /bases/{name}/documents?status=…` | 新增状态过滤;带估算 / 待确认时附 `estimate_summary {estimating, awaiting, duplicates, total_micros, unpriced}` |
| `POST /bases/{name}/documents/confirm`(新) | 逐份或全部;409 `estimate_changed` |
| `POST /bases/{name}/documents/cancel`(PR-1 新,PR-2 扩撤回) | 逐份 `withdrawn_deleted / withdrawn / cancelled / not_cancellable / not_found` |
| `POST …/documents/{id}/estimate`(新) | 副本「仍入库」 |
| 文档字段 | 新增 `ingest_estimate`(公开部分,不含 `restore`)、`confirm_by`、`ingest_actual` |

写接口 `manifest:write`、console_only;读接口 `ensure_single_tenant_scope`。审计:`knowledge:document_cancel`(PR-1);`knowledge:document_confirm`、`knowledge:document_withdraw`、`knowledge:document_estimate_expired`、`knowledge:base_reingest`(PR-2),每份一条。`ingest_estimate` v1 形状:`{id, version, origin, restore?, created_at, duplicate_of?, currency, total_micros, unpriced, notes, counts{chunks, scanned_pages, figures, pages_over_limit}, models{embedding, recognition, fallback, figure}, models_fingerprint, items[{kind, units, unit, reused_units, model, input_tokens, output_tokens, micros, basis}], confirmed_at?, confirmed_by?}`;`notes` 是提示码(如 `scanned_figures_not_counted`、`recognition_unavailable`、`figures_off`、`default_average`、`over_page_limit`),管理端翻译成中英文案。

## 10. 迁移(PR-2,`0178_kb_ingest_estimate`)

`knowledge_document` 加 `ingest_estimate JSONB`、`ingest_actual JSONB`、`confirm_by TIMESTAMPTZ`(都可空);部分索引 `(lease_until) WHERE status IN ('pending','processing','estimating')` 与 `(confirm_by) WHERE status='awaiting_confirmation'`;新表 `knowledge_parse_snapshot`(§5)按 `expires_at` 建索引。downgrade 先把 `estimating` / `awaiting_confirmation` 改成 `failed`(`error` 写明「入库前预估回滚」)再删表 / 列 / 索引;只换回旧镜像时先手工执行同一条 UPDATE。(编号:0176 / 0177 已分给 C 线。)

## 11. 管理端

- 上传队列每个文件一行:估算中 → 待确认「¥0.12」(悬浮看分项)/ 副本 / 入库中 / 已完成「实际 ¥」/ 已取消 / 失败;每行确认、取消、仍入库。表头「本批 20 份:待确认 17 份,预估合计 ¥1.23;副本 2 份(不入库,不计费);估算中 1 份」+「确认 17 份 · 跳过 2 份副本」「全部取消」。
- 文档页横幅:库里有待确认文档时显示「N 份文档等待确认入库,预估合计 ¥X,D 天内不确认将自动撤回」+「全部确认」「全部取消」「只看待确认」;刷新后上传队列没了,横幅还在。
- 文档表:三种新状态的标签;待确认行「确认」「取消」,估算中 / 排队 / 入库中行「取消」;入库结束显示「预估 ¥x · 实际 ¥y」。工具条「按当前切块参数重新入库」。跨租户只读:按钮置灰,金额和状态照常显示。所有角色本来都有 `billing:read`。

## 12. 形态 / 厂商检验

Ⓐ 有标题长文档 / Ⓑ 无标题长文本 / Ⓒ 短文档 / Ⓔ 对话体:分段与断点计数和入库同一套切块代码,用等价测试钉死;Ⓓ 表格:xlsx 由 K-18 本地解析;Ⓕ 扫描件 / 图片:页数张数精确,金额按均值,扫描页插图不计入并标注。均值与换算系数在模型目录,价目只认 `model_rate_card`,没有任何地方写死某家的常量。

## 13. 不做

「超过阈值才确认」(用户否决);预估时调模型抽样;用 `knowledge_task` 承载批次;副本「仍入库」时跨文档复制识别缓存和向量(与 B-209「缓存按文档、删得干净」冲突);实际超预估自动暂停;重建索引走租户确认(平台出钱)。

## 14. 拍板(10-27 用户「就7天吧,其他的都按推荐的做」)

1. 待确认最多 **7 天**,到期自动撤回,页面显示剩余天数。
2. 撤回还没入库的新上传 = **连原文件和快照一起删**。
3. 副本只在**同一知识库内**判定。
4. 确认后重新确认的条件:**换模型一律;合计涨超 20% 且至少 ¥0.01**。
5. 实际超出预估**不自动暂停**。
6. 每份文档**显示实际花费**(「预估 ¥x · 实际 ¥y」)。

## 15. 风险

1. 回滚与滚动发布:旧镜像读到新状态会 500 → PR-1 的读容错兜住回滚到 PR-1;回滚到更早要先跑手册里的 UPDATE。发布窗口内旧 pod 收到的上传照旧直接入库(过渡期)。
2. 分词换算:不同分词器差距大(同 8,000 个 o200k token 的印地文在 text-embedding-v4 上是 2.6 倍)→ 校准前不承诺百分比,每份显示实际花费兜底。
3. 均值误差:单份扫描页 / 图片金额可能差到 2 倍,批量会平均;偏差方向多为多估。
4. 崩溃重试超出预估:现有行为;识别和断点有缓存,分段嵌入会重付,行上显示实际值。
5. 快照占库:约 1 MB / 份、最长 8 天;有 32 MiB 单份上限和 TTL 清理。
6. 用户摩擦:每次上传多一次点击;7 天不确认会撤回新上传 → 横幅 + 剩余天数。
7. 并发竞态:副本靠估算完成时复查;确认与重传冲突靠预估 id CAS;取消与落定冲突靠代次栅栏。
8. PR-2 必须后端与管理端一起合:只合后端会让所有上传卡在待确认。
