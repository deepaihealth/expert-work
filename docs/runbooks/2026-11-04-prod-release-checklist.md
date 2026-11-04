# 生产发布执行单 2026-11-04(待执行):`5a1600934` → `${REL}`(测试记录 PR 合入后的 origin/main;代码 = 测试 `e6c1041a7`)

> 拍板(11-04):① 今晚发 ② 迁移 0181 + 0182 **单独先跑**,等 migrate Job 完成再滚 Deployment(B-228 的临时做法;`release.sh` 本身没改,它仍然把 Job 和 Deployment 一起下发,见 `tools/deploy/release.sh:384-390`)③ 生产命令全部由你执行 ④ 评测不在生产跑。
> 每段做完把输出贴给我,我对照判据说能不能进下一段;对不上就停。
> 拍板(11-04,用户「同意」):B-242 P2(进程互不可见)与 B-261(待命沙箱 envd 无 token)**都不挡本版**,生产现状即如此、本版不加重,按 ROADMAP 后续排期。
> 本版第一次把 **B-236(每个对话独立工作目录)** 带上生产,所以必须**先换沙箱镜像、回收在用沙箱,再发代码**。

## 总览

| 段 | 内容 | 预计 | 对用户影响 |
|---|---|---|---|
| 前置 | 发前硬条件(要你拍板) | — | 无 |
| 0 | 只读盘点:装载、集群现状、库、NAS、留存日志、进度聚合 EXPLAIN、日志基线 | 40–60 分钟(NAS 遍历耗时未知) | 无 |
| 1 | 发前动作:通知深护智康 → 构建迁移镜像 → 沙箱换镜像 + 回收 + 金丝雀 | 30–45 分钟 | 回收沙箱时,正在执行的工具调用失败一次 |
| 2 | 发版两步:2.1 迁移 Job(等完成)→ 2.2 `release.sh prod` | 35–55 分钟 | 滚动更新不停服;热沙箱按新布局各重建一次 |
| 3 | 发后验证(当晚)+ 次日核对 + 记录 | 当晚 30 分钟 + 次日 15 分钟 | 无 |
| | **合计(当晚)** | **约 2.5 小时** | **挑低峰发**(B-242 / B-262 换沙箱布局,见 2.2) |

---

## 前置:发前硬条件(没满足就停,请你拍板)

- [x] **H1 本版在测试环境原样跑过**:11-04 测试 `72abce48b`(#2012 + #2013)按本单 1.2 + 2.1 + 2.2 两步原样演练(迁移专用镜像 `72abce48b-migrate`、从 overlay 渲染的 Job + 锁超时、再 release.sh),smoke OK、金丝雀 PASS;手册 §88 / §89 全过(记录 PR 见 0.2)。生产的 `${REL}` 是记录 PR 合入后的 main:比 `72abce48b` 只多文档与 test overlay 提交,代码相同。 **追加**:B-276(#2015,只改 orchestrator / control-plane 的平台文字与工具说明,无迁移、无配置)用户要求发版前解决,测试 `e6c1041a7` 直接 release.sh(smoke OK、金丝雀 PASS),改前 / 改后对比见 ROADMAP B-276。
- [x] **H2 深护智康已通知并回复(11-04)**:1 / 3 / 4 / 5 项 project-service 不受影响(429 = `QUOTA_EXCEEDED`、产物清单 `name` 不带 `threads/` 均已确认)。第 2 项(同名上传换名)影响 ai-strategy 报告调整流程:报告标题 / 发客户的文件名会带 `-<8 位>`(存量会话首轮调整即可能出现,老布局 `uploads/` 共用)。**用户拍板方案 A**:今晚照发;深护收件时循环去掉末尾 `-[0-9a-f]{8}` / `-[0-9a-f]{12}`(他们的用户拍板、上线时间待回复,记进发布记录);平台后续登记 B-275(附件信息带原文件名 / 交付名沿用原名)。**深护已改完、其用户拍板今晚随 project-service 上线**(`report-file.ts` `pickReportFile` 收件时循环去掉末尾 `-[0-9a-f]{8}` / `-[0-9a-f]{12}`,19/20 开头的纯数字日期段不去;下载仍用清单原名;ai-strategy 单测 178 过)。发布记录写明:深护上线晚于我们时,中间调整过的报告标题可能带后缀;B-275 做完通知深护评估撤掉这段处理。
- [ ] **H3 记录 PR 合入后重跑 0.2 的装载核对**:比 `560bad398` 多 #2013(B-274,admin-ui + 文档)、#2014 与本次测试记录 PR(只改 `infra/k8s/overlays/test/kustomization.yaml` 与文档)、#2015(B-276,orchestrator / control-plane 代码);迁移仍然只有 2 条,`infra/k8s/` 不出现新文件。

---

## 段 0:发前只读盘点(不影响线上)

### 0.1 建发版目录(不动你当前的分支)

```sh
cd ~/src/github/jone_qian/expert-work && git fetch origin
git log -3 --oneline origin/main            # 最上面应是 B-274,下面是 560bad398
REL=$(git rev-parse --short origin/main); echo "${REL}"
git worktree add --detach ~/ew-prod-${REL} ${REL}
cd ~/ew-prod-${REL} && git status --porcelain     # 必须为空
export KUBECONFIG=~/.kube/expert-work-prod.yaml
NS=expert-work
PREV=5a1600934
TAG=$(git rev-parse --short HEAD)           # 与 release.sh 默认 tag 同一取法
ACR=crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work
OUT=~/rel-20261104; mkdir -p "${OUT}"
ls -l ~/.kube/expert-work-prod.yaml ~/.kube/expert-work-prod-params.env    # 都在、都是 600
```

之后所有命令都在这个目录、这个终端里执行。换了终端要重新设这几个变量。

### 0.2 装载核对(`production-release.md` §2.0 原样跑)

```sh
grep -A2 'expert-work/control-plane$' infra/k8s/overlays/prod/kustomization.yaml | grep newTag   # newTag: 5a1600934
git log --oneline "${PREV}".."${REL}" | tee "${OUT}/loaded.txt" | wc -l
git diff --name-status "${PREV}".."${REL}" -- '*/migrations/versions/*'
git diff --name-status "${PREV}".."${REL}" -- tools/persistence
git diff --name-status "${PREV}".."${REL}" -- infra/k8s/
git diff "${PREV}".."${REL}" -- infra/k8s/sandbox/sandboxset.yaml
git diff "${PREV}".."${REL}" -- infra/k8s/base/ | grep -i secretKeyRef
git diff --name-status "${PREV}".."${REL}" -- infra/k8s/sandbox-prod infra/k8s/base/retention-cleanup-job
```

期望(对 `560bad398` 核过,B-274 合入后应该只多它自己的提交):
- [ ] 提交数 = **33 + B-274 的提交数**(squash 合并就是 34)
- [ ] 迁移**恰好 2 行**:`A …/0181_thread_workspace_layout.py`、`A …/0182_batch1103_columns.py`
- [ ] `tools/persistence`:无输出
- [ ] `infra/k8s/` 恰好这 6 行:`M overlays/prod/kustomization.yaml`(10-30 那次的记录 PR,0db5633d3→5a1600934)、`M overlays/test/configmap-patch.yaml`、`M overlays/test/kustomization.yaml`、`A overlays/test/web-search-fixtures-patch.yaml`、`A overlays/test/web-search-fixtures/b267-kb-route.json`、`M sandbox/sandboxset.yaml`
- [ ] sandboxset 只改一行镜像:`…-vpc…/sandbox:daf63cb1` → `…/sandbox:f2ee1b4a`
- [ ] 新增 secretKeyRef:无。`sandbox-prod`、`retention-cleanup-job`:无变化

多出任何一项都说明装载和本单对不上,停下来查。

本版装载(编号 → 生产相关动作):

| 编号 | 内容 | 生产上要额外做的 |
|---|---|---|
| B-236 + B-244 / B-245 | 每个对话独立 `/workspace`,`/agent` 是智能体级共享区;开场复制 20 MB;`/agent` 文件留版本;迁移 0181 | 先钉沙箱镜像 + 回收(1.3)、量 agent 根大小(0.5)、通知(附 A) |
| B-242 P0+P1 | 沙箱本地目录按对话隔离,堵住登录 shell 劫持 | 不用换镜像,低峰发(热沙箱按 `layout_mismatch` 重建) |
| B-262 P0 / P1 / P3 | 同名上传不覆盖;上传、输入、工具结果按对话分开;老对话在下一次新 run 时自动迁移 | 通知(附 A)、量迁移量(0.4 / 0.5)、发后看 `platform_migration` |
| B-249 | `exec_python` 拿工作区写锁(只在新布局) | 无 |
| B-248 / B-259 / B-260 | MCP 池退役收尾、改目录不再整池断开、智能体缓存有代次保护 | 0.8 记基线,次日对比(§82.5) |
| B-237 / B-238 / B-256 / B-239 / B-241 / B-255 | 知识库估价、控制台修正 | 无 |
| B-250..B-254、B-257、B-265、B-266、B-263 | 看图相关;上传图片按方向转正、新增像素上限 | 更新记录改日期 |
| B-267..B-273(#2012) | 先查知识库、看页窗口、排队与进度、上传到期删文件、参数还原、整体题走整图;迁移 0182 | 0.4 / 0.6 / 0.7、发后画像预演(3.3);**测试未验(H1)** |
| B-274 | admin-ui:历史轮附件显示成标签(#2013) | 无 |
| B-276 | 不能看图的主模型不再重做看图子任务的工作(#2015,平台文字 / 工具说明) | 无;发后若有「主模型不看图 + 子任务看图」的智能体,次日抽一轮看是否还装 OCR |
| 依赖升级 | python-deps 9 项、admin-ui、vite、pypdf(沙箱镜像) | 构建时镜像源可能缺新版本 → `EXPERT_WORK_PYPI_MIRROR=` 回退到 pypi.org |

### 0.3 集群现状快照(Step P,和仓库钉的值对一遍)

```sh
kubectl -n default get sandboxset expert-work-sandbox -o jsonpath='{.spec.template.spec.containers[*].image}{"  cache="}{.spec.template.metadata.annotations.image\.alibabacloud\.com/enable-image-cache}{"  sg="}{.spec.template.metadata.annotations.network\.alibabacloud\.com/security-group-ids}{"\n"}' | tee "${OUT}/sandboxset-before.txt"
for ns in expert-work default; do kubectl -n ${ns} get secret acr-pull -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | python3 -c 'import json,sys; print(*sorted(json.load(sys.stdin)["auths"]))'; done
kubectl -n ${NS} get deploy -o 'custom-columns=N:.metadata.name,I:.spec.template.spec.containers[0].image' | tee "${OUT}/deploy-before.txt"
kubectl -n ${NS} get secret canary-credentials -o name
kubectl get imagecaches.image.alibabacloud.com | grep -E 'daf63cb1|f2ee1b4a'
```

期望:
- [ ] 沙箱是 `…-vpc…/sandbox:daf63cb1`、`cache=true`、**`sg=` 为空**:11-03 ACS 自动升级 CSI 组件后沙箱专用安全组挡住 NAS 挂载、所有沙箱开不出来,用户用 JSON patch 摘掉了模板上的安全组注解(池回到集群安全组),等 VPC 流日志查清被拒的目的地再恢复(B-217 隔离暂时打开)。今晚**不恢复**安全组
- [ ] 两个 namespace 的 `acr-pull` 各打印出两个 host
- [ ] control-plane / credential-proxy 是 `:5a1600934`,admin-ui 是 `:0db5633d3-prod`
- [ ] `secret/canary-credentials` 存在
- [ ] 镜像缓存:`f2ee1b4a` 有一行最好(测试做的缓存,10-29 生产直接命中过同账号的缓存);没有也行,只是第一台沙箱要冷拉

对不上就停,先弄清楚是谁改的。

### 0.4 只读盘点 SQL(`run_sql` 带 `SET TRANSACTION READ ONLY`,写语句会直接报错)

```sh
POD=$(kubectl -n ${NS} get pods -l app.kubernetes.io/name=control-plane --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
run_sql() {  # 用法: run_sql 本地文件.sql
  { printf 'SQL = r"""\n'; cat "$1"; printf '\n"""\n'; cat <<'EOF'
import asyncio
from sqlalchemy import text
from control_plane.app import _build_sql_stores
from control_plane.settings import Settings
from control_plane.tenant_scope import bypass_rls_session

async def main():
    stores = _build_sql_stores(Settings())
    try:
        async with bypass_rls_session():
            async with stores.session_factory() as s:
                await s.execute(text("SET TRANSACTION READ ONLY"))
                r = await s.execute(text(SQL))
                print(" | ".join(r.keys()))
                for row in r:
                    print(" | ".join(str(x) for x in row))
    finally:
        await stores.engine.dispose()

asyncio.run(main())
EOF
  } | kubectl -n ${NS} exec -i "${POD}" -- python3 -
}
cat > "${OUT}/pre.sql" <<'EOF'
SELECT 'alembic' k, version_num::text v FROM alembic_version
UNION ALL SELECT 'server_version', current_setting('server_version')
UNION ALL SELECT 'long_xacts_gt_1min', count(*)::text FROM pg_stat_activity WHERE datname=current_database() AND xact_start < now()-interval '1 minute' AND pid<>pg_backend_pid()
UNION ALL SELECT 'runs_in_flight', count(*)::text FROM agent_run WHERE status IN ('running','queued','paused')
UNION ALL SELECT 'threads_total', count(*)::text FROM thread_meta
UNION ALL SELECT 'threads_with_run_30d', count(DISTINCT thread_id)::text FROM agent_run WHERE created_at > now()-interval '30 days'
UNION ALL SELECT 'runs_30d', count(*)::text FROM agent_run WHERE created_at > now()-interval '30 days'
UNION ALL SELECT 'uploads_total', count(*)::text FROM user_upload
UNION ALL SELECT 'uploads_soft_deleted', count(*)::text FROM user_upload WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'uploads_live_older_90d', count(*)::text FROM user_upload WHERE deleted_at IS NULL AND created_at < now()-interval '90 days'
UNION ALL SELECT 'uploads_live_older_76d', count(*)::text FROM user_upload WHERE deleted_at IS NULL AND created_at < now()-interval '76 days'
UNION ALL SELECT 'uploads_oldest_day', COALESCE(min(created_at)::date::text,'-') FROM user_upload
UNION ALL SELECT 'upload_threads_live', count(DISTINCT thread_id)::text FROM user_upload WHERE deleted_at IS NULL
UNION ALL SELECT 'kb_bases', count(*)::text FROM knowledge_base
UNION ALL SELECT 'kb_documents', count(*)::text FROM knowledge_document
UNION ALL SELECT 'agents_with_kb', count(*)::text FROM agent_spec WHERE status <> 'deleted' AND CASE WHEN jsonb_typeof(spec_json->'spec'->'knowledge'->'knowledge_base_refs')='array' THEN jsonb_array_length(spec_json->'spec'->'knowledge'->'knowledge_base_refs') ELSE 0 END > 0
UNION ALL SELECT 'rate_glm_5_3_flash', count(*)::text FROM model_rate_card WHERE tenant_id IS NULL AND provider='glm' AND model='glm-5.3-flash'
UNION ALL SELECT 'size_'||relname, reltuples::bigint||' rows / '||pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class WHERE relkind='r' AND relname IN ('thread_meta','agent_run','user_upload','knowledge_base','run_event')
EOF
run_sql "${OUT}/pre.sql" | tee "${OUT}/pre.txt"
cat > "${OUT}/pre-sandbox.sql" <<'EOF'
SELECT layout, state, count(*) FROM sandbox_instance WHERE destroyed_at IS NULL GROUP BY 1,2 ORDER BY 1,2
EOF
run_sql "${OUT}/pre-sandbox.sql" | tee "${OUT}/pre-sandbox.txt"
```

怎么读:
- `alembic` = **`0180_artifact_version_snapshot`**。不是这个就停:装载对不上。
- `long_xacts_gt_1min` = 0。不为 0 先等。0181 / 0182 要短暂拿 `thread_meta` / `agent_run` / `user_upload` / `knowledge_base` 的排他锁,0181 的 CHECK 还要扫一遍 `thread_meta`,长事务会让它排队,排队期间这几张表上的查询全部跟着卡住。
- `runs_in_flight`:选它为 0 或很小的时候做 1.3 和 2.x。
- **B-271**:`uploads_soft_deleted` 不为 0,就要对这些行补扫一次(方案第 6 条),**先停下来问我**。`uploads_live_older_90d` 应为 0(方案估生产最早约 11-15 才有行到期)。`uploads_live_older_76d` 就是未来两周内会被新留存作业**真删文件**的行数,记下来。
- **B-262 P3**:`threads_with_run_30d` ≈ 发布后会被逐个迁移的老对话数(生产现在全是老布局)。
- **B-267**:`kb_bases` / `agents_with_kb` 是 0,画像和每轮探测就和生产无关。`rate_glm_5_3_flash` = 0 时,补画像预演会报 `unpriced`,`--apply` 会拒绝执行。
- 表大小:`thread_meta` 的行数决定 0181 CHECK 的扫描时长,几万行以内是秒级。
- `pre-sandbox`:旧代码写的布局应是 `agent-ns`(或 `user-root`)。新代码是 `agent-conv-platform`,发版后这些热会话会在下一次取用时按 `layout_mismatch` 重建。

> release.sh 为什么不做:它不看库。漏了会怎样:长事务没清就迁移会卡住线上;B-271 的补扫与否、迁移量都要发后才知道。

### 0.5 NAS:各 agent 根的大小(B-236 要求发前量;B-262 P3 迁移量)

release.sh 为什么不做:它不碰 NAS。
漏了会怎样:三件事都会悄悄吃 NAS 空间,而且量都由 agent 根的大小决定:
- 新对话、以及每个被迁移的老对话,开场都会把 agent 根下的一级目录复制进来(每个对话最多 20 MB)。
- 每次主 run 结束,`/agent` 下的文件按观察留版本(每个用户 × 智能体最多 100 MiB)。
- B-262 P3 迁移时复制老对话自己的上传和工具结果。

不先量,发后才发现空间涨得快。

```sh
cat > "${OUT}/agent_roots.py" <<'EOF'
import os, stat, time
ROOT = "/mnt/workspaces"
RESERVED = {"threads", "uploads", "inputs", ".tool_results", "skills", "shared", "agents"}
MB = 1024 * 1024
t0 = time.time()

def tree(path):
    total = files = 0
    for dp, _dns, fns in os.walk(path, followlinks=False):
        for n in fns:
            try:
                st = os.lstat(os.path.join(dp, n))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
                files += 1
    return total, files

def dirs(path):
    try:
        return [e for e in os.scandir(path) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    except OSError:
        return []

rows = []
for t in dirs(ROOT):
    for u in dirs(t.path):
        agents = os.path.join(u.path, "agents")
        if os.path.islink(agents) or not os.path.isdir(agents):
            continue
        for a in dirs(agents):
            seed = loose = up = tr = files = 0
            try:
                entries = list(os.scandir(a.path))
            except OSError:
                continue
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    b, n = tree(e.path)
                    files += n
                    if e.name == "uploads":
                        up += b
                    elif e.name == ".tool_results":
                        tr += b
                    elif not e.name.startswith(".") and e.name not in RESERVED:
                        seed += b
                elif e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                    loose += e.stat(follow_symlinks=False).st_size
            rows.append((seed, loose, up, tr, files))

def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))] if s else 0

seeds = [r[0] for r in rows]
print(f"agent_roots={len(rows)} files={sum(r[4] for r in rows)} secs={time.time() - t0:.0f}")
print(f"seedable_dirs_mb p50={pct(seeds, .5) / MB:.1f} p90={pct(seeds, .9) / MB:.1f} max={max(seeds, default=0) / MB:.1f} over_20mb={sum(s > 20 * MB for s in seeds)}")
print(f"root_loose_files_mb total={sum(r[1] for r in rows) / MB:.1f}")
print(f"versionable_mb_upper_bound={sum(min(r[0] + r[1], 100 * MB) for r in rows) / MB:.1f}")
print(f"uploads_mb total={sum(r[2] for r in rows) / MB:.1f} max_per_root={max((r[2] for r in rows), default=0) / MB:.1f}")
print(f"tool_results_mb total={sum(r[3] for r in rows) / MB:.1f} max_per_root={max((r[3] for r in rows), default=0) / MB:.1f}")
EOF
kubectl -n ${NS} exec -i "${POD}" -- python3 - < "${OUT}/agent_roots.py" | tee "${OUT}/agent_roots.txt"
kubectl -n ${NS} exec "${POD}" -- df -h /mnt/workspaces | tee -a "${OUT}/agent_roots.txt"
```

脚本只读,只打印数字,不打印名字和路径。目录结构按 `layout.py`(`agents/<key>`)与 `workspace_paths.py:155`(保留名)写的,**未在真 pod 上跑过**。

怎么读:
- 粗估开场复制会带来的 NAS 增长 ≈ `threads_with_run_30d × min(seedable p90, 20 MB)`。
- 版本库最终上限 ≈ `versionable_mb_upper_bound`。
- P3 迁移复制量 ≤ `uploads_mb + tool_results_mb`(工具结果每个对话另有 100 MiB 上限)。

和 `df` 的剩余空间比一比。**估算增长超过剩余空间的 30% 就停**,我们先讨论。

### 0.6 留存作业日志(B-271 方案「上生产前要做」第 1 条)

```sh
kubectl -n ${NS} get jobs -l app.kubernetes.io/name=retention-cleanup
for p in $(kubectl -n ${NS} get pods -l app.kubernetes.io/name=retention-cleanup -o jsonpath='{.items[*].metadata.name}'); do printf '%s %s orphan_copies_failed=' "${p}" "$(kubectl -n ${NS} get pod ${p} -o jsonpath='{.status.phase}')"; kubectl -n ${NS} logs "${p}" | grep -c 'retention.orphan_artifact_copies_failed'; done
```

判据:每个 pod 的计数都是 0。

不为 0 的话,新镜像上这一段失败会让作业以 **3** 退出,CronJob 显示失败(今晚 03:23 就会出现)。要先弄清原因,或者接受「明早作业红一次」。
release.sh 为什么不做:它不看 CronJob 历史。

### 0.7 B-270 进度聚合在生产最大 run 上的耗时(方案「上生产前要做」第 2 条)

用的是本版源码里的那条 SQL,原样从文件里抽出来。它不涉及新列,旧库上就能量:

```sh
awk '/^_PROGRESS_SQL = text\($/{f=1;next} f&&/^    """$/{c++; if(c==2) exit; next} f&&c==1' packages/expert-work-runtime/src/expert_work/runtime/runs/event_store.py > "${OUT}/progress.sql"
head -1 "${OUT}/progress.sql"; wc -l < "${OUT}/progress.sql"     # "    WITH ev AS MATERIALIZED (" / 73
cat > "${OUT}/explain_progress.py" <<'EOF'
import asyncio
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from control_plane.app import _build_sql_stores
from control_plane.settings import Settings
from control_plane.tenant_scope import bypass_rls_session

TOP = """
SELECT e.run_id, r.tenant_id, count(*) AS frames, sum(pg_column_size(e.data)) AS bytes
FROM run_event e JOIN agent_run r ON r.id = e.run_id
WHERE r.created_at > now() - interval '30 days'
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5
"""

async def main():
    stores = _build_sql_stores(Settings())
    try:
        async with bypass_rls_session():
            async with stores.session_factory() as s:
                await s.execute(text("SET TRANSACTION READ ONLY"))
                await s.execute(text("SET LOCAL statement_timeout = '120s'"))
                top = (await s.execute(text(TOP))).all()
                for i, row in enumerate(top, 1):
                    print(f"top{i} frames={row.frames} bytes={row.bytes}")
                if not top:
                    print("no runs in 30 days")
                    return
                same = [row.run_id for row in top if row.tenant_id == top[0].tenant_id]
                for label, ids in (("largest_run", [top[0].run_id]), (f"same_tenant_top{len(same)}", same)):
                    stmt = text("EXPLAIN (ANALYZE, BUFFERS) " + SQL).bindparams(
                        bindparam("ids", value=ids, type_=ARRAY(PG_UUID(as_uuid=True))),
                        bindparam("tenant_id", value=top[0].tenant_id, type_=PG_UUID(as_uuid=True)),
                    )
                    print(f"== {label}")
                    for (line,) in (await s.execute(stmt)).all():
                        print(line)
    finally:
        await stores.engine.dispose()

asyncio.run(main())
EOF
{ printf 'SQL = r"""\n'; cat "${OUT}/progress.sql"; printf '\n"""\n'; cat "${OUT}/explain_progress.py"; } | kubectl -n ${NS} exec -i "${POD}" -- python3 - | tee "${OUT}/explain_progress.txt"
grep -E '^top|^==|Execution Time' "${OUT}/explain_progress.txt"
```

判据:`largest_run` 的 `Execution Time` **< 2000 ms**(代码里的 `statement_timeout` 是 2 s),最好 < 500 ms。

超过 2 s 不挡发布:后果是那些最长的 run 不显示 `progress`(列表照常返回),而恰好最需要进度的就是它们。记下来,按方案「超标再改为心跳时顺带写一列」另开票。
release.sh 为什么不做:它不碰性能。

### 0.8 B-260 日志基线(§82.5 要「发布前同时长」对比)

```sh
for p in $(kubectl -n ${NS} get pods -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[*].metadata.name}'); do printf '%s not_started_24h=' "${p}"; kubectl -n ${NS} logs "${p}" --since=24h | grep -c 'not started'; done | tee "${OUT}/baseline-not-started.txt"
```

旧 pod 发版后就没了,只能现在记。

---

## 段 1:发前动作

### 1.1 通知深护智康(H2,内容见附 A)

release.sh 为什么不做:它不对外沟通。
漏了会怎样:对方按路径前缀或文件名匹配的代码会看到 `threads/{session_id}/work/…` 路径、带 `-<8 位>` 后缀的文件名,以及新错误码 `UPLOAD_SESSION_DELETED` / `UPLOAD_CHANGED`,这些都没有被告知过。

### 1.2 构建迁移专用镜像(不碰集群,10–25 分钟)

release.sh 为什么不做:它的迁移和 Deployment 是同一次 `apply -k` 下发的(B-228)。
为什么另起 tag:迁移要先跑,就得先有带 0181 / 0182 的镜像;而同一个 tag 不能推两次(ACS 镜像缓存按 tag 解析,不回源比 digest)。所以迁移镜像用 `${TAG}-migrate`,和 release.sh 稍后建推的 `${TAG}` 是同一份源码。

```sh
MIG_TAG=${TAG}-migrate; echo "${MIG_TAG}"
tools/deploy/build-push.sh --images control-plane --tag "${MIG_TAG}" --push 2>&1 | tee "${OUT}/build-migrate.log"
grep -E 'mirror|Pushing' "${OUT}/build-migrate.log" | tail -5    # 记下选中的 PyPI / apt 镜像源
```

注意 build-push.sh 也会推一次 `control-plane:latest`,release.sh 每次都推,无害。

2.2 的构建想命中缓存,就把这里选中的源原样带过去:`EXPERT_WORK_PYPI_MIRROR=… EXPERT_WORK_APT_MIRROR=… tools/deploy/release.sh prod`。镜像源选得不一样只会多花 10–25 分钟,不影响正确性。

### 1.3 沙箱换镜像 `daf63cb1 → f2ee1b4a` + 回收在用沙箱(必须在发代码之前,约 15 分钟)

release.sh 为什么不做:SandboxSet 在 `default` namespace,不在 overlay 里。
漏了会怎样(§73「发布顺序」,`pending-test-release-acceptance.md:1584`):新代码拿到旧镜像的沙箱,每次 exec 都以 97 退出(`ew-exec-view: image lacks /agent`)。布局判断只比 `sandbox_instance.layout`、不看镜像,不会自愈,要等那台沙箱过期(最长约 12 小时)。这期间那个用户的所有沙箱工具都不可用。

**今晚只换镜像,用 JSON patch,不能 `apply -k infra/k8s/sandbox-prod`,也不用 `apply -f sandboxset.yaml`**:`apply -k sandbox-prod` 会把安全组注解加回去,在阿里云给出放行清单之前,这等于重演 11-03 的事故(所有沙箱 `csi mount: deadline_exceeded`);`apply -f sandboxset.yaml` 会删掉池上的 `sg-dns-peers` / `sg-apiserver-peers` 两条注解(smoke 要用)。JSON patch 先 `test` 旧镜像再 `replace`,只动这一个字段。这一行的改动和仓库里 `sandboxset.yaml` 在 `5a1600934..${REL}` 之间的差异(只有镜像一行,0.2 已核)相同。

```sh
kubectl -n default get pods -l agents.kruise.io/sandbox-pool=expert-work-sandbox -L agents.kruise.io/sandbox-claimed
#   看 claimed=true 有几台;挑 0.4 的 runs_in_flight 为 0 的时候做
kubectl -n default patch sandboxset expert-work-sandbox --type=json -p '[{"op":"test","path":"/spec/template/spec/containers/0/image","value":"crpi-sgadimluo7wm655m-vpc.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:daf63cb1"},{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"crpi-sgadimluo7wm655m-vpc.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:f2ee1b4a"}]' 2>&1 | tee "${OUT}/sandbox-apply.log"
kubectl -n default delete sandbox -l agents.kruise.io/sandbox-pool=expert-work-sandbox
kubectl -n default get sandboxset expert-work-sandbox -w        # 等 availableReplicas=1,Ctrl-C
kubectl -n default get pods -l agents.kruise.io/sandbox-pool=expert-work-sandbox -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[?(@.name=="main")].image}{"\n"}{end}'
kubectl -n default get sandboxset expert-work-sandbox -o jsonpath='{.spec.template.spec.containers[*].image}{"  sg="}{.spec.template.metadata.annotations.network\.alibabacloud\.com/security-group-ids}{"\n"}'
tools/deploy/pg_upgrade/run_canary.sh prod 2>&1 | tee "${OUT}/canary-old-code-new-image.log"
```

判据:
- [ ] 池里**每一台**沙箱都是 `sandbox:f2ee1b4a`,没有一台还是 `daf63cb1`
- [ ] 模板 `sg=` 仍为空(安全组今晚不动;smoke 的 `WARN sandbox network (B-217)` 是预期)
- [ ] 金丝雀 `RESULT: PASS`。这一步验的是「旧代码 `5a1600934` + 新镜像」,这个组合在测试上**没有直接验过**:73.11 验的是 `deaefa60` + `5a1600934`,81.9 验的是 `f2ee1b4a` + `e86a81a29`。红了就把镜像退回去(见段 4 第 5 行),今晚停发。

回收那一刻正在执行的工具调用会失败一次;工作区在 NAS 上,重建不丢数据。

---

## 段 2:发版(两步)

### 2.1 第一步:只跑迁移,等完成(5 分钟)

release.sh 为什么不做:B-228(`release.sh:384-390`,ROADMAP B-228 行)。
漏了会怎样:新 pod 可能在迁移完成前起来。新代码建对话要写 `thread_meta.workspace_layout`,读 run 要读 `agent_run.started_at`,这段时间里这些请求会全部报 `UndefinedColumn`;迁移如果在等锁,这段时间会拉长到分钟级。

```sh
# 从生产 overlay 渲染出 migrate Job(带上 overlay 的全部补丁),只换镜像 tag、加锁超时 —— 11-04 测试原样演练过
kubectl kustomize infra/k8s/overlays/prod > "${OUT}/prod-render.yaml"
~/src/github/jone_qian/expert-work/.venv/bin/python -I - "${OUT}/prod-render.yaml" "${OUT}/migrate-job.yaml" "${MIG_TAG}" <<'EOF'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
jobs = [d for d in docs if d.get("kind") == "Job" and d["metadata"]["name"] == "migrate"]
assert len(jobs) == 1, len(jobs)
c = jobs[0]["spec"]["template"]["spec"]["containers"][0]
c["image"] = c["image"].rsplit(":", 1)[0] + ":" + sys.argv[3]
c.setdefault("env", []).insert(0, {"name": "PGOPTIONS", "value": "-c lock_timeout=5s"})
yaml.safe_dump(jobs[0], open(sys.argv[2], "w"), sort_keys=False)
print("image tag:", c["image"].rsplit(":", 1)[1], "| env:", [e["name"] for e in c["env"]], "| ns:", jobs[0]["metadata"].get("namespace"))
EOF
#   应打印:image tag: <MIG_TAG> | env: ['PGOPTIONS', 'EXPERT_WORK_DB_URL'] | ns: expert-work
run_sql "${OUT}/pre.sql" | grep -E 'alembic|long_xacts|runs_in_flight'     # 0180 / 0 / 尽量小
kubectl -n ${NS} delete job migrate --ignore-not-found
kubectl -n ${NS} apply -f "${OUT}/migrate-job.yaml"
kubectl -n ${NS} wait --for=condition=complete job/migrate --timeout=300s
kubectl -n ${NS} logs job/migrate | tee "${OUT}/migrate-step1.log" | grep 'Running upgrade'
```

判据:
- [ ] `condition met`
- [ ] Job 日志可能是空的(11-04 测试实测 alembic 不往 stdout 打 `Running upgrade`),**以库里的版本号为准**:
- [ ] `run_sql` 再查 `alembic` = `0182_batch1103_columns`

`PGOPTIONS` 让 DDL 等锁最多 5 秒。两条迁移在 alembic 的同一个事务里,超时就整体回滚、库不变,Job 会自动重试 2 次。日志里有 `canceling statement due to lock timeout` 而且 3 次都失败时:等长事务结束,`delete job migrate` 后重新 apply。**这个写法没在 migrate Job 上用过**(libpq 认 `PGOPTIONS`,迁移走的是同步驱动,`env.py` 本身没设锁超时)。

Job 失败(不是锁)时:`kubectl -n ${NS} get pods -l job-name=migrate` 看日志,停下来。此时什么都还没滚,线上仍是旧代码 + 旧库。

迁移完成后到第二步之间,旧代码跑在新库上:两条迁移都只增不删,旧代码不写这些列,新建对话会拿到默认值 `agent_root`,这正是设计意图(0181 头注)。

### 2.2 第二步:`release.sh prod`(25–40 分钟,低峰)

```sh
git status --porcelain              # 仍为空
tools/deploy/release.sh prod 2>&1 | tee "${OUT}/release.log"      # 提示时输入 prod;镜像源按 1.2 记下的带上
```

- [ ] stage 1 三个镜像都建推成功。卡在 pnpm / `uv sync` 属于网络问题,原地重跑;停在 stage 1 时没动过集群。
- [ ] stage 3 / 4:migrate Job 用 `${TAG}` 镜像再跑一次,**这次是空操作**。用 `kubectl -n ${NS} logs job/migrate` 看不出什么(日志通常为空),以 Job `Complete` 且 `alembic` 仍为 `0182_batch1103_columns` 为准。
- [ ] rollout 完成。B-80 收口要等在跑的对话结束,最长约 5 分钟;超时先看 release.sh 打印的判断帮助,别直接回滚。
- [ ] smoke:
  - `OK credential-proxy tag matches control-plane`
  - **`WARN sandbox image pin f2ee1b4a 落后 1 个提交`(f28fb358b,B-262 P1 改了 `runner.py`)属于预期**:`runner.py` 只给本地 docker 后端用,云上的执行脚本由 control-plane 下发(§81,`pending-test-release-acceptance.md:1722`),测试 §86 就是在 `f2ee1b4a` 上过的。
  - `OK sandbox image pulls over the VPC endpoint`
  - `sandbox image cache` 是 OK 或首次 WARN
  - `OK sandbox pool is on its own security group (sg-bp1a9oa7oi3v4s3ccquu)`、`OK sandbox security group still covers cluster DNS and the k8s API`
- [ ] 阶段 6 金丝雀 **PASS**(不是 WARNING)。
- [ ] 把 release.sh 最后打印的「Image tags (previous → new)」整段存好,记录 PR 要用。

**为什么挑低峰**:B-242 / B-262 把沙箱布局换成 `agent-conv-platform`,每个用户的热沙箱在下一次取用时会销毁重建(冷启动十几秒)。滚动期间新旧副本同时在线,同一用户的请求轮流落到两版上时会来回重建,直到旧副本全部下线(§81 发布顺序)。

---

## 段 3:发后验证

### 3.1 当晚点检(rollout 完成后 10 分钟内)

```sh
kubectl -n ${NS} get pods
kubectl -n ${NS} get deploy -o 'custom-columns=N:.metadata.name,I:.spec.template.spec.containers[0].image'
kubectl -n ${NS} get cronjob retention-cleanup -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}{"\n"}'
for p in $(kubectl -n ${NS} get pods -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[*].metadata.name}'); do kubectl -n ${NS} logs "${p}" > "${OUT}/cp-${p}.log"; printf '%s undefcol=%s lacks_agent=%s ro_fail=%s sha_mismatch=%s seed_failed=%s migr_failed=%s\n' "${p}" "$(grep -c -E 'UndefinedColumn|does not exist' "${OUT}/cp-${p}.log")" "$(grep -c 'lacks /agent' "${OUT}/cp-${p}.log")" "$(grep -c 'could not be made read-only' "${OUT}/cp-${p}.log")" "$(grep -c 'does not match its sha256' "${OUT}/cp-${p}.log")" "$(grep -c 'conversation_seed.failed' "${OUT}/cp-${p}.log")" "$(grep -c 'platform_migration.failed' "${OUT}/cp-${p}.log")"; done
cat > "${OUT}/post.sql" <<'EOF'
SELECT 'alembic' k, version_num::text v FROM alembic_version
UNION ALL SELECT 'new_columns', count(*)::text FROM information_schema.columns WHERE (table_name,column_name) IN (('thread_meta','workspace_layout'),('agent_run','started_at'),('user_upload','sha256'),('knowledge_base','profile'))
UNION ALL SELECT 'layout_'||workspace_layout, count(*)::text FROM thread_meta GROUP BY workspace_layout
UNION ALL SELECT 'new_threads_1h_not_conversation', count(*)::text FROM thread_meta WHERE created_at > now()-interval '1 hour' AND workspace_layout <> 'conversation'
UNION ALL SELECT 'sandbox_live_'||layout, count(*)::text FROM sandbox_instance WHERE destroyed_at IS NULL GROUP BY layout
UNION ALL SELECT 'sandbox_destroyed_1h_'||COALESCE(destroy_reason,'-'), count(*)::text FROM sandbox_instance WHERE destroyed_at > now()-interval '1 hour' GROUP BY destroy_reason
EOF
run_sql "${OUT}/post.sql" | tee "${OUT}/post.txt"
```

期望:
- [ ] 应用镜像都是 `${TAG}`,admin-ui 是 `${TAG}-prod`,留存 CronJob 也是 `${TAG}`
- [ ] 每个 pod 的 `undefcol`、`lacks_agent`、`ro_fail`、`sha_mismatch`、`seed_failed`、`migr_failed` 全是 0。`undefcol` 不为 0 时,先看是不是只出现在 2.1 之前、之后不再增长;在增长就停下来查。
- [ ] `alembic` = `0182_batch1103_columns`,`new_columns` = 4
- [ ] `layout_agent_root` ≈ 0.4 的 `threads_total`,`layout_conversation` = 发版后新建的对话(至少有金丝雀那一段)
- [ ] `new_threads_1h_not_conversation` = 0。滚动期间旧 pod 建的对话可能 > 0,属预期,但不能再增长。
- [ ] 新沙箱行是 `agent-conv-platform`,旧布局的热会话 `destroy_reason=layout_mismatch`

### 3.2 金丝雀数据抽查(只用金丝雀产生的数据)

```sh
cat > "${OUT}/canary.sql" <<'EOF'
SELECT t.workspace_layout, r.status, r.completed, r.started_at IS NOT NULL AS has_started_at, r.unresolved_failures
FROM agent_run r JOIN thread_meta t ON t.thread_id = r.thread_id
WHERE r.created_at > now()-interval '1 hour' ORDER BY r.created_at DESC LIMIT 5
EOF
run_sql "${OUT}/canary.sql"
```

期望:金丝雀的 run 是 `conversation | success | True | True | []`。

### 3.3 B-267 老库补画像:只预演(方案「上生产前要做」第 3 条)

release.sh 为什么不做:要花钱,按 B-222 必须先报价、你确认后才执行。
漏了会怎样:发版前建的知识库一直没有画像,目录里不显示 `<profile>`,探测只有关键词、没有向量校准。不坏,只是效果打折。

```sh
kubectl -n ${NS} exec "${POD_NEW:=$(kubectl -n ${NS} get pods -l app.kubernetes.io/name=control-plane --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')}" -- python -m control_plane.knowledge.backfill_profiles | tee "${OUT}/backfill-dryrun.txt" | tail -1
```

把最后一行 `bases=… total_cost_cny=… unpriced_bases=…` 贴给我。
- `bases=0`:没有要补的。
- 否则我报总额,你确认后再执行 `… backfill_profiles --apply --expected-total <总额>`(不在今晚的发布闸门里)。
- `unpriced_bases > 0`:`--apply` 会拒绝,先补价目表。

### 3.4 次日核对(11-05 上午)

```sh
kubectl -n ${NS} get jobs -l app.kubernetes.io/name=retention-cleanup
P=$(kubectl -n ${NS} get pods -l app.kubernetes.io/name=retention-cleanup --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}'); kubectl -n ${NS} logs "${P}" > "${OUT}/retention-1105.log"; grep -c -E 'ERROR|_failed' "${OUT}/retention-1105.log"; grep -E 'upload_files_removed|upload_rows|agent_file_versions_removed' "${OUT}/retention-1105.log" | tail -5
for p in $(kubectl -n ${NS} get pods -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[*].metadata.name}'); do kubectl -n ${NS} logs "${p}" --since=24h > "${OUT}/cp24-${p}.log"; printf '%s not_started=%s mid_build=%s migrated=%s copied_only=%s migr_failed=%s lacks_agent=%s seed_failed=%s\n' "${p}" "$(grep -c 'not started' "${OUT}/cp24-${p}.log")" "$(grep -c 'reason=invalidated_mid_build' "${OUT}/cp24-${p}.log")" "$(grep -c 'platform_migration.done outcome=migrated' "${OUT}/cp24-${p}.log")" "$(grep -c 'platform_migration.done outcome=copied_only' "${OUT}/cp24-${p}.log")" "$(grep -c 'platform_migration.failed' "${OUT}/cp24-${p}.log")" "$(grep -c 'lacks /agent' "${OUT}/cp24-${p}.log")" "$(grep -c 'conversation_seed.failed' "${OUT}/cp24-${p}.log")"; done
run_sql "${OUT}/post.sql"
kubectl -n ${NS} exec -i "${POD_NEW}" -- python3 - < "${OUT}/agent_roots.py"; kubectl -n ${NS} exec "${POD_NEW}" -- df -h /mnt/workspaces
```

期望:
- [ ] 03:23 那次留存作业 Complete、退出码 0。B-271 这一段如果按 `${OUT}/pre.txt` 的 `uploads_live_older_90d` = 0,`upload_files_removed` 应该没有或为 0。
- [ ] `not_started` 不比 `${OUT}/baseline-not-started.txt` 多(§82.5);`mid_build` 有数也正常
- [ ] `migr_failed`、`lacks_agent`、`seed_failed` 都是 0;`migrated` 有数,说明对方的老对话已经开始迁移
- [ ] NAS 的 `df` 增长和 0.5 的估算在同一量级

### 3.5 记录(我来开 PR,你点合并)

- [ ] 记录 PR `chore(deploy): prod newTag ${TAG} — …`,写上:
  - 上一版 control-plane / credential-proxy 是 `5a1600934`、admin-ui 是 `0db5633d3-prod`
  - 沙箱 `daf63cb1 → f2ee1b4a`(JSON patch 只换镜像;安全组仍摘着,等流日志,B-217 隔离未恢复)
  - 迁移 0181 + 0182 先单独跑
  - 回滚命令(段 4)
  - 0.x 的盘点数字(只放聚合)
- [ ] 同一 PR:`production-release.md` 的现状表和历次表
- [ ] `apps/admin-ui/docs-site/guide/changelog.md` 五条「待发布」改为 2026-11-04
- [ ] ROADMAP:本版上线的条目标「生产上线」。B-228 不销案(只是手工绕过,脚本没修)。B-260 的 82.5 用 3.4 的结果补上。

---

## 段 4:回滚

### 单向门(回滚窗口)

| 门 | 什么时候关上 | 回滚之后的样子 |
|---|---|---|
| **B-236 新布局对话**(0181 + 新代码) | 发版后**第一段新对话**出现时 | 代码回滚后,新布局对话退回老视图:自己写在 `threads/<id>/work/` 下的文件看不见;**还能看到同一用户、同一智能体所有对话的 `threads/*/work/`**,串客户的口子重新打开(73.11 演练过)。不丢数据,升回来就恢复 |
| **B-262 P3 老对话迁移** | 发版后**每段老对话第一次新提问**时(CAS 把 `agent_root` 改成 `conversation`) | 改过的值没有改回去的工具。回滚后旧代码不看这一列,照老视图跑:原件还在 agent 根(迁移是复制不是移动),迁移之后新写进 `work/` 的文件看不见。升回来就恢复 |
| **B-242 本地目录隔离** | 发版即生效 | 回滚后热沙箱按布局各重建一次,回到共用 `/tmp`、`~` 的状态 |
| **B-271 上传到期真删文件** | 从 03:23 的作业开始,只删到期的行(生产最早约 11-15 才有) | 删了不能恢复;回滚 Deployment **不会**把 CronJob 退回旧镜像 |
| **B-267 补画像 `--apply`** | 只在你确认之后 | 花出去的钱收不回 |
| 迁移 0181 / 0182 | 2.1 完成时 | **不要 downgrade**:0181 的 downgrade 删掉 `workspace_layout`,再升回来时所有新布局对话都变成 `agent_root`,`/workspace` 指回 agent 根,自己的文件全部看不见;0182 的 downgrade 会删掉已经花钱生成的画像 |

**实际可干净回滚的窗口 = 今晚对方流量进来之前**(只有金丝雀和我们自己的探针时)。过了这个窗口,回滚在机制上仍然是秒级的,但每次回滚都会让已经分开的对话退回共用状态。所以优先用下面的「开关」或「只退一项」,别整版回滚。

### 回滚命令(上一版的 tag 不是同一个)

```sh
tools/deploy/rollback.sh prod 5a1600934 --images control-plane,credential-proxy
tools/deploy/rollback.sh prod 0db5633d3 --images admin-ui
```

`rollback.sh` 只换三个 Deployment 的镜像,留存 CronJob 和 configmap 都留在新值上。对旧代码无害:新列都在,旧代码不读。

### 回滚前后要做的

1. **回滚前**:`kubectl -n ${NS} exec -it <cp-pod> -- python -m control_plane.knowledge.probe_opt_outs | tee "${OUT}/probe-optouts-before-rollback.txt"`。升回来后再跑一次,前有后无的行要在配置页把「每条消息先探测知识库」重新关掉(`production-release.md` §3)。
2. 回滚后,待确认入库的文档不显示预估、不能确认,对它们重新预估即可(同上)。
3. **沙箱镜像不要先退**。代码已经是新版时,退回 `daf63cb1` 会让每次 exec 都以 97 退出。只回代码,镜像留在 `f2ee1b4a`(旧代码 + 带 `/agent` 的镜像在 73.11 演练过)。

### 只退一项

| 退哪一项 | 怎么做 |
|---|---|
| 只想关掉 B-267(先查知识库 / 画像 / 探测) | `kubectl -n ${NS} patch configmap control-plane-config --type merge -p '{"data":{"EXPERT_WORK_KB_FIRST_ROUTING":"false"}}'` 然后 `kubectl -n ${NS} rollout restart deploy/control-plane`。下次 release.sh 会把它恢复成默认的开 |
| 2.1 迁移失败 | 什么都没滚。库在 0180(事务回滚),沙箱已经是新镜像 + 旧代码,金丝雀已验。停发即可,沙箱镜像可以留着 |
| 2.2 stage 1–2 失败 | 没动集群,原地重跑 |
| 2.2 stage 3–6 失败 | 用上面两条 `rollback.sh`。迁移已经是只增不删,不用动库 |
| 1.3 之后、代码之前就要撤 | 同样用 JSON patch 把镜像退回(安全组不动):`kubectl -n default patch sandboxset expert-work-sandbox --type=json -p '[{"op":"test","path":"/spec/template/spec/containers/0/image","value":"crpi-sgadimluo7wm655m-vpc.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:f2ee1b4a"},{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"crpi-sgadimluo7wm655m-vpc.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:daf63cb1"}]' && kubectl -n default delete sandbox -l agents.kruise.io/sandbox-pool=expert-work-sandbox` |

---

## 附 A:发前通知深护智康(`changelog.md` 五条「待发布」)

1. **工作区文件按会话分开(B-236,要看对方代码)**:
   - 智能体在会话里写的文件放在 `threads/{session_id}/work/` 下,5.6 列文件每条新增 `session_id`(智能体级文件为 `null`)。
   - 会话开始时复制过来、之后没改过的文件不列出。
   - **按 `path` 前缀或文件名匹配的代码要能处理新前缀**,`path` 仍然原样回传给下载接口。
   - 发布前的老会话:每段在第一次新提问时迁移,这一轮模型会收到一次性说明「之前 `/workspace` 下的文件现在在 `/agent/` 下」。
2. **同名文档上传不再覆盖(B-262 P0,行为变化)**:同名、内容不同的文件存成 `<原名>-<8 位>.<扩展名>`;同名同内容复用已有文件;下载文件名、告知智能体的文件名都是实际名字。
3. **引用别的会话上传的文档时复制到本次会话(B-262 P1)**:
   - 附件名可能带 `-<8 位>`,复制按一次上传计入容量(满了返回 429)。
   - 新错误码:404 `UPLOAD_SESSION_DELETED`(原会话已删)、409 `UPLOAD_CHANGED`(文件在原会话里被改过)。
4. **上传图片按拍摄方向转正、画质提高,新增像素上限(B-263)**:超过上限返回 413 `UPLOAD_TOO_LARGE`(以前可能是 500)。
5. **排队与执行中的 run 新增开始时间和进度(B-269 / B-270)**:只加可选字段(`queued` 帧的 `ahead_*`、202 的 `ahead`、run 列表的 `started_at` / `heartbeat_at` / `progress` / `queued_behind`)。不需要提前通知,随发布记录。

## 附 B:发前没能核实的地方

- 本版没上过测试(H1)。B-274 的内容不知道(H3)。
- 2.1 的 `PGOPTIONS` 锁超时、`-migrate` 镜像这套做法第一次用。
- 1.3 的「旧代码 + `f2ee1b4a`」组合没有直接验过,靠 1.3 末尾那次金丝雀兜底。
- 0.5、0.7 的脚本没在 pod 上跑过。
- NAS 的容量和配额没核。
- B-261:**待命**(未认领)沙箱的 envd 不带 token 返回 200,可以以 root 起进程(§81.10)。这是已知风险,不挡本版,记在 B-261 待问阿里云。
