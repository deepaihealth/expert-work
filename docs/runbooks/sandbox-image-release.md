# Runbook — 沙箱镜像发布（E2B / ACS Agent Sandbox 后端）

> 适用 `sandbox_backend=e2b` 的测试集群。沙箱镜像跑在 ACS `SandboxSet` 池里,
> **不在 kustomize overlay 内**——`tools/deploy/release.sh` 不会碰它,
> 发布是独立的手动路径（本文档）。compose/supervisor 后端见 [sandbox.md](./sandbox.md)。

## 镜像从哪来

- CI 自动构建:push 到 main 且触及 `infra/sandbox-image/**`（或 workflow 文件本身）
  → `.github/workflows/sandbox-image.yml` 构建多架构镜像推 ACR,
  tag = `<完整 sha>` / `<sha8>` / `latest`。
- 手动兜底:workflow_dispatch 触发同一 workflow。
- 构建约 30 分钟;workflow 有并发组,多次 push 串行排队。

## 发布步骤

1. **确认镜像已在 ACR**（本地 docker 已 login 同一 ACR）:

   ```bash
   docker manifest inspect crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:<sha8>
   ```

2. **换 tag**:改 `infra/k8s/sandbox/sandboxset.yaml` 中 `containers[main].image` 的 tag。

   **永不复用已存在的 tag**:ACS 镜像缓存按 tag 解析、不回源比对 digest,
   重推同名 tag 集群可能继续用旧层。每次发布都用新 sha tag。

3. **apply + 等池就绪**（SandboxSet 在 `default` namespace,不是 `expert-work`）:

   ```bash
   export KUBECONFIG=~/.kube/expert-work-test.yaml
   kubectl apply -f infra/k8s/sandbox/sandboxset.yaml
   kubectl get sandboxset expert-work-sandbox -n default -w
   # 到 UPDATEDAVAILABLEREPLICAS=1 为止(池空冷启约 30s)
   ```

4. **验证**:CI 的「Contract suite against the real E2B test cluster」连的就是这个集群,
   最近一次 main 全绿即为验证;要单独验证可本地跑契约测试的 e2b 侧。

5. **记账**:sandboxset.yaml 的 tag 改动随 `chore(deploy)` 记录 PR 提交
   （与 overlay newTag 同 PR,先例 #1074/#1075）。

## 回滚

换回上一个 sha tag,重跑第 3 步。镜像无状态,池滚动替换即完成。

## 波 2 首发步骤(NAS 工作区上线,一次性)

> 沙箱迁移波 2(`docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md`)
> 把用户工作区从沙箱本地盘搬到 NAS(`workspace-nas` PV/PVC,§ 三),技能文件搬到
> 沙箱本地盘 `/opt/skills`(§ 四),并改造了沙箱镜像(容器 root 启动、不预建
> `/workspace`——W2 Task 9,见本文件上方「发布步骤」)。**这是两条独立发布线的
> 一次性协同上线**:control-plane/admin-ui 走常规 `tools/deploy/release.sh`,
> 沙箱镜像走本文件的「发布步骤」——W2 两条都要走,且顺序敏感,漏一步或调换顺序
> 会导致上传/`exec` 写工作区在发布后立刻失败。按下列顺序执行。

### 1. 在 NAS 上建 `/workspaces` 目录(必须先于 PVC 挂载生效)

`workspace-nas` PV 的 `path` 是 `/workspaces`——NAS 根上的一个子目录。CSI 驱动
只挂载已存在的路径,不会替你新建;PV/PVC apply 早于这一步的话,`control-plane`
挂上去的要么是空挂载点要么行为未定义(探针报告「一、根因」一节里 mountPath 相关
的教训:平台对不存在路径的行为不可预期,别赌它会自动建)。用一个挂 W0 PoC 遗留
`nas-test-pvc`(同一 NAS 文件系统,挂载在 NAS 根 `/`,仍在集群里,见探针报告
§ 五)的临时 Pod 建它:

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: w2-workspaces-mkdir
  namespace: default
spec:
  restartPolicy: Never
  containers:
    - name: mkdir
      image: busybox
      command:
        - sh
        - -c
        - mkdir -p /mnt/nas/workspaces && chmod 1777 /mnt/nas/workspaces && ls -la /mnt/nas
      volumeMounts:
        - name: nas
          mountPath: /mnt/nas
  volumes:
    - name: nas
      persistentVolumeClaim:
        claimName: nas-test-pvc
EOF
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/w2-workspaces-mkdir -n default --timeout=60s
kubectl logs pod/w2-workspaces-mkdir -n default
kubectl delete pod/w2-workspaces-mkdir -n default
```

**`chmod 1777` 不是可选的一步**:集群实测 NAS 新建子目录属主是 root(spec
§ 二之二),而 `control-plane` 容器以非 root 身份运行(方向变更后是 uid
10000,与沙箱镜像的 `agent` 用户同 uid,见
`services/control-plane/Dockerfile` 的 `useradd --uid 10000 ... expert_work` /
`USER expert_work`;方向变更详见
`docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md` § 六)。只
`mkdir` 不放开权限的话,`NasWorkspaceStore` 第一次在
`/workspaces` 下建 `{tenant_id}/{user_id}` 子树(即端到端验收第一项"前端上传
文档")会撞 `PermissionError`——这不是新推测,是同一份"NAS 新目录属主 root、非
root 写入被拒"事实(探针报告 § 一)在 control-plane 这一层的必然重现,只是这次
挡的是 control-plane 而不是沙箱。**这一层的权限设置没有随任何一个 W2 Task 的
代码改动自动发生**——之前没有任何一个 Task 报告测过"control-plane 真的能在
`/workspaces` 下建目录"这条,发布后第一次上传文档如果报 500,先查这个。

**为什么是 `1777` 而不是 `777`**(集群实测坐实,W2 Task 4 审查追加;方向变更
——共享 gid 改统一 uid——之后理由更新,结论不变):`/workspaces` **根**的属主
是 root,`control-plane` 是非 root 进程,无权 `chown` 一个属主是 root 的目录,
只能靠 `chmod` 放宽权限位——这条与两侧是否同 uid 无关,统一 uid 之后依然成
立。`1777` 的前导 `1` 是 sticky bit:world-writable 目录里,一个条目只能被它
的属主、这个目录的属主、或 root 删除/改名,其他人即使有写权限也删不动。
`/workspaces` 根上真的会有多个租户各自的子树平级摆着,只放 `777` 会让任何一
个能写这棵根目录的账号理论上删掉另一个租户的顶层目录(它们都在同一个
world-writable 父目录下);`1777` 把这条路堵死,又不需要精确控制每个子目录的
属主(那本来就做不到)。

（这条与每个**用户子目录**自己的 mode 是两件事——那一层在方向变更之后已经从
`chmod 0o777` 收紧到属主独占的 `chmod 0o700`,见
`AgentSandboxClient._ensure_workspace_dir`:统一 uid 之后不再需要靠
world-writable 兜底跨 uid 访问。`/workspaces` 根本身的属主还是 root、
`control-plane` 还是非 root 这件事没有变,所以根这一层仍然需要 `1777`。）

### 2. apply PV/PVC(base 已含,随常规发布带出)

`workspace-nas` PV/PVC 定义在 `infra/k8s/base/control-plane/workspace-nas.yaml`
(W2 Task 2),已进 `infra/k8s/base/kustomization.yaml`,不需要单独 `kubectl
apply`——下一步的常规发布会带出它。要脱离常规发布单独校验 manifest 是否合法:

```bash
kustomize build infra/k8s/overlays/test | kubectl apply --dry-run=client -f -
```

### 3. 沙箱镜像换 tag(W2 Task 9 改了 `infra/sandbox-image/Dockerfile`)

这是本文件开头「发布步骤」一节要走的**另一条**发布线,不是第 4 步的
control-plane 发布——W2 首发两条都要走。等本分支合并 main 后 CI 自动构建新镜像
(约 30 分钟),按本文件「发布步骤」一节换 `infra/k8s/sandbox/sandboxset.yaml`
的 tag。**永不复用已存在的 tag**(本文件已有的规则,W2 同样适用——W2 Task 9
改了 Dockerfile,旧 tag 对应的镜像仍是改造前的"非 root + 预建 `/workspace`"
版本,沙箱侧挂载会照 § 一 的旧根因原样失败)。

> **为什么镜像在前、control-plane 在后**(W2 全分支终审 I-3 改的顺序)。
> 这两步之间必然有一段沙箱工具不可用的窗口,方向由顺序决定,**要做的是把
> 窗口压到最短**:
>
> | 顺序 | 中间态 | 症状 | 窗口长度 |
> |---|---|---|---|
> | 先 control-plane(**旧顺序**) | 新 control-plane 注入 `csi-volume-config` + 旧镜像(`USER agent` + 预建 `/workspace`) | § 二之二 两个真因原样重现,**每一次 acquire 都失败** | 等 CI 构建 ≈ 30 分钟 + 等池就绪 |
> | 先镜像(**现顺序**) | 新镜像(不预建 `/workspace`)+ 旧 control-plane(不注入挂载) | 沙箱里没有 `/workspace`,`exec` 的 `cwd` 踩空 | 一次 `release.sh` 的分钟级 |
>
> 两个中间态都是"沙箱工具不可用",没有哪个更安全;差别只在持续多久。先换
> 镜像 tag、SandboxSet 就绪后**立刻**走第 4 步,窗口就是一次常规发布的时长。
> **这段时间沙箱工具报错属预期**,不要据此回滚——回滚只会把窗口拉长。

### 4. `tools/deploy/release.sh` 常规发布(control-plane / admin-ui)

第 3 步的 SandboxSet 回到 `UPDATEDAVAILABLEREPLICAS=1` 之后**立刻**执行这一
步(见上方窗口说明)。走常规发布路径,带上 W2 新增的三个配置项(已在
`infra/k8s/overlays/test/configmap-patch.yaml` 里,零手工步骤):
`EXPERT_WORK_WORKSPACE_NAS_ROOT` / `EXPERT_WORK_SANDBOX_WORKSPACE_PV_NAME` /
`EXPERT_WORK_SANDBOX_WORKSPACE_SUBPATH_PREFIX`。新镜像含 W2 全部代码
(`NasWorkspaceStore`、技能 per-agent 落点、软删闸)。

前两项**漏配会在进程启动时直接 `RuntimeError` 点名**(W2 终审 I-2:Task 9
之后"不配 = 波 1 行为"不再成立),不会静默降级成一个 exec 全废的 Pod。

### 5. 冒烟

```bash
kubectl exec deploy/control-plane -n expert-work -- ls /mnt/workspaces
# 应看到第 1 步建的空目录(还没有 tenant 子树——首次真实上传/exec 之后才会出现)
```

第 4 步的 control-plane 发布完成后,再走
`docs/research/2026-08-07-sandbox-w2-probe-results.md`「九、端到端验收清单」
确认真实 `acquire` 能挂上 NAS、`exec` 能写进去。

## 存量迁移(control-plane uid 10002 → 10000,一次性)

> 方向变更(`docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md`
> § 六)把 control-plane 镜像的 uid 从 10002 改成 10000,与沙箱镜像的 `agent`
> 用户同 uid;工作区目录同时从波 2 的 `0o777`/`0o644`(world-writable/
> world-readable)收紧到 `0o700`/`0o600`(属主独占)。测试集群已经按上一节
> 「波 2 首发步骤」跑了一段时间,NAS 上有真实的租户/用户目录、临时沙箱的
> `_scratch` 子树、软删 marker,属主混着 10002(老 control-plane 建的)和
> 10000(沙箱 agent 建的)。**发布这版镜像之前必须先把这些存量目录/文件的
> 属主统一转成 10000**——不是顺手收紧权限,是下一节要讲的三个真实故障唯一
> 的修法。首次上线、NAS 还是空的(没有任何存量目录)可以跳过整节。

### 1. 为什么必须先迁移

**新镜像对着未迁移的树,三条结构性口子当场就开——第 2 条还会把整个租户的
`acquire` 一起挡死。**

`AgentSandboxClient._ensure_workspace_dir`
(`services/orchestrator/src/orchestrator/tools/agent_sandbox.py`)在每次
`acquire` 都跑一遍 `mkdir(exist_ok=True)` + `chmod(0o700)`;`chmod` 失败(目录
属主还是旧 uid)是**尽力而为**,会被吞掉、写一条 warning,不让 `acquire`
失败——这条自愈是这一版代码专门为迁移窗口加的(该方法 docstring 原话:
"目录还没被 uid 迁移 Job(Task D)接管、属主还是旧 uid 时,chmod 是尽力而
为,失败不影响 mkdir/acquire 本身继续")。所以**已存在的用户,大多数操作
在迁移之前也能凑合跑**——继续吃波 2 遗留的 `0777`/`0644` world 位,新
control-plane 作为 "other" 一样能读写。

真正结构性打不开的口子是三个,都不受上面那条自愈保护:

1. **给已有租户新建用户,直接挡在 mkdir 这一步。**
   `NasWorkspaceStore._open_parent_dir_fd` 首次落盘时的
   `user_root.mkdir(parents=True)` 和
   `_ensure_workspace_dir` 里的 `path.mkdir(...)`(
   **不在任何 try/except 里**,原样冒到外层 `except OSError`)都要求在
   **租户目录**(老 control-plane 建的,`0755`,属主 10002)里新建一个条
   目——`0755` 的 `other` 位只有 `r-x`,没有 `w`。新 control-plane(10000)
   对这个目录是彻头彻尾的 "other",没有写权限,`mkdir` 直接
   `PermissionError`,`acquire` 翻 `SandboxSupervisorError`、`write_file`
   翻 `WorkspacePermissionError` → 500。任何该租户下**还没有工作区目录**的
   用户,发布完成那一刻,第一次 acquire / 第一次上传就会撞上这个——不需要
   等窗口拖长。`_scratch`(临时沙箱的 scratch 子树,契约测试也在用)是同
   一类顶层目录,测试集群里现在属主是裸 `root`(实测,见任务报告),同样
   挡住新建临时沙箱。这也是下面第 2 步的迁移命令按"每一个顶层目录"处理、
   不按"看起来像租户 UUID 的才处理"的原因。

2. **软删闸读不动 `.deleted/`,于是拒掉该租户下的每一次 acquire——前提是
   `{tenant}/.deleted/` 这棵目录已经存在。**
   `{tenant}/.deleted/` 是 `0700`,属主是老 control-plane(10002)。新
   control-plane(10000)对它是彻底的陌生人,连 `stat` 都过不去。而 acquire
   前的软删检查(`_reject_if_workspace_deleted`)是 **fail-closed** 的:
   读不到 marker 就拒绝,不猜。所以该租户下**每一个用户**(不只是被软删过
   的那些)的 acquire 都会翻成
   `SandboxSupervisorError: workspace delete-marker unreadable`。

   **只对"曾经有人被软删过"的租户成立。** 判定读的是
   `os.stat({tenant}/.deleted/{user})`——如果这个租户下从没软删过任何人,
   `.deleted/` 目录本身就不存在,`os.stat` 撞的是 `FileNotFoundError`
   (ENOENT,目录缺失)而不是 `PermissionError`(EACCES,目录存在但读不
   动),闸判定"没被软删"放行,不受这条影响。会被这条挡住的是**曾经软删过
   至少一个用户、因而 `.deleted/` 已经存在**的租户——测试环境目前的几个
   租户是否踩中,看实际有没有软删记录,不能一概而论。

   波及面比第 1 条大得多(在会踩中的租户里)——第 1 条只挡"该租户下还没有
   工作区目录的用户",这一条挡的是**全部**。好在它响亮:错误信息直接点名
   delete-marker 读不动,而不是含混的 404 或 500。

   > **这道闸原本不是 fail-closed 的**,本波顺手改的(commit
   > `64dad897`)。原实现用 `marker.exists()`,而 `pathlib.Path.exists()`
   > 对 `EACCES` 的处理跨版本不一致——实测 3.12.8/3.13.1 抛、**3.14.0 吞掉
   > 返回 `False`**。在 3.14 上那就是 fail-open:已软删的用户在这个窗口里
   > 被静默放行,拿到一个本该拒掉的沙箱,且不留任何异常日志。
   > `requires-python = ">=3.12"` 允许 3.14,所以这不是"以后再说"的事。
   > 改成 fail-closed 之后,同一个窗口的表现从"安静的安全漏洞"变成"响亮的
   > 可用性中断"——**这是有意的取舍**:窗口本来就该短到不出现,真出现时宁可
   > 全挡也不能放错人进来。

   这个口子只有迁移 Job 把 `.deleted/` 的属主也转成 10000 才会关上
   (`.deleted/` 的 `0700` mode 本身不变,见第 2 步)。

3. **软删 / 用户 purge 在这个窗口里对全租户是坏的,不只是"读不动"。**
   `NasWorkspaceStore.mark_deleted`(软删一个用户工作区的唯一写入路径,
   仅被管理员专属的 `POST /v1/users/{user_id}:purge` 端点调用——`user:write`
   是 ADMIN 专属 scope,普通用户到不了)在 `{tenant}/.deleted/` 还是老
   control-plane(10002)建的 `0700` 时,`marker.parent.mkdir(parents=True)`
   撞 `FileExistsError`(目录已存在)→ `created=False` → 跳过
   `os.chmod(marker.parent, 0o700)`(同第 1 条的"只在真正带入存在时才
   chmod"策略)→ `marker.touch()` 本身对着一个新 control-plane 不是属主、
   mode `0700` 的目录,同样撞 `PermissionError` → 翻成
   `WorkspacePermissionError`。也就是说,这个窗口里对该租户下**任何一个
   用户**发起 purge 都会失败,不区分是不是先前已经软删过的用户——与第 2
   条同一个根因(`.deleted/` 属主还是旧 uid),但方向相反:第 2 条是
   "误挡不该挡的 acquire",这一条是"该做的软删做不了"。同样,只有迁移
   Job 把 `.deleted/` 的属主转成 10000 才会关上,不需要单独处理。

三条都不是"发布后再补救"能救的——前两条要迁移 Job 跑完才有写权限/才能读到
真实答案,第 3 条要迁移 Job 跑完 purge 端点才恢复可用,新镜像的代码里没有、
也不该有任何绕过属主检查的备用路径(见第 4 节「为什么不做代码自愈」)。

**反过来,迁移跑得太早、离发布太远,一样会坏,而且坏得更彻底。** 部署这版
镜像**之前**当前正在跑的旧镜像(uid 10002——用第 2 步的
`kubectl get deploy control-plane -o jsonpath=...` 现查,不要假设一个固定
sha,那个值会随每次发布漂移)里 `_ensure_workspace_dir._do()` 的 `chmod`
调用是**没有 try/except 包着的**:

```python
# 旧镜像(uid 10002,本次发布之前),services/orchestrator/src/orchestrator/tools/agent_sandbox.py
def _do() -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)  # noqa: S103 — 见上方 docstring:宽 mode 是跨 uid 无 chown 权限下的刻意取舍,不是疏忽。
```

迁移 Job 把用户目录属主从 10002 改成 10000 之后,如果老镜像还在跑一段时
间,**每一个已存在用户**的下一次 acquire 都会在这句 `chmod` 上撞
`EPERM`——不区分新老用户,波及面比"新镜像早于迁移"那两条加起来还大。这就
是「迁移 Job → 发布」之间的窗口必须尽量短、最好连着做的真正原因:**窗口两头
都是全租户级中断**,只是撞在不同的代码行上——

**这句 `chmod` 只是最先撞上的那一处,不是唯一一处。** 迁移之后目录属主是
10000、mode `0700`,老镜像的 control-plane 进程仍是 10002——对这棵树它是
彻底的 "other",`0700` 把 other 位清零意味着它不止 acquire 里的 `chmod`
会撞 `EPERM`:`NasWorkspaceStore` 直接在同一个进程里做的
`read_file`/`write_file`/`delete_file`/`list_files`(下载、上传、删除、
列文件——不经过 `agent_sandbox.py` 那句 `chmod`,是控制面自己对 NAS 的
文件系统调用)同样对着一个自己不是属主的 `0700` 目录,同样是 `EPERM`/
`EACCES`。也就是说迁移早于新镜像的这个窗口里,老镜像不只是"新 acquire 失
败",而是**已迁移用户的下载/上传/列表/删除全部失败**,和 acquire 一样是
全租户级中断,只是分布在更多个端点上。

| 顺序错法 | 谁受影响 | 撞在哪 |
|---|---|---|
| 新镜像早于迁移 | 该租户**全部**用户的 acquire(第 2 条)+ 新用户的 onboarding(第 1 条) | `.deleted/` 读不动 → `delete-marker unreadable`;租户目录写不进 → `mkdir` `PermissionError` |
| 迁移早于新镜像 | **全部**已迁移用户的 acquire,以及对这些用户工作区的下载/上传/列表/删除(老镜像 10002 对已收紧到 10000:`0700` 的目录彻底是 other) | 老镜像 `_ensure_workspace_dir` 那句无保护的 `chmod` → `EPERM`;`NasWorkspaceStore` 各方法对已迁移目录的读写同样 `EPERM`/`EACCES` |

两边都是响亮的失败(不是错误答案),都能从日志一眼认出来,但都是中断。
**不存在"提前迁移更安全"这种说法**,迁移和发布要当成一个不可拆分的动作去做。

### 2. 迁移 Job

一次性 root Pod,挂 `workspace-nas`(与 control-plane 同一个 PVC,namespace
`expert-work`)。**镜像不要用 `busybox`**:docker.io 在这个集群拉取会超时
(`ErrImagePull` / `dial tcp ... i/o timeout`,验证时实测撞到过,见任务报
告)。改用**当前正在跑的 control-plane 镜像**——它是 ACR 镜像,保证已经能
拉到,而且是 Debian 基础镜像,自带 GNU coreutils/findutils(`find` 支持
`-exec ... +` 批量执行),够用,不用额外找一个新镜像。

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
kubectl get deploy control-plane -n expert-work \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
# 把上面打印的 tag 填进下面 YAML 的 image 字段
```

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: workspace-uid-migrate
  namespace: expert-work
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 0
    runAsGroup: 0
  containers:
    - name: migrate
      image: <上一步打印的 control-plane 镜像 tag>
      command:
        - sh
        - -c
        - |
          set -eu
          rm -rf /mnt/workspaces/_gidprobe /mnt/workspaces/_chgrpprobe
          find /mnt/workspaces -mindepth 1 -maxdepth 1 -exec chown -R 10000:10000 {} +
          find /mnt/workspaces -mindepth 2 -type d ! -path '*/.deleted*' -exec chmod 0700 {} +
          find /mnt/workspaces -mindepth 3 -type f ! -path '*/.deleted/*' -exec chmod 0600 {} +
          echo "--- post-migration ---"
          ls -la /mnt/workspaces/*/ | head -40
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      volumeMounts:
        - name: nas
          mountPath: /mnt/workspaces
  volumes:
    - name: nas
      persistentVolumeClaim:
        claimName: workspace-nas
```

```bash
kubectl apply -f workspace-uid-migrate.yaml   # 或用 cat <<'EOF' | kubectl apply -f - 直接贴
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/workspace-uid-migrate -n expert-work --timeout=120s
kubectl logs pod/workspace-uid-migrate -n expert-work
kubectl delete pod/workspace-uid-migrate -n expert-work
```

`set -eu` 保证任何一步失败(比如某个文件被别的进程锁住)整个 Job 都会以非
`Succeeded` 收场——`kubectl wait` 会超时而不是假装成功,不会带着"迁移了一
半"的树静默过关。

命令本身有两处**没有**照抄 brief 草稿里的原始写法,都在测试环境验证过(方
法:在 `/mnt/workspaces` 下自建一个 `_taskD_dryrun` 前缀、用与真实数据同构
的 mixed-ownership fixture 跑过这四条命令、核对结果、再整棵删掉,细节见任
务报告,**没有对着真实数据跑过**):

- **`chown -R 10000:10000 /mnt/workspaces` 会把根目录本身也扫进去。**
  `/mnt/workspaces` 是 `1777` 属主 `root`(NAS CSI 建的,详见上一节第 1
  步),这是刻意的——多个租户子树在根下平级摆着,sticky bit 防止一个租户
  删掉另一个租户的顶层目录。`chown -R` 从根开始递归会把根自己的属主也改成
  10000,不影响功能(mode 还是 `1777`,world-writable + sticky 位都还
  在),但没必要、不可逆(以后只有 root 能再把它 chown 回去),而且**这不
  是根本来的责任归属**——control-plane 从来不需要拥有这个根,只需要
  `1777` 放行的写权限。改成 `find /mnt/workspaces -mindepth 1 -maxdepth 1
  -exec chown -R 10000:10000 {} +`:对根下每一个条目(租户 UUID 目录、
  `_scratch`、清理前的探针目录……不区分名字模式)分别递归 `chown`,根本身
  不碰。测试环境验证过:这样跑完之后,伪造的"根"目录属主/mode 原样不动,
  子树全部转成 10000。
- **`.deleted/` 要不要碰,答案是"属主要改、mode 不要改"。** `.deleted/`
  现在是 `0700` 属主 10002(老 control-plane 建的,唯一的写者)。迁移之
  后新 control-plane 是 10000,如果 `.deleted/` 的属主不跟着改,上面第 1
  节第 2 条(软删闸拒掉全租户 acquire)原样成立——迁移这一步如果漏了它,
  等于没解决软删闸的问题。但 `.deleted/` 的**权限位**不能跟着通用的"目录 0700/文件
  0600"两条 `find` 走,因为里面的 marker 文件的落地 mode(`0644` 之类,
  由 `Path.touch()` 决定)本来就不是这套设计管的对象——`NasWorkspaceStore.
  mark_deleted` 的注释原话是"这个目录只有一个写者、没有任何 `subPath`
  把它投影进沙箱,不需要 group/other 位,`0700` 的目录本身就是唯一的
  保护"。所以顶层的 `chown -R`(第一条 `find`,递归覆盖 `.deleted/` 和它
  里面的所有 marker 文件)负责属主,后面两条 `chmod` 的 `find` 用
  `! -path '*/.deleted*'` / `! -path '*/.deleted/*'` 把 `.deleted/` 目录
  和它下面的文件排除掉,让 `chmod` 不去动一个本来就该保持原样的 mode。
  测试环境验证过:跑完之后 `.deleted/` 属主变成 10000、mode 仍是
  `0700`;marker 文件属主变成 10000、mode 保持它原来的样子不动。

另外两条命令是幂等/加固性质,原样照抄了 brief 草稿,测试环境同样跑过一
遍确认行为符合预期:已经是 `0600` 的沙箱写入产物(`write_file`/
`_atomic_write` 落的文件)不受影响;`exec_python` 里裸 `open()` 写的老文
件(实测过的 `0666`)会被这次迁移一并收紧到 `0600`——目录已经是属主独占
的 `0700`,收紧文件本身的 mode 是防御纵深,不是这次修复严格要求的,但符合
新设计"leaf 文件 `0o600`"的目标状态,顺手做掉。

### 3. 顺序:迁移 → 发布 → 复验,不能拆开也不能颠倒

1. **迁移 Job**(本节)。
2. `tools/deploy/release.sh` 常规发布,新镜像(uid 10000)。与波 2 首发不
   同,这次**不需要**协调 manifest——共享 gid 方案的 `supplementalGroups`
   已经作废(方向变更,
   `docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md`
   § 六),仓库里也确认没有残留(`grep -rn
   supplementalGroups infra/` 零命中),只需要走常规的镜像发布路径。
3. 复验:参照本文件「5. 冒烟」一节 + 探针报告「九、端到端验收清单」,重点
   过一遍新用户 onboarding(全新用户在一个已有租户下第一次 acquire/上传)、
   软删闸(软删一个用户 → 再 acquire 应该被拒)和用户 purge(对已迁移用户
   发起 `POST /v1/users/{user_id}:purge` 应该成功,不再撞
   `WorkspacePermissionError`),这三条正是第 1 节点名的三个结构性故障,
   迁移前会复现、迁移后应该恢复正常。

三步之间的窗口越短越好——第 1 节已经证明窗口两头都有真实故障(迁移早于发
布 = 全体存量用户 acquire 硬挡;发布早于迁移 = 全租户 acquire 被软删闸拒 +
新用户 onboarding 硬挡),没有"先做哪个更安全"这种选项,只有"两步之间隔多久"的风险
可以控制,所以尽量连着做,不要把迁移 Job 和 `release.sh` 排进两个不同的运
维窗口。

### 4. 为什么不做代码自愈

`_ensure_workspace_dir` 已经在 chmod 失败时优雅降级(见第 1 节),这是刻意
留的一小块自愈——但它只覆盖"目录已存在、只是 mode 需要重新收紧"这一种情
形。再往前多走一步(比如在 `mkdir` 撞上 `PermissionError` 时让代码自己想
办法把租户目录的属主抢过来),需要 control-plane 拥有它现在没有、也不该有
的能力(`chown` 到自己不是属主的目录需要 root/`CAP_CHOWN`——方向变更设计
(`docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md` § 六)
已经论证过"control-plane 以 root 跑技术上可行但不划算",跑时自愈同样是把这个
不划算的选项换了个更隐蔽的位置塞回来)。更根本的问题是:一旦运行期代码里
出现"目录属主不对时,悄悄接管/绕过"的分支,"存量目录属主归谁负责"就从一个
答案(迁移 Job)变成两个答案(迁移 Job + 一段每次 acquire 都会跑的隐藏逻
辑)——下次这类问题重现时,得同时排查两条路径,而且运行期自愈天然没有事
后可核对的执行记录,迁移 Job 的 `kubectl logs` 有。生产还没上线,这是一次
性成本;写进运行手册反而让"上线前必须做这一步"这件事显式可查,而不是靠一
段读者看不见的自愈代码悄悄兜底。
