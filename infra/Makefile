# Expert Work local dev stack — convenience wrapper over docker compose.
#
# 封装常用的「全量 dev 起栈」命令:三个 profile(full/auth/observability)的
# 必需服务一把拉起,且**排除 control-plane-green**(它与 sandbox-supervisor
# 抢 host 8001,见 docs/runbooks 的端口冲突说明)。
#
# 用法:
#   cd infra
#   make dev-up        # 起全栈(admin-ui 能跑所需的一切)
#   make dev-ps        # 看状态
#   make dev-down      # 停(留容器+卷)
#   make dev-clean     # 删容器+网络(留数据卷)
#   make langfuse-reset  # 重置 langfuse 账号/project(改 INIT 值后用)
#   make dev-info      # 打印各 web 地址 + dev 登录凭据

COMPOSE  := docker compose
PROFILES := --profile full --profile auth --profile observability

# 数据层 + 应用(不含 green) + langfuse 栈。keycloak 单列,见 dev-up 注释。
DEV_DATA     := postgres pgbouncer redis minio
DEV_APP      := migrate control-plane-blue sandbox-supervisor credential-proxy
DEV_LANGFUSE := langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio-init langfuse-worker langfuse-web
# --wait 安全的服务集(全部带可靠 healthcheck / 一次性退出)。
WAIT_SERVICES := $(DEV_DATA) $(DEV_APP) $(DEV_LANGFUSE)

.DEFAULT_GOAL := help

.PHONY: help dev-up dev-down dev-clean dev-ps dev-logs dev-build build-sandbox rebuild-sandbox dev-bootstrap-admin dev-reset-admin dev-seed-keycloak dev-keycloak-config langfuse-reset dev-info

help: ## 列出可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

dev-build: ## 重建 control-plane + sandbox-supervisor 镜像(改了后端代码后用;缓存命中则秒级)
	# control-plane-blue 进程内 lazy-import orchestrator(含技能种文件 §5.1);
	# sandbox-supervisor 是独立服务(docker cp 种文件)——两者都要重建,否则
	# 后端改动(如 skill-runtime auto-mount)只半数生效。
	$(COMPOSE) $(PROFILES) build control-plane-blue sandbox-supervisor

# 沙箱执行镜像不是 compose 服务,由 supervisor 运行时 docker run。单一 full
# 镜像(python + 办公/数据/媒体库 + soffice/poppler/ffmpeg/node + CJK,~1.3GB);
# 变体已合并(sandbox-image-consolidation)。host daemon build 一次即可
# (supervisor 经挂载 socket 用同一 daemon)。context=sandbox-image/(本地 COPY)。
# tag 对上 settings 默认值,或用 EXPERT_WORK_SANDBOX_SANDBOX_IMAGE 覆盖。
#
# Debian 源按测速选(B-211):约 280 个 apt 包,默认 mirrors.aliyun.com 够快就用它,
# 慢了换最快的(10-23 办公网阿里云约 30 KB/s)。探针与 tools/deploy/build-push.sh
# 同一个,在容器里跑 = 和 docker build 走同一条网络;探针跑不起来退回阿里云。
# 手动指定:make build-sandbox APT_MIRROR=mirrors.ustc.edu.cn
APT_MIRROR ?=
MIRROR_PROBE_IMAGE := ghcr.io/astral-sh/uv:python3.12-bookworm-slim
build-sandbox: ## build 沙箱镜像(full:office 库 + soffice/poppler/ffmpeg/node + egress shim ~1.3GB)
	@mirror="$(APT_MIRROR)"; \
	if [ -z "$$mirror" ]; then \
	  mirror="$$(docker run --rm --platform linux/amd64 --entrypoint python3 \
	    -v "$(CURDIR)/../tools/deploy/pick_mirror.py:/pick_mirror.py:ro" \
	    $(MIRROR_PROBE_IMAGE) /pick_mirror.py apt)" \
	  || { echo "WARN Debian mirror probe failed; using mirrors.aliyun.com" >&2; mirror=mirrors.aliyun.com; }; \
	fi; \
	echo "==> APT_MIRROR=$$mirror"; \
	docker build --build-arg "APT_MIRROR=$$mirror" -f sandbox-image/Dockerfile -t expert-work-sandbox:dev sandbox-image
	@echo "✓ expert-work-sandbox:dev built"

# 改了沙箱镜像内容(runner.py / sitecustomize.py / Dockerfile / requirements)后
# 一键:重建镜像 + 重启 supervisor 丢弃预热池(否则池里残留旧镜像容器)。
# 不改后端代码就不用跑 dev-up。
rebuild-sandbox: build-sandbox ## 改沙箱镜像后一键:重建镜像 + 重启 supervisor 清池
	$(COMPOSE) $(PROFILES) up -d --force-recreate sandbox-supervisor
	@echo "✓ 沙箱镜像已重建,supervisor 已重启(预热池已清,下次 acquire 用新镜像)"

dev-up: dev-build ## 起全量 dev 栈(full+auth+observability,排除 green)
	# 先 --wait 起带可靠 healthcheck 的服务集。
	$(COMPOSE) $(PROFILES) up -d --wait $(WAIT_SERVICES)
	# keycloak 单独起、不 --wait:它的 healthcheck 4s 超时会误报 unhealthy
	# (实际可用),--wait 会卡死。约 30s 后 :8080 就绪。
	$(COMPOSE) $(PROFILES) up -d keycloak
	# realm 运行期配置:开 unmanagedAttributePolicy,否则 Admin API 建的用户
	# (/setup 向导 + 邀请流)自定义属性 tenant_id 被静默丢弃 → 登录 401。
	# realm import 不还原 user-profile,故用 kcadm 后置设置。幂等;非致命。
	@$(MAKE) -s dev-keycloak-config || echo "⚠ keycloak-config 失败,栈仍可用;就绪后手动: make dev-keycloak-config"
	# seed Keycloak admin client secret 进金库:/setup 向导 + 邀请流建真账号时
	# 要用它取 KC admin token(prod 走 KMS)。幂等;非致命。
	@$(MAKE) -s dev-seed-keycloak || echo "⚠ seed-keycloak 失败,栈仍可用;就绪后手动: make dev-seed-keycloak"
	# 与生产一致:不自动建管理员。首个 system_admin 走 /setup 向导
	# (见 dev-info);要快路径仍可 `make dev-bootstrap-admin`(提权 dev 用户)。
	@echo ""
	@$(MAKE) -s dev-info

# dev 用户的 Keycloak id 在 realm 导入时随机生成(realm json 未固定),所以
# 查一次而非硬编码。client_credentials 用 api-internal 服务账号(带 view-users)。
# bootstrap_admin 在容器内跑:用容器的 EXPERT_WORK_DB_DSN(pgbouncer)+ 已装包,
# 免 host 的 uv/DB 端口依赖。幂等(重复跑 OK)。dev-up 不再自动调它(与生产
# 一致走 /setup 向导);这是想跳过向导时的快路径 / break-glass。
# ADMIN=<用户名|邮箱> 指定要提权的账号(含 @ 当邮箱查,否则当用户名)。默认 dev。
# 提权确定的账号 —— 不靠"零-admin 自动升给第一个登录者"那种非确定行为
# (数据重置后那条会把 system_admin 给随便谁先登录,常常不是你)。
ADMIN ?= dev
dev-bootstrap-admin: ## 提权账号为 system_admin(ADMIN=<用户名|邮箱>,默认 dev;首选 /setup)
	@echo "bootstrap-admin: 等 Keycloak 就绪 + 取 [$(ADMIN)] 用户 id..."
	@case "$(ADMIN)" in *@*) Q="email=$(ADMIN)";; *) Q="username=$(ADMIN)";; esac; \
	SUB=""; for i in $$(seq 1 30); do \
	  SA=$$(curl -sf http://localhost:8080/realms/expert-work/protocol/openid-connect/token \
	    -d grant_type=client_credentials -d client_id=expert-work-api-internal \
	    -d client_secret=dev-internal-secret-rotate-me 2>/dev/null \
	    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])' 2>/dev/null) || { sleep 2; continue; }; \
	  SUB=$$(curl -sf "http://localhost:8080/admin/realms/expert-work/users?$$Q&exact=true" \
	    -H "Authorization: Bearer $$SA" 2>/dev/null \
	    | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])' 2>/dev/null) || { sleep 2; continue; }; \
	  [ -n "$$SUB" ] && break; sleep 2; \
	done; \
	if [ -z "$$SUB" ]; then echo "FAILED: 取不到 [$(ADMIN)] 用户 id(Keycloak 未就绪 / 账号不存在?)"; exit 1; fi; \
	echo "$(ADMIN) subject_id = $$SUB"; \
	$(COMPOSE) $(PROFILES) exec -T control-plane-blue python -m control_plane.bootstrap_admin --subject-id $$SUB

# 只删平台 system_admin 绑定 → 回到零-admin,让 /setup 向导重新可用。
# 不动 LLM key(密钥库)/租户/agent/记忆/审计 —— 仅清授权,数据保留。
# role_binding 是 FORCE-RLS;容器内 postgres 超级用户直删可绕 RLS。
dev-keycloak-config: ## 配 realm unmanagedAttributePolicy=ENABLED(Admin API 建的用户才留 tenant_id)
	@$(COMPOSE) $(PROFILES) exec -T keycloak sh -c '\
	  K=/opt/keycloak/bin/kcadm.sh; \
	  for i in $$(seq 1 30); do $$K config credentials --server http://localhost:8080 --realm master \
	    --user "$${KEYCLOAK_ADMIN:-admin}" --password "$${KEYCLOAK_ADMIN_PASSWORD:-admin_dev}" >/dev/null 2>&1 && break; \
	    sleep 2; done; \
	  $$K update users/profile -r expert-work -s unmanagedAttributePolicy=ENABLED; \
	  $$K update realms/expert-work -s sslRequired=NONE -s loginTheme=expert-work; \
	  $$K update realms/master -s sslRequired=NONE'
	@echo "keycloak realm: unmanagedAttributePolicy=ENABLED, sslRequired=NONE, loginTheme=expert-work"

skill-assets-backfill: ## 存量技能支撑文件搬对象存储(幂等;先 dry-run,加 APPLY=1 落库)
	$(COMPOSE) $(PROFILES) exec -T control-plane-blue \
	  python -m control_plane.backfill_skill_assets $(if $(APPLY),--apply,)

dev-seed-keycloak: ## seed Keycloak admin client secret 进金库(幂等;/setup + 邀请流需要)
	$(COMPOSE) $(PROFILES) exec -T control-plane-blue \
	  python -m control_plane.seed_keycloak_secret --value dev-internal-secret-rotate-me

dev-reset-admin: ## 清平台管理员绑定(保留其它数据;还原 /setup 向导)
	$(COMPOSE) $(PROFILES) exec -T postgres \
	  sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "DELETE FROM role_binding WHERE platform_scope = true;"'
	@echo "── /setup 状态 ───────────────────────────────────"
	@curl -s http://localhost:8000/v1/setup/status || echo "(control-plane 未就绪?)"
	@echo ""
	@echo "→ 开 admin-ui 走 /setup 向导(initialized 应为 false)"

dev-down: ## 停所有 dev 服务(留容器 + 数据卷)
	$(COMPOSE) $(PROFILES) stop $(WAIT_SERVICES) keycloak

dev-clean: ## 删容器 + 网络(留数据卷;比 stop 更彻底)
	$(COMPOSE) $(PROFILES) down

dev-ps: ## 看 expert_work 容器状态
	@docker ps --filter "name=expert_work" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# 用法:make dev-logs SVC=control-plane-blue
SVC ?= control-plane-blue
dev-logs: ## tail 某服务日志(SVC=<service>,默认 control-plane-blue)
	$(COMPOSE) $(PROFILES) logs -f $(SVC)

langfuse-reset: ## 重置 langfuse 账号/project(改 LANGFUSE_INIT_* 或 .env 后)
	# LANGFUSE_INIT_* 仅首次建库跑;改值需清 postgres 卷 + flush redis 认证缓存
	# (只清 postgres 会残留旧 key→旧 project 缓存,API 返空)。
	$(COMPOSE) stop langfuse-web langfuse-worker langfuse-postgres
	-$(COMPOSE) rm -f langfuse-postgres
	-docker volume rm infra_langfuse-postgres-data
	$(COMPOSE) --profile observability up -d --wait \
	  langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-worker langfuse-web
	-docker exec expert-work-langfuse-redis redis-cli FLUSHALL
	@echo "langfuse 已重置;新 INIT 值生效"

dev-info: ## 打印各 web 地址 + dev 登录凭据
	@echo "── Expert Work dev 栈地址 ──────────────────────────────"
	@echo "  admin-ui      http://localhost:5173   (单独 pnpm dev 起;SSO 登录)"
	@echo "  control-plane http://localhost:8000   (后端 API)"
	@echo "  Langfuse      http://localhost:3001   (trace 调试)"
	@echo "  Keycloak      http://localhost:8080   (IdP;约 30s 就绪)"
	@echo "  MinIO         http://localhost:9001   (对象存储控制台)"
	@echo "── 首次:建第一个平台管理员(与生产一致)────────────"
	@echo "  开 admin-ui → 自动进 /setup 向导 → 填邮箱/密码 + Setup Token"
	@echo "  Setup Token   infra/.env 的 EXPERT_WORK_SETUP_TOKEN(默认 dev-setup-token)"
	@echo "  邮箱用新地址(别用 dev@expert-work.local,Keycloak 已占)"
	@echo "  快路径(跳向导): make dev-bootstrap-admin  (提权 dev 用户)"
	@echo "── 其它登录 ──────────────────────────────────────"
	@echo "  Keycloak 管理 admin / admin_dev"
	@echo "  Langfuse      见 LANGFUSE_INIT_USER_* / .env"
