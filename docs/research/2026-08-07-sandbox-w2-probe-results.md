# 沙箱迁移 W2 风险探针实测结果(2026-08-07)

> **⚠️ 2026-08-07 追加勘误(见文末「2026-08-07 追加勘误」一节)**:本文档下方
> 「结论」「§ 一」「§ 七待办 #1/#3/#6」给出的"挂载不通,需工单开通特权容器 +
> hostPath 安全豁免"判断,已被同日晚些时候的后续实测**证伪**——工单当天已批复
> 且平台侧前置条件全部满足,真因全在我方沙箱镜像(容器非 root 启动 + 预建了
> `/workspace` 挡住平台建 symlink)。不删除本文档原始记录(它忠实记录了当时的
> 认知与证据),只在原结论处加了醒目指向,完整勘误见文末新增一节。canonical 版本
> 见 `docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md` § 二之二。

计划 `docs/superpowers/plans/2026-08-07-sandbox-migration-w2.md` Task 1 是 Task 4(云后端挂载注入)的前置探针,回答四个问题。上游设计 `docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md` § 一.1 基于阿里云现行文档判断"`csi-volume-config` 已官方文档化、探针只是验证配方,非探生死"——**这个判断在本集群上被推翻**:配方本身没错,但机制被一道我们没开通的安全闸拦住了。

**结论:问题一 NO(机制当前不通,已定位根因,需工单);问题二部分回答(前导斜杠无关已证,相对语义未证——见 § 二);问题四测得未命中基线 53s,加速数据待 Task 8;问题三因问题一未过而无法实测,按计划默认值不变。Task 4 的"云后端挂载注入"在工单批复前不能按当前设计走通,需要先把这件事捅给运维/上层。**
>
> **⚠️ 上面这段"需工单"的结论已证伪,见文末「2026-08-07 追加勘误」**——工单当天即批复生效,真因是我方镜像非 root 启动 + 预建了 `/workspace`。「相对语义未证」这一半也已结案(见勘误)。

## 一、`csi-volume-config` 在池领取路径下是否生效——不生效,EPERM,根因已定位

> **⚠️ 本节"根因已定位"指向的根因(安全闸未开通,需工单)已证伪,见文末「2026-08-07 追加勘误」。** 以下实测记录(EPERM 报错、`fork/exec /mnt/envd/sandbox-runtime-storage: operation not permitted`)本身仍然真实、原样保留——错的是对这条报错**归因**的判断,不是报错本身。真因是我方镜像非 root 启动,与安全闸/工单无关。

### 实测

变体 1(池领取,`subPath="w2-probe/tenant-a/user-1"`)：

```
e2b.exceptions.SandboxException: 500: Internal: failed to perform csi mount:
invalid_argument: error starting process '/mnt/envd/sandbox-runtime-storage
mount --driver nasplugin.csi.alibabacloud.com --config <base64>':
fork/exec /mnt/envd/sandbox-runtime-storage: operation not permitted,
pick sandbox failures: [{"key":"default/expert-work-sandbox-9vfmg",
"reason":"failed to perform csi mount: ... operation not permitted","count":1}]
```

`pick sandbox failures` 里点名的 `expert-work-sandbox-9vfmg` 正是当时池内唯一可用的沙箱——证实池领取路径确实触发了 CSI 挂载尝试(不是被跳过),失败发生在挂载这一步,而且**失败把这个池内沙箱也搭进去了**:`kubectl get sbx` 显示它被打上 `CLAIMED=true` 后随即 `Terminating`,`AVAILABLE` 从 1 掉到 0,SandboxSet 花了几十秒补一个新的。也就是说,每次带 `csi-volume-config` 的 create() 失败,不是"不消耗资源的空跑",而是真会烧掉一个池内沙箱。

### 排除"只是池领取路径的问题"

写了 `probe_cold_vs_pool.py`:先用一个不带 CSI 的 create() 把当时唯一的池内可用沙箱吃掉(不 kill,留着),紧接着(池还没补上,`AVAILABLE=0`)立刻发第二个带 CSI metadata 的 create()。第二次耗时 28.5s(接近冷启基线 35~40s,证实走的是冷建路径,不是复用刚才那个），报的还是**同一个错误**,只是这次点名的沙箱换成了新冷建出来的 `expert-work-sandbox-xz4sx`:

```
B FAILED after 28.5s: SandboxException: 500: Internal: failed to perform csi mount:
... fork/exec /mnt/envd/sandbox-runtime-storage: operation not permitted ...
pick sandbox failures: [{"key":"default/expert-work-sandbox-xz4sx", ...}]
```

**结论:池领取和冷建两条路径行为一致,都失败在同一处。这不是"预建沙箱事后挂载"这个机制本身的问题,是更底层的一道闸。**

### 根因:官方文档写了个我们(和上游设计)都没读到的前提条件

WebSearch 查阿里云现行文档《为Agent Sandbox挂载共享存储》(`help.aliyun.com/zh/cs/user-guide/mount-shared-storage-for-agent-sandbox`,浏览器工具在本环境不可用,以下是搜索引擎摘要,两次独立查询表述一致):

> 启用动态存储挂载功能,需要为容器开放特权容器(Privileged Container)和宿主机路径(hostPath,`/var/run/csi`)的容器安全验证,可以提交工单放开限制。

这条前提条件**在上游设计文档 § 一.1 的"官方文档新事实"核对中被漏掉了**——那次核对得出"申请时动态挂载,机制已经官方化,探针降级为验证配方"的结论,但显然没有覆盖到"特权容器 + hostPath 安全豁免需要工单"这一节。`/var/run/csi` 和我们错误里的 `/run/cnfs/alinas-mounter.sock`、`/mnt/envd/sandbox-runtime-storage` 都是同一类"容器需要越权访问宿主机 CSI 相关路径/执行特权二进制"的操作,EPERM(不是 ENOENT、不是 InvalidArgument 业务校验错误)与"安全策略拦截"这个定性完全吻合。

**旁证**:SandboxSet 的 `spec.runtimes` 已经声明了 `[{name: agent-runtime}, {name: csi}]`(集群现状,不是本次探针加的),按官方文档这是"申请 CSI Sidecar 自动注入"的正确配法,注入本身生效了(`/mnt/envd/sandbox-runtime-storage` 这个二进制确实存在并被尝试执行,不是 "command not found"),只是执行被拒——和"配方对、安全闸没开"这个结论一致,不是我们配置错了 `runtimes`。

### SDK/API 层面没问题(附带验证)

两个变体的 base64 config 解码后,`path` 字段与我们传入的 `subPath` 完全对应(见 § 二),证明 `metadata={"e2b.agents.kruise.io/csi-volume-config": json.dumps(vc)}` 经 `AsyncSandbox.create()` 到 sandbox-manager 再到 CSI 驱动这一路**透传无损**——W1 报告 § 六待办里"钉死 SDK create(metadata=...) 透传无损"这条在 W2 上继续成立,问题不在 SDK/客户端代码。

### 对 Task 4 的影响(重要)

> **⚠️ 本节整节结论已证伪,见文末「2026-08-07 追加勘误」——Task 4 未 gated on 工单,已按原设计走通并交付。** 原始判断保留在下面,仅供留存当时的决策依据。

**Task 4"云后端挂载注入"目前不能按 § 三 设计的配方(`Sandbox.create(metadata={"csi-volume-config": ...})`)在这个集群上跑通**,不是代码问题,是集群未被授予"特权容器 + hostPath `/var/run/csi`"安全豁免。这需要:

1. 提交阿里云工单开通("需要用户承担一定的安全风险"这句官方原话意味着这不是纯技术审批,可能要过安全评审——留出时间);
2. 工单批复后,重跑本探针 Step 2 三变体确认打通,再让 Task 4 按原计划走;
3. 在工单批复前,Task 4 如果要继续推进,只能是"先把 `NasWorkspaceStore`(control-plane 侧,走普通 PVC 挂载,不受此限制影响,见 § 五 佐证)和沙箱侧挂载注入的代码分两步落——先做 control-plane 侧,沙箱侧的接线代码可以写但标注"待打通CSI 后启用",或者干脆本波不做沙箱侧云挂载,退回 W1 遗留的"沙箱侧工作区仍走旧路径"过渡状态。这个决策超出 Task 1 授权范围,留给运维/下一步规划者拍板,这里只负责把事实摆清楚。

## 二、`subPath` 语义——两种写法在协议层完全等价,给 Task 4 的取值指令

两个变体的 create() 请求虽然都因 § 一 的根因失败,但失败发生在**挂载执行阶段**,请求本身已经被 sandbox-manager 完整解析并生成了 CSI 驱动的 protobuf config(嵌在 500 错误消息的 base64 里)——这段数据足以回答语义问题,不需要挂载真正成功。(brief Step 2 要求的跨 Pod 共享验证——用另一个挂 `nas-test-pvc` 的 Pod 读 `probe.txt` 内容比对——因为挂载从未成功、沙箱侧从未写出任何数据而**跳过**,没有东西可比对。)

变体 1,`subPath="w2-probe/tenant-a/user-1"`(不带前导 `/`)解码结果:

```
path: "/w2-probe/tenant-a/user-1"   # sandbox-manager 自动补了前导 /
server: "001qwl4r8snh205ihrs-gcl98.cn-hangzhou.nas.aliyuncs.com"
vers: "3"
```

变体 2,`subPath="/w2-probe/tenant-a/user-1"`(带前导 `/`)解码结果:

```
path: "/w2-probe/tenant-a/user-1"   # 与变体 1 字节级相同
```

**两次解码出的 `path` 字段逐字节相同**——sandbox-manager 会把 `subPath` 规范化成带前导 `/` 的绝对路径,不管调用方传不传这个前导 `/`。

`nas-test-pv` 本身 `spec.csi.volumeAttributes.path` 是 `/`(NAS 根),所以本探针**无法从数据上区分**"`subPath` 相对 PV 的 `path` 字段解析"还是"相对 NAS 根解析"这两种理论(PV path 是根,两种理论算出来的绝对路径必然重合)——要真正分开这两种解释,需要另一个 `path` 不是 `/` 的 PV,当前没有,也不在本任务授权范围内新建。

**对 Task 4 的取值指令**:前导斜杠无关**已证**——`EXPERT_WORK_SANDBOX_WORKSPACE_SUBPATH_PREFIX`(或等价常量)带不带前导 `/` 在协议层零差异,sandbox-manager 会自动规范化,设计文档 § 三给的例子 `subPath: "<tenant_id>/<user_id>"`(不带前导 `/`)可以照抄。但"`subPath` 相对 PV `path` 解析、还是相对 NAS 根解析"这一条**相对语义未证**——`nas-test-pv` 的 `path` 恰好是 `/`,两种理论在本探针数据上无法区分(上段已说明)。生产用的 `workspace-nas` PV 的 `path` 是 `/workspaces`(非根),这才是唯一能把两种理论分开的场景,**建成后必须专项验证**(见 § 七待办),不能把这里"零差异"的结论直接推广到非根 path 的场景。

> **⚠️ 2026-08-07 追加勘误:上面"相对语义未证"这半句已结案。** `workspace-nas` PV(`path=/workspaces`)建成后专项验证:实测拼出的挂载源是 `/workspaces/w2v/tenant-a/user-1` = PV `path` + `subPath`,证实**相对 PV 的 `path` 字段解析**(不是相对 NAS 根)。详见文末「2026-08-07 追加勘误」与 `docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md` § 二之二。

## 三、新目录不存在时自动建还是失败——无法实测,保留计划默认值

因为 § 一 的根因,挂载操作从未真正执行到"检查/创建目标目录"这一步——不管 `subPath` 指向的路径存不存在,报错都是同一个 `fork/exec ... operation not permitted`,发生在真正的挂载调用之前。这题在当前集群状态下**无法回答**。

**对 Task 4 的指令:维持计划默认——`AgentSandboxClient.acquire` 在 create 前经 control-plane 挂载点 `mkdir -p` 目标目录,不要因为本探针跳过这一步。** 待 § 一 的工单打通后,应补测这一项(见 § 六待办)。

## 四、ImageCache 实测——集成用户建缓存的窗口,当前判定"未就绪"

按分工,控制台建缓存的操作由用户并行处理,这里做的是"删池内沙箱触发补池 + 计时 + 看注解"这部分。

```
删除时刻的池内沙箱: expert-work-sandbox-pkd7c
t=53s AVAILABLE=1   # 补池到位耗时(vs W1 基线 35~40s)
```

新沙箱 Pod 的注解里**没有** `image.alibabacloud.com/matched-image-caches` 这个 key(不是空值,是整个 key 都不存在):

```
$ kubectl get pod -l alibabacloud.com/compute-class=agent-sandbox -o json | jq .metadata.annotations
# 只有 network.alibabacloud.com/enable-dns-cache,没有任何 image.alibabacloud.com/* 键
```

`kubectl get imagecaches` 报 `the server doesn't have a resource type "imagecaches"`——符合预期(ImageCache 是控制台/OpenAPI 管理的平台侧对象,不是这个集群里的 CRD,详见勘误 § 五)。

**判定:缓存未就绪**(耗时 53s,比基线还慢,没有命中缓存的痕迹)。按 Task 1 brief 的指示,不死等——**缓存生效后的补测归 Task 8(端到端验收)负责**,届时重复同样的"删池内沙箱→计时→查注解"流程即可。

## 五、清理

- 三次带 `csi-volume-config` 的 create() 全部失败,**没有任何数据真正写到 NAS**(挂载从未成功),`/w2-probe/` 这个目录在 NAS 上不存在,无需清理。
- NAS 根上的 W0 PoC 残渣:经一个挂 `nas-test-pvc`(标准 PVC 挂载,走常规 K8s CSI 供应路径,**不受 § 一 那道 Agent-Sandbox 专属安全闸影响**,这次探针顺带验证了这条路径本身完全正常)的临时 Pod(`crpi-.../expert-work/control-plane:d1a2cdf5` 镜像,`runAsUser: 0` 才有权限删——PoC 残渣文件 owner 是 root,默认非 root 用户删不动)确认并清理:
  - `/tenant-a`(含 `/tenant-a/user-1/f.txt`、`/tenant-a/user-1/sandbox-wrote.txt`)——**已删除**。
  - `/w2-probe`——本来就不存在(§ 一 挂载从未成功),无需删。
- **未清理、需要 flag**:NAS 根上还有一个 `/probe.txt`(15 字节,内容 `nas-write-test`,时间戳与 `/tenant-a` 同批,明显也是 W0 PoC 残渣)。Task 1 brief 明确把清理范围限定在"`/tenant-a` 与 `w2-probe` 这两个路径,别碰其他",这个文件不在授权范围内,**原样保留**,留给 Task 2(或运维)决定是否一并清掉。
- `nas-test-pv` / `nas-test-pvc` 按要求保留,未改动。
- 探针用的临时 Pod(`w2-probe-nas-check`)已删除。
- 沙箱池状态:探针结束时 `kubectl get sbx -n default` 只有 1 个未领取的池内沙箱(`AVAILABLE=1`),是稳态基线,不是泄漏。过程中因 CSI 挂载失败被销毁重建的池内沙箱(variant1 的 `9vfmg`、cold-test 的 `xz4sx`、variant2 消耗的 `72nr8`、imagecache 计时删除的 `pkd7c`)均由 SandboxSet 控制器自动回收/补池,没有手工残留。

## 六、E2B SDK 补充验证

`AsyncSandbox.create(template=..., timeout=300, metadata={...}, domain=..., api_key=...)` 签名与 W1 报告 § 五记录的一致,`metadata` dict 透传到 sandbox-manager 侧无丢字段/无编码问题(§ 二的字节级比对是证据)。`orchestrator.tools.e2b_patch._ensure_e2b_patched(domain=..., api_key=...)` 用法与模块 docstring 描述一致,探针脚本按此调用无异常(唯一的函数名不是 brief 草稿里写的 `ensure_patched()`,是 `_ensure_e2b_patched(*, domain, api_key)`,私有下划线前缀——已按模块实际签名调用)。

## 七、给后续任务的待办

> **⚠️ 下表第 1/2/3/6 行的判断已被 2026-08-07 追加勘误(文末)证伪/结案**,原样保留、逐行加勘误指向,不删除。

| # | 事项 | 归属 |
|---|---|---|
| 1 | ~~提工单开通"特权容器 + hostPath `/var/run/csi`"安全豁免(§ 一根因),这是 Task 4 云端挂载能落地的前置条件~~ → **已证伪**:工单当天即批复生效,平台侧前置条件全部满足,真因在我方镜像(见文末勘误) | ~~运维/上层决策~~ 已结案 |
| 2 | ~~工单批复后重跑本探针 Step 2(尤其变体 3:新目录自动建 vs 失败),当前是唯一因 § 一 被卡住没能实测的问题~~ → 变体 3(新目录不存在时的行为)因真因另有其人,**仍未实测**,不是"工单批复后"的问题;维持 § 三 的计划默认值不变 | Task 4 开工前(结论调整,任务本身仍开放) |
| 3 | ~~用非根 path 的 PV(`workspace-nas`,`path=/workspaces`)专项验证 `subPath` 相对 PV `path` 解析还是相对 NAS 根解析~~ → **已结案**:实测拼出 `/workspaces/w2v/tenant-a/user-1` = PV path + subPath,证实相对 PV `path` 解析(见文末勘误) | ~~Task 4,`workspace-nas` 建成后~~ 已结案 |
| 4 | ImageCache 补测(是否命中、耗时是否降到官方宣称的秒级)| Task 8 端到端验收 |
| 5 | NAS 根残留的 `/probe.txt`(W0 PoC 遗留)如何处理,待决策 → 已由 Task 2 清理(临时 Pod `rm -f /mnt/nas/probe.txt`,见 `task-2-report.md`) | Task 2 或运维(已完成) |
| 6 | ~~工单进度未知时,Task 4 是否要拆成"control-plane 侧先落地(不受影响)+ 沙箱侧挂载暂缓"两步,需要拍板~~ → **已不成立**:真因不是工单,Task 4 按原设计一步走通并交付(commits f44fc57e..507d7ee6) | ~~运维/上层决策~~ 已结案 |

## 八、2026-08-07 追加勘误:上面的"需工单"结论是错的,真因在我方镜像

**不删除上面的原始记录**——它忠实记录了 Task 1 探针当天的实测与推理路径(EPERM
报错、官方文档里"特权容器 + hostPath 安全豁免需要工单"这条前提条件确实存在且
确实被漏读),这个记录本身没有编造。错的是**归因**:探针把 EPERM 完全归给"安全
闸没开",而当天晚些时候工单批复后,挂载**仍然失败**,倒逼出了一次对照实验,才
发现真正的病根另有其人。canonical 记录在
`docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md` § 二之二,本
节是面向这份探针报告读者的对照勘误。

### 工单侧:前提条件已满足

工单白名单当天已批复并生效:新建沙箱 Pod 已注入 privileged 的 `csi-sidecar` /
`csi-agent-sidecar`,hostPath `/var/run/csi`(mount-root)已挂——上面 § 一「根因:
官方文档写了个我们都没读到的前提条件」一节点名的那道安全闸,**已经开了**。平台
侧前置条件全部满足。

### 真因:全在我方沙箱镜像,与工单/安全闸无关

控制器在测试集群做了对照实验(同集群/同 PV/同注解,只换镜像):

| 配置 | 结果 |
|---|---|
| 官方 `code-interpreter` 镜像 | **挂载成功**,0.3s 池领取,`/workspace` 是指向 `/run/csi/mount-root/nas/<hash>` 的 symlink,写入正常 |
| 我方镜像(`USER agent`,uid 10000) | `fork/exec /mnt/envd/sandbox-runtime-storage: operation not permitted`(与本文档 § 一实测的报错逐字节相同) |
| 我方镜像 + pod `runAsUser: 0` | 错误变为 `process error: exit status 1`(helper **已能执行**——EPERM 消失,证明问题不在安全闸) |
| 我方镜像 + root + `mountPath=/mnt/ws`(镜像里不存在的路径) | **成功**,写入读回正常 |
| 我方镜像 + 非 root(uid 10000)+ gid 0 | 仍 EPERM ⇒ gid 0 不够,必须 root |

**真因一:容器必须以 root 启动。** envd 与容器同身份,要 fork/exec NAS 挂载
helper(helper 本身权限位 `rwxr-xr-x`,不是文件模式问题)。官方镜像不设
`USER`(容器 root 启动),`commands.run(user=...)` 再由 envd 降权执行——这正是
W1 已经在用的机制。我们的 Dockerfile 把容器身份也锁成非 root(`USER agent`),
顺带锁死了平台自己需要 root 才能干的活。安全上不亏:ACS 侧的隔离边界是
microVM,不是容器用户;本地 docker 侧容器仍是边界,靠 `docker run --user
10000:10000` 保住非 root(W2 Task 6)。

**真因二:`mountPath` 在镜像里不能预先存在。** 平台是在该路径**建 symlink**
指向 `/run/csi/mount-root/nas/<hash>`,不是往目录上挂。我们的镜像预建了
`/workspace`(且 `HOME`/`WORKDIR`/`MPLCONFIGDIR` 都指它),挡住了平台建
symlink。

两个真因已在 W2 Task 9(`infra/sandbox-image/Dockerfile`)修复并交付:去
`USER agent`、去预建 `/workspace` 与 `WORKDIR`、`HOME` 迁 `/home/agent`。

### 附带实测事实(补全本文档 § 二/§ 三/§ 五 的悬案)

- **`subPath` 相对语义结案**:本文档 § 二当时因 `nas-test-pv` 的 `path` 恰好是
  `/`(NAS 根)而无法区分"相对 PV path"与"相对 NAS 根"两种理论。`workspace-nas`
  PV(`path=/workspaces`,非根)建成后专项验证:对照实验里错误载荷解出的 driver
  config 里,挂载源是 `/workspaces/w2v/tenant-a/user-1` = PV `path` +
  `subPath`,**证实相对 PV 的 `path` 字段解析**,不是相对 NAS 根。§ 七待办表第
  3 行就此结案。
- **NAS 新建子目录属主是 root**,非 root 用户写入被拒——`AgentSandboxClient.acquire`
  在 create 前 `mkdir` 目标目录后还要 `chown`,不能只 mkdir(W2 Task 4 已按此
  实现)。
- **pod 级 `securityContext` 会波及 sidecar**(实测把 `csi-agent-sidecar` 波及
  到 CrashLoop),只能用容器级 `securityContext`,不能在 pod 级设。
- **`commands.run(user="root")` 被平台拒**(`InvalidArgumentException`)——沙箱
  内没有"降级失败就退回 root 执行"这条兜底路径,容器身份和执行身份的降权链路
  必须在 envd native sidecar 层面走通,不能在调用侧绕过。

### 本文档需要跟着订正的具体判断

- § 一标题「不生效,EPERM,根因已定位」——EPERM 报错是真的,"根因"判断是错的
  (已加行内勘误指向)。
- § 一「对 Task 4 的影响(重要)」整节——Task 4 **没有** gated on 工单,已按原
  设计一步走通并交付(commits f44fc57e..507d7ee6,见 `task-4-report.md`)。
- § 七待办表第 1 行(提工单)——不需要,已作废。
- § 七待办表第 3 行(非根 path PV 专项验证)——已结案,见上。
- § 七待办表第 6 行(工单进度未知时两步走的拍板)——不成立,已作废。
- § 七待办表第 2 行(变体 3:新目录不存在时自动建还是失败)**仍然开放**——这
  是唯一一条因为"真因另有其人"而**依然没有答案**的待办,不要被"工单已批复"
  误导以为已经解决;§ 三 的计划默认值(`acquire` 前 `mkdir -p`)继续生效。

### 已知待办(非本次勘误范围,列在此处避免读者遗漏)

- **ImageCache 尚未实测生效**(本文档 § 四:补池 53s,未命中缓存基线),补测归
  Task 8 端到端验收负责。
- **新镜像(含本次勘误里的两个真因修复)要等本分支合并 main 后 CI 自动构建**
  (约 30 分钟),再按 `docs/runbooks/sandbox-image-release.md` 换 SandboxSet
  tag——永不复用已存在的 tag。

## 九、端到端验收清单(测试环境真栈,W2 Task 8)

发布顺序见 `docs/runbooks/sandbox-image-release.md`「波 2 首发步骤」——先确认那
五步(尤其第 1 步 NAS `chmod`)已做完,再跑本清单。清单按 spec
(`docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md` § 八)给出
的顺序设计成一条链,前一项失败后面通常也过不去,建议按顺序跑、卡在哪项就地排查
而不是跳过继续。同场并跑还挂着的 **W1 Task 11 验收**(agent 真跑 `exec_python`
出结果,出网经 credential-proxy 且审计落 `sandbox_egress_audit` 表)——这是 W1
遗留至今未跑的一项,与 W2 无直接关系,但只需要真栈跑一次就能把两波的账一起结清。

```
□ 前端上传文档 → NAS 上 {tenant}/{user}/uploads/ 出现
□ agent read_document 读到内容
□ agent exec_python 写 /workspace/out.txt → 前端工作区浏览可见 + 下载内容一致
□ 工作区浏览不含 skills/、uploads/(reserved 隐藏)且 NAS 用户目录下无技能文件
□ kubectl delete sbx <该用户沙箱> → 再跑 agent → out.txt 仍在(权威在 NAS)
□ 沙箱内 cat /opt/skills/<agent_key>/<skill>/SKILL.md 有内容
□ (W1 Task 11)exec 出网经 credential-proxy,sandbox_egress_audit 表落行
□ 删用户 → purge 成功,NAS 目录留 marker,acquire 被拒
```

逐项提示:

1. **前端上传文档 → NAS `{tenant}/{user}/uploads/`**:直接在 NAS 侧确认落盘
   (临时 Pod 挂 `nas-test-pvc`,或 `kubectl exec deploy/control-plane -n
   expert-work -- find /mnt/workspaces/<tenant>/<user>/uploads`),不要只信前端
   "上传成功"提示。**本清单里唯一会直接暴露"control-plane 非 root
   mkdir/chown 权限"风险的一项**(见运行手册波 2 首发步骤第 1 步的说明)——报
   500 先查那一步的 `chmod` 是否真的做了。
2. **agent `read_document` 读到内容**:验证沙箱侧能读到 control-plane 侧写的
   文件——真栈上第一次交叉验证"NFS 共享"而非"各自本地盘各写各的"。
3. **`exec_python` 写 `/workspace/out.txt`**:同一条链反向验证(沙箱写、
   control-plane 读),下载内容要逐字节比对,不是只看文件名出现在列表里。
4. **workspace 浏览隐藏 `skills/`/`uploads/`**:隐藏规则钉在
   `WORKSPACE_RESERVED_PREFIXES`(`packages/expert-work-persistence/src/
   expert_work/persistence/workspace/layout.py`)。"NAS 用户目录下无技能文件"
   这半句是本波带来的新断言——技能已搬到 `/opt/skills`(沙箱本地盘),NAS 上不
   应该再出现任何 `skills/` 子目录(哪怕会被隐藏也不该有数据),跟"反正隐藏了就
   行"是两回事,两个都要查。
5. **`kubectl delete sbx` 后数据仍在**:直接验证 spec 的核心主张——工作区权威在
   NAS,不在沙箱本地盘。删沙箱不经过 `release()`,模拟节点驱逐/OOM 等非正常
   终止,跟正常释放流程不是同一条路径。
6. **`/opt/skills/<agent_key>/<skill>/SKILL.md`**:验证 per-agent 命名空间落点
   (spec 决策 4、W2 Task 5)。`<agent_key>` 是 agent manifest 名清洗后的值
   (`[^a-zA-Z0-9._-]` → `-`),不是 DB UUID。
7. **egress 审计(W1 Task 11)**:断言表里落的行 `tenant_id`/`user_id` 与本次
   测试身份一致,不是只看"有没有行"。
8. **删用户 → purge**:验证软删闸(W2 Task 4)与既有用户 purge 流程
   (`user_purge.py`)对齐——purge 后 `NasWorkspaceStore.mark_deleted` 在
   `{root}/{tenant_id}/.deleted/{user_id}` 落标记(**不在**用户子树里,
   全分支终审 Critical-1:那棵树整个经 subPath 挂进沙箱,沙箱里的 agent
   能自己写出同名文件),之后同一 `(tenant_id, user_id)` 再 `acquire` 应被
   软删闸(`_reject_if_workspace_deleted`)拒绝,不是静默建出一个新工作区。
   一并验:在 `/workspace` 里手写一个 `.ew-workspace-deleted` 文件**不该**
   让后续 `acquire` 被拒。

### 验收时一并处理的两条已知待办

- **ImageCache 补测**(本文档 § 四:补池 53s,未命中缓存基线)。控制台建缓存
  后,重复 § 四同样的"删池内沙箱 → 计时 → 查
  `image.alibabacloud.com/matched-image-caches` 注解"流程,结果补进 § 四。
- **沙箱侧各项(尤其第 5/6 项)必须在换完新 SandboxSet tag 之后跑**——本波镜像
  改造(root 启动、不预建 `/workspace`,W2 Task 9)要等分支合并 main、CI 构建出
  新镜像(约 30 分钟)后才存在;用旧 tag 跑,沙箱侧挂载会照 § 一 的旧根因原样
  失败(`fork/exec ... operation not permitted`),不代表 W2 本身有问题,换新
  tag 步骤见运行手册「波 2 首发步骤」第 4 步。
