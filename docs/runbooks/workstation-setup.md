# 工作站从零到能发版

> 读者:换了电脑、机器坏了、或新接手发版的人。
> 目标:只凭这个仓库 + 阿里云账号 + GitHub 账号,把一台空机器弄到能跑
> `tools/deploy/release.sh`。**本文只写每样东西从哪来、谁读它,不写任何密钥值。**
>
> 2026-09-07 立此文的原因:发版步骤本身在 `release.sh` 头注和
> [production-release.md](./production-release.md) 里,但「起点」——kubeconfig 从哪下、
> ACR 用什么账号登、`~/.kube/` 里那几个文件是什么——此前只在执行人脑子里。

## 1. 本机工具

发版脚本(`release.sh` / `smoke.sh` / `rollback.sh` / `build-push.sh`)实际调用的只有这些:

| 工具 | 用途 | 备注 |
|---|---|---|
| `git` | 取 tag 基准(short HEAD)、提交记录 PR | |
| `docker` | 构建 + 推镜像 | 必须支持 `--platform linux/amd64`(集群节点是 amd64;Apple Silicon 上不带这个 flag 会推上 arm64 镜像,pod 起不来)。Docker Desktop 默认可用 |
| `kubectl` | apply / rollout / exec | 与集群 minor 版本差不超过 1 |
| `kustomize` | 钉 `newTag` + `apply -k` | 独立二进制,不是 `kubectl -k`(脚本用 `kustomize edit`) |
| `gh` | 记录 PR、查 CI | |
| `base64` / `curl` | smoke 里用 | macOS 自带 |

**开发另需**(跑测试、改前端;发版不需要):`uv`(Python 3.12 工作区)、`node` + `pnpm`(admin-ui)。
镜像里的 Python / Node 由 Dockerfile 自带,本机不装也能发版。

## 2. 三个登录

| 登录 | 怎么做 | 给什么用 |
|---|---|---|
| **阿里云账号** | 浏览器登控制台 | 下 kubeconfig(§3)、拿 ACR 密码、重置 RDS/Redis 密码 |
| **ACR(镜像仓库)** | `docker login crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com`。用户名 = 阿里云账号全名;密码在「容器镜像服务 · 个人版 → 访问凭证 → 固定密码」里设/看 | `build-push.sh` 推镜像前检查 `~/.docker/config.json` 里有没有这个 registry,没有直接拒推 |
| **GitHub** | `gh auth login` | 记录 PR、`gh pr checks` |

登录态都在本机(`~/.docker/config.json`、gh keyring),换机器重登即可,不用备份。

## 3. `~/.kube/` 里的文件

脚本按固定路径找,文件名不能改。

| 文件 | 是什么 | 谁读 | 丢了怎么重建 |
|---|---|---|---|
| `expert-work-test.yaml` | 测试集群 kubeconfig | `release.sh` / `smoke.sh` / `rollback.sh` / [sandbox-image-release.md](./sandbox-image-release.md) | **阿里云 ACS 控制台 → 集群列表 → `expert-work-test` → 连接信息 → 公网 kubeconfig**,整段复制保存。菜单名以控制台当前为准 |
| `expert-work-prod.yaml` | 生产集群 kubeconfig | 同上 | 同上,集群换成生产那个 |
| `expert-work-prod-params.env` | 两行:`PROD_DOMAIN=` / `PROD_LANGFUSE_DOMAIN=` | `release.sh prod` / `smoke.sh prod` | 手写,见 [production-release.md §1.1](./production-release.md#11-本机接线不进-git)。值不加引号 |
| `expert-work-test-params.env` | 测试环境云资源凭据档案:NAS id、OSS 桶/AK/SK、RDS 地址/账号/密码、Redis 地址/密码、ACR、域名、SSL 证书 id | **没有脚本读它**,是开荒时的手工记录 | 非密的部分(地址、桶名、域名)都在 `infra/k8s/overlays/test/*.yaml` 里;密码类从阿里云对应产品页重置,或按 §4 从集群 Secret 捞 |
| `expert-work-test-secrets.env` | 测试集群六个 dotenv Secret 的本地合集 + 登录档案(平台管理员 / 租户管理员 / Keycloak 登录 / Langfuse 全套) | **没有脚本读它** | 按 §4 从集群捞回;登录密码类丢了就在对应系统里重置 |
| `expert-work-test-e2b.key` | 沙箱组件(ack-sandbox-manager)的 API key | 没有脚本读;它的值已进 `control-plane-secrets` 的 `EXPERT_WORK_SANDBOX_E2B_API_KEY` | 从集群 Secret 捞;或 ACS 控制台沙箱组件页 |
| `expert-work-test-token.txt` | 用途未考证(2026-08-10 生成,单值) | 没有脚本读 | 不需要恢复 |

生产开荒后会多出 `expert-work-prod-secrets.env`(§1.4 的本地副本)——同样没有脚本读,同样能从集群捞回。

## 4. 从集群捞回 Secret(唯一真值源是集群,不是本机)

只要 kubeconfig 在,本机任何一份 `*-secrets.env` 都能重建:

```sh
export KUBECONFIG=~/.kube/expert-work-test.yaml
# 六个 dotenv Secret + 金丝雀凭据,逐个反解成 KEY=VALUE 行
for s in control-plane-secrets keycloak-secrets observability-secrets \
         langfuse-secrets credential-proxy-secrets canary-credentials; do
  echo "# --- $s"
  kubectl -n expert-work get secret "$s" \
    -o go-template='{{range $k,$v := .data}}{{$k}}={{$v|base64decode}}{{"\n"}}{{end}}'
done > ~/.kube/expert-work-test-secrets.env
chmod 600 ~/.kube/expert-work-test-secrets.env
```

`control-plane-secret-files` 是文件挂载(内容可空),`acr-pull` 是拉镜像凭据(用 ACR 固定密码重建),都不用进这个文件。

集群 Secret 本身丢了(集群删了)= 回到 [production-release.md §1.4](./production-release.md#14-secrets六个--企微) 按 `secrets.env.example` 重铸,随机密钥全部新生成。

## 5. 测试环境发版惯例(三步,缺一步都算没发完)

```sh
tools/deploy/release.sh test          # build → 钉 newTag → apply → rollout → smoke → canary
```

1. **每次必须 fresh tag**(脚本默认取 git short HEAD)。ACS 的镜像缓存按 tag 命中、不回源查 registry,重推同 tag 等于没发。
2. **smoke 全绿 + canary PASS** 才算发成功;红了 `tools/deploy/rollback.sh test <上一版 tag>`。
   发完打开界面右上角头像菜单 →「关于」,版本 / 环境 / 更新时间应与本次一致(值是发版时烤进镜像的)。
3. **提交记录 PR**:脚本故意把 overlay 的 `newTag` 改动留在工作树不提交。开 PR,标题
   `chore(deploy): test newTag <新 tag>(一句话摘要)`,正文写:上一版 tag → 新 tag、
   本次带上去的 PR 列表、是否含迁移、smoke/canary 输出块。样板:#1396。
   回滚时靠这条记录找上一版 tag。

生产发版:[production-release.md §2](./production-release.md#2-日常发布),惯例相同,多一道交互确认。

## 6. 换机器最少动作(照抄)

```sh
git clone <repo> && cd expert-work
brew install kubectl kustomize gh                # docker 装 Docker Desktop
gh auth login
docker login crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com
mkdir -p ~/.kube && chmod 700 ~/.kube
# 控制台下载 kubeconfig → ~/.kube/expert-work-test.yaml(生产另存 expert-work-prod.yaml)
chmod 600 ~/.kube/*.yaml
tools/deploy/release.sh test --dry-run          # 每一步只打印不执行,通了就是能发
```

## 7. 本机唯一副本(与发版无关,但丢了补不回来)

- `~/expert-work-superpowers-archive-20260821.tar.gz` —— 已收官 program 的执行台账,
  [ROADMAP](../superpowers/ROADMAP.md) 开头指向它。非密,建议放进私有仓库或网盘。
- Claude Code 的项目记忆目录(`~/.claude/projects/<本仓库路径>/memory/`)—— 跨会话教训与
  项目状态。非密。重要结论已陆续进 ROADMAP / runbook,但不是全部。
