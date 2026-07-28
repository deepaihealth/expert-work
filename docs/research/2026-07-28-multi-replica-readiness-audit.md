# 多副本就绪审计(2026-07-28)

前提(用户拍板):**上线即多副本 + 弹性扩容**;Postgres/Redis/对象存储用阿里云托管(基础设施成本视为零);目标是上线前改完,不接受线上救火。启动时机:等 gate-obs-followup PR 收尾后开工;届时用户提供阿里云云产品文档做适配。

三路侦察(运行面 / 限流配额 / 存储)的完整 file:line 证据在 2026-07-28 会话;本文是行动清单蒸馏。

## 已就绪(当年做对了,不用动)

- CAS 家族:run 队列 `claim_queued`、孤儿接管 `reclaim`+heartbeat 自杀、审批 `mark_decided`、触发器三通路(`claim_cron_fire`/`claim_retry`/`claim_reconcile`)、配额预留 `FOR UPDATE`、知识摄取 `SKIP LOCKED`+lease、QualityDrift/OAuth 刷新 advisory lock。
- 跨副本工作区写锁 `PgWorkspaceLock`(pg_advisory_xact_lock)。
- 蓝绿 stateless control-plane;migrate 独立一次性容器;checkpointer setup 竞态重试;catalog seed 幂等。
- Redis 限流切换机制(`single_instance=false` + `quota_redis_url` → RedisTokenBucketLimiter/RedisQuotaService)**代码已在**,只差配置与守卫。
- 对象存储抽象 S3v4(`storage/factory.py` docstring 直接点名 Aliyun OSS 配法)。
- 配置类 TTL 缓存家族全部自带多副本语义声明(kill-switch 5s / 各 platform config 30s / tenant config 60s)——有意退化,不改。

## 第 0 波:部署配置清单(不改代码;漏一条即事故)

1. `EXPERT_WORK_SINGLE_INSTANCE=false` + `EXPERT_WORK_QUOTA_REDIS_URL`(settings.py:87 默认 true;compose/ha-e2e 均未设;ratelimit/in_process.py:7 声称的启动守卫**不存在**——第 1 波补真守卫)。
2. `CHECKPOINTER_BACKEND=postgres` + **直连** DSN 不走连接池代理(AsyncPostgresSaver prepared statements;不设则 InMemorySaver,跨副本接管假成功,app.py:1189-1197)。
3. 五服务对象存储后端统一 `s3-compatible`(control-plane/supervisor/retention-job 默认 `memory`;memory 下 skill_asset_store 置 None 即功能禁用 app.py:1363-1370);supervisor 的 AK/SK 是明文 env 非 secret ref(顺手改)。
4. 配额 Redis 禁 `allkeys-lru`(dev compose 是 lru,配额键可被驱逐)。

## 第 1 波:多副本正确性修复(一个 PR 批,机械可并行)

1. **Webhook 投递 CAS 领取**——`webhook/sql.py:312-333 list_ready` 无领取无 lease,N 副本对外重复 POST ×N;熔断器进程内。最严重(对外副作用)。
2. **MemoryDLQWorker 加 claim**——`memory/dlq.py:254-291` 裸取+无条件 attempts+1,重复 embed 烧钱 + 重试预算 ×N 烧穿误判死信;且无开关永远启动。
3. **MemoryConsolidator/SkillCurator 加 advisory lock**(照 quality_drift_worker.py:192 先例)+ `enable_scheduler`/`enable_curation_worker`/`enable_reaper` 从 create_app 函数参数改 env 可控(app.py:542-544,main.py 裸调,生产关不掉)。
4. **apikey 限流桶 HMAC 盐共享化**——middleware/rate_limit.py:51 进程随机盐,切 Redis 后各副本仍各算各键。
5. **Redis 故障策略**——rate_limit.py:101/tenant_rate_limit.py:116/_quota_admission.py:71 裸穿 RedisError→500;补 fail-open(health.py:50 与 redis_quota.py docstring 承诺过未实现)。
6. **PENDING run 回收**——list_orphans 只看 running(store.py:1130-1140);create→RUNNING 窗口副本崩则永久卡。
7. TOCTOU 双处:`redis_quota.py:135-150 reserve_tokens` 月预算超发;`api/curation.py:113-125` eval 数据集配额。
8. OrphanSweep 保守失败路径(orphan_sweep.py:200-212 `_fail_orphan` 经无守卫 set_status)——N 份重复 ERROR+audit。
9. **文档上传改对象存储**——uploads.py:158-163 走 supervisor write_workspace_file 写本机卷(图片早已走对象存储,文档是漏网);多节点读不到。
10. 真·single_instance 启动守卫(见第 0 波 1)。
11. LangGraph store `AsyncPostgresStore.setup()` 补竞态重试(store/factory.py:77,checkpointer 同款问题已修它没修)。

## 第 2 波:两个中型设计题(各自小设计后独立 PR)

- **live SSE 跨副本**:非终态 run 的 `/events` 路由到非执行副本 → `InMemoryStreamBridge.subscribe` 静默建空流,**永久挂空只吐心跳收不到 end**(memory.py:61-65,112-143 + api/runs.py:1475-1489);Redis 后端 factory 里 NotImplementedError 占位。两案已在 docs/research/2026-06-16-9.4-9.5-ha-failover-design.md:80-83(b1 轮询 durable 事件库 / b2 Redis pub-sub);PR3 的 run_event 批写为 b1 打好了地基。几千用户下最用户可感。
- **供应商 RPM 全局化**:`rate_limit_rpm` 进程内 AsyncLimiter 挂 BuiltAgent 挂进程 LRU(rate_limit.py:57-97 + agent_factory.py:1964-1970),实际上游压力=配置×N×fallback-key 数,扩容打穿账号配额;接入已有 Redis 令牌桶加 per-provider-key 维度。LLM 熔断器(llm_error_handling.py:220)同性质顺带评估。
- (搭车可选)agent 构建缓存/凭据缓存/MCP 池的**跨副本失效广播**(现全是本进程 hook,旧 graph 最长 30min、旧 key 最长 5min、MCP 目录变更旧连接);有 Redis pub/sub 基建后顺带,不做也有 TTL 兜底。

## 第 3 波:沙箱/工作区多节点(架构级独立 epic,先 brainstorm)

从未有多节点设计(代码/文档明标 M0 单机、跨 host 推 M1):

- supervisor 挂宿主 docker.sock 只能管本机(docker_client.py CLI 子进程,无 DOCKER_HOST 参数化);control-plane 只配单个 supervisor URL(settings.py:167);`sandbox_instance.node` 写死 "local" 且无人查询。
- 用户工作区 = 宿主本机 Docker 卷(`expert-work-ws-{t}-{u}`),run 换节点即数据分叉成两份空卷;热会话表/exec 锁/warm pool 全进程内存(supervisor.py:159-166)。
- reaper/daily-backup 每副本各跑无选主,会跨节点误杀容器/重复归档;归档单发 buffer 上限 1.5GiB < 10GiB 工作区配额(settings.py:163 vs :139),超限永远归档失败进 DLQ。
- 方向候选:每节点一 supervisor + 注册/节点感知路由(node 列真用起来),或按 docs 既定路线 K8s + RuntimeClass=gvisor;工作区权威搬 OSS/NAS + 按需水化。**方案须正经设计拍板。**

## 明确不改(记录立场)

per-run 预算全家(WorkerSpawnBudget/TokenBudget/上传限制)、DelegationGate per-replica(2026-07-28 调研裁决:Envoy local limit/Knative per-pod 同型,总容量随副本弹性是特性)、幂等重复做功类 worker(Curation/Feedback/TranscriptMirror/QualityMonitor,唯一约束兜底仅 ×N 白干)、skill_activity 写节流 ×N、backpressure per-process(设计意图)、TTL 配置缓存家族。
