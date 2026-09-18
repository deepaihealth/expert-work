# 2026-09-24 生产发布执行单（班车 2）

> 一次性文档，发完归档。通用流程在 [`production-release.md`](./production-release.md)，
> **这份只列那一份不覆盖的东西**。上一班的单子在
> [`2026-09-14-prod-release-checklist.md`](./2026-09-14-prod-release-checklist.md)（已归档）。

| | |
|---|---|
| 发布日 | **2026-09-24（周四）**，用户 2026-09-17 拍板（原 09-22） |
| 上一版 tag（回滚用） | **`5775fbf3`**（班车 1 的 B2，2026-09-16 18:51 上线） |
| 本版 tag | **`42426d31`** —— 测试环境 2026-09-17 实际发过并验收通过的那一版 |
| 区间提交数 | **28**（`git log --oneline 5775fbf3..42426d31`） |
| 数据库迁移 | **无**（区间内没有 alembic 文件改动；migrate Job 会空跑） |
| 段数 | **单段**（不是三段式：没有迁移、没有数据搬迁、没有 expand/contract 关系） |
| 沙箱镜像钉子 | `e8aac104` → **`621249f6`**（Step A） |
| 新增集群对象 | **留存清理 CronJob `retention-cleanup`**（首次进 prod overlay，`apply -k` 会创建） |
| 执行人 / 开始时间 | `___________` |

---

## 0. 本版装载（28 个提交）

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

> **钉子纪律**：`42426d31` 之后 main 上只有纯排期表 / 设计文档的提交（#1584），不进镜像。
> 发布日若要带上 `42426d31` 之后的**任何代码或 admin-ui 文档站改动**，必须**先发一次测试环境
> 验过**再改钉子 —— 别在发布当天直接发 main HEAD。

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
      git log --oneline 5775fbf3..42426d31 | wc -l          # 期望 28
      git diff --name-only 5775fbf3..42426d31 | grep -i alembic   # 期望无输出（无迁移）
      ```

- [ ] **预拉三个 base 镜像**（ECR Public 按 IP 限流，一天能红六次；建镜像前先拉一遍，
      见 [`production-release.md`](./production-release.md) 的预拉脚本）。
- [ ] **测试环境 24h 复查已做**：测试环境 09-17 23:08 发的 `42426d31`，发布前确认它跑满 24h 后
      `rls.would_fail_closed` 里这两个模块为 0（同 Step E 的判据）、没有新的异常告警。
- [ ] **窗口定在低峰**（见 §2 的 B-80）：与对接方约好时间段，或选北京时间 22:00 之后。
- [ ] **通知对接方**：窗口内正在跑的对话会被打断、待审批会话在窗口内先别处理。

---

## 2. `release.sh prod` 不覆盖的动作（本版清点结果）

`release.sh prod` 只做四件事：建推三个镜像 → 钉 overlay newTag → `apply -k`（含 migrate Job）
→ rollout + smoke。本版它**不做**的：

1. **沙箱镜像钉子**（`infra/k8s/sandbox/sandboxset.yaml`，`default` 命名空间）→ Step A。
2. **发布会打断正在跑的对话（B-80）** —— 本版**不修代码，靠窗口规避**：
   - 事实：pod 被删（哪怕 `--grace-period=0 --force`）进程仍走优雅关机，事件循环取消在跑的 run，
     `run_agent` 把它收成 `interrupted`（`error` 为空）；**不会**被另一副本接管
     （接管只在进程被硬杀 / 节点丢失时发生）。
   - 动作：发版前查在跑数量，为 0 或很小才开始。**这条查询由你（用户）在生产上跑**：

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
3. **审批在窗口内的两条一次性影响**（B-76 / #1582 带来的，只在滚动替换的那几分钟）：
   - **发布顺序**：新版控制面写下的裁定如果由还没替换到的旧副本执行，旧副本按老逻辑**放行整轮调用**。
     → 窗口内尽量不处理审批；rollout 全部完成后再处理。
   - **窗口前就挂着的旧审批**：发布后批准它们，可能这一步什么都不执行（平台无法证明审批单对应哪一个调用，
     一律不放行），Agent 会重新发起、再审批一次。对外表现是「批了但没动作」，不是故障。
4. **留存清理 CronJob 首跑**：`apply -k` 会创建它，但**第一次真正删数据是发布次日 03:23（北京时间）**
   → Step E 次日核对。
5. **回滚前置清理**（§4）：生产上一旦配了 `arg_bindings` 或 `render:`，回滚会让 Agent 起不来。

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

一个档位就够：本版没有迁移、没有数据搬迁，旧镜像直接能跑在当前 schema 上。

⚠️ **回滚前必须先清三样东西**，否则旧代码会起不来或行为不对：

| 东西 | 为什么 | 回滚前怎么办 |
|---|---|---|
| Agent 配置里的 `arg_bindings`（工具参数绑定） | 旧版 `MCPToolSpec` 是 `extra="forbid"`，读到带 `arg_bindings` 的配置**校验失败 → Agent 起不来** | 回滚窗口内**别在生产配绑定**；配了就先在配置页删掉再回滚 |
| 变量上的 `render: raw` | 同上，旧版 `PromptVariableSpec` 也是 `extra="forbid"` | 同上 |
| 留存 CronJob | `apply -k` 旧 overlay **不会**删掉已创建的对象 | 要一并退掉就显式删：`kubectl -n expert-work delete cronjob retention-cleanup` |

沙箱钉子单独回：`git checkout 5775fbf3 -- infra/k8s/sandbox/sandboxset.yaml && kubectl apply -f infra/k8s/sandbox/sandboxset.yaml`
（回到 `e8aac104`）。

其它回滚事实：

- 被打断的对话（B-80）回滚也救不回来，只能让用户重发。
- 审批：回滚后旧代码恢复「批准放行整轮」的老行为（那是被本版修掉的漏洞），
  所以**回滚窗口内同样不要处理审批**。
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
