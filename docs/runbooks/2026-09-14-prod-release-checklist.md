# 2026-09-14 生产发布执行单（班车 1）

> ✅ **发布时间已定：2026-09-16（用户 2026-09-15 拍板）。**
>
> 上一次改期（2026-09-14 16:30）的原因是 B-50 的写入侧漏洞：`exec_python` 写 `/workspace/x`
> 落用户根、产物登记指向 agent 目录、下载 404，对接方的 PPT 流程会在生产直接中招。
> **B-60 已经把它修掉并验完**：PR-B #1553 / PR-C #1554 全合，测试环境 2026-09-15 发布
> `621249f6`（SMOKE PASS + 金丝雀 PASS），真栈验收逐条过（热会话换代、探针绝对路径落
> agent 目录、下载链完整），**测试人员的对接流程也已跑完并确认通过（2026-09-15）**。
>
> **B1/B2 钉子已按新情况重定（见下表）。B2 钉的是测试环境实际验过的那一版,不是「发布当天的
> main」** —— 发布日 main 上已经有 B-61 PR-A（#1557，4408 行，测试环境一次没跑过），
> 它走班车 2，不在本次装载内。

> 一次性文档，发完归档。通用流程在
> [`production-release.md`](./production-release.md)，**这份只列那一份不覆盖的东西**。

| | |
|---|---|
| 上一版 tag（回滚用） | **`ad79ba28`** |
| 本版 tag | **B1 = `ca225258`**(expand,不变) / **B2 = `5775fbf3`**(contract,**2026-09-15 重钉**,= 测试环境验过的那一版;原为 `97a2e724`) |
| 区间提交数 | **129**（`git log --oneline ad79ba28..5775fbf3`;原 115,多出来的 14 个是 B-60） |
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

- [x] **本机接线还在**（2026-09-14 查过，只看存在与权限，没读内容）：

      | 文件 | 权限 | 大小 |
      |---|---|---|
      | `~/.kube/expert-work-prod.yaml` | 600 | 6358 B |
      | `~/.kube/expert-work-prod-secrets.env` | 600 | 2833 B |
      | `~/.kube/expert-work-prod-params.env` | 600 | 1203 B |
- [x] **金丝雀已 seed** —— 2026-09-13 在生产只读确认：`release-canary` 有会话行，
      它的工作区里 `canary-check.txt` 在。
      （**未 seed 时 smoke 阶段 6 会 WARNING 跳过**，等于这次发布没有真栈闸门；
      真要补按 [`production-release.md` §1.6.7](./production-release.md)）
- [x] **本版装载已重新确认**（2026-09-15 核过，钉子重定后）：`ad79ba28..5775fbf3`
      = **129 个提交**（原 115 + B-60 的 14 个）。`ca225258`（B1）确认是 `5775fbf3`（B2）
      的祖先，三段顺序成立。区间内的迁移**四个**（比原先多了 `0155`，它是 B-60 带进来的）：

      ```
      0152_feedback_run_scope.py
      0153_agent_run_supersede.py
      0154_artifact_agent_key.py
      0155_sandbox_instance_layout.py      ← B-60(PR-B)
      ```

      **`621249f6` → `5775fbf3` 之间只有 ROADMAP 与测试环境 overlay 的 newTag 记录，
      零服务代码**（`git diff --stat` 实测）—— 也就是说生产要发的代码与测试人员验过的
      逐字节相同。
- [x] **发布时段 = 2026-09-16（用户 2026-09-15 拍板）**。2026-09-13 查过生产近一个月的
      非金丝雀 run 分布（北京时间）：

      ```
      10时:5  13时:1  14时:4  15时:1  │  20时:11  21时:16  22时:6  │  0–9时:0
      ```

      主峰在 20–22 时，次峰 10–15 时，**凌晨到上午 9 点历史上零 run**。
      三段走完约 **1 小时**（B1 ~20min + C ~5min + B2 ~20min + 验证）。

      > ⚠️ **18:00 开始 = 19:00 前后收工，距 20 点主峰只剩约 1 小时缓冲。**
      > 我提过更早的时段（早上 8:00 有 11 小时缓冲），用户拍板 18:00，照做。
      >
      > **缓冲要花在哪里，这里说死**：三段式里最贵的窗口是
      > **「C 搬迁跑完、B2 还没发」**那一段 —— 那时要回滚必须先按
      > `~/b50-prod-apply.txt` 里的 `moves` 把文件反向 `mv` 回去（见 §4 第二档）。
      > **所以 19:00 还卡在 C 与 B2 之间的话，不要硬推 B2**：
      > 停在 B1（回落还在，用户侧照常，见 §4），等第二天早上再走。
      > 「停在 B1」是设计好的安全位，不是失败。
- [x] **不需要通知对接方** —— 2026-09-12 跨会话对齐时已拍板三条：默认
      `scope=agent` 不改、34 个孤儿产物不出清单不认领、「搬迁前工作区列表近乎空」
      那个窗口**对外零影响**（他们是 run 结束按 end 帧清单一次性收割 → 落自己 OSS →
      之后走自己直链，两个 workspace 端点一次都没调过）。
      **写在这里是为了别临场再纠结一遍**（我自己重复起草过一次，两节还与已定结论相反）。

- [x] **B2 钉死提交（2026-09-14 拍板）** —— Step B2 是 `git checkout 97a2e724`，
      **不是** `git checkout main`。

      B1 一直是钉死的（`ca225258`），B2 却写成浮动的 main HEAD —— 同一份单子两套
      规矩。浮动的代价不在「发布不准」，在于**从写下这份单子到 09-16，任何进三个
      应用镜像的合并都会自动进生产**：那是一次隐式的主干冻结，顺带堵死开发和测试
      环境发布。钉死之后 main 当天解冻。

      > ⚠️ **09-16 当晚别慌：测试环境会比 `97a2e724` 新。** 钉住之后主干继续走
      > （至少多了下面那三条 deps），测试环境跟着走。**两边不一致是预期的**，
      > 不是漏发。对 `97a2e724` 本身的演练单独做过（见下一条）。
      > 要比对生产实况就拿 `97a2e724` 比，别拿测试环境当下的版本比。

- [x] **依赖 PR 的取舍已拍板（2026-09-13 定诉求，2026-09-14 改手段）** ——
      按「会不会进本版 B2」分两堆：

      | PR | 落点 | 进本版 B2？ | 处置 |
      |---|---|---|---|
      | #1520 python-deps ×7 | control-plane / orchestrator 运行时 | **不会**（B2 钉 `97a2e724`） | 随时可合，走班车 2 |
      | #1487 vitest 5 | admin-ui 镜像 | **不会**（同上） | 随时可合，走班车 2 |
      | #1485 admin-ui minor-patch ×2 | admin-ui 镜像 | **不会**（同上） | 随时可合，走班车 2 |
      | #1519 codeql-action SHA | 只改 `.github/workflows/` | 不进任何镜像 | ✅ 已合 |
      | #1484 pypdf | 沙箱镜像 | **不会**（Step A 钉死 `e8aac104`） | ✅ 已合 |
      | #1486 weasyprint | 沙箱镜像 | **不会**（同上） | ✅ 已合 |

      诉求是「B2 的 diff 越接近**只有 contract 段**越好」—— 真出事时「是搬迁还是
      7 个依赖」要能一眼分开。**诉求没变，变的是实现它的手段**：原先靠「押后合并」
      实现，代价是冻主干；改成钉死 B2 的提交之后，诉求同样满足，主干照常走。

- [x] **#1486 weasyprint 告警（`#137 medium`，唯一还开着的一条）已拍板顺延** ——
      **本版明确不带**。它在沙箱镜像里，而 Step A 钉的是 `e8aac104`，不含它；
      **告警真正关掉要等沙箱钉子刷新**（B-59，09-16 之后）。

      **#1486 已于 2026-09-14 合入 main（`97a2e724`）。合进 main ≠ 关掉告警** ——
      集群跑哪个沙箱镜像由手工钉子决定，钉子不动告警就一直挂着。这正是 B-59 存在
      的理由。

      两件事要分开看：

      - 「weasyprint 与 pydyf 必须成对升」这条顾虑**已被 CI 实测清掉**：#1486 的
        `Build + smoke + Trivy` 是绿的，而 `smoke_payload.py:171` 真跑
        `weasyprint.HTML(...).write_pdf(...)` —— 文件注释自己写着「pydyf 版本不匹配
        会在这里 render 时炸，不是 import 时」。
      - 它的 `Acceptance suite under runsc` 红过两次（20m15s 撞上限），**根因查清了，
        与 weasyprint 无关**：那一档没有预建镜像也没有缓存，19m51s 全耗在
        `docker-buildx` 冷建 LibreOffice 镜像上，`collected 16 items` 但 **0/16 跑过**。
        #1541 给它加了 buildx gha 缓存 + 预建镜像，同一个 weasyprint 提交
        **1m7s 通过**（建 36s、套件 5s）。#1543 再把预算 30→45 分钟兜底。

> ⚠️ **发布当天 smoke 会打一条 `WARN` 沙箱钉子落后，那是预期的，不是故障。**
> #1484 已经合进 main，`smoke.sh` 里 #1518 加的那节会拿 `sandboxset.yaml` 的 tag
> 与 `infra/sandbox-image/` 的 HEAD 比，报 `WARN 落后 N 个提交`。
> **它是 warn-only，不会让 `release.sh` 失败**（刻意的，见 B-57：别拿一次好发版
> 去赔一个无关的镜像滞后）。照常往下走，钉子刷新走 B-59。
>
> B1 阶段不会打 —— 那时 HEAD 是 `ca225258`，钉子对它不滞后。

- [x] **测试环境已泡过 `97a2e724` 本身**（不是「泡过某个更新的 main」）——
      **2026-09-14 已发，SMOKE PASS + 金丝雀 PASS 5/5**（`CANARY_OK 60875447`，
      run 28.7s，记录 PR #1546）。发的时候 main 已经是 `698192b1`，**刻意发的钉子
      不是 main**。B-50 搬迁 2026-09-13 在测试环境跑通过一次。

      演练的意义不在「验出新东西」（`fd12882c..97a2e724` 的 8 个文件零个进三个
      应用镜像），而在**让演练跑在真正要发生产的那个产物上**。两者要分开算。

      发布时打的那条 `WARN 沙箱钉子落后` 已实测为 **3 个提交**（`97a2e724`
      weasyprint / `b3cbee20` apt upgrade / `3585f9ef` pypdf），warn-only 确认。
- [x] **发布前一天先把三个镜像建一遍**（缓存预热 + 腾盘位）—— 2026-09-14 那次
      `97a2e724` 发布已顺带做完：三个 base 预拉全 OK，三个镜像建推成功，一次过。2026-09-12/13 发测试
      环境连炸三次，三个不同原因，全部是本机环境而非代码：

      | 症状 | 处置 |
      |---|---|
      | `failed to fetch anonymous token … EOF` 或 `Head …/manifests: EOF` | ECR Public 按 IP 限流。**三个 base 全预拉**，见下 |
      | `copy file range failed: no space left on device` | 本机 Docker 盘满。`docker builder prune -af` |
      | apt 拉到 `1021 B/s` 后 `Connection failed` | 网络瞬时塌陷。先探源的速度，通了再重跑 |

      后两条有因果：prune 清掉构建缓存 → 下一跑必须重建 apt 层 → 正好撞上网络。

      **预拉要拉全三个** —— 09-13 我只预拉了 nginx，结果下一跑撞在 `node` 上；
      「上次炸的那个」不等于「会炸的那些」：

      ```sh
      for img in public.ecr.aws/docker/library/node:22-alpine \
                 public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine \
                 public.ecr.aws/docker/library/python:3.12-slim-bookworm; do
        for i in 1 2 3; do docker pull -q "$img" >/dev/null 2>&1 && { echo "OK  $img"; break; }; sleep 8; done
      done
      ```

      提前一天建好镜像 + 拉全 base，能一次性避开这三条。

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
- **迁移 `0152` / `0153` / `0154` / `0155` 自动跑** —— 在 `apply -k` 的 migrate Job 里

---

## 3. 执行顺序

本次发布是 **expand → migrate → contract 三段式**，三段**在同一个窗口里连着做完**，
和测试环境 2026-09-13 走的**逐字相同**：

| 段 | 做什么 | 为什么必须分开 |
|---|---|---|
| **B1 expand** | 发 `ca225258`（带迁移期读回落） | 这一版**容忍旧扁平布局**：agent 在自己目录下读不到时回落用户根。搬迁还没跑，历史文件都还在用户根上 |
| **C migrate** | 跑存量搬迁 | 文件从用户根搬进 `agents/<key>/` 与 `shared/` |
| **B2 contract** | 发 `97a2e724`（摘掉回落，PR6） | 搬完之后回落反过来成了跨 agent 读洞 —— 用户根上剩下的恰恰是**别人的**历史文件 |

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
      按 §1 的预拉脚本把**三个 base 全拉一遍**再重跑）
- [ ] migrate Job `condition met`（= `0152`/`0153`/`0154`/`0155` 跑过）
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

> ⚠️ **不要在生产上跑对接方的 agent 去验这条。** 生产唯一的多 agent 用户
> (`81066c49…`)两个 agent 都是对接方的(`sop2-designer` / `ai-health-plan`),
> 照「用 A agent 起一轮看看能不能看到 B」去做,等于**拿他们的生产工作区做实验**。
> 真栈探针只用**金丝雀**或探针 user —— 这条规矩不因为「只是读一下」而放宽。

**生产上验两件可验的**：

```sh
# ① 文件系统侧：两个 agent 的树是分开的，且都非空
kubectl -n $NS exec -i "$POD" -- sh -c '
  R=/mnt/workspaces/b0f0d29b-62ce-4326-ae92-e1c18631c935/81066c49-0f8e-4dc2-9acd-4c57e3be5973
  for d in "$R"/agents/*/ "$R"/shared; do
    [ -d "$d" ] && echo "$(basename "$d"): $(find "$d" -type f | wc -l) 文件"
  done'
```

- [ ] ① `agents/sop2-designer-c97db277/` 与 `agents/ai-health-plan-30817804/` **各自非空且互相独立**，
      `shared/` 单独一份
- [ ] ② **金丝雀那一轮就是作用域的活证据** —— Step B1 的 smoke 阶段 6 里，
      `release-canary` 真跑了一次 run、写文件、存产物、按 `agent_code` 取回、下载校验内容。
      它全程只碰自己的 `agents/release-canary-fd420deb/`，PASS 就说明 agent 作用域在生产上是通的
- [ ] ③ **控制台工作区浏览面**复看一眼：按 agent 分组、`shared/` 单独一组
      （人眼一秒能看出搬迁有没有把树搞乱，比任何脚本都直观）

> **「A agent 看不到 B agent 的目录」这条已在测试环境用真 run 实证**：
> `pf-probe` 的 `list_dir(".")` 只返回 `uploads/`，同一用户下另一个 agent 的
> 24 个文件零泄露。生产不重跑这一条，是**刻意的**——它需要拿对接方的 agent
> 在对接方的工作区上做实验，代价大于收益。

#### C.6 数字对不上怎么办

**对不上 ≠ 紧急。** 搬迁是可以**不做**的 —— B1 那一版带着迁移期读回落，
不搬也能正常跑，只是控制台工作区浏览面和对外两个 workspace 端点返回空。

所以顺序是：

1. **不要 `--apply`**（空跑阶段对不上时）；已经 apply 的**不要手工改数据**
2. 把 `~/b50-prod-dryrun.txt` / `~/b50-prod-apply.txt` 留好
3. **停在 B1**，别走 B2 —— 回落还在，用户侧照常
4. 按 [`workspace-agent-scoping-migration.md` §出了问题怎么办](./workspace-agent-scoping-migration.md) 处理；
   崩在半路的重跑同一条命令即可（脚本幂等）

C.1 的表就是为这一刻准备的：对不上时你**立刻知道是哪个用户、差多少**，
而不是从零开始查。

### Step B2 — 发版（contract：摘掉迁移期读回落，PR6）

**闸门：Step C 的验收四个数全对之后才做。** 搬迁没跑完就摘回落 = agent 读不到
自己目录里的历史文件，而那时唯一的缓解手段是再发一次版把回落加回去。

**发 `97a2e724`，不是当下的 main。** 理由见 §1：那之后合进 main 的东西（deps 等）
属于班车 2，本版不带。**测试环境此刻多半已经比它新，那是预期的。**

```sh
git fetch origin main
git checkout 97a2e724            # contract 版本；钉死的，不是「当下的 main」
git log -1 --oneline             # 确认就是它

tools/deploy/release.sh prod
```

- [ ] Step C 的 C.4 四个数全对
- [ ] 确认 checkout 的是 `97a2e724`
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

- [ ] `chore(deploy): prod newTag 97a2e724` PR，正文写上**上一版 tag `ad79ba28`**，
      并注明本次是 **B1 `ca225258` → 搬迁 → B2 `97a2e724`** 三段
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

DB 侧不用担心：`0152`/`0153`/`0154`/`0155` 都是 expand-only（加列/加约束，向后兼容一版）。
`0155` 实测就是一句 `add_column('sandbox_instance', 'layout', server_default='user-root')` ——
旧版本代码不认识这一列,但它有默认值,写入不会失败。

⚠️ **已知未验**：`rollback.sh prod` **没有在生产上实跑过**。它是秒级 `set image`
（`rollback.sh` 头注），逻辑简单，但「没跑过」就是没跑过。真要用的时候，先
`kubectl -n expert-work get deploy -o wide` 记下当前镜像，跑完再比一次。

---

## 5. 收工确认

- [ ] 三段都做完，且 `git status` 里 overlay 的 newTag 改动已进 Step E 的记录 PR
- [ ] 生产 `kubectl -n expert-work get pods` 无 CrashLoop / 无重启计数异常
- [ ] `~/b50-prod-dryrun.txt` 与 `~/b50-prod-apply.txt` 两份留档还在
      （**唯一的回退依据**，别清）
- [ ] 执行单本身归档（这是一次性文档，发完就该躺进历史，而不是被下一次误用）
