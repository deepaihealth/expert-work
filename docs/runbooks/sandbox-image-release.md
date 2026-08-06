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
