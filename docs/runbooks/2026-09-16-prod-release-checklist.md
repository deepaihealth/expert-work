# 2026-09-16 生产发布执行单（班车 1）

> 一次性文档，发完归档。通用流程在
> [`production-release.md`](./production-release.md)，**这份只列那一份不覆盖的东西**。

| | |
|---|---|
| 上一版 tag（回滚用） | **`ad79ba28`** |
| 本版 tag | **B1 = `ca225258`**(expand,已定) / **B2 = `___________`**(contract,发布时填 = main short HEAD) |
| 区间提交数 | 93+（`git log --oneline ad79ba28..<本版>`） |
| 生产实况已勘察 | **2026-09-13，只读**：1 个租户 / 8 个用户目录 / 152 文件 / 56 条产物版本行 / 唯一的多 agent 用户一个。逐个用户的预期结果写死在 §3 Step C.1 —— **发布当晚不需要临场查询或判断** |
| 执行人 / 开始时间 | `___________` |

---

## 为什么需要这份单子

`release.sh prod` 只做四件事：**建推三个镜像 → 钉 overlay newTag → `apply -k`（含
migrate Job = `alembic upgrade head`）→ rollout + smoke**。

它**不做**的事每一版都不一样，而那些事恰恰是漏了最疼的。本版清点结果见 §2 ——
清点方法在 [`production-release.md` §2.0](./production-release.md),下次照做。

---

## 1. 前置（建议发布前一天做完）

- [ ] **本机接线还在**：`~/.kube/expert-work-prod.yaml`、
      `~/.kube/expert-work-prod-secrets.env`、`~/.kube/expert-work-prod-params.env`
      三个文件都在且权限 600
- [x] **金丝雀已 seed** —— 2026-09-13 在生产只读确认：`release-canary` 有会话行，
      它的工作区里 `canary-check.txt` 在。
      （**未 seed 时 smoke 阶段 6 会 WARNING 跳过**，等于这次发布没有真栈闸门；
      真要补按 [`production-release.md` §1.6.7](./production-release.md)）
- [ ] **确认本版装载**：`git log --oneline ad79ba28..HEAD`，与 ROADMAP 班车 1 条目对齐
- [ ] **依赖 PR 的取舍已拍板**：#1520 / #1485 / #1519 / #1484 合不合进本版？
      （建议：本版**不带**，B-50 搬迁是这次的主要风险，混进依赖升级会让出问题时
      分不清归因。依赖单独发一版。）
- [ ] **#1486 weasyprint 的 SSRF 告警（`#137 medium`）已单独处置或明确顺延** ——
      它在沙箱镜像里，与三个应用镜像正交，但别让它永远挂着
- [ ] **测试环境已泡过同一版**，且 B-50 搬迁在测试环境跑通过一次
- [ ] **发布前一天先把三个镜像建一遍**（缓存预热 + 腾盘位）。2026-09-12/13 发测试
      环境连炸三次，三个不同原因，全部是本机环境而非代码：

      | 症状 | 处置 |
      |---|---|
      | `Head .../nginx-unprivileged/manifests: EOF` | ECR Public 按 IP 限流。先 `docker pull` 预拉 base，再重跑 |
      | `copy file range failed: no space left on device` | 本机 Docker 盘满。`docker builder prune -af` |
      | apt 拉到 `1021 B/s` 后 `Connection failed` | 网络瞬时塌陷。先探源的速度，通了再重跑 |

      后两条有因果：prune 清掉构建缓存 → 下一跑必须重建 apt 层 → 正好撞上网络。
      提前一天建好镜像能一次性避开这三个。

---

## 2. 本版 `release.sh` 不覆盖的动作（清点结果）

| # | 动作 | `release.sh` 为什么不做 | 漏了会怎样 |
|---|---|---|---|
| A | **沙箱镜像钉子 `63a3109f → e8aac104`** | `infra/k8s/sandbox/sandboxset.yaml` 在 `default` namespace、**手工 apply、不进 kustomize** | 沙箱继续跑 34 天前的镜像；PR3b 改的 `infra/sandbox-image/runner.py`（exec cwd 按 agent）不在里面 |
| B | **B-50 工作区存量搬迁** | 它动的是 NAS 上的文件，不是 k8s 对象 | 控制台工作区浏览面与对外两个 workspace 端点**返回空**（文件还在扁平根，新代码按 agent 目录找） |
| C | **P-1 重新生成的真栈验证** | smoke 不覆盖 | `0153` 的三列上没跑过真流量 |
| D | **`chore(deploy)` 记录 PR** | newTag 改动脚本**故意留在工作区不提交** | 回滚时查不到上一版 tag |
| E | **第二次发版(contract,摘掉迁移期读回落)** | `release.sh` 一次只发一个版本;三段式是这次发布的形状,不是它的功能 | 回落留着 = **跨 agent 读洞**:搬完之后用户根上剩的恰恰是别人的历史文件,任何 agent 都读得到(测试环境已实证) |

**不在本版范围**（写出来是为了别误做）：

- **留存 CronJob 不进生产** —— 它只挂在 test overlay（`infra/k8s/overlays/test/kustomization.yaml:13`），
  按排期是班车 2（09-22，test 泡一周无事故才加）
- **无新增 secret / 必配 env** —— 区间里两条新 `secretKeyRef`
  （`EXPERT_WORK_CRED_PROXY_REDIS_URL` 复用既有 quota Redis 键且可选；
  `EXPERT_WORK_RETENTION_DB_DSN` 只在 test overlay 的 job 上）都不需要新建
- **迁移 `0152` / `0153` / `0154` 自动跑** —— 在 `apply -k` 的 migrate Job 里

---

## 3. 执行顺序

本次发布是 **expand → migrate → contract 三段式**，三段**在同一个窗口里连着做完**，
和测试环境 2026-09-13 走的**逐字相同**：

| 段 | 做什么 | 为什么必须分开 |
|---|---|---|
| **B1 expand** | 发 `ca225258`（带迁移期读回落） | 这一版**容忍旧扁平布局**：agent 在自己目录下读不到时回落用户根。搬迁还没跑，历史文件都还在用户根上 |
| **C migrate** | 跑存量搬迁 | 文件从用户根搬进 `agents/<key>/` 与 `shared/` |
| **B2 contract** | 发 main HEAD（摘掉回落，PR6） | 搬完之后回落反过来成了跨 agent 读洞 —— 用户根上剩下的恰恰是**别人的**历史文件 |

> **两段都是 main 上的提交**，可追溯。别把它读成「发了一半」——
> contract 是这次发布的一部分，不是留到下一班车的尾巴。
>
> **回滚因此多了一个档位**：B2 之后出问题，回滚到 `ca225258` 的镜像就把回落带回来了，
> **不用把文件搬回去**。见 §4。

> 顺序不是建议，是约束：**A 在 B1 之前**（金丝雀要用新沙箱镜像验），
> **C 在 B1 的金丝雀绿之后立刻**，**B2 在 C 验收全过之后**。

### Step A — 沙箱镜像钉子

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml

# 发前值（留档）。2026-09-13 只读实测就是 `…/sandbox:63a3109f`，`replicas=1`；
# 对不上说明这中间有人动过，停下来先弄清楚再 apply。
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'

kubectl apply -f infra/k8s/sandbox/sandboxset.yaml

# 发后值应为 …/sandbox:e8aac104
kubectl -n default get sandboxset expert-work-sandbox \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"  replicas="}{.spec.replicas}{"\n"}'
```

- [ ] 已 apply，镜像 tag 变成 `e8aac104`
- [ ] 池 pod 重建完成（`kubectl -n default get pods | grep sandbox`，冷拉约 110s）

⚠️ apply 会重建温池 pod，**在途沙箱会被打断**。放在发布窗口内做。

### Step B1 — 发版（expand：带迁移期读回落）

**发 `ca225258`，不是 main HEAD。** 这一版容忍旧扁平布局，是 Step C 跑不顺时的安全网。

```sh
git fetch origin main
git checkout ca225258            # expand 版本；main 上的提交，可追溯
git log -1 --oneline             # 确认就是它

tools/deploy/release.sh prod     # 输入 'prod' 确认；或 --yes
```

- [ ] 确认 checkout 的是 `ca225258`
- [ ] 三个镜像建推成功（ECR Public 抽风是已知形态 —— 失败先
      `docker pull public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine` 再重跑）
- [ ] migrate Job `condition met`（= `0152`/`0153`/`0154` 跑过）
- [ ] 全部 Deployment rollout 完成
- [ ] **smoke 全绿，且阶段 6 金丝雀是 PASS 不是 WARNING**
- [ ] overlay 的 newTag 改动**先别提交** —— Step B2 之后一起记（见 Step E）

### Step C — B-50 工作区存量搬迁（金丝雀绿之后**立刻**）

完整步骤与验收判据在
[`workspace-agent-scoping-migration.md`](./workspace-agent-scoping-migration.md)。
下面这一节是**照抄即可执行**的版本：生产的规模、用户清单、每个用户的预期结果
都已于 2026-09-13 在生产集群上**只读勘察过并写死在这里**，不需要临场查询或判断。
**任何一格对不上就停下来，不要继续。**

#### C.0 生产实况（2026-09-13 勘察，只读）

| | |
|---|---|
| 租户 | **1 个**：`b0f0d29b-62ce-4326-ae92-e1c18631c935` |
| NAS 根 | `/mnt/workspaces`（已确认挂载） |
| 用户目录 | **8 个**，共 **152 个文件** |
| 有会话行的用户 | 9 个（`502b89e5…` 有会话但**没有 NAS 目录**，会被跳过） |
| 产物版本行（带 `path_in_workspace`） | **56** |
| 多 agent 用户 | **只有 1 个**：`81066c49…` |

> ⚠️ 这些工作区属于**对接方的真实生产用户**（agent 是 `ai-health-plan` /
> `sop2-designer`）。搬迁只做同文件系统内的 `os.replace`，不读内容、不改权限位、
> **目的地已有文件时绝不覆盖**（源改投 `shared/` 并单独报出来）。

#### C.1 预期结果 —— 逐个用户写死

| 用户（前 8 位） | 文件 | 产物行 | agent | 预期 |
|---|---|---|---|---|
| `81066c49` | 51 | 32 | `sop2-designer` + `ai-health-plan` | **唯一会产生 `shared/` 的用户** |
| `4f85f3b0` | 58 | 6 | `sop2-designer` | 全树 → `agents/sop2-designer-c97db277/`，`shared 0` |
| `1fef0d73` | 20 | 3 | `sop2-designer` | 同上 |
| `91bdbc51` | 7 | 4 | `ai-health-plan` | 全树 → `agents/ai-health-plan-30817804/`，`shared 0` |
| `5e949c8f` | 6 | 5 | `ai-health-plan` | 同上 |
| `52d0f26f` | 5 | 2 | `ai-health-plan` | 同上 |
| `05d882f5` | 4 | 2 | `ai-health-plan` | 同上 |
| `01d73931` | 1 | 2 | `release-canary` | 全树 → `agents/release-canary-fd420deb/`（金丝雀自己） |
| `502b89e5` | — | 0 | `ai-health-plan` | **无 NAS 目录，跳过**（脚本会报「无事可做」） |
| **合计** | **152** | **56** | | |

**判读规则**：
- 单 agent 用户的 `shared` 必须是 **0**。非 0 → 停下来看报告里点名的文件。
- `81066c49` 的 `shared` 会 > 0（根级共写文件 `MEMORY.md`/`PLAN.md`/`TODO.md`
  + run 已清理的孤儿 `.tool_results/`），这是**设计要的形态**。
- `artifact rows updated` **会 ≥** 空跑报的 path 条数（一个 path 挂多个版本行）。
  只有 `⚠️ N 条更新在库里一行都没命中` 才是红旗。

#### C.2 空跑（先全部跑一遍，看完再动手）

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
NS=expert-work
TENANT=b0f0d29b-62ce-4326-ae92-e1c18631c935
POD=$(kubectl -n $NS get pod -l app.kubernetes.io/name=control-plane -o name | head -1)
echo "POD=$POD"   # 空的话停 —— 标签是 app.kubernetes.io/name，不是 app

USERS="81066c49-0f8e-4dc2-9acd-4c57e3be5973
4f85f3b0-2f85-49a5-850f-daaec7b329a2
1fef0d73-af6f-4e84-9ed8-fbdfad266837
91bdbc51-3c23-423d-8031-7207dcdfa5d3
5e949c8f-9834-4adf-b42d-4aaad364125c
52d0f26f-535e-4d68-a159-9cf2cb02f86a
05d882f5-243b-4b42-9b86-db9a4384ba15
01d73931-55d0-4412-978e-e0a4f4dcf388"

for U in $USERS; do
  echo "=== $U"
  kubectl -n $NS exec -i "$POD" -- python3 -m tools.persistence.migrate_workspace_agent_scoping \
    --root /mnt/workspaces --tenant "$TENANT" --user "$U"
done 2>&1 | tee ~/b50-prod-dryrun.txt
```

- [ ] 八个用户的 `moved + to shared + untouched` 各自等于 C.1 表里的文件数
- [ ] 单 agent 的七个 `to shared` 全是 **0**
- [ ] `81066c49` 的 `shared` 清单看过（应是根级共写文件 + 孤儿 `.tool_results/`）
- [ ] 报告**留档**（`~/b50-prod-dryrun.txt`）—— 没有自动回退，回退靠这份 `moves`

#### C.3 真搬

```sh
for U in $USERS; do
  echo "=== $U"
  kubectl -n $NS exec -i "$POD" -- python3 -m tools.persistence.migrate_workspace_agent_scoping \
    --root /mnt/workspaces --tenant "$TENANT" --user "$U" --apply
done 2>&1 | tee ~/b50-prod-apply.txt
```

- [ ] 每个用户的 `moved`/`to shared`/`untouched` 与 C.2 空跑**逐个一致**
- [ ] **一条 `⚠️` 都没有**
- [ ] 输出留档（`~/b50-prod-apply.txt`）
- [ ] **崩了就重跑同一条命令**（脚本幂等），别手工改数据

#### C.4 验收（照抄，四个数全部要对）

```sh
kubectl -n $NS exec -i "$POD" -- python3 - <<'EOF'
import os
from control_plane.settings import Settings
import psycopg
R="/mnt/workspaces"
tot=agents=shared=0
for t in os.listdir(R):
    tp=os.path.join(R,t)
    if not os.path.isdir(tp): continue
    for u in os.listdir(tp):
        up=os.path.join(tp,u)
        if not os.path.isdir(up) or u.startswith("."): continue
        tot+=sum(len(fs) for _,_,fs in os.walk(up))
        for name in ("agents","shared"):
            d=os.path.join(up,name)
            if os.path.isdir(d):
                n=sum(len(fs) for _,_,fs in os.walk(d))
                if name=="agents": agents+=n
                else: shared+=n
        extra=[e for e in os.listdir(up) if e not in {"agents","shared","skills"}]
        if extra: print(f"  !! {u[:8]} 顶层还有 {extra[:4]}")
print(f"① 文件总数 {tot}          (期望 152)")
print(f"   agents/ {agents} + shared/ {shared} = {agents+shared}")
with psycopg.connect(Settings().db_dsn.replace("+asyncpg","")) as c:
    cur=c.cursor()
    cur.execute("""SELECT a.tenant_id::text,a.user_id::text,av.path_in_workspace
                     FROM artifact_version av JOIN artifact a ON a.id=av.artifact_id
                    WHERE av.path_in_workspace IS NOT NULL AND av.path_in_workspace<>''""")
    rows=cur.fetchall()
stale=[r for r in rows if not r[2].startswith(("agents/","shared/"))]
bad=[r for r in rows if r[2].startswith(("agents/","shared/"))
     and not os.path.exists(f"{R}/{r[0]}/{r[1]}/{r[2]}")]
print(f"② 产物版本行 {len(rows)}   (期望 56)")
print(f"③ 仍是扁平旧路径 {len(stale)}   (期望 0)")
print(f"④ 新路径但文件不在 {len(bad)}   (期望 0)")
EOF
```

- [ ] ① `152`，且 `agents/ + shared/` 合计也是 `152`
- [ ] ② `56`
- [ ] ③ **`0`**
- [ ] ④ **`0`** ← 这条排除「改对了格式但指错了地方」
- [ ] 没有 `!!` 行（顶层只剩 `agents/` / `shared/`）

#### C.5 真隔离（这才是本次改动的目的本身）

控制台用 `sop2-designer` 起一轮，让它 `list_dir(".")`：

- [ ] **看不到** `ai-health-plan` 的目录
- [ ] `read_file("shared:MEMORY.md")` 读得到

> 测试环境同一条已实证:`pf-probe` 的 `list_dir(".")` 只返回 `uploads/`,
> 同一用户下另一个 agent 的 24 个文件零泄露。

### Step B2 — 发版（contract：摘掉迁移期读回落，PR6）

**闸门：Step C 的验收四个数全对之后才做。** 搬迁没跑完就摘回落 = agent 读不到
自己目录里的历史文件，而那时唯一的缓解手段是再发一次版把回落加回去。

```sh
git checkout main
git log -1 --oneline             # 记下来，这是「本版 tag」

tools/deploy/release.sh prod
```

- [ ] Step C 的 C.4 四个数全对
- [ ] 三个镜像建推成功
- [ ] 全部 Deployment rollout 完成
- [ ] **smoke 全绿 + 金丝雀 PASS**

**复验回落真的摘了**（与测试环境 2026-09-13 做的 A/B 同一套）：

```sh
# 在金丝雀用户的**用户根**上放一个它 agent 目录里没有的文件
NS=expert-work
POD=$(kubectl -n $NS get pod -l app.kubernetes.io/name=control-plane -o name | head -1)
kubectl -n $NS exec -i "$POD" -- python3 - <<'EOF'
import os
R="/mnt/workspaces/b0f0d29b-62ce-4326-ae92-e1c18631c935/01d73931-55d0-4412-978e-e0a4f4dcf388"
p=os.path.join(R,"legacy-probe.txt")
open(p,"w").write("PROD_LEGACY_PROBE\n"); os.chmod(p,0o600)
print("放置:", p, "| agent 目录里有同名吗:",
      os.path.exists(os.path.join(R,"agents","release-canary-fd420deb","legacy-probe.txt")))
EOF
```

然后用 `release-canary` 跑一轮，让它 `read_file("legacy-probe.txt")`：

- [ ] **读不到**（`read_file failed: not_found`）← 回落确实摘了
- [ ] **跑完把探针文件删掉**（`os.remove` 同一路径）

> 测试环境的对照:**A(带回落)读到了那个文件**,**B(PR6)not_found**。
> 也就是说这一步同时证明了两件事 —— 回落摘干净了,以及它摘掉之前**真的是个
> 跨 agent 读洞**,不是洁癖。

**测试环境实测形态（2026-09-13，36 个用户 1080 文件），拿来当「正常长什么样」的参照：**

- 单 agent 用户（56/65）走 `sole_agent_key` 捷径，整棵树进自己的 agent 目录，
  `shared 0`。看到 `shared` 非 0 的单 agent 用户要看一眼。
- 多 agent 用户的 `shared/` 比例**可以很高** —— 实测一个用户 111/180 = 62%。
  构成是「根级共写文件」（`MEMORY.md`/`PLAN.md`/`style/`/`客户案例/`）+
  「run 已被清理的孤儿 `.tool_results/<run_id>/`」。**这是设计要的形态，不是异常。**
  判法：拿 `.tool_results` 进 shared 的那批，看它们的 `<run_id>` 还在不在
  `agent_run` 表里 —— 全都不在就是真孤儿。
- `artifact rows updated` **会大于**空跑报的 path 条数。一个 path 挂着同一产物的
  多个版本行（实测金丝雀那个 `canary-check.txt` 一个路径 **27 个版本行**）。
  只有 `⚠️ N 条更新在库里一行都没命中` 才是红旗。
- `{tenant}/.deleted/<user>/` 不会被搬（软删归档，按 `{tenant}/{user}` 寻址碰不到），
  这是对的。

### Step D — P-1 真栈验证

用探针 user 对 `release-canary` 跑一次
`POST …/runs/{run_id}:regenerate`（queue 模式），然后：

- [ ] `/messages` 里旧轮每条带 `superseded_by`
- [ ] `GET /v1/runs/{id}` 里两轮 `tokens` 都在（明确**不**回滚计费）

> queue 模式偶发 `EmptyInputError: Received no input for __start__`（B-58，
> 2026-09-13 测试环境撞到一次）。**重跑同一请求即可**；别当成 P-1 的问题去查。

### Step E — 记录

- [ ] `chore(deploy): prod newTag <B2 的 tag>` PR，正文写上**上一版 tag `ad79ba28`**，
      并注明本次是 **B1 `ca225258` → 搬迁 → B2 `<tag>`** 三段
- [ ] ROADMAP 班车 1 销案

---

## 4. 回滚

三段式发布对应**三个档位**，按出问题的时点选，别一律往最深处退：

| 出问题的时点 | 回到 | 代价 |
|---|---|---|
| **B1 之后、C 之前** | `rollback.sh prod ad79ba28` | 干净。文件还在扁平根，老代码本来就那么读 |
| **C 之后、B2 之前** | `rollback.sh prod ad79ba28` | ⚠️ **要先把文件搬回去**，见下 |
| **B2 之后** | `rollback.sh prod ca225258` | **最轻**。回落跟着镜像回来了，文件**不用动** |

```sh
tools/deploy/rollback.sh prod <上面那一列的 tag>
```

**第三档是三段式带来的**：B2 出问题不必退回 `ad79ba28`，退到 B1 的 `ca225258` 就行 ——
那一版认得 `agents/<key>/` 布局，又带着回落，是搬迁后最宽容的一版。

⚠️ **第二档才是真正要小心的那个。** `ad79ba28` 完全没有 agent 维度（它早于 B-50 PR1）。
搬迁跑完之后退到它，老代码按扁平用户根去读，而文件已经在 `agents/<key>/` 下面了
—— **用户工作区看着就是空的**。要退必须先按留档的 `~/b50-prod-apply.txt` 里的 `moves`
反向 `mv` 回去。

所以 **B1 金丝雀绿到 C 开跑之间**，是唯一能零成本退到 `ad79ba28` 的时点。
金丝雀不绿就不要往下走。

DB 侧不用担心：`0152`/`0153`/`0154` 都是 expand-only（加列/加约束，向后兼容一版）。
