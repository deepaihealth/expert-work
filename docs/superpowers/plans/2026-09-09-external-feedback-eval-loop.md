# 对外消息级反馈(点赞 / 点踩)进评测回路 —— 实施计划(P-2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对接方能按轮(`run_id`)给 agent 回答打 👍/👎(可附评论、可改票),👎 当场进策展待审池并可一路 promote 到 `eval_dataset`,控制台能筛、能看、能审。

**Architecture:** 在既有 `feedback` 表上加 `run_id / source / item_id / updated_at` 四列与 `(tenant, run, actor)` 部分唯一索引,把「写反馈」从 append 改成 upsert;新增对外路由 `POST /v1/agents/{agent_code}/runs/{run_id}/feedback`(与 `:cancel` 同一路由族、同一套 `load_owned_run` 404 语义),`/items` `/messages` 回显本人反馈。👎 写入时同步找该 thread 已落盘的 trajectory 直接 upsert / 升级 `curation_candidate`(worker 300s 兜底不变),候选行多带 `feedback_run_id / feedback_comment / feedback_changed_at` 三列;修 promote `source` 与前端 signal 两处词表漂移让回路最后一步能走通。控制台新增 `GET /v1/sessions/{thread_id}/feedback`、`GET /v1/conversations?has_down_rated`、对话详情轮脚与候选行展示。

**Tech Stack:** FastAPI + pydantic v2(control-plane);SQLAlchemy 2 async + asyncpg + Alembic(persistence,双实现 in-memory / Postgres);React 18 + antd + vitest(admin-ui);VitePress(docs-site)。

**Spec:** `docs/superpowers/specs/2026-09-09-external-feedback-eval-loop-design.md`(行号以 main `723c5a40` 写成;本计划全部行号已在 main `4ac73c71` 上重核,出入见下节)。

## Global Constraints

- 迁移号 **`0152_feedback_run_scope`**(22 字符,上限 32),`down_revision = "0151_backfill_approval_user_id"`;P-1 用 0153,**P-2 PR1 先合**。
- 对外端点:`external_only()` + `require("session", "write")`;归属走 `load_owned_run` → 不属于 `(user, agent)` 一律 **404 `RUN_NOT_FOUND`**,不泄露存在性;响应 `{ "success", "data", "error" }` 信封。
- `comment` ≤ 4000 字、`item_id` ≤ 255 字,二者都过 `reject_nul`;路由级 `reject_nul_path_params`。
- 同 `(tenant, run, actor)` 再打 = **覆盖**(`rating / comment / item_id` 全量替换,`created_at` 不变,写 `updated_at`)。
- 👎 同步进池、👍 **不**建候选也**不**降级;👎→👍 候选保留并标 `feedback_changed_at`;👍→👎 按 👎 处理并清空该标记。
- 评论原文控制台**全员可见**(`session:read`),与会话原文 operator+ 的门槛**有意不一致**(用户拍板)。
- 对外读面只回显**该 `user_id` 自己**打的那条;别的终端用户的反馈不对外。
- `turn_seq` 保留不动(死字段,另议);配额不单列 `feedback` 动作(核对结果见 Task 3)。
- SQL store 与 in-memory store 的谓词与定序 **byte-identical**:`feedback_store.py`(`InMemoryFeedbackStore` / `DbFeedbackStore`)、`curation/memory.py` / `curation/sql.py`。
- 每条新断言按仓库规矩「实现改坏 → 红 → 改回 → 绿」自证;每个 Task 的测试步骤写明改坏哪一行、哪条断言红。
- 本地命令:根目录 `uv run --no-sync pytest <path> -q`;`uv run --no-sync ruff check <paths>` + `uv run --no-sync ruff format <paths>`;mypy CI 范围含 tests(测试里不用 lambda 挂 `no-untyped-call`,用具名函数)。admin-ui:`cd apps/admin-ui && pnpm typecheck`(裸 `tsc --noEmit` 恒绿不算数)、`pnpm vitest run <file>`;改页面前 `rg <testid> apps/admin-ui/e2e/`;i18n 两个 locale(`src/i18n/locales/en.ts` / `zh-CN.ts`)同时加键、同 object 内不得重复键(`src/i18n/__tests__/i18n.test.tsx:50` 断言键集相等)。
- 对外文档五页在 `apps/admin-ui/docs-site/guide/`,按 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md` 自检;读者是第三方开发工程师,零平台黑话,字段表穷举取值。
- 真栈验收只用探针 user `pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation` 与金丝雀 agent `release-canary`(user `canary:release`);**永不碰 `ai-health-plan` / `sop2-designer`**。API key 只经 stdin heredoc,永不落文件、永不上 argv。
- 生产 / 测试 pod 内读库一律 `python - <<'EOF'` + `from control_plane.settings import Settings` 拿 DSN,**只读 SELECT,永不 dump env、永不打印密钥**;kubeconfig `~/.kube/expert-work-test.yaml` / `~/.kube/expert-work-prod.yaml`,namespace `expert-work`,Deployment `control-plane`(label `app.kubernetes.io/name=control-plane`)。
- 不改 `docs/superpowers/ROADMAP.md`、`infra/k8s/overlays/*`、任何 Secret;不起后台任务;不用 `git checkout` / `git stash` 丢工作。

---

## 与 spec 的出入(2026-09-09 在 main `4ac73c71` 重核)

spec 行号写在 `723c5a40`,中间合了 #1447/#1449/#1452/#1453/#1455/#1456/#1457。逐条核过的出入:

| # | spec 写法 | 现状(`4ac73c71`) | 处理 |
|---|---|---|---|
| 1 | `tests/test_external_only_gate.py:66` `_EXTERNAL_ROUTES` | `:67`(#1457 B-10 改了本文件,表体 `:67-94`,最后一条在 `:92`) | 计划按 `:67` / 追加位 `:92` 之后 |
| 2 | `tests/test_external_path_param_nul_guard.py:502` `_AGENTS_ROUTER_EXTERNAL_ROUTES` 要登记 | `:513`;这张表**只列 `agents.py` 自己 router 上的四条非 `external` tag 路由**,且 `:584` 断言 `set(live) == _AGENTS_ROUTER_EXTERNAL_ROUTES`(live = 非 external tag 的 `/v1/agents/{x}/literal` 形状路由)。新端点挂在 `tags=["external"]` 的新 router 上,由 `test_every_external_agents_route_carries_the_nul_path_guard`(`:441-445` 按 tag 发现)**自动**覆盖;**登记进去反而会让 `:584` 变红** | 手工表实为 **两张**(#1、#3),第三张是自动审计。证据:`rg -n "runs/\{run_id\}:cancel" services/control-plane/tests/` → 列表里出现 `:cancel` 的只有 `test_console_lockdown.py:255` 与 `test_external_only_gate.py:77` 两处 |
| 3 | `tests/test_console_lockdown.py:251` `_EXTERNAL_AGENT_ROUTES` | `:251` 不变,表体 `:251-283`,最后一条 `:281`;`:568-589` 断言 `/v1/agents` 下每条 live 路由必须落在 `_CONSOLE_ROUTES` 或本表 | 计划按 `:281` 之后追加 |
| 4 | `api/external_sessions.py:211-280` `/messages` | `:243-312`(#1457 B-14 在 `:48-76` 加了 `_strip_image_mentions`);`load_owned_session` 返回值在 `:267` 被丢弃,回显本人反馈要接住 `meta` | 计划按新行号 |
| 5 | `tests/test_rls_integration.py:270` | 测试 `test_feedback_tenants_cannot_see_each_other` 在 `:264-301`,夹具 `feedback_rls_store` 在 `:249-262` | 计划按 `:249` / `:264` |
| 6 | `api/conversations.py:243-253` `narrowed_ids` | `:243-258`(`narrowed_ids` 声明 `:243`,`set.intersection` `:258`) | 计划按 `:243-258` |
| 7 | spec §3 `curation_candidate` 加**两**列 | §5「改票:候选行加 `feedback_changed_at`」需要第**三**列 | 0152 一次加三列 |
| 8 | spec §6「复用 `feedback_store.py:65 down_rated_threads`」做 `has_down_rated` | `down_rated_threads(thread_ids=…)` 是「给定候选集取子集」;列表筛选需要**租户内枚举**有 👎 的 thread。照 `RunStore.thread_ids_with_runs`(`packages/expert-work-runtime/src/expert_work/runtime/runs/store.py:393-416`,cap 500)新加 `down_rated_thread_ids(tenant_id, limit=500)` | Task 2 新方法,SQL / 内存同谓词 |
| 9 | spec §2.3 说词表漂移**两**处 | 第三处:前端 `api/curation.ts:39` `feedback_rating: number \| null`,后端是 `"up" \| "down" \| null`(`protocol/eval_dataset.py:57`) | Task 11 一并修 |
| 10 | spec §2.1「`turn_seq` 全仓无读取方」 | 核实成立:`rg -n "turn_seq" services/ packages/ apps/admin-ui/src --glob '!*/tests/*' --glob '!*.test.*' --glob '!*/migrations/*'` 命中 12 处,全是存 / 透传 / 前端写入(`FeedbackBar.tsx:45`),没有一处拿它做逻辑 | 不动 |
| 11 | spec §2.2「内部消费者四处」 | 核实成立:`rg -n "\.list_for_thread\(\|\.down_rated_threads\(\|\.list_unprocessed_down_all_tenants\(" services/ packages/ --glob '!*/tests/*' --glob '!feedback_store.py'` → `skill_rollback_gate.py:115`、`curation_worker.py:240`、`skill_evolution_wiring.py:483`、`feedback_consumer.py:149`,恰好四处 | 四处都不改签名(新读路径走新方法) |
| 12 | (spec 未提)`list_for_thread` 无租户谓词,靠 RLS | 运行期 app 以 super+bypassrls 连库(memory `rls-inert-runtime-superuser`),RLS 兜底是空的 | 新读路径一律带显式 `tenant_id` 谓词(`list_for_thread_scoped`),老四处不动 |
| 13 | (spec 未提)`docs/superpowers/plans/` | 仓库里没有这个目录(`git ls-files docs/superpowers/plans` 为 0) | 本 PR 创建 |
| 14 | spec §4.1 `api/external_runs.py:169` | 不变(`@router.post("/{agent_code}/runs/{run_id}:cancel"` 在 `:169`) | — |
| 15 | 其余 spec 行号 | `feedback.py:18-44`、`0014_feedback.py:34-72`、`api/feedback.py:48-104`/`:51`、`curation_worker.py:93-110`/`:191-267`/`:213-217`/`:239-243`、`api/curation.py:246-256`/`:310-373`、`protocol/eval_dataset.py:36`、`CandidatesPanel.tsx:141`、`api/curation.ts:20-27`、`api/_external.py:50-69`/`:349-398`、`external_session_items.py:104-130`/`:353-500`、`conversation_items.py:14-17`、`query.md:804`、`feedback_consumer.py:146-195`、`skill_evolution_wiring.py:482-487`/`:535-572`、`skill_rollback_gate.py:115`、`components/console/types.ts:25-29`、`ledger.ts:204`、`test_user_purge.py:276` | 全部核对一致 |

## 文件结构(新建 / 修改一览)

**新建**
- `packages/expert-work-persistence/migrations/versions/0152_feedback_run_scope.py` — 迁移。
- `packages/expert-work-persistence/tests/test_feedback_store_upsert.py` — 反馈 store 新方法,双实现同一场景。
- `services/control-plane/src/control_plane/api/external_feedback.py` — 对外写反馈端点(单一职责:归属校验 + upsert + 同步进池 + 审计)。
- `services/control-plane/src/control_plane/feedback_candidates.py` — 👎 同步进池 / 改票标记的纯逻辑(端点与控制台共用)。
- `services/control-plane/tests/test_external_feedback.py`、`tests/test_feedback_candidates.py`。
- `apps/admin-ui/src/components/console/FeedbackSummary.tsx` — 轮脚只读反馈展示。
- 本计划文件。

**修改(按 PR)**
- PR1:`models/feedback.py`、`models/eval_dataset.py`、`feedback_store.py`、`api/__init__.py`、`app.py`、`api/external_sessions.py`、`api/external_session_items.py`、`api/feedback.py`、两张路由表、`test_feedback_api.py`、`test_user_purge.py`、`test_rls_integration.py`、`test_external_sessions.py`、`test_external_session_items.py`、`api/sessions.ts`、`FeedbackBar.tsx`、`TurnFooter.tsx`、`TurnFooter.test.tsx`、docs 五页 + `.vitepress/config.mts`。
- PR2:`protocol/eval_dataset.py`、`curation/base.py`、`curation/sql.py`、`curation/memory.py`、`trajectory/reader.py`、`curation_worker.py`、`api/curation.py`、`api/external_feedback.py`、`api/feedback.py`、`api/curation.ts`、`CandidatesPanel.tsx`、对应测试。
- PR3:`api/feedback.py`(GET)、`api/conversations.py`、`feedback_store.py`(已在 PR1 加好方法)、`api/sessions.ts`、`api/conversations.ts`、`ConversationsList.tsx`、`ConversationDetail.tsx`、`Transcript.tsx`、`TurnBlock.tsx`、`TurnFooter.tsx`、`FeedbackSummary.tsx`、`CandidatesPanel.tsx`、两个 locale、对应测试。

---

### Task 0: 真跑确认(spec §7-1 / 7-2 / 7-3)并回填 spec

**Files:**
- Modify: `docs/superpowers/specs/2026-09-09-external-feedback-eval-loop-design.md`(§7 第 1、2、3 条划线 + 结果)

**Interfaces:**
- Consumes: 测试 / 生产 kubeconfig;探针 user;控制台登录态(Playwright storage,照 memory `live-console-smoke-via-playwright-storage`)。
- Produces: 三条结论回填进 spec §7;§7-3 的结论决定 Task 8 的并发集成测是否必须(默认必须)。

- [ ] **Step 1: §7-1 本地先证 promote 恒 422 与 signal 筛空**

Run(根目录):

```bash
uv run --no-sync python - <<'EOF'
from pydantic import ValidationError
from control_plane.api.curation import _PromoteBody
try:
    _PromoteBody(name="x", source="promoted_candidate")
    print("promoted_candidate ACCEPTED (unexpected)")
except ValidationError as exc:
    print("promoted_candidate -> 422:", exc.errors()[0]["type"])
from typing import get_args
from expert_work.protocol import CurationSignal
print("backend signals:", get_args(CurationSignal))
EOF
```

Expected: 第一行打印 `promoted_candidate -> 422: literal_error`;第二行 `('negative_feedback', 'failed_outcome', 'positive_feedback', 'implicit_success')` —— 前端 `SIGNAL_OPTIONS`(`CandidatesPanel.tsx:56-62`)里的 `manual / tool_failure / timeout / policy_block` 四个值后端不认,`GET /v1/curation/candidates?signal=tool_failure` 会 422(`api/curation.py:212` 的 `CurationSignal | None` Query)。

- [ ] **Step 2: §7-1 测试环境真栈复现(用户登录态,无头)**

先由用户登一次(照 memory 配方,storage 文件放 scratchpad,不入仓):

```bash
cd apps/admin-ui && pnpm exec playwright codegen --save-storage=/private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad/console-test.json https://expert-work-test.deepaihealth.com
```

然后无头复用 token 打两条请求(token 只从 storage 文件读进进程,不打印):

```bash
cd apps/admin-ui && node - <<'EOF'
const fs = require("node:fs");
const storage = JSON.parse(fs.readFileSync(process.env.STORAGE, "utf8"));
const entry = storage.origins.flatMap((o) => o.localStorage).find((e) => e.name === "expert_work_token");
const token = entry.value;
const base = "https://expert-work-test.deepaihealth.com";
(async () => {
  const list = await fetch(`${base}/v1/curation/candidates?status=pending&signal=tool_failure`, { headers: { Authorization: `Bearer ${token}` } });
  console.log("signal=tool_failure ->", list.status);
  const pending = await fetch(`${base}/v1/curation/candidates?status=pending`, { headers: { Authorization: `Bearer ${token}` } });
  const items = (await pending.json()).items ?? [];
  console.log("pending candidates:", items.length);
  if (items.length === 0) { console.log("no candidate to promote; 422 proof stays local (Step 1)"); return; }
  const resp = await fetch(`${base}/v1/curation/candidates/${items[0].id}/promote`, {
    method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ name: "p2-task0-probe", source: "promoted_candidate" }),
  });
  console.log("promote source=promoted_candidate ->", resp.status, (await resp.text()).slice(0, 200));
})();
EOF
```

Run with: `STORAGE=/private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad/console-test.json node - <<'EOF' ... EOF`(把上面的脚本喂进去)。
Expected: `signal=tool_failure -> 422`;有候选时 `promote source=promoted_candidate -> 422`(信封 `detail` 里含 `literal_error`)。注意 storage 里 token 键名以 `apps/admin-ui/src/api/client.ts` 的 `getStoredToken` 为准,先 `rg -n "localStorage" apps/admin-ui/src/api/client.ts` 核对。

> **✅ 已执行(09-09):拿不到登录态时的等价路径(给后来人)。** 本步要求用户先跑一次 `playwright codegen --save-storage` 亲自登一次;这个前提当时不满足(scratchpad 里也没有存量 storage),于是改走**进程内真 HTTP**:复用 `services/control-plane/tests/test_curation_api.py` 的 `ctx` 夹具(真 `create_app()` + 真路由 + 真 JWT,`auth_mode="dev"`),在临时探针文件里对同一批端点发请求,拿到的是**真实状态码**而非读代码推断的结论;载荷逐字对齐 `CandidatesPanel.tsx:141` 的实际发送内容。
>
> 结论强度等价的理由:curation 端点挂 `require("session","write")` / `("session","read")`,**依赖先于 body/query 校验求解** —— 没有登录态只会拿到 401,原步骤本来也必须先有 token 才走得到 422。唯一没覆盖的是「在真实浏览器里点下按钮」那一层,而那一层的结论另有出路(见 Task 5 的「前置事实」:控制台写路径今天只在调试台可达)。
>
> 探针文件放 `services/control-plane/tests/` 下才吃得到 `conftest` 夹具,**跑完必须删掉**并用 `git status` 确认工作树只剩预期改动。

- [ ] **Step 3: §7-2 生产库只读三问(pod 内,永不打印 DSN)**

```bash
kubectl --kubeconfig ~/.kube/expert-work-prod.yaml -n expert-work exec -i deploy/control-plane -- python - <<'EOF'
import asyncio, asyncpg
from control_plane.settings import Settings
dsn = Settings().db_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
async def main():
    conn = await asyncpg.connect(dsn)
    try:
        print("role:", await conn.fetchrow("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        print("feedback rows:", await conn.fetchval("SELECT count(*) FROM feedback"))
        print("feedback by rating:", await conn.fetch("SELECT rating, count(*) FROM feedback GROUP BY rating"))
        print("turn_seq shape:", await conn.fetch("SELECT turn_seq, count(*) FROM feedback GROUP BY turn_seq ORDER BY turn_seq NULLS FIRST LIMIT 20"))
        print("turn_seq max:", await conn.fetchval("SELECT max(turn_seq) FROM feedback"))
        print("curation_candidate rows:", await conn.fetchval("SELECT count(*) FROM curation_candidate"))
        print("curation_candidate by signal/status:", await conn.fetch("SELECT signal, status, count(*) FROM curation_candidate GROUP BY 1, 2"))
        print("eval_dataset by source:", await conn.fetch("SELECT source, count(*) FROM eval_dataset GROUP BY 1"))
    finally:
        await conn.close()
asyncio.run(main())
EOF
```

Expected: 第一行 `rolbypassrls=True`(否则 FORCE-RLS 表在无 GUC 下读到 0 行,后面的计数不可信,改用 `SET ROLE audit_reader` 重跑);`turn_seq` 若全是小整数(0/1/2…)且 `max` 与会话轮数同量级 → 实证它是 UI 局部序号,不是 `event_log.seq`。测试环境同样跑一遍(`~/.kube/expert-work-test.yaml`)。

- [ ] **Step 4: §7-3 多副本下 curation worker 是否两个 pod 都在跑**

```bash
rg -n "ENABLE_CURATION_WORKER" infra/k8s/base/configmap.yaml infra/k8s/overlays/prod/ infra/k8s/overlays/test/
kubectl --kubeconfig ~/.kube/expert-work-prod.yaml -n expert-work get pods -l app.kubernetes.io/name=control-plane -o name
for p in $(kubectl --kubeconfig ~/.kube/expert-work-prod.yaml -n expert-work get pods -l app.kubernetes.io/name=control-plane -o name); do
  echo "== $p"; kubectl --kubeconfig ~/.kube/expert-work-prod.yaml -n expert-work logs "$p" --since=24h | rg -c "curation_worker\.(swept|cycle_failed|key_failed)" || echo "0 curation_worker log lines"
done
```

Expected: `configmap.yaml:34` 是 `"true"` 且两个 overlay 都没有覆盖 → 两个 pod 都跑(`app.py:1665-1675` 每个进程都构造 `CurationWorker`);日志计数两边都 >0 即坐实。结论写进 spec §7-3:同步 upsert 与 worker 的竞争靠 `curation_candidate_trajectory_uniq` + `ON CONFLICT DO NOTHING`(`curation/sql.py:246`)+ 升级 UPDATE 幂等;Task 8 的并发集成测(两副本同一 key 同时 upsert 只成一行)是必做项。若某 pod 24h 内一条日志都没有,再看 `logs --previous` 与 `_worker_cycle_errors` 指标,别下「没在跑」的结论(`swept` 只在 detected>0 时打)。

> **⚠️ 已执行(09-09):上面这条「日志计数两边都 >0 即坐实」的判据在实践中不成立。** `curation_worker.swept` 只在 `detected > 0` 时打(`curation_worker.py:322`),而两个环境近期都没有**新**检测(测试环境最后一次 detected_at 是 08-28),所以四个 pod 的 `rg -c "curation_worker\."` 全是 0 —— 本步原文已经提醒了「别下没在跑的结论」,但没给替代判据。**替代判据(实测有效,建议后来人直接用这两条)**:
>
> 1. **`pg_stat_activity` 按 `client_addr` 采样**:在任一 pod 内连库,每 0.5s 取一次 `SELECT host(client_addr), left(regexp_replace(query,'\s+',' ','g'),90) FROM pg_stat_activity WHERE datname = current_database() AND client_addr IS NOT NULL AND pid <> pg_backend_pid()`,滚 **≥ 340s**(> 一个 300s 周期),把 query 里含 `curation_candidate` / `thread_meta` / `trajector` 的行按 IP 归堆。worker 的预检查询会把自己暴露出来。**实测:测试环境 `172.16.176.31` 于 t=178s、`172.16.176.32` 于 t=276s 各自独立出现,相隔 ~98s = 两 pod 启动错峰的相位差 → 两个 pod 都在扫,坐实。** 注意别只看 `state='idle'` 的 `query` —— 连接池上最后一条语句常被 `ROLLBACK;` / `COMMIT;` 盖掉,要靠高频采样抓 `active` 的那一瞬。
> 2. **per-pod metrics 计数器**(比日志强,因为它是进程内累计值):逐 pod 取 `http://127.0.0.1:8000/metrics` 里的 `expert_work_control_plane_curation_candidates_detected_total` —— 该计数器**只在 `curation_worker.py:228` 的 `run_once()` 内自增**,非零即证明该 pod 的 loop 真的跑过并落过行。**实测:生产 pod `…-sdpzg` = `1.0`,同期 `…-pz5s8` = `0.0`** —— 这一对数正是「两边都扫、谁先扫到谁建行、另一边预检跳过」的形态,**`0.0` 不能读成「没在跑」**。
>
> 另外两条实测顺带记下:①`rolbypassrls` 前置检查通过 —— 生产 `expert_work` / 测试 `expert_work_dev` 都是 `rolsuper=False, rolbypassrls=True`,计数可信,**不需要** `SET ROLE audit_reader`。②本步 Expected 里的 `app.py:1665-1675` 外面还套着 `app.py:1389 if agent_runtime is None:` —— 部署形态走这个分支,注入 runtime 的测试才跳过;判「每个进程都起」要连这层一起看。

- [ ] **Step 5: 回填 spec §7 并提交**

把 §7 第 1、2、3 条改成 `~~原文~~ **✅ 已核实(日期)**:<一句话结论 + 关键数字>`,格式照第 4、5 条。

```bash
git add docs/superpowers/specs/2026-09-09-external-feedback-eval-loop-design.md
git commit -m "docs(spec): P-2 §7 真跑确认 1/2/3 回填(promote 422 实证 / 生产 feedback·candidate 行数与 turn_seq 形态 / 多副本 worker)"
```

---
## PR1 —— 存储 + 对外端点 + 回显 + 文档

### Task 1: 迁移 0152 + ORM 模型两张表加列

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0152_feedback_run_scope.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/feedback.py:11-44`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/eval_dataset.py:67-117`
- Test: `packages/expert-work-persistence/tests/test_feedback_store_upsert.py`(本 Task 只放迁移形状测试;Task 2 往同文件加 store 测试)

**Interfaces:**
- Consumes: 现有 `FeedbackRow`(`models/feedback.py:18-44`)、`CurationCandidateRow`(`models/eval_dataset.py:67-117`)、上一版本 `0151_backfill_approval_user_id`。
- Produces: `feedback` 表新列 `run_id UUID NULL`、`source TEXT NOT NULL DEFAULT 'console'`(CHECK `source IN ('console','external')`)、`item_id TEXT NULL`、`updated_at TIMESTAMPTZ NULL`;部分唯一索引 `feedback_run_actor_uniq (tenant_id, run_id, actor_id) WHERE run_id IS NOT NULL`;索引 `ix_feedback_tenant_run (tenant_id, run_id)`。`curation_candidate` 新列 `feedback_run_id UUID NULL`、`feedback_comment TEXT NULL`、`feedback_changed_at TIMESTAMPTZ NULL`;部分索引 `ix_curation_candidate_feedback_run (tenant_id, feedback_run_id) WHERE feedback_run_id IS NOT NULL`。ORM 属性同名。

- [ ] **Step 1: 写迁移形状的失败测试(Postgres 集成,照 `test_feedback_store_delete.py` 的容器夹具)**

```python
# packages/expert-work-persistence/tests/test_feedback_store_upsert.py
"""``FeedbackStore.upsert`` / ``list_for_thread_scoped`` / ``down_rated_thread_ids`` — P-2 PR1.

迁移 0152 的形状先在这里钉住(部分唯一索引真的拒绝重复),再对两套实现跑同一场景。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import DatabaseConfig, create_async_engine_from_config

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def migrated_engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield engine
    finally:
        await engine.dispose()


_INSERT = text(
    "INSERT INTO feedback (tenant_id, thread_id, run_id, rating, actor_id, source) "
    "VALUES (:t, :th, :r, 'down', :a, :s)"
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0152_partial_unique_index_rejects_duplicate_run_actor(
    migrated_engine: AsyncEngine,
) -> None:
    """同 (tenant, run, actor) 第二行被 ``feedback_run_actor_uniq`` 拒绝;run_id 为 NULL 的老式行不受约束。"""
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(_INSERT, {"t": tenant, "th": thread, "r": run, "a": "u-1", "s": "external"})
    with pytest.raises(IntegrityError) as exc_info:
        async with migrated_engine.begin() as conn:
            await conn.execute(_INSERT, {"t": tenant, "th": thread, "r": run, "a": "u-1", "s": "external"})
    assert "feedback_run_actor_uniq" in str(exc_info.value)
    # 老式行(run_id NULL)想插几条插几条 —— 部分索引不管它们。
    async with migrated_engine.begin() as conn:
        for _ in range(2):
            await conn.execute(_INSERT, {"t": tenant, "th": thread, "r": None, "a": "u-1", "s": "console"})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0152_source_check_and_candidate_columns(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                _INSERT, {"t": uuid4(), "th": uuid4(), "r": uuid4(), "a": "u", "s": "widget"}
            )
    assert "feedback_source_valid" in str(exc_info.value)
    async with migrated_engine.connect() as conn:
        cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'curation_candidate'"
                )
            )
        }
    assert {"feedback_run_id", "feedback_comment", "feedback_changed_at"} <= cols
```

- [ ] **Step 2: 跑测试确认红**

Run: `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run --no-sync pytest packages/expert-work-persistence/tests/test_feedback_store_upsert.py -q -m integration`
Expected: FAIL —— `asyncpg.exceptions.UndefinedColumnError: column "run_id" of relation "feedback" does not exist`。

- [ ] **Step 3: 写迁移**

```python
# packages/expert-work-persistence/migrations/versions/0152_feedback_run_scope.py
"""feedback 按 run 归因 + 对外来源 + 改票幂等键;curation_candidate 记「哪一轮被踩 + 原话 + 改票」。

P-2(spec ``2026-09-09-external-feedback-eval-loop-design.md`` §3)。今天的 ``feedback``
只有 ``thread_id``:同一 thread 可无限写重复行,对外端点没有稳定的消息 id 可打分,
三条读路径都稳定的一等 id 只有 ``run_id``。于是:

* ``run_id``(NULL = 迁移前的历史行)、``source``(``console`` / ``external``)、
  ``item_id``(对接方附带的段落标签,只存不 join)、``updated_at``(改票时间,
  NULL = 从没改过)。
* 部分唯一索引 ``(tenant_id, run_id, actor_id) WHERE run_id IS NOT NULL`` —— 「可改票」
  = upsert;历史行 ``run_id`` 为 NULL 不受约束。
* ``curation_candidate`` 三列:审阅员打开候选直接看到哪一轮被踩、用户原话、是否后改票。

``turn_seq`` 保留不动(死字段,另议)。

Revision ID: 0152_feedback_run_scope
Revises: 0151_backfill_approval_user_id
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0152_feedback_run_scope"
down_revision: str | Sequence[str] | None = "0151_backfill_approval_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("feedback", sa.Column("run_id", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "feedback",
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'console'")),
    )
    op.add_column("feedback", sa.Column("item_id", sa.Text(), nullable=True))
    op.add_column("feedback", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "feedback_source_valid", "feedback", "source IN ('console', 'external')"
    )
    op.create_index(
        "feedback_run_actor_uniq",
        "feedback",
        ["tenant_id", "run_id", "actor_id"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_index("ix_feedback_tenant_run", "feedback", ["tenant_id", "run_id"])

    op.add_column(
        "curation_candidate", sa.Column("feedback_run_id", UUID(as_uuid=True), nullable=True)
    )
    op.add_column("curation_candidate", sa.Column("feedback_comment", sa.Text(), nullable=True))
    op.add_column(
        "curation_candidate",
        sa.Column("feedback_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_curation_candidate_feedback_run",
        "curation_candidate",
        ["tenant_id", "feedback_run_id"],
        postgresql_where=sa.text("feedback_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_curation_candidate_feedback_run", table_name="curation_candidate")
    op.drop_column("curation_candidate", "feedback_changed_at")
    op.drop_column("curation_candidate", "feedback_comment")
    op.drop_column("curation_candidate", "feedback_run_id")
    op.drop_index("ix_feedback_tenant_run", table_name="feedback")
    op.drop_index("feedback_run_actor_uniq", table_name="feedback")
    op.drop_constraint("feedback_source_valid", "feedback", type_="check")
    op.drop_column("feedback", "updated_at")
    op.drop_column("feedback", "item_id")
    op.drop_column("feedback", "source")
    op.drop_column("feedback", "run_id")
```

- [ ] **Step 4: ORM 模型同步(两张表)**

`models/feedback.py`:`from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Text, func, text`(`:11`),在 `processed_at`(`:39`)之后加:

```python
    #: P-2 — 打分对象(run)。NULL = 0152 之前的历史行(只按 thread 打过分)。
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: P-2 — 'console' | 'external'(第三方 API)。
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'console'"))
    #: P-2 — 对接方附带的段落标签,只存不 join。
    item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: P-2 — 改票时间;NULL = 从没改过。
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

`__table_args__`(`:41-44`)改为:

```python
    __table_args__ = (
        CheckConstraint("source IN ('console', 'external')", name="feedback_source_valid"),
        Index("feedback_tenant_thread_idx", "tenant_id", "thread_id"),
        Index("feedback_tenant_time_idx", "tenant_id", text("created_at DESC")),
        Index("ix_feedback_tenant_run", "tenant_id", "run_id"),
        Index(
            "feedback_run_actor_uniq",
            "tenant_id",
            "run_id",
            "actor_id",
            unique=True,
            postgresql_where=text("run_id IS NOT NULL"),
        ),
    )
```

`models/eval_dataset.py` `CurationCandidateRow`:在 `retry_count`(`:97-99`)之后加:

```python
    #: P-2 — 被踩的那一轮 / 用户原话 / 👎→👍 改票时间(NULL = 没改过)。
    feedback_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

并在 `__table_args__`(`:101-117`)末尾追加:

```python
        Index(
            "ix_curation_candidate_feedback_run",
            "tenant_id",
            "feedback_run_id",
            postgresql_where=text("feedback_run_id IS NOT NULL"),
        ),
```

- [ ] **Step 5: 跑测试确认绿 + 单 head**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_feedback_store_upsert.py -q -m integration`
Expected: 2 passed。
Run: `cd packages/expert-work-persistence && uv run --no-sync alembic -c alembic.ini heads`
Expected: 恰好一行 `0152_feedback_run_scope (head)`。

- [ ] **Step 6: 变异自证**

把迁移里 `postgresql_where=sa.text("run_id IS NOT NULL")` 那一行(`feedback_run_actor_uniq`)整个删掉 → 重跑 → `test_0152_partial_unique_index_rejects_duplicate_run_actor` 在「run_id NULL 插两条」处红(`IntegrityError`);改回 → 绿。再把 `op.create_check_constraint(...)` 删掉 → `test_0152_source_check_and_candidate_columns` 在 `assert "feedback_source_valid"` 前一行红(没有抛 `IntegrityError`);改回 → 绿。

- [ ] **Step 7: lint + 提交**

```bash
uv run --no-sync ruff check packages/expert-work-persistence && uv run --no-sync ruff format packages/expert-work-persistence
git add packages/expert-work-persistence/migrations/versions/0152_feedback_run_scope.py packages/expert-work-persistence/src/expert_work/persistence/models/feedback.py packages/expert-work-persistence/src/expert_work/persistence/models/eval_dataset.py packages/expert-work-persistence/tests/test_feedback_store_upsert.py
git commit -m "feat(persistence): 0152 feedback 加 run_id/source/item_id/updated_at + (tenant,run,actor) 部分唯一索引;curation_candidate 加 feedback 三列"
```

---

### Task 2: `FeedbackStore` 新方法 —— `upsert` / `list_for_thread_scoped` / `down_rated_thread_ids`(双实现同谓词)

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/feedback_store.py:31-45`(record)、`:48-110`(ABC)、`:113-152`(memory)、`:155-264`(SQL + `_row_to_record`)
- Test: `packages/expert-work-persistence/tests/test_feedback_store_upsert.py`(续)
- Test: `packages/expert-work-persistence/tests/test_rls_integration.py:249-262`(复用夹具,加一条)

**Interfaces:**
- Consumes: Task 1 的列。
- Produces:
  - `FeedbackRecord` 新字段 `run_id: UUID | None = None`、`source: str = "console"`、`item_id: str | None = None`、`updated_at: datetime | None = None`(有默认,现有构造点 `api/feedback.py:69`、四个测试文件不用改)。
  - `async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]` —— 键 `(tenant_id, run_id, actor_id)`;返回 `(存下的行, updated)`,`updated=True` 表示覆盖了已有行;`record.run_id is None` → `ValueError("upsert requires run_id")`。覆盖只换 `rating / comment / item_id / trace_id / turn_seq / source` 并写 `updated_at=now(UTC)`;`id / created_at / processed_at` 不动。
  - `async def list_for_thread_scoped(self, *, tenant_id: UUID, thread_id: UUID) -> list[FeedbackRecord]` —— 显式租户谓词,`id` 降序(与 `list_for_thread` 同序)。
  - `async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]` —— 租户内(`None` = 跨租户,SQL 走 `SET LOCAL ROLE audit_reader`)有 ≥1 条 `rating='down'` 的 `thread_id`,按「该 thread 最早一条 👎 的 id」升序取前 `limit` 个。

- [ ] **Step 1: 写失败测试(同一场景跑两套实现)**

在 `test_feedback_store_upsert.py` 末尾追加:

```python
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence import create_async_session_factory
from expert_work.persistence.feedback_store import (
    DbFeedbackStore,
    FeedbackRecord,
    FeedbackStore,
    InMemoryFeedbackStore,
)


def _rec(
    *,
    tenant_id: UUID,
    thread_id: UUID,
    run_id: UUID | None,
    actor_id: str = "ext-user-1",
    rating: str = "down",
    comment: str | None = None,
    item_id: str | None = None,
    source: str = "external",
) -> FeedbackRecord:
    return FeedbackRecord(
        tenant_id=tenant_id,
        thread_id=thread_id,
        run_id=run_id,
        rating=rating,
        comment=comment,
        item_id=item_id,
        source=source,
        actor_id=actor_id,
    )


async def _upsert_scenario(store: FeedbackStore) -> None:
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    first, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, comment="太慢", item_id="p1")
    )
    assert updated is False
    assert first.id is not None and first.created_at is not None and first.updated_at is None

    second, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, rating="up", comment=None)
    )
    assert updated is True
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.rating == "up" and second.comment is None and second.item_id is None
    assert second.updated_at is not None

    rows = await store.list_for_thread_scoped(tenant_id=tenant, thread_id=thread)
    assert [r.id for r in rows] == [first.id]  # 一行,不是两行

    # 另一个 actor 同一轮 → 各自一行。
    other, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, actor_id="ext-user-2")
    )
    assert updated is False and other.id != first.id
    rows = await store.list_for_thread_scoped(tenant_id=tenant, thread_id=thread)
    assert [r.id for r in rows] == [other.id, first.id]  # id 降序

    # 显式租户谓词:别的租户看不到。
    assert await store.list_for_thread_scoped(tenant_id=uuid4(), thread_id=thread) == []

    with pytest.raises(ValueError, match="run_id"):
        await store.upsert(_rec(tenant_id=tenant, thread_id=thread, run_id=None))


async def _down_rated_scenario(store: FeedbackStore) -> None:
    tenant = uuid4()
    t_down, t_up, t_mixed = uuid4(), uuid4(), uuid4()
    await store.upsert(_rec(tenant_id=tenant, thread_id=t_up, run_id=uuid4(), rating="up"))
    await store.upsert(_rec(tenant_id=tenant, thread_id=t_down, run_id=uuid4(), rating="down"))
    await store.upsert(_rec(tenant_id=tenant, thread_id=t_mixed, run_id=uuid4(), rating="up"))
    await store.upsert(
        _rec(tenant_id=tenant, thread_id=t_mixed, run_id=uuid4(), rating="down", actor_id="b")
    )
    assert await store.down_rated_thread_ids(tenant_id=tenant) == {t_down, t_mixed}
    assert await store.down_rated_thread_ids(tenant_id=tenant, limit=1) == {t_down}
    assert await store.down_rated_thread_ids(tenant_id=uuid4()) == set()


@pytest.mark.asyncio
async def test_in_memory_upsert_overwrites_same_run_actor() -> None:
    await _upsert_scenario(InMemoryFeedbackStore())


@pytest.mark.asyncio
async def test_in_memory_down_rated_thread_ids() -> None:
    await _down_rated_scenario(InMemoryFeedbackStore())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_upsert_overwrites_same_run_actor(migrated_engine: AsyncEngine) -> None:
    await _upsert_scenario(DbFeedbackStore(create_async_session_factory(migrated_engine)))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_down_rated_thread_ids(migrated_engine: AsyncEngine) -> None:
    await _down_rated_scenario(DbFeedbackStore(create_async_session_factory(migrated_engine)))
```

`test_rls_integration.py` 在 `test_feedback_down_rated_threads_rls_scoped`(`:304`)之后追加(夹具 `feedback_rls_store` 是 RLS 包装的 app 角色):

```python
@pytest.mark.asyncio
async def test_feedback_upsert_and_scoped_reads_isolated_by_tenant(
    feedback_rls_store: tuple[DbFeedbackStore, AsyncEngine],
) -> None:
    """P-2 — 同一 (run, actor) 在两个租户各自 upsert 成一行;租户 A 的 scoped 读与
    ``down_rated_thread_ids`` 都看不到 B 的 👎。"""
    store, engine = feedback_rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        thread_id, run_id = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_a, thread_id=thread_id, run_id=run_id, rating="up", actor_id="x"
            )
        )
        current_tenant_id_var.set(tenant_b)
        _, updated = await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_b, thread_id=thread_id, run_id=run_id, rating="down", actor_id="x"
            )
        )
        assert updated is False  # 不是覆盖 A 的行

        current_tenant_id_var.set(tenant_a)
        a_rows = await store.list_for_thread_scoped(tenant_id=tenant_a, thread_id=thread_id)
        assert [r.rating for r in a_rows] == ["up"]
        assert await store.down_rated_thread_ids(tenant_id=tenant_a) == set()

        current_tenant_id_var.set(tenant_b)
        assert await store.down_rated_thread_ids(tenant_id=tenant_b) == {thread_id}
    finally:
        await engine.dispose()
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_feedback_store_upsert.py -q`
Expected: FAIL —— `TypeError: FeedbackRecord.__init__() got an unexpected keyword argument 'run_id'`。

- [ ] **Step 3: 实现 —— record 字段 + ABC**

`FeedbackRecord`(`feedback_store.py:31-45`)在 `processed_at` 之后加四个字段:

```python
    #: P-2 — 打分对象(run);NULL = 0152 之前的历史行。
    run_id: UUID | None = None
    #: P-2 — 'console' | 'external'。
    source: str = "console"
    #: P-2 — 对接方附带的段落标签,只存不 join。
    item_id: str | None = None
    #: P-2 — 改票时间;None = 从没改过。
    updated_at: datetime | None = None
```

ABC(`:48-110`)加三个抽象方法(放在 `delete_for_threads` 之后):

```python
    @abc.abstractmethod
    async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]:
        """Insert-or-overwrite keyed by ``(tenant_id, run_id, actor_id)`` — P-2「可改票」.

        Returns ``(stored, updated)``: ``updated`` is ``True`` when an existing
        row was overwritten. Overwrite replaces ``rating / comment / item_id /
        trace_id / turn_seq / source`` and stamps ``updated_at``; ``id``,
        ``created_at`` and ``processed_at`` are left alone. ``record.run_id``
        must be set — raises ``ValueError`` otherwise (thread-only rows are
        append-only history, never upserted).
        """

    @abc.abstractmethod
    async def list_for_thread_scoped(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[FeedbackRecord]:
        """Like :meth:`list_for_thread` but with an explicit tenant predicate,
        newest (``id``) first. New read paths use this — the runtime connects
        with a BYPASSRLS role, so :meth:`list_for_thread`'s "RLS is the tenant
        filter" contract is not something P-2 read paths can lean on.
        """

    @abc.abstractmethod
    async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]:
        """Thread ids carrying ≥1 👎, oldest-👎-first, capped at ``limit``.

        Feeds ``GET /v1/conversations?has_down_rated`` (P-2 §6), mirroring
        ``RunStore.thread_ids_with_runs``. ``tenant_id=None`` is the
        cross-tenant aggregate: the SQL implementation assumes the
        ``audit_reader`` BYPASSRLS role for that read (same precedent as
        :meth:`list_unprocessed_down_all_tenants`); the caller must be in a
        bypass scope so no tenant GUC is emitted.
        """
```

- [ ] **Step 4: 实现 —— in-memory**

`InMemoryFeedbackStore`(`:113-152`)加:

```python
    async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]:
        if record.run_id is None:
            raise ValueError("upsert requires run_id")
        for i, r in enumerate(self._rows):
            if (
                r.tenant_id == record.tenant_id
                and r.run_id == record.run_id
                and r.actor_id == record.actor_id
            ):
                stored = replace(
                    r,
                    rating=record.rating,
                    comment=record.comment,
                    item_id=record.item_id,
                    trace_id=record.trace_id,
                    turn_seq=record.turn_seq,
                    source=record.source,
                    updated_at=datetime.now(UTC),
                )
                self._rows[i] = stored
                return stored, True
        stored = replace(record, id=next(self._ids), created_at=datetime.now(UTC), updated_at=None)
        self._rows.append(stored)
        return stored, False

    async def list_for_thread_scoped(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[FeedbackRecord]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id and r.thread_id == thread_id]
        return sorted(rows, key=_record_id, reverse=True)

    async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]:
        out: list[UUID] = []
        for r in sorted(self._rows, key=_record_id):
            if r.rating != "down":
                continue
            if tenant_id is not None and r.tenant_id != tenant_id:
                continue
            if r.thread_id in out:
                continue
            out.append(r.thread_id)
            if len(out) >= limit:
                break
        return set(out)
```

模块级加一个具名 key 函数(mypy CI 扫 tests,也别在实现里留 lambda):

```python
def _record_id(record: FeedbackRecord) -> int:
    return record.id or 0
```

并把 `:127` / `:135` 两处 `key=lambda r: r.id or 0` 换成 `key=_record_id`(这两处是本改动创建的重复,顺手统一;不动其它行)。

- [ ] **Step 5: 实现 —— SQL(谓词与内存版逐字对应)**

`DbFeedbackStore`(`:155-249`)加:

```python
    async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]:
        if record.run_id is None:
            raise ValueError("upsert requires run_id")
        async with self._sf() as session:
            existing_id = (
                await session.execute(
                    select(FeedbackRow.id).where(
                        FeedbackRow.tenant_id == record.tenant_id,
                        FeedbackRow.run_id == record.run_id,
                        FeedbackRow.actor_id == record.actor_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                row = (
                    await session.execute(
                        update(FeedbackRow)
                        .where(FeedbackRow.id == existing_id)
                        .values(
                            rating=record.rating,
                            comment=record.comment,
                            item_id=record.item_id,
                            trace_id=record.trace_id,
                            turn_seq=record.turn_seq,
                            source=record.source,
                            updated_at=datetime.now(UTC),
                        )
                        .returning(FeedbackRow)
                    )
                ).scalar_one()
                stored = _row_to_record(row)
                await session.commit()
                return stored, True
            row = FeedbackRow(
                tenant_id=record.tenant_id,
                thread_id=record.thread_id,
                run_id=record.run_id,
                source=record.source,
                item_id=record.item_id,
                turn_seq=record.turn_seq,
                trace_id=record.trace_id,
                rating=record.rating,
                comment=record.comment,
                actor_id=record.actor_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # Lost a concurrent-insert race on ``feedback_run_actor_uniq`` —
                # the row exists now, so the retry takes the UPDATE branch.
                await session.rollback()
                return await self.upsert(record)
            await session.refresh(row)
            stored = _row_to_record(row)
            await session.commit()
            return stored, False

    async def list_for_thread_scoped(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[FeedbackRecord]:
        async with self._sf() as session:
            result = await session.execute(
                select(FeedbackRow)
                .where(FeedbackRow.tenant_id == tenant_id, FeedbackRow.thread_id == thread_id)
                .order_by(FeedbackRow.id.desc())
            )
            return [_row_to_record(row) for row in result.scalars().all()]

    async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]:
        stmt = select(FeedbackRow.thread_id).where(FeedbackRow.rating == "down")
        if tenant_id is not None:
            stmt = stmt.where(FeedbackRow.tenant_id == tenant_id)
        stmt = (
            stmt.group_by(FeedbackRow.thread_id)
            .order_by(func.min(FeedbackRow.id).asc())
            .limit(limit)
        )
        async with self._sf() as session:
            if tenant_id is None:
                await session.execute(_SET_AUDIT_READER_ROLE)
            return set((await session.execute(stmt)).scalars().all())
```

import 行(`:21`)改为 `from sqlalchemy import delete, func, select, text, update` 并加 `from sqlalchemy.exc import IntegrityError`。`_row_to_record`(`:252-264`)加 `run_id=row.run_id, source=row.source, item_id=row.item_id, updated_at=row.updated_at`。`insert`(`:168-184`)的 `FeedbackRow(...)` 也补 `run_id=record.run_id, source=record.source, item_id=record.item_id`(控制台老路径经 Task 5 后不再调用 `insert`,但 worker 测试仍用它)。

- [ ] **Step 6: 跑测试确认绿(单元 + 集成 + RLS)**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_feedback_store_upsert.py packages/expert-work-persistence/tests/test_rls_integration.py -q`
Expected: 全绿(集成用例需要 `DOCKER_HOST`)。

- [ ] **Step 7: 变异自证(两套实现各一刀)**

1. 内存版 `upsert` 里把 `and r.actor_id == record.actor_id` 删掉 → `test_in_memory_upsert_overwrites_same_run_actor` 在 `assert updated is False and other.id != first.id` 红;改回 → 绿。
2. SQL 版 `upsert` 的 `.values(...)` 里删掉 `comment=record.comment,` → `test_sql_upsert_overwrites_same_run_actor` 在 `assert second.rating == "up" and second.comment is None` 红;改回 → 绿。
3. SQL 版 `down_rated_thread_ids` 删掉 `.limit(limit)` → `test_sql_down_rated_thread_ids` 在 `limit=1` 断言红;改回 → 绿。
4. RLS 用例:把 SQL `list_for_thread_scoped` 的 `FeedbackRow.tenant_id == tenant_id,` 删掉 → 由于夹具是 RLS 角色,这条**不会红**(RLS 替你挡了)—— 这正是出入表 #12 的意思;用 `test_sql_upsert_overwrites_same_run_actor`(超级用户引擎)的 `assert await store.list_for_thread_scoped(tenant_id=uuid4(), thread_id=thread) == []` 来红;改回 → 绿。

- [ ] **Step 8: lint + 提交**

```bash
uv run --no-sync ruff check packages/expert-work-persistence && uv run --no-sync ruff format packages/expert-work-persistence
git add packages/expert-work-persistence/src/expert_work/persistence/feedback_store.py packages/expert-work-persistence/tests/test_feedback_store_upsert.py packages/expert-work-persistence/tests/test_rls_integration.py
git commit -m "feat(persistence): FeedbackStore.upsert((tenant,run,actor) 覆盖) + list_for_thread_scoped + down_rated_thread_ids,双实现同谓词"
```

---
### Task 3: 对外端点 `POST /v1/agents/{agent_code}/runs/{run_id}/feedback` + 两张路由表

**Files:**
- Create: `services/control-plane/src/control_plane/api/external_feedback.py`
- Modify: `services/control-plane/src/control_plane/api/__init__.py:15-23`(import)、`:94-102`(`__all__`)
- Modify: `services/control-plane/src/control_plane/app.py:57-65`(import)、`:2643-2651`(`include_router`,放在 `build_external_runs_router()` 之后)
- Modify: `services/control-plane/tests/test_external_only_gate.py:92`(`_EXTERNAL_ROUTES` 最后一条之后)
- Modify: `services/control-plane/tests/test_console_lockdown.py:281`(`_EXTERNAL_AGENT_ROUTES` 最后一条之后)
- Test: `services/control-plane/tests/test_external_feedback.py`

**Interfaces:**
- Consumes: Task 2 的 `FeedbackStore.upsert`;`load_owned_run`(`api/_external.py:349-398`,返回 `(RunInfo, ThreadMeta)`);`reject_nul` / `reject_nul_path_params` / `ExternalScopeError` / `external_error`(`_external.py:50-69` / `:96-157` / `:207-226`);`external_only()` / `require()`(`_authz.py:265` / `:46`);`emit`(`control_plane/audit.py:195-229`,`resource_type="feedback"` 已在 Literal `:123`,不改 Literal)。
- Produces: `build_external_feedback_router() -> APIRouter`;body `ExternalFeedbackRequest(user_id: str, rating: Literal["up","down"], comment: str | None ≤4000, item_id: str | None ≤255)`;响应 200 `{"success": true, "data": {"run_id", "rating", "updated"}, "error": null}`;`actor_id = str(meta.user_id)`、`source="external"`。PR2 的 Task 9 会在同一处接同步进池(此处先留 `candidate_sync` 的调用点:本 Task 只写反馈 + 审计)。
- 配额核对(spec §4.1 要求「实施时核 quota 动作表」):`packages/expert-work-protocol/src/expert_work/protocol/quota.py:53-84` 的 `QuotaDimension` 只有 `qps / tokens_per_day / sandboxes / monthly_token_budget / image_upload_count_30d / image_storage_bytes / artifact_download_count_30d / artifact_storage_bytes / workspace_bytes_per_user`,没有按端点动作的维度;`check_admission` 只在 `agents.py:49`、`external_uploads.py:60`、`external_artifacts.py:46` 三处被调用。结论:不单列,沿用网关 `RateLimitMiddleware`;`(run, user)` 唯一键使行数上限 = run 数。

- [ ] **Step 1: 写失败测试(夹具照 `test_external_runs_cancel.py:57-166`)**

```python
# services/control-plane/tests/test_external_feedback.py
"""对外打分 —— ``POST /v1/agents/{agent_code}/runs/{run_id}/feedback``(P-2 PR1)。

夹具照 ``test_external_runs_cancel.py``:服务账号 key、直接往 ``RunStore`` 写 run 行。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import InMemoryFeedbackStore
from expert_work.protocol import AgentSpec, AuditQuery
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        run_store: InMemoryRunStore,
        feedback: InMemoryFeedbackStore,
        audit_store: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store
        self.feedback = feedback
        self.audit_store = audit_store

    async def seed_agent(self, name: str = "support-bot") -> None:
        spec = _spec().model_copy(deep=True)
        spec.metadata.name = name
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=spec, spec_sha256="a" * 64, created_by="seed"
        )

    async def bind_session(self, user_id: str, agent: str = "support-bot") -> UUID:
        bound = await self.client.post(
            f"/v1/agents/{agent}/sessions", json={"user_id": user_id}, headers=self.headers
        )
        assert bound.status_code == 201, bound.text
        return UUID(bound.json()["data"]["session_id"])

    async def end_user_id(self, user_id: str) -> UUID:
        row = await self.app.state.tenant_user_repo.resolve(
            tenant_id=self.tenant_id, subject_type="user", subject_id=f"ext:{user_id}"
        )
        return row.id  # type: ignore[no-any-return]

    async def seed_run(self, thread_id: UUID, user_id: str) -> UUID:
        run_id = uuid4()
        await self.run_store.create(
            RunInfo(
                run_id=run_id,
                tenant_id=self.tenant_id,
                thread_id=thread_id,
                user_id=await self.end_user_id(user_id),
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=_NOW,
                updated_at=_NOW,
                finished_at=_NOW,
            )
        )
        return run_id

    async def rate(self, run_id: UUID, body: dict[str, Any], agent: str = "support-bot") -> Any:
        return await self.client.post(
            f"/v1/agents/{agent}/runs/{run_id}/feedback", json=body, headers=self.headers
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
    feedback = InMemoryFeedbackStore()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        feedback_repo=feedback,
    )
    tenant_id = uuid4()
    jwt = make_test_jwt(
        tenant_id=tenant_id,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("write",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store, feedback, audit_store)


@pytest.mark.asyncio
async def test_down_then_up_is_one_row_last_wins(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")

    first = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down", "comment": "太慢", "item_id": "p2"})
    assert first.status_code == 200, first.text
    assert first.json() == {
        "success": True,
        "data": {"run_id": str(run_id), "rating": "down", "updated": False},
        "error": None,
    }

    second = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "up"})
    assert second.status_code == 200, second.text
    assert second.json()["data"] == {"run_id": str(run_id), "rating": "up", "updated": True}

    rows = await ctx.feedback.list_for_thread_scoped(tenant_id=ctx.tenant_id, thread_id=thread)
    assert len(rows) == 1
    assert rows[0].rating == "up"
    assert rows[0].comment is None and rows[0].item_id is None
    assert rows[0].run_id == run_id
    assert rows[0].source == "external"
    assert rows[0].actor_id == str(await ctx.end_user_id("cust-77"))
    assert rows[0].updated_at is not None


@pytest.mark.asyncio
async def test_other_user_and_other_agent_are_404_envelope(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    await ctx.seed_agent("other-bot")
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")

    other_user = await ctx.rate(run_id, {"user_id": "cust-99", "rating": "down"})
    assert other_user.status_code == 404, other_user.text
    assert other_user.json() == {
        "success": False,
        "data": None,
        "error": {"code": "RUN_NOT_FOUND", "message": "run not found"},
    }
    other_agent = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down"}, agent="other-bot")
    assert other_agent.status_code == 404
    assert other_agent.json()["error"]["code"] == "RUN_NOT_FOUND"
    # 404 不铸造 tenant_user 行(load_owned_run 是 mint=False)。
    users = await ctx.app.state.tenant_user_repo.list_by_tenant(ctx.tenant_id, subject_type="user")
    assert {u.subject_id for u in users} == {"ext:cust-77"}
    assert await ctx.feedback.list_for_thread_scoped(tenant_id=ctx.tenant_id, thread_id=thread) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_body",
    [
        {"user_id": "cust-77"},  # missing rating
        {"user_id": "cust-77", "rating": "sideways"},
        {"user_id": "cust-77", "rating": "up", "comment": "x\x00y"},  # NUL
        {"user_id": "cust-77", "rating": "up", "item_id": "a\x00b"},  # NUL
        {"user_id": "cust-77", "rating": "up", "comment": "c" * 4001},
        {"user_id": "cust-77", "rating": "up", "extra": 1},
    ],
)
async def test_bad_bodies_are_422_envelope(ctx: _Ctx, bad_body: dict[str, Any]) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    resp = await ctx.rate(run_id, bad_body)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False and body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "detail" not in body


@pytest.mark.asyncio
async def test_audit_row_carries_run_and_rating_but_never_the_comment(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    resp = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down", "comment": "SECRET-PROSE"})
    assert resp.status_code == 200
    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=100))
    rows = [e for e in page.entries if e.action.value == "feedback:create"]
    assert len(rows) == 1
    assert rows[0].details["run_id"] == str(run_id)
    assert rows[0].details["rating"] == "down"
    assert rows[0].details["source"] == "external"
    assert rows[0].on_behalf_of == str(await ctx.end_user_id("cust-77"))
    assert "SECRET-PROSE" not in str(rows[0].details)
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_feedback.py -q`
Expected: 全部 FAIL,首条为 `assert 404 == 200`(路由不存在,落到 `agents.py` 的 `{name}/{version}` 或 404)。

- [ ] **Step 3: 写端点**

```python
# services/control-plane/src/control_plane/api/external_feedback.py
"""对外打分 —— ``POST /v1/agents/{agent_code}/runs/{run_id}/feedback``(P-2)。

打分对象是 **run**(一轮):``/messages`` 不给消息 id,``/items`` 的条目 id 不跨接口稳定,
三条读路径都稳定的一等 id 只有 ``run_id``(spec §2.4)。``item_id`` 只是对接方自己的
段落标签,只存不 join。

同 ``(run, 终端用户)`` 再打 = 覆盖(可改票),``created_at`` 不变、写 ``updated_at``。
归属校验与 ``:cancel`` 同一套:不属于 ``(user, agent)`` 一律 404 ``RUN_NOT_FOUND``,
不泄露存在性。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
    reject_nul,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.feedback_store import FeedbackRecord, FeedbackStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import AuditAction
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStore


class ExternalFeedbackRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}/feedback``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=4000)
    item_id: str | None = Field(default=None, max_length=255)

    # ``comment`` / ``item_id`` 原样进 ``feedback.comment`` / ``item_id``(text 列),
    # 一个 NUL 就是 asyncpg 的 CharacterNotInRepertoireError → 裸文本 500;
    # ``user_id`` 在 ``external_subject_id`` 里已经守过一次,这里不重复。
    @field_validator("comment", "item_id")
    @classmethod
    def _no_nul(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field = getattr(info, "field_name", "value")
        return reject_nul(value, field=field)


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_feedback_store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_external_feedback_router() -> APIRouter:
    """Mount the external per-run feedback endpoint."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.post(
        "/{agent_code}/runs/{run_id}/feedback",
        response_model=None,
        dependencies=[Depends(require("session", "write"))],
    )
    async def rate_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalFeedbackRequest,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        store: Annotated[FeedbackStore, Depends(_get_feedback_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        try:
            run, meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=payload.user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        trace_id = current_trace_id_hex()
        actor_id = str(meta.user_id)
        stored, updated = await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=run.thread_id,
                run_id=run.run_id,
                rating=payload.rating,
                comment=payload.comment,
                item_id=payload.item_id,
                source="external",
                trace_id=trace_id,
                actor_id=actor_id,
            )
        )
        # 审计只记动作,永不记评论原文(评论住在 feedback 表里,控制台全员可见是
        # 另一回事;审计流不该复制一份用户散文)。
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.FEEDBACK_CREATE,
            resource_type="feedback",
            resource_id=str(stored.id),
            trace_id=trace_id,
            details={
                "thread_id": str(run.thread_id),
                "run_id": str(run.run_id),
                "rating": payload.rating,
                "updated": updated,
                "source": "external",
            },
            on_behalf_of=actor_id,
        )
        return JSONResponse(
            {
                "success": True,
                "data": {"run_id": str(run.run_id), "rating": stored.rating, "updated": updated},
                "error": None,
            }
        )

    return router
```

注册:`api/__init__.py` 在 `:18` 之后按字母序加 `from control_plane.api.external_feedback import build_external_feedback_router`,`__all__` 在 `"build_external_events_router",`(`:97`)之后加 `"build_external_feedback_router",`;`app.py:60` 之后加 import,`:2644`(`build_external_events_router()` 之后)加 `app.include_router(build_external_feedback_router())`。

- [ ] **Step 4: 两张手工路由表各加一行(精确位置)**

`services/control-plane/tests/test_external_only_gate.py`,在 `:92` `("GET", "/v1/agents/{agent_code}/uploads/{upload_id}"),` 之后:

```python
        # P-2 — 对外打分(external_feedback.py)。与 ``:cancel`` 同一路由族。
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}/feedback"),
```

`services/control-plane/tests/test_console_lockdown.py`,在 `:281` `("GET", "/v1/agents/{agent_code}/uploads/{upload_id}"),` 之后:

```python
        # P-2 — 对外打分(external_feedback.py),挂 ``require("session", "write")``
        # 而非 ``console_only()``。
        ("POST", "/v1/agents/{agent_code}/runs/{run_id}/feedback"),
```

`test_external_path_param_nul_guard.py:513` 的 `_AGENTS_ROUTER_EXTERNAL_ROUTES` **不加**(出入表 #2)。

- [ ] **Step 5: 跑三张表的审计 + 本文件测试确认绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_feedback.py services/control-plane/tests/test_external_only_gate.py services/control-plane/tests/test_console_lockdown.py services/control-plane/tests/test_external_path_param_nul_guard.py services/control-plane/tests/test_external_route_reachability.py -q`
Expected: 全绿。特别是 `test_route_table_covers_every_live_external_agents_route`(`test_external_only_gate.py:416`)与 `test_every_agents_route_is_classified`(`test_console_lockdown.py:568-589`)—— 这两条是「不登记就红」的闸。

- [ ] **Step 6: 变异自证(五刀)**

1. 把 `test_external_only_gate.py` 刚加的那行删掉 → `test_route_table_covers_every_live_external_agents_route` 红(`表缺(应用里有、表里没有…)`);加回 → 绿。
2. 把 `test_console_lockdown.py` 刚加的那行删掉 → `:568` 的分类断言红(`unclassified /v1/agents routes`);加回 → 绿。
3. 把 `_AGENTS_ROUTER_EXTERNAL_ROUTES`(`test_external_path_param_nul_guard.py:513`)**试着**加上新路由 → `:584` 红(`set(live) == …` 多出一条);删回 → 绿。这一刀就是出入表 #2 的证据,做完写进 PR 描述。
4. 端点里把 `except ExternalScopeError as exc: return external_error(exc)` 改成 `raise` → `test_other_user_and_other_agent_are_404_envelope` 红(500);改回 → 绿。
5. 端点里 `details` 加上 `"comment": payload.comment` → `test_audit_row_carries_run_and_rating_but_never_the_comment` 红;改回 → 绿。

- [ ] **Step 7: lint + 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane/src/control_plane/api/external_feedback.py services/control-plane/src/control_plane/api/__init__.py services/control-plane/src/control_plane/app.py services/control-plane/tests/test_external_feedback.py services/control-plane/tests/test_external_only_gate.py services/control-plane/tests/test_console_lockdown.py
git commit -m "feat(external): POST /v1/agents/{code}/runs/{run_id}/feedback —— 按轮打分、可改票、404 不泄露;两张路由表登记"
```

---

### Task 4: `/items` 与 `/messages` 回显本人 `feedback`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_session_items.py:344-505`(`list_session_items`:`:391-401` 接住 `meta`,`:476-490` `runs[]` 字典加 `feedback`)
- Modify: `services/control-plane/src/control_plane/api/external_sessions.py:243-312`(`get_messages`:`:267` 接住 `meta`,`:296-305` 每条消息加 `feedback`)
- Test: `services/control-plane/tests/test_external_session_items.py`(新增一条)
- Test: `services/control-plane/tests/test_external_sessions.py`(新增一条)

**Interfaces:**
- Consumes: `FeedbackStore.list_for_thread_scoped(tenant_id, thread_id)`(Task 2);`load_owned_session` 返回的 `meta.user_id`。
- Produces: `/items` 的 `runs[]` 每项与 `/messages` 每条消息多一个键 `feedback: {"rating": "up"|"down", "comment": str|null, "item_id": str|null} | null`;只取 `actor_id == str(meta.user_id)` 且 `run_id` 非空的行。共享的小函数 `own_feedback_by_run(rows, *, actor_id) -> dict[str, dict[str, Any]]` 放在 `api/_external.py` 末尾(两处只认它)。

- [ ] **Step 1: 写失败测试**

`test_external_session_items.py` 末尾追加(用该文件既有 `ctx` / `open_session`):

```python
from expert_work.persistence.feedback_store import FeedbackRecord


@pytest.mark.asyncio
async def test_runs_echo_only_the_callers_own_feedback(ctx: _Ctx) -> None:
    """``runs[]`` 回显本 user_id 自己那条;别的终端用户在同一轮打的分不对外。"""
    await ctx.seed_agent()
    session_id, run_id = await ctx.open_session()
    me = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:u-123"
    )
    store = ctx.app.state.feedback_store
    await store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id, thread_id=session_id, run_id=run_id,
            rating="down", comment="太慢", item_id="p1", source="external", actor_id=str(me.id),
        )
    )
    await store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id, thread_id=session_id, run_id=run_id,
            rating="up", source="external", actor_id="someone-else",
        )
    )
    resp = await ctx.items(session_id)
    assert resp.status_code == 200, resp.text
    runs = resp.json()["data"]["runs"]
    assert [r["run_id"] for r in runs] == [str(run_id)]
    assert runs[0]["feedback"] == {"rating": "down", "comment": "太慢", "item_id": "p1"}


@pytest.mark.asyncio
async def test_runs_feedback_defaults_to_null(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    session_id, _ = await ctx.open_session()
    resp = await ctx.items(session_id)
    assert resp.json()["data"]["runs"][0]["feedback"] is None


@pytest.mark.asyncio
async def test_feedback_stays_on_the_run_it_was_given_for(ctx: _Ctx) -> None:
    """分只跟着它评的那一轮走:同一段会话里再来一轮,新轮的 ``feedback`` 是 ``null``。

    这就是「重新生成 / 编辑重发后旧轮的分不迁到新轮」(2026-09-09 拍板)在读面上的
    形状 —— 评分是对那一次回答的评价。这里用普通的第二轮构造,不依赖 P-1 的
    supersede;带 ``superseded_by`` 的那一版断言等 P-1 PR1 合入后补(见下方说明)。
    """
    await ctx.seed_agent()
    session_id, first_run = await ctx.open_session()
    me = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:u-123"
    )
    await ctx.app.state.feedback_store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id, thread_id=session_id, run_id=first_run,
            rating="down", comment="答非所问", source="external", actor_id=str(me.id),
        )
    )
    second_run = await ctx.add_run(session_id, created_at=ctx.origin + timedelta(minutes=1))

    resp = await ctx.items(session_id)
    assert resp.status_code == 200, resp.text
    by_run = {r["run_id"]: r["feedback"] for r in resp.json()["data"]["runs"]}
    assert by_run[str(first_run)] == {"rating": "down", "comment": "答非所问", "item_id": None}
    assert by_run[str(second_run)] is None
```

`test_external_sessions.py` 末尾追加(该文件已有 `_seed_thread_messages` / `STAMP_RUN_ID` / `ctx` 夹具;若其 `_Ctx` 没有 `bind_session`,照 `test_external_runs_cancel.py:128-134` 加一个):

```python
from expert_work.persistence.feedback_store import FeedbackRecord


@pytest.mark.asyncio
async def test_messages_echo_only_the_callers_own_feedback(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    session_id = await ctx.bind_session("cust-77")
    run_id = uuid4()
    stamp = {STAMP_RUN_ID: str(run_id), STAMP_CREATED_AT: datetime(2026, 9, 9, tzinfo=UTC).isoformat()}
    await _seed_thread_messages(
        ctx.app.state.agent_runtime.durable_checkpointer,
        str(session_id),
        [
            HumanMessage(content="hi", additional_kwargs=dict(stamp)),
            AIMessage(content="hello", additional_kwargs=dict(stamp)),
        ],
    )
    me = await ctx.app.state.tenant_user_repo.resolve(
        tenant_id=ctx.tenant_id, subject_type="user", subject_id="ext:cust-77"
    )
    store = ctx.app.state.feedback_store
    await store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id, thread_id=session_id, run_id=run_id,
            rating="up", source="external", actor_id=str(me.id),
        )
    )
    await store.upsert(
        FeedbackRecord(
            tenant_id=ctx.tenant_id, thread_id=session_id, run_id=run_id,
            rating="down", comment="nope", source="external", actor_id="someone-else",
        )
    )
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/sessions/{session_id}/messages",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["data"]["messages"]
    assert len(msgs) == 2
    assert all(m["feedback"] == {"rating": "up", "comment": None, "item_id": None} for m in msgs)
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_session_items.py::test_runs_echo_only_the_callers_own_feedback services/control-plane/tests/test_external_sessions.py::test_messages_echo_only_the_callers_own_feedback -q`
Expected: FAIL —— `KeyError: 'feedback'`。

- [ ] **Step 3: 共享投影函数(`api/_external.py` 末尾)**

```python
def own_feedback_by_run(
    rows: Sequence[FeedbackRecord], *, actor_id: str
) -> dict[str, dict[str, Any]]:
    """P-2 §4.2 — the caller's OWN per-run feedback, keyed by ``str(run_id)``.

    Only rows whose ``actor_id`` is the end user named by ``user_id`` (their
    ``tenant_user.id``, the same value ``load_owned_session`` verified) and
    which are run-scoped (``run_id`` set) are projected; another end user's
    rating on the same run never leaves the platform. ``/messages`` and
    ``/items`` both go through here so the wire shape cannot drift.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.run_id is None or row.actor_id != actor_id:
            continue
        out[str(row.run_id)] = {
            "rating": row.rating,
            "comment": row.comment,
            "item_id": row.item_id,
        }
    return out
```

import:`from collections.abc import Sequence` 与 `from expert_work.persistence.feedback_store import FeedbackRecord`。

- [ ] **Step 4: 两个端点接入**

`external_session_items.py`:加 `def _get_feedback_store(request) -> FeedbackStore`(照 `:93-94` 的样子),handler 签名加 `feedback: Annotated[FeedbackStore, Depends(_get_feedback_store)]`;`:391` 改成 `meta = await load_owned_session(...)`;在 `turns = [...]`(`:434`)之后加:

```python
        own = own_feedback_by_run(
            await feedback.list_for_thread_scoped(tenant_id=tenant_id, thread_id=session_id),
            actor_id=str(meta.user_id),
        )
```

`runs[]` 字典(`:478-489`)在 `"artifacts": run.artifacts,` 之后加 `"feedback": own.get(str(run.run_id)),`。

`external_sessions.py`:同样加 `_get_feedback_store` 与依赖;`:267` 改成 `meta = await load_owned_session(...)`;`page = turns[offset : offset + limit]`(`:295`)之后加同样三行 `own = ...`;`out` 每条(`:297-304`)加 `"feedback": own.get(str(t.run_id)) if t.run_id else None,`。

- [ ] **Step 5: 跑测试确认绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_external_session_items.py services/control-plane/tests/test_external_sessions.py services/control-plane/tests/test_external_api_contract.py -q`
Expected: 全绿(既有 `:364-375` 的 `runs == [...]` 全等断言要补 `"feedback": None`)。

- [ ] **Step 6: 变异自证**

`own_feedback_by_run` 里删掉 `or row.actor_id != actor_id` → `test_runs_echo_only_the_callers_own_feedback` 红(取到 `someone-else` 的 `up`,因为 `id` 降序后者在前);改回 → 绿。`external_sessions.py` 里把 `own.get(str(t.run_id)) if t.run_id else None` 改成常量 `None` → `test_messages_echo_only_the_callers_own_feedback` 红;改回 → 绿。`external_session_items.py` 里把 `own.get(str(run.run_id))` 改成 `next(iter(own.values()), None)`(退化成「这段会话的任意一条反馈」)→ `test_feedback_stays_on_the_run_it_was_given_for` 在 `by_run[str(second_run)] is None` 红;改回 → 绿。

- [ ] **Step 6b: 记下 P-1 合入后要补的那条测试(本 PR 不写)**

拍板(2026-09-09):**重新生成 / 编辑重发后,旧轮上已打的 👍/👎 不迁到新轮** —— 评分是对那一次回答的评价,新一轮是新的回答;候选池里的候选指向旧 trajectory,分留在旧轮才对得上。两侧都不写迁移代码,这条拍板在 P-2 侧是「什么都不做」。

上面的 `test_feedback_stays_on_the_run_it_was_given_for` 用普通第二轮钉住了同一条行为(分只属于它评的那一轮),**不依赖 supersede**,所以现在就能跑。带 supersede 形状的那一版 —— 对一轮打 👎、再 `:regenerate`、读 `/items` 断言「旧轮 `superseded_by` 非空且 `feedback` 仍在,新轮 `feedback` 为 `null`」—— 需要 P-1 的 `supersede_run` 与 `:regenerate` 端点才能构造,**P-1 PR1/PR3 合入后**加到 `test_external_session_items.py`,由那时在手的一方补(在 P-1 的 PR 描述里引本节)。不在 P-2 里写一个跑不了的测试。

- [ ] **Step 7: lint + 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane/src/control_plane/api/_external.py services/control-plane/src/control_plane/api/external_session_items.py services/control-plane/src/control_plane/api/external_sessions.py services/control-plane/tests/test_external_session_items.py services/control-plane/tests/test_external_sessions.py
git commit -m "feat(external): /items runs[] 与 /messages 每条消息回显本人 feedback(别的终端用户不对外)"
```

---
### Task 5: 控制台 POST 必带 `run_id`、`source='console'`;`FeedbackBar` / `TurnFooter` 传 `runId`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/feedback.py:30-37`(body)、`:57-102`(handler)
- Modify: `services/control-plane/tests/test_feedback_api.py`(三条既有用例改 body + 新增一条)
- Modify: `apps/admin-ui/src/api/sessions.ts:61-78`
- Modify: `apps/admin-ui/src/components/turn/FeedbackBar.tsx:21-58`
- Modify: `apps/admin-ui/src/components/console/TurnFooter.tsx:155-159`
- Test: `apps/admin-ui/src/components/console/__tests__/TurnFooter.test.tsx:260-315`(既有用例)+ 新增一条

**Interfaces:**
- Consumes: Task 2 的 `upsert`。
- Produces: `FeedbackRequest` 新增必填 `run_id: UUID`;响应 201 body 加 `run_id` 与 `updated`;`submitSessionFeedback(threadId, {rating, comment?, run_id})`;`FeedbackBar` 新 prop `runId: string`;`TurnFooter` 只在 `turn.runId !== null` 时渲染 `FeedbackBar`。控制台老式 `turn_seq` 继续可选透传(死字段不动)。

**前置事实(Task 0 实测,09-09 —— 动手前先读):控制台的写路径今天只在「调试台」可达,对话详情页根本不渲染 `FeedbackBar`。** 渲染链逐层核过:

| 层 | 位置 | 决定性的那一行 |
|---|---|---|
| 渲染闸 | `components/console/TurnFooter.tsx:155` | `{!readOnly && (status === "done" \|\| status === "interrupted") && threadId && (<FeedbackBar …/>)}` |
| 唯一上游 | `components/console/TurnBlock.tsx:238` | `<TurnFooter … readOnly={readOnly} />` |
| 唯一上游 | `components/console/Transcript.tsx:213` / `:246` | 两处 `<TurnBlock … readOnly={readOnly} />` |
| 调用点 ① | `pages/ConversationDetail.tsx:702` | 裸 `readOnly`(= `true`)→ **对话详情页永不渲染反馈条** |
| 调用点 ② | `pages/agent_detail/PlaygroundTab.tsx:705` | `readOnly={false}` → **只有调试台渲染** |

这解释了 Task 0 §7-2 测到的「`feedback` 表两个环境都 0 行、`n_tup_ins = 0`(建表至今没写进过一行)」:能点的地方只有员工自己的调试试跑,**没人会给自己的试跑打分**。

两个由此而来的判断,别搞反:

- **这个 Task 不是「在一条从没通电的线上加约束」。** 调试台那条线是通的(渲染链完整、`services/control-plane/tests/test_feedback_api.py` 三条测试在跑、后端写路径有覆盖),改成必带 `run_id` 是给一条能用的线换键,不是复活死代码。
- **但也别宣称它会立刻带来数据。** 本 Task 之后 `feedback` 大概率仍然接近 0 行 —— P-2 真正的写入来源是 **Task 3 的对外端点**(终端用户在对接方界面上打分),控制台这条只是顺手对齐语义、避免同一张表两种写法。给用户/审阅者汇报时不要把这个 Task 说成「反馈功能上线了」。
- **与 PR3 Task 15 不矛盾。** Task 15 在对话详情页加的是**只读展示**(员工看终端用户打的分),`readOnly=true` 挡的是写入按钮,不挡展示。「写只在调试台、读在对话详情页」是本设计的既定形态,不是需要修的 bug;要不要把写入也放开到对话详情页,是另一个产品问题,**本计划不做**。

- [ ] **Step 1: 后端失败测试**

`test_feedback_api.py`:三条既有用例的 body 都加 `"run_id": str(uuid4())`(`:76`、`:109`、`:133` 的 `bad_body` 各项加 `run_id`,并在 `bad_body` 列表**新增** `{"rating": "up"}` 一项 —— 漏 `run_id` 是 422);新增:

```python
@pytest.mark.asyncio
async def test_console_feedback_is_run_scoped_upsert(
    client: AsyncClient, feedback_store: InMemoryFeedbackStore
) -> None:
    thread_id, run_id = uuid4(), uuid4()
    first = await client.post(
        f"/v1/sessions/{thread_id}/feedback",
        json={"rating": "down", "comment": "bad", "run_id": str(run_id)},
    )
    assert first.status_code == 201
    assert first.json()["run_id"] == str(run_id)
    assert first.json()["updated"] is False
    second = await client.post(
        f"/v1/sessions/{thread_id}/feedback", json={"rating": "up", "run_id": str(run_id)}
    )
    assert second.status_code == 201
    assert second.json()["updated"] is True
    rows = await feedback_store.list_for_thread(thread_id=thread_id)
    assert len(rows) == 1
    assert rows[0].rating == "up" and rows[0].run_id == run_id and rows[0].source == "console"
```

- [ ] **Step 2: 跑测试确认红**

Run: `uv run --no-sync pytest services/control-plane/tests/test_feedback_api.py -q`
Expected: 新用例 `KeyError: 'run_id'`;`bad_body={"rating": "up"}` 那项 `assert 201 == 422` 红。

- [ ] **Step 3: 改后端**

`FeedbackRequest`(`:30-37`)加 `run_id: UUID`(必填,放在 `rating` 之前);handler 把 `store.insert(...)` 换成:

```python
        stored, updated = await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=payload.run_id,
                turn_seq=payload.turn_seq,
                trace_id=trace_id,
                rating=payload.rating,
                comment=payload.comment,
                source="console",
                actor_id=actor_id,
            )
        )
```

审计 `details` 加 `"run_id": str(payload.run_id), "updated": updated, "source": "console"`;响应 `content` 加 `"run_id": str(payload.run_id), "updated": updated`。模块 docstring 第一段末尾加一句:「P-2 起按 run 打分、同 (run, actor) 覆盖;不再校验 thread 存在这一点不变。」

- [ ] **Step 4: 跑后端确认绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_feedback_api.py -q` → 全绿。

- [ ] **Step 5: 前端失败测试**

`TurnFooter.test.tsx` 新增(放在 `:260` 那条之后):

```tsx
  it("feedback bar needs a run id: hidden for a settled turn whose runId is null, shown once runId is known", () => {
    const { rerender } = render(
      <MemoryRouter>
        <TurnFooter
          turn={makeConsoleTurn({ status: "done" })}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("playground-turn-feedback")).not.toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <TurnFooter
          turn={{ ...makeConsoleTurn({ status: "done" }), runId: "run-1" }}
          threadId="th-1"
          summary={FULL_SUMMARY}
          costCny={null}
          readOnly={false}
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("playground-turn-feedback")).toBeInTheDocument();
  });
```

既有 `:260`、`:405` 两条用例里 `makeConsoleTurn({ status: "done" })` / `interrupted` 的 turn 都要改成带 `runId: "run-1"`(`{ ...makeConsoleTurn(...), runId: "run-1" }`),否则按新规则不渲染 —— 这是刻意的行为变化,不是测试将就实现。
同时改 `FeedbackBar` 的提交断言:`TurnFooter.test.tsx` 没有测提交体;在 `apps/admin-ui/src/components/turn/__tests__/FeedbackBar.test.tsx`(新建)加:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import * as sessionsSdk from "../../../api/sessions";
import { FeedbackBar } from "../FeedbackBar";

describe("FeedbackBar", () => {
  it("posts run_id with the rating", async () => {
    const spy = vi
      .spyOn(sessionsSdk, "submitSessionFeedback")
      .mockResolvedValue({ id: 1, thread_id: "th-1", run_id: "run-1", rating: "up", turn_seq: 3, trace_id: null, updated: false });
    render(<FeedbackBar threadId="th-1" runId="run-1" turnSeq={3} />);
    await userEvent.setup().click(screen.getByTestId("playground-feedback-up"));
    expect(spy).toHaveBeenCalledWith("th-1", { rating: "up", comment: undefined, run_id: "run-1", turn_seq: 3 });
    expect(await screen.findByText(/thanks|感谢/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 6: 跑前端确认红**

Run: `cd apps/admin-ui && pnpm vitest run src/components/console/__tests__/TurnFooter.test.tsx src/components/turn/__tests__/FeedbackBar.test.tsx`
Expected: 新增两条红(`runId` 不是合法 prop → typecheck 也红:`pnpm typecheck`)。

- [ ] **Step 7: 改前端**

`api/sessions.ts:61-78`:

```ts
export interface SessionFeedback {
  id: number;
  thread_id: string;
  run_id: string;
  rating: "up" | "down";
  turn_seq: number | null;
  trace_id: string | null;
  /** true = 覆盖了同 (run, 本人) 的上一票。 */
  updated: boolean;
}

/** POST /v1/sessions/{threadId}/feedback — bare JSON row (201, no envelope).
 *  P-2:按 run 打分,同 (run, 本人) 再打 = 覆盖。 */
export async function submitSessionFeedback(
  threadId: string,
  payload: { rating: "up" | "down"; comment?: string; run_id: string; turn_seq?: number },
): Promise<SessionFeedback> {
```

`FeedbackBar.tsx`:props 加 `runId: string`;`submitSessionFeedback(threadId, { rating, comment: text?.trim() || undefined, run_id: runId, turn_seq: turnSeq })`;依赖数组加 `runId`。
`TurnFooter.tsx:155-159`:

```tsx
        {!readOnly && (status === "done" || status === "interrupted") && threadId && turn.runId !== null && (
          <ReadonlyTooltip on={isTenantSwitched}>
            <FeedbackBar threadId={threadId} runId={turn.runId} turnSeq={turn.seq} disabled={isTenantSwitched} />
          </ReadonlyTooltip>
        )}
```

- [ ] **Step 8: 前端确认绿 + e2e 引用核对**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/components/console/__tests__/TurnFooter.test.tsx src/components/turn/__tests__/FeedbackBar.test.tsx src/pages/__tests__/ConversationDetail.test.tsx src/pages/__tests__/PlaygroundTab.test.tsx`
Expected: 全绿。`rg -n "playground-turn-feedback|playground-feedback" apps/admin-ui/e2e/` → 0 命中(已核:e2e 只引用 `login-*`、`/curation` 路由与 `**/v1/curation/candidates*` mock),不需要改 Playwright。

- [ ] **Step 9: 变异自证**

`TurnFooter.tsx` 把 `&& turn.runId !== null` 删掉 → 新用例第一段红;改回 → 绿。`FeedbackBar.tsx` 删掉 `run_id: runId,` → `FeedbackBar.test.tsx` 的 `toHaveBeenCalledWith` 红;改回 → 绿。后端:handler 里把 `source="console"` 改成 `"external"` → `test_console_feedback_is_run_scoped_upsert` 末行红;改回 → 绿。

- [ ] **Step 10: lint + 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane/src/control_plane/api/feedback.py services/control-plane/tests/test_feedback_api.py apps/admin-ui/src/api/sessions.ts apps/admin-ui/src/components/turn/FeedbackBar.tsx apps/admin-ui/src/components/turn/__tests__/FeedbackBar.test.tsx apps/admin-ui/src/components/console/TurnFooter.tsx apps/admin-ui/src/components/console/__tests__/TurnFooter.test.tsx
git commit -m "feat(console): 控制台打分改按 run(必带 run_id、source=console、同 (run,本人) 覆盖);FeedbackBar/TurnFooter 传 runId"
```

---

### Task 6: purge 归零覆盖 run 级行(扩 `test_user_purge.py` 既有用例)

**Files:**
- Modify: `services/control-plane/tests/test_user_purge.py:203-205`(A 的反馈种子)、`:274-277`(断言)、`:315`

**Interfaces:**
- Consumes: `purge/user_purge.py:232` 的 `deps.feedback.delete_for_threads(tenant_id, thread_ids)`(按 thread 删,run 级行同样带 `thread_id`,不用改实现)。
- Produces: 既有用例多覆盖一种行形态;实现零改动。这是「实现不变、测试变严」的 Task —— 变异自证改的是**实现**。

- [ ] **Step 1: 改种子与断言**

`:203-205` 改成两条(一条老式 thread 级、一条 run 级 upsert):

```python
    await feedback.insert(
        FeedbackRecord(tenant_id=t1, thread_id=t_a, rating="up", actor_id="subj-a")
    )
    await feedback.upsert(
        FeedbackRecord(
            tenant_id=t1, thread_id=t_a, run_id=uuid4(), rating="down",
            comment="run-scoped", source="external", actor_id=str(a.id),
        )
    )
```

`:275` 改成 `assert summary.deleted["feedback"] == 2`;`:276-277` 不变;`:315` 不变(幂等仍是 0)。

- [ ] **Step 2: 跑测试确认绿(实现已满足)**

Run: `uv run --no-sync pytest services/control-plane/tests/test_user_purge.py -q` → 全绿。

- [ ] **Step 3: 变异自证(改实现)**

`feedback_store.py` 内存版 `delete_for_threads` 的过滤条件改成 `not (r.tenant_id == tenant_id and r.thread_id in wanted and r.run_id is None)`(只删老式行)→ `assert summary.deleted["feedback"] == 2` 红(得到 1);改回 → 绿。

- [ ] **Step 4: 提交**

```bash
git add services/control-plane/tests/test_user_purge.py
git commit -m "test(purge): 用户清除同样清掉 run 级反馈行(扩既有级联用例)"
```

---

### Task 7: 对外文档五页 + 侧栏(按 style-guide 自检)

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/chat.md`(文件末尾 `:538` 之后新增 `## 2.9 给一轮回答打分`)
- Modify: `apps/admin-ui/docs-site/.vitepress/config.mts:50`(2.8 条目之后加 2.9)
- Modify: `apps/admin-ui/docs-site/guide/query.md:212-217`(5.3 响应字段表加 `feedback` 行)、`:971-979`(5.8「轮的信息」表加 `feedback` 行)
- Modify: `apps/admin-ui/docs-site/guide/errors.md:9-13`(端点名列表)、`:34`(`RUN_NOT_FOUND` 行端点列)、`:188`(8.6 段落)、`:248`(8.10 `INVALID_USER_ID` 例外说明)
- Modify: `apps/admin-ui/docs-site/guide/examples.md`(末尾 `:3624` 之后新增 `## 10.9 给一轮回答打分`,四语言)
- Modify: `apps/admin-ui/docs-site/guide/best-practices.md:79-81`(9.5 新增一问)、`:99-100`(9.6 清单加一项)

**Interfaces:**
- Consumes: Task 3 / Task 4 的接口形状。
- Produces: 对接方可读的完整接入说明;新锚点 `#_2-9-给一轮回答打分`、`#_10-9-给一轮回答打分`。

- [ ] **Step 1: `chat.md` 新节(照 2.8 的结构:用途 → 请求 → 响应 → 示例 → 错误)**

```markdown
## 2.9 给一轮回答打分

终端用户对 Agent 的某一轮回答点「好」或「不好」时，调用方把这一票记到平台；平台据此改进 Agent。打分的对象是一轮，也就是一次 run，不是单条消息。

打分需要 `run_id`。发起对话时事件流的第一个 `metadata` 事件就带 `run_id`（见 [3.4 metadata](./sse-events#metadata)），历史会话里每一轮的 `run_id` 见 [5.4 run 列表](./query#_5-4-run-列表) 或 [5.8 对话条目](./query#_5-8-对话条目) 的 `runs`。

### 请求

``` [端点]
POST /v1/agents/{agent_code}/runs/{run_id}/feedback
```

需要 `write` 权限。`agent_code` 与 `run_id` 在路径里，其余参数在请求体里。

| 参数 | 必填 | 说明 |
|---|---|---|
| `user_id` | 是 | string，长度 1–255 字符。必须是发起这一轮的终端用户 |
| `rating` | 是 | string。取值：`up`（回答好）/ `down`（回答不好） |
| `comment` | 否 | string，最长 4000 字符。终端用户的原话，`down` 时建议附上，平台的审阅人员会看到 |
| `item_id` | 否 | string，最长 255 字符。调用方自己的段落标签，原样保存、原样回显，平台不解释它 |

### 响应

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string（UUID） | 被打分的那一轮 |
| `rating` | string | 本次记下的取值。取值：`up` / `down` |
| `updated` | boolean | `true` 表示这一轮此前已被同一个终端用户打过分，本次是覆盖 |

同一个终端用户对同一轮再次打分，以最后一次为准：`rating`、`comment`、`item_id` 三个字段整体替换，没有传的字段视为清空。

### 示例

```bash [请求]
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}/feedback" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123", "rating": "down", "comment": "答非所问", "item_id": "p2"}'
```

```json [响应 200]
{ "success": true, "data": { "run_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "rating": "down", "updated": false }, "error": null }
```

### 错误

- `run_id` 不存在，或不属于这个 `user_id` 与 `agent_code`，返回 404 `RUN_NOT_FOUND`，响应不透露这一轮是否存在；`user_id` 为空白时同样是这个 404，不是 422。
- `rating` 不是 `up` / `down`、`comment` 超过 4000 字符、`item_id` 超过 255 字符、请求体带了未声明的字段，返回 422 `INVALID_REQUEST`。

打过的分会出现在 [5.3 历史消息](./query#_5-3-历史消息) 与 [5.8 对话条目](./query#_5-8-对话条目) 的 `feedback` 字段里，只回显当前 `user_id` 自己打的那一票。
```

侧栏 `config.mts:50` 之后加 `{ text: "2.9 给一轮回答打分", link: "/guide/chat#_2-9-给一轮回答打分" },`。

- [ ] **Step 2: `query.md` 两张表各加一行**

5.3 表(`:217` `run_id` 行之后):

```markdown
| `feedback` | object \| null | 当前 `user_id` 对这条消息所在的那一轮打过的分；没打过是 `null`。字段：`rating`（取值：`up` / `down`）、`comment`（string \| null）、`item_id`（string \| null），含义见 [2.9 给一轮回答打分](./chat#_2-9-给一轮回答打分) |
```

5.8「轮的信息」表(`:979` `artifacts` 行之后):

```markdown
| `feedback` | object \| null | 当前 `user_id` 对这一轮打过的分；没打过是 `null`。字段与 [5.3 的 `feedback`](#_5-3-历史消息) 相同 |
```

- [ ] **Step 3: `errors.md` 四处**

`:13` 之后加一行 `- 打分：\`POST /v1/agents/{agent_code}/runs/{run_id}/feedback\``;`:34` `RUN_NOT_FOUND` 行的端点列改为 `取消 run / 审批决策 / 事件接口 / 打分`;`:188` 段落首句改为 `` `RUN_NOT_FOUND` 与 `APPROVAL_NOT_FOUND`：取消 run（`:cancel`）、审批决策（`:decide`）、打分（`/feedback`）三个端点的归属校验与审批查找失败。``;`:248` 那条补充说明里「取消 run 与审批决策是例外」改为「取消 run、审批决策与打分是例外」。

- [ ] **Step 4: `examples.md` 新节(四语言,照 10.6 的形状裁到最小)**

在文件末尾追加 `## 10.9 给一轮回答打分`,`::: code-group` 里四段:curl(与 2.9 示例同一请求)、Python(`urllib.request`,函数 `rate_run(user_id, run_id, rating, comment=None)`,错误处理照 10.6 的 `read → try json.loads` 写法)、Node.js(`fetch`,函数 `rateRun(userId, runId, rating, comment)`,`response.text()` 打错误)、Java(JDK 8 `HttpURLConnection`,类 `RateRun`,复用 10.6 的 `readBody / readErrorBody / jsonEscape` 三个静态方法原文)。每段的示例值统一 `u-123`、`rating: "down"`、`comment: "答非所问"`;返回注释 `{"success": true, "data": {"run_id": "...", "rating": "down", "updated": false}, "error": null}`。

- [ ] **Step 5: `best-practices.md` 两处**

9.5 在 `:81` 之后加:

```markdown
### 打分的对象是一轮不是一条消息

打分接口只接受 `run_id`。一轮里有多条消息时，`item_id` 可以带上调用方自己的段落标签，平台原样保存并回显，但不会用它定位到某条消息。见 [2.9 给一轮回答打分](./chat#_2-9-给一轮回答打分)。
```

9.6 清单末尾加 `- [ ] 对一轮回答打一次 \`down\` 并再打一次 \`up\`，确认第二次响应里 \`updated\` 为 \`true\`，且历史消息里只剩最后一票 —— [2.9](./chat#_2-9-给一轮回答打分)`。

- [ ] **Step 6: 构建 + 死链 + 禁用词扫描**

```bash
cd apps/admin-ui/docs-site && pnpm install --frozen-lockfile && pnpm build && python3 scripts/check_links.py
cd /private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad/wt-plan-p2 && rg -n "图|节点|落库|入库|游标|帧|铸造|mint|闸|门禁|终态|载荷|口径|落地|接线|编排|LangGraph|checkpoint|supervisor|真栈|场景|审计|上一轮|Task|评审" apps/admin-ui/docs-site/guide/chat.md apps/admin-ui/docs-site/guide/query.md apps/admin-ui/docs-site/guide/errors.md apps/admin-ui/docs-site/guide/examples.md apps/admin-ui/docs-site/guide/best-practices.md
```

Expected: 构建过、零死链;禁用词 rg 只允许命中**改动前就存在**的行(用 `git diff -U0 -- apps/admin-ui/docs-site/guide | rg "^\+" | rg "<同一词表>"` 再扫一次,必须为空)。再对照 style-guide §12 清单逐项打勾:标题无标点、表 ≤4 列、参数表三列、响应表三列、正文全角标点、代码块有标题、示例值 `u-123`。

- [ ] **Step 7: 提交**

```bash
git add apps/admin-ui/docs-site/guide/chat.md apps/admin-ui/docs-site/guide/query.md apps/admin-ui/docs-site/guide/errors.md apps/admin-ui/docs-site/guide/examples.md apps/admin-ui/docs-site/guide/best-practices.md apps/admin-ui/docs-site/.vitepress/config.mts
git commit -m "docs(external): 2.9 给一轮回答打分 + 5.3/5.8 feedback 字段 + 错误码 + 10.9 四语言示例 + 注意事项"
```

---
## PR2 —— 👎 当场进池 + 升级 signal + 改票标记 + 修词表漂移

### Task 8: `CurationCandidateRecord` 三字段 + store `upgrade_to_negative` / `mark_feedback_changed`(双实现)+ API 投影

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/eval_dataset.py:74-119`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/curation/base.py:105-203`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/curation/sql.py:199-217`(`_candidate_row_to_dto`)、`:226-249`(`upsert`)、`:361-382`(`update`)、新增两方法
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/curation/memory.py:82-96`(`upsert`)、新增两方法
- Modify: `services/control-plane/src/control_plane/api/curation.py:64-81`(`_candidate_dict`)
- Test: `packages/expert-work-persistence/tests/test_in_memory_curation_store.py`、`tests/test_sql_curation_store.py`(各加两条)
- Test: `services/control-plane/tests/test_curation_api.py:297-308`(list 用例断言新键)

**Interfaces:**
- Consumes: Task 1 的三列。
- Produces:
  - `CurationCandidateRecord` 新字段 `feedback_run_id: UUID | None = None`、`feedback_comment: str | None = None`、`feedback_changed_at: datetime | None = None`。
  - `async def upgrade_to_negative(self, *, tenant_id: UUID, trajectory_key: str, feedback_run_id: UUID, feedback_comment: str | None) -> bool` —— 按 `(tenant_id, trajectory_key)` 命中已有候选,写 `signal='negative_feedback', feedback_rating='down', feedback_run_id, feedback_comment, feedback_changed_at=NULL`;`status` 不动;返回是否命中。
  - `async def mark_feedback_changed(self, *, tenant_id: UUID, feedback_run_id: UUID, at: datetime) -> int` —— `UPDATE … SET feedback_changed_at=:at WHERE tenant_id=:t AND feedback_run_id=:r AND feedback_changed_at IS NULL`;返回行数。
  - `upsert` / `update` / `_candidate_row_to_dto` 携带三列;`_candidate_dict` 输出 `feedback_run_id`(str|null)、`feedback_comment`、`feedback_changed_at`(ISO|null)。

- [ ] **Step 1: 失败测试(两套 store 同一场景)**

`test_in_memory_curation_store.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_candidate_upgrade_to_negative_rewrites_signal_and_feedback_columns() -> None:
    store = InMemoryCurationCandidateStore()
    tenant, key, run = uuid4(), "trajectories/t/x.jsonl", uuid4()
    await store.upsert(_candidate(tenant_id=tenant, trajectory_key=key, signal="failed_outcome"))

    hit = await store.upgrade_to_negative(
        tenant_id=tenant, trajectory_key=key, feedback_run_id=run, feedback_comment="太慢"
    )
    assert hit is True
    row = await store.get_by_trajectory_key(tenant_id=tenant, trajectory_key=key)
    assert row is not None
    assert row.signal == "negative_feedback" and row.feedback_rating == "down"
    assert row.feedback_run_id == run and row.feedback_comment == "太慢"
    assert row.feedback_changed_at is None
    assert row.status is CandidateStatus.PENDING

    # 别的租户 / 不存在的 key → 不命中,不建行。
    assert await store.upgrade_to_negative(
        tenant_id=uuid4(), trajectory_key=key, feedback_run_id=run, feedback_comment=None
    ) is False
    assert len(await store.list_for_review(tenant_id=tenant)) == 1


@pytest.mark.asyncio
async def test_candidate_mark_feedback_changed_is_keyed_by_run_and_idempotent() -> None:
    store = InMemoryCurationCandidateStore()
    tenant, key, run = uuid4(), "trajectories/t/y.jsonl", uuid4()
    await store.upsert(
        _candidate(tenant_id=tenant, trajectory_key=key, signal="negative_feedback", feedback_rating="down")
    )
    await store.upgrade_to_negative(
        tenant_id=tenant, trajectory_key=key, feedback_run_id=run, feedback_comment=None
    )
    at = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    assert await store.mark_feedback_changed(tenant_id=tenant, feedback_run_id=run, at=at) == 1
    assert await store.mark_feedback_changed(tenant_id=tenant, feedback_run_id=run, at=at) == 0
    assert await store.mark_feedback_changed(tenant_id=tenant, feedback_run_id=uuid4(), at=at) == 0
    row = await store.get_by_trajectory_key(tenant_id=tenant, trajectory_key=key)
    assert row is not None and row.feedback_changed_at == at
    # 👍→👎 再来一次:升级清掉改票标记。
    await store.upgrade_to_negative(
        tenant_id=tenant, trajectory_key=key, feedback_run_id=run, feedback_comment="again"
    )
    row = await store.get_by_trajectory_key(tenant_id=tenant, trajectory_key=key)
    assert row is not None and row.feedback_changed_at is None and row.feedback_comment == "again"
```

`test_sql_curation_store.py` 末尾追加同名两条,唯一区别是 `_, candidates = curation_stores` 取 store(照 `:197-206` 的写法),断言逐字相同。

`test_curation_api.py:297-308` 的 list 用例加断言:`assert {"feedback_run_id", "feedback_comment", "feedback_changed_at"} <= set(resp.json()["items"][0])`。

- [ ] **Step 2: 跑确认红**

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_in_memory_curation_store.py -q`
Expected: `AttributeError: 'InMemoryCurationCandidateStore' object has no attribute 'upgrade_to_negative'`。

- [ ] **Step 3: 协议字段 + ABC**

`eval_dataset.py` 在 `retry_count: int = 0`(`:108`)之后:

```python
    #: P-2 — 被踩的那一轮 / 用户原话:审阅员打开候选直接看到,不用翻整条 trajectory。
    feedback_run_id: UUID | None = None
    feedback_comment: str | None = None
    #: P-2 — 👎→👍 改票时间;None = 没改过(升级为 negative_feedback 时清空)。
    feedback_changed_at: datetime | None = None
```

`curation/base.py` 在 `update`(`:174-179`)之后加两个抽象方法(docstring 即上面 Interfaces 的措辞)。

- [ ] **Step 4: SQL 实现**

`sql.py` `_candidate_row_to_dto` 加 `feedback_run_id=row.feedback_run_id, feedback_comment=row.feedback_comment, feedback_changed_at=row.feedback_changed_at`;`upsert` 的 `.values(...)` 加 `feedback_run_id=record.feedback_run_id, feedback_comment=record.feedback_comment, feedback_changed_at=record.feedback_changed_at`;`update` 的 `.values(...)` 同样加三个。新增:

```python
    async def upgrade_to_negative(
        self,
        *,
        tenant_id: UUID,
        trajectory_key: str,
        feedback_run_id: UUID,
        feedback_comment: str | None,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(CurationCandidateRow)
                .where(
                    CurationCandidateRow.tenant_id == tenant_id,
                    CurationCandidateRow.trajectory_key == trajectory_key,
                )
                .values(
                    signal="negative_feedback",
                    feedback_rating="down",
                    feedback_run_id=feedback_run_id,
                    feedback_comment=feedback_comment,
                    feedback_changed_at=None,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def mark_feedback_changed(
        self, *, tenant_id: UUID, feedback_run_id: UUID, at: datetime
    ) -> int:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(CurationCandidateRow)
                .where(
                    CurationCandidateRow.tenant_id == tenant_id,
                    CurationCandidateRow.feedback_run_id == feedback_run_id,
                    CurationCandidateRow.feedback_changed_at.is_(None),
                )
                .values(feedback_changed_at=at)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
```

- [ ] **Step 5: 内存实现(谓词逐字对应)**

```python
    async def upgrade_to_negative(
        self,
        *,
        tenant_id: UUID,
        trajectory_key: str,
        feedback_run_id: UUID,
        feedback_comment: str | None,
    ) -> bool:
        for cid, r in list(self._rows.items()):
            if r.tenant_id == tenant_id and r.trajectory_key == trajectory_key:
                self._rows[cid] = r.model_copy(
                    update={
                        "signal": "negative_feedback",
                        "feedback_rating": "down",
                        "feedback_run_id": feedback_run_id,
                        "feedback_comment": feedback_comment,
                        "feedback_changed_at": None,
                    }
                )
                return True
        return False

    async def mark_feedback_changed(
        self, *, tenant_id: UUID, feedback_run_id: UUID, at: datetime
    ) -> int:
        changed = 0
        for cid, r in list(self._rows.items()):
            if (
                r.tenant_id == tenant_id
                and r.feedback_run_id == feedback_run_id
                and r.feedback_changed_at is None
            ):
                self._rows[cid] = r.model_copy(update={"feedback_changed_at": at})
                changed += 1
        return changed
```

`api/curation.py` `_candidate_dict` 加:

```python
        "feedback_run_id": (
            str(record.feedback_run_id) if record.feedback_run_id is not None else None
        ),
        "feedback_comment": record.feedback_comment,
        "feedback_changed_at": (
            record.feedback_changed_at.isoformat()
            if record.feedback_changed_at is not None
            else None
        ),
```

- [ ] **Step 6: 跑确认绿(含并发集成测 —— §7-3 已核实,这条 🔴 必做,不是可选)**

> **为什么必做(Task 0 §7-3 实测,09-09):`curation_worker` 在两个 pod 上同时跑,而且代码里没有任何互斥。** `curation_worker.py:3` 那句「single-replica」只是一句陈旧注释,不是机制 —— 全文件 grep `advisory|lock|replica` 只命中那句话本身,**没有 leader 选举 / advisory lock / lease**。运行期实证:`pg_stat_activity` 按 `client_addr` 采样 340s,测试环境两个 pod(`172.16.176.31` 于 t=178s、`172.16.176.32` 于 t=276s)**各自独立**发出 worker 的 `curation_candidate` 预检查询;生产 pod `…-sdpzg` 的 `expert_work_control_plane_curation_candidates_detected_total = 1.0` 而同期 `…-pz5s8 = 0.0`。
>
> **今天不炸的唯一原因**(生产库上实测确认存在,不是推测):唯一索引
> `curation_candidate_trajectory_uniq ON curation_candidate (tenant_id, trajectory_key)`
> 配 `packages/expert-work-persistence/src/expert_work/persistence/curation/sql.py:246` 的 `on_conflict_do_nothing(constraint="curation_candidate_trajectory_uniq")`。
>
> P-2 把**同步 upsert**(Task 9,落在请求路径上)加进来之后,同一把键上的并发方从「两个 worker」变成「两个 worker + N 个对外写反馈请求」。**只要新路径复用同一把唯一键 + 升级 UPDATE 幂等,就不需要引入锁** —— 但这条不变式必须有测试钉住,否则下次谁把 `on_conflict_do_nothing` 改成 `INSERT` 或者给升级加个「先读后写」就静默退化成竞争。这就是下面这条用例存在的理由。

`test_sql_curation_store.py` 再加一条并发用例:两个协程同时对同一 `(tenant, key)` 做 `upsert`(`asyncio.gather`)→ `sum(results) == 1`、表里一行;再 `gather` 两个 `upgrade_to_negative` → 两个都 `True`、行仍一条、`feedback_comment` 是其中之一。**变异自证**:把 `sql.py:246` 的 `.on_conflict_do_nothing(constraint=…)` 去掉 → 并发用例必须红(`IntegrityError` / 两行);改回 → 绿。

Run: `uv run --no-sync pytest packages/expert-work-persistence/tests/test_in_memory_curation_store.py packages/expert-work-persistence/tests/test_sql_curation_store.py services/control-plane/tests/test_curation_api.py -q` → 全绿。

- [ ] **Step 7: 变异自证**

SQL `upgrade_to_negative` 删掉 `feedback_changed_at=None,` → SQL 版 `..._idempotent` 末段 `row.feedback_changed_at is None` 红;改回 → 绿。内存版 `mark_feedback_changed` 删掉 `and r.feedback_changed_at is None` → 第二次调用返回 1 而非 0 → 红;改回 → 绿。`_candidate_dict` 不加三键 → `test_curation_api.py` 新断言红。

- [ ] **Step 8: lint + 提交**

```bash
uv run --no-sync ruff check packages/ services/control-plane && uv run --no-sync ruff format packages/ services/control-plane
git add packages/expert-work-protocol/src/expert_work/protocol/eval_dataset.py packages/expert-work-persistence/src/expert_work/persistence/curation/base.py packages/expert-work-persistence/src/expert_work/persistence/curation/sql.py packages/expert-work-persistence/src/expert_work/persistence/curation/memory.py packages/expert-work-persistence/tests/test_in_memory_curation_store.py packages/expert-work-persistence/tests/test_sql_curation_store.py services/control-plane/src/control_plane/api/curation.py services/control-plane/tests/test_curation_api.py
git commit -m "feat(curation): 候选带 feedback_run_id/comment/changed_at;store 加 upgrade_to_negative + mark_feedback_changed(双实现同谓词)"
```

---

### Task 9: 👎 同步进池 —— `feedback_candidates.py` + `TrajectoryReader.find_by_thread` + 接入两条写路径

**Files:**
- Create: `services/control-plane/src/control_plane/feedback_candidates.py`
- Modify: `services/orchestrator/src/orchestrator/trajectory/reader.py:52-99`(新增 `find_by_thread`)
- Modify: `services/control-plane/src/control_plane/api/external_feedback.py`(Task 3 的 handler,upsert 之后接入)
- Modify: `services/control-plane/src/control_plane/api/feedback.py`(Task 5 的 handler,同样接入)
- Test: `services/orchestrator/tests/test_trajectory_reader.py`(新增一条)
- Test: `services/control-plane/tests/test_feedback_candidates.py`(新建)
- Test: `services/control-plane/tests/test_external_feedback.py`(新增两条端到端)

**Interfaces:**
- Consumes: `TrajectoryReader.list_keys(tenant_id=…)` / `read(key)`(`reader.py:64-99`;key 形如 `trajectories/{tenant}/{outcome}/{YYYY}/{MM}/{DD}/{thread_id}.jsonl`,`recorder.py:182-201`);`ThreadMetaStore.get(thread_id, tenant_id=)`;Task 8 的两个 store 方法;`app.state.object_store`(`app.py:1595`,注入 runtime 的测试里是 `None`,`:2471`)。
- Produces:
  - `TrajectoryReader.find_by_thread(*, tenant_id: UUID, thread_id: UUID) -> StoredTrajectory | None` —— 列租户前缀,取以 `/{thread_id}.jsonl` 结尾的全部 key(同一 thread 只有个位数:每个 `(outcome, 日期)` 分区至多一个),**逐个 `read()`**,按 `finished_at` 取最大,并列取 key 大的。**不能按 key 字典序取**:key 里 `outcome` 排在日期前面(`recorder.py:198-200`),`…/success/2026/09/01/x.jsonl` 字典序大于 `…/failed/2026/09/09/x.jsonl`,`max(keys)` 会挑到旧的。
  - `feedback_candidates.py`:

    ```python
    @dataclass(frozen=True)
    class CandidateSyncDeps:
        threads: ThreadMetaStore
        candidates: CurationCandidateStore
        reader: TrajectoryReader | None  # None = 没有 ObjectStore(注入 runtime 的测试)

    CandidateSyncResult = Literal["inserted", "upgraded", "changed", "deferred", "noop"]

    async def sync_candidate_for_feedback(
        *, deps: CandidateSyncDeps, tenant_id: UUID, thread_id: UUID, run_id: UUID,
        rating: str, previous_rating: str | None, comment: str | None,
    ) -> CandidateSyncResult
    ```

    规则:`rating == "down"` → 有 thread_meta(且 `agent_name` 非空)且 trajectory 已落盘 → 已有候选则 `upgrade_to_negative` → `"upgraded"`;没有则 `upsert(CurationCandidateRecord(signal="negative_feedback", feedback_rating="down", feedback_run_id=run_id, feedback_comment=comment, …))` → `"inserted"`(`upsert` 返回 False 说明刚被别人插入 → 再 `upgrade_to_negative` → `"upgraded"`);缺 meta / 缺 trajectory / 无 reader → `"deferred"`(worker 兜底)。`rating == "up"` 且 `previous_rating == "down"` → `mark_feedback_changed` → 行数>0 `"changed"` 否则 `"noop"`。其它 → `"noop"`。**👍 永不建候选、永不降级。**
  - 两个端点在 `upsert` 之后调用它,`try/except Exception` → `logger.warning("feedback.candidate_sync_failed", exc_info=True)`,结果写进审计 `details["candidate"]`;反馈写入永不因它失败。`previous_rating` 来自 upsert 前的 `list_for_thread_scoped` 里同 `(run, actor)` 的行(端点里先读一次)。

- [ ] **Step 1: reader 失败测试**

`services/orchestrator/tests/test_trajectory_reader.py` 末尾追加(照该文件既有的 `InMemoryObjectStore` + `TrajectoryRecorder` 写法):

```python
@pytest.mark.asyncio
async def test_find_by_thread_returns_newest_by_finished_at_not_by_key_order() -> None:
    """旧 ``success``、新 ``failed``:key 字典序里 ``success/…`` 排在 ``failed/…`` 后面
    (outcome 段在日期段前面),按 key 取最大会拿到旧的 —— 必须按 ``finished_at``。"""
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    tenant, thread = uuid4(), uuid4()
    old = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    new = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    await recorder.record(
        TrajectoryRecord(thread_id=thread, tenant_id=tenant, outcome="success",
                         messages=[HumanMessage(content="a")], finished_at=old)
    )
    await recorder.record(
        TrajectoryRecord(thread_id=thread, tenant_id=tenant, outcome="failed",
                         messages=[HumanMessage(content="b")], run_id=uuid4(), finished_at=new)
    )
    reader = TrajectoryReader(object_store=store)
    keys = sorted(k for k in await reader.list_keys(tenant_id=tenant) if k.endswith(f"/{thread}.jsonl"))
    assert keys[-1].split("/")[2] == "success"  # 钉住前提:字典序最大的 key 是旧的那个
    found = await reader.find_by_thread(tenant_id=tenant, thread_id=thread)
    assert found is not None
    assert found.outcome == "failed" and found.finished_at == new
    assert await reader.find_by_thread(tenant_id=tenant, thread_id=uuid4()) is None
    assert await reader.find_by_thread(tenant_id=uuid4(), thread_id=thread) is None


@pytest.mark.asyncio
async def test_find_by_thread_breaks_finished_at_ties_by_key() -> None:
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    tenant, thread = uuid4(), uuid4()
    at = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    for outcome in ("failed", "success"):
        await recorder.record(
            TrajectoryRecord(thread_id=thread, tenant_id=tenant, outcome=outcome,  # type: ignore[arg-type]
                             messages=[HumanMessage(content=outcome)], finished_at=at)
        )
    found = await TrajectoryReader(object_store=store).find_by_thread(tenant_id=tenant, thread_id=thread)
    assert found is not None and found.outcome == "success"  # 同 finished_at → key 大者(确定性)
```

- [ ] **Step 2: 实现 `find_by_thread`**

```python
    async def find_by_thread(self, *, tenant_id: UUID, thread_id: UUID) -> StoredTrajectory | None:
        """The newest stored trajectory for ``thread_id`` under ``tenant_id``, or ``None``.

        P-2 — the synchronous 👎 → candidate path needs "is this thread's
        trajectory on disk yet" without waiting for the curation worker's
        300 s sweep. Keys are ``{prefix}/{tenant}/{outcome}/{YYYY}/{MM}/{DD}/
        {thread}.jsonl`` — the OUTCOME segment comes BEFORE the date, so key
        order is NOT time order (``…/success/2026/09/01/…`` sorts after
        ``…/failed/2026/09/09/…``). A thread matches at most one key per
        ``(outcome, day)`` partition, i.e. a handful, so every match is read
        and the one with the greatest ``finished_at`` wins; ties (same
        instant, or no ``finished_at`` on a legacy envelope) fall back to the
        greater key so the choice is deterministic. Same list-prefix call the
        worker already issues per sweep, scoped to one tenant.
        """
        suffix = f"/{thread_id}.jsonl"
        keys = [k for k in await self.list_keys(tenant_id=tenant_id) if k.endswith(suffix)]
        newest: StoredTrajectory | None = None
        for key in keys:
            stored = await self.read(key)
            if stored is None:
                continue
            if newest is None or _recency(stored) > _recency(newest):
                newest = stored
        return newest
```

模块级(与 `_opt_uuid` / `_opt_dt` 并列)加一个具名 key 函数,不用 lambda:

```python
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _recency(stored: StoredTrajectory) -> tuple[datetime, str]:
    """Sort key for :meth:`TrajectoryReader.find_by_thread` — ``finished_at``
    first (legacy envelopes without one sort oldest), key string second."""
    return (stored.finished_at or _EPOCH, stored.key)
```

(`from datetime import UTC, datetime` —— `reader.py:17` 现只 import `datetime`,补 `UTC`。)

Run: `uv run --no-sync pytest services/orchestrator/tests/test_trajectory_reader.py -q` → 绿(先跑一次确认红:`AttributeError`)。

- [ ] **Step 3: 同步逻辑失败测试**

```python
# services/control-plane/tests/test_feedback_candidates.py
"""👎 当场进池 / 改票标记 —— ``control_plane.feedback_candidates``(P-2 PR2)。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from control_plane.feedback_candidates import CandidateSyncDeps, sync_candidate_for_feedback
from expert_work.persistence import InMemoryCurationCandidateStore, InMemoryThreadMetaStore
from expert_work.protocol import CandidateStatus
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryReader, TrajectoryRecord, TrajectoryRecorder

_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Fx:
    def __init__(self, *, with_reader: bool = True) -> None:
        self.object_store = InMemoryObjectStore()
        self.threads = InMemoryThreadMetaStore()
        self.candidates = InMemoryCurationCandidateStore()
        self.deps = CandidateSyncDeps(
            threads=self.threads,
            candidates=self.candidates,
            reader=TrajectoryReader(object_store=self.object_store) if with_reader else None,
        )

    async def seed_thread(self, tenant: UUID, thread: UUID, *, agent_name: str | None = "reporter") -> None:
        await self.threads.create(
            thread_id=thread, tenant_id=tenant, created_by="seed", user_id=uuid4(),
            agent_name=agent_name, agent_version="1.0.0",
        )

    async def seed_trajectory(self, tenant: UUID, thread: UUID, *, outcome: str = "success") -> None:
        await TrajectoryRecorder(object_store=self.object_store).record(
            TrajectoryRecord(
                thread_id=thread, tenant_id=tenant, outcome=outcome,  # type: ignore[arg-type]
                messages=[HumanMessage(content="hi"), AIMessage(content="bye")],
                run_id=uuid4(), finished_at=_AT,
            )
        )

    async def sync(self, tenant: UUID, thread: UUID, run: UUID, *, rating: str, previous: str | None = None, comment: str | None = None) -> str:
        return await sync_candidate_for_feedback(
            deps=self.deps, tenant_id=tenant, thread_id=thread, run_id=run,
            rating=rating, previous_rating=previous, comment=comment,
        )


@pytest.mark.asyncio
async def test_down_with_trajectory_inserts_candidate_immediately() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    await fx.seed_trajectory(tenant, thread)
    assert await fx.sync(tenant, thread, run, rating="down", comment="太慢") == "inserted"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_rating == "down"
    assert rows[0].feedback_run_id == run and rows[0].feedback_comment == "太慢"
    assert rows[0].agent_name == "reporter" and rows[0].status is CandidateStatus.PENDING
    # 重复 👎 不产生第二条候选。
    assert await fx.sync(tenant, thread, run, rating="down", previous="down", comment="还是慢") == "upgraded"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1 and rows[0].feedback_comment == "还是慢"


@pytest.mark.asyncio
async def test_down_upgrades_existing_failed_outcome_candidate() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    await fx.seed_trajectory(tenant, thread, outcome="failed")
    keys = await fx.deps.reader.list_keys(tenant_id=tenant)  # type: ignore[union-attr]
    from expert_work.protocol import CurationCandidateRecord
    await fx.candidates.upsert(
        CurationCandidateRecord(
            id=uuid4(), tenant_id=tenant, agent_name="reporter", agent_version="1.0.0",
            thread_id=thread, trajectory_key=keys[0], outcome="failed", signal="failed_outcome",
            detected_at=_AT,
        )
    )
    assert await fx.sync(tenant, thread, run, rating="down") == "upgraded"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1 and rows[0].signal == "negative_feedback" and rows[0].feedback_run_id == run


@pytest.mark.asyncio
async def test_down_without_trajectory_or_meta_defers_and_creates_nothing() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    assert await fx.sync(tenant, thread, run, rating="down") == "deferred"  # 没落盘
    await fx.seed_trajectory(tenant, thread)
    assert await fx.sync(tenant, uuid4(), run, rating="down") == "deferred"  # 没 meta
    no_reader = _Fx(with_reader=False)
    await no_reader.seed_thread(tenant, thread)
    assert await no_reader.sync(tenant, thread, run, rating="down") == "deferred"
    assert await fx.candidates.list_for_review(tenant_id=tenant) == []


@pytest.mark.asyncio
async def test_up_never_creates_and_marks_change_only_after_a_down() -> None:
    fx = _Fx()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant, thread)
    await fx.seed_trajectory(tenant, thread)
    assert await fx.sync(tenant, thread, run, rating="up") == "noop"
    assert await fx.candidates.list_for_review(tenant_id=tenant) == []
    assert await fx.sync(tenant, thread, run, rating="down") == "inserted"
    assert await fx.sync(tenant, thread, run, rating="up", previous="down") == "changed"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1 and rows[0].signal == "negative_feedback"  # 保留、不降级
    assert rows[0].feedback_changed_at is not None
    # 👍→👎:升级清掉改票标记。
    assert await fx.sync(tenant, thread, run, rating="down", previous="up") == "upgraded"
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].feedback_changed_at is None
```

Run: `uv run --no-sync pytest services/control-plane/tests/test_feedback_candidates.py -q` → `ModuleNotFoundError: control_plane.feedback_candidates`。

- [ ] **Step 4: 实现 `feedback_candidates.py`**

```python
"""👎 当场进策展待审池(P-2 §5)—— 端点与控制台共用的纯逻辑。

worker(``curation_worker.py``)每 300s 扫一遍 trajectory 才建候选;用户点 👎 是最有
价值的信号,不该等。这里在写反馈的同一请求里:trajectory 已落盘 → 直接建 / 升级候选;
还没落盘(run 刚结束的窗口)→ 什么都不做,worker 兜底。👍 永不建候选、永不降级;
👎→👍 只在候选上打改票标记。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from expert_work.persistence import CurationCandidateStore, ThreadMetaStore
from expert_work.protocol import CurationCandidateRecord
from orchestrator.trajectory import TrajectoryReader

CandidateSyncResult = Literal["inserted", "upgraded", "changed", "deferred", "noop"]


@dataclass(frozen=True)
class CandidateSyncDeps:
    threads: ThreadMetaStore
    candidates: CurationCandidateStore
    #: ``None`` = 没有 ObjectStore(注入 runtime 的测试装配)→ 一律 ``deferred``。
    reader: TrajectoryReader | None


async def sync_candidate_for_feedback(
    *,
    deps: CandidateSyncDeps,
    tenant_id: UUID,
    thread_id: UUID,
    run_id: UUID,
    rating: str,
    previous_rating: str | None,
    comment: str | None,
) -> CandidateSyncResult:
    if rating == "up":
        if previous_rating != "down":
            return "noop"
        changed = await deps.candidates.mark_feedback_changed(
            tenant_id=tenant_id, feedback_run_id=run_id, at=datetime.now(UTC)
        )
        return "changed" if changed else "noop"
    if rating != "down" or deps.reader is None:
        return "deferred" if rating == "down" else "noop"
    meta = await deps.threads.get(thread_id, tenant_id=tenant_id)
    if meta is None or meta.agent_name is None:
        return "deferred"
    stored = await deps.reader.find_by_thread(tenant_id=tenant_id, thread_id=thread_id)
    if stored is None:
        return "deferred"
    existing = await deps.candidates.get_by_trajectory_key(
        tenant_id=tenant_id, trajectory_key=stored.key
    )
    if existing is None:
        inserted = await deps.candidates.upsert(
            CurationCandidateRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                agent_name=meta.agent_name,
                agent_version=meta.agent_version,
                thread_id=thread_id,
                user_id=stored.user_id or meta.user_id,
                trajectory_key=stored.key,
                outcome=stored.outcome,
                signal="negative_feedback",
                feedback_rating="down",
                feedback_run_id=run_id,
                feedback_comment=comment,
                detected_at=datetime.now(UTC),
            )
        )
        if inserted:
            return "inserted"
    # 已有候选(或刚被 worker / 另一副本抢先插入)→ 升级为 negative_feedback 并补两列。
    await deps.candidates.upgrade_to_negative(
        tenant_id=tenant_id,
        trajectory_key=stored.key,
        feedback_run_id=run_id,
        feedback_comment=comment,
    )
    return "upgraded"


__all__ = ["CandidateSyncDeps", "CandidateSyncResult", "sync_candidate_for_feedback"]
```

- [ ] **Step 5: 接入两条写路径**

`api/external_feedback.py`:加依赖 `_get_candidate_sync_deps(request) -> CandidateSyncDeps`(`threads=request.app.state.thread_meta_repo`,`candidates=request.app.state.curation_candidate_store`,`reader=TrajectoryReader(object_store=os) if (os := getattr(request.app.state, "object_store", None)) is not None else None`);handler 在 `upsert` **之前**读 `previous`:

```python
        previous = next(
            (
                r.rating
                for r in await store.list_for_thread_scoped(
                    tenant_id=tenant_id, thread_id=run.thread_id
                )
                if r.run_id == run.run_id and r.actor_id == actor_id
            ),
            None,
        )
```

`upsert` 之后:

```python
        candidate: CandidateSyncResult = "noop"
        try:
            candidate = await sync_candidate_for_feedback(
                deps=sync_deps,
                tenant_id=tenant_id,
                thread_id=run.thread_id,
                run_id=run.run_id,
                rating=payload.rating,
                previous_rating=previous,
                comment=payload.comment,
            )
        except Exception:
            # 进池是反馈的副产品:它失败不能让用户那一票丢掉;worker 300s 后兜底。
            logger.warning("feedback.candidate_sync_failed", exc_info=True)
```

审计 `details` 加 `"candidate": candidate`。`api/feedback.py` 控制台 handler 同样接入(`previous` 用 `actor_id=request.state.actor_id`)。两个文件都加 `logger = logging.getLogger("expert_work.control_plane.api.<module>")`。

- [ ] **Step 6: 端到端失败测试(`test_external_feedback.py` 追加两条)**

```python
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.trajectory import TrajectoryRecord, TrajectoryRecorder
from langchain_core.messages import AIMessage, HumanMessage


async def _land_trajectory(ctx: _Ctx, thread: UUID, run_id: UUID) -> None:
    object_store = InMemoryObjectStore()
    ctx.app.state.object_store = object_store
    await TrajectoryRecorder(object_store=object_store).record(
        TrajectoryRecord(
            thread_id=thread, tenant_id=ctx.tenant_id, outcome="success",
            messages=[HumanMessage(content="hi"), AIMessage(content="bye")],
            run_id=run_id, finished_at=_NOW,
        )
    )


@pytest.mark.asyncio
async def test_down_lands_a_candidate_in_the_same_request(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    await _land_trajectory(ctx, thread, run_id)
    resp = await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down", "comment": "太慢"})
    assert resp.status_code == 200, resp.text
    rows = await ctx.app.state.curation_candidate_store.list_for_review(tenant_id=ctx.tenant_id)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_run_id == run_id
    assert rows[0].feedback_comment == "太慢"
    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.tenant_id, limit=100))
    assert [e.details["candidate"] for e in page.entries if e.action.value == "feedback:create"] == ["inserted"]


@pytest.mark.asyncio
async def test_up_never_lands_a_candidate_and_change_is_marked(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread = await ctx.bind_session("cust-77")
    run_id = await ctx.seed_run(thread, "cust-77")
    await _land_trajectory(ctx, thread, run_id)
    assert (await ctx.rate(run_id, {"user_id": "cust-77", "rating": "up"})).status_code == 200
    candidates = ctx.app.state.curation_candidate_store
    assert await candidates.list_for_review(tenant_id=ctx.tenant_id) == []
    assert (await ctx.rate(run_id, {"user_id": "cust-77", "rating": "down"})).status_code == 200
    assert (await ctx.rate(run_id, {"user_id": "cust-77", "rating": "up"})).status_code == 200
    rows = await candidates.list_for_review(tenant_id=ctx.tenant_id)
    assert len(rows) == 1 and rows[0].feedback_changed_at is not None
```

- [ ] **Step 7: 跑确认绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_feedback_candidates.py services/control-plane/tests/test_external_feedback.py services/control-plane/tests/test_feedback_api.py services/orchestrator/tests/test_trajectory_reader.py -q` → 全绿。

- [ ] **Step 8: 变异自证(spec §8 PR2 点名的那刀 + 三刀)**

1. **删升级逻辑**:`sync_candidate_for_feedback` 末尾的 `await deps.candidates.upgrade_to_negative(...)` 整段删掉、直接 `return "upgraded"` → `test_down_upgrades_existing_failed_outcome_candidate` 的 `rows[0].signal == "negative_feedback"` 红;改回 → 绿。
2. `if rating == "up": if previous_rating != "down": return "noop"` 改成无条件 `mark_feedback_changed` → `test_up_never_creates_and_marks_change_only_after_a_down` 不红(标记按 run 命中不了)→ 这刀说明「👍 不建候选」的守卫是 `rating == "up"` 早返回,不是这行;改成删掉整个 `if rating == "up":` 分支让 👍 也走 👎 路径 → 同一用例第一段 `list_for_review == []` 红;改回 → 绿。
3. `find_by_thread` 整个循环换回 `return await self.read(max(keys))`(按 key 字典序取)→ `test_find_by_thread_returns_newest_by_finished_at_not_by_key_order` 在 `found.outcome == "failed"` 红(拿到的是旧的 `success`);改回 → 绿。再把 `_recency` 的元组第二项 `stored.key` 删掉(只剩 `finished_at`)→ `test_find_by_thread_breaks_finished_at_ties_by_key` 的结果取决于 `list_prefix` 顺序,`InMemoryObjectStore` 下会红或时红时绿;改回 → 稳定绿。
4. 端点里把 `try/except` 去掉并让 `sync_deps.candidates` 抛错(临时在测试里 monkeypatch `upsert` raise)→ 用户那一票返回 500;恢复 → 200 且 `candidate` 仍写进审计。这一刀做完就撤,不留测试(它测的是 except 分支,`test_down_without_trajectory_or_meta_defers_and_creates_nothing` 已覆盖 deferred)。

- [ ] **Step 9: lint + 提交**

```bash
uv run --no-sync ruff check services/ && uv run --no-sync ruff format services/
git add services/control-plane/src/control_plane/feedback_candidates.py services/orchestrator/src/orchestrator/trajectory/reader.py services/control-plane/src/control_plane/api/external_feedback.py services/control-plane/src/control_plane/api/feedback.py services/orchestrator/tests/test_trajectory_reader.py services/control-plane/tests/test_feedback_candidates.py services/control-plane/tests/test_external_feedback.py
git commit -m "feat(curation): 👎 当场进池 —— trajectory 已落盘直接建/升级候选,未落盘 worker 兜底;👎→👍 打改票标记"
```

---
### Task 10: `curation_worker.py` 预检去重改「可升级」+ 新建候选带两列

**Files:**
- Modify: `services/control-plane/src/control_plane/curation_worker.py:207-229`(`_process_key`)、`:231-267`(`_evaluate`)、模块 docstring `:17-20`
- Test: `services/control-plane/tests/test_curation_worker.py:104-109`(`seed_feedback` 加 `run_id`/`comment` 参数)+ 新增两条

**Interfaces:**
- Consumes: Task 8 的 `upgrade_to_negative`;`FeedbackStore.list_for_thread`(不改,worker 在 `_tenant_scope` 里读)。
- Produces: `_process_key`:已有候选且 `signal != "negative_feedback"` 且 thread 有 👎 → `upgrade_to_negative`(计入 `run_once` 返回值);`_evaluate` 新建 `negative_feedback` 候选时带最新一条 👎 的 `run_id` / `comment`。`test_rescan_is_idempotent`(`:211`)语义不变(没有 👎 的 failed_outcome 候选第二次仍是 0)。

- [ ] **Step 1: 失败测试**

`seed_feedback` 改成:

```python
    async def seed_feedback(
        self,
        *,
        tenant_id: UUID,
        thread_id: UUID,
        rating: str,
        run_id: UUID | None = None,
        comment: str | None = None,
    ) -> None:
        await self.feedback.insert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=run_id,
                rating=rating,
                comment=comment,
                actor_id="user@example.com",
            )
        )
```

新增:

```python
@pytest.mark.asyncio
async def test_negative_candidate_carries_run_and_comment() -> None:
    fx = _Fixture()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="success")
    await fx.seed_feedback(tenant_id=tenant, thread_id=thread, rating="down", run_id=run, comment="太慢")
    assert await fx.worker.run_once() == 1
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert rows[0].feedback_run_id == run and rows[0].feedback_comment == "太慢"


@pytest.mark.asyncio
async def test_existing_failed_candidate_is_upgraded_when_a_down_arrives_later() -> None:
    fx = _Fixture()
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    await fx.seed_thread(tenant_id=tenant, thread_id=thread)
    await fx.seed_trajectory(tenant_id=tenant, thread_id=thread, outcome="failed")
    assert await fx.worker.run_once() == 1  # failed_outcome
    await fx.seed_feedback(tenant_id=tenant, thread_id=thread, rating="down", run_id=run, comment="错了")
    assert await fx.worker.run_once() == 1  # 升级计一次
    rows = await fx.candidates.list_for_review(tenant_id=tenant)
    assert len(rows) == 1
    assert rows[0].signal == "negative_feedback" and rows[0].feedback_run_id == run
    assert await fx.worker.run_once() == 0  # 已是 negative,第三次不再计
```

Run: `uv run --no-sync pytest services/control-plane/tests/test_curation_worker.py -q` → 两条新用例红(`feedback_run_id is None` / 第二次 `run_once()` 得 0)。

- [ ] **Step 2: 实现**

`_process_key`(`:207-229`)的 `if existing is not None: return False` 改为:

```python
        if existing is not None:
            return await self._maybe_upgrade(existing)
```

新增方法:

```python
    async def _maybe_upgrade(self, existing: CurationCandidateRecord) -> bool:
        """P-2 §5 — a candidate flagged on an earlier sweep for a weaker signal
        becomes ``negative_feedback`` once a 👎 lands on its thread. Only the
        signal / feedback columns change; ``status`` (the human verdict) is
        left alone. Already-negative candidates are the cheap no-op the
        pre-check used to be."""
        if existing.signal == "negative_feedback":
            return False
        with _tenant_scope(existing.tenant_id):
            feedback = await self._feedback.list_for_thread(thread_id=existing.thread_id)
        newest_down = next((f for f in feedback if f.rating == "down"), None)
        if newest_down is None or newest_down.run_id is None:
            return False
        with _tenant_scope(existing.tenant_id):
            upgraded = await self._candidates.upgrade_to_negative(
                tenant_id=existing.tenant_id,
                trajectory_key=existing.trajectory_key,
                feedback_run_id=newest_down.run_id,
                feedback_comment=newest_down.comment,
            )
        return upgraded
```

`_evaluate`(`:255-267`)的 `CurationCandidateRecord(...)` 加两项:

```python
            feedback_run_id=newest_down.run_id if newest_down is not None else None,
            feedback_comment=newest_down.comment if newest_down is not None else None,
```

其中在 `:241` 之后加 `newest_down = next((f for f in feedback if f.rating == "down"), None)`(`list_for_thread` 是 `id` 降序,首个即最新)。模块 docstring `:19-20` 那句「re-scanning a trajectory is a cheap no-op — a pre-check skips even the ObjectStore read」改为「…is a cheap no-op unless a 👎 has since landed on the thread (P-2: the pre-check then upgrades the signal in place, still without an ObjectStore read)」。

- [ ] **Step 3: 跑确认绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_curation_worker.py -q` → 全绿(既有 16 条不变)。

- [ ] **Step 4: 变异自证**

`_maybe_upgrade` 首行改成 `if existing.signal == "negative_feedback": return True` → 升级用例末行 `== 0` 红;改回 → 绿。`_evaluate` 里两项改成常量 `None` → `test_negative_candidate_carries_run_and_comment` 红;改回 → 绿。

- [ ] **Step 5: lint + 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane/src/control_plane/curation_worker.py services/control-plane/tests/test_curation_worker.py
git commit -m "feat(curation): worker 预检去重改「可升级」—— 已建候选遇后到的 👎 升级为 negative_feedback 并补 run/原话"
```

---

### Task 11: 修三处词表漂移 —— promote `source`、signal 枚举、`feedback_rating` 类型 + promote 弹窗补 `expected`

**Files:**
- Modify: `apps/admin-ui/src/api/curation.ts:20-27`(两个枚举)、`:29-44`(`feedback_rating` + 三个新字段)、`:97-104`(`PromoteCandidateBody`)
- Modify: `apps/admin-ui/src/pages/curation/CandidatesPanel.tsx:56-62`(`SIGNAL_OPTIONS`)、`:134-153`(`onPromote`)、`:352-372`(promote Modal)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts:1591-1622`(`curation` type)、`:4857-4891`(values);`zh-CN.ts:1677-1710`
- Test: `apps/admin-ui/src/pages/__tests__/Curation.test.tsx`(新增两条)

**Interfaces:**
- Consumes: 后端真值:`EvalDatasetSource = "golden" | "trajectory" | "regression"`(`protocol/eval_dataset.py:36`);`CurationSignal` 四值(`:42-50`);`FeedbackRating = "up" | "down"`(`:57`);`EvalDatasetRecord._check_expected`(`:146-156`):`golden` / `regression` 必须带非空 `expected`。
- Produces: 前端 `EvalDatasetSource` / `CurationSignal` / `feedback_rating` 与后端一致;promote 请求体 `source` 按候选 signal 推导:`negative_feedback` / `failed_outcome` → `"regression"`(弹窗必填 `expected` JSON),`positive_feedback` / `implicit_success` → `"trajectory"`(`expected` 选填)。`CurationCandidate` 新增 `feedback_run_id: string | null`、`feedback_comment: string | null`、`feedback_changed_at: string | null`(展示在 PR3 Task 17)。

**实测前提(Task 0 §7-1,09-09 —— 下面的步骤已按这些真值写好,这里补上「凭什么」)。** 用控制台**原样载荷**在真 app + 真路由 + 真 JWT 上进程内发 HTTP,拿到的是真实状态码:

| 真打的请求 | 返回 |
|---|---|
| `POST /v1/curation/candidates/{id}/promote` `{"name":…,"source":"promoted_candidate"}`(= `CandidatesPanel.tsx:141` 今天发的) | **422** `literal_error`,`msg = "Input should be 'golden', 'trajectory' or 'regression'"` |
| 同一候选,只把 `source` 换成 `"trajectory"` | **201** |

→ **promote 本身没坏,唯一病因就是词表漂移** —— 这也是 Step 5 那句「回路最后一步走通的证据」的底气。

signal 下拉逐个真打 `GET /v1/curation/candidates?status=pending&signal=…`:

| 前端 `SIGNAL_OPTIONS`(`CandidatesPanel.tsx:56-62`)今天的五个值 | 返回 |
|---|---|
| `manual` | **422** |
| `negative_feedback` | 200(0 条) |
| `tool_failure` | **422** |
| `timeout` | **422** |
| `policy_block` | **422** |

后端真值只有四个:`('negative_feedback', 'failed_outcome', 'positive_feedback', 'implicit_success')`。所以这个下拉**五个选项里四个 422、唯一能通的那个必然筛空** —— 因为前端漏掉的 `failed_outcome` / `implicit_success` **恰好是库里唯二真实有数据的**(Task 0 §7-2 实测:测试环境 167 行 = 162 `implicit_success` + 5 `failed_outcome`,生产 2 行全 `implicit_success`,**一行 `negative_feedback` 都没有**)。两种坏法都占齐了,所以 Step 3 的 `SIGNAL_OPTIONS` 不是「修」而是**换掉四个、补上两个**,en / zh-CN 两个 locale 的标签键要跟着**同步增删**(Step 4)。

三处漂移逐一对应(标题说的「三处」就是这三处):

1. `CandidatesPanel.tsx:141` 发 `source: "promoted_candidate"` → Step 3 改成 `promoteSourceOf(signal)`。
2. `api/curation.ts:20-26` `CurationSignal` 五值 → Step 2 改成后端四值;`CandidatesPanel.tsx:56-62` 的 `SIGNAL_OPTIONS` 跟着换(Step 3)。
3. `api/curation.ts:39` `feedback_rating: number | null` → **线上真值是字符串**(实测候选行回 `'down'`)→ Step 2 改成 `"up" | "down" | null`。

**外加一处类型本身就错、但改法是「收窄即可」的**:`api/curation.ts:27`

```ts
export type EvalDatasetSource = "golden" | "promoted_candidate";   // 两个值都对不上后端
```

后端是 `"golden" | "trajectory" | "regression"`。这个类型被 **三处**引用 —— `:103`(`PromoteCandidateBody.source`)、`:130`(`EvalDataset.source`,响应形状)、`:169`(`CreateEvalDatasetBody.source`)。Step 2 只改类型定义那一行就够,`:130` / `:169` **不需要动**:收窄联合类型对它们是源码兼容的,全仓唯一会因此报错的赋值点就是 `CandidatesPanel.tsx:141`,而那正是 Step 3 要改的地方。**但 Step 5 的 `pnpm typecheck` 必须真跑**(裸 `tsc --noEmit` 恒绿不算数),它是这条「只改一行就够」的判据。

- [ ] **Step 1: 失败测试**

`Curation.test.tsx` 的 `candidateRow`(`:103-118`)加 `feedback_run_id: "7c9e6679-7425-40de-944b-e07fc1f90ae7", feedback_comment: "太慢", feedback_changed_at: null`,并把 `feedback_rating: 2` 改成 `"down"`。新增(放在 `:258` 那条之后):

```tsx
  it("promote posts a backend-valid source and the reviewer's expected for a negative candidate", async () => {
    const posted: unknown[] = [];
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [candidateRow], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get",
        respond: () => ({ ...candidateRow, trajectory: null }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1/promote" && m === "post",
        respond: () => datasetRow,
      },
    ]);
    const realAdapter = apiClient.defaults.adapter;
    apiClient.defaults.adapter = (config) => {
      if (config.method === "post") posted.push(JSON.parse(String(config.data)));
      return (realAdapter as (c: typeof config) => Promise<unknown>)(config) as never;
    };
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("research")).toBeInTheDocument());
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(screen.getByTestId("curation-promote-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("curation-promote-btn"));
    await user.type(screen.getByTestId("curation-promote-name-input"), "neg-set");
    await user.type(screen.getByTestId("curation-promote-expected-input"), '{{"answer": "corrected"}}');
    await user.click(screen.getByText(/^Promote$/));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({ name: "neg-set", source: "regression", expected: { answer: "corrected" } });
    expect(["golden", "trajectory", "regression"]).toContain((posted[0] as { source: string }).source);
  });

  it("signal filter only offers backend-known signals", async () => {
    installAdapter([
      { match: (u) => u.startsWith("/v1/curation/candidates"), respond: () => ({ items: [], total: 0, cross_tenant: false }) },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByTestId("curation-signal-filter")).toBeInTheDocument());
    await user.click(screen.getByTestId("curation-signal-filter").querySelector(".ant-select-selector") as HTMLElement);
    const labels = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(labels).toEqual(["All signals", "negative_feedback", "failed_outcome", "positive_feedback", "implicit_success"]);
  });
```

Run: `cd apps/admin-ui && pnpm vitest run src/pages/__tests__/Curation.test.tsx` → 两条红(`curation-promote-expected-input` 不存在;签名列表含 `manual`)。

- [ ] **Step 2: 改 `api/curation.ts`**

```ts
/** 与后端 ``protocol/eval_dataset.py`` 的 ``CurationSignal`` 逐字一致(P-2 修漂移)。 */
export type CurationSignal =
  | "negative_feedback"
  | "failed_outcome"
  | "positive_feedback"
  | "implicit_success";

/** 与后端 ``EvalDatasetSource`` 逐字一致;``golden`` / ``regression`` 必须带非空 ``expected``。 */
export type EvalDatasetSource = "golden" | "trajectory" | "regression";
```

`CurationCandidate`:`feedback_rating: "up" | "down" | null;` 并加三字段 `feedback_run_id: string | null; feedback_comment: string | null; feedback_changed_at: string | null;`。`PromoteCandidateBody` 不动(`source: EvalDatasetSource` 已随类型收窄)。

- [ ] **Step 3: 改 `CandidatesPanel.tsx`**

`SIGNAL_OPTIONS` 改成 `["negative_feedback", "failed_outcome", "positive_feedback", "implicit_success"]`。加纯函数:

```ts
/** P-2 — promote 的 source 按候选信号推导:负例 → regression(需人工写 expected),正例 → trajectory。 */
export function promoteSourceOf(signal: string): EvalDatasetSource {
  return signal === "negative_feedback" || signal === "failed_outcome" ? "regression" : "trajectory";
}
```

`promoteForm` 类型改 `Form.useForm<{ name: string; expected?: string }>()`;`onPromote`:

```ts
    const values = await promoteForm.validateFields();
    const source = promoteSourceOf(selected.signal);
    let expected: Record<string, unknown> | undefined;
    if (values.expected && values.expected.trim()) {
      try {
        expected = JSON.parse(values.expected) as Record<string, unknown>;
      } catch {
        message.error(t("curation.promote_expected_invalid"));
        return;
      }
    }
    setPromoteSubmitting(true);
    try {
      await promoteCandidate(selected.id, { name: values.name, source, ...(expected ? { expected } : {}) });
```

Modal 里 `name` 之后加:

```tsx
          <Form.Item
            name="expected"
            label={t("curation.promote_expected")}
            rules={[
              {
                required: selected !== null && promoteSourceOf(selected.signal) === "regression",
                message: t("curation.promote_expected_required"),
              },
            ]}
          >
            <Input.TextArea data-testid="curation-promote-expected-input" rows={4} placeholder='{"answer": "..."}' />
          </Form.Item>
```

- [ ] **Step 4: i18n(两个 locale 同时加,`curation` object 内)**

en type(`:1621` `dismissed: string;` 之后):`promote_expected: string; promote_expected_required: string; promote_expected_invalid: string;`;en values(`:4890` 之后):

```ts
    promote_expected: "Expected output (JSON)",
    promote_expected_required: "A negative candidate needs a corrected expected output",
    promote_expected_invalid: "Expected output must be valid JSON",
```

zh-CN(`:1709` `dismissed` 之后):

```ts
    promote_expected: "期望输出（JSON）",
    promote_expected_required: "负例候选必须填写修正后的期望输出",
    promote_expected_invalid: "期望输出必须是合法 JSON",
```

`Curation.stories.tsx:76` 的 `feedback_rating: 2` 改成 `"down"`(typecheck 会逼这一步)。

- [ ] **Step 5: 跑确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/pages/__tests__/Curation.test.tsx src/i18n/__tests__/i18n.test.tsx` → 全绿。后端侧对照:`uv run --no-sync pytest services/control-plane/tests/test_curation_api.py::test_promote_candidate_creates_eval_dataset -q`(`source="regression"` + `expected` → 201,这就是回路最后一步走通的证据)。

- [ ] **Step 6: 变异自证**

`promoteSourceOf` 改回返回 `"promoted_candidate" as EvalDatasetSource` → typecheck 红 + 新用例 `toContain` 红;改回 → 绿。`SIGNAL_OPTIONS` 加回 `"manual"` → typecheck 红(不在联合类型里)+ 第二条用例红;改回 → 绿。

- [ ] **Step 7: 提交**

```bash
git add apps/admin-ui/src/api/curation.ts apps/admin-ui/src/pages/curation/CandidatesPanel.tsx apps/admin-ui/src/pages/Curation.stories.tsx apps/admin-ui/src/i18n/locales/en.ts apps/admin-ui/src/i18n/locales/zh-CN.ts apps/admin-ui/src/pages/__tests__/Curation.test.tsx
git commit -m "fix(curation-ui): promote source 按信号推导为 regression/trajectory 并补 expected;signal 枚举与 feedback_rating 类型对齐后端"
```

---
## PR3 —— 控制台可见

### Task 12: 控制台 `GET /v1/sessions/{thread_id}/feedback`(`console_only` + `session:read`,评论全员可见)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/feedback.py:48-104`(同一 router 加 GET)
- Test: `services/control-plane/tests/test_feedback_api.py`(新增三条)

**Interfaces:**
- Consumes: `FeedbackStore.list_for_thread_scoped`(Task 2);`ThreadMetaStore.get(thread_id, tenant_id=)`;`ensure_single_tenant_scope` / `applied_scope` / `cross_tenant_query_enabled`(`control_plane/tenant_scope`,用法照 `api/curation.py:268-278`)。
- Produces: `GET /v1/sessions/{thread_id}/feedback?tenant_id=<uuid 可选>` → 200 裸 JSON `{"items": [{"id", "run_id", "rating", "comment", "item_id", "source", "actor_id", "created_at", "updated_at"}]}`(`id` 降序);thread 不在目标租户 → 404 `{"detail": "session not found"}`;API key → 403(`console_only`,由 `test_console_lockdown.py` 的 `/v1/sessions` 前缀审计自动覆盖);viewer 角色可读且能看到 `comment` 原文。

- [ ] **Step 1: 失败测试(`test_feedback_api.py` 追加)**

```python
from tests.auth_fixtures import make_test_jwt as _jwt


async def _seed_thread(client: AsyncClient, thread_id: UUID) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=_DEFAULT_TENANT, created_by="seed",
        user_id=uuid4(), agent_name="alpha", agent_version="1.0.0",
    )


@pytest.mark.asyncio
async def test_get_feedback_lists_every_rating_on_the_thread_newest_first(
    client: AsyncClient, feedback_store: InMemoryFeedbackStore
) -> None:
    thread_id, run_a, run_b = uuid4(), uuid4(), uuid4()
    await _seed_thread(client, thread_id)
    await feedback_store.upsert(
        FeedbackRecord(tenant_id=_DEFAULT_TENANT, thread_id=thread_id, run_id=run_a,
                       rating="down", comment="太慢", item_id="p1", source="external", actor_id="ext-1")
    )
    await feedback_store.upsert(
        FeedbackRecord(tenant_id=_DEFAULT_TENANT, thread_id=thread_id, run_id=run_b,
                       rating="up", source="console", actor_id="emp-1")
    )
    resp = await client.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["run_id"] for i in items] == [str(run_b), str(run_a)]
    assert items[1] == {
        "id": items[1]["id"], "run_id": str(run_a), "rating": "down", "comment": "太慢",
        "item_id": "p1", "source": "external", "actor_id": "ext-1",
        "created_at": items[1]["created_at"], "updated_at": None,
    }


@pytest.mark.asyncio
async def test_get_feedback_is_readable_by_a_viewer_including_comments(
    feedback_store: InMemoryFeedbackStore, audit_store: InMemoryAuditLogStore
) -> None:
    """用户拍板:评论原文全员可见 —— viewer 也能读到 comment,与会话原文的 operator+ 门槛有意不一致。"""
    settings = Settings(env="dev", auth_mode="dev", rate_limit_burst=10_000,
                        rate_limit_per_second=10_000.0, oidc_issuer=TEST_ISSUER,
                        oidc_audience=[TEST_AUDIENCE])
    app = create_app(settings=settings, audit_logger=build_default_audit_logger(audit_store),
                     feedback_repo=feedback_store, jwt_verifier=build_test_jwt_verifier())
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=_DEFAULT_TENANT, created_by="seed",
        user_id=uuid4(), agent_name="alpha", agent_version="1.0.0",
    )
    await feedback_store.upsert(
        FeedbackRecord(tenant_id=_DEFAULT_TENANT, thread_id=thread_id, run_id=uuid4(),
                       rating="down", comment="VISIBLE-TO-VIEWER", source="external", actor_id="ext-1")
    )
    headers = {"Authorization": f"Bearer {_jwt(tenant_id=_DEFAULT_TENANT, subject='viewer-1', roles=('viewer',))}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test", headers=headers) as viewer:
        resp = await viewer.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["comment"] == "VISIBLE-TO-VIEWER"


@pytest.mark.asyncio
async def test_get_feedback_404s_for_a_thread_of_another_tenant(
    client: AsyncClient, feedback_store: InMemoryFeedbackStore
) -> None:
    other_tenant, thread_id = uuid4(), uuid4()
    app = client._transport.app  # type: ignore[attr-defined]
    await app.state.thread_meta_repo.create(
        thread_id=thread_id, tenant_id=other_tenant, created_by="seed",
        user_id=uuid4(), agent_name="alpha", agent_version="1.0.0",
    )
    await feedback_store.upsert(
        FeedbackRecord(tenant_id=other_tenant, thread_id=thread_id, run_id=uuid4(),
                       rating="down", comment="LEAK?", source="external", actor_id="ext-9")
    )
    resp = await client.get(f"/v1/sessions/{thread_id}/feedback")
    assert resp.status_code == 404, resp.text
    assert "LEAK?" not in resp.text
```

(`from uuid import UUID, uuid4`、`from expert_work.persistence.feedback_store import FeedbackRecord, InMemoryFeedbackStore`、`from httpx import ASGITransport` 按需补 import。)

Run: `uv run --no-sync pytest services/control-plane/tests/test_feedback_api.py -q` → 三条红(GET 405 / 404)。

- [ ] **Step 2: 实现 GET**

`api/feedback.py` 在 `submit_feedback` 之后:

```python
    @router.get(
        "/{thread_id}/feedback",
        response_model=None,
        dependencies=[Depends(console_only()), Depends(require("session", "read"))],
    )
    async def list_feedback(
        thread_id: UUID,
        request: Request,
        store: Annotated[FeedbackStore, Depends(_get_feedback_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """这段会话的全部反馈(含评论原文)。

        评论原文对全员可见(``session:read``),与会话原文的 operator+ 门槛**有意
        不一致**(2026-09-09 用户拍板:评论是用户对 Agent 的评价,不是会话内容)。
        跨租户 drill-in 照 ``api/curation.py`` 的候选详情:concrete ``tenant_id``
        可以,``"*"`` 没有意义。
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/feedback",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=scope.tenant_id)
            if meta is None:
                raise HTTPException(status_code=404, detail="session not found")
            rows = await store.list_for_thread_scoped(
                tenant_id=scope.tenant_id, thread_id=thread_id
            )
        return JSONResponse(
            content={
                "items": [
                    {
                        "id": r.id,
                        "run_id": str(r.run_id) if r.run_id is not None else None,
                        "rating": r.rating,
                        "comment": r.comment,
                        "item_id": r.item_id,
                        "source": r.source,
                        "actor_id": r.actor_id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in rows
                ]
            }
        )
```

新 import:`HTTPException, Query` 自 fastapi;`from control_plane.api._authz import console_only, require, require_key_scope`;`from control_plane.tenant_scope import applied_scope, cross_tenant_query_enabled, ensure_single_tenant_scope`;`from expert_work.persistence.thread_meta import ThreadMetaStore`;`def _get_thread_repo(request) -> ThreadMetaStore: return request.app.state.thread_meta_repo`。

- [ ] **Step 3: 跑确认绿(含 lockdown 前缀审计)**

Run: `uv run --no-sync pytest services/control-plane/tests/test_feedback_api.py services/control-plane/tests/test_console_lockdown.py -q` → 全绿(`test_every_console_route_carries_the_lockdown_dependency` 按 `/v1/sessions` 前缀自动纳入新 GET)。

- [ ] **Step 4: 变异自证**

把 `dependencies=[Depends(console_only()), ...]` 里的 `console_only()` 去掉 → `test_console_lockdown.py` 的前缀审计红;改回 → 绿。`meta is None → 404` 分支删掉 → `test_get_feedback_404s_for_a_thread_of_another_tenant` 红(200 且空 items;但 `LEAK?` 仍不出现,因为 `list_for_thread_scoped` 带租户谓词 —— 两道门各挡一层);改回 → 绿。`require("session", "read")` 改 `"write"` → viewer 用例 403 红;改回 → 绿。

- [ ] **Step 5: lint + 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane/src/control_plane/api/feedback.py services/control-plane/tests/test_feedback_api.py
git commit -m "feat(console): GET /v1/sessions/{thread_id}/feedback —— 会话全部反馈含评论原文,session:read 全员可见"
```

---

### Task 13: `GET /v1/conversations?has_down_rated=true`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/conversations.py:58-75`(加 `_get_feedback_store`)、`:188-215`(签名加参数)、`:243-258`(`narrowed_ids` 组合)
- Test: `services/control-plane/tests/test_conversations_api.py:347-358`(照 `has_error` 用例加两条)

**Interfaces:**
- Consumes: `FeedbackStore.down_rated_thread_ids(tenant_id, limit=500)`(Task 2;`CrossTenant` 时传 `None`,store 自己 `SET LOCAL ROLE audit_reader`)。
- Produces: 查询参数 `has_down_rated: bool = False`;与 `has_error` / `has_pending` / `since` 交集组合;≥500 时 `set_capped=True` → `X-Limit-Capped` 头(与既有筛选同款)。

- [ ] **Step 1: 失败测试**

```python
@pytest.mark.asyncio
async def test_list_filters_by_has_down_rated(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    """has_down_rated 只剩有 ≥1 条 👎 的会话;👍 不算。"""
    client, ids = client_and_threads
    store = client._transport.app.state.feedback_store  # type: ignore[attr-defined]
    await store.upsert(
        FeedbackRecord(tenant_id=_TENANT, thread_id=ids["other_user"], run_id=uuid4(),
                       rating="down", source="external", actor_id="ext-b")
    )
    await store.upsert(
        FeedbackRecord(tenant_id=_TENANT, thread_id=ids["convo"], run_id=uuid4(),
                       rating="up", source="console", actor_id="emp")
    )
    resp = await client.get("/v1/conversations", params={"has_down_rated": "true"})
    assert resp.status_code == 200
    assert {i["thread_id"] for i in resp.json()["data"]["items"]} == {str(ids["other_user"])}


@pytest.mark.asyncio
async def test_has_down_rated_composes_with_has_error(
    client_and_threads: tuple[AsyncClient, dict[str, UUID]],
) -> None:
    client, ids = client_and_threads
    store = client._transport.app.state.feedback_store  # type: ignore[attr-defined]
    await store.upsert(
        FeedbackRecord(tenant_id=_TENANT, thread_id=ids["other_user"], run_id=uuid4(),
                       rating="down", source="external", actor_id="ext-b")
    )
    # other_user 有 👎 但没有失败 run;convo 有失败 run 但没有 👎 → 交集为空。
    resp = await client.get("/v1/conversations", params={"has_down_rated": "true", "has_error": "true"})
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []
```

(加 `from expert_work.persistence.feedback_store import FeedbackRecord`。)
Run: `uv run --no-sync pytest services/control-plane/tests/test_conversations_api.py -q` → 第一条红(未知参数被忽略,返回三条)。

- [ ] **Step 2: 实现**

`_get_feedback_store` 照 `:58-59` 加;签名(`:206` `has_pending` 之后)加:

```python
        # P-2 — only conversations with ≥1 👎 (any actor, any run). Composes
        # with has_error / has_pending / since the same way (intersection).
        has_down_rated: Annotated[bool, Query()] = False,
```

并加依赖 `feedback: Annotated[FeedbackStore, Depends(_get_feedback_store)]`。`:243-258` 改为:

```python
            narrowed_ids: set[UUID] | None = None
            if has_error or has_pending or since is not None:
                agg_scope = None if isinstance(scope, CrossTenant) else scope.tenant_id
                only_filters: list[Literal["failed", "pending"] | None] = []
                if has_error:
                    only_filters.append("failed")
                if has_pending:
                    only_filters.append("pending")
                if not only_filters:
                    only_filters.append(None)  # bare ``since`` window
                id_sets = [
                    await runs.thread_ids_with_runs(tenant_id=agg_scope, since=since, only=f)
                    for f in only_filters
                ]
                set_capped = any(len(s) >= 500 for s in id_sets)
                narrowed_ids = set.intersection(*id_sets)
            if has_down_rated:
                fb_scope = None if isinstance(scope, CrossTenant) else scope.tenant_id
                down_ids = await feedback.down_rated_thread_ids(tenant_id=fb_scope)
                set_capped = set_capped or len(down_ids) >= 500
                narrowed_ids = down_ids if narrowed_ids is None else narrowed_ids & down_ids
```

import `from expert_work.persistence.feedback_store import FeedbackStore`。

- [ ] **Step 3: 跑确认绿**

Run: `uv run --no-sync pytest services/control-plane/tests/test_conversations_api.py -q` → 全绿。

- [ ] **Step 4: 变异自证**

`narrowed_ids & down_ids` 改成 `narrowed_ids | down_ids` → 组合用例红(返回两条);改回 → 绿。`down_rated_thread_ids` 内存版把 `if r.rating != "down": continue` 删掉 → 第一条用例红(`convo` 的 👍 也进来);改回 → 绿。

- [ ] **Step 5: lint + 提交**

```bash
uv run --no-sync ruff check services/control-plane && uv run --no-sync ruff format services/control-plane
git add services/control-plane/src/control_plane/api/conversations.py services/control-plane/tests/test_conversations_api.py
git commit -m "feat(console): GET /v1/conversations 加 has_down_rated 筛选(与 has_error/has_pending/since 交集)"
```

---
### Task 14: 对话列表「被点踩」勾选框

**Files:**
- Modify: `apps/admin-ui/src/api/conversations.ts:81-103`(`ListConversationsParams` 加 `hasDownRated`)、`:107-142`(`listConversations` 透传 `has_down_rated`)
- Modify: `apps/admin-ui/src/pages/ConversationsList.tsx:93-94`(state)、`:163-175`(调用)、`:195-196`(依赖数组)、`:420-426`(新 Checkbox)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts:380`(type)、`:3531`(value);`zh-CN.ts:390`
- Test: `apps/admin-ui/src/pages/__tests__/ConversationsList.test.tsx:235-247`(照写一条)

**Interfaces:**
- Consumes: Task 13 的查询参数。
- Produces: URL 参数 `?down=1` ↔ `hasDownRated: true`;`data-testid="conversations-down-rated-only"`;i18n `conversations_page.filter_down_rated_only`("Rated bad" / "被点踩")。改页面前核对:`rg -n "conversations-" apps/admin-ui/e2e/` → `conversations.spec.ts` 只用 `login-*` testid,无需改 Playwright。

- [ ] **Step 1: 失败测试(`ConversationsList.test.tsx:247` 之后)**

```tsx
  it("rated-bad checkbox flows into the hasDownRated param", async () => {
    const user = userEvent.setup();
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    await user.click(screen.getByTestId("conversations-down-rated-only"));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenCalledWith(
        expect.objectContaining({ hasDownRated: true, offset: 0 }),
      ),
    );
  });
```

Run: `cd apps/admin-ui && pnpm vitest run src/pages/__tests__/ConversationsList.test.tsx` → 红(testid 不存在)。

- [ ] **Step 2: 实现**

`conversations.ts`:`ListConversationsParams` 加 `/** Only conversations with ≥1 👎 (P-2). */ hasDownRated?: boolean;`;解构与 query 各加一行 `hasDownRated,` / `has_down_rated: hasDownRated ? true : undefined,`。
`ConversationsList.tsx`:`:94` 之后 `const downRatedOnly = searchParams.get("down") === "1";`;`:168` 之后 `hasDownRated: downRatedOnly,`;`:196` 之后依赖 `downRatedOnly,`;`:426` `pendingOnly` Checkbox 之后:

```tsx
            <Checkbox
              checked={downRatedOnly}
              onChange={(e) => setParam("down", e.target.checked ? "1" : undefined)}
              data-testid="conversations-down-rated-only"
            >
              {t("conversations_page.filter_down_rated_only")}
            </Checkbox>
```

i18n:en type `filter_down_rated_only: string;`(`:380` 后),value `filter_down_rated_only: "Rated bad",`(`:3531` 后);zh-CN `filter_down_rated_only: "被点踩",`(`:390` 后)。

- [ ] **Step 3: 跑确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/pages/__tests__/ConversationsList.test.tsx src/i18n/__tests__/i18n.test.tsx` → 全绿。

- [ ] **Step 4: 变异自证**

`conversations.ts` 里 `has_down_rated: hasDownRated ? true : undefined` 改成常量 `undefined` → 用例不红(mock 在 SDK 层之上)。所以这一层用 `apps/admin-ui/src/api/__tests__/conversations.test.ts`(若不存在则新建,照同目录其它 SDK 测试用 `apiClient.defaults.adapter` 捕获 `config.params`)加一条断言 `params.has_down_rated === true`;该断言在上面那刀下红,改回 → 绿。页面层:`ConversationsList.tsx` 的 `hasDownRated: downRatedOnly` 删掉 → 页面用例红;改回 → 绿。

- [ ] **Step 5: 提交**

```bash
git add apps/admin-ui/src/api/conversations.ts apps/admin-ui/src/api/__tests__/conversations.test.ts apps/admin-ui/src/pages/ConversationsList.tsx apps/admin-ui/src/pages/__tests__/ConversationsList.test.tsx apps/admin-ui/src/i18n/locales/en.ts apps/admin-ui/src/i18n/locales/zh-CN.ts
git commit -m "feat(console-ui): 对话列表加「被点踩」筛选(?down=1 ↔ has_down_rated)"
```

---

### Task 15: 对话详情页每轮脚部显示 👍/👎 与评论

**Files:**
- Create: `apps/admin-ui/src/components/console/FeedbackSummary.tsx`
- Modify: `apps/admin-ui/src/api/sessions.ts:135-148`(之后加 `getSessionFeedback`)
- Modify: `apps/admin-ui/src/components/console/TurnFooter.tsx:29-46`(prop)、`:150-159`(渲染)
- Modify: `apps/admin-ui/src/components/console/TurnBlock.tsx:36-84`(prop)、`:238-250`(透传)
- Modify: `apps/admin-ui/src/components/console/Transcript.tsx:33-84`(prop)、`:213-241` / `:246-`(透传)
- Modify: `apps/admin-ui/src/pages/ConversationDetail.tsx:44`(import)、`:170-175`(并行取反馈)、`:688-712`(`feedbackOf`)
- Modify: i18n `console` object:en type `:1318` 后、values `:4575` 后;zh-CN `:1399` 后
- Test: `apps/admin-ui/src/components/console/__tests__/TurnFooter.test.tsx`、`src/pages/__tests__/ConversationDetail.test.tsx:561-585`(既有用例扩断言)

**Interfaces:**
- Consumes: Task 12 的 GET(裸 JSON `{items}`)。
- Produces:
  - `export interface SessionFeedbackItem { id: number; run_id: string | null; rating: "up" | "down"; comment: string | null; item_id: string | null; source: "console" | "external"; actor_id: string; created_at: string | null; updated_at: string | null }`;`getSessionFeedback(threadId, tenantId?) => Promise<SessionFeedbackItem[]>`。
  - `FeedbackSummary({ items })`:每条渲染 `👍/👎` 图标(lucide `ThumbsUp`/`ThumbsDown`)+ `comment`(有则显示,`Typography.Text ellipsis`)+ `source` 标签;`data-testid="console-turn-feedback-summary"`,每条 `data-testid="console-turn-feedback-item"`。
  - `TurnFooter` 新 prop `feedback?: readonly SessionFeedbackItem[]`(非空即渲染 `FeedbackSummary`,与 `FeedbackBar` 互不排斥:对话页只读没有 bar,只有 summary);`TurnBlock` / `Transcript` 新 prop `feedbackOf?: (turn: ConsoleTurn) => readonly SessionFeedbackItem[] | undefined`(照 `planOf` 的 opt-in 形状,调试台不传 → 零变化)。
  - i18n `console.feedback_up_label` "Rated good"/"点赞"、`console.feedback_down_label` "Rated bad"/"点踩"、`console.feedback_source_external` "end user"/"终端用户"、`console.feedback_source_console` "employee"/"员工"。

- [ ] **Step 1: 失败测试**

`TurnFooter.test.tsx` 新增:

```tsx
  it("renders the turn's recorded feedback (rating + comment + source) when given", () => {
    render(
      <MemoryRouter>
        <TurnFooter
          turn={{ ...makeConsoleTurn({ status: "done" }), runId: "run-1" }}
          threadId="th-1"
          summary={EMPTY_SUMMARY}
          costCny={null}
          readOnly
          isTenantSwitched={false}
          onExport={vi.fn()}
          exporting={false}
          onInspect={vi.fn()}
          feedback={[
            { id: 1, run_id: "run-1", rating: "down", comment: "答非所问", item_id: "p2", source: "external", actor_id: "u", created_at: null, updated_at: null },
          ]}
        />
      </MemoryRouter>,
    );
    const summary = screen.getByTestId("console-turn-feedback-summary");
    expect(summary).toHaveTextContent("点踩");
    expect(summary).toHaveTextContent("答非所问");
    expect(summary).toHaveTextContent("终端用户");
    expect(screen.queryByTestId("playground-turn-feedback")).not.toBeInTheDocument(); // readOnly:只有 summary
  });
```

`ConversationDetail.test.tsx:561` 那条用例:在 `vi.spyOn(runsSdk, "listThreadRuns")` 之后加 `vi.spyOn(sessionsSdk, "getSessionFeedback").mockResolvedValue([{ id: 1, run_id: RUN_2, rating: "down", comment: "太慢", item_id: null, source: "external", actor_id: "u", created_at: null, updated_at: null }]);`,断言块末尾加:

```tsx
      const summaries = await screen.findAllByTestId("console-turn-feedback-summary");
      expect(summaries).toHaveLength(1);
      expect(summaries[0]).toHaveTextContent("太慢");
```

其余用 `getSessionMessages` 的既有用例,在 `beforeEach`(`:293-303`)加默认 `vi.spyOn(sessionsSdk, "getSessionFeedback").mockResolvedValue([]);`(`getSessionFeedback` 不存在时 spyOn 抛错 → 这就是红)。

Run: `cd apps/admin-ui && pnpm vitest run src/components/console/__tests__/TurnFooter.test.tsx src/pages/__tests__/ConversationDetail.test.tsx` → 红。

- [ ] **Step 2: SDK + 组件**

`sessions.ts`(`:148` 之后):

```ts
/** P-2 — one 👍/👎 recorded on a run of this thread (console GET, bare JSON). */
export interface SessionFeedbackItem {
  id: number;
  run_id: string | null;
  rating: "up" | "down";
  comment: string | null;
  item_id: string | null;
  source: "console" | "external";
  actor_id: string;
  created_at: string | null;
  updated_at: string | null;
}

/** GET /v1/sessions/{threadId}/feedback — every rating on the thread, newest first.
 *  Bare ``{items}`` (no envelope), same convention as the POST. */
export async function getSessionFeedback(
  threadId: string,
  tenantId?: string,
): Promise<SessionFeedbackItem[]> {
  const response = await apiClient.get<{ items: SessionFeedbackItem[] }>(
    `/v1/sessions/${threadId}/feedback`,
    { params: tenantId ? { tenant_id: tenantId } : undefined },
  );
  return response.data.items;
}
```

`FeedbackSummary.tsx`:

```tsx
/**
 * FeedbackSummary — read-only rendering of the 👍/👎 recorded on one turn
 * (P-2). The conversation page is read-only, so this is the only feedback
 * affordance there; the playground keeps ``FeedbackBar`` for writing.
 */
import { Tag, Typography } from "antd";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { SessionFeedbackItem } from "../../api/sessions";

const { Text } = Typography;

export function FeedbackSummary({ items }: { items: readonly SessionFeedbackItem[] }) {
  const { t } = useTranslation();
  return (
    <span
      style={{ display: "inline-flex", gap: 8, alignItems: "center" }}
      data-testid="console-turn-feedback-summary"
    >
      {items.map((item) => (
        <span
          key={item.id}
          style={{ display: "inline-flex", gap: 4, alignItems: "center" }}
          data-testid="console-turn-feedback-item"
        >
          {item.rating === "up" ? (
            <ThumbsUp size={13} strokeWidth={1.75} color="var(--ew-status-success, #52c41a)" />
          ) : (
            <ThumbsDown size={13} strokeWidth={1.75} color="var(--ew-status-error, #f5222d)" />
          )}
          <Text style={{ fontSize: 12 }}>
            {t(item.rating === "up" ? "console.feedback_up_label" : "console.feedback_down_label")}
          </Text>
          {item.comment && (
            <Text type="secondary" style={{ fontSize: 12, maxWidth: 320 }} ellipsis={{ tooltip: item.comment }}>
              “{item.comment}”
            </Text>
          )}
          <Tag bordered={false} style={{ fontSize: 11, marginInlineEnd: 0 }}>
            {t(item.source === "external" ? "console.feedback_source_external" : "console.feedback_source_console")}
          </Tag>
        </span>
      ))}
    </span>
  );
}
```

`TurnFooter.tsx`:props 加 `/** P-2 — recorded ratings for this turn (conversation page). Omitted / empty → nothing renders. */ feedback?: readonly SessionFeedbackItem[];`;在 `<span className="ew-turn-footer__acts">` 开头(`:150` 之后)加 `{feedback && feedback.length > 0 && <FeedbackSummary items={feedback} />}`。
`TurnBlock.tsx`:props 加 `feedbackOf?: (turn: ConsoleTurn) => readonly SessionFeedbackItem[] | undefined;`,解构后传 `feedback={feedbackOf?.(turn)}` 给 `TurnFooter`。`Transcript.tsx`:同名 prop,两处 `<TurnBlock` 都透传 `feedbackOf={feedbackOf}`。

- [ ] **Step 3: 页面接线**

`ConversationDetail.tsx`:import 加 `getSessionFeedback, type SessionFeedbackItem`;state `const [feedbackByRun, setFeedbackByRun] = useState<ReadonlyMap<string, SessionFeedbackItem[]>>(new Map());`;`:170-175` 的 try 块改成并行、各自 best-effort:

```ts
    try {
      const msgs = await getSessionMessages(threadId, loaded?.tenant_id);
      setMessages(Array.isArray(msgs) ? msgs : null);
    } catch {
      setMessages(null);
    }
    // P-2 — 反馈同样 best-effort:读不到就没有轮脚标记,不影响页面。
    try {
      const items = await getSessionFeedback(threadId, loaded?.tenant_id);
      const grouped = new Map<string, SessionFeedbackItem[]>();
      for (const item of items) {
        if (item.run_id === null) continue;
        grouped.set(item.run_id, [...(grouped.get(item.run_id) ?? []), item]);
      }
      setFeedbackByRun(grouped);
    } catch {
      setFeedbackByRun(new Map());
    }
```

`feedbackOf` 与 `planOf` 同款 `useCallback`:`(turn: ConsoleTurn) => (turn.runId ? feedbackByRun.get(turn.runId) : undefined)`,`<Transcript … feedbackOf={feedbackOf} />`。

- [ ] **Step 4: i18n**

en `console` type(`:1318` 后):`feedback_up_label: string; feedback_down_label: string; feedback_source_external: string; feedback_source_console: string;`;values(`:4575` 后):`feedback_up_label: "Rated good", feedback_down_label: "Rated bad", feedback_source_external: "end user", feedback_source_console: "employee",`;zh-CN(`:1399` 后):`feedback_up_label: "点赞", feedback_down_label: "点踩", feedback_source_external: "终端用户", feedback_source_console: "员工",`。

- [ ] **Step 5: 跑确认绿**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/components/console src/pages/__tests__/ConversationDetail.test.tsx src/pages/__tests__/PlaygroundTab.test.tsx src/i18n/__tests__/i18n.test.tsx` → 全绿(PlaygroundTab 不传 `feedbackOf`,零变化)。

- [ ] **Step 6: 变异自证**

`TurnFooter.tsx` 把 `{feedback && feedback.length > 0 && <FeedbackSummary …/>}` 删掉 → 两条新用例红;改回 → 绿。`ConversationDetail.tsx` 里 `grouped.set(...)` 那行删掉 → 详情页用例 `findAllByTestId` 超时红;改回 → 绿。

- [ ] **Step 7: 提交**

```bash
git add apps/admin-ui/src/api/sessions.ts apps/admin-ui/src/components/console/FeedbackSummary.tsx apps/admin-ui/src/components/console/TurnFooter.tsx apps/admin-ui/src/components/console/TurnBlock.tsx apps/admin-ui/src/components/console/Transcript.tsx apps/admin-ui/src/pages/ConversationDetail.tsx apps/admin-ui/src/components/console/__tests__/TurnFooter.test.tsx apps/admin-ui/src/pages/__tests__/ConversationDetail.test.tsx apps/admin-ui/src/i18n/locales/en.ts apps/admin-ui/src/i18n/locales/zh-CN.ts
git commit -m "feat(console-ui): 对话详情每轮脚部显示已记录的 👍/👎、评论与来源"
```

---

### Task 16: 候选列表 / 详情显示「被踩的轮 + 原话 + 是否后改票」

**Files:**
- Modify: `apps/admin-ui/src/pages/curation/CandidatesPanel.tsx:155-201`(columns)、`:320-350`(drawer)
- Modify: i18n `curation` object:en type `:1591-1622`、values `:4857-4891`;zh-CN `:1677-1710`
- Test: `apps/admin-ui/src/pages/__tests__/Curation.test.tsx`(新增一条)

**Interfaces:**
- Consumes: Task 8 的 `_candidate_dict` 三键、Task 11 的前端类型。
- Produces: 表格新列「用户原话」(`feedback_comment`,ellipsis + Tooltip;`feedback_changed_at` 非空时同格加 `Tag` "后改为 👍");详情 drawer 加两行「被踩的轮」(`feedback_run_id` 短 id + 完整值 Tooltip)与「用户原话」。i18n:`col_feedback_comment` "User's words"/"用户原话"、`detail_feedback_run` "Rated-down run"/"被踩的轮"、`detail_feedback_comment` 同上、`feedback_changed_tag` "later changed to 👍"/"后改为 👍"。`data-testid="curation-feedback-changed-tag"`、`"curation-detail-feedback-run"`。

- [ ] **Step 1: 失败测试**

```tsx
  it("shows the rated-down run, the user's words and the changed-vote tag", async () => {
    const changed = { ...candidateRow, feedback_changed_at: "2026-09-09T12:00:00Z" };
    installAdapter([
      { match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"), respond: () => ({ items: [changed], total: 1, cross_tenant: false }) },
      { match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get", respond: () => ({ ...changed, trajectory: null }) },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("太慢")).toBeInTheDocument());
    expect(screen.getByTestId("curation-feedback-changed-tag")).toBeInTheDocument();
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(screen.getByTestId("curation-detail-feedback-run")).toHaveTextContent("7c9e6679"));
  });
```

Run: `cd apps/admin-ui && pnpm vitest run src/pages/__tests__/Curation.test.tsx` → 红。

- [ ] **Step 2: 实现**

columns(`:191-201` `col_outcome` 之后)加:

```tsx
    {
      title: t("curation.col_feedback_comment"),
      key: "feedback_comment",
      ellipsis: true,
      render: (_: unknown, record) => (
        <Space size={4}>
          {record.feedback_comment && (
            <Tooltip title={record.feedback_comment}>
              <Text style={{ fontSize: 12 }}>{record.feedback_comment}</Text>
            </Tooltip>
          )}
          {record.feedback_changed_at && (
            <Tag color="gold" data-testid="curation-feedback-changed-tag">{t("curation.feedback_changed_tag")}</Tag>
          )}
        </Space>
      ),
    },
```

drawer(`:328` `detail_outcome` 块之后)加:

```tsx
            {selected.feedback_run_id && (
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>{t("curation.detail_feedback_run")}</Text>
                <div data-testid="curation-detail-feedback-run">
                  <Tooltip title={selected.feedback_run_id}><code>{selected.feedback_run_id.slice(0, 8)}</code></Tooltip>
                  {selected.feedback_changed_at && (
                    <Tag color="gold" style={{ marginInlineStart: 8 }}>{t("curation.feedback_changed_tag")}</Tag>
                  )}
                </div>
              </div>
            )}
            {selected.feedback_comment && (
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>{t("curation.detail_feedback_comment")}</Text>
                <div>{selected.feedback_comment}</div>
              </div>
            )}
```

i18n 四键两 locale 同加(值见 Interfaces)。

- [ ] **Step 3: 跑确认绿 + 变异**

Run: `cd apps/admin-ui && pnpm typecheck && pnpm vitest run src/pages/__tests__/Curation.test.tsx src/i18n/__tests__/i18n.test.tsx` → 绿。变异:列 render 里 `record.feedback_changed_at &&` 改成 `false &&` → 用例红;改回 → 绿。e2e 核对:`rg -n "curation" apps/admin-ui/e2e/` → `governance.spec.ts:33-38` 只跑页面渲染 + axe,`fixtures.ts:211` 的候选 mock 缺新键也只是不渲染新列,无需改。

- [ ] **Step 4: 提交**

```bash
git add apps/admin-ui/src/pages/curation/CandidatesPanel.tsx apps/admin-ui/src/pages/__tests__/Curation.test.tsx apps/admin-ui/src/i18n/locales/en.ts apps/admin-ui/src/i18n/locales/zh-CN.ts
git commit -m "feat(curation-ui): 候选行显示被踩的轮、用户原话与改票标记"
```

---
### Task 17: 真栈验收(每个 PR 发测试环境后各跑一段;只用探针 user + 金丝雀 agent)

**Files:** 无代码改动;结果贴进各 PR 描述。

**Interfaces:**
- Consumes: 测试环境 `https://expert-work-test.deepaihealth.com`;金丝雀 agent `release-canary`(user `canary:release`)或对接测试 agent 下的探针 user `pc:proj_8f52dd4458d24ff4a3cc71af94a534c1:emp:probe-delegation`;一把 `write` 档 API key(由用户从控制台发,**只经 stdin 喂进脚本**);控制台登录态(需要用户亲自登一次 —— Task 0 Step 2 当时**没能**拿到,scratchpad 里没有存量 storage 文件可复用,本 Task 开跑前要先跟用户要)。**永不碰 `ai-health-plan` / `sop2-designer`。**
- Produces: spec §8 三段验收的真栈证据。

**前置事实(Task 0 §7-2 实测,09-09):`feedback` 是一张空表,没有任何存量数据可以拿来演示。** 生产与测试 **都是 0 行**,且 `pg_stat_user_tables.n_tup_ins = 0` —— **建表至今没写进过任何一行**(原因见 Task 5 的「前置事实」:控制台写路径今天只在调试台可达)。连带地:`curation_candidate` 里**一行 `negative_feedback` 都没有**(测试 167 行全是 `implicit_success` / `failed_outcome`,生产 2 行全 `implicit_success`),`eval_dataset` 两个环境**都是 0 行**。

对本 Task 的三个直接后果:

1. **每一段验收都必须自己先造数据,顺序不能颠倒。** 下面 Step 1 起 run → Step 2 用 **PR1 的对外端点**打 👎,这就是全平台第一条 `feedback` 行。别指望「翻到一条现成的有 👎 的会话」—— 翻不到。
2. **PR3 的 `has_down_rated` 筛选(Step 4-1)天然只有你自己造的那一条**。「勾上只剩有 👎 的会话」要拿「勾上 = 1 条(你造的那条)、勾掉 = N 条」来判,不是拿「筛出一堆」来判。
3. **Step 3-4 的 promote 是全平台第一条 `eval_dataset` 行**。验收时 `GET /v1/eval-datasets?agent_name=release-canary` 从 0 行变 1 行 —— 这个「从 0 到 1」本身就是 spec §8 PR2「promote 一路走通(今天走不通)」最干净的证据形态,记得把两次查询都贴进 PR 描述。

- [ ] **Step 1: 起一轮拿 `run_id`(key 从 stdin 读,不落文件、不上 argv)**

```bash
python3 - <<'EOF'
import json, sys, urllib.request
key = sys.stdin.readline().strip()  # 用户把 key 粘进来,回车
base = "https://expert-work-test.deepaihealth.com"
body = json.dumps({"user_id": "canary:release", "input": "用一句话介绍你自己", "mode": "queue"}).encode()
req = urllib.request.Request(f"{base}/v1/agents/release-canary/runs", data=body, method="POST",
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
with urllib.request.urlopen(req) as r:
    data = json.load(r)["data"]
print("run_id:", data["run_id"], "session_id:", data["thread_id"])
EOF
```

等 `GET .../runs?user_id=canary:release` 里该 run `status` 终态(照 docs 10.2 的轮询)。

- [ ] **Step 2: PR1 验收(spec §8 PR1)**

同样的 stdin 读 key 写法,依次:
1. `POST .../runs/{run_id}/feedback` body `{"user_id":"canary:release","rating":"down","comment":"探针:太慢","item_id":"p1"}` → 200,`updated:false`。
2. 再 POST `rating:"up"` → 200,`updated:true`。
3. `GET .../sessions/{session_id}/items?user_id=canary:release` → `runs[0].feedback == {"rating":"up","comment":null,"item_id":null}`;`GET .../messages?user_id=canary:release` 每条 `feedback` 同值。
4. 换 `user_id:"canary:someone-else"` POST → 404 `RUN_NOT_FOUND` 信封;换 `agent_code` 为一个真实存在但不是这个 run 的 agent(用 `/v1/agent-catalog` 里另一个**测试** agent;没有就跳过并记录)→ 404。
5. body `comment` 带 `\x00`(Python 里 `"探针\x00"`)→ 422 `INVALID_REQUEST` 信封,`detail` 键不存在。
6. 测试 pod 内只读核对(照 Task 0 Step 3 的 heredoc,kubeconfig 换 test):`SELECT count(*) FROM feedback WHERE run_id = '<run_id>'` = 1,`source='external'`,`updated_at IS NOT NULL`。
7. purge 归零不在真栈做(会删探针用户);以 Task 6 的单测为准。

- [ ] **Step 3: PR2 验收(spec §8 PR2)**

1. 新起一轮(Step 1),等 run 终态后 **立即**(<300s 内)POST `rating:"down"`、`comment:"探针:答非所问"`;随即用控制台登录态 `GET /v1/curation/candidates?status=pending` → 出现该 thread 的候选,`signal=negative_feedback`,`feedback_run_id` = 该 run,`feedback_comment` = 原话。若审计 `details.candidate` 是 `deferred`(trajectory 还没落盘),等 ≤300s 再查一次(worker 兜底),两种结果都写进 PR 描述,分清楚是哪一种。
2. 同一轮再 POST `rating:"up"` → 候选仍在,`feedback_changed_at` 非空;再 POST `rating:"down"` → `feedback_changed_at` 回到 null。
3. 重复 POST `down` 三次 → 候选仍只有一条。
4. 控制台 promote 该候选:`source` 由前端推导为 `regression`,填 `expected` → 201;`GET /v1/eval-datasets?agent_name=release-canary` 里出现新行,`source_trajectory_key` = 候选 key。这条就是「今天走不通、现在走通」的修复证据。
5. `GET /v1/curation/candidates?signal=implicit_success` → 200(不再 422)。

- [ ] **Step 4: PR3 验收(spec §8 PR3)**

1. 控制台 `GET /v1/conversations?has_down_rated=true&agent_name=release-canary` → 只剩有 👎 的会话;勾掉 → 全部会话。
2. 用一个 **viewer** 角色员工的登录态 `GET /v1/sessions/{session_id}/feedback` → 200 且 `comment` 原文可见(拍板)。
3. 对话详情页打开该会话,轮脚出现「点踩 · “探针:答非所问” · 终端用户」;Curation 页候选行显示原话与「后改为 👍」标签(按 PR2 Step 3-2 的状态)。
4. 跨租户不可见:系统管理员切到另一个租户后 `GET /v1/sessions/{session_id}/feedback?tenant_id=<另一租户>` → 404。

---

## PR 切分与并行波次表

| PR | 分支 | Task | 触及文件集合 | 波次 |
|---|---|---|---|---|
| **PR1 存储 + 对外端点 + 回显 + 文档** | `feat/p2-pr1-feedback-run-scope` | 0, 1, 2, 3, 4, 5, 6, 7 | `migrations/versions/0152_feedback_run_scope.py`、`models/feedback.py`、`models/eval_dataset.py`、`feedback_store.py`、`api/external_feedback.py`(新)、`api/__init__.py`、`app.py`、`api/_external.py`、`api/external_sessions.py`、`api/external_session_items.py`、`api/feedback.py`、`tests/test_external_only_gate.py`、`tests/test_console_lockdown.py`、`tests/test_external_feedback.py`(新)、`tests/test_external_sessions.py`、`tests/test_external_session_items.py`、`tests/test_feedback_api.py`、`tests/test_user_purge.py`、persistence `tests/test_feedback_store_upsert.py`(新)、`tests/test_rls_integration.py`、`api/sessions.ts`、`FeedbackBar.tsx`、`TurnFooter.tsx`(+test)、`turn/__tests__/FeedbackBar.test.tsx`(新)、docs-site 五页 + `config.mts`、spec §7 | 波 A:Task 0 ‖ Task 1 → 波 B:Task 2 → 波 C:Task 3 ‖ Task 4 ‖ Task 5 ‖ Task 6 ‖ Task 7(五个 worktree 并行;文件零交集 —— Task 3 独占 `api/__init__` / `app.py` / 两张路由表,Task 4 独占两个 external_* 端点文件 + `_external.py`,Task 5 独占 `api/feedback.py` + 前端,Task 6 只改一个测试,Task 7 只改 docs) |
| **PR2 进池 + 升级 + 改票 + 修漂移** | `feat/p2-pr2-negative-into-pool` | 8, 9, 10, 11 | `protocol/eval_dataset.py`、`curation/base.py`、`curation/sql.py`、`curation/memory.py`、`api/curation.py`、`feedback_candidates.py`(新)、`orchestrator/trajectory/reader.py`、`api/external_feedback.py`、`api/feedback.py`、`curation_worker.py`、`api/curation.ts`、`CandidatesPanel.tsx`、`Curation.stories.tsx`、两个 locale、`tests/test_in_memory_curation_store.py`、`tests/test_sql_curation_store.py`、`tests/test_curation_api.py`、`tests/test_feedback_candidates.py`(新)、`tests/test_external_feedback.py`、`tests/test_curation_worker.py`、`orchestrator/tests/test_trajectory_reader.py`、`Curation.test.tsx` | 波 A:Task 8 ‖ Task 11(后端 store vs 纯前端,零交集) → 波 B:Task 9 ‖ Task 10(都依赖 8;9 动端点 + reader,10 动 worker,零交集) |
| **PR3 控制台可见** | `feat/p2-pr3-console-visibility` | 12, 13, 14, 15, 16, 17 | `api/feedback.py`、`api/conversations.py`、`tests/test_feedback_api.py`、`tests/test_conversations_api.py`、`api/sessions.ts`、`api/conversations.ts`(+`__tests__/conversations.test.ts`)、`ConversationsList.tsx`(+test)、`ConversationDetail.tsx`(+test)、`FeedbackSummary.tsx`(新)、`TurnFooter.tsx`、`TurnBlock.tsx`、`Transcript.tsx`(+tests)、`CandidatesPanel.tsx`(+test)、两个 locale | 波 A:Task 12 ‖ Task 13 ‖ Task 16(16 只依赖 PR2) → 波 B:Task 14 ‖ Task 15(分别依赖 13 / 12;两者都改两个 locale,但不同 object,rebase 时只需顺序合入 i18n 增行) → Task 17 |

合并顺序:**PR1 → PR2 → PR3**,且 **P-2 PR1 先于 P-1 任何 PR 合入**(迁移链 0152 → 0153)。

### 与 P-1 线(`docs/superpowers/specs/2026-09-09-regenerate-edit-resend-design.md`)的文件交集与冲突点

P-1 触及(按其 spec §3-§5、§7):`agent_run` 模型 + 迁移 0153、`api/runs.py`(`spawn_run`)、`builder.py`、`transcript.py`、`thread_message` 模型 / 镜像、**`external_session_items.py`**(`_group_messages_by_run` 下沉 common + 条目 / `runs[]` 加 `superseded_by` / `regenerated_from`)、**`external_sessions.py`**(`/messages` 每条加 `superseded_by`)、**`external_runs.py`**(`:regenerate` / `:edit`)、`common/conversation_items.py`、**两张路由表**、**docs-site `chat.md` / `sse-events.md` / `query.md` / `errors.md` / `examples.md` / `best-practices.md` + `config.mts`**、**`ConversationDetail.tsx`**(被取代轮折叠态)。

| # | 交集文件 | P-2 改动 | P-1 改动 | 冲突点(「P-2 PR1 先合、P-1 rebase」之外要人手处理的) |
|---|---|---|---|---|
| 1 | 迁移链 | `0152_feedback_run_scope`(Task 1) | `0153_*` | P-1 的 `down_revision` 必须写 `"0152_feedback_run_scope"`。若 P-1 PR1 抢先合入,会出现两个 head,`alembic heads` 与所有 SQL 集成测夹具的 `upgrade(cfg, "head")` 全红;届时由后合的一方改 `down_revision`,不是靠 rebase 自动解决 |
| 2 | `api/external_session_items.py` | `:391` 接住 `meta`;`runs[]` 加 `feedback`;handler 加 `feedback` 依赖(Task 4) | 把 `_group_messages_by_run`(`:104-130`)搬去 common;`runs[]` 加 `superseded_by` / `regenerated_from`;条目加 `superseded_by` / `tombstone` | 同一个 `runs[]` 字典字面量两边各加键 → 文本冲突,手工合成四个键;P-2 不动 `_group_messages_by_run`,P-1 的下沉不受影响。`load_owned_session` 那行(`:391`)两边都可能改成 `meta = …`,合并时保留一份 |
| 3 | `api/external_sessions.py` | `:267` 接住 `meta`;`out` 每条加 `feedback`(Task 4) | `out` 每条加 `superseded_by` | 同上,同一字典字面量两边加键 |
| 4 | `api/external_runs.py` | 不动(P-2 新端点在独立文件 `external_feedback.py`) | 加 `:regenerate` / `:edit` | 无冲突。两张路由表两边都在表尾追加 → 文本冲突,按字面顺序都保留即可 |
| 5 | `test_external_only_gate.py:92` / `test_console_lockdown.py:281` | 各追加一条 | 各追加两条 | 表尾追加冲突,合并时三条都留 |
| 6 | `docs-site/guide/chat.md` | 末尾新增 `## 2.9`(Task 7) | 新增「重新生成与编辑重发」节 | **编号**:P-1 必须用 `2.10`,侧栏同步;两边都在文件末尾追加 → 文本冲突,按 2.9 → 2.10 顺序排 |
| 7 | `docs-site/guide/examples.md` | 末尾新增 `## 10.9` | 新增示例节 | 同上:P-1 用 `10.10`(以及之后) |
| 8 | `docs-site/guide/query.md` | 5.3 表加 `feedback` 行、5.8 `runs` 表加 `feedback` 行 | 5.3 / 5.8 加 `superseded_by`、`regenerated_from`、`tombstone` 行 | 同一张表两边加行 → 文本冲突;都保留,`feedback` 行放在最后 |
| 9 | `docs-site/guide/errors.md` | 8.1 表改 `RUN_NOT_FOUND` 端点列、8.6 段落、8.10 例外说明 | 8.1 表加 `RUN_NOT_LAST` / `THREAD_BUSY` / `RUN_AWAITING_APPROVAL` / `RUN_ALREADY_SUPERSEDED` 四行,`RUN_NOT_FOUND` 端点列也要加 `:regenerate` / `:edit` | `RUN_NOT_FOUND` 那一行两边都改 → 必冲突,合并后端点列应为「取消 run / 审批决策 / 事件接口 / 打分 / 重新生成 / 编辑重发」 |
| 10 | `docs-site/.vitepress/config.mts` | `:50` 后加 2.9 | 加 2.10 与其它节 | 相邻行插入 → 文本冲突,按编号排 |
| 11 | `apps/admin-ui/src/pages/ConversationDetail.tsx` | `:170-175` 并行取反馈;`<Transcript feedbackOf=…>`(Task 15) | 被取代轮折叠态(改 `buildConsoleTurns` 输入或 `TurnBlock` 渲染) | `<Transcript …/>` 的 prop 列表两边都加 → 文本冲突;语义上无关。若 P-1 改 `TurnBlock` / `Transcript` 的 props 接口,与 P-2 的 `feedbackOf` 同一 interface 块 → 手工合并 |
| 12 | `RunInfo` / `agent_run` | 只读 `run.thread_id` / `run.run_id` | 加 `superseded_by_run_id` / `regenerated_from_run_id` 列与字段 | 无代码冲突。**语义点(2026-09-09 用户拍板)**:重新生成 / 编辑重发后,旧轮上已打的 👍/👎 **不迁到新轮**。理由:评分是对**那一次回答**的评价,新一轮是新的回答;而且候选池里的候选指向的是旧 trajectory,分留在旧轮才对得上。落到行为上 = 被取代轮的 `feedback` 照常回显、新轮 `feedback` 为 `null`(见 Task 4 的行为说明与断言),被取代的轮仍可被打分(P-2 不检查 supersede 状态),两侧都无需为此写迁移代码 |
| 13 | `test_external_session_items.py:364-375` 的 `runs == [...]` 全等断言 | 补 `"feedback": None` | 补 `"superseded_by": None` 等 | 同一字面量两边加键,手工合 |
| 14 | `api/__init__.py` / `app.py:2643-2651` | 加 `build_external_feedback_router`(Task 3) | 若 P-1 不新建 router 则不动;若新建则同一 import 块 / `include_router` 块 | 相邻行插入冲突,按字母序保留两份 |

不在交集里、因此可以完全并行的 P-2 部分:Task 1 / 2 / 5 / 6 / 8 / 9 / 10 / 11 / 12 / 13 / 14 / 16 的全部文件;P-1 的 `builder.py` / `transcript.py` / `api/runs.py` / `thread_message` 与 P-2 零交集。

---

## 自审(writing-plans 三项)

**1. Spec 覆盖:**
- §3 数据模型 → Task 1(四列 + 三列 + 索引 + `turn_seq` 不动)。
- §4.1 写反馈(权限 / body / 归属 404 / 覆盖 / 信封 / 路由表 / 配额核对)→ Task 3(路由表按出入表 #2 为两张 + 一张自动审计)。
- §4.2 读反馈 `/items` `/messages` 只回显本人 → Task 4。
- §4.3 文档五页 → Task 7。
- §5 同步进池 / 升级 / 未落盘 worker 兜底 / 👍 不建不降级 / 改票标记 / 修两处词表漂移(+ 出入表 #9 的第三处)→ Task 8、9、10、11。
- §6 控制台 GET / `has_down_rated` / 轮脚 / 候选行 / 控制台 POST 必带 `run_id` + `source='console'` → Task 12、13、15、16、5。
- §7-1/2/3 真跑确认与回填 → Task 0。
- §8 三段验收(含「删升级逻辑必须红」的变异、purge 归零、跨租户不可见、viewer 看评论)→ Task 9 Step 8-1、Task 6、Task 2 RLS 用例 + Task 12、Task 17。
- 未覆盖项:无。spec 未提但计划补的:`list_for_thread_scoped`(出入 #12)、`down_rated_thread_ids`(出入 #8)、promote 弹窗 `expected`(不补则 `regression` 仍 422,§8 PR2「promote 一路走通」达不到)。

**2. 占位符扫描:** 全文搜 `TBD` / `TODO` / `implement later` / `fill in` / `Similar to Task` / `add appropriate` / `handle edge cases` → 0 命中(Task 7 Step 4 的四语言示例只给了结构与函数名,因为 10.6 的四段模板在 `examples.md:2597-2796` 原样可抄,计划里已写明「复用 10.6 的 `readBody / readErrorBody / jsonEscape` 原文」与示例值)。

**3. 类型一致性:** 跨 Task 引用的名字逐一对过 —— `FeedbackStore.upsert -> tuple[FeedbackRecord, bool]`(Task 2 定义;Task 3 / 5 / 12 / 13 用);`list_for_thread_scoped(*, tenant_id, thread_id)`(Task 2;Task 4 / 9 / 12 用);`down_rated_thread_ids(*, tenant_id: UUID | None, limit=500)`(Task 2;Task 13 用);`CurationCandidateStore.upgrade_to_negative(*, tenant_id, trajectory_key, feedback_run_id, feedback_comment) -> bool` 与 `mark_feedback_changed(*, tenant_id, feedback_run_id, at) -> int`(Task 8;Task 9 / 10 用);`TrajectoryReader.find_by_thread(*, tenant_id, thread_id)`(Task 9 定义与使用);`sync_candidate_for_feedback(*, deps, tenant_id, thread_id, run_id, rating, previous_rating, comment) -> CandidateSyncResult`(Task 9);前端 `SessionFeedbackItem` / `getSessionFeedback(threadId, tenantId?)`(Task 15 定义,`TurnFooter.feedback` / `feedbackOf` 同型);`submitSessionFeedback(threadId, {rating, comment?, run_id, turn_seq?})`(Task 5;`FeedbackBar` 用);`promoteSourceOf(signal) -> EvalDatasetSource`(Task 11)。

## 最不确定的两处(给审阅者)

1. **`TrajectoryReader.find_by_thread` 的定位方式**(Task 9):按租户前缀 `list_prefix` 再按 `/{thread_id}.jsonl` 后缀过滤,匹配到的每个 key 都 `read()` 一次,按 `finished_at` 取最大(并列取 key 大的)。代价:大租户一次 👎 = 一次全租户前缀列举 + 个位数次对象读(与 worker 每 300s 的跨租户列举同量级,但落在请求路径上);同一 thread 同一时刻两个 outcome 分区并列时按 key 定胜负是为了确定性,不代表业务上哪个更「新」。替代方案是在 `agent_run` 上记 `trajectory_key`(orchestrator 写 trajectory 时回填),那是跨服务改动,本计划没做。
2. **覆盖时 `processed_at` 不重置**(Task 2 Step 5,spec-literal):👍→👎 改票后,`FeedbackConsumerWorker`(`feedback_consumer.py:149` 只扫 `processed_at IS NULL` 的 👎)不会再为这一行打记忆 `review_flagged_at`。spec 只写了「rating/comment/item_id 全量替换」,没提 `processed_at`;若拍板要重置,改 Task 2 的 `.values(...)` / `replace(...)` 各加一行 `processed_at=None`,测试在 `_upsert_scenario` 加一条断言即可。

已拍板、不再是问号(2026-09-09):**旧轮的 👍/👎 不迁到新轮**,被取代的轮仍可被打分 —— 交集表 #12 与 Task 4 Step 6b 有结论、理由与对应断言。

其它待拍板(不阻塞):`upgrade_to_negative` 对 `promoted` / `dismissed` 状态的候选是否同样改 signal(Task 8,spec 写「任何 signal」,没提 status)。

## 顺带发现(Task 0 途中撞见,**不属于本计划范围,不要在 P-2 里改**)

用户已知悉,会另行入册。列在这里只为留住证据、避免下一个人重新查一遍。

1. **`event_log` 表在生产与测试都是 0 行,是一张死表。** 真正在用的事件表是 `run_event`(生产 18 行 / 测试 10540 行)。连带后果:`packages/expert-work-persistence/migrations/versions/0014_feedback.py:11` 那句
   > ``turn_seq`` points at ``event_log.seq`` but carries no foreign key

   是**双重过时** —— 既指错了表(控制台实际传的是 UI 时间线的 0-based 局部下标,见 `components/console/types.ts:29-30` → `TurnFooter.tsx:157` → `FeedbackBar.tsx:45`),它所指的那张表**本身也没有数据**。本计划对 `turn_seq` 的处置维持不变:**保留不动,不读不写不删**(spec §3)。要不要给 `event_log` 立清理 / 弃用条目,由用户决定。

2. **控制台「写反馈」与「看反馈」天生不在同一个页面**,详见 Task 5 的「前置事实」表:写只在调试台(`PlaygroundTab.tsx:705` `readOnly={false}`),对话详情页恒 `readOnly`(`ConversationDetail.tsx:702`)因而永不渲染 `FeedbackBar`。这是既定形态、不是 bug,P-2 也**不**改它 —— 但「要不要让员工在对话详情页也能替终端用户补打分」是一个真实的产品问题,值得单独入册,别在本计划里顺手做掉。
