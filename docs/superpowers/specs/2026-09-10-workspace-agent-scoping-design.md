# 用户工作区加 agent 维度 —— 设计

> 2026-09-10 立项。用户提出:「工作区是按用户隔离的,那这个用户可以用多个 Agent,那 Agent 读文件不会读串吗?」
> 答案是会,而且**已经在串**。本文是设计,不是计划;实施计划另出。

## 一、问题

工作区的 NAS 布局是 `{root}/{tenant_id}/{user_id}`
(`orchestrator/tools/nas_workspace_store.py:357`),**没有 agent 段**。产物表的唯一键是
`(tenant_id, user_id, name)`(`migrations/versions/0019_artifact.py:83`),**没有 agent 列**。
同一个用户绑的每个 agent,读写的是同一棵树、同一个产物命名空间。

### 1.1 这不是假设,是现状

测试环境 `thread_meta` 按用户聚合(2026-09-10 实测):64 个有会话的用户里,
**8 个用过一个以上 agent**,其中 6 个正是 `ai-health-plan` + `sop2-designer` 这一对:

| user_id 前缀 | agents | threads |
|---|---|---|
| `99d3c664` | ai-health-plan, sop2-designer | 112 |
| `92c5b903` | ai-health-plan, sop2-designer | 35 |
| `062f1790` | ai-health-plan, sop2-designer | 15 |
| `175ea1a8` | ai-health-plan, sop2-designer | 14 |
| `bfba8ab8` | ai-health-plan, test-agent | 10 |
| `62bf1547` | ai-health-plan, sop2-designer | 6 |
| `36ae2aa0` | ai-health-plan, sop2-designer | 2 |
| `93396886` | pf-probe, release-canary, sop2-designer | 17 |

`99d3c664` 的工作区(435 个文件)实测长这样:

```
/                    35-60岁女性_20260910160656.{pptx,json}  ← 交付物,同时进 artifact 表
MEMORY.md                                                   ← 根级长期记忆
style/               PLAN_STYLE.md  render_plan.py
assets/              logo.jpg  bg_trend_张女士.png  *.mp4
客户案例/             三诺首单成交/  山丘联康/  秀域/
payload/             三诺首单成交/  山丘联康/  秀域/
sop_review/  review_manual/
uploads/             用户上传的原始 docx / xlsx
threads/             7 个会话 × {MEMORY,PLAN,TODO}.md
.tool_results/       40 个 run 目录,bash / skill_view / exec_python 输出缓存
```

这棵树上的 `MEMORY.md`、`style/`、`客户案例/` 已经被 **ai-health-plan 与 sop2-designer 跨 112 个会话共同读写**。
哪一句是谁写的,查不出来 —— 这些文件没有任何登记行。**这就是损失本身。**

### 1.2 三种失败形态

| | 形态 | 现有防御 |
|---|---|---|
| **事实污染** | agent B 读到 agent A 写的客户档案,当作本次任务的事实使用 | **无**。spotlighting 只声明「这是数据,别当指令」,不声明「这数据不是你的」 |
| **静默覆盖** | 两个 agent 存同名产物 → `ON CONFLICT DO UPDATE SET latest_version = latest_version + 1`,合并成一行、旧字节被覆盖(`persistence/artifact/sql.py:65-105`) | 无 |
| **上下文污染** | `list_artifacts` / `list_dir` 无差别返回全部,agent 只能靠文件名猜哪些跟本次有关 | 无 |

### 1.3 工具描述在说反话

- `list_artifacts` — "List the named artifacts **you** have saved",实际 `store.list_for_user(tenant_id, user_id)`,无 agent 过滤(`orchestrator/tools/artifact.py:179`)
- `list_dir` — "List the entries of a directory in **the agent's** workspace",实际 `/workspace` 是用户根(`orchestrator/tools/file_ops.py:610`)

喂给模型的事实是错的。模型据此认为列出来的都是自己的。

### 1.4 对外端点把 agent 显式丢掉

`external_artifacts.py` 三个 handler 每个第一行都是 `del agent_code`(`:147`、`:198`、`:322`),
模块 docstring 把「不按 agent 分」写成既定语义(`:12-15`)。URL 里有 `agent_code`,查询条件里没有。

上传相反 —— `POST /v1/agents/{agent_code}/uploads` 的 `agent_code` **是真在用的**
(过 kill-switch 闸、进 `_resolve_session`,`external_uploads.py:194-255`),
且 `user_upload.thread_id` 是 **NOT NULL**(`models/user_upload.py:29`)。
**平台在上传那一刻就知道是哪个 agent,只是写盘时丢掉了。**

## 二、这个问题代码库已经解过一次

同一个失败形态在**沙箱本地盘**上已经被识别并解决(沙箱迁移波 2):

```python
SANDBOX_SKILLS_ROOT = "/opt/skills"    # + <agent_key>
#   "per-agent namespaced so two agents sharing one warm sandbox
#    never clobber each other's skill files"

SANDBOX_AGENTS_ROOT = "/opt/agents"    # + <agent_key>
#   "Same sharing problem as skills: two agents on one warm sandbox share
#    $HOME/.local by default, so a pip install --user from one can clobber
#    or race the other's"
```

(`persistence/workspace/layout.py:36-55`)

`agent_key = sanitize_agent_key(spec.metadata.name)`(`agent_factory.py:747`,
定义 `tools/skill_seed.py:159-188`,= 清洗后的名字 + 原始名 sha256 前 8 位)**已经存在**,
已经绑在 SandboxRuntime 上(`agent_factory.py:792`),
已经有一个「每次 exec 按 agent 注入」的钩子 `agent_key_envs()`(`tools/sandbox.py:95-108`)。

**本设计 = 把这个已经被两次采纳的模式,应用到唯一漏掉的那一处(NAS 工作区)。**
不是发明新分层。

## 三、约束:挂载点不能按 agent 分

热沙箱按 `(tenant_id, user_id)` 复用 —— `sandbox_instance` 表**没有 agent 列**
(`models/sandbox_instance.py:25-47`),`acquire()` 也不收 agent 参数
(`agent_sandbox.py:545-553`,且 `del thread_id`)。**一个沙箱同时服务该用户的所有 agent**,
这正是 `/opt/skills/<agent_key>` 当初存在的理由。

CSI 的 `subPath` 在 create 时钉死(`agent_sandbox.py:1172-1190`),一个沙箱只挂一次。
所以把 `/workspace` 挂到 `agents/<agent_key>/` 有且只有两条路,都不可取:

- 每个 agent 一个沙箱 → 热复用失效(实测冷起 31s vs 热命中亚秒)
- 挂多个 subPath → 仍然要在 create 时知道本次是哪个 agent,回到上一条

另有一条构造期硬闸明文禁止改挂载点(`agent_sandbox.py:388-401`):
`workspace_pv_name` 已配时任何非空 `workspace_subpath_prefix` 直接 `ValueError`,
理由写死为「`NasWorkspaceStore` 的布局恒为 `{root}/{tenant_id}/{user_id}`」。

**结论:挂载点保持用户根,`workspace_user_root()` 不动,那条硬闸不动。
分层做在路径上,执行做在工具边界 —— 与 `/opt/skills/<agent_key>` 完全同构。**

## 四、布局

```
{root}/{tenant_id}/{user_id}/
  shared/                              ← 反推不出归属的 legacy;新用户为空
  agents/<agent_key>/
    MEMORY.md  style/  assets/  客户案例/  …   ← agent 持久工作态,无 TTL
    artifacts/                         ← 该 agent 的交付物
    uploads/                           ← 经该 agent 的会话上传的文件
    threads/<thread_id>/               ← 会话投影 PLAN/TODO/MEMORY.md
    .tool_results/<run_id>/            ← 纯缓存
  skills/  _scratch/                   ← 保留段,不动
```

`<agent_key>` 复用 `sanitize_agent_key()`,与 `/opt/skills/<agent_key>` 同一个值。

### 4.1 三层寿命

| 层 | 内容 | 寿命 |
|---|---|---|
| **纯缓存** | `.tool_results/<run_id>/` | 可清。**今天完全没有清理**,实测单用户堆了 40 个 run 目录 |
| **会话投影** | `threads/<thread_id>/` | 已随对话生死 + 无主 24h 宽限(B-27,`orphan_threads.py:36`) |
| **agent 持久工作态** | `MEMORY.md` / `style/` / `客户案例/` / `artifacts/` / `uploads/` | **无 TTL**。它是 agent 跨轮一致性的载体,按时间删不合理(用户 09-10 拍板) |

> 立项时曾提「中间文件按 TTL 清」,用户指出 `MEMORY.md` / `style/*` 是多轮一致性的保证,
> 按时间删不成立。核实后确认:该 TTL 只对 `.tool_results/` 成立,对第三层不成立。已按此定稿。

## 五、工具行为

### 5.1 读可达,写单点

- **默认根** = `agents/<agent_key>/`。裸相对路径(`MEMORY.md`、`style/x.md`)一律在这里解析。
- **`shared/` 可读不可写**,且**不与默认根合并**:必须写 `shared:` 前缀才能读到
  (`read_file("shared:MEMORY.md")`)。`list_dir(".")` 列的是 agent 自己的目录,
  另外附一条合成条目提示 `shared/` 存在且怎么读 —— agent 得知道它在那儿,
  但不能让 legacy 文件默默混进日常列表里(那正是要治的病)。
- **写** 一律落 `agents/<agent_key>/`。`shared:` 前缀的写入**拒绝**,不静默改写目标。

`shared/` 可读是**迁移期的连续性要求**:那 8 个用户的 `MEMORY.md`/`style/` 是工作记忆,
迁移日读不到就当场归零。写只进自己目录,于是各 agent 的新记忆从此分开,`shared/` 冻结并随时间失效。

### 5.2 具体改动

| 点 | 现状 | 改成 |
|---|---|---|
| exec `cwd` | `cwd=WORKSPACE_ROOT`(`agent_sandbox.py:1472`) | `WORKSPACE_ROOT/agents/<agent_key>` —— 相对路径天然落对 |
| `read_file`/`write_file`/`list_dir`/`edit_file` | 相对 `/workspace` 解析 | 相对 agent 目录解析;`shared:` 前缀落 `shared/`(照 ADK 的 `user:` 前缀约定) |
| `save_version` | `path` 缺省 = `name`,不加前缀(`tools/artifact.py:133`) | 落 `agents/<agent_key>/artifacts/` |
| `list_artifacts` | `list_for_user(tenant, user)` | 默认只列本 agent;描述改成实话 |
| `list_dir` 描述 | "the agent's workspace" | 说明并集语义与 `shared:` 前缀 |

### 5.3 这是命名空间,不是安全边界

`bash` / `exec_python` 能 `cd /workspace && ls` 走出去。与 `/opt/skills/<agent_key>` 的性质完全一致 ——
**它防的是「拿错」,不防「故意拿」**。同一用户的不同 agent 之间本就不构成信任边界
(跨用户、跨租户的隔离由归属轴和 RLS 负责,不在本设计范围)。

这一条要写进工具描述与文档,**不要在别处把它说成隔离**。

## 六、数据模型

### 6.1 artifact 加 agent 维度

唯一键 `(tenant_id, user_id, name)` → `(tenant_id, user_id, agent_key, name)`。

同名不再合并:两个 agent 各存 `报告.docx` = 两条独立 artifact 行,各自版本序列。
`ON CONFLICT` 分支的语义不变,只是键宽了一位。

### 6.2 ToolContext 需要 agent 身份

`ToolContext` 今天**没有** `agent_key` / `agent_name` / `agent_spec_sha` 任何一个
(`tools/registry.py:174-277`),run config 的 `configurable` 也没有
(`run_queue_worker.py:353-360`、`sse.py:380-397`)。

`agent_key` 只绑在 SandboxRuntime 上,只用于 `/opt/skills`、`/opt/agents` 两个沙箱本地路径。
本设计要把它送进 ToolContext —— 这是所有工具侧改动的前置。

### 6.3 对外端点

`external_artifacts.py` 三处 `del agent_code` 改成真用它过滤。
这是**对外行为变更**:对接方今天用 A agent 的 key 能列到 B agent 的产物,改后列不到。
对外文档五页需同步(chat / query / errors / examples / conventions 按实际涉及页)。

## 七、迁移

### 7.1 判据(用户 09-10 拍板:能反推的反推,反推不出的进 `shared/`)

**单 agent 用户(实测 56/64)** —— 整棵树归那个 agent,零歧义。

**多 agent 用户(实测 8/64)** —— 逐类反推:

| 内容 | 反推路径 | 可推? |
|---|---|---|
| `uploads/` | `user_upload.thread_id` → `thread_meta.agent_name` | ✅ `thread_id` NOT NULL |
| 产物 | `artifact_version.created_in_thread` → `thread_meta.agent_name` | ✅ |
| `threads/<id>/` | `thread_id` 直接查 | ✅ |
| `.tool_results/<run_id>/` | `run_id` → `agent_run` → thread → agent | ✅ |
| `MEMORY.md` / `style/` / `客户案例/` / `payload/` / `assets/` / 根级散文件 | 无登记行 | ❌ → `shared/` |

### 7.2 已接受的代价

那 8 个用户的第三层 legacy 文件**留在 `shared/` 且读串问题不被本次消除**,只随时间稀释。
立刻消除的唯一办法是人工判定每个文件归谁 —— 需要业务侧认领,不在工程范围。**用户已知悉并接受。**

### 7.3 迁移必须同步改的消费点

`path_in_workspace` 拼真实路径的七处:

- `api/artifacts.py:224-228`(控制台下载)、`:264`(MIME)
- `api/external_artifacts.py:249-251`(对外下载)、`:285`(MIME)
- `api/sessions.py:668-671`(会话维度下载)、`:690`(MIME)
- `retention-cleanup-job/job.py:538-549`(`unlink_registered_file`)

只展示不拼路径的(回显值会变,前端无需改逻辑):
`api/artifacts.py:409`、`admin-ui/src/pages/user_detail/ArtifactsPane.tsx:235`。

路径形状硬编码的三处:

- 会话 purge 钩子删 `threads/<id>/`(`api/sessions.py:996-998`)
- 孤儿扫描要求 tenant/user/thread **三层都是 UUID 名**(`retention_cleanup_job/workspace_files.py:205-228`、`orphan_threads.py:56-72`)—— 新布局多了 `agents/<agent_key>/` 一层,枚举器必须跟着改
- 留存 job 手抄的 `user_root()`(`workspace_files.py:64-72`,不 import,parity 靠测试钉)

保留段:`WORKSPACE_RESERVED_PREFIXES` 今天只有 `skills` / `uploads`(`layout.py:58-61`)。
`uploads` 迁进 agent 目录后,该常量与 `is_reserved_workspace_path()` 的语义需重新定;
`agents` / `shared` 是否进保留段决定它们在浏览面是否可见。

浏览面:`GET /v1/workspace/files` 走 `os.walk` 用户根全量平铺
(`nas_workspace_store.py:690-697`),前端再拍成树(`WorkspacePane.tsx:364-393`)。
新布局下多一层,且**应当按 agent 分组展示** —— 今天「产物」块与「文件」块之间没有任何关联字段。

## 八、不做什么

- **不改沙箱挂载点**,不动 `agent_sandbox.py:388-401` 那条硬闸,不动 `workspace_user_root()`。
- **不做跨 agent 的交付物共享层**。用户 09-10 拍板:两个 agent 不需要互读交付物。
- **不给对话加 TTL**。对话今天不会自动过期,本设计不改这一点。
- **不动跨用户 / 跨租户隔离**。那是归属轴和 RLS 的事,与本设计正交。
- **不把命名空间说成安全边界**(见 §5.3)。

## 九、验收判据

1. 同一用户下两个 agent 各存同名产物 → 两条独立 artifact 行,各自 v1,字节互不覆盖。
2. agent A 写入 `MEMORY.md` 后,agent B 的 `read_file("MEMORY.md")` 读到的是**自己的**(或不存在),不是 A 的。
3. agent B 的 `list_artifacts` 不含 agent A 的产物;`list_dir(".")` 不含 A 的工作目录。
4. `shared/` 下的 legacy 文件对两个 agent **都可读**;两者的写入都不落进 `shared/`。
5. 对外 `GET /v1/agents/{code}/artifacts` 只返回该 code 对应 agent 的产物。
6. 迁移后单 agent 用户的工作区内容**一个文件不少**,路径全部落在 `agents/<agent_key>/` 下。
7. 迁移后 8 个多 agent 用户的可反推内容各归其位,不可反推内容全在 `shared/`,总文件数守恒。
8. 留存 job 的「根目录其它文件永不触碰」不变式在新布局下仍然成立
   (`retention-cleanup-job/tests/test_workspace_invariant.py` 钉住)。

## 十、开放项(实施计划阶段定)

- `.tool_results/` 的清理规则(纳入留存 job 哪个阶段、宽限多久)。今天零清理。
- `agents` / `shared` 是否进 `WORKSPACE_RESERVED_PREFIXES`(决定浏览面可见性)。
- 迁移是在线搬还是停机搬;失败回滚姿势。
- `agent_key` 进 ToolContext 的传递链路(`configurable` → `_tool_context()`)具体改法。
