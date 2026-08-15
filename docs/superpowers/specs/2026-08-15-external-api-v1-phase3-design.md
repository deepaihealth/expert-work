# 第三方对接 API v1 · 阶段 3(补能力)设计

**日期**:2026-08-15
**状态**:待实施
**前置**:阶段 2(P3 波 1 / 波 2)已全交付并上线测试环境(`5fee6c5e`)
**来源**:`.superpowers/sdd/ROADMAP-2026-08-13.md` §阶段 3

---

## 一、要解决什么

对接方(workbuddy / openclaw 这类 agent 客户端)现在能发 run、能收流、能翻会话和消息、能拿工作区文件。三个洞:

| # | 洞 | 现在只能怎么办 |
|---|---|---|
| 3.1 | **不知道这个租户有哪些 agent 可以调** | 对接时人工问一遍 agent_code,写死在客户端里。agent 上下线客户端不知道 |
| 3.2 | **列不出历史任务** | 只能按 `run_id` 拿事件。客户端要做「我的任务」列表,得自己在本地记每个 run_id |
| 3.3 | **分不出哪个文件是成果** | `workspace/files` 吐工作区**全部**文件。一次 run 留十几个中间文件,只有一两个是给用户看的 |

3.3 顺带把 spec §九第 4 项(文件删除)一起收了 —— 它是产物视图的子集。

---

## 二、已拍板的决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 3.3 范围 | **列表 + 下载 + 删除(软删)** | 一次收口,控制台侧四个端点已有完整实现可镜像 |
| 3.1 显示名 | **manifest 新增可选 `display_name`** | 现在只有 `name`(=agent_code,机器标识)和 `description`。客户端界面上直接显示 `code` 很难看 |
| 3.1 路径 | **`GET /v1/agent-catalog`** | `/v1/agents` 已被控制台面占死。新前缀让平面分区永远干净 —— #1153 那轮 9 条路由漏挂 `console_only` 正是混前缀的代价 |
| 交付切分 | **PR-A = 3.1 + 3.2,PR-B = 3.3** | 前两个都是薄只读列表端点、共用同一套 owner 校验;3.3 涉及新 store 通路 + 破坏性操作,边界独立更干净 |

---

## 三、PR-A:agent 目录 + run 列表

### 3.1 `GET /v1/agent-catalog`

**契约**

```
GET /v1/agent-catalog?limit=50&offset=0
Authorization: Bearer <key>
```

```json
{
  "success": true,
  "data": {
    "agents": [
      {
        "agent_code": "report-writer",
        "display_name": "报表助手",
        "description": "根据数据生成周报",
        "available": true
      }
    ],
    "limit": 50,
    "offset": 0
  },
  "error": null
}
```

**字段来源**

| 字段 | 来源 |
|---|---|
| `agent_code` | `AgentSpecRecord.name`(= manifest `metadata.name`) |
| `display_name` | **新增** `spec.display_name`;为空时回落到 `agent_code`(响应里永远非空,客户端不用做空判断) |
| `description` | `spec.description`(已有字段,默认 `""`) |
| `available` | 未被 kill switch 禁用 **且** 存在 `status=ACTIVE` 的版本 |

**`available` 必须与 run 端点同判据**。`api/agents.py:_resolve_session` 的两道闸是:

```python
if await disable_service.is_disabled(tenant_id, agent_code):   # → 403 AGENT_DISABLED
active = await repo.list_by_tenant(status=ACTIVE, name=agent_code, limit=1)
if not active:                                                  # → 404 AGENT_NOT_FOUND
```

目录端点必须用同一对判据算 `available`,否则会列出一个「点了就 403」的 agent —— 这类不一致是客户端最难排查的。

**不列出的东西**:版本号(第三方不选版本,平台自动用 ACTIVE)、系统提示词、工具清单、模型配置 —— 那些是 `/v1/agents` 控制台面的东西,`85abdb39` 刻意对第三方关死的。

**分页**:`limit` 1–200 默认 50,`offset` ≥ 0。租户 agent 数量小,但形状与其它列表端点保持一致。

**scope 闸**:`require("manifest", "read")`。`read` scope key → VIEWER 角色 → `manifest: {read}`,通过;零 scope key 挡住。

**禁用的 agent 出不出现在列表里**:出现,`available: false`。客户端界面上置灰比「凭空消失」好排查。

#### `display_name` 全栈改动

| 层 | 改什么 |
|---|---|
| protocol | `AgentSpecBody` 加 `display_name: str = ""`,与 `description` 并排 |
| schema 端点 | 零改动 —— `GET /v1/agents/schema` 由 `AgentSpec.model_json_schema()` 自动生成 |
| 配置页 | `FormView.tsx` basic section 加一个输入框(照 `description` 的形状);`form_model.ts` 加 `readDisplayName` / `setDisplayName` |
| i18n | 两个 locale 各加 `field_display_name` + `field_display_name_help` |

`bare` 模式(`AgentTemplateConfigForm` 的合并 tab)照 `description` 现有处理 —— 那条路径自带描述字段,不重复渲染。

**存量 agent**:字段有默认值 `""`,老 manifest 反序列化不受影响,响应里回落到 `agent_code`。不需要数据迁移。

### 3.2 `GET /v1/agents/{agent_code}/runs`

**契约**

```
GET /v1/agents/{agent_code}/runs?user_id=u_123&session_id=<uuid>&status=success&limit=50&offset=0
```

```json
{
  "success": true,
  "data": {
    "runs": [
      {
        "run_id": "...",
        "session_id": "...",
        "status": "success",
        "created_at": "2026-08-15T10:00:00+00:00",
        "finished_at": "2026-08-15T10:00:12+00:00",
        "error": null
      }
    ],
    "limit": 50,
    "offset": 0
  },
  "error": null
}
```

**`user_id` 必填**(无默认)。与 `GET .../sessions` 同一条铁律:漏传参数必须是 422,不能降级成「列出整个租户的 run」。

**`session_id` 选填**。给了就先走 `load_owned_session`(不属于 `(user, agent)` 就 404),再按该 thread 过滤。

**`status` 选填**,取 `RunStatus` 全集。

**`error` 字段可以给,不新开泄露面** —— 这一条有实证:

- `agent_run.error` 存的是 `str(exc)`(`orchestrator/sse.py:745` / `:779`)
- 同一次 run 的 SSE `error` 帧发的是 `{"message": str(exc), "name": type(exc).__name__}`(同两处)

**同一个字符串**。第三方在实时流里已经收到过它。列表里再给一次,信息量为零增量。owner 校验也一致(都是 `(user, agent)` 维度)。

> 顺带记 backlog:`str(exc)` 本身是否该脱敏,是**既有面**的问题(SSE error 帧),不是本轮引入。不在本轮扩范围。

**实现关键点:`RunStore.list_for_tenant` 要加 `agent_name` 参数**

现在的签名没有 agent 维度:

```python
async def list_for_tenant(self, *, tenant_id, status=None, thread_ids=None,
                          user_id=None, q=None, limit=100, offset=0) -> list[RunInfo]
```

两条路:

| 做法 | 问题 |
|---|---|
| API 层先查 `(user, agent)` 的全部 thread_ids,再传 `thread_ids=` | **分页会不准**:thread 数量无上限,先取一页 thread 再过滤 run,`offset` 语义就错了 |
| **给 store 加 `agent_name` 参数**(SQL join `thread_meta`) | 正确。`list_running_for_agent` 已有同款 join 可照抄 |

选后者。**SQL 与 in-memory 两个后端的谓词必须字节级同义** —— 这是本仓反复踩过的命门(记忆 `I-1` 类)。两个后端各写一组测试,且必须有一个跨后端的等价性测试。

**scope 闸**:`require("session", "read")`,与 `GET .../sessions` 同档。

---

## 四、PR-B:产物视图

### 端点三条

```
GET    /v1/agents/{agent_code}/artifacts?user_id=u_123
GET    /v1/agents/{agent_code}/artifacts/download?user_id=u_123&name=<name>
DELETE /v1/agents/{agent_code}/artifacts?user_id=u_123&name=<name>
```

**`agent_code` 不参与过滤** —— 产物和工作区一样是 `(tenant_id, user_id)` 维度。路径带它只是为了和其它对外端点形状一致,同 `external_workspace.py` 的既有处理,模块 docstring 里要写明。

**`name` 走 query 不走 path**。控制台侧用的是 `{name:path}`;对外用 query 参数,避免产物名含 `/` 时的路径穿越与编码歧义。DELETE 的 `user_id` 和 `name` 都在 query —— 与同资源的 GET 一致(backlog B-6 记的就是同资源两个写操作参数位置不一致坑对接方,这里不重蹈)。

### 列表响应

```json
{
  "success": true,
  "data": {
    "artifacts": [
      {
        "name": "2026-08 周报.docx",
        "kind": "document",
        "latest_version": 3,
        "created_at": "...",
        "updated_at": "..."
      }
    ]
  },
  "error": null
}
```

**不带 `size_bytes`**。`ArtifactStore.list_for_user` 返回的 `Artifact` 不含版本详情,要带大小得逐行查 latest version —— 一个现成的 N+1。控制台自己的列表也只有 name/kind/latest_version。`size_bytes` / `sha256` 本来就是**首次下载时才懒回填**的,列表里给出来一半是 null,反而误导。

**软删的产物不出现**(`list_for_user` 默认 `include_deleted=False`)。

### 下载

镜像控制台 `GET /v1/artifacts/download` 的全套安全处理:

- MIME 推断 + XSS 安全 disposition(active content 强制 attachment)+ `X-Content-Type-Options: nosniff`
- 首次读回填 `size_bytes` / `sha256`
- **配额扣减**:`resource_kind="artifact_download"`,`cost=1`(走 QPS + `ARTIFACT_DOWNLOAD_COUNT_30D`)。第三方比员工更需要这道闸
- 权限失败(`WorkspacePermissionError`)→ 500 + 固定文案;内容不存在(`SandboxSupervisorError`)→ 404。**两者不能合并成一个 404** —— 沙箱迁移 W2-BUG-1 的教训
- 成功响应是**裸文件字节流**,不套 `{success, data, error}`;只有错误路径套信封(同 `workspace/file`)

### 删除

软删(`store.soft_delete`),与控制台完全同语义:

- 命中 → 200 `{"deleted": "<name>"}`
- 不存在 / 已软删 / 跨用户 → **统一 404**,不泄露存在性
- 工作区里的字节**不动**。保留期扫描到期后才硬删;agent 重新 save 同名会把它复活
- **scope 闸 = `require("session", "write")`**,不是 `"delete"`。`ApiKeyScope` 没有独立 delete 档,挂 `"delete"` 等于只有 `admin` scope 的 key 能删 —— 逼第三方拿一把能改服务账号的钥匙才能删自己的文件,是反向的最小权限。这与 `archive_session` 的既有裁决同源(2026-08-13 用户决策)
- 审计:`AuditAction.ARTIFACT_DELETE`,带 `on_behalf_of`

### 不做

- **版本历史 / 下载指定版本**。列表已给 `latest_version`(客户端知道「改过 3 版」),历史本身对客户端界面价值低。控制台侧 `/versions` 保持 console-only
- **改 `kind`**(控制台的 PATCH)。分类是 agent 保存时声明的,第三方改它没有语义
- **硬删**。与会话侧一致 —— `:purge` 永远是 console-only

---

## 五、安全考量

### 新增的暴露面(设计意图,但要说清)

**`/v1/agent-catalog` 让持 key 方看到整个租户的 agent 清单和描述。** 这是用户确认要的能力,但有两个后果要正视:

1. **`spec.description` 首次对第三方可见**。存量 agent 的 description 是员工在控制台自己写的,可能是内部备注(「张三让加的临时逻辑」「接财务系统别动」)。**发布前必须抽查测试/生产环境存量 agent 的 description 实际内容**,不是抽查代码。
2. `display_name` 是新字段,不存在存量污染。

这条要写进 PR 的验收清单,不是"注意一下"。

### 保持的既有铁律

| 铁律 | 落在哪 |
|---|---|
| `user_id` 必填、无默认 | 3.2 的 runs 列表;3.3 的三条 |
| 读路径 `mint=False`,不给陌生 `user_id` 造 `tenant_user` 行 | 3.2 / 3.3 全部用 `lookup_external_user_id` |
| 越权一律 404,不用 403(不泄露存在性) | 3.2 的 `session_id` 过滤;3.3 的下载 / 删除 |
| NUL 字节防御 | 新端点自动继承 `reject_nul_path_params`;新增的 query 参数(`name`)要显式过 `reject_nul` |
| 长度上界 | `name` query 参数 `max_length=512`。**写入侧没有任何长度限制** —— `artifact.name` 是 `Text` 列(迁移 `0019_artifact.py:68`),`save_artifact` 工具只校验"是不是字符串"(`tools/artifact.py:124`)。所以这个上界不是"对齐写入侧",而是挡住异常长的查询参数别一路打到 store —— 与 P2-b 给 `workspace/file` 的 `path` 加 4096 完全同款理由(当时一个 30004 字符的 path 一路 200 穿到 supervisor)。512 对文件名式的逻辑名(`'report.md'`)有充足余量 |
| 平面分区自审 | `/v1/agent-catalog` 是新前缀,必须在 `test_route_plane_partition.py` 里有归属,否则自审报未分类 |

---

## 六、文档站

**五条**新端点(3.1 一条 + 3.2 一条 + 3.3 三条)都要进公开文档站(`apps/admin-ui/docs-site/`):

- **第 4 章 `run-agent.md`**:3.2 的 runs 列表 + 3.3 的三条,挂在既有「会话列表与历史消息」「工作区文件」旁边
- **新增 agent 目录一节**:3.1 放在第 4 章开头(客户端的第一步就是"我能调哪个 agent")
- **第 6 章 `errors.md`**:如有新错误码补进总表
- **侧栏 `config.mts`**:补 anchor

**第 8 章四语言示例**:不强制为新端点新增。8.1–8.7 已覆盖全部调用模式(带信封的 GET、二进制下载、错误处理),新端点是同款形状。若评审认为某条形状确实新(如 DELETE 带 query),再单独补。

**机密红线照旧**:公开文档不得出现凭据、密钥名、金库路径、内网地址、集群串、内部服务名、内部模块路径。

---

## 七、测试要求

### PR-A

1. `available` 与 `_resolve_session` **同判据**:构造「已禁用」「无 ACTIVE 版本」两种 agent,断言目录里 `available=false` **且**直接发 run 确实被拒(同一个测试里两边都验,防止两处判据各自漂移)
2. `display_name` 空 → 回落到 `agent_code`;非空 → 原样返回
3. 零 scope key 打 `/v1/agent-catalog` → 403;read scope → 200
4. `/v1/agent-catalog` 在平面分区自审里有归属
5. runs 列表:漏传 `user_id` → 422;陌生 `user_id` → 空列表且**不新建 tenant_user 行**(查库断言);`session_id` 指向别人的会话 → 404
6. **`list_for_tenant(agent_name=...)` 的 SQL / in-memory 等价性测试**,且 SQL 侧必须跑真容器集成测(`DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`)
7. `error` 字段:跑一个必失败的 run,断言列表里的 `error` 与 SSE `error` 帧的 `message` **字符串相等**(这条同时是"没新开泄露面"的可执行证明)

### PR-B

1. 跨用户产物 → 404(列表为空 / 下载 404 / 删除 404,三条都验)
2. 软删后:列表不含、下载 404、再删一次仍 404(幂等)
3. 删除**不动工作区字节**:软删后直接读 `workspace/file` 同路径仍能下到(证明只是元数据软删)
4. 下载扣配额:配额打满后 → 配额拒绝响应
5. 权限失败与不存在**分开**:mock `WorkspacePermissionError` → 500,`SandboxSupervisorError` → 404
6. active content(HTML/SVG)强制 `attachment` + `nosniff`
7. `name` 含 `/`、含 NUL、超长 → 分别 200(合法名)/ 422 / 422

### 通用

- 变异自证:每条新断言必须 break → red → restore → green(记忆 `fix-tests-certify-broken-version`)
- 契约测试 `test_external_api_contract.py` 补新端点
- **真栈验收**:三个端点在测试集群上各跑一次真调用,不是只跑单测

---

## 八、不做什么(以及为什么)

| 不做 | 为什么 |
|---|---|
| agent 目录带版本号 / 能力清单 | 第三方不选版本;能力清单属于 manifest 面,`85abdb39` 刻意关死 |
| run 列表带 token 用量 / 耗时明细 | 那是控制台的可观测面。第三方要细节可以拉该 run 的事件流 |
| 产物版本历史 / 下载旧版 | 客户端界面价值低,`latest_version` 已经回答了"改过几版" |
| 产物按 session 过滤 | 产物是 `(tenant, user)` 维度、`name` 唯一,跨会话更新同一个 name 是正常用法,按 session 过滤语义含糊。登记制本身已经解决"分主次" |
| 给 `workspace/files` 加删除 | 产物视图是更好的答案。裸文件删除缺上下文(第三方不知道哪个文件正被 agent 用着) |

---

## 九、实施顺序

```
PR-A(3.1 + 3.2)→ 评审 → 合并
        ↓
PR-B(3.3)→ 评审 → 合并
        ↓
发布测试环境 + 真栈验收(五条新端点全跑)
```

两个 PR 无代码耦合,理论上可并行开分支;但 PR-B 的文档站改动会碰 PR-A 改过的同一批文件(`run-agent.md` / `config.mts`),串行更省一轮冲突处理。
