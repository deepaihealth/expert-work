# 沙箱迁移 W1 风险探针实测结果(2026-08-04)

计划 `docs/superpowers/plans/2026-08-03-sandbox-migration-w1.md` Task 3 是 GO/NO-GO 门:三个未验证项任一不通就退兜底(普通 ACS Pod 当沙箱)。

**结论:三项全通,门 PASS,Task 7+ 可以开工。**

过程中改变方案的发现有四条,都写在下面 —— 其中两条(私有协议、`user=` 参数)是没有它们就跑不通的。

## 一、协议选择:走私有协议,不是原生协议

计划和 spec 默认按 E2B 原生协议写的,那条路要求**泛域名 DNS + 泛域名证书**:

```
数据面  <PORT>-<SANDBOX_ID>.DOMAIN
API 面  api.DOMAIN
```

`ConnectionConfig.get_host()` 硬编码 `f"{port}-{sandbox_id}.{sandbox_domain}"`,`/etc/hosts` 给不了通配符 —— 这就是 W0 PoC "API 面通、数据面 502" 的确切原因:hosts 能映射单个 `api.*`,映射不了 `*.*`。

阿里云文档给了第二条路,**私有协议**:

| | 原生协议 | 私有协议 |
|---|---|---|
| 数据面 | `<PORT>-<SANDBOX_ID>.DOMAIN` | `DOMAIN/kruise/<SANDBOX_ID>/<PORT>` |
| API 面 | `api.DOMAIN` | `DOMAIN/kruise/api` |
| DNS | 泛域名 | **单域名** |
| 证书 | 泛域名证书 | 单域名证书 |
| SDK | 原生支持 | **需装 `kruise-agents` 扩展包** |
| 官方定位 | 生产 | 测试验证、快速集成 |

选私有协议的理由:手上的证书是 `*.deepaihealth.com`,**单层**通配符。原生协议下数据面是 `49983-<id>.expert-work-sbx-test.deepaihealth.com` —— 两层,证书不覆盖,得另申。私有协议只要一条 A/CNAME 记录,现有证书直接够用。

集群的 ALB Ingress 本来就同时铺了两套路由(`/kruise/api`→manager、`/`→gateway),所以切协议不用改 Ingress。

**生产要不要换回原生协议是独立决策**(官方把私有协议定位成"测试验证"),换的成本是申一张 `*.expert-work-sbx.deepaihealth.com` 证书 + 客户端去掉 `patch_e2b()`。域名选择对两种协议都成立,不用返工。

### 接入配方

```bash
# 组件侧(控制台 → 组件管理 → ack-sandbox-manager → 配置)
domain = expert-work-sbx-test.deepaihealth.com     # 不带 * 前缀
"Whether to enable TLS for ingress" 不勾            # ALB 只监听 HTTP 80
adminApiKey = <值>                                  # 客户端的 E2B_API_KEY 必须与它一致

# DNS:一条 CNAME
expert-work-sbx-test  →  alb-cv3xokwot6r9bgojzx.cn-hangzhou.alb.aliyuncsslb.com

# 客户端
pip install "e2b==2.24.0" "e2b-code-interpreter==2.7.0"
pip install "git+https://github.com/openkruise/agents.git#subdirectory=sdk/customized_e2b"
export E2B_DOMAIN=expert-work-sbx-test.deepaihealth.com
export E2B_API_KEY=<adminApiKey>
```

```python
from kruise_agents.patch_e2b import patch_e2b
patch_e2b(https=False)      # 必须在 import e2b 之前!
from e2b import Sandbox
```

**`https=False` 是必须的**,别被文档那句"集群外通过 HTTPS 访问时传入 https=True"带偏 —— 签名是 `patch_e2b(https: bool = True, ...)`,默认就是 `True`。我们的 ALB 只监听 80,不传 `False` 会拿 ALB 的 503。

## 二、`commands.run` 必须传 `user="agent"`

不传就是:

```
e2b.exceptions.AuthenticationException: invalid username: 'user'
```

E2B 默认以用户 `user` 执行命令,我们的沙箱镜像是 `USER agent`(uid 10000,`nologin`),没有 `user` 这个账号。W1 Task 1 核对镜像要求时把"官方未表态非 root 用户是否可行"标成了风险项,探针证实风险为真,但**可修且代价极小**:每个 `commands.run` / `files.write` 传 `user="agent"`。

`user="root"` 也不行(`InvalidArgumentException`)。

→ **Task 7/8 实现 `AgentSandboxClient` 时,每一处 `commands.run` 和 `files.write` 都要带 `user=`,并做成常量而不是散落的字面量。**

## 三、三个探针的实测数据

### 探针 1:E2B 数据面经真域名 ✅

```
created id=default--expert-work-sandbox-9ll89   36.8s
user=agent  exit=0  stdout='agent\nHELLO\n2'
```

创建成功、命令执行成功、输出正确。沙箱 id 形态带 namespace 前缀:`default--<sandboxset>-<suffix>`。

### 探针 2:microVM 能否访问集群内 Service ✅

在沙箱里跑(镜像**没有 curl**,用 python urllib):

```
DNS credential-proxy               192.168.66.33
DNS kubernetes.default             192.168.0.1
HTTP proxy /admin/health           http=200
HTTP proxy egress 8081             http=400
HTTP 公网 (baidu)                   http=200
```

集群 DNS 能解析、credential-proxy 两个端口都可达(8081 的 400 是拒绝非 CONNECT 请求,即它活着)、公网也通。

**这意味着不需要 TrafficPolicy** —— 那个 CRD 这个集群根本没有(`kubectl api-resources` 只有 `agents.kruise.io` 那一组),而默认网络本来就通。计划里"铺 TrafficPolicy 放行到 credential-proxy"这一步作废。

### 探针 3:gateway 吞吐 ✅

gateway 是 1 副本 2c4Gi:

```
n=1  wall= 38.5s  ok=1  err=0  max=38.5s  min=38.5s
n=3  wall= 35.3s  ok=3  err=0  max=35.3s  min= 0.0s
n=5  wall= 39.3s  ok=5  err=0  max=39.2s  min=20.6s
```

并发 5 零失败,且 wall clock 不随并发线性增长 —— **gateway 不是瓶颈**,瓶颈是沙箱冷启。`min=0.0s` 是池领取命中,几乎瞬时。

对我们的规模,gateway 保持 1 副本 2c4Gi 够用。

## 四、镜像缓存的价值被这组数据坐实

冷启 35~40 秒,几乎全花在拉 2.46GB 镜像上(池内领取是 0.0s)。官方实测 1.34GB 镜像不加速 36s、加速后 4s,量级对得上。

**勘误(2026-08-07,W2 Task 1 核对现行文档后改正)**:本节原表述"官方称邀测阶段需白名单/工单"已过时。ACS 镜像缓存**无需开通**,单地域默认配额 200(免费),超了才需要工单;创建走控制台「镜像缓存」页或 OpenAPI,是平台侧对象(非集群内 CRD——这个集群没有 `eci.alibabacloud.com` API 组不说明任何问题,只是因为它本来就不是集群内资源)。沙箱池扩容默认吃镜像缓存(`ops.alibabacloud.com/update-with-image-cache` 默认 `false` = 预热池扩容时使用缓存),SandboxSet 模板零改即受益。详见 `docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md` § 一.2。

## 五、E2B SDK 2.24.0 的真实签名(给 Task 7/8)

计划里按公开文档写的形态与实际有出入,以这里为准:

```python
AsyncSandbox.create(
    template: str | None = None,
    timeout: int | None = None,
    metadata: dict[str, str] | None = None,
    envs: dict[str, str] | None = None,
    secure: bool = True,
    allow_internet_access: bool = True,
    mcp: McpServer | dict | None = None,
    network: SandboxNetworkOpts | None = None,
    lifecycle: SandboxLifecycle | None = None,
    volume_mounts: dict[str, AsyncVolume | str] | None = None,
    **opts: Unpack[ApiParams],
) -> Self
```

`domain` / `api_key` / `api_url` / `debug` 都在 `**opts`(`ApiParams`)里,不是显式形参。环境变量对应 `E2B_DOMAIN` / `E2B_API_KEY` / `E2B_API_URL` / `E2B_SANDBOX_URL` / `E2B_DEBUG`。

几个后续波次用得上的参数:

- `network: SandboxNetworkOpts` — 字段 `allow_out` / `deny_out` / `rules` / `allow_public_traffic` / `mask_request_host`。**E2B 自己的出网策略**,可能部分替代 credential-proxy 的 allowlist 职能;是否被阿里云实现未验证
- `volume_mounts` — 波 2 挂 NAS 可能走这里,而不是 CR 注解
- `lifecycle: SandboxLifecycle` — 休眠/保留期,Task 7 会用
- `allow_internet_access: bool = True` — 默认放开公网(探针 2 印证)

`ConnectionConfig` 的逃生舱:`api_url` 覆盖 API 面、`sandbox_url` 覆盖数据面(`get_sandbox_url` 第一行就 return 它)、`debug=True` 走 http + localhost。`envd_port = 49983`。

## 六、给后续任务的待办

| # | 事项 | 归属 |
|---|---|---|
| 1 | `commands.run` / `files.write` 一律传 `user="agent"`,做成常量 | Task 7/8 |
| 2 | 依赖里加 `kruise-agents`(GitHub 源码装,无 PyPI 版本锁 —— 记为供应链风险) | Task 7 |
| 3 | `patch_e2b(https=False)` 必须在 import e2b 之前,这对模块导入顺序有硬要求,得在 `AgentSandboxClient` 模块里处理干净 | Task 7 |
| 4 | 沙箱镜像没有 `curl`,agent 用 `bash` 工具时要知道 | 文档/Task 11 |
| 5 | ~~提工单开通 ImageCache~~ 勘误(2026-08-07):无需开通,控制台/OpenAPI 直接建;`replicas` 保持 ≥1 仍是常规建议(让常见路径走池领取而非冷启) | 运维 |
| 6 | 生产是否换回原生协议(需泛域名证书) | 波 4 / 上线前 |
| 7 | SandboxSet 现在在 `default` namespace,门过了可以考虑挪进 `expert-work` | 波 4 |
