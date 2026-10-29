# 生产发布执行单 2026-10-29(已执行,全部通过)：`94dc73ba` → `7e6b7679e` + PostgreSQL 16 → 17

> 拍板(10-29 晚):① PostgreSQL 17 今晚升 ② B-228 今晚接受、发后看日志，明天修 ③ 沙箱按测试顺序动两次（先换镜像，代码上线后换安全组）④ 旧提示文件明天清 ⑤ B-236 不在本版 ⑥ 深护智康生产今晚同步更新。
> **所有生产命令由你执行**；每一段做完把输出贴给我，我对照判据说能不能进下一段。对不上就停。
> 数据库升级和代码发布**分开验收**（`rds-pg-major-upgrade.md` §9 第 4 条）：先升库、验完，再发代码。

## 总览

| 段 | 内容 | 预计 | 对用户影响 |
|---|---|---|---|
| 0 | 前置：通知、阿里云升级检查报告、只读自查 | 30–40 分钟（升级检查报告要等） | 无 |
| 1 | 数据库 16 → 17：停写 → 升级 → 检查 → 装 `pg_textsearch` → 恢复写 → 验收 | 约 60 分钟 | **平台停服约 30 分钟**（测试实测停写 → 恢复写 30 分钟，其中真正只读 4 分 47 秒） |
| 2 | 代码发布：P 快照 → A 换沙箱镜像 → B 发版 → C 点检 → G 沙箱换安全组 → D 抽查 → F 记录 | 约 1.5 小时 | 在途沙箱被打断两次；发版滚动更新不停服 |
| | **合计** | **约 3 小时** | |

---

## 段 0：前置（现在就可以做，不影响线上）

### 0.1 通知深护智康（必须在段 1 之前）
- **停服窗口**：段 1 期间平台所有接口不可用约 30 分钟（具体起止你们定），请他们这段时间暂停调用 / 提示员工。
- **他们的生产更新**：`leyi` 分支（含 `8463e046`）今晚上线；建议他们**先于或紧接着**我们的段 2 上线。
- **接口变化**：见文末「附 A」。

### 0.2 建独立发版目录（不动你当前分支）
```sh
cd ~/src/github/jone_qian/expert-work && git fetch origin
git worktree add --detach ~/ew-prod-7e6b7679 7e6b7679e
cd ~/ew-prod-7e6b7679 && git log -1 --oneline   # 7e6b7679e chore(deploy): test newTag 9da3dbda7 …(#1973)
export KUBECONFIG=~/.kube/expert-work-prod.yaml
NS=expert-work
```
之后所有命令都在这个目录、这个终端里执行。

### 0.3 阿里云控制台：创建升级检查报告（你在控制台点）
生产 RDS 实例 →「大版本升级」→「升级检查」→「创建升级检查报告」→ 目标版本 **17**、升级模式 **本地升级** → 确定。
- 检查期间实例显示「维护实例中」（测试上没影响读写）。结果「成功」或「警告」可以升级，「失败」必须先修。报告 7 天有效。
- 同时确认：存储类型是**云盘**、没有挂只读实例（本地升级的前提）。

### 0.4 数据库只读自查 + 数写方
```sh
POD=$(kubectl -n $NS get pod -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[0].metadata.name}')
kubectl -n $NS exec "$POD" -- python -c "$(cat tools/deploy/pg_upgrade/precheck.py)"
kubectl -n $NS get deploy,statefulset,cronjob
```
判据：没有 `ERROR`；复制槽、发布、订阅、两阶段事务、reg* 列、unlogged 表、collation 不一致都是 0；pgcrypto 不在 `pg_catalog`。
写方清单以生产实际为准（测试是 control-plane 2、credential-proxy 1、keycloak 1、langfuse-web 1、langfuse-worker 1，CronJob retention-cleanup），**对象数和测试数量级不同时先停，重估只读时长**。把两段输出贴给我。

### 0.5 发版前的只读核对（原执行单 §1）

- [ ] 三个本机文件都在、权限是 600：`ls -l ~/.kube/expert-work-prod.yaml ~/.kube/expert-work-prod-params.env`
- [ ] **对接方前置（硬条件）**：深护智康**生产**的 project-service 已经适配 `incomplete`。现在只确认到 leyi 分支、合 dev。没适配的话（对方 10-29 自查结论），`incomplete` 的 run 他们**不收件、不扣费，战略报告判失败**。见待拍板 ③。
- [ ] 金丝雀还在：上次是首跑 PASS。
- [ ] 本机工作区干净：`git status --porcelain` 输出为空，然后 `git fetch origin`。
- [ ] 装载核对：
  - `git log --oneline 94dc73ba..7e6b7679e | wc -l` 应为 **178**
  - `git diff --name-only 94dc73ba..7e6b7679e | grep migrations/versions` 应为 **17 行**：`0022` 加上 0164–0169、0171–0180

**只读盘点 SQL**（`run_sql` 包装直接照抄 `2026-09-24-prod-release-checklist.md` §1，里面带 `SET TRANSACTION READ ONLY`）。这条 SQL 未在测试库跑过，列名我按 7e6b7679 的 ORM 核过：

```sql
SELECT 'alembic' k, version_num::text v FROM alembic_version
UNION ALL SELECT 'server_version', current_setting('server_version')
UNION ALL SELECT 'pgvector', extversion FROM pg_extension WHERE extname='vector'
UNION ALL SELECT 'kb_bases', count(*)::text FROM knowledge_base
UNION ALL SELECT 'kb_documents', count(*)::text FROM knowledge_document
UNION ALL SELECT 'kb_chunks', count(*)::text FROM knowledge_chunk
UNION ALL SELECT 'size_'||relname, reltuples::bigint||' rows / '||pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class WHERE relname IN ('agent_run','artifact_version')
UNION ALL SELECT 'rate_deepseek_v4_flash', count(*)::text FROM model_rate_card WHERE tenant_id IS NULL AND provider='deepseek' AND model='deepseek-v4-flash'
UNION ALL SELECT 'long_xacts_gt_1min', count(*)::text FROM pg_stat_activity WHERE datname=current_database() AND xact_start < now()-interval '1 minute' AND pid<>pg_backend_pid()
UNION ALL SELECT 'running_queued_paused', count(*)::text FROM agent_run WHERE status IN ('running','queued','paused')
```

期望结果：
- `alembic` = `0163_writeback_job_dedup`
- 三个 `kb_*` 都是 0
- `rate_deepseek_v4_flash` = 1（是 0 的话，0166 什么也不插，`deepseek-flash` 的用量会记成「未定价」，发后到控制台补价）
- `long_xacts_gt_1min` = 0（不为 0 就先等它结束，否则 0178 拿锁会排队）
- `artifact_version` 应该是几千行以内

记下 `pgvector` 版本。如果低于 0.8，检索会退回只带 `ef_search`，检索本身照常可用（§24.5）。

---

## 段 1：数据库 16 → 17（照测试 10-21 实际走过的步骤重放，`rds-pg-major-upgrade.md` §4–§7）

### U1 停写 + 升级前快照（约 10 分钟，从这里开始停服）
```sh
OUT=~/pg-upgrade-prod-$(date +%Y%m%d); mkdir -p "$OUT"
kubectl -n $NS get deploy/control-plane deploy/credential-proxy deploy/langfuse-web deploy/langfuse-worker statefulset/keycloak \
  -o custom-columns=KIND:.kind,NAME:.metadata.name,REPLICAS:.spec.replicas | tee "$OUT/replicas.txt"
IMG=$(kubectl -n $NS get deploy control-plane -o jsonpath='{.spec.template.spec.containers[0].image}')
sed "s#__CONTROL_PLANE_IMAGE__#${IMG}#" tools/deploy/pg_upgrade/dbtool-pod.yaml | kubectl apply -f -
kubectl -n $NS wait --for=condition=Ready pod/pg-upgrade-dbtool --timeout=180s
kubectl -n $NS patch cronjob retention-cleanup -p '{"spec":{"suspend":true}}'
kubectl -n $NS scale deploy control-plane credential-proxy langfuse-web langfuse-worker --replicas=0
kubectl -n $NS scale statefulset keycloak --replicas=0
kubectl -n $NS get pods | grep -E '^(control-plane|credential-proxy|langfuse-web|langfuse-worker|keycloak)-' || echo "writers gone"
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/precheck.py)" > "$OUT/precheck-before.tsv"
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/snapshot.py)" > "$OUT/snapshot-before.tsv"
awk -F'\t' '$2 == "client_connections"' "$OUT/precheck-before.tsv"
sleep 20; kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/snapshot.py)" > "$OUT/snapshot-before2.tsv"
diff "$OUT/snapshot-before.tsv" "$OUT/snapshot-before2.tsv" && echo "writes really stopped"
```
- 如果 0.4 数出的写方和这里不同，按生产实际改 scale 那两行。control-plane 会等进行中的运行结束（最长约 5 分钟），要等到打印 `writers gone`。
- 判据：`client_connections` 只剩个位数（工具 pod + 阿里云管理账号）；两份快照逐字一致（打印 `writes really stopped`）；没有 `ERROR`。

### U2 升级（你在控制台点，约 10–15 分钟）
实例「大版本升级」→ 目标版本 **17**、**本地升级**、割接时间 **立刻** → 确认。
- 在备份列表里**记下升级前那份全量备份的时间**（回退要用）。
- 等实例回到「运行中」、版本显示 17。测试：点升级后约 6 分钟进入只读，只读 4 分 47 秒。

### U3 升级后检查（写还停着，约 10 分钟）
```sh
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/precheck.py)" > "$OUT/precheck-after.tsv"
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/snapshot.py)" > "$OUT/snapshot-after.tsv"
diff <(tail -n +2 "$OUT/snapshot-before.tsv") <(tail -n +2 "$OUT/snapshot-after.tsv") && echo "snapshot identical"
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/analyze.py)" | tee "$OUT/analyze.tsv"
```
判据：`server_version` 17.x；扩展清单和升级前相同（版本号可变）；**行数行全部一致**（不一致就停，不恢复写）；`analyze` 每个库 `ok`、`tables_without_stats=0`。
向量探针行不一致时先别判坏，跑 `kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/exact_knn.py)"`，升级后 ≥ 9/10 就继续（生产升级前已知只有 60%，B-208，升级是修好它）。

### U4 装 `pg_textsearch`（你在控制台点，实例会自动重启一次，约 3 分钟）
控制台「插件管理」给 `expert_work_dev`（以生产库名为准）安装 `pg_textsearch`，等实例回到运行中。
```sh
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/snapshot.py)" > "$OUT/snapshot-after-ext.tsv"
diff <(tail -n +2 "$OUT/snapshot-after.tsv") <(tail -n +2 "$OUT/snapshot-after-ext.tsv") && echo "identical after extension"
```
再在工具 pod 里查 `select extversion from pg_extension where extname='pg_textsearch'` 应为 1.3.1（命令我到时给你）。

### U5 恢复写 + 验收（约 20 分钟，到这里停服结束）
```sh
# 按 $OUT/replicas.txt 的生产实际副本数恢复（下面是测试的数，照你的 replicas.txt 改）
kubectl -n $NS scale statefulset keycloak --replicas=1
kubectl -n $NS scale deploy control-plane --replicas=2
kubectl -n $NS scale deploy credential-proxy langfuse-web langfuse-worker --replicas=1
for r in statefulset/keycloak deploy/control-plane deploy/credential-proxy deploy/langfuse-web deploy/langfuse-worker; do kubectl -n $NS rollout status "$r" --timeout=600s; done
kubectl -n $NS patch cronjob retention-cleanup -p '{"spec":{"suspend":false}}'
kubectl -n $NS delete pod pg-upgrade-dbtool
tools/deploy/smoke.sh prod
tools/deploy/pg_upgrade/run_canary.sh prod
tools/deploy/pg_upgrade/vector_recall_check.sh prod
```
判据：`SMOKE PASS`（含 Keycloak、Langfuse 两行）；金丝雀 `RESULT: PASS`；向量检查 `[A]` / `[B]` / `[C]` 都是 **100%**（`[A]` 升级前 60%）。
再验：① 你在浏览器**登录控制台、退出再登一次**，进一个智能体发一句话（Keycloak 25 官方没测过 PG 17，这条不能省）；② 金丝雀结束 1 分钟后，会话详情「计时」页「Langfuse 精确」列有数据。
**段 1 全过才进段 2。**

---

## 段 2：代码发布 `7e6b7679e`

### Step P：只读快照（2 分钟）

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
kubectl -n default get sandboxset expert-work-sandbox -o jsonpath='{.spec.template.spec.containers[*].image}{"  cache="}{.spec.template.metadata.annotations.image\.alibabacloud\.com/enable-image-cache}{"  sg="}{.spec.template.metadata.annotations.network\.alibabacloud\.com/security-group-ids}{"\n"}'
for ns in expert-work default; do kubectl -n $ns get secret acr-pull -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | python3 -c 'import json,sys; print(*sorted(json.load(sys.stdin)["auths"]))'; done
kubectl -n expert-work get deploy -o wide > ~/prod-before-$(date +%Y%m%d%H%M).txt
```

期望（从 e3e940ab7 那次推算，10-16 生产开镜像缓存时 apply 的就是它，未核）：
- 镜像是 `crpi-…-vpc…/sandbox:d8a32812`，`cache=true`，`sg` 为空
- 两个 namespace 的 `acr-pull` 都打印出两个 host

对不上就停下来，先弄清楚。

### Step A：沙箱镜像换成 `daf63cb1`（5 分钟。在途沙箱会被打断一次，所以放在窗口内）

```sh
git show 7e6b7679e:infra/k8s/sandbox/sandboxset.yaml | grep 'expert-work/sandbox:'   # 必须是 …-vpc…/sandbox:daf63cb1
git show 7e6b7679e:infra/k8s/sandbox/sandboxset.yaml | kubectl apply -f -
kubectl -n default get sandboxset expert-work-sandbox -w        # 等 availableReplicas=1
kubectl get imagecaches.image.alibabacloud.com | grep daf63cb1
```

- 测试上 `daf63cb1` 的缓存 `imc-bp18rptgtptdtmgk83tg` 已经 Ready。生产和测试如果是同一个阿里云账号，生产会直接命中（10-16 有先例）。
- 判据：pool pod 有 `ImageCacheHit`；如果走的是冷拉，40–70 秒内就绪也算正常。

### Step B：发版（25–40 分钟）

```sh
cd ~/ew-prod-7e6b7679 && git log -1 --oneline   # 前置 0.2 已建好这个目录   # 标题：chore(deploy): test newTag 9da3dbda7 …(#1973)
tools/deploy/release.sh prod      # 输入 prod；tag 默认取短 sha，预计是 7e6b7679e
```

- [ ] 三个镜像都建推成功。admin-ui 卡在 pnpm、或 `uv sync` 超时，属于网络问题，原地重跑即可；失败停在 stage 1 时没有动过集群。
- [ ] migrate Job `condition met`，日志里**恰好 16 条** upgrade，从 `0163_writeback_job_dedup -> 0164_…` 到 `0179_… -> 0180_artifact_version_snapshot`。
- [ ] rollout 完成。B-80 收口要等在跑的对话结束，最长约 5 分钟；超时先按 09-24 单 §2 第 3 条那张表判断，别直接回滚。
- [ ] smoke：
  - `OK sandbox image pin is current (daf63cb1)`
  - `sandbox image cache` 是 OK 或首次 WARN
  - **`sandbox network (B-217)` 这一行现在是 WARN，属于预期**（沙箱池模板还没有安全组），到 Step G 之后才变 OK
- [ ] 阶段 6 金丝雀是 **PASS**，不是 WARNING。

### Step C：发后点检（10 分钟）

```sh
kubectl -n expert-work get pods; kubectl -n expert-work get deploy -o 'custom-columns=N:.metadata.name,I:.spec.template.spec.containers[0].image'
kubectl -n expert-work get pods -l app.kubernetes.io/name=credential-proxy -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.annotations.network\.alibabacloud\.com/security-group-id}{"\n"}{end}'
for p in $(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[*].metadata.name}'); do printf '%s: ' $p; kubectl -n expert-work logs $p | grep -c -E 'UndefinedColumn|unresolved_failures.*does not exist|snapshot_key.*does not exist'; done
```

上面三条命令里，取标签和注解的写法我在测试集群上跑通过。期望：
- [ ] 应用镜像都是新 tag；admin-ui 是 `…-prod`
- [ ] credential-proxy 的安全组是 `sg-bp14yleiog3dkd0io2ox`，pod Ready
- [ ] 每个 pod 的 B-228 报错计数为 0。不为 0 时，先看是不是只出现在迁移完成前那几秒、之后不再增长；在增长就停下来查

用 `run_sql` 跑：

```sql
SELECT version_num FROM alembic_version;
SELECT table_name, column_name FROM information_schema.columns WHERE (table_name,column_name) IN (('agent_run','unresolved_failures'),('artifact_version','snapshot_key'));
SELECT provider, model FROM model_rate_card WHERE tenant_id IS NULL AND (provider,model) IN (('deepseek','deepseek-flash'),('qwen','qwen3.7-flash'),('qwen','qwen3.8-flash'),('glm','glm-ocr'),('qwen','text-embedding-v4'));
```

期望：
- 版本是 `0180_artifact_version_snapshot`
- 两列都在，返回 2 行
- 价目表 5 行；§1 里 `rate_deepseek_v4_flash` 是 0 的话就是 4 行

代码在位检查（未在 pod 上跑过，符号是对着 7e6b7679 源码核的）：

```sh
kubectl -n expert-work exec <cp-pod> -- python3 -c 'import os; from orchestrator.tools.artifact_snapshots import NasArtifactSnapshotStore, MAX_SNAPSHOT_BYTES; assert MAX_SNAPSHOT_BYTES==64*1024*1024; from expert_work.persistence import ARTIFACT_SNAPSHOT_DIR; from expert_work.runtime.runs.schemas import ExternalRunStatus; assert ExternalRunStatus.INCOMPLETE=="incomplete"; from control_plane.settings import Settings; s=Settings(); assert (s.knowledge_parse_workers, s.knowledge_parse_worker_max_vm_mb)==(1,2400); assert os.environ["MALLOC_ARENA_MAX"]=="2"; assert os.environ["EXPERT_WORK_SANDBOX_PIP_EXTRA_INDEX_URL"].startswith("https://mirrors.cloud.tencent.com"); print("ok")'
```

### Step G：B-217 沙箱池换安全组（15 分钟，设计文档 §7）

```sh
kubectl -n default get pods -l agents.kruise.io/sandbox-pool=expert-work-sandbox -L agents.kruise.io/sandbox-claimed   # 先看有没有 claimed=true 的，挑没人在跑的时候
kubectl apply -k infra/k8s/sandbox-prod          # 当前检出的就是 7e6b7679e
kubectl -n default delete sandbox -l agents.kruise.io/sandbox-pool=expert-work-sandbox   # 此刻正在跑的工具调用会失败一次；工作区在 NAS，重建无损
kubectl -n default get sandboxset expert-work-sandbox -w     # 等回到 availableReplicas=1
tools/deploy/smoke.sh prod
```

smoke 必须有这两行：
- [ ] `OK   sandbox pool is on its own security group (sg-bp1a9oa7oi3v4s3ccquu)`
- [ ] `OK   sandbox security group still covers cluster DNS and the k8s API`

然后在待命沙箱里探测，只打印主机名和状态，不跑任何对接方的 Agent：

```sh
SBX=$(kubectl -n default get pods -l agents.kruise.io/sandbox-pool=expert-work-sandbox,agents.kruise.io/sandbox-claimed=false -o jsonpath='{.items[0].metadata.name}')
kubectl -n default exec $SBX -c main -- python3 -c '
import socket
for n,h,p in (("baidu","www.baidu.com",443),("control-plane","control-plane.expert-work.svc.cluster.local",8000),("prometheus","prometheus.expert-work.svc.cluster.local",9090),("proxy-8080","credential-proxy.expert-work.svc.cluster.local",8080),("proxy-8081","credential-proxy.expert-work.svc.cluster.local",8081),("pip-mirror","mirrors.cloud.aliyuncs.com",80),("nas","172.29.41.55",2049)):
    try: socket.create_connection((h,p),timeout=4).close(); print(n,"OPEN")
    except Exception as e: print(n,"closed",type(e).__name__)'
```

期望：
- [ ] baidu、control-plane、prometheus、proxy-8080 四项都是 `closed`
- [ ] proxy-8081、pip-mirror、nas 三项都是 `OPEN`
- [ ] 能解析出域名，说明 DNS 是通的
- [ ] 再跑一次金丝雀，PASS。用 `tools/deploy/smoke.sh prod` 加上 canary，或者重跑 `release.sh` 的阶段 6；具体入口按 09-24 单的写法

### Step D：真栈抽查（10 分钟，只用金丝雀产生的数据）

```sql
SELECT count(*) FILTER (WHERE snapshot_key IS NOT NULL) with_copy, count(*) total FROM artifact_version WHERE created_at > now()-interval '1 hour';
SELECT status, completed, exit_reason, unresolved_failures FROM agent_run WHERE created_at > now()-interval '1 hour' ORDER BY created_at DESC LIMIT 5;
```

期望：
- 金丝雀登记的产物版本都有副本，即 `with_copy = total`
- 金丝雀的 run 是 `success / true`，`unresolved_failures` 是 `[]`

### Step F：收尾记录（10 分钟）

- [x] 记录 PR `chore(deploy): prod newTag 7e6b7679e`（#1974），写上：上一版 `94dc73ba`；沙箱 `d8a32812→daf63cb1`；B-217 切换；回滚命令。
- [x] 同一 PR 里改 `production-release.md` 的现状表。
- [x] 更新记录 `changelog.md` 两条的「待发布」改成 2026-10-29。
- [x] ROADMAP 里本版上线的条目（B-207 / B-208 / B-217 / B-222 / B-226 / B-235）标注已上线。

---

## 回滚

**数据库**：本地升级**不能原地降回 16**。要回 16 只能用 U2 记下的升级前全量备份「恢复到新实例」，再把 4 份连接配置（`control-plane-secrets`、`credential-proxy-secrets`、`keycloak-secrets`、`langfuse-secrets`）改到新实例并重启写方（`rds-pg-major-upgrade.md` §8）。窗口内写是停着的，只丢验收期间的数据。代码在 16、17 上都能跑，**代码回滚不需要动数据库**。


**回滚目标**：`tools/deploy/rollback.sh prod 94dc73ba`。
- 它只换 control-plane、admin-ui、credential-proxy 三个镜像。
- wecom-adapter、留存 CronJob、configmap、4Gi、安全组注解都留在新值上，对旧代码无害。

**不要 `alembic downgrade`**：16 条迁移全部是只增不删，旧代码不读新列和新表，0180 的 CHECK 对旧代码的插入（NULL）照样成立。

**不可逆的部分**（生产上实际影响都是 0）：
- 价目表新插的行：downgrade 本来就不删，无害。
- 0176 删的孤儿修订行。
- 0171 / 0174 的 `UPDATE knowledge_base`：0 个知识库，所以 0 行。

**各步怎么退**：

| 退哪一步 | 怎么做 |
|---|---|
| 已经做过 G，要回滚代码 | **先**把沙箱池退回集群安全组，否则旧代码跑在隔离组上会出问题（§7）：`git show 7e6b7679e:infra/k8s/sandbox/sandboxset.yaml \| kubectl apply -f -`（三方合并会去掉安全组注解；未核，apply 后用 Step P 的命令确认 `sg=` 为空），再回收沙箱，然后跑 `rollback.sh` |
| 只回沙箱镜像 | `git show e3e940ab7:infra/k8s/sandbox/sandboxset.yaml \| kubectl apply -f -`，回到 `d8a32812` 加缓存注解、不带安全组 |
| 只有 G 出问题 | 同第一行的第一步，代码不用动 |
| credential-proxy 安全组 | 不用退：沙箱在集群组时，它经 VPC 规则照样可达 |

**回滚前先清这几样**：
- 有 Agent 配了 `knowledge_read` 工具：旧版不认，回滚前删掉。生产 0 个知识库，应该没人配。
- 有文档处在 `estimating` / `awaiting_confirmation`：先按 §69.5 那条 UPDATE 改成 `failed`。生产应为 0 行。
- 回滚期间，新登记的产物版本旧代码只读工作区路径，等于退回 B-235 之前的行为。
- `incomplete` 在库里存的仍是 `success` 加 `completed=false`，旧代码读得了。

---

## 附 A：发前通知深护智康的接口变化（`changelog.md` 里两条标着「待发布」）


1. **B-226（对方需要改代码）**：
   - `status` 新增 `incomplete`，出现在 SSE `end`、listRuns、对话条目、Webhook 里。
   - listRuns、对话条目、Webhook 新增 `completed`、`exit_reason`、`unresolved_failures` 三个字段。
   - listRuns 的 `status=success` 筛选不再包含被截停的 run。
   - 子任务的 `outcome` / `worker.status` 会出现 `approval_blocked` / `cancelled`。
   - 新增 `guard` 的 warning 事件。
   - `no_progress` 默认改成「第 2 次原地打转就停」，以前 0 表示不检查。
   - 脚本退出码非 0、MCP 报错、提交类 4xx/5xx 都会让 `completed=false`。
   - **轮询必须把 `incomplete` 当作结束状态。**
2. **B-235（行为变化，对方已确认不受影响）**：
   - 下载产物带 `version` 时，取的就是那一版的内容（以前不管传几都给最新）；版本不存在、或上线前登记且已不是最新版，仍返回 409。
   - 新增响应头 `X-Artifact-Version`。
   - 超过 64 MiB 的文件 `save_artifact` 直接报错、不登记。
3. **另外两条，没进更新记录，请确认之前是否单独通知过**：
   - B-186：只有思考、没有正文的中断回复，现在 run 记失败。频率很低，生产 60 天里 0 次。
   - B-188：新对话不再往工作区根目录写 `PLAN.md` / `TODO.md` / `MEMORY.md` 这几个提示文件。

B-236（路径形状、`session_id`）不在本版，今晚不用通知。

---


## 附 B：发前没能核实的地方（执行后的结论）

- 生产当前的沙箱钉子、`acr-pull`、表大小、价目表、`pgvector` 版本：Step P 和 0.5 的 SQL 已确认。
- 盘点 SQL 和 pod 内的代码在位检查命令：发前先在测试库、测试 pod 上跑通，生产照常输出。
- 生产能否直接命中测试已建好的 `daf63cb1` 缓存：Step A 已命中。
- 4Gi 对 ACS 计费的影响：仍未核。
- `apply -f` 能否去掉 `apply -k` 写上的安全组注解（回滚第一行）：本次没有回滚，仍未核。
