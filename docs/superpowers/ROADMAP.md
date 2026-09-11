# 剩余工作总清单(全项目待办唯一入口)

> 📦 2026-08-21 清理:已收官 program 的历史 plans/specs(163 个文件)已从工作树移除,需要时从 git 历史找(该日期前的任意 commit);`.superpowers/` 执行台账归档在 `~/expert-work-superpowers-archive-20260821.tar.gz`。本文件引用的留存文档不受影响。

> 原为本机 git-ignored 文件 `.superpowers/sdd/ROADMAP-2026-08-13.md`,2026-08-17 搬进仓库,此后以本文件为唯一入口。
> 各 program 的完整执行历史仍在各自的(本机)`.superpowers/sdd/<plan>/progress.md`;入仓的设计与计划在
> `docs/superpowers/specs/` 与 `docs/superpowers/plans/`。

## 排期(2026-09-08 拍板;生产已于 09-08 上线 `ad79ba28`)

> 生产已在线,发布从「一个日子」变成**每周一班车:默认周二**(周一合完泡测试一天)。本周(09-08 ~ 09-12)只进测试环境。
> 并行按文件冲突分线,零交集的线 worktree 并跑;每条线一个或两个 PR。开工前按行内来源核实现状(本文件两次误报都是没核实)。

| 周 | 生产班车 | 同周开发(周五前上测试) |
|---|---|---|
| 09-08 ~ 09-12 | 无(09-08 已发 `ad79ba28`) | **✅ 六线全合(09-08 当天,main `77468612`,测试环境 smoke+金丝雀 PASS,记录 #1437)**:A MCP 探针拆 ExceptionGroup(消息+日志给叶子原因)/ B release.sh(X-14 P5 记录 PR 写上一版 tag + B-29 ③ 失败分支假成功)/ C X-8 收官(pgvector·pgbouncer 镜像进 GHCR + 沙箱镜像 build 走 buildx 缓存,integration 不再每 PR 烧 40 分钟)/ D B-42 worker token 错价(usage 按 provider·model 分桶 + admin-ui 按桶计价,2 PR)/ E B-33 模型参数 catalog 约束 / F B-23 platform_items 200 截断 |
| 09-15 ~ 09-19 | **班车 1(09-15)**= 本周六线 + 波 2 + **09-10 四 PR** | **✅ 波 2 提前全合(09-08~09,main `723c5a40`)**:#1440 Redis 全局 RPM 令牌桶(桶 key = handle + 凭据 ref;Redis 故障降级本地 + 60s 冷却;rerank/aux/judge 四条构建路径全接;`ResolvingReranker` 静态版不在生产路径未接)/ #1441 跨副本取消亚秒化(现状数出来 = 属主靠 `sse.py` 心跳 CAS,lease_ttl/3 = **10s**;新 kind `run_cancel`,五个取消入口收进 `cancel_run_two_level`,handler 走同一心跳 CAS;`verify_cancel.py` 加 2s 上界)/ #1439 B-31 ① credential-proxy 订阅总线四 kind + 跨包契约测试(复用 `control-plane-secrets/EXPERT_WORK_QUOTA_REDIS_URL`)/ #1443 RLS D 线(见下)/ #1442 alembic `fileConfig(disable_existing_loggers=False)`(集成 job 单进程跑迁移后采集期已建 logger 全被关掉,caplog 静默为空 —— 任何集成测试都可能中招)。**合后浮出的新项:B-43 / B-44 / B-45(见小 backlog)** |
| 09-22 ~ 09-26 | **班车 2(09-22)**= 留存 CronJob 生产 overlay(test 泡一周无事故才加)+ RLS 第二轮判定 | **✅ 波 3 提前全合(09-09,main `a7c29e20`)**:#1456 留存链(X-15 ① 第二套审批超时已删、B-45 retention job 接 `build_rls_sessionmaker`、CronJob 只进 test overlay、B-27 `threads/` 随对话死亡 + 孤儿 24h 宽限、B-28 产物 90 天、上传 90 天、X-4 ② 已删工作区 90 天硬删;根目录不变式 + tenant/user 目录 symlink 不 resolve)/ #1455 X-4 ① 成员清除两步 + 最后可达 admin 闸 / #1457 B-9·B-10·B-11·B-14 / #1452 X-5 / #1449 X-7 ②③④ / #1453 B-24 + B-44 ② / #1447 P-5。**班车 1(09-15)实际装载 = main 全部(波 1+2+3)**,生产不会拿到 CronJob(overlay 未加)。下一波待排:P-1(spec #1454)/ P-2(spec #1450)两份 spec 待用户审 → writing-plans;X-3 Spec2a webhook  **加:B-50 用户工作区按 agent 隔离**(spec+计划 #1490,14 task / 6 PR)—— 前置=B-49 viewer 收窄 + B-45 余 2 + 对外 `?scope=user`,三件完成后开工,PR1 地基零行为变更可先上 |
| 09-29 ~ 09-30 | **班车 3(09-29)**= 09-22 后合的(RLS 第二轮判定若已归零);国庆前最后一班 | 10-01 ~ 10-07 不发生产、不合大改 |
| 10-09 起 | 每条迁移单独一班 | **RLS 真 enforce(PR B,Task 5/6)= 节后第一班,合完只上测试泡一周** → 波 4 依赖迁移串行(都动 uv.lock):~~X-12 fastapi 路由自审~~ **✅ 09-10 提前做掉(#1483),这条链短一截** → X-11 mcp SDK 2.x + httpx2 → X-13 e2b;每条合前真栈测 MCP 工具 / 沙箱工具,做完删对应 dependabot ignore |
| 之后 | 按需 | 波 5 收尾池:X-1 沙箱波 4 / X-10 方案文档 / X-7 观测三小项 / X-9 perf 池 / X-14 P3-P4 / D-7 / X-5 / X-6 ③ / B-14 / B-9~B-11 / 小 B 打包 |

- **B-40 乱码验收**不占排期:对方发版当天查库贴结果。
- **拍板(2026-09-09,用户)**:① 留存:产物 90 天 / 上传 90 天 / 已删工作区库行 90 天销账;`threads/<id>/` 只跟对话生死;**工作区根目录其它文件(`style/` 风格锚、`MEMORY.md` 等、未登记文件)永不触碰,写成不变式+测试**(实测 ai-health-plan 的 `style/PLAN_STYLE.md` 在根目录、不在产物表,清扫器只归档软删工作区)。② P-3/P-4 先走流程(runbook §1.9,#1446),admin scope 代码收窄放波 5。③ P-5 = operator+(#1447)。④ **P-1 重新生成/编辑重发要做**,逐条拍板(09-09):**B 方案**(同 thread 原地标「已被取代」,不分叉不导出)/ 被取代轮的副作用(工作区文件、产物登记、长期记忆)**不撤销**,文档写明 / **计划(plan 通道)随之回退**,原则=那一轮没发生过 / 会话有 run 在跑 → **409 拒绝** / 被取代轮停在等审批 → **409 拒绝** / 同一轮旧版本**最多留 5 份**,超出真删最老 / 两轮都计费不退 / 对外文档(chat·sse-events·query·errors·examples)随 PR 同步。4 PR(内核+读面 → 执行侧整轮过滤+per-thread 并发闸 → `:regenerate`/`:edit` 端点 → 文档+控制台折叠态);**开工前 spike**:消息 id 跨 Postgres 往返是否稳定(存亡判据)、`aupdate_state` 后再起 run 的起点行为、plan 回退是否一行能做。⑤ **P-2 点赞/点踩要做,定位=改进 Agent、必须进评测回路**,逐条拍板(09-09):**按轮(run_id)打分**,可附不承诺可对应的 `item_id` 段落标签 / **👎 一条当场进策展待审池(不等 5 分钟 worker),👍 不进池** / **评论原文全员可见**(与会话原文 operator+ 门槛不一致,有意为之)/ **可改票**,后一次覆盖,已进池的记录保留并显示改票 / **只做到评测集为止**(3 PR:存储+对外端点(feedback 加 run_id 归因地基)→ 当场进池+升级 signal+修 promote source 词表漂移 → 控制台可见);对外文档随 PR 同步。**新入册 B-46:按评测集跑评测出分数**(见小 backlog)。⑥ X-3 触发器通知:调研结论主流一边倒 webhook(OpenAI background/LangGraph cron/Anthropic managed agents 均 webhook,附状态查询兜底),**只做 Spec2a webhook,2b 长连接不做**,排期待定。⑦ B-30 kimi-k2.7-code 延后。⑧ X-4 ① 成员清除**并进波 3 做**。
- **✅ 波 3 提前开工即收官(09-09)**:R 留存链 #1456(X-15 ①+B-45+CronJob test 先 → B-27/B-28/上传 90 天/X-4 ②)/ M 成员清除 #1455 / S1 B-9·B-10·B-11·B-14 #1457 / S2 X-5 #1452 / S3 X-7 ②③④ #1449 / S4 B-24+B-44 ② #1453。CI 红一条是真 bug(`list_versions_expired` 用 uuid4 `id` 做 tiebreak,同一产物两版本顺序随机,本地 3/5 红)→ SQL 与内存 store 同改 `created_at, artifact_id, version`。
- **P-1 / P-2 开工(09-09~09-10)**:两份 spec(#1450 / #1454)与两份实施计划(#1460 / #1461)已合;**波 0 = 各自 Task 0 真栈核实**(#1462 / #1463)出了三条改设计的结论:① P-1 取轮边界原设计 `aget_state_history(filter=)` 在最长会话上 **810–950 ms / 52 MB**,换成「两列窄 SQL + 单条 `aget_state`」后 **40–97 ms**,`base_checkpoint_id` 锚点列实测**不必要**;② P-1 压缩摘要**不落检查点、无 run 戳**,spec §6 顾虑不成立;③ P-2 控制台 promote **恒 422**(前端发 `promoted_candidate`、后端 Literal 只认 `golden|trajectory|regression`),评测回路最后一步今天是断的 —— 生产/测试 `eval_dataset` 双双 0 行、`feedback` 表建表至今 `n_tup_ins=0`;另查明反馈按钮**只在调试台渲染**(`ConversationDetail.tsx` 传 `readOnly`),对话详情页点不到,零行由此解释。**波 1 = 两条线各自 PR1 全合(09-10,main `258f2202`)**:#1465 P-2 PR1(迁移 0152:`feedback` 四列 + 部分唯一索引、`curation_candidate` 四列;`FeedbackStore.upsert`/`list_for_thread_scoped`/`down_rated_thread_ids` 双实现;对外 `POST /v1/agents/{code}/runs/{run_id}/feedback`;`/items`+`/messages` 回显**本人**反馈;控制台 POST 改必带 `run_id`;对外文档五页)、#1464 P-1 PR1(迁移 0153:`agent_run` 两列 + `thread_message.superseded_by`;`supersede_run` 内核 + 7 条真 Postgres 集成测;六处读面字段)。**用户新拍板(09-09)**:对话详情页放开打分,**operator+ 能打、viewer 只能看**,重跑仍挡;四个下游(候选池/记忆待复核/技能蒸馏/回滚闸)**一律不按来源过滤**(回滚闸取的是「该技能版本时间窗内被用过的会话」,员工说砸了与用户说砸了同样有效);列并进 0152,做成 P-2 **PR4**。**波 2 = 两条线各自 PR2 全合(09-10,main `1e882f01`)**:#1468 P-2 PR2(候选行接 `feedback_run_id`/`feedback_comment`/`feedback_changed_at`;`upgrade_to_negative` + `mark_feedback_changed` 双实现;`TrajectoryReader.find_by_thread` 按 `finished_at` 取最新;新模块 `feedback_candidates.py`,对外与控制台两条写路径 upsert 后同步进池;worker 预检去重从「有候选就跳过」改「有候选就可升级」;修三处词表漂移 + promote 弹窗补 `expected`)、#1469 P-1 PR2(`agent_node` 在 pruner/working_window/compressor 三道闸**之前**整轮剔除被取代消息;`spawn_run(supersede=)` 用 `supersede_thread_lock` 把「取代 → 建行」整段包住;`:regenerate` 旧轮 `[System, Human]` 原件换新 id 重放,queue 与 stream 两分支走同一个 `replay_graph_input`)。**变异自证逮到三处假绿**:① P-2「别人打的 👎 被错标成本人后改 👍」的不变式在 store 层与端点层**两个**断点都测不出(计划只预判了前者且解释成「这行无所谓」),补断言后都红;② P-2 的 `except` 分支原来什么都不写,审计里「进池失败」与「真 no-op」长得一模一样,现记 `candidate="failed"` 并补两条写路径各一条永久回归用例;③ **计划 Task 6 Step 5 我判「过滤位置测不了」是错的** —— working_window 按 `HumanMessage` 数轮,过滤放在后面时被取代轮会先占一格窗口预算、把仍有效的第一轮挤出去,新增 `test_filter_runs_before_the_working_window` 只红这一条。另两个执行入口已查实不受影响(`orphan_sweep` 传 `graph_input=None` 纯 checkpoint 续跑;`trigger_firing` 自拼输入不读 `enqueued_input`,且无触发器路径传 `supersede=`)。**波 3 = 两条线各自 PR3 全合(09-10,main `807fc0d6`)**:#1473 P-2 PR3(控制台 `GET /v1/sessions/{thread_id}/feedback`;`GET /v1/conversations?has_down_rated=true` 并进 `narrowed_ids` 交集;对话列表「被点踩」勾选框;详情页轮脚新 `FeedbackSummary.tsx`,`feedback`/`feedbackOf` 沿 TurnFooter→TurnBlock→Transcript 串起来、调试台零变化;候选行显示用户原话 + 「后改为 👍」Tag)、#1472 P-1 PR3(对外 `:regenerate` / `:edit` 两端点,共享 `_supersede_and_run`;`agents.py` 抽出 `resolve_external_files` / `external_run_bounds_error`;Idempotency-Key、两张路由表登记、NUL 逐端点 7 条、`errors.md` 六个错误码)。**用户拍板(09-10)两条**:① **第六个错误码 `RUN_BOUNDARY_UNRESOLVED`(422)要补**,销掉 PR1 留在 `supersede.py` 的 `TODO(Task 8)` —— 原来「轮边界划不出来」借用 `RUN_NOT_LAST`,而 `errors.md` 教对接方「去 run 列表取最新 run_id 再试」,但这种情况下目标**本来就是**最后一轮,照做会拿回同一个 id、撞同一个 422,永远绕不出来。**全仓扫描确认生产代码里只有 `supersede.py` 抛 `RUN_NOT_LAST`、恰好两处**,两支边界拒绝都改成新码,spec §4 补一句「`RUN_NOT_LAST` 从此只用于 `rows[-1].run_id != target_run_id`」把边界钉死;② **控制台反馈 GET 的归属轴保持放开**(不挂 `caller_owns_thread`)—— 对外 API 建的会话 `user_id` 必填、写的是对接方自己的终端用户标识,**控制台上没有任何员工是这些会话的 owner**,挂上归属门 = 只有租户 admin 能看到评论,而 👎 全部来自对外终端用户,等于把功能关掉、也架空 09-09「评论原文全员可见」那条拍板;与 `/v1/conversations`(2026-08-14 裁决:对话是运营对象,读面对全员开)同族。代价已知并接受:租户内任何持 `session:read` 的员工可读任意终端用户的反馈评论原文;实现与测试都留了注释防后来人当漏闸「修正」。**波 3 逮到的假绿**:P-2 T12 那条跨租户用例被**第一道门(404 归属)先挡住**,第二道门(读行的租户谓词)一次都没被测到 —— 把 `list_for_thread_scoped` 换回无谓词版本 15 条全绿;补 `test_get_feedback_read_carries_its_own_tenant_predicate`(thread 在本租户、同 id 上挂别租户的反馈行)才咬得住,T13 同形状的洞一并补。**勘误**:姊妹端点 `GET /v1/sessions/{tid}` 与 `.../messages` 挂的是 `caller_owns_thread`(admin 或 owner),不是计划/spec 写的「operator+」,实际比 operator+ 更紧。**波 4 = 两条线各自 PR4 全合(09-10,main `44db27eb`),P-1 / P-2 两条线全部收官**:#1477 P-2 PR4(`allowRate` 沿 `Transcript → TurnBlock → TurnFooter` 传下去、默认 `false` fail-closed;`POST /v1/sessions/{thread_id}/feedback` 加 `require("session","write")` = operator+;`feedback_source` 从协议到前端 Tag 打通,四个下游消费者只加注释零逻辑改动)、#1476 P-1 PR4(`chat.md` §2.10「重新生成与编辑重发」+ 其余六页增量 + 侧栏;新 `SupersededFold.tsx` 折叠壳、墓碑「内容已清理」、只读无重跑入口;runbook §2 一句)。**波 4 逮到的**:① **加闸前 viewer POST 反馈实测返回 201 而非 403** —— `require_key_scope` 的 docstring 自陈不管人类 JWT、`console_only()` 又挡掉所有 API key,两者叠加使那句 scope 检查在控制台路由上恒为空转(引出 B-49);② 计划写的 `input` 上限 32000 与实现的 `MAX_RUN_INPUT_CHARS = 65536` 不符,按实现定稿;③ **spec §3 关于 `feedback_source=NULL` 的理由不成立** —— 它说 worker 建的候选「归因不到某一条 feedback」,而 `curation_worker._evaluate` 已经从 `newest_down` 取到了 `feedback_run_id`/`feedback_comment`;拍板补齐(单独 PR,同时改 spec 那句错理由)。**删掉两条重言测试**:P-2「翻转 `readOnly` 则重跑按钮出现」(重跑开关是 `onRetry` 传没传,该页从不传 handler)、P-1「折叠态里没有重跑入口」(断言一个从未存在过的 testid)。两条都改成写明「靠评审而非测试保证」。**rebase 冲突一处**(`Transcript.tsx` history 循环,P-2 加 `allowRate`、P-1 换成折叠壳),两边都保留;并逐行对过 `origin/main` 确认 P-2 那 46 行测试一条没丢 —— 前几轮踩过「机械两边都保留把两个测试函数熔成一条」且 `git status` 干净。**顺带补齐**:`examples.md` 侧栏原停在 10.8(P-2 加 §10.9 时漏登记),10.9 + 10.10 一并补上,锚点做了产物级验证与反证(故意改错锚 → 10 条死链 → 改回 → 零死链)。两份计划在 `docs/superpowers/plans/2026-09-09-*.md`,任务编号与验收判据齐全。**Task 12 真栈验收 20/20 PASS(09-10,测试环境 `e1601bd7`,零 FAIL 零不可判)** —— 探针 user `pc:proj_8f52…:emp:probe-delegation` + 金丝雀 agent `release-canary`,会话 `165e1b23-b9bb-4e7b-92f4-63681f851858`。验到:① **暗号法证明被取代的轮真的没进上下文** —— 第二轮回 BANANA、`:edit` 改成 CHERRY、第三轮问「我让你回过哪些水果词」答 **CHERRY 且无 BANANA**(此前只在单测与真 graph 探针上验过,这是真栈第一次);② 读面双向指向(旧轮 `superseded_by` 指新轮、新轮 `regenerated_from` 指回、第一轮未被牵连);③ 被取代轮 2 条消息带标记且原文仍读得到(不删只标);④ 三个错误码实测 —— 已取代轮 409 `RUN_ALREADY_SUPERSEDED`、非最后一轮 422 `RUN_NOT_LAST`、`:regenerate` 带 `input` 422 `INVALID_REQUEST`(文档说不忽略,确实不忽略);⑤ 同 Idempotency-Key 两次拿同一个 run_id、`runs[]` 只多 1 条;⑥ 打分落库 + 条目接口回显本人 + 可改票 + 改票后回显 `up`;⑦ 控制台折叠态由用户在真环境肉眼验收(「已被取代 → 新一轮 …」summary 行渲染正确,旧轮默认收起)。**过程记录**:首跑空转 19 分钟,根因是探针把 run 状态打到 `GET /v1/agents/{code}/runs/{run_id}` —— **对外面没有这个端点**(只有 run 列表),裸 404 被 case 分支吃掉,每轮轮询满 360s;产品零缺陷,改从 run 列表读状态后一次通过。**至此 P-1 / P-2 两条 program 全部关账。**
- **RLS 拍板(2026-09-08,用户)**:真 enforce(PR B = 计划 Task 5/6)**顺延到下下波**(节后 10-09 起第一班)。原因:① Task 3 的 Detect 信号**从未可归因**——JSON 格式器(`expert_work/common/observability/log.py`)把 `stack_info` 列在跳过属性里,24h 真栈 prod 5711 / test 4347 条告警全是同一句话,93% 无 trace 无 run,Gate 1 观察窗无法做;② 计划/spec 08-21 已清出工作树(git 历史 `6e855aea` 可捞)。波 2 D 线 = 修可归因 + 本地全量枚举第一轮判定;发测试观察一周拿真栈 caller 聚合做第二轮;归零后才 PR B。**✅ D 线 #1443 已合(09-09)**:归因失效其实**两层**(格式器丢 stack_info + SQLAlchemy 同步 ORM 跑在 greenlet 里,listener 内无应用帧,要顺父 greenlet `gr_frame` 链走),现在日志带 `rls_caller`/`rls_caller_outer`,每 caller 60s 限 10 条首条必打、512 caller 上限、探测器自身异常兜底绝不砸事务;本地全量枚举 2092 条 / 47 函数 → 生产信号归零,顺带修两条真 bug:`tenant_status.is_suspended` 在 AuthMiddleware(RLS 中间件外层)无租户上下文读 `tenant_config`(enforce 后暂停租户拦不住)、memory DLQ worker 认领扫描跨租户。**待判定(真栈第二轮)——✅ 2026-09-11 已跑,但结论是「观察给不出判决」**:三个长周期 worker 的 24h `rls_caller` 聚合如下。**生产 24h 零信号,而这不是修好了的证据** —— 三个里有两个在生产**根本没运行过**(这正是「在不可能失败的条件下验证等于没验证」)。① **`skill_curator` 两个环境都被显式关掉**(`EXPERT_WORK_ENABLE_SKILL_EVOLUTION_WORKER: "false"` 写在 base configmap,test/prod 同值),两边都零信号,**观察不出任何东西,只能靠读代码判或书面接受**。② **`workspace_janitor`**:生产 24h 内连一行日志都没有(含 `rls_caller` 与它自己的 INFO);测试环境 **1536 条**,命中 5 个调用点(`_loop:192` / `run_once:212` / `_sweep_archives:266` / `_archive_one:279` / `_sweep_sizes:318`,其中 `_sweep_sizes` 独占 1280)。生产 configmap 的 `OBJECT_STORE_BACKEND=s3-compatible` 与 `WORKSPACE_NAS_ROOT=/mnt/workspaces` 都满足 `app.py:1603-1616` 的启动条件,剩下的变量只有 `resolved_workspace_quota is not None`;`elif` 分支的 `workspace_janitor.not_started_memory_object_store` 日志也没打过,**为什么不上岗待查**。③ **`memory_consolidator`**:生产有 30 行日志但零 `rls_caller`;测试环境 **~640 条**,命中 9 个调用点(`_loop:606` / `run_once:629,642` / `_run_sweep:653,662,666,684` / `_sweep_user:728,761,785` / `_find_candidate_clusters:845,861` / `_list_tenants:1139` / `_resolve_thresholds:1146`)。**判决**:janitor 与 consolidator 在测试环境**实证确认无租户上下文**,enforce 前必须接;`skill_curator` 无法观察。**enforce 前置因此不是「三选一」而是三条各自处理。****Task 6 预警**:persistence 下 15 个测试模块依赖默认 `bypass=False`,整包 autouse bypass 夹具会让它们静默走 bypass,enforce 时按模块显式设。
- **✅ 新入册即销案(2026-09-08,#1428)**:MCP 探针把真因吞成 `ExceptionGroup` —— `mcp_probe.py` 只把异常类名放进消息,`mcp_probe.failed` 日志不带栈;真栈复现:生产建 `deep-ai-health-mcp` 六次 422 无一处能看出是 401 还是 DNS(从 pod 手探才定位到对方生产服务在、无 token 回 401)。修法=递归拆 ExceptionGroup 取叶子异常进消息(截断、脱敏),日志带 `exc_info`。

- **拍板(2026-09-11,用户)**:① **B-49 —— viewer 定义为「只读旁观者」**:读面全保留,**写操作一律 operator+**。据此收窄 18 条(sessions 族 11 含扫描时不可判的 `runs`/`uploads`、artifacts 2、plan 1、uploads 1、workspace 1、knowledge `bases/{name}/test` 1、skill-evolution `promote-requests` 1);kill-switch 与 triggers 已在 #1489 先行修掉。**三条敏感读暂不动、单独拍**:`GET /v1/tenants/{tid}/config/credentials`、`GET /v1/webhook-endpoints`(+`/{id}`)、`GET /v1/audit`(+`/{id}`,该条要动 `rbac.py` 决策矩阵,不是加闸能解决的)。② **对外 `?scope=user` 并集要做**(已告知对接方):三个列表端点支持、默认 `agent`;**下载端点不支持**(放开等于让 A 的 code 取 B 的字节);`scope=user` 的条目带 `agent_code`(不是内部 `agent_key`)标归属;`shared/` 两种 scope 都不对外投影。契约与测试见计划 §9.4。③ **B-50 工作区按 agent 隔离排进正式排期**:B-49 收窄 + B-45 余 2 + `scope=user` 三件完成后开工,按计划的 PR1→PR6 走。
- **2026-09-10 收工(P-1/P-2 关账之后的四个 PR,全部已合)**:**#1483 X-12** fastapi 解钉 `>=0.138` + 五族路由自审改走 `iter_route_contexts`(顺带撤掉 `tools/ci/protected_pins.toml` 里同步登记的两条钉子——不同步改 CI 直接红,那个 guard 的设计就是「合法升级=同 PR 改这里」);**#1482 小批五条** B-47 classid 登记表(挪号方向经纠正:让位的是 janitor 不是投递锁,判据是滚动发布的互斥真空谁扛得住)/ B-1 webhook 创建者 / B-13 死参删除 / B-17 ①② / B-18 对外 404 文档;**#1489 B-49 两个真洞** 租户 kill-switch 仅 admin + triggers 四条 operator+;**#1490 新立项** 用户工作区加 agent 维度(spec + 计划,见 B-50)。**当天三条方法论教训**:① 我对 B-49 的静态扫描**两版都错、方向相反** —— `console_only()` 大多挂在 `APIRouter(...)` 构造器上、角色闸大量在 handler 体内,grep 不作数,判据必须是拿三种身份真发一次请求;② `configurable` 的执行入口是**四个**(队列 worker / stream 端点 / 触发器 / 孤儿复活),「执行入口三个,规矩只写一处就漏两个」那条教训又来一次,这次配了 AST 穷举测试防漏;③ **并行 subagent 共用同一个 scratchpad 根目录**,两个任务都写 `pr_body.md`,一条 `python -c ... && gh pr edit` 在断言失败后仍执行,把 A 任务的 PR 正文推到了 B 任务的 PR 上(已还原)——worktree 隔离的是仓库不是 scratchpad,以后 brief 里按任务分子目录 |

## 状态(2026-08-30)

> 近三日新增(08-28 晚 → 08-30),按 program 归拢;逐 PR 明细见「生产发布前置」节末的进度条目。
>
> - **B-36 弹性 worker 预算 ✅(#1368)/ B-37 子 Agent 上下文继承 ✅(#1369)** —— 两条都已合并 + 发测试 + 真栈实证,下文表格已销案。
> - **可观测性四修(08-29)**:#1370 过程条把子智能体耗时计了两遍、#1373+#1382 三个执行入口的 token 记账 trace 绑定、#1374 子智能体 token 计入本轮总数、#1367 产物行漏 worker 产物。
> - **「保存 Agent 配置 ≠ 立即上线」program ✅ 全交付(08-29~08-30,五 PR)**:#1387 保存前 dry-run 构建 / #1388 编辑必须带 If-Match / #1389 存草稿 + 显式发布(迁移 `0150_agent_spec_draft`)/ #1391 调试台用草稿试跑 / #1390 对话页标出会话期间配置被改过。地基是 #1385(缓存改内容寻址)+ #1386(run 记下实际执行的配置版本)。
> - **「委派时子代看得见本轮附件」✅ 全交付(08-30,两 PR)**:#1392 文档 + 图片进子代种子消息(图片按**子代自己**的能力三分)、#1393 改走 `AgentState` 通道所以审批续跑 / orphan 复活不再丢。
> - **跨团队定位:上传中文文件名乱码** —— 根因在对接方(busboy 解 multipart 头部走 latin1,multer 不设 `defParamCharset`),我方在生产 pod 里三情形对照实测排除;对方已修待发。**我方验收待做**,记 B-40。
>
> **未完成的只有真栈复验**:上面两个 program 的行为都只能在发测试环境后验(见 B-39)。

- **阶段 0/1/2/3 全部交付并上线**(见下文各节的 ✅ 标记)。
- **收官后的文档反馈波全部交付并上线**:
  - #1189 线 B 文档可读性(SSE 章重写、代码块标题插件、「帧」→「事件」)
  - #1191 文档站宽屏版式
  - #1193 线 A 附件模型统一(`upl_` 统一 id、`files:[{upload_id}]`、附件下载端点;设计 `docs/superpowers/specs/2026-08-17-external-upload-unification-design.md`)
  - #1195 第三轮可读性(审计驱动,枚举值取代码真值;审计 `docs/superpowers/specs/2026-08-17-external-docs-readability-w3-audit.md`)
  - #1197 第四轮整站重写(第三方工程师视角 + 企业级语气;**写作规范 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`,以后改对外文档先按它自检**)
- **调试台 / 对话记录 / Run 详情交互重设计 program(2026-08-17 立项)—— ✅ 全收官(2026-08-20)**:
  PR0 #1200(Jinja 两 bug)→ PR1 #1202(对外 `plan` SSE 事件 + 文档)→ PR-A #1207(三栏壳 + 轨迹合一)→
  PR-A.1 #1214(六条界面反馈)→ PR-A.2 #1216(轨迹对标 deepseek-harness 重写)→
  PR-A.3 #1218 + follow-up #1219(Schema tab / SYSTEM 行 / TTFT 补数据)→
  **PR-B #1221(对话记录页 + Run 详情页切 console 组件,退役 TurnCard 集群 −9.6k 行)**。
  测试环境 `afe00ebb`,两页真栈验收 15/15,记录 PR #1222 已合并(`72a5421b`)。
  设计 `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md`;遗留见下文「D · 调试台 program follow-up」。
- **仍然有效的剩余项**(2026-09-10 复核)= 「待产品拍板」P-3 / P-4(P-1 / P-2 已于 09-10 全部关账)、
  「其它 program 挂起项」X-1 / X-3 / X-4(余项)/ X-6 / X-7 ① / X-9~X-11 / X-13 / X-14 P3-P4 / X-15 ①、
  「小 backlog」B-2~B-7、B-9~B-12、B-14~B-16、B-17 ③、B-21、B-27 余项、B-28 余项、B-29 ①②、B-30、B-38、B-40、B-43~B-46、**B-48~B-50**、
  「调试台 follow-up」D-7。已销案的:D-1~D-6、B-8、B-19、B-20 主体、B-22、B-25、B-26、B-34、
  **B-35 / B-36 / B-37 / B-39**、**B-31 ② / B-32(08-31 发布前必修批)**、**B-41**、X-2、**X-8 全部(含 09-08 余项)**、**X-15 ②**、X-16、**B-23 / B-33 / B-42 / B-29 ③ / X-14 P5(09-08 六线批)**、**B-31 ① + 波 2 全部(09-09,#1439-#1443)**、
  **X-12 / B-1 / B-13 / B-17 ①② / B-18 / B-47(09-10 四 PR 批)**、**B-49 的两个真洞(09-10,#1489;剩 ~28 条写面等拍板)**。
  X-3 起与 D 节为 2026-08-20 从各 program 记忆汇集补录,
  **开工前先按行内注明的来源核实现状**(记忆反映的是记录时点;本文件已两次因未核实而误报进度)。

---

## 🚀 生产发布前置(2026-08-23 立项;**发布日 = 2026-09-10 周四**(2026-08-31 用户拍板)(时间线:原定 08-26 → 08-24 改 08-30 → 08-29 延期 → 08-31 定 09-10。**勘误:本文件此前把 08-30 称作「周六」,该日实为周日**——旧条目里的星期标注不可信,以日期为准);三道拍板 2026-08-24 已收:单副本首发(**2026-08-26 推翻改多副本首发,阻塞项七 PR #1312-#1318 已全清**)/ 书面接受单层隔离 / 告警走企微 —— 记录在 docs/runbooks/production-release.md)

> 来源:2026-08-23 生产就绪盘点,对照 `docs/research/2026-07-28-multi-replica-readiness-audit.md` 逐条核实现状。
> **好消息:审计第 0/1 波大头已落地**(实测核过:webhook 投递有 `claim_ready` + `FOR UPDATE SKIP LOCKED`;多副本启动守卫真存在——`app.py:1188` 未配 quota Redis 直接拒启;文档上传已走对象存储;base configmap 已 `SINGLE_INSTANCE=false` + postgres checkpointer + s3;prod overlay 骨架在,占位符待填)。

### A · 必须完成(不做 = 生产事故或安全洞)

| # | 项 | 内容 | 量级 |
|---|---|---|---|
| PROD-1 | ~~**live SSE 跨副本兜底**(X-10 波 2-1)~~ **✅ 已交付(2026-08-26,PR #1312;2026-08-26 用户拍板多副本首发,推翻 08-24 单副本拍板)**:b1 方案落地 —— 非属主副本 `/events` attach 轮询 durable `run_event` 表尾随(`has_live_stream` 只认 publisher-fed,防早 attach 毒探针;终态后静默两轮再收尾;end 帧从 run 行取 status+artifacts)。同批 #1313 CAS 守卫(`expected_statuses`+`guard_claimed_by` 堵取消/重宣两洞,顺带修 #1305 审批续跑产物种子 no-op)、#1316 verify_cancel 跨副本取消验收 harness、#1318 收口(prod 摘单副本 patch 回 base 2 副本 + smoke replicas 节)。测试环境 `89c63dc1` smoke 全绿;~~真跨副本 attach 带凭据验收待跑~~ **✅ 已过(2026-08-27,`3fc9350e` 真栈)**:双副本同时 attach 同一 run,非属主副本收到完整帧流+end=success;跨副本取消链亦实证(CAS 立即翻转 user_cancel/非属主 attach 收 end=interrupted/8s 不复活/属主心跳停走) | ✅ |
| PROD-2 | ~~**alertmanager receivers**(X-7 ①)~~ **✅ 已交付(2026-08-24,PR #1257)**:企微群机器人方案 —— 集群内 wecom-adapter(ConfigMap 脚本 + 复用 control-plane 镜像)converts Alertmanager JSON→企微 markdown,p0 追加 @all;URL 走 Secret `wecom-alert-webhook`(未建则 log-only 降级);**剩用户侧一步:建群机器人 + create secret(步骤在 base/secrets.example.yaml)** | ✅ |
| PROD-3 | ~~**RLS 闸 1 拍板**~~ **✅ 已拍板(2026-08-24):书面接受单层 ORM 隔离首发**(记录在 production-release.md 拍板记录节);硬要求=生产应用账号建成非 superuser、无 bypassrls(secrets.env.example 已写明);FORCE RLS(捞回 PR B)= 发布后第一波 | ✅ |
| PROD-4 | ~~**retention-cleanup-job 第二套审批超时**~~ **✅ 已定案(2026-08-24):核实 infra/k8s 零 CronJob manifest** —— 该 job(连同 billing-rollup / event-log-archive / audit-backup)只有代码没有部署物,test/prod 都不跑,风险不进生产;代码修回归 X-15①,**该 job 部署前必须先修**(production-release.md §4 已钉) | ✅ |
| PROD-5 | ~~**发布工程收尾**~~ **✅ 已交付(2026-08-24)**:prod overlay 补成与 test 同构(镜像 mirror 全表 / langfuse 两 patch / searxng CN / NAS PV patch / 单副本 patch / configmap 沙箱六键+password 开通+bootstrap 邮箱 / secrets.env.example 全量重写——骨架曾缺 18 键)+ release.sh/smoke.sh/rollback.sh prod 路径(域名走 ~/.kube/expert-work-prod-params.env 不进 git;placeholder 预检;交互确认门)+ **docs/runbooks/production-release.md**(开荒清单+发布+回滚+已接受风险);剩真值填充=开荒日照 runbook §1 | ✅ |
| PROD-6 | ~~**在途三 PR 合并 + 测试环境验证**~~ **✅ 已完成(2026-08-24)**:#1252/#1253/#1254 全合并,测试环境发布 `1e6295ec` smoke 9/9(记录 PR #1256);交互面(对话页 live/批拒取消/审批两 tab)待人工点验 | ✅ |

### B · 强烈建议(不做需书面接受风险)

| # | 项 | 触发条件 / 理由 |
|---|---|---|
| PROD-7 | **X-14 P2 钉版卫兵 ✅(2026-08-26,#1317)+ P1 金丝雀 ✅(2026-08-27,#1330:canary.py + release.sh 阶段 6 + seed CLI;测试环境真栈首跑 PASS 35s 五项全绿)** | e2b 事故复盘产物三层防线补齐;prod 侧只差周六开荒时跑一次 §1.6.7 seed(选 prod 有平台 key 的厂商) |
| PROD-8 | ~~**egress token 24h 到期出网全挂**~~ **✅ 销案(2026-08-24 核实代码,ROADMAP 此条已过时)**:沙箱 PR-B #1b 已修 —— `agent_sandbox.py` 热会话总年龄封顶 `egress_token_ttl_s // 2`(12h)超龄强制重建,会话必死在 token 之前;407 可观测兜底(egress 审计指标)同波落地(PR-B #1a)。X-1 的 W1 遗留清单同步作废该条 |
| PROD-9 | ~~**X-3 触发器投递 CAS**~~ **✅ 已交付(2026-08-26,PR #1314)**:`deliver_run_result` 的读-查-写整段包进 per-thread `pg_advisory_xact_lock`(classid 8619,key=hashtext(thread_id);无 session_factory 退单进程语义);cron claim/retry/finalize 的 CAS 此前已安全,病灶只在投递段。testcontainers PG 集成测试实证同 thread 串行、异 thread 并行 | ✅ |
| PROD-10 | ~~**B-19 配额维度混扣**~~ **✅ 已修(2026-08-27,PR #1338,详见 B-19)** | ✅ |
| PROD-11 | **P-4 拍板**(api_keys 凭据横向扩散) | 发 key 给第三方前至少把「只发 write key、永不发 admin」写成硬流程;修 rbac 映射另议 |
| PROD-12 | ~~**X-10 波 2-2 供应商 RPM 全局化**~~ **✅ 已交付(2026-08-26,PR #1315+#1318 静态除法)**:`effective_rpm = max(1, ceil(rpm / EXPERT_WORK_REPLICA_COUNT))`,env 已进 prod+test 两 overlay(=2),smoke 校验 env==spec.replicas 防分母漂移。Redis 全局令牌桶(真·全局化,弹性扩容不用改 env)仍是 backlog | ✅ |

### C · 明确不阻塞发布

X-13 / X-11 / X-12(钉版迁移池)、X-5 / X-6 / X-9、D-7、B-20 ②(通知路由)、P-1 / P-2 / P-5、B 系小项。(X-16 批 E E3b 已于 08-27 交付)

**进度(2026-08-24 周一收账,当晚改期周六)**:A 级六项中 2/3/4/5/6 全清,PROD-1 拍板降级为扩容前置;主流程 BUG-1~5(#1259-#1261)+ 第二轮 BUG-6~8(#1265)全修,测试环境 `31e203a0` smoke 双发全过,平台技能 category 用户补录 51/52。

**进度(2026-08-26 用户拍板多副本首发 → 08-27 夜收账)**:08-24 单副本首发拍板被推翻,按 plan A 一夜清完全部多副本阻塞项 —— 七 PR #1312-#1318 全合(PROD-1 / CAS 守卫 / PROD-9 / PROD-12 / verify_cancel / PROD-7 P2 / 收口),测试环境发 `89c63dc1`(记录 #1319),smoke 含新 replicas 节全绿,prod overlay 回 base 2 副本。**剩余排期**:

- ~~周四~周五~~(**排期已随 08-29 延期作废,新日程见本节开头的发布日 2026-09-10;下列内容仍是待办清单本身**):①资源开通+开荒(production-release.md §0-§1,用户侧为主;kubeconfig/params.env/非 superuser 应用账号/企微告警群+Secret)② 开荒完成后 prod overlay 渲染预检(placeholder 扫描)③ 测试环境真栈验收:跨副本 /events attach(带凭据)+ `tools/ha/verify_cancel.py` + PPT 内容质量人工抽查(用户)
- ~~周六~~ **发布当日(2026-09-10)**:照 runbook §1-§3 开荒 seed → `release.sh prod` → smoke → 回滚待命
- 发布后第一波:RLS PR B / Redis 全局令牌桶 / 取消亚秒化(invalidation_bus 地基已就位)/ **B-32 redis Lua 回填量纲 1000×**(QPS 配额在 redis 引擎形同虚设)/ **CI integration 基础镜像镜像离 Docker Hub**(08-27 一天四次 40 分钟限流假红,提档)/ 按需 PROD-11(PROD-10=B-19 已于 08-27 修,X-14 P1 与 E3b 已提前交付)

**进度(2026-08-27 白天批收账)**:收账 #1320;功能四连 —— #1321 glm-5.3-flash 上架(`ModelEntry.always_thinking` 声明式字段,关思考=降 `reasoning_effort:"low"`;GLM 独有 `clear_thinking` 保留式思考参数刻意不发,与 B-30 `thinking.keep` 同族)、#1322 `dynamic_workers.model` 临时子 Agent 模型覆盖(校验器禁 fallback 链)、#1323 激活语义改「登录过就算」(MemberActivationMiddleware 认证路径激活,首 run 钩子退役)、#1326 成员列表「最后活跃」列(15 分钟节流 bump;CI 逮到副作用=调用者也被自动注册进 tenant_user,测试已适配);文案专业化两连 —— #1324「努力程度→推理深度」+ #1325 全站术语统一(智能体/对话/全角标点/调试台运行时文案迁 i18n,零键误伤)。测试环境发 `e891b918`(双镜像,记录 #1327),smoke 含 replicas 节全绿。存量「已邀请」账号无历史回填,等各自下次登录自然激活(设计语义,用户已接受)。

**进度(2026-08-27 第三批:发布后第一波提前清账 + UI 反馈 + 技能批量 + 委派增强)**:B-19/B-25/B-26 三小修并行 worktree 全合(#1337 围栏 unspotlight / #1338 配额维度路由+黏性 Retry-After / #1339 防御参数单源对齐,CodeQL 循环导入两轮修最终 BuiltAgent 挪中立模块根治);测试环境发 `39b98b6d`(记录 #1341,smoke+金丝雀 24s PASS)。当日用户反馈六连:#1340 MCP 工具弹窗放宽+只看已选、#1342 所有者列显成员名+401 跳登录+入参按配置序可复制+变量区多列内滚、#1343 调试台重设计(左对话右设置侧栏/系统提示词卡+只读抽屉/必填前置+选填折叠/说明挪 tooltip/变量草稿/输入框 placeholder 改「输入消息」——蓝色巨块真相=操作人被旧文案误导粘贴了 prompt)。#1344 平台技能批量导出/导入(生产开荒搬运,category 走 sidecar 防丢,runbook §1.8 配套落账)。**动态子智能体委派增强层 0-3 全交付**(#1345 工具描述形状判据+规模分档注入 / #1346 配置页生成委派策略(辅助 LLM 读 manifest,人审入 prompt)/ #1347 计划≥2 未完成条目时注入委派提醒,去重 hash 刻意不含 status;层 4 per-agent plan_first 执行模式备选,等委派率数据)。sop2-designer 对接双侧收官(key=A 方案租户级通用;对方交付帧统一 harvest/x-harvest)。CI integration 当日四次 Docker Hub 限流 40 分钟假红(egress e2e 在测试内 build 镜像),镜像离库结构修提档进发布后第一波。

**进度(2026-08-27 第二批:E3b 全量接线 + 金丝雀)**:三路侦察(E3a 总线现状/剩余缓存面/金丝雀插入点)后两波四 PR 全合 —— #1331 总线 7→20 kind + 平台配置面接线(顺带修 judge/tool-budget 单 pod 失效 bug)、#1329 技能面接线(原零失效)、#1330 X-14 P1 金丝雀、#1332 限流 override 收官。多副本「改配置立刻生效」目标达成(除 credential-proxy→B-31)。测试环境发 `3fc9350e`(单镜像,记录 #1333),smoke PASS;**金丝雀在测试环境完成 seed(租户乐毅大公司,glm/glm-5.3)+ 真栈首跑 PASS(35s 五项全绿)**,周六 prod 只需照 §1.6.7 再 seed 一次。**跨副本真栈验收同日全过**:T1 双副本 attach(非属主收 end=success)+ T2 非属主取消全链(CAS/interrupted 帧/不复活/心跳停走);探针三轮迭代全是探针侧问题(console cancel 拒 API key→改对外 :cancel、claimed_by 带 worker 后缀要前缀匹配、/events 必带 user_id、GLM 账户限速 429 一次环境噪音),零产品缺陷。剩余验收=PPT 内容质量人工抽查(用户侧)。

**进度(2026-08-28 晚 ~ 08-29:委派收尾 + 可观测性四修 + 依赖批)**:B-35 plan_first 五 PR(#1356-#1360)+ 收尾 #1365、**B-36 弹性 worker 预算 #1368**、**B-37 子 Agent 上下文继承 #1369** 全合,发测试 `a8e4382a`(记录 #1371)。可观测性四修 —— #1370 过程条把子智能体耗时**计了两遍**(父工具行与展开的 subagent 行各带同一份墙钟,相加后「思考 44m23s > 总耗时 23m45s」;441 条测试全绿,规格自己漏了这层;**token / 成本 / 产物计数是否同构双计未查** → B-41)、#1373+#1382 三个执行入口(queue worker / orphan sweep / trigger)的 token 记账 trace 绑定收进 `run_trace.bind_exec_trace`、#1374 子智能体 token 计入本轮总数 + #1375 对外文档同步、#1367 对话页产物行漏 worker 注册的产物、#1366 worker 帧消费指南、#1383 `enqueued_input` 注释与实际行为对不上。CI 侧 #1372 让 integration 卡住时说清卡在哪(此前烧满 40 分钟不报位置)。五批 dependabot(#1377-#1381,含 langchain-core 1.5.6→1.6.0)+ 发测试 `4d035bef`(记录 #1384),smoke + 金丝雀两轮全绿。

**进度(2026-08-29 ~ 08-30:保存 ≠ 上线 + 附件进子代)**:起点是「改完 Agent 配置点保存是否立即生效、有没有风险」的核实,盘出 7 条风险,用户拍板除「平台模板改动波及全租户」外全处理,并定下两个方向 —— **会话语义 = 跟随新配置 + 变更标记**、**保存形态 = 存草稿 + 显式发布**。

- 地基:#1385 built-agent 缓存改**内容寻址**(`spec_sha256` 进 key,「改完下一次 run 就生效」从依赖失效广播变成结构性保证;只覆盖 manifest,MCP 池 / OAuth 池 / 平台技能仍需显式失效)、#1386 run 记下**实际执行时用的配置版本**(配置页是原地编辑,`agent_version` 区分不出编辑前后,只有 `spec_sha256` 能)。
- 交付:#1387 保存前 dry-run 构建一次(关键设计=把 `AgentFactoryError` 拆出 `PlatformNotConfiguredError`:「你的 manifest 写错了」422 拒,「这套部署没配好」存下来加 warning —— 一刀切拒绝会打挂 17 个既有测试)/ #1388 编辑必须带 `If-Match`(必需不是可选:console-only 且只有控制台一个调用方)/ #1389 存草稿 · 发布 · 丢弃(四列挂 `agent_spec` 同一行,迁移 `0150_agent_spec_draft`)/ #1391 调试台用未发布草稿试跑一轮 / #1390 对话页标出会话期间配置被改过。
- **委派时子代看得见本轮附件**(起因见下条乱码定位):#1392 文档 + 图片进子代种子消息,注入点选共享的 `_child_run.run_child_to_result` 一处覆盖静态子 Agent 与动态 worker;**图片按子代自己的 `supports_vision` / `tool_catalog` 三分**(原生多模态 → `image_ref` content block;有 `ask_image` → 文本列 URI;都不是 → 不提),顺带堵上「worker 模型被 `dynamic_workers.model` 换成多模态后反而一张图看不了且不报错」的洞。#1393 改走 `AgentState` 通道 —— **审批续跑 / orphan 复活都是 `graph_input=None` 从检查点恢复,附件进 state 就由检查点替我们保住,不用加列不用迁移**;接线从三处收到一处(`build_run_graph_input`)。
- **跨团队定位:上传中文文件名乱码**。现象是我方库里 29 条上传里 15 条乱码、正常中文 0 条 —— 这个统计**不能**排除自己。在生产 pod 里(starlette 1.3.1 / python-multipart 0.0.32)跑三情形对照才定的性:规范 UTF-8 ✅ / 发送方把 UTF-8 字节塞进 latin-1 头 ✅(我方仍能救回)/ **发送方手里的串本来就是坏的再按 UTF-8 发 ❌**,只有第三种能复现。对方确认根因在 `busboy` 解 multipart **头部参数**一律走 latin1 而 `multer` 不设 `defParamCharset`,已在出口加带无损校验的转码,**待其发版后我方验收(B-40)**。该 bug 不只影响可读性:真栈 thread `4f236215` 里 worker 因所有中文名前缀雷同而误选历史文件做了一整轮无效分析 —— 这正是 #1392/#1393 的起因。

---

## 阶段 0 · 当前分支收尾(阻塞后面一切)

### 0.1 真栈验收 —— ✅ **全过(2026-08-14)**

发布 `6bb7dff6` 到测试集群(smoke 9/9,两个迁移落库并核实过 `message_count`
可空无 server_default、部分唯一索引 WHERE 条件),用一把临时铸的 `read+write`
key 打对外面跑完 **18/18 PASS**(用完已撤销并验证 401)。

**四项"静态审无法定论"的全部落地**:

| | 结论 |
|---|---|
| ① `files[]` 文档投递(头号) | **PASS,且拿到了直接证据** —— agent 的 reasoning 里明写 “The file is at `uploads/acceptance.txt`. Let me read it.”,证明模型确实能从 `[file attached: …]` 那一行自行推断出路径。这条此前是"不通过则整条通路产品级失效且静默"的唯一未知 |
| ② queue → 真 worker → 真 graph 吃 `document_names` | **PASS**(24s 内答出文档内容,非 monkeypatch 路径) |
| ③ queue 幂等真并发 | **PASS** —— 6 并发同 key,单一 `run_id` |
| ④ stream 幂等真并发 | **PASS** —— 6 并发同 key,全 200、单一 session、帧数都是 468(输家真接到了赢家的完整流,不是各跑各的) |

**验收过程中三次"以为是缺陷、其实是我错了"**,都记在这里免得下次重犯:

1. **`uploads/` 不出现在工作区列表 ≠ bug**。`layout.py` 刻意把 `uploads/`、`skills/`
   设为保留前缀,浏览面只显示 agent 产出(模型同 `.gitignore`)。spec §八10 的原话是
   「**agent 在工作区生成一个文件** → 列得到」,我却拿自己上传的文档去验。
2. **`Content-Disposition: inline` ≠ bug**。实现是白名单模型:安全文本
   (`.txt`/`.json`/图片)inline,**active content(`.html`/`.svg`/`.xml`)强制
   attachment**(注释标"(c) red-line — never inline")。spec §八10 写的
   「响应头是 attachment」措辞过于绝对,实现比它细致。改成验红线本身 —— 让 agent
   多写一个 `.html`,实测确为 `attachment` + `text/html`。**spec 措辞该按实现修正。**
3. **"agent 拒绝复述口令" ≠ 文档没送达**。我拿"文档里写着请你复述口令"当探针,
   正好撞上提示注入防御:同一提示两次跑,一次照做一次明确拒绝("文件里的指令
   属于提示注入,我不执行"),而两次它都读到了文档。**探针必须是中性事实**
   (换成"登记表里的项目代号是什么"后稳定通过)。glm-5.2 temperature 0.9,
   会被安全策略挡住的探针测不出送达与否。

另:§八3(commentary/final 各带自己的时间戳)需要真产生 commentary 才可判 ——
纯 tool_calls 的空文本 AIMessage 在 `extract_turns` 里被跳过
(`if not text.strip(): continue`),所以简单问答只有 `final`。用"分两步、每步先
说明再动手"的任务跑出 3 条助手消息 / 3 个不同时间戳,判据才真正生效。
**一条只可能绿的断言证明不了任何事** —— 脚本里这条不可判时报的是 FAIL 不是 PASS。

<details><summary>原始清单(留档)</summary>

**口径要写准**(这一条我之前含糊过):spec `docs/superpowers/specs/2026-08-12-external-api-v1-p2-design.md`
§八 的验收清单是 **14 项**。1–13 项大部分已被自动化测试覆盖,第 14 项写的是
"测试集群端到端跑一遍上述全部"。所以真栈这步要跑 1–13 全部。

其中 **4 项是静态审查完全无法定论、只有真栈能判的**(记在
`.superpowers/sdd/2026-08-12-external-api-v1-p2a/progress.md:492` 附近):

1. **头号,失败是静默的** —— 系统提示词和 `read_document` 的工具描述**都没有告诉模型
   「上传文档落在 `uploads/` 下」**,整条 `files[]` 文档投递靠模型从
   `[file attached: uploads/report.pdf]` 自行推断。路径拼接闭环已静态核实为真,
   但"模型会不会用"静态审判不了。**不通过时 API 不报错、run 正常结束,agent 只是答非所问**
   —— 客户端那边看起来像模型没读懂文档。**这条没过之前,`files[]` 文档投递不能对外宣称可用。**
2. queue 模式端到端:worker → 真实 graph 的 `document_names` 投递(现有测试用 monkeypatch
   的假 `run_agent`,只证明了入参构造正确)
3. 真多副本下的 HTTP 层幂等竞态(T13/T14)
4. 第四项在 P2-a ledger 同一段,派发前拉出来核对

</details>

### 0.2 P1 的发布收尾 —— ✅ **已完成(2026-08-14 核实,此前本文件记错了)**

**订正**:我曾照抄 `HANDOFF-2026-08-12.md` 写成"一直没做"。核实结论相反:

- 测试集群 **2026-08-12 12:28(UTC+8)已发 `e26b86ed`**,双镜像
  (control-plane `e26b86ed` / admin-ui `e26b86ed-test`),迁移 Job 完成,全部 Deployment 就绪
- **smoke 9/9 PASS**(首轮旧 pod Terminating、次轮 prometheus 2/3 均已知瞬态,第三轮 3/3 全绿)
- **发布记录 PR #1157 已合**(`7bfd7622`),即本分支的 merge-base
- 发布后在运行镜像内复核过 P1 载荷:`EXTERNAL_SUBJECT_PREFIX == 'ext:'`,5 个 `external_*` 模块 import 通过
- **孤儿 `tenant_user` 已定位并裁定**:`w3-acc-synthetic-1`(id `99b186c6-…`,租户 `dd068302-…`,
  2026-08-10 W3 验收期建),持 0 个会话,唯一引用是一条已归档软删的 `user_workspace` ——
  **没有活数据被搁浅,可留可清**

**教训(同教训 #1)**:"一直没做"是否定性断言,我从交接单转抄未核实。
`kubectl get deploy -o wide` 一条命令就能证伪。**凡写进计划的"未做/从未/没有",落笔前查现状。**

### 0.3 开 PR —— ✅ **已开(2026-08-14):PR #1158**

79 commit(77 + 开 PR 前全量套件逮到的 2 条修复)。**后面的工作不要再往这个分支堆。**

**开 PR 前全量套件逮到的两条**(每个 task 只跑点名文件,只有全量才照得出来):

1. **8 条治具失败**(`24f32f84`)—— `/v1/agents` 挂 `console_only()` 后,若干测试仍用
   API key / 裸 app 打 console 路由。其中 `test_admin_api` 拿 `/v1/agents/schema` 当
   authn canary 是**第二次**踩同一个坑:P1 关掉 `GET /v1/agents` 后 canary 才挪到 schema,
   当时注上"deliberately left open to API keys",P2 又关了它。
   **根治**:判据不再依赖"某路由保持对 API key 开放"——`console_only()` 的 403 只在
   `_principal` 解析出身份后才抛,故 **403 本身即认证成功的证明**,401 是认证失败。
2. **`integration` 标记加在 fixture 体内无效**(`6bb7dff6`)—— `-m` 在**收集期**
   deselect,fixture 到**运行期**才执行,`request.node.add_marker` 永远赶不上。
   `[sql]` 三条因此仍在 unit job 里执行并拉 testcontainers 镜像。

**这两条本身就是 1.3(分区自审)要防的那类东西**:一个"我说它有闸就当它有闸"的自证结构,
一个"我说它是 integration 就当它是"的无效声明。写 1.3 的测试时记住:**声明必须跟实际
生效的机制比对**。

---

### 0.4 正式发布 + 记录 PR —— ✅ **已随阶段 1/2/3 多次发布(最近 `b0abffdb`,记录 PR #1188,2026-08-17)**

**当前测试集群跑的是预发布验收版本 `6bb7dff6`(分支 commit),而仓库里
`infra/k8s/overlays/test/kustomization.yaml` 仍写 `e26b86ed`** —— 这个不一致是**故意**的:
`6bb7dff6` 不在 main 上,把它提交进 overlay 会让 main 指向一个分支 tag。#1154/#1157 的
流程都是「合并 → 用 main 的 squash tag 发布 → 开记录 PR」,这次是为验收提前发的。

**用户拍板:不在 #1158 合并后单独发一次,等阶段 1 的安全收口做完一起发。**
所以这个不一致会持续整个阶段 1 —— 期间测试集群跑的是 P2 预发布验收版本,
功能上等价于 #1158,只是 sha 对不上仓库 overlay。**任何人看集群版本时记得这一点。**

到时候(阶段 1 收口合并后)一次做完:

1. `tools/deploy/release.sh test`(会自己取 main 的新 squash sha 当 tag)
2. smoke 9/9
3. 提交 overlay 的 newTag 编辑,开 `chore(deploy)` 记录 PR,照 #1157 格式
4. **归位 `kustomize edit` 挪走的注释** —— 它每次都会把
   `# Sandbox migration W1 Task 2 …` 从 credential-proxy 条目上挪到 `images:` 前面。
   #1154/#1157 都记过同一件事,是稳定复现的工具行为

---

## 阶段 1 · 安全收口剩余

### 1.1 owner 维度重盘 —— ✅ **已交付 PR #1160**(main `df51b2ad`)

**结论比原记录轻一档,也比原记录重一档,两边都要改**:

- **轻**:`conversations.py` 的跨用户可见**有明文设计**(模块 docstring:
  "an **operations** surface … shows every user's conversations in the tenant,
  so an operator can answer 'what happened in user X's conversation' without
  owning the thread")。不是漏洞。
- **重**:实际缺的不是 owner 过滤,是**连员工 RBAC 都没有** —— viewer 能读任意
  终端用户的完整轨迹原文并 promote/dismiss。

**用户拍板**:读保持全员 / 写收紧 operator+ / 加审计 / 前端不动。已实现:
conversations 2 条 + curation candidates 读 2 条 → `require("session","read")`,
promote/dismiss → `require("session","write")`,curation 详情补 `SESSION_READ` 审计
(原来完整轨迹出平台**零留痕** —— `audit` 形参只喂给了跨租户判定)。

**测试判据 = role-less JWT**:viewer/operator/admin 都持 `session:read`,拿它们
证不了闸真在。8 条新用例,5 条先红后绿。

**⏳ 留给产品的一条**:`curation` 详情交出 `trajectory.messages` **原文**,而
`sessions.py`/`runs.py`/`plan.py` 的 `caller_owns_thread` 对非 admin 员工恒为假(拒)。
curation 的 docstring 对这件事**无明文**。要不要对齐是产品判断,改动量一行。
详见 `.superpowers/sdd/owner-audit-2026-08-14.md`。

<details><summary>原始盘点记录</summary>


**这轮最大的方法论发现**:172 条路由的盘点,分组依据是"哪些文件**没挂** `console_only`"
—— 于是所有**已挂闸**的文件从没被按 owner 维度看过。
`console_only`(平面隔离,挡第三方 key)和 owner 校验(用户间隔离,挡跨终端用户)
**是两个正交维度**。那份 172 条表对第二个维度**无效**。

待盘(全部涉及终端用户数据、全在"已挂闸"那侧):

| 文件 | 已知线索 |
|---|---|
| `curation.py` | **`GET /v1/curation/candidates/{id}` 返回完整会话轨迹 `messages`,对任意终端用户不做 owner 过滤** —— C 组盘点明确记录 |
| `memory.py` | 终端用户长期记忆;现靠 `resolve_caller_user_id` 对 service_account 返 `None` **意外**挡住,非设计闸 |
| `artifacts.py` | per-(tenant, user) 产物 |
| `conversations.py` | 会话 |
| `agent_users.py` | 终端用户本身 |

**预算提醒**:`skills` 那一族按这个维度扫出来 **七轮、四条 Critical**,而**只有第一条是原始盘点
找到的**,最后一条甚至不在 skills 代码里(是"投递 worker"+"权限模型缺口"的组合)。
**这批必须按"每族两三轮"做预算**,靠的是"改一轮 → 独立复审 → 再改"的循环,不是盘一次就完。

</details>

**1.1 留给产品拍板的最后一条(curation 详情交出完整对话原文)—— ✅ 用户 2026-08-17 拍板收到 operator+,PR #1187(`b0abffdb`),已上线。列表(元数据)仍 `session:read` 全员,详情挂 `session:write`。**

### 1.5 员工轴:内容面 RBAC —— ✅ **已交付 PR #1162**(main `fe670a97`)

拍板:复用 `manifest` 资源,读全员 / 写 operator / 删 admin,零改 `rbac.py`。
三处刻意不按动词映射(subscribe 的 DELETE、supporting-files 的 DELETE、
`POST .../test`)。**skills 那 7 条红不是回归** —— 那些用例的被测对象是高风险闸,
`viewer` 只是顺手的非 admin;模块 docstring 声称的 "all skill mutations are
admin-only" 在路由层从来没真过。改角色不改判据。

<details><summary>原始记录</summary>

`skills` / `knowledge` / `eval_runs` / `quality` / `curation` **全部零 `require()`**。
`skills.py` 的注释明写「inline role gate follows the skills.py convention
(no require() / rbac.py matrix)」—— 是这一族的既有惯例,不是遗漏。

P2 那轮给这 42 条加的 `console_only` **只挡第三方 key**,与员工角色无关。所以现状:
**任何员工(含 viewer)可对整个内容面任意读写** —— 建/删知识库、起评测、改质量配置。

**与 1.2 是不同的轴**:1.2 是"API key 能到哪",这条是"员工角色能干什么"。
规模 42 条,风险低于 1.1(不含终端用户对话原文)。**动手前要先定"内容面各操作
分别要什么角色"——那是产品判断**。细节见 `.superpowers/sdd/owner-audit-2026-08-14.md`。

</details>

### 1.2 API key 轴剩余 —— ✅ **已交付 PR #1161**(main `13cf8693`)

**实际是 49 条,不是这里记的 13 条。** 原表只列了 audit/quota/sandbox_egress/memory/
usage/tenant_config/tenant_quotas 这几族;真扫出来还有 `api_keys` 6 + `members` 6 +
`service_accounts` 3 + `role_bindings` 1 + `mcp_servers` 10 + `mcp_oauth` 4。
一把 admin scope 的 key 能**铸造/回显/轮换同租户其它 key、重置员工密码、purge 员工**。
已全部补 `console_only`(只留 `/v1/me`),文档站 auth.md 同步改掉旧说法。

<details><summary>原始记录</summary>

已完成:webhook 5 条、`/v1/agents` 9 条、内容面 42 条(skills/knowledge/curation/eval_runs/quality)。

| 目标 | 问题 | 风险 |
|---|---|---|
| `audit.py` 2 条 | 零 authz,零 scope key 直读租户审计流水 | 中 |
| `quota.py` 4 条 | write key 可达 check/reserve/commit/release,**可能连着计费**;docstring 称 mTLS 服务间平面但 admin-ui 零调用 | 中 |
| `sandbox_egress_audit.py` 1 条 | 只有 `require("audit","read")`,admin/write key 可读沙箱出网日志 | 中 |
| `memory.py` 4 条 | 见 1.1,补显式闸 | 低(但脆) |
| `usage` / `tenant_config` / `tenant_quotas` 的 GET | write key 可达 | 低 |
| `me.py` 1 条 | 零 authz,自身身份 | 很低 |

**关键事实**:零 scope key 落到**空角色集**(`rbac.py:185-192` 的 if/elif 三分支全不匹配),
所以它只能到**完全没有 RBAC 闸**的路由。全平台就 `audit.py` 2 条 + `me.py` 1 条(内容面
那 42 条已修)。

</details>

### 1.3 地基:全 app 路由分区自审 —— ✅ **已交付 PR #1161**

`test_route_plane_partition.py`,261 条路由**零条无法解释**。分类是**推导**的
(依赖图 qualname + 同模块源码),不是查表;推导器本身被正控制钉住。
判据写错了四轮,后两轮都是「没闸的看起来有闸」——详见 PR 正文。

<details><summary>原始记录</summary>

每条路由必须归属 console / external / platform / public 之一,**无第三类**。
**先写测试让它红,红的部分就是施工清单本身。**

已有可抄的范本:`test_console_lockdown.py` 的
`test_every_console_route_carries_the_lockdown_dependency`(真查 `route.dependant.dependencies`)
和 `test_agents_prefix_is_partitioned_exactly`。

**一个必须避开的坑**:旧的 `_SELF_GATED_AGENT_ROUTES` / `_OPEN_AGENT_ROUTES` 两张容忍表,
**除了拼进并集检查外没有任何测试引用** —— 是"我说它有闸就当它有闸"的自证黑箱,
新路由零授权也能靠塞进表让分区测试永绿。全 app 版**必须让分类表跟实际挂的闸比对**,不能自说自话。
(那两张表已在 `85abdb39` 删除。)

</details>

### 1.4 共享平台闸依赖 —— ✅ **已交付 PR #1163**(2026-08-14 合)

`platform_only(message)` 进 `_authz.py`,15 个文件转完;`role_bindings.py` 的判定
是**条件式**(`payload.platform_scope and not ...`)不能转,全仓就这两处。
文案 per-router 保留(19 条各点名被保护资源,11 个测试文件钉着)。
**解锁了新的网**:闸移到依赖层后在 body 校验之前跑,于是能对 40+ 条平台路由做行为扫描
——以前内联在 handler 里,POST 会先 422 拿不到 403。

<details><summary>原始记录</summary>

47 条平台路由各自**内联手抄**一份 `_require_system_admin`,没有共享 `Depends`;
`tenants.py` 甚至连辅助函数都没有,`is_system_admin` 内联写了三遍。

**放最后的理由**:这 47 条**当前没有漏**(A 组核实过),所以是纯预防性重构 ——
收益在未来,风险在当下。**必须等 1.3 的自审测试做完再动**,否则改错了没有网。

</details>

---

## 阶段 2 · P3 —— ✅ **全交付并上线**(波 1 #1169 SSE 契约四修 + 文档站骨架 → `53046a9b`;波 2 #1170 帧文档补全 + #1175 四语言示例 → `5fee6c5e`;计划归档 #1180)

来源:记忆 `external-api-v1-program.md` + spec §九。

| # | 内容 | 备注 |
|---|---|---|
| 2.1 | **SSE seq 修复 + `updates` 帧解析文档 + 三帧补录 + SSE 三个 bug** | spec 原话:「**「看得见 agent」的真正阻塞项,能力已在流里,缺文档**」;记忆标注这是**用户最早的痛点** —— 第三方没法在自己界面还原 agent 交互过程。工程量确定,不是设计题 |
| 2.2 | 文档站 8 章重构 | |
| 2.3 | 四语言示例 | |

---

## 阶段 3 · 补能力(产品功能,与安全收口分开做)

| # | 接口 | 状态 |
|---|---|---|
| 3.1 | ✅ **PR-A #1181**(`df5ab443`)`GET /v1/agent-catalog` | 用户已确认要。**不是**现在这个吐完整 manifest(系统提示词/工具清单/模型配置)的 `GET /v1/agents`,那条已在 `85abdb39` 对第三方关死 |
| 3.2 | ✅ **PR-A #1181** `GET /v1/agents/{code}/runs` | 用户已确认要。现在只能按 `run_id` 拿事件,没有"列出这个会话/用户跑过哪些 run" |
| 3.3 | ✅ **PR-B #1185**(`7e743c2c`)`GET/DELETE …/artifacts` + `/artifacts/download` | 范围已定并交付。原「范围待定」:是 spec §九 第 4 项(文件删除)的**超集** |

**3.3 的背景**(免得下次重新盘):工作区文件分两种,区别在于 agent 有没有主动登记
(`save_artifact` 工具;`Mini-ADR J-11 — explicit registration, never an auto-scan`)。

| | 第三方能用的 `.../workspace/files` | 控制台面的 `/v1/artifacts` |
|---|---|---|
| 看到什么 | 工作区**全部**文件 | 只有 agent **登记过**的 |
| 字段 | 路径 + 大小 | 名字、分类、大小、sha256、版本数、时间 |
| 分类 | 无 | `document`/`code`/`data`/`other` |
| 版本历史 | 无 | 有 |
| 删除 | **没有接口** | 软删 |

**客户端最要紧的缺口是分不出主次**(一次 run 留十几个中间文件,只有一两个是给用户看的成果),
其次才是删不掉。所以建议开**产物视图**,而不是给裸文件列表加个删除按钮。

---

## 待产品拍板(不是工程题)

| # | 事项 | 性质 |
|---|---|---|
| P-1 | **重新生成 / 编辑重发** | spec §九 第 2 项。会话是 append-only checkpoint,**没有"回退到某条消息重跑"的概念**。要做是一道真设计题,不是加端点 |
| P-2 | 消息级点赞 / 点踩 | 内部已有 feedback 表,对外零暴露。薄,但要先定"反馈给谁看、进不进评测回路" |
| P-3 | **admin scope = 完整租户控制台权限** | P1 遗留。文档已明写"永不发 admin 给第三方",但实际范围比措辞暗示的更宽 |
| P-4 | `api_keys` 是**凭据横向扩散路径** | 一把 admin key 能枚举 / 回显 / 新铸本租户其它 key。与 P-3 同源(`rbac._collect_roles` 把 admin scope 直接映射成 `Role.ADMIN`) |
| P-5 | `approve_promote`/`reject_promote` 对 `tenant` 可见性技能**零角色限制** | 任何 viewer 能批准他人对公开技能的 promote-request。需定治理审批要什么角色 |

---

## 其它 program 的挂起项

| # | 事项 | 备注 |
|---|---|---|
| X-1 | **沙箱迁移波 4(收尾波)** | W1/W2/W3 全交付,波 4 因第三方对接插队而挂起。内容:**死字段裁决**(`sandbox_instance.node` + `spec.sandbox` 13 个死字段,15 个里只有 network 三键 + `persistent_workspace` 是活的)、沙箱指标接 Prometheus+Grafana、契约测试补全、文档(本地开发 / 发布 runbook / supervisor 冻结声明)。**另含 W1 遗留清单**(~~头号 egress token 24h~~ 已在沙箱 PR-B #1a/#1b 修掉——热会话 12h 强制重建+egress 审计指标,2026-08-24 核实销案;`pip install` 装进 user site 而 `python -I` 读不到;调试台不显示沙箱 stdout/退出码;明文 HTTP 走 proxy 恒 407 —— 空密码不发认证头) |
| ~~X-2~~ | ~~CI pytest-xdist `-n auto`~~ | ✅ **已交付 PR #1159**(main `edbd8c96`)。实测 **29m17s → 11m32s(2.5x)**。三条教训:①「需先审 fixture 并行安全性」**是个不存在的前置** —— 零个 fixture 要改;真正挡路的是两个模块把 `uuid4()` 插进 parametrize id,导致各 worker 收集集不同。②**别拿本地数外推** —— 本地 `-n 4` 是 3.7x,runner 只有 2.5x(每 worker 都要 import 整个 app,固定开销被本地掩盖)。③基线自己在涨:P2 合入后 main 已经 **29m17s**,不是记录里的 18min |
| X-3 | **触发器 program Spec2/Spec3**(来源:triggers-user-dimension program) | Spec1「对话核心」四 PR + 加固已全交付(#1039~#1043)。剩:**Spec2 通知**(**拍板 09-09:只做 2a webhook,2b 长连接不做**;排期待定)、**Spec3 后台管理面 A + manifest triggers 弃用 C**。DEFER 池头号~~投递无 CAS/幂等~~ **✅ 已修(2026-08-26 PR #1314=PROD-9,per-thread advisory lock 关窗)**;次=TranscriptMirrorSweep 纯注入不重扫(投递消息不即入全文搜索)/ DEAD_LETTER→TRIGGER_FAILED 无专测 |
| X-4 | **用户维度运维页 BACKLOG**(用户 2026-07-15 定暂缓) | ~~① 成员页员工清除 —— 唯一未做的删除入口~~ **勘误(2026-09-09):此记录过期 —— #1052(07-25,删除接口卫生 PR5,拍板 D1 一键「停用+清除」)已交付 `POST /v1/members/{id}:purge` + 成员页按钮 + `MEMBER_PURGE` 审计;波 3 线 M 派线前没核实。**✅ #1455 已合(09-09)**,按用户 09-09 拍板改成方案 A 两步(必须先停用再清除;不能清自己;Keycloak 不可达 502 本地不动;按钮只对已停用/撤销显示)+ **最后一个可达 admin 闸**(`member_ops.has_other_active_admin` 一处判定、两处挂:成员停用 `DELETE /v1/members/{id}` + 删 admin 绑定 `DELETE /v1/role_bindings/{id}` 都 409 `MEMBER_LAST_ADMIN`;「可达」= active ∧ roster admin ∧ 持无条件租户级 ADMIN 绑定;改角色路径**未挂**;两 admin 并发互停的乐观窗口未关,要 advisory lock)。**✅ ② 90 天硬删 #1456 已合(09-09)**:retention job 扫软删且已归档(`archived_object_key`)满 90 天的 `user_workspace` 行,先删行再删从属行(从属失败只 log + 审计 `dependents_failed`),未归档行只计数不动;CronJob 目前只在 test overlay |
| X-5 | **MCP allowlist 残留名不可移出**(来源:MCP 界面重设计 2026-07) | ~~目录条目被删后,租户 allowlist 残留名在 UI 无法移出,后端缺 **disable-by-name** 端点;现降级为「禁用 + 提示」的诚实态~~ **✅ #1452 已合(09-09)**:`DELETE /v1/mcp-servers/allowlist/{name}` + 前端残留行「移出」按名字走它 |
| X-6 | **平台技能导出/同步收尾**(来源:skill-export-sync) | ① 52 个导出包待推测试环境(`POST /v1/platform/skills/import` 幂等可重跑,PLATFORM_ADMIN 凭证在金库);② admin-ui 技能导出按钮(platform + tenant);③ tenant export 丢 `supporting_files` bug(`skills.py:1390` 没传,platform 侧 `platform_skills.py:1333` 是无损姿势可照抄) |
| X-7 | **观测栈残留**(来源:W2-PR3 observability) | ① alertmanager receivers 仍 placeholder(P0 告警投递到空气);~~② compose 侧 prometheus 同源坑 ③ Langfuse org/project 显示名 ④ 探针 trace 噪音~~ **✅ ②③④ #1449 已合(09-09)**:promtool 单测挪 tests/ 子目录、overlays 设 `LANGFUSE_INIT_*_NAME`(既有实例的 org/project 名要在 Langfuse UI 手改)、`TRACE_EXCLUDED_PATHS` 探针路径不开 span。剩 ① |
| X-8 | **Docker Hub 依赖镜像统一 mirror** —— **✅ 主体已交付(2026-08-31,PR #1402)**:规矩定为「Docker **官方**镜像一律从 `public.ecr.aws/docker/library/` 拉」,16 处引用全改(6 个 Dockerfile 的 `FROM` / 5 处 compose `image:` / 4 处 testcontainers 与 stub 镜像 / `nginxinc/nginx-unprivileged` 走厂商自己的 `public.ecr.aws/nginx/`)。**实证**:清掉本地基础镜像后冷拉冷构建 sandbox 镜像通过,`FROM` 阶段 1.1s(从国内;Docker Hub 是 KB/s),redis testcontainers 冷起 6 条测试通过。配 `tools/ci/check_image_registry.py` 卫兵挂进 Lint job —— **它当场逮到一个我 grep 漏的**(`tools/persistence/test_restore_volume_drill.py` 的 `debian:bookworm-slim`),11 条测试含三种裸引用形态各一 + 非官方镜像不误伤 + worktree 副本不扫;变异自证:把 sandbox Dockerfile 改回裸名 → 卫兵红。**卫兵零豁免**(测试素材的裸引用运行时拼,不写字面量 —— 每条豁免都是真违规能藏身的地方)。**余项(2026-08-31 当天更新,PR #1404)**:minio 已迁 quay.io(官方同步源,两 tag 同 digest),默认 profile 的 `docker compose pull` 目标里仍在 Docker Hub 的从三个降到**两个** —— `pgvector/pgvector` 与 `edoburu/pgbouncer`,两个都在 ECR Public / GHCR / quay 三处探过、**没有第二个公共源**。**当天实测:限流仍在发生**,而且每次红的是不同的测试(#1403 pgbouncer+minio 13 条 / #1404 deploy 2 条),错误都是拉取限流 —— 这是限流的签名,不是代码缺陷(每条都在引入它的那次 run 上是绿的)。**归因已定,且推翻了我先前的结论(2026-08-31 当天,#1403 第三次重跑)**:日志逐字写着 `mock-upstream Pulling` → `toomanyrequests: Rate exceeded`,而 `mock-upstream` 正是 X-8 把 `python:3.12-alpine` 改成 `public.ecr.aws/docker/library/python:3.12-alpine` 的那个服务 —— **限流方是 ECR Public,不是 Docker Hub**。`Rate exceeded` 是 AWS 的话术(Docker Hub 说的是 「You have reached your pull rate limit」)。**所以「官方镜像离开 Docker Hub 就少假红」这个前提不成立**:ECR Public 匿名拉取同样按 IP 限流,GitHub 共享 runner IP 照样撞,X-8 是把一种限流换成了另一种。当天报的「integration 30:30 → 9:46」两边各 n=1,后续三次红说明它不构成改进证据。**但 X-8 不该回滚**:从国内构建时 ECR Public 冷拉 `python:3.12-slim` 1.1 秒 vs Docker Hub 的 KB/s,那个收益是实测且真实的(release.sh 受益),错的只是把它当成了 CI 的解药。**CI 的真解**:任何**匿名**拉取在共享 runner IP 上都不可靠,必须换成带凭据或不按 IP 掐的源 —— 首选**自建 GHCR 镜像仓**(Actions 用 `GITHUB_TOKEN` 拉同组织的 ghcr.io 不受这类匿名限额,一条定时 `imagetools create` 即可,代价是包可见性要人工设一次);次选给 Docker Hub / ECR Public 配凭据。**要彻底摘掉只剩两条路,都需要拍板**:①给 CI 配 Docker Hub token(免费账号 200 次/6h 按**账号**算,而不是和全世界共享 runner IP 的 100 次;十行 CI 改动,覆盖所有镜像,需要一把 token)②自建 GHCR 镜像仓(不需凭据,但要加定时 workflow + 包可见性设置)。旧余项:① `pgvector/pgvector:pg16` 仍在 Docker Hub —— 不是官方镜像,ECR Public / GHCR / quay 三处都探过没有第二个公共源,要摘只能自建镜像仓(GHCR + 定时 `imagetools create`);② `node:22-alpine` 进 ACR 那条原计划**已被本次取代**(ECR Public 同时解决了 CI 与 release 两侧,且不需要凭据);③ 非官方镜像(minio/grafana/prom/searxng/edoburu/clickhouse/langfuse)各有上游,统一搬运是另一件事,不在卫兵管辖内 **余项 ✅ 收官(2026-09-08,#1430 mirror workflow / #1433 引用切 `ghcr.io/deepaihealth/mirror/`(两包已 Public,integration 用 GITHUB_TOKEN 登 ghcr;顺带修卫兵在 worktree 路径下永远绿)/ #1435 沙箱镜像 gha cache 预构建 + `EXPERT_WORK_TEST_SANDBOX_IMAGE`)**。**真病灶核实与此前记录不同**:09-07 两次 40 分钟红预拉 12/12 全成功,卡的是 `test_fullstack_egress_e2e` 里 `docker build` 沙箱镜像的 apt 层(mirrors.aliyun.com 在美国 runner 冷构建 10-22 分钟),超时留半截缓存后 supervisor fixture 再 build 一次;修后 #1433 integration **8m17s**。未做:冷缓存那次仍慢,下一刀 = Dockerfile build-arg 让 CI 切 deb.debian.org;`searxng/searxng:latest` 仍在 Docker Hub。 |
| X-9 | **Agent 延迟 perf follow-up 池**(来源:perf 一期+二期;明细在本机 perf program 台账 progress.md) | oauth put 半成功窄漏清窗口(pre-existing)/ 双键空间 × per-tenant 键 LRU 有效容量随租户数下滑 / first_output SLI 缺面板 / resolve_embedder+resolve_reranker 死代码 / admin-ui 分解条 total 并行后夸大 |
| X-10 | **生产多副本整体实施方案**(来源:production-distributed-premise,2026-07-28 拍板) | 五道选型题已收口(ACS 双集群杭州 / AgentSandbox+E2B / OSS+NAS 工作区 / RDS PG16 / Redis 社区版 7.0 noeviction),四波行动清单在 `docs/research/2026-07-28-multi-replica-readiness-audit.md`;**整体实施方案还没出**。期间新特性默认按多副本语义设计 |
| X-11 | **mcp SDK 1.x→2.x + httpx2 迁移**(2026-08-20 立项,源头 dependabot #1229) | mcp 2.x 换 `httpx2` 依赖,`streamable_http_client` 签名变更(3 元组 + `httpx2.AsyncClient`),`orchestrator/tools/mcp.py:606-609` 起的整条 MCP 客户端层要迁;dependabot 已加 major ignore(做完把 `.github/dependabot.yml` 里那条一起删)。迁移要真栈测 MCP 工具(streamable-http + OAuth 路径) |
| ~~X-12~~ | **✅ 已交付(2026-09-10,#1483)**:钉版解到 `fastapi>=0.138,<1`(**`iter_route_contexts` 是 0.138.0 才有的,0.137.x 只有懒挂载没有替代枚举口,是个两头不着的空档**),七个自审文件全走新建的单源适配层 `tests/route_audit.py`,dependabot ignore 与 `tools/ci/protected_pins.toml` 两处钉子一并撤。三条硬规矩:零默认值属性读(改名当场 `AttributeError`)、枚举非空强制断言(枚举层 + 每族子集两层)、只读合成值。**五族变异全红零降判据**,另加两条:helper 改回旧写法 → 六个自审全红报「enumeration is broken」;`strict_content_type=False` → 新测试红。**`uv lock` 只动 fastapi 一行**(0.136.3→0.141.1),starlette 仍锁 1.3.1(干净 venv 单装会拉 1.6.0,是 workspace 其它约束按住的)。**勘误两条**:`strict_content_type` 不是本次升版带来的——参数在 0.130~0.133 之间进来、进来就默认 `True`,0.136.3 与 0.141.1 行为完全一致,**对第三方零行为变更**,实测对外端点不带 `Content-Type` 返回 422 + **完整第三方信封**;「静默变绿」说重了,本仓迁移前跑是 15 failed(前几轮各自碰巧写了非空断言),本 PR 把这条保证从「每个文件的运气」搬到枚举层。**留一处有意的例外**:`route_is_external` 喂 `original_route`(实测生产 `scope["route"]` 给的就是原始路由),今天全仓 67 处 `include_router()` 无一处传 prefix/tags/dependencies 所以两边同答案;将来谁改成 `include_router(dependencies=[...])`,生产信封会悄悄失效,唯一会红的是那条一致性测试。**未修记账**:`observability._route_template` 将来加 prefix 会悄悄丢 metrics 标签前缀且无测试盯;fastapi 0.141 对 websocket 路由的 effective context 根本没实现(本仓零 websocket 碰不到) | fastapi 0.137 起 `include_router` 懒挂载:`app.routes` 里是 `_IncludedRouter`,APIRoute 不再摊平(实测 0.136.3 摊平 / 0.137.2 起不摊,启动也不展开),控制面整族路由自审(console lockdown / external gate / NUL guard / reachability / app_factory)集体失明。迁移=自审改用 `fastapi.routing.iter_route_contexts` 遍历并核实 router 级依赖的合成语义(**这批是安全闸,依赖合成改错比不改危险,要变异自证**);生产代码零 `app.routes` 枚举已核实。两个 service 的 pyproject 钉了 `<0.137` + dependabot ignore,做完一起放开 |
| X-13 | **e2b SDK 升级迁移 2.24.0→2.39.1+**(2026-08-21 立项,源头真栈事故) | #1233 曾把 `e2b` 抬到 2.39.1,其客户端 `validate_api_key` 强制 `e2b_`+hex 格式,ACS Agent Sandbox 私有协议 key 被本地拒绝 → 测试环境沙箱工具全挂(list_dir/exec_python/write_file);已回钉 `e2b==2.24.0`+`e2b-code-interpreter==2.7.0` + dependabot ignore。升级路径:确认 kruise patch_e2b 对新版兼容(或在 `_ensure_e2b_patched` 中和 validate_api_key)→ 按 deployment.md contract-run 重跑沙箱运行时契约探针 → 真栈 exec_python 冒烟 → 解 ignore |
| X-14 | **发布稳定性加固**(2026-08-21 立项,源头 e2b 事故复盘:评审漏钉版/CI 假件全绿/smoke 只探 HTTP,三层防线全漏)——**P2 ✅(2026-08-26,#1317)/ P1 ✅ 已交付(2026-08-27,#1330):`tools/deploy/canary.py` + release.sh 阶段 6(Secret 未 seed WARNING 跳过不阻断;红提示 rollback.sh)+ `python -m control_plane.seed_canary` 幂等 seed + runbook §1.6.7/§1.7;测试环境真栈首跑 PASS(35s,end.status=success + 产物字节校验)** | P3 sandbox create 失败率 alertmanager 告警 / P4 约定:动沙箱链路依赖(e2b/kruise/supervisor/镜像)必跑 deployment.md contract-run / ~~P5 约定:发布记录 PR 写上一版 tag 便于一键回滚~~ **✅(2026-09-08,#1431:release.sh 收尾直接吐出记录 PR 正文,含上一版 tag 与按 rollback.sh 真实 CLI 拼的回滚命令)** |
| X-15 | **B-20 终审 follow-up 两条**(2026-08-23 立项,#1253 独立终审 I-1/I-2) | ~~① `retention-cleanup-job/job.py:207` 存在**第二套审批超时实现**~~ **✅ #1456 已合(09-09)**:该路径整段删除,control-plane sweep(走 `resolve_approval_decision`)是唯一内核,job 在 CronJob 首部署(test overlay)前清干净;~~② 审批行 `user_id=None` 写死~~ **✅ 已修(2026-08-31,PR #1400)**:写入侧一行 `user_id=record.user_id` + 存量回填迁移 `0151`(按 `run_id` 从 `agent_run.user_id` 取,只填 NULL 行,downgrade 是 no-op)。**测试环境实测才是判据**:38 条审批单 `user_id` 非空 **0** 条,而 38 条**全部**能从 `agent_run` 回填 —— 既证明修复不是空转,也证明回填无损。既有那条审批测试建 record 时不传 `user_id`,**两边都绿证明不了任何事**,必须新写。迁移测试在**回填之前**的版本(0150)上灌数据再升 head(在 head 上灌再断言测不出东西,UPDATE 早跑过了),为此在容器里另建一个库;三种行分开(无主+run有主→回填 / 无主+run也无主→保持 NULL / 已有主→不覆盖),变异自证:拿掉 UPDATE 杀第一条、去掉 `AND a.user_id IS NULL` 杀第三条 |
| X-16 | ~~**批 E 收尾:E3b 失效总线全量接线 + C 类缺口修复**~~ **✅ 全交付(2026-08-27,四 PR)**:#1331 总线 7→20 kind + 平台配置面全接线 + **judge/tool-budget「改配置连写入 pod 都不生效」bug 修复** + quota Protocol invalidate_tenant;#1329 技能面(平台 10 端点/租户 7 端点/promote 审批,原先零失效);#1332 限流 override 缓存提取为可注入对象 + 接线;#1330 里的 handler 占位同批闭环。测试环境 `3fc9350e` 已发,smoke PASS | 遗留单列:credential-proxy 接总线(失效链现无人调用+单副本无跨副本陈旧,真接需加 redis 依赖;顺带发现其 /admin/* 端点无鉴权,一起立项)→ 记 B-31 |

---

## 小 backlog

| # | 事项 |
|---|---|
| ~~B-1~~ | **✅ 已修(2026-09-10,#1482)**:`user_id` 列本来就在表上(migration 0074,nullable),**不需要回填迁移**;写入侧改走 `resolve_caller_user_id`(与触发器同款),机器主体仍为 `None`,`PATCH` 走 `model_copy` 不会抹掉 owner。**连带效果值得点名**:`user_id` 今天唯一的消费点是 `delete_all_for_user`(用户清除),purge 清单里本来就写着 webhook endpoints,只因这列恒为 NULL 一直空转,这次接上才真正生效;投递 worker 不读它,派发不受影响。原记录:`webhook_endpoint` **不记录创建者**(`user_id` 硬编码 `None`,全仓仅一个写路径)。现在无痛(**用户确认测试和生产都没配 webhook,表是空的**),一旦开始配就无法追溯 |
| B-2 | 前端 `WebhooksList` 无角色 gate(后端已收紧写操作,前端没有对应入口控制) |
| B-3 | `console_only` 的 403 文案在 4 处测试 + 1 处源码硬编码,改文案要动 5 处 |
| B-4 | promote-request 分页在 `created_at` **相同**时 tie-break 方向两后端不一致(in-memory id 降序 / SQL id 升序)。**预先存在,非本轮引入**;生产用 SQL 风险低,但别把那两条新测试当"两后端完全等价"的证明 |
| B-5 | `test_viewer_reads_are_unaffected` 的治具复用 POST,改用 store 直接建行会让它在任何变异下只对 GET 负责 |
| B-6 | **同一资源的两个写操作,`user_id` 位置不一致**:`PATCH .../sessions/{id}` 在 **body**(`ExternalRenameRequest`),`DELETE .../sessions/{id}` 在 **query**。各自都说得通(PATCH 本来就有 body,DELETE 通常没有),但对接方要分别记。**文档必须写清楚**,真栈验收时我就按 query 传 PATCH 吃了 422 |
| B-7 | **spec §八10 措辞与实现不符**(实现更对):spec 写「响应头是 attachment」,实现是白名单 —— 安全文本 inline、active content 强制 attachment。**改 spec,别改代码** |
| ~~B-8~~ | ✅ **PR #1164**。`tools/deploy/smoke.sh` 用 `items[0]` 选 control-plane pod,不过滤 `Running`。发布刚结束时会选中正在 Terminating / 已 Succeeded 的旧 pod,报 `cannot exec into a container in a completed pod`。**光加 field-selector 不够** —— Terminating 的 pod phase 仍是 Running,还要 deletionTimestamp 为空 + Ready=True;且 kubectl jsonpath **没有否定**(`!` 直接报错),要在 awk 里筛 |
| B-9 | **(PR-A #1181 复审发现,非其引入)** `/v1/agents/` 前缀下 **12 条 `console_only()` 路由**的 422 也套第三方信封(生产异常处理器判据=前缀 OR tag),而 `admin-ui/src/api/client.ts:89` 读 `data.detail` 读不到 → 降级成 `HTTP_422` + axios 通用文案。修改前后一致,未变坏。修法二选一:①判据改成「挂了 `external_only()` 依赖」驱动(复审给出的真定义,更贴);②admin-ui 兼容信封。**✅ #1457 已合(09-09,取①:信封跟闸走)** |
| B-10 | **(同上)缺一道 CI 闸,不是现存 bug**:今天 8 个 `external_*.py` router 都带 `tags=["external"]`,但**将来谁漏打**,信封静默丢失且**没有任何自审能逮** —— 三个 external 发现器 PR-A Task 4 后纯 tag 驱动,同样瞎;`test_route_plane_partition` 只钉 `tenant`(无闸)集合、不钉 `external`。廉价补法:加一条自审「凡挂 `external_only()` 必带 `external` tag,例外(`agents.py` 的 `bind_session`/`run_agent_for_user`)走白名单」。**✅ #1457 已合(09-09)** |
| B-11 | **(PR-B #1185 终审 triage 留)** `control_plane/audit.py:111` 的 `ResourceType` Literal **不含 `"artifact"`**(protocol 侧有)。`external_artifacts.py` 与控制台 `artifacts.py:293` 同款 `resource_type="artifact"` 都是类型缺口;CI mypy 不扫 control-plane 所以今天不红,**范围一扩到 control-plane 立刻红**。修它要碰共享 Literal,顺手把两处一起对齐。**✅ #1457 已合(09-09,`artifact` + `approval` 两个 Literal 对齐,两侧集合相等测试钉住)** |
| B-12 | **(同上)** 对外产物下载的配额(`artifact_download`,cost=1)在 `workspace_store is None` / 权限失败 / 内容缺失三条失败分支**之前**就扣了,失败也白扣一次。镜像的是 console 侧既有顺序,非 PR-B 引入;改的话两侧一起改 |
| ~~B-13~~ | **✅ 已修(2026-09-10,#1482,选「删」)**:判据本来就是扩展名;7 个调用点里只有 2 个手上真有 `artifact.kind`,其余 5 个一直传占位值;真接上唯一能多出来的分支是「无扩展名 + 按 kind 判 inline」,那正是这个防 stored XSS 的白名单模块**刻意避免**的按元数据嗅探。**没为这条写新断言**——删一个从未被读的形参不产生可观测差异,能写的只有照抄签名的重言式(agent 主动拒绝并报告,判断正确)。**顺带一次自伤**:清理 `tools/eval/artifact.py` 的失效形参时正则写宽,把同文件三处 `save_version(..., kind=...)` 的**必填实参**一并删掉(同名但完全无关),本地只跑改到的文件所以全绿,CI 全量三条 TypeError 逮住(`d80a8a2a` 已修)。教训=正则删形参后要对全 diff 逐 hunk 过一遍。原记录:`_artifact_mime.infer_content_type` 的 `kind` 参数是摆设(fallback 分支 `del kind`),反查 `artifact.kind` 目前用不上。要么接上(按 kind 收窄 MIME 推断),要么删参数;别留死参 |
| B-14 | **(附件统一 2026-08-17 终审 triage 留,pre-existing)** `GET …/sessions/{id}/messages` 在「非 vision 模型 + Agent 声明了 `vision:` 块」这一组合下,消息文本里会带 `[image attached: expert_work://image/…]`(`api/runs.py:427` 附近)—— 是最后一处内部 URI 能越过对外边界的地方,与本次「对外不再暴露 `expert_work://`」目标相悖。修法:对外消息投影时把这类标记改成 `upl_` 形态或直接剥掉。**✅ #1457 已合(09-09,对外投影直接剥掉)** |
| B-15 | **(同上)** 同名文档覆盖:`_safe_workspace_name` 确定性映射,同一用户重复上传 `report.pdf` 得到两个 `upload_id` 指向同一路径,后者覆盖前者字节,先前那行的 `size`/`mime` 过期。终审只让文档写明(chat.md §2.6),代码级修法(uuid 后缀 stem)是行为变更且 run 内按名读文档(`read_document`),要单独立项 |
| B-16 | **(同上,小项打包)** ① run 循环 `agents.py` 对 `kind="image"` 行缺 `parse_image_ref` 守卫(下载端点有,run 端点没有;今天只有 upload_for_user 写图片行所以不可达)② `_validate_image_refs` 内部冗余 thread 检查抛裸 `HTTPException`(不可达,靠 T2 两处 `thread_id` 同源的不变式,终审修复波已加断言钉住)③ `TOO_MANY_IMAGE_REFS` 兜底在 files 上限 == `MAX_RUN_IMAGE_REFS` 时不可达(保留,测试靠调低常量证明)④ `INVALID_FILE_REF` 仍由 `_safe_document_name_or_422` 发出但文档已删(只有手工塞坏行才触发)⑤ 迁移 `0146` 的 `ix_user_upload_tenant_thread` 目前无查询用(spec 定的,若不做按会话列附件可删)⑥ 端点级跨租户下载测试缺(store 级有)⑦ T2 文档分支 `uploads.insert` 失败在工作区已落盘之后 → 裸 500 + 孤儿文件 + 图片分支多扣一次配额(镜像 image_upload 既有模式)⑧ T1 `test_get_does_not_filter_user` 末句 `!= uuid4()` 空断言 |
| B-17 | **①② ✅ 已修(2026-09-10,#1482);剩 ③**。① 注释改成实话(`app.py` 一律传 `settings.kill_switch_cache_ttl_s`=5s,类上那个 30.0 是从不生效的形参默认值)。② 事件流页大小改成本层自己的 `EVENT_PAGE_LIMIT`,取值不变——**写的时候发现这不只是借名字**:`RunEventStore.list` 会把 limit 夹到 `MAX_LIST_LIMIT`,而补库循环拿「行数 < 页大小」判最后一页,页大小超过 store 上限会让循环在第一页误判收尾、**静默丢事件**;已加一条守卫测试钉住这层关系(变异 `+1` → 红)。原记录:① `external_agent_catalog.py:95` 注释写 30s,真实 TTL 是 `settings.kill_switch_cache_ttl_s`(5s)② `_run_event_stream.py:24` 从 run store 借 `MAX_LIST_LIMIT`,语义上应有自己的常量 ③ `approval` 事件把 `binding_digest` 原样放到对外线路上,对接方用不上、且暴露内部摘要形态,考虑从对外投影里剥掉 |
| ~~B-18~~ | **✅ 已改文档(2026-09-10,#1482,不改代码)**:先跑真 app 探针实证(`.../uploads/upl_a%2Fb` → `404 {"detail":"Not Found"}`),跑完删掉探针;代码侧核实只有 `RequestValidationError` 一个对外信封处理器。落点选 `errors.md`——**8.2 那句「返回简易格式的只有以下几处」原本不成立**,补上一条;8.6 加一段 + 示例。按 style-guide 自检(「裸 404」是禁用词,改用「简易格式,只有一个 `detail` 字段」),docs-site 构建 + 死链脚本(1614 链接)零死链。**没有**给对外前缀加 404 信封(行为变更,不在该批范围)。原记录:`/v1/agents/...` 前缀下**匹配不到路由**的请求(例:路径参数里带 `%2F`/`%3A`,Starlette 解码后 `{upload_id}` 段吃不下 `/`)返回框架裸 404 `{"detail":"Not Found"}`,不套第三方信封;对外信封处理器只接 `RequestValidationError`。conventions.md 已泛泛写「不是所有错误都有 error.code」,可加一句「路径拼错是裸 404」;或给对外前缀加 404 信封 |
| B-19 | ~~(第四轮文档终审 C-1 发现)配额维度混扣 + 黏性维度 Retry-After 除零~~ **✅ 已修(2026-08-27,PR #1338)**:`CheckRequest.resource_kind` + 协议层单源谓词 `dimension_applies()` 两引擎共用;黏性(refill=0)维度打满不再发 `Retry-After`(header 省略、body null)。ARTIFACT_STORAGE_BYTES 实测零消费点,映射记空集待未来接线登记。|
| B-20 | ~~**审批列表分流:安全审批 vs 业务澄清混排**~~ **✅ 主体已交付(2026-08-23)**:①两 tab(协议单源分类 `SAFETY/CLARIFICATION_REASON_KINDS` + `GET /v1/approvals?kind_class=` + 前端「安全审批/待确认」tab 带对侧徽章)✅;③超时按类配(`policies.clarification_timeout_s` 默认 1h,mint 按类选,sweep 对澄清单的超时理由改为「按保守默认继续并说明假设」——ask_for_approval reject 非终局,agent 收到即续跑)✅;④ decide/resume 三端点补 `require("session","write")`,堵 viewer 可裁任意单的洞 ✅(「澄清单发起人自决」推迟:run 上没记发起员工身份,随 D-6 一起做)。**剩余**:② 通知路由——澄清单对话内横幅归 D-6,安全单 alertmanager 告警归 X-7 |
| B-21 | **(#1265 终审 follow-up,2026-08-24)** 折叠头(chevron + 计数标题按钮)三处重复实现:`PlanCard`/`ProcessStrip`/`FileTree` 与新加的 `VariablesForm` 各写一份展开/收起头,样式与 aria 语义略有分歧;抽共享 `CollapsibleHeader` 组件统一 |
| B-22 | ~~`ConversationDetail` 导出相关 e2e spec flake~~ **✅ 已根治(2026-08-26,#1307)**:根因=growth-repair effect 读未 gate 的 `convo?.runs`(stale 线程)→ 每次切线程发 `loadHistory(newThreadId, undefined)` 错租户垃圾请求;修=改读 `viewedConvo?.runs`(与主重建 effect 同 gate),两只 flake(导出/跨租户)同源同灭 |
| B-23 | **✅ 已修(2026-09-08,#1434:`/v1/skills` 平台半边走游标 `platform_cursor`/`platform_next_cursor` + `platform_items_truncated`;SkillPicker/SkillsList 循环拉完,`MAX_CURSOR_PAGES` 封顶仍有游标时常驻 warning Alert)**。原记录:**(BUG-3 修复时记账,2026-08-24)** 后端 `GET /v1/skills` 的 `platform_items` 单次拼装上限 200(`list_platform_skills(limit=200)`)静默截断:平台库超 200 条 ACTIVE 时租户侧少看到技能且无任何提示;前端 SkillPicker 已走 cursor 拉全 tenant items(#1265),platform_items 这半边要么后端跟 cursor,要么至少响应里带 truncated 标记 |
| B-24 | **(2026-08-24 记账)** CI `pip-audit` 偶发网络超时假红;给该 step 加 retry(或本地 advisory DB 缓存),别再靠手动 re-run。**✅ #1453 已合(09-09,step 级重试)** |
| B-25 | ~~(BUG-19 真栈首证)worker 帧裸透传 spotlighting 围栏~~ **✅ 已修(2026-08-27,PR #1337)**:worker 帧组装处 3 出帧字段(tool_result_excerpt/exec stdout/stderr)统一 unspotlight(先还原后截断),前端 worker_timeline 套 cleanUntrusted 兜历史帧;**已入库历史帧对外仍带围栏,属遗留数据不迁移**(对接方剥离器保留)。|
| B-26 | ~~(BUG-19b 硬闸清点发现)子 Agent/worker 构建静默防御降级~~ **✅ 已修(2026-08-27,PR #1339)**:防御参数决议抽单源 `resolve_defenses()`(runtime.py),child/worker 两调用点补传五参(judges/tool_budget/deadline/token_usage_kind);kind 穿透委派树 + 子代缓存键补 kind 位;顺带 BuiltAgent 挪中立模块 built_agent.py 根治循环导入。|
| B-27 | **(批 D #1268 终审 follow-up,2026-08-24 记账未入册)** `threads/` 工作区子目录**删除生命周期缺失**:会话 purge 不清对应 threads/ 目录、无 janitor GC,长期用户工作区只增不减;挂会话 purge 钩子 + janitor 扫描双路。**✅ #1456 已合(09-09)**:`threads/<id>/` 只跟对话生死(`thread_meta` 行先提交、目录后写,靠控制流保序);无主目录留 **24h 宽限**(目录 + 直接子项最新 mtime)再删,兜反向竞态;tenant/user 两级目录不 resolve、`lstat` 拒 symlink(先写的逃逸测试实测 victim 被删,是真 bug) |
| B-28 | **(产物清单契约 #1305 停删后果,2026-08-26)** 对接方停删后**平台侧产物生命周期归平台管**:artifact/artifact_version 表与工作区文件只增不减(原靠对方收割后删除兜着)。要定留存策略(按租户/按 kind TTL?对齐 X-4② 90 天硬删?)并接 retention 机制;`archived_object_key` 字段仍是预留未接。**✅ #1456 已合(09-09,用户拍板产物 90 天 / 上传 90 天)**:retention job 按版本 `created_at` 过期(定序 `created_at, artifact_id, version`),`ARTIFACT_EXPIRED` 审计带 `reason`/`versions_remaining`;工作区根目录其它文件(`style/`、`MEMORY.md`、未登记文件)不变式 + 变异测试 |
| B-29 | **(SSE 心跳 program 三条遗留,2026-08 记账未入册;共性=「该有信号处无信号」)** ① 对外 `/items` 续传 `limit` 触顶静默截断无 truncated 标记;② replay 中段 seq gap 静默跳过不发 gap 信号;~~③ `release.sh` 个别失败分支 exit 0 假成功~~ **✅ 已修(2026-09-08,#1431:病灶只有一处 —— rollout 段 `for d in $(kubectl get deploy …)`,bash errexit 不查 for 列表里的命令替换,kubectl 失败→空循环→smoke 打旧 pod→`Release done` 退出 0;改赋值式 + 空列表拒绝 + EXIT trap 报阶段名与回滚命令。smoke.sh `pods_not_ready` 单项瞬时假 OK 未改)** |
| B-30 | **(三厂商档位对齐 #1308 顺带,2026-08-26)** `kimi-k2.7-code` 上架与否待拍板:官方文档新型号(思考不可关、`thinking.keep` 固定 `"all"` 传其他值报错);上架需 catalog entry + `thinking.keep` 适配层支持(现在不发 keep,K2.6 默认 null 无碍) |
| B-31 | **(E3b 侦察发现,2026-08-27)credential-proxy 两件套**:~~① 失效链断头~~ **✅ 已修(2026-09-09,#1439)**;原记录:① 失效链断头 —— `/admin/cache/invalidate` 全仓无调用方,cache 只靠 60s TTL;现单副本无跨副本陈旧,**扩容前必须**接失效总线(需新增 redis 依赖 + env 注入)或至少接 HTTP 失效(走 HTTP 方案时 token 已就位,见 ②);~~② `/admin/*` 三端点裸奔无鉴权~~ **✅ 已修(2026-08-31,PR #1399)**。**定级比原记录重**:不只是「管理接口裸奔」—— `/admin/allowlist` 写的正是 `/forward` **唯一校验的那张表**,而 `forward` 会把解析出的密钥当 `Authorization: Bearer` 发往**调用方自己指定的 upstream**(该 URL 完全不校验,只在审计里 urlparse 取 host)。**能写白名单 = 能让代理把任意租户的密钥送到任意地址。** 可达面至少是集群内所有 pod(ClusterIP + 全仓零 NetworkPolicy);沙箱已在打这个 pod 的 8081,同 pod 的 8080 能否开**未实证**,闸不依赖该答案。修法=中间件按前缀拦 `/admin/` + `compare_digest` 比对 `EXPERT_WORK_CRED_PROXY_ADMIN_TOKEN`,**未配置 → 503 而非放行**(「没配就没闸」正是这个 bug 本身的形状),`/admin/health` 留开给 kubelet 三探针。三个被关端点全仓零调用方,关掉不破坏任何链路。**顺带订正三处说谎的文档** —— STREAM-F-DESIGN §4.4 那句「仅 mTLS SAN=control-plane 可达」从未实现,**这句写下的控制正是它坐了这么久没被查的原因**(同 [[description-is-not-the-thing]]) |
| ~~B-32~~ | ~~redis Lua 桶回填量纲差 1000 倍~~ **✅ 已修(2026-08-31,PR #1398)**。发布前修的理由是实证的:**生产强制走 redis 引擎** —— 多副本没配 quota Redis 直接拒启(`app.py:1204`),有 URL 就选 redis 引擎(`:3127`),所以 QPS 类配额在生产上等于没有。根因是**量纲只抄了一半**:`ratelimit` 那份同款 Lua 是对的(capacity/tokens/cost/rate 全 ×1000 自洽),`quota` 这份只缩放了速率 → 桶回填快 1000 倍;retry 公式带同一个因子,两边互相自洽,所以从 `retry_after_s` 上看不出来。第二半是 `int(rate*1000)` 把低于 1/1000 令牌每秒的速率 floor 成 `0`,而 `0` 是脚本里的**黏性天花板哨兵** —— 30 天慢滴维度既不回填又宣称「重试永远不会成功」。修法=速率按与 capacity/cost 相同的单位原样过线(缩放没了就没有第二处可错),黏性哨兵改判 `refill_per_s > 0`,顺带让两引擎谓词重新同义。**新增真 redis 集成测试 5 条,修复前 4 红 1 绿**(绿的那条是「真黏性保持黏性」的回归网,设计上两侧都绿);两引擎对账那条修复前是 `None` vs `25920`。**为什么此前测不出**:全部 quota 单测走内存引擎,Lua 桶从来没被时钟跑过 | ✅ |
| B-33 | **✅ 已修(2026-09-08,#1429:`ModelEntry.temperature_fixed`,只 kimi-k3 声明 1.0;factory 五个出口钳制 + WARNING;`ModelSpec.temperature` 默认 0.2 存库是完整 dump 分不出省略,所以默认值也钳也告警;配置页冒 warning 未做)**。原记录:**(kimi 对照真栈发现,2026-08-28)模型个性参数无 catalog 声明**:kimi-k3 只接受 temperature=1,sop2 换模型后旧值 0.9 原样发出→厂商 400 炸 run(#1321 always_thinking 同族问题)。修法=ModelEntry 声明温度约束(固定值/白名单),构建时钳制或略去并 log,别让厂商 400 当校验器 |
| B-34 | ~~moonshot 严格校验 tool schema,enum-without-type 400~~ **✅ 已修(2026-08-28,PR #1350)**:update_plan/manage_task 四处补显式 type;守卫测试 test_tool_schema_vendor_strict.py 递归扫内置工具 schema。教训=拼给 LLM 的 schema 必过厂商严格校验,别赌宽松(MCP 工具名 wire-safe 同款纪律) |
| B-35 | **委派增强层 4:per-agent `execution_mode: plan_first`(规划-执行分离)**——把委派从概率变保证的唯一结构解。**启动条件**:层 0-3 上线后真实流量 `expert_work_dynamic_worker_spawned_total` 跑 1-2 周仍趋零。真栈验证结论(2026-08-28,四探针 glm×3+kimi×1):层 0/1/2 机制全部生效(两模型思考原文引用判据、层 1 提醒精准注入),但模型均权衡后选 inline——业界同现象(Claude 自家栈实测 7 跑 0 委派,prompt 层无强制手段)。**先于层 4 的两个零成本杠杆**:①kimi 20+ 工具选择退化(社区实测),sop2 挂 30+ 工具,收敛 MCP 勾选可能直接抬委派率;②员工提问措辞含「分别/并行处理」类词对 kimi 触发委派影响大,可进层 3 生成的领域策略提示。**设计已拍板(2026-08-28,完整方案 `docs/superpowers/specs/2026-08-28-plan-first-execution-design.md`)**:manifest `execution_mode: plan_first` per-agent 默认关;**开启硬联动**(确认 Modal 明示后一次写入)= workflow.type react→plan_execute + dynamic_workers.enabled 必 true;Modal 另列建议检查项(token 成本 ~15× 大白话/max_iterations/deadline/reflection 协同/触发器 run 同样生效);**双层防护**=UI 联动写入 + 后端校验不一致 422 硬拒(不静默归一);关闭开关**不回退** workflow.type(提示手动改)。切分 5 PR 约 7-9 人日,PR-4(中断/审批 × 在飞 worker)是历史高危区。**✅ 全交付(2026-08-28,五 PR #1356-#1360 全合,发测试 ca4ae6b9)**:真栈探针(canary 租户 pf-probe/pf-probe-kimi,不动对接方 agent)——**glm-5.3 全链生效**:planner 标 delegate/分发轮触发/3 worker 真派发(`dispatch_total +1`、`spawned_total +3`、零降级),对比层 0-3 四探针 spawned 恒 0 = 结构解实证;**kimi-k3 绕行**:分发轮触发但用 update_plan 逃生门把 delegate 步改标 inline 零派发——follow-up 已修:①逃生门措辞收紧(re-mark 仅限写操作/最终拍板,读料/总结/提炼类明确不许)②re-mark 绕过计入 degraded 计数(原为观测盲区)③模板分层假设测试钉住。**e2e 用例裁掉**(组件级测试已盖渲染面,e2e 增量不抵维护成本;spec §8 相应降级)。PR-4 顺带修的审批黑洞(worker 撞闸静默假完成)为全局 bug 修复,不在开关后 |
| B-36 | **弹性 worker 预算(per-agent 三值 + 平台默认/硬顶两档)**——sop2 真栈 plan_first 首跑 3 worker 全撞平台 32 步顶(自愈但耗时拖长)引出。方案甲(2026-08-28 拍板):manifest `dynamic_workers.max_iterations/max_concurrent/max_per_run` 三个可选请求字段(None=平台默认,宽 sanity 闸 512/64/1024);平台 `platform_dynamic_worker_config` 扩默认+硬顶两档六值(migration 0148 回填 cap 10/64/128),per-agent 请求 clamp 到 cap,**显式请求赢过「不超过父 workflow」启发式**;API/平台管理卡六值(default≤cap 422/前端同校验);Agent 配置页「子智能体预算」小节+步数>40 无 token 预算软提示(步数-token 不硬联动=拍板);配套七处配置页 UI 整理。**✅ 已交付(2026-08-28,PR #1368)+ 发测试 `a8e4382a`**;guard 帧实证 max=48 生效,撞顶后 worker 诚实报未完成、主线补派收尾。sop2 worker 步数已放宽并知会 project-service | ✅ |
| B-37 | **子 Agent 上下文继承(技能继承 + 共享工作区认知 + 委派四要素)**——实证起点=会话 abf70030(run d73fc7e3,**ai-health-plan**;权威归属查 `thread_meta.agent_name`,别靠名字模糊匹配猜——本条初稿把主角误记为 sop2):做 PPT 的 worker **没有 pptx 技能**(该 Agent 绑了 19 个技能含 pptx/docx/pdf/banner-design/ui-ux-pro-max,worker `skills` 被剥空,连 `skill_view` 入口都不注册),只能靠主 Agent 把风格锚**手抄进 task 文本**,真正渲染退回主 Agent 自己跑 render_plan.py。**业界六框架调研结论**:子 Agent 提示词独立生成是共识(我们没做错),但人人都有一条「共享约定」通道由框架自动下发(Claude Code CLAUDE.md+`skills:` 预载 / CrewAI crew 级技能 / ADK global_instruction / LangGraph Store+中间件 / OpenAI RunConfig 注入),**我们唯独缺这一档**;且**没有一家能自动识别提示词里哪段该共享**——统一答案是「搬到共享位置」。**设计原则(负责人拍板)=平台缺口先改默认行为,不设计成让配置者去配**(「Claude Code 就没让我做特殊配置」;opt-in 开关有人会忘、忘了静默跑偏)。**本批零新增配置项**:①worker 继承父 manifest 技能(技能=Agent 级共享约定的标准容器;惰性加载常驻仅摘要行)②继承技能软失败(模型不匹配/工具冲突 skip+log,否则 worker 换便宜档模型会炸掉整次委派)③worker 提示词点明共享工作区(用户级偏好那条腿)④spawn_worker 描述补委派四要素+「约定用路径引用别抄内容」。运维回滚阀=env `dynamic_worker_inherit_skills`(不进 UI)。**明确不做**:全量继承父提示词(非标准)/提示词标记块/per-agent 继承配置。spec=`docs/superpowers/specs/2026-08-28-worker-context-inheritance-design.md`。**✅ 已交付(2026-08-28,PR #1369)+ 发测试 + 真栈行为探针 5/5**(worker 真的调了 `skill_view` 拉模板正文——按约定用行为探针验,没问它自己)。**勘误**:技能种子路径用**父** key 不是 worker key;worker 子 run 不落 `agent_run` 表 | ✅ |
| B-38 | **(#1392/#1393 明写的边界,2026-08-30)委派上下文只覆盖「本轮用户附件」**:文档路径与图片引用已结构性进子代种子消息并活过检查点续跑,但**用户偏好、历史轮的附件、跨轮上下文仍不进子代** —— 与 [[B-37]] 同族(子代不继承对话是刻意设计,缺的是「共享约定」通道)。真栈上翻过的车是主 Agent 漏抄文件名导致 worker 选错文件;同类风险在别的维度上仍在。要不要再开通路、开哪些,等下一次实证 |
| ~~B-39~~ | ~~#1392/#1393 的真栈复验~~ **✅ 两半全过(2026-08-31,测试环境 `7ed71857`)**。判据不看模型行为,看**检查点** —— 子代跑在自己的 sub_thread_id 上,种子 `HumanMessage` 落进 `checkpoint_blobs`,而 `[attachments]` 这个字面标记只可能出现在子代种子里(父消息里是 `[file attached: …]`)。**①附件到达子代**:`pf-probe` 传文档起一轮派了 3 个 worker,3 个子线程种子里都读到 `- uploads/b39-probe-doc.md`;对照排除误读 —— 父线程含 `[file attached:` 9 条、含 `[attachments]` **0 条**。另实证 `turn_documents` / `turn_image_refs` 是真实被 checkpoint 持久化的通道。**②审批续跑不丢**:给 `pf-probe` 配 `approval_required_tools` 后,run 撞 `write_file` 闸 `paused`(审批单 `policy_gate`)→ 对外 API 批准 → continuation `is_resume=True` `graph_input=None` 恢复并 `success` → **新增** 2 个子代线程,种子仍带 `- uploads/b39-resume-doc.md`(判据用「新增」而非总数,排除拿暂停前的旧证据充数)。**过程勘误**:探针脚本一度打印「没停在审批闸」,那是 SSE end 帧没被解析到的误判,查库才见 `status=paused` —— 别拿脚本输出当被观测之物 |
| B-40 | **(2026-08-30 记账)对接方文件名乱码修复的我方验收** —— 根因已定位在对方 `busboy`/`multer`(见进度节),对方修完待发。发版后我方查库里新上传行的 `ref` / `filename` 两列贴给对方。**验收判据是「有没有汉字」,不是「有没有下划线」**:我方 `_safe_workspace_name` 的 `[^\w.-]+` 清洗里 `\w` 是 Unicode 感知的,汉字原样保留,但空格 / 全角括号会变下划线 —— 正确结果长这样 `uploads/糖尿病健康管理AI_SOP_内部试用版_v1.1.docx`。另:`user_upload.filename` 存的是清洗后的叶子名,**原始文件名全平台不留**,对方已明确不需要、不加字段 |
| ~~B-41~~ | ~~嵌套执行的双计是否只在耗时上~~ **✅ 已查清(2026-08-31,PR #1403)。结论:只有耗时那一路是双计,已修;另三路都不是** —— 但**其中一路的不变式从没被钉住**。逐路对账:**耗时**=双计(同一段墙钟既进父工具行又进 subagent 行),#1370 已修且有测试;**token**=不双计,靠两件事同时成立 ——(a)孙 worker 的 end 帧也直达父 run 的 bridge(`_child_run` 向下透传 `worker_event_sink`),(b)每个 end 帧只装自己那份(子代跑在自己线程上,回父侧只是一条不带 `usage_metadata` 的 `ToolMessage`);**成本**=token 的纯函数(`costCnyOf` = `summary.usage` × 费率卡),token 不双计它就不双计;**产物**=`turnArtifacts` 按名去重,且「两个源同名」这一情形真有测试盖住。**真正的发现**:token 那条不变式在**三处被声称、零处被钉住** —— `_usage_of` 的 docstring、`turn_summary.ts` 的注释,以及一条名叫 `counts each worker once across a whole delegation tree` 的测试;而那条测试的 fixture 因为 helper 把 `worker_id`/`parent_worker_id`/`depth` 全写死,实际是**同一个 w-1 的 end 帧发了三遍**,没有树,所以任何嵌套相关的回归都不可能让它变红(同 [[verify-where-it-can-fail]]、[[description-is-not-the-thing]])。已补两条真嵌套的钉子(前端两层树 + 后端「父的账不含从孙那儿回来的东西」),变异自证:后端读一下 `additional_kwargs` → `3300330 == 330` 红(一万倍,正是那个形状);前端只认 `depth === 1` → 两条齐红 | ✅ |
| B-42 | **✅ 已修(2026-09-08,#1432 后端 + #1436 前端:usage 按 (provider, model) 分桶 `usage_by_model`,worker end 帧 + run/对话 tokens 都带;前端成本 = Σ 桶 × 各自费率卡,同模型逐位相同;桶按**配置的**模型记不按响应别名;worker 卡缺失时成本留空不退回主卡)**。原记录:**(B-41 查证顺带发现,2026-08-31)整轮成本按主 Agent 的费率给 worker 的 token 计价** —— 不是双计,是**错价**。`TurnBlock.costCnyOf` 是 `summary.usage × 单张费率卡`,而那张卡是「the agent model's rate, fetched once per (provider, model)」(`PlaygroundTab.tsx:240`);自 #1374 起 `summary.usage` **含 worker 的 token**,而 #1322 允许 `dynamic_workers.model` 给 worker 换模型 —— 两个 PR 各自都对,合起来就错。量级不小:线上 run f562fa69 里 worker 占 3,317,974 / 主线 175,137,**95% 的计价 token 来自 worker**。**前端修不了**:`build_worker_end_frame` 根本不带模型名(`outcome`/`iteration_used`/`llm_call_count`/`wall_clock_ms`/`usage` 五个字段),数据源里没有这笔信息。修法=end 帧补 `model`(或 provider+model)+ 前端按模型分别取费率卡累加。**计费不受影响** —— 那条走 `token_usage` 行 + rollup,按 `{parent}-worker` 逐次调用记全,与本条无关;错的只有控制台显示的那个数 |
| B-43 | **(#1439 线 C 发现,2026-09-08)credential-proxy 的 SecretStore 后端在 test/prod 两个 overlay 仍是 `local_dev`**(启动读一次 dotenv 永不重载),control-plane 是 `sql_encrypted`;`make_secret_store` 工厂连 `sql_encrypted` 都不认。总线现在能清缓存,但代理下次 resolve 读回的还是那份静态文件 —— **管理台轮换的凭据代理看不见**。先在真栈核实代理今天到底服务哪些路径(沙箱 egress 注入?哪些 ref?),再对齐后端;对齐前 B-31 ① 只是管道通了 |
| B-44 | **(#1440 线 A 留,2026-09-08)** ① 同一凭据被不同 Agent 配不同 `rate_limit_rpm` 时,全局桶上限跟最后调用方走 —— 根治要把 rpm 提到平台级按凭据配;~~② `ResolvingReranker` 静态版未接工厂~~ **✅ ② #1453 已合(09-09)**;③ 降级没有 metric 只有日志。剩 ①③ |
| B-45 | **(#1443 线 D 留,2026-09-09)** retention_cleanup_job / event_log_archive_job / sandbox_supervisor.store 各自进程**从不调 `build_rls_sessionmaker`**,listener 不存在 → 生产无 Detect 信号也不会 enforce(spec §8 残余风险 2);enforce 前要么接同一 sessionmaker,要么书面接受。**✅ retention_cleanup_job #1456 已接 `build_rls_sessionmaker`(09-09)**;剩 event_log_archive_job / sandbox_supervisor.store 两个。**2026-09-10 记账**:当天曾列入「顺手清的小批」但**实际没做**,#1482 那批只含 B-47/B-1/B-13/B-17①②/B-18;它是 10-09 RLS enforce 的前置,别再漏 |
| B-46 | **(P-2 拍板 5,2026-09-09,用户要求入册防遗漏)按评测集跑评测、出分数**:P-2 做完的回路是「👎 → 待审池 → 人审 → `eval_dataset`」到此为止;今天生产**没有任何东西跑这张表**(技能进化 worker `ENABLE_SKILL_EVOLUTION_WORKER=false`;eval_worker 只跑 `tools/eval/datasets` 文件集,`eval_engine.py` 零处读 `eval_dataset`)。要定:跑什么模型 / 怎么打分(LLM judge 还是 expected 比对)/ 结果放哪看(quality 面板另开一块,不受 `ENABLE_QUALITY_MONITOR` 开关)/ 按 agent 版本归因用 `agent_run.agent_spec_sha256`。等样本攒到几十条再排 |
| ~~B-47~~ | **✅ 已修(2026-09-10,#1482)**:全仓 grep 出的使用点是 **9 处**(本条原记录只点了 3 处),新建 `control_plane/advisory_locks.py` 收 9 个常量 + `CLASSID_BY_OWNER` 登记表,9 处全改引用,各自那份手工「既有取值」列表(撞号成因)一并删。第 10 处 `event_log/db.py` 走单参 `bigint` 空间、无 classid,不入表,理由写进 docstring。自审三条(不得重复 / 常量必须进表 / **AST 扫描禁止任何模块再写 classid 字面量**),四种变异逐个红→还原→绿。**挪号方向经主线纠正换过来**:让位的是 `workspace_janitor`(→8621),`trigger_delivery` **保持 8619**——判据不是「谁先占 / 文档里名字更响」,是**滚动发布窗口**:改已上线的锁要付一次互斥真空,投递锁的 split-brain 是「同一结果投递两次 / checkpoint lost-update」(它自己 docstring 写的),janitor 的各阶段幂等且输的副本整轮跳过(runbook 的 `cycle_failed` 排障项已把 janitor 并发 cycle 记为已知非损坏状态),代价只是一轮浪费的归档上传带宽。模块 docstring 新增「Renumbering a live lock」一节把这条判据写给后来人。**勘误**:说谎的注释是**两条**不是三条,`supersede.py` 那条其实如实记了这次重复。原记录:advisory lock classid `8619` 被两个 worker 同时占用:`trigger_delivery.py:54` 与 `workspace_janitor.py:69` 各自注释都写「本模块取新的 8619,永不共键」——两边都以为自己独占。今天不炸是因为第二个 key 不同,但这是**注释即证据**的典型坑,下一个抄注释挑号的人会真撞上。修法:建一张 classid 登记表(代码里一处常量集合 + 一条自审测试「classid 不得重复」),两处引用它 |
| B-48 | **(P-2 Task 0 #1463 发现,2026-09-10)`event_log` 表在生产与测试双双 0 行**(真正在用的是 `run_event`),而 `0014_feedback.py:11` 的注释仍写 `turn_seq` 指向 `event_log.seq` —— 表废弃 + 注释过时**双重**。顺带:`feedback.turn_seq` 是死字段(控制台传的是 UI 局部下标,全仓无读取方),P-2 保留不动。要定的是:`event_log` 是删表还是留着,`turn_seq` 是删列还是改口 |
| B-49 | **控制台角色轴(2026-09-10 实证重写,原记录的数全是错的)**。**方法**:建真 app、摊平依赖树取出 **182 条** `console_only()` 路由,用 viewer / operator / admin 三种 JWT 各真发一次请求,**判据只认响应码不认 grep**;基线被 422/503/404 遮住判定点的 19 条另做四轮种子化复探(真 agent / 真会话 / 真触发器 / 真产物 / 合法请求体)。矩阵在 scratchpad `b49-scan/matrix_final.csv`。**结果:BLOCKED(403)84 条 / REACHED 96 条 / 不可判 2 条**;viewer↔operator 判定不同 48 条,operator↔admin 不同 37 条。**原记录「62 条只有 7 条有角色闸」两版都错,而且错的方向相反** —— 角色轴大面积存在,`require()` 覆盖 107 条,另一批闸在 handler 体内(`ensure_resource_access` / `is_admin`),**任何静态扫描都看不见**:同一个 router 里 `disable`/`enable`/`rollback` 挂显式 `require("manifest","write")`,而 `create`/`update`/`draft`/`publish`/`delete`/`fork` 走 handler 内 `ensure_resource_access`,行为等价——两版扫描都栽在这儿。缺的不是角色轴,是**覆盖不全 + 位置不一致**。**点名的 9 条里 8 条被推翻**(`/v1/agents` 那族 viewer 全 403 实证;`agents.py:1386` 的注释甚至明写「单独一个 `require()` 会和这个调用冗余」)。**✅ 两个真洞已修(#1489)**:① 租户 kill-switch —— `scope="tenant"` 分支零角色判断,viewer/operator 打 `engage`/`release` 都 200,**一个 viewer 能关掉整租户的技能进化再自己放开**,而模块 docstring 白纸黑字写着「a tenant admin manages their own tenant」;修成 `tenant_config:write`(矩阵里只有 ADMIN 持有),因按 body `scope` 分支、路由级挂不上,闸放 handler 体内,并把 `_authz.py::require` 的判定+审计+403 抽成共用 `ensure_allowed`,**保证两条路径的拒绝报文按构造同形状**。② triggers 四条写操作(`POST`/`PATCH`/`DELETE`/`:fire`,`:fire` 真起了一个 run)零角色闸 → `session:write`(operator+);归属闸一行没动,8 条既有测试身份 viewer→operator。变异四轮全红(删 `ensure_allowed` → 恰好 4 红 admin 绿;去掉 `require` → 一路由一红;删审计块 → 4 红)。**剩 ~28 条写面等一条产品拍板:「viewer 是只读旁观者,还是不能改配置的普通员工」** —— 现状默认后者(能建会话、改 plan、起触发器、删自己的产物)。这条不定,sessions/plan/artifacts/workspace 那批一条都动不了。**建议 admin(高置信)**:`GET /v1/tenants/{tid}/config/credentials`(凭据元数据)、`GET /v1/webhook-endpoints`(+`/{id}`,出站地址;写面已 `is_admin`,读面全员可见不对称)。**建议 operator(高置信)**:`POST /v1/skill-evolution/skills/{id}/promote-requests`(往晋升队列塞候选,审批侧已是 `manifest:write`,提名侧零闸不对称)、`POST /v1/knowledge/bases/{name}/test`(闸挂 `manifest:read` 但会真打一次 embedding,花钱)。**两条不是漏挂闸、要动决策矩阵才能改**:`GET /v1/audit` 系列对 viewer 开放是 `rbac.py` 里 VIEWER 被**显式**授了 `audit:{read}`;`DELETE /v1/agents/{name}/{version}` 对 **operator 也 403**(`manifest:delete` 只在 ADMIN 表里),而同族 PUT/publish operator 能做——「能改不能删」是有意还是漏配,值得拍一下。**探针方法两个坑**(供复用):合成 body 用 enum 第一个值会造出**假 BLOCKED**(kill-switch 就是这么一度被误判成 403);404/400/422/503 都可能遮住 handler 体内的判定点,而**路由级依赖跑在 body 校验之前**(9 条实证:同一请求 viewer 403 / admin 422),所以装饰器上的闸不会被 422 遮,被遮的只有 handler 体内的 |
| B-50 | **(用户 2026-09-10 提问引出)用户工作区没有 agent 维度,同一用户的多个 agent 正在互相读错写错**。NAS 布局 `{root}/{tenant}/{user}` 零 agent 段;`artifact` 唯一键 `(tenant,user,name)` 零 agent 列;`list_artifacts` 描述写「**你**存的」实际返回该用户全部,`list_dir` 写「**the agent's** workspace」实际是用户根 —— **喂给模型的事实是错的**。**不是假设**:测试环境 64 个有会话的用户里 **8 个已用过一个以上 agent**(6 个正是 ai-health-plan + sop2-designer);最大那个工作区 435 文件 / 112 会话,根级 `MEMORY.md`、`style/`、`客户案例/` 由两个 agent 共同读写,**哪一句是谁写的查不出来**(无登记行)。**这个问题代码库已经解过两次** —— `/opt/skills/<agent_key>`、`/opt/agents/<agent_key>` 的 docstring 明写「two agents sharing one warm sandbox」,`agent_key` 早就存在并绑在 SandboxRuntime 上;唯独 NAS 工作区漏了。**布局被约束锁死**:热沙箱按 `(tenant,user)` 复用(`sandbox_instance` 无 agent 列、`acquire()` 不收 agent)、CSI `subPath` create 时钉死 → 挂载点不可能按 agent 分,只能按路径分 + 工具边界执行,与既有两处先例同构;那条禁止改挂载点的构造期硬闸与 `workspace_user_root()` 都不动。**用户拍板(09-10)**:交付物与中间文件都按 agent 分、`uploads/` 也分(上传本就从会话进来、会话属于某个 agent,平台在上传那刻就知道、只是写盘时丢了);两个 agent **不互读交付物**;存量能反推的反推、反推不出的进 `shared/`(读可达、写拒绝,保证迁移日 agent 工作记忆不归零)。**三层寿命**:`.tool_results/` 与 `threads/` 同规则(会话 purge 连带删 + 孤儿宽限扫描)、agent 持久工作态**无 TTL**(用户指出 `MEMORY.md`/`style/*` 是多轮一致性的载体,按时间删不成立——立项时我提的 TTL 瞄错了层)。**顺带勘误一条说谎的注释**:`overflow.py:44-47` 称 `.tool_results/` 的生命周期由留存机制负责,实则留存 job 的不变式是「根目录其它文件永不触碰」且该目录无登记行,它引的「90 天归档」是**已删工作区**的归档 —— 实测单用户堆了 40 个 run 目录,一次没被清过。**spec + 14 task / 6 PR 的实施计划见 #1490**;真栈验收拿 `99d3c664` 那个用户做判据(搬前搬后文件数守恒 / 两个 agent 目录各自非空 / `shared/` 是反推不出的那批)。**已接受的代价**:那 8 个用户第三层 legacy 的读串问题**不被本次消除**,只随时间稀释(立刻消除需业务侧人工认领每个文件) |
| B-51 | **(2026-09-11 查 D 线时撞见)平台凭据页会让运维得出「不用配」,而平台自己的后台功能正因此空转**。实证:**生产 24h 报 6 次、测试 24h 报 208 次** `memory_consolidator.cluster_failed`,栈底 `tenant_secret_overlay.py:62` → `CredentialsResolverError: platform credentials missing for provider=anthropic`;每次 `sweep_complete` 都是 `consolidated=0` —— **长期记忆整合从上线起就没成功过,两个环境都是**。**配置入口是有的**(`SettingsPlatformConfig` 的 credentials tab 按 `PROVIDER_CATALOG` 全量列,未配置的也在、`source="unset"`,`platform_config.py:316`),问题不是没地方配。**问题是页面主动误导**:`used_by_agents`(`platform_config.py:312`)**只数 agent**,而平台自身有**四个** provider 依赖点(`settings.py:285 embedding_provider="qwen"` / `:630 eval_agent_provider="anthropic"` / `:651 quality_judge_provider="anthropic"` / `:764 memory_consolidator_default_aux_provider="anthropic"`),**一个都不计入**。于是 anthropic 那行显示「`未设置` + `被智能体引用 0`」,运维据此判断不需要配,完全合理。生产上 eval 与 quality monitor 是关的,今天只有记忆整合在撞;**那两个一旦打开就是同样的死法**。 <br><br>**方案(用户 09-11 拍板)—— 不动默认值、不加启动闸,只把「没配」变成看得见**:<br>**① 病根是 `summary.errors` 丢了原因**。`memory_consolidator.py:497` 的 `errors: int = 0`,八个调用点全部只 `+= 1` —— 它知道错了 N 次、**不知道为什么错**;日志里的 `errors=1` 与栈里那句 `credentials missing` 是两条互不相连的东西。改成按原因分类计数(`credentials_missing` / `llm_timeout` / `other`),界面读它。**数据通路本来就在,只是丢了原因**,不需要新建健康检查体系。<br>**② 凭据页加「平台自身在用」维度**,与 `used_by_agents` 并列(数据就是上面四个 settings 键),Tag 悬停列出具体功能 + 模型,并**区分「已开启 / 未开启」**。<br>**③ `未设置` + 有平台依赖 + 该功能已开启 → 来源那格红色警示 + 页面顶部 Alert**(带最近失败时间与累计次数、直达配置)。**未开启的只在表格里标,不弹 banner** —— 否则生产一上来就红三条、其中两条运维根本不用管,噪音会淹掉真问题(用户拍板)。<br>**④ 记忆面(`MemoryAdmin.tsx`)顶部读同一数据源**显示「整合已连续失败 N 次,原因:平台凭据未配置」。<br><br>**明确不做两件**:**不把默认值改成空** —— `Provider` 是 `Literal[9 个厂商]`,空串非法、pod 直接起不来;**不加启动期凭据闸** —— `app.py:2024` 的注释记着这条路走过并撤掉了:旧闸判的是 env 里的 `effective_platform_provider_credentials`,而凭据的权威来源是**金库(UI 粘贴)**,启动时看 env 会把「已在界面配好」误判成没配 → 装 null adapter → 静默空转零整合,与 skill-evolution worker #656 同款。**那个 bug 与本条是镜像**:一个「配了却假装没配」,一个「没配却假装配了」,加回启动闸等于把另一个洞挖回来。<br><br>**单独留一条待拍**:四个平台依赖里三个默认 `anthropic`,而实际主力是 glm / kimi —— 默认值指向大概率没配的厂商本身在制造这个问题。但改默认值是行为变更,且上面这套做完「没配」会自己喊出来,优先级可降。 |
---

## D · 调试台 program follow-up —— D-1~D-6 ✅ 全清(D-1~D-4 于 2026-08-20,D-5/D-6 于 2026-08-23);**仅剩 D-7**

| # | 事项 | 结果 |
|---|---|---|
| D-1 | Run 详情页轨迹「真配对」e2e 用例 | ✅ `e2e/run_detail.spec.ts` 增真配对用例(配对 messages/runs + 真 SSE 回放体 → 账本行断言)。**顺带逮到既有假象**:该文件的 messages/runs 桩用裸 glob,匹配不到带 `?tenant_id=` 的真请求,页面一直靠「穿透→报错→降级空态」活着 —— 改 URL 谓词匹配(exact pathname),桩第一次真正生效 |
| D-2 | RunDetail 切线程竞态守卫(Ruling 4) | ✅ `convoTenant` 状态改为 `{threadId, tenantId}` 打上来源线程标签,history 效果拒绝错线程的 tenant;单测用 `useNavigate` 同页切线程驱动潜伏帧,变异自证(拆守卫→红) |
| D-3 | `runs_page.summary_resume` 死 i18n 键 | ✅ 三处(en 类型/en 值/zh-CN)全删,全仓零残留 |
| D-4 | `RecordDetails.tsx:241` 既有占位行空 `<dt>` | ✅ **已被 PR-B 顺手修掉**(占位行已是 `<dl>` 外的散文 `<p>`,注释明写避免空 `<dt>`),本波仅核实 |
| D-5 | ~~**对话详情页:按轮重建 + 尾轮 live**~~ | ✅ **已交付(2026-08-23,与 D-6 同 PR)**:配对容忍尾部连续非终局块(running 新轮 / paused 等审批 / paused+continuation 刚起);尾轮 eager live attach(增量 rAF 合批,end 帧→done+静默刷新);重建键改稳定 (thread,tenant) 键 + 增长触发一次性重配对(防两源短暂不一致打环);live 载入态循 playground 先例映射 done+running。中段 paused(continuation 已终局)维持诚实降级→D-7。**已知限制(终审 I-6)**:live 接合订阅本进程 InMemoryStreamBridge,多副本下 run 属别的副本时 attach 只有一次性补库、无实时帧(断流重挂 5s 兜底会反复重补库拿到增量,非零但非推流);真正的跨副本 live 归 X-10 多副本方案(bridge 需换共享通道或加状态轮询 fallback) |
| D-6 | ~~**对话详情页操作面:批/拒/取消**~~ | ✅ **已交付(2026-08-23)**:paused 末轮就地批/拒(从 approval 帧合成卡;有后继 run 不合成防 409;`allowDecide` 单独放行,R2 全链只读其余不破)+ running/pending 尾 run 取消。**勘误**:「后端端点全现成」失实——run 级取消端点当时不存在,本次新建 `POST /v1/sessions/{t}/runs/{r}:cancel`(复用 tenants 批量取消内核,operator+ 闸,paused 409 走审批路);角色 gate=operator/admin+本租户;后端闸:cancel 端点本 PR 自带 `require(session,write)`,**decide 的后端闸随 #1253(B-20 ④)落地——两 PR 都合并前,前端 gate 是 decide 路径唯一角色闸**(终审 I-9 指正) |
| D-7 | **审批 continuation 链折叠**(2026-08-23 立项,D-5 裁定的遗留) | 一条用户消息对应 paused 父 run + continuation 子 run(可链式),count 配对对此天然歧义;continuation 终局后硬刷新回落扁平视图(与今日行为一致,非回归——decide 后的当场富视图已由「刷新只 patch 不重配对」保住)。做法:① `GET /{thread}/runs` 补 `continuation_of`(join approval 行的 `continuation_run_id`;ApprovalStore 需补 list_for_thread);② 前端把链折叠进 owner 轮——注意各 run 的 seq 独立编号,直接 concat 违反 usePlanCard 严格递增帧约束 |

---

## 建议顺序(2026-08-31 更新)

> 口令历史:2026-08-23「ask_for_approval 默认关(✅ #1252)→ B-20(✅ 主体)→ D-6(✅)」;
> 其后 08-26~08-30 数批均由用户当场下口令,已全部交付。**其余仍等用户口令再开工。**

### P0 —— 唯一真正卡住 09-10 的一件

**生产资源开通 + 开荒**(runbook §0-§1)。2026-08-31 核实:prod **零开荒** —— overlay 15 处
`PROD_PLACEHOLDER_*` 待填、`~/.kube/expert-work-prod-params.env` 与 prod kubeconfig 都不存在。
阻塞项全在用户侧(阿里云开通),不是代码。**域名 + 证书提前量最长**(备案/签发可能数天),
它卡住后面全卡。执行视图见开荒调度板 artifact(按「你侧 / 我侧」分道 + 依赖链 + 易错点)。

开荒清单核出来的三条,runbook 自己没写清:

1. **只有 8 个值进 git**(15 处 yaml)。`secrets.env.example` 里另有 32 处是**模板** ——
   填的是本地副本,**原文件永不提交**。runbook §1.2 写「grep `PROD_PLACEHOLDER` 逐个替换」
   没分这两类,照字面做会把生产密钥提交进仓库。
2. **金丝雀要两家厂商的 prod LLM key**(主 + 跨厂商备用)。§0 资源表漏了这条,
   而金丝雀是**发布合格判据**,没有它发布判不了绿。
3. `newTag` 不用手填,首次 `release.sh prod` 自动钉,预检也专门放行它。

### 发布前必修 —— ✅ 2026-08-31 三条全清

原本 B-32 / X-8 排在「发布后第一波」,核实后把 B-32 提前(生产必踩):

| | 为什么必须在发布前 |
|---|---|
| ~~**B-32**~~ ✅ #1398 | 生产**强制**走 redis 配额引擎(多副本无 quota Redis 直接拒启)→ QPS 类配额在生产等于没有 |
| ~~**B-31 ②**~~ ✅ #1399 | 「能写白名单 = 能让代理把任意租户密钥送到任意地址」,可达面至少集群内所有 pod。**勘误(2026-09-07)**:#1399 合了 30 多小时**没进过任何环境** —— credential-proxy 不在 release.sh 里,test/prod overlay 手工钉 94f1a371;测试环境 `20388f4b` 发完 pod 内探 `/admin/allowlist` 无 token 返 405 而非 503 才发现。修法 = 进发布路径 + prod 改占位符 + smoke 校验 tag 一致 |
| ~~**X-15 ②**~~ ✅ #1400 | 澄清超时降到 1h 后,「续跑丢 per-user OAuth 池」从罕见变常态;实测 38/38 条审批单无主 |

### 剩下十天的可直接开工项(不依赖环境、不依赖拍板)

1. ~~**X-8 / CI 镜像离 Docker Hub**~~ **✅ 已交付(#1402)** —— 原**提到第一位**。08-27 四次、08-29~30 两次、
   **08-31 又一次(PR #1398 的 integration 跑满 30 分钟换一条假红)**。
   这条的成本不在「做它要多久」,在于它每周都在收利息,且发布当天再撞一次会很难受。
2. ~~**B-41** 嵌套执行双计的同构核查~~ **✅ 已查清(#1403)**:只有耗时那一路是双计(#1370 已修),
   token / 成本 / 产物都不是。但 token 的不变式**在三处被声称、零处被钉住**,已补两条真嵌套的钉子。
3. **B-42** 整轮成本按主 Agent 的费率给 worker 的 token 计价(B-41 顺带查出)—— 不是双计是**错价**;
   线上那个 run 里 **95% 的计价 token 来自 worker**。前端修不了,`build_worker_end_frame` 不带模型名。
4. **B-31 ①** credential-proxy 失效链断头 —— **扩容前必接**;token 已随 ② 就位,
   走 HTTP 失效方案的前置条件已满足。
5. **B-33** 模型温度约束进 catalog —— 现在靠厂商 400 当校验器。

### 等外部

- **B-40** 对接方文件名乱码修复的我方验收(等对方发版)。**判据是有没有汉字,不是有没有下划线。**
- **PPT 内容质量人工抽查** —— 真栈验收只剩这一项,用户侧。

### 需要拍板才动

- **P-1~P-5** 产品题;P-4(api_keys 凭据横向扩散)风险最高,建议先议。
- **X-1 沙箱波 4** 或 **X-10 多副本整体实施方案** —— 两个大体量项,按业务优先级择一。

**发布后第一波(已排队)**:RLS PR B / Redis 全局令牌桶 / 取消亚秒化 / X-1 / X-10。

## 这一轮攒下的教训(派发时带上)

1. **否定性断言必须穷尽**。"一道闸都没有"/"零 authz"/"从未" —— 窗口有限的 grep/sed 的
   "没找到"只支撑得起**肯定**断言。本轮因此错判 `fork_template` 为零授权(它有
   `ensure_resource_access`,在我 40 行窗口外的第 978 行),写进 brief 后**直接造成一个未修的
   Critical**(implementer 照我说的跳过了 `POST /v1/skills/import`)。
2. **数字必须带口径**。本轮三次同类错误:external 路由"2 条"(是文件内 vs 前缀下)、
   基线"93 passed"(是双文件合计 vs 单文件)、报告"351 用例"(把被测文件自己的数折回去了)。
   凡写进 brief 的数字,连口径一起写。
3. **单独看低危的缺口,组合起来是 Critical**。我在 Task 3 brief 里亲手把"员工 viewer 能注册
   webhook"标成"独立的低优先级问题" —— 它后来成了绕过整条 `agent_private` 隐私修复链的通道。
4. **凡"这种数据不会存在,所以不用过滤"的推理,先找出所有能产生这种数据的路径**,
   而不是只想到攻击路径。上一轮漏判 `list_promote_requests` 就是漏了"admin 合法开
   promote-request 是正常工作流"。
5. **让复审独立判断而非采信报告叙述** —— 它防的不只是过度自信,也防过度自责:本轮有一次
   实现者自报"这条测试无法独立证明 GET 未受影响",复审实测反驳,证明它**能**独立捕获,
   省了一轮白改。
6. **加闸时不光看闸挂没挂,还要确认目标路径没被上游认证豁免吃掉**
   (`auth_exempt_path_prefixes` 里的 `/v1/webhooks` 差一点就吃掉 `/v1/webhook-endpoints`,
   靠的是豁免判断写的是 `prefix + "/"` 而非裸 `startswith`)。
