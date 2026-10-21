# Runbook — RDS PostgreSQL 大版本升级 16 → 17(B-207)

> **拍板(2026-10-21)**:测试库先升;生产库等 B-206(知识库检索优化)全部做完,
> 和知识库版本一起升、一起发。生产照本手册在测试上**实际走过的步骤**原样重放。
>
> **为什么升**:B-206 K-9 想用的 BM25 插件 `pg_textsearch` 在阿里云 RDS 上只有
> PG 17 / 18 有(PG 16 及以下「无」,插件列表 2026-09-23 版);PG 16 上只能自己算 BM25。
> 量 BM25 值不值得用,也要先有一个 17 的库。
>
> 分工:控制台操作(升级检查、升级、插件安装、必要时恢复实例)由用户做 —— 本机
> 没有阿里云 CLI / 凭据;集群侧(停写、快照、验证、恢复写)按下面的命令执行。
> **生产上的任何命令都由用户执行或明确授权后执行。**

## 0. 过渡期规矩(测试 17、生产 16)

- **主干必须同时能在 16 和 17 上跑。** 只有 17 才有的东西一律做成可选:迁移里
  「插件可用才 `CREATE EXTENSION`」,代码检测不到 `pg_textsearch` 就走现有的
  tsvector 关键词检索。理由:生产每次发布发的是整个主干,只要主干里有一条
  「必须 17」的迁移,之后任何一次生产发布(哪怕只修一个无关 bug)都会在 16 上失败。
  私有化部署的客户库也不一定装得了这个插件,本来就该可选。
- **两个版本各有一层覆盖**:CI 集成测试用 `pgvector:pg16`(与生产同),测试环境是 17。
  过渡期不改 CI。
- 生产升级排在 B-206 生产发布之前、同一个窗口里(§9)。

## 1. 现状与预检查

### 1.1 测试库实测(2026-10-21,PG 16.13,内核小版本 20260630)

| 项 | 值 |
|---|---|
| 实例上的库 | `expert_work_dev` 4.6 GB / `keycloak` 13 MB / `langfuse` 12 MB(+ `postgres`、`rdsadmin`) |
| 对象数(`pg_class`) | 944 / 826 / 787 / 413 —— 合计约 3,000,文档说「百万级」才到十分钟以上,预计只读分钟级 |
| 扩展 | `expert_work_dev`:`pg_trgm 1.6`、`pgcrypto 1.3`、`uuid-ossp 1.1`、`vector 0.8.0.2`,都在 `public`;其余库只有 `plpgsql` |
| PG 17 上是否都有 | 有:pgvector 0.8.0、pgcrypto 1.3、pg_trgm 1.6、uuid-ossp 1.1;另有 `pg_textsearch` 1.3.1、`pg_jieba` 1.2.0 |
| 复制槽 / 发布 / 订阅 / 两阶段事务 | 全 0 |
| `pg_upgrade` 不支持的 reg* 列 / unlogged 表 / collation 版本不一致 | 全 0 |
| pgcrypto 在 `pg_catalog`(检查报告会报错的情形) | 否,在 `public` |
| 零停机方式才要求的项(§2,不采用) | `wal_level=replica`(要求 logical);无主键 / 唯一键的表:`expert_work_dev.vector.rabitq_info`(阿里云 pgvector 扩展自带)、`keycloak.public.databasechangelog`(Keycloak 的 Liquibase 表);物化视图 / 外部表 / 大对象 0;序列 11 |

### 1.2 谁连这个库(升级窗口里要停的写方)

| 组件 | 类型 | 测试副本数 | 连接配置 |
|---|---|---|---|
| control-plane | Deployment | 2 | `control-plane-secrets` / `EXPERT_WORK_DB_DSN` |
| credential-proxy | Deployment | 1 | `credential-proxy-secrets` / `EXPERT_WORK_CRED_PROXY_DB_DSN` |
| keycloak | StatefulSet | 1 | `keycloak-secrets` / `KC_DB_URL` |
| langfuse-web、langfuse-worker | Deployment | 1、1 | `langfuse-secrets` / `DATABASE_URL` |
| retention-cleanup | CronJob(每天 03:23) | — | 用 `control-plane-secrets` / `EXPERT_WORK_DB_DSN` |
| migrate | Job(只在发版时) | — | 同 control-plane |

不连库:wecom-adapter、admin-ui、沙箱。四份连接配置指向同一个实例。
**生产要重新数一遍**(`kubectl -n expert-work get deploy,statefulset,cronjob` 后逐个看
`envFrom` / `env`),别照抄这张表。

control-plane 的存活检查 `/healthz/live` 不查库(停库时 pod 不会被反复重启,只会因
`/healthz/ready` 失败暂时摘流量);应用直连 RDS,没有 PgBouncer。

### 1.3 上层组件对 PG 17 的支持

- **Keycloak 25.0(集群里的版本)**:官方只测到 PG 16(25.0.6 release notes「The
  supported and tested databases now include PostgreSQL 16」;`pom.xml`
  `postgresql.version=16`);26.1 起测试版本为 17,26.4 起明列 `17.x`。没有已知的
  不兼容,但**官方没背书 → 升级后必须验登录**(§7)。要彻底消掉这条风险,另起一项把
  Keycloak 升到 26.4+(有破坏性变更,不随本次做)。
- **Langfuse 3.122.0**:「Langfuse supports Postgres versions >= 12」,无限制。
- 应用侧:asyncpg / SQLAlchemy / alembic 都不区分 16、17;过渡期规矩见 §0。

### 1.4 自己先跑的预检查(只读)

在一个带 control-plane 数据库配置的 pod 里跑。脚本从本机仓库读出来作为参数传进去,
镜像里不需要有这个文件。输出每行 `范围<TAB>检查项<TAB>值`,只有计数和设置,
不含数据和凭据。

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
POD=$(kubectl -n expert-work get pod -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[0].metadata.name}')
kubectl -n expert-work exec "$POD" -- python -c "$(cat tools/deploy/pg_upgrade/precheck.py)"
```

判据:没有 `ERROR`;复制槽、发布、订阅、两阶段事务、reg* 列、unlogged 表、collation
不一致都是 0;pgcrypto 不在 `pg_catalog`。

## 2. 选哪种升级方式

阿里云文档(「升级大版本」概述页)列了四种,前三种底层都是 `pg_upgrade`:

| | 本地升级 | 蓝绿部署(割接 / 不割接) | 零停机 | DTS 迁移 |
|---|---|---|---|---|
| 做法 | 原实例上升级 | 恢复出一个新实例再升级 | 原实例升级 + 逻辑复制追增量 | 手建新实例,逻辑复制迁移 |
| 原实例只读 | 分钟级(随对象数增长) | 割接:分钟级;不割接:无 | 秒级 | 秒级 |
| 回退 | 不能原地降级 | 保留原实例 | 切换前可取消;切换后从备份克隆 | 原实例在 |
| 费用 | 免费 | 新实例按量付费 | 免费 | 新实例 + DTS |
| 额外前提 | 云盘 | 新实例只支持 ESSD / 高性能云盘 | 云盘、`wal_level=logical`、小版本 ≥ 20250228、表要有主键、升级期间默认禁 DDL | 每个库一个任务、手动切地址 |

**测试和生产只用一种方式:本地升级。** 测试演练的就是生产要走的那一套 —— 换方式等于
测试没演练过(用户 10-21:「两套方案,无法保证生产环境升级没问题」)。

- 本地升级:免费、实例和连接地址不变,对象数小(测试约 3,000)只读是分钟级;
  回退靠升级前的自动全量备份(§8)。生产在低峰窗口提前通知,接受分钟级只读。
- **零停机不采用**:它靠逻辑复制追增量,要求每张表有主键 / 唯一键,而两张没有主键的表
  都不归我们管 —— `vector.rabitq_info` 是阿里云 pgvector 扩展自带的,
  `databasechangelog` 是 Keycloak 自己的 Liquibase 表;另外还要 `wal_level` 改 logical
  (改参数要重启)、升级期间默认禁 DDL。前提我们满足不了,也就没法在测试上演练。
- 蓝绿部署要新实例按量付费、新实例不继承备份和告警规则;DTS 一个库一个任务、手动切地址 ——
  都比本地升级多步骤,没有换来我们需要的东西。

## 3. 窗口前(T-1 天内)

1. **自查**:跑 §1.4,结果符合判据。
2. **控制台升级检查(用户)**:实例左侧「大版本升级」→「升级检查」页签 →
   「创建升级检查报告」→ 选目标版本 **17**、升级模式 **本地升级** →「确定」。
   检查期间实例状态为「维护实例中」。结果为「成功」或「警告」可以升级;警告建议按报告
   修完重跑到「成功」;「失败」必须修。**报告 7 天内有效**(API 错误码
   `InvalidUpgradePrecheckResult`)。检查成功后又建了插件,要重跑检查。
3. **确认前提**:实例存储类型是云盘(本地升级只支持云盘)、不是只读实例、没有只读实例
   挂着(有的话本地升级要先删)。
4. **定窗口**:避开 03:23 的 retention-cleanup;预留 60 分钟(停写 5 分钟 + 升级
   分钟级 + 验证 20~30 分钟);提前告知在用测试环境的人(包括对接方联调)。

## 4. 窗口开始:停写 + 升级前快照

本节和 §6 的输出存在仓库外的一个目录(例如 `~/pg-upgrade-<日期>/`),下面记作 `$OUT`。

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
NS=expert-work
OUT=~/pg-upgrade-$(date +%Y%m%d); mkdir -p "$OUT"

# 4.1 记下副本数(§7 照这个恢复)
kubectl -n $NS get deploy/control-plane deploy/credential-proxy deploy/langfuse-web deploy/langfuse-worker statefulset/keycloak \
  -o custom-columns=KIND:.kind,NAME:.metadata.name,REPLICAS:.spec.replicas | tee "$OUT/replicas.txt"

# 4.2 起工具 pod(control-plane 镜像 + 数据库配置,只 sleep,不接流量)
IMG=$(kubectl -n $NS get deploy control-plane -o jsonpath='{.spec.template.spec.containers[0].image}')
sed "s#__CONTROL_PLANE_IMAGE__#${IMG}#" tools/deploy/pg_upgrade/dbtool-pod.yaml | kubectl apply -f -
kubectl -n $NS wait --for=condition=Ready pod/pg-upgrade-dbtool --timeout=180s

# 4.3 暂停定时任务、停所有写方
kubectl -n $NS patch cronjob retention-cleanup -p '{"spec":{"suspend":true}}'
kubectl -n $NS scale deploy control-plane credential-proxy langfuse-web langfuse-worker --replicas=0
kubectl -n $NS scale statefulset keycloak --replicas=0
# control-plane 会等进行中的运行结束(最长约 5 分钟),等 pod 全部消失:
kubectl -n $NS get pods | grep -E '^(control-plane|credential-proxy|langfuse-web|langfuse-worker|keycloak)-' || echo "writers gone"

# 4.4 升级前检查 + 快照
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/precheck.py)" > "$OUT/precheck-before.tsv"
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/snapshot.py)" > "$OUT/snapshot-before.tsv"
awk -F'\t' '$2 == "client_connections"' "$OUT/precheck-before.tsv"
```

判据:写方 pod 全部消失;`client_connections` 只剩个位数(工具 pod 自己 + RDS 的
管理账号 `aurora` 的连接,测试实测 10 = 9 个 `aurora` + 1 个工具 pod;拿不准就在工具 pod 里
`select usename, datname, application_name, count(*) from pg_stat_activity group by 1, 2, 3`
看清是谁);两份输出没有 `ERROR`。**再隔 20 秒拍第二份快照,两份必须逐字一致** ——
这才证明写真的停了(测试实测一致)。快照约 3 秒,内容是每张表的精确行数 + 向量检索
(HNSW)和关键词检索各 3 条固定探针的前 10 个 id,不调模型、不含数据。

## 5. 升级(用户,控制台)

1. 实例「大版本升级」→ 目标版本 **17**、升级模式 **本地升级**、割接时间 **立刻** → 确认。
   - 升级会顺带把内核小版本升到最新(文档原文:「系统将自动更新至最新的内核小版本」)。
   - 规格低于推荐规格时系统会自动按推荐规格升级,多一次秒级闪断(文档没写是否计费)。
   - 升级任务创建后不能修改或删除。
2. 系统在升级前后各自动做一次全量备份(文档原文:「在升级前后,均会对实例进行全量备份」)。
   在实例的备份列表里记下**升级前那份备份的时间**,回退用(§8)。
3. 等实例回到「运行中」、版本显示 17。

测试实测时间线(2026-10-21):14:48 点升级 → 14:54:43 进入只读(前 6 分钟在做升级前备份等,
库照常可读写)→ 14:59:30 切到 17.11 并可读写。**只读 4 分 47 秒;每 5 秒一次的连接探测全程
没有连不上过。** 内核小版本随之从 20260630 升到 20260915。窗口里可以在工具 pod 里每 5 秒探一次
`show server_version` + `current_setting('transaction_read_only')`,记下只读起止。

## 6. 升级后检查(写还停着)

```bash
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/precheck.py)" > "$OUT/precheck-after.tsv"
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/snapshot.py)" > "$OUT/snapshot-after.tsv"
# 第一行是版本号,其余必须逐行一致:
diff <(tail -n +2 "$OUT/snapshot-before.tsv") <(tail -n +2 "$OUT/snapshot-after.tsv") && echo "snapshot identical"
# 重建统计信息(pg_upgrade 不带过去;控制台是否自动收集文档没写):
kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/analyze.py)" | tee "$OUT/analyze.tsv"
```

判据:

- `precheck-after`:`server_version` 17.x;扩展清单与升级前相同(版本号可能随 17 的
  插件版本变,如 `vector`);collation 不一致 0;没有 `ERROR`。
- 快照:**行数行必须全部一致**,不一致 = 停下来查,不恢复写。关键词探针行也必须一致。
- 向量探针行不一致时,不要直接判坏,先请精确结果当裁判:
  `kubectl -n $NS exec pg-upgrade-dbtool -- python -c "$(cat tools/deploy/pg_upgrade/exact_knn.py)"`
  (同样的种子和查询,关掉索引走全表扫描算真正的前 10),逐条算升级前 / 升级后与精确结果的重合度。
  **升级后 ≥ 9/10 → 继续**(测试实测:升级后 6 条全部 10/10,升级前只有 1~6/10,
  差异是升级修好了,不是弄坏了 —— 见 B-208);升级后低于 9/10 → 停下来查,不恢复写。
- `analyze`:每个库 `ok`,`tables_without_stats=0`(测试 16 上实测主库 0.9 秒)。
- 向量检索准确度:恢复写之后跑 `tools/deploy/pg_upgrade/vector_recall_check.sh <环境>`(只读,只出数字)。
  `[A]` 段(强制走 HNSW)必须 100%;`[B]` / `[C]` 是应用真实检索形态,也必须 100%。测试 17.11 实测全部 100%;
  升级前的生产 16.14 实测 `[A]` 只有 60%(B-208)—— 生产升级后这一行必须变成 100%,否则停下来查。

**顺带装 `pg_textsearch`(在窗口内做,会自动重启一次)**:阿里云文档没写它要不要预加载,
**测试实测要**:控制台「插件管理」给 `expert_work_dev` 安装 `pg_textsearch` 后,
`shared_preload_libraries` 自动多了 `pg_textsearch`,实例自动重启了一次(15:06:14)。所以必须
趁写还停着装。装完确认:`select extversion from pg_extension where extname = 'pg_textsearch'`
为 1.3.1;再拍一次快照,与升级后那份逐字一致(测试实测一致)。可选的功能自检:在一个事务里
建临时表 + `USING bm25 (body) WITH (text_config = 'simple')` 索引、`ORDER BY body <@> to_bm25query(...)`
查一次、然后回滚(测试实测:相关行分数为负、越小越相关,不相关为 0,回滚后不留对象)。

## 7. 恢复写 + 验收

```bash
# 按 $OUT/replicas.txt 恢复(测试:control-plane 2,其余 1)
kubectl -n $NS scale statefulset keycloak --replicas=1
kubectl -n $NS scale deploy control-plane --replicas=2
kubectl -n $NS scale deploy credential-proxy langfuse-web langfuse-worker --replicas=1
for r in statefulset/keycloak deploy/control-plane deploy/credential-proxy deploy/langfuse-web deploy/langfuse-worker; do
  kubectl -n $NS rollout status "$r" --timeout=600s
done
kubectl -n $NS patch cronjob retention-cleanup -p '{"spec":{"suspend":false}}'
kubectl -n $NS delete pod pg-upgrade-dbtool

tools/deploy/smoke.sh test                  # SMOKE PASS(含 Keycloak OIDC、Langfuse 两行)
tools/deploy/pg_upgrade/run_canary.sh test  # 真跑一轮 + 产物下载,RESULT: PASS
```

再验三条产品路径:

1. **登录(用户)**:浏览器登录控制台,退出再登一次;进一个智能体发一句话。
   Keycloak 25 官方没测过 PG 17,这条不能省。
2. **Langfuse**:金丝雀那一轮结束 1 分钟后,会话详情「计时」页的「Langfuse 精确」列有
   数据(做法同 `pending-test-release-acceptance.md` §39.2 / §39.3)。
3. **知识库**:控制台「检索测试」对测试知识库查一句,能返回结果(SQL 层的一致性 §6 已验,
   这里验产品链路)。

次日:`kubectl -n expert-work get job` 里 03:23 那次 retention-cleanup `COMPLETIONS 1/1`。

## 8. 回退

- **升级任务失败**:写还停着,数据不受影响;按控制台任务详情处理,必要时提工单。
  恢复写之前先确认实例状态与版本。
- **升级成功但验收不过、要回到 16**:本地升级不能原地降级(文档原文:「升级后暂不支持
  进行大版本降级操作」)。做法:
  1. 用 §5 记下的升级前全量备份「恢复到新实例」(版本 16);白名单 / 安全组按原实例配;
  2. 把 §1.2 的四份连接配置改成新实例地址(`control-plane-secrets`、
     `credential-proxy-secrets`、`keycloak-secrets`、`langfuse-secrets`);
  3. 重启 §1.2 的全部写方,跑 §7 验收。
  - 窗口内写是停着的,所以只会丢验收期间产生的数据。恢复新实例要多久文档没写,
    随数据量(测试 4.6 GB)。

## 9. 生产重放(随 B-206 生产发布)

1. 生产先跑 §1.4 自查(生产 kubeconfig `~/.kube/expert-work-prod.yaml`),再跑控制台升级检查。
2. 重新数生产的写方(§1.2 末尾的命令),副本数以生产实际为准。
3. 窗口选低峰,提前通知生产用户(含对接方)会有分钟级只读;方式与测试相同 —— 本地升级(§2),不换方式。
   生产的对象数以 §1.4 自查为准,数量级与测试不同时先停下来重估只读时长。
4. 顺序:§4 停写 → §5 升级 → §6 检查(含 `pg_textsearch`,若测试装了)→ §7 恢复写 + 验收
   (含 `vector_recall_check.sh prod`,`[A]` 段从升级前的 60% 变成 100%)→ 再用 `release.sh prod` 发 B-206 版本。库的升级和应用的发布分开验收,出问题好定位。
5. 执行记录写进 §10 生产一栏。

## 10. 执行记录

| 环境 | 日期 | 窗口起止 | 只读时长 | 快照比对 | `pg_textsearch` / 预加载变化 | 问题 |
|---|---|---|---|---|---|---|
| 测试 | 2026-10-21 | 14:45:55 停写 → 15:15:20 恢复写(约 30 分钟);15:2x 用户验登录 / Langfuse / 检索测试正常 | 14:54:43–14:59:30,**4 分 47 秒**;连接探测全程可连 | 行数 / 关键词探针逐行一致;向量探针升级后 = 精确 10/10(升级前 1~6/10,B-208) | 插件管理装 1.3.1 → 自动加入预加载 + 自动重启 1 次;BM25 功能自检通过 | 无阻塞;用户在窗口中途登录失败 = Keycloak 停着,预期内 |
| 生产 | | | | | | |

## 出处(2026-10-21 读原文)

- 阿里云 RDS PostgreSQL「升级大版本」概述(更新 2026-07-30)、「本地升级」(2025-12-29)、
  「蓝绿部署」(2026-04-20)、「零停机」(2026-08-07)、「大版本升级检查报告解读」(2026-09-24)、
  API `UpgradeDBInstanceMajorVersion` / `UpgradeDBInstanceMajorVersionPrecheck`、
  「支持的插件列表」(2026-09-23)、「AliPG 内核小版本发布记录」(2026-09-23)、「管理插件」(2026-08-14):
  <https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/upgrade-the-major-engine-version-of-an-apsaradb-rds-for-postgresql-instance/>
  及同目录各页。
- `pg_textsearch` 上游:<https://github.com/timescale/pg_textsearch>(README:「supports PostgreSQL 17 and 18」,
  要求 `shared_preload_libraries`)。
- Keycloak 25.0.6 release notes:<https://www.keycloak.org/docs/25.0.6/release_notes/index.html>;
  当前版本数据库页:<https://www.keycloak.org/server/db>。
- Langfuse 自托管 Postgres 要求:langfuse-docs 仓库 `content/self-hosting/deployment/infrastructure/postgres.mdx`。
