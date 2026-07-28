# 沙箱运行时选型:ACS Agent Sandbox 映射分析(2026-07-28)

前提:容器集群已拍板 ACS(测试+生产,杭州;售后工单确认杭州支持 Agent Sandbox,开工首步实测坐实)。本文=现有 sandbox-supervisor 架构 → Agent Sandbox 的逐能力映射、缺口与推荐。两路侦察(16 篇官方文档深挖 / 仓库沙箱子系统接口面)完整证据在 2026-07-28 会话。

## 一、迁移的核心利好:接缝已存在

orchestrator 与 control-plane **全部**经 `SupervisorClient` Protocol(`orchestrator/tools/sandbox.py:126-198`,10 方法:acquire/exec/release/destroy/read_workspace_file/list_workspace_files/write_workspace_file/delete_workspace_file/mark_workspace_deleted/reap)调沙箱。exec_python/bash/read_file/write_file/edit_file/list_dir/read_document 六类工具 + workspace-ingest 全走该 Protocol。

**迁移工程 = 新写一个 `AgentSandboxClient` 实现同一 Protocol**(E2B SDK 打底),上层工具与 control-plane 代理端点零改动。sandbox-supervisor 服务本身大部分退役(生命周期/池/回收/归档下沉到 ACS 平台)。

## 二、逐能力映射表

| 现有能力(file:line 见会话侦察) | Agent Sandbox 对应 | 评估 |
|---|---|---|
| `acquire`(创建/复用,seed_files,限额覆盖) | `Sandbox.create(template=SandboxSet)` 池领取,亚秒级;seed 用 `files.write` 注入 | ✅ 顺滑 |
| `exec`(held-pipe line-JSON,自建 runner.py) | `run_code`(需镜像内置 code-interpreter)或 `commands.run`;须注入 `agent-runtime` sidecar(envd) | 🔧 中等改造:自建 runner → envd 通道 |
| 热会话(per (tenant,user) 进程内 dict,15min idle reaper) | 休眠/唤醒:`timeout`+`on_timeout: pause` 自动休眠,`Sandbox.connect(id)` 自动唤醒(1-10s);(tenant,user)→sandbox_id 映射存 DB | ✅ 语义更好(内存态保留);代价=唤醒 1-10s 尾延迟 |
| warm pool(进程内,pool_size 2) | SandboxSet 预热池,平台控制器补货,`updateStrategy` 滚动升级 | ✅ 下沉平台,多副本问题消失 |
| reaper 空闲回收 | 休眠保留期(Go duration,默认 forever)到期自动删 | ✅ 平台自动 |
| 工作区 = 本机 Docker 卷 per (tenant,user) | **最大缺口**:动态挂载完整验证的只有 OSS 静态 PV(AccessKey、挂载点须空目录、ossfs FUSE 高延迟低随机 IO);NAS 仅参数表提一句;云盘只能建时固化非动态 | ⚠️ 设计题,与存储选型联动 |
| 工作区文件 API(list/read/write/delete,10/25MB 限) | 工作区权威若在 OSS → control-plane **直接走 OSS API**,不需沙箱在场 | ✅ 更好:文件浏览与沙箱解耦 |
| 归档/每日备份(tar.gz→ObjectStore,1.5GiB buffer 上限,DLQ) | 工作区在 OSS → 整块退役,归档=OSS 生命周期规则 | ✅ 大幅简化,1.5GiB 限制消失 |
| 限额(1c/1024MB/128pids/300s,env) | K8s resources.requests/limits + E2B `timeout`(默认 300s);**规格会被"规整"到未公开档位**(0.5c1g 是否可行需实测);pids 无对应(microVM 内自治) | ✅ 基本对应,粒度实测 |
| egress 管控(credential-proxy+HMAC 一次性 token,allowlist/denylist) | TrafficPolicy(L3/4,FQDN 无泛域名)+ SecurityProfile(L7 域名/路径/方法,HTTPS 须显式 TLS 终止)+ 凭据注入(ApiKey/AliyunSTS 占位符替换) | ✅ 平台化更强;是否退役 credential-proxy 为方案阶段设计题 |
| gVisor runsc + read-only rootfs + cap-drop | MicroVM(强于 gVisor 共享内核模型) | ✅ 隔离升级 |
| 镜像 `expert-work-sandbox:dev`(python3.12+LibreOffice+Node,>1GB) | 自定义镜像可用;须兼容 agent-runtime;**镜像缓存必配**(不加速 36s 级拉取) | 🔧 Dockerfile 适配 + 镜像缓存 |
| 单 supervisor URL / node="local" / 进程内状态 | 全部消失(平台托管) | ✅ 第 3 波多节点问题整体解决 |

## 三、缺口与风险清单

1. **工作区性能(头号)**:ossfs FUSE 不适合高频小文件/随机写。我们的写模式=产出型(agent 写结果文件、上传落地),非数据库型,或可接受——**必须实测定案**(pandas 读写、unzip、批量小文件三个场景)。fallback=NAS(POSIX 完整、毫秒延迟;文档提及支持动态挂载但无完整示例,需验证)。→ 与下一道存储选型题合并决策。
2. **公测依赖**:组件版本矩阵(controller≥0.5.8/manager≥0.6.7 等)、E2B SDK 钉版本(<2.25.0,官方配方 e2b==2.24.0 + e2b-code-interpreter==2.7.0)、工单兜底。
3. **唤醒语义**:1-10s 尾延迟;唤醒后长连接不恢复(我们 exec 是请求-响应式、SSE 在平台侧不进沙箱 → 影响小);唤醒可能因库存/欠费失败(要有重建路径)。
4. **休眠态存储计费**:无 30GiB 免费额度,0 起全额(0.0021 元/GiB/h)。per-user 永久保留休眠沙箱会累积成本 → 保留期设有限值(如 24h),过期删沙箱,工作区权威在外部存储所以无损。
5. **E2B 缺口**:upload_url/download_url 预签名不支持(用 files.read/write 或 OSS 直传);logs/metrics/network API 不支持(用 Prometheus 面)。
6. **吞吐配额**:真实新建 1000 Pod/分钟(池领取不受限),我们规模远够;镜像缓存 API 有限速。
7. **Checkpoint 目前仅 filesystem** 且与存储挂载互斥——克隆功能对我们非必需,忽略。

## 四、成本量级

- 运行态(性能型价):1c2g 沙箱 ≈ 0.156 元/小时;活跃 100 用户各开 1 个 = 15.6 元/小时,但配合休眠(空闲即 pause)实际远低。
- 休眠态:CPU/内存 0 费,仅临时存储(如 2GiB ≈ 0.004 元/h/沙箱)。
- SandboxGateway 默认 4c8g×3 副本 ≈ 常驻 12c24g ≈ 1900 元/月(**可调小规格/副本**,一人规模 1-2 副本小规格足够,方案阶段核)。
- 预热池实例大概率按运行态同价计费(文档未明说,池 size 设 1-2 即可)。

## 五、推荐(待拍板)

**主方案:Agent Sandbox + E2B SDK 接入,新写 `AgentSandboxClient` 实现现有 `SupervisorClient` Protocol。**
- 接入方式选 E2B SDK 而非裸 CRD:生命周期语义(create/connect/pause/kill/files/commands)与我们 Protocol 天然对齐,官方推荐路径,车企迁移案例同路。个别 SDK 覆盖不到的(SandboxSet 池配置、TrafficPolicy)用 K8s CR 声明式配置,一次性铺设。
- 兜底:普通 ACS Pod 当沙箱(同 microVM 隔离,K8s API 同一套,损失=亚秒创建/休眠/E2B 糖),公测出问题可退。
- 工作区方案与存储选型题合并决策(OSS 主候选 + NAS fallback,实测定案)。

## 六、方案阶段待设计项(非选型层)

- runner.py exec 协议 → envd/commands.run 的语义对齐(timeout/输出截断 1M chars/bash 包装)
- credential-proxy 去留(SecurityProfile+凭据注入能否全替代;egress token 模型映射)
- (tenant,user)→sandbox_id 映射表与并发 acquire 的 CAS
- 镜像 CI(ACR 企业版?)+ 镜像缓存 + SandboxSet 滚动升级流程
- 沙箱 Prometheus 指标接入现有 observability 栈
- spec.sandbox 死字段清理/接线(resources 等 8 死字段随迁移一并裁决)
