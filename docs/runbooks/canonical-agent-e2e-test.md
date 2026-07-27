# Canonical Agent 端到端测试 SOP

> M0 → M1 Gate 准入测试。覆盖 [STREAM-M-DESIGN](../streams/STREAM-M-DESIGN.md)
> § 2.1 SLO + § 2.2 canonical agent 端到端 6 条 + § 2.3 eval baseline +
> § 2.4 安全验收 + § 2.5 数据保护演练。
>
> 此文档是**操作手册**(逐条命令可粘贴)。Gate 退出决策的判定标准在
> [`m0-m1-gate.md`](./m0-m1-gate.md) § 5.1 GO Criteria。

---

## 0. 测试目标 + 范围

### 0.1 测试什么

| 维度 | 来源 | 条数 |
|------|------|------|
| **A. 能力级 eval baseline** | STREAM-M § 2.3 | 14 项 capability(J.1-J.15,J.13 除外)|
| **B. canonical agent 端到端 6 条** | STREAM-M § 2.2 | 6 项端到端用户场景 |
| **C. 系统级 SLO 数据采集** | STREAM-M § 2.1 | 8 项 SLO 指标可被 Prometheus 拉到 |
| **D. 安全验收** | STREAM-M § 2.4 | gVisor 7/7 + cross-tenant 3 项 |
| **E. 数据保护演练** | STREAM-M § 2.5 | PG / WORM / KMS / Volume 各 1 次 |

**A/B/C 是日常 dev 环境就能跑的;D/E 必须有 staging Linux 主机才能完整跑。**

### 0.2 测试不包含

- 30 天观察期本身(本文是把 Gate 入场前的能力跑通,30 天观察用 `m0-m1-gate.md` § 1)
- 真实 LLM provider 长跑(只在 Phase 2/3/4/5 各打 1-3 个真实 turn 验证;其余用 mock_provider)
- 多 region / 跨 AZ 灰度

---

## 1. 测试矩阵 + 顺序

执行**按 Phase 顺序**进行 — 每个 Phase 是下一个 Phase 的前置(Phase 6/7 之间可并行)。

| Phase | 名称 | 估时 | 必需环境 | 阻塞下游 |
|-------|------|------|---------|---------|
| **Phase 0** | 前置准备:起栈 + bootstrap admin + 登录 + 配模型 + 建租户 + 注册 canonical agent | 0.5-1d | dev | 全部 |
| **Phase 1** | 能力级 eval baseline 重产 + diff | 0.5d | dev | — |
| **Phase 2** | 多轮对话 + 跨 thread 长记忆 | 0.5d | dev | — |
| **Phase 3** | 持久工作区 + 强制冷启动 restore | 0.5d | dev | — |
| **Phase 4** | artifact + 审批门 | 0.5d | dev | — |
| **Phase 5** | 多模态(图像)输入 | 0.5d | dev | — |
| **Phase 6** | SLO 8 项指标采集联调 | 0.5d | dev + observability profile | — |
| **Phase 7** | 安全 + 数据保护(staging) | 1-2d | staging Linux | ⏸ **本迭代不跑** |

**本迭代覆盖 Phase 0–6(dev 跑通)。Phase 7 单列后续** —— gVisor 7 用例 + cross-tenant
3 件套 + KMS 轮换 runbook 都依赖 **staging Linux 主机 provisioning**(macOS dev 跑不动
gVisor),环境就绪后单列 Stream/PR。**Phase 0–6 累计 3-4 工作日。**

---

## Phase 0 — 前置准备

> 顺序:起栈 + dev key → bootstrap system_admin → OIDC 登录 → 配平台凭证 →
> 建租户 → 注册 canonical agent。每步都依赖前一步。

### 0.1 dev key + 起栈 + 健康检查

```sh
cd /Users/mac/src/github/jone_qian/expert-work
git checkout main && git pull --ff-only

# 1. dev 真实 LLM key —— 两种后端,二选一:
#  (A) 推荐 / 全程 web:加密金库后端。key 在 0.3 直接在网页粘贴,加密落库,
#      agent 主对话模型(canonical manifest 已无 api_key_ref)自动取它。设两个 env:
#        export EXPERT_WORK_SECRET_STORE_BACKEND=sql_encrypted
#        export EXPERT_WORK_SECRET_ENCRYPTION_KEY=$(openssl rand -base64 32)  # 32B KEK,别提交
#      (compose 已 passthrough 这两个 env;需 store_backend=sql)
#  (B) 文件后端(legacy):local_dev SecretStore 只读文件,不读进程 env。
#        cp infra/dev-keys/dev-llm-keys.example infra/dev-keys/dev-llm-keys.local
#        # 填 expert-work/dev/llm/anthropic-api-key=sk-ant-<真key>
#      详见 docs/runbooks/bootstrap-admin.md § 5。

# 2. 起栈 —— 三个 profile 缺一不可:
#      full          control-plane / postgres / pgbouncer / redis / sandbox-supervisor / minio
#      auth          keycloak(OIDC,登录拿 token 用)
#      observability prometheus / grafana / otel-collector / tempo / loki(Phase 6 用)
cd infra
docker compose --profile full --profile auth --profile observability up -d

# 3. 等就绪 + healthcheck
docker compose ps                  # 期望全 healthy / Up
curl -sS http://localhost:8000/healthz/ready | python3 -m json.tool
# 期望:{"status": "ready", "checks": {"postgres": "ok", "redis": "ok", ...}}
```

**失败排查**:
- 容器起不来 → `docker compose logs control-plane`,常见 DB 未 migrate(`migrate` 一次性服务,看其 log)
- `ready=false` 但容器 Up → 看 `checks` 里哪个失败

### 0.2 bootstrap 首个 system_admin + 登录

平台第一个管理员有"鸡生蛋"问题(授 system_admin 本身需要 system_admin),用
infra 级 CLI 破环(详见 [`bootstrap-admin.md`](./bootstrap-admin.md)):

```sh
# 1. 取 dev 用户的 subject id(keycloak realm 预置 dev/devpass;UUID 非 email)
TOKEN=$(curl -sS -X POST http://localhost:8080/realms/expert-work/protocol/openid-connect/token \
  -d grant_type=password -d client_id=expert-work-admin-ui \
  -d username=dev -d password=devpass -d scope=openid | jq -r .access_token)
SUB=$(echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq -r .sub)

# 2. 一次性授 system_admin(幂等)
docker compose exec control-plane python -m control_plane.bootstrap_admin --subject-id "$SUB"

# 3. 验证 /v1/me
curl -sS http://localhost:8000/v1/me -H "Authorization: Bearer ${TOKEN}" | jq '.is_system_admin'
# 期望:true
```

> Admin UI 登录(localhost:5173):走同一个 keycloak;登录后右上角应出现平台级入口
> `/settings/platform`(仅 system_admin 可见)。

### 0.3 配平台 provider / tool 凭证(web 粘贴真 key)

**agent 主对话模型 + embedder + web_search 的 key 全在这里配**(canonical manifest 已不带
`api_key_ref`,主模型也从平台凭证取 —— Stream Q)。后端 = `sql_encrypted` 时,**直接粘贴真 key**,
后端加密落库;catalog 只存生成的 `secret://` ref。

```sh
# 粘贴 anthropic 真 key(主对话模型 + 视觉都用它)
curl -sS -X PUT http://localhost:8000/v1/platform/credentials/providers/anthropic \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"value":"sk-ant-<真key>","enabled":true}'
# 期望:200;data.secret_ref = secret://expert-work/platform/llm/anthropic(是 ref,非明文)

# (可选)web_search 的 Tavily key
curl -sS -X PUT http://localhost:8000/v1/platform/credentials/tools/web_search \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"value":"tvly-<真key>","enabled":true}'

# 查合并视图(refs + flags,绝无明文值)
curl -sS http://localhost:8000/v1/platform/credentials -H "Authorization: Bearer ${TOKEN}" | jq .
```

> **或** 用平台配置页 **`/settings/platform`**(system_admin)在浏览器粘贴(`type=password` 不回显)。
> 后端 = `local_dev`(文件)时,这里改填 `secret_ref`(`secret://<name>`),值由 0.1(B) 的文件提供。
> `value`(粘贴真 key)二选一,与 `secret_ref` 互斥。

### 0.4 创建租户

```sh
# 真端点:POST /v1/tenants(system_admin only),payload 是 display_name
TENANT_ID=$(curl -sS -X POST http://localhost:8000/v1/tenants \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"display_name":"Gate E2E Tenant"}' | jq -r .data.tenant_id)
echo "tenant_id=$TENANT_ID"
# 非 admin 调 → 403;重复同 id → 409(惰性建租;tenant_config 行即租户)
```

### 0.5 注册 canonical agent

✅ **manifest 已入仓:`manifests/canonical-agent/v1.0.0.yaml`**(按真 `AgentSpec` schema 写,
CI 由 `services/control-plane/tests/test_canonical_manifest.py` 守护)。能力面:长记忆 /
持久工作区 / 审批门(`approval_required_tools: [http]`)/ 多模态(`model.supports_vision: true`)。
**它不带 `api_key_ref`** —— 主对话模型的 key 来自 0.3 配的平台 anthropic 凭证(Stream Q)。
直接注册即可,无需手写:

```sh
cd /Users/mac/src/github/jone_qian/expert-work
curl -sS -X POST http://localhost:8000/v1/agents \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d "$(jq -Rn --rawfile y manifests/canonical-agent/v1.0.0.yaml '{manifest: $y}')"
# 期望:201 Created,返回 spec_sha256

# 验证
curl -sS http://localhost:8000/v1/agents -H "Authorization: Bearer ${TOKEN}" \
  | jq '.data.items[] | select(.name=="canonical-agent")'
# 期望:有 name=canonical-agent version=1.0.0
```

**或**通过 Admin UI:`/agents` → `New Agent` → `Upload YAML` → 选 `manifests/canonical-agent/v1.0.0.yaml`。

### 0.6 前置 Checklist(全部 ✅ 才能进 Phase 1)

- [ ] `docker compose ps` 全栈 healthy(full + auth + observability)
- [ ] `/healthz/ready` 返回 ready
- [ ] `infra/dev-keys/dev-llm-keys.local` 已填真 key(git-ignored)
- [ ] `bootstrap_admin` 跑过,`/v1/me` `is_system_admin: true`
- [ ] `GET /v1/platform/credentials` 反映已配 provider/tool
- [ ] `POST /v1/tenants` 返回 201(非 admin 403 / 重复 409)
- [ ] `POST /v1/agents` 注册 canonical-agent 返回 201,`GET /v1/agents` 能看到
- [ ] Admin UI(localhost:5173)能看到 canonical-agent 1.0.0

---

## Phase 1 — 能力级 eval baseline 重产 + diff

目标:确认当前 main 跑出的 baseline 等于 checked-in baseline(`tools/eval/baselines/m0_gate_baseline.yaml` 已记录的 14 capability 全 PASS)。

### 1.1 跑 baseline

```sh
cd /Users/mac/src/github/jone_qian/expert-work

# 1. 安装 eval 依赖(如未装)
uv sync

# 2. 跑 baseline,写到临时文件
.venv/bin/python tools/eval/run_baseline.py --out /tmp/baseline-phase1.yaml

# 3. diff capability 分数(忽略 metadata)
diff <(yq '.capabilities' tools/eval/baselines/m0_gate_baseline.yaml) \
     <(yq '.capabilities' /tmp/baseline-phase1.yaml)
```

**期望**:diff 为空(所有 capability 分数 100% 一致,只 metadata 字段变)。

### 1.2 单独 capability 抽查

抽查 3 个最复杂的(J.1 plan_execute / J.3 memory_recall / J.6 multimodal):

```sh
# J.1 plan_execute(LLM-judge;有 ANTHROPIC_API_KEY 走真 judge,否则自动退到
# ScriptedJudge —— 后者也能 PASS,只是判定更弱)
ANTHROPIC_API_KEY=<key> .venv/bin/pytest tools/eval/test_plan_execute.py -v

# J.3 memory_recall
.venv/bin/pytest tools/eval/test_memory_recall.py -v

# J.6 multimodal
.venv/bin/pytest tools/eval/test_multimodal.py -v
```

**期望**:全部 PASS。

### 1.3 Phase 1 Checklist

- [ ] `run_baseline.py` 写出的临时文件 diff main 上 baseline = 空
- [ ] J.1 / J.3 / J.6 单独 pytest 全 PASS
- [ ] 任何 capability `status: PASS` 转 `FAIL` → **STOP,P0 升级**

---

## Phase 2 — 多轮对话 + 跨 thread 长记忆

对应 STREAM-M § 2.2 第 ① 条:**多轮对话跨会话保持记忆**(J.3 long-term memory 跨 thread 召回,K6/K7 CRUD/DLQ 闭环)。

### 2.1 测试场景

**用户故事**:用户 A 在 thread #1 告诉 agent "我在做 Stream M 的 Gate 验收",在 thread #2 问 "我最近在做什么" — agent 应能召回。

### 2.2 操作步骤

通过 Admin UI Playground(localhost:5173/agents/canonical/1.0.0/playground):

```
# Thread #1
user> 我在做 Stream M 的 Gate 验收,正在跑 canonical agent 端到端测试。
agent> [记录到长期记忆]
user> 请记住我现在的工作。
agent> [写 memory key="current_work" value="Stream M Gate 验收"]
```

新开 Thread #2:

```
# Thread #2
user> 我最近在做什么?
agent> 你在做 Stream M 的 Gate 验收,正在跑 canonical agent 端到端测试。
```

### 2.3 后端验证

```sh
# 1. 列 user 的 long-term memory
curl -sS http://localhost:8000/v1/memory?kind=long_term \
  -H "Authorization: Bearer ${TOKEN}" | jq .
# 期望:items 里有 current_work 这条记忆

# 2. cross-tenant 隔离验证(另起一个 tenant 的 user 应看不到)
curl -sS http://localhost:8000/v1/memory?kind=long_term \
  -H "Authorization: Bearer ${OTHER_TENANT_TOKEN}" | jq '.items | length'
# 期望:0(或不含 current_work)

# 3. 在 Admin UI /memory 也能看到这条记忆
```

### 2.4 Phase 2 Checklist

- [ ] Thread #2 能复述 Thread #1 的内容
- [ ] `/v1/memory` 能列出 current_work 记忆
- [ ] cross-tenant 用 $OTHER_TENANT_TOKEN(另一租户用户) 看不到这条记忆
- [ ] Admin UI /memory 页能编辑这条记忆(走 H.4 PR 2 path)

---

## Phase 3 — 持久工作区 + 强制冷启动 restore

对应 STREAM-M § 2.2 ② + ③:**持久工作区跨 run 留存** + **空闲回收 + 快速 restore**。

> ⚠️ **M0 范围**:canonical agent(M0)的工具是 `web_search` + `http`,**没有写
> `/workspace` 的工具**(`exec_python` / file I/O 是 **M1**)。所以 M0 能验证的是:
> ① persistent_workspace **挂载语义**(manifest `filesystem.persistent_workspace: true`
> → /workspace 是 named volume,不是 ephemeral tmpfs);② **reap 端点保留 volume 只删
> session**(PR K);③ **冷启动延迟 SLO**。**agent 自己写文件 → 重读的完整闭环依赖 M1
> exec_python**,本迭代标注为 M1 项,不在 M0 跑通。
>
> dev 主机用 **runc**(非 gVisor),这是预期;gVisor 安全用例在 Phase 7(staging)。

### 3.1 强制冷启动 + volume 保留(M0 可验)

```sh
# Step 1 - 起一个 thread 跑一个真实 turn,让 sandbox 冷启动一次(产生 cold_start 样本 +
#          挂上该 user 的持久 workspace volume)。通过 Playground 发任意 prompt 即可。

# Step 2 - 强制回收所有空闲 sandbox(system_admin;PR K 的端点)
curl -sS -X POST 'http://localhost:8000/v1/sandboxes/reap?force=true' \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data.reaped_count'
# 期望:reaped_count >= 1;volume 不被删(reaper 只删 session)

# Step 3 - 确认 workspace volume 仍在(被 reap 的是 sandbox 容器,不是 volume)
docker volume ls | grep workspace
# 期望:该 user 的 workspace volume 仍列出

# Step 4 - 再发一个 turn 触发冷启动,抓延迟
curl -sS 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.95,sum%20by%20(le)(rate(expert_work_sandbox_cold_start_seconds_bucket[5m])))' \
  | jq '.data.result[0].value[1]'
# 期望:P95 < 5.0
```

### 3.2 Phase 3 Checklist

- [ ] `POST /v1/sandboxes/reap?force=true` 返回 `reaped_count >= 1`(非 admin → 403)
- [ ] reap 后该 user 的 workspace volume 仍在(`docker volume ls`)
- [ ] `expert_work_sandbox_cold_start_seconds` P95 < 5s
- [ ] Admin UI Run Detail 能看到 sandbox lifecycle 事件
- [ ] _(M1)_ agent 经 exec_python 写 `/workspace` 文件 → reap → 重读仍在 —— **本迭代不跑**

---

## Phase 4 — artifact + 审批门

对应 STREAM-M § 2.2 ④ + ⑤:**artifact 跨 thread 可访问** + **审批门跑通**。

### 4.1 artifact 测试

```
# Thread #3 - 创建 artifact
user> 把当前 /workspace/notes.md 作为 artifact 保存,名字 "gate-notes"
agent> [调用 artifact 工具] → 保存 artifact gate-notes v1
```

```sh
# 验证 artifact 已写入
curl -sS http://localhost:8000/v1/artifacts \
  -H "Authorization: Bearer ${TOKEN}" | jq '.items[] | select(.name=="gate-notes")'
# 期望:items 里有 name=gate-notes version=1
```

```
# Thread #4 - 跨 thread 访问
user> 列出我所有的 artifacts
agent> [列表] → gate-notes v1
user> 下载 gate-notes v1 并显示内容
agent> Gate verification notes - 2026-05-27
```

### 4.2 审批门测试

> 审批门是**平台强制**的,由 manifest `policies.approval_required_tools` 声明 —— canonical
> agent 列了 **`http`**(**精确工具名,不是正则**)。agent 一旦要调 `http` 工具,LangGraph
> `interrupt()` 把 run 暂停成 **`paused`**,等人审批。
>
> ⚠️ SOP 旧版写的 `hitl.triggers[].match` 正则 **运行期不存在**;审批触发只认
> `approval_required_tools` 里的精确工具名(Mini-ADR P-17)。

```
# Thread #5 —— 诱导 agent 调用 http 工具(被审批门拦)
user> 帮我用 HTTP 工具抓一下 https://example.com 的首页内容
agent> ⚠️ 该操作需要人工审批 —— 已暂停(interrupt 触发)
```

```sh
# 后端验证 —— 应有一条 paused 的 run(注意:status=paused,不是 awaiting_approval)
curl -sS 'http://localhost:8000/v1/runs?status=paused' \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data.items[0]'
# 期望:有一条 run 状态 paused,interrupt 指向 http 工具调用

# Admin UI 操作:
# 1. 开 /runs 列表(可按 status=paused 过滤)→ 找到那条 run
# 2. 打开 Run Detail 页 → ApprovalCard
# 3. 点 Approve → run resume → audit_log 写 APPROVAL_GRANTED
# 4. 或点 Reject → run 终止 → audit_log 写 APPROVAL_DENIED

# 验证 audit trail
curl -sS 'http://localhost:8000/v1/audit?action=approval_granted' \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data.items[0]'
# 期望:有审批通过的 audit entry
```

### 4.3 Phase 4 Checklist

- [ ] artifact 跨 thread 可列 + 可下载
- [ ] cross-tenant 拒绝(用 $OTHER_TENANT_TOKEN(另一租户用户) 取不到 gate-notes)
- [ ] 调 `http` 工具触发 `interrupt()`,run 进 `status=paused`
- [ ] Admin UI ApprovalCard 可 Approve / Reject
- [ ] Resume 后 run 状态变化:paused → running → completed/failed
- [ ] Audit log 有完整 trail(APPROVAL_REQUESTED → APPROVAL_GRANTED → ACTION_EXECUTED)

---

## Phase 5 — 多模态(图像)输入

对应 STREAM-M § 2.2 ⑥:**多模态输入**(J.6 Path A + Path B 均在 staging 跑通真实图像)。

> **dev vs staging 分层**:Path A 在 dev 用真 anthropic vision key 就能跑真实描述
> (canonical manifest 主模型 `claude-sonnet-4-5` `supports_vision: true`)。Path B 需要
> 临改 manifest + 一个 OpenAI key + `vision:` 块,较重 —— 列为**进阶/staging**,M0 dev
> 主跑 Path A。

### 5.1 Path A — 主模型支持视觉(content block)

最简:**用 Admin UI Playground 的传图入口**(PR M)。

```
# Playground (localhost:5173/agents/canonical-agent/1.0.0/playground)
# 1. 点「添加图片」→ 选一张本地 png/jpg(走 POST /v1/sessions/{thread_id}/uploads
#    → 返回 Expert Work://image/... ref,作为附件 chip 显示)
# 2. 输入框打字 + Run
user> [附件:screenshot.png] 描述这张图
agent> 这是一张 ... 的截图,我看到 ...
```

手动 API(等价,Playground 内部就是这两步):

```sh
# 先建一个 thread(POST /v1/sessions),拿 thread_id;再上传到该 thread:
curl -sS -X POST "http://localhost:8000/v1/sessions/${THREAD_ID}/uploads" \
  -H "Authorization: Bearer ${TOKEN}" -F "file=@/path/to/screenshot.png" | jq .image_ref
# 期望:201 + {"image_ref": "Expert Work://image/..."};该 ref 放进下个 run 的 image_refs
```

### 5.2 Path B — `ask_image` 工具 + 单独 VL 模型(进阶 / staging)

主模型不支持视觉时,图走 `ask_image` 工具路由到单独 VL 模型。需把 manifest 主模型换成
`supports_vision: false` 的模型并加 `spec.vision:` 块(声明 VL 模型)。本迭代不在 dev 主跑。

### 5.3 Phase 5 Checklist

- [ ] Path A:Playground 传图 → 主模型(真 anthropic key)直接看图、描述合理
- [ ] `POST /v1/sessions/{thread_id}/uploads` 返回 `Expert Work://image/...` ref
- [ ] _(进阶/staging)_ Path B:主模型不支持视觉时自动走 `ask_image`

---

## Phase 6 — SLO 8 项指标采集联调

对应 STREAM-M § 2.1 — 验证 8 项 SLO 指标都能被 Prometheus 拉到,K10 大盘有数据。

### 6.1 触发足够流量

指标需要样本才查得到。**`run_baseline.py` 没有 `--warmup` 参数** —— 手工造流量:
在 Playground 跑 ~5 个 thread、每个 3-5 个真实 turn(混入 Phase 2/5 的对话即可),
或重复 `run_baseline.py --out /tmp/warmup.yaml` 几次。确保 control-plane 和
sandbox-supervisor 的 `/metrics` 都有近期样本后再查 Prometheus。

### 6.2 逐项 Prometheus 查询

8 项查询(完整列表见 [`m0-m1-gate.md` § 1.2](./m0-m1-gate.md#12-daily-slo-check)):

```sh
PROM=http://localhost:9090

# 1. 可用性 5xx 错误率
curl -sS "${PROM}/api/v1/query?query=Expert Work:sli:control_plane_availability:ratio5m" | jq .data.result[0].value[1]
# 期望:>= 0.999

# 2. 首个图节点 P95(前名 TTFT)
curl -sS "${PROM}/api/v1/query?query=histogram_quantile(0.95,sum%20by%20(le)(rate(expert_work_session_first_node_seconds_bucket[1h])))" | jq .data.result[0].value[1]
# 期望:< 2.0

# 3. End-to-end P95
curl -sS "${PROM}/api/v1/query?query=histogram_quantile(0.95,sum%20by%20(le)(rate(expert_work_session_duration_seconds_bucket{outcome=\"success\"}[1h])))" | jq .data.result[0].value[1]
# 期望:< 30.0

# 4. SSE 流断裂率
curl -sS "${PROM}/api/v1/query?query=sum(rate(expert_work_llm_stream_stale_total[1h]))/sum(rate(expert_work_llm_tokens_total[1h]))" | jq .data.result[0].value[1]
# 期望:< 0.0005

# 5. Sandbox 冷启动 P95
curl -sS "${PROM}/api/v1/query?query=histogram_quantile(0.95,sum%20by%20(le)(rate(expert_work_sandbox_cold_start_seconds_bucket[1h])))" | jq .data.result[0].value[1]
# 期望:< 5.0

# 6. Durable resume P95
curl -sS "${PROM}/api/v1/query?query=histogram_quantile(0.95,sum%20by%20(le)(rate(expert_work_durable_resume_seconds_bucket[1h])))" | jq .data.result[0].value[1]
# 期望:< 1.0

# 7. Memory recall@5
.venv/bin/python tools/eval/memory_recall.py tools/eval/datasets/memory_recall/zh_en_seed.yaml | jq .recall_at_5
# 期望:>= 0.7

# 8. P0 事故数 — 手工核查
ls docs/incidents/2026-05/ 2>/dev/null | wc -l
# 期望:= 0(本测试期间无 P0)
```

### 6.3 Grafana 大盘联调

打开 Grafana(localhost:3000),进 `Expert Work — Overview` 大盘,验证 8 项 panel 都有数据点(不是 No data)。

### 6.4 Phase 6 Checklist

- [ ] 8 项 Prometheus query 全部返回数据(非 NaN / empty)
- [ ] 8 项数值都在 Gate 阈值内
- [ ] Grafana `Expert Work — Overview` 大盘 panel 全部有数据

---

## Phase 7 — 安全 + 数据保护(staging)

> ⏸ **本迭代不跑 —— 单列后续**。Phase 7 整段依赖 **staging Linux 主机 provisioning**
> (macOS dev 跑不动 gVisor),且 KMS 轮换 runbook 尚缺。等 staging 环境就绪后单列
> Stream/PR。下文保留为目标清单。

⚠️ **必须 staging Linux 主机**,macOS 跑不动 gVisor。

### 7.1 gVisor 7/7 沙盒安全用例

```sh
# 在 staging Linux 主机上
ssh staging-host
cd /opt/expert-work
cd services/sandbox-supervisor
.venv/bin/pytest tests/integration/test_gvisor_security.py -v
```

**期望**:7/7 PASS,含 `test_gvisor_cve_2019_5736_poc_fails` + `test_gvisor_timing_isolation`。

### 7.2 Cross-tenant 测试

```sh
.venv/bin/pytest \
  services/control-plane/tests/test_sse_cross_tenant.py \
  packages/expert-work-persistence/tests/test_memory_recall_cross_tenant.py \
  services/control-plane/tests/test_artifact_cross_tenant.py -v
```

**期望**:全部 reject(测试断言异常被正确抛出)。任何静默通过 → **P0**。

### 7.3 数据保护演练(各 1 次)

| 演练 | Runbook | 命令摘要 |
|------|---------|---------|
| PG 恢复 | [pg-restore.md](./pg-restore.md) | dump staging DB → restore 到临时 DB → 验证表 row count |
| WORM 恢复 | [audit-restore.md](./audit-restore.md) | export audit 段 → re-ingest → 验证 hash 匹配 |
| KMS 轮换 | (K13 drill,待补 runbook) | rotate KEK → 重读旧加密数据 → 验证解密成功 |
| Volume restore | [volume-restore.md](./volume-restore.md) | J.15 备份 → 新卷 restore → 验证文件 hash |

每项演练写入 `gate-log/drills/<date>-<drill>.md`(per `m0-m1-gate.md` § 4)。

### 7.4 Phase 7 Checklist

- [ ] gVisor 7/7 全 PASS
- [ ] cross-tenant 3 项测试全 reject
- [ ] PG 演练成功
- [ ] WORM 演练成功
- [ ] KMS 演练成功
- [ ] Volume restore 演练成功
- [ ] 所有 staging 演练写入 `gate-log/drills/`

---

## 8. 总验收清单

**本迭代目标 = Phase 0–6 在 dev 跑通。** Phase 7(安全 + 数据保护)单列后续,
不阻塞 Phase 0–6 的验收记录。

| 维度 | Phase | Status |
|------|-------|--------|
| A. eval baseline | Phase 1 | ☐ |
| B. canonical agent 端到端 |  |  |
| &nbsp;&nbsp;① 多轮对话长记忆 | Phase 2 | ☐ |
| &nbsp;&nbsp;② 持久工作区 volume 保留 + reap | Phase 3 | ☐ |
| &nbsp;&nbsp;③ 冷启动 restore P95 < 5s | Phase 3 | ☐ |
| &nbsp;&nbsp;④ artifact 跨 thread | Phase 4 | ☐ |
| &nbsp;&nbsp;⑤ 审批门(`approval_required_tools` / `status=paused`)| Phase 4 | ☐ |
| &nbsp;&nbsp;⑥ 多模态 Path A | Phase 5 | ☐ |
| C. SLO 8 项采集 | Phase 6 | ☐ |
| _(M1)_ agent 写 /workspace 闭环(exec_python)| Phase 3 | ⏸ 后续 |
| _(Phase 7)_ 安全 sandbox 7/7 | Phase 7 | ⏸ staging |
| _(Phase 7)_ cross-tenant 3 项 | Phase 7 | ⏸ staging |
| _(Phase 7)_ PG / WORM / KMS / Volume 演练 | Phase 7 | ⏸ staging |

**Phase 0–6 全 ✅ → 本迭代 E2E readiness 达成。** 进 Gate 30 天窗口还需 Phase 7
(staging 就绪后单列跑通),见 `m0-m1-gate.md` § 0。

---

## 9. 常见失败 + 处理

| 症状 | 可能原因 | 处理 |
|------|---------|------|
| Phase 0 `POST /v1/agents` 401 | token 未注入 / 过期 | `keycloak admin login` 重发 / 用 dev token |
| Phase 0 `POST /v1/tenants` 403 | 当前 token 不是 system_admin | 确认跑过 `bootstrap_admin` 且 `/v1/me` `is_system_admin: true` |
| Phase 0 manifest 校验失败 `MANIFEST_VALIDATION` | YAML 字段不符合 AgentSpec | 本地先 `test_canonical_manifest.py` 复现;看 control-plane log error path |
| Phase 0 agent 跑真实 turn 报无 key | dev key 没进 SecretStore 文件 | 确认 `infra/dev-keys/dev-llm-keys.local` 有 `expert-work/dev/llm/anthropic-api-key=…`(注意:`ANTHROPIC_API_KEY` 进程 env **不被读**)|
| Phase 1 baseline diff 不空 | 上游 PR 影响 eval 分数 | retro 找根因;不允许"分数轻微下降但继续观察"|
| Phase 2 Thread #2 没复述 Thread #1 | 长记忆没写入 / embedder key 没配 | 看 `/v1/memory` 列表 + 确认平台 provider 凭证(embedder)已配 |
| Phase 3 reap 后 volume 没了 | reaper 误删 volume(应只删 session)| 看 sandbox-supervisor log + `docker volume ls` |
| Phase 4 审批不触发 | manifest 没把工具列进 `approval_required_tools` / agent 没真去调该工具 | 确认 manifest `policies.approval_required_tools: [http]`;**`hitl.triggers.match` 正则运行期不存在,别再找它** |
| Phase 5 多模态 Path B 不走 ask_image | `supports_vision` 探测错误 | 看 `ModelSpec.supports_vision` 配置 |
| Phase 6 某项 Prometheus query 返回空 | 指标 emitter 没就位 / scrape job 漏 | 查 `tools/observability/rules/` + control-plane `/metrics` |
| Phase 7 gVisor 测试 SKIP | runsc 没装 / kernel 不支持 | staging Linux runner image 装 runsc |

---

## 10. References

- 测试目标定义:[STREAM-M-DESIGN.md](../streams/STREAM-M-DESIGN.md) § 2.1-2.5
- Gate 30 天观察 SOP:[m0-m1-gate.md](./m0-m1-gate.md)
- Eval baseline 制品:`tools/eval/baselines/m0_gate_baseline.yaml`
- SLO 文档:[STREAM-G-DESIGN.md](../streams/STREAM-G-DESIGN.md) § G.1
- Canonical agent 定义:[08-AGENT-CAPABILITY-ASSESSMENT.md](../architecture/08-AGENT-CAPABILITY-ASSESSMENT.md)
