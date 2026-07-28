# Agent 延迟优化二期 PR2 — 缓存三合一 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 失效连动(修「换 key 要重启」生产 bug)→ secret/resolve 值缓存(吃掉 embed/rerank 路径每次的 vault 读)→ built-agent 缓存加界(LRU+TTL,现无界且烤明文 key)。

**Architecture:** 主动失效为主力、TTL 兜底(本仓既定立场:进程内失效 + TTL 兜底跨副本);先补失效再加缓存,顺序不可换。缓存实现照 credential-proxy `SecretCache` 现成范式,零新框架。

**Tech Stack:** Python 3.12 / FastAPI / pytest。

## Global Constraints

- spec:`docs/superpowers/specs/2026-07-27-agent-latency-phase2-design.md`(PR2 节 + 决策纪要 2)
- 分支:`perf-phase2-pr2`(从 `perf-phase2-pr1` cut;PR base 先设 perf-phase2-pr1,PR1 合并后改 main)
- CI 门:`uv run ruff check .` 全库 + `uv run ruff format --check .` + CI-scope mypy(`uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src`)
- control-plane 测试:仓库根 `uv run pytest services/control-plane/tests/ -x -q`;orchestrator:`DOCKER_HOST= uv run pytest services/orchestrator/tests/ -x -q`
- **拍定参数**:secret 值缓存 TTL **300s** / LRU **256**;built-agent 缓存(两处)TTL **30 分钟** / LRU **256**。TTL 全部注入 `clock=time.monotonic` 便于测试
- **不做**:不穿透 `build_llm_router` 的 vault 读(rerank 的 LLM 分支 + agent build 时的——前者少见配置、后者已被 agent 缓存挡,记 follow-up);不做跨副本失效广播;驱逐/失效不调 `MCPServerPool.close_all`(活连接可能在被进行中的 run 用,遗弃靠 GC,与现状 invalidate 行为一致)
- 测试同步跑,禁止后台化 + 轮询
- **并行波次**:Wave 1 = Task 1 ∥ Task 2(文件零交集,worktree 隔离);Wave 2 = Task 3 ∥ Task 4(runtime.py 不同区域,先合 T3 再合 T4,冲突手工调);Wave 3 = Task 5 主会话真栈

## 侦察事实(实施者需知,已核实)

- **轮换 = 同 ref 原地覆写**:`_canonical_secret_name`(platform_config.py:174-203)对同 (provider,key_id) 永远同 slot 名 → 换 key 后 secret_ref 不变,已构建 agent 里烤住的旧明文 key 无从发现。这是「换 key 要重启」的根因。
- **失效缺口全名单**:platform_config.py 10 个写入口(平台 PUT×3/DELETE×3 只调 `service.invalidate()`——那只刷 30s catalog 视图;租户覆盖 PUT×2/DELETE×2 同)+ `mcp_oauth_refresh.py:184,188` 后台刷新完全不失效。
- **已有失效基建**:`AgentRuntime.invalidate_all/invalidate_tenant`(runtime.py:304-327)+ hook 注册(`register_invalidation_hook`/`register_invalidation_all`,285-302);`invalidate_user`(329-340)**不 fan-out**——subagent 缓存的 5-tuple 带 oauth_user_id,OAuth 断连后子缓存残留(现有缺口,本 PR 补)。
- **handler 拿 runtime 的现成写法**:`getattr(request.app.state, "agent_runtime", None)`(agents.py:253 等 4 处先例;部分测试 app 不装 runtime,必须 getattr 兜底)。platform_config.py 的 upsert 系 handler 现无 `request: Request` 参数,加上即可(同文件 delete 系 handler 已有)。
- **缓存实现参照**:`services/credential-proxy/src/credential_proxy/cache.py` `SecretCache`——OrderedDict LRU + `_Entry(value, expires_at)` + 注入 clock + key 含 tenant_id(「Never shared across tenants」,平台级 ref 每租户各存一份是有意冗余,LRU 256 兜得住)。
- **4 个 Resolving 类**(runtime.py:825-1021,生产走 Dynamic 两个):每次 embed/rerank 调用做 config(DB)+resolve(DB)+secret(vault);`resolve_ms/secret_ms/config_ms` attribute 一期已埋(852-853/906/909/955-957/1005-1009)——收益直接从 span attr 读。provider→ref 映射已有 PlatformSecretsService 30s TTL 挡;**本 PR 吃的是 `secret_store.get` 的 vault 读**。rerank 的 secret 读只在 DashScope 分支(`_is_dashscope_rerank_model`,860-865)。
- **同缺口其他消费点**:aux_model_adapter.py:100、quality_judge.py:115(同 resolver+secret_store,顺手接入)。
- **built-agent 缓存**:`runtime.py:217` `_cache: dict` 纯 dict 无界无 TTL 无 size 指标;key 3-tuple `(tenant,name,version)` 或 OAuth 时 4-tuple(+user_id),`k[0]` 恒 tenant。subagent 侧独立缓存 `subagent_runtime.py:186`(4-tuple +depth / OAuth 5-tuple +oauth_user_id)同样裸。BuiltAgent 烤着:N 个带明文 key 的 LLM client(主+fallback×每 provider key 数)+ 双 judge router + 3 个 MCPServerPool(活连接/子进程)+ 数十 KB prompt。
- **TenantConfigService 模式**(6 个同构先例):`_expires_at` + `asyncio.Lock` 双检 + `invalidate()` 置 0 + `_clock` 注入——单值 TTL 的仓内标准形状;per-key 的用 credential-proxy SecretCache 形状。

---

### Task 1: `CredentialValueCache`(纯新文件,Wave 1)

**Files:**
- Create: `services/control-plane/src/control_plane/credential_value_cache.py`
- Test: `services/control-plane/tests/test_credential_value_cache.py`

**Interfaces:**
- Produces(Task 3 消费):
  ```python
  class CredentialValueCache:
      def __init__(self, *, max_size: int = 256, ttl_s: float = 300.0,
                   clock: Callable[[], float] = time.monotonic) -> None: ...
      def get(self, tenant_id: UUID, secret_ref: str) -> str | None: ...
      def put(self, tenant_id: UUID, secret_ref: str, value: str) -> None: ...
      def invalidate_all(self) -> None: ...
      def invalidate_tenant(self, tenant_id: UUID) -> None: ...
      def __len__(self) -> int: ...
  ```
- Task 2 消费:`invalidate_all` / `invalidate_tenant`(经 app.state getattr,见 Task 2)。

**要点**(照 credential-proxy `SecretCache` 逐条对齐,先通读它):
- `CacheKey = tuple[UUID, str]`;`_Entry(value: str, expires_at: float)` frozen dataclass;底层 `OrderedDict` + `move_to_end`(get 命中)+ `popitem(last=False)`(put 超界驱逐);get 惰性过期删除。
- 新增于参照物的两点:`invalidate_tenant`(遍历删 `k[0] == tenant_id`,平台级凭据变更走 `invalidate_all`,租户覆盖走这个);docstring 写明「与 credential-proxy SecretCache 同范式;进程内,多副本陈旧度由 TTL 兜底(本仓立场)」。
- 同步方法即可(无 I/O、调用方在单事件循环)——不加 asyncio.Lock,docstring 注明依赖单线程事件循环语义。

**TDD 步骤**:
- [ ] Step 1 失败测试(≥6 case):get miss 返 None / put+get 命中 / TTL 过期后 miss(clock 注入拨表)/ LRU 驱逐最旧(容量 2 塞 3,验证被驱逐者)/ invalidate_all 清空 / invalidate_tenant 只清该租户(另一租户条目保留)。
- [ ] Step 2 跑红:`uv run pytest services/control-plane/tests/test_credential_value_cache.py -x -q` → ImportError。
- [ ] Step 3 实现(≤80 行)。
- [ ] Step 4 跑绿。
- [ ] Step 5 `git commit -m "feat(control-plane): CredentialValueCache — secret 值进程内 LRU+TTL 缓存(PR2 T1)"`

---

### Task 2: 失效连动(修「换 key 要重启」bug,Wave 1)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/platform_config.py`(10 个写入口)
- Modify: `services/control-plane/src/control_plane/runtime.py`(user 级 hook fan-out)
- Modify: `services/control-plane/src/control_plane/subagent_runtime.py`(user invalidator 注册)
- Modify: `services/control-plane/src/control_plane/mcp_oauth_refresh.py`(后台刷新 2 处)
- Modify: `services/control-plane/src/control_plane/app.py`(oauth refresh worker 注入 + subagent user hook 挂线)
- Test: `services/control-plane/tests/test_credential_invalidation.py`(新建)+ 既有 platform_config API 测试文件补断言

**Interfaces:**
- Consumes: `AgentRuntime.invalidate_all/invalidate_tenant/invalidate_user`(现有);`getattr(request.app.state, ...)` 兜底范式。
- Produces: `AgentRuntime.register_user_invalidation_hook(hook: Callable[[UUID, str], None])`;platform_config 内部 helper `_invalidate_built_agents(request, tenant_id=None)`。

**实现要点**:
1. platform_config.py 加模块级 helper(照 agents.py:242-254 范式):
   ```python
   def _invalidate_built_agents(request: Request, *, tenant_id: UUID | None = None) -> None:
       """凭据轮换后清 built-agent 缓存 —— 轮换是同 ref 原地覆写值
       (_canonical_secret_name 对同 (provider,key_id) 恒同 slot),ref 比对
       发现不了,已构建 agent 里烤住的旧明文 key 只能靠显式失效清掉。
       同时清 secret 值缓存(T3 落地后 app.state 上才有,getattr 兜底)。"""
       runtime = getattr(request.app.state, "agent_runtime", None)
       cache = getattr(request.app.state, "credential_value_cache", None)
       if tenant_id is None:
           if runtime is not None:
               runtime.invalidate_all()
           if cache is not None:
               cache.invalidate_all()
       else:
           if runtime is not None:
               runtime.invalidate_tenant(tenant_id)
           if cache is not None:
               cache.invalidate_tenant(tenant_id)
   ```
2. 10 个入口逐个接:平台级 6 个(`upsert_provider`/`upsert_provider_key`/`upsert_tool`/`delete_provider`/`delete_provider_key`/`delete_tool`)在 `service.invalidate()` 后调 `_invalidate_built_agents(request)`;租户级 4 个(`upsert_tenant_provider`/`upsert_tenant_tool`/`delete_tenant_provider`/`delete_tenant_tool`)调 `_invalidate_built_agents(request, tenant_id=tenant_id)`。upsert 系 handler 签名补 `request: Request`(delete 系已有,照抄参数位置)。
3. runtime.py:`_user_invalidation_hooks: list[Callable[[UUID, str], None]]` 字段 + `register_user_invalidation_hook` 方法(照 285-302 两个现成 register 的样);`invalidate_user` 末尾遍历调 hooks。
4. subagent_runtime.py:`make_child_agent_builder` 加 `register_user_invalidation` 参数,内部注册 `_invalidate_user(tenant_id, user_id)`——清 `len(k) == 5 and k[0] == tenant_id and k[4] == user_id` 的条目(5-tuple 布局见 :197-201 注释,实施时核对下标)。
5. mcp_oauth_refresh.py:构造处注入 runtime(实施时打开看 worker 构造签名与 app.py 装配点,加参数或回调;两处 `secret_store.put`(184/188)成功后调 `runtime.invalidate_user(tenant_id, user_id)`——tenant/user 从刷新上下文取,实施时核实变量名)。
6. app.py:`make_child_agent_builder` 调用处补 `register_user_invalidation=resolved_agent_runtime.register_user_invalidation_hook`;oauth refresh worker 构造处传 runtime。

**测试**(新文件,fake/spy runtime 挂 app.state):
- [ ] PUT 平台 provider → spy.invalidate_all 被调恰一次;PUT 租户 provider → spy.invalidate_tenant(该 tenant) 被调。
- [ ] DELETE 各级同断言(6+4 覆盖至少平台 PUT/DELETE 各一 + 租户 PUT/DELETE 各一,其余同构入口 parametrize 全覆盖)。
- [ ] runtime 单测:`register_user_invalidation_hook` 注册后 `invalidate_user` fan-out;未注册不炸。
- [ ] subagent:user invalidator 只清匹配 (tenant, oauth_user) 的 5-tuple,4-tuple 与他人条目保留。
- [ ] app.state 无 runtime/cache 时(getattr 兜底)PUT 不 500。
- [ ] 先红后绿;commit `fix(control-plane): 凭据写入口连动失效 built-agent/secret 缓存(修换 key 要重启,PR2 T2)`

---

### Task 3: Resolving 4 类 + aux/judge 接缓存(Wave 2,依赖 T1+T2)

**Files:**
- Modify: `services/control-plane/src/control_plane/runtime.py`(4 个 Resolving 类,825-1021 区域)
- Modify: `services/control-plane/src/control_plane/aux_model_adapter.py`(:100 附近)
- Modify: `services/control-plane/src/control_plane/quality_judge.py`(:115 附近)
- Modify: `services/control-plane/src/control_plane/app.py`(建 cache 挂 `app.state.credential_value_cache` + 传给 6 个消费点)
- Test: `services/control-plane/tests/test_resolving_secret_cache.py`(新建)

**Interfaces:**
- Consumes: `CredentialValueCache`(T1);`app.state.credential_value_cache`(T2 的 helper 已 getattr 它——本 task 落地后失效链自动闭合)。
- Produces: 4 个 Resolving 类新字段 `secret_cache: CredentialValueCache | None = None`(frozen dataclass 加带默认字段,不破既有构造)。

**实现要点**:
1. 统一小 helper(runtime.py 模块级):
   ```python
   async def _cached_secret(
       cache: CredentialValueCache | None, secret_store: SecretStore,
       tenant_id: UUID, secret_ref: str,
   ) -> str:
       if cache is not None:
           hit = cache.get(tenant_id, secret_ref)
           if hit is not None:
               return hit
       value = await secret_store.get(parse_secret_ref(secret_ref))
       if cache is not None:
           cache.put(tenant_id, secret_ref, value)
       return value
   ```
2. 4 个 Resolving 类的 `secret_store.get(...)` 调用点换 `_cached_secret(self.secret_cache, ...)`(embed 2 类各 1 处;rerank 2 类各 1 处 DashScope 分支)。**`secret_ms` attribute 打点位置不动**——缓存命中时它自然趋 0,正是收益读数。
3. aux_model_adapter.py:100 / quality_judge.py:115 同样换(各自类加 cache 字段或构造参数,照该文件现有注入风格;`record_credentials_resolve` 指标打点不动)。
4. app.py:lifespan 里 `credential_value_cache = CredentialValueCache()`(默认参数即拍定值)挂 `app.state`,构造 4 个 Resolving 实例 + aux + judge 处传入。
5. **config(DB)读不动**——那是 PlatformSecretsService 30s TTL 与 PR1 P1.2 的领地。

**测试**:
- [ ] fake secret_store 计数:两次 embed resolve 只打一次 vault(第二次命中);TTL 过期后再打。
- [ ] cache=None(未接线)行为与现状逐字节一致(每次都打 vault)。
- [ ] `invalidate_all` 后下一次重新拉。
- [ ] rerank DashScope 分支同断言;LLM-rerank 分支不受影响(不经 cache——断言 fake store 调用来自 build_llm_router 路径照旧)。
- [ ] 先红后绿;跑 T2 的失效测试确认链路闭合(PUT → cache 清 → 下次 miss);commit `perf(control-plane): embed/rerank/aux/judge 的 vault 读接 CredentialValueCache(PR2 T3)`

---

### Task 4: built-agent 缓存加界(Wave 2,与 T3 并行)

**Files:**
- Modify: `services/control-plane/src/control_plane/runtime.py`(150-340 `_cache` 区域)
- Modify: `services/control-plane/src/control_plane/subagent_runtime.py`(:186 子缓存)
- Modify: control-plane 现有 metrics 定义处(实施时找 `record_credentials_resolve` 等 prometheus 先例所在模块,同处加 gauge)
- Test: `services/control-plane/tests/test_agent_cache_bounds.py`(新建)+ 既有 runtime 缓存测试回归

**Interfaces:**
- 不改 `get_agent` 签名与 invalidate 系方法签名;`AgentRuntime` 加字段 `cache_max_size: int = 256`、`cache_ttl_s: float = 1800.0`、`_clock: Callable[[], float] = time.monotonic`(dataclass 带默认,不破构造)。

**实现要点**:
1. `_cache` 改 `OrderedDict[_CacheKey, tuple[BuiltAgent, float]]`(值带 expires_at):get 命中先查过期(过期 del + 当 miss)再 `move_to_end`;put 后超界 `popitem(last=False)`。**驱逐/过期 = 纯遗弃**(注释写明:MCPServerPool 活连接可能在被进行中的 run 使用,不 close,靠 GC——与现状 invalidate 行为一致,不恶化)。
2. TTL 过期与 LRU 驱逐**不 fan-out hooks**(hooks 语义是「配置变更广播」,本体自然过期不广播);显式 invalidate 系方法照旧 fan-out。
3. subagent_runtime.py 的闭包 dict 同款改造(容量/TTL 同参数;从 make_child_agent_builder 加带默认参数传入)。
4. gauge 两个:`expert_work_built_agent_cache_entries{scope="runtime"|"subagent"}`——每次 get/put/invalidate 后 `gauge.set(len(...))`(照现有 metrics 模块范式命名/注册)。
5. docstring 更新 runtime.py:150-153 的 key 注释块:补「值带 expires_at;TTL 只兜漏接的失效写入口(如未来新增凭据入口忘接 T2 的连动),主动失效是主力」。

**测试**:
- [ ] 容量 2 塞 3 → 最旧被驱逐,下次 get 触发重建(agent_builder spy 计数)。
- [ ] TTL 过期(拨 clock)→ 重建;未过期命中不重建。
- [ ] 过期/驱逐不调 invalidation hooks(spy 断言零调用);显式 invalidate_tenant 照旧 fan-out。
- [ ] subagent 缓存同断言(驱逐/TTL)。
- [ ] gauge 数值随 put/invalidate 变化。
- [ ] 既有全部 runtime/subagent 测试回归绿。
- [ ] 先红后绿;commit `feat(control-plane): built-agent 缓存加 LRU 256 + TTL 30min(两处)+ size gauge(PR2 T4)`

---

### Task 5: 真栈验证(主会话执行,Wave 3,不派实施 subagent)

1. 栈热重载:checkout `perf-phase2-pr2` + restart control-plane-blue。
2. **换 key 回归冒烟**(修 bug 的验收):PUT deepseek key 为一个坏值 → 立刻跑一轮 bench run 应报 auth 错(说明立即生效,不用重启)→ PUT 回真值 → 再跑一轮应成功。
3. **secret 缓存收益**:跑 10 轮 bench(同 PR1 配方),读 trace 里 embed span 的 `secret_ms` before(PR1 基线的 trace 有历史值,或 main 上再抽一轮)/after 对照;数据进 `tools/bench/baselines/2026-07-27-phase2-pr2-after.yaml` 的 meta.note。
4. agent 缓存 gauge 在 metrics 端点可见(curl /metrics grep)。

---

## Self-Review 记录

- spec 覆盖:PR2 三 task + 失效顺序(先补失效再加缓存)= spec Task 1/2/3 全映射;spec「Task 1 失效连动排第一」在本 plan 为 T2 但与 T1(纯新类,无接线)同波次,接线顺序(T3 才挂 app.state)保证「缓存生效时失效链已在」——不违背 spec 意图 ✓。
- 参数一致:300s/256/30min/256 与 spec 拍定值一致 ✓。
- 类型一致:`CredentialValueCache` 接口在 T1(定义)/T2(getattr 消费)/T3(注入消费)一致;`register_user_invalidation_hook` 在 T2 定义、app.py 挂线 ✓。
- 占位符:mcp_oauth_refresh 构造签名、subagent 5-tuple 下标、metrics 模块位置为「实施时核实」项——均给出了核实路径与判据,非 TBD ✓。
