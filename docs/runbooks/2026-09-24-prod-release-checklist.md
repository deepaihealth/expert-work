# 2026-09-24 生产发布执行单（班车 2）

> 一次性文档，发完归档。通用流程在 [`production-release.md`](./production-release.md)，
> **这份只列那一份不覆盖的东西**。上一班的单子在
> [`2026-09-14-prod-release-checklist.md`](./2026-09-14-prod-release-checklist.md)（已归档）。

| | |
|---|---|
| 发布日 | **2026-09-24（周四）**，用户 2026-09-17 拍板（原 09-22） |
| 上一版 tag（回滚用） | **`5775fbf3`**（班车 1 的 B2，2026-09-16 18:51 上线） |
| 本版 tag | **`233791e5`** —— 测试环境 2026-09-19 第二次发过的那一版（`SMOKE PASS` 一遍过，金丝雀两次连过） |
| 区间提交数 | **52**（`git log --oneline 5775fbf3..233791e5`） |
| 数据库迁移 | **两条**：`0156_thread_message_hidden`（expand-only，`thread_message` 加 `hidden` 一列带默认 `false`）+ `0157_thread_mirror_resweep`（**数据迁移**，一句 `DELETE FROM thread_message_sync`）。migrate Job 自动跑，不需要额外动作 |
| 段数 | **单段**。有迁移但不是三段式：`0156` 纯加列、`0157` 只清一张派生状态表，都没有数据搬迁、没有 expand/contract 关系，新旧两版代码都能在这张表上正常跑 |
| 回滚纪律 | **只回镜像，不要 `alembic downgrade`。** 多一列对旧版本无害（旧 ORM 不映射它，既不 SELECT 也不 INSERT，`server_default` 兜住）；downgrade 会把新版本写进去的 `hidden` 全抹掉，而回滚窗口里随时可能再滚回来。`0157` 的 downgrade 是空转，`downgrade -1 && upgrade head` 会把那句 DELETE **再跑一遍**（只是多触发一次全量重扫，不丢数据，但没必要） |
| 沙箱镜像钉子 | `e8aac104` → **`621249f6`**（Step A） |
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
  ⚠️ **③ 用户 2026-09-19 拍板也带上，但它还没开工** —— 这是本单里唯一一条
  「已决定装载、代码尚不存在」的。`status=success` 只表示「图跑完没抛异常」，不表示
  「事做成了」。**不改 `status` 语义**（那是对外契约变更，对接方正在读它），而是另给一个
  「本轮有工具失败且模型放弃了」的独立信号。做完要走「发一次测试环境 → 重钉」，
  与 B-55 同一条路径。**没做完就不能进这班车** —— 到 09-22 还没进测试环境的话，
  是「这一条延期」还是「班车延期」，是拍板题。
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

> **钉子纪律**：本单钉 `233791e5`。发布日若要带上它之后的**任何代码或 admin-ui 文档站改动**，
> 必须**先发一次测试环境验过**再改钉子 —— 别在发布当天直接发 main HEAD。
>
> **改期记录**：2026-09-18 先钉 `498492d5`（#1591），当天下午用户拍板把 B-56 / B-72 / B-65 /
> B-73 四条一起带上，重钉 `dfd4e6de`（#1597）。当晚发现 B-73 ① 在生产上只能修一半
> （写隐藏消息的源头有三个，另外两个早就在生产跑），补 `0157` 逼 sweep 重扫，重钉 `233791e5`（本单）。
> 形态：无迁移 → 一条 expand-only → **两条（`0156` 加列 + `0157` 数据迁移）**，全程仍是单段。
>
> **2026-09-19 第四次重钉：`233791e5`（#1618）。** 这一版在测试环境**实际发过两次**
> （上一个钉子 → `f5b78124` → `233791e5`），比上一个钉子多 10 个提交
> （**这里刻意不写旧 sha 的字面量** —— 判据是 `grep '<旧 sha>'` 零命中，
> 历史叙述里留一个反而让判据永远过不了）：
>
> - **`anyio` 4.13.0 → 4.14.2**（#1605）—— 两个 CVE 卡住了当时每一个 PR 的 `Security (pip-audit)`。
>   只改 `uv.lock`；锁里出现两个 anyio 是预期的解析分叉，运行时镜像与 CI 都是 python 3.12，落地的是 4.14.2。
> - **B-80 两个修**（#1599）—— 排空中的旧 pod 不再被算成发布失败 + 排空过程落审计。
> - **B-81 沙箱 pip 镜像源**（#1602）—— 三个 `EXPERT_WORK_SANDBOX_PIP_*` 已经在
>   `overlays/prod/configmap-patch.yaml` 里，随 `apply -k` 一起上，**不需要额外手工步骤**。
>   阿里云杭州实测：官方 CDN 16 KiB/s → 内网镜像 3863 KiB/s，同一个 wheel 26 分钟 → 2 秒。
> - **B-82 预装清单**（#1606）+ **B-84 技能摘要瘦身**（#1608）—— 都是纯代码（工具描述 / 系统提示词）。
>   ⚠️ **这一句当时写的是「不重烤沙箱镜像，沙箱镜像钉子仍是 `621249f6`」，09-19 晚已不成立**：
>   B-55 的 #1623 改了 `infra/sandbox-image/Dockerfile`（砍 apt ffmpeg / npm），**本版要重烤**，
>   沙箱镜像钉子要从 `621249f6` 换到 `ef9e157a`。见下一段的待重钉表。
>
> 迁移**仍是两条**（`0156` / `0157`），形态仍是单段，回滚纪律不变。
>
> **✅ 2026-09-19 用户拍板：B-55 / B-58 / B-85 三条全部带上。** 此前本段写的「刻意不带 B-58
> （#1611）与 B-85（#1618）」**整条作废** —— 那句话的前提是「它们在 `233791e5` 之后才合入、
> 没发过测试环境」，而用户先后两次拍板（「58 和 55 带上」、「85 也带上」）把它们都收进本班。
> 钉子纪律本身不变：**先发一次测试环境验过，再重钉**。
>
> **🔲 本单还欠一次重钉（两个钉子都欠）。** §0 已经把 B-55 三条写进装载、Step A0 / Step A 的
> 动作也写好了，但**钉子本身还没动**：
>
> | 钉子 | 现值 | 应该变成 | 还差什么 |
> |---|---|---|---|
> | 应用镜像 | `233791e5` | 覆盖 `233791e5..main` 全部 10 个提交的新 sha | 发一次测试环境 |
> | 沙箱镜像 | `621249f6` | `ef9e157a`（砍掉 ffmpeg/npm 的那次重烤） | 同上，且要等 CI 把镜像推上 ACR |
>
> `233791e5..main` 的 10 个提交（2026-09-19 清点）：
>
> | 提交 | 是什么 | 进本班的理由 |
> |---|---|---|
> | `604e4e42` #1610 | test newTag 记录 + 金丝雀带上 B-55 探针 + 新立 B-85 | 记账 |
> | `36e675a8` #1611 | **B-58** 孤儿重收丢输入 | 用户拍板 |
> | `e0d5bbd1` #1619 / `9066c191` #1621 / `1833c746` #1622 | 执行单重钉、ROADMAP 勘误、执行单补漏 | 纯文档 |
> | `87a7cb5a` #1620 | smoke 公网探针重试连接级失败 | 发布工具，发布当天会用到 |
> | `5ba246eb` #1618 | **B-85 ①②** 沙箱认领撞并发时平台自己等 | 用户拍板 |
> | `97762881` #1613 | dependabot：pypdf / matplotlib 补丁版 | 随镜像重烤一起上 |
> | `ef9e157a` #1623 / `4d0d1562` #1624 | **B-55** 镜像瘦身 + VPC endpoint | 用户拍板 |
>
> 重钉之后按 §0 顶上那条判据自检：**`grep -n '<旧 sha>' 这份文件` 必须零命中**。

> **⛔ #1597 那次重钉漏了正文**：表头改成了 `dfd4e6de`，Step B 的 `git checkout` 和另外 4 处
> 却仍停在 `42426d31`（落后两代）。这已经是同一形状的**第二次**（班车 1 的 B2 正文钉子漏改，#1566）。
> 本次一并补齐，改钉子的判据定死为：**`grep -n '<旧 sha>' 这份文件` 必须零命中**才算改完。

---

## 1. 前置（发布前一天做完）

- [ ] **本机接线还在**（只看存在与权限，不读内容）：

      ```sh
      ls -l ~/.kube/expert-work-prod.yaml ~/.kube/expert-work-prod-secrets.env ~/.kube/expert-work-prod-params.env
      ```

      三个文件都在、都是 `600`。

- [ ] **overlay 无占位符**：`grep -rn PROD_PLACEHOLDER infra/k8s/overlays/prod/` 无输出。
- [ ] **金丝雀还在**：班车 1 已 seed 过（`release-canary` 有会话行、工作区里有 `canary-check.txt`）。
      smoke 阶段 6 报 WARNING 而不是 PASS，就说明它没了，按
      [`production-release.md` §1.6.7](./production-release.md) 补种，**不要带着 WARNING 往下发**。
- [ ] **装载确认**：

      ```sh
      git fetch origin main
      TAG=<本版 tag>                                        # 见表头「本版 tag」
      git log --oneline 5775fbf3..$TAG | wc -l             # 与表头「区间提交数」对得上
      git diff --name-only 5775fbf3..$TAG | grep -i migrations/versions   # 期望**恰好两条**：
      #   packages/expert-work-persistence/migrations/versions/0156_thread_message_hidden.py
      #   packages/expert-work-persistence/migrations/versions/0157_thread_mirror_resweep.py
      # 多出别的迁移 = 装载和这份单子对不上，停下来查，别往下发
      ```

- [ ] **预拉三个 base 镜像**（ECR Public 按 IP 限流，一天能红六次；建镜像前先拉一遍，
      见 [`production-release.md`](./production-release.md) 的预拉脚本）。
- [ ] **测试环境 24h 复查已做**：确认**本版 tag 那一版**在测试环境跑满 24h 后
      `rls.would_fail_closed` 里 `workspace_janitor` / `memory_consolidator` 两个模块为 0
      （同 Step E 的判据）、没有新的异常告警。
- [ ] **窗口挑非高峰**：B-80 修复后发布**不再打断在跑的对话**，所以这条从"硬要求"降为
      "常识"——不必再为它专门约时间。仍建议避开对接方明确的高峰，理由只剩一条：
      滚动期间在跑的对话越多，收口越久（见 §2 第 2 条的实测数字）。
- [ ] **通知对接方**：只有一件事要说 ——**窗口内先别处理待审批**（§2 第 3 条）。
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
**A 在 B 之前**（金丝雀要在新沙箱镜像上验）、**B 之后立刻做 C**。

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
| `Pulling` 之后长时间没有 `Pulled` | 正常冷拉，VPC 实测 66s（公网 112s） | 等 |

### Step B — 发版（单段）

```sh
git fetch origin main
git checkout 233791e5
git log -1 --oneline            # 确认就是它

tools/deploy/release.sh prod    # 输入 'prod' 确认；或 --yes
```

- [ ] 确认 checkout 的是 `233791e5`
- [ ] 三个镜像建推成功（ECR Public 限流是已知形态 —— 失败先把三个 base 全拉一遍再重跑）
- [ ] migrate Job `condition met`，且日志里出现**两条** upgrade：`0155… -> 0156_thread_message_hidden`、`0156… -> 0157_thread_mirror_resweep`（本版不是空跑）
- [ ] 全部 Deployment rollout 完成
- [ ] **smoke 全绿，且阶段 6 金丝雀是 PASS 不是 WARNING**
- [ ] smoke 里的沙箱钉子检查是 `OK`（Step A 做过了；显示 `WARN 落后 N` 说明 Step A 漏了）
- [ ] overlay 的 newTag 改动先别提交 —— Step F 一起记

### Step C — 发后即时点检（rollout 完成后 10 分钟内）

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
kubectl -n expert-work get pods            # 无 CrashLoop、重启计数为 0
kubectl -n expert-work get deploy -o 'custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'
```

- [ ] 三个应用镜像都是 `233791e5`（admin-ui 是 `233791e5-prod`）
- [ ] 全 pod Running、零重启
- [ ] **留存 CronJob 已创建且参数正确**：

      ```sh
      kubectl -n expert-work get cronjob retention-cleanup \
        -o jsonpath='{.spec.schedule}{"  tz="}{.spec.timeZone}{"  suspend="}{.spec.suspend}{"\n"}'
      ```

      期望 `23 3 * * *  tz=Asia/Shanghai  suspend=false`。

- [ ] **对话可用**（金丝雀之外再看一眼真实流量）：控制台随便打开一段最近会话，能正常加载。

### Step D — 真栈验证（本版新功能，按需做，全部只用金丝雀）

> 对接方的 agent（`ai-health-plan` / `sop2-designer`）**不做实验**，只读观察。

- [ ] **B-66**：对 `release-canary` 的一轮做 `:regenerate`，确认这一轮**真的重新调用了模型**
      （用量不为 0）。提示词里带一个随机串，避开响应缓存。
- [ ] **B-67 / B-61（只读观察，可留到次日）**：对接方跑过一轮之后，在控制台看那一轮的系统提示词
      —— 变量位置应当是 `$EXPERT_WORK_INPUTS_DIR/...` 的本地路径，不再是原始签名链接。
- [ ] **审批（可选）**：rollout 全部完成之后再处理任何待审批；确认批准之后只执行审批单上那一条。

### Step E — 次日核对（09-25 早上）

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

- [ ] `chore(deploy): prod newTag 233791e5` 记录 PR，正文写上：上一版 `5775fbf3`、本版装载、
      沙箱钉子 `e8aac104 → 621249f6`、留存 CronJob 首次接入、回滚命令。
- [ ] ROADMAP 班车 2 行销案；本执行单补 §6 执行记录。

---

## 4. 回滚

```sh
tools/deploy/rollback.sh prod 5775fbf3
```

一个档位就够：本版**有一条迁移但不用退**，没有数据搬迁，旧镜像直接能跑在当前 schema 上。

🚫 **不要 `alembic downgrade`。** `0156` 是纯加列（`thread_message.hidden`，带默认 `false`）：
旧版本的 ORM 不映射这一列，既不 SELECT 也不 INSERT，`server_default` 兜住写入 —— 多这一列
对旧代码完全无害。反过来 downgrade 会把新版本已经写进去的 `hidden` 全抹掉，而回滚窗口里
随时可能再滚回来，抹掉的值只能等 sweep 重扫才补得回来（还只补有新活动的线程）。

⚠️ **回滚前必须先清三样东西**，否则旧代码会起不来或行为不对：

| 东西 | 为什么 | 回滚前怎么办 |
|---|---|---|
| Agent 配置里的 `arg_bindings`（工具参数绑定） | 旧版 `MCPToolSpec` 是 `extra="forbid"`，读到带 `arg_bindings` 的配置**校验失败 → Agent 起不来** | 回滚窗口内**别在生产配绑定**；配了就先在配置页删掉再回滚 |
| 变量上的 `render: raw` | 同上，旧版 `PromptVariableSpec` 也是 `extra="forbid"` | 同上 |
| 留存 CronJob | `apply -k` 旧 overlay **不会**删掉已创建的对象 | 要一并退掉就显式删：`kubectl -n expert-work delete cronjob retention-cleanup` |

沙箱钉子单独回：`git checkout 5775fbf3 -- infra/k8s/sandbox/sandboxset.yaml && kubectl apply -f infra/k8s/sandbox/sandboxset.yaml`
（回到 `e8aac104`）。

其它回滚事实：

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

## 6. 执行记录（2026-09-24，发完当晚写）

| 项 | 实况 |
|---|---|
| 窗口 | `___ : ___` ~ `___ : ___` |
| 发布前在跑 / 排队 / 待审批 | `___` |
| Step A 沙箱钉子 | 发前 `________` → 发后 `________` |
| Step B smoke / 金丝雀 | `________` |
| migrate Job | 期望跑两条（`0156` + `0157`），实况 `________` |
| CronJob 创建 | `________` |
| 次日首跑删除计数 | `________` |
| 与执行单不符之处 | `________` |
