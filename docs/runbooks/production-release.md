# 生产发布 runbook —— 开荒 + 发布 + 回滚

> PROD-5(2026-08-24)。读者:执行生产首发的运维/负责人。测试环境的既有惯例
> (fresh tag / 记录 PR / smoke)原样沿用,本文只写生产差异与一次性开荒。
> K8s 侧唯一事实源是 `infra/k8s/overlays/prod/`;本文是操作顺序,不是配置副本。

## 发布日

**2026-09-10(周四)** —— 2026-08-31 用户拍板。

改期时间线:原定 08-26 → 08-24 改 08-30 → 08-29 延期 → 08-31 定 09-10。
(勘误:ROADMAP 早前把 08-30 称作「周六」,该日实为周日;旧记录里的星期标注不可信,以日期为准。)

## 拍板记录(2026-08-24)

| 决策 | 内容 | 恢复点 |
|---|---|---|
| ~~单副本首发~~ **改判:多副本首发(2026-08-26 拍板推翻)** | 阻塞项已全清(#1312 PROD-1 跨副本 SSE / #1313 CAS 守卫 / #1314 PROD-9 投递锁 / #1315+#1318 PROD-12 RPM 除法):`replicas-patch.yaml` 已删,control-plane 回 base `replicas: 2`,`EXPERT_WORK_REPLICA_COUNT="2"` 进 configmap-patch(与 spec.replicas 必须同步改,smoke 校验一致) | —(恢复点已兑现) |
| 书面接受单层租户隔离 | 生产库只有 ORM WHERE 一层;RLS 完全惰性。**应用账号必须建成非 superuser、无 bypassrls**,给日后 FORCE RLS 留门 | 发布后第一波捞回 RLS PR B(ROADMAP PROD-3) |
| 告警走企微群机器人 | P0(+@all)/P1/P2 全进「生产告警」群,经 wecom-adapter(PROD-2) | 通道扩展(邮件/电话)按需另议 |

## 0. 资源开通清单(全部齐了才能进 §1)

| 资源 | 要求 | 产出物 |
|---|---|---|
| ACS prod 集群(杭州) | 装 ack-sandbox-manager 组件(记下 adminApiKey)+ ALB Ingress controller | kubeconfig → `~/.kube/expert-work-prod.yaml` |
| RDS PG16 | 三库三账号:应用库 + `keycloak` + `langfuse`,**全部非 superuser** | host + 三组账号密码 |
| Redis 社区版 7.0 | **实例级 `maxmemory-policy=noeviction`**(部署时 `CONFIG GET` 验证);DB 0=平台 / DB 1=Langfuse | host + 密码 |
| OSS bucket | 平台文档/产物 + `langfuse/` 前缀共 bucket;RAM AK/SK | endpoint / bucket / region / AK/SK |
| 通用型 NAS | 挂载点 + 手工建 `/workspaces` 目录(mount 后 mkdir;回收站开 7 天,照 test 勘误 #1144) | 挂载点域名 |
| 域名 ×3 + 证书 | 主域名 / Langfuse 子域名 / 沙箱网关域名;泛域名证书或逐张 | DNS CNAME → prod ALB;证书上传阿里云 |
| 企微「生产告警」群 | 群机器人 webhook URL | 进 `wecom-alert-webhook` Secret(§1.6) |
| **LLM 厂商 prod key ×2** | 主 + **跨厂商**备用各一家,两家都要已开通且有余额 | §1.6.5 在 UI 手工录;§1.6.7 金丝雀 seed 依赖它 |

> **上一行 2026-08-31 补** —— 原表漏了它,而 §1.7 的金丝雀是**发布合格判据**:
> 没有 prod LLM key 就 seed 不了金丝雀,release.sh 阶段 6 只打 WARNING 跳过,
> 等于这次发布没有合格判据。备用必须是**另一家厂商**(挡住主模型 429 误红发布闸)。

## 1. 开荒(一次性,建议发布前一天完成)

### 1.1 本机接线(不进 git)

```sh
# kubeconfig
cp <下载的凭据> ~/.kube/expert-work-prod.yaml
# 域名参数(release.sh / smoke.sh prod 都从这读)
cat > ~/.kube/expert-work-prod-params.env <<'EOF'
PROD_DOMAIN=<主域名>
PROD_LANGFUSE_DOMAIN=<langfuse 子域名>
EOF
# 值不要加引号(脚本按键提取不走 shell 解析;带引号会被烤进 admin-ui 镜像 URL)
```

### 1.2 填 overlay 占位符

**只改 `.yaml`,不要碰 `secrets.env.example`。** 那份 example 不被 kustomize 引用,
是给 §1.4 抄的模板;它里面另有 ~32 处占位符,填的是**本地副本**,原文件保持占位符、
**永不提交**。(2026-08-31 核实并加此警告 —— 原文只写「grep `PROD_PLACEHOLDER` 逐个
替换」,照字面做会把生产密钥提交进 git。)

要填的就 **8 个值 / 15 处**,全在 yaml 里:

| 占位符 | 落在哪 |
|---|---|
| `PROD_PLACEHOLDER_DOMAIN` | ingress / keycloak / configmap 的 OIDC issuer + JWKS(5 处) |
| `PROD_PLACEHOLDER_LANGFUSE_DOMAIN` | langfuse ingress + `NEXTAUTH_URL`(2 处) |
| `PROD_PLACEHOLDER_SANDBOX_DOMAIN` | `EXPERT_WORK_SANDBOX_E2B_DOMAIN` |
| `PROD_PLACEHOLDER_OSS_ENDPOINT` / `_BUCKET` / `_REGION` | 平台 + Langfuse 各一份(共 6 处) |
| `PROD_PLACEHOLDER_NAS_MOUNT_TARGET` | workspace-nas patch |
| `PROD_PLACEHOLDER_ADMIN_EMAIL` | `EXPERT_WORK_BOOTSTRAP_ADMIN_EMAIL` |

`newTag: PROD_PLACEHOLDER_TAG` **不用手填** —— 首次 `release.sh prod` 自动钉,预检也
专门放行它。填完提交 PR(占位符替换是配置变更,走评审)。`release.sh prod` 在构建前
会拒绝任何残留占位符。

### 1.3 集群侧一次性对象

```sh
export KUBECONFIG=~/.kube/expert-work-prod.yaml
# namespace 先行 —— §1.4 的 create secret 都指定 -n expert-work,
# namespace 要到 apply -k 才会出现,所以单独提前建:
kubectl apply -f infra/k8s/base/namespace.yaml
# AlbConfig 监听(prod 变体):照 infra/k8s/cluster/albconfig-listeners-patch.yaml
# 的头注新建 prod 文件(prod 有自己的 ALB 实例与证书 id,勿复用 test 的),
# kubectl patch albconfig alb --type merge --patch-file <prod 文件>
# SandboxSet(namespace 语义见文件头注,by hand,不进 kustomize):
kubectl apply -f infra/k8s/sandbox/sandboxset.yaml
```

### 1.4 Secrets(六个 + 企微)

按 `infra/k8s/overlays/prod/secrets.env.example` 填一份本地副本(**绝不提交**),
切五个 dotenv Secret:

```sh
grep -E '^EXPERT_WORK_' secrets.env | grep -v '^EXPERT_WORK_CRED_PROXY_' > /tmp/cp.env
kubectl create secret generic control-plane-secrets    -n expert-work --from-env-file=/tmp/cp.env
grep -E '^(KEYCLOAK_|KC_DB_)' secrets.env > /tmp/kc.env
kubectl create secret generic keycloak-secrets         -n expert-work --from-env-file=/tmp/kc.env
grep -E '^GRAFANA_' secrets.env > /tmp/obs.env
kubectl create secret generic observability-secrets    -n expert-work --from-env-file=/tmp/obs.env
grep -E '^(DATABASE_URL|SALT|ENCRYPTION_KEY|CLICKHOUSE_|REDIS_CONNECTION_STRING|LANGFUSE_|NEXTAUTH_SECRET)' secrets.env > /tmp/lf.env
kubectl create secret generic langfuse-secrets         -n expert-work --from-env-file=/tmp/lf.env
grep -E '^EXPERT_WORK_CRED_PROXY_' secrets.env > /tmp/cred.env
kubectl create secret generic credential-proxy-secrets -n expert-work --from-env-file=/tmp/cred.env
rm -f /tmp/cp.env /tmp/kc.env /tmp/obs.env /tmp/lf.env /tmp/cred.env
```

第六个是 **`control-plane-secret-files`**(deployment 的文件挂载,`optional:
false` —— 不建则 pod 卡 FailedMount 起不来;sql_encrypted 后端不读它,内容可为
空,但 Secret 对象必须存在):

```sh
: > /tmp/secret-store.env
kubectl create secret generic control-plane-secret-files -n expert-work \
  --from-file=secret-store.env=/tmp/secret-store.env && rm /tmp/secret-store.env
```

企微告警 URL 单独:

```sh
kubectl create secret generic wecom-alert-webhook -n expert-work \
  --from-literal=WECOM_WEBHOOK_URL='<群机器人 URL>'
```

所有随机密钥(`SECRET_ENCRYPTION_KEY` 等)生产**新铸**,绝不复用 test 值。

### 1.5 首次发布(两跑,第一跑预期红)

```sh
tools/deploy/release.sh prod        # 交互确认输入 'prod'
```

= build 双镜像(admin-ui 烤 prod OIDC)→ 钉 newTag → migrate(空库全量)→
apply → rollout → smoke。**第一跑预期在 control-plane rollout 卡住**:prod 直上
`sql_encrypted` 后端,lifespan 启动即解析 OSS `secret://` ref,金库还是空的 →
CrashLoopBackOff。这不是故障,是鸡生蛋:表结构(migrate)已就位,先去 §1.6
seed 金库,再重跑 `release.sh prod --images control-plane`(或直接
`kubectl -n expert-work rollout restart deploy/control-plane`)转绿。smoke 的
公网检查在 DNS 生效前也会红,先看 `/healthz/ready` 与 pods 两项。

### 1.6 应用层 seed(顺序敏感,按编号执行)

1. **Keycloak realm 三件**(漏了登录/首装直接失败,见 deployment.md §6.7):
   - kcadm 给 `expert-work-admin-ui` client 加 `https://<主域名>/*` redirectUris + webOrigins(realm 文件 seed 的是 localhost);
   - kcadm `update users/profile -r expert-work -s unmanagedAttributePolicy=ENABLED`;
   - **轮换 realm 内嵌的 dev 秘密**(base realm 文件带 dev client secret 与 dev 用户密码,base/kustomization.yaml 头注的硬警告):重置 `expert-work-api-internal` client secret(**记下新值,下一步进金库**)、删 dev 用户。

2. **金库 seed 三条**(control-plane 转绿的前提)。pod 起不来,用一次性
   seed pod(同镜像同 env,不跑 uvicorn):

   ```sh
   TAG=<刚发布的 control-plane tag>
   kubectl -n expert-work run vault-seed --restart=Never \
     --image=crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/control-plane:$TAG \
     --overrides='{"spec":{"containers":[{"name":"vault-seed","image":"crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/control-plane:'$TAG'","command":["sleep","3600"],"envFrom":[{"configMapRef":{"name":"control-plane-config"}},{"secretRef":{"name":"control-plane-secrets"}}]}]}}'
   kubectl -n expert-work wait --for=condition=Ready pod/vault-seed --timeout=180s
   # ① KC admin-client secret(值 = 步骤 1 重置出的 expert-work-api-internal client secret):
   kubectl -n expert-work exec vault-seed -- \
     python -m control_plane.seed_keycloak_secret --value '<client secret>'
   # ②③ OSS AK/SK(configmap 的 EXPERT_WORK_OBJECT_STORE_*_REF 两个 secret:// ref
   # 所指;--name 走同一 CLI,PROD-5 加的通用模式):
   kubectl -n expert-work exec vault-seed -- \
     python -m control_plane.seed_keycloak_secret \
       --name expert-work/platform/oss/access-key --value '<OSS AK>'
   kubectl -n expert-work exec vault-seed -- \
     python -m control_plane.seed_keycloak_secret \
       --name expert-work/platform/oss/secret-key --value '<OSS SK>'
   kubectl -n expert-work delete pod vault-seed
   ```

3. **control-plane 转绿**:重跑 §1.5 第二跑,smoke 全绿为准。
4. **首个平台管理员**:configmap 已设 `EXPERT_WORK_BOOTSTRAP_ADMIN_EMAIL`,
   该邮箱首登自动升(兜底走 bootstrap-admin.md break-glass)。
5. **租户开通 + LLM key**:admin-ui 建租户 → 金库粘贴 LLM provider key。
6. **平台技能导入(可选,可发布后补)**:走 §1.8 的批量导出/导入(#1344,
   2026-08-27 起替代旧「52 导出包」手工路径);幂等,不阻塞发布。
7. **金丝雀 seed(X-14 P1,发布合格判据的前置)**:release.sh 阶段 6 需要
   `canary-credentials` Secret + 金丝雀 Agent;未 seed 时该阶段只打 WARNING
   跳过(发布不被打断),seed 后才真正生效。依赖第 5 步(租户 + LLM key)。

   ```sh
   # 在 control-plane pod 里跑(幂等;--model-provider/--model-name 选一个
   # 本环境已配置平台 key 的模型,默认 anthropic/claude-sonnet-4-5;
   # --fallback-provider/--fallback-name 选另一家也有 key 的备用,默认
   # deepseek/deepseek-v4-pro —— 备用挡住主模型 429 误红发布闸):
   kubectl -n expert-work exec -it <control-plane-pod> -- \
     python -m control_plane.seed_canary --tenant-id <第 5 步租户的 uuid>
   # CLI 只打印一次 API key 明文,并给出建 Secret 的确切命令(照抄执行):
   #   kubectl -n expert-work create secret generic canary-credentials \
   #     --from-literal=api-key='<刚打印的 key>' \
   #     --from-literal=agent-code='release-canary'
   # key 明文丢了不可恢复:重跑加 --rotate-key 铸新 key,再重建 Secret。
   ```

### 1.7 金丝雀(发布合格判据)

**已自动化为 release.sh 阶段 6**(X-14 P1):`tools/deploy/canary.py` 在
control-plane pod 里跑一条真 run(exec_python + write_file + save_artifact +
产物下载),end 帧 status=success 且产物字节校验通过才算发布成功;红则提示
rollback.sh。前置是 §1.6.7 的 seed(未 seed 只 WARNING 跳过)。以下手工四步
**保留为兜底**(canary 红了要定位、或 Secret/seed 链路本身出问题时照做;
canonical-agent-e2e-test.md 是全量 SOP,以下是最小闭环):

1. 用 §1.6.5 的租户建一个带沙箱工具的 Agent(或导入 test 环境验证过的配置);
2. 调试台发一条要求「用 exec_python 算个结果,write_file 写 /workspace,
   save_artifact 产出文件」的消息;
3. 断言:run 终态 success、工具卡三个全绿、产物在会话附件里**能下载**;
4. Langfuse 里能看到该 run 的 trace(观测链路活着)。

**任何一步红 = 不对外开放**,rollback.sh 待命。

### 1.8 平台资产搬运(测试 → 生产;基于 2026-08-27 测试库实测行数)

生产库开局是空的,逐项对照(除技能外全部**手工**——密钥与 MCP 地址本就该逐环境配置,批量搬运是反模式):

| 资产 | 测试现量 | 动作 |
|---|---|---|
| 平台技能 | 54(版本 55) | **批量工具**(#1344):测试环境技能页「导出全部」下载 zip → 生产「批量导入」上传 → 弹窗核对逐包结果 + category 抽查。幂等,重跑显示「已跳过」 |
| 模型目录 / 费率卡 | 代码内置 | 随镜像走,零动作 |
| 厂商密钥(platform_provider_secret) | 5 | 生产 UI 手工录 **prod 自己的 key**(金丝雀依赖,§1.6 里已有前置);永不从测试导 |
| MCP 连接器目录 | 2 | 手工建,用 **prod 的 URL/凭据**(deep-ai-health-mcp 生产地址;照搬测试值是错的) |
| 平台 judge / embedding 配置 | 各 1 | UI 手工录 |
| tool-budget / quality / delegation / worker 平台配置、模板市场、知识库 | 0 | 空,零动作 |
| Agent 配置 | 3 | release-canary 走 §1.6.7 seed;**ai-health-plan / sop2-designer 从测试配置页 YAML tab 复制 manifest 手建**,注意把里面引用的 MCP 服务器名对成 prod 注册名,技能引用在批量导入完成后才可解析 |

顺序:技能批量导入 → MCP 连接器 → Agent 手建(依赖前两者)。

## 2. 日常发布

```sh
tools/deploy/release.sh prod            # 确认 'prod';或 --yes 走脚本
```

与 test 同惯例:fresh tag、newTag 变更提交 `chore(deploy)` 记录 PR(**记录里
写上一版 tag** —— X-14 P5,回滚一键可查)、smoke 全绿 + 阶段 6 金丝雀绿后才
算完(金丝雀未 seed 会 WARNING 跳过 —— 先按 §1.6.7 补 seed)。
发布窗口:migrate 是 expand-only 约定(向后兼容一版,deployment.md §10)。

## 3. 回滚

```sh
tools/deploy/rollback.sh prod <上一版 tag>     # 秒级 set image,无确认门
```

带破坏性迁移的版本回滚需要人(rollback.sh 头注);回滚后同样补记录 PR。

## 4. 已接受风险与延后项(首发)

- ~~单副本~~ 2026-08-26 改多副本首发(2 副本),原风险条目作废;取消传播走心跳 CAS
  最长 ~10s(亚秒化=发布后 invalidation_bus 接线)。
- 单层租户隔离(RLS 惰性):发布后第一波。
- ~~金丝雀未自动化~~(X-14 P1 已自动化为 release.sh 阶段 6;P2 钉版卫兵已上
  #1317)、配额维度混扣
  (B-19,给第三方配配额前必修)、RPM 静态除法非全局桶(Redis 令牌桶=发布后,
  弹性扩容需与 `EXPERT_WORK_REPLICA_COUNT` 同步改)。触发器投递 CAS 已修(#1314)。
- retention-cleanup-job / billing-rollup-job / event-log-archive-job /
  audit-backup-worker:**只有代码没有部署物**(infra/k8s 零 CronJob),test 也
  没跑,首发保持一致;retention job 部署前必须先修 X-15①(第二套审批超时)。
