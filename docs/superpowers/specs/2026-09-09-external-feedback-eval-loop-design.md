# 对外消息级反馈(点赞 / 点踩)进评测回路 —— 设计(P-2)

> 2026-09-09 用户拍板逐条:按轮打分 / 👎 一条当场进池、👍 不进 / 评论原文全员可见 / 可改票 / 只做到评测集为止;「按评测集跑评测出分数」入册 B-46。
> 读者:实施本设计的工程师 + 用户审阅。术语:**员工** = 租户内使用控制台的人;**终端用户** = 对接方应用里的人(`user_id=pc:…`);**对接方** = 第三方开发工程师。

## 1. 目标 / 非目标

**目标**
- 对接方能在自己的界面上让终端用户给 agent 的一轮回答打 👍 / 👎(可附评论),平台记下来。
- 👎 进入平台已有的「策展待审池」,人审后进评测集 —— 用于改进 Agent。
- 控制台能看到反馈(列表可筛、会话详情逐轮显示)。

**非目标(明确不做)**
- 按单条消息打分(平台没有跨接口稳定的 message id,见 §2.4)。
- 按评测集跑评测、出分数、按 agent/版本/模型汇总面板 —— B-46,等样本攒够再做。
- 👍 进待审池(正例 worker 已在自动产出,人审带宽有限)。

## 2. 现状(2026-09-09 核实,行号以 main `723c5a40` 为准)

### 2.1 反馈表与写路径
- 模型 `packages/expert-work-persistence/src/expert_work/persistence/models/feedback.py:18-44`:`id / tenant_id / thread_id / turn_seq / trace_id / rating / comment / actor_id / created_at / processed_at`。FORCE RLS(迁移 `0014_feedback.py:34-72`),`0071` 加 `processed_at` + `audit_reader` GRANT。
- **没有 `run_id`、没有 agent 身份、没有唯一约束**:同一 thread 可无限写重复行。
- 唯一写入端点 `services/control-plane/src/control_plane/api/feedback.py:48-104`:`POST /v1/sessions/{thread_id}/feedback`,`console_only()` → API key 一律 403,**对外零暴露**。fire-and-forget,不校验 thread 存在。
- `turn_seq` 是死字段:迁移与模型说它指 `event_log.seq`,控制台实际传的是 UI 局部序号(`components/console/types.ts:25-29`),全仓无读取方。

### 2.2 读路径
- **不存在任何 GET**(`api/feedback.py:51` 只注册 POST)。控制台哪儿都看不到反馈。
- 内部消费者四处,全部 **thread 粒度**:`curation_worker.py:239-243`(`has_down/has_up`)、`feedback_consumer.py:146-195`(只吃 👎 → 记忆 `review_flagged_at`)、`skill_evolution_wiring.py:482-487`(👎 comment 进蒸馏证据)、`skill_rollback_gate.py:115`(`down_rated_threads`)。

### 2.3 待审池与评测集
- 候选产生:`curation_worker.py:191-267` 每 300s(`ENABLE_CURATION_WORKER=true`,base configmap)跨租户扫 trajectory ObjectStore,按 `(tenant, trajectory_key)` **预检去重**(`:213-217`,已建候选直接跳过),分类 `has_down → negative_feedback > failed → failed_outcome > has_up → positive_feedback > implicit_success`(`:93-110`)。
- promote:`api/curation.py:310-373` 人工填 `name/input/expected/source` → `eval_dataset` 行;候选详情与 promote 挂 `session:write`(`:246-256`)。
- **评测集今天没人跑**:`eval_worker.py` 只跑文件数据集(`eval_engine_live.py:50-65`),`eval_engine.py` 零处读 `eval_dataset`;吃 `eval_dataset` 的只有技能进化 held-out replay(`skill_evolution_wiring.py:535-572`,只认 `source in ("golden","regression")`),而 `ENABLE_SKILL_EVOLUTION_WORKER=false`、`ENABLE_EVAL_WORKER=false`。
- **既有漂移(待真跑确认,§7)**:控制台 promote 发 `source: "promoted_candidate"`(`pages/curation/CandidatesPanel.tsx:141`),后端 Literal 是 `golden|trajectory|regression`(`protocol/eval_dataset.py:36`)→ 疑似恒 422;`api/curation.ts:20-27` 的 signal 枚举与后端真值不一致。

### 2.4 对外 message id 形态 → 端点按 run
- `/messages`(`api/external_sessions.py:211-280`)不给 id;`/items`(`api/external_session_items.py:353-500`)历史侧 `f"{run_id}:{n}"`、实时侧 `{run}:step:{n}` / `{run}:call:{id}`;`common/conversation_items.py:14-17` 与对外文档 `docs-site/guide/query.md:804` 明写「不跨接口稳定」。
- 三条路径都稳定的一等 id 只有 `run_id`(UUID)。

## 3. 数据模型

迁移 `0152_feedback_run_scope`(当前最新 0151):
- `feedback` 加列:`run_id UUID NULL`(对外与控制台新写入必填;历史行 NULL)、`source TEXT NOT NULL DEFAULT 'console'`(`console|external`)、`item_id TEXT NULL`(对接方附带的段落标签,**只存不 join**)。
- 部分唯一索引 `(tenant_id, run_id, actor_id) WHERE run_id IS NOT NULL` —— 「可改票」= upsert。
- 索引 `(tenant_id, run_id)`。
- `turn_seq` 保留不动(死字段,另议)。
- `actor_id`:对外写入时 = 终端用户的内部 id(`lookup_external_user_id` 解析,与 `/messages` 的 `user_id` 校验同源)。

`curation_candidate`(现有表)加两列:`feedback_run_id UUID NULL`、`feedback_comment TEXT NULL` —— 审阅员打开候选能直接看到「哪一轮被踩 + 用户原话」,不用翻整条 trajectory。

## 4. 对外 API

### 4.1 写反馈
`POST /v1/agents/{agent_code}/runs/{run_id}/feedback`(与 `…/runs/{run_id}:cancel` 同一路由族,`api/external_runs.py:169`)
- 权限:`external_only()` + `require("session","write")`(write 档即够,与对接文档「给对接方 write 一档」一致)。
- body:`{ "user_id": "<终端用户 id>", "rating": "up" | "down", "comment": "<=4000 字, 可选>", "item_id": "<可选标签>" }`;`comment` 走 `reject_nul` 校验(`api/_external.py:50-69`),路由级 `reject_nul_path_params`。
- 归属:`load_owned_run`(`api/_external.py:349-398`)—— run 不属于该 user/agent → 404,不泄露存在性。
- 语义:同 `(run, user)` 再打 = 覆盖(`rating`/`comment`/`item_id` 全量替换,`created_at` 不变、加 `updated_at`)。
- 响应:`{ "success": true, "data": { "run_id", "rating", "updated": bool }, "error": null }`。
- 三张手工路由表**必须同时登记**(不登记 CI 红,这是设计):`tests/test_external_only_gate.py:66` `_EXTERNAL_ROUTES`、`tests/test_console_lockdown.py:251` `_EXTERNAL_AGENT_ROUTES`、`tests/test_external_path_param_nul_guard.py:502` `_AGENTS_ROUTER_EXTERNAL_ROUTES`。
- 配额 / 限流:沿用网关 `RateLimitMiddleware`;`(run, user)` 唯一键使行数上限 = run 数。对外配额是否需要单列 `feedback` 动作,实施时核 `quota` 动作表,默认不单列。

### 4.2 读反馈(对外)
- `/items` 与 `/messages` 的 run 级结构(`runs[]` / 每条消息的 `run_id`)加 `feedback: { "rating": "up"|"down", "comment": "...", "item_id": "..." } | null`(只返回**该 user_id 自己**打的那条;别的终端用户的反馈不对外)。缺省 `null`。

### 4.3 文档
`apps/admin-ui/docs-site/guide/`:`chat.md`(新节「给一轮回答打分」)、`query.md`(`feedback` 字段表)、`errors.md`(404/422 语义)、`examples.md`(四语言示例各一段)、`best-practices.md`(一句:按轮打分,`item_id` 只是标签)。按 `2026-08-17-external-docs-style-guide.md` 自检。

## 5. 进待审池(评测回路)

- 写入 👎(`rating=down`)时**同步**:
  1. 若该 thread 的 trajectory 已落 ObjectStore → 直接 upsert `curation_candidate`(signal `negative_feedback`,带 `feedback_run_id` + `feedback_comment`);已有候选(任何 signal)→ **升级**为 `negative_feedback` 并补两列(今天 `curation_worker.py:213-217` 会直接跳过已建候选,这条要改成「可升级」)。
  2. trajectory 尚未落盘(run 刚结束的窗口)→ 不阻塞写反馈;300s worker 兜底扫到时按 `has_down` 建候选(worker 逻辑不变,只多带两列)。
- 👍 不建候选;对已有候选也**不**降级。
- 改票:👎 → 👍 时候选保留,候选行加 `feedback_changed_at`,审阅列表显示「后改为 👍」;👍 → 👎 按上面 1 处理。
- 修 §2.3 两处词表漂移(否则审阅员点 promote 报错,回路在最后一步断掉)。

## 6. 控制台

- 后端 `GET /v1/sessions/{thread_id}/feedback`(`console_only`,`session:read`):返回该会话全部反馈(含评论原文 —— 用户拍板全员可见,与会话原文 operator+ 的门槛**有意不一致**)。
- `GET /v1/conversations` 加筛选 `has_down_rated`(复用 `feedback_store.py:65` `down_rated_threads`,按 `api/conversations.py:243-253` 的 `narrowed_ids` 组合)。
- 对话详情页(`pages/ConversationDetail.tsx`)每轮脚部显示 👍/👎 与评论;Curation 候选行显示「被踩的轮 + 原话 + 是否后改票」。
- 控制台自己的 `POST /v1/sessions/{thread_id}/feedback` 改为必带 `run_id`(`components/console/ledger.ts:204` 已有 `runId`),`source='console'`。
- **对话详情页放开打分(2026-09-09 用户拍板,PR4)**:今天 `TurnFooter.tsx:155` 用同一个 `readOnly` 同时挡「打分」与「重跑」,而 `ConversationDetail.tsx:702` 恒传 `readOnly` → 员工在对话详情页**看得到别人的分、自己打不了**。拍板改为 **operator 及以上能打、viewer 只能看**:前端把打分从 `readOnly` 里拆出来走独立开关(重跑仍然挡住),后端 `POST /v1/sessions/{thread_id}/feedback` **自己加 operator+ 闸**(前端置灰不算闸,viewer 打分必须 403)。员工打的分与终端用户的分同表同列,只靠 `source` 区分:`source='console'` / `run_id` 照 PR1 既有语义,不新增写入通道。
- **凡是人看的地方必须显示来源**:控制台反馈列表与轮脚展示、策展候选行,都要能一眼看出这条 👎 是员工打的还是终端用户打的 —— 审阅员不能把两者当一回事。
- **但四个下游消费者一律不按 `source` 过滤**(§2.2 那四处):回滚闸取的是「该技能版本在时间窗内被用过的会话」再看其中哪些被踩,那里的「踩」本来就是在说这个技能跑砸了,员工说的和终端用户说的同样有效;候选池 / 记忆待复核 / 技能蒸馏证据同理 —— 专家的负反馈只会更值钱。**不要**给这四处加来源判据。

## 7. 待真跑确认(实施第一天做,结果回填本文)

1. ~~控制台 promote 是否真 422(`CandidatesPanel.tsx:141` vs `protocol/eval_dataset.py:36`);signal 筛选下拉是否恒 422/筛空。~~ **✅ 已核实(09-09)**:两处都真断,回路最后一步今天走不通。
   - promote:拿控制台原样载荷 `{"name": …, "source": "promoted_candidate"}` 真打 `POST /v1/curation/candidates/{id}/promote`(真 app + 真路由 + 真 JWT)→ **422**,`detail[0].type = "literal_error"`、`msg = "Input should be 'golden', 'trajectory' or 'regression'"`。同一候选只把 `source` 换成 `trajectory` → **201**,说明 promote 本身没坏,**唯一病因就是词表漂移**。
   - signal 筛选:前端 `SIGNAL_OPTIONS`(`CandidatesPanel.tsx:56-62`)五个值里 `manual` / `tool_failure` / `timeout` / `policy_block` **四个全 422**,只有 `negative_feedback` 通(200,0 条)。后端真值是 `('negative_feedback', 'failed_outcome', 'positive_feedback', 'implicit_success')` —— 前端**漏掉的恰好是库里唯二真实存在的两个**(`failed_outcome` / `implicit_success`,见第 2 条),所以这个下拉今天要么 422、要么必然筛空。
   - 第三处漂移(spec §2.3 只写了两处):`api/curation.ts:39` 声明 `feedback_rating: number | null`,线上真值是字符串 —— 实测候选行回 `'down'`。
   - 旁证:生产与测试 `eval_dataset` **双双 0 行**,与「从来没有一条候选成功 promote 过」一致。
2. ~~生产 `curation_candidate` 是否有行、`feedback` 是否有行、`turn_seq` 长什么样(实证它是 UI 局部序号)。~~ **✅ 已核实(09-09,生产 + 测试 pod 内只读)**:
   - `curation_candidate`:生产 **2 行**(全 `implicit_success` / `pending`,detected 09-07~09-08);测试 **167 行**(162 `implicit_success` + 5 `failed_outcome`,全 `pending`,07-29~08-28)。**一行 `negative_feedback` 都没有** —— 池子里今天全是 worker 自动产出的隐式正例与失败例,人审入口从未被真实差评喂过。
   - `feedback`:生产与测试**都是 0 行**,且 `pg_stat_user_tables.n_tup_ins = 0` —— **建表至今没写进过任何一行**。阳性对照(同一次读):`curation_candidate` n_tup_ins = 167、`agent_run` 597 行,连接角色 `rolbypassrls = True`,所以这不是 FORCE-RLS 把结果静默滤空。`feedback` 现有索引只有 `pkey` + 两个非唯一索引 + 一个部分索引,**确实没有任何唯一约束**(spec §2.1 成立)。
   - `eval_dataset`:两边都 0 行。迁移头两边都是 `0151_backfill_approval_user_id`,0152 可直接续。
   - `turn_seq`:**数据侧无从实证**(0 行,连一个样本都取不到),改由源码定判 —— `TurnFooter.tsx:157` 传 `turnSeq={turn.seq}`,`turn.seq` 即 `ConsoleTurn.seq`(`components/console/types.ts:29-30` 注释明写「0-based;history turns come first, live turns after」)= 控制台时间线的**局部下标**,`FeedbackBar.tsx:45` 原样 POST 成 `turn_seq`。而迁移 `0014_feedback.py:11` 声称它指 `event_log.seq`:`event_log` 表在生产与测试**都是 0 行**(真正在用的事件表是 `run_event`,生产 18 / 测试 10540 行),那个所指根本没有数据。结论:`turn_seq` 是 UI 局部序号无疑,死字段结论成立,**保留不动**。
3. ~~多副本下 `curation_worker` 是否两个 pod 都在跑(base `ENABLE_CURATION_WORKER=true` 全局,worker 自称 single-replica);同步 upsert 与 worker 的竞争靠唯一键 + 升级语义幂等,要实测。~~ **✅ 已核实(09-09)**:**两个 pod 都在跑**。`curation_worker.py:3` 的「single-replica」只是一句陈旧注释,不是机制 —— 全文件 grep `advisory|lock|replica` 只命中那句话本身,**没有任何 leader 选举 / advisory lock / lease**。
   - 配置面:`ENABLE_CURATION_WORKER=true` 只在 `infra/k8s/base/configmap.yaml:34`,两个 overlay 都没覆盖;两个生产 pod 各自在 pod 内读自己的 `Settings().enable_curation_worker` 均为 `True`(interval 300s / batch 200)。
   - 代码面:`app.py:1389 if agent_runtime is None:` 分支内 `:1665` 构造、`:1998` 启动 —— 部署形态下**每个进程**都起一个。
   - 运行期实证(不只看配置):测试环境按 `client_addr` 采样 `pg_stat_activity` 340s,**两个 pod 各自独立**发出了 worker 的 `curation_candidate` 预检查询 —— `172.16.176.31` 于 t=178s、`172.16.176.32` 于 t=276s,相隔 ~98s,正是两 pod 启动错峰的相位差;另一次 20s 采样还抓到 `.32` 的 `thread_meta` + `curation_candidate` 连发(一次完整 sweep)。生产侧,pod `…-sdpzg` 的 `expert_work_control_plane_curation_candidates_detected_total = 1.0`(该计数器只在 `curation_worker.py:228` 的 `run_once()` 内自增),同期 pod `…-pz5s8` 为 `0.0` —— 正是「两边都扫、谁先扫到谁建行、另一边预检跳过」的形态。
   - 今天不炸的原因已实测坐实:生产库上 `curation_candidate_trajectory_uniq (tenant_id, trajectory_key)` 唯一索引真实存在,配 `curation/sql.py:246` 的 `on_conflict_do_nothing(constraint=…)`。**结论:Task 8 的并发集成测是必做项**;P-2 的同步 upsert 只要复用同一把唯一键 + 升级 UPDATE 幂等即可,不需要引入锁。
4. ~~对接方在 `stream_format=legacy` 实时流里哪一帧拿到 `run_id`~~ **✅ 已核实(09-09)**:legacy 流首帧 `metadata` 就带 `run_id` + `thread_id`,`docs-site/guide/sse-events.md` 明写「保存 run_id」,对接方现有代码已在存。
5. ~~对接方客户端对新增 `feedback` 字段是否 strict 解析~~ **✅ 已确认(09-09,用户)**:对接方无 strict,自动忽略多出的字段。

## 8. PR 切分与验收

**PR1 存储 + 对外端点**(迁移 0152、store upsert、`api/external_feedback.py`、三张路由表、控制台 POST 回填 run_id、文档四页)
- 验收:三张表测试绿;他人 run → 404 且 external 信封;同 `(run,user)` 两次 → 一行、以最后为准;comment 带 NUL → 422 信封;purge 后归零(扩 `tests/test_user_purge.py:276`);`/items` 回显本人的 `feedback`。

**PR2 进池**(同步 upsert 候选、升级 signal、候选两列、改票标记、修两处词表漂移)
- 验收:对外打 👎 → 候选**当场**出行不等 300s;先 👍 后 👎 → signal 升级(变异:删升级逻辑必须红);promote 一路走通到 `eval_dataset` 行(今天走不通,这条就是修复证据);重复 👎 不产生第二条候选;👎→👍 候选保留且带改票标记。

**PR3 控制台可见**(GET 端点、`has_down_rated` 筛选、对话详情轮脚、候选行展示)
- 验收:勾「被点踩」只剩有 👎 的会话;跨租户不可见(照 `tests/test_rls_integration.py:270` 集成测);viewer 能看到评论原文(拍板)。

每条新断言按仓库规矩「实现改坏 → 红 → 改回 → 绿」自证;真栈验收用探针 user(`pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation`)与金丝雀 agent,不碰对接方 agent。

## 9. 关联

- 对外 API 定位与 key 档位:`docs/runbooks/production-release.md` §1.9、`docs-site/guide/auth.md`。
- 候选/评测集既有设计:`docs/streams/STREAM-SE-DESIGN.md`、`docs/research/2026-06-13-p1-s2-eval-platform-design.md`。
- 后续:B-46 按评测集跑评测出分数(ROADMAP 小 backlog)。
