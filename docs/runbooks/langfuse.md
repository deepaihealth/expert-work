# Runbook — Langfuse (agent trace 调试)

Self-hosted Langfuse v3 用于看每次 LLM 调用的 prompt / completion / token /
latency / 错误时间线。背景见 [ADR-0005 §6 Mini-ADR OBS-L1](../adr/0005-observability-stack.md)。

- **范围**：本 runbook 只覆盖 **dev 本地栈**。生产部署（K8s/Helm）押到真要对
  生产开放可观测那天另立。
- **PII**：prompt/completion 在**入库前**由 `langfuse_sdk` 的 `mask=` 回调脱敏
  （secrets + 对话 PII，默认开）。到 ClickHouse 的已是脱敏态。

---

## 1. 起栈

Langfuse 走 `observability` profile，默认 `docker compose up` 不启。

```bash
cd infra
docker compose --profile observability up -d \
    langfuse-postgres langfuse-clickhouse langfuse-redis \
    langfuse-minio-init langfuse-worker langfuse-web
# 首启 langfuse-web 会跑 DB + ClickHouse migration,约 30–60s。
docker compose logs -f langfuse-web   # 等 "Ready" / 监听 3000
```

UI：<http://localhost:3001>（登录 `dev@expert_work.local` / `expert_work_langfuse_dev`，
可用 `EXPERT_WORK_LANGFUSE_INIT_*` 覆盖）。

依赖的 `minio` 是核心服务（非 profile-gated），若没起先 `docker compose up -d minio`。

## 2. API key（dev 已自动预置）

`langfuse-web` 用 `LANGFUSE_INIT_*` 自动建好 org `Expert Work` + project `Expert Work (dev)` +
固定 key，无需点 UI：

| 项 | dev 默认值 | 覆盖 env |
|----|-----------|----------|
| public key | `pk-lf-expert-work-dev` | `EXPERT_WORK_LANGFUSE_PUBLIC_KEY` |
| secret key | `sk-lf-expert-work-dev` | `EXPERT_WORK_LANGFUSE_SECRET_KEY` |

> 任何共享环境务必覆盖这些 key 和 §5 的安全默认值。

## 3. 让 control-plane 推送 trace

control-plane 三个 settings 配齐才真推（否则退化成 M0 内存桩，看不到东西）。
env 前缀是 `EXPERT_WORK_`（`control_plane.Settings` 的 `env_prefix`）。写进
`infra/.env`（compose 自动读取，gitignored）：

```bash
# control-plane 在 compose 内 → 用服务名;在宿主跑 → 用 localhost:3001
EXPERT_WORK_LANGFUSE_HOST=http://langfuse-web:3000
EXPERT_WORK_LANGFUSE_PUBLIC_KEY=pk-lf-expert-work-dev
EXPERT_WORK_LANGFUSE_SECRET_KEY=sk-lf-expert-work-dev
# PII 脱敏默认开;仅应急排查原始 prompt 时显式关:
# EXPERT_WORK_LANGFUSE_PII_MASKING_ENABLED=false
```

> compose 的 control-plane `environment:` 块已透传这四个 `${...}`（Mini-ADR
> OBS-L1）——所以放 `.env` 即生效。`.env.example` 另加一份占位作文档登记。

重启 control-plane 后看日志 `langfuse.enabled host=... pii_masking=True`。

## 4. 验证

1. 跑一次真 agent run（需真模型 key —— CI 无凭证,只能本地/SE 验）。
2. Langfuse UI → project `Expert Work (dev)` → Traces，应出现一条 generation，
   含 input messages / output / model / token usage。
3. **PII 脱敏自检**：prompt 里故意放 `测试 alice@example.com 13812345678`，
   UI 里这两个值应显示成 `***REDACTED***`。
   - 若**没**脱敏 → 命中 Mini-ADR OBS-L1 的待验证项：langfuse v3 的 `mask`
     调用时机 vs BaseMessage 序列化时机不匹配。回退：在
     `LangfuseSdkClient.start_span` 入口先把 messages 规整成 dict 再交 SDK。
4. trace_id 与 Tempo 共享（OTel context），可在两边互跳。

## 5. 安全默认值（生产必须覆盖）

compose 里全是 **clearly-dev** 默认，生产部署务必经真 env 覆盖：

| env | 用途 |
|-----|------|
| `EXPERT_WORK_LANGFUSE_SALT` | Langfuse 哈希 salt |
| `EXPERT_WORK_LANGFUSE_ENCRYPTION_KEY` | 64 hex（32 字节）；dev 默认全 0 |
| `EXPERT_WORK_LANGFUSE_NEXTAUTH_SECRET` | UI session 密钥 |
| `EXPERT_WORK_LANGFUSE_DB_PASSWORD` / `EXPERT_WORK_LANGFUSE_CLICKHOUSE_PASSWORD` | 后端密码 |
| `EXPERT_WORK_LANGFUSE_PUBLIC_KEY` / `EXPERT_WORK_LANGFUSE_SECRET_KEY` | project API key |

生成 ENCRYPTION_KEY：`openssl rand -hex 32`。

## 6. 关栈 / 重置

```bash
docker compose --profile observability stop langfuse-web langfuse-worker \
    langfuse-clickhouse langfuse-postgres langfuse-redis
# 彻底清数据(含历史 trace):
docker compose --profile observability down
docker volume rm infra_langfuse-postgres-data infra_langfuse-clickhouse-data
```

## 7. 故障排查

- **web 起不来 / migration 卡**：先确认 `langfuse-postgres` /
  `langfuse-clickhouse` healthy（`docker compose ps`）。ClickHouse 首启慢。
- **trace 不出现**：control-plane 日志若是 `langfuse.disabled — settings
  incomplete` → §3 三个 settings 没配齐。
- **bucket 报错**：`langfuse-minio-init` 应 `completed`；没有则手动
  `mc mb m/langfuse`（见 [infra/README](../../infra/README.md) bucket 段）。
- **队列丢事件**：确认连的是 `langfuse-redis`（noeviction）而非 quota redis。
