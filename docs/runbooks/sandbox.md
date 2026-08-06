# Runbook — Sandbox（sandbox-supervisor + 沙盒容器）

> Stream G.3 故障预案。sandbox-supervisor（Stream F.1）管理沙盒生命周期；
> `exec_python` 工具经它 `acquire / exec / release` 沙盒容器。
> 它故障 = 声明 `exec_python` 的 agent 工具调用失败（但不影响纯 LLM agent）。
>
> 本文档只覆盖 compose/supervisor 后端（`sandbox_backend=docker`）。
> E2B / ACS 集群后端的沙箱镜像发布见 [sandbox-image-release.md](./sandbox-image-release.md)。

## 故障现象

- agent 的 `exec_python` 工具调用报错（supervisor HTTP 不可达 / 5xx）。
- supervisor `/v1/health` 返回 `docker_ok: false`。
- 沙盒容器泄漏：`docker ps` 出现大量未回收的 `expert-work-sb-*` 容器。
- 沙盒启动慢 / 超时（冷启动 acquire 超 `runner_ready_timeout_s`）。

## 诊断

1. **supervisor 存活**：`docker compose ps sandbox-supervisor`；
   `GET /v1/health` —— `status` + `docker_ok`。
2. **Docker daemon**：supervisor 走 docker-out-of-docker（Mini-ADR I-2，挂宿主
   socket）。`docker_ok:false` = supervisor 容器内 `docker` CLI 连不上宿主 daemon
   —— 查 `/var/run/docker.sock` 挂载、宿主 daemon 是否健康。
3. **沙盒镜像**：`docker image ls expert-work-sandbox` —— 镜像不存在则 `acquire`
   会 `docker run` 失败（需预构建，见 infra/README）。
4. **泄漏容器**：`docker ps --filter name=expert-work-sb-` —— 计数异常说明 release/reaper
   没回收。
5. **网络**：沙盒 join `expert-work-sandbox-egress`（`--internal`，F.9）；
   出网故障查该网络与 credential-proxy（见 [credential-proxy.md](./credential-proxy.md)）。

## 处置

- **supervisor 不健康**：`docker compose restart sandbox-supervisor`。
- **`docker_ok:false`**：确认宿主 docker daemon 健康 + socket 挂载正常；
  重启 supervisor 让它重连。
- **沙盒镜像缺失**：
  `docker build -f infra/sandbox-image/Dockerfile -t expert-work-sandbox:dev infra/sandbox-image`。
- **容器泄漏**：supervisor 的 TTL reaper（每 10s 扫 `IN_USE` 孤儿强杀）是兜底；
  reaper 也失效时手动清理：`docker rm -f $(docker ps -aq --filter name=expert-work-sb-)`，
  之后查 reaper 为何没工作（多为 DB 不可达，supervisor 读不到 `sandbox_instance`）。
- **冷启动慢**：M0 无 warm pool（接受 1-3s 冷启动）；持续超时查宿主资源
  / Docker daemon 压力。

## 回滚

supervisor 镜像无状态，回滚 = 部署上一版镜像
（`docker compose up -d sandbox-supervisor`）。
沙盒镜像（`expert-work-sandbox`）有问题 → 重新构建上一版 Dockerfile。

## 升级

`exec_python` 不可用属 P2（不影响纯 LLM agent）；
若伴随宿主 Docker daemon 故障（影响所有容器）→ 按宿主故障升级 P0。
