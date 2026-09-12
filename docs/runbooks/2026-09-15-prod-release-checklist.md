# 2026-09-15 生产发布执行单（班车 1）

> 一次性文档，发完归档。通用流程在
> [`production-release.md`](./production-release.md)，**这份只列那一份不覆盖的东西**。

| | |
|---|---|
| 上一版 tag（回滚用） | **`ad79ba28`** |
| 本版 tag | `___________`（发布时填，= 发布那刻 main 的 short HEAD） |
| 区间提交数 | 93（`git log --oneline ad79ba28..<本版>`） |
| 执行人 / 开始时间 | `___________` |

---

## 为什么需要这份单子

`release.sh prod` 只做四件事：**建推三个镜像 → 钉 overlay newTag → `apply -k`（含
migrate Job = `alembic upgrade head`）→ rollout + smoke**。

它**不做**的事每一版都不一样，而那些事恰恰是漏了最疼的。本版清点结果见 §2 ——
清点方法写在 [`production-release.md` §2](./production-release.md) 顶部，下次照做。

---

## 1. 前置（建议发布前一天做完）

- [ ] **本机接线还在**：`~/.kube/expert-work-prod.yaml`、
      `~/.kube/expert-work-prod-secrets.env`、`~/.kube/expert-work-prod-params.env`
      三个文件都在且权限 600
- [ ] **金丝雀已 seed**：`release-canary` agent + `canary:release` 用户存在于生产。
      没有的话按 [`production-release.md` §1.6.7](./production-release.md) 补 ——
      **未 seed 时 smoke 阶段 6 会 WARNING 跳过**，等于这次发布没有真栈闸门
- [ ] **确认本版装载**：`git log --oneline ad79ba28..HEAD`，与 ROADMAP 班车 1 条目对齐
- [ ] **依赖 PR 的取舍已拍板**：#1520 / #1485 / #1519 / #1484 合不合进本版？
      （建议：本版**不带**，B-50 搬迁是这次的主要风险，混进依赖升级会让出问题时
      分不清归因。依赖单独发一版。）
- [ ] **#1486 weasyprint 的 SSRF 告警（`#137 medium`）已单独处置或明确顺延** ——
      它在沙箱镜像里，与三个应用镜像正交，但别让它永远挂着
- [ ] **测试环境已泡过同一版**，且 B-50 搬迁在测试环境跑通过一次

---

## 2. 本版 `release.sh` 不覆盖的动作（清点结果）

| # | 动作 | `release.sh` 为什么不做 | 漏了会怎样 |
|---|---|---|---|
| A | **沙箱镜像钉子 `63a3109f → e8aac104`** | `infra/k8s/sandbox/sandboxset.yaml` 在 `default` namespace、**手工 apply、不进 kustomize** | 沙箱继续跑 34 天前的镜像；PR3b 改的 `infra/sandbox-image/runner.py`（exec cwd 按 agent）不在里面 |
| B | **B-50 工作区存量搬迁** | 它动的是 NAS 上的文件，不是 k8s 对象 | 控制台工作区浏览面与对外两个 workspace 端点**返回空**（文件还在扁平根，新代码按 agent 目录找） |
| C | **P-1 重新生成的真栈验证** | smoke 不覆盖 | `0153` 的三列上没跑过真流量 |
| D | **`chore(deploy)` 记录 PR** | newTag 改动脚本**故意留在工作区不提交** | 回滚时查不到上一版 tag |

**不在本版范围**（写出来是为了别误做）：

- **留存 CronJob 不进生产** —— 它只挂在 test overlay（`infra/k8s/overlays/test/kustomization.yaml:13`），
  按排期是班车 2（09-22，test 泡一周无事故才加）
- **无新增 secret / 必配 env** —— 区间里两条新 `secretKeyRef`
  （`EXPERT_WORK_CRED_PROXY_REDIS_URL` 复用既有 quota Redis 键且可选；
  `EXPERT_WORK_RETENTION_DB_DSN` 只在 test overlay 的 job 上）都不需要新建
- **迁移 `0152` / `0153` / `0154` 自动跑** —— 在 `apply -k` 的 migrate Job 里

---

## 3. 执行顺序

> 顺序不是建议，是约束：**A 在发版前**（金丝雀要用新沙箱镜像验），
> **B 在金丝雀绿之后立刻**（见 §4 的回滚窗口）。

### Step A — 沙箱镜像钉子

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml

# 发前值（留档）：
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'

kubectl apply -f infra/k8s/sandbox/sandboxset.yaml

# 发后值应为 …/sandbox:e8aac104
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'
```

- [ ] 已 apply，镜像 tag 变成 `e8aac104`
- [ ] 池 pod 重建完成（`kubectl -n default get pods | grep sandbox`，冷拉约 110s）

⚠️ apply 会重建温池 pod，**在途沙箱会被打断**。放在发布窗口内做。

### Step B — 发版

```sh
tools/deploy/release.sh prod          # 输入 'prod' 确认；或 --yes
```

- [ ] 三个镜像建推成功（ECR Public 抽风是已知形态 —— 失败先
      `docker pull public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine` 再重跑）
- [ ] migrate Job `condition met`（= `0152`/`0153`/`0154` 跑过）
- [ ] 全部 Deployment rollout 完成
- [ ] **smoke 全绿，且阶段 6 金丝雀是 PASS 不是 WARNING**

### Step C — B-50 工作区存量搬迁（金丝雀绿之后**立刻**）

完整步骤在 [`workspace-agent-scoping-migration.md`](./workspace-agent-scoping-migration.md)，
这里只列骨架与闸门：

```sh
NS=expert-work
POD=$(kubectl -n $NS get pod -l app.kubernetes.io/name=control-plane -o name | head -1)

# 1) 空跑，逐个用户看报告
kubectl -n $NS exec -i "$POD" -- python3 -m tools.persistence.migrate_workspace_agent_scoping \
  --root /mnt/workspaces --tenant "$TENANT" --user "$USER"

# 2) 报告看过了再真搬
kubectl -n $NS exec -i "$POD" -- python3 -m tools.persistence.migrate_workspace_agent_scoping \
  --root /mnt/workspaces --tenant "$TENANT" --user "$USER" --apply
```

- [ ] **每个用户先 dry-run**，报告里 `moved / to_shared / untouched` 三个数**互不重叠**
      且**合计等于文件总数**
- [ ] `conflicts` 里点名的文件逐个看过（目的地已存在 → 源进 `shared/`，这是单 agent
      用户的常态，不是异常）
- [ ] **每次 `--apply` 的输出留档** —— 没有自动回退，回退要靠报告里的 `moves` 反向 `mv`
- [ ] 按 runbook §Step 3 的 5+2 条判据验收
- [ ] 搬完复看控制台工作区浏览面：按 agent 分组、`shared/` 单独一组

> 脚本自己会断言 `0154` 已跑过并回填（`assert_backfill_ran`）。它没跑时，本可归属的
> 产物会被**静默**扫进 `shared/` —— 不报错、文件数照样守恒，只有人工比对才看得出来。

### Step D — P-1 真栈验证

用探针 user 对 `release-canary` 跑一次
`POST …/runs/{run_id}:regenerate`（queue 模式），然后：

- [ ] `/messages` 里旧轮每条带 `superseded_by`
- [ ] `GET /v1/runs/{id}` 里两轮 `tokens` 都在（明确**不**回滚计费）

### Step E — 记录

- [ ] `chore(deploy): prod newTag <本版>` PR，正文写上**上一版 tag `ad79ba28`**
- [ ] ROADMAP 班车 1 销案

---

## 4. 回滚

```sh
tools/deploy/rollback.sh prod ad79ba28
```

⚠️ **回滚的干净窗口在 Step C 之前。**

`ad79ba28` 完全没有 agent 维度（它早于 B-50 PR1）。搬迁跑完之后回滚，老代码按扁平
用户根去读，而文件已经在 `agents/<key>/` 下面了 —— **用户工作区看着就是空的**。
要在搬迁之后回滚，必须先按留档的 `moves` 把文件反向 `mv` 回去。

所以 Step B（金丝雀绿）和 Step C（搬迁）之间那段，是这次发布**唯一**能低成本回滚的
时间点。金丝雀不绿就不要往下走。

DB 侧不用担心：`0152`/`0153`/`0154` 都是 expand-only（加列/加约束，向后兼容一版）。
