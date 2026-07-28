# 数据库与 Redis 选型(2026-07-28)

前提:集群=ACS(杭州)、沙箱=Agent Sandbox、对象存储=OSS、工作区=NAS 主候选均已拍板。本文=RDS PostgreSQL 与云 Redis 的选型结论。完整调研证据在 2026-07-28 会话;价格为文档公开口径,购买前控制台核实。

## RDS PostgreSQL(已拍板)

**版本:三环境统一钉 PG 16。**
- pgvector 仅 PG14+ 支持(RDS 上 0.8.0);我们 memory/knowledge 重度依赖(HNSW/cosine)。
- 本地 compose 镜像 `pgvector/pgvector:pg16` → 云上 16 = 零版本漂移。
- 中文全文检索 = 应用侧 jieba 分词 + `simple` tsvector 配置,**无 zhparser 等云上难装扩展的依赖**(当年设计红利)。
- 扩展需求:pgvector / pg_trgm / uuid-ossp / pg_stat_statements(后者需 RDS 参数模板 shared_preload_libraries)。

**实例形态:**
| 环境 | 形态 | 规格 | 成本 |
|---|---|---|---|
| 测试 | 基础系列(单节点,无 HA/无 PITR,官方定位即"开发测试环境") | 2c2g 通用型 pg.n1e.2c.1m + 40GB 高性能云盘 | 36 元/月,包年含存储 775 元(已购配置核过:PG16.0/专有网络/VPC 白名单) |
| 生产 | 高可用系列(主备,切换 ≤15s,PITR) | 2c4g 通用型起步(400 连接 vs 需求 40-80) | ~350 元/月量级 |

Serverless 弃选:基础版 36 元/月已够便宜,且无自动暂停唤醒延迟。

**直连命门(checkpointer prepared statements)官方架构解决:**
- RDS 5432 原生直连端口恒在;内置 PgBouncer 开启走 6432(默认 transaction 模式),两地址并存互不影响(官方原文)。
- 本地 compose 已是双 DSN 架构(`EXPERT_WORK_DB_DSN`→pgbouncer:6432 / `EXPERT_WORK_CHECKPOINTER_DSN`→postgres:5432 直连)→ **云上同构照搬,零代码改动**。

**部署清单项:**
- 连接池 connect_timeout 1-2s + 自动重连(主备切换断连,长连接不配超时会挂数百秒)。
- 白名单=VPC 网段;全程内网,不开公网。
- 小版本自动升级有 ~30s 闪断,设维护窗口。

## 云 Redis(已拍板)

**选型:社区版(非 Tair)+ 标准架构(非集群)+ 版本 7.0。**
- 官方选型页"成本优先"档 = 社区版/标准架构/不启用集群,正是我们画像(限流令牌桶+配额计数,MB 级数据,千级 QPS)。
- Tair 三形态(性能增强/持久内存 4GB 起/磁盘型)全部过剩;持久内存型虽命令级持久化但只支持 6.0(与本地 redis:7 漂移)且规格高一个量级——配额是预扣非账本,不值。
- 7.0 与本地 redis:7 钉死;EVAL/EVALSHA 全支持;**Redis 7 Functions 云上不支持**(我们未用);分片 pub/sub 仅 7.0(第 2 波 SSE 广播备用)。
- 标准架构单分片,无集群 Lua 跨 slot 限制(我们全部单 key 脚本,本就无碍)。

**硬要求闭环:`maxmemory-policy` 全版本可配 `noeviction`,免重启——建实例后第一步改**(默认 volatile-lru)。配额键永不被驱逐;代价=内存满拒写,监控水位 <80%。

**持久化:**默认 AOF everysec + 每日 RDB + 主备复制,理论丢失窗口 ~1s;配额/令牌桶可自愈(账本在 PG),够用。

**实例形态:**
| 环境 | 形态 | 成本 |
|---|---|---|
| 测试 | 标准架构·单节点·256MB~1GB·按量(无 SLA/无备份,测试专用) | 45~91 元/月 |
| 生产 | 标准架构·高可用双副本·1GB·包年 + noeviction | ~150 元/月量级 |

**明确否决:测试/生产共用一实例分 DB**——内存/CPU/参数/pub-sub 全实例级共享,测试塞爆内存 → 生产 noeviction 拒写 = 测试打挂生产;FLUSHDB 误操作半径;pub/sub 跨 DB 全局。45 元/月的隔离费必须花。本地开发"逃生舱"可连测试实例(风险同级)。

**部署清单项:**
- 建实例后改 noeviction;内存水位告警。
- 客户端重连/重试(主备切换 RTO ~30s,连接闪断);NOSCRIPT 兜底已有(redis_impl.py/redis_quota.py 均为 evalsha 失败→script_load 重载)。
- VPC 白名单同 RDS;内网不开 TLS(可选加固)。
