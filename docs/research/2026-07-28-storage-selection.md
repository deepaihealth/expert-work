# 存储选型:OSS / NAS / CPFS / AgenticFS(2026-07-28)

前提:集群=ACS(杭州)、沙箱=Agent Sandbox+E2B SDK 已拍板。存储按**两个职责分别选**,不是三选一。完整调研证据在 2026-07-28 会话(官方文档深挖,价格类数字建议上线前控制台核价)。

## 职责①:平台对象存储(图片/文档/技能资产/归档)→ OSS(拍定)

代码现状:S3 兼容层已就绪(`expert-work-runtime/storage/factory.py`,signature s3v4),全仓实际只用 5 个操作:get_object/put_object/delete_object/list_objects_v2/generate_presigned_url。

**适配点(部署清单)**:
1. **OSS S3 兼容仅支持 virtual-hosted style,不支持 path-style** —— 我们 `use_path_style` 必须配 false(走 auto→virtual-hosted)。MinIO 本地开发惯用 path-style,双环境配置差异要写清。
2. **Signature V4 兼容性有文档冲突**(boto3 V4 与 chunked encoding 耦合的说法)——开工首日跑 PoC:真实 PUT/GET/presigned/list 五操作打通,不许假设兼容。
3. endpoint:内网 `s3.oss-cn-hangzhou-internal.aliyuncs.com`(同地域内网免下行流量费),外网 presigned URL 场景另核。
4. **生命周期规则替代自建归档**:按前缀转低频/归档/删除 + 版本控制(NoncurrentVersionExpiration 控成本);注意归档最短存储 60/180 天的计费底线。
5. 价格量级:标准 ~0.12 元/GB/月;PUT 免费额度 500 万次/月、GET 2000 万次/月,超出 0.01 元/万次(我们规模远在免费额度内)。

## 职责②:沙箱用户工作区(per (tenant,user),10GiB 配额)→ NAS 容量型主候选,PoC 定案

### 候选对比

| | OSS 直挂(ossfs) | 通用型 NAS 容量型 | CPFS | AgenticFS(邀测) |
|---|---|---|---|---|
| Agent Sandbox 挂载 | **唯一官方完整验证路径**(静态 PV/AccessKey/空目录) | 未文档化:`csi-volume-config` 的 pvName 理论可指 NAS PV,需实测 | K8s 仅静态卷,出局 | 未知 CSI 集成 |
| POSIX/写语义 | **弱**:ossfs 1.0 随机写=本地改完整文件重传、不支持并发写同文件;2.0 仅追加写;chmod 不生效、无硬链接 | **完整**:随机写/在线修改/文件锁/硬链接 | 完整 | 完整(POSIX,NFSv3) |
| 配额 | 无目录配额(应用层计量) | CSI `volumeAs: subpath`+`volumeCapacity: "true"`:PVC storage 直接映射子目录硬配额(仅容量型支持);**上限 500 目录/文件系统** | Fileset 自管 | **原生 per-space 配额,50 万 space/地域**(1GiB 起自动扩) |
| 计费 | 0.12 元/GB/月 | **0.35 元/GiB/月,按实际写入弹性**(0 起步) | 3.6TiB 起步 ≈5040 元/月,出局 | 按容量峰值弹性 |
| 延迟 | FUSE 高延迟 | 容量型 10ms/高级型 2ms | 亚毫秒 | 未公开细节 |

### 结论

- **CPFS 出局**:HPC 定位,容量起点 3.6TiB/约 5000 元月,K8s 只有静态卷,数量级错配。
- **主候选 = 通用型 NAS 容量型**:写语义完整(agent 跑 unzip/pandas/随机写不会踩 ossfs 的坑)、真弹性计费、CSI subpath+硬配额官方配方现成(可替代应用层计量;超 500 用户再用应用层计量兜底)。**前提待实测**:Agent Sandbox 的 e2b 挂载注解带 NAS PV 是否可行(未文档化)。
- **备选 = OSS 直挂**:官方唯一验证路径,但写语义弱,只有 NAS 路不通才用,且用之前三场景性能实测(pandas 读写/unzip/批量小文件)。
- **AgenticFS = 工单探路**:官方选型指导页原文"AI Agent 多租户场景,为大规模终端用户提供独立隔离 Workspace → 建议选 AgenticFS"——与我们场景逐字匹配;per-space 原生配额上限 50 万(vs NAS 500 目录),专为此场景设计。邀测阶段,**建议提工单问:杭州开服?CSI/Agent Sandbox 集成?开测资格?** 不依赖它开工,作为 NAS 500 配额上限的未来解法。

### PoC 清单(实施第一步,与沙箱验证合并)

1. 杭州 ACS 集群创建 agent-sandbox 沙箱(坐实售后说的杭州支持)。
2. e2b 注解挂 NAS PV(subpath 模式)→ 可行则 NAS 定案。
3. 不可行 → OSS 静态 PV 挂载 + 三场景性能实测 → 决定 OSS 直挂 or 升级方案(本地盘工作+OSS 同步)。
4. OSS S3 兼容 PoC:现有 storage 层 5 操作 + virtual-hosted style 打通。
5. AgenticFS 工单并行发出。

## control-plane 工作区访问路径(方案阶段细化)

- NAS 方案:control-plane 普通 ACS Pod 挂同一 NAS(RWX,官方完整支持)直接文件系统读写,工作区文件 API 与沙箱解耦。
- OSS 方案:control-plane 走 OSS SDK 直读桶前缀。
- 两案均消除"文件浏览必须经 supervisor"的现状耦合。
