# Bootstrap 第一个平台管理员(system_admin)

> **生产推荐:邮箱首登自动升(Stream ACCT)。** 部署时设 env
> `EXPERT_WORK_BOOTSTRAP_ADMIN_EMAIL=<运维负责人邮箱>`。该邮箱用户**首次**用
> 已验证(`email_verified=true`)的 Keycloak JWT 登录、且系统**当前零 platform admin**
> 时,自动获 `system_admin` 绑定 —— 无需跑下文脚本。**安全闸**:一旦系统存在任一
> platform admin(脚本或 API 授的),此自动升永久不再触发,不能用于事后提权。
> 之后用 admin-ui「平台管理员」页(`/settings/platform-users`)自助增删 admin。
> 本脚本(`python -m control_plane.bootstrap_admin`)退为 **break-glass** 兜底
> (env 不便 / 邮箱不可用 / 需指定非邮箱主体时)。

> **本地 dev 已自动化**:`infra/Makefile` 的 `make dev-up` 末尾自动跑 `make dev-bootstrap-admin`
> (幂等),把 dev 用户提权为 system_admin —— 本 runbook §1–2 的手动步骤已封装进该 target
> (查 dev 用户 id → 容器内跑 `bootstrap_admin`)。单独补跑:`cd infra && make dev-bootstrap-admin`。
> 下文是机制说明 + 生产/手动路径。

> Stream P / Mini-ADR P-6。解决**鸡生蛋**:建 platform-scope role binding 需要
> `is_system_admin`(`api/role_bindings.py`),而 `resolve_system_admin` 又只在该
> binding 已存在时才报 `is_system_admin=True` —— 空 `role_binding` 表里没人能授第一个 admin。
>
> 本 runbook 用 `python -m control_plane.bootstrap_admin` **直插 DB 一条 platform binding**
> 打破死锁。它没有 HTTP/JWT 入口,只靠 DB 直连权限(ops 控制),**每个部署跑一次**;
> 之后所有授权走审计化的 `POST /v1/role_bindings`,不再需要本脚本。
>
> **本 runbook 的 §1（起 Keycloak `--profile auth` → 取 token）+ §3（`/v1/me` Bearer 验证）
> 即 Stream C.1 OIDC + JWT 的端到端验收路径**（OIDC 登录 → JWKS 验签 → 受保护端点鉴权）。

---

## 0. 前置

```sh
cd infra
# 数据层 + control-plane(full)+ Keycloak(auth)
docker compose --profile full --profile auth up -d
# 等就绪
curl -sS http://localhost:8000/healthz/ready | python3 -m json.tool   # status: ready
```

迁移必须已 apply(`role_binding` 表存在):若 control-plane 未自动迁移,
`docker compose exec control-plane-blue alembic upgrade head`。

---

## 1. 取 dev 用户的 subject id(UUID,**不是 email**)

`bootstrap_admin` 要的 `--subject-id` 必须等于 Keycloak 用户的 JWT `sub` claim
(`resolve_system_admin` 用 `UUID(principal.subject_id)` 匹配;填 email 会被静默跳过)。

dev realm(`infra/keycloak/realm-expert-work.json`)预置用户 `dev` / 密码 `devpass`,
但其 `id` 由 Keycloak 导入时生成,需查一次:

**方式 A — Keycloak Admin Console(推荐,可靠)**
1. 开 `http://localhost:8080`,用 `admin` / `admin_dev` 登录(compose env `EXPERT_WORK_KEYCLOAK_ADMIN(_PASSWORD)`)。
2. 左上 realm 切到 `expert-work` → Users → 点 `dev`。
3. 复制 **ID** 字段(形如 `3f8c…` 的 UUID)—— 这就是 `sub`。

**方式 B — 服务账号 Admin API(可脚本化)**
```sh
# admin-ui 是 PKCE 公有客户端,已禁用 password grant。改用服务账号
# expert-work-api-internal(client_credentials + manage-users)查用户 id:
SA_TOKEN=$(curl -sS -X POST \
  http://localhost:8080/realms/expert-work/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=expert-work-api-internal \
  -d client_secret=dev-internal-secret-rotate-me | jq -r .access_token)
curl -sS "http://localhost:8080/admin/realms/expert-work/users?username=dev&exact=true" \
  -H "Authorization: Bearer ${SA_TOKEN}" | jq -r '.[0].id'
```

---

## 2. 跑 bootstrap

```sh
cd /Users/mac/src/github/jone_qian/expert-work
# DSN 默认取 Settings.db_dsn(本地 postgresql+asyncpg://…@localhost:6432/expert_work_dev);
# 也可 --dsn 覆盖或 export EXPERT_WORK_DB_DSN=…
uv run python -m control_plane.bootstrap_admin --subject-id <上一步的 UUID>
```

期望:
```
OK: created platform system_admin binding <binding-uuid> for <subject-uuid>
```
再跑一次同 subject 是幂等的:
```
OK (idempotent): <subject-uuid> already holds platform system_admin
```

---

## 3. 验证

登录 admin-ui(见 §4)后:
```sh
curl -sS http://localhost:8000/v1/me -H "Authorization: Bearer <token>" | jq '{is_system_admin, allowed_tenants}'
# 期望:{"is_system_admin": true, "allowed_tenants": "*"}
```

之后即可建租户(`POST /v1/tenants`)、授更多 admin(`POST /v1/role_bindings`
`{platform_scope: true, role: system_admin}`)—— 都走 API,不再用本脚本。

---

## 4. 登录 admin-ui(本地)

- admin-ui dev server 默认端口 **5173**(`apps/admin-ui/vite.config.ts`)。
- dev realm 的 `expert-work-admin-ui` client `redirectUris` / `webOrigins` 已对齐到
  `http://localhost:5173/*`(Stream R W4),5173 起前端直接走 OIDC 授权码流即可。
- 配 OIDC env(`apps/admin-ui/.env.development.local`):
  ```
  VITE_OIDC_ISSUER=http://localhost:8080/realms/expert-work
  VITE_OIDC_CLIENT_ID=expert-work-admin-ui
  ```
- 登录后 `/v1/me` 返回服务端真实 `is_system_admin`(本地解码的 roles 不可信)。

---

## 5. dev 跑真实 agent turn:配真 LLM key(Mini-ADR P-13)

`infra/mock-upstream` 只是 egress e2e 的 echo server,**不是 mock LLM**;dev 跑出真实
agent 回话需要一个真 provider key。

**key 值只活在 SecretStore 里,绝不入仓、绝不靠进程 env。** 关键机制:dev 用
`local_dev` SecretStore,它**只读一个 `name=value` 文件**(`EXPERT_WORK_SECRET_STORE_ENV_FILE`),
**不读 `ANTHROPIC_API_KEY` 这类进程环境变量**。所有 key 最终都经
`secret_store.get(<name>)` 取值,其中:

- **agent 主对话模型** ← manifest 的 `model.api_key_ref`(canonical manifest 写的是
  `secret://expert-work/dev/llm/anthropic-api-key`)直接走 SecretStore。
- **embedder(Phase 2 长记忆)/ reranker / web_search** ← 平台/租户凭证
  `resolve_provider`/`resolve_tool` 返回一个 `secret://…` ref,**同样**再过一次 SecretStore 取值。

所以无论哪条路,真 key 都要落进那个文件:

```sh
# 1. 拷模板 → 填真 key(.local 已被 *.local gitignore,绝不提交)
cp infra/dev-keys/dev-llm-keys.example infra/dev-keys/dev-llm-keys.local
$EDITOR infra/dev-keys/dev-llm-keys.local
#   expert-work/dev/llm/anthropic-api-key=sk-ant-<真key>      # ← 主对话模型(必填)
#   # 跑 Phase 2 记忆 / web_search 时,再把 embedder / tavily 的 ref 名也填进来

# 2. 起栈:compose 已把 infra/dev-keys 挂进 control-plane 并设好
#    EXPERT_WORK_SECRET_STORE_ENV_FILE(见 infra/docker-compose.yml)
cd infra && docker compose --profile full up -d
```

> embedder / web_search 的**平台级 provider/tool 凭证**(即那些 `secret://…` ref 名)
> 可在**平台配置页 `/settings/platform`**(PR I)运行时填(DB 覆盖 env),但 ref 指向的
> **值**仍由上面的 SecretStore 文件提供。
> 无 key 时 dev 只能验证到 API / 结构层,跑不出真实 LLM 回话(Phase 2/4/5 真实推理依赖真 key)。

---

## 6. 安全

- 脚本只在 `EXPERT_WORK_DB_DSN` 指向可写 DB 时能跑 —— 受网络 + DB 凭证(ops 掌握)天然门控,无 HTTP 暴露。
- 它只写 **一行** binding,不删、不提权既有行。
- 这是**唯一**绕过"is_system_admin 才能授 is_system_admin"的代码路径,且需 infra 级访问。生产:从受控 ops 主机/跳板跑一次,用后收紧 DB 凭证;之后授权全走审计化 API。

---

## 成员开通模式(member_provisioning_mode)

`EXPERT_WORK_MEMBER_PROVISIONING_MODE`:

- `email`(默认):Keycloak set-password 邮件,依赖 realm SMTP 配置。
- `password`:无 SMTP 依赖。建租户(带首管)/邀请成员时服务端生成
  `word-word-word-NNNN` 初始密码写入 Keycloak(temporary,首登强制改密),
  密码只在那一次创建/重发响应与 admin-ui 的一次性面板里出现——不落库、
  不进审计、不进日志,关掉面板后唯一找回方式是「重发」(重新生成)或
  成员页「重置密码」。测试集群当前为 `password`(2026-08-10 起)。

`email` 模式时代创建的账号,在切到 `password` 模式后走「重发」能拿到临时密码,
但其 Keycloak `emailVerified` 仍为 `False`——当前 realm 未开强制邮箱验证,所以
不拦登录;若将来开 realm 级 `verifyEmail`,这批账号会因无 SMTP 被锁死,切模式前
需批量补 `verified` 标记。
