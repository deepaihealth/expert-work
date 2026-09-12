# 工作区按 agent 分层 —— 存量搬迁 runbook（B-50）

> 一次性搬迁：把每个用户扁平的 `/workspace` 搬成
> `agents/<agent_key>/…` + `shared/…`。**发版之后立刻跑**，不是择日再跑。
>
> 脚本：`tools/persistence/migrate_workspace_agent_scoping.py`
> （库 + CLI 双形态，**默认 dry-run**，`--apply` 才真搬）
> 设计：`docs/superpowers/specs/2026-09-10-workspace-agent-scoping-design.md` §七

## 为什么必须紧接着发版跑

`release.sh` 跑完之后、搬迁跑完之前，这段窗口里：

| 面 | 现象 |
|---|---|
| 控制台工作区浏览 | 按 agent 分组后每组都是空的（文件还在扁平根） |
| 对外 `GET .../workspace/files` | `scope=agent` 与 `scope=user` 都返回空列表 |
| 对外 `GET .../workspace/file` | 404 |
| agent 自己的 run | **不受影响** —— PR3 的迁移期读回落兜着（找不到就回落用户根一次） |
| 产物 / 附件接口 | **不受影响** —— 走数据库字段，不依赖文件位置 |

所以窗口只伤「浏览 / 下载」，不伤 run。但它是分钟级还是天级，取决于你是不是
接着就跑了搬迁。

## 前置

### 0. `0154` 必须已经跑过

`release.sh` 的 stage 3 会 `alembic upgrade head`，正常情况下这条自动满足。
**脚本自己也会断言**（`assert_backfill_ran`）：`artifact` 表非空而
`agent_key` 全是空串时直接拒绝开工。

为什么值得单独挡一道：`0154` 没跑时反推链断在第一跳，本可归属的产物会被
**静默**扫进 `shared/` —— 不报错、文件总数照样守恒、报告看起来完全正常，
只有事后人工比对才看得出来。这是本次搬迁唯一一种「跑完了但结果是错的」
失败形态。

### 1. 拿到 NAS 根路径与用户清单

```sh
export KUBECONFIG=~/.kube/expert-work-test.yaml   # 生产换 -prod.yaml
export NS=expert-work
POD=$(kubectl -n $NS get pod -l app.kubernetes.io/name=control-plane -o name | head -1)
```

> 选择器的键是 `app.kubernetes.io/name`(本仓库统一用它)。写成 `app` 那个短键会
> 选不中任何 pod,而后面 `exec` 报的
> `pod, type/name or --filename must be specified` 完全看不出是选择器的锅。
> `tools/deploy/test_runbook_pod_commands.py` 会拦住这类漂移。

工作区根在控制平面 pod 里挂着,**是 `/mnt/workspaces`**(NAS `:/workspaces` 的挂载点,
`{tenant}/{user}` 就在它下面)。不确定时当场问 pod,别照记忆填:

```sh
kubectl -n $NS exec "$POD" -- sh -c 'mount | grep -i nas'
# …:/workspaces on /mnt/workspaces type nfs (…)
```

用户清单从库里取（跑过 run 的用户）：

```sh
kubectl -n $NS exec -i "$POD" -- python3 - <<'PY'
from control_plane.settings import Settings
import psycopg
with psycopg.connect(Settings().db_dsn.replace("+asyncpg", "")) as c, c.cursor() as cur:
    cur.execute("""
        SELECT tenant_id, user_id, count(*) AS threads,
               count(DISTINCT agent_name) AS agents
          FROM thread_meta
         WHERE user_id IS NOT NULL
         GROUP BY 1, 2
         ORDER BY agents DESC, threads DESC
    """)
    for row in cur.fetchall():
        print(*row, sep="\t")
PY
```

**多 agent 的用户排在前面** —— 他们是唯一会产生 `shared/` 内容的那一档，
先看他们的 dry-run。

## Step 1 — 空跑，逐个用户看报告

```sh
kubectl -n $NS exec -i "$POD" -- python3 -m tools.persistence.migrate_workspace_agent_scoping \
  --root /mnt/workspaces --tenant "$TENANT" --user "$USER"
```

输出形如：

```
DRY-RUN tenant=… user=…
  moved     412
  to shared 23
  untouched 0
  artifact rows updated 0
  ⚠️ 2 file(s) went to shared/ because the agent dir already holds a newer copy — review before deleting anything:
      MEMORY.md
      style/PLAN_STYLE.md
  files with no inferable owner (→ shared/):
      …
```

三个数**互不重叠**，相加等于这个用户的文件总数。

### 要看什么

- **`to shared` 是否可解释。** 单 agent 用户应该是 0；多 agent 用户应该只有
  根级散文件（`MEMORY.md` / `style/` / `客户案例/` 那批无登记行的）。
  如果一个单 agent 用户出现非零，**停下来** —— 多半是 `0154` 的回填有问题。
- **`⚠️ conflicts` 那一段。** 它意味着 agent 目录里已经有一份同名的、更新的
  副本（PR3 上线后「读老的、写新的」造成的）。脚本**不覆盖**，把老的放进
  `shared/`。这批值得人工看一眼两份的差异，但不阻塞搬迁。
- **`untouched` 里应该只有 `skills/`** 和（重跑时）已经在终态的东西。

## Step 2 — 真搬

确认 dry-run 的数解释得通之后：

```sh
kubectl -n $NS exec -i "$POD" -- python3 -m tools.persistence.migrate_workspace_agent_scoping \
  --root /mnt/workspaces --tenant "$TENANT" --user "$USER" --apply
```

脚本是**幂等**的：重跑一遍会重新 plan，已经在终态的东西进 `untouched`，
`moves` 为空。中途失败直接重跑，不需要先清理。

## Step 3 — 验收

对每个搬过的用户：

```sh
kubectl -n $NS exec -i "$POD" -- sh -c '
  R=/mnt/workspaces/'"$TENANT/$USER"'
  echo "总数: $(find $R -type f | wc -l)"
  echo "--- 顶层 ---"; ls -1 $R
  echo "--- agent 子树 ---"; ls -1 $R/agents 2>/dev/null
  echo "--- shared ---"; find $R/shared -type f 2>/dev/null | head -20
'
```

判据：

1. **搬迁前后 `find | wc -l` 相等**（搬迁不产生也不消灭文件）
2. `agents/` 下每个目录都非空,且目录数 **≤** 该用户用过的 agent 数;差额要能解释
   —— agent 有会话却一个文件都没留下是常态(2026-09-13 实见:某用户的
   `release-canary` 有 9 个 run、0 条产物行、0 个文件)。照字面读成「每个用过的
   agent 都必须有目录」会把正常情况判成失败。差额的判法:那个 agent 名下
   `artifact` 行数与 `.tool_results/<run_id>/` 目录都为 0,就是真没东西
3. `shared/` 里是报告列出的那批，没有别的
4. `skills/` 还在原位

再从控制台起一轮对话，验隔离真的成立：

5. 用 A agent 起一轮，让它 `list_dir(".")` —— **看不到** B agent 的目录
6. 让它 `read_file("shared:style/PLAN_STYLE.md")` —— 读得到

第 5 条是这次改动的**目的本身**；前四条只证明「文件没丢」。

## 出了问题怎么办

搬迁只做 `os.replace`（同一文件系统内的原子改名，不读内容、不改权限位），
没有删除、没有覆盖。所以「搬错了」的最坏情况是文件在错误的目录里，
不是文件没了。

- **某个用户搬岔了**：手工 `mv` 回去，或者从 `shared/` 里取回。
  文件内容与权限位都没动过。
- **整批要回退**：没有自动回退。按 dry-run 报告里的 `moves` 反向 `mv` 即可
  —— 所以**把每次 `--apply` 的输出留档**。
- **发现文件真的少了**：先查 `docs/runbooks/volume-restore.md`
  （J-29 日备保留 7 天）。
- **跑到一半崩了**：直接**重跑同一条命令**。搬迁是幂等的 —— 已经在终态的文件
  不会再搬,而产物登记行的更新是从「文件现在盘上在哪」反推的,所以第一遍没写成
  的那些行第二遍会补上。（2026-09-13 真栈实见:文件全搬完之后更新登记行那步抛
  `RuntimeError`,235 行全断链;当时的实现只从本轮搬迁表推导更新,重跑算出的
  计划是空的、永远补不回来。已改。）

## 之后

搬迁跑完并验收通过，才能上 PR6（Task 14，摘掉 PR3 的迁移期读回落）。
回落还在的时候，搬岔的文件仍然读得到，问题会被掩盖；摘掉之后才是真隔离。
