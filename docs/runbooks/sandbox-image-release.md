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
§ 二之二),而 `control-plane` 容器以非 root 身份运行(uid 10002,见
`services/control-plane/Dockerfile` 的 `useradd --uid 10002 ... expert_work` /
`USER expert_work`)。只 `mkdir` 不放开权限的话,`NasWorkspaceStore` 第一次在
`/workspaces` 下建 `{tenant_id}/{user_id}` 子树(即端到端验收第一项"前端上传
文档")会撞 `PermissionError`——这不是新推测,是同一份"NAS 新目录属主 root、非
root 写入被拒"事实(探针报告 § 一)在 control-plane 这一层的必然重现,只是这次
挡的是 control-plane 而不是沙箱。**这一层的权限设置没有随任何一个 W2 Task 的
代码改动自动发生**——之前没有任何一个 Task 报告测过"control-plane 真的能在
`/workspaces` 下建目录"这条,发布后第一次上传文档如果报 500,先查这个。

**为什么是 `1777` 而不是 `777`**(集群实测坐实,W2 Task 4 审查追加):非 root
进程无权 `chown` 成另一个 uid(control-plane 与每个用户沙箱各自的 uid 都不同),
`AgentSandboxClient._ensure_workspace_dir` 给每个用户子目录放权限时也是
`chmod 0o777` 而不是 `chown`(同一个根因,见该方法 docstring)——world-writable
的代价是任何有权限进这棵目录的人都能删掉别人的文件/目录,`1777` 的前导 `1` 是
sticky bit:world-writable 目录里,一个条目只能被它的属主、这个目录的属主、或
root 删除/改名,其他人即使有写权限也删不动。`/workspaces` 根上真的会有多个租户
各自的子树平级摆着,只放 `777` 会让任何一个租户的沙箱理论上能删掉另一个租户的
顶层目录(它们都在同一个 world-writable 父目录下);`1777` 把这条路堵死,又不
需要精确控制每个子目录的属主(那本来就做不到)。

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
