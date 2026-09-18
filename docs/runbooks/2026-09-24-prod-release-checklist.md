# 2026-09-24 生产发布执行单（班车 2）

> 一次性文档，发完归档。通用流程在 [`production-release.md`](./production-release.md)，
> **这份只列那一份不覆盖的东西**。上一班的单子在
> [`2026-09-14-prod-release-checklist.md`](./2026-09-14-prod-release-checklist.md)（已归档）。

| | |
|---|---|
| 发布日 | **2026-09-24（周四）**，用户 2026-09-17 拍板（原 09-22） |
| 上一版 tag（回滚用） | **`5775fbf3`**（班车 1 的 B2，2026-09-16 18:51 上线） |
| 本版 tag | **`dfd4e6de`** —— 测试环境 2026-09-18 实际发过的那一版 |
| 区间提交数 | **40**（`git log --oneline 5775fbf3..dfd4e6de`） |
| 数据库迁移 | **一条：`0156_thread_message_hidden`**（expand-only，`thread_message` 加 `hidden` 一列带默认 `false`）。migrate Job 自动跑，不需要额外动作 |
| 段数 | **单段**。有迁移但不是三段式：`0156` 是纯加列、没有数据搬迁、没有 expand/contract 关系，新旧两版代码都能在这张表上正常跑 |
| 回滚纪律 | **只回镜像，不要 `alembic downgrade`。** 多一列对旧版本无害（旧 ORM 不映射它，既不 SELECT 也不 INSERT，`server_default` 兜住）；downgrade 会把新版本写进去的 `hidden` 全抹掉，而回滚窗口里随时可能再滚回来 |
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
- **B-73 四条小尾巴**（#1593 + #1595，2026-09-18 追加）：压缩不再把「本轮输入」段摘要掉、
  隐藏段的文件名前缀歧义指向清单、**平台脚手架行不进控制台内容搜索**（带迁移 `0156`）、
  审计视图里这些行渲染成折叠的「平台自动生成」块。

> **钉子纪律**：本单钉 `dfd4e6de`。发布日若要带上它之后的**任何代码或 admin-ui 文档站改动**，
> 必须**先发一次测试环境验过**再改钉子 —— 别在发布当天直接发 main HEAD。
>
> **改期记录**：2026-09-18 先钉 `498492d5`（#1591），当天下午用户拍板把 B-56 / B-72 / B-65 /
> B-73 四条一起带上，重钉 `dfd4e6de`（本单）。形态从「无迁移」变成「一条 expand-only 迁移」，
> 表头三行与上面的迁移预检随之改过。

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
      git diff --name-only 5775fbf3..$TAG | grep -i alembic   # 期望**恰好一条**：
      #   packages/expert-work-persistence/migrations/versions/0156_thread_message_hidden.py
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

---

## 3. 执行顺序

顺序是约束：**A 在 B 之前**（金丝雀要在新沙箱镜像上验），**B 之后立刻做 C**。

### Step A — 沙箱镜像钉子（`e8aac104` → `621249f6`）

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml

# 发前值（留档）。期望 …/sandbox:e8aac104；对不上说明中间有人动过，停下来先弄清楚
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'

kubectl apply -f infra/k8s/sandbox/sandboxset.yaml

# 发后值应为 …/sandbox:621249f6
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'
```

- [ ] 已 apply，tag 变成 `621249f6`
- [ ] 池 pod 重建完成（`kubectl -n default get pods | grep sandbox`，冷拉约 110s）

⚠️ apply 会重建温池 pod，**在途沙箱会被打断** —— 所以放在窗口内、B 之前。

### Step B — 发版（单段）

```sh
git fetch origin main
git checkout 42426d31
git log -1 --oneline            # 确认就是它

tools/deploy/release.sh prod    # 输入 'prod' 确认；或 --yes
```

- [ ] 确认 checkout 的是 `42426d31`
- [ ] 三个镜像建推成功（ECR Public 限流是已知形态 —— 失败先把三个 base 全拉一遍再重跑）
- [ ] migrate Job `condition met`（**本版应当空跑**：没有新迁移）
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

- [ ] 三个应用镜像都是 `42426d31`（admin-ui 是 `42426d31-prod`）
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

- [ ] `chore(deploy): prod newTag 42426d31` 记录 PR，正文写上：上一版 `5775fbf3`、本版装载、
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
| migrate Job | 期望空跑，实况 `________` |
| CronJob 创建 | `________` |
| 次日首跑删除计数 | `________` |
| 与执行单不符之处 | `________` |
