# 波 1 遗留 PR-E:关机有界化 + TTL 真实配置漂移闸 + 审计表成本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉沙箱迁移波 1 的最后四条遗留(#11 worker 关机无界、#10 TTL 漂移闸只比默认值、#12 caplog 全局零 WARNING 断言、审计表扫描/增长成本),波 1 遗留归零、波 2 可开工。

**Architecture:** 四块互不依赖的修复。①把 5 个 `stop()` 裸 `await self._task` 的 worker 搬到仓库既有的「有界等待 + 超时取消」房规上;②给 control-plane 补 `sandbox_egress_token_ttl_s` 配置项(此前云侧只能吃 dataclass 默认值),并把漂移闸从「比两个默认值」升级成「比两侧真实解析出的环境变量名 + 部署清单里的实际取值」;③把 `test_rls_detect` 的全局零 WARNING 断言收紧到自家 logger;④给 `sandbox_egress_audit` 加匹配扫描谓词的 partial index,并接进 retention 清理任务。

**Tech Stack:** Python 3.12 / asyncio / pydantic-settings / SQLAlchemy + Alembic / pytest / Postgres

## Global Constraints

- **只改 5 个 worker**:`sandbox_reap_worker`、`approval_metrics`、`sandbox_egress_metrics`、`run_queue_worker`、`orphan_sweep`。`skill_rollback_monitor` 看着像同类但**不是** —— 它 `stop()` 先 `cancel()` 再 `await`(它的循环用 `asyncio.sleep(3600)` 而非 stop-event 等待,取消是唯一正确手法),已经有界,不要动它。其余 worker(webhook_delivery / scheduler / memory_consolidator / curation / quota reaper / transcript_mirror_sweep / quality_drift / eval_worker / approval_timeout_sweep / knowledge recovery)本就有 `wait_for`,也不要动。
- **超时值统一 `_STOP_TIMEOUT_S = 5.0`**,每个模块各自定义一份(照仓内 `_INTERVAL_S` 的既有做法,不新建共享模块)。**不要**照抄别处的 `self._interval_s + 5`:本批 worker 的 interval 是 240s / 60s / 60s,`interval + 5` 会给出 245s 的「上界」,远超 K8s 默认 30s 优雅期,那不是上界是摆设。
- 迁移 revision 号 `0143`,`down_revision = "0142_sandbox_warm_backend_scope"`。迁移里**不 import 应用代码**,表名/常量用字面量(仓内惯例,见 0142 docstring)。
- 提交信息用 conventional commits;每个 task 一次提交。
- 本地跑测试:control-plane / persistence 单测直接 `uv run pytest <path>`;persistence 的 integration 用例需要 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。
- 终门跑 CI 同款:`uv run ruff check .`、`uv run ruff format --check .`、CI-scope mypy(`uv run mypy packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src`)、`uv run pytest -m "not integration"`。注意 **CI 的 mypy scope 不含 control-plane**,control-plane 的类型问题本地自查。
- 注释/docstring 语言随各文件既有风格(这批文件是英文 docstring + 中文说明混排,照邻居写)。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `services/control-plane/src/control_plane/{sandbox_reap_worker,approval_metrics,sandbox_egress_metrics,run_queue_worker,orphan_sweep}.py` | Task 1:`stop()` 有界化 |
| `services/control-plane/tests/test_{sandbox_reap_worker,approval_metrics,sandbox_egress_metrics,run_queue_worker,orphan_sweep}.py` | Task 1:各加一条「卡死的 sweep 不阻塞 stop」用例 |
| `packages/expert-work-persistence/tests/test_rls_detect.py` | Task 2:断言收紧 |
| `services/control-plane/src/control_plane/settings.py` | Task 3:新增 `sandbox_egress_token_ttl_s` |
| `services/control-plane/src/control_plane/runtime.py` | Task 3:把 TTL 传给 `AgentSandboxClient` |
| `infra/docker-compose.yml` | Task 3:control-plane 锚点补 `EXPERT_WORK_SANDBOX_EGRESS_TOKEN_SECRET`(与 supervisor 同源) |
| `services/orchestrator/tests/test_sandbox_runtime_contract.py` | Task 3:漂移闸升级(env 名同一 + 部署清单不得单边设置) |
| `packages/expert-work-persistence/migrations/versions/0143_sandbox_egress_audit_scan_index.py` | Task 4:partial index + retention 角色授权 |
| `packages/expert-work-persistence/tests/test_sql_sandbox_egress_audit_store.py` | Task 4:执行计划真走索引 + 授权可删的集成断言 |
| `services/retention-cleanup-job/src/retention_cleanup_job/{settings,job,main}.py` | Task 5:新增 `sandbox_egress_audit` 清理 pass |
| `services/retention-cleanup-job/tests/{test_job_integration,test_job_unit}.py` | Task 5:真删除的集成用例 + 报告字段默认值 |

---

### Task 1: 5 个 worker 的 `stop()` 有界化

**Files:**
- Modify: `services/control-plane/src/control_plane/sandbox_reap_worker.py:82-86`
- Modify: `services/control-plane/src/control_plane/approval_metrics.py:76-80`
- Modify: `services/control-plane/src/control_plane/sandbox_egress_metrics.py:127-131`
- Modify: `services/control-plane/src/control_plane/run_queue_worker.py:131-135`
- Modify: `services/control-plane/src/control_plane/orphan_sweep.py:151-155`
- Test: 同名 5 个 `services/control-plane/tests/test_*.py`

**Interfaces:**
- Consumes: 无(不依赖其他 task)
- Produces: 各模块新增模块级常量 `_STOP_TIMEOUT_S: float = 5.0`;`stop()` 签名不变(`async def stop(self) -> None`)

**背景(实现者必读):** 这 5 个 worker 的 `stop()` 都是 `await self._task` 不设上界。它们全部由 control-plane 的 lifespan 在关机时**顺序** await(`app.py:2130-2135` 等)。任一 worker 的 sweep 卡住(reap 发远端 E2B kill、run_queue 认领+启动 run、egress/approval 扫库),关机就一直挂到 K8s 把进程 SIGKILL。仓库多数 worker 早已是「有界等待 + 超时取消」,这 5 个是漏网的。

- [ ] **Step 1: 先写会失败的测试(以 reap 为例,五个文件同形)**

在 `services/control-plane/tests/test_sandbox_reap_worker.py` 末尾追加。注意 `import asyncio` 与模块别名 `from control_plane import sandbox_reap_worker as mod` 若文件里没有则一并补上:

```python
@pytest.mark.asyncio
async def test_stop_is_bounded_when_a_sweep_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡死的 sweep 不能把关机拖到 SIGKILL —— stop() 等一小会儿就取消它。

    lifespan 顺序 await 每个 worker 的 stop();这里少了上界,一个连不上
    E2B 的 sweep 就能把整个进程的关机挂到 K8s 优雅期耗尽。
    """
    worker = SandboxReapWorker(runtime=FakeRuntime(), interval_s=0.01)  # type: ignore[arg-type]
    entered = asyncio.Event()

    async def _never_returns() -> int:
        entered.set()
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(worker, "sweep_once", _never_returns)
    monkeypatch.setattr(mod, "_STOP_TIMEOUT_S", 0.05)

    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)

    # 修复前:stop() 永远等下去,这里超时失败。
    await asyncio.wait_for(worker.stop(), timeout=2)
    assert worker._task is None
```

五个文件的差异只有三处,其余逐字相同:

| 文件 | 构造 | 被 patch 的 sweep 方法 | 模块别名 |
|---|---|---|---|
| `test_sandbox_reap_worker.py` | `SandboxReapWorker(runtime=FakeRuntime(), interval_s=0.01)` | `sweep_once` | `from control_plane import sandbox_reap_worker as mod` |
| `test_approval_metrics.py` | `ApprovalGaugeWorker(approval_store=_ExplodingStore(), interval_s=0.01)` | `refresh_once` | `from control_plane import approval_metrics as mod` |
| `test_sandbox_egress_metrics.py` | `SandboxEgressMetricsWorker(audit_store=InMemorySandboxEgressAuditStore(), interval_s=0.01)` | `refresh_once` | `from control_plane import sandbox_egress_metrics as mod` |
| `test_run_queue_worker.py` | `_worker(InMemoryRunStore(), _FakeRuntime(), interval_s=0.01)`(用该文件已有的 `_worker` 工厂;store/runtime 用该文件其他用例同款的构造) | `run_once` | `from control_plane import run_queue_worker as mod` |
| `test_orphan_sweep.py` | `_sweep(InMemoryRunStore(), _FakeRuntime(), interval_s=0.01)`(用该文件已有的 `_sweep` 工厂) | `run_once` | `from control_plane import orphan_sweep as mod` |

`approval_metrics` / `sandbox_egress_metrics` 的 `_loop` 在进循环前会先跑一次 `refresh_once()`,所以 patch 后第一次就卡住,行为与 reap 一致。

- [ ] **Step 2: 跑测试确认 5 条全红**

Run: `uv run pytest services/control-plane/tests/test_sandbox_reap_worker.py services/control-plane/tests/test_approval_metrics.py services/control-plane/tests/test_sandbox_egress_metrics.py services/control-plane/tests/test_run_queue_worker.py services/control-plane/tests/test_orphan_sweep.py -k bounded -v`

Expected: 5 FAILED,失败原因是 `TimeoutError`(`wait_for(worker.stop(), timeout=2)` 超时)。**如果某条不是超时而是别的错(构造签名不对等),先修测试再往下走** —— 这条红必须红在「stop 挂住」上,否则它证明不了任何东西。

- [ ] **Step 3: 五个模块各加常量 + 改 `stop()`**

每个模块在已有的 `_INTERVAL_S`(或文件顶部常量区)旁边加:

```python
#: ``stop()`` 等待当前这一轮 sweep 收尾的上限,超时就取消。刻意**不是**
#: 别处那种 ``interval + 5``:本 worker 的 interval 是分钟级,那个式子给出的
#: 「上界」比 K8s 默认 30s 优雅期还长,等于没有上界。5 秒足够一轮正常 sweep
#: 收尾;收不了尾就取消 —— 这些 sweep 都是周期性、幂等的,下次启动会重来。
_STOP_TIMEOUT_S = 5.0
```

`stop()` 改成(保持各文件原有的语句顺序,只把 `await self._task` 包起来):

```python
    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None
```

`except` 里带上 `asyncio.CancelledError` 与仓内既有写法一致(见 `webhook_delivery_worker.py:296-305`、`scheduler.py:235-245`):`wait_for` 超时时会先取消被等待的 task,某些实现会把 `CancelledError` 透出来。确认各文件已 `import asyncio`(五个都已 import)。

- [ ] **Step 4: 跑测试确认 5 条全绿**

Run: 同 Step 2 的命令
Expected: 5 passed

- [ ] **Step 5: 跑这 5 个 worker 的完整测试文件,确认没打破既有用例**

Run: `uv run pytest services/control-plane/tests/test_sandbox_reap_worker.py services/control-plane/tests/test_approval_metrics.py services/control-plane/tests/test_sandbox_egress_metrics.py services/control-plane/tests/test_run_queue_worker.py services/control-plane/tests/test_orphan_sweep.py -q`
Expected: 全 passed

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/sandbox_reap_worker.py \
        services/control-plane/src/control_plane/approval_metrics.py \
        services/control-plane/src/control_plane/sandbox_egress_metrics.py \
        services/control-plane/src/control_plane/run_queue_worker.py \
        services/control-plane/src/control_plane/orphan_sweep.py \
        services/control-plane/tests/test_sandbox_reap_worker.py \
        services/control-plane/tests/test_approval_metrics.py \
        services/control-plane/tests/test_sandbox_egress_metrics.py \
        services/control-plane/tests/test_run_queue_worker.py \
        services/control-plane/tests/test_orphan_sweep.py
git commit -m "fix(control-plane): 5 个后台 worker 的 stop() 加有界超时——卡住的 sweep 不再拖死关机"
```

---

### Task 2: `test_rls_detect` 断言收紧到自家 logger

**Files:**
- Modify: `packages/expert-work-persistence/tests/test_rls_detect.py:71,84`

**Interfaces:**
- Consumes: 无
- Produces: 无

**背景:** 两处 `assert caplog.records == []` 是**全局零 WARNING** 断言 —— caplog 抓的是所有 logger 冒泡上来的记录,不只本模块的。#1077 就是这个模式被 CI 里其他测试遗留的 OTLP BatchSpanProcessor 后台线程(连不上 `localhost:4318` 时往 root 吐 `Transient error ... retrying`)污染,把一个 deploy-only PR 卡红。本文件今天不红只是因为 pytest 按目录字母序收集,`packages/` 排在制造噪音的 `services/` 前面 —— 顺序上的运气,不是断言写对了。文件顶部已有 `_LOGGER_NAME = "expert_work.persistence.rls"`,直接用。

- [ ] **Step 1: 改两处断言**

`test_no_warning_when_bypass_set`(:71)与 `test_no_warning_when_tenant_set`(:84),把

```python
    assert caplog.records == []
```

改成

```python
    # 只看本模块自己的 logger:caplog 抓的是所有冒泡上来的记录,全局零
    # WARNING 断言会被无关噪音污染(#1077:CI 里其他测试遗留的 otel
    # exporter 后台线程往 root 吐 retry WARNING,卡红过一个 deploy-only PR)。
    assert [r.message for r in caplog.records if r.name == _LOGGER_NAME] == []
```

两处的注释只写一次(写在第一处),第二处只改断言行。

- [ ] **Step 2: 跑测试**

Run: `uv run pytest packages/expert-work-persistence/tests/test_rls_detect.py -v`
Expected: 3 passed

- [ ] **Step 3: 变异自验 —— 证明断言还在起作用**

临时把 `expert_work/persistence/rls.py` 里 `_rls_after_begin` 的 bypass 分支去掉(让 bypass 场景也吐 `rls.would_fail_closed`),重跑上面的命令,`test_no_warning_when_bypass_set` **必须**变红;确认后把改动还原(`git checkout -- packages/expert-work-persistence/src/expert_work/persistence/rls.py`)。收紧后的断言若连自家 logger 的记录也不管,这一步会绿 —— 那就是改坏了。

- [ ] **Step 4: 提交**

```bash
git add packages/expert-work-persistence/tests/test_rls_detect.py
git commit -m "test(persistence): rls detect 断言收紧到自家 logger——免疫无关 WARNING 噪音"
```

---

### Task 3: TTL 漂移闸比真实配置

**Files:**
- Modify: `services/control-plane/src/control_plane/settings.py:227`(在 `sandbox_egress_token_secret` 之后)
- Modify: `services/control-plane/src/control_plane/runtime.py:1501-1509`
- Modify: `infra/docker-compose.yml:39` 附近(`x-control-plane-base` 锚点的 environment)
- Modify: `services/orchestrator/tests/test_sandbox_runtime_contract.py:464-492`(`test_egress_token_ttl_matches_supervisor_default` 附近)

**Interfaces:**
- Consumes: 无
- Produces: `Settings.sandbox_egress_token_ttl_s: int`;`build_sandbox_runtime` 构造 `AgentSandboxClient` 时多传一个 `egress_token_ttl_s=`

**背景(实现者必读):** 现在的漂移闸比的是两个**默认值**(`SandboxSupervisorSettings.model_fields["egress_token_ttl_s"].default` vs `AgentSandboxClient.__dataclass_fields__["egress_token_ttl_s"].default`)。而 `build_sandbox_runtime` 构造云客户端时根本没传这个参数 —— control-plane 侧压根没有这个配置项,只能吃 dataclass 默认值。运维只要在 supervisor 部署上设 `EXPERT_WORK_SANDBOX_EGRESS_TOKEN_TTL_S`,两个后端铸出的 token 有效期就劈叉了,而闸一路绿。

关键事实:两侧的环境变量名**本来就是同一个** —— supervisor 的 `SandboxSupervisorSettings` 前缀是 `EXPERT_WORK_SANDBOX_`、字段 `egress_token_ttl_s`;control-plane 的 `Settings` 前缀是 `EXPERT_WORK_`、字段(本 task 新增)`sandbox_egress_token_ttl_s` —— 拼出来都是 `EXPERT_WORK_SANDBOX_EGRESS_TOKEN_TTL_S`。既有的 `egress_token_secret` / `sandbox_egress_token_secret` 就是这个套路。所以「比真实配置」的落地方式是两条:钉住这个名字相等(结构性保证),再钉住部署清单不会只给一边设。

- [ ] **Step 1: 先写会失败的测试**

在 `services/orchestrator/tests/test_sandbox_runtime_contract.py` 里 `test_egress_token_ttl_matches_supervisor_default` **之后**追加两条。文件顶部若无 `from pathlib import Path` / `import re` 一并补上:

```python
#: 仓库根 —— 本文件在 services/orchestrator/tests/ 下,上溯三级。
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 两侧共享的出网配置项。左=control-plane ``Settings`` 字段名,
#: 右=sandbox-supervisor ``SandboxSupervisorSettings`` 字段名。
_SHARED_EGRESS_FIELDS = [
    ("sandbox_egress_token_secret", "egress_token_secret"),
    ("sandbox_egress_token_ttl_s", "egress_token_ttl_s"),
]


def test_shared_egress_settings_resolve_to_the_same_env_var() -> None:
    """两侧的同名配置必须解析到**同一个**环境变量名。

    这是「比真实配置」的结构性保证:名字一样,部署里改一次两个后端一起改;
    名字一旦分叉(比如有人给 control-plane 那侧改了字段名),运维设一个变量
    只会生效一边,而比默认值的闸完全看不见这种劈叉。
    """
    from control_plane.settings import Settings
    from sandbox_supervisor.settings import SandboxSupervisorSettings

    cp_prefix = Settings.model_config["env_prefix"]
    sup_prefix = SandboxSupervisorSettings.model_config["env_prefix"]

    for cp_field, sup_field in _SHARED_EGRESS_FIELDS:
        assert cp_field in Settings.model_fields, f"control-plane 少了 {cp_field}"
        assert sup_field in SandboxSupervisorSettings.model_fields, f"supervisor 少了 {sup_field}"
        cp_env = f"{cp_prefix}{cp_field}".upper()
        sup_env = f"{sup_prefix}{sup_field}".upper()
        assert cp_env == sup_env, (
            f"control-plane 的 {cp_field} 读 {cp_env},supervisor 的 {sup_field} 读"
            f" {sup_env} —— 两个名字不一样,部署里设一个只会生效一边。"
        )


def test_compose_never_sets_a_shared_egress_var_for_only_one_service() -> None:
    """docker-compose 里这些变量要么两边都设、要么都不设,且取值表达式相同。

    compose 是唯一两个服务同时在跑的地方(k8s 上没有 sandbox-supervisor
    部署)。control-plane 走 ``x-control-plane-base`` 锚点,supervisor 有自己的
    environment 块 —— 只给一边设,就是两个后端铸出不同待遇的 token,而且
    「默认值一致」的闸看不见。
    """
    compose = (_REPO_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    # 锚点块:从 `x-control-plane-base:` 到下一个顶格键;supervisor 块:从
    # `  sandbox-supervisor:` 到下一个同级服务键。
    cp_block = re.search(r"^x-control-plane-base:.*?(?=^\S)", compose, re.S | re.M)
    sup_block = re.search(r"^  sandbox-supervisor:.*?(?=^  \S)", compose, re.S | re.M)
    assert cp_block is not None, "compose 里找不到 x-control-plane-base 锚点"
    assert sup_block is not None, "compose 里找不到 sandbox-supervisor 服务块"

    from sandbox_supervisor.settings import SandboxSupervisorSettings

    prefix = SandboxSupervisorSettings.model_config["env_prefix"]
    for _cp_field, sup_field in _SHARED_EGRESS_FIELDS:
        var = f"{prefix}{sup_field}".upper()
        cp_line = re.search(rf"^\s*{var}:\s*(\S.*)$", cp_block.group(0), re.M)
        sup_line = re.search(rf"^\s*{var}:\s*(\S.*)$", sup_block.group(0), re.M)
        assert (cp_line is None) == (sup_line is None), (
            f"{var} 只在一边设了(control-plane={cp_line is not None},"
            f" supervisor={sup_line is not None})—— 两个后端会拿到不同的值。"
        )
        if cp_line is not None and sup_line is not None:
            assert cp_line.group(1).strip() == sup_line.group(1).strip(), (
                f"{var} 两边取值表达式不同:control-plane={cp_line.group(1).strip()!r}"
                f" vs supervisor={sup_line.group(1).strip()!r}"
            )
```

- [ ] **Step 2: 跑测试确认两条都红**

Run: `uv run pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -k "shared_egress or compose_never" -v`

Expected: 两条都 FAILED —— 第一条因为 `Settings` 还没有 `sandbox_egress_token_ttl_s` 字段;第二条因为 `EXPERT_WORK_SANDBOX_EGRESS_TOKEN_SECRET` 今天只在 supervisor 块里设了(control-plane 锚点没设,靠两边 dev 默认值恰好是同一个字符串在「巧合地一致」)。

- [ ] **Step 3: control-plane Settings 加字段**

在 `services/control-plane/src/control_plane/settings.py` 的 `sandbox_egress_token_secret` 之后加(该文件已 `from pydantic import Field`,若无则补):

```python
    #: 铸沙箱出网 token 的有效期。与 sandbox-supervisor 的
    #: ``egress_token_ttl_s`` 解析到**同一个**环境变量
    #: ``EXPERT_WORK_SANDBOX_EGRESS_TOKEN_TTL_S``(两侧前缀不同、字段名不同,
    #: 拼出来是同一个名字,同 ``sandbox_egress_token_secret`` 的套路)——
    #: ``test_shared_egress_settings_resolve_to_the_same_env_var`` 钉住这条。
    #:
    #: 补这个旋钮之前,云侧(``AgentSandboxClient``)只能吃 dataclass 默认值:
    #: 运维在 supervisor 上调短 TTL,两个后端铸出的 token 有效期就差着倍数,
    #: 而当时只比默认值的漂移闸完全看不见。上下界与 supervisor 侧同款。
    sandbox_egress_token_ttl_s: int = Field(default=24 * 60 * 60, gt=0, le=7 * 24 * 60 * 60)
```

- [ ] **Step 4: 把 TTL 传给云客户端**

`services/control-plane/src/control_plane/runtime.py:1501` 的 `AgentSandboxClient(...)` 里补一行(放在 `egress_token_secret` 之后,保持与 settings 里的相邻顺序):

```python
            egress_token_ttl_s=settings.sandbox_egress_token_ttl_s,
```

- [ ] **Step 5: compose 锚点补上 secret,让两边同源**

`infra/docker-compose.yml` 的 `x-control-plane-base` 锚点 environment 里,`EXPERT_WORK_SANDBOX_SUPERVISOR_URL` 那行附近加:

```yaml
    # 出网 token 的 HMAC secret。sandbox-supervisor 与 credential-proxy 都从
    # 同一个 ${EXPERT_WORK_EGRESS_TOKEN_SECRET} 取值;control-plane 在
    # agent_sandbox 后端下自己铸 token,过去这里没设、靠两边 dev 默认值恰好
    # 相同而「巧合地一致」——真设了 .env 里那个变量就会只生效一半,proxy 会
    # 拒掉云侧铸的每一个 token。test_compose_never_sets_a_shared_egress_var_for_only_one_service 钉住。
    EXPERT_WORK_SANDBOX_EGRESS_TOKEN_SECRET: ${EXPERT_WORK_EGRESS_TOKEN_SECRET:-dev-egress-token-secret-rotate-me}
```

**不要**给 TTL 加 compose 条目:今天没有任何部署需要非默认 TTL,加了就是投机配置;闸会在将来真有人设的那天替我们把关。

- [ ] **Step 6: 跑测试确认全绿**

Run: `uv run pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -m "not integration" -q`
Expected: 全 passed(含既有的默认值闸 + 新增两条)

- [ ] **Step 7: 变异自验**

两条各杀一次,做完还原:
1. 把 Step 3 新增字段名临时改成 `sandbox_egress_ttl_s`(去掉 `token_`)→ `test_shared_egress_settings_resolve_to_the_same_env_var` 必须红。
2. 把 Step 5 新增的 compose 行临时删掉 → `test_compose_never_sets_a_shared_egress_var_for_only_one_service` 必须红。

- [ ] **Step 8: control-plane 侧回归**

Run: `uv run pytest services/control-plane/tests/test_runtime.py -q`
Expected: passed(`build_sandbox_runtime` 的既有工厂测试不能被新参数打破)

- [ ] **Step 9: 提交**

```bash
git add services/control-plane/src/control_plane/settings.py \
        services/control-plane/src/control_plane/runtime.py \
        infra/docker-compose.yml \
        services/orchestrator/tests/test_sandbox_runtime_contract.py
git commit -m "fix(sandbox): 出网 token TTL 补齐云侧旋钮 + 漂移闸改比真实配置"
```

---

### Task 4: `sandbox_egress_audit` 扫描索引 + retention 角色授权

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0143_sandbox_egress_audit_scan_index.py`
- Modify: `packages/expert-work-persistence/tests/test_sql_sandbox_egress_audit_store.py`(末尾追加两条用例,复用该文件已有的 `sql_store` fixture 与 `_insert` 辅助)

**Interfaces:**
- Consumes: 无
- Produces: 索引 `sandbox_egress_audit_scan_idx`;`retention_cleanup_worker` 对该表的 SELECT/DELETE 授权(Task 5 依赖)

**背景:** 两件事一次迁移办完(先例 0140 就是 GRANT + 索引同迁移):

1. **索引**:`SandboxEgressMetricsWorker` 每 60 秒扫一次
   `WHERE occurred_at >= :since AND occurred_at < :until AND verdict <> 'allowed' GROUP BY verdict`
   (`SqlSandboxEgressAuditStore.count_by_verdict_since`)。0087 建的索引都以 `tenant_id` 打头,这条不带 tenant 谓词的查询用不上,只能顺序扫整张 append-only 表(50 万行实测 12.5ms/次,且只会更差)。
2. **授权**:`sandbox_egress_audit` 建表至今**没有任何 GRANT**。Task 5 的清扫作业以 `retention_cleanup_worker` 角色连库,拿不到 DELETE 就是 permission denied —— 0131 的 docstring 记的正是这个历史欠账(清扫 pass 上线时忘了补授权)。**好消息**:这张表没有 RLS/policy(0087/0088 都没建),不存在「删了个寂寞」的隐形行陷阱。

- [ ] **Step 1: 写迁移**

创建 `packages/expert-work-persistence/migrations/versions/0143_sandbox_egress_audit_scan_index.py`:

```python
"""0143 — 出网审计表的扫描索引 + retention 角色授权.

**索引**。``SandboxEgressMetricsWorker`` 每 60 秒跑一次
``occurred_at >= :since AND occurred_at < :until AND verdict <> 'allowed'``
的窗口聚合(``SqlSandboxEgressAuditStore.count_by_verdict_since``)。0087 建的
两个索引都以 ``tenant_id`` 打头,这条不带 tenant 谓词的查询用不上它们,只能
顺序扫整张 append-only 表——随行数线性变贵,而 ``allowed`` 又恰好是量最大的
那一类。索引谓词与查询的 ``WHERE`` 逐字相同(``verdict <> 'allowed'``),
Postgres 才认得出这是可用的 partial index;两列 ``(occurred_at, verdict)``
让 ``GROUP BY verdict`` 也走 index-only scan,不用回表。

**授权**。这张表建表至今没有任何表级 GRANT(0087/0088 都没写),而波 1 PR-E
给它加了 retention 清扫 pass —— ``retention_cleanup_worker``(0010 建)拿不到
SELECT/DELETE 就是 permission denied。0131 记过这笔历史欠账的教训:清扫 pass
上线要跟着补授权。这张表没有 RLS/policy,所以只补表级 GRANT 就够。

非 CONCURRENTLY(同 0141/0142 的理由:alembic 迁移跑在事务里,
``CREATE INDEX CONCURRENTLY`` 不允许)。这张表写多读少,建索引会短暂持写锁
——runbook 记一笔,生产上挑低峰执行。

Revision ID: 0143_sandbox_egress_audit_scan_index
Revises: 0142_sandbox_warm_backend_scope
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0143_sandbox_egress_audit_scan_index"
down_revision: str | Sequence[str] | None = "0142_sandbox_warm_backend_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_INDEX = "sandbox_egress_audit_scan_idx"
_TABLE = "sandbox_egress_audit"
_RETENTION_ROLE = "retention_cleanup_worker"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX {_INDEX}
          ON {_TABLE} (occurred_at, verdict)
          WHERE verdict <> 'allowed'
        """
    )
    op.execute(f"GRANT SELECT, DELETE ON TABLE {_TABLE} TO {_RETENTION_ROLE};")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE {_TABLE} FROM {_RETENTION_ROLE};")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
```

- [ ] **Step 2: 写集成测试 —— 断言执行计划真用上了这个索引**

在 `packages/expert-work-persistence/tests/test_sql_sandbox_egress_audit_store.py` 末尾追加。该文件已有 `sql_store` fixture(跑完 alembic upgrade head 后给出 `(store, engine)`)与 `_insert(engine, verdict=, occurred_at=)` 辅助,直接复用;需要补的 import 只有 `from sqlalchemy import text`(若文件里还没有):

```python
@pytest.mark.asyncio
async def test_metrics_scan_uses_the_partial_index(sql_store: SqlStoreFixture) -> None:
    """0143 的 partial index 对 metrics 那条窗口聚合可用 —— 光建索引不算数。

    索引谓词跟查询谓词只要差一个字,Postgres 就当它不适用、默默回退顺序扫描:
    建了索引、查询照旧慢,而且没有任何报错。所以断言看的是执行计划,不是
    ``pg_indexes`` 里有没有这一行。

    ``enable_seqscan = off``:测试表只有几行,优化器此刻当然更愿意顺序扫。
    这里要证的是「这个索引对这条查询可用」,不是「优化器此刻会选它」——
    关掉顺序扫描,可用则走索引,不可用则仍然是 Seq Scan(那就是红)。
    """
    _store, engine = sql_store
    try:
        now = datetime.now(UTC)
        await _insert(engine, verdict="blocked_auth", occurred_at=now)
        await _insert(engine, verdict="allowed", occurred_at=now)

        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL enable_seqscan = off"))
            rows = await conn.execute(
                text(
                    "EXPLAIN SELECT verdict, count(*) FROM sandbox_egress_audit "
                    "WHERE occurred_at >= :since AND occurred_at < :until "
                    "AND verdict <> 'allowed' GROUP BY verdict"
                ),
                {"since": now - timedelta(minutes=5), "until": now + timedelta(minutes=5)},
            )
            plan = "\n".join(str(r[0]) for r in rows)

        assert "sandbox_egress_audit_scan_idx" in plan, plan
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_role_can_delete_from_the_audit_table(sql_store: SqlStoreFixture) -> None:
    """``retention_cleanup_worker`` 拿得到 SELECT/DELETE。

    这张表建表至今没有任何表级 GRANT,而 PR-E 给它加了清扫 pass —— 少了这条
    授权,清扫作业每次跑都是 permission denied(0131 记过同样的欠账)。
    """
    _store, engine = sql_store
    try:
        await _insert(engine, verdict="allowed", occurred_at=datetime.now(UTC))
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE retention_cleanup_worker"))
            deleted = await conn.execute(text("DELETE FROM sandbox_egress_audit"))
        assert deleted.rowcount == 1
    finally:
        await engine.dispose()
```

- [ ] **Step 3: 跑测试**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest packages/expert-work-persistence/tests/test_sql_sandbox_egress_audit_store.py -v -m integration
```
Expected: 全 passed(既有用例 + 新增两条)

- [ ] **Step 4: 变异自验(两条各杀一次,做完还原)**

1. 迁移里索引谓词临时改成 `WHERE verdict <> 'blocked_auth'`(与查询不匹配)→ `test_metrics_scan_uses_the_partial_index` 必须红。
2. 迁移里 GRANT 那行临时注释掉 → `test_retention_role_can_delete_from_the_audit_table` 必须红。

- [ ] **Step 5: 迁移链完整性**

Run: `uv run pytest packages/expert-work-persistence/tests -k "migration or alembic" -q`(该目录已有迁移链/单头校验用例;若都没命中,`grep -rl "script.get_heads\|alembic" packages/expert-work-persistence/tests | head` 找到后跑它)
Expected: passed

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-persistence/migrations/versions/0143_sandbox_egress_audit_scan_index.py \
        packages/expert-work-persistence/tests/test_sql_sandbox_egress_audit_store.py
git commit -m "perf(persistence): sandbox_egress_audit 加扫描 partial index + retention 角色授权"
```

---

### Task 5: `sandbox_egress_audit` 接进 retention 清理

**Files:**
- Modify: `services/retention-cleanup-job/src/retention_cleanup_job/settings.py`(tuning 段之后)
- Modify: `services/retention-cleanup-job/src/retention_cleanup_job/job.py`(`CleanupReport` + `__init__` + `run_once` + 新方法)
- Modify: `services/retention-cleanup-job/src/retention_cleanup_job/main.py:91-125`(建 job 时传参 + done 日志行)
- Test: `services/retention-cleanup-job/tests/test_job_integration.py`(真删除)+ `services/retention-cleanup-job/tests/test_job_unit.py`(报告字段默认值)

**Interfaces:**
- Consumes: Task 4 的 GRANT(**硬依赖**:清扫以 `retention_cleanup_worker` 角色执行,没有那条授权这个 pass 每次都 permission denied)
- Produces: `RetentionCleanupSettings.sandbox_egress_audit_retention_days`;`RetentionCleanupJob(..., sandbox_egress_audit_retention_days=90)`;`CleanupReport.sandbox_egress_audit_deleted`

**背景:** `sandbox_egress_audit` 是 append-only 且没有任何清理路径 —— 每一次沙箱出网都写一行,包括量最大的 `allowed`。仓内既有的表级 retention 都是「构造参数一个全局天数 + ctid 子查询限批 DELETE」(见 `_delete_event_log` / `_delete_expired_images`),照抄。不做 per-tenant 覆盖,理由与 `image_retention_days` 那段注释写的一样(M0 无 per-tenant 覆盖)。**注意 `RetentionCleanupJob.__init__` 收的是一串显式 kwargs,不是 Settings 对象** —— 天数走构造参数,`main.py` 负责从 settings 传进来,与其余几个窗口完全同形。

- [ ] **Step 1: 先写会失败的测试**

(a) `services/retention-cleanup-job/tests/test_job_integration.py` 末尾追加,照该文件 `test_event_log_retention_deletes_old_rows` 的形态(同款 `db_fixture`、同款 admin 连接插行、同款 `RetentionCleanupJob(...)` + `run_once()`):

```python
@pytest.mark.asyncio
async def test_sandbox_egress_audit_retention_deletes_old_rows(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """过了保留期的出网审计行被清掉,期内的留着。

    这张表每次沙箱出网都写一行(``allowed`` 占绝大多数),PR-E 之前没有任何
    清理路径 —— 只增不减。没有 per-tenant 覆盖,窗口是 job 级参数。
    """
    _app_engine, worker_engine, sync_admin = db_fixture
    try:
        tenant = uuid4()
        admin = create_engine(sync_admin, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                insert = text(
                    "INSERT INTO sandbox_egress_audit "
                    "(tenant_id, agent_name, agent_version, sandbox_id, target_host, "
                    " target_port, verdict, bytes_up, bytes_down, duration_ms, occurred_at) "
                    "VALUES (:t, 'alpha', '1.0.0', 'sbx-1', 'api.openai.com', 443, "
                    " 'allowed', 1, 2, 3, now() - (:age || ' days')::interval)"
                )
                conn.execute(insert, {"t": str(tenant), "age": 100})  # 过期
                conn.execute(insert, {"t": str(tenant), "age": 1})  # 期内
        finally:
            admin.dispose()

        sf = create_async_session_factory(worker_engine)
        job = RetentionCleanupJob(
            db_session_factory=sf,
            batch_size=10000,
            sandbox_egress_audit_retention_days=90,
        )
        report = await job.run_once()
        assert report.sandbox_egress_audit_deleted == 1

        async with worker_engine.begin() as conn:
            remaining = (
                await conn.execute(text("SELECT count(*) FROM sandbox_egress_audit"))
            ).scalar_one()
        assert remaining == 1
    finally:
        await worker_engine.dispose()
```

(b) `services/retention-cleanup-job/tests/test_job_unit.py` 的 `test_cleanup_report_default_is_all_zero` 里补一行:

```python
    assert report.sandbox_egress_audit_deleted == 0
```

- [ ] **Step 2: 跑测试确认红**

```bash
uv run pytest services/retention-cleanup-job/tests/test_job_unit.py -k all_zero -v
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/retention-cleanup-job/tests/test_job_integration.py -k sandbox_egress -v
```
Expected: 两条都 FAILED(`CleanupReport` 没有该字段 / 构造不认这个 kwarg)

- [ ] **Step 3: 加设置项**

`settings.py` 的 tuning 段之后:

```python
    # --------------------------------------------------- 沙箱出网审计 (波 1 PR-E)
    # ``sandbox_egress_audit`` 每次沙箱出网写一行(``allowed`` 占绝大多数),
    # 此前没有任何清理路径。与上面几个窗口同样是 M0 全局旋钮、无 per-tenant
    # 覆盖。这张表是运维/安全遥测,不是 audit_log 那种合规 WORM 表,所以没有
    # ``backup_acked`` 之类的前置闸。
    sandbox_egress_audit_retention_days: int = Field(default=90, ge=1, le=3650)
```

- [ ] **Step 4: 加清理 pass**

`job.py` 的 `CleanupReport` 末尾加字段:

```python
    # 波 1 PR-E —— 沙箱出网审计的保留期清理。
    sandbox_egress_audit_deleted: int = 0
```

`__init__` 签名末尾加参数(照 `image_retention_days` 的位置与校验风格):

```python
        sandbox_egress_audit_retention_days: int = 90,
```

并在既有那串 `if ... < 1: raise ValueError` 校验之后补一条同形的:

```python
        if sandbox_egress_audit_retention_days < 1:
            msg = "sandbox_egress_audit_retention_days must be >= 1"
            raise ValueError(msg)
```

再在既有预取字段旁存一份:`self._sandbox_egress_audit_retention_days = sandbox_egress_audit_retention_days`(照本文件其余参数的存法,不要新造访问方式)。

新增方法(放在 `_delete_event_log` 之后,与它同形):

```python
    async def _delete_sandbox_egress_audit(self) -> int:
        """删掉过了保留期的沙箱出网审计行。

        ``ctid`` 子查询给 DELETE 限批(Postgres 不支持 ``DELETE ... LIMIT``),
        与本文件其他几个 pass 同款。全局窗口、无 per-tenant 覆盖 —— 同
        ``image_retention_days`` 的理由。表级 GRANT 见迁移 0143:这个 pass 以
        ``retention_cleanup_worker`` 角色执行,少了那条授权就是 permission denied。
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM sandbox_egress_audit
                    WHERE ctid IN (
                        SELECT a.ctid
                        FROM sandbox_egress_audit a
                        WHERE a.occurred_at < now() - (:days || ' days')::interval
                        LIMIT :batch
                    )
                    """
                ),
                {
                    "days": self._sandbox_egress_audit_retention_days,
                    "batch": self._batch_size,
                },
            )
            await session.commit()
        return result.rowcount or 0
```

`run_once` 里在 `event_deleted = ...` 之后加调用,并填进 `CleanupReport`:

```python
        sandbox_egress_audit_deleted = await self._delete_sandbox_egress_audit()
```

```python
            sandbox_egress_audit_deleted=sandbox_egress_audit_deleted,
```

- [ ] **Step 5: main.py 传参 + 日志行**

`main.py:91` 的 `RetentionCleanupJob(...)` 里补:

```python
            sandbox_egress_audit_retention_days=settings.sandbox_egress_audit_retention_days,
```

`main.py:110` 那条 `retention_cleanup_job.done ...`:格式串里加 `sandbox_egress_audit=%d`,参数列表对应位置加 `report.sandbox_egress_audit_deleted`。**格式串占位符与参数顺序必须一一对应** —— 这类日志错位不会报错,只会长期打印错数据。

- [ ] **Step 6: 跑测试确认绿**

```bash
uv run pytest services/retention-cleanup-job/tests -q -m "not integration"
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest services/retention-cleanup-job/tests -q -m integration
```
Expected: 全 passed

- [ ] **Step 7: 变异自验**

把 Step 4 的 SQL 谓词临时改成 `a.occurred_at < now() + (:days || ' days')::interval`(把过去改成未来),重跑集成用例 —— 必须变红(会变成删 2 行、剩 0 行)。确认后还原。

- [ ] **Step 8: 提交**

```bash
git add services/retention-cleanup-job/src/retention_cleanup_job/settings.py \
        services/retention-cleanup-job/src/retention_cleanup_job/job.py \
        services/retention-cleanup-job/src/retention_cleanup_job/main.py \
        services/retention-cleanup-job/tests/test_job_integration.py \
        services/retention-cleanup-job/tests/test_job_unit.py
git commit -m "feat(retention): sandbox_egress_audit 接进保留期清理——append-only 表不再只增不减"
```

---

## 终门(全部 task 完成后)

- [ ] `uv run ruff check .` + `uv run ruff format --check .`(ruff 跑全库,含 tests)
- [ ] CI-scope mypy:`uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src`
- [ ] `uv run pytest -m "not integration"` 全绿
- [ ] `DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest packages/expert-work-persistence/tests -m integration` 全绿
