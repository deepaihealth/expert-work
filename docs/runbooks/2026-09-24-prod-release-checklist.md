# 班车 2 生产发布执行单（2026-10-08 发；原定 09-24，后改 09-28）

> **⚠️ 2026-09-29 再次改期**：09-28 没有发，**用户 09-29 拍板推迟到 2026-10-08**，并把 **B-125 健康方案交付件技能**（`health-plan-report` 导入 + ai-health-plan 提示词 rev39）一起带上，见 §0.2 与 Step A3 / A4。钉子当时未变（`c0789ca6`），B-125 不进镜像（`platform-skills/` 只由导入脚本上线）。**同日用户再拍板：B-126（跨轮上下文降本）也随本班上生产；钉子等 B-126 合入后一次性前移到 main**（届时连同 #1690 fast-uri / #1691 MinIO 一起带上），前移后先发测试环境验过再重钉，本单操作位随之改。**✅ 09-29 已前移（第十一次重钉）到 main `ab4097cf`**：带 B-126 / B-127 / B-128 与 #1690 / #1691，测试环境验过，操作位已全部改到新钉子，见 §0.1 第十一次重钉。**✅ 09-30 第十二次重钉到 main `39c93912`**：用户 09-30 拍板 B-131（减少修改轮的额外调用，平台部分）随本班上，同时带上 pyjwt 2.14.0 安全升级（#1701）；测试环境验过，操作位已全部改到新钉子，见 §0.1 第十二次重钉。**✅ 10-01 第十三次重钉到 main `31f22602`**：用户 09-30 拍板新模型 glm-5.3-flashx（#1702）随本班上，同时带上三个依赖安全升级（urllib3 #1703、pyjwt 2.15.1 #1705、virtualenv #1706）；测试环境验过，操作位已全部改到新钉子，见 §0.1 第十三次重钉。**✅ 10-01 第十四次重钉到 main `54d1ed70`**：用户 10-01 拍板子智能体委派三项修复（#1710 计划不再被旧 PLAN.md 盖掉 + 委派标记保留；#1709 子代容错；#1708 委派说明对齐事实）随本班上；测试环境验过，操作位已全部改到新钉子，见 §0.1 第十四次重钉。
>
> **⚠️ 2026-09-25 改期说明**：本班 09-24 **没有发**（用户 09-20 拍板等 B-84 本波做完一起发，
> 之后又陆续合入 B-64 / B-106 / B-102~104 / B-105）。**用户 09-25 拍板 09-28（周一）发**。文件名保留原日期，免得外链断；**发布日以表头为准**。
> 这一版把 09-20 之后合入的全部内容一并带上，钉子重钉到 `f92c6fae`（第七次重钉，见 §0.1）；09-27 再在它上面只叠一个 B-121 沙箱时区修复，钉子为 `e5341495`（第八次重钉）；同日第九次重钉到 main 的 `68a7b75f`，再带上 B-122 / B-123 与 B-119 的文案；当晚第十次重钉到 main 的 `c0789ca6`，再带上 B-124 子代沙箱工作区修复；09-29 第十一次重钉到 main 的 `ab4097cf`，再带上 B-126 / B-127 / B-128；09-30 第十二次重钉到 main 的 `39c93912`，再带上 B-131 与 pyjwt 升级；10-01 第十三次重钉到 main 的 `31f22602`，再带上 glm-5.3-flashx 与三个依赖安全升级；同日第十四次重钉到 main 的 `54d1ed70`，再带上子智能体委派三项修复（见 §0.1）。
>
> 一次性文档，发完归档。通用流程在 [`production-release.md`](./production-release.md)，
> **这份只列那一份不覆盖的东西**。上一班的单子在
> [`2026-09-14-prod-release-checklist.md`](./2026-09-14-prod-release-checklist.md)（已归档）。

| | |
|---|---|
| 发布日 | **2026-10-08（周四）** —— 用户 2026-09-29 拍板（09-28 未发）。此前 **2026-09-28（周一）** —— 用户 2026-09-25 拍板。原定 09-24（用户 09-17 拍板，原 09-22），09-20 用户拍板延期等 B-84 本波 |
| 上一版 tag（回滚用） | **`5775fbf3`**（班车 1 的 B2，2026-09-16 18:51 上线） |
| 本版 tag | **`54d1ed70`**（**main**，#1708 的合并提交，父提交 `6eba2435`）= `31f22602` + 其后 main 上的 4 个提交：子智能体委派三项修复（#1710 / #1709 / #1708）与执行单本身（#1707）。测试环境 2026-10-01 发过（`release.sh test`，镜像 `54d1ed70`、admin-ui 沿用 `f29ac13b-test` —— 三个 PR 都不碰 `apps/admin-ui`）；smoke PASS；金丝雀首跑客户端读流 `ReadError`（服务端该 run 32 秒 success，0 重启），单独重跑 PASS；委派两轮探针 + Step C 命令通过，见 §0.1 第十四次重钉。**以下为第十三次钉子的记录**：`31f22602`（**main**，#1702 的合并提交，父提交 `f3cfa4f7`）= `39c93912` + 其后 main 上的 5 个提交：新模型 glm-5.3-flashx（#1702，模型目录加一条，能力位与 glm-5.3-flash 逐项相同）、`uv.lock` 三个依赖安全升级（#1703 urllib3 2.7.0 → 2.8.0；#1705 pyjwt 2.14.0 → 2.15.1；#1706 virtualenv 21.3.1 → 21.7.13，只是开发工具 pre-commit 的依赖、不进镜像）与执行单本身（#1698）。测试环境 2026-10-01 发过（`release.sh test`，镜像 `31f22602`、admin-ui 沿用 `f29ac13b-test` —— `git diff f29ac13b 31f22602 -- apps/admin-ui` 为空；smoke + 金丝雀 PASS；Step C 符号 / 版本核对命令在测试 pod 实跑输出 `ok`；见 §0.1 第十三次重钉）。**以下为第十二次钉子的记录**：`39c93912`（**main**，#1700 的合并提交，父提交 `63a0600c`）= `ab4097cf` + 其后 main 上的 2 个提交：B-131（#1700，减少修改轮的额外调用：计划打勾与下一步动作同一次回复、自然答完时平台把未完成步骤标完成；另含 `health-plan-report` 技能 v6，不进镜像）与 pyjwt 2.13.0 → 2.14.0（#1701，只改 `uv.lock`，修 2026-09-30 公布的 10 个 CVE）。测试环境 2026-09-30 发过（`release.sh test`，镜像 `39c93912`、admin-ui 沿用 `f29ac13b-test` —— `git diff f29ac13b 39c93912 -- apps/admin-ui` 为空；smoke + 金丝雀 PASS；`health-plan-report` 技能在 pod 内 dry-run 为 `786d30fe…`、unchanged；见 §0.1 第十二次重钉）。**以下为第十一次钉子的记录**：`ab4097cf`（**main**，#1697 的合并提交，父提交 `78abe0f3`）= `c0789ca6` + 其后 main 上的 13 个提交：运行时代码 B-126（#1694 / #1696，跨轮上下文降本）、B-127（#1695，MCP 工具参数固定值，含 admin-ui 配置页）、B-128（#1697，prompt 只留最新一份系统提示词）与 admin-ui 依赖 fast-uri 升版（#1690）；其余是 `platform-skills/`（B-125，不进镜像）/ CI / 文档。测试环境 2026-09-29 发过（`release.sh test`，镜像 `f29ac13b`、admin-ui `f29ac13b-test`，代码树与 `ab4097cf` 逐字相同 —— `git diff f29ac13b ab4097cf` 为空；smoke + 金丝雀 PASS；ai-health-plan 真实 7 轮对话重放 7/7 success，见 §0.1 第十一次重钉）。**以下为第十次钉子的记录**：`c0789ca6`（**main**，#1685 的合并提交，父提交 `68a7b75f`）= `f92c6fae` + 其后 main 上的 11 个提交：运行时代码只多 B-121（#1681，与第八次钉子 `e5341495` 同一份改动）、B-122（#1682）、B-123（#1683）、B-124（#1685）与 B-119 的一处文案（#1678），其余是文档 / `platform-skills/`（不进镜像）/ test overlay。**不再用分支 `release/train2-tz`**（发版不从它取，留不留不影响本单）。测试环境 2026-09-27 发过 `c0789ca6`（`release.sh test`，镜像 `c0789ca6`、admin-ui `c0789ca6-test`；smoke + 金丝雀 PASS；B-124 真栈四条全过，见 §0.1 第十次重钉）。第九次钉子 `68a7b75f` 此前 09-27 发过（`release.sh test`，镜像 `68a7b75f`、admin-ui `68a7b75f-test`）：测试环境 09-27 发 `68a7b75f`,smoke + 金丝雀 PASS;B-122/B-123 真栈 4 条全过(子智能体无 save_artifact、能调深护智康只读工具、写工具 `kept=31 dropped_write=9`)。第八次钉子 `e5341495` 09-27 测试验过（smoke PASS + 金丝雀产物链 PASS；沙箱 `date` 打出 CST，#1681）；`f92c6fae` 本身 09-25 测试验过（记录 #1667 / #1670） |
| 区间提交数 | **144**（`git log --oneline 5775fbf3..54d1ed70`；第十三次钉子 `31f22602` 时为 140，第十四次多 4 个 —— #1707 / #1710 / #1709 / #1708；第十二次钉子 `39c93912` 时为 135，第十三次多 5 个 —— #1698 / #1703 / #1705 / #1706 / #1702；第十一次钉子 `ab4097cf` 时为 133，第十二次多 2 个 —— B-131 / pyjwt；第十次钉子 `c0789ca6` 时为 120，第十一次多 13 个 —— B-125 四个 + B-126 两个 + B-127 / B-128 / fast-uri / MinIO CI 各一个 + 两个文档；原记录：其中 74 个是原班车 2，35 个是 09-20 之后追加，见 §0.1，再加 `f92c6fae` 之后 main 上的 11 个 —— B-121 / B-122 / B-123 / B-124 / office 技能两个 / 五个纯文档或 test overlay 记录） |
| 数据库迁移 | **四条**（`0159` 为 09-20 之后追加）：`0159_skill_usage_viewed`（只放宽 `skill_run_usage.outcome` 的 CHECK，允许 `viewed`；不加表不加列不动数据）+ 原三条：`0156_thread_message_hidden`（expand-only，`thread_message` 加 `hidden` 一列带默认 `false`）+ `0157_thread_mirror_resweep`（**数据迁移**，一句 `DELETE FROM thread_message_sync`）+ `0158_run_completion`（expand-only，`agent_run` 加 `completed` / `exit_reason` 两列，**可空、不回填**，B-85 ③）。migrate Job 自动跑，不需要额外动作 |
| 段数 | **单段**。有迁移但不是三段式：`0156` / `0158` 纯加列、`0157` 只清一张派生状态表，都没有数据搬迁、没有 expand/contract 关系，新旧两版代码都能在这些表上正常跑 |
| 回滚纪律 | **只回镜像，不要 `alembic downgrade`。** 多一列对旧版本无害（旧 ORM 不映射它，既不 SELECT 也不 INSERT，`server_default` 兜住）；downgrade 会把新版本写进去的 `hidden` 全抹掉，而回滚窗口里随时可能再滚回来。`0157` 的 downgrade 是空转，`downgrade -1 && upgrade head` 会把那句 DELETE **再跑一遍**（只是多触发一次全量重扫，不丢数据，但没必要）。`0158` 同 `0156`：两列可空、旧代码不读不写，多两列对旧版本无害；downgrade 会把新版本写进去的 `completed` / `exit_reason` 全抹掉。`0159` 同理：旧代码从不写 `viewed`，放宽的 CHECK 对它无害；downgrade 会先删掉全部 `viewed` 行 |
| 沙箱镜像钉子 | `e8aac104` → **`7ac31957`**（Step A）。瘦身 + 只建 amd64，测试实测 619.4→433.7 MiB、拉取 66.19s→40.55s |
| 新增集群对象 | **留存清理 CronJob `retention-cleanup`**（首次进 prod overlay，`apply -k` 会创建） |
| 执行人 / 开始时间 | `___________` |

---

## 0. 本版装载

- **B-61 注入变量按引用 + 工具参数绑定**（#1556/#1557/#1559/#1561/#1562/#1563）：声明变量落
  `inputs.json`、沙箱内预拉、变量可绑定到 MCP 工具参数由平台填值。测试环境真栈验收 39/39。
- **B-67 本轮输入由平台整段接管**（#1570/#1575/#1576/#1578/#1579/#1580）：预拉文件按变量名建链接、
  提示词里链接换本地路径、用户消息后贴隐藏「本轮输入」段、沙箱代码手抄 URL 的守卫。
- **#1577 续跑带本轮输入**（班车 2 阻断项）：审批续跑 / 孤儿复活 / 重新生成三条路径都带上这一轮输入。
- **审批两处安全修复**：#1581（等审批时发新消息会绕过审批 → 新一轮作废上一轮待审批，含 B-77）、
  #1582（批准会放行审批人没看到的调用 → 只放行审批单上那一条，含 B-79）。
- **B-66**（#1572）：重新生成不再命中响应缓存；命中缓存时记一行零用量。
- **RLS 补丁**（#1573）：三个长周期后台任务每次库访问都带显式租户上下文。
- **留存清理 CronJob 接入 prod overlay**（#1574）+ 钉 `timeZone: Asia/Shanghai`、`schedule: 23 3 * * *`。
- **B-80 滚动发布不再打断在跑的对话**（#1587/#1589，2026-09-18 追加）：关机时先让在跑的
  对话自己跑完，到点没完的交给别的副本从存档点接着跑；不可安全接管的才收成 `interrupted`。
  交接会落一条审计（`run:failover` / `handed_off`），与接手侧的 `reclaimed` **同形**，
  按 `run_id` 查一次就能看出「谁交出去的 → 谁接的手 → 隔了多久」。
  连带改了收口上限（300s）、容器关机宽限期（360s）、滚动策略（两个新 pod 一次起齐）
  和发布/回滚脚本的等待上限（600s）—— 见 §2 第 2 条。
- soupsieve `2.8.4 → 2.9.2`（#1586）：CVE-2026-85999 / 86000。本仓库不在受影响路径上
  （上游两个包都不调 `.select()`），是把 CI 的 pip-audit 扫绿。
- **B-56 滚动发布慢不等于发布失败**（#1592，2026-09-18 追加）：`rollout status` 超时时，
  脚本先打印证据（该 Deployment 的状态、它自己的 pod 行、它自己的事件尾 15 行），
  再分两支说明怎么读 —— **这条发布当天你会直接用到，见 §2 第 3 条**。
  同时把 control-plane 的 `startupProbe.failureThreshold` 从 30 提到 90（60s → 180s）。
  **这是本批唯一一条改生产 Deployment 清单的改动。**
- **B-72 / B-65 —— B-61 与 B-67 自带的两个洞**（#1593 / #1594，2026-09-18 追加）：
  B-72「地址后面跟一句说明」的值整串被当成地址（预拉必失败、链接名没扩展名）；
  B-65 两个长工具名在 64 字符处折成同一个内部名时，后注册的把前者的参数绑定**静默**顶掉。
  这两个特性 24 号才第一次上生产，不带的话生产会带着已知洞跑。
- **B-80 第二批 —— 排空中的旧 pod 不算发布失败 + 排空过程落审计**（#1599，2026-09-19 追加）：
  **发布当天你会直接用到** —— 上一版里，正在排空的旧 pod 会被发布脚本算进「有 pod 不健康」，
  于是一次正常的滚动更新被判成失败。这条把判据改对，并让排空过程自己落一条审计。
- **anyio `4.13.0 → 4.14.2`**（#1605，2026-09-19 追加）：`CVE-2026-63374` / `CVE-2026-64847`。
  只改 `uv.lock`（anyio 没有直接钉版，是 httpx / mcp / sse-starlette / starlette / watchfiles
  五个包共同拉进来的传递依赖）。锁里出现**两个** anyio 是预期的解析分叉：
  `python_full_version < '3.15'` → **4.14.2**（运行时镜像与 CI 都是 python 3.12，落地的是这一支）、
  `>= '3.15'` → 4.15.1（本仓库永远不会实例化）。五个上层包版本一个没动。
- **B-81 沙箱 pip 走阿里云镜像源**（#1602，2026-09-19 追加）：**本批第二条改生产配置的改动**（第一条是 B-56 的
  `startupProbe`）。`overlays/prod/configmap-patch.yaml` 新增三个
  `EXPERT_WORK_SANDBOX_PIP_INDEX_URL` / `_EXTRA_INDEX_URL` / `_TRUSTED_HOST`，随 `apply -k` 一起上，
  **不需要额外手工步骤**。实测：沙箱里 pip 走官方 CDN 只有 16 KiB/s（24.6 MiB 的 wheel 要约 26 分钟，
  必然 `ReadTimeout`），走阿里云内网镜像 3863 KiB/s、同一个 wheel 2 秒。
  内网地址只有 http，所以必须给 `PIP_TRUSTED_HOST` —— 放弃的是传输层校验，**兜底的是 pip 自己的 wheel
  哈希校验**，且链路在阿里云内网不过公网。这个取舍是明写的，不是疏忽。
- **B-82 沙箱预装清单进工具描述**（#1606，2026-09-19 追加）：纯代码。把镜像里**已经预装**的 15 个 Python 库
  和命令行工具告诉模型，省掉它「先探测环境」或干脆先装一遍（在阿里云上装一遍是一两分钟）。
  ✅ **原来这条写着「下一次重烤镜像（砍 apt ffmpeg / 砍 npm）时必须同步改
  `sandbox_image_contract.py`」—— 那次重烤就是本版的 B-55 #1623，已经同步改了**：
  命令行工具从 5 个变成 4 个（`npm` 摘掉），`ffmpeg` 的来源从 apt 标成 wheel，
  并加了一句**否定**断言告诉模型沙箱里没有 npm（`docx` / `pptx` 两个平台技能的正文
  明写 `npm install -g`，只摘掉不说明的话模型照技能正文去敲仍然白跑一轮）。
  那道漂移闸也跟着改成按**来源**分别验，这正是这个契约模块存在的理由。
- **B-84 技能摘要瘦身**（#1608，2026-09-19 追加）：纯代码。技能块砍掉 63~65%（两个 agent 实测
  9,269 / 11,438 字节），整个系统提示词 −33~50%。**摊到每次调用的输入上约 −7%**
  （`ai-health-plan` 入均 48,376 token）—— 三个数都对，只有第三个是用户体感，报的时候别放大。
- **B-58 孤儿重收先确认有东西可续**（#1611，2026-09-19 追加，用户拍板带上）：
  一个 run 被另一副本重收（`reclaim_count=1`）之后立刻 `error`，报
  `EmptyInputError: Received no input for __start__` —— 一句**与事实矛盾**的错误
  （这一轮明明有输入，只是重收那条路没把它带上）。成功的 run 一律 `reclaim_count=0`，
  失败的一律 `=1`，判据干净。伴随 `turn_inputs.prompt_frame_missing` 与
  `run_event` 的 `duplicate key (run_id,seq)=(…,0)`。
  **对用户的样子**：对话跑到一半，平台自己换了个副本接手，然后告诉你「没有输入」。
  纯代码，无迁移、无配置。
- **B-85 ①② 沙箱认领撞并发时平台自己等**（#1618，2026-09-19 追加，用户拍板带上）：
  两个 acquire 同时进来，CAS 只能有一个赢家，**输家收到的是一条它无法采取行动的内部竞争
  错误**，而错误分类器的关键词表一条都不匹配 → 判 `unknown` → advisory 告诉模型
  「别重试同一个调用」→ 模型放弃 → **run 报 `status=success` 而产物为空**（2026-09-19
  金丝雀实况）。修法两层：平台自己等赢家把沙箱建好再复用它（上限 120s，每 2s 回看），
  等满才抛的异常是**双基类**（同时是 `TimeoutError`，让分类器按**类型**而不是按关键词判
  `transient`）。纯代码，无迁移、无配置。
- **B-85 ③ run 做没做成要有独立信号**（#1636，2026-09-20 追加，用户拍板带上）：
  `status=success` 只表示「图跑完没抛异常」，**不表示「事做成了」**。①② 修的是一个
  具体来源，而形态与来源无关 —— 任何工具失败之后模型不再动作，都会复现同一次静默假绿。
  做法：图的每个出口由**知道答案的那一行代码**盖一个 `exit_reason`，`tools` 节点每批
  记下未解决的工具失败，终局算出 `completed` 落库 + 落审计 + 随 `end` 帧发出去。
  **`status` 的取值与语义一个字没动。**
  ⚠️ **本条与本班其它条目形态不同，发布前必须知道三件**：
  1. **带一条迁移** `0158_run_completion`（expand-only，`agent_run` 加 `completed` /
     `exit_reason` 两列，**可空、不回填**）—— 本班迁移因此从两条变成**三条**。
     不回填是刻意的：`NULL` = 这两列上线前的老 run，回填 `false` 会把跑得好好的历史 run
     说成没做成，回填 `true` 会把当年真出过这个问题的那些洗白。
  2. **是对外契约变更（追加字段）**：`end` 帧多 `completed` / `exit_reason`。对接方
     非 strict 解析，不读也不会坏；但**判成功的口径要改**成
     `status === "success" && completed !== false`。通知稿已备好，**等真栈验过再发**。
  3. **不新增 SSE 事件类型** —— 对接方的流处理有事件白名单，新事件类型会被静默丢掉
     （`item.*` 那轮的头号坑）。所以信号是挂在已有 `end` 帧上的字段。
- **B-55 冷建沙箱 —— 三条一起**（#1623 / #1624 / #1625，2026-09-19 追加）：
  这批的根因不是「慢」，是**建不出来**。去数 `sandbox_instance` 528 行：`create_failed`
  **99 行 = 18.8%**，09-19 当天 **15 次里 13 次失败 = 87%**，每一条的存活时间都恰好 **60 秒**，
  报错正文是 `504 Gateway Time-out … <center>alb</center>`。CI 的
  「Contract suite against the real E2B test cluster」是同一个受害者。三条各修一段：

  | | 改什么 | 量到的 |
  |---|---|---|
  | #1625 | ALB **80 端口**监听补 `requestTimeout: 180`（原来没设，吃默认 60s） | 冷建要 66~112s > 60s，这是 504 的直接原因 |
  | #1624 | 沙箱镜像引用改走 ACR 的 **`-vpc`** host | 同一个 619MB 镜像：公网 112s → VPC **66.19s** |
  | #1623 | 镜像砍掉 apt `ffmpeg`（145 包/387MB）与 `npm`（359 包/133MB） | 解包 1263MB → 742MB，压缩 619MB → 预计 ~435MB |

  **三条缺一不可**：只修 ALB，冷建仍要 112s；只改 VPC，66s 还是过不了 60s 的闸。
  ALB 是把闸打开，另外两条是把余量做厚。

  ⚠️ **#1625 是本批第三条改生产集群配置的改动**（前两条是 B-56 的 `startupProbe`、B-81 的
  pip 镜像源），而且**不走 `apply -k`** —— AlbConfig 与 acr-pull Secret 都是手工对象，见 §2 第 7 条与 Step A0。

  ⚠️ #1623 顺带改了 `sandbox_image_contract.py`（`npm` 从预装清单摘掉、`ffmpeg` 来源标成 wheel），
  正是 B-82 那条注释预告的「下一次重烤镜像时必须同步改」。所以**沙箱镜像钉子这次必须动**。

- **B-73 四条小尾巴**（#1593 + #1595，2026-09-18 追加）：压缩不再把「本轮输入」段摘要掉、
  隐藏段的文件名前缀歧义指向清单、**平台脚手架行不进控制台内容搜索**（带迁移 `0156`）、
  审计视图里这些行渲染成折叠的「平台自动生成」块。

### 0.1 09-20 之后追加（2026-09-25 第七次重钉带上）

全部已合 main、已在测试环境真栈验过。按「对生产行为的影响」从大到小排：

- **B-105 输出上限 / 思考上限**（#1666，09-25 测试 `8eaed4ba` 真栈）：**本批唯一一条会让已有 Agent 行为变化的改动，发前必须先跑 §1 的盘点 SQL。**
  - 「输出上限」语义定为**思考 + 回答合计**（用户拍板，与 OpenRouter / Claude Code / Anthropic 一致），
    并且**第一次对非 Anthropic 厂商真正生效**（此前除 Anthropic 外请求体里根本不带上限）。
  - 存量里的 `max_tokens: 4096` 在非 Anthropic 上是**老默认值、从没生效过**，加载时归一为「厂商默认」
    —— 这部分行为不变、配置指纹不变。**不是 4096 的值从本版起开始生效**（测试环境 `ai-health-plan`
    存的 40960 即如此；它历史最长单次输出 33,522，真栈复跑读扫描 PDF 未截断）。
  - 新增「思考长度上限」（`thinking_max_tokens`，只有通义能用；其它模型填了**保存即拒**）。
  - 通义只设档位、没设上限的：思考预算的基数从 4096 变成 81920（思考可以更长）。
  - 豆包档位改走 `reasoning_effort`（此前发的 `budget_tokens` 被厂商忽略）→ **档位从本版起真正生效，high 会变慢**。
  - 回复被上限截断且不可用时，run 以「模型输出被截断（已用满输出上限 N，含思考）」失败、**照常计费**；
    计数器 `expert_work_llm_output_truncated_total{provider,model,usable}`。
  - ⚠️ **回滚影响**：见 §4 新增的一行（`thinking_max_tokens`）。
- **豆包「开思考、档位留空」400**（#1668，09-25 测试 `f92c6fae` 真栈复验）：此前发 `thinking.type=auto`，
  seed-2.1 直接 400（**既有 bug，生产现在就有**）。改发 `enabled`。
- **B-102/103/104 用量记账口径补齐**（#1663，测试 `c78f6d15` 真栈）：run 内规划 / 反思 / 压缩 / 记忆四类调用
  记入会话用量；输出评审 / 工具评审 / 重排序记入 `platform_overhead`（不进账单、不进对外、不进控制台合计）；
  **所有用量行按实际回答的模型记**（备用模型接管时不再记在主模型名下）。
  ⚠️ **计价前置**：价目表没覆盖的模型，成本从「按主模型价错算」变成 **0** → §1 核价目表。
  **测试环境价目表是空的（0 行），计价这条链在测试上从没验过**，只能到生产上核。
- **B-64 文档图片按需取用 + B-106 分词表进镜像**（#1661，测试 `f6bf42ee` 真栈）：
  PDF 等文档里的图按需渲染（`read_page`，渲染页落 NAS 工作区）、看图用量记账、`ask_image` 单次 120s + 默认快看不开思考。
  B-106：`tiktoken` 分词表构建期打进 control-plane 镜像 —— 修掉「每个新进程第一个 run 卡 56~63s 被存活探针杀掉」，
  **这条发布当天就会用到**（滚动出来的新 pod 不再被误杀）。
- **B-84 本波**（#1639 / #1645 / #1647 / #1648 / #1650 / #1651 / #1653 等，测试 `927087ed` 真栈验收 #1657）：
  工具失败后给模型明确下一步、一次失败不被后面的漂亮话盖过去、`list_dir` / `read_file` 改走宿主 NAS（新增 `search_files`）、
  每轮工作区树形摘要进提示词、技能体积护栏、技能「被打开过」单独记账（**带迁移 `0159`**）。
  宿主读 NAS 用的是 control-plane 已有的 `workspace-nas` 挂载与 `EXPERT_WORK_WORKSPACE_NAS_ROOT`（生产 overlay 早已配好），**无新配置**。
- **其余**：SLI 规则 `token_estimate_drift` 分母改口径（随 `apply -k` 上）、CI（gVisor 走发布 tarball）、
  dependabot 四批（buildx / build-push action、admin-ui 小版本、python 四包）。

**没有新的手工集群对象、没有新 secret、没有新配置键**：09-20 之后追加的 35 个提交里 `infra/k8s/` 只动了
`base/observability/rules/sli.yml` 与 test overlay；沙箱镜像没有重烤（钉子仍是 `7ac31957`，Step A 不变）。

> **2026-10-01 第十四次重钉：`31f22602` → `54d1ed70`（main HEAD）。** 10-01 子智能体复盘（生产近 30 天：ai-health-plan 真实用户 0 委派；sop2-designer 规划器标了「派出去」的 19 次只派了 5 次，没派的 14 次里 13 次是计划被上一轮旧 PLAN.md 盖掉；44 次 run 里 25 次本轮新计划被换成上一轮旧计划），用户拍板三项修复全上本班。比第十三次钉子多出的**运行时代码**（`git diff --stat 31f22602 54d1ed70 -- services packages infra apps`，不含测试，只动 orchestrator）：
>
> | 条目 | 文件 | 做什么 |
> |---|---|---|
> | 计划完整性（#1710） | `context/workspace_projection.py`、`graph_builder/workspace_ingest.py`、`graph_builder/builder.py`、`graph_builder/planner.py`、`tools/update_plan.py`、`state.py` | ① 投影成功写 PLAN.md 时把写入正文的摘要记进检查点（`last_plan_md_digest`）；run 开始只在文件与它**不同**（真被人 / agent 改过）时才读回，读回过的改动只用一次；没有摘要的老会话一律不读回。② PLAN.md 与每轮计划清单显示委派标记（PLAN.md 行尾 `_(delegate)_`，清单 `(delegate)`），没有委派步骤的计划逐字节不变。③ `update_plan` 没写 `execution` 的步骤沿用描述相同那一步原来的标记。④ 新增 `plan_first_dispatched_steps`：分发轮只派还没派过的步骤；planner 每轮出新计划时清空它与分发去重键 |
> | 子代容错（#1709） | `tools/_child_run.py`、`tools/spawn_worker.py`、`tools/subagent.py`、`tools/scheduling.py`（注释）、`docs/api/streaming-events.md` | ① 孙代 worker 计入本 run 的 worker 个数上限（不占并发位，防父等子死锁）；② 先排本 run 并发位、再占全进程委派闸位；③ 正在跑的子代到 run 墙钟上限即停，部分结果加首行说明交回；④ 步数 / token / 无进展收尾的子代首行写明「被迫收尾、可能不完整」，end 帧 `max_steps`；没给答案的记失败；⑤ 意外异常补发 end 帧（`cancelled`）后照旧抛出。**end 帧 `outcome` 没有新增取值**，对接方文档 `streaming-events.md` 只补了两个已有取值的含义 |
> | 委派说明（#1708） | `tools/spawn_worker.py`（工具描述）、`agent_factory.py`（委派段）、`graph_builder/builder.py`（提醒 / 分发文案） | 去掉「轻量、快、便宜」，如实写成本与收益（读进主线的内容之后每步都要重付）；写明 worker 没有知识库检索、超约 1.2 万字结果换预览 + 路径、写文件 / bash 排队；「不能派的写操作」统一定义为对外有副作用的动作与交付物最终拍板；计划提醒不再说「看起来互相独立」 |
>
> **无迁移、无配置键、无新集群对象、不动沙箱镜像、admin-ui 零改动、不涉及平台技能**（Step A2 / A3 的 hash 不变）。新增两个检查点字段 `last_plan_md_digest`、`plan_first_dispatched_steps`。
>
> **上线后行为**：所有开了 `persistent_workspace` 的 plan_execute 类 Agent（生产 = sop2-designer），多轮对话从第二轮起不再丢规划结果。**老会话**没有摘要，上线后第一轮一律不读回 PLAN.md —— 若有人恰好在上线前手改过 PLAN.md，这一次不会被采纳（手改极少）。
>
> **回退阀**：三项都没有开关，出问题只能回滚镜像。旧镜像读到新检查点字段会忽略（10-01 用 LangGraph 实测：旧图读、续跑新字段写过的检查点均正常）。**回滚后再前进**时，回滚期间旧镜像写过的 PLAN.md 与残留的旧摘要不一致，前进后的第一轮会把它当「人改过」读回一次 —— 只影响回滚期间有对话的 plan_execute 会话、各一轮。
>
> 测试环境验收：
>
> - 复现（`31f22602`，修复前）：临时 plan_first 探针 Agent，同一会话两轮各给三段独立文字。第二轮 planner 的新计划（丁戊己，3 步 delegate）被 `workspace_ingest` 换成第一轮旧计划（甲乙丙，全 inline、已完成）→ **0 个 worker**。
> - 修复后（`9b7b8cf9` = 三个 PR 合并前同树，以及 `54d1ed70` 本身各跑一遍）：第二轮计划保持丁戊己、**派出 3 个 worker**，标进度后 delegate 标记一直在。
> - 子代撞步数（worker 步数上限 3 的探针）：2 个 worker 撞顶，end 帧 `max_steps`，父侧收到「worker stopped early: reached its step limit (3 steps) … may be incomplete」，主线自己补派 2 个 worker 收尾。探针 Agent 跑完已删。
> - `54d1ed70` 本身：smoke PASS；金丝雀首跑客户端 `ReadError`（服务端 run 32 秒 success、pod 0 重启 = 部署窗口读流断开），单独重跑 PASS；本单 Step C 命令原样在测试 pod 里跑，输出 `ok`。
>
> 下面第十三次重钉段里的 `31f22602` 从此是记账位。
>
> **2026-10-01 第十三次重钉：`39c93912` → `31f22602`（main HEAD）。** 用户 09-30 拍板新模型 glm-5.3-flashx 随本班上；09-30 ~ 10-01 又先后公布 urllib3、pyjwt（2.14.0 也中）、virtualenv 的漏洞，pip-audit 挡住所有 PR，三个升级 PR 一并带上。比第十二次钉子多出的**运行时代码**（`git diff --stat 39c93912 31f22602 -- services packages infra apps`，不含测试）：
>
> | 条目 | 文件 | 做什么 |
> |---|---|---|
> | glm-5.3-flashx（#1702） | `expert_work/protocol/model_catalog.py` | 模型目录加 `glm-5.3-flashx`。智谱 09-18 发布，与 glm-5.3-flash 是同一个模型（官方同一页文档），只是出字更快（约 200 vs 49 tokens/s），价格是 flash 的 2.5 倍。能力位逐项照抄 flash：看图、1M 上下文、始终思考、思考档位 low / high / max（`medium` 落到 `high`）、输出上限字段 `max_tokens` 131072；新测试钉住两条逐项相同 |
> | urllib3 升级（#1703） | `uv.lock` | 2.7.0 → 2.8.0（CVE-2026-97687、CVE-2026-97689） |
> | pyjwt 升级（#1705） | `uv.lock` | 2.14.0 → 2.15.1（CVE-2026-101918；第十二次钉子刚升到的 2.14.0 也受影响），`pyproject` 范围 `>=2.13.0,<3` 不变 |
> | virtualenv 升级（#1706） | `uv.lock` | 21.3.1 → 21.7.13（PYSEC-2026-4011~4014），连带 python-discovery 1.3.0 → 1.6.1；两者只是开发工具 pre-commit 的依赖，**不进镜像** |
>
> **无迁移**（区间内 `migrations/versions` 零命中，迁移仍是四条）、**无配置键、无新集群对象、不动沙箱镜像、admin-ui 零改动**（模型下拉框从接口取目录，flashx 自动出现）。不涉及任何平台技能，Step A2 / A3 的 hash 不变。
>
> **价目表**：用户 09-30 拍板**生产价目表不补** glm-5.2 / glm-5.3-flashx —— 生产上若有 Agent 选它们，控制台成本栏这两个模型显示 0（看 token 数不受影响）。测试环境已补（glm-5.2 8 / 2 / 28，flashx 2 / 0.57 / 7 元每百万 token，输入 / 缓存命中 / 输出），审计 actor `rate-card-bootstrap-2026-09-30`。对接方计费另算：project-service 已在价目表补上这两个模型（provider 均为 `glm`，与 end 帧逐字一致），随它的计费改动一起上线（§1）。
>
> **回退阀**：flashx 只是目录多一条，没人选就不生效；回滚镜像后已选 flashx 的 Agent 保存 / 运行会报「模型不在目录」，生产当前没有 Agent 用它。
>
> 测试环境验收：
>
> - flashx 真跑（`b33f6830` = #1702 合并前同树，临时 Agent，跑完已删）：一题「用 exec_python 算 1~1000 质数和」，调了 `exec_python`、答案 76127 正确、带思考、两次模型调用第二次命中缓存（4480 token）；end 帧 `usage_by_model` 为 `glm` / `glm-5.3-flashx` 一桶。
> - 依赖升级：三个 PR 的 CI 全绿（pytest 11 千余条、integration）；pyjwt 2.15.1 本地跑控制面鉴权相关 321 条全过。
> - `31f22602` 本身：2026-10-01 `release.sh test` 发到测试环境（control-plane / credential-proxy `31f22602`，admin-ui `f29ac13b-test`），smoke + 金丝雀 PASS；本单 Step C 的符号 / 版本核对命令原样在测试 pod 里跑，输出 `ok`（pyjwt 2.15.1、urllib3 2.8.0、目录有 glm-5.3-flashx）。
>
> 下面第十二次重钉段里的 `39c93912` 从此是记账位。
>
> **2026-09-30 第十二次重钉：`ab4097cf` → `39c93912`（main HEAD）。** 用户 09-30 拍板 B-131 的平台部分随本班上（技能部分已先拍板随本班）；同日 pyjwt 公布 10 个 CVE，pip-audit 挡住所有 PR，#1701 升到修复版一并带上。比第十一次钉子多出的**运行时代码**（`git diff --stat ab4097cf 39c93912 -- services packages infra apps`，不含测试）：
>
> | 条目 | 文件 | 做什么 |
> |---|---|---|
> | B-131 减少修改轮的额外调用（#1700） | `orchestrator/graph_builder/builder.py`、`graph_builder/planner.py`、`tools/update_plan.py` | 计划附言与 `update_plan` 说明改成「换到下一步时，在做下一步的同一次回复里打勾，不单独花一次回复；最后一下可省」；新增 `plan_close` 节点：**自然答完**（`exit_reason=text_response`）且计划还有未完成步骤时，把它们全部标完成并同步 `PLAN.md`。步数 / 预算耗尽、审批暂停 / 拒绝、派发打回、复查返工都不经过它；定时任务投递、重新生成（`aupdate_state(as_node="agent")`）也不会停在它前面 |
> | pyjwt 升级（#1701） | `uv.lock` | `pyjwt` 2.13.0 → 2.14.0（CVE-2026-101917、CVE-2026-102265~102274），`pyproject` 的范围 `>=2.13.0,<3` 不变 |
>
> **无迁移**（区间内 `migrations/versions` 零命中，迁移仍是四条）、**无配置键、无新集群对象、不动沙箱镜像、admin-ui 零改动**。`platform-skills/health-plan-report` 同一 PR 改成 v6（加「改稿速查」），仍由 Step A3 单独导入 —— 期望 hash 随之从 `cc2df66a` 改为 **`786d30fe`**。
>
> **行为变化面向全部 Agent**：所有用 `update_plan` 的 Agent 在正常答完时，控制台的计划卡片会全部显示完成（此前常留着「进行中」和一串「待处理」）。代价：模型中途反问用户、活没干完就答完时，也会显示全部完成（用户 09-30 拍板接受）。
>
> **回退阀**：B-131 **没有开关**，出问题只能按 §4 回滚镜像；它不写新字段、不改配置结构，旧镜像读新检查点无影响。技能 v6 在旧镜像上照常可用，回滚镜像不必连技能一起回。
>
> 测试环境验收（同一段 ai-health-plan 8 轮对话重放，基线 3 次 vs B-131 6 次，`7f68b32d` 3 次 + `40ae953b` 3 次）：
>
> - 修改轮（第 2~8 轮）平均每轮模型回复次数 7.0 → 4.2~6.0；先翻文档 / 列目录 10 → 0~2 次、逐张看图多余 4 → 0~1 次；看图检查次数不减（22 → 21 / 26）。
> - 每次都做同样活的第 1 / 2 / 7 / 8 轮成本 ¥7.31 → ¥5.99（−18%），纯改稿第 7 / 8 轮 ¥3.55 → ¥2.55（−28%）。交付件（初稿 PPT、改稿 PPT、第 8 轮 PDF）与「以后都这样」写个人默认，9 次全部一致。
> - 第一版只收「进行中」，重放发现模型常建完计划就不再更新，留下一串待办 → 用户拍板「提示词要求逐步打勾 + 平台兜底收全部」；修后 24 轮每轮结束都全部打勾。代价：3 次里有 1 次模型仍会在最终回复前单独花一次回复打勾（约 ¥0.1 / 次）。
> - `39c93912` 本身：2026-09-30 `release.sh test` 发到测试环境（control-plane / credential-proxy `39c93912`，admin-ui `f29ac13b-test`），smoke + 金丝雀 PASS；`health-plan-report` 在 pod 内 dry-run 导入得 `786d30fe…`、`unchanged`（测试环境已是这版）。
>
> 下面第十一次重钉段里的 `ab4097cf` 从此是记账位。
>
> **2026-09-29 第十一次重钉：`c0789ca6` → `ab4097cf`（main HEAD）。** 用户 09-29 拍板 B-126 随本班上生产、钉子等它合入后一次性前移到 main；同日 B-127、B-128 也拍板随本班。比第十次钉子多出的**运行时代码**（`git diff --stat c0789ca6 ab4097cf`，不含测试）：
>
> | 条目 | 文件 | 做什么 |
> |---|---|---|
> | B-126 跨轮上下文降本（#1694 / #1696） | `orchestrator/context/tool_result_prune.py`、`context/compressor.py`、`context/working_window.py`、`graph_builder/builder.py`、`sse.py`、`state.py`、`agent_factory.py`、`protocol/agent_spec.py` | 新一轮开头把**之前各轮**的大工具结果（≥4000 字、有可重读副本或技能引用）换成一行引用，同一轮内逐字不变保缓存；看图结论不清。窗口 / 压缩器 / 兜底门槛统一封顶 `min(0.7×窗口, 20 万)`（百万窗口模型原来 70 万才触发 = 从不触发）。压缩摘要写回检查点复用（`AgentState.context_summary`，不进 SSE / run_event） |
> | B-127 MCP 工具参数固定值（#1695） | `orchestrator/tools/arg_bindings.py`、`tools/mcp.py`、`tools/registry.py`、`tools/assembly.py`、`tools/_child_run.py`、`control_plane/api/agents.py`、`control_plane/prompt_render.py`、`protocol/agent_spec.py`；admin-ui `manifest-editor/*`、`i18n` | `arg_bindings[].fixed`：配置里写死「某工具参数 P 恒为 V」，平台从模型看到的 schema 里剥掉 P、每次调用注入 V。上游 schema 还没有 P 时按「绑定落空」处理：保存时告警、**不注入**。配置页每个参数行多一档「固定值」 |
> | B-128 只留最新一份系统提示词（#1697） | `orchestrator/context/system_prompt_view.py`（新）、`graph_builder/builder.py`、`context/compressor.py`（谓词改公开名） | 每轮入口都往检查点追加一条系统提示词，原来第 N 轮请求带 N 份、且每轮在上一份末尾断缓存；现在 prompt 视图只留最新一份放最前。只改发给模型的视图，检查点不动 |
> | admin-ui 依赖（#1690） | `apps/admin-ui/package.json`、`pnpm-lock.yaml` | `fast-uri` override `>=3.1.7`（GHSA-58mr-gqgx-xq4g） |
>
> **无迁移**（区间内 `migrations/versions` 零命中，迁移仍是四条）、**无新集群对象、不动沙箱镜像**；#1691 只改 CI 与本地 `docker-compose` 的 MinIO 镜像来源，不进生产。`platform-skills/`（B-125）仍由 Step A3 单独导入。
>
> **行为变化面向全部 Agent**（B-126 / B-128 默认即生效，不是只对 ai-health-plan）：多轮对话从第 2 轮起旧的大工具结果被收成引用、百万窗口模型的窗口 / 压缩在 20 万处开始工作、发给模型的系统提示词只剩一份（变量值以当轮渲染为准 —— 测试环境实测对接方 09-15 起每轮都传全量 inputs）。
>
> **回退阀**：B-126 跨轮清理可按 Agent 关（配置 `policies.tool_result_prune.cross_turn: false`，保存并发布即生效）；B-126 的 20 万封顶、摘要复用与 B-128 **没有开关**，只能按 §4 回滚镜像（已验旧镜像读带 `context_summary` 的新检查点不报错；B-128 不写任何数据）。B-127 不用就无影响。
>
> 测试环境验收：
>
> - **B-126**（`78abe0f3`）：Tempo 上第 2 轮起每次调用收起 2~3 条旧结果、每次省 1.1~1.4 万 token，轮首上下文少约 2 万 token；同一轮内前缀逐字不变。
> - **B-127**：ai-health-plan 在测试环境用「固定值」把 `form_get_field_detail` 的 `detail_level` 定为 `brief`（当时测试配置 sha `2b536d40`；之后又删 8 个零调用工具，rev39 终版为 `28eb1ba5`，见 §0.2），真跑中该工具单次结果从平均 4,440 字降到 538 字（加上对方的 options 列后 783 字）。
> - **B-128 + 整体**（`f29ac13b`）：同一段 ai-health-plan 真实 7 轮对话重放，成本 ¥12.64（基线）→ ¥8.01（B-126 + 精简模式）→ **¥6.63**（再加 B-128，7/7 success）；第 3~7 轮首次调用未命中缓存的 token 从每轮 3.4~6 万降到 1.3~2.4 千。smoke + 金丝雀 PASS（金丝雀首跑即 PASS，真 run + 产物下载）。
> - **admin-ui**（`f29ac13b-test`）：B-127 固定值配置页在测试环境发过，见 §6 前的记录。
>
> 下面第十次重钉段里的 `c0789ca6` 从此是记账位。
>
> **2026-09-27 第十次重钉：`68a7b75f` → `c0789ca6`（main HEAD）。** 只多 B-124（#1685）：子代（动态子智能体 + 静态子 Agent）在沙箱里执行（`write_file` / `exec_python` / `bash` …）用的是按**子代自己名字**建的工作区，而它的存储类工具（`read_file` / `list_dir` …）按设计用**父的** key —— 于是父读不到子代写的文件，子代在沙箱里也看不到父的 `inputs/` 与 `style/`。**生产 09-16 起就这样**（B-60 PR-C `621249f6` 带进来的），B-122 验收时暴露。用户 09-27 拍板「现在就修」，随本班上。运行时代码只改 `services/orchestrator/src/orchestrator/tools/sandbox.py` 一个文件（其余是测试与 ROADMAP B-124 行）；**无迁移、无配置键、不动沙箱镜像**；**B-124 没有回退阀**，出问题只能按 §4 回滚镜像。
>
> 测试环境真栈：
>
> - **修前探针（`68a7b75f`）**：子智能体 `exec_python` 读父的 style 文件报 `FileNotFoundError`；子智能体 `read_file` 读自己刚写的文件 `not_found`；父 `read_file` 读子智能体写的文件 `not_found`，`list_dir` 里也没有。
> - **修后（分支树与 `c0789ca6` 逐字相同，09-27 约 19:00 发测试）**：四条全过 —— 子智能体 `read_file` 与 `exec_python` 都读到 `PARENT-OK`；子智能体读回自己写的 `WORKER-OK`；父 `read_file` 读到 `WORKER-OK`，`list_dir` 里有它。ai-health-plan 委派回归：父 `read_file("layout_design.md")`（子智能体写的版式设计）成功，交付物只有 json + docx，run success。smoke + 金丝雀 PASS。
> - **`c0789ca6` 本身**：`release.sh test` 发过，smoke PASS；阶段 6 金丝雀**首跑 `FAIL transport: ReadError`**（rollout 刚完、旧 pod 正在摘除的窗口里连接被断；新 pod 零重启），原地单独重跑金丝雀 PASS（真 run + 产物下载）—— 按「一次 vs 每次」判为环境；随后在 `c0789ca6` 上重跑 B-124 四条检查全过。**发生产时若金丝雀首跑出同形态的 transport 错,先原地重跑一次再判**,别直接回滚。
>
> 下面第九次重钉段里的 `68a7b75f` 从此是记账位。
>
> **2026-09-27 第九次重钉：`e5341495` → `68a7b75f`（main HEAD）。** 用户 09-27 拍板：B-122（#1682，子智能体工具边界平台化）与 B-123（#1683，子 Agent / 子智能体构建也按租户 MCP 开通名单取工具）随本班上生产。钉子反正要动，用户选**直接前移到 main**，不再往 `release/train2-tz` 上 cherry-pick —— 这就同时推翻了 B-119 的「钉子不前移」，office 技能那处代码侧文案随本班上线（B-119 由本班销案，见 Step C 的核对项）。B-121 在 main 上是 `c632d3e3`（#1681 的 squash），与 `e5341495` 的改动逐字相同（`git diff e5341495 68a7b75f -- services/orchestrator/src/orchestrator/agent_timezone.py services/orchestrator/src/orchestrator/tools/sandbox.py` 为空），所以时区修复照带。
>
> 比第八次钉子多出的**运行时代码**（`git diff --stat e5341495 68a7b75f -- services packages infra`，不含测试）：
>
> | 条目 | 文件 |
> |---|---|
> | B-122 子智能体工具边界 | `orchestrator/tools/worker_policy.py`（新）、`tools/assembly.py`、`tools/mcp.py`、`tools/http.py`、`tools/spawn_worker.py`、`agent_factory.py`、`control_plane/subagent_runtime.py` |
> | B-123 子代构建读 MCP 开通名单 | `control_plane/subagent_runtime.py` |
> | B-119 文案 | `orchestrator/tools/sandbox_image_contract.py`（`SANDBOX_UNAVAILABLE_NOTE` 与注释，无功能影响） |
>
> 另有 `infra/k8s/overlays/test/kustomization.yaml`（test newTag 记录）与各自的测试。**无迁移**（区间内 `migrations/versions` 零命中，迁移仍是四条）、**无配置键、无新集群对象、不动沙箱镜像**；`platform-skills/` 不进任何镜像（Dockerfile 只 COPY `packages/` / `services/` 等），仍由 Step A2 单独导入。
>
> **B-122 出问题不用回滚镜像**：它自带运维回退阀 `EXPERT_WORK_WORKER_TOOL_POLICY=off`（`0` / `false` / `no` 同义），设上并重启 control-plane pod 后，子智能体的工具与父侧文案逐字节回到 B-122 之前。B-123 没有单独的阀，要退只能回滚镜像（§4）。
>
> 测试环境验收（09-27 发 `68a7b75f`）：测试环境 09-27 17:40 发 `68a7b75f`(`release.sh test` smoke PASS + 金丝雀产物链 PASS);B-122/B-123 真栈 4 条全过:① ai-health-plan 派子智能体做版式 → 子智能体只读/写工作区文件、无 `save_artifact`,交付物只有 json+docx(无中间稿);② 子智能体调深护智康只读工具 `form_list_by_project` 拿到真实表单(B-123 生效;ahp 只声明 11 个只读工具 → `kept=11 dropped_write=0`);③ 要子智能体发企微 → 手里没有写工具,如实回「无法发送」;④ `ai‑expert‑group`(不限工具,全 40 个)的子智能体 `mcp.worker_read_only_filter kept=31 dropped_write=9`,自报三类写工具均不存在。每次派活都有 `worker_policy.applied`,剥掉 save_artifact / remember / note_behavior_patch / clarify_tool_usage
>
> **2026-09-27 第八次重钉：`f92c6fae` → `e5341495`。** 只叠 B-121（沙箱注入 `TZ`，与系统提示词「当前日期」的时区同源；此前沙箱是 UTC，模型取到的时间慢 8 小时），用户 09-27 拍板随本班带上。**cherry-pick 到旧钉子上而不是前移到 main**：B-119 的「钉子不前移」仍成立，生产只比 `f92c6fae` 多这一个提交（6 个文件，全在 `services/orchestrator/`，无迁移、无配置键、不动沙箱镜像）。操作位（表头、Step B / C / F、Step A2 说明）已改到新钉子。
>
> **2026-09-25 第七次重钉：`139057c8` → `f92c6fae`。** 操作位（表头、Step B / C / F、§6）已全部改到新钉子；
> 下面历史段落里出现的旧 sha 是记账位，按原判据放过。

> **钉子纪律**：本单钉 `54d1ed70`。发布日若要带上它之后的**任何代码或 admin-ui 文档站改动**，
> 必须**先发一次测试环境验过**再改钉子 —— 别在发布当天直接发 main HEAD。
>
> **改期记录**：2026-09-18 先钉 `498492d5`（#1591），当天下午用户拍板把 B-56 / B-72 / B-65 /
> B-73 四条一起带上，重钉 `dfd4e6de`（#1597）。当晚发现 B-73 ① 在生产上只能修一半
> （写隐藏消息的源头有三个，另外两个早就在生产跑），补 `0157` 逼 sweep 重扫，又重钉一次。
> 形态：无迁移 → 一条 expand-only → 两条（`0156` 加列 + `0157` 数据迁移）→ **三条（09-20 加装 B-85 ③ 的 `0158` 加列）**，全程仍是单段。
>
> **2026-09-19 第四次重钉（#1618）。** 那一版在测试环境**实际发过两次**，比上一个钉子多
> 10 个提交（**这里刻意不写旧 sha 的字面量** —— 判据是 `grep '<旧 sha>'` 零命中，
> 历史叙述里留一个反而让判据永远过不了）：
>
> - **`anyio` 4.13.0 → 4.14.2**（#1605）—— 两个 CVE 卡住了当时每一个 PR 的 `Security (pip-audit)`。
>   只改 `uv.lock`；锁里出现两个 anyio 是预期的解析分叉，运行时镜像与 CI 都是 python 3.12，落地的是 4.14.2。
> - **B-80 两个修**（#1599）—— 排空中的旧 pod 不再被算成发布失败 + 排空过程落审计。
> - **B-81 沙箱 pip 镜像源**（#1602）—— 三个 `EXPERT_WORK_SANDBOX_PIP_*` 已经在
>   `overlays/prod/configmap-patch.yaml` 里，随 `apply -k` 一起上，**不需要额外手工步骤**。
>   阿里云杭州实测：官方 CDN 16 KiB/s → 内网镜像 3863 KiB/s，同一个 wheel 26 分钟 → 2 秒。
> - **B-82 预装清单**（#1606）+ **B-84 技能摘要瘦身**（#1608）—— 都是纯代码（工具描述 / 系统提示词）。
>   ⚠️ **这一句当时写的是「不重烤沙箱镜像，沙箱镜像钉子不动」，09-19 晚已不成立**：
>   B-55 的 #1623 改了 `infra/sandbox-image/Dockerfile`（砍 apt ffmpeg / npm），**本版重烤了**，
>   沙箱镜像钉子已换成 **`7ac31957`**（09-20 重钉，见下）。
>   ⚠️ **中途出过两个不能用的 tag，别照着旧记录去钉**：`ef9e157a`（#1623）那次构建在 90 分钟
>   超时上被 kill，**从来没推上 ACR**（`imagetools inspect` 报 not found）；`05b20c8f`（#1626
>   把超时抬到 180 之后重跑）推上去了，但那还是 amd64+arm64 的双架构 index。#1630 删掉 arm64
>   之后重烤出的 **`7ac31957`** 才是本单要钉的那个（单 amd64 manifest）。
>
> 迁移在 09-20 加装 B-85 ③ 之后变成**三条**（`0156` / `0157` / `0158`），形态仍是单段，
> 回滚纪律不变（三条都只回镜像，不 downgrade）。
>
> **✅ 2026-09-19 用户拍板：B-55 / B-58 / B-85 三条全部带上。** 此前本段写的「刻意不带 B-58
> （#1611）与 B-85（#1618）」**整条作废** —— 那句话的前提是「它们在当时那个钉子之后才合入、
> 没发过测试环境」，而用户先后两次拍板（「58 和 55 带上」、「85 也带上」）把它们都收进本班。
> 钉子纪律本身不变：**先发一次测试环境验过，再重钉**。
>
> **✅ 2026-09-20 第五次重钉：两个钉子都动了。**
>
> | 钉子 | 新值 | 怎么验过的 |
> |---|---|---|
> | 应用镜像 | **`139057c8`** | `release.sh test` 发过两遍（09-20 两次）：`SMOKE PASS` 17 项全绿、金丝雀真跑 + 产物链 PASS；B-85 ③ 另做了双向真栈验（见 §0 那条） |
> | 沙箱镜像 | **`7ac31957`** | 测试集群 apply 过，池 `1/1`；拉取 66.19s → **40.55s**，压缩层 619.4 → **433.7 MiB**；pod 内复探 soffice / pdftoppm / ffmpeg（wheel 静态二进制）/ node 全在、npm 已删、weasyprint 渲 PDF + pypdf 读回命中 marker |
>
> 这一版比上一个钉子多 **22** 个提交：
>
> | 提交 | 是什么 | 进本班的理由 |
> |---|---|---|
> | `604e4e42` #1610 | test newTag 记录 + 金丝雀带上 B-55 探针 + 新立 B-85 | 记账 |
> | `36e675a8` #1611 | **B-58** 孤儿重收丢输入 | 用户拍板 |
> | `e0d5bbd1` #1619 / `9066c191` #1621 / `1833c746` #1622 | 执行单重钉、ROADMAP 勘误、执行单补漏 | 纯文档 |
> | `87a7cb5a` #1620 | smoke 公网探针重试连接级失败 | 发布工具，发布当天会用到 |
> | `5ba246eb` #1618 | **B-85 ①②** 沙箱认领撞并发时平台自己等 | 用户拍板 |
> | `97762881` #1613 | dependabot：pypdf / matplotlib 补丁版 | 随镜像重烤一起上 |
> | `ef9e157a` #1623 / `4d0d1562` #1624 / `4704bd19` #1625 / `05b20c8f` #1626 | **B-55** 镜像瘦身 + VPC endpoint + ALB 80 端口 timeout + 构建超时 | 用户拍板 |
> | `f27ada2e` #1627 | 合规锁被后端静默吞掉时报错（B-88 的第一步，纯代码） | 随车 |
> | `128e4194` #1628 / `0a44dab4` #1629 / `1cdbc495` #1631 | ROADMAP 记 B-88 / B-89、执行单改钉、B-88 单独立项 | 纯文档 |
> | `7ac31957` #1630 | 沙箱镜像只建 amd64，删掉 arm64；构建超时退回 90 | 镜像重烤的那一版 |
> | `5a809aee` #1632 | 沙箱镜像钉子推到 `7ac31957`，实测数据写进 yaml 注释 | 本单 Step A 就照这一行发 |
> | `139057c8` #1636 | **B-85 ③** run 做没做成的独立信号（带迁移 `0158`） | 用户 09-20 拍板 |
> | `f8436883` #1633 / `4775132d` #1634 / `cf7e0464` #1635 | test newTag 记录 + 第五次重钉、B-55 销案、B-85 ③ spec+计划 | 记账 / 纯文档 |
>
> 重钉之后按 §0 顶上那条判据自检过。09-20 第六次重钉（应用镜像 → `139057c8`，带 B-85 ③）之后：
> `233791e5` / `621249f6` / `5a809aee` 作为**钉子**的命中数都是 **0**。
>
> ⚠️ **判据的一条说明**：上面这张提交清单里会出现旧钉子的 sha —— 因为那一版**本身就是一个提交**
> （`5a809aee` 是 #1632）。判据管的是**操作位**（表头、钉子表、Step B 的 `git checkout`、Step C 的
> 核对、Step F 的记录 PR 标题），不是提交清单这种**记账位**。`grep` 之后逐条看一眼落在哪儿，
> 落在清单里的放过，落在上面五处任何一处的都是漏改。

> **⛔ #1597 那次重钉漏了正文**：表头改成了 `dfd4e6de`，Step B 的 `git checkout` 和另外 4 处
> 却仍停在 `42426d31`（落后两代）。这已经是同一形状的**第二次**（班车 1 的 B2 正文钉子漏改，#1566）。
> 本次一并补齐，改钉子的判据定死为：**`grep -n '<旧 sha>' 这份文件` 必须零命中**才算改完。

---

### 0.2 2026-09-29 追加：B-125 健康方案交付件技能（随 10-08 发）

- **内容**：平台技能 `health-plan-report`（把定稿的健康方案渲染成可编辑 PPT + A4 PDF，纯 Python，在沙箱里跑）
  + ai-health-plan 配置 **rev39**（技能绑定 `pptx` / `pdf` / `docx` / `health-plan-report`，出交付件改用本技能；
  另含三处 09-29 追加：① MCP 工具 `form_get_field_detail` 的参数 `detail_level` 用 B-127「固定值」定为 `brief`；
  ② 提示词第 2 行的改写；③ **删掉 8 个近 30 天零调用的工具**（测试环境 116 段会话 / 2134 次模型调用统计）：
  内置 `web_search` / `list_artifacts` / `remember` / `note_behavior_patch` / `clarify_tool_usage`、`http` 工具、
  MCP `customer_search_by_cpwx_tags` / `customer_list_cpwx_tags`（连同这两个工具上的 `project_code` / `employee_code` 绑定）。
  测试环境 rev39 配置 sha **`28eb1ba5`**（① ② 时为 `2b536d40`，再做 ③ 得到）。
  ③ 的依据与验收：`remember` 等三个自我进化工具全平台从没被用起来（草稿无人启用），`remember` 对本 Agent 还有串客户风险
  （记忆按员工存、不按客户）；删后工具说明 10,072 → 8,744 token（o200k 计，−13%），首次调用输入实测 −1,252 token；
  同一 7 轮对话重放 7/7 success、被删工具零调用。`list_artifacts` 是平台基础能力，删了仍会自动带上（构建出 24 个工具，不是 23 个），属预期。）。
  代码 #1686–#1689 已合 main（`523a1050`）；ROADMAP B-125。
- **不动镜像、不动钉子**：`platform-skills/` 不进任何镜像，技能由 Step A3 用导入脚本上线；提示词由 Step A4 改 Agent 配置。
- **依赖**：① Step B 的 `54d1ed70`（`EXPERT_WORK_SKILLS_DIR`，B-84；「固定值」要 B-127）；② Step A 的沙箱镜像 `7ac31957`（python-pptx /
  weasyprint / Noto CJK —— 测试环境 in-image 用例就在这个镜像上跑）；③ Step A2 的 `pptx` / `pdf` / `docx`（rev39 绑定它们）。
  所以顺序是 **A → B → C → A2 → A3 → A4**。
- **两件必须一起做**：只导入技能，Agent 不会用；只发 rev39，Agent 会调一个生产上不存在（或未启用）的技能。
- **测试环境验收**：技能 v1→v5（v5 = `cc2df66a`，卡片组整组不拆页）；rev39 在测试环境 `ai-health-plan` 上两次重放真实 5 轮对话，
  带出的问题全部修进 v2~v5；v5 抽查 PPTX 16/19 页、PDF 11 页，质检全过。**09-30 起 v6**（`786d30fe`，B-131 加「改稿速查」，同一 8 轮对话重放 6 次验收，见 §0.1 第十二次重钉）。

## 1. 前置（发布前一天做完）

> **本班时间点**：09-28 是周一，前一天是周日 → **两条只读盘点 SQL 建议 09-26（周五）就跑**：
> ① 价目表若有缺价要在控制台补价、② 上限偏小的 Agent 要找负责人确认，这两件都需要工作日。
> 测试环境 24h 复查的对象是 `f92c6fae`（09-25 发到测试），09-26 起即满 24h。`e5341495` 09-27 13:10 发到测试，只多 B-121 一处，按它的真栈验证（沙箱时钟 + 出方案）放行，不另等 24h。`68a7b75f`（第九次重钉）同一原则：在验过的钉子上只多 B-122 / B-123 与 B-119 的一处文案，按它的真栈验收放行、不另等 24h —— 测试环境 09-27 发 `68a7b75f`,smoke + 金丝雀 PASS;B-122/B-123 真栈 4 条全过(子智能体无 save_artifact、能调深护智康只读工具、写工具 `kept=31 dropped_write=9`)。`c0789ca6`（第十次重钉）同理：只多 B-124 一个文件，按它的真栈验收（修前修后四条探针 + ai-health-plan 委派回归 + smoke / 金丝雀 PASS）放行，不另等 24h。`ab4097cf`（第十一次重钉）09-29 发到测试（镜像 `f29ac13b`，树相同），到 10-08 早已满 24h，**24h 复查按原判据对它做**（重点看 B-126 / B-128 生效后有无 run 失败或上下文相关报错）。`39c93912`（第十二次重钉）09-30 发到测试，只多 B-131 与 pyjwt，按它的真栈验收放行；到 10-08 同样早已满 24h，复查时顺带看控制台计划卡片是否都收尾。`31f22602`（第十三次重钉）10-01 发到测试，只多模型目录一条与三个依赖升级，按它的 smoke + 金丝雀 + flashx 真跑放行。`54d1ed70`（第十四次重钉）同日发到测试，只多子智能体委派三项修复，按它的 smoke + 金丝雀 + 委派两轮探针放行。

- [ ] **本机接线还在**（只看存在与权限，不读内容）：

      ```sh
      ls -l ~/.kube/expert-work-prod.yaml ~/.kube/expert-work-prod-secrets.env ~/.kube/expert-work-prod-params.env
      ```

      三个文件都在、都是 `600`。

- [ ] **本机 venv 能跑打包导入**（Step A2 / A3 的 `import_in_pod.py bundle` 在本机跑，模块顶层就 import `control_plane`）：

      ```sh
      uv run --no-sync python -c 'import control_plane, expert_work.protocol; print(control_plane.__file__)'
      ```

      打印的路径必须在**主仓库目录**下（不是 `.worktrees/...` 或 `.claude/worktrees/...`）。报 `ModuleNotFoundError` 或路径在别的 worktree 里，说明共享 venv 的 editable 安装被某个 worktree 改指过（2026-10-02 本机实测就是这样：指向一个已删除的 worktree），在主仓库目录跑一次 `uv sync` 修复后再核一次。
- [ ] **overlay 无占位符**：`grep -rn PROD_PLACEHOLDER infra/k8s/overlays/prod/` 无输出。
- [ ] **金丝雀还在**：班车 1 已 seed 过（`release-canary` 有会话行、工作区里有 `canary-check.txt`）。
      smoke 阶段 6 报 WARNING 而不是 PASS，就说明它没了，按
      [`production-release.md` §1.6.7](./production-release.md) 补种，**不要带着 WARNING 往下发**。
- [ ] **装载确认**：

      ```sh
      git fetch origin main
      TAG=<本版 tag>                                        # 见表头「本版 tag」
      git log --oneline 5775fbf3..$TAG | wc -l             # 与表头「区间提交数」对得上
      git diff --name-only 5775fbf3..$TAG | grep -i migrations/versions   # 期望**恰好四条**：
      #   packages/expert-work-persistence/migrations/versions/0156_thread_message_hidden.py
      #   packages/expert-work-persistence/migrations/versions/0157_thread_mirror_resweep.py
      #   packages/expert-work-persistence/migrations/versions/0158_run_completion.py
      #   packages/expert-work-persistence/migrations/versions/0159_skill_usage_viewed.py
      # 多出别的迁移 = 装载和这份单子对不上，停下来查，别往下发
      ```

- [ ] **两条只读盘点 SQL（09-25 新增，由你在生产上跑）**。两条都只读、只出聚合，不含客户数据。
      跑法相同：把 SQL 存成本地文件，喂给下面这段（`SET TRANSACTION READ ONLY` 兜底，写语句会直接报错）：

      ```sh
      export KUBECONFIG=~/.kube/expert-work-prod.yaml
      POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
        -o jsonpath='{.items[0].metadata.name}')
      run_sql() {  # 用法: run_sql 本地文件.sql
        { printf 'SQL = r"""\n'; cat "$1"; printf '\n"""\n'; cat <<'EOF'
      import asyncio
      from sqlalchemy import text
      from control_plane.app import _build_sql_stores
      from control_plane.settings import Settings
      from control_plane.tenant_scope import bypass_rls_session

      async def main():
          stores = _build_sql_stores(Settings())
          try:
              async with bypass_rls_session():
                  async with stores.session_factory() as s:
                      await s.execute(text("SET TRANSACTION READ ONLY"))
                      r = await s.execute(text(SQL))
                      print(" | ".join(r.keys()))
                      for row in r:
                          print(" | ".join(str(x) for x in row))
          finally:
              await stores.engine.dispose()

      asyncio.run(main())
      EOF
        } | kubectl -n expert-work exec -i "$POD" -- python3 -
      }
      ```

      **① 价目表覆盖（B-102~104 的前置）** —— 把会被计费的模型逐个对价目表：

      ```sql
      WITH a AS (SELECT spec_json->'spec' AS s FROM agent_spec WHERE status <> 'deleted'),
      used AS (
        SELECT s->'model'->>'provider' AS provider, s->'model'->>'name' AS model, 'agent 主模型' AS src FROM a
        UNION SELECT f->>'provider', f->>'name', 'agent 备用' FROM a, jsonb_array_elements(COALESCE(s->'model'->'fallback','[]'::jsonb)) f
        UNION SELECT s->'vision'->'model'->>'provider', s->'vision'->'model'->>'name', 'agent 看图' FROM a WHERE jsonb_typeof(s->'vision') = 'object'
        UNION SELECT f->>'provider', f->>'name', 'agent 看图备用' FROM a, jsonb_array_elements(COALESCE(s->'vision'->'fallbacks','[]'::jsonb)) f
        UNION SELECT s->'dynamic_workers'->'model'->>'provider', s->'dynamic_workers'->'model'->>'name', 'worker 模型' FROM a WHERE jsonb_typeof(s->'dynamic_workers'->'model') = 'object'
        UNION SELECT judge_provider, judge_model, '平台评审模型' FROM platform_judge_config WHERE judge_model IS NOT NULL
        UNION SELECT 'qwen', 'qwen-plus', '重排序(settings 默认 rerank_model)'
        UNION SELECT DISTINCT provider, model, '近 30 天实际用量' FROM token_usage WHERE observed_at > now() - interval '30 days'
      )
      SELECT u.provider, u.model, string_agg(DISTINCT u.src, ' / ') AS used_as,
             CASE WHEN EXISTS (SELECT 1 FROM model_rate_card r WHERE r.tenant_id IS NULL AND r.provider = u.provider AND r.model = u.model)
                  THEN '有' ELSE '⚠️ 缺' END AS platform_rate_card
      FROM used u WHERE u.model IS NOT NULL
      GROUP BY u.provider, u.model
      ORDER BY platform_rate_card DESC, u.provider, u.model
      ```

      判据：**`⚠️ 缺` 的行要么发前补价（控制台价目表页），要么明确接受「这些模型的成本记 0」**，写进 §6。
      测试环境跑出来是全缺（价目表 0 行）—— 那是测试的现状，不是 SQL 错了。

      **② 输出上限 / 思考档位盘点（B-105 的前置）** —— 哪些 Agent 的行为会因本版变化：

      ```sql
      WITH a AS (SELECT name, spec_json->'spec' AS s FROM agent_spec WHERE status <> 'deleted'),
      m AS (
        SELECT name, s->'model' AS mm FROM a
        UNION ALL SELECT name, f FROM a, jsonb_array_elements(COALESCE(s->'model'->'fallback','[]'::jsonb)) f
        UNION ALL SELECT name, s->'vision'->'model' FROM a WHERE jsonb_typeof(s->'vision') = 'object'
        UNION ALL SELECT name, f FROM a, jsonb_array_elements(COALESCE(s->'vision'->'fallbacks','[]'::jsonb)) f
      )
      SELECT mm->>'provider' AS provider, mm->>'name' AS model,
             COALESCE(mm->>'max_tokens','<空>') AS max_tokens,
             COALESCE(mm->>'effort','<空>') AS effort,
             COALESCE(mm->>'thinking_enabled','<空>') AS thinking_enabled,
             count(*) AS entries,
             string_agg(DISTINCT CASE WHEN name = 'ai-health-plan' THEN 'ai-health-plan' END, ',') AS named
      FROM m GROUP BY 1,2,3,4,5 ORDER BY 1,2,3
      ```

      读法：

      | 看到的行 | 本版之后 |
      |---|---|
      | 非 anthropic，`max_tokens` = 4096 或 `<空>` | 行为不变（4096 是老默认，从没生效过） |
      | 非 anthropic，`max_tokens` 是别的值 | **这个上限开始生效**（含思考）。偏小的（< 16000）发前和 Agent 负责人确认 |
      | qwen，有 `effort`、`max_tokens` 空 | 思考可以更长（预算基数 4096 → 81920） |
      | doubao，有 `effort` | 档位开始真正生效（high 会变慢） |
      | doubao，`thinking_enabled=true`、`effort` 空 | **生产现在每次都 400**（看图位只影响「细看」），本版修好 |
      | anthropic | 不变（空 = 4096） |

      两条 SQL 都已在测试库原样跑过（09-25）。

- [ ] **预拉三个 base 镜像**（ECR Public 按 IP 限流，一天能红六次；建镜像前先拉一遍，
      见 [`production-release.md`](./production-release.md) 的预拉脚本）。
- [ ] **测试环境 24h 复查已做**：确认**本版 tag 那一版**在测试环境跑满 24h 后
      `rls.would_fail_closed` 里 `workspace_janitor` / `memory_consolidator` 两个模块为 0
      （同 Step E 的判据）、没有新的异常告警。
- [ ] **窗口挑非高峰**：B-80 修复后发布**不再打断在跑的对话**，所以这条从"硬要求"降为
      "常识"——不必再为它专门约时间。仍建议避开对接方明确的高峰，理由只剩一条：
      滚动期间在跑的对话越多，收口越久（见 §2 第 2 条的实测数字）。
- [ ] **通知对接方**：只有一件事要说 ——**窗口内先别处理待审批**（§2 第 3 条）。
- [ ] **project-service 的计费改动已先上生产**（09-30 与对方会话一起定）：本版起 end 帧的 `usage_by_model` 会多出看图模型 `doubao:doubao-seed-2-1-pro-260628` 一桶（B-102~104 起看图调用记账；B-125 让 ai-health-plan 每次出稿都看图）。project-service 旧代码遇到没定价的桶会**整个 run 不扣康豆**，所以它必须在本班之前上线三件事：豆包补价（6 / 1.2 / 30 元每百万 token）、未定价桶只跳过并报警、缓存命中价计费；同批还补了 `glm:glm-5.2`（8 / 2 / 28）与 `glm:glm-5.3-flashx`（2 / 0.57 / 7）。问对方要上线时间与版本号，没上就先别发本班。
      在跑的对话不用管，会自动换实例接着跑完，对外无感。

---

## 2. `release.sh prod` 不覆盖的动作（本版清点结果）

`release.sh prod` 只做四件事：建推三个镜像 → 钉 overlay newTag → `apply -k`（含 migrate Job）
→ rollout + smoke。本版它**不做**的：

1. **沙箱镜像钉子**（`infra/k8s/sandbox/sandboxset.yaml`，`default` 命名空间）→ Step A。
2. **发布会打断正在跑的对话（B-80）—— 本版已修**（2026-09-18 加入本班）：
   - 修之前：进程关机时在跑的对话被收成 `interrupted`（`error` 为空），不会被另一副本接管。
   - 修之后：关机时先给在跑的对话一段宽限期让它们自己跑完；到点没跑完的，
     **能安全接管的**交给别的副本从存档点接着跑（用户无感），**不能安全接管的**
     （正卡在发消息这类不可重跑的工具上）照旧收成 `interrupted`。
   - **本版因此变慢**，三个数字是一套，任何一个都别单独改：

     | 参数 | 值 | 含义 |
     |---|---|---|
     | `EXPERT_WORK_RUN_DRAIN_TIMEOUT_S` | 300 秒 | 等在跑对话收尾的**上限**（不是固定等待） |
     | `terminationGracePeriodSeconds` | 360 秒 | k8s 到点强杀；必须显著大于上一行 |
     | `EXPERT_WORK_ROLLOUT_TIMEOUT` | 600s | 发布/回滚脚本等滚动完成；必须大于第一行 |

     滚动策略同时改成 `maxSurge: 2 / maxUnavailable: 0`（两个新 pod 一次起齐、两个旧 pod
     并行收口）。不改的话两个旧 pod 只能一个接一个腾位子，整轮 >10 分钟，脚本必超时。
   - **预期（2026-09-18 测试环境两轮真实滚动实测，不是估算）**：

     | 观察项 | 实测 |
     |---|---|
     | 被打断的对话 | **0 条**（日志 `run.drain_done hard_stopped=0`） |
     | 一次交接耗时 | 241 毫秒 |
     | 两个旧实例各自收口耗时 | 40 秒 / 200 秒（**不是**固定 300 秒） |
     | 整轮滚动 | 83 秒 |

     收口在"最后一条对话交接出去"的瞬间就结束，不会空等到上限。`rollout status`
     比平时久**不是故障** —— 它在等对话跑完。

     **"等满 5 分钟还得硬停"基本不会发生**：`exec_python` / `bash` 自身的超时上限
     总是先于收口上限触发，工具一结束这一轮就变成可交接了。真出现 `hard_stopped>0`，
     说明有工具跑了 5 分钟以上 —— 那是另一个问题，记下来别忽略。
   - 发版前仍然建议查一次在跑数量（低峰 + 心里有数）。**这条查询由你（用户）在生产上跑**：

     ```sh
     export KUBECONFIG=~/.kube/expert-work-prod.yaml
     POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
       -o jsonpath='{.items[0].metadata.name}')
     kubectl -n expert-work exec -i "$POD" -- python3 - <<'EOF'
     import asyncio
     from sqlalchemy import text
     from control_plane.app import _build_sql_stores
     from control_plane.settings import Settings
     from control_plane.tenant_scope import bypass_rls_session

     async def main():
         stores = _build_sql_stores(Settings())
         try:
             async with bypass_rls_session():
                 async with stores.session_factory() as s:
                     rows = (await s.execute(text(
                         "SELECT status, count(*) FROM agent_run "
                         "WHERE status IN ('running','queued','paused') GROUP BY status"))).all()
             print(rows or "无在跑 / 排队 / 待审批")
         finally:
             await stores.engine.dispose()

     asyncio.run(main())
     EOF
     ```

   - 有 `paused`（等人审批）时，见第 3 条。
   - **回滚要更快时**（事故中等 5 分钟很难受）：先照 §4 换镜像，再
     `kubectl -n expert-work delete pod -l app.kubernetes.io/name=control-plane --grace-period=0 --force`。
     被强杀的副本来不及写终局，它手上的对话留在"在跑 + 租约过期"，正是接管形态，
     新副本会从存档点接着跑完 —— 快速回滚不再以打断对话为代价。
   - **发布后怎么查（推荐查审计，不要查日志）**：pod 一被回收日志就没了，审计在库里。

     ```sql
     -- 本次窗口内的交接与接手，同一个 run_id 首尾相接
     SELECT occurred_at, resource_id, reason, actor_id
     FROM audit_log
     WHERE action = 'run:failover' AND occurred_at > now() - interval '1 hour'
     ORDER BY resource_id, occurred_at;
     ```

     `reason='handed_off'`（actor `orchestrator`）= 旧实例交出去的；
     `reason='reclaimed'`（actor `system`）= 新实例接的手。两条成对出现才算闭环。
     测试环境实测间隔 8～15 秒。

     日志侧（趁实例还在时）：`run.drain_started` / `run.drain_done hard_stopped=N`。
     **`hard_stopped` 不为 0 要追**——说明有工具跑了 5 分钟以上。

3. **rollout 报超时不等于发布失败（B-56）—— 本版已修**（2026-09-18 加入本班）：
   - 修之前：`kubectl rollout status` 超时后脚本只打印 `RELEASE FAILED` 加一句
     `roll back with: …`。09-12 测试环境真发生过一次 —— 新 pod 冷启动慢、被 kubelet 重启
     5 次、约 13 分钟后自己起来了，旧 pod 全程在服务。**照那句提示做就是把一次健康发布
     回滚掉**，而且 smoke 与金丝雀（唯一能判定好坏的两件）恰好被跳过。
   - 修之后：超时会先打印**证据**——该 Deployment 的状态、它自己的 pod 行、它自己的
     事件尾 15 行——再分两支：

     | 你看到的 | 判定 | 下一步 |
     |---|---|---|
     | 新 pod `Running` / `ContainerCreating`，`RESTARTS` 在涨，旧 pod 还 `Ready` | **还在起来，不是失败** | 等它，然后手工补跑 `kubectl -n expert-work rollout status deploy/control-plane --timeout=600s`，再跑 `tools/deploy/smoke.sh prod`。**smoke 与金丝雀被跳过了，没跑之前这次发布是未验证的。** |
     | 新 pod `CrashLoopBackOff` / `ImagePullBackOff` / `Error`，或事件里是镜像 / 挂载失败 | **真失败** | 照 §4 回滚 |

   - 同时 `startupProbe` 预算从 60 秒提到 180 秒，这个情形本身更不容易触发。
   - **这是本批唯一一条改生产 Deployment 清单的改动**（`startupProbe.failureThreshold` 30 → 90）。
     滚上去的时候新 pod 用新预算，旧 pod 不受影响。

4. **审批在窗口内的两条一次性影响**（B-76 / #1582 带来的，只在滚动替换的那几分钟）：
   - **发布顺序**：新版控制面写下的裁定如果由还没替换到的旧副本执行，旧副本按老逻辑**放行整轮调用**。
     → 窗口内尽量不处理审批；rollout 全部完成后再处理。
   - **窗口前就挂着的旧审批**：发布后批准它们，可能这一步什么都不执行（平台无法证明审批单对应哪一个调用，
     一律不放行），Agent 会重新发起、再审批一次。对外表现是「批了但没动作」，不是故障。
5. **留存清理 CronJob 首跑**：`apply -k` 会创建它，但**第一次真正删数据是发布次日 03:23（北京时间）**
   → Step E 次日核对。
6. **回滚前置清理**（§4）：生产上一旦配了 `arg_bindings` 或 `render:`，回滚会让 Agent 起不来。
7. **ALB 监听超时 + `acr-pull` 凭据（B-55）—— 两个手工对象，`apply -k` 都不碰** → Step A0。
   - **AlbConfig 不在 kustomize 树里**（它是装 ack-sandbox-manager 时建的、`sandbox-system`
     的 Ingress 与我们共用同一个 ALB 实例，所有权是共享的），只能 `kubectl patch`。
     merge patch 会**整段替换** `listeners`，所以那份文件必须永远带全部监听 —— 包括 80，
     那正是沙箱网关用的那个。
   - **`acr-pull` Secret 必须同时带公网与 `-vpc` 两个 host**。dockerconfigjson 按 host 索引，
     kubelet 只查与镜像引用 host **完全相同**的那一条，没有通配也没有回退。少了 `-vpc` 那条，
     Step A apply 完之后池会停在 `availableReplicas 0` 而**什么错都不报**
     （事件里是 `insufficient_scope: authorization failed`）。
   - 两件事都必须**先于 Step A** 做完：Step A 会重建温池 pod，那一刻就要拉 `-vpc` 的镜像。

---

## 3. 执行顺序

顺序是约束：**A0 在 A 之前**（B-55：凭据与 ALB 都要先就位，A 一 apply 就去拉 `-vpc` 的镜像）、
**A 在 B 之前**（金丝雀要在新沙箱镜像上验）、**B 之后立刻做 C**、**A2 在 B 通过、C 点检完之后**（技能正文的
脚本路径靠 B-84 带来的 `EXPERT_WORK_SKILLS_DIR`，生产现版 `5775fbf3` 没有；09-26 终审改，原先放在 B 之前）。
实际顺序：A0 → A → B → C → A2 → D → E → F。

### Step A0 — `acr-pull` 两个 host + ALB 80 端口超时（B-55，本版新增）

两件事都**必须在 Step A 之前做完**，理由见 §2 第 7 条。两件都不是 `apply -k` 的范围。

**A0-1 —— 重建 `acr-pull`（两个 namespace、两个 host）**

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml

# 发前值（留档）：期望只有公网一个 host
for ns in expert-work default; do
  printf '%s: ' "$ns"
  kubectl -n "$ns" get secret acr-pull -o jsonpath='{.data.\.dockerconfigjson}' \
    | base64 -d \
    | python3 -c 'import json,sys; print(*sorted(json.load(sys.stdin)["auths"]))'
done

tools/deploy/acr-pull-secret.sh   # 交互式问用户名 + ACR 固定密码，不回显、不进 argv、不落盘
```

- [ ] 两个 namespace 都打印出**两个** host（`crpi-….personal…` 与 `crpi-…-vpc.personal…`）

脚本自己会在结尾回显每个 namespace 实际写进去的 host，对不上就停下来。
用户名 = 阿里云账号全名，密码 = ACR 个人版「访问凭证 → 固定密码」
（`docs/runbooks/workstation-setup.md` §2）。

**A0-2 —— ALB 80 端口补 `requestTimeout`**

```sh
# 发前值（留档）：期望 80 只有 port/protocol，没有任何 timeout
kubectl get albconfig alb -o jsonpath='{.spec.listeners}{"\n"}'

kubectl apply -f infra/k8s/cluster/prod/albconfig.yaml
```

⚠️ **生产用 `apply -f prod/albconfig.yaml`**（它是完整对象，还带同文件里的 IngressClass），
**不是** `kubectl patch --patch-file albconfig-listeners-patch.yaml` —— 那一份是**测试**集群的，
带的是测试的证书 id；两份的 `listeners` 现在内容相同，但证书不同，用错会把生产 443 的证书
换成测试的。这条 `apply -f` 与 `docs/runbooks/production-release.md` §1.3 里建集群时那条**是同一条**，
重复执行幂等。

```sh
# 发后值：80 与 443 都应有 idleTimeout 60 / requestTimeout 180
kubectl get albconfig alb -o jsonpath='{.spec.listeners}{"\n"}'
```

- [ ] 80 端口出现 `requestTimeout: 180`
- [ ] 443 端口的 `CertificateId` **没变**（还是生产那张）

> `listeners` 是整段替换的，所以那份文件必须带齐所有监听 —— 包括 80，那是沙箱网关用的。
> 生效是秒级的，不重启任何 pod，不影响在途请求。

### Step A — 沙箱镜像钉子（`e8aac104` → `<新 tag>`，且 host 改成 `-vpc`）

⚠️ **本版这一步同时换两样东西**：tag（新镜像，砍掉了 ffmpeg / npm）和 **host**（公网 → `-vpc`）。
`sandboxset.yaml` 里两样都改好了，照常 `apply -f` 即可，但发后核对要**两样都看**。

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml

# 发前值（留档）。期望 crpi-….personal.cr.aliyuncs.com/expert-work/sandbox:e8aac104
# —— 公网 host + 老 tag。对不上说明中间有人动过，停下来先弄清楚
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'

kubectl apply -f infra/k8s/sandbox/sandboxset.yaml

# 发后值应为 crpi-…-vpc.personal.cr.aliyuncs.com/expert-work/sandbox:<新 tag>
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'
```

- [ ] 已 apply，host 变成 **`-vpc`**、tag 变成新 tag（两样都要核）
- [ ] 池 pod 重建完成（`kubectl -n default get pods | grep sandbox`）
- [ ] `kubectl -n default get sandboxset expert-work-sandbox -o jsonpath='{.status.availableReplicas}'` 回到 `1`

⚠️ apply 会重建温池 pod，**在途沙箱会被打断** —— 所以放在窗口内、B 之前。

**卡在 `availableReplicas 0` 怎么读**（B-55 的两个已知形态）：

```sh
kubectl -n default get events --field-selector involvedObject.kind=Pod | grep -i -E "Pull|Failed"
```

| 事件里看到 | 说明 | 修法 |
|---|---|---|
| `insufficient_scope: authorization failed` | A0-1 没做或只写了一个 host | 回去做 A0-1 |
| `Pulling` 之后长时间没有 `Pulled` | 正常冷拉。**瘦身后测试集群实测 40.5s**（瘦身前 66.2s；公网 112s） | 等 |

### Step B — 发版（单段）

```sh
git fetch origin main
git checkout 54d1ed70
git log -1 --oneline            # 确认就是它

tools/deploy/release.sh prod    # 输入 'prod' 确认；或 --yes
```

- [ ] 确认 checkout 的是 `54d1ed70`（`git log -1` 标题是 `fix(orchestrator): make delegation guidance truthful and non-contradictory (#1708)`，父提交是 `6eba2435`（#1709）；`git merge-base --is-ancestor 54d1ed70 origin/main` 退出码 0 = 在 main 上）
- [ ] 三个镜像建推成功（ECR Public 限流是已知形态 —— 失败先把三个 base 全拉一遍再重跑）
- [ ] migrate Job `condition met`，且日志里出现**四条** upgrade：`0155… -> 0156_thread_message_hidden`、`0156… -> 0157_thread_mirror_resweep`、`0157… -> 0158_run_completion`、`0158… -> 0159_skill_usage_viewed`（本版不是空跑）
- [ ] 全部 Deployment rollout 完成
- [ ] **smoke 全绿，且阶段 6 金丝雀是 PASS 不是 WARNING**
- [ ] smoke 里的沙箱钉子检查是 `OK`（Step A 做过了；显示 `WARN 落后 N` 说明 Step A 漏了）
- [ ] overlay 的 newTag 改动先别提交 —— Step F 一起记

> Step B 失败要回滚见 §4。此时 Step A2 还没做，技能不受影响；若 A2 做完之后才决定回滚，§4 里有「技能也要一起回」那一条。

### Step C — 发后即时点检（rollout 完成后 10 分钟内）

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
kubectl -n expert-work get pods            # 无 CrashLoop、重启计数为 0
kubectl -n expert-work get deploy -o 'custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'
```

- [ ] 三个应用镜像都是 `54d1ed70`（admin-ui 是 `54d1ed70-prod`）
- [ ] **B-126 / B-128 / B-131 代码在位、目录有 glm-5.3-flashx、pyjwt 2.15.1 / urllib3 2.8.0、委派修复在位（#1710 / #1709）**（只看符号与版本号）：

      ```sh
      POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
      kubectl -n expert-work exec "$POD" -- python3 -c 'from orchestrator.context.system_prompt_view import keep_latest_system_prompt; from orchestrator.context.tool_result_prune import prune_prior_turns; from orchestrator.graph_builder.planner import complete_open_steps; import jwt, urllib3; assert jwt.__version__ == "2.15.1", jwt.__version__; assert urllib3.__version__ == "2.8.0", urllib3.__version__; from expert_work.protocol.model_catalog import catalog_entry; assert catalog_entry("glm", "glm-5.3-flashx") is not None; from orchestrator.context import plan_md_digest; from orchestrator.tools._child_run import _deadline_result; print("ok")'
      ```

      期望 `ok`。
- [ ] 全 pod Running、零重启
- [ ] **留存 CronJob 已创建且参数正确**：

      ```sh
      kubectl -n expert-work get cronjob retention-cleanup \
        -o jsonpath='{.spec.schedule}{"  tz="}{.spec.timeZone}{"  suspend="}{.spec.suspend}{"\n"}'
      ```

      期望 `23 3 * * *  tz=Asia/Shanghai  suspend=false`。

- [ ] **对话可用**（金丝雀之外再看一眼真实流量）：控制台随便打开一段最近会话，能正常加载。
- [ ] **B-122 子智能体工具边界是开着的**（默认开；生产不该配回退阀）。只看变量名在不在、不打印任何取值：

      ```sh
      for p in $(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
          --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
        printf '%s: ' "$p"
        kubectl -n expert-work exec "$p" -- python3 -c 'import os; from orchestrator.tools.worker_policy import WORKER_POLICY_ENV as k, worker_policy_enabled as f; print("set=", k in os.environ, " enabled=", f())'
      done
      ```

      每个 pod 期望 `set= False  enabled= True`。`set= True` 说明有人在 ConfigMap / Secret / Deployment 里配了
      `EXPERT_WORK_WORKER_TOOL_POLICY`，先弄清楚是谁、为什么，再决定是否保留。
- [ ] **B-119 文案已上线**（销案判据）：生产 `SANDBOX_UNAVAILABLE_NOTE` 是新文案：

      ```sh
      POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
      kubectl -n expert-work exec "$POD" -- python3 -c 'from orchestrator.tools.sandbox_image_contract import SANDBOX_UNAVAILABLE_NOTE as n; print("需要生成 Word/PPT 时改用 Python 等价物" in n, "技能文档里的" in n)'
      ```

      期望 `True False`（新文案在、旧文案「技能文档里的 `npm install`」不在）。
      过了就把 ROADMAP B-119 销案。

### Step A2 — 导入 office 技能（docx / pptx / xlsx / pdf，本版新增）

必须在 **Step B 通过之后**（smoke 全绿、金丝雀 PASS，且 Step C 点检完）才做，理由（09-26 终审改，原先放在
Step B 之前是错的）：新技能正文里的脚本路径全部写成 `$EXPERT_WORK_SKILLS_DIR/<技能>/scripts/…`，这个环境变量
是 B-84（`233791e5`）才加的，随本班钉子（`f92c6fae` 起就有，`54d1ed70` 照带）上生产；生产现版 `5775fbf3` 没有它，路径会展开成
`/docx/scripts/…`，脚本调用全部失败。若先导入技能再发版，从导入到 Step B 结束这段时间里 office 技能不可用，
Step B 一旦回滚就一直坏下去（见 §4）。自然也在 **Step A 之后**（技能脚本依赖新沙箱镜像里的 LibreOffice 与
预装库，第二层测试验的也是这份镜像）。

放在 Step B 之后，**缓存失效就要靠本步自己**：Step B 的滚动重启已经发生过，不会再顺带清缓存。导入脚本在
有新版本时会发一次跨副本失效广播；没发到时要补一次 `rollout restart`（见下面「正式导入」的检查项）。

**逐技能 go / no-go**：以 09-27 晚测试环境验收结论为准（controller 拍板，结论写进 §6）。

> **09-27 结论：四个技能全部 GO。** 测试环境导入 v2 后第三层验收 8 项（新建 docx/pptx/xlsx/pdf、按模板出文档、复制 PPT 页、Word 转 PDF、PDF 加水印）：全部打开技能、跑技能脚本、npm 0 次、成品可打开且内容正确；唯一系统性问题是「检查成品」看图步骤写错（7/8 首次 ask_image 报错）→ #1679 修正后导入 v3 复验 4 项全部 completed、工具失败 0。ai-health-plan 回归（虚构客户出 Word 方案）completed、0 工具失败、按新步骤看图。**没过的技能
本步跳过、保持生产原版不导入**——下面的命令只把 go 的技能对应的 `$PS/dist/<name>.skill`
传给 `import_in_pod.py`，不要整批 `*.skill` glob 把 no-go 的也带上。

**用哪份源码打包**：用 office 技能在 main 上的最后一个改动提交 `58c0f2a9`（#1678 + #1679，09-27 回填，
即测试环境验收 GO 的那一份）打包导入。第九次重钉之后，Step B 检出的钉子（现为 `54d1ed70`）本身就包含它，而且
`git diff 58c0f2a9 54d1ed70 -- platform-skills/docx platform-skills/pptx platform-skills/xlsx platform-skills/pdf platform-skills/shared platform-skills/build.py platform-skills/import_in_pod.py` 为空 —— 四个 office 技能的源码逐字相同（`58c0f2a9` 之后 `platform-skills/` 只多了 B-125 的 `health-plan-report/` 与 `tests/`，由 Step A3 另导）。（09-26 拍板的「发版钉子不前移」
已被 09-27 第九次重钉推翻，office 技能的代码侧文案随本班上生产，见 ROADMAP B-119 与 Step C 的核对项。）
仍然**用独立 worktree 打包、不直接用主仓库的 `platform-skills/`**：主仓库目录此刻有 Step B 留下的
**未提交的 overlay newTag 改动**（Step F 要用），别在它上面 checkout；worktree 钉死提交，打包输入不随主仓库
工作区变化，下面的回滚做法也是同一个形状。命令仍在主仓库目录里跑（要用仓库自己的 venv；在 worktree 目录里跑
`uv run` 会建一个没有依赖的空 venv）。`build.py` 的输入与 `dist/` 输出都按它自己所在的目录定位，与当前目录无关：

```sh
git fetch origin main
git worktree add /tmp/ps-office 58c0f2a9
git -C /tmp/ps-office log -1 --oneline   # 确认就是它
```

`import_in_pod.py bundle` 在本机导入的 `control_plane` 代码来自主仓库（`54d1ed70`），与生产 pod 里跑的是
同一版，这正是要的。

**导入前只读核对**：

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
kubectl config current-context  # 执行 exec 前务必确认连的是生产
POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
```

① 四个平台技能当前版本（`run_sql`，用 §1 已定义的那个包装）：

```sql
SELECT s.name, s.latest_version, v.content_hash
FROM skill s JOIN skill_version v ON v.skill_id = s.id AND v.version = s.latest_version
WHERE s.tenant_id IS NULL AND s.name IN ('docx','pptx','xlsx','pdf') ORDER BY s.name;
```

② 绑定这四个技能的 Agent 数（同一份 `run_sql`）：

```sql
SELECT sk AS skill, count(*) AS agents FROM agent_spec a,
  jsonb_array_elements_text(COALESCE(a.spec_json->'spec'->'skills','[]'::jsonb)) sk
WHERE a.status <> 'deleted' AND sk IN ('docx','pptx','xlsx','pdf') GROUP BY 1 ORDER BY 1;
```

- [ ] 两条查询结果已记入 §6（导入前基线）

**dry-run**（只看会不会建版本，不落库）：

```sh
uv run --no-sync python /tmp/ps-office/platform-skills/build.py   # 写到 /tmp/ps-office/platform-skills/dist/
PS=/tmp/ps-office/platform-skills

# 下面按四个技能全部 go 写；哪个 no-go 就把它的 .skill 从这行删掉
uv run --no-sync python $PS/import_in_pod.py bundle --dry-run \
  $PS/dist/docx.skill $PS/dist/pptx.skill \
  $PS/dist/xlsx.skill $PS/dist/pdf.skill \
  | kubectl -n expert-work exec -i "$POD" -- python3 -
```

- [ ] 每个 go 的技能都输出一行 `"status":"dry-run"`，`would_create_version` 是期望的下一个版本号

**正式导入**（同一份文件列表，去掉 `--dry-run`）：

```sh
uv run --no-sync python $PS/import_in_pod.py bundle \
  $PS/dist/docx.skill $PS/dist/pptx.skill \
  $PS/dist/xlsx.skill $PS/dist/pdf.skill \
  | kubectl -n expert-work exec -i "$POD" -- python3 -
```

- [ ] 每个 go 的技能输出 `"status":201`（新建版本）或 `"status":200`（`content_hash` 与 `latest` 相同，
      幂等跳过，不产生冗余版本）
- [ ] 只要有一个 `201`，输出里出现 `{"invalidation":"published","receivers":N}`（N ≥ 1 = 有 N 个订阅者
      收到失效，不保证是全部副本）。Step B 的重启已经过去了，**这一行就是各副本换上新技能的唯一信号**
- [ ] 若输出 `{"invalidation":"skipped",...}` 且退出码非 0：版本已写入，只是失效没发到。**不要重跑**（重跑
      全是 `200`，不会再发失效）——补救用 `kubectl -n expert-work rollout restart deploy/control-plane`
      （会再滚一次 control-plane，在跑的对话按 B-80 交接），或接受最多 1800s 后缓存自然过期；选哪个记进 §6
- [ ] 若中途某个包失败：已输出的 `201` 行已经落库，失效行照样会打印；把失败原因记进 §6 后按该技能 no-go 处理

**导入后只读核对**：把①②两条 SQL 再跑一遍，写入 §6。做完删掉临时 worktree：
`git worktree remove /tmp/ps-office`。

- [ ] go 的技能：`latest_version` / `content_hash` 与刚才导入输出的 `version` / `content_hash` 一致
- [ ] 绑定 Agent 数（②）与导入前**不变**——导入只加版本，不改任何 Agent 的技能绑定
- [ ] no-go 的技能：`latest_version` / `content_hash` 与导入前**不变**（没碰过）

**回滚**：某个技能新版本有问题——`git worktree add /tmp/ps-rollback <旧提交>`，在**主仓库目录**里跑
`uv run --no-sync python /tmp/ps-rollback/platform-skills/build.py`（打包的是 worktree 里的旧源码，输出在
`/tmp/ps-rollback/platform-skills/dist/`），先 `PS=/tmp/ps-rollback/platform-skills`（上面的 `/tmp/ps-office` 此时已删），再按上面「正式导入」把 `$PS/dist/<技能>.skill`
重新导入（平台存成更新的版本，内容等同旧版），最后 `git worktree remove /tmp/ps-rollback`。**不要**
`git checkout <旧提交> -- 路径`（会覆盖工作区未提交的改动），也**不要**在 worktree 目录里跑 `uv run`（空 venv）
或从主仓库跑 `platform-skills/build.py`（打的是主仓库当前的源码）。**不要**手工改 `skill.latest_version`
或直接删版本行。每个技能独立回滚，互不影响其它三个。需要紧急退回 Anthropic 原版：控制台导出第 1 版
（原版保留在版本历史里未删，见 ROADMAP B-117）再重新导入。

### Step A3 — 导入 `health-plan-report` 技能并启用（B-125，本版新增）

在 **Step A2 之后**做（理由见 §0.2 的依赖）。打包方式同 A2：用独立 worktree 打包，不用主仓库目录的 `platform-skills/`。
**打包源码钉在本版 tag `54d1ed70`，不用 `origin/main`**（2026-10-02 改）：10-08 前主干会继续合入新开发，从 main 打包会让发版内容随主干漂移；钉住之后，主干上无论合什么都影响不到这一步。2026-10-02 已按下面的命令在 `54d1ed70` 上实打一次，`content_hash` = `786d30fe`（与测试环境 v6 一致；`54d1ed70` 之后主干上 `platform-skills/` 零改动）。

```sh
git worktree add /tmp/ps-hpr 54d1ed70
uv run --no-sync python /tmp/ps-hpr/platform-skills/build.py --only health-plan-report
PS=/tmp/ps-hpr/platform-skills
uv run --no-sync python $PS/import_in_pod.py bundle --dry-run $PS/dist/health-plan-report.skill \
  | kubectl -n expert-work exec -i "$POD" -- python3 -
```

- [ ] dry-run 输出 `"status":"dry-run"`；生产上是**首次导入**，应为 `"created": true` 的新建（不是版本递增）
- [ ] 打包出的 `content_hash` 应为 `786d30fe…`（测试环境 v6，B-131 加「改稿速查」；v5 是 `cc2df66a`）；不同说明 checkout 的不是 `54d1ed70`，或本机 venv 不对（见 §1「本机 venv 能跑打包导入」），停下来先查

正式导入去掉 `--dry-run`：

- [ ] 输出 `"status":201` 与 `{"invalidation":"published","receivers":N}`（N ≥ 1）
- [ ] **⚠️ 首次导入的技能落库是草稿（DRAFT），必须在控制台「平台技能」里把 `health-plan-report` 启用**。
      测试环境实测：不启用时 Agent 调用返回 422 `skill 'health-plan-report' is not in 'active' status`
- [ ] 做完 `git worktree remove /tmp/ps-hpr`

**回滚**：在控制台把 `health-plan-report` 停用即可（A4 若已做，先回 A4）。技能本身不影响其它 Agent。

### Step A4 — 发布 ai-health-plan 提示词 rev39（B-125，本版新增）

在 **Step A3 启用之后**做。rev39 是在**测试环境 rev38** 上改出来的，生产的提示词未必与测试一致：

- [ ] **发布前一天（只读）**：导出生产 `ai-health-plan` 当前的提示词与技能绑定，与测试环境 rev38 逐字比对；
      结果记入 §6。**完全一致** → 直接发 rev39；**有差异** → 把差异拿给用户，合并成生产版 rev39 后再发，不要直接覆盖
- [ ] 保存生产当前配置的备份（控制台导出或 API 取 manifest），作为回滚点
- [ ] 按 rev39 更新：提示词正文（含第 2 行改写）+ 技能绑定 `pptx` / `pdf` / `docx` / `health-plan-report` + MCP 工具 `form_get_field_detail` 参数 `detail_level` 选「固定值」填 `brief`；**保存后必须「发布」草稿**（保存 ≠ 上线）
- [ ] 同一次保存里删掉 8 个工具（生产配置里有哪个删哪个，没有的跳过）：内置 `web_search` / `list_artifacts` / `remember` / `note_behavior_patch` / `clarify_tool_usage`、`http` 工具、MCP 允许列表里的 `customer_search_by_cpwx_tags` / `customer_list_cpwx_tags`；**这两个 MCP 工具上的参数绑定要一起删**，否则配置引用了不在允许列表里的工具
- [ ] 保存时若提示 `detail_level` **绑定落空**（上游 schema 里没有这个参数）：**预期内，照常发布** —— 对方生产 MCP 还没上 brief 版本时就是这样，平台不注入、行为与不配相同；对方上线后下一次构建起自动生效（主 Agent 构建缓存最长 1800s）。记入 §6
- [ ] 核对线上版本号已递增、技能绑定为上面四个、`form_get_field_detail` 的 `detail_level` 是固定值 `brief`
- [ ] 控制台 Agent 的「工具」清单里没有上面除 `list_artifacts` 外的 7 个（`list_artifacts` 是平台基础能力，会自动带上，属预期）

**回滚**：用备份把提示词、技能绑定、工具清单与工具参数恢复成发布前的版本并发布。**回滚镜像（§4）之前必须先做这一步** —— 旧镜像读不了带 `fixed` 的配置。

### Step D — 真栈验证（本版新功能，按需做，全部只用金丝雀）

> 对接方的 agent（`ai-health-plan` / `sop2-designer`）**不做实验**，只读观察。

- [ ] **B-66**：对 `release-canary` 的一轮做 `:regenerate`，确认这一轮**真的重新调用了模型**
      （用量不为 0）。提示词里带一个随机串，避开响应缓存。
- [ ] **B-67 / B-61（只读观察，可留到次日）**：对接方跑过一轮之后，在控制台看那一轮的系统提示词
      —— 变量位置应当是 `$EXPERT_WORK_INPUTS_DIR/...` 的本地路径，不再是原始签名链接。
- [ ] **审批（可选）**：rollout 全部完成之后再处理任何待审批；确认批准之后只执行审批单上那一条。
- [ ] **B-106**：新 pod 起来后的**第一个** run（金丝雀就是）不再卡顿；`kubectl -n expert-work get pods` 的 RESTARTS 仍是 0
      （修之前每个新进程首个 run 卡 56~63s 被存活探针杀）。
- [ ] **B-102~104**：金丝雀这一轮的 `end` 帧 `usage_by_model` 有值；控制台该会话的成本 —— 价目表缺的模型会显示 0，
      与 §1 ① 的结果对得上即可。
- [ ] **B-124（只读观察，可留到次日）**：首个带子智能体的真实会话里，子智能体写的文件父能 `read_file`（看该 run 的 `run_event` 里没有 `read_file` 的 `not_found`）。
- [ ] **B-105（只读观察）**：发布后 24h 内看 `expert_work_llm_output_truncated_total` 有没有 `usable="false"` 的增长。
      有增长 = 某个 Agent 的上限偏小开始截断，对照 §1 ② 的盘点结果找到它，和负责人商量调大上限。

### Step E — 次日核对（发布次日早上）

- [ ] **留存清理首跑**：

      ```sh
      kubectl -n expert-work get jobs -l app.kubernetes.io/name=retention-cleanup
      kubectl -n expert-work logs job/<上面那个 job 名> | tail -40
      ```

      看每类规则的删除计数。**首跑的预期形态**：生产 2026-09-07 才上线、数据不到 3 周，
      而产物 / 上传 / 图片 / 出网审计 / 工作区归档 / 成员 / 记忆这些规则都是 **90 天**，
      所以它们这次应当是 **0**；可能非 0 的只有 `jwt_blacklist`（按 token 过期时间删）
      与孤儿行清理。**某一类一次删掉成千上万行 = 不正常**，先暂停再查：

      ```sh
      kubectl -n expert-work patch cronjob retention-cleanup -p '{"spec":{"suspend":true}}'
      ```

- [ ] **RLS 补丁信号**（#1573 的验收判据）：过去 24h 的日志里，`rls.would_fail_closed` 中
      `rls_caller_outer` 落在 `workspace_janitor` / `memory_consolidator` 这两个模块的条数应为 **0**
      （其它模块还有若干不带上下文的调用点，是 enforce 之前的另一批账，不在本版范围）。
- [ ] 对接方那边这一晚的对话没有异常反馈。

### Step F — 记录

- [ ] `chore(deploy): prod newTag 54d1ed70` 记录 PR，正文写上：上一版 `5775fbf3`、本版装载、
      沙箱钉子 `e8aac104 → 7ac31957`、留存 CronJob 首次接入、回滚命令。
- [ ] ROADMAP 班车 2 行销案，B-64 / B-102~104 / B-105 / B-121 / B-122 / B-123 / B-124 / **B-125** / **B-126** / **B-127** / **B-128** 的「生产待发」改成已上线，B-119 按 Step C 核对结果销案；本执行单补 §6 执行记录。

---

## 4. 回滚

```sh
tools/deploy/rollback.sh prod 5775fbf3
```

一个档位就够：本版**有四条迁移但都不用退**，没有数据搬迁，旧镜像直接能跑在当前 schema 上。

🚫 **不要 `alembic downgrade`。** `0156` 是纯加列（`thread_message.hidden`，带默认 `false`）：
旧版本的 ORM 不映射这一列，既不 SELECT 也不 INSERT，`server_default` 兜住写入 —— 多这一列
对旧代码完全无害。反过来 downgrade 会把新版本已经写进去的 `hidden` 全抹掉，而回滚窗口里
随时可能再滚回来，抹掉的值只能等 sweep 重扫才补得回来（还只补有新活动的线程）。

⚠️ **回滚前必须先清三样东西**，否则旧代码会起不来或行为不对：

| 东西 | 为什么 | 回滚前怎么办 |
|---|---|---|
| Agent 配置里的 `arg_bindings`（工具参数绑定） | 旧版 `MCPToolSpec` 是 `extra="forbid"`，读到带 `arg_bindings` 的配置**校验失败 → Agent 起不来** | 回滚窗口内**别在生产配绑定**；配了就先在配置页删掉再回滚 |
| 变量上的 `render: raw` | 同上，旧版 `PromptVariableSpec` 也是 `extra="forbid"` | 同上 |
| 模型上的「思考长度上限」（`thinking_max_tokens`，B-105） | 旧版 `ModelSpec` 也是 `extra="forbid"`，读到这个字段**校验失败 → Agent 起不来**。留空的上限不落库，所以只有**真填了**的才有问题 | 回滚前先在配置页清空再回滚。另：回滚后非 Anthropic 的输出上限重新**全部不生效**，豆包「开思考不填档位」重新 400 |
| 工具参数固定值 `arg_bindings[].fixed`（B-127，rev39 用了它） | 旧版 `ArgBindingSpec` 是 `extra="forbid"`，读到 `fixed` **校验失败 → Agent 起不来** | 回滚前先按 Step A4 的「回滚」把 ai-health-plan 恢复成发布前配置；其它 Agent 配了也一样先删 |
| B-126 的上下文策略字段（`policies.tool_result_prune.cross_turn` / `min_context_tokens` / `min_reclaim_tokens` / `absolute_cap_tokens`） | 同上，旧版读不了。取默认值的不落库，所以只有**真改过**的才有问题 | 回滚前先在配置页恢复默认再回滚 |
| 留存 CronJob | `apply -k` 旧 overlay **不会**删掉已创建的对象 | 要一并退掉就显式删：`kubectl -n expert-work delete cronjob retention-cleanup` |

⚠️ **Step A2 已做（新 office 技能已导入）时，回滚镜像必须连技能一起回。**回到 `5775fbf3` 后控制面没有
`EXPERT_WORK_SKILLS_DIR`，新技能正文里的脚本路径全部失效，office 技能会一直坏着。`rollback.sh` 跑完立刻
把本次导入过（201）的技能退回上一版：控制台「平台技能」页导出第 1 版（Anthropic 原版，仍在版本历史里，
见 ROADMAP B-117），再逐个上传导入（平台存成新版本，内容等同原版）。导入后用 Step A2 的 SQL ① 核对
`content_hash` 回到了 A2 导入前的基线值。只导入了其中几个就只回那几个。

沙箱钉子单独回：`git checkout 5775fbf3 -- infra/k8s/sandbox/sandboxset.yaml && kubectl apply -f infra/k8s/sandbox/sandboxset.yaml`
（回到 `e8aac104`）。

其它回滚事实：

- **只是 B-122（子智能体工具边界）出问题时不用走本节**：在 control-plane 上设 `EXPERT_WORK_WORKER_TOOL_POLICY=off`
  并重启 control-plane pod，子智能体的工具与父侧文案逐字节回到 B-122 之前（见 §0.1 第九次重钉）。B-123、B-124 没有单独的阀。
- **委派三项修复（#1710 / #1709 / #1708）没有阀**：出问题只能回滚镜像；旧镜像忽略新增的两个检查点字段。回滚后再前进，回滚期间有对话的 plan_execute 会话第一轮会把旧镜像写的 PLAN.md 当成人改读回一次（见 §0.1 第十四次重钉）。
- **glm-5.3-flashx 回滚后**：已选它的 Agent 保存 / 运行会报模型不在目录（生产当前无人用）；依赖升级随镜像一起回退，无数据面影响。
- **B-131 没有阀**：出问题只能回滚镜像；它不写新字段，旧镜像读新检查点无影响；`health-plan-report` v6 在旧镜像上照常可用，不用回退技能。
- **只是 B-126 跨轮清理出问题时不用走本节**：在出问题的 Agent 配置里设 `policies.tool_result_prune.cross_turn: false` 并发布。20 万封顶、摘要复用与 B-128 没有阀，只能回滚镜像；旧镜像读带 `context_summary` 的检查点不报错（测试环境已验），B-128 不写数据。
- **回滚会退回「发布打断在跑的对话」的老行为**（B-80 的修复在本版里）：回滚窗口内被
  打断的对话救不回来，只能让用户重发。要回滚得快，见 §2 第 2 条末尾的强杀写法 ——
  但注意那条只有**回滚前**（还是本版代码）才带接管效果；镜像换回旧版之后就没有了。
- 审批：回滚后旧代码恢复「批准放行整轮」的老行为（那是被本版修掉的漏洞），
  所以**回滚窗口内同样不要处理审批**。
- **B-56 的判定表在回滚路径上同样适用**：`rollback.sh` 也等 rollout，也会超时。
  回滚时看到超时，先按 §2 第 3 条那张表判「还在起来」还是「真失败」，别再套一层回滚。
- ⚠️ `rollback.sh prod` 至今没有在生产上实跑过。真要用时先 `kubectl -n expert-work get deploy -o wide`
  记下当前镜像，跑完再比一次。

---

## 5. 收工确认

- [ ] Step A~F 都做完，overlay 的 newTag 改动已进记录 PR
- [ ] `kubectl -n expert-work get pods` 无 CrashLoop、无异常重启
- [ ] 本执行单 §6 填好并归档（一次性文档，别被下一次误用）

---

## 6. 执行记录（发完当晚写）

| 项 | 实况 |
|---|---|
| 窗口 | `___ : ___` ~ `___ : ___` |
| 发布前在跑 / 排队 / 待审批 | `___` |
| Step A 沙箱钉子 | 发前 `________` → 发后 `________` |
| Step B smoke / 金丝雀 | `________` |
| Step A2 office 技能导入（B、C 之后） | go/no-go(docx/pptx/xlsx/pdf)**全部 GO**（09-27 测试环境验收，见下）；导入前 latest_version `________` → 导入后 `________`；失效 published(N=`__`) / skipped → restart 或等 1800s `________` |
| Step A3 health-plan-report 导入 + 启用 | dry-run `created`/hash `________`；导入 `201` + published(N=`__`)；控制台启用 `____` |
| Step A4 ai-health-plan rev39 | 生产 vs 测试 rev38 比对 `一致 / 有差异（已合并）`；备份位置 `________`；发布后版本号 `____`、技能绑定 `____`；删掉的工具 `________`（生产原本没有而跳过的 `____`）；工具清单总数 `____` |
| B-125 发后验证 | 用户指定的测试客户跑一轮出方案：PPTX `____` / PDF `____`（`ok:true`、打开正常） |
| migrate Job | 期望跑四条（`0156` ~ `0159`），实况 `________` |
| CronJob 创建 | `________` |
| 次日首跑删除计数 | `________` |
| §1 ① 价目表缺的模型 / 处理方式 | 09-27 核对 8 个全缺 → 用户拍板按官网标价补 7 个（DeepSeek 取高峰价），已写入生产（审计 actor `rate-card-bootstrap-2026-09-27`），复核 7 有；`qwen-plus`（重排序）不在模型目录、价目表拒收，接受记 0（ROADMAP B-120） |
| §1 ② 会变化的上限（Agent / 值） | 09-27 核对：ai-health-plan 的 glm-5.3 / qwen3.8-max 与 deepseek-v4-pro 的 40960 开始生效（≥16000，测试已验）；豆包看图 medium 档位开始生效；4096 的 glm-5.3 / kimi-k3 不变；无「豆包开思考无档位」行。无需动作 |
| Step C B-126 / B-128 / B-131 在位 + flashx 在目录 + pyjwt 2.15.1 / urllib3 2.8.0 + 委派修复在位 | `____`（期望 `ok`） |
| project-service 计费改动（豆包补价 / 未定价桶 / 缓存价） | 上线时间 `________`、版本 `________`（必须早于 Step B） |
| Step A4 `detail_level` 固定值 | 保存时是否提示绑定落空 `____`（对方生产 MCP 未上 brief 时预期提示） |
| Step C B-122 阀 / B-119 文案 | `set= ___ enabled= ___` / `____ ____`（期望 `False True` / `True False`） |
| B-124 首个带子智能体的会话 | 父 `read_file` 子代文件 `____`（期望无 `not_found`） |
| 与执行单不符之处 | `________` |
