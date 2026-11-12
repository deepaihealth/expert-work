# ROADMAP 归档

> 2026-11-12 从 `docs/superpowers/ROADMAP.md` 移出:已完成(已上生产 / 已完成 / 已结案)的条目,和整段过时的历史小节。条目原文逐字搬来、未改,按原所在小节分组(保留原小节标题)。
> 部分完成、主清单只留了没做部分的条目,原文全文也在这里。按 11-12 核实移走的条目,行前有一句「11-12 核实:…」写明依据。
> 更早的历史见 git:`git log -p -- docs/superpowers/ROADMAP.md`。

## 🧪 统一发测试(2026-10-02 用户拍板,10-03 改为提前发)

> 11-12:仍有效的规矩(每个要上测试才能验的 PR 在同一个 PR 里加验收用例)留在主清单;下面是当时的日期安排原文。

> **10-03 改拍板**(取代 10-02 的「10-08 前测试环境保持 `54d1ed70` 不动」):主干上做完的这一批提前重钉 main 统一发测试,按 `docs/runbooks/pending-test-release-acceptance.md` 逐条做真栈验收,随 10-08 上生产;**10-07 判定,不过就退回 `54d1ed70`**。10-03 已重钉 `fc07b8b1`(执行单第十五次重钉),除 §3(沙箱镜像本班不带)、§6.5(上线一周后量)、§8.5(无只读账号)、§9.3(部分覆盖)外全部过。**10-04 24h 复查 GO 带三条问题(B-155 / B-156 / B-157),修完合 main 第十六次重钉 `5091938c`,测试真栈过(清单 §10)**。上线后再销案。
> **10-07 改期**:生产推迟到 **10-12(周一)**,用户拍板周末赶工、整理的这一波尽量全上(清单 §14–§19:依赖修复、B-152 / B-153 / B-161 / B-142 / B-141 / B-143);10-07 判定 `85af5304` GO。流量低,「上线后 24h / 一周看」一律改用 B-140 评测主动造的运行来量;10-11 周日 12:00 前没过的项撤出本班。
> **每个要上测试才能验的 PR,同一个 PR 里往该清单加用例**(怎么触发 + 看哪里 + 判据)。截至 2026-10-02 待验:B-136(#1718)、Python 依赖(#1716)、沙箱镜像依赖(#1713,**要单独重建沙箱镜像才生效**)、admin-ui 依赖(#1714 / #1715)。

## 排期(2026-09-08 拍板;生产已于 09-08 上线 `ad79ba28`)

> 11-12:本节唯一仍未做的「RLS 真 enforce(PR B)」已提到主清单「其它 program 的挂起项」表(行 `RLS`);其余条目已完成或在主清单各有一行。

> 生产已在线,发布从「一个日子」变成**每周一班车:默认周二**(周一合完泡测试一天)。本周(09-08 ~ 09-12)只进测试环境。
> 并行按文件冲突分线,零交集的线 worktree 并跑;每条线一个或两个 PR。开工前按行内来源核实现状(本文件两次误报都是没核实)。

| 周 | 生产班车 | 同周开发(周五前上测试) |
|---|---|---|
| 09-08 ~ 09-12 | 无(09-08 已发 `ad79ba28`) | **✅ 六线全合(09-08 当天,main `77468612`,测试环境 smoke+金丝雀 PASS,记录 #1437)**:A MCP 探针拆 ExceptionGroup(消息+日志给叶子原因)/ B release.sh(X-14 P5 记录 PR 写上一版 tag + B-29 ③ 失败分支假成功)/ C X-8 收官(pgvector·pgbouncer 镜像进 GHCR + 沙箱镜像 build 走 buildx 缓存,integration 不再每 PR 烧 40 分钟)/ D B-42 worker token 错价(usage 按 provider·model 分桶 + admin-ui 按桶计价,2 PR)/ E B-33 模型参数 catalog 约束 / F B-23 platform_items 200 截断 |
| 09-14 ~ 09-19 | **✅ 班车 1 已发生产(2026-09-16 18:20~18:51)**:三段式 **B1 `ca225258`(expand)→ C 存量搬迁 → B2 `5775fbf3`(contract + B-60)**,上一版 `ad79ba28`;两段 smoke + 金丝雀各 PASS,B2 后回落复验与 P-1 重新生成验证见记录 PR。搬迁实况 **11 个用户 / 222 文件 / 79 产物行**(不是 09-13 勘察的 8 / 152 / 56 —— 对接方 09-14 之后新开 3 个员工账号,验收 ③ 扁平残留一度 20,补搬后 ③④ 双 0);第二个双 agent 用户 `880813f0` 的 `shared/` 5 个 = 根级共写文件,形态与 `81066c49` 相同。**执行中发现并已修**:执行单 B2 正文钉子仍是 `97a2e724`(#1566);单子里 `for U in $USERS` 是 bash 写法在 zsh 下把 8 个 UUID 粘成一个参数。**后续**:B-59 沙箱钉子刷新已解锁;deps 三批(#1520/#1487/#1545)随本班上了生产,班车 2 不再带它们。 **原排期记录(留档)**:班车 1(⛔ 09-14 16:30 再次改期:今晚不发,待 B-60;此前改期 09-14 18:00,原定 09-16)= 本周六线 + 波 2 + **09-10 四 PR**。**📋 执行单 = [`docs/runbooks/2026-09-14-prod-release-checklist.md`](../runbooks/2026-09-14-prod-release-checklist.md)** —— 发布当天照它打勾,别只跑 `release.sh`。**B1 = `ca225258` / B2 = `97a2e724`,两段都钉死提交(09-14 拍板)** —— 此前 B2 写的是 `git checkout main`,那等于把主干从写单子那天一直隐式冻到发布日,顺带堵死开发与测试环境发布;钉死之后 main 照常合、照常发测试环境,新合的东西走班车 2。**连带:发布当晚测试环境会比 `97a2e724` 新,这是预期的不是漏发。** **改期:原定 09-16,用户 09-14 改为当天 18:00** —— 钉子与日期无关,且 `97a2e724` 的测试演练就在同一天上午跑完(#1546),镜像缓存是热的。里面列了这一版 **`release.sh` 不覆盖的四件事**(① 生产沙箱镜像还钉在 `63a3109f`,仓库与测试都是 `e8aac104`,SandboxSet 手工 apply 不进 kustomize;② B-50 工作区存量搬迁,**金丝雀绿之后立刻**跑,漏了工作区浏览面与对外两个 workspace 端点直接返回空;③ P-1 重新生成的真栈验证;④ `chore(deploy)` 记录 PR 写上一版 tag),以及**不在范围**的三件(留存 CronJob 走班车 2、无新增 secret、三个迁移随 migrate Job 自动跑)。**回滚窗口在搬迁之前** —— 上一版 `ad79ba28` 完全没有 agent 维度,搬完再回滚用户工作区按扁平根去读会是空的。清点方法固化进 `production-release.md` §2.0,下一班照做。 | **✅ 波 2 提前全合(09-08~09,main `723c5a40`)**:#1440 Redis 全局 RPM 令牌桶(桶 key = handle + 凭据 ref;Redis 故障降级本地 + 60s 冷却;rerank/aux/judge 四条构建路径全接;`ResolvingReranker` 静态版不在生产路径未接)/ #1441 跨副本取消亚秒化(现状数出来 = 属主靠 `sse.py` 心跳 CAS,lease_ttl/3 = **10s**;新 kind `run_cancel`,五个取消入口收进 `cancel_run_two_level`,handler 走同一心跳 CAS;`verify_cancel.py` 加 2s 上界)/ #1439 B-31 ① credential-proxy 订阅总线四 kind + 跨包契约测试(复用 `control-plane-secrets/EXPERT_WORK_QUOTA_REDIS_URL`)/ #1443 RLS D 线(见下)/ #1442 alembic `fileConfig(disable_existing_loggers=False)`(集成 job 单进程跑迁移后采集期已建 logger 全被关掉,caplog 静默为空 —— 任何集成测试都可能中招)。**合后浮出的新项:B-43 / B-44 / B-45(见小 backlog)** |
| 09-22 ~ 10-12 | **✅ 班车 2 已发生产(2026-10-12 19:04~19:31,`94dc73ba`,上一版 `5775fbf3`;smoke + 金丝雀首跑 PASS,A2/A3/A4 技能与 ai-health-plan rev39 同晚完成,记录见执行单 §6 与本次记录 PR)** **班车 2 —— ⛔ 2026-09-29 用户拍板:09-28 未发,推迟到 2026-10-08;同车带上 B-125(健康方案技能导入 + ai-health-plan rev39,执行单 §0.2 / Step A3 / A4)与 **B-126 跨轮上下文降本**;钉子等 B-126 合入后一次性前移到 main(先发测试再重钉)。** **✅ 09-29 第十一次重钉到 main `ab4097cf`**(带 B-126 / B-127 / B-128 + #1690 / #1691;测试环境 `f29ac13b` 同树,smoke + 金丝雀 PASS,admin-ui 固定值页已发;执行单 §0.1)。 **✅ 09-30 第十二次重钉到 main `39c93912`**(带 B-131 减少修改轮额外调用 + #1701 pyjwt 2.14.0;测试环境 `39c93912` smoke + 金丝雀 PASS,`health-plan-report` v6 = `786d30fe`;**前置:project-service 计费改动(豆包补价 / 未定价桶只跳过 / 缓存价)须先于本班上线**,执行单 §1)。 **✅ 10-01 第十三次重钉到 main `31f22602`**(带 glm-5.3-flashx #1702 + 依赖安全升级 urllib3 #1703 / pyjwt 2.15.1 #1705 / virtualenv #1706;测试环境 `31f22602` smoke + 金丝雀 PASS;生产价目表不补 glm-5.2 / flashx,用户 09-30 拍板)。 **✅ 10-01 第十四次重钉到 main `54d1ed70`**(带 B-133 子智能体委派三项修复 #1710 / #1709 / #1708,用户 10-01 拍板随本班;测试环境 `54d1ed70` smoke + 金丝雀 + 委派两轮探针 PASS)。 **✅ 10-03 第十五次重钉到 main `fc07b8b1`**(用户 10-03 拍板主干上做完的全部随本班:B-136 / B-137 / B-139 / B-149 / B-151 + 依赖三批 + 真栈首轮修复 #1732 / B-153 / B-154;#1713 沙箱镜像依赖不带;迁移多一条 `0160`;测试真栈清单除 §3 / §6.5 / §8.5 / §9.3 外全过;**10-07 判定,不过就退回 `54d1ed70`**)。 **✅ 2026-09-25 用户拍板:2026-09-28(周一)发生产,钉 `f92c6fae`**(**09-27 第九次重钉到 main `68a7b75f`**:在 `f92c6fae` 上加带 B-121 / B-122 / B-123 与 B-119 文案,用户 09-27 拍板;**当晚第十次重钉 `c0789ca6`** 再带 B-124;执行单 §0.1)(09-24 未发;09-20 拍板等 B-84 本波,之后并入 B-64 / B-106 / B-102~104 / B-105 / 豆包修复,执行单 `docs/runbooks/2026-09-24-prod-release-checklist.md` #1671)。以下为原记录 —— **(⛔ 09-17 用户改期:09-22 → 09-24 周四)**。**09-17 拍板装载**:① **B-61** 四个代码 PR(测试环境 T10 39/39);② **B-67 本轮输入平台接管**(09-17 开工,逐 task 派子 Agent,6 PR);③ **B-66** `:regenerate` 命中缓存 + 命中时对外用量 `None`(本班修);④ **RLS 两处补丁**:`workspace_janitor` / `memory_consolidator` 补租户上下文(09-11 第二轮实证无上下文),`skill_curator` 读代码判(两环境都关着,观察不到);⑤ **留存 CronJob 生产 overlay** —— 判据改为「测试环境只用金丝雀账号造一批过期探针数据、手动跑一次、删对了」才加。起因:09-17 核实测试环境可见的最近 3 次(09-14~16)全部成功,但**各项删除数全是 0** —— 测试环境约 49 天,产物/上传/已删工作区都是 90 天规则,「泡一周无事故」其实是「无物可删」,原判据验不出删对删错;生产 09-07 上线,90 天规则 12 月前也不会触发。⑥ **#1577** B-61 续跑 / 重新生成带本轮输入(**阻断项**:不带时审批续跑被判完整性否决、续跑段读不到 inputs.json,B-67 之后提示词里只剩路径);⑦ **审批门修复**(#1581):线程上有待审批的 run 时再开新一轮,新一轮里需审批的工具调用不经审批直接派发 → 改为新一轮作废上一轮待审批(**安全修复**,用户 09-17 拍板进班车 2;同 PR 修 B-77);⑧ **B-76 批准只放行审批单上那一个**(**安全修复**,用户 09-17 拍板进班车 2,「只放行被批的那个」;同 PR 关 B-79;B-78 留待办)。**09-17 晚:以上代码全部合入(main `42426d31`),测试环境已发并通过 B-67 真栈验收(见 B-67 行);执行单另需写入 B-80(滚动发布会中断在跑 run)与 B-76 的发布顺序注意。****本班形态**:~~截至 09-17,`5775fbf3..main` 无迁移、无配置/密钥变动(只有 tag 记录与沙箱钉子),按单段发布~~ **09-18 加装后重核:仍是单段,但有一条迁移** —— 用户 09-18 拍板把当天修的六条全部带上(B-56 / B-72 / B-73 ①②③④ / B-65),其中 B-73 ① 带 `0156`(expand-only,`thread_message` 加 `hidden` 一列带默认值)。**回滚只回镜像,不要 downgrade `0156`** —— 多一列对旧版本无害(旧 ORM 不映射它,既不 SELECT 也不 INSERT,`server_default` 兜住),而 downgrade 会把新版本写进去的值全抹掉,回滚窗口里随时可能再滚回来。另 B-56 改了生产 Deployment 的 `startupProbe.failureThreshold`(30 → 90),是这批里唯一动 k8s 清单的一条。执行单的钉子、区间提交数、迁移预检那行随重钉一并改。 **09-19 用户拍板再加装:B-81 / B-82 / B-83 三条沙箱依赖治理全部在 09-24 前做掉**(理由=「这个问题很影响用户体验」)。B-81 只改 `sandboxset.yaml` 的 env,不动镜像;B-82 要走一次沙箱镜像手工发布(`docs/runbooks/sandbox-image-release.md`)并按 #1233 先例真栈复验;B-83 改沙箱生命周期语义(空闲即删 → 先挂起再删),**是这三条里唯一动运行期语义的**,排在最后做、做不稳就不上这班车。**同时记下 B-84**:我原以为对接方两个 Agent 慢是装依赖导致的,去数之后发现**不是** —— 88.4% 的时间是纯模型段、零 pip、零审批,真因是入均 5 万~11 万 token 的上下文;所以这三条对「对接方体感」的改善有限,真正的大头在 B-84。 **⛔ 09-19 新拦路项:`anyio 4.13.0` 两个新 CVE(`CVE-2026-63374` / `CVE-2026-64847`,修复版 4.14.2)。**`Security (pip-audit)` 是**必需 check**,所以这会**卡住之后每一个 PR**,不是某个 PR 自己的问题 —— #1602(B-81)已经被它拦住;#1601 显示必需项全绿只是因为它跑在公告落地之前(同一天,几小时之差)。dependabot 截至 09-19 还没开票。**清掉它是 B-81/82/83 三条的共同前置**。`anyio` 是 httpx / starlette 的底层,升版要看兼容性,不能只改版本号就合。 **✅ 09-19 已清(#1605,main `818a793c`)**:`uv lock --upgrade-package anyio`,只改 `uv.lock` 一个文件 —— anyio 没有任何直接钉版,是 httpx / mcp / sse-starlette / starlette / watchfiles 五个包共同拉进来的传递依赖。锁里因此出现**两个** anyio,这是预期的解析分叉不是错误:`python_full_version < '3.15'` → **4.14.2**(我们的运行时镜像和 CI 都是 python 3.12,走这一支,落地的就是修复版)、`>= '3.15'` → 4.15.1(本仓库永远不会实例化)。**兼容性判据**:五个上层包版本一个没动(httpx 0.28.1 / mcp 1.30.0 / sse-starlette 3.4.4 / starlette 1.3.1 / watchfiles 1.1.1),本地跑了 async 最重的 8 个测试文件 180 passed,全量交给必需 check `Test (pytest)`(本地全量跑到一半被系统因内存不足杀掉,如实记)。#1605 八项 check 全绿,连一向假红的 `Test (integration)` 这次都是真跑绿。**B-59 沙箱钉子已于 09-16 单独刷到生产(#1568),不再随车。** B2 照班车 1 做法钉死提交,main 照常合。**B-61 的回滚坑必须进班车 2 执行单**:`MCPToolSpec` 是 `extra="forbid"`,旧版本读到带 `arg_bindings` 的 manifest 会校验失败、那个 agent 直接起不来 —— 发布后回滚窗口内生产先别配绑定;真要回滚,先清掉绑定配置再回。**B-67 回滚纪律**:`MCPToolSpec.arg_bindings` 与 `PromptVariableSpec.render` 都是 `extra="forbid"` 模型上的新字段、默认值不落库 —— 回滚窗口内别在生产配 `arg_bindings` / `render: raw`;真要回滚先在配置页清掉。**以下为原记录(留档)。** 留存 CronJob 生产 overlay(test 泡一周无事故才加)+ RLS 第二轮判定 + **deps 车 ✅ 已全合进 main(09-14)**:#1520 python-deps ×7 / #1487 vitest 4.1.11→5.0.0 / **#1545** react·react-dom·@types/react 19.2.8→19.3.0 + lucide-react 1.40→1.44。**勘误(09-16)**:B2 于 09-15 重钉 `5775fbf3`(为带上 B-60),这三批**已随班车 1 上了生产**,班车 2 不再带它们。(原记录:B2 钉死 `97a2e724`,所以它们不进班车 1。)**勘误**:原列的 #1485(lucide 1.43 + @playwright/test 1.63.0)被 dependabot 在 rebase 时关掉、重算 group 换成了 #1545。**二次勘误(同日)**:我当时断言「`@playwright/test` 掉出 group、没有任何 PR 带它」,**是错的** —— #1545 实际带 7 个包,`@playwright/test 1.62.1→1.63.0` 就在其中(另有 `@types/node`、`@types/react-dom`)。错因=只 grep PR 正文里的 `Updates \`X\` from A to B` 行就下否定结论,正文没把 7 个全列成那种格式。**否定断言要去读真实边界**(`package.json` / lock / `git log -S`),别拿一次窗口搜索的「没找到」当证据。**这三条还没在测试环境泡过**(09-14 发的测试是钉子 `97a2e724`,刻意不是 main),09-16 生产之后要从 main 再发一次测试才开始泡+ **B-59 刷沙箱钉子** + **B-61 随车**(main `8f0d21d3` 起都带;测试环境 09-16 T10 真栈过)。 | **✅ 波 3 提前全合(09-09,main `a7c29e20`)**:#1456 留存链(X-15 ① 第二套审批超时已删、B-45 retention job 接 `build_rls_sessionmaker`、CronJob 只进 test overlay、B-27 `threads/` 随对话死亡 + 孤儿 24h 宽限、B-28 产物 90 天、上传 90 天、X-4 ② 已删工作区 90 天硬删;根目录不变式 + tenant/user 目录 symlink 不 resolve)/ #1455 X-4 ① 成员清除两步 + 最后可达 admin 闸 / #1457 B-9·B-10·B-11·B-14 / #1452 X-5 / #1449 X-7 ②③④ / #1453 B-24 + B-44 ② / #1447 P-5。**班车 1(09-16,原定 09-15,用户 09-12 改期)实际装载 = main 全部(波 1+2+3)**,生产不会拿到 CronJob(overlay 未加)。下一波待排:P-1(spec #1454)/ P-2(spec #1450)两份 spec 待用户审 → writing-plans;X-3 Spec2a webhook  **加:B-50 用户工作区按 agent 隔离**(spec+计划 #1490,14 task / 6 PR)—— 前置**两件,已全清**:**B-49 viewer 收窄 ✅(09-11,#1494)**、**B-45 余 2 ✅(09-11,#1493 + #1496)**。**勘误(09-11)**:此前把「对外 `?scope=user`」也列为前置,**错了** —— 它是 B-50 **自己的 Task 9**(计划 §9.4,与六个 handler 按 agent 收口同一个 PR4),不可能先于 B-50 完成:今天 `artifact` 唯一键是 `(tenant_id, user_id, name)`、NAS 路径是 `{tenant}/{user}` —— **零 agent 维度**,现在做 `scope` 参数只能做成两种取值行为完全相同的空壳,正是本仓库反复出问题的「死字段 / 说谎的参数」形态。**B-52 已交付(09-11),前置全清,B-50 已开工**:**PR1 ✅ 合(09-11,#1507)**—— `agent_key` 打通到 `ToolContext`,零行为变更,**入口数勘误:不是四个,是九个调用点 / 七个构造点**;**PR2 ✅ 合(09-12,#1509)**—— 产物按 agent 分,唯一键加 `agent_key`,带迁移 `0154`;控制台单条寻址改 `artifact_id`(用户拍板方案 A)。**`0154` 还没在任何真环境跑过 —— 发测试环境是 PR3 的前置**,验的是回填结果(空 `agent_key` 占比可解释、抽查对得上 `thread_meta.agent_name`),不是「迁移没报错」。**PR3 ✅ 合(09-12,#1512)** —— 工具层按 agent 分层,五个工作区工具换作用域 + 迁移期读回落;**勘误:计划登记四个调用点,实测七个**(同一 program 第三次漏点名,已配 AST 登记表)。**#1512 的 `Contract suite against the real E2B test cluster` 红了,但它什么都没验到** —— 全部用例死在 `acquire()` 的 504,零断言失败;六项必需全绿,按必需合,**发版金丝雀当真栈闸门**(它真跑 run + 写文件 + 存产物 + 下载校验,PR3 的头号风险正是这条链)。该失效常态已入册 B-55。**PR3b ✅(#1513)/ PR4 ✅(#1522)/ PR5 ✅(#1523)全合**,另修 PR3b 遗留的 cwd 契约测试 bug(#1524)。**下一站 = 发测试环境(PR4+PR5 一起)→ 立刻跑存量搬迁 → 按 `workspace-agent-scoping-migration.md` 真栈验收 → PR6(Task 14 摘掉迁移期读回落)** **09-19 发测试环境 `88a2b6c3` → `f5b78124`**(带 anyio 升版 + B-81 + B-80 的两个修 + 0157)。smoke 17 项全绿;**金丝雀连挂两次后连过三次** —— 挂的那两次不是这批代码的问题,是部署窗口里 pod 被 liveness 误杀、run 被孤儿扫描重收而重收丢输入(见 B-58,严重度已上调)。B-81 的真实路径复验 PASS(见 B-81 行)。**注意:测试环境现在钉的是 `f5b78124`,不含 B-82 的 #1606 `bd69d496`** —— 刻意不紧接着再发一次,避开又一个部署窗口;B-82 ③ 与后续 B-84 一起发。 **09-19 第二次发测试环境 `f5b78124` → `233791e5`**(带 B-82 ③ 预装清单 + B-84 技能摘要瘦身)。`SMOKE PASS` 一遍过,两个 control-plane pod `restarts=0`。金丝雀首跑 FAIL —— **不是这批代码**:沙箱池被占光后走冷启动,`POST /kruise/api/sandboxes` 504(B-55),run 卡住并攥着该 tenant/user 的创建锁,我重试的那次于是撞上「already being created」并被误判为不可重试(新立 B-85)。等池子恢复后**金丝雀两次连过**(295.0s 冷 / 16.0s 暖),产物链全验通。**测试环境现钉 `233791e5`。**  **⛔ 09-19 用户两次加装拍板:B-55 / B-58 / B-85 全部带上。**此前执行单写着「刻意不带 B-58(#1611)与 B-85(#1618)」—— 那句话的前提是「它们在钉子之后才合入、没发过测试环境」,是**排期**问题不是技术障碍;用户先后拍「58 和 55 带上」「85 也带上」,整条作废。**B-55 当天挖出真根因并修完三条**(#1623 镜像瘦身 / #1624 VPC endpoint / #1625 ALB 80 端口超时),详见 B-55 行 —— 一句话:它不是「CI 那条检查失效了」,是**每五次冷建沙箱就有一次直接失败**,而 ALB 的 80 端口监听从开荒起就没设过 timeout。**本班因此还欠一次重钉**(应用镜像 `233791e5` + 沙箱镜像 `621249f6` 两个钉子都欠):`233791e5..main` 已有 10 个提交,按钉子纪律要先发一次测试环境验过再重钉,执行单里已把这 10 个提交逐条列出并标注进本班的理由。**B-85 ①② 已修已合**(#1618)。**③ ⛔ 09-20 用户拍板:也带上(#1636)** —— 此前本行写的「③ 不在本班」作废,那句话的前提是「代码还不存在」,09-20 已交付。③ 的形态与 ①② 不同,**发布前必须知道的三件**:① 带**一条迁移** `0158_run_completion`(expand-only,`agent_run` 加 `completed` / `exit_reason` 两列,可空不回填)—— 本班的迁移因此从两条变成**三条**;② 是**对外契约变更(追加字段)**:`end` 帧多 `completed` / `exit_reason`,`status` 的取值与语义一个字没动,对接方不读也不会坏,但**判成功的口径要改**(`status === "success" && completed !== false`),通知稿已备好、**等真栈验过再发**;③ 回滚纪律:**只回镜像,不要 downgrade `0158`** —— 两列可空、旧代码不读不写,多两列对旧版本无害;downgrade 会把新版本写进去的值全抹掉,而回滚窗口里随时可能再滚回来(与 `0156` 同一条理由)。 **⛔ 09-20 用户拍板:整车延期,等 B-84 本波做完一起发,本波一条都不拆。**我摆出了代价 —— 车上有两条安全修复(#1581 审批门:线程有待审批 run 时新一轮的需审批工具不经审批直接派发;B-76 批准只放行审批单上那一个)与 B-55 的生产故障修复(每五次冷建沙箱失败一次 + ALB 80 端口无 timeout),延车等于把它们一起押后。用户的理由是「目前生产还没大面积开放」,拍板延。**所以 09-24 这个日期作废,新发布日 = B-84 本波六个 PR 全部合入并通过测试环境验收之后**,届时重钉第七次。**本波装载(B-84,详见 B-84 行)**:PR-1 工具反馈质量(**✅ 已合 #1639**,第 1+2 条)/ PR-2 工作区常驻上下文(主线,第 4+5 条;spec **#1641** + 计划 **#1642** 已合,拆成 **PR-2b** 只读文件操作走宿主 NAS + `search_files`〔计划 Task 1-5,进行中〕与 **PR-2** 系统提示词树形摘要〔Task 6-7,等 PR-2b〕)/ PR-3 全回合按路径记账(**✅ 已合 #1645**,第 3 条,**改的是 B-85 ③ 的判据,验收必须把 B-85 ③ 的双向真栈重跑一遍**;记账键最终定为「资源空间 + 路径」而非工具名,并连带把 `mutation_classifier` 从只认 `save_artifact` 扩到 `write_file` / `edit_file` —— 缺口在取不到 path 那一层,光改键治不了)/ PR-4 技能使用计数(第 6 条,进行中;**口径推翻重做**:ai-health-plan 绑的 19 个技能实测 **19/19 都是平台技能**〔`skill.tenant_id IS NULL`〕,原方案把信号写在共享 `skill` 行上覆盖率为 0,改写进 `skill_run_usage` 的 `viewed` 证据行〔按消费方租户 + agent_name〕)/ PR-5 技能块预算降级 + 编辑格式引导(第 7+9 条,等 PR-2)/ PR-6 技能上架校验(第 8 条,等 PR-4)。**并行方式**:用户 09-20 拍板派子 Agent 并行做,冲突面只有 `builder.py`(PR-1↔PR-3)与 `agent_factory.py`(PR-2↔PR-5)两处,各自串起来。 |
| 09-29 ~ 09-30 | ~~**班车 3(09-29)**~~ **❌ 2026-09-25 用户拍板取消** —— 装载(班车 2 旧钉子 `139057c8` 之后的合入)已全部并入 09-28 班车 2(`f92c6fae`);`f92c6fae` 之后 main 只有文档提交。09-29~09-30 留作 09-28 发布后的急修窗口(有事才发,走同一套执行单)。原记录:班车 2 钉子之后合的(RLS 两处补丁已改随班车 2,09-17 拍板);国庆前最后一班 | 10-01 ~ 10-07 不发生产、不合大改 |
| 10-09 起 | 每条迁移单独一班 | **RLS 真 enforce(PR B,Task 5/6)= 节后第一班,合完只上测试泡一周** → 波 4 依赖迁移串行(都动 uv.lock):~~X-12 fastapi 路由自审~~ **✅ 09-10 提前做掉(#1483),这条链短一截** → X-11 mcp SDK 2.x + httpx2 → X-13 e2b;每条合前真栈测 MCP 工具 / 沙箱工具,做完删对应 dependabot ignore |
| 之后 | 按需 | 波 5 收尾池:X-1 沙箱波 4 / X-10 方案文档 / X-7 观测三小项 / X-9 perf 池 / X-14 P3-P4 / D-7 / X-5 / X-6 ③ / B-14 / B-9~B-11 / 小 B 打包 |

- **B-40 乱码验收**不占排期:对方发版当天查库贴结果。
- **拍板(2026-10-16,用户)B-187**(追踪里补实际请求参数):先登记,**Harness-Bench 全量跑完后再修**。
- **拍板(2026-09-09,用户)**:① 留存:产物 90 天 / 上传 90 天 / 已删工作区库行 90 天销账;`threads/<id>/` 只跟对话生死;**工作区根目录其它文件(`style/` 风格锚、`MEMORY.md` 等、未登记文件)永不触碰,写成不变式+测试**(实测 ai-health-plan 的 `style/PLAN_STYLE.md` 在根目录、不在产物表,清扫器只归档软删工作区)。② P-3/P-4 先走流程(runbook §1.9,#1446),admin scope 代码收窄放波 5。③ P-5 = operator+(#1447)。④ **P-1 重新生成/编辑重发要做**,逐条拍板(09-09):**B 方案**(同 thread 原地标「已被取代」,不分叉不导出)/ 被取代轮的副作用(工作区文件、产物登记、长期记忆)**不撤销**,文档写明 / **计划(plan 通道)随之回退**,原则=那一轮没发生过 / 会话有 run 在跑 → **409 拒绝** / 被取代轮停在等审批 → **409 拒绝** / 同一轮旧版本**最多留 5 份**,超出真删最老 / 两轮都计费不退 / 对外文档(chat·sse-events·query·errors·examples)随 PR 同步。4 PR(内核+读面 → 执行侧整轮过滤+per-thread 并发闸 → `:regenerate`/`:edit` 端点 → 文档+控制台折叠态);**开工前 spike**:消息 id 跨 Postgres 往返是否稳定(存亡判据)、`aupdate_state` 后再起 run 的起点行为、plan 回退是否一行能做。⑤ **P-2 点赞/点踩要做,定位=改进 Agent、必须进评测回路**,逐条拍板(09-09):**按轮(run_id)打分**,可附不承诺可对应的 `item_id` 段落标签 / **👎 一条当场进策展待审池(不等 5 分钟 worker),👍 不进池** / **评论原文全员可见**(与会话原文 operator+ 门槛不一致,有意为之)/ **可改票**,后一次覆盖,已进池的记录保留并显示改票 / **只做到评测集为止**(3 PR:存储+对外端点(feedback 加 run_id 归因地基)→ 当场进池+升级 signal+修 promote source 词表漂移 → 控制台可见);对外文档随 PR 同步。**新入册 B-46:按评测集跑评测出分数**(见小 backlog)。⑥ X-3 触发器通知:调研结论主流一边倒 webhook(OpenAI background/LangGraph cron/Anthropic managed agents 均 webhook,附状态查询兜底),**只做 Spec2a webhook,2b 长连接不做**,排期待定。⑦ B-30 kimi-k2.7-code 延后。⑧ X-4 ① 成员清除**并进波 3 做**。
- **✅ 波 3 提前开工即收官(09-09)**:R 留存链 #1456(X-15 ①+B-45+CronJob test 先 → B-27/B-28/上传 90 天/X-4 ②)/ M 成员清除 #1455 / S1 B-9·B-10·B-11·B-14 #1457 / S2 X-5 #1452 / S3 X-7 ②③④ #1449 / S4 B-24+B-44 ② #1453。CI 红一条是真 bug(`list_versions_expired` 用 uuid4 `id` 做 tiebreak,同一产物两版本顺序随机,本地 3/5 红)→ SQL 与内存 store 同改 `created_at, artifact_id, version`。
- **P-1 / P-2 开工(09-09~09-10)**:两份 spec(#1450 / #1454)与两份实施计划(#1460 / #1461)已合;**波 0 = 各自 Task 0 真栈核实**(#1462 / #1463)出了三条改设计的结论:① P-1 取轮边界原设计 `aget_state_history(filter=)` 在最长会话上 **810–950 ms / 52 MB**,换成「两列窄 SQL + 单条 `aget_state`」后 **40–97 ms**,`base_checkpoint_id` 锚点列实测**不必要**;② P-1 压缩摘要**不落检查点、无 run 戳**,spec §6 顾虑不成立;③ P-2 控制台 promote **恒 422**(前端发 `promoted_candidate`、后端 Literal 只认 `golden|trajectory|regression`),评测回路最后一步今天是断的 —— 生产/测试 `eval_dataset` 双双 0 行、`feedback` 表建表至今 `n_tup_ins=0`;另查明反馈按钮**只在调试台渲染**(`ConversationDetail.tsx` 传 `readOnly`),对话详情页点不到,零行由此解释。**波 1 = 两条线各自 PR1 全合(09-10,main `258f2202`)**:#1465 P-2 PR1(迁移 0152:`feedback` 四列 + 部分唯一索引、`curation_candidate` 四列;`FeedbackStore.upsert`/`list_for_thread_scoped`/`down_rated_thread_ids` 双实现;对外 `POST /v1/agents/{code}/runs/{run_id}/feedback`;`/items`+`/messages` 回显**本人**反馈;控制台 POST 改必带 `run_id`;对外文档五页)、#1464 P-1 PR1(迁移 0153:`agent_run` 两列 + `thread_message.superseded_by`;`supersede_run` 内核 + 7 条真 Postgres 集成测;六处读面字段)。**用户新拍板(09-09)**:对话详情页放开打分,**operator+ 能打、viewer 只能看**,重跑仍挡;四个下游(候选池/记忆待复核/技能蒸馏/回滚闸)**一律不按来源过滤**(回滚闸取的是「该技能版本时间窗内被用过的会话」,员工说砸了与用户说砸了同样有效);列并进 0152,做成 P-2 **PR4**。**波 2 = 两条线各自 PR2 全合(09-10,main `1e882f01`)**:#1468 P-2 PR2(候选行接 `feedback_run_id`/`feedback_comment`/`feedback_changed_at`;`upgrade_to_negative` + `mark_feedback_changed` 双实现;`TrajectoryReader.find_by_thread` 按 `finished_at` 取最新;新模块 `feedback_candidates.py`,对外与控制台两条写路径 upsert 后同步进池;worker 预检去重从「有候选就跳过」改「有候选就可升级」;修三处词表漂移 + promote 弹窗补 `expected`)、#1469 P-1 PR2(`agent_node` 在 pruner/working_window/compressor 三道闸**之前**整轮剔除被取代消息;`spawn_run(supersede=)` 用 `supersede_thread_lock` 把「取代 → 建行」整段包住;`:regenerate` 旧轮 `[System, Human]` 原件换新 id 重放,queue 与 stream 两分支走同一个 `replay_graph_input`)。**变异自证逮到三处假绿**:① P-2「别人打的 👎 被错标成本人后改 👍」的不变式在 store 层与端点层**两个**断点都测不出(计划只预判了前者且解释成「这行无所谓」),补断言后都红;② P-2 的 `except` 分支原来什么都不写,审计里「进池失败」与「真 no-op」长得一模一样,现记 `candidate="failed"` 并补两条写路径各一条永久回归用例;③ **计划 Task 6 Step 5 我判「过滤位置测不了」是错的** —— working_window 按 `HumanMessage` 数轮,过滤放在后面时被取代轮会先占一格窗口预算、把仍有效的第一轮挤出去,新增 `test_filter_runs_before_the_working_window` 只红这一条。另两个执行入口已查实不受影响(`orphan_sweep` 传 `graph_input=None` 纯 checkpoint 续跑;`trigger_firing` 自拼输入不读 `enqueued_input`,且无触发器路径传 `supersede=`)。**波 3 = 两条线各自 PR3 全合(09-10,main `807fc0d6`)**:#1473 P-2 PR3(控制台 `GET /v1/sessions/{thread_id}/feedback`;`GET /v1/conversations?has_down_rated=true` 并进 `narrowed_ids` 交集;对话列表「被点踩」勾选框;详情页轮脚新 `FeedbackSummary.tsx`,`feedback`/`feedbackOf` 沿 TurnFooter→TurnBlock→Transcript 串起来、调试台零变化;候选行显示用户原话 + 「后改为 👍」Tag)、#1472 P-1 PR3(对外 `:regenerate` / `:edit` 两端点,共享 `_supersede_and_run`;`agents.py` 抽出 `resolve_external_files` / `external_run_bounds_error`;Idempotency-Key、两张路由表登记、NUL 逐端点 7 条、`errors.md` 六个错误码)。**用户拍板(09-10)两条**:① **第六个错误码 `RUN_BOUNDARY_UNRESOLVED`(422)要补**,销掉 PR1 留在 `supersede.py` 的 `TODO(Task 8)` —— 原来「轮边界划不出来」借用 `RUN_NOT_LAST`,而 `errors.md` 教对接方「去 run 列表取最新 run_id 再试」,但这种情况下目标**本来就是**最后一轮,照做会拿回同一个 id、撞同一个 422,永远绕不出来。**全仓扫描确认生产代码里只有 `supersede.py` 抛 `RUN_NOT_LAST`、恰好两处**,两支边界拒绝都改成新码,spec §4 补一句「`RUN_NOT_LAST` 从此只用于 `rows[-1].run_id != target_run_id`」把边界钉死;② **控制台反馈 GET 的归属轴保持放开**(不挂 `caller_owns_thread`)—— 对外 API 建的会话 `user_id` 必填、写的是对接方自己的终端用户标识,**控制台上没有任何员工是这些会话的 owner**,挂上归属门 = 只有租户 admin 能看到评论,而 👎 全部来自对外终端用户,等于把功能关掉、也架空 09-09「评论原文全员可见」那条拍板;与 `/v1/conversations`(2026-08-14 裁决:对话是运营对象,读面对全员开)同族。代价已知并接受:租户内任何持 `session:read` 的员工可读任意终端用户的反馈评论原文;实现与测试都留了注释防后来人当漏闸「修正」。**波 3 逮到的假绿**:P-2 T12 那条跨租户用例被**第一道门(404 归属)先挡住**,第二道门(读行的租户谓词)一次都没被测到 —— 把 `list_for_thread_scoped` 换回无谓词版本 15 条全绿;补 `test_get_feedback_read_carries_its_own_tenant_predicate`(thread 在本租户、同 id 上挂别租户的反馈行)才咬得住,T13 同形状的洞一并补。**勘误**:姊妹端点 `GET /v1/sessions/{tid}` 与 `.../messages` 挂的是 `caller_owns_thread`(admin 或 owner),不是计划/spec 写的「operator+」,实际比 operator+ 更紧。**波 4 = 两条线各自 PR4 全合(09-10,main `44db27eb`),P-1 / P-2 两条线全部收官**:#1477 P-2 PR4(`allowRate` 沿 `Transcript → TurnBlock → TurnFooter` 传下去、默认 `false` fail-closed;`POST /v1/sessions/{thread_id}/feedback` 加 `require("session","write")` = operator+;`feedback_source` 从协议到前端 Tag 打通,四个下游消费者只加注释零逻辑改动)、#1476 P-1 PR4(`chat.md` §2.10「重新生成与编辑重发」+ 其余六页增量 + 侧栏;新 `SupersededFold.tsx` 折叠壳、墓碑「内容已清理」、只读无重跑入口;runbook §2 一句)。**波 4 逮到的**:① **加闸前 viewer POST 反馈实测返回 201 而非 403** —— `require_key_scope` 的 docstring 自陈不管人类 JWT、`console_only()` 又挡掉所有 API key,两者叠加使那句 scope 检查在控制台路由上恒为空转(引出 B-49);② 计划写的 `input` 上限 32000 与实现的 `MAX_RUN_INPUT_CHARS = 65536` 不符,按实现定稿;③ **spec §3 关于 `feedback_source=NULL` 的理由不成立** —— 它说 worker 建的候选「归因不到某一条 feedback」,而 `curation_worker._evaluate` 已经从 `newest_down` 取到了 `feedback_run_id`/`feedback_comment`;拍板补齐(单独 PR,同时改 spec 那句错理由)。**删掉两条重言测试**:P-2「翻转 `readOnly` 则重跑按钮出现」(重跑开关是 `onRetry` 传没传,该页从不传 handler)、P-1「折叠态里没有重跑入口」(断言一个从未存在过的 testid)。两条都改成写明「靠评审而非测试保证」。**rebase 冲突一处**(`Transcript.tsx` history 循环,P-2 加 `allowRate`、P-1 换成折叠壳),两边都保留;并逐行对过 `origin/main` 确认 P-2 那 46 行测试一条没丢 —— 前几轮踩过「机械两边都保留把两个测试函数熔成一条」且 `git status` 干净。**顺带补齐**:`examples.md` 侧栏原停在 10.8(P-2 加 §10.9 时漏登记),10.9 + 10.10 一并补上,锚点做了产物级验证与反证(故意改错锚 → 10 条死链 → 改回 → 零死链)。两份计划在 `docs/superpowers/plans/2026-09-09-*.md`,任务编号与验收判据齐全。**Task 12 真栈验收 20/20 PASS(09-10,测试环境 `e1601bd7`,零 FAIL 零不可判)** —— 探针 user `pc:proj_8f52…:emp:probe-delegation` + 金丝雀 agent `release-canary`,会话 `165e1b23-b9bb-4e7b-92f4-63681f851858`。验到:① **暗号法证明被取代的轮真的没进上下文** —— 第二轮回 BANANA、`:edit` 改成 CHERRY、第三轮问「我让你回过哪些水果词」答 **CHERRY 且无 BANANA**(此前只在单测与真 graph 探针上验过,这是真栈第一次);② 读面双向指向(旧轮 `superseded_by` 指新轮、新轮 `regenerated_from` 指回、第一轮未被牵连);③ 被取代轮 2 条消息带标记且原文仍读得到(不删只标);④ 三个错误码实测 —— 已取代轮 409 `RUN_ALREADY_SUPERSEDED`、非最后一轮 422 `RUN_NOT_LAST`、`:regenerate` 带 `input` 422 `INVALID_REQUEST`(文档说不忽略,确实不忽略);⑤ 同 Idempotency-Key 两次拿同一个 run_id、`runs[]` 只多 1 条;⑥ 打分落库 + 条目接口回显本人 + 可改票 + 改票后回显 `up`;⑦ 控制台折叠态由用户在真环境肉眼验收(「已被取代 → 新一轮 …」summary 行渲染正确,旧轮默认收起)。**过程记录**:首跑空转 19 分钟,根因是探针把 run 状态打到 `GET /v1/agents/{code}/runs/{run_id}` —— **对外面没有这个端点**(只有 run 列表),裸 404 被 case 分支吃掉,每轮轮询满 360s;产品零缺陷,改从 run 列表读状态后一次通过。**至此 P-1 / P-2 两条 program 全部关账。**
- **RLS 拍板(2026-09-08,用户)**:真 enforce(PR B = 计划 Task 5/6)**顺延到下下波**(节后 10-09 起第一班)。原因:① Task 3 的 Detect 信号**从未可归因**——JSON 格式器(`expert_work/common/observability/log.py`)把 `stack_info` 列在跳过属性里,24h 真栈 prod 5711 / test 4347 条告警全是同一句话,93% 无 trace 无 run,Gate 1 观察窗无法做;② 计划/spec 08-21 已清出工作树(git 历史 `6e855aea` 可捞)。波 2 D 线 = 修可归因 + 本地全量枚举第一轮判定;发测试观察一周拿真栈 caller 聚合做第二轮;归零后才 PR B。**✅ D 线 #1443 已合(09-09)**:归因失效其实**两层**(格式器丢 stack_info + SQLAlchemy 同步 ORM 跑在 greenlet 里,listener 内无应用帧,要顺父 greenlet `gr_frame` 链走),现在日志带 `rls_caller`/`rls_caller_outer`,每 caller 60s 限 10 条首条必打、512 caller 上限、探测器自身异常兜底绝不砸事务;本地全量枚举 2092 条 / 47 函数 → 生产信号归零,顺带修两条真 bug:`tenant_status.is_suspended` 在 AuthMiddleware(RLS 中间件外层)无租户上下文读 `tenant_config`(enforce 后暂停租户拦不住)、memory DLQ worker 认领扫描跨租户。**待判定(真栈第二轮)——✅ 2026-09-11 已跑,但结论是「观察给不出判决」**:三个长周期 worker 的 24h `rls_caller` 聚合如下。**生产 24h 零信号,而这不是修好了的证据** —— 三个里有两个在生产**根本没运行过**(这正是「在不可能失败的条件下验证等于没验证」)。① **`skill_curator` 两个环境都被显式关掉**(`EXPERT_WORK_ENABLE_SKILL_EVOLUTION_WORKER: "false"` 写在 base configmap,test/prod 同值),两边都零信号,**观察不出任何东西,只能靠读代码判或书面接受**。② **`workspace_janitor`**:生产 24h 内连一行日志都没有(含 `rls_caller` 与它自己的 INFO);测试环境 **1536 条**,命中 5 个调用点(`_loop:192` / `run_once:212` / `_sweep_archives:266` / `_archive_one:279` / `_sweep_sizes:318`,其中 `_sweep_sizes` 独占 1280)。生产 configmap 的 `OBJECT_STORE_BACKEND=s3-compatible` 与 `WORKSPACE_NAS_ROOT=/mnt/workspaces` 都满足 `app.py:1603-1616` 的启动条件,剩下的变量只有 `resolved_workspace_quota is not None`;`elif` 分支的 `workspace_janitor.not_started_memory_object_store` 日志也没打过,**为什么不上岗待查**。**09-17 复查:已上岗** —— `5775fbf3` 后生产两副本都有 `workspace_janitor` 调用点的信号(`_loop:185` → `run_once:205`、`_sweep_sizes:311` → `workspace_quota.refresh`,单 pod 约一天 ~590 条),B-61 的 inputs 回收在生产有执行者。③ **`memory_consolidator`**:生产有 30 行日志但零 `rls_caller`;测试环境 **~640 条**,命中 9 个调用点(`_loop:606` / `run_once:629,642` / `_run_sweep:653,662,666,684` / `_sweep_user:728,761,785` / `_find_candidate_clusters:845,861` / `_list_tenants:1139` / `_resolve_thresholds:1146`)。**判决**:janitor 与 consolidator 在测试环境**实证确认无租户上下文**,enforce 前必须接;`skill_curator` 无法观察。**enforce 前置因此不是「三选一」而是三条各自处理。****09-17 拍板**:janitor / consolidator 两处补丁随**班车 2**;`skill_curator` 读代码判。**09-17 判定与补丁(#1573)**:`skill_curator` **不是关着的** —— 它由 `enable_scheduler` 门控(两环境 true),不是 `ENABLE_SKILL_EVOLUTION_WORKER`(09-11 这条记错了);零信号是因为 `_loop` 先睡 24h 才扫第一轮而 pod 每次发版都重启(→ B-68)。代码里零 RLS 上下文、形态同 consolidator,`skill` 表策略没有 `NULLIF`(enforce 下不带租户直接报错)→ 一并补进 #1573。**enforce 前还欠(测试环境 09-17 24h 聚合,不在 #1573)**:`sandbox_egress_metrics.refresh_once` ~1900、`agent_sandbox.reap` ← `sandbox_reap_worker` ~470、`platform_quality_config.effective` ← quality monitor / drift worker ~380、鉴权中间件 `resolve_system_admin` / `resolve_tenant_roles` / `api_key_verifier.verify` ~480、`audit.emit` 与审计 redactor 读 `tenant_config`。**Task 6 预警**:persistence 下 15 个测试模块依赖默认 `bypass=False`,整包 autouse bypass 夹具会让它们静默走 bypass,enforce 时按模块显式设。
- **✅ 新入册即销案(2026-09-08,#1428)**:MCP 探针把真因吞成 `ExceptionGroup` —— `mcp_probe.py` 只把异常类名放进消息,`mcp_probe.failed` 日志不带栈;真栈复现:生产建 `deep-ai-health-mcp` 六次 422 无一处能看出是 401 还是 DNS(从 pod 手探才定位到对方生产服务在、无 token 回 401)。修法=递归拆 ExceptionGroup 取叶子异常进消息(截断、脱敏),日志带 `exc_info`。

- **✅ B-50 PR1 已合(2026-09-11,#1507)—— agent_key 打通到工具层,零行为变更**:`ToolContext` 加 `agent_key` 字段、`_build_tool_context` 从 `configurable` 抬进来(不可信输入,非字符串回落空串);七个 `configurable` 构造点填 `sanitize_agent_key(spec.metadata.name)`。**还没有任何消费点**,工具层改动在 PR3。
  **勘误 —— 计划登记的「四个执行入口」是错的,实测九个调用点 / 七个构造点,而且四个里三个的函数名也错**(`_run_one`/`trigger_run`/`_revive` 实际叫 `_execute`/`spawn_run`/`_respawn`)。漏掉的三处:
  ① **`api/runs.py::resolve_approval_decision`(审批续跑)** —— 漏了它,审批通过之后的**后半程**写去用户根、前半程写在 agent 子树里,同一轮活分两处;
  ② **`skill_evolution_wiring.py::_make_replay_config_factory`(技能演化 replay)** —— 它跑的是完整 agent 图(含文件工具)且带**真实** `tenant_id`/`user_id`,读写真实用户工作区;漏了它,replay 看不见被 replay 的那个 agent 自己的文件,held-out 判定失真;
  ③ **`_child_run.py::_child_config`(委派子代)** —— 透传**父的** `agent_key`(不是子代自己的):worker/静态子 Agent 干的是父 agent 的活,产物必须落在父的子树里,否则父读不到自己 worker 刚写的文件。与技能种子路径同一个取值口径(用父 key)。
  两处确认**不需要**:`eval_engine_live::_run` 建的是内联 spec、连 `tenant_id` 都不给,没有用户工作区可言;`sse.py::run_agent` 是 merge 点不是构造点,`**` 展开时 `agent_key` 原样穿过,而且 `RunRecord` 里没有 agent 身份。
  **测试形状**:`test_agent_key_plumbing.py` 三条 AST 不变式 —— 清单穷举(全仓「起 agent 图」的调用点集合必须与登记表一致,新增第十条漏登记会红)/ 每个构造点都真写了 / **现算的必须调 `sanitize_agent_key`、透传的必须不调**(agent name 无字符集约束,未净化的值直接进路径是真汇点;Global Constraint 又要求只许有一种算法)。变异自证:七处逐个删 → 每次点名被删的那一处;加假入口 → 清单红;写未净化原始名 → sanitize 红;子代改成自己算 key → sanitize 红。
  **一条教训**:CI 红 9 条,根因是 `resolve_approval_decision` 读 `spec_record.spec.metadata.name`,两处测试桩返回 `SimpleNamespace(spec=SimpleNamespace())` —— 比真实类型少一层(`AgentSpec.metadata` 是必填字段)。我本地只跑了三条入口的测试、没跑审批那条,**与 #1501 同一形态:改了函数没 grep 全部触到它的测试文件**。修完按「grep 全部相关测试文件再跑」重验:control-plane 25 个文件 380 passed、orchestrator 13 个文件 352 passed。

- **✅ B-50 PR2 已合(2026-09-12,#1509)—— 产物按 agent 分,同名不再合并(带迁移 `0154`)**:`artifact` 唯一键 `(tenant_id,user_id,name)` → `(tenant_id,user_id,agent_key,name)`,`name` 不再是身份;`sanitize_agent_key` 从 orchestrator 下沉到 `expert-work-protocol`(持久化只依赖 protocol,而迁移要用它),orchestrator 侧改成再导出并钉 `reexported is sanitize_agent_key`——Global Constraint 要求全仓只许有一种算法,两份实现会静默分叉;`ArtifactStore` 七个方法重排签名:按 `(agent_key, name)` 定位的四个把 `agent_key` 设成**必填无默认**(有默认 = 漏传时静默跨 agent),按 `artifact_id` 定位的三个不要 agent 维度;控制台五条路由改成 `artifact_id` 寻址,列表载荷带 `id` + `agent_key`。
  **拍板(用户 09-11,方案 A)—— 控制台单条寻址一律用 `artifact_id`**:跨 agent 的浏览面(产物列表、用户详情)拿 `id` 走,行里带 `agent_key` 让运营分得清同一用户下两个同名产物;agent 上下文里的面(对话详情、调试台)走会话级端点,按「会话所属 agent + name」解析,一个会话只属于一个 agent,天然无歧义。
  **顺带修出两个真 bug** —— 不是这次引入的,是唯一键放宽之后立刻会炸的潜伏:`get_latest_version` 用 `scalar_one_or_none()`,同名多 agent 时**抛异常**而不是返回一条,改 `ORDER BY updated_at DESC LIMIT 1`;`soft_delete` 是无 LIMIT 的批量 `UPDATE`,会把两个 agent 的同名行**一起删**,改成 `WHERE id IN (子查询 LIMIT 1)`。
  **`path_in_workspace` 有意不动 —— 计划 Task 5 那条是错的**:这一列是下载时真去读的物理路径,而文件是 `write_file` 早先落的盘,它的根目录要到 PR3 才改;PR2 就加前缀 = 每一次下载 404。已钉 `test_path_in_workspace_keeps_the_agents_own_relative_path`。
  **`agent_key=None` 的逃生舱做成了可审计的**:两张 AST 登记表——`_ALLOWED_UNFILTERED`(字面量 `None`)与 `_ALLOWED_COMPUTED`(调用结果可能是 None),每条都要写理由,新增一个没登记的调用点直接红;对外三个端点那几条标了「PR4 落地后删」。
  **三条 CI 教训**:① `Test (integration)` 红在 `could not create unique index "artifact_identity_uniq"` —— 我的契约测试在**共享的 session 级库**里造了同名不同 agent 的行,另外两个测试的 downgrade 重建三元组索引时撞上。**本地单文件跑全绿,只有全仓 integration 能暴露**;修法是给契约测试自己的库(先例 `test_backfill_approval_user_id.py`),不是去弱化 downgrade。② `Test (pytest)` 红在 `tools/eval/` 的 `soft_delete() missing 1 required keyword-only argument` —— 早先的文本替换漏了两处,因为 `ruff format` 把它们折了行、替换静默没匹配上(**「变异没落地看起来像测试不咬」同一形态**);改走 AST 扫描,并且第一次跑了 `pytest tools/`(`testpaths` 里本来就有 `tools/*`)。③ 六项必需 check 全绿仍 BLOCKED —— CodeQL 线程未解决:`test_sessions_api.py` 留了个改完之后无人引用的模块级 `_AGENT_KEY`,删掉(不是 `noqa`),并全仓核了其余 13 个同名定义都还在用。
  **`Contract suite against the real E2B test cluster` 取消 + 14 次 `504 Gateway Time-out` 是基础设施,不是这个 PR**:本 PR 不碰沙箱代码,且该 check 不在六项必需内;测试集群实查健康——`sandbox-gateway`/`sandbox-manager` 都 Running,新起的 sandbox pod 事件里 `Successfully pulled image sandbox:63a3109f in 1m49.432s`(635MB),是拉镜像叠 ALB 超时,不是链路断了。
  **⚠️ 勘误(09-12,PR3 开工前必读)—— spec §5.2 与 §4 的 `artifacts/` 段自相矛盾,定论是删掉那一段**:§4 布局画了 `agents/<agent_key>/artifacts/`,§5.2 要求 `save_version`「落 `agents/<agent_key>/artifacts/`」,计划 Task 5 据此断言 `path_in_workspace == "agents/<key>/artifacts/<path>"`。**但 `save_artifact` 从来不搬字节** —— 它只往 `artifact_version` 写一行,`path` 缺省等于 `name`,文件是 `write_file` 早先落的盘;而 PR3 的 `resolve_scope`(计划 Task 6)把 `write_file("报告.docx")` 解析到 `/workspace/agents/<key>/报告.docx`,**没有 `artifacts/` 这一段**。两边对不上 = 登记的路径指向一个不存在的文件,**每次下载 404**;而且这个目录今天没有写入者、PR3 之后也不会有,画出来永远是空的。**定论:`path_in_workspace` 取 `agents/<agent_key>/<path>`,`artifacts/` 段从 §4 布局与 §5.2 一并删。** 理由:① 分类语义已经由 `artifact.kind` 列承担,目录名是第二套说法;② 让 `save_artifact` 真去搬字节 = 给一个纯写库的工具加沙箱文件操作,双份存储、搬运成功但登记失败(或反过来)的一致性问题,而且它跑在 control-plane 侧、字节在沙箱里;③ 要求模型自己先 `write_file("artifacts/报告.docx")` 是把不变式交给提示词,它会忘,失败形态是静默 404。PR5 的搬迁脚本(Task 11)本来就要重写 `artifact_version.path_in_workspace`(`MigrationReport.artifact_rows_updated`),按同一个形状产出即可。

- **拍板(2026-09-11,用户)**:① **B-49 —— viewer 定义为「只读旁观者」**:读面全保留,**写操作一律 operator+**。据此收窄 18 条(sessions 族 11 含扫描时不可判的 `runs`/`uploads`、artifacts 2、plan 1、uploads 1、workspace 1、knowledge `bases/{name}/test` 1、skill-evolution `promote-requests` 1);kill-switch 与 triggers 已在 #1489 先行修掉。**三条敏感读暂不动、单独拍**:`GET /v1/tenants/{tid}/config/credentials`、`GET /v1/webhook-endpoints`(+`/{id}`)、`GET /v1/audit`(+`/{id}`)。**勘误**:我原写「该条要动 `rbac.py` 决策矩阵、不是加闸能解决」,实测是**漏挂闸** —— 那条路由压根没有 `require("audit","read")`,VIEWER 的 grant 从没被查过。补闸后 viewer 仍过,所以要收紧最终还是要动矩阵,但**先得把闸补上**,这是两件事。② **对外 `?scope=user` 并集要做**(已告知对接方):三个列表端点支持、默认 `agent`;**下载端点不支持**(放开等于让 A 的 code 取 B 的字节);`scope=user` 的条目带 `agent_code`(不是内部 `agent_key`)标归属;`shared/` 两种 scope 都不对外投影。契约与测试见计划 §9.4。③ **B-50 工作区按 agent 隔离排进正式排期**:B-49 收窄 + B-45 余 2 + `scope=user` 三件完成后开工,按计划的 PR1→PR6 走。
- **2026-09-10 收工(P-1/P-2 关账之后的四个 PR,全部已合)**:**#1483 X-12** fastapi 解钉 `>=0.138` + 五族路由自审改走 `iter_route_contexts`(顺带撤掉 `tools/ci/protected_pins.toml` 里同步登记的两条钉子——不同步改 CI 直接红,那个 guard 的设计就是「合法升级=同 PR 改这里」);**#1482 小批五条** B-47 classid 登记表(挪号方向经纠正:让位的是 janitor 不是投递锁,判据是滚动发布的互斥真空谁扛得住)/ B-1 webhook 创建者 / B-13 死参删除 / B-17 ①② / B-18 对外 404 文档;**#1489 B-49 两个真洞** 租户 kill-switch 仅 admin + triggers 四条 operator+;**#1490 新立项** 用户工作区加 agent 维度(spec + 计划,见 B-50)。**当天三条方法论教训**:① 我对 B-49 的静态扫描**两版都错、方向相反** —— `console_only()` 大多挂在 `APIRouter(...)` 构造器上、角色闸大量在 handler 体内,grep 不作数,判据必须是拿三种身份真发一次请求;② `configurable` 的执行入口是**四个**(队列 worker / stream 端点 / 触发器 / 孤儿复活),「执行入口三个,规矩只写一处就漏两个」那条教训又来一次,这次配了 AST 穷举测试防漏(**⚠️ 09-11 勘误:这个「四个」本身就是错的,实测九个调用点 / 七个构造点,而且四个里三个的函数名也错了 —— 见下方 09-11 B-50 PR1 条目。同一条教训在同一个 program 里连犯两次**);③ **并行 subagent 共用同一个 scratchpad 根目录**,两个任务都写 `pr_body.md`,一条 `python -c ... && gh pr edit` 在断言失败后仍执行,把 A 任务的 PR 正文推到了 B 任务的 PR 上(已还原)——worktree 隔离的是仓库不是 scratchpad,以后 brief 里按任务分子目录 |

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
  「小 backlog」B-2~B-7、B-9~B-12、B-14~B-16、B-17 ③、B-21、B-27 余项、B-28 余项、B-29 ①②、B-30、B-38、B-40、B-43、B-44 ①③、B-46、**B-48 / B-49 余 2(敏感读,要动矩阵)/ B-50 / B-53 / B-54 余项 / B-59(生产发完刷沙箱钉子,关掉告警 #137)/ B-60(写入侧隔离:每次 exec 一个私有 `/workspace`,**发布阻断项**)/ B-61(注入变量按引用给沙箱,别让模型手抄)**、
  「调试台 follow-up」D-7。已销案的:D-1~D-6、B-8、B-19、B-20 主体、B-22、B-25、B-26、B-34、
  **B-35 / B-36 / B-37 / B-39**、**B-31 ② / B-32(08-31 发布前必修批)**、**B-41**、X-2、**X-8 全部(含 09-08 余项)**、**X-15 ②**、X-16、**B-23 / B-33 / B-42 / B-29 ③ / X-14 P5(09-08 六线批)**、**B-31 ① + 波 2 全部(09-09,#1439-#1443)**、
  **X-12 / B-1 / B-13 / B-17 ①② / B-18 / B-47(09-10 四 PR 批)**、**B-49 的两个真洞(09-10,#1489;剩 ~28 条写面等拍板)**、
  **B-56 / B-65 / B-72 / B-73(09-18 四 PR:#1592 / #1594 / #1593 / #1595)**、**B-45 / B-51 + B-49 写面 18 条(09-11,#1493/#1495/#1494/#1496/#1497)**、**B-52 按 run 的用量对外可见(09-11,#1499-#1502,已发测 `11fdcd88` 并与对接方真栈 10/10 对通)**。
  X-3 起与 D 节为 2026-08-20 从各 program 记忆汇集补录,
  **开工前先按行内注明的来源核实现状**(记忆反映的是记录时点;本文件已两次因未核实而误报进度)。

## 🚀 生产发布前置(2026-08-23 立项;**发布日 = 2026-09-10 周四**(2026-08-31 用户拍板)(时间线:原定 08-26 → 08-24 改 08-30 → 08-29 延期 → 08-31 定 09-10。**勘误:本文件此前把 08-30 称作「周六」,该日实为周日**——旧条目里的星期标注不可信,以日期为准);三道拍板 2026-08-24 已收:单副本首发(**2026-08-26 推翻改多副本首发,阻塞项七 PR #1312-#1318 已全清**)/ 书面接受单层隔离 / 告警走企微 —— 记录在 docs/runbooks/production-release.md)

> 11-12:本节条目均已完成,只剩 PROD-11(= 主清单 P-4,流程侧已由 #1446 写进 runbook);「发布后第一波」里 Redis 全局令牌桶 #1440、跨副本取消亚秒化 #1441 已于 09-08 合并,RLS PR B 见主清单行 `RLS`。

> 来源:2026-08-23 生产就绪盘点,对照 `docs/research/2026-07-28-multi-replica-readiness-audit.md` 逐条核实现状。
> **好消息:审计第 0/1 波大头已落地**(实测核过:webhook 投递有 `claim_ready` + `FOR UPDATE SKIP LOCKED`;多副本启动守卫真存在——`app.py:1188` 未配 quota Redis 直接拒启;文档上传已走对象存储;base configmap 已 `SINGLE_INSTANCE=false` + postgres checkpointer + s3;prod overlay 骨架在,占位符待填)。

### A · 必须完成(不做 = 生产事故或安全洞)

| # | 项 | 内容 | 量级 |
|---|---|---|---|
| PROD-1 | **✅ 09-07 已上生产(`2d5742c6`,#1312 #1313 #1316 #1318)** **live SSE 跨副本兜底**(X-10 波 2-1;用户 08-26 拍板多副本首发,推翻 08-24 单副本拍板):非属主副本 `/events` attach 时轮询 durable `run_event` 表尾随(`has_live_stream` 只认 publisher-fed,防早 attach 毒探针;终态后静默两轮再收尾;end 帧从 run 行取 status+artifacts)。#1313 CAS 守卫(`expected_statuses`+`guard_claimed_by`)堵取消 / 重宣两洞,顺带修 #1305 审批续跑产物种子 no-op;#1316 跨副本取消验收 harness `verify_cancel`;#1318 prod 回到 base 2 副本 + smoke 校验副本数。 | ✅ |
| PROD-2 | **✅ 09-07 已上生产(`2d5742c6`,#1257)** alertmanager 告警接收(X-7 ①),企微群机器人方案:集群内 wecom-adapter(ConfigMap 脚本 + 复用 control-plane 镜像)把 Alertmanager JSON 转成企微 markdown,p0 追加 @all;webhook URL 走 Secret `wecom-alert-webhook`,未建则只记日志(降级)。**未完成:** 用户侧建群机器人 + create secret(步骤在 base/secrets.example.yaml);用户 10-08 拍板测试、生产都暂不配,告警目前只进 wecom-adapter 日志。 | ✅ |
| PROD-3 | **✅ 已完成(08-24,用户书面拍板)** **RLS 闸 1 拍板**:接受单层 ORM 隔离首发(记录在 `docs/runbooks/production-release.md` 拍板记录节);硬要求:生产应用账号建成非 superuser、无 bypassrls(`secrets.env.example` 已写明);FORCE RLS(捞回 PR B)排在发布后第一波 | ✅ |
| PROD-4 | **✅ 已完成(08-24,核实定案)** retention-cleanup-job 第二套审批超时:核实 infra/k8s 零 CronJob manifest,该 job(连同 billing-rollup / event-log-archive / audit-backup)只有代码没有部署物,test/prod 都不跑,风险不进生产。**该 job 部署前必须先修**(代码修回归 X-15①,production-release.md §4 已钉)。 | ✅ |
| PROD-5 | **✅ 09-07 已上生产(`2d5742c6`,#1258)** **发布工程收尾**:prod overlay 补成与 test 同构(镜像 mirror、langfuse、searxng CN、NAS PV、configmap 沙箱键 / password 开通 / bootstrap 邮箱;`secrets.env.example` 全量重写,骨架原缺 18 键);release.sh / smoke.sh / rollback.sh 加 prod 路径(域名放 `~/.kube/expert-work-prod-params.env` 不进 git、placeholder 预检、交互确认门);开荒 / 发布 / 回滚 / 已接受风险见 `docs/runbooks/production-release.md` | ✅ |
| PROD-6 | **✅ 09-07 已上生产(`2d5742c6`,#1252/#1253/#1254)** 在途三 PR 合并 + 测试环境验证:08-24 三 PR 全合并、发测试 `1e6295ec`(记录 #1256)。**未完成:** 交互面(对话页 live / 批拒取消 / 审批两 tab)待人工点验。 | ✅ |

### B · 强烈建议(不做需书面接受风险)

| # | 项 | 触发条件 / 理由 |
|---|---|---|
| PROD-7 | **✅ 09-07 已上生产(`2d5742c6`,#1317 #1330)** X-14 发布防线:P2 钉版卫兵(#1317)+ P1 金丝雀(#1330:`canary.py` + `release.sh` 阶段 6 + seed CLI) | e2b 事故复盘产物,三层防线补齐;生产金丝雀 seed 已在 09-07 开荒时完成(glm-5.3 主 / kimi-k3 备,真跑 PASS) |
| PROD-8 | **✅ 09-07 已上生产(`2d5742c6`,沙箱 PR-B #1105)** egress token 24h 到期出网全挂 —— 销案:`agent_sandbox.py` 热会话总年龄封顶 `egress_token_ttl_s // 2`(12h),超龄强制重建,会话必死在 token 之前;407 出网可观测兜底(egress 审计指标)同波落地。X-1 的 W1 遗留清单同步作废该条。 |
| PROD-9 | **✅ 09-07 已上生产(`2d5742c6`,#1314)** X-3 触发器投递 CAS:`deliver_run_result` 的读-查-写整段包进按会话的 `pg_advisory_xact_lock`(classid 8619,key=hashtext(thread_id);无 session_factory 时退回单进程语义),同会话串行、异会话并行;cron claim/retry/finalize 的 CAS 原本就安全,病灶只在投递段。 | ✅ |
| PROD-10 | **✅ 09-07 已上生产(`2d5742c6`,#1338)** B-19 配额维度混扣已修,详见 B-19 | ✅ |
| PROD-11 | **P-4 拍板**(api_keys 凭据横向扩散) | 发 key 给第三方前至少把「只发 write key、永不发 admin」写成硬流程;修 rbac 映射另议 |
| PROD-12 | **✅ 09-07 已上生产(`2d5742c6`,#1315 #1318)** **X-10 波 2-2 供应商 RPM 按副本静态均分**:`effective_rpm = max(1, ceil(rpm / EXPERT_WORK_REPLICA_COUNT))`,env 已进 prod / test 两 overlay(=2),smoke 校验 env == spec.replicas 防分母漂移。**未完成:** Redis 全局令牌桶(真·全局化,弹性扩容不用改 env)仍在 backlog。 | ✅ |

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

## 待产品拍板(不是工程题)(移出条目)

11-12 核实:P-1 已于 09-10 关账 —— #1476(对外文档 2.10「重新生成与编辑重发」+ 控制台被取代轮折叠)、#1480(P-1 Task 12 真栈 20/20);代码里 `:regenerate` 已在(`services/control-plane/src/control_plane/supersede.py:142`、`turn_inputs.py:12`)。

| # | 事项 | 性质 |
|---|---|---|
| P-1 | **重新生成 / 编辑重发** | spec §九 第 2 项。会话是 append-only checkpoint,**没有"回退到某条消息重跑"的概念**。要做是一道真设计题,不是加端点 |

11-12 核实:P-2 已于 09-10 关账 —— #1477(对话详情页放开打分 operator+、候选行显示反馈来源)、#1480;后续「按评测集跑分」单列 B-46。

| # | 事项 | 性质 |
|---|---|---|
| P-2 | 消息级点赞 / 点踩 | 内部已有 feedback 表,对外零暴露。薄,但要先定"反馈给谁看、进不进评测回路" |
| P-3 | **admin scope = 完整租户控制台权限** | P1 遗留。文档已明写"永不发 admin 给第三方",但实际范围比措辞暗示的更宽 |
| P-4 | `api_keys` 是**凭据横向扩散路径** | 一把 admin key 能枚举 / 回显 / 新铸本租户其它 key。与 P-3 同源(`rbac._collect_roles` 把 admin scope 直接映射成 `Role.ADMIN`) |

11-12 核实:#1447(09-09 合并)给技能 promote-request 的 approve / reject 加了 operator+ 角色闸,本条所述「零角色限制」已不成立。

| # | 事项 | 性质 |
|---|---|---|
| P-5 | `approve_promote`/`reject_promote` 对 `tenant` 可见性技能**零角色限制** | 任何 viewer 能批准他人对公开技能的 promote-request。需定治理审批要什么角色 |

## 其它 program 的挂起项(移出条目)

| # | 事项 | 备注 |
|---|---|---|
| ~~X-2~~ | ~~CI pytest-xdist `-n auto`~~ | **✅ 已完成(08-14 合 main,#1159;只改 CI)** CI 测试 29m17s → 11m32s(2.5x)。教训:① 没有 fixture 需要改,真正挡路的是两个模块把 `uuid4()` 插进 parametrize id,各 worker 收集集不同;② 别拿本地数外推(本地 `-n 4` 3.7x,runner 只有 2.5x,每 worker 都要 import 整个 app)。 |
| X-4 | **用户维度运维页 BACKLOG**(用户 2026-07-15 定暂缓) | **✅ 09-16 已上生产(`5775fbf3`,#1455 #1456;生产留存定时任务 #1574 随 10-12 `94dc73ba` 才开始跑)** ① 成员清除:#1052(07-25)已有 `POST /v1/members/{id}:purge` + 成员页按钮 + `MEMBER_PURGE` 审计;#1455 按用户 09-09 拍板改成两步(必须先停用再清除;不能清自己;Keycloak 不可达回 502、本地不动;按钮只对已停用 / 撤销显示),并加「最后一个可达 admin」闸:成员停用 `DELETE /v1/members/{id}` 与删 admin 绑定 `DELETE /v1/role_bindings/{id}` 都回 409 `MEMBER_LAST_ADMIN`(一处判定 `member_ops.has_other_active_admin`;「可达」= active ∧ roster admin ∧ 持无条件租户级 ADMIN 绑定)。② 90 天硬删(#1456):留存作业删软删且已归档(`archived_object_key`)满 90 天的 `user_workspace` 行,先删行再删从属行(从属失败只记日志 + 审计 `dependents_failed`),未归档行只计数不动。**未完成:**改角色路径没挂最后 admin 闸;两 admin 并发互停的乐观窗口未关(要 advisory lock) |
| X-5 | **✅ 09-16 已上生产(`5775fbf3`,#1452)** **MCP allowlist 残留名不可移出**(来源:MCP 界面重设计 2026-07) | 目录条目被删后,租户 allowlist 残留名在 UI 移不掉(后端缺按名禁用端点)。修:新增 `DELETE /v1/mcp-servers/allowlist/{name}`,前端残留行「移出」按名字走它。 |
| X-6 | **平台技能导出/同步收尾**(来源:skill-export-sync) | ① 52 个导出包待推测试环境(`POST /v1/platform/skills/import` 幂等可重跑,PLATFORM_ADMIN 凭证在金库);② admin-ui 技能导出按钮(platform + tenant);③ tenant export 丢 `supporting_files` bug(`skills.py:1390` 没传,platform 侧 `platform_skills.py:1333` 是无损姿势可照抄) |
| X-7 | **观测栈残留**(来源:W2-PR3 observability) | ① alertmanager receivers 仍 placeholder(P0 告警投递到空气);~~② compose 侧 prometheus 同源坑 ③ Langfuse org/project 显示名 ④ 探针 trace 噪音~~ **✅ ②③④ #1449 已合(09-09)**:promtool 单测挪 tests/ 子目录、overlays 设 `LANGFUSE_INIT_*_NAME`(既有实例的 org/project 名要在 Langfuse UI 手改)、`TRACE_EXCLUDED_PATHS` 探针路径不开 span。剩 ① |
| X-8 | **✅ 10-29 已上生产(`7e6b7679e`,最后一刀 #1832;#1402 #1404 随 09-07 `2d5742c6`,#1430 #1433 #1435 随 09-16 `5775fbf3`)** **Docker Hub 依赖镜像统一 mirror(CI 拉取限流假红 + 国内构建慢)**。规矩:Docker **官方**镜像在源码里一律写 `public.ecr.aws/docker/library/`(国内冷拉 `FROM` 1.1s vs Docker Hub KB/s,release.sh 受益;`nginxinc/nginx-unprivileged` 走 `public.ecr.aws/nginx/`),卫兵 `tools/ci/check_image_registry.py` 挂 Lint job、**零豁免**(测试素材的裸引用运行时拼,不写字面量);minio 迁 quay.io。**教训**:ECR Public 匿名拉取同样按 runner IP 限流(`Rate exceeded`,10-16 起 `Data limit exceeded`),任何匿名拉取在共享 runner 上都不可靠 → CI 改走自建 GHCR mirror(`ghcr.io/deepaihealth/mirror/`,包已 Public,integration 用 `GITHUB_TOKEN` 登录):#1430 定时同步 workflow;#1433 无第二公共源的 pgvector / pgbouncer 引用切 GHCR;#1435 沙箱镜像 gha cache 预构建 + `EXPERT_WORK_TEST_SANDBOX_IMAGE`(真病灶是沙箱镜像 apt 层在美国 runner 冷构建 10–22 分钟,修后 integration 8m17s);#1832 8 个官方镜像也进 GHCR mirror,**源码引用不改、只 CI 换源**(`prepull_images.py` 先拉 GHCR 再 tag 成 ECR 名,拉不到退回 ECR 退避;沙箱预构建用 build-contexts;`test_mirror_images_workflow.py` 钉「每个 ECR 官方引用都在 matrix 里」)。**未完成:** `searxng/searxng:latest` 仍在 Docker Hub;CI 冷缓存时沙箱镜像 apt 层仍慢(Dockerfile 已有 `ARG APT_MIRROR`,CI 不接,见 B-211)。 |
| ~~X-12~~ | **✅ 09-16 已上生产(`5775fbf3`,#1483)** fastapi 钉版放开到 `>=0.138,<1`(`iter_route_contexts` 0.138.0 才有;`uv lock` 只动 fastapi 一行 0.136.3→0.141.1)。七个路由自审文件统一走单源适配层 `tests/route_audit.py`,dependabot ignore 与 `tools/ci/protected_pins.toml` 两处钉子已撤。`strict_content_type` 不是本次带来的(0.130~0.133 起就默认 `True`),**对第三方零行为变更**:对外端点不带 `Content-Type` 返回 422 + 完整第三方信封。**有意的例外**:`route_is_external` 喂 `original_route`;今天全仓 `include_router()` 无一处传 prefix/tags/dependencies,将来谁改成 `include_router(dependencies=[...])`,生产信封会悄悄失效,只有那条一致性测试会红。**未修记账**:`observability._route_template` 将来加 prefix 会悄悄丢 metrics 标签前缀且无测试盯;fastapi 0.141 对 websocket 路由的 effective context 未实现(本仓零 websocket)。 | fastapi 0.137 起 `include_router` 懒挂载:`app.routes` 里是 `_IncludedRouter`,APIRoute 不再摊平,控制面整族路由自审(console lockdown / external gate / NUL guard / reachability / app_factory)集体失明。迁移=自审改用 `fastapi.routing.iter_route_contexts` 遍历;这批是安全闸,依赖合成改错比不改危险。生产代码零 `app.routes` 枚举。 |
| X-15 | **✅ 09-16 已上生产(`5775fbf3`,#1456;#1400 已随 09-07 `2d5742c6`)** **B-20 终审 follow-up 两条**(#1253 独立终审 I-1/I-2) | ① `retention-cleanup-job` 里的第二套审批超时实现整段删除(#1456),control-plane sweep(走 `resolve_approval_decision`)是唯一内核;② 审批行 `user_id` 原写死 `None`:写入侧改 `user_id=record.user_id`,迁移 `0151` 按 `run_id` 从 `agent_run.user_id` 回填(只填 NULL 行,downgrade 无操作)(#1400);测试环境 38 条审批单修前全空、全部可回填。 |

11-12 核实:行内「未完成(单列 B-31)」两件已由 B-31 解决并于 09-16 随 `5775fbf3` 上生产 —— 失效链 #1439(B-31 ①);`/admin/*` 写面 bearer 闸 #1399(B-31 ②,`services/credential-proxy/src/credential_proxy/app.py:167`)。

| # | 事项 | 备注 |
|---|---|---|
| X-16 | **✅ 09-07 已上生产(`2d5742c6`,#1329–#1332)** **批 E 收尾:E3b 失效总线全量接线 + C 类缺口**:#1331 总线 7→20 种 kind、平台配置面全接线,修好 judge / tool-budget「改配置连写入的 pod 都不生效」,quota Protocol 加 `invalidate_tenant`;#1329 技能面(平台 10 / 租户 7 个端点 + promote 审批,原先零失效);#1332 限流 override 缓存提取为可注入对象并接线;#1330 的 handler 占位同批闭环 | **未完成(单列 B-31):**credential-proxy 接总线(失效链现无人调用、单副本无跨副本陈旧,真接要加 redis 依赖);其 `/admin/*` 端点无鉴权,一起立项 |

## 小 backlog(移出条目)

| # | 事项 |
|---|---|
| ~~B-139~~ | **✅ 10-12 已上生产(`94dc73ba`,#1727 #1728)** **同一会话上一轮还在跑时,再发一轮不会被拦**:普通 run 没有忙检查(`RunManager.has_inflight` 无生产调用点,409 只在重新生成 / 编辑两个入口),两个 run 可能在同一份检查点上交错(B-75 记的「两轮交错」来源);测试环境实测约 0.3%。用户拍板策略 = **排队**:上一轮没结束时新一轮排在后面、等它结束再跑(不拒绝、不当插话注入)。后端 #1727(迁移 0160 + 对外文档),控制台排队显示 #1728;是 B-144 的前置。设计 `docs/superpowers/specs/2026-10-02-b139-same-thread-queue-design.md`。 |
| ~~B-151~~ | **✅ 10-12 已上生产(`94dc73ba`,#1731)** **定时触发把结果送回原会话时,不检查原会话是否有 run 在跑**:`trigger_delivery` 用 `aupdate_state` 把结果写进原会话检查点,只有按会话的锁(classid 8619),不看有没有正在跑的一轮;并行写会让结果从「最新」里消失(与 B-139 同类,测试环境探针复现)。修:投递在 8619 里再套 B-139 的会话接单锁 8620,原会话有没结束的一轮或最新一轮停在审批上就推迟,`trigger_run` 留在 FIRED 由调度器下一遍再投;「立即运行」此时返回 `pending`。 |
| B-269 | **✅ 11-04 已上生产(`91e8daf02`,#2012)** 一轮跑很久时新消息只能干等、对方看不到原因(同一会话一次只跑一个 run)。修(对外只新增可选字段):`agent_run.started_at`;`queued` 帧加 `ahead_run_id` / `ahead_status` / `ahead_started_at`,队列模式 202 加 `ahead` 与同样三个字段;对外 / 控制台 run 列表加 `started_at`、非终态加 `heartbeat_at`,排队行加 `queued_behind`(只列同一用户的 run,挡在前面的是别人的为 `null`);控制台排队气泡显示「上一轮已进行 N 分钟」。方案 `docs/superpowers/specs/2026-11-03-batch-b267-b273.md`。**未完成:** 「新消息打断上一轮」(`if_busy=interrupt`)未做 —— 对外行为变化 + 新旧两轮重叠写检查点的风险,需另立、先出方案(未编号)。 |
| B-270 | **✅ 11-04 已上生产(`91e8daf02`,#2012;执行单 `docs/runbooks/2026-11-04-prod-release-checklist.md`)** **长任务没有进度**:一轮几十分钟,用户只看到「运行中」。修(与 B-269 一起,只加信息字段):对外 run 列表与控制台 run 详情 / 列表对非终态 run 返回可选字段 `progress`(`elapsed_s` / `step` / `tool_calls` / `active_workers` / `current` / `last_event_at` / `plan_done` / `plan_total`,不含内容),由 `run_event` 一条聚合查询算出(2 秒超时,失败只省略该字段);新列 `agent_run.started_at`(迁移 0182);控制台运行中气泡显示「已进行 N 分钟 · 第 S 步 · 调用工具 T 次 · 子任务 W 个在跑」。对外文档 `query.md` §5.4 与更新日志已写。不做:事件流进度事件。 |
| B-35 | **✅ 09-07 已上生产(`2d5742c6`,#1356–#1360)** **委派增强层 4:per-agent `execution_mode: plan_first`(规划-执行分离)**:层 0–3 上线后模型都权衡后选 inline、动态委派趋零(业界同现象,提示词层无强制手段),plan_first 是把委派从概率变保证的结构解。设计 `docs/superpowers/specs/2026-08-28-plan-first-execution-design.md`,用户拍板:manifest 开关 per-agent 默认关;开启硬联动 `workflow.type` react→plan_execute + `dynamic_workers.enabled=true`(确认 Modal 明示后一次写入,另列 token 成本约 15× 等建议检查项);后端校验不一致 422 硬拒(不静默归一);关闭开关不回退 `workflow.type`。真栈:glm-5.3 全链生效(3 个 worker 真派发);kimi-k3 曾用 update_plan 把 delegate 步改标 inline 绕过,已收紧逃生门措辞(只许写操作 / 最终拍板改标)并把绕过计入 degraded 计数。PR-4 顺带修了全局审批黑洞(worker 撞闸静默假完成),不在开关后 |
| B-116 | **✅ 10-12 已上生产(`94dc73ba`,#1678 #1679;四个技能 10-12 当晚按执行单 Step A2 导入)** 平台自写 office 技能(docx/pptx/xlsx/pdf)clean-room 重写:原四个技能是 08-03 从 `anthropics/skills` 原样导入,许可证禁止衍生作品,且「新建」路线依赖沙箱里没有的 npm(B-55 已删)。源码放 `platform-skills/`,走 PR + CI;推翻 OFFICE-ADR-5「平台不预置内容」一句(见 `docs/streams/STREAM-OFFICE-DESIGN.md`)。#1679 把「检查成品」看图步骤改为 read_page→ask_image。生产导入依赖 B-84 的 `EXPERT_WORK_SKILLS_DIR`(Step A2 须在 Step B 之后)。设计 `docs/superpowers/specs/2026-09-25-platform-office-skills-design.md`,计划 `docs/superpowers/plans/2026-09-26-platform-office-skills.md`。旧版本许可风险见 B-117;沙箱字体见 B-118。 |
| ~~B-119~~ | **✅ 10-12 已上生产(`94dc73ba`,#1678)** **B-116 office 技能随附的平台文案修订**:`services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py` 的 `SANDBOX_UNAVAILABLE_NOTE` 删掉针对旧技能正文的说法(「docx/pptx 技能正文写着 npm install」),「沙箱里没有 npm」的核心指引不变,无功能影响。运维要点:新 office 技能正文的脚本路径依赖 `EXPERT_WORK_SKILLS_DIR`(B-84 `233791e5` 才有),所以技能导入必须在代码发版通过之后;代码回滚时导入过的技能要一起退回原版。 |
| ~~B-121~~ | **✅ 10-12 已上生产(`94dc73ba`,#1681)** 沙箱时钟是 UTC,与系统提示词「当前日期」的时区(Asia/Shanghai)不一致(全平台共性,ai-health-plan 带出):沙箱没有 `TZ`,`datetime.now()` 慢 8 小时,文件名时间错、0~8 点日期差一天。修法:新模块 `orchestrator/agent_timezone.py` 作单源,`platform_exec_envs()` 恒注入 `TZ`,与提示词共用 `resolve_agent_timezone()`(改 `EXPERT_WORK_TIMEZONE` 两边一起变,配错两边都回落 UTC);镜像自带 zoneinfo,不用重建。 |
| ~~B-122~~ | **✅ 10-12 已上生产(`94dc73ba`,#1682)** 子智能体工具边界平台化:原来子智能体继承父的全部工具(只剥 `manage_task`),边界全靠提示词 —— ai-health-plan 的子智能体自己 `save_artifact` 登记中间稿,还拿得到给客户发企微消息的 MCP 工具。参照 hermes-agent / openclaw(平台写死禁用清单 + 子代提示词)。修:动态子智能体不给登记产物 / 审批、不给自我进化类工具,MCP 只给只读工具(按 `readOnlyHint`,没标当写)且 http 只许读方法;不禁改文件但小结须列改动文件;平台自动插入主子两侧提示词;主 Agent 零变化。依赖深护智康 MCP 读写标注(`f365a1e`,40 个工具 31 只读 / 9 写)。回退开关 `EXPERT_WORK_WORKER_TOOL_POLICY=off`(改后需重启 pod)。设计 `docs/superpowers/specs/2026-09-27-worker-tool-policy-design.md`。**未完成:** 分出待办 —— 主 Agent 写工具自动审批(先核对接方能否处理审批暂停),未编号。 |
| ~~B-123~~ | **✅ 10-12 已上生产(`94dc73ba`,#1683)** **子 Agent / 子智能体拿不到平台共享目录的 MCP**:共享目录的 MCP 服务要租户开通(`mcp_allowlist`,空名单 = 一个不给),只有主 Agent 构建(`runtime.py`)读了开通名单,静态子 Agent 与子智能体的构建(`subagent_runtime.py` 两个 `_build`)没读 → 深护智康 MCP 对子代完全不可见。修:子代构建也读开通名单。顺序硬约束:B-122(子智能体只拿只读工具)必须先于本条,否则子智能体能调写操作工具。 |
| ~~B-124~~ | **✅ 10-12 已上生产(`94dc73ba`,#1685)** 子代(动态子智能体 + 静态子 Agent)写的文件父读不到:子代的存储类工具(read_file / list_dir)用父的 `agent_key`,但沙箱执行(write_file / exec_python / bash)绑的是子代自己名字算出的 key;B-60 起沙箱 `/workspace` 按 key 分目录,子代文件落 `agents/<子代 key>/`,父 read_file not_found,子代也看不到父的 `inputs/`、`style/`(B-122 验收实证 ahp 子智能体 1.6 万字版式方案丢失)。修法:沙箱执行改用运行时 `ctx.agent_key`(子代=父的),技能种子路径锚到同一个 key;主 run 行为不变。**未完成:** ① 静态子 Agent 与父同名不同版本的技能会在 `/opt/skills/<父 key>/` 互相覆盖(低概率,可改按 `技能@版本` 分目录);② 09-16 以来子代写进 `agents/<子代 key>/` 的旧文件无人可见,需一次性清理或纳入清理任务。 |
| B-125 | **✅ 10-12 已上生产(`94dc73ba`,#1686–#1689;技能导入并启用 + ai-health-plan rev39 生产版 = 线上第 4 版)** **健康方案交付件平台技能 `health-plan-report`**:把定稿的健康管理方案渲染成客户交付件(可编辑 PPT + A4 PDF),纯 Python、在沙箱里跑。用户拍板:**技能只呈现不制定内容**;用药数据只内部用、不进交付件。spec `docs/superpowers/specs/2026-09-28-health-plan-report-skill-design.md`,计划 `docs/superpowers/plans/2026-09-28-health-plan-report-skill.md`。**未完成:**等对接方真实出稿后只读观察再销案;`org_name` 对方两端都没传,封面暂无机构名。 |
| ~~B-126~~ | **✅ 10-12 已上生产(`94dc73ba`,#1694 #1696)** 跨轮上下文降本:收缩门槛都是 0.7×窗口,百万窗口模型 70 万才触发 = 从不触发,每轮全量回放旧工具结果(第 3 轮起步 7.3 万 token,跨轮首调缓存命中约 25%)。PR1:只清本轮之前的旧工具结果,换成可重读的引用(持久化路径 / skill_view),本轮内逐字不变保缓存;看图结论不清;兜底门槛 = min(0.7×窗口, 20 万);顺带修 CM-12 footer 标签注入。PR2:工作窗口 / 压缩器同样封顶 20 万;压缩摘要写回检查点复用,不再每轮重算(摘要全文不进 SSE / run_event)。关跨轮清理:Agent 配置 `cross_turn: false`;摘要复用无开关,只能代码回滚(旧镜像读新检查点不报错)。不做清理前记笔记(工作区按员工不按客户,会串客户)。spec `docs/superpowers/specs/2026-09-28-cross-turn-context-pruning-design.md`。**未完成:** 测试环境重放 ai-health-plan 对比成本 + 核 skill_view 不被截断。 |
| ~~B-127~~ | **✅ 10-12 已上生产(`94dc73ba`,#1695)** MCP 工具参数固定值(`arg_bindings[].fixed`):配置里写死「某工具的参数 P 恒为 V」,平台把 P 从模型看到的 schema 里剥掉、每次调用注入 V 并覆盖模型给的值(复用 B-61 剥 / 填路径)。用户否决写进提示词(「没人会记得写」),也不走变量绑定(要对接方每轮都传)。值只收字符串;`args` 可空,但一条绑定至少配一边、两边参数名不相交;空 `fixed` 不落库(存量指纹不变)。上游 schema 还没有该参数时按「绑定落空」:保存时告警、**不注入**(防 `additionalProperties:false` 硬拒),上游加上后下一次构建生效(主 Agent 构建缓存 TTL 1800s;worker 每次现建)。子智能体经父 spec 拷贝拿到固定值;配置页每个参数行多一档「固定值」。**回滚纪律**:用了 `fixed` 的 manifest 旧镜像读不了(`extra="forbid"`),回滚前先把该 agent 退到不带 `fixed` 的配置版本。 |
| ~~B-128~~ | **✅ 10-12 已上生产(`94dc73ba`,#1697)** prompt 视图只保留最新一份系统提示词:入口每轮往检查点追加一条 SystemMessage,`coalesce_system_messages` 拼成一条 → 第 N 轮带 N 份几乎相同的提示词(7 轮 7 份、每份约 5k token),前缀缓存每轮断开;中途改过提示词时模型同时看到新旧两版。修:`agent_node` 在 `filter_superseded_turns` 之后、上下文闸之前调 `keep_latest_system_prompt`,只留最后一条非 `<context-summary>` 的 SystemMessage 放到下标 0;只改 prompt 视图,检查点不动,无开关无迁移,回滚 = 回镜像。语义变化:变量值只以当轮渲染为准,对接方须每轮传全量变量(09-15 起已如此);`:regenerate` 仍用被取代那一轮的原提示词。实测同一段 7 轮对话 ¥9.98 → ¥6.63(−34%)。 |

11-12 核实:PR #1699 状态 CLOSED(未合并);做法已由 B-130 接替(B-130 仍在主清单,11-05 挂起)。

| # | 事项 |
|---|---|
| B-129 | **(2026-09-29 立项;❌ 2026-09-30 测试环境验收未通过,PR #1699 已关闭未合并,不上班车 2)旧的只读查询结果收成「再调一次」**:新一轮开头把旧轮次里可重新查询的只读工具结果(无副本、≥500 字)换成一行「需要请再调一次」。验收(ai-health-plan 8 轮对话,开 / 关各 3 次):成本降 20%~28%、48 轮全部成功、交付件全过校验,**但 1 次在第 3 轮没有重查、凭记忆拼出血糖曲线,编了一个数据里和用户话里都不存在的值,还给用户口述值安了错误日期** —— 「凭印象写错 = 0」判据不达标。调研结论(openclaw / hermes-agent 源码 + Anthropic / Manus / LangChain / JetBrains / MemGPT 等):成熟框架不会只靠「再调一次」的提示,要么只在上下文吃紧或缓存已过期时才清,要么把事实换一种形式留在上下文里;行业没有人实测过「清理后编造」,都假设模型会自己回头查。分支 `docs/b129-requery-prune-spec` 保留作记录(spec / 计划 / 验收数据)。由 B-130 取代。 |
| ~~B-131~~ | **✅ 10-12 已上生产(`94dc73ba`,#1700)** **减少修改轮的额外调用**:ai-health-plan 近 30 天 110 个修改轮里约 23% 的调用是额外开销(单独一次只为给计划打勾 11.3%、两份交付件分两次保存约 5.5%、重新摸索技能用法 5.4%)。平台:计划附言与 `update_plan` 说明改为「打勾与下一步动作放同一次回复」;本轮自然答完(`text_response`)时平台把未完成步骤全部标为完成(步数 / 预算耗尽、审批暂停、复查返工不动)—— 用户拍板「提示词要求逐步打勾 + 平台兜底收全部」。技能:health-plan-report 开头加「改稿速查」。效果:修改轮平均调用 7.0 → 4.2~6.0 次,纯改稿轮成本 −28%。 |
| ~~B-136~~ | **✅ 10-12 已上生产(`94dc73ba`,#1718)** **读文件静默截断、不能分页**:`read_file` 超 2 万字符直接截断,截断标记只进元数据(`ToolMessage.artifact` 不发给模型),模型以为读到了全文;`read_document`(上限 20 万字符)同样;大结果外置后提示「用 read_file 查看」,而 read_file 自己读不完。生产近 60 天 ai-health-plan 30 次读文件截断 7 次、sop2-designer 28 次截断 2 次。修(`tools/file_ops.py` `ReadFileTool`):加起始行 / 行数参数,没给全时在围栏外写明「显示第几行到第几行、共几行、下次从哪行读」;`read_document` 截断同样明说;外置提示语同步改(对齐 openclaw / hermes / Claude Code)。 |
| ~~B-137~~ | **✅ 10-12 已上生产(`94dc73ba`,#1725)** 没有增量写大文件的办法:`edit_file` 一次只改一处、没有批量替换,编辑失败后的官方提示是「用 write_file 整篇重写」。做法(与 B-149 同一设计稿 `docs/superpowers/specs/2026-10-02-b149-b137-layout-and-edit-design.md`,一起开发):`edit_file` 加一次多处编辑(主项,省往返)与批量替换(顺带);不做补丁格式(对国产模型不一定友好);`write_file` 说明写明「改已有文件用 edit_file」;`read_file` 说明里的内容哈希模型看不到、编辑失败以「哈希对不上」为主,一并处理。用户拍板不暂缓,范围按数据定:生产 ai-health-plan 重写占写入字符 35%(重写内容 61%~89% 与上一版逐行相同),带 B-84 修复后可省降到约 3.5%,但「连续多轮只对同一文件各改一处」13 组多花 35 次模型往返;sop2-designer 上没有收益。 |
| B-140 | **✅ 已完成(10-07,#1740 #1742 #1758;评测工具,不进生产镜像)** 行为回归评测集第一版:此前 `tools/eval/datasets` 的 golden / regression 目录是空的,提示词 / 工具 / 模型改动只靠临时重放 + 金丝雀放行。做法:按线上失败的形态造合成用例(用户拍板纯合成,客户对话原文不进仓库;通用 + ai-health-plan 两种形态),在测试环境用对接方租户 + canary key 真跑,支持改动前后对照;#1758 加压缩用例 c01–c04、「不可判」判定与 `eval-compress` 智能体。24 个用例 ×3 基线,修后 72 次过 69(带出 B-158 / B-159 / B-160),阈值按基线 ×1.5。用户拍板:只在动执行层的 PR 合并前跑,不定期、不每晚跑。B-46(差评进数据集)另排。设计 `docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md`。 |
| ~~B-141~~ | **✅ 10-12 已上生产(`94dc73ba`,#1755)** **压缩摘要器读得太少**:摘要输入每条消息最多 2,000 字符、整体最多 24,000 字符(`context/compressor.py`),中段几十万 token 只靠约 6 千 token 头尾概括;研究显示压缩的主要伤害是模型丢失「做到哪一步」。修:摘要输入放宽到 16 万字符 + 先砍旧工具结果 + 摘要固定保留 `## Progress` 段 + COMPACTION 帧带 `summary_input_chars`;不重做压缩。设计 `docs/superpowers/specs/2026-10-07-b141-b143-compression-summary-design.md`。现行压缩门槛是 B-126 的 min(0.7×窗口, 20 万),不是 70%。上限放大的那一半由 B-165 的留存评测补验。 |
| B-142 | **✅ 10-12 第一步已上生产(`94dc73ba`,#1757)** **缓存命中没有持续指标,前缀在运行中会变**:第一步加了按厂商口径的命中计数器、记录规则 `expert_work:llm_prompt_cache_hit:ratio1h` 与看板,顺修估算漂移分母在 OpenAI 兼容厂商上重复计缓存。**未完成:**第二步 —— 按指标找前缀变化,再决定动哪个点。已知会动前缀的点:`find_tools` 提升 / 降级增删工具定义、`plan_first` 派发轮收窄工具、收尾轮清空工具、系统提示词里的日期行、追加在最后一条消息上的上下文压力提示(数字每步变);Anthropic 路径的工具定义没打缓存标记。参考:openclaw 稳定 / 动态分界标记 + 断点检测,hermes「缓存前缀不可破」。 |
| ~~B-143~~ | **✅ 10-12 已上生产(`94dc73ba`,#1756)** 压缩、摘要等辅助调用原先都用主模型。修:新增路由 `routing.when: compression`,默认 glm-5.3 / 5.2 → `glm-5.3-flash`,主模型挂备用,不跨厂商;前置是生产价目表有 `glm-5.3-flash`(新模型先核价目表,B-132 教训)。实测 flash 12/12 vs glm-5.3 11/12,摘要约占会话花费 4%(0.10 元 vs 0.96 元)。更大范围的按任务路由等有数据再议。 |
| ~~B-149~~ | **✅ 10-12 已上生产(`94dc73ba`,#1724;B-137 并入)** **模型读到的文件和命令输出没有换行、没有缩进**:防注入「数据标记」(`expert_work.common.spotlight.datamark`)把工具结果里每段空白(含换行、缩进)都换成标记符,默认开、作用于所有工具结果;实测 `read_file` 平均 6,567 字符只剩围栏自带的 2 个换行。后果:模型看自己写的 Python 没有缩进、技能文档标题 / 表格被压平,改不准只能整篇重写(B-137)。修(用户 10-02 授权,选方案 ①):标记只打在行内空白上,保留换行与行首缩进(防注入强度略降:换行处不再打断);真模型对照 glm-5.3 / kimi-k3 注入 160/160 未被带偏。设计 `docs/superpowers/specs/2026-10-02-b149-b137-layout-and-edit-design.md`。`unspotlight` 不可逆(只还原词,不还原版式)。 |
| ~~B-152~~ | **✅ 10-12 已上生产(`94dc73ba`,#1752 / #1762)** 控制台代码编辑器(Agent 配置页 YAML、技能文件编辑等 8 处,`@monaco-editor/react`)运行时从 cdn.jsdelivr.net 拉 `monaco-editor`:测试环境从打开到可用 26 秒,加载期间显示没有样式的半成品。修法:Monaco 打进 admin-ui 构建产物(`loader.config({ monaco })` 走本地包),首次打开编辑器才下载,JS/TS/HTML/CSS 语言服务按文件类型懒加载(用户 10-07 要求保留「第一次打开才下载」);#1762 补镜像构建内存。改后编辑器 0.56 秒可用、0 个 CDN 请求。 |
| ~~B-153~~ | **✅ 10-12 已上生产(`94dc73ba`,#1733 #1753)** Langfuse 从 08-20 起收不到任何数据:依赖批量升级 #1233 把 langfuse 3.15 升到 4.14,4.x 删了 `start_generation`(改为 `start_observation(as_type="generation")`);适配层测试用自写假 SDK 全绿,异常被「Langfuse 出错不影响 LLM 调用」的兜底吞成 warning,6 周没人发现,生产很可能从 09-07 上线起就是空的。修:适配层改调 `start_observation`,加两条对已安装 SDK 真类的契约测试(#1733);被兜底吞掉的异常加计数器 `expert_work_langfuse_delivery_failures_total{stage}` + P2 告警,出现 1 次就报(用户拍板,#1753)。告警只到 wecom-adapter 日志(企微 webhook 用户 10-08 拍板不配)。第二层原因(ClickHouse 写满盘)见 B-154。 |
| ~~B-154~~ | **✅ 10-12 已上生产(`94dc73ba`,#1734;执行单 Step C 已删生产旧日志表)** Langfuse 的 ClickHouse 数据盘写满,写入 `NOT_ENOUGH_SPACE`、worker 重试三次后丢弃,而 Langfuse web 照样 200、smoke 照过,没有任何信号(测试环境 08-20 起丢数据)。根因:占满的是 ClickHouse 自己的系统日志表(`text_log` 12GiB 等),默认没有保留期,约 20 天写满;Langfuse 数据只有约 170MiB。修法:`infra/k8s/base/langfuse/clickhouse-system-logs.xml` 挂进 `config.d`(系统日志 7 天 TTL、服务日志与 `text_log` 降到 warning、关掉 `processors_profile_log` / `opentelemetry_span_log`);换配置后旧表被改名成 `*_0`,需手工删;smoke 加数据盘 ≥80% 告警(warn-only)。**注意**:ACS 上扩 PVC 文件系统不跟着扩;`apply -k` 会重启 `langfuse-clickhouse-0`,Langfuse 写入中断约 1 分钟。 |
| ~~B-155~~ | **✅ 10-12 已上生产(`94dc73ba`,#1738,与 #1736 #1737 同批)** **流式请求在 run 已 `success` 后 uvicorn 报 cancel scope `RuntimeError`,客户端收到 `ReadError`**:对接方看到 run 成功但连接异常断开;每个新 pod 第一个流式请求必中,之后每次 MCP 池(缓存 30 分钟)过期重建再中(发版金丝雀首跑失败两次都是它,曾误归为部署抖动)。根因:`tools/mcp.py` 的 `_open_session` 带着没退出的 `AsyncExitStack` 返回,anyio task group 的 cancel scope 留在请求任务上;anyio 4.14(#1605)起请求结束即报错,所以必须与 #1605 同班上线。修:会话的打开、使用、关闭都放进一个专属任务(`_open_owned_session`),含「打开中途被取消」的处理。 |
| ~~B-156~~ | **✅ 10-12 已上生产(`94dc73ba`,#1736;10-13 核对两 pod 5 轮整理全 `errors=0`)** **平台辅助模型默认写死 anthropic / claude-\*,而测试、生产只有 glm / kimi / deepseek / qwen / doubao 凭据**(B-51 的根因):记忆整理从开荒起每 4 小时一轮、每轮约 196 次 `CredentialsResolverError`,一条没整理成;同一默认值还被「生成委派策略」、技能进化、评测、质量评审复用,界面改不了(在 `infra/k8s/base/configmap.yaml`)。修:三对默认值统一改 `glm` / `glm-5.3-flash`(用户 10-04 拍板,生产同);smoke 加「辅助模型厂商有没有平台凭据」warn-only 检查。连带修:整理单飞锁的事务整轮空闲开着,`idle_in_transaction_session_timeout` 5 分钟会被 PG 杀掉,另一副本并发再跑、赢家报 `cycle_failed` → 改 12 小时(同 `workspace_janitor`)。 |
| ~~B-157~~ | **✅ 10-12 已上生产(`94dc73ba`,#1737)** 三个 204 接口(删除智能体、断开 MCP OAuth、删除上传图片)回 `JSONResponse(None)`,带 4 字节 `null` 响应体,uvicorn 每次报 `RuntimeError: Response content longer than Content-Length`;客户端拿到的 204 正常,只是服务端留错误堆栈。修:改回空的 `Response(status_code=204)`,三处测试加「响应体为空」断言。 |
| ~~B-158~~ | **✅ 10-12 已上生产(`94dc73ba`,#1744)** 一次回答超出输出上限,整轮直接失败、什么都不交付:模型想在一次回答里写完整份方案 + 脚本 + 思考,撞满上限,按 B-105 规则(截断且带工具调用 = 不可用)抛 `OutputTruncatedError`,run 错误结束、产物为空(评测 eval-ahp h08 3 次败 2、h11 3 次败 1;当时生产真实智能体未见到顶)。修法(不是调大上限):丢掉被截的那条回答,给模型一条「上一条太长被截断,请分步写」的提示重来一次(与 B-191 合计每步至多一次)。修后 B-140 全量 36 次截断致败 0 次,h08 1/3 → 3/3、h11 0/3 → 3/3。上线后盯 `expert_work_llm_output_truncated_total{usable="false"}`。降档重试见 B-189(不做)。 |
| ~~B-159~~ | **✅ 10-12 已上生产(`94dc73ba`,#1743 #1763)** **读文件失败后换别的工具补到了,run 仍被标「未完成」**:`unresolved_failures` 按「同一个键再次成功才抵消」记账,`read_file` 失败(如模型编了不存在的技能路径)后用 `skill_view` 补救永远抵消不掉 → `completed=false`,对接方会把正常交付当成没做完。修:只读工具(`read_file` / `skill_view` / `search_files` 等)的失败不进「未完成」账(参照 hermes 只记文件变更类)。#1763 补漏:工作区文件工具真实抛的是 `FileOpError("read_file failed: not_found")`,文本兜底加认 `not_found`,用例改用真实异常。同类后续见 B-164。 |
| ~~B-160~~ | **✅ 10-12 已上生产(`94dc73ba`,#1747)** 上一轮没做成,对话此后每一轮都标「未完成」(B-140 h07 带出):`unresolved_failures` 通道没有 reducer,`tools` 节点只在跑过工具时写它,每轮图输入(`build_run_graph_input` / `replay_graph_input`)清了审批三件套却没清它 → 欠账跨轮;对接方读 `completed` 会把一次失败之后的每一轮都当没做完。修:两处图输入每轮写空列表。已知局限(不在本条):换工具补救(`edit_file` 失败 → `exec_python` 改同一文件)仍抵消不了失败,按键记账所致。 |
| ~~B-161~~ | **✅ 10-12 已上生产(`94dc73ba`,#1754 #1764)** **检查点里我们自己的类型没登记**:langgraph 读 `Plan` / `ClassifiedToolError` 时警告「未登记类型,将来版本会拒读」,仓库没有 `allowed_msgpack_modules`,依赖升到拒读版本时带计划或失败记录的对话会读不出来;计时包装层 `TimingCheckpointSaver` 还丢了严格模式下的自动登记。修(#1754):8 个类型显式登记(读取立即严格,用户拍板)+ 包装层转发登记 + 全局测试守新类型。#1764 补登 asyncpg 的 UUID 子类(记忆 `MemoryItem.id`;被拦时退回十六进制串再转回,数据没丢),用例改用 asyncpg UUID。 |
| ~~B-162~~ | **✅ 10-12 已上生产(`94dc73ba`,#1761)** 模型把每一步尾部的平台段当成用户最新消息:B-84 PR-2(09-21)起工作区快照、计划、`per_turn` 记忆各作为一条 user 消息挂在提示词最后(工具调到一半也是);测试环境 eval-compress 114 次 run 里 48 次模型明说「这一轮的用户消息只是工作区快照」,09-21 前为 0。GLM 对话模板里 tool 后跟 user = 新一轮开始,各家也都不在工具结果后另起 user 消息。修:三者合成一段 `<platform-context>`,追加到提示词最后一条消息末尾(轮首 = 用户消息或「本轮输入」段,中途 = 最后一条工具结果);标签含义只在系统提示词讲一次;用户与工具文本里的同名标签先转义;渲染页带图仍单独一条;判官取用户原话时剥掉追加段。代价:被追加的那条下一步变回原样,前缀缓存多 prefill 一次。 |
| ~~B-163~~ | **✅ 10-12 已上生产(`94dc73ba`,#1774)** 工具出错后的恢复建议单独占一条 user 消息:`<recovery-advisory>` 作为独立 HumanMessage 落库,GLM 模板把 tool → user 读成用户新一轮(B-162 之后平台段也贴在它上面)。参照 openclaw / hermes-agent(失败提示追加在出错的工具结果里,只追加、不断缓存)。修法:工具节点给失败的 ToolMessage 打标记,拼提示词时把建议贴到它末尾(落库原文不变),删独立消息,建议改事实口吻。 |
| ~~B-164~~ | **✅ 10-12 已上生产(`94dc73ba`,#1772)** **只读工具失败被记成欠账**(同类第三例,前两例 B-159 / #1763 每次只补一种错误):如 `read_file` 带 `..` 被拒(`invalid_arguments`)后改用 `skill_view` 拿到内容,仍 `completed=false`。修:声明只读的工具,非瞬态失败一律不进欠账;写入 / 执行 / MCP / 未知工具照旧(参照 hermes 只给写类工具记账、openclaw 从不因工具失败判未完成)。对外口径变化:纯问答、读失败后照答的 run 记为完成。 |
| ~~B-165~~ | **✅ 10-12 已上生产(`94dc73ba`,#1773)** **压缩摘要看不到工具调用参数**:摘要输入只取 `msg.content`,AIMessage 的 `tool_calls` 不渲染,写了哪些文件、改了什么进不了摘要,工具结果行也不带工具名。修:摘要输入渲染工具名 + 截断后的参数(对齐 openclaw / hermes)。离线留存评测(合成长对话中段埋编号)新 12/12 vs 旧 24k 上限 1/12,补上 B-141 没验到的一半。 |

11-12 核实:①②③④⑥ 已并入 B-176(设计与计划 #1793,B-176 仍在主清单);⑤「记忆步骤单独指定模型」已由 #1779(B-168,10-12 上生产)做掉 —— `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:571` 的 `routing.when` 已含 `memory`。

| # | 事项 |
|---|---|
| B-167 | **→ 10-10 ①②③④⑥ 并入 B-176 设计与计划,以 B-176 为准。** **(2026-10-09 登记,用户拍板:先记不排期)长期记忆模块的治理缺口与效果基准** —— 对照《Agent 长期记忆的主流技术路线横向对比(2026 版)》(mp.weixin.qq.com/s/djEYRmmRxiWeMFhQPZbOog)评估:我们属于「抽取管线(Mem0 式增删改)+ 向量 / 全文混合检索 + 双时态事实时间线(不建图)+ 后台整理」,方向对;文章三原则「可解释 / 可编辑 / 可遗忘」+ 跨用户隔离 + 审计逐项核过代码,缺口如下。① **健康类智能体开长期记忆前必须先定作用域**:记忆按员工(user)存,员工同时服务多位客户,抽取出的客户病情 / 用药会在服务下一位客户时被召回(与 B-126「不做记笔记」同一顾虑);事实默认跨该用户的所有智能体共享(`agent_name` 为空,`memory/sql.py:245-250`),只有经历类按智能体隔离。测试环境 10-09 实查:ai-health-plan / ai-health-report 都是 `"long_term": null` 未开、0 条,风险现未发生;真开的是 sop2-designer(346 条)、ai‑expert‑group 等。做法二选一:按客户分作用域,或健康类只记员工工作偏好、客户事实以对接方业务系统为准。② **人工更正原地覆盖**:`PATCH` / `/correct` 走 `update_content` 直接改行(`memory/sql.py:405-443`),不走自动更新用的 supersede 版本链,改前内容丢失 → 改为 supersede。③ **删除语义不准**:`purge_user` 注释写 HARD-DELETE,记忆实际是软删(`delete_all_for_user` 设 `deleted_at`),90 天后 retention 物理删 → 注释与对外说法改准;**租户删除时记忆的连带清理未找到**,要补或核实。④ **自动写入无审计**:人工改 / 删 / 更正 / 整理有审计,模型自动抽取与 reconcile 写入没有。⑤ **记忆步骤不能单独指定模型**:运行时的抽取 / 去重比对 / 压缩前抢存 / 读时复核 / 检索改写五处共用智能体主模型路由(`agent_factory.py:966-975`「都用 Agent 主模型」),「按步骤指定模型」`routing.when` 只有 planning / reflection / compression(`agent_spec.py:568`),没有 memory;只有后台整理能单独指定(`policies.memory_consolidation.aux_model`,缺省走平台默认 glm-5.3-flash)。做法照 B-143(#1756):`when` 加 `memory`,把五处接到该路由;再用 LoCoMo 评测对照主模型 vs 同厂商便宜型号的抽取质量后决定默认值。**→ ✅ 10-10 随 B-168 PR A #1779 合入(`routing.when: memory`,默认同家便宜型号 + 关思考;控制台 #1784)。LoCoMo 对照(主模型开思考 vs 记忆默认)未跑,仍在第⑥项。**⑥ **效果基准要重跑**:`longmem_baseline.yaml` 记的 LoCoMo 端到端 0.20 是 06-25、`--limit 50 --sample 0`(第一段对话前 50 题,零单跳题);但 06-11 本机其实跑过全量 1,540 题(`eval-out/locomo_endtoend.jsonl`,抽取 / 答题 / 判分全用 deepseek-v4-pro,早于 P5):总 23.1%(单跳 21.5% / 多跳 36.9% / 时序 15.3%),**70% 的回答是「记忆里没有」**(第一段对话 19 会话 419 轮只写入 44 条)→ 瓶颈在抽取丢信息,远低于设计文档参考线(Mem0 66.9%、朴素 RAG ~61%)。重跑:全量 + 一组「不抽取、直接按原文检索」对照;抽取 / 答题用 glm-5.3(平台实际用主模型)约 140 元,全用 DeepSeek 约 20–50 元,对照组加约 10–20 元(10-09 估算,未实测)。附带:后台整理(B-156)10-12 才在生产修好,上线后看首轮整理效果;生产上哪些智能体开了长期记忆未核。 |
| B-168 | **✅ 10-12 已上生产(`94dc73ba`,#1779 / #1783,设计稿 #1780,控制台 #1784)** 长期记忆写入挡住结束帧:每轮 LLM 已答完,对接方要再等 1.9–36.1 秒才收到 `end` 帧。根因:`memory_writeback` 节点在 END 之前,结束帧要等它;记忆的五处模型调用(写回抽取 / 去重合并 / 压缩前抢存 / 读时复核 / 检索改写)全走主模型路由并继承思考。业界(Hermes / OpenClaw / LangMem / Letta / Zep / Mem0)都是先答后写。修法:PR A #1779 —— `routing.when` 加 `memory`,五处统一用它;没配规则时默认同厂商便宜型号 + 思考降到最低(找不到便宜型号用主模型但也降思考);用量按记忆模型记账;控制台路由可选「记忆」;写回帧带条数。PR B #1783 —— 写回挪到 `end` 之后的后台任务(迁移 0161–0163,默认 `background`,应急 `EXPERT_WORK_MEMORY_WRITEBACK_MODE=inline`;同一用户串行、跨用户并行、重启后捡回;后台记忆 token 记 `platform_overhead`);代价=新开对话几秒内可能读不到上一轮刚写的记忆(同一对话不受影响)。改后 answer→`end` 0.16–0.19 s(修前 1.7–5.5 s)。**回滚坑**:旧镜像 Literal 不认 `when: memory`,生产不确认不回滚前别写,只靠默认值。**未完成:** ① `sop2-designer` 有人用后核写回(执行单 Step E;10-13 发版后 0 个 run,未验)再销案;② 遗留 M2 增量切片 / 抢存与写回同队列 / RLS 前改 worker 扫描已并入 B-176;③ 未排期:降低抽取频率(每 N 轮 / 压缩前 / 会话结束,须 LoCoMo 验召回)、at-least-once 窗口(极端情况下过期写者仍可能写一次)、上一轮后台写回与下一轮压缩前抢存可并发、控制台按轮 `getRun` 的请求扇出。 |
| ~~B-171~~ | **✅ 已完成(10-10,并入 B-175,以 B-175 设计为准;本条只有方案无代码)** **知识库模块整体优化(方案)**:设计 `docs/superpowers/specs/2026-10-10-b171-knowledge-base-optimization-design.md`(对标 WeKnora / PageIndex / OpenViking 的结论在 §8,待拍板 7 项在 §9)。起因:客户原始资料格式多样、结构混乱,现状只有「原样解析 → 切块 → 检索」。当时核实的缺陷:关键词 `plainto_tsquery` 全词 AND 致真实查询 0 命中;智能体只有一个笼统的 `knowledge_search`、引用无页码;重排默认用聊天模型、专用重排丢分数;扫描件 / 图片读不出;入库三个可靠性 bug(第 5 次尝试崩溃卡 processing、恢复任务重复处理、同名重传写回旧内容);切块重叠按 token 截中文出乱码;评测只有 11 条手造用例。方案主线:原始资料层 + 整理条目层两层知识,每句事实有出处,矛盾交人确认。 |
| ~~B-172~~ | **✅ 已完成(10-10):并入 B-175 的 P10 物料库,以 B-175 设计(#1792)为准,本条不单独实施** **物料库:按客户语义 / 意图搜索商品与视频物料**(用户拍板先出方案、不排期)。原设计 `docs/superpowers/specs/2026-10-10-b172-material-library-design.md`(#1789)要点:别的系统里的物料走 MCP、不在本方案;物料是一条记录(短编号 `M{seq}`,前端按 ID 渲染卡片,模型无法编造);「物料类型」声明属性(可筛选 / 硬排除 / 软加权 / 对客可见),默认只对禁忌、上下架、有效期硬过滤;每条生成需求画像解决说法对不上;视频需新接语音识别;`material_search` 的对客 / 对员工模式由平台判定、不给模型选。评测门槛:硬规则违反 = 0、对客泄露 = 0。待拍板 6 项见设计 §10。 |
| B-175 | **✅ 10-29 已上生产(`7e6b7679e`,#1799–#1804 #1806 #1808 #1818–#1821 #1827 #1847 #1955 #1958 #1965 #1966)** 知识库 v2 整体优化(后端 · Web 管理端 · 开放 API),取代 B-171 / B-172。设计 `docs/superpowers/specs/2026-10-10-b175-knowledge-base-v2-design.md`,计划 `docs/superpowers/plans/2026-10-10-b175-knowledge-base-v2-plan.md`(波 1 / 波 2 逐 task 计划 `docs/superpowers/plans/2026-10-13-b175-wave1-plan.md`、`docs/superpowers/plans/2026-10-14-b175-wave2-plan.md`)。已上线:波 1 地基(切块、上传队列、代际围栏、终态巡检、嵌入退避、50 MiB 上限、审计、服务端分页、同名重传当更新、文档 / 分段 / 修订 / 标签表,迁移 0164 / 0165);波 2 检索质量(中文关键词、向量参数 + 加权融合、文档名进检索文本 + 两视角重排 0.7 带标题 / 0.3 正文(迁移 0167)、每库重排分数下限只挡离题(迁移 0168,默认关,输入框参考 0.70)、`knowledge_search` 带平台提示「只按段落明确写了的回答,没写就说知识库没写」(只随该工具走)、重排失败降级不报错);检索日志 + 命中测试接口(迁移 0177)、文档与整块维护(迁移 0176)及对应两个管理端页面。效果:资料没写的题编造 13/30 → 3/30,有答案误拒 0。用户拍板:P8 FAQ / P9 数据源 / P10 物料库不做;开放 API 是定位变更(原定位「第三方只给 agent run」),对外前缀不能以 `/v1/knowledge` 开头(锁定检查按字符串前缀,`test_console_lockdown.py`)。读文档 / 生成问题 / 列文档 / 元数据抽取四项以 B-206 为准。**未完成:** 其余项排进知识库优化总计划步 4~8;P12 知识库体检(一键自动出题 → 检索 → 判能否回答 → 报告,只报告不改资料)4 波做完后再安排;管理端 viewer / operator 账号相关子项无账号未验。 |
| B-181 | **✅ 10-29 已上生产(`7e6b7679e`,#1849)** **知识库上传 `.md` / `.txt` / `.csv` 被认错编码或整篇入库失败**:MarkItDown 自动猜字符集(`knowledge/parsing.py`),非 ASCII 少时猜成 GBK(`¥198` → `瞼198`,上传不报错);0.1.5 只看前 4KB,前 4KB 全 ASCII 就判 `ascii`,后面出现 `—` / 弯引号整篇解析失败(EnterpriseRAG-Bench Confluence 5,189 篇里 700 篇);另吃掉行尾空格(Markdown 硬换行丢失)。修:① `.md` / `.txt` 是合法 UTF-8 就直接按 UTF-8 用(含 NUL 的不当 UTF-8),`.csv` 告诉 MarkItDown 是 UTF-8,其余照旧自动识别;② 同批修切分器(用户 10-19 定):原用 `splitlines` 拆行,文中出现换页符 / U+2028 时后面整段丢、文档照样「就绪」,改按 markdown-it 的规则拆行。改后 Confluence 重解析 0 失败,检索复核命中率不变。已坏的文档要在控制台点「重新摄取」。 |
| B-182 | **✅ 10-29 已上生产(`7e6b7679e`,#1825)** **发布脚本里金丝雀的逐项结论丢失**:本机 kubectl v1.36.1 `exec -i` 喂 stdin 时,stdin 结束后远端写的输出会丢(切回旧协议也一样);`release.sh` 把 key + `canary.py` 全文一起走 stdin,于是金丝雀判词一行不剩,只能按退出码判、失败时看不到哪项挂了。修:key 走 stdin 首行且保持 stdin 不关、`canary.py` 走 argv;没有 `RESULT: PASS` 一律判红。注意:金丝雀的 key 不能进 argv。 |
| B-183 | **✅ 10-29 已上生产(`7e6b7679e`,#1813)** **系统管理员切入别的租户后,整页刷新知识库详情页报「知识库加载失败 HTTP_404」**:刷新时 `/v1/me` 未回,`apiTenantScope` 先报本人租户,页面先按本人租户读(404)再按切入租户读(200),迟到的 404 盖住 200(整站通病,#1089 / #1098 跨租户钻取起就有);OIDC 静默续期每次把身份打回「未确认」是第二个触发点。修:① `TenantScopeGate` 包住路由页面,非本人租户范围在作用域定下前先显示占位(本人租户不等;`/v1/me` 非 401 失败按本人租户放行);② 同一用户静默续期保留已确认身份;③ 详情页加请求序号兜底。 |
| B-184 | **✅ 10-29 已上生产(`7e6b7679e`,#1814)** 一个解析进程崩溃,连带同时在解析的几份文档永久失败(`internal_error`「A process in the process pool was terminated abruptly」,单独重跑都成功;B-175 波 1 真实语料导入带出)。根因:进程池一个 worker 死掉时,每个在跑的解析都拿到 `BrokenProcessPool` 并各自「杀池 → 重建 → 重试一次」,后反应的杀掉先反应者刚重建的新池。修法:只有发现「坏掉的正是当前池」的那个解析才丢池重建,其余在新池上重试;超时路径同样处理。用例 runbook 22.12。**未完成(没查清 / 未改):** 最早那个 worker 为什么死(pod 内存上限 2Gi,疑 OOM,无直接证据);快路径失败一律终态、恢复路径按次数重试,两条路径口径不一。 |
| B-186 | **✅ 10-29 已上生产(`7e6b7679e`,#1830)** **模型输出中途停住、只拿到思考没拿到回答,被当成正常结束:run 记成功,用户收到空回复** —— 流式停顿超过 `idle_timeout_s`(默认 45 秒)时 router 用已收到的部分结束本轮(`llm/router.py`),只有思考时智能体循环当空文字回复收尾(`status=success`)。测试 60 天 7 次停顿 6 次是空结果;生产 60 天 1 次且带正文。修(用户选「报错不重试」):路由照旧交回半截并盖停顿章,主对话循环判出「只有思考」后 run 以中文提示失败,照样记账、不进缓存;规划 / 复盘仍 fail-open,结构化输出先按格式重发、分发轮先重试。注意:停顿时思考已推到前端,换模型 / 重发会让前端再推一遍。 |
| B-187 | **✅ 10-29 已上生产(`7e6b7679e`,#1835)** **模型调用的 Langfuse 记录里没有实际发出的请求参数**:平台会静默改写参数(目录强制固定温度、不支持采样参数的模型不发温度、思考档位按厂商映射,见 `agent_factory.py`),要确认配置是否生效只能读代码。修:每次调用的 Langfuse 记录带实际应答模型名和真正发出的白名单参数(温度 / 输出上限及字段名 / 思考字段,平台改写后的值,没发的不出现;备用模型、升档路由各记各的),不记密钥和消息内容;发给厂商的请求不变。改前连模型名都没有;租户 / 会话 / run 编号缺失另登 B-192。 |

11-12 核实:修复提交 `202a9eb6e`(#1836)在 10-29 生产版 `7e6b7679e` 的祖先链内(`git merge-base --is-ancestor` 通过),按已上生产移出。

| # | 事项 |
|---|---|
| B-188 | **✅ 已修(2026-10-17 登记并修,Harness-Bench 全量带出,中)新对话往用户工作区根目录写 3 个「已迁移」提示文件,还会覆盖用户自己的同名文件** —— `WorkspaceProjector.project` 在每个对话第一次投影时,不论根目录有没有文件、是谁的,都把 `LEGACY_REDIRECT_NOTE` 写进根目录 `PLAN.md` / `TODO.md` / `MEMORY.md`(本意是一次性改写方案 a 之前的旧投影)。Harness-Bench 257 题里 77 题被判「工作区多出文件」,1 题因此 0 分(其余 7 家满分);真实用户的工作区同样被塞,根目录里用户写的 / 上传的 `PLAN.md`、`MEMORY.md` 会被覆盖,违反 09-09 拍板「根目录其它文件永不触碰」。修法:只有「文件存在且第一行是旧投影的头注释(`_PLAN_NOTE` / `_READONLY_NOTE`,#492 起到 #1268 不变)」才改写;不存在、不是旧投影、已是提示的都不动;写入接口加 `read`。**存量没清**:已写进各工作区的提示文件(内容与 `LEGACY_REDIRECT_NOTE` 逐字相同)还在;生产 09-07 才上线、晚于 #1268,生产上这类文件全是本 bug 写的,可按全文相等一次性删除 —— 清不清待用户定。真栈验收 runbook §30。 |
| ~~B-189~~ | **✅ 已完成(10-17,评估后不做,用户选「保持现状」)** 截断重试再撞上限时降低思考档位:Harness-Bench 2 题思考写满 32768,B-158 重试一次又写满,run 失败。不做的理由:DeepSeek V4 不认 `low`(与 `high` 输出 token 无差,印证 STREAM-CM-DESIGN §13.1「low/medium 折成 high」);B-158 上线后测试环境 21 次重试救回 17 次,失败 4 次全是 DeepSeek 评测智能体,GLM 8/8 已救回,降档哪儿都帮不上。对 DeepSeek 唯一有效的关思考会答错(3 次错 2),放宽上限会越过租户的成本闸;频率低、代价都更大。 |
| B-190 | **✅ 10-29 已上生产(`7e6b7679e`,#1834)** **发版构建从 PyPI 官方地址下载 Python 包,国内网络慢时拖住整次发布**(10-16 约 65 KB/s,多花近一小时)。只改 uv 的 index 不生效:uv.lock 钉死每个文件的完整地址(uv 0.9.30;上游 astral-sh/uv#6349 / #20790 未发布)。修:control-plane / credential-proxy 两个 Dockerfile 加构建参数 `PYPI_MIRROR`,只改镜像里那份锁文件副本的地址前缀(仓库 uv.lock 不动,哈希照旧校验,改完还剩官方地址就失败);`tools/deploy/build-push.sh` 默认传阿里云 `https://mirrors.aliyun.com/pypi/`,`EXPERT_WORK_PYPI_MIRROR=`(设成空)退回官方源,用于镜像没同步到刚升级的版本时。CI / compose / 本地构建不传参数,行为不变。实测 control-plane 下载 25.0 s → 11.3 s。admin-ui 的 npm 源未改。 |
| B-191 | **✅ 10-29 已上生产(`7e6b7679e`,#1836)** 正常结束却既没回答也没工具调用,run 记成功、用户收到空回复(Harness-Bench 036 带出,低):DeepSeek V4 Pro 把要发的工具调用漏进了思考内容,`content` 空、`tool_calls` 空、`finish_reason = stop`;不是停顿,B-186 管不到。修法(照 B-158,用户认可):主循环回复没有正文、没有可执行的工具调用(只带解析失败的调用也算)、不是截断也没有停顿时,丢弃并落隐藏提示重试一次;重试后还是空就以 `EmptyReplyError` 可见失败。丢弃的那次照样记账、不进缓存;与 B-158 截断重试合计每步至多一次;结构化输出、分发轮、预算收尾轮不在此列。计数 `expert_work_llm_empty_reply_total{outcome=retried|failed}`。 |
| B-194 | **✅ 10-29 已上生产(`7e6b7679e`,#1842)** **回答撞上输出上限被作废后,每一步只重来 1 次**:Harness-Bench 第 2 轮 5 道题截断后重来 1 次又截断即失败,而同一步多来几次多数能写下。修:截断(B-158)和空回复(B-191)两种重试合计每步最多 3 次,第 4 次仍失败才报错(参照 Claude Code 3 次、Codex 5 次);被截的回答照旧丢弃、不执行其中工具调用,每次重试占一步;平台默认,不加配置。空回复报错文案改为「已自动重试过 3 次」。代价:写不下时多花 2 次调用。 |
| B-195 | **✅ 10-29 已上生产(`7e6b7679e`,#1843)** DeepSeek 带工具的请求要回传历史思考内容(`reasoning_content`),平台从来没回传:DeepSeek 文档要求带 `tools` 的请求之前所有轮的 `reasoning_content` 必须完整回传,否则 400;平台 `services/orchestrator/src/orchestrator/llm/providers/openai.py` 的 `_ai_message_to_openai` 只发正文和工具调用。当时接口未强制(20 次探针 + 三轮评测全 200),修掉以防厂商按文档强制。 |

11-12 核实:本条 11-05 已结案(B-236 新布局下复现不了);边缘情况的修复随 #2021 合 main,跟着 B-185 / B-243(仍在主清单,待发测试)一起验。

| # | 事项 |
|---|---|
| B-196 | **✅ 11-05 结案:B-236 新布局下复现不了;边缘情况(范围根目录不存在按空处理)随 #2021 合 main** **(2026-10-19 登记,B-175 PR3 真栈验收带出,低,待查)新建的智能体第一次跑,`search_files` / `list_dir` 报 `workspace path not found: 'agents/<智能体名>-<后缀>'`** —— 10-19 测试 `a4bc1b38` 验收 §33.3 / §33.5 时用 API 新建的临时智能体(manifest `tools: []`,仍拿到平台默认工具 exec_python / bash / read_file / search_files / list_dir / spawn_worker 等),模型调 `search_files` / `list_dir` 都报这个错,同一轮 `exec_python` 正常、`/workspace` 是空的;run `687d9eb3`、`6d73a4f7`。像是新智能体的工作区目录还没建、文件类工具就去读。待查:是否只在「从没写过文件的新智能体」上出现、控制台建的智能体是否同样;另 `tools: []` 仍拿到默认工具是否符合设计(若是,控制台 / 文档应说明)。 |
| B-197 | **✅ 10-29 已上生产(`7e6b7679e`,#1854)** **文档正文里有 `<|endoftext|>` 这类分词器特殊记号,整篇入库失败**:切分器用 tiktoken 数 token / 硬切 / 取重叠(`knowledge/chunking.py`),默认遇特殊记号抛 `ValueError`,「重新摄取」也照样失败;`cl100k_base` 有 5 个:`<|endoftext|>` `<|fim_prefix|>` `<|fim_middle|>` `<|fim_suffix|>` `<|endofprompt|>`,讲模型 / 分词器的技术文档、导出的对话记录都可能带。修:切分器三处编码统一走 `_encode()`,传 `disallowed_special=()`,当普通文字切(智能体侧 `expert_work/runtime/tokens.py` 早已如此)。已失败的文档发版后在控制台点「重新摄取」。
| B-198 | **✅ 10-29 已上生产(`7e6b7679e`,#1856)** **一段文字下一行紧跟 `---` / `===`,整段被当成标题:内容不进正文,重排大面积失败** —— 切分器按 markdown-it 认标题(`knowledge/chunking.py` `_parse_blocks`),「段落 + 下一行 `---`」不论多少行都算二级标题:文字只在标题路径上(没正文时彻底丢),每段都带巨型标题淹没嵌入与关键词;送重排超过模型单条上限(qwen3-rerank 4,000 token / qwen3-vl-rerank 8,000 token)时阿里云整次回 400,退回不重排(`knowledge.rerank_failed`)。会议纪要、导出的聊天记录、带 YAML 段的文档常见。修:① 标题只认单行且不超过 200 字的,否则当正文;② `DashScopeReranker` 每份候选截到 3,000 字再送(兜底修复前入库的段)。受影响的文档要「重新摄取」才能找回正文。
| B-199 | **✅ 10-29 已上生产(`7e6b7679e`,#1857;依赖 #1856)** 切分规则对齐 Dify / RAGFlow / Onyx(B-198 后续):① `.txt` 按纯文本、按空行分段,`#`、`---`、缩进都不当结构(`.md` 和转换来的格式照旧按 Markdown);② 关掉 markdown-it 的下划线式标题,只认 `#` 标题;③ 切分不丢字:markdown-it 吞掉的行(`[名字]: 网址` 链接定义)和下面没有正文的标题都留作正文;④ 按 token 硬切时退到词边界(此前 EnterpriseRAG-Bench 抽样 3,000 篇里 25 篇有词被切成两半,如 `Triage` → `T` + `riage`,关键词搜不到)。效果:抽样 3,000 篇「每个词都完整出现」2,975 → 3,000;华大检索复核不变或略好(主集 MRR@10 0.903 → 0.906)。txt 改纯文本对检索的影响要在下次 EnterpriseRAG-Bench 评测里看。
| B-200 | **✅ 10-29 已上生产(`7e6b7679e`,#1858;依赖 #1856 #1857)** 送嵌入 / 重排的文本都加上限(B-198 后续第 4 条):① 段长上限管整段嵌入文本(文档标题 + 标题路径 + 重叠 + 正文),标题路径超上限四分之一先丢最外层;② 表格单行超上限也切开、每块带表头(华大整库超限段 795 → 0);③ 送嵌入前每条截到 8,000 字节(`EXPERT_WORK_EMBEDDING_MAX_INPUT_BYTES`,可调)—— 阿里云 text-embedding-v4 单条超 16,000 token 整次请求 400(文档写的 8,192 不是硬限);按字节而非 token 截,任何模型、任何文字都管得住;④ 送重排的候选截到 3,000 字,从阿里云适配器移到调用方(知识检索两种视图 + 记忆召回)。代价:段划分几乎全变,点「重新摄取」要重新嵌入、按量计费,不点不受影响。带出回归:标题路径过长时从最外层丢标题,而标题文字只在路径里 → 文字消失(3 道题答案丢失,占 MRR 损失 46%);用户拍板不热修,并入 B-206 K-3(知识库优化总计划步 2)。 |
| B-201 | **✅ 10-29 已上生产(`7e6b7679e`,#1869;B-201~B-203 三批里的批 1)** **查 Langfuse 会卡住后台,会话轨迹「Langfuse 精确」与「成本」长期无数据**:① trace 查询在异步接口里同步调 Langfuse(每次 4–5 秒),整个 control-plane 副本请求排队;② SDK 默认 5 秒超时常 `ReadTimeout`,被吞成 `unavailable` 不留日志;③ langfuse SDK 4.x(#1233 升级起)默认只导出它认定与大模型相关的 span,平台 `expert-work` tracer 的 span 全被丢,trace 只剩孤立的 `llm_call`,页面永远「入库中」。修:查询挪到线程;超时 30 秒且每次请求带上(SDK 只认首次构造参数),失败分支记 warning(只记 trace id 与异常类名);导出过滤加回平台 span,只读请求(GET / HEAD / OPTIONS)的请求 span 不导出(否则一天多约 6.4 万条单 span trace)。用户拍板:运行结束 2 分钟后 trace 仍不完整,接口返回 `incomplete`、页面显示「数据不完整」不再重试;系统管理员另有「去 Langfuse 查看」链接(Langfuse 不分租户)。修复前的 trace 补不回来。 |
| B-202 | **✅ 10-29 已上生产(`7e6b7679e`,#1870)** **会话详情页轨迹显示错误 6 处**:① 过程条时长只加了「思考」行,直接调工具的步骤漏算 → 改为全部模型调用 + 工具耗时(子智能体行不重复计),**用户拍板「思考 N 次」改「模型调用 N 次」**;② 轨迹一长右侧详情面板停在顶部 → 补视口高外框;③ 「概要」预览按固定高度裁在表格中间 → 真被裁才渐隐并给「查看全文」;④ 只调工具的模型步骤「预览」「原文」全空 → 列出调了哪些工具和参数;⑤ 「原始」页显示浏览器回放时刻 → 用帧 id 里的服务器时间,没带 id 的帧标「收到」;⑥ 「请求 #N」跨轮累计 → 按轮编号。**用户拍板成本用平台价目表、按人民币算**:「计时」页与会话详情页统计条 / 轮脚 / 轨迹都接价目表(Langfuse 美元列保留),与调试台同一道闸(价目表只有 system_admin 读得到,租户用户不显示成本)。 |
| B-203 | **✅ 10-29 已上生产(`7e6b7679e`,#1871,批 3)** 轨迹右侧详情面板体验 6 处 + 运行详情页布局 + 工具行对不上 Langfuse:① 计时页标签留边、加「开始时刻」、tokens 标「输入 / 输出」带千分位;② 工具行的「模型 / tokens / 成本」只给模型调用;看图工具用量随工具结果回来(`vl_usage`),单列「看图模型 tokens」(帧里没有看图模型名,不写模型、不算成本);③ 「Assistant Message」「Tool Call」补中文;④ 系统提示词行、用户输入行不写时长;⑤ token 数统一带千分位;⑥ 工具「结果」页直接摊开、不再列重复的入参;⑦ 运行详情页(`/runs/…`)轨迹用与 B-202 同一个定高外框;⑧ 工具行在「计时」页永远「无法与 span 对齐」:门面给所有工具 span 的 label 都是「工具调用」、工具名在 detail 里,改按 detail(模型调用时的原名,MCP 带前缀)配。 |
| B-207 | **✅ 10-29 已上生产(`7e6b7679e`;手册 #1883)** **RDS PostgreSQL 16 → 17 大版本升级**(起因:B-206 K-9 要用的 BM25 插件 `pg_textsearch` 在阿里云 RDS 只有 PG 17 / 18)。生产只读 3 分 58 秒、行数逐行一致,已装 `pg_textsearch` 1.3.1。手册 `docs/runbooks/rds-pg-major-upgrade.md`,脚本 `tools/deploy/pg_upgrade/`(只读预检查 / 停写前后快照 / 升级后 ANALYZE / 金丝雀 / 窗口用工具 pod)。方式(用户 10-21 定,测试生产同一种):本地升级;不用零停机(要求每表有主键,`vector.rabitq_info`、`keycloak.databasechangelog` 无主键且不归我们管)。**不能原地降级**:回退 = 用升级前自动全量备份恢复出 16 的新实例 + 改 4 份连接配置。停写方:control-plane / credential-proxy / keycloak / langfuse-web / langfuse-worker + retention-cleanup CronJob(wecom-adapter 不连库)。Keycloak 25.0 官方只测到 PG 16,升大版本后必验登录。 |
| B-208 | **✅ 10-29 已上生产(`7e6b7679e`,随 PG 17 升级 B-207,无代码改动)** PG 16 上向量索引(HNSW)返回的邻居大多是错的:测试库升级前 6 条探针与精确结果重合只有 1~6/10,升到 PG 17 后全部 10/10;生产 PG 16 的 `memory_item` 强制走 HNSW 只对 60%,指向阿里云 pgvector 0.8.0.2 在 PG 16 上本身的问题(不是已删条目堆积)。当时生产用户不受影响:应用的记忆召回带租户 + 用户过滤,规划器走 btree + 精确排序,不用 HNSW(100%);生产知识库 0 行。生产升 PG 17 后记忆向量召回 `[A]` 60% → 100%;升级手册 §6 / §9 已加「升级后 `[A]` 必须 100%」(检查脚本 `tools/deploy/pg_upgrade/vector_recall_check.sh`)。ERB 70.9 不受影响(本机容器 + 精确检索,没用 HNSW)。 |
| B-209 | **✅ 10-29 已上生产(`7e6b7679e`,PR-1 #1898 + PR-2 #1910;同批 #1911 / #1912 / #1916)** 知识库不处理图片:PDF 里的图直接丢,Word 里的图变成无意义占位符进分段,纯图片 Word / PPT 不报错存成一段占位符,纯扫描 PDF `parse_failed`;另有混合 PDF 扫描页静默为空(`_is_sparse` 按整份平均)。设计 `docs/superpowers/specs/2026-10-21-b209-kb-scan-ocr-and-figures-design.md`(10-21 拍板):托管看图模型逐页自判、费用记上传租户、不收单独图片、保留 PyMuPDF(法务可接受)、模型平台级配置;默认 glm-ocr(错字率中位 1.8%、¥0.4/千页)+ qwen3-vl-plus 兜底,qwen-vl-ocr / qwen3.5-ocr 双栏漏整栏不用。PR-1 #1898 扫描页逐页识别,识别缓存按文档、删文档即删识别文字;PR-2 #1910 文档内图片描述;同批 #1911 读文档不再带原件、#1912 单文档字数上限 + 入库内存预算(后续 B-214)、#1916 图片刷新回 304(B-213)。**未完成:** 多份大扫描件同时入库时 glm-ocr 并发打满、大部分页退到兜底模型(10-23 观察:217 页里 184 页,整轮约 35 分钟,功能正确)—— 识别按租户公平调度 / 每日配额,排期见知识库优化总计划步 1'。 |
| B-210 | **✅ 10-29 已上生产(`7e6b7679e`,#1898)** **模型目录把已下架的 `qwen3-vl-flash` 仍列为在售看图模型**:我们的阿里账号对它及两个快照都回 `404 model_not_found`,配置页却能选、选了就 404。修:`MODEL_CATALOG["qwen"]` 里标 `deprecated=True`(保留可解析),配置页不再列出;当前在售能看图的 flash 档是 `qwen3.7-flash` / `qwen3.8-flash`(B-209)。|
| B-211 | **✅ 10-29 已上生产(`7e6b7679e`,#1929;测试沙箱 `daf63cb1` 起生效)** **沙箱镜像构建按测速选 Debian 源**:`infra/sandbox-image/Dockerfile` 的 apt 源原写死 mirrors.aliyun.com(约 280 个包);控制面镜像发布构建已由 #1915 改成测速选源。修:加 `ARG APT_MIRROR`(默认仍阿里云,行为不变),测速选源只接本机 `make -C infra build-sandbox`。**CI 不接**:五个 workflow 共用一份层缓存,且测的必须是推上去的那份,各 job 各测会各选各的源。随 brotli 换沙箱镜像一起改(单独改 Dockerfile 会触发约 54 分钟冷建并多出一版沙箱镜像)。 |
| B-212 | **✅ 10-29 已上生产(`7e6b7679e`,#1932)** **沙箱运行时 pip 主备源都是阿里云,且备源拖慢安装**:主源阿里云内网镜像 + 备源阿里云公网镜像,同一家会一起挂。10-24 沙箱实测:pip 遇到同一文件两源都有时**下载用的是备源**,阿里云公网 382 KiB/s,scikit-learn 21.5s(只配内网 1.8s);出网代理放行腾讯云 / 中科大(清华回 403)。修(用户 10-24 同意):备源换腾讯云,`PIP_TRUSTED_HOST` 只留内网;测试 / 生产两个 overlay 的 `EXPERT_WORK_SANDBOX_PIP_*` 一起改(`tools/deploy/test_sandbox_pip_mirror.py` 同步)。手册 §53。 |
| B-213 | **✅ 10-29 已上生产(`7e6b7679e`,#1916)** 原图缩略图经负载均衡永远拿不到 304:负载均衡压缩 JPEG 时把 `ETag: "id"` 改成弱形式 `W/"id"`,接口做字符串完全相等比较,于是每次整张图 200。只影响带宽和缓存。修:按 RFC 9110 弱比较(去掉 `W/`、支持逗号分隔多个值)。|
| B-214 | **✅ 10-29 已上生产(`7e6b7679e`,#1918;重新摄取路径补修 B-216 #1919)** **入库内存预算按字数算,内存却跟段数走**:每段约 64 KiB(1,024 维向量按 Python 对象存 + 行对象),短条目(FAQ / 词条,约 43 字一段)的 250 万字文档估约 3.6 GiB,超 4 GiB pod 上限,pod 被杀、同 pod 对话全断;单文档上限与并发预算都按字数,挡不住。修:向量存 `array("d")`、嵌入按 200 段一窗、行按 500 条分批写入(每段约 11 KiB);新设置 `knowledge_document_max_chunks`(默认 40,000),嵌入前查段数,超了报 `too_many_chunks`(终态,不调嵌入);并发预算取 max(字数, 段数折算)。测试环境 9,917 段:+616 MiB → +196 MiB。|
| B-215 | **✅ 10-29 已上生产(`7e6b7679e`,#1918)** 重新摄取 / 补识别会把语义断点的嵌入整份重算、再记一次费:切块找语义断点时每一块都重新嵌入、不缓存,大文档每次重新摄取约花首次嵌入费的四成多(250 万字 PDF 首次 1,536,106 token,补识别又记 683,103 token)。修法:语义断点判定按文档 + 嵌入模型缓存进 `knowledge_parse_cache`(kind `semantic_break`,迁移 0173),文字没变的重新摄取 / 补识别不再嵌入,改了的只嵌改动处相邻的块。改后 9,917 段重新摄取 81 秒、0 token(首次 12.6 分钟、400 万 token)。 |
| B-216 | **✅ 10-29 已上生产(`7e6b7679e`,#1919)** 重新摄取 / 补识别读回已存向量时,`chunk_embeddings_by_hash` 一条查询读回整份文档的向量并逐条转成 1,024 个 Python 浮点的元组:9,917 段的 Word 重新摄取峰值 +597 MiB(首次入库的 3 倍;首次入库已由 #1918 改成 8 字节数组),按段数推算 4 万段约 +2.4 GiB,逼近 pod 4 GiB 上限。修:按内容哈希每 500 个一批查,读回即转 8 字节数组,入库不再复制;同一文档重新摄取峰值 +597 → +11 MiB。 |
| B-222 | **✅ 10-29 已上生产(`7e6b7679e`,#1960 #1968 #1970;带出 B-237 / B-238)** **知识库入库前先预估费用,用户确认后才开始**。**用户拍板:无论金额大小(含 ¥0.00)都要用户确认才开始**。上传、同名重传、重新摄取、补识别、批量重摄先进 `estimating`(本地解析 + 计数,不调模型不花钱),再停在 `awaiting_confirmation`;按平台价目表折成识别 / 图片描述 / 向量化各项金额,平台出钱的项(K-11 补上下文)不列给租户;同库内容相同的副本标出、不计费(K-21);解析结果存快照(迁移 0179),确认后不再解析。接口:文档带 `ingest_estimate` / `confirm_by` / `ingest_actual`,列表返回 `estimate_summary`;新 `POST …/documents/confirm`(按 `(document_id, estimate_id)` CAS 或全部确认,条数 / 总额与用户所见不符回 409)、`POST …/documents/{id}/estimate`(副本仍要入库);待确认最长 7 天,过期自动撤回;确认后真花钱前再估一次,不成立就退回待确认。#1960:入库中可取消,取消即停止花钱。换嵌入模型重建只重嵌不在新模型上的分段。对标:Dify 预估只给分段数不给价格。 |
| B-223 | **✅ 10-29 已上生产(`7e6b7679e`,#1949)** 技能包允许 `.pdf`(用户「pdf 加进白名单」):报告类技能包里带固定附页 PDF 被 400 拒收。改法:`.pdf` 加进 `ALLOWED_EXTENSIONS`,按二进制处理(同 pptx / 字体:沙箱里用、不做文本扫描、受大小上限约束);`docs/runbooks/skill-packaging.md` 过时的上限表改成现值。 |
| B-225 | **✅ 10-29 已上生产(`7e6b7679e`,#1953)** `read_file` 碰到技能目录时报错直接给出能用的读法:模型用 `read_file` 读 `../opt/skills/…` 被工作区限制拒掉,白花一步。技能文件在沙箱 `$EXPERT_WORK_SKILLS_DIR`(`/opt/skills/<agent_key>/`)下、不在工作区里,这是有意的。修:`file_ops._require_path` 拒绝的路径含 `opt/skills/` 时追加提示「用 skill_view(skill_name, path) 读,或在 bash 里到 `$EXPERT_WORK_SKILLS_DIR/<技能名>/` 下读或运行」;read_file / write_file / edit_file / list_dir 共用这一处。同一会话另有技能自身问题(PDF 导出脚本依赖沙箱不装的 Chrome,应改用 weasyprint),已告知用户。 |
| B-226 | **✅ 10-29 已上生产(`7e6b7679e`,#1959 #1961 #1963 #1964;深护智康 project-service 同晚上线适配)** **运行没做完时,答复和对外状态要如实**:B-225 会话 49 步用满上限、0 个交付物,模型却答「交付文件…已核验」,run 记 `success`;测试环境 30 天 6,043 次运行里约 150 次「显示成功但没做完」(中途报错未解决 105、脚本最后失败仍判完成 46)。修(都不解读模型写的文字):① 收尾提示带平台记录的事实(登记了几个交付物 / 几个失败没解决 / 几个计划步骤没完成),只有登记成功的文件算交付,结束原因如实,收尾提示存进历史;② 剩约 1/6 步数时 hermes 式预警「先存进展再继续」;③ 软失败入账:脚本非 0 退出 / 超时、外部工具 `isError`、HTTP ≥ 400、子智能体失败或被拒;④ 原地打转被拦不再记正常完成,按整次 run 累计(配 N 第 N 次停,0 = 第 2 次),计划自动打勾前看有无未解决失败;⑤ 审批被拒、动作被拦截也入账。**对外变化**(#1964,迁移 0178;文档 webhook 页与更新日志 #1961):新增 `status=incomplete`(仍触发 webhook `run.completed`),listRuns、会话条目 `runs[]`、webhook 带 `completed` / `exit_reason` 与 `unresolved_failures` 清单,listRuns 可按状态筛选。后续:B-227~B-234、B-240。另记(不在本条):运行时限在主循环不生效(`deadline_at` 只到委派工具)、可用截断的回复仍记完成、续跑接手时交付物清单可能少报。 |
| B-235 | **✅ 10-29 已上生产(`7e6b7679e`,#1971)** 产物每个版本单独留一份,对话页显示本会话那一版:`save_artifact` 原来只登记路径不复制字节,同一用户同一智能体两段会话重叠且写同名文件时旧版本内容丢失,会话 1 的对话页下到会话 2 的报告。改法(迁移 0180):① 登记时把字节复制到沙箱挂不到的 `{租户}/.artifact-versions/{用户}/{版本 id}`,大小 / sha256 当场写进版本行;复制失败、超 64 MiB 或读的过程中文件在变都报错不登记;② 对话页 / 调试台按这一轮登记的版本号下载,控制台版本列表逐版可下;③ **对外下载 `version` 改成「取这一版」**(只增不破:不存在的版本与上线前登记的非最新版仍 409,加 `X-Artifact-Version` 头;已通知深护智康,对方确认不受影响);④ 版本到期、软删到期、清除用户、已删工作区销账一起删副本,每天清孤儿副本;顺带修留存作业误删新版本还在用的工作区文件。用户拍板:对话页遇到上线前登记、已不是最新的那一版,提示「内容没有单独保留」并给「下载最新版」,绝不悄悄换成最新版;副本不计入每用户工作区配额。不在本条:每会话独立工作目录(B-236)、文件名唯一(对接方配置);「写完文件、登记之前被另一会话覆盖」平台管不到。 |
| B-237 | **✅ 11-04 已上生产(`91e8daf02`,#1993,范围收窄为可观测)** **扫描件识别大量落到备用模型** —— 测试一份 40 页扫描件第二轮入库,32 页识别里 25 页走了备用 qwen3.7-flash(单页约是 glm-ocr 的 140 倍)。#1993:转备用时按引擎记原因日志 `knowledge.recognition_fallback` + 计数 `expert_work_knowledge_recognition_fallback_total{reason}`,厂商繁忙类错误带原因(rate_limited / timeout / server_error 等)。**未完成:** 真转备用的情况还没在真栈出现过;出现后按记下的原因再定是否调重试或限速。 |
| B-238 | **✅ 11-04 已上生产(`91e8daf02`,#1993)** 取消后重新入库的估算没扣掉已识别的页:取消留下的识别缓存不会再收费,但估算只认上次完成入库的页记录,显示的识别页数偏高(估高、不多收)。修:估算时把缓存命中算进 `reused_units`。 |
| B-239 | **✅ 11-04 已上生产(`91e8daf02`,#1994)** **用户详情「产物」页不带租户** —— `apps/admin-ui/src/pages/user_detail/ArtifactsPane.tsx` 调 `listArtifacts` / `listArtifactVersions` / `downloadArtifact` 不传租户,系统管理员切到别的租户后显示「该用户暂无产物」。修后切入租户能看到产物,写操作置灰、下载可用。 |
| B-241 | **✅ 11-04 已上生产(`91e8daf02`,#1994)** 知识库维护两处口径:① 丢弃「没套回去的内容」时确认框按条目计数,接口返回与审计按修订行计数(`discarded:9`),已对齐;② 修订历史的作者从用户编号改为显示登录邮箱。 |
| B-256 | **✅ 11-04 已上生产(`91e8daf02`,#1996)** 换了识别模型后「重新摄取」估低(B-238 审查带出):估算把上次记为「已识别」的页一律算免费,不看当时是哪个模型 / 提示词版本 / 渲染参数读的,换模型后实际整份重新收费。修法:只认缓存里当前读法的那一行,逐页记录不再算数;B-238 之前识别的页第一次重新入库时补标记(那一次按收费估,方向安全)。顺带:读法指纹加进渲染库(PyMuPDF / MuPDF)版本(只加进读法指纹,不加进缓存键,缓存键本身按图片字节算)。 |
| B-257 | **✅ 11-04 已上生产(`91e8daf02`,#1995;默认档位后由 B-265 改「低」)** **看图「深入看」太慢**:测试 30 天深入看 p50 43 秒、p90 107 秒,约 23% 等满 120 秒超时;探针(豆包 seed-2.1-pro)不设档位 ≈ 高 95–123 秒、中 58–63 秒、低 22–44 秒,四档答案全对。修(用户拍板):① 有快速看路由时深入看最多等 60 秒就改快速看(没有仍 120 秒);② 看图模型没设思考档位时深入看给默认档位(按目录思考形态翻译,显式配置优先;主模型及其备用、快速看不变;看图模型的备用同样给默认);③ 有快速看路由时深入看首 token 超时 30 秒;默认档位或快速看被厂商 4xx 拒时,用配置原样的路由再看一次。 |
| B-259 | **✅ 11-04 已上生产(`91e8daf02`,#2002)** 改 MCP 目录会打断所有平台 MCP 连接:平台目录任一次新建 / 修改 / 删除(连只改说明)都关掉整个平台池,所有副本上的平台 MCP 连接被硬停,正在跑的运行之后调平台 MCP 工具都报 `not started`(10-30 事故同款);租户 MCP 服务改动、租户开通 / 取消平台服务同样整池关掉该租户。修:按前后两份记录分类(旧值在更新事务行锁下读,B-248 `update_with_previous`)—— 只改展示字段什么都不动;改连接配置只退役该项(在途调用做完);停用 / 删除只硬停该项;新建 / 重新启用只清构建;同池其它项的长连接按连接指纹复用;oauth2 目录项不再碰平台池,允许清单改动只清该租户构建。新跨副本事件 `platform_mcp_server_changed` / `_removed`、`tenant_mcp_server_changed` / `_removed`;老 pod 忽略新事件,旧整池事件保留兼容;credential-proxy 同时订阅这四个,未更新前最多 60 秒旧值。带出 B-264(目录超时改不回默认)。 |
| B-260 | **✅ 11-04 已上生产(`91e8daf02`,#2000)** **智能体缓存没有代次保护**:`AgentRuntime.get_agent` 缓存构建好的智能体 30 分钟,构建期间若来了 `invalidate_all` / `invalidate_tenant` / `invalidate_user`(MCP 池关掉客户端、缓存清空),构建完又把拿着已关闭客户端的智能体写回缓存,之后 30 分钟每次 run 报 `StreamableHttpMCPClient … not started`(10-30 生产故障同款症状,那次的直接原因由 #1979 修)。修:按全局 / 租户 / 用户三级记失效代次,定键前记下、入缓存前比对,变了丢弃重建,最多 3 次(第 3 次交出但不入缓存);`invalidate_*` 签名不变。子智能体构建缓存(`subagent_runtime.make_child_agent_builder`)同一套闸;主智能体构建后核对用户 OAuth 池仍是定键时那个对象才入缓存(防授权工具落到全租户共享键)。§82.5 次日核对(11-05):两个 control-plane 发布后约 17 小时 `not started` 0 次(发版前 24 小时基线也是 0),`invalidated_mid_build` 各 1 次(闸生效、重建成功)。 |
| B-262 | **✅ 11-04 P0 / P1 / P3 已上生产(`91e8daf02`,P0 #2001、P1+P3 #2008)** 同一智能体的对话共用上传 / 输入 / 工具结果 / 长期记忆(B-242 安全审查带出):对接方一个平台用户下跑很多终端客户(一客户一对话),已会串客户的两处:① 客户 B 的同名 `report.pdf` 覆盖客户 A 的,A 的 `upload_id` 下载到 B 的内容;② `ask_image` 按页看时扫智能体下全部 run,A 能拿到 B 在同名文档上渲的页。P0:同名不同内容的上传落成 `<原名>-<内容 sha256 前 8 位>`(临时文件再 `link`,原子、不覆盖;同名同内容复用、不重复记配额),上传响应 / 登记行 / 审计 / `[file attached: …]` / 下载文件名一律是实际名字,对外响应字段不变(对接文档与更新记录已改、已通知深护智康);看图按页只看本对话渲染过页的 run(取自检查点 `viewed_figures`,子代并上父的)。P1+P3(方案 `docs/superpowers/specs/2026-11-02-b262-p1-p3-conversation-platform-dirs.md`):上传 / 输入(含预拉缓存)/ 工具结果移进 `threads/<conv>/work/`,模型路径不变;引用别的会话 / 别的 agent 的上传复制进本会话(用户拍板,计配额、记审计,原会话删了 404、文件被改 409);手写看图 ref 必须规范且只许本对话;老对话下一次新 run 迁移(续跑不迁移)。P1 不能单独发。**未完成:** P2 长期记忆只读、走平台写入(扫描 + 审计 + 留版本,禁脚本)待排期。 |
| B-263 | **✅ 11-04 已上生产(`91e8daf02`,#2009,与 B-266 同 PR)** **上传的照片丢方向、降画质**:为去 EXIF 隐私信息重编码时,JPEG 没指定质量(Pillow 默认 75),且删了方向标记却没先转正(`_image_sanitize.py`、`uploads.py`),竖拍化验单可能横着送给看图模型。修:按 EXIF 方向转正后再去元数据;JPEG 92 / WebP 90,重编码超字节上限依次降 85 / 75,仍超回 413;超 6000 万像素的 JPEG 用 draft 缩小后保存(约 1.79 亿像素以内),其它格式 413(以前超大图 500)。 |
| B-265 | **✅ 11-04 已上生产(`91e8daf02`,#2007)** 看图「深入看」默认「中」档常超过 60 秒、退成快速看。修:深入看默认档改「低」,60 秒上限 + 超时退快看不变(用户 11-02 拍板)。实测(豆包 seed-2.1-pro,729 次,约 ¥25):快看 / 低 / 中准确率同为 99–100%;难图超 60 秒:低 8%、中 15%、高 50%;耗时几乎全由思考长度决定(r²=0.97)。只测了豆包;kimi-k3 / glm 原先「中」被顶成「高」,这次是高→低,未单测。 |
| B-266 | **✅ 11-04 已上生产(`91e8daf02`,#2009;带出 B-272 / B-273)** **看图「放大局部」**(用户 11-02 提出)。实测:整页快看小字读对 10%,裁约 1/6 区域原像素快看 100%,放大 2 倍无增益;厂商把任何图都压到约 1,400 输入 token,裁剪等于同样预算看更小面积。**用户拍板**:按面积缩图取消(厂商自己降采样,先缩只省流量);做 `region`。方案 `docs/superpowers/specs/2026-11-03-b266-region-zoom.md`:小图(< 400 万像素,文档按 300 dpi 计)直接整图;大图切 3–8 条整宽横条(过宽过长按长边切)并行快看 + 同时看一次整图,条上 JSON 回答,读数一致用条,冲突 / 都没找到 / 有条被截断用整图并标不确定(具体小值 98% vs 整图 44%);「找不到」与读数比较不换算单位、不放宽误差、不投票;`ask_image` 新增 `region=[x1,y1,x2,y2]`(0–1000)指定区域;主模型能看图的走 `read_page(region)`。glm-5.3 写不出 `region` 数组见 B-272,计数题条上误报 `found` 见 B-273。 |
| B-267 | **✅ 11-04 已上生产(`91e8daf02`,#2012;执行单 `docs/runbooks/2026-11-04-prod-release-checklist.md`)** **绑了知识库却去搜网**:提示词为空、绑 1 个知识库且有 `web_search` 的智能体,问库里有的书「谁写的」只调了 `web_search`。原因:`knowledge_search` 说明笼统、单库不展示目录(`MIN_CATALOG_BASES = 2`)、没有平台「先查知识库」规则。三步都已上线(方案 `docs/superpowers/specs/2026-11-03-b267-kb-first-routing.md`,实现记录 `docs/superpowers/specs/2026-11-03-batch-b267-b273.md`):① 平台先查规则(即使像常识也先查)+ 单库也展示目录;② 自动知识库画像(只在用户确认入库费用后刷新,费用进 B-222 确认;老库补画像先预演报价);③ 每轮首步关键词 + 向量轻量探测,命中只给提示不塞段落。开关:平台总开关 `EXPERT_WORK_KB_FIRST_ROUTING` 默认开(关 = 三步都不生效,兼作应急回退);智能体可设 `knowledge.probe: false` 单独关探测。搜网替身 `EXPERT_WORK_WEB_SEARCH_FIXTURES_DIR` 只在测试环境配,生产选夹具会构建失败。迁移 0182 加 `knowledge_base.profile`。回滚注意:旧镜像期间保存会丢掉关闭探测的设置;新镜像算的入库预估旧镜像读不懂,回滚后待确认文档要重新预估。 |
| B-268 | **✅ 11-04 已上生产(`91e8daf02`,#2012,11-03 批次;方案 `docs/superpowers/specs/2026-11-03-batch-b267-b273.md`)** 看文档页来回重看、一轮跑几十分钟(生产会话 `e909d886`):子代对一份 4 页 PDF 按 4→1→2→3→4→1… 循环调 `read_page` 20 多次,每次输入约 7 万 token、单步 1.5–3 分钟。根因:内联页面只保留最近 3 张(`FIGURE_KEEP_RECENT = 3`),工作集 4 > 窗口 3 来回抖动,退出提示又叫模型「需要重看就调用 read_page」。修法:正在读的那份文档最多 6 页整页留在视野里(其它文档仍 3 张),区域裁剪单独 2 格、每文档 12 次预算;每 run 记读账:重看给回执、超额提示,同一项第 4 次读或一份文档整页渲染超 2×页数+3 次即拒(算一次循环熔断);退出提示不再点名要拉哪一页;读次数跨审批续跑按本轮根 run 计。 |
| B-271 | **✅ 11-04 已上生产(`91e8daf02`,#2012)** 上传过期清理实际删不到文件:留存作业按 `{用户根}/uploads/<名字>` 删,而 B-50 起上传在 `agents/<key>/uploads/`、B-262 起在对话 `work/uploads/`,从没删到(行照样软删,文件永远留着)。修:按会话当前 `agent_name` / `workspace_layout` 算真实位置,目录句柄逐层打开(`O_NOFOLLOW`)、删前核对是同一文件;还有未到期行指向同一文件则不删(应对 B-262 同名同内容复用);大小或新列 `user_upload.sha256` 不符则不删;用户目录内路径打不开则行照常过期、文件保留;超过 3 个用户出问题或存储根打不开则整段停下,作业以退出码 3 结束。方案 `docs/superpowers/specs/2026-11-03-batch-b267-b273.md`。写明不处理:控制台上传 / 引用复制不建登记行、永不过期;迁移后留在 agent 根的原件另立。 |
| B-272 | **✅ 11-04 已上生产(`91e8daf02`,#2012;执行单 `docs/runbooks/2026-11-04-prod-release-checklist.md`)** **glm-5.3 传不了 `ask_image` 的指定区域**:`region` 的 schema 是「字符串 `auto` 或 4 个整数的数组」,生产主力 glm-5.3 每次把框写成字符串 `"[0, 325, 1000, 512]"`,被参数校验拒(`$.region (anyOf)`)。修:在统一校验层(`_validate_tool_args`)只在校验已失败时,把顶层字符串参数 `json.loads` 成 list / dict 放进副本、用同一 schema 重新校验,全部合格才调用,否则返回原错误(不回显值);不改 schema,所有带数组 / 对象参数的工具(含 MCP)同样受益;计数 `expert_work_tool_args_coerced_total{tool}`;轨迹 / 审计记模型原样给的参数。 |
| B-273 | **✅ 11-04 已上生产(`91e8daf02`,#2012,11-03 批次;方案 `docs/superpowers/specs/2026-11-03-batch-b267-b273.md`)** 计数 / 整体类问题在切条上会误报 `found`:长截图问「提醒了几次」,各条各报局部数,若碰巧一致就会当成条上读数返回错数(评测:整体题单条答 `found` 111 题错 54 且不标)。修法:`region="auto"` 按措辞把整体题(计数、合计、最高 / 最低、排名、趋势;中英词表,防误伤测量名 / 字段名)路由到一次整图读(99.1%,具体题不变);命中时条带照跑,若条带一致给出整图答案缺的值,答案标不确定并附条带读数;多条一致只在两条重叠条看到同一行时才采信。 |
| B-274 | **✅ 11-04 已上生产(`91e8daf02`,#2013)** **对话页历史轮把附件显示成用户说的话** —— 平台在用户消息里写附件标记告诉模型(对外接口 `files[]` → 正文后 `[file attached: uploads/<名字>]`;不能看图的模型再加 `[image attached: expert_work://image/…]`;调试台在正文前加「[已上传文档 —— 用 read_document 读取]」),检查点存的就是这条消息,对话页 / 运行详情 / 调试台历史轮原样显示。修(只改管理端,`turn/attachment_markers.ts`):只认平台写得出的形状,只摘正文末尾(不可信内容块之前)的标记行和开头的调试台说明,显示成附件标签;用户自己写的同样字样、不可信内容块里的一律不动(第三方可控);历史轮「重试」带回附件;导出 JSON 带 `attachments`。**不改**:对外接口返回的消息原文(对接方在用);能直接看图的模型那一轮历史仍没有图片标签,重试也带不回这些图片;图片标签显示图片编号不是原文件名。 |
| B-276 | **✅ 11-04 已上生产(`91e8daf02`,#2015)** 不能看图的主模型(glm-5.3)反复重做看图子代(glm-5.3-flash)的结果:子代交回后主模型自己 `read_page`、在沙箱 `pip install` OCR 再识别,两次把运行拖到 12 分钟上限。原因:`spawn_worker` 描述末句无条件要求「verify key conclusions here」,且委派说明没告诉主模型「子代能看图、你不能」。修:① 核对句改为「只做顺手能做的检查(数字对得上、写的文件在),不绕路重做;可疑就再派子代复查那一项」;② 只在主模型不能看图且子代能看图时加一句「看图 / 看页交给子代,别自己装 OCR 或渲染」(按能力门控)。改前后各 6 次对比:子代交回后主模型重做看图 4/5 → 0/6,截停或没做完 3/6 → 0/6,全对 3/6 → 6/6,主模型花费 ¥3.67 → ¥2.06;改后主模型仍做顺手核对,没有盲信。 |
| B-236 | **✅ 11-04 已上生产(`91e8daf02`,#1976 #1983,随附 B-244 / B-245 修复 #1985;执行单 `docs/runbooks/2026-11-04-prod-release-checklist.md`)** **对话独立工作目录**:每个对话有自己的 `/workspace`,`/agent` 是智能体级共享区;老布局的兼容目录只读挂载(文件工具写入被拒并指向对话目录或 `/agent`),兼容窗口在 overlay 里配。工作区写锁改为按对话(#1983):10-30 生产同一用户 15 个并发报告 run 在按用户的锁上排队,写小文件要 3~6 分钟;新布局按对话串行,写 `/agent` 下文件另加按智能体的键;老布局仍按用户。上线顺序:先钉沙箱镜像并回收在用沙箱,再发代码。对接影响:路径与 `session_id` 有变化(发前需通知深护智康)。 |
| B-242 | **✅ 11-04 P0+P1 已上生产(`91e8daf02`,#1999)** 沙箱内共享状态(B-236 第四轮安全审查带出,早于 B-236):同一用户所有智能体、所有对话的 exec 共用 `/tmp`、`~/.bash_profile`、`/opt/agents`,进程互相看得见(`ps` 看得到别的对话的命令行,`/proc/<pid>/root` 被挡),恶意脚本可借共享文件跑进别的对话。P0+P1:堵住跨对话登录脚本劫持、本地目录按对话分、技能目录只读、兜底只读。**未完成:** P2(进程互不可见)—— 用户拍板先实测再做。 |
| B-244 | **✅ 11-04 已上生产(`91e8daf02`,#1985)** **开场复制遇到「能读不能进」的目录整体失败**:`conversation_seed._children` 对 `lstat` 只吞 `FileNotFoundError`;`chmod 644` 的目录能列名、`lstat` 每个名字都 `EACCES`,整次复制失败、不写标记、每轮重试每轮失败,这个 agent 之后所有新对话都没有开场副本(沙箱里一个 `chmod` 就能触发)。修:`lstat` 出任何 `OSError` 跳过并计数,补用例。 |
| B-245 | **✅ 11-04 已上生产(`91e8daf02`,#1985)** **技能自带的长期默认被平台记忆规矩抢走** —— `health-plan-report` 规定「以后都这样」写 `/agent/report-style/personal.json`(渲染只读它),模型却按 B-236 的通用记忆规矩写成 `/agent/preferences/*.md`,没打开技能。修(平台通用文字,不带租户内容):记忆规矩里加「技能自己规定了默认值存在哪的,按技能来」;复验 3 / 3 先打开技能、写 `personal.json`。 |
| B-248 | **✅ 11-04 已上生产(`91e8daf02`,#1998)** 10-30 MCP 热修 #1979 的遗留:池到期时一并让缓存的智能体构建失效;退役连接排空;不缓存的池的 `last_attempt`(反复打断不入缓存);oauth2 目录失效;同名客户端泄漏。 |
| B-249 | **✅ 11-04 已上生产(`91e8daf02`,#1997)** **`exec_python` 不拿工作区写锁**:`bash` / `write_file` / `edit_file` 都拿,`exec_python` 写工作区不互斥。修:同一对话的 `exec_python` 与 `bash` 串行、不同对话并行,停止后锁 1 秒内释放;老布局下 `exec_python` 不拿锁、不挡同一用户的别的对话。 |
| B-250 | **✅ 11-04 已上生产(`91e8daf02`,#1987)** **`ask_image` 直接看工作区里的图片文件**(用户拍板):以前只认上传图片(`image_ref`)与 `read_page` 渲染过的 pdf / docx / pptx 页,模型自己裁的 png 看不了,只能绕路拼 PDF 再渲染。修:`path` 指向 png / jpg / jpeg / webp / gif 时不要 `unit`,换成工作区图片 ref 走现有租户 / 用户 / agent 三项校验;`image_ref` 填了工作区路径(不是 `expert_work://`)时按 `path` 处理。 |
| B-251 | **✅ 11-04 已上生产(`91e8daf02`,#1987)** `ask_image` 按页看时没渲染就自动渲染:`path` + `unit` 要求这一页先被 `read_page` 渲染过,否则报名字误导的 `FileNotFoundError`,模型要多花一轮。做法:没渲染 / 渲染已过期时 `ask_image` 自己调一次 `read_page`(同一工具实例、同一套沙箱与锁)再看;文档本身不在照旧报错;智能体没有 `read_page` 时不自动渲染;同一 run 同一文档的代渲染排队(并行看图会在同一渲染目录里互删)。**已知**:`read_page` 被列进 `approval_required_tools` 时代渲染不走审批(今天没有这样配的智能体)。 |
| B-252 | **✅ 11-04 已上生产(`91e8daf02`,#1987)** 「深入看」超时自动改用「快速看」再试一次:原先 `depth='deep'` 120 秒超时只在报错里建议换 quick(那次 3 次 deep 超时白等 6 分钟)。修:deep 超时且 run 还有时间时,用剩余时间以 quick 再看一次,结果里写明是快速看的回答;仍超时再报错。 |
| B-253 | **✅ 已完成(10-31,用户拍板暂不做;看图「深入看」的默认档位由 B-257 #1995 处理,随 `91e8daf02` 于 11-04 上生产)** **「总在思考」的模型给平台默认思考档位**:glm-5.3 关不掉思考,智能体不设档位时按厂商默认,一次运行 95% 的输出是思考(单步最长 6 分 46 秒)。不能凭感觉改(09 月实测降一档每步反而慢一倍)。**未完成:**主模型 / 备用模型的默认档位先不动,以后用真实报告在测试环境实测各档总耗时、看图次数与结论质量再定。 |
| B-254 | **✅ 11-04 已上生产(`91e8daf02`,#1987)** 对话页每轮过程条拆开显示时长(用户拍板做):原先只有一个合计时长,看不出慢在模型思考、工具还是失败重试。修:合计拆成模型 / 工具 / 失败工具三段时长,子智能体行照旧不重复计。带出 B-255(并行工具时长重复计)。 |
| B-255 | **✅ 11-04 已上生产(`91e8daf02`,#1994)** 过程条把同一步里并行的工具时长相加:一轮墙钟 34 分钟,过程条显示「工具 33m14s」,页头按工具阶段墙钟算 18m11s(B-254 之前的合计时长也有同样重复)。修法:每一步的工具时间取该步 `tools` 节点的 `_duration_ms`(页头同口径),没有时取该步最长的一次调用。 |
| ~~B-133~~ | **✅ 10-12 已上生产(`94dc73ba`,#1708 #1709 #1710)** **子智能体几乎不派活**(生产 30 天 ai-health-plan 真实用户 0 委派;sop2-designer planner 标 delegate 19 次只派 5 次)。① 多轮对话里 `workspace_ingest` 把上一轮 PLAN.md 当人改读回、盖掉 planner 新计划 → #1710 只读回与上次投影摘要不同的文件,委派标记进 PLAN.md / 计划清单,`update_plan` 沿用标记,防重复派发;② 子代容错(孙代逃出 worker 个数上限、排队占全局闸位、不受 run 墙钟约束、被迫收尾报成功、异常无 end 帧)→ #1709;③ 委派说明夸大且自相矛盾 → #1708。**不做强制委派**(业界默认也不主动派,串行任务多 Agent 反而掉点)。可选后续:按「读多回少」场景加运行时提醒,先离线估收益再立项。 |
| ~~B-132~~ | **✅ 已完成(10-01 销案;价目表数据,无代码)** **平台价目表缺 glm-5.2**:测试环境已补 glm-5.2(输入 / 缓存命中 / 输出 8 / 2 / 28 元每百万 token)与新模型 glm-5.3-flashx(2 / 0.57 / 7,模型随 #1702 于 10-12 上生产 `94dc73ba`)。用户 09-30 拍板生产价目表**不补**这两个模型(生产当前无 Agent 用,选了的话控制台成本栏显示 0)。对接方 project-service 自己的价目表已补。 |
| ~~B-1~~ | **✅ 09-16 已上生产(`5775fbf3`,#1482)** `webhook_endpoint` 不记录创建者(`user_id` 硬编码 `None`)。修:写入侧改走 `resolve_caller_user_id`(与触发器同款),机器主体仍为 `None`;`PATCH` 走 `model_copy` 不抹 owner;列早已在表上(迁移 0074),无需回填(当时测试、生产都没配 webhook)。连带效果:用户清除 `delete_all_for_user` 清 webhook endpoints 从此真正生效(原先这列恒为 NULL,一直空转);投递 worker 不读它。 |
| B-6 | **✅ 已完成(08-17,对外文档写明,#1197)** **同一资源的两个写操作,`user_id` 位置不一致**:`PATCH .../sessions/{id}`(重命名)在 **body**(`ExternalRenameRequest`),`DELETE .../sessions/{id}`(归档)在 **query**;按 query 传 PATCH 的 `user_id` 回 422。接口不改,对接文档 `apps/admin-ui/docs-site/guide/query.md` §5.5 已写清 |
| ~~B-8~~ | **✅ 已完成(08-14 合 main,#1164;发布脚本)** `tools/deploy/smoke.sh` 用 `items[0]` 选 control-plane pod 不过滤状态,发布刚结束时会选中 Terminating / 已 Succeeded 的旧 pod,报 `cannot exec into a container in a completed pod`。光加 field-selector 不够:Terminating 的 pod phase 仍是 Running,还要 deletionTimestamp 为空 + Ready=True;kubectl jsonpath 没有否定(`!` 报错),要在 awk 里筛。 |
| B-9 | **✅ 09-16 已上生产(`5775fbf3`,#1457)** `/v1/agents/` 前缀下 12 条 `console_only()` 路由的 422 也套了第三方信封(生产异常处理器判据 = 前缀 OR tag),`admin-ui/src/api/client.ts` 读 `data.detail` 读不到,降级成 `HTTP_422` 通用文案。修:判据改成「挂了 `external_only()` 依赖」—— 信封跟闸走。(PR-A #1181 复审发现,非其引入) |
| B-10 | **✅ 09-16 已上生产(`5775fbf3`,#1457)** **缺一道 CI 闸**:8 个 `external_*.py` router 都带 `tags=["external"]`,但将来漏打时信封静默丢失,external 发现器(纯 tag 驱动)与 `test_route_plane_partition` 都逮不到。加自审:凡挂 `external_only()` 必带 `external` tag,例外(`agents.py` 的 `bind_session` / `run_agent_for_user`)走白名单。 |
| B-11 | **✅ 09-16 已上生产(`5775fbf3`,#1457)** `control_plane/audit.py` 的 `ResourceType` Literal 不含 `"artifact"`(protocol 侧有),`external_artifacts.py` 与控制台 `artifacts.py` 都写 `resource_type="artifact"`;CI mypy 不扫 control-plane 所以不红。修:`artifact` + `approval` 两个 Literal 对齐,测试钉住两侧集合相等。(来源:PR-B #1185 终审) |
| ~~B-13~~ | **✅ 09-16 已上生产(`5775fbf3`,#1482)** `_artifact_mime.infer_content_type` 的 `kind` 参数是摆设(fallback 分支 `del kind`),选「删」:判据本来就是扩展名,7 个调用点里 5 个一直传占位值;接上 kind 唯一多出的分支是「无扩展名 + 按 kind 判 inline」,正是这个防 stored XSS 白名单模块刻意避免的按元数据嗅探。教训:正则删形参后要对全 diff 逐 hunk 过一遍(曾误删 `tools/eval/artifact.py` 三处 `save_version(..., kind=...)` 必填实参,CI 逮住,`d80a8a2a` 已修)。 |
| B-14 | **✅ 09-16 已上生产(`5775fbf3`,#1457)** `GET …/sessions/{id}/messages` 在「非 vision 模型 + Agent 声明了 `vision:` 块」时,消息文本带 `[image attached: expert_work://image/…]`(`api/runs.py`),是内部 URI 越过对外边界的最后一处。修:对外消息投影时直接剥掉 |
| B-15 | **✅ 11-04 已上生产(`91e8daf02`,B-262 P0 #2001)** **同名文档覆盖**:同一用户重复上传同名文件(如 `report.pdf`)映射到同一路径,后者覆盖前者字节,先前的 `upload_id` 指向新内容。#2001 改为原子创建:名字被占且字节相同就复用原文件(不写、不计配额),字节不同落成 `<stem>-<sha8><ext>`,仍冲突再试最多 3 个随机后缀、否则 502;实际存储名回到控制台 / 对外响应、下载文件名、审计与 `[file attached: …]`;对外文档 chat.md 不再说同名覆盖。 |
| B-17 | **①② ✅ 已修(2026-09-10,#1482);剩 ③**。① 注释改成实话(`app.py` 一律传 `settings.kill_switch_cache_ttl_s`=5s,类上那个 30.0 是从不生效的形参默认值)。② 事件流页大小改成本层自己的 `EVENT_PAGE_LIMIT`,取值不变——**写的时候发现这不只是借名字**:`RunEventStore.list` 会把 limit 夹到 `MAX_LIST_LIMIT`,而补库循环拿「行数 < 页大小」判最后一页,页大小超过 store 上限会让循环在第一页误判收尾、**静默丢事件**;已加一条守卫测试钉住这层关系(变异 `+1` → 红)。原记录:① `external_agent_catalog.py:95` 注释写 30s,真实 TTL 是 `settings.kill_switch_cache_ttl_s`(5s)② `_run_event_stream.py:24` 从 run store 借 `MAX_LIST_LIMIT`,语义上应有自己的常量 ③ `approval` 事件把 `binding_digest` 原样放到对外线路上,对接方用不上、且暴露内部摘要形态,考虑从对外投影里剥掉 |
| ~~B-18~~ | **✅ 已完成(09-10,#1482,只改对外文档不改代码)** `/v1/agents/...` 下匹配不到路由的请求(如路径参数带 `%2F`)返回框架简易格式 404 `{"detail":"Not Found"}`,不套第三方信封;对外信封处理器只接 `RequestValidationError`。修:`errors.md` 8.2 补上这一处(原文「返回简易格式的只有以下几处」不成立),8.6 加说明 + 示例。没有给对外前缀加 404 信封(属行为变更,未做)。 |
| B-19 | **✅ 09-07 已上生产(`2d5742c6`,#1338)** **配额维度混扣 + 黏性维度 Retry-After 除零**(第四轮文档终审 C-1):`CheckRequest.resource_kind` + 协议层单源谓词 `dimension_applies()` 两引擎共用;黏性(refill=0)维度打满不再发 `Retry-After`(header 省略、body null)。`ARTIFACT_STORAGE_BYTES` 实测零消费点,映射记空集,待未来接线时登记。|
| B-20 | ~~**审批列表分流:安全审批 vs 业务澄清混排**~~ **✅ 主体已交付(2026-08-23)**:①两 tab(协议单源分类 `SAFETY/CLARIFICATION_REASON_KINDS` + `GET /v1/approvals?kind_class=` + 前端「安全审批/待确认」tab 带对侧徽章)✅;③超时按类配(`policies.clarification_timeout_s` 默认 1h,mint 按类选,sweep 对澄清单的超时理由改为「按保守默认继续并说明假设」——ask_for_approval reject 非终局,agent 收到即续跑)✅;④ decide/resume 三端点补 `require("session","write")`,堵 viewer 可裁任意单的洞 ✅(「澄清单发起人自决」推迟:run 上没记发起员工身份,随 D-6 一起做)。**剩余**:② 通知路由——澄清单对话内横幅归 D-6,安全单 alertmanager 告警归 X-7 |
| B-22 | **✅ 09-07 已上生产(`2d5742c6`,#1307)** `ConversationDetail` 导出 / 跨租户两只 e2e flake 根治:growth-repair effect 读未 gate 的 `convo?.runs`(旧线程),每次切线程发错租户的 `loadHistory`;改读 `viewedConvo?.runs`(与主重建 effect 同 gate) |
| B-23 | **✅ 09-16 已上生产(`5775fbf3`,#1434)** `GET /v1/skills` 的 `platform_items` 单次上限 200(`list_platform_skills(limit=200)`)静默截断,平台库超 200 条 ACTIVE 时租户侧少看到技能且无提示。修法:平台半边走游标 `platform_cursor` / `platform_next_cursor` + `platform_items_truncated`;SkillPicker / SkillsList 循环拉完,`MAX_CURSOR_PAGES` 封顶仍有游标时常驻 warning Alert。 |
| B-24 | **✅ 09-16 已上生产(`5775fbf3`,#1453)** CI `pip-audit` 偶发网络超时假红 → 该 step 加 step 级重试,不再靠手动 re-run。 |
| B-25 | **✅ 09-07 已上生产(`2d5742c6`,#1337)** worker 帧裸透传 spotlighting 围栏:worker 帧组装处 3 个出帧字段(tool_result_excerpt / exec stdout / stderr)统一 unspotlight(先还原后截断),前端 worker_timeline 用 cleanUntrusted 兜历史帧。已入库历史帧对外仍带围栏,不迁移(对接方剥离器保留)。|
| B-26 | **✅ 09-07 已上生产(`2d5742c6`,#1339)** **子 Agent / worker 构建静默防御降级**(BUG-19b 硬闸清点发现):防御参数决议抽单源 `resolve_defenses()`(runtime.py),child / worker 两调用点补传五参(judges / tool_budget / deadline / token_usage_kind);kind 穿透委派树 + 子代缓存键补 kind 位;顺带 BuiltAgent 挪中立模块 built_agent.py 根治循环导入。|
| B-27 | **✅ 09-16 已上生产(`5775fbf3`,#1456)** `threads/` 工作区子目录缺删除生命周期(会话 purge 不清、无 GC,只增不减;批 D #1268 终审 follow-up)。修:`threads/<id>/` 只跟对话生死(`thread_meta` 行先提交、目录后写);无主目录留 24h 宽限(按目录 + 直接子项最新 mtime)再删;tenant / user 两级目录不 resolve、`lstat` 拒 symlink(逃逸测试曾实测删到 victim,是真 bug)。 |
| B-28 | **✅ 09-16 已上生产(`5775fbf3`,#1456;生产留存定时任务 #1574 随 10-12 `94dc73ba` 才开始跑)** 对接方停删产物(#1305)后,产物生命周期归平台管:artifact / artifact_version 与工作区文件原本只增不减。用户拍板:产物 90 天、上传 90 天。留存作业按版本 `created_at` 过期(定序 `created_at, artifact_id, version`),`ARTIFACT_EXPIRED` 审计带 `reason` / `versions_remaining`;工作区根目录其它文件(`style/`、`MEMORY.md`、未登记文件)不动 |
| B-31 | **✅ 09-16 已上生产(`5775fbf3`;② #1399 已随 09-07 `2d5742c6`,① #1439)** credential-proxy 两件套。① 失效链断头(`/admin/cache/invalidate` 无调用方,缓存只靠 60s TTL):#1439 订阅 control-plane 的 Redis 失效频道 `expert_work:invalidation`,密钥轮换即清;`EXPERT_WORK_CRED_PROXY_REDIS_URL` 未配置则不起订阅者、行为同旧。② `/admin/*` 三端点无鉴权(#1399):`/admin/allowlist` 写的正是 `/forward` 唯一校验的表,而 `forward` 把解析出的密钥发往调用方自己指定的 upstream(URL 不校验)—— 能写白名单 = 能让代理把任意租户的密钥送到任意地址,集群内所有 pod 可达。修法:中间件按前缀拦 `/admin/` + `compare_digest` 比对 `EXPERT_WORK_CRED_PROXY_ADMIN_TOKEN`,**未配置 → 503 而非放行**;`/admin/health` 留给 kubelet 探针。顺带订正 STREAM-F-DESIGN §4.4「仅 mTLS SAN=control-plane 可达」(从未实现)。 |
| ~~B-32~~ | **✅ 09-07 已上生产(`2d5742c6`,#1398)** **redis Lua 配额桶回填量纲差 1000 倍**:生产强制走 redis 引擎(多副本没配 quota Redis 拒启),所以 QPS 类配额在生产上等于没有。根因:`quota` 的 Lua 只把速率缩放了 ×1000(`ratelimit` 那份全量纲自洽),桶回填快 1000 倍、retry 公式带同一因子所以从 `retry_after_s` 看不出;`int(rate*1000)` 还把极慢速率 floor 成 `0`,撞上黏性天花板哨兵。修:速率与 capacity / cost 同单位原样过线,黏性哨兵改判 `refill_per_s > 0`,两引擎谓词重新同义;新增真 redis 集成测试 5 条(此前 quota 单测全走内存引擎,Lua 桶从没被跑过)。 | ✅ |
| B-33 | **✅ 09-16 已上生产(`5775fbf3`,#1429)** 模型个性参数无 catalog 声明:kimi-k3 只接受 temperature=1,sop2 换模型后旧值 0.9 原样发出 → 厂商 400 炸 run(同族:#1321 always_thinking)。修:`ModelEntry.temperature_fixed`(只 kimi-k3 声明 1.0),factory 五个出口钳制 + WARNING;`ModelSpec.temperature` 默认 0.2 存库分不出是否省略,所以默认值也钳也告警。**未完成:** 配置页冒 warning 未做(未编号)。 |
| B-34 | **✅ 09-07 已上生产(`2d5742c6`,#1350)** moonshot 严格校验 tool schema,enum 缺 type 报 400:update_plan / manage_task 四处补显式 type;守卫测试 `test_tool_schema_vendor_strict.py` 递归扫内置工具 schema。规矩:拼给 LLM 的 schema 必须过厂商严格校验,别赌宽松(同 MCP 工具名 wire-safe) |
| B-36 | **✅ 09-07 已上生产(`2d5742c6`,#1368)** 弹性 worker 预算:sop2 plan_first 首跑 3 个 worker 全撞平台 32 步顶。方案甲(08-28 拍板):manifest `dynamic_workers.max_iterations/max_concurrent/max_per_run` 三个可选字段(None = 平台默认,sanity 闸 512/64/1024);平台 `platform_dynamic_worker_config` 扩为默认 + 硬顶两档六值(迁移 0148 回填 cap 10/64/128),per-agent 请求 clamp 到 cap,显式请求赢过「不超过父 workflow」启发式;API / 平台管理卡六值(default≤cap,否则 422);Agent 配置页「子智能体预算」小节,步数 >40 且无 token 预算时软提示(步数与 token 不硬联动,拍板)。sop2 worker 步数已放宽并知会 project-service。 | ✅ |
| B-37 | **✅ 09-07 已上生产(`2d5742c6`,#1369)** **子 Agent 上下文继承**:worker 不继承父 Agent 的技能(实证 ai-health-plan 会话 abf70030:做 PPT 的 worker 没有 pptx 技能,连 `skill_view` 都没注册,只能靠主 Agent 手抄风格进 task;归属以 `thread_meta.agent_name` 为准)。业界共识是子 Agent 提示词独立生成,但都有一条框架自动下发的「共享约定」通道,我们缺这一档。**用户拍板:平台缺口先改默认,不设计成让配置者去配,零新增配置项**:① worker 继承父 manifest 技能(惰性加载,常驻仅摘要行);② 继承技能软失败(模型不匹配 / 工具冲突 skip+log);③ worker 提示词点明共享工作区;④ `spawn_worker` 描述补委派四要素 +「约定用路径引用别抄内容」。运维回滚阀 = env `dynamic_worker_inherit_skills`(不进 UI)。**不做**:全量继承父提示词 / 提示词标记块 / per-agent 继承配置。spec `docs/superpowers/specs/2026-08-28-worker-context-inheritance-design.md`。注:技能种子路径用父 key 不是 worker key;worker 子 run 不落 `agent_run` 表。 | ✅ |
| ~~B-39~~ | **✅ 09-07 已上生产(`2d5742c6`,#1392 / #1393);08-31 真栈复验两半全过** 本轮附件送达子代的真栈复验。判据看检查点而不是模型行为:`[attachments]` 标记只会出现在子代种子 `HumanMessage` 里(父消息里是 `[file attached: …]`)。① 附件到达子代:派出的子线程种子都带上传文件;`turn_documents` / `turn_image_refs` 是被 checkpoint 持久化的真实通道。② 审批续跑不丢:批准后续跑新增的子代线程种子仍带附件。教训:别拿探针脚本输出当被观测之物(脚本一度误报「没停在审批闸」,查库才见 `status=paused`)。 |
| ~~B-41~~ | **✅ 09-07 已上生产(`2d5742c6`,#1370 修复 + #1403 查清补测试)** 嵌套执行的双计是否只在耗时上:只有耗时双计(同一段墙钟既进父工具行又进 subagent 行),#1370 已修;token 不双计(孙 worker 的 end 帧直达父 run 的 bridge,每个 end 帧只装自己那份,回父侧的 `ToolMessage` 不带 `usage_metadata`);成本是 token 的纯函数;产物 `turnArtifacts` 按名去重。发现 token 这条不变式此前零处被钉住(原测试 fixture 实为同一个 w-1 的 end 帧发三遍),已补前端两层树 + 后端「父的账不含从孙那儿回来的东西」两条真嵌套测试,变异自证。 | ✅ |
| B-42 | **✅ 09-16 已上生产(`5775fbf3`,#1432 后端 + #1436 前端)** 控制台整轮成本按主 Agent 的费率给 worker 的 token 计价(错价,不是双计):自 #1374 起 `summary.usage` 含 worker 的 token,而 #1322 允许 worker 换模型;线上一个 run 里 95% 的计价 token 来自 worker。修法:usage 按 (provider, model) 分桶 `usage_by_model`,worker end 帧 + run / 对话 tokens 都带;前端成本 = Σ 桶 × 各自费率卡;桶按**配置的**模型记,不按响应别名;worker 费率卡缺失时成本留空,不退回主卡。计费不受影响(走 `token_usage` 行 + rollup,逐次调用记全),错的只有控制台显示的数。 |
| ~~B-45~~ | **✅ 09-16 已上生产(`5775fbf3`,#1456 #1493 #1496)** **三个后台进程的 RLS 信号接不上、归因不落盘**:`retention_cleanup_job`(#1456)、`event_log_archive_job` + `sandbox_supervisor`(#1493)接上 sessionmaker;第二层失效:归因(`rls_caller` / `rls_caller_outer` / `rls_suppressed`)是 `extra=` 结构化字段,这些进程不装平台 JSON formatter,信号打得出但归因一个字段都不落盘 → #1493 / #1496 补上(#1496 改 `retention_cleanup_job/main.py` 的 `basicConfig`,三者里唯一真部署的)。教训:「接上了」与「看得见」是两件事。**未完成:** 归 RLS 第二轮 —— sandbox-supervisor 只装了 listener、没逐条声明作用域(会话三类:带租户的前门请求 / 只拿 sandbox_id 的 release·destroy·exec / 跨租户常驻循环),按信号再定作用域。 |
| ~~B-47~~ | **✅ 09-16 已上生产(`5775fbf3`,#1482)** advisory lock classid `8619` 被 `trigger_delivery` 与 `workspace_janitor` 同时占用(两边注释都说自己独占)。修:新建 `control_plane/advisory_locks.py` 收全仓 9 个 classid 常量 + `CLASSID_BY_OWNER` 登记表;自审测试:不得重复、常量必须进表、AST 扫描禁止别的模块写 classid 字面量(`event_log/db.py` 走单参 bigint 空间,不入表)。让位的是 `workspace_janitor`(→8621),`trigger_delivery` 保持 8619 —— 判据是滚动发布窗口的代价:改已上线的锁要付一次互斥真空,投递锁 split-brain 会重复投递 / checkpoint lost-update,janitor 各阶段幂等。模块 docstring「Renumbering a live lock」一节写了这条判据。 |
| B-50 | **✅ 09-16 已上生产(`5775fbf3`,#1507 #1509 #1512 #1513 #1522 #1523 #1524 #1532;生产存量搬迁随班车 1 同日执行)** 用户工作区没有 agent 维度,同一用户的多个 agent 互相读错写错:NAS 布局 `{root}/{tenant}/{user}`、`artifact` 唯一键 `(tenant,user,name)` 都没有 agent,`list_artifacts` / `list_dir` 对模型的描述是错的;测试环境 64 个用户里 8 个用过多个 agent。热沙箱按 `(tenant,user)` 复用、CSI subPath 钉死,挂载点不能按 agent 分,只能按路径分 + 工具边界执行(与 `/opt/skills/<agent_key>` 同构)。修:`agent_key` 地基;`artifact` 唯一键加 `agent_key`(迁移 0154);工作区工具按 `agents/<key>/` 作用域,`bash` / `exec_python` 的 cwd 同根(迁移期读回落已由 #1532 摘掉);对外六个端点按 agent 收口,跨 agent 一律 404,两个列表端点加 `?scope=user` 并集(默认 `scope=agent`,对接方要求);`agent_code → agent_key` 就是 `sanitize_agent_key(agent_code)`,不查库、不归一大小写;存量搬迁脚本 `tools/persistence/migrate_workspace_agent_scoping.py`(反推不出的进 `shared/`,读可达、写拒绝;不覆盖已有目的地)。用户拍板(09-10):交付物、中间文件、`uploads/` 都按 agent 分,两个 agent 不互读交付物;`.tool_results/` 与 `threads/` 随会话 purge + 孤儿宽限扫描,agent 持久工作态(`MEMORY.md` / `style/`)无 TTL;34 个孤儿产物不出清单、不认领。已接受的代价:那 8 个用户 legacy 文件的读串不被消除,只随时间稀释。cwd 不是隔离手段,真边界是文件工具。spec + 计划见 #1490,runbook `docs/runbooks/workspace-agent-scoping-migration.md`。

**真跑逮到 11 个问题,9316 条测试全绿一个都没拦住。** 共同形状:那些分支只在真环境执行。① **runbook 三条命令全跑不起来**(#1526)—— `tools.persistence` 不在镜像里(只 `COPY tools/eval`)、`-l app=control-plane` 选不中(本仓库用 `app.kubernetes.io/name`)、NAS 根写成 `/workspace-root`(实为 `/mnt/workspaces`)。根因是那份 runbook 照着仓库写、**一条命令都没在真集群上执行过**。已配 `tools/deploy/test_runbook_pod_commands.py` 两条守卫。② **嵌套 `asyncio.run` 必炸**(#1528)—— `apply_migration` 里 `asyncio.run(_update_artifact_paths(…))` 而 `_main` 本身就是 `asyncio.run` 起的。**没被逮到是因为八处调用全不传 `session_factory`,这个分支从来没被执行过一次**;而且炸在**文件搬完之后**,当场 235 条登记行断链。同 PR 把更新改成从「文件现在盘上在哪」反推 → 搬迁变**幂等**,崩了重跑即可。③ **归属表被当成「登记过的路径」表用**(#1529)—— `Attributions.artifacts` 过滤空 `agent_key` 是对的(判归属),但它同时被拿去回答「哪条路径是登记过的产物、搬完要跟着改」,而后者不该过滤。后果两条:空 key 的产物登记行不跟着文件走(实测 12 条);**单 agent 捷径档返回空表 → 65 个用户里的 56 个产物登记行从来不会被更新**,而报告里的 `artifact rows updated 0` 看着完全正常。拆成 `artifacts`(归属)+ `artifact_paths`(全集,**必填无默认**)。④ **三个报告缺陷**,全是「两种归因长得一模一样」:空跑印 `artifact rows updated 0`(空跑本就不写库)与「真的一行都不用改」不可分;`conflicts ⊂ to_shared` 两张清单全量打印,同一文件挂两个互相矛盾的理由;我在 #1528 加的「实改 ≠ 计划」告警**误报且措辞说反**(208 个 path 改到 223 行是正常的 —— 一个 path 挂多个版本行,两个不同单位)。改成按「有几条更新一行都没命中」判。**一条变异零杀**:未命中计数我只测了渲染没测计数本身,补假 session 按序吐 `rowcount=[3,0]` 才咬住。

**顺带记两条**:`{tenant}/.deleted/<user>/`(软删工作区归档)与日备恢复出来的工作区都是**旧扁平布局** —— 今天靠 PR3 的迁移期读回落兜着,**PR6 摘掉回落的那一刻它们会直接看不见**,Task 14 要一并处置。另:金丝雀用户一个 `canary-check.txt` 挂了 **27 个版本行**(每次发版写同名产物),是「行数 ≠ path 数」最直观的例子。**PR6(Task 14 摘掉迁移期读回落)✅ 已合(#1532)—— B-50 六个 PR 全部交付。**

**勘误(同日,用户纠正)**:我原先把 PR6 定成「刻意不合、下一班车才上生产」,并把闸门写进执行单 Step F。**这个形状是错的。** 用户指出:**每次发布都应该是完整的,生产只是把测试上的步骤原样重放**,而不该是「测试发一半、剩下等生产发完再发」。我造出的状态正是后者 —— 测试跑分支构建、main 没有它。

PR6 不是「留到下一班车的尾巴」,是标准 **expand → migrate → contract** 里 contract 那一段。测试环境 09-13 已经完整走完三段(`ca225258` → 搬迁 → `d89dfc6f`),生产就该在**同一个窗口里逐字重放**:
`Step A 沙箱钉子 → B1 发 ca225258(expand,带回落)→ C 跑搬迁 → B2 发 main HEAD(contract,摘回落)+ A/B 复验 → D P-1 → E 记录`。两段都是 main 上的提交,可追溯。这样原先的闸门顾虑**自动消失**:B1 与 C 之间回落在场,窗口是安全的,所以 PR6 当天就合了。Step F 随之取消,它变成 Step B2。

**回滚因此多一个档位,而且是最有用的那个**:B2 之后出问题退到 `ca225258` —— 那一版认得 `agents/<key>/` 布局**又带着回落**,是搬迁之后最宽容的一版,**文件不用搬回去**。原方案只有「退到 `ad79ba28` 并手工把文件搬回去」这一条路。

**方法论**:我两次在同一个地方判断偏保守 —— 先是把「合并到 main」和「发测试」混为一谈(以为 PR6 只能等 09-16 之后才能真栈验),再是把 contract 段当成可以顺延的尾巴。两次都是用户当场质疑纠正的。共同的形状是:**我按「这一步有风险」去推迟,而没问「把它做完整反而是不是更安全」** —— 三段式恰恰比两段式多一个回滚档位。

**闸门**:`read_with_legacy_fallback` 是**生产发版到搬迁完成之间的安全网** —— agent 在自己目录下读不到时回落用户根再读一次。搬迁完成后它反过来成了纯粹的跨 agent 读洞,但**在那之前它是必需的**:今天测试环境的搬迁跑了三轮修 bug 才成,生产同样可能中途卡住,那个窗口里没有回落,agent 连历史文件都读不到。~~闸门写进 09-16 执行单 Step F~~ —— 见上方勘误,已改成三段式的 **Step B2**,与本次发布同窗口完成。

**真栈 A/B 实证(09-13,测试环境)—— 先造条件再验,否则是恒真断言。** 搬迁之后用户根已经空了,回落没东西可找,直接跑「摘掉回落」观察不到任何差异。所以先在探针用户的**用户根**上放一个 `legacy-probe.txt`(agent 目录里没有),然后同一个探针、同一句指令跑两次:
  * **A(有回落,`ca225258`)**:`pf-probe` **读到了** `LEGACY_ROOT_ONLY_9F3A2B` —— **这一跑的副产物比验证本身更重要:它实证了「搬迁之后回落 = 跨 agent 读洞」不是理论**,agent 真的读到了不属于它的文件。
  * **B(PR6,`d89dfc6f`)**:`read_file failed: not_found`,且那个标记串在**整个事件流里一次都没出现**。
测试环境**刻意留在 PR6 分支构建上泡着**(`d89dfc6f`,未合并)—— 发测试不需要合并,`release.sh test` 是从工作树建的。这一条是我先前的判断错误:我把「合并到 main」和「发测试」混为一谈,因此本来打算把 PR6 的真栈验证推迟到 09-16 之后。用户当场质疑「为什么不在今天就跑完它」,是对的。

**PR6 范围**:删 `read_with_legacy_fallback` + 三个调用点(`file_ops` ×2 / `read_document` ×1),四条「不回落」钉子(桩里第二个 outcome 是「用户根上有这个文件」的**诱饵**,回落被加回来就会拿到它;含 `read_document` 单独一条 —— 它在 PR3 计划里被漏登记过一次),`volume-restore.md` 加 Step 3b:**备份恢复出来的工作区是旧扁平布局,恢复完必须重跑一次搬迁脚本**(脚本幂等,跑错也安全);`{tenant}/.deleted/<user>/` 同理,且它**没有产品级恢复路径**,真要恢复是人工 `mv` + 同一步 |

11-12 核实:行内「未完成(待拍)」那条(平台依赖默认 `anthropic`)已由 B-156(#1736,10-12 随 `94dc73ba` 上生产)把默认值统一改成 `glm` / `glm-5.3-flash`;遗留另见 B-53(仍在主清单)。

| # | 事项 |
|---|---|
| ~~B-51~~ | **✅ 09-16 已上生产(`5775fbf3`,#1495)** 平台凭据页会让运维得出「不用配」,平台自己的后台功能因此空转:长期记忆整合从上线起两个环境都没成功过(`memory_consolidator.cluster_failed` → `CredentialsResolverError: platform credentials missing for provider=anthropic`,每次 `consolidated=0`)。原因:凭据页 `used_by_agents` 只数 agent,平台自身的 provider 依赖(嵌入 / eval / quality judge / 记忆整合 / rerank)一个都不计,anthropic 那行显示「未设置 + 被智能体引用 0」。方案(用户 09-11 拍板:不动默认值、不加启动闸,只把「没配」变成看得见):① 记忆整合 `summary.errors` 按原因分类计数(`credentials_missing` / `llm_timeout` / `other`);② 凭据页加「平台自身在用」维度,区分已开启 / 未开启;③ `未设置` + 有平台依赖 + 功能已开启 → 来源格红色 + 页面顶部 Alert(未开启的只在表格里标,不弹 banner);④ 记忆面 `MemoryAdmin.tsx` 顶部显示「整合已连续失败 N 次,原因」。**明确不做**:不把默认值改成空(`Provider` 是 Literal,空串 pod 起不来);不加启动期凭据闸(凭据权威来源是金库,启动看 env 会误判没配 → null adapter 静默空转,`app.py:2024` 注释记着这条路撤过,同 #656)。**未完成(待拍):** 四个平台依赖里三个默认 `anthropic`,而主力是 glm / kimi,默认值指向大概率没配的厂商;改默认值是行为变更,优先级可降。 | 落地以实测为准:平台 provider 依赖是五个(含 `rerank_provider`);`quality_judge` 的开关是 `enable_quality_monitor` AND DB 里的 UI 开关(默认关),所以 handler 接 `PlatformQualityConfigService`(embedding / rerank 接 `PlatformEmbeddingConfigService`),避免假的「已开启」。**依赖登记表 `platform_provider_uses.py` 是唯一登记处**,AST 自审扫 `Settings` 类体比对,漏登记就红。遗留见 B-53。 |
| ~~B-52~~ | **✅ 09-16 已上生产(`5775fbf3`,#1499 #1500 #1501 #1502)** **按 run 的用量与成本查不出来**(对接方 09-11 计费需求):对外没有用量 / run 详情端点;条目模式(`stream_format=items`)下主线用量整块收不到,对接方纯对话轮收 0、派了 worker 的反而收得多。修(零迁移、零新列,复用 `totals_by_trace_ids` + `TokenTotals.by_model`):`usage_kind` 过滤(平台自身开销 `quality_sampling` / `skill_evolution` 不算);新对外端点 `GET .../runs/{id}/usage`(tokens 按 `(provider, model)` 分桶、四档 token,`run_status` 用与 `GET .../runs` 同一套 `RunStatus` 原值);`end` 帧带 `usage_by_model`(`end_frame_data` 四个调用点全接;`end` 不带 `id:`、断线即丢,只是便利,REST 端点才是正路)。**用户拍板**:对外**不给金额**、**四档 token 字段恒全给**(计价口径是对接方的事)。对接方须知:`input_tokens` **已包含** `cache_read_tokens`。此前给对接方的「items 跑完用 legacy 重放捞用量」凑合方案作废。遗留见 B-54(`end.artifacts` 语义边界)。 | ✅ |
| B-55 | **✅ 10-12 已上生产(`94dc73ba`,#1623 #1624 #1625;另 #1542 #1626 #1630)** **冷建沙箱 18.8% 直接失败,每条恰好卡 60 秒报 `504 Gateway Time-out (alb)`**(09-19 当天 87%;用户看到工具报错,发版金丝雀与 CI `Contract suite` 同样被打红)。原标题「Contract suite 在 CI 里失效」是归因错误:闸门长期同形红是在如实报生产故障。根因:沙箱 SDK 走 HTTP 80,ALB 的 80 端口监听开荒起没设 timeout,吃阿里云默认 60 秒,而冷建要 66~112 秒(443 端口早设了 180)。三条一起修:① #1625 ALB 80 端口 `requestTimeout: 180`(开闸,唯一让冷建能完成的一条);② #1624 镜像引用改走 ACR `-vpc` host;③ #1623 镜像瘦身:砍 apt `ffmpeg`(改用 `imageio-ffmpeg` 自带静态二进制)与 `npm`(出网代理的 CONNECT 鉴权 npm 不支持,真实 run 从没装上过 npm 包)。另:#1542 契约档 job 预算 25 分钟 + pytest `--timeout=300`(这一档故意不加 continue-on-error);#1630 删 arm64(用户拍板,ACR 上无消费者)。不能用的沙箱 tag:`ef9e157a`、`05b20c8f`,要钉 `7ac31957`。main 上 `Contract suite` 恢复 26 passed / 2 skipped。<br>| | 瘦身前 `621249f6` | 瘦身后 `7ac31957` | |<br>|---|---|---|---|<br>| 压缩层合计 | 619.4 MiB | 433.7 MiB | −30.0% |<br>| kubelet 拉取 | 66.19s | 40.55s | −38.7% |<br>| apt 包数 | 786 | 282 | |<br>| 安装体积 | 1263 MB | 742 MiB | |<br>后续单列:B-86(沙箱池 `replicas` 用户按成本拍板维持 1,已关)、B-87(金丝雀分不出「拿不到沙箱」与「跑错了」)。 |
| ~~B-56~~ | **✅ 10-12 已上生产(`94dc73ba`,#1592)** **`release.sh` 在 rollout 慢时报 `RELEASE FAILED` 并叫你回滚,而发布其实是好的**:新 pod 启动探针 60 秒预算内还没 bind(`connection refused`,ACS serverless 冷启动 I/O 慢,同 B-55 族),被杀重启 5 次约 13 分钟后自己起来;照提示回滚会把健康发布回滚掉,且 smoke / 金丝雀被跳过。修:超时时先打印该 Deployment 的状态、自己的 pod 行、事件尾,分「还在起来 → 等 + 手工补跑 rollout status 与 smoke」/「真炸了 → 回滚」两支给命令;所有 Deployment 探完一起报;给过更好建议时不再重复通用的「roll back with:」。`startupProbe.failureThreshold` 30 → 90(60s → 180s)。`tools/deploy/test_rollout_budgets.py` 钉住四个超时的次序:startup(180s) < 收口上限(300s) < `terminationGracePeriodSeconds`(360s) < rollout 超时(600s),改一个要一起核其它的。 |
| ~~B-57~~ | **✅ 09-16 已上生产(`5775fbf3`,#1518;钉子 63a3109f → e8aac104 #1521)** 沙箱镜像 tag 手工钉在 `infra/k8s/sandbox/sandboxset.yaml`,集群跑的镜像落后 34 天(期间 `infra/sandbox-image/` 改过 4 次,pypdf 升级、ECR Public 改源都没进集群)。根因:有手工路径(`docs/runbooks/sandbox-image-release.md`),缺的是触发信号 —— `release.sh` 不碰它、smoke 不比对、CI 不提醒(与 credential-proxy 钉死同形)。修法(先立信号再清欠账):① main 构建推 ACR 被拒(buildx 默认 provenance attestation 用了 ACR 个人版不认的 OCI 空媒体类型),设 `provenance: false`(#1518);② `smoke.sh` 比对钉子 tag 与 `infra/sandbox-image/` 的提交数,落后就 `WARN 落后 N 个提交` 并点名 —— **刻意 warn-only**(smoke 非零会让 `release.sh` 建议回滚,镜像滞后要人走 runbook 而不是回滚);③ 按 runbook 清欠账、真栈复验。**不做**:把沙箱镜像纳入 `release.sh`(每次发版多等 ~30 分钟构建,信号已治病根)。注意:`python:3.12-slim` 是浮动 tag,长期不构建底下会自己漂;推钉子后必须真栈复验(先例 #1233)。 |
| B-58 | **✅ 10-12 已上生产(`94dc73ba`,#1611)** **被重收的孤儿 run 必死于 `EmptyInputError: Received no input for __start__`**(09-13 初判 queue 模式偶发抖动,09-19 查实):pod 被 liveness 杀重启(事件循环约 30 秒无响应)→ 在跑的 run 成孤儿 → 另一 pod `orphan_sweep` 接管后从头重跑而不是续跑(伴随 `spec_sha256_diverged`、`turn_inputs.prompt_frame_missing`、`run_event` `(run_id, seq)` 主键冲突)→ run 立即 error,一次 LLM 都没调。后果:任何 pod 重启撞上在跑的 run 都会把它判死,还会把发布金丝雀弄红、诱导回滚健康版本。修:孤儿重收先确认有东西可续。相关 B-80(排空中的旧 pod 不算发布失败,修的是判据)。 |
| B-59 | **✅ 已完成(09-16,沙箱钉子 e8aac104 → 621249f6,#1568;测试与生产都已 apply)** 生产发布后刷一次沙箱镜像钉子,关掉安全告警 #137(weasyprint):#1484(pypdf)/ #1486(weasyprint)改的是 `infra/sandbox-image/requirements.txt`,集群跑哪个沙箱镜像由 `infra/k8s/sandbox/sandboxset.yaml` 手工钉子决定,不随 `release.sh` 走。带上去的:weasyprint 69.0→70.0、pypdf 6.16.2→6.18.0、pdfplumber 0.11.10、apt 层 `apt-get upgrade`(#1540)、B-60 的 `runner.py`。顺序:先测试 apply + 真栈探针(weasyprint 渲 PDF → pypdf 读回),再生产 apply + 同一探针。注意:dependabot 告警按 main 上 manifest 判(#1486 合入即 `fixed`),集群真跑到新版本靠刷钉子,两者别混。B-57 加的 smoke `WARN 落后 N 个提交` 两次发布各响一次,信号成立。**未完成:** 让它不只响、还能把「该刷了」变成一条待办。 |
| B-60 | **✅ 09-16 已上生产(`5775fbf3` 班车 1 B2,#1551 #1553 #1554)** **写入侧隔离:每次 exec 一个私有 `/workspace`**(发布阻断项,用户 09-14 拍板):`exec_python` 写 `/workspace/…pptx` 落到用户根,`save_artifact` 登记在 agent 目录,工具与 run 都报成功、下载 404。方案 A′:NAS 挂 `/mnt/workspace`,每次 exec 在自己的 mount ns 里把 agent 目录 bind 成 `/workspace`、`shared` 只读挂入、tmpfs 盖掉 `/mnt/workspace`;热沙箱复用不变,并发按进程树天然隔离;两后端用同一个命令生成函数(`exec_view.build_exec_command`),契约测试钉;`sandbox_instance.layout` 列让热会话自动换代;fail-closed。生产 `EXPERT_WORK_SANDBOX_BACKEND=agent_sandbox`,隔离在 control-plane 镜像里生效,不依赖沙箱镜像钉子。**否掉的**:按 exec 切换符号链接(并发 acquire 输家共享赢家沙箱,会串目录)、每 agent 一个沙箱(容量)。spec `docs/superpowers/specs/2026-09-14-workspace-exec-mount-namespace-design.md`;#1551 = 产物边界(定位 / 认领 / 拒绝)。**未完成:** ⓪ `--map-current-user`(util-linux ≥2.39,镜像 2.41.5)spike —— 命名空间里 `geteuid()==0`(`whoami` 报 root、pip 提示 running as root、个别工具拒绝以 root 跑),映射成当前 uid 可去掉;原计划「做完拆 #1551 的认领分支」未见单独提交,待核。 |
| B-85 | **✅ 10-12 已上生产(`94dc73ba`,①② #1618,③ #1636,设计 #1635)** 瞬时失败被判 `unknown`,平台劝模型「别重试」,run 报 `status=success` 却没做完事(静默假绿)。沙箱认领撞并发时错误原文明写 `retry shortly`,但 `error_classifier._TRANSIENT_NEEDLES` 不匹配 → `unknown` → advisory 劝勿重试,模型放弃,图正常收尾。①② 修法:`AgentSandboxClient._claim_warm_waiting` 首次占坑撞上竞争时平台自己等赢家填好 `container_id` 再复用(上限 `_CLAIM_CONTENDED_WAIT_S=120s`,只挂首次占坑);等满才抛的 `SandboxClaimTimeoutError` 是双基类(`SandboxSupervisorError` + `TimeoutError`),分类器按类型判 `transient`,刻意不加关键词;两个生产 store 的「赢家还在建」改抛 `SandboxClaimContendedError` 并写进 Protocol 契约。③ run 做没做成的独立信号(用户拍板):**`status` 一个字不动**(对接方在读),只加字段;判据是客观事实「最后一批工具调用里有非 transient 失败,之后模型没再调工具,run 就结束」,不猜模型意图。`AgentState` 加 `last_batch_failures`(`tools` 节点每批覆盖)与 `exit_reason`(六个封闭取值,由走那条退出路径的代码盖章);终局落 `agent_run` 两个新列(迁移 `0158`,expand-only 不回填,NULL = 老 run 无记录)+ 审计行 + `end` 帧两个字段;**信号挂在已有 `end` 帧上,不做新事件类型**(对接方有事件白名单,新类型会被静默丢掉)。另在系统提示词加平台级 completion contract(无开关),平台不解析正文里的 `[blocked]`。spec `2026-09-20-run-completion-signal-design.md`。 |

11-12 核实:B-55 行记「B-86(沙箱池 `replicas` 用户按成本拍板维持 1,已关)」;`infra/k8s/sandbox/sandboxset.yaml:72` 仍为 `replicas: 1`,与拍板一致。

| # | 事项 |
|---|---|
| B-86 | **(B-55 收尾时去数带出,2026-09-19)沙箱温池 `replicas: 1`,而历史峰值并发是 11**。`sandbox_instance` 528 行按 `[acquired_at, destroyed_at]` 做一次扫描,峰值同时存活 **11 个**沙箱(2026-08-25 11:57:33),而 `infra/k8s/sandbox/sandboxset.yaml` 的池深一直是 **1**。池深 1 的语义是「只有**第一个**认领者能命中温池(亚秒),第二个开始全部走冷建」——B-55 之前冷建就是 112s + 一半概率 504,之后仍是 66s。**这条在 B-55 修完之后才值得单独看**:60s 闸还在的时候,池深多少都救不了冷建;闸拿掉之后,池深才重新变成决定「用户等多久」的那个数。<br><br>**为什么没顺手调大**:调多少是个要先量的数,不是拍的。缺的量是「acquire 到达时池空的频率」与「并发认领的分布」——峰值 11 是**历史极值**,拿它当池深会常年空养 10 个 microVM(每个 1c/2Gi)。另外还观察到池本身有抖动:一次 apply 之后 `availableReplicas` 在 0 上停了约 3 分钟,期间 pod 反复创建/终止,伴随平台注入的 `postStart` hook(`bash /mnt/envd/envd-run.sh`)`FailedPostStartHook`。那个 hook 不是我们的(ACS agent-runtime 注入),时间戳也早于任何本方操作 —— **先要分清「池深不够」和「池自己在抖」,再决定调多少**。<br><br>**✅ 2026-09-19 已去数,池深有答案了。** 对 `sandbox_instance` 里每一次 acquire,回看它到达前 W 秒内已经发生过几次 acquire —— 那个数 +1 就是「这一次要不走冷建,池得有多深」。**刻意把 CI 的契约套件与我自己的探针排掉**(它们一轮 26 个用例各认领一次,是突发不是真实流量,混在一起会把尾巴拉得很难看):只取 `destroy_reason in ('reap','release')` 的 **392 次**,窗口取 60s(瘦身后 ~46s 拉取 + 调度):<br><br>| 池深 | 覆盖 | | 池深 | 覆盖 |<br>|---|---|---|---|---|<br>| **1(现状)** | **80.1%** | | 4 | 96.2% |<br>| 2 | 86.2% | | 5 | 98.5% |<br>| **3** | **92.3%** | | 6 | 99.2% |<br><br>**读法**:现在**每五次真实认领就有一次走冷建**。池深 3 把它压到 1/13,池深 5 压到 1/67。每个池位是常驻的 1c/2Gi microVM,所以这是「空养几台」换「多少比例的用户不用等」。**建议起步 3**(覆盖 92%,空养 2 台),理由是剩下 8% 在 B-55 修完之后只是等 ~46s,不再是失败。<br><br>⚠️ **这份数据的口径要说清**:它是**测试环境**的流量形状(66 个用户,以金丝雀、探针、对接方零星调用为主),生产至今几乎没有真实流量。拿它定生产池深是「现有最好的依据」,不是「有把握的依据」—— 生产跑起来之后必须重算一次。这也是为什么记成 backlog 而不是直接改 `replicas`。 |
| ~~B-102~~ | **✅ 10-12 已上生产(`94dc73ba`,#1663)** **`token_usage.model` 列有两种含义**:主模型走备用模型时记配置的主模型名,而 `ask_image` 看图记实际回答的模型;按模型汇总时备用调用被算到主模型价格上。修:所有 `token_usage` 行按实际回答的模型记(配置名,不用厂商回显)。历史行未回填。 |
| ~~B-103~~ | **✅ 10-12 已上生产(`94dc73ba`,#1663)** worker 事件帧里的 `usage_by_model` 不含 worker 内部的看图调用(只按 worker 自己消息里的主模型汇总;run 级 `end` 帧与用量查询接口本就包含,是权威数据)。修:worker 帧 `usage_by_model` 按实际模型分桶,含看图与 in-run 调用;`llm_call_count` 与 usage 同源。(B-64 Task 9 复审带出) |
| ~~B-104~~ | **✅ 10-12 已上生产(`94dc73ba`,#1663)** **judge / rerank / 辅助 LLM 调用不写 `token_usage`**(直接调 `build_llm_router`,不经 agent 节点记账中间件,用量与配额看不见)。修:in-run 10 类调用接上记账与预算 —— 规划 / 反思 / 压缩 / 记忆 4 件记 `conversation`;输出 / 工具评审、重排序记 `platform_overhead`(不进账单、对外用量、控制台合计;按模型明细只给系统管理员)。遗留 B-107~B-110。 |
| ~~B-105~~ | **✅ 10-12 已上生产(`94dc73ba`,#1666;回归带出的豆包 `type:auto` 400 由 #1668 修)** **输出 / 思考上限**:「输出上限」= 思考 + 回答合计,按厂商真实字段发送(GLM / DeepSeek `max_tokens`,通义 3.5+ / 豆包 / Kimi `max_completion_tokens`,qwen3-max / qwen3-vl 拆成 `thinking_budget` + 回答 `max_tokens`);留空 = 厂商默认(Anthropic 仍 4096);旧默认 4096 在非 Anthropic 上加载时归一为空,行为与指纹不变。新增「思考长度上限」(只有通义可用,其它模型保存即拒);豆包档位改走 `reasoning_effort`。回复被上限截断且不可用时 run 以「模型输出被截断(已用满输出上限 N,含思考)」失败、照常计费,计数器 `expert_work_llm_output_truncated_total`。ai-health-plan 存的 40960 从此生效。遗留见 B-114 / B-115。 |
| ~~B-106~~ | **✅ 10-12 已上生产(`94dc73ba`,随 B-64 #1661)** 新 control-plane 进程的第一个 run 卡死 44~63 秒被 liveness 杀:`expert_work.runtime.tokens` 首次在事件循环线程里持锁 `tiktoken.get_encoding("o200k_base")`,同步从境外下载 3.6MB(集群实测 56~63 秒),liveness 约 30 秒判死,每个新进程重来一遍(此前金丝雀连挂同源)。修:构建期把 o200k / cl100k 打进镜像 + `TIKTOKEN_CACHE_DIR`,lifespan 就绪前 `to_thread` 预热共享估算器。 |
| B-84 | **✅ 10-12 已上生产(`94dc73ba`,#1608 #1639 #1645 #1647 #1648 #1650 #1651 #1653 #1656;验收 #1657)** **对接方两个 Agent(ai-health-plan / sop2-designer)一轮 20 分钟**。**归因(09-20 实测,推翻「prefill 主导」)**:prefill 暖态约 32 ms/万 token,decode 35–63 tok/s 且不随上下文变慢,每 token 差约 2000 倍;真 run `7fad305b` 墙钟 decode 占 86.7%、工具 10.4%、prefill 2.9%。模型输出 97% 是工具参数,write_file / edit_file 占一半;`.py` 写入 50% 是跨 run 重打同一文件(`style/render_plan.py` 重打 6 遍)—— 平台没让模型知道工作区里已有什么,`list_dir` 走沙箱 60 天失败率 11%,失败后模型以为目录不存在。注:入出 token 比 ≠ 时间比;按 run 统计 token 要走 `agent_run.trace_id = token_usage.trace_id` 连表。**交付**:#1608 技能摘要去掉 `files=` / `version=`、`dir=` 换 `EXPERT_WORK_SKILLS_DIR`(技能块 −63~65%,墙钟每轮只省 0.6 秒,此方向不再投入);#1639 `update_plan` 的 `reason` 改可选 + 报错点出字段名、环境类工具失败不被读成「不存在」;#1645 全回合按「资源空间 + 路径」记工具失败并抵消(改了 B-85 ③ 判据);#1647 `skill_run_usage` 记 `viewed`,技能「绑了」与「被打开过」分开;#1648 `list_dir` / `read_file` 改走宿主 NAS + 新增 `search_files`(写操作仍走沙箱);#1650 每轮工作区树形摘要进提示词尾部;#1651 `edit_file` 失败后告诉模型下一步;#1653 技能体积护栏(第 7+8 条:eager 正文上限 + 索引段字符预算)。重放 `7fad305b`:1,178s → 222s,LLM 调用 24 → 13。**拍板 / 硬约束**:摘要只列当前 agent 自己的目录、**按目录摘要,不按时间或字典序排**(要防重打的是旧文件)、`WorkspaceFileEntry` 加 mtime;宿主侧读路径复用既有边界原语,判据比 `(st_dev,st_ino)` 不比路径字面量,symlink 不 resolve 出根,#1656 钉住「只读文件工具不许依赖沙箱」;NFS 跨 pod 实测 12–15 ms 可见(无 `noac`),以实测为准,别按 NFS 默认属性缓存的理论改回去;技能块若要降级:不丢条目(hermes)+ 明写告知模型(openclaw),别抄二分截断。设计 `docs/superpowers/specs/2026-09-20-workspace-visibility-design.md`。**未完成(待核):** 计划第 5 条「会话级环境事实块」未见对应提交;上线后按 09-20 基线做群体统计复算(拆开本波与 B-61 / B-67 / #1608 的功劳)未见记录。 |
| B-82 | **(2026-09-19 立项,用户拍板 09-24 前做掉)沙箱预装清单补高频包 + 建立重烤节奏**。现状其实不差:镜像已预装 15 个(pandas / openpyxl / python-docx / python-pptx / pypdf / pdfplumber / markdown / weasyprint / Pillow / matplotlib / pdf2image / imageio / imageio-ffmpeg / pydyf / defusedxml),与 Claude Code Interpreter 的清单(NumPy / pandas / Matplotlib / openpyxl / python-docx / python-pptx / pypdf)基本对齐,Manus 也是「常用库预装」。**但长尾装不完**:2026-09-18 一次真实对话里模型要 `pymupdf`(不在任何人的默认清单里),四次 pip 全超时,最后它自己退回用 `pdfplumber` 完成了任务。**行业判据不是「预装到全」,是「预装 + 可扩展」**:E2B 自定义 template 注册后从预构建快照启动、跳过全部装包;Modal 建议**把稳定依赖闭包烤进 template 并每晚重烤**;openclaw 走另一路 —— 只烤**工具链**(nodejs/python3/golang/rustc/cargo/build-essential + pnpm + bun + Homebrew),库留给运行期装。**本条做两件**:① 把 `pymupdf` 等高频缺口补进 `infra/sandbox-image/requirements.txt`;② 给「重烤」一个**触发信号**——#1518 已经证明这条手工发布路径没有信号就会静默烂掉(34 天)。**代价**:要走一次 `docs/runbooks/sandbox-image-release.md`,并按先例 #1233 真栈复验,不能只看 apply 成功。 **🔀 09-19 去数之后换了形状,拆成两半;③ 已交付,① 仍开着。** 原票假设是「缺包」,实测测试环境 60 天的真实 `pip install`(13 个 run / 14 次)结论相反:`python-docx` ×2(**镜像里本来就有 1.2.0**,白装)、`pymupdf` ×4(**唯一真缺口**)、`pytesseract` ×1(模型自己说了「没有二进制装了也没用」)。**采集口径的一处勘误**:另有 7 条命中 `weasyprint`,**那不是安装命令**,是 08-26/27 某脚本报的一句「weasyprint 未安装,无法生成 pdf」;活沙箱实测 **weasyprint 70.0 在镜像里,15 个钉版包只缺 `pymupdf`**,这 7 条不构成加包依据(我一度按 14 次里 9 次多余报过,量级错了)。**比次数更硬的证据是模型自己说的话**:「尝试 import docx,如果没有就 pip install。但沙箱可能不能联网装包。**让我先探测环境**」/「if it fails on import, I'll pip install」/「如需重算公式**再装 LibreOffice**」—— LibreOffice 是 apt 预装的。它在猜,而平台从来没告诉过它镜像里有什么(提示词、工具描述、系统提示词都没有)。**③ 把预装清单告诉模型 ✅ 已合(#1606 `bd69d496`)**:`sandbox_image_contract` 加两份清单 + 一个渲染函数,接进 `exec_python`/`bash` 描述,渲染出来 282 字符、落在系统提示词稳定前缀里走缓存读;三道漂移闸(双向比 `requirements.txt` 含顺序 / 单向比 Dockerfile 的 apt 段 / 渲染必须念全),变异自证三轮全被咬死。**口径写进了常量注释**:清单描述的是**部署中的镜像**,而闸比的是 `requirements.txt`,两者只在镜像钉子跟得上源码时才等价 —— 那道信号是 B-57 在 `smoke.sh` 立的 warn-only 钉子漂移检查,**所以往清单里加包 = 必须同时重烤并刷钉子,否则工具描述开始说谎**。**① 加 `pymupdf==1.28.2` 仍未做**:要走一次 `docs/runbooks/sandbox-image-release.md` 并按 #1233 先例真栈复验。**`pytesseract` 明确不加**:要连带装 `tesseract-ocr` 引擎和中文语言包(上百 MB),而 60 天只出现 1 次,证据不足。**② 重烤节奏**:不用新建 —— B-57 那道 warn-only 信号就是它。 |
| ~~B-81~~ | **✅ 10-12 已上生产(`94dc73ba`,#1602)** 沙箱 pip 没配镜像源,在阿里云杭州等于装不了包:真实对话里四次 `pip install pymupdf` 全部 `ReadTimeoutError`(PyPI 16 KiB/s);沙箱里没有代理、没有 index 配置,只有 apt 配了阿里云源。阿里云内网镜像(http)3863 KiB/s,快 240 倍。修:新增 `orchestrator.tools.sandbox.exec_envs`(平台级 + 每 agent / 每 run)注入 `PIP_INDEX_URL=http://mirrors.cloud.aliyuncs.com/pypi/simple/`、公网镜像 `mirrors.aliyun.com` 做备源与 `PIP_TRUSTED_HOST`(内网镜像只有 http,传输校验让给 pip 的 wheel 哈希校验)。不放 Dockerfile ENV(GitHub CI 构建解析不到内网地址),不放 `sandboxset.yaml` pod env(ACS / envd 未必继承 pod env)。合成时 per-agent 最后写,部署配置盖不掉 `PYTHONUSERBASE`(否则两个 agent 共享 `.local`)。测试环境经真实路径复验:`pip download pymupdf` 11 秒成功。 |
| ~~B-80~~ | **✅ 10-12 已上生产(`94dc73ba`,#1587 #1589)** **滚动发布打断在跑的对话**:热接管机制本来就有(租约过期后 `OrphanSweep` 从 checkpoint 接管),但关机时 `run_agent` 的取消兜底主动写 INTERRUPTED,`list_orphans` 只捡 running 且租约过期的行,于是永远接管不到。修:`RunStore.abandon_lease`(租约置 now、不碰 status,CAS 在 running ∧ 本副本持有);`RunManager.mark_shutting_down()` / `drain_runs`;`run_agent` 三条终局路径接关机交接(在关机 ∧ `replay_is_safe` ∧ CAS 赢),交接成功不发 end 帧;lifespan 顺序 mark → 停 worker → drain;正在关机的副本不接管孤儿。用户拍板(09-18):宽限期 300s、只交接可安全重放的(悬空不可重放工具照旧 INTERRUPTED);300s 是上限不是固定等待。**三个数字强耦合,改一个要一起核**:`EXPERT_WORK_RUN_DRAIN_TIMEOUT_S`(300)< `terminationGracePeriodSeconds`(360)< `tools/deploy` 的 `rollout status --timeout`(600s,`EXPERT_WORK_ROLLOUT_TIMEOUT` 可覆盖);deployment 用 `maxSurge: 2 / maxUnavailable: 0`。紧急回滚:`kubectl delete pod -l … --grace-period=0 --force`,被杀副本的 run 留在 running 由新副本接管(写在 `rollback.sh` 注释)。观测:`expert_work_run_handed_off_total` 在关机路径结构性采不到,主信号改用审计行(#1589,与接手方 `reason="reclaimed"` 同形)。 |
| ~~B-79~~ | **✅ 10-12 已上生产(`94dc73ba`,随 B-76 #1582)** Agent 主动请求审批(`ask_for_approval`)被批准后,模型收到的是 `[tool error] ask_for_approval must be handled by the approval gate…` 而不是「已批准」(续跑把它当普通工具派发),可能误以为出错而重复请求或放弃;只影响开了 `ask_for_approval` 的 Agent(默认关)。修:批准的提问由平台直接回 `[approved]`,改参时附审核人给的参数,长度上限 2048。 |
| ~~B-77~~ | **✅ 10-12 已上生产(`94dc73ba`,#1581)** 审批超时扫描遇到非 `AgentFactoryError` 的构建异常时整轮中止(#1581 把构建挪到 CAS 之前后,逐行处理只接 HTTP 错误),每轮卡在同一行,后面的过期审批得不到处理。修:逐行兜住所有异常,只记 run_id 与异常类型,下一轮重试。 |
| ~~B-76~~ | **✅ 10-12 已上生产(`94dc73ba`,#1582,顺带关 B-79)** 批准后同一轮里审批人没看到的调用也一起派发(安全,既有):① 一轮两个门控调用,审批单只显示第一个,批了第二个照跑;② `ask_for_approval` 与门控调用同轮,批了门控调用照跑;③ 动作审查的 `modify` 可能改写到另一个调用或落空。根因:`apply_resume_decision` 批准即派发整轮。修法(用户 09-17 拍板):批准只放行审批单上那一个,同轮其它调用回 `[not run]`「未执行,需要请重新发起」(门控的会各自再审批);发布前挂着、无法证明来源的旧审批批准后什么都不执行。发布顺序注意:新控制面写的裁定由未滚动的旧实例执行时仍放行整轮。 |
| ~~B-73~~ | **✅ 10-12 已上生产(`94dc73ba`,#1593 #1595 #1598)** B-67 小尾巴四条:① 控制台跨租户内容搜索会命中隐藏的「本轮输入」段(假阳性,不泄文本)→ `thread_message` 加 `hidden` 列(迁移 0156,expand-only),`extract_turns` 从真实 `additional_kwargs` 填,两个 store 的搜索都过滤;`hidden` 是唯一在 ON CONFLICT 时也写的列。写隐藏消息的源头有三个(B-67、CM-1 `<recovery-advisory>`、循环检测 `<system-reminder>`,后两者早已在生产),所以加迁移 0157 清空 `thread_message_sync` 水位,让 sweep 照检查点重扫(每 60 秒 200 个线程,一万线程约 50 分钟收敛,窗口期内镜像行一行不少);0157 有数据副作用,不 downgrade。② 审计视图前端原先丢掉 `hidden: true` 的行 → 新增 `PlatformRows` 折叠块,标「平台自动生成」。③ 压缩按消息条数留头尾,长的一轮里「本轮输入」段会被摘要掉 → 压缩后把最新一段原件放回提示词末尾(`INPUTS_BLOCK_MARK` 挪进 common)。④ 同时声明 `org` 与 `org_logo` 时文件前缀有歧义 → 改成指向清单里该变量自己的 `local_path`(untrusted 不把真名写进提示词,裁定 P16)。 |
| ~~B-72~~ | **✅ 10-12 已上生产(`94dc73ba`,#1593)** **「URL 后跟文字」的值整串被当成一个 URL**:宿主 `inputs_doc._is_http_url` 与沙箱 `prefetch_script._sites` 只看开头 → 链接名无扩展名、预拉失败,B-67 渲染的路径不存在。修:两侧都改成取第一段非空白(与渲染层 `prompt_render._render_url` 同义),宿主侧改为 `http_url_in` 返回那个 URL;等价测试覆盖两侧。对外文档已写「一个位置只放地址本身」。 |
| B-70 | **✅ 10-12 已上生产(`94dc73ba`,由 B-128 #1697 解决)** 每轮的 SystemMessage 在会话历史里逐轮累积,厂商侧合并成越来越长的一段(09-17 B-67 PR3 终审探针发现)。B-128 让 prompt 视图只保留最新一条系统提示词,即本条的修法。B-67 的「本轮输入」隐藏 HumanMessage 同样逐轮累积(`ai-health-plan` 约 450 ~ 580 tok/轮),当时判为可接受,未另修。 |
| B-69 | **(2026-09-17 记,留存 CronJob 造数据验证带出)两处留存「看不见」的缺口**。① **图片 90 天那条 pass 两个环境都不跑**:job 的对象存储后端是默认 `memory`(不持有 OSS 凭据,`cronjob.yaml` 头注写明),`image_upload` 的行与字节都没有回收路径,只进不出;② `retention_cleanup_job.done` 日志**没打 `tool_result_dirs_removed`**(报告字段有、日志行漏了),孤儿 `.tool_results` 回收只能看文件系统。① 要么给 job 接 OSS(走金库解析那条链),要么把图片回收挪进 control-plane;② 一行日志。不阻塞班车 2。 |
| ~~B-67~~ | **✅ 10-12 已上生产(`94dc73ba`,#1575 / #1576 / #1578 / #1579 / PR5 #1580;设计 #1570)** 本轮输入由平台整段接管 —— 写模板的人不必再写「inputs 里有 xxx」(起因:用户 09-16 三问 —— 变量写进文件是否合适、靠人记得写提示词不可靠、「有些写有些不写」后人没法判断;参照 hermes-agent / openclaw:作者文件无变量语法,「本轮来了什么」由平台在固定位置写)。四件事,独立可发可回滚:**A** 预拉文件按变量名建符号链接(`inputs/<run_id>/org_logo.png → ../cache/<digest>`)+ `EXPERT_WORK_INPUTS_DIR` + JSON 字符串值解析成 `value_parsed`;**B** `{{ var }}` 按值的形态渲染(URL → 本地路径、列表逐项、其它原值;被绑定的变量同样),存量模板零改动,唯一行为变化是 URL 不再以原文进提示词;**C** 平台生成「本轮输入」段(只有名字 / 说明 / 状态 / 路径,零值)作隐藏 HumanMessage 贴在用户消息之后,不进对外 `/messages`;**D** 手抄守卫:`exec_python` / `bash` 代码出现输入 URL 原文或近似(同 scheme 与 host,之后编辑距离 ≤ `min(3, 尾串长度 // 16)`)→ 只拦这一条调用,提示改用 `$EXPERT_WORK_INPUTS_DIR` 或从清单读;守卫出错或比较预算用完一律放行;审计 `tool:blocked` `reason=input_url_retyped`,**KPI = 命中次数趋零**,有真实误拦再收紧。效果(N=10):提示词含 LOGO 原 URL 10/10 → 0/10,对原地址请求报错 7 次 → 0。对接方 addendum「必改」5 处 → 0。spec `docs/superpowers/specs/2026-09-17-inputs-platform-owned-design.md`。衍生 B-70 ~ B-74、B-80。 |
| B-66 | **✅ 10-12 已上生产(`94dc73ba`,#1572)** **`:regenerate` 命中 E.13 响应缓存,原样返回旧答案且不调模型;缓存命中的 run 对外用量为 `None`**:缓存键 = 租户 + 模型 + 归一化消息 + temperature + max_tokens(temperature ≤ 0.1 才走,TTL 3600s,`middleware/llm_cache.py`),regenerate 原样重放 `[System, Human]`,键必然相同。修:regenerate 的 run(stream 与 queue 两个入口)跳过缓存查找、写入照常(`:edit` 不受影响);命中缓存时记一行全 0 用量。**对外变化**:用量从 `None` 变成「模型 + 各项 0」(全 0 = 回答缓存命中),重新生成一定重新调模型,对外文档已写。 |
| ~~B-65~~ | **✅ 10-12 已上生产(`94dc73ba`,#1594)** **MCP 参数绑定被 wire 名撞掉时没有任何信号**:registry 以折叠后的 wire 名为键(非 `[a-zA-Z0-9_-]` 折 `_`、截断 64),按名覆盖注册,两个工具折成同名时后者把前者的 B-61 绑定清掉,参数回到模型手里。可达形状:两个长工具名在 64 字符处截断成同名、服务器重复广播同一工具名、服务器名带 `__`。修:`note_landed_arg_binding` 记 `(server, tool) → wire 名`(wire 名传下来不重算,B-61 裁定 X 仍成立),判定时回查名字是否还在;`tool_found: bool` 换成三态 `reason`(`tool_missing` / `params_absent` / `name_collision`),dataclass、控制面文案、运行期日志三处一起改;参数全漂的绑定做了豁免,避免同时挂两个矛盾理由。 |
| ~~B-64~~ | **✅ 10-12 已上生产(`94dc73ba`,#1661;设计 #1660)** 文档里的图片完全读不到且是静默的:`read_document` 只做纯文字抽取(PDF 文字层、docx 段落与表格、pptx 文本框、xlsx 单元格),扫描件抽出空串,关键信息在图里时模型拿着残缺内容往下做而不知道。平台本有视觉通道(`supports_vision` 或 manifest `vision:` + `ask_image`),缺的是把文档里的图交给它。方向(用户 09-16 拍板,照 hermes-agent 的形状):前期一张图都不抽,只按每页文字量生成图清单 / 缺口图(标注缺口前最后一段文字),模型挑中哪几页再 `read_page` 渲染;`ask_image` 扩为可收工作区 / 沙箱路径(短形态 path+unit,路径校验同 `artifact._validate_path` 一样严),渲染页保持临时、不进图片库(避免配额 / 保留期问题)。另含看图用量记账、`ask_image` 单次 120 秒、默认快看、分词表进镜像(B-106)。效果:ai-health-plan 不改配置读 10 页全扫描 PDF 全部答对,看图单次 9~234 秒 → 2~4 秒。遗留见 B-90~B-105。 |
| B-61 | **✅ 10-12 已上生产(`94dc73ba`,数据面 #1557 / #1559,绑定面 #1561 / #1563,文档 #1562)** 注入变量按引用给沙箱,别让模型手抄:ai-health-plan(glm-5.3)三轮三次把提示词里正确的 `org_logo` URL 抄错写进 `exec_python`(`1789…`→`17889…`),404 后反让员工「更换地址」。平台默认、不 opt-in。分流判据:变量的消费者是沙箱代码 → `inputs.json` / 预拉文件;是工具参数 → 绑定。**数据面**:声明变量落 `agents/<key>/inputs/<run_id>/inputs.json`,URL 值在沙箱内预拉(受该 agent `sandbox.network` 约束)到内容寻址共享缓存 `inputs/cache/<sha256(url)[:32]><ext>`,`EXPERT_WORK_INPUTS` 走 per-exec env;janitor 回收 `inputs_cache` 7 天、`inputs_run_dir` 30 天(`uploads` / 产物两条 `enabled=False` → B-63);续跑撞上 `local_path` 已回收是被接受的降级(`value` 留原始 URL),消费方判据必须是「为空**或**本地不存在」。**绑定面**:manifest `arg_bindings` 进 `MCPToolSpec`,被绑参数从给模型的 JSON schema 里剥掉,`tools_node` 在审批门与 action screening 之前填值(审批人与 judge 看到真参数),审计只记参数名;下发给 MCP 的校验用剥之前的 schema(`ToolSpec.dispatch_parameters`);保存时试建比对,对不上只告警不 422;配置页 MCP tab 逐参数「自动 / 绑定变量」。**回滚坑**:`MCPToolSpec` 是 `extra="forbid"`,旧版本读到带 `arg_bindings` 的 manifest 校验失败,要回滚先清绑定配置。**对外文档**:`apps/admin-ui/docs-site/guide/chat.md` §2.7「输入是怎么到达 Agent 的」;对接方 `docs/design/agent-config-1235-addendum-b61.md`(硬规则:地址、编码这类长串永远不许从工具输出抄进工具参数或代码);对接方改提示词是第二步,自己挑时间,平台不设时间表。「同名参数全局绑定」已在设计 §十一 否决(同名不同义会静默灌错值)。衍生 B-62 / B-63。 |

## D · 调试台 program follow-up —— D-1~D-6 ✅ 全清(D-1~D-4 于 2026-08-20,D-5/D-6 于 2026-08-23);**仅剩 D-7**

> 11-12:D-7 仍未做(代码里没有 `continuation_of`),已挪到主清单「其它 program 的挂起项」表;D-1~D-6 原文留在下面。

| # | 事项 | 结果 |
|---|---|---|
| D-1 | Run 详情页轨迹「真配对」e2e 用例 | **✅ 09-07 已上生产(`2d5742c6`,#1230)** `e2e/run_detail.spec.ts` 增真配对用例(配对 messages / runs + 真 SSE 回放体 → 账本行断言)。顺带修了桩:messages / runs 桩用裸 glob 匹配不到带 `?tenant_id=` 的真请求,改为按 pathname 精确匹配,桩第一次真正生效 |
| D-2 | RunDetail 切线程竞态守卫(Ruling 4) | **✅ 09-07 已上生产(`2d5742c6`,#1230)** `convoTenant` 状态改为 `{threadId, tenantId}` 带来源线程标签,history 效果拒绝错线程的 tenant |
| D-3 | `runs_page.summary_resume` 死 i18n 键 | **✅ 09-07 已上生产(`2d5742c6`,#1230)** 三处(en 类型 / en 值 / zh-CN)全删,全仓零残留 |
| D-4 | `RecordDetails.tsx:241` 既有占位行空 `<dt>` | **✅ 09-07 已上生产(`2d5742c6`,PR-B #1221 已顺手修掉,#1230 仅核实)** 占位行已是 `<dl>` 外的 `<p>` |
| D-5 | ~~**对话详情页:按轮重建 + 尾轮 live**~~ | **✅ 09-07 已上生产(`2d5742c6`,#1254,与 D-6 同 PR)** 配对容忍尾部连续非终局块(running 新轮 / paused 等审批 / 刚起的续跑);尾轮 eager live attach(rAF 合批,end 帧后静默刷新);重建键改稳定的 (thread, tenant) 键 + 增长触发一次性重配对。中段 paused 维持诚实降级 → D-7。已知限制:live 订阅本进程 InMemoryStreamBridge,多副本下 run 在别的副本时只有一次性补库(断流 5s 重挂反复补库),真正跨副本 live 归 X-10 |
| D-6 | ~~**对话详情页操作面:批/拒/取消**~~ | **✅ 09-07 已上生产(`2d5742c6`,#1254;decide 后端闸随 #1253)** paused 末轮就地批 / 拒(从 approval 帧合成卡;有后继 run 不合成,防 409);running / pending 尾 run 可取消,新建 `POST /v1/sessions/{t}/runs/{r}:cancel`(复用 tenants 批量取消内核,operator+ 闸,自带 `require(session,write)`;paused 回 409 走审批路)。角色闸:operator / admin + 本租户 |
| D-7 | **审批 continuation 链折叠**(2026-08-23 立项,D-5 裁定的遗留) | 一条用户消息对应 paused 父 run + continuation 子 run(可链式),count 配对对此天然歧义;continuation 终局后硬刷新回落扁平视图(与今日行为一致,非回归——decide 后的当场富视图已由「刷新只 patch 不重配对」保住)。做法:① `GET /{thread}/runs` 补 `continuation_of`(join approval 行的 `continuation_run_id`;ApprovalStore 需补 list_for_thread);② 前端把链折叠进 owner 轮——注意各 run 的 seq 独立编号,直接 concat 违反 usePlanCard 严格递增帧约束 |

## 建议顺序(2026-08-31 更新)

> 11-12:本节仍未做的条目(B-40、P-3 / P-4、X-1、X-10、B-176、B-179、RLS PR B)在主清单各有一行;其余已完成。「PPT 内容质量人工抽查」是 08-27 批真栈验收留给用户侧的一项,此后 PPT 相关能力已换成 B-116 自写 office 技能与 B-125 / B-296(各有自己的验收),按过时处理。

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
- ~~**B-175 知识库 v2 整体优化(后端 · 管理端 · 开放 API,取代 B-171 / B-172)**(2026-10-10 登记)—— 设计 + 计划已出,开工等用户指令;开工前先拍设计 §9「开工前必拍」5 项,并提供一家客户真实资料包 + 50~100 条真实问题。~~ → **10-13 已开工**;剩余拍板项挪到各阶段开工前(#3 → P3 管理端;#1 #2 #4 → P6)。
- **B-176 长期记忆 v2 整体优化(后端 · 管理端 · 开放 API,收编 B-167 / B-168 遗留)**(2026-10-10 登记)—— 设计 + 计划已出,开工等用户指令;开工前先拍设计 §9.1「开工前必拍」5 项,并做生产库记忆使用量实查。
- **B-179 内容安全接入(输入 / 输出检测、策略与敏感词、复核、投诉举报、评测、知识库、生成内容标识)**(2026-10-12 登记)—— 设计 + 计划已出,开工等用户指令;开工前先拍设计 §9 的 6 项,M0 先在测试环境打供应商探针(延迟、健康类误杀率、数据留存、词库规模证明)。

**发布后第一波(已排队)**:RLS PR B / Redis 全局令牌桶 / 取消亚秒化 / X-1 / X-10。
