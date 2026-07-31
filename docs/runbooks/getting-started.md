# 本地部署上手指南(dev / dogfood)

> 面向**第一次**把 expert-work 在本机跑起来、并跑通"一家公司从零用起来"完整闭环的人:
> 建公司 → 配 LLM key → 建首位管理员 → 管理员邀员工 → 员工登录用上 agent —— **全程网页操作**。
>
> **范围**:仅本地 dev / dogfood(macOS + Docker)。staging / prod 的发布、蓝绿、
> 回滚见 [`deployment.md`](./deployment.md);Gate 准入的完整验收(eval / 长记忆 /
> 审批门 / SLO)见 [`canonical-agent-e2e-test.md`](./canonical-agent-e2e-test.md)。
>
> 状态:🚧 边跑边写 —— 标 `【实跑回填】` 的小节会在本机真实跑通后补上真实输出与坑。

---

## 0. 你需要先准备的

| 准备项 | 说明 |
|--------|------|
| **Docker Desktop**(或 OrbStack / Lima)| 起整套 compose 栈 |
| **Node + pnpm** | 跑 Admin UI 前端(`apps/admin-ui`,host 上 `pnpm dev`,端口 5173)|
| **一个真 Anthropic key**(`sk-ant-…`)| agent 主对话模型 + 视觉都用它;在网页粘贴 |
| **浏览器** | Admin UI(登录 / 配 key / 建公司 / 邀员工 / Playground)|
| `git` / `curl` / `jq` / `openssl` / `python3` | 命令行工具(macOS 基本自带,`jq` 需 `brew install jq`)|

> 不需要本地装 Python 依赖就能起栈(服务都在容器里)。只有跑 eval(Gate Phase 1)才需要 `uv sync`。

---

## 1. 拉代码 + 配 `infra/.env`

```sh
git clone <repo-url> expert-work      # 已克隆则跳过
cd expert-work
git checkout main && git pull --ff-only
```

所有 dev 配置放在 **`infra/.env`**(git-ignored,`docker compose` 在 `infra/` 目录下自动加载)。
**不要**写进 `docker-compose.yml`(那个文件进 git,会泄露密钥)。

```sh
cd infra
cp .env.example .env
```

编辑 `infra/.env`,确认/填好这几项:

```ini
# —— 密钥金库:全程网页粘贴 LLM key,加密落库(Stream Q)——
EXPERT_WORK_SECRET_STORE_BACKEND=sql_encrypted
EXPERT_WORK_SECRET_ENCRYPTION_KEY=<下方命令的输出,base64 32 字节 KEK>

# —— 成员 onboarding:用真 Keycloak 建真账号(Stream R)——
EXPERT_WORK_KEYCLOAK_ENABLED=true

# —— 11.4/11.5 live eval worker:对真 agent 跑 adversarial/trace 评测(可选,E2E 用)——
# 默认全 OFF(enqueue API 仍能排队,只是没人 drain)。要在 /eval-runs 跑
# adversarial / trace_eval suite 时才开。EVAL_AGENT_PROVIDER 必须是本租户
# 平台配了凭证的 provider(走 resolve_provider 取 key),否则 build 失败→job ERROR。
# interval 设短(15s)是为 E2E 快排空;生产用默认 300。
EXPERT_WORK_ENABLE_EVAL_WORKER=true
EXPERT_WORK_EVAL_WORKER_INTERVAL_S=15
EXPERT_WORK_EVAL_AGENT_PROVIDER=deepseek
EXPERT_WORK_EVAL_AGENT_MODEL=<该 provider 的模型名>

# —— 4.4 skill-evolution 飞轮:从真实轨迹+反馈自动蒸馏/验证/升级 skill(可选,E2E 用)——
# 默认 OFF。开后 SkillEvolutionWorker 排空 curation candidate:aux LLM 蒸 DRAFT
# skill → 真 graph replay(有 skill vs 无 skill)→ grounding 判决 → 自动升级/留审。
# AUX_PROVIDER 必须是本租户平台配了凭证的 provider。interval 设短为 E2E 快跑。
EXPERT_WORK_ENABLE_SKILL_EVOLUTION_WORKER=true
EXPERT_WORK_SKILL_EVOLUTION_WORKER_INTERVAL_S=15
EXPERT_WORK_CURATION_WORKER_INTERVAL_S=15
EXPERT_WORK_MEMORY_CONSOLIDATOR_DEFAULT_AUX_PROVIDER=deepseek
EXPERT_WORK_MEMORY_CONSOLIDATOR_DEFAULT_AUX_MODEL=<该 provider 的模型名>
```

生成 KEK:

```sh
openssl rand -base64 32
```

> ⚠️ **KEK 一旦丢失或更改,数据库里所有加密过的 key(含下面要种的 Keycloak admin secret)永久解不开**。
> dev 阶段丢了大不了重粘;但同一轮测试中途别换它,否则会突然"拿不到 key"。

> `EXPERT_WORK_KEYCLOAK_ENABLED` 默认 `false`(用进程内 Fake 客户端,不建真账号)。
> 设 `true` 后,建租户 / 邀员工会真的调 Keycloak Admin API 建账号——这是"员工能登录"的前提。

---

## 2. 起栈

```sh
cd infra
# 三个 profile:
#   full          control-plane(blue/green)/ postgres / pgbouncer / redis / sandbox-supervisor / minio
#   auth          keycloak(OIDC 登录拿 token + 建账号)
#   observability prometheus / grafana / otel-collector(Gate SLO 用,可选)
docker compose --profile full --profile auth --profile observability up -d

docker compose ps                                          # 期望全 healthy / Up
curl -sS http://localhost:8000/healthz/ready | python3 -m json.tool
# 期望:{"status": "ready", "checks": {"postgres": "ok", "redis": "ok", ...}}
```

> 服务是蓝绿对(Stream I.2):active 颜色是 **`control-plane-blue`**(serving :8000)。
> 下面所有 `docker compose exec` 都打到 `control-plane-blue`。
> `migrate` 是一次性服务(`alembic upgrade head` 后退出),control-plane 等它跑完才起。

**【实跑回填】** —— 首次起栈实际耗时、`docker compose ps` 真实输出、健康检查结果、坑。

### 2.1 本机 override(端口冲突 + 浏览器 OIDC 登录)

实跑发现两个 **per-machine** 问题,需要一个本地 `docker compose` override 解决。该文件
**被 git 忽略**(每台机器不同),`docker compose` 会自动加载,所以**首次起栈前手动建**
`infra/docker-compose.override.yml`:

```yaml
# infra/docker-compose.override.yml —— 本机专用,git 忽略,docker compose 自动加载。
services:
  # 1) 本机已有宿主 redis 占着 6379;Expert Work 走 compose 网络(redis://redis:6379)通信,
  #    宿主端口映射纯属 dev 便利,丢掉它避免 bind 冲突。!override 替换(而非拼接)base 的 ports。
  redis:
    ports: !override []
  # 2) 浏览器 OIDC 登录:Keycloak(start-dev 无固定 hostname)按浏览器用的 URL 签 iss
  #    (http://localhost:8080),而后端默认 oidc_issuer 是容器 URL(http://keycloak:8080)。
  #    把校验器期望的 issuer 指到 localhost,但 JWKS 仍从容器内可达的 keycloak 主机名取。
  #    admin-ui 发的是 Keycloak **ID token**,其 aud = SPA client id(非 API service client),
  #    所以 audience 要同时接受两者(service-account audience 留给 M2M 调用)。
  control-plane-blue:
    environment:
      EXPERT_WORK_OIDC_ISSUER: http://localhost:8080/realms/expert-work
      EXPERT_WORK_OIDC_JWKS_URI: http://keycloak:8080/realms/expert-work/protocol/openid-connect/certs
      EXPERT_WORK_OIDC_AUDIENCE: '["expert-work-api-internal","expert-work-admin-ui"]'
```

admin-ui(Vite dev server,如果你单独跑前端)也要知道 OIDC 端点 —— 建
`apps/admin-ui/.env.development.local`(同样 git 忽略):

```sh
VITE_OIDC_ISSUER=http://localhost:8080/realms/expert-work
VITE_OIDC_CLIENT_ID=expert-work-admin-ui
# 可选:平台界面「Keycloak」页快捷入口。设为浏览器可达的 Keycloak origin,
# 该页「打开」按钮跳 ${base}/admin/(realm expert-work → Users → Credentials
# 设成员/首位管理员密码)。不设则该页显示"未配置"提示。
VITE_KEYCLOAK_BASE_URL=http://localhost:8080
# 可选:调试台轮次卡「在 Langfuse 打开」深链的 Langfuse UI origin
#(本地 observability profile 起的 langfuse-web 是 3001)。不设则链接隐藏。
# 部署版镜像走 tools/deploy/build-push.sh --langfuse-base-url 构建期烘入。
VITE_LANGFUSE_BASE_URL=http://localhost:3001
```

> 没有 ①,起栈会因 6379 端口冲突起不来(或你本机 redis 没占 6379 就不用)。
> 没有 ②,浏览器能登 Keycloak 但回跳后端会 `AUTH_INVALID_TOKEN`(issuer/audience 不匹配)。

---

## 3. 种入 Keycloak Admin secret(Stream R)

`keycloak_enabled=true` 时,control-plane 通过服务账号 `expert-work-api-internal` 调 Keycloak
Admin API,其 client secret 从加密金库按名 `expert-work/platform/keycloak/admin-client-secret` 取。
`sql_encrypted` 后端下没有别处写这个 key,所以**起栈后种一次**:

```sh
cd infra
docker compose exec control-plane-blue \
  python -m control_plane.seed_keycloak_secret --value dev-internal-secret-rotate-me
# 期望:OK: seeded keycloak admin secret under 'expert-work/platform/keycloak/admin-client-secret'
```

> `dev-internal-secret-rotate-me` 是 dev realm 里预置的 client secret
> (`infra/keycloak/realm-expert-work.json`,非真密钥,prod 务必轮换)。
> 命令幂等:每跑一次写一个新版本,轮换 client secret 后重跑即更新。

**【实跑回填】** —— seed 真实输出。

---

## 4. Bootstrap 第一个平台管理员 + 登录

平台第一个 `system_admin` 有"鸡生蛋"问题(授权本身需要 system_admin)。dev **与生产
一致**:`make dev-up` 不自动建管理员,走 `/setup` 向导。

### 路径 A — /setup 向导(推荐,与生产一致)

1. host 起前端(`cd apps/admin-ui && pnpm dev`)→ 开 `http://localhost:5173`。
2. 未初始化会自动跳 `/setup`。填:平台名 / 管理员邮箱 / 密码 / Setup Token。
   - **Setup Token** = `infra/.env` 的 `EXPERT_WORK_SETUP_TOKEN`(dev 默认 `dev-setup-token`)。
   - 邮箱**用新地址**(如 `founder@corp.com`),别用 `dev@expert_work.local`(Keycloak 已占 → 409)。
3. 提交 → 后端建平台租户 + Keycloak 账号(已验证 + 该密码)+ `system_admin` → 跳登录,用刚设的邮箱密码登入。

> 安全:`/v1/setup` 无需登录,靠 Setup Token + 零-admin 不变量双重门控,建好后自动失效。

### 路径 B — CLI 快路径(跳向导 / break-glass)

直接提权预置的 `dev` 用户。`expert-work-admin-ui` 是 PKCE 公有客户端(禁了 password
grant),所以用**服务账号 Admin API** 查 dev 用户 subject id:

```sh
cd infra && make dev-bootstrap-admin   # 封装了下面三步,幂等
```

或手动:

```sh
# 1. 服务账号 token(client_credentials,带 manage-users)
SA_TOKEN=$(curl -sS -X POST http://localhost:8080/realms/expert-work/protocol/openid-connect/token \
  -d grant_type=client_credentials \
  -d client_id=expert-work-api-internal \
  -d client_secret=dev-internal-secret-rotate-me | jq -r .access_token)

# 2. 查 dev 用户 UUID(浏览器登录时 token 的 sub)
SUB=$(curl -sS "http://localhost:8080/admin/realms/expert-work/users?username=dev&exact=true" \
  -H "Authorization: Bearer ${SA_TOKEN}" | jq -r '.[0].id')
echo "dev subject-id=$SUB"

# 3. 一次性授 system_admin(幂等)
docker compose exec control-plane-blue python -m control_plane.bootstrap_admin --subject-id "$SUB"
```

完整 CLI 说明见 [`bootstrap-admin.md`](./bootstrap-admin.md)。

**Admin UI 登录**:host 上起前端 → 浏览器登录:

```sh
cd apps/admin-ui && pnpm install && pnpm dev      # 起在 http://localhost:5173
```

浏览器开 http://localhost:5173,走 keycloak(**dev / devpass**)。登录后右上角应出现
仅 system_admin 可见的 `/settings/platform`、`/settings/create-tenant` 入口。

**【实跑回填】** —— bootstrap 真实输出、Admin UI 登录截图/坑。

---

## 5. 网页粘贴 LLM key(Stream Q 核心)

后端 = `sql_encrypted` 时,**直接粘贴真 key**,后端 AES-256-GCM 加密落库;catalog 只存生成的
`secret://` ref(绝不存明文)。canonical agent 已不带 `api_key_ref`,主对话模型也从这里取 key。

浏览器:`/settings/platform`(system_admin)→ Anthropic provider → 粘贴真 key
(`type=password` 不回显)→ 保存。

**【实跑回填】** —— 网页粘贴流程截图、确认 DB 里是密文不是明文。

### 5.1 配平台 Embedding & Rerank(Stream T)

新建智能体默认开启**长期记忆**,需要平台级 Embedding 模型(rerank 可选)。**建任何 agent 前必须先配**,否则建 agent 入口会挡住并引导回这里。

浏览器:`/settings/platform`(system_admin)→ **Embedding & Rerank** 区:
1. **Embedding provider** 选一个已配 key 的 provider(只列有 embedding 模型的)→ **Embedding model** 选一个(如通义 `text-embedding-v4`、智谱 `embedding-3`)。
2. (可选)打开 **Rerank**,选 provider/model(如通义 `qwen3-vl-rerank`)。
3. 保存。没配该 provider 的 key 会被拦,先回 §5 配 key。

> 立即生效:embedder 运行期读当前平台配置,改完无需重启。

---

## 6. 建公司 + 首位管理员(Stream R W1)

浏览器:`/settings/create-tenant`(system_admin):

1. **显示名** 填公司名(如 `Acme Inc`)。
2. **首位管理员邮箱** 填一个邮箱(如 `boss@acme.com`)→ 提交。
3. 成功提示会显示新 **tenant id** + **首位管理员已邀请**(状态 `invited`)。

> 这一步同时建了租户、给首管建了 Keycloak 账号、写了租户级 `ADMIN` 权限。
> dev 无 SMTP,**设密码邮件发不出来**(后台静默跳过)——账号已建好,下一步手动设密码。

### 6.1 在 Keycloak 控制台给首管设密码(dev 无 SMTP 路径)

浏览器开 Keycloak 管理控制台 http://localhost:8080(**admin / admin_dev**)→ 选 realm
`expert-work` → Users → 找到首管邮箱 → **Credentials** → Set password
(关掉 Temporary)→ 保存。

### 6.2 首管登录

退出 dev,用首管邮箱 + 刚设的密码登录 http://localhost:5173。
首管的 token 带租户 `tenant_id` + `ADMIN` 角色(平台在登录时把租户级 role binding enrich 进授权)。

**【实跑回填】** —— 建公司返回、控制台设密码、首管登录截图。

---

## 7. 注册租户 agent + 邀员工(Stream R W2)

以下用**首管**身份(已登录、tenant 已是新公司)操作:

### 7.1 注册 canonical agent 到本租户

浏览器:`/agents` → New Agent → Upload YAML → 选 `manifests/canonical-agent/v1.0.0.yaml` → 注册。
(员工对话默认走"租户默认 agent",未显式设默认时回落到名为 `canonical-agent` 的最新 ACTIVE 版本。)

### 7.2 邀员工

浏览器:`/settings/members` → Invite → 填员工邮箱 + 角色选 **operator**(viewer 只读、跑不了 agent)→ 提交。
成功后名单出现该员工,状态 `invited`。

> 同样 dev 无 SMTP,设密码邮件发不出——账号 + `operator` 权限已落库。重复 6.1 给员工在
> Keycloak 控制台设密码即可。

**【实跑回填】** —— 注册 agent、邀员工、控制台给员工设密码截图。

---

## 8. 冒烟:员工登录 → 用上 agent(W3 闭环收尾)

1. 员工用自己邮箱 + 密码登录 http://localhost:5173。
2. 进 `/agents` → 打开 `canonical-agent` → **Playground** → 发一句。
3. agent 用**平台 anthropic key** 真实回话(key 全程只在第 5 步网页填过,没碰过任何文件)。
4. 员工的**首个 run** 触发首登激活:名单里该员工从 `invited` 自动转 `active`(W3 `ensure_member_active`)。

到这里,"**一家公司从零、员工登录用上 agent**"的闭环就全程网页跑通了。

**【实跑回填】** —— 员工 Playground 真实对话截图、确认名单状态 invited→active。

---

## 9. 下一步

完整的 Gate 准入验收(eval baseline / 跨 thread 长记忆 / 持久工作区 / artifact /
审批门 / 多模态 / SLO 8 项)见 [`canonical-agent-e2e-test.md`](./canonical-agent-e2e-test.md) Phase 1–6。

---

## 10. 常见问题(边跑边补)

| 症状 | 原因 | 处理 |
|------|------|------|
| 登录后回调 `redirect_uri mismatch` | realm 的 admin-ui redirectUris 与前端端口不一致 | 确认前端在 `5173`(realm 已配 `http://localhost:5173/*`)|
| 邀员工 / 建公司后员工"无法登录" | dev 无 SMTP,设密码邮件没发出 | 到 Keycloak 控制台(:8080)手动设密码(§6.1)|
| 首管/员工登录后"无权限" | role binding 未 enrich,或角色给成了 viewer | 邀员工选 `operator`;确认 `keycloak_enabled=true` 且账号是本租户邀的 |
| seed / bootstrap 报找不到服务 | 服务名是蓝绿对 | 用 `control-plane-blue`,不是 `control-plane` |
| agent 回话报"拿不到 key" | KEK 中途变了,或没粘 key | 别换 KEK;`/settings/platform` 重新粘 anthropic key |
| _(其它待回填)_ | | |
