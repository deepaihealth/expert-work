# 沙箱 exec 旗标 `-E -P` + 明文 HTTP proxy 认证(PR-C)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修沙箱两条出生缺陷:①`pip install` 装得上却 import 不到(`-I` 含 `-s` 把 user site 踢出 `sys.path`,与镜像 `PIP_USER=1` 流程矛盾)→ 两后端 exec 全部换 `-E -P`;②明文 HTTP 走 egress proxy 恒 407(stdlib `ProxyHandler` 只在 user **and** password 都非空才发 `Proxy-Authorization`,我们的 proxy URL 是 `http://<token>:@host` 空密码)→ sitecustomize 补 `proxy_open` patch。顺带 #9:runner 常量/旗标补漂移闸。

**Architecture:** 旗标语义:`-I` = `-E -P -s`(3.11+);换成 `-E -P` 只去掉 `-s`(user site 回到 `sys.path`),保住 `-E`(环境配置隔离;副作用=镜像声明的 `PYTHONUNBUFFERED` 等对子进程失效,记档不修)与 `-P`(脚本目录/cwd 不进 `sys.path`,防 `/tmp`、`/workspace` 里的模块遮蔽 stdlib)。两处改:docker 路径 `runner.py` 的 subprocess argv、云路径 `agent_sandbox.py` 的命令串 —— 旗标升单源常量 `SANDBOX_PYTHON_FLAGS` 进 `sandbox_image_contract.py`,ast 漂移闸钉 runner 侧副本。明文 HTTP:sitecustomize 现只 patch `http.client.HTTPConnection.set_tunnel`(CONNECT/HTTPS);补 patch `urllib.request.ProxyHandler.proxy_open` 给请求 setdefault 同一个 Basic 头(HTTPS 场景 stdlib `do_open` 会把该头搬进 tunnel headers,与 set_tunnel patch 的 setdefault 语义重叠无冲突)。

**Tech Stack:** Python(镜像 stdlib-only 文件 + orchestrator pytest 契约/漂移测试)。镜像重建/推 ACR/SandboxSet 换 tag 是 PR 内的部署步骤,由控制会话执行,不在 task 里。

## Global Constraints

- **旗标恰为 `-E -P`,不是 `-E`、不是 `-P`、不是 `-I`**:只 `-E` 会让脚本目录进 `sys.path`;纯 `-P` 会让 `PYTHONPATH`(`bash -l` source 的 profile 可注入)生效。已拍板。
- **`runner.py` / `sitecustomize.py` 保持 stdlib-only、自包含**(镜像文件不 import 仓库其它代码)。
- 明文 HTTP patch 必须 **setdefault 语义**(客户端已带 `Proxy-Authorization` 时不覆盖),与现 set_tunnel patch 一致;header 名拼写照 stdlib 自己的 `add_header('Proxy-authorization', ...)`。
- 契约测试新用例必须**两实现同用**(`runtime` fixture 参数化,`@pytest.mark.integration @pytest.mark.asyncio`),`thread_id` 接着现有 c1–c12 往下编。
- 漂移闸测试(非 integration)进 `test_sandbox_runtime_contract.py` 现有 `test_exec_contract_constants_match_the_sandbox_image` 一族,普通 pytest 就跑。
- 验证命令:根目录 `DOCKER_HOST= uv run pytest tests/test_sandbox_runner.py services/sandbox-supervisor/tests/test_egress_sitecustomize.py -q`;orchestrator `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_sandbox.py tests/test_sandbox_runtime_contract.py -q`(integration 用例无 env 自动 skip,本地跑的是漂移闸与单元部分)。
- 本仓 ruff select 不含 SLF001,别加多余 `# noqa`(RUF100 挂);ruff format 也在 CI(`ruff format --check`)。
- `docs/ITERATION-PLAN.md:1611` 的 `-I` 措辞是历史规划档,**刻意不改**。
- commit 遵循 conventional commits,不加 Co-Authored-By。

---

### Task 1: 镜像侧 —— runner 旗标 + sitecustomize 明文 HTTP + smoke 断言

**Files:**
- Modify: `infra/sandbox-image/runner.py:52-59`(subprocess argv)+ 模块 docstring 第 10 行附近措辞
- Modify: `infra/sandbox-image/sitecustomize.py`(docstring 全面更新 + 新 patch)
- Modify: `infra/sandbox-image/smoke_payload.py`(docstring :3-4 + 新用例)
- Modify: `infra/sandbox-image/Dockerfile`(`python -I -c` 注释行,约 :167-170)
- Test: `tests/test_sandbox_runner.py`(仓库根 tests/)
- Test: `services/sandbox-supervisor/tests/test_egress_sitecustomize.py`

**Interfaces:**
- Consumes: 现有 `_load_shim`/save-restore 测试模式(test_egress_sitecustomize.py,先读该文件再写);`runner.run_once` 单测直跑真子进程。
- Produces: runner argv = `[sys.executable, "-E", "-P", "-c", code]`(Task 2 的 ast 漂移闸解析此处);sitecustomize 新增 `ProxyHandler.proxy_open` patch(受同一个 `EXPERT_WORK_EGRESS_PROXY_AUTH` 门控)。

- [ ] **Step 1: 写失败测试 —— runner 子进程旗标**

`tests/test_sandbox_runner.py` 追加(照该文件现有 `_load_runner()` 已加载的 `runner` 模块引用方式):

```python
def test_run_once_child_flags_enable_user_site_and_safe_path() -> None:
    # PR-C — the child must run `-E -P`, NOT `-I`: `-I` implies `-s`, which
    # kicks the user site out of sys.path and silently breaks the image's
    # PIP_USER=1 on-demand install flow (installs succeed, imports fail).
    result = runner.run_once(
        "import sys; print(sys.flags.no_user_site, sys.flags.safe_path, "
        "sys.flags.ignore_environment, sys.flags.isolated)",
        10,
    )
    assert result["exit_code"] == 0
    # no_user_site=0 (user site ON), safe_path=1 (-P), ignore_environment=1 (-E),
    # isolated=0 (not -I).
    assert result["stdout"].strip() == "0 1 1 0"
```

Run: `DOCKER_HOST= uv run pytest tests/test_sandbox_runner.py -q`
Expected: 新用例 FAIL(现为 `-I`:输出 `1 1 1 1`),其余 14 条 PASS。

- [ ] **Step 2: 改 runner.py argv**

`runner.py:53-54` 改为:

```python
        proc = subprocess.run(  # noqa: S603 - arbitrary code execution is the tool
            # -E -P, deliberately NOT -I: -I implies -s, which kicks the user
            # site out of sys.path and silently breaks `pip install --user`
            # (the image's PIP_USER=1 flow). -E keeps PYTHON* env-config
            # isolation; -P keeps the script dir / cwd off sys.path.
            [sys.executable, "-E", "-P", "-c", code],
```

模块 docstring 第 10-11 行 `runs in a *child* ``python -c`` process` 的段落不用改(没提旗标)。

Run: `DOCKER_HOST= uv run pytest tests/test_sandbox_runner.py -q`
Expected: 全 PASS。

- [ ] **Step 3: 写失败测试 —— sitecustomize 明文 HTTP**

先读 `services/sandbox-supervisor/tests/test_egress_sitecustomize.py` 现有三条用例的加载/还原模式(shim 按路径 load、全局 patch 测完还原),照同一模式追加两条。要点:在 load shim **之前**把 `urllib.request.ProxyHandler.proxy_open` 换成记录用 stub(shim load 时捕获的 `_orig` 就是 stub),测完 finally 还原真原函数:

```python
def test_shim_adds_proxy_auth_to_plain_http_proxy_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR-C — plain-HTTP requests never reach set_tunnel; stdlib's own
    # proxy_open only sends the header when the proxy URL carries BOTH a
    # user and a password (`if user and password`), and ours is
    # `http://<token>:@host` (empty password) → 407 without this patch.
    import urllib.request

    orig = urllib.request.ProxyHandler.proxy_open
    calls: list[object] = []

    def _stub(self, req, proxy, type):  # noqa: ANN001, ANN202
        calls.append(req)
        return None

    urllib.request.ProxyHandler.proxy_open = _stub
    try:
        _load_shim_with_auth(monkeypatch)  # 用该文件现有的"带 EXPERT_WORK_EGRESS_PROXY_AUTH load"入口
        req = urllib.request.Request("http://example.com/path")
        urllib.request.ProxyHandler({"http": "http://proxy:8081"}).proxy_open(req, "http://proxy:8081", "http")
        assert calls, "patched proxy_open must delegate to the original"
        assert req.get_header("Proxy-authorization", "").startswith("Basic ")
    finally:
        urllib.request.ProxyHandler.proxy_open = orig


def test_shim_preserves_client_auth_on_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    orig = urllib.request.ProxyHandler.proxy_open
    urllib.request.ProxyHandler.proxy_open = lambda self, req, proxy, type: None
    try:
        _load_shim_with_auth(monkeypatch)
        req = urllib.request.Request("http://example.com/path")
        req.add_header("Proxy-authorization", "Basic client-own")
        urllib.request.ProxyHandler({"http": "http://proxy:8081"}).proxy_open(req, "http://proxy:8081", "http")
        assert req.get_header("Proxy-authorization") == "Basic client-own"
    finally:
        urllib.request.ProxyHandler.proxy_open = orig
```

(`_load_shim_with_auth` 是占位名 —— 用该文件真实的加载 helper/固定模式;无 helper 就照现有用例逐行复刻。断言语义不变。)

Run: `DOCKER_HOST= uv run pytest services/sandbox-supervisor/tests/test_egress_sitecustomize.py -q`
Expected: 两条新用例 FAIL(shim 未 patch proxy_open,header 缺失),现有三条 PASS。

- [ ] **Step 4: 实现 sitecustomize patch + docstring**

`infra/sandbox-image/sitecustomize.py` 的 `if _AUTH:` 块末尾追加:

```python
    import urllib.request

    _orig_proxy_open = urllib.request.ProxyHandler.proxy_open

    def _proxy_open(self, req, proxy, type):  # type: ignore[no-untyped-def]
        # Plain-HTTP requests through the proxy never reach set_tunnel, and
        # stdlib's own proxy_open only sends Proxy-Authorization when the
        # proxy URL carries BOTH a user and a password (`if user and
        # password:`) — ours is `http://<token>:@host` (empty password), so
        # stdlib drops the credential and the proxy answers 407. Mirror
        # stdlib's own header spelling; never override a client's own value.
        # For https:// URLs do_open later migrates this header into the
        # CONNECT tunnel headers — harmless overlap with the set_tunnel
        # patch above (both are setdefault-shaped).
        if not req.has_header("Proxy-authorization"):
            req.add_header("Proxy-authorization", _PROXY_AUTH_HEADER)
        return _orig_proxy_open(self, req, proxy, type)

    urllib.request.ProxyHandler.proxy_open = _proxy_open
```

docstring 三处更新:
1. 首行 `on HTTPS ``CONNECT``` → `on both HTTPS ``CONNECT`` and plain-HTTP proxying`。
2. "What it does" 段末尾补一句:`A second patch on ``urllib.request.ProxyHandler.proxy_open`` does the same for plain-``http://`` requests, which never reach ``set_tunnel`` — stdlib only sends the header itself when the proxy URL has both a user and a non-empty password.`
3. "Loading" 段(现 :23-31,写的是 `-I` 语义)整段改为:

```
Loading
-------
Python auto-imports ``sitecustomize`` from the global site-packages at startup.
Both sandbox runners execute submitted code via ``python -E -P`` (the docker
runner's ``-c`` child and the cloud backend's script file — PR-C; formerly
``-I``, whose implied ``-s`` also broke ``pip install --user``): ``-E`` only
suppresses ``PYTHON*`` config env, so ``EXPERT_WORK_EGRESS_PROXY_AUTH`` is
still readable, and neither flag is ``-S``/``-s``, so the ``site`` module
still imports this module from the global site-packages and the *user* site
stays on ``sys.path``.
```

Run: `DOCKER_HOST= uv run pytest services/sandbox-supervisor/tests/test_egress_sitecustomize.py -q`
Expected: 5/5 PASS。

- [ ] **Step 5: smoke payload 断言 + 注释修正**

`infra/sandbox-image/smoke_payload.py`:
1. docstring :3-4 的 `python -I -c` 措辞改为 `python -E -P -c`。
2. 在最终 `print("OK")` 之前、`:109` import-only 检查附近追加:

```python
# PR-C — the runner must exec children with -E -P (user site ON, safe path ON).
import sys

if sys.flags.no_user_site or sys.flags.isolated:
    raise RuntimeError(f"user site disabled in exec child (flags={sys.flags})")
if not sys.flags.safe_path:
    raise RuntimeError("safe_path (-P) missing — script dir / cwd would shadow stdlib")
```

3. `infra/sandbox-image/Dockerfile` 约 :167-170 注释里的 `python -I -c` 措辞同步改 `python -E -P -c`;`ENV PIP_USER=1` 上方那段注释(约 :151-154 "the user-site is on sys.path automatically")现在重新为真,不动。

(smoke 真跑要 docker build 2.4GB 镜像,本地不跑 —— PR CI 的 sandbox-image workflow 会 build+smoke。)

- [ ] **Step 6: Commit**

```bash
git add infra/sandbox-image/runner.py infra/sandbox-image/sitecustomize.py infra/sandbox-image/smoke_payload.py infra/sandbox-image/Dockerfile tests/test_sandbox_runner.py services/sandbox-supervisor/tests/test_egress_sitecustomize.py
git commit -m "fix(sandbox-image): exec 子进程 -I 换 -E -P(user site 复活)+ 明文 HTTP proxy 认证(PR-C)"
```

---

### Task 2: orchestrator 侧 —— 命令串换旗标 + 单源常量 + 漂移闸 + 契约用例

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py`(新常量)
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(:817 命令串、:780/:786 docstring)
- Modify: `services/orchestrator/tests/test_agent_sandbox.py`(:847 docstring、:867 断言)
- Modify/Test: `services/orchestrator/tests/test_sandbox_runtime_contract.py`(漂移闸扩展 + 3 条契约用例 + :362-363 docstring)

**Interfaces:**
- Consumes: Task 1 的 runner argv 形状 `[sys.executable, "-E", "-P", "-c", code]`(ast 闸解析对象);现有 `_runner_py_constants()`(只收 int 常量)、`runtime` fixture(两实现参数化,integration env 门控)。
- Produces: `SANDBOX_PYTHON_FLAGS: tuple[str, ...] = ("-E", "-P")`(sandbox_image_contract.py 导出,agent_sandbox.py 消费)。

- [ ] **Step 1: 写失败测试 —— 漂移闸扩展**

`test_sandbox_runtime_contract.py`:新 helper + 扩展现有 `test_exec_contract_constants_match_the_sandbox_image`(:412):

```python
def _runner_py_exec_flags() -> list[str]:
    """ast 抠 runner.py subprocess argv 里 sys.executable 与 "-c" 之间的旗标。"""
    path = _RUNNER_PY  # 复用 _runner_py_constants() 用的那个路径常量/表达式
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        if isinstance(head, ast.Attribute) and head.attr == "executable":
            flags = []
            for elt in node.elts[1:]:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                    break
                if elt.value == "-c":
                    return flags
                flags.append(elt.value)
    raise AssertionError("runner.py 的 subprocess argv([sys.executable, ..., '-c', code])没找到")
```

现有 :412 测试体内追加两条断言(import 处补 `SANDBOX_PYTHON_FLAGS`):

```python
    # PR-C #9 — runner 的 MAX_TIMEOUT_S 此前没有闸钉着,改一边就静默分叉。
    assert MAX_TIMEOUT_S == runner["MAX_TIMEOUT_S"], (
        f"MAX_TIMEOUT_S 漂移:contract={MAX_TIMEOUT_S} runner.py={runner['MAX_TIMEOUT_S']}"
    )
    # PR-C #2 — 解释器旗标单源:runner argv 必须与 SANDBOX_PYTHON_FLAGS 一致。
    assert _runner_py_exec_flags() == list(SANDBOX_PYTHON_FLAGS), (
        f"exec 旗标漂移:runner.py={_runner_py_exec_flags()} contract={list(SANDBOX_PYTHON_FLAGS)}"
    )
```

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_sandbox_runtime_contract.py -q -k constants`
Expected: FAIL(`SANDBOX_PYTHON_FLAGS` ImportError)。

- [ ] **Step 2: 加常量 + 换命令串**

`sandbox_image_contract.py` 常量块(:67-82 附近)追加:

```python
#: 解释器旗标 —— 两后端 exec 子进程共用(PR-C)。刻意是 ``-E -P`` 而非
#: ``-I``:``-I`` 隐含 ``-s``,把 user site 踢出 ``sys.path``,静默弄坏镜像
#: ``PIP_USER=1`` 的按需安装流(装得上、import 不到)。``-E`` 保住 PYTHON*
#: 环境配置隔离(副作用:镜像声明的 PYTHONUNBUFFERED / PYTHONDONTWRITEBYTECODE
#: 对子进程失效 —— 一直如此,记档不修);``-P`` 保住"脚本目录 / cwd 不进
#: sys.path"(防 /tmp、/workspace 落的文件遮蔽 stdlib)。对家是 ``runner.py``
#: 的 subprocess argv,闸在 test_exec_contract_constants_match_the_sandbox_image。
SANDBOX_PYTHON_FLAGS: tuple[str, ...] = ("-E", "-P")
```

`agent_sandbox.py`:
1. import 块补 `SANDBOX_PYTHON_FLAGS`(:113 那组)。
2. :817 改:

```python
            result = await sbx.commands.run(
                f"python {' '.join(SANDBOX_PYTHON_FLAGS)} {script}",
```

3. docstring :780 `再 ``python -I <path>`` 执行` → `再 ``python -E -P <path>``(:data:`SANDBOX_PYTHON_FLAGS`)执行`;:786 `subprocess.run([sys.executable, "-I", "-c", code], ...)` → `subprocess.run([sys.executable, "-E", "-P", "-c", code], ...)`。

`test_agent_sandbox.py`:
- :867 `assert "python -I " in cmd` → `assert "python -E -P " in cmd`
- :847 docstring 里的 `-I` 措辞同步。

`test_sandbox_runtime_contract.py:362-363` docstring `全新的 ``python -I`` 子进程` → `全新的 ``python -E -P`` 子进程`。

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_sandbox_runtime_contract.py tests/test_agent_sandbox.py -q`
Expected: 全 PASS(integration 无 env 自动 skip)。

- [ ] **Step 3: 写契约用例三条(integration,两实现)**

`test_sandbox_runtime_contract.py` integration 段追加(thread_id 接 c13/c14/c15;每条照 `test_exec_sees_the_image_environment` 的 acquire/try/finally destroy 形状):

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_user_site_survives_to_the_next_exec(runtime: SandboxRuntime) -> None:
    """PR-C #2 —— user site 必须在 exec 子进程的 ``sys.path`` 上。

    第一步把模块文件落进 ``site.getusersitepackages()``(镜像 HOME=/workspace,
    可写),第二步全新子进程 import 它 —— 等价于「pip install --user 之后
    下一次 exec import 得到」,但不依赖网络。旧旗标 ``-I``(含 ``-s``)下
    第二步必失败。
    """
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c13")
    try:
        seeded = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import pathlib, site\n"
                "d = pathlib.Path(site.getusersitepackages())\n"
                "d.mkdir(parents=True, exist_ok=True)\n"
                "(d / 'ew_contract_usersite.py').write_text(\"MARK = 'usersite-ok'\")\n"
                "print('seeded', d)\n"
            ),
            timeout_s=30,
        )
        assert seeded.exit_code == 0, seeded.stderr
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import ew_contract_usersite; print(ew_contract_usersite.MARK)",
            timeout_s=30,
        )
        assert outcome.exit_code == 0, outcome.stderr
        assert "usersite-ok" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_sys_path_excludes_cwd_and_script_dir(runtime: SandboxRuntime) -> None:
    """PR-C #2 —— ``-P``:cwd(supervisor 的 ``-c`` 模式)与脚本目录
    (云后端的 /tmp 脚本模式)都不得进 ``sys.path``,否则 LLM 落在
    /workspace 或 /tmp 的文件会遮蔽 stdlib。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c14")
    try:
        outcome = await runtime.exec(
            sandbox_id=sid, code="import sys; print(repr(sys.path))", timeout_s=30
        )
        assert outcome.exit_code == 0, outcome.stderr
        paths = ast.literal_eval(outcome.stdout.strip())
        assert "" not in paths, paths
        assert "/tmp" not in paths, paths
        assert "/workspace" not in paths, paths
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exec_pip_user_install_then_import(runtime: SandboxRuntime) -> None:
    """PR-C #2 的端到端本尊:``pip install --user`` 之后,下一次 exec 的
    全新子进程要 import 得到。选 sortedcontainers(纯 py、无依赖、镜像
    requirements 未收);第一步先断言它当前 import 不到,防镜像哪天把它
    收编后本用例退化成空转。走真网络,超时给足。"""
    sid = await runtime.acquire(tenant_id=uuid4(), thread_id="c15")
    try:
        installed = await runtime.exec(
            sandbox_id=sid,
            code=(
                "import importlib.util, subprocess, sys\n"
                "assert importlib.util.find_spec('sortedcontainers') is None, "
                "'already baked into the image — pick another probe package'\n"
                "r = subprocess.run([sys.executable, '-m', 'pip', 'install', '--user',\n"
                "                    '--quiet', '--no-input', 'sortedcontainers==2.4.0'])\n"
                "print('pip-rc', r.returncode)\n"
            ),
            timeout_s=240,
        )
        assert installed.exit_code == 0, installed.stderr
        assert "pip-rc 0" in installed.stdout, installed.stdout
        outcome = await runtime.exec(
            sandbox_id=sid,
            code="import sortedcontainers; print('import-ok', sortedcontainers.__version__)",
            timeout_s=30,
        )
        assert outcome.exit_code == 0, outcome.stderr
        assert "import-ok 2.4.0" in outcome.stdout
    finally:
        await runtime.destroy(sandbox_id=sid, reason="contract-test")
```

前置核实:`grep -i sortedcontainers infra/sandbox-image/requirements.txt` 应为空;若被收编换一个纯 py 小包。文件顶部若无 `import ast` 补上。

- [ ] **Step 4: 本地全量验证**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_sandbox_runtime_contract.py tests/test_agent_sandbox.py -q`(integration skip)
Run: 根目录 `DOCKER_HOST= uv run pytest tests/test_sandbox_runner.py -q`(确认 Task 1 未被本 task 弄红)
Expected: 全 PASS。真 integration 三条由 PR 的 sandbox-contract CI(agent_sandbox 参数)对测试集群跑。

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py services/orchestrator/src/orchestrator/tools/agent_sandbox.py services/orchestrator/tests/test_agent_sandbox.py services/orchestrator/tests/test_sandbox_runtime_contract.py
git commit -m "fix(sandbox): exec 命令串 -I 换 -E -P 单源常量+漂移闸+user-site/pip 契约用例(PR-C)"
```

---

### Task 3: live 验证脚本明文 HTTP phase + 设计文档措辞

**Files:**
- Modify: `tools/eval/verify_live_egress.py`(新 phase + `_amain` 接线)
- Modify: `docs/design/sandbox-egress-per-agent.md:190-193`(stdlib 措辞段)

**Interfaces:**
- Consumes: 现有 `_gated_exec` / `_tool_text_has` / `_find_audit_row` / `_exec_prompt` 与 `phase_allowed`(:331-352)的形状;probe 常量的 base64 + no-redirect 手法(:258-293)。
- Produces: `phase_plain_http`,在 `_amain`(:393-395)接在 phase_allowed 之后。

- [ ] **Step 1: 加 probe 常量与 phase**

照 `_ALLOWED_CODE` 的形状加(base64 串是 `http://1.1.1.1/cdn-cgi/trace`;沿用同一 `_NoRedirect` 手法 —— probe 代码块整体复制 `_ALLOWED_CODE` 再换 URL 常量即可):

```python
#: 明文 HTTP —— PR-C #4:stdlib 对空密码 proxy URL 不发 Proxy-Authorization,
#: 修复前这条恒 407(HTTPS CONNECT 通是因为 sitecustomize 只 patch 了 set_tunnel)。
_PLAIN_HTTP_CODE = _ALLOWED_CODE.replace(
    "aHR0cHM6Ly8xLjEuMS4xL2Nkbi1jZ2kvdHJhY2U=",
    "aHR0cDovLzEuMS4xLjEvY2RuLWNnaS90cmFjZQ==",
)


async def phase_plain_http(client: httpx.AsyncClient, *, name: str, version: str) -> bool:
    print(f"\n[phase 3] plain-HTTP egress — sandbox → http://{_ALLOWED_HOST} via the audited proxy")
    tr = await _gated_exec(
        client, name=name, version=version, prompt=_exec_prompt(_PLAIN_HTTP_CODE), label="plain-http"
    )
    if tr is None:
        return False
    if not _tool_text_has(tr, "EGRESS_RESULT", "'ok': True"):
        print("  FAIL — the plain-HTTP request did not succeed from the sandbox "
              "(PR-C 前的已知形态:stdlib 不发 Proxy-Authorization → 407).")
        return False
    row = await _find_audit_row(client, host=_ALLOWED_HOST, verdict="allowed")
    ...
```

(`...` 处照 `phase_allowed` 的 audit 断言/打印收尾逐行同构;若 `_find_audit_row` 支持 port 参数则钉 80,不支持就不钉。)`_amain` 里接在 phase_allowed 之后,计入总 verdict;若有 `--allowed-only` 类 CLI 分支,同样处理。

- [ ] **Step 2: 设计文档措辞**

`docs/design/sandbox-egress-per-agent.md:190-193` 讲 stdlib 丢 userinfo 只到 CONNECT 的那段,补明文 HTTP 一句:sitecustomize 现同时 patch `set_tunnel`(CONNECT)与 `ProxyHandler.proxy_open`(明文 HTTP),两处都是 setdefault 语义。

- [ ] **Step 3: 静态验证 + Commit**

Run: `uv run ruff check tools/eval/verify_live_egress.py && uv run ruff format --check tools/eval/verify_live_egress.py`
Expected: clean(脚本是手动 live 工具,真跑放部署步骤)。

```bash
git add tools/eval/verify_live_egress.py docs/design/sandbox-egress-per-agent.md
git commit -m "feat(eval): verify_live_egress 加明文 HTTP phase + egress 设计文档措辞(PR-C)"
```

---

## 部署步骤(控制会话执行,不派 task)

1. Task 1-3 全过审后:本地 `docker build infra/sandbox-image -t <ACR>/expert-work/sandbox:<tag>`(tag=镜像内容终版 commit 短 sha),push ACR(需 ACR 凭据;`--platform linux/amd64`)。
2. `infra/k8s/sandbox/sandboxset.yaml:90` image tag 换新,commit 进本 PR;`kubectl apply -f infra/k8s/sandbox/sandboxset.yaml`(KUBECONFIG=~/.kube/expert-work-test.yaml),等 pool Ready。
3. 重跑 PR 的 sandbox-contract workflow(新镜像 + 新用例全绿才算)。
4. `tools/eval/verify_live_egress.py` 对测试栈跑通 phase 1/2/3(#4 的唯一真验证)。
5. CI 全绿 → 等用户合并;merge 后 sandbox-image.yml 会再自动 build(push 步骤视 ALIYUN_ACR_* vars 配置而定,不依赖它)。
